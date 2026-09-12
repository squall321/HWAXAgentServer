# 붙인 문서 주입(_doc_block) 회귀 테스트 — 예산은 건수로 나누고, 자른 건 잘랐다고 말한다
#
#   실행:  .venv/bin/python -m pytest tests/test_doc_attach.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_context():
    """모델 컨텍스트를 고정한다.

    안 고정하면 이 파일의 결과가 **그 박스에 떠 있는 모델**에 따라 달라진다(dev 16K ·
    운영 GLM 128K). 예산 로직을 시험하는 것이지 그 박스의 모델을 시험하는 게 아니다.
    """
    saved = dict(app._ctx_cache)
    app._ctx_cache["n"] = 128000
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)


def _doc(name, chars):
    return {"name": name, "text": "가" * chars}


def _budget(docs):
    return app._doc_total_chars(docs)


def test_문서_예산은_모델_컨텍스트에서_나온다():
    """고정 숫자로 두면 박스마다(dev 16K · 운영 GLM 128K · Opus 200K~1M) 한쪽이 반드시
    틀리고, 틀리면 400 이라 사용자에게는 '응답 생성 실패' 로만 보인다 — 실측으로 터졌다."""
    ko = [{"text": "한글 본문입니다. " * 100}]
    en = [{"text": "English body text here. " * 100}]
    assert app._doc_budget_tokens(0) == int((128000 - app.DOC_RESERVE_TOKENS) * app.DOC_SAFETY)
    # 같은 토큰 예산이라도 글자 수는 글 구성에 따라 다르다 — 한 값으로 뭉뚱그리면
    # 한국어에서 400 이 나거나 영문에서 창의 3분의 1만 쓴다.
    assert _budget(en) > _budget(ko) * 2, "영문/한국어 토큰 밀도 차이가 반영 안 됐다"


def test_큰_컨텍스트일수록_문서에_더_준다():
    """비율 배분이면 1M 창에서 절반을 놀린다. 고정 오버헤드는 창에 비례하지 않는다."""
    got = {}
    for ctx in (16384, 128000, 200000, 1000000):
        app._ctx_cache["n"] = ctx
        got[ctx] = app._doc_budget_tokens(0)
    assert got[16384] < got[128000] < got[200000] < got[1000000]
    # 큰 창에서는 예비분만 떼고 거의 다 준다(비율 배분이면 여기서 절반이 날아간다).
    assert got[1000000] >= 1000000 * 0.95


def test_긴_대화_중이면_문서_예산이_줄어든다():
    """이력은 최대 200,000자까지 실려 문서와 같은 창을 다툰다. 고정값으로 빼면 긴 대화
    중에 문서를 붙이는 순간 400 이 난다."""
    none_ = app._doc_budget_tokens(0)
    some = app._doc_budget_tokens(40000)
    assert some == pytest.approx(none_ - 40000 * app.DOC_SAFETY, abs=2)
    # 이력이 창을 다 먹었으면 **없는 자리를 있다고 하지 않는다**(바닥값이 그 버그였다).
    assert app._doc_budget_tokens(500000) <= 1500


def test_붙인_문서가_없으면_빈_문자열():
    assert app._doc_block(None) == ""
    assert app._doc_block([]) == ""


def test_글자_없는_항목은_버린다():
    """빈 문서를 근거라고 실으면 좌석·모델이 '자료는 있는데 내용이 없다' 로 읽는다."""
    assert app._doc_block([{"name": "빈것", "text": "   "}]) == ""
    assert app._doc_block(["문자열은 문서가 아니다"]) == ""


def test_상한_안이면_본문이_그대로_실린다():
    n = min(5000, _budget([_doc("x", 5000)]) - 100)
    out = app._doc_block([_doc("설계검토.hwax.md", n)])
    assert "가" * n in out
    assert "설계검토.hwax.md" in out
    # 자름 표시는 **문서 머리말**에만 나와야 한다. 규율 문구에도 같은 말이 나오므로
    # 전체 문자열로 보면 안 된다(이 테스트가 처음에 거기서 틀렸다).
    assert ", 이 중 앞" not in out


def test_예산은_건수로_균등_분배된다():
    """앞 문서가 예산을 다 먹으면 사용자는 두 건을 붙였는데 답은 한 건만 본 채로 나온다.

    그 실패는 화면에 아무 흔적도 안 남는다 — 그래서 코드가 몫을 나눈다."""
    docs = [_doc("앞.md", 200000), _doc("뒤.md", 200000)]
    out = app._doc_block(docs)
    assert "앞.md" in out and "뒤.md" in out, "뒤 문서가 통째로 사라졌다"
    share = _budget(docs) // 2
    # 두 문서 모두 같은 몫만큼 실린다(한쪽이 예산을 다 먹지 않는다).
    assert out.count("가" * share) == 2, f"몫({share:,}자)이 균등 분배되지 않았다"


def test_잘랐으면_잘랐다고_적는다():
    """모델이 '뒷부분을 못 봤다'는 걸 알아야 지어내지 않고 되물을 수 있다."""
    n = _budget([_doc("x", 1)]) + 1000
    out = app._doc_block([_doc("긴문서.md", n)])
    assert "만 실림" in out
    assert f"{n:,}자" in out   # 원문 길이를 밝힌다


def test_건수_상한을_넘으면_자른다():
    out = app._doc_block([_doc(f"d{i}.md", 100) for i in range(app.DOC_MAX_FILES + 3)])
    assert f"d{app.DOC_MAX_FILES - 1}.md" in out
    assert f"d{app.DOC_MAX_FILES}.md" not in out


def test_근거_규율이_함께_실린다():
    """문서만 주고 규율을 안 주면 모델이 기억으로 메운다 — 이 허브에서 가장 위험한 실패다."""
    out = app._doc_block([_doc("a.md", 10)])
    assert "여기 적힌 것만" in out          # 기억으로 채우지 마라
    assert "지어내지 마라" in out           # 못 본 구간을 만들어내지 마라
    assert "어긋난다고 말하라" in out       # 사전 지식과 충돌하면 조용히 덮지 마라


def test_핸드오프_발췌가_너무_짧지_않다():
    """예산만 올리고 발췌 길이를 안 올리면 예산은 크고 실제로 실리는 건 그대로다 —
    종전이 그 상태였다(11KB 예산에 220자×12건 ≈ 2.6KB 만 도착).

    예산과의 '곱셈 일치' 는 더 이상 걸지 않는다. 근거 예산은 이제 **붙인 문서**도 채우므로
    도구 발췌만으로 예산을 메워야 할 이유가 없다. 회귀 방어선만 남긴다."""
    assert app.HANDOFF_RESULT_CHARS >= 4000, (
        f"발췌가 {app.HANDOFF_RESULT_CHARS}자로 줄었다 — 표의 첫 줄만 심의에 간다"
    )


def test_문서_건수_상한이_세_곳에서_같다():
    """프론트가 더 많이 붙이게 하면 포털이 422 를 낸다(문서 글자 상한과 같은 함정)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "HWAXPortal"
    front = re.search(r"^export const DOC_MAX\s*=\s*(\d+)",
                      (root / "frontend/src/components/chat/docAttach.ts").read_text(encoding="utf-8"),
                      re.M)
    portal = re.search(r"documents: list\[ChatDocument\] \| None = Field\(default=None, max_length=(\d+)\)",
                       (root / "backend/app/agent/routes.py").read_text(encoding="utf-8"))
    assert front and portal, "상한 선언을 못 찾았다 — 이름이 바뀌었으면 이 테스트도 고쳐라"
    assert int(front.group(1)) == int(portal.group(1)) == app.DOC_MAX_FILES, (
        f"문서 건수 불일치 — 프론트 {front.group(1)} · 포털 {portal.group(1)} · "
        f"엔진 {app.DOC_MAX_FILES}"
    )


def test_문서가_없으면_이력이_창을_더_쓴다(monkeypatch):
    """문서를 안 붙인 턴에 창을 놀릴 이유가 없다 — 종전엔 문서 유무와 무관하게 40% 만 썼다."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    app._ctx_cache["n"] = 1000000
    solo = app._hist_budget_chars(has_docs=False)
    with_docs = app._hist_budget_chars(has_docs=True)
    assert solo > with_docs * 1.8, f"문서 없을 때 {solo:,}자 / 있을 때 {with_docs:,}자"


# ── 긴 문서 맞추기(fit_document) — 앞에서 자르면 결론이 날아간다 ────────────────────
def _deck(n_slides: int, per: int = 1200) -> str:
    """n장짜리 발표자료. **결론은 맨 뒤 슬라이드에 있다**(현실이 그렇다)."""
    out = ["---\nschema: hwax-doc/1\nkind: ppt\n---\n\n# 대형_발표자료\n"]
    for i in range(1, n_slides + 1):
        body = "결론: 구리 두께를 12um 로 낮춘다." if i == n_slides else ("본문 " * (per // 3))
        out.append(f"## [s.{i}] 제목{i}\n\n{body}")
    return "\n\n".join(out)


def test_예산_안이면_한_글자도_안_바뀐다():
    import evidence

    deck = _deck(5)
    body, note = evidence.fit_document(deck, 10**7)
    assert body == deck and note == ""


def test_넘치면_앞뒤를_남기고_가운데를_덜어낸다():
    """이 테스트가 이 기능의 존재 이유다 — 앞에서 잘랐다면 결론이 사라진다."""
    import evidence

    deck = _deck(120)
    budget = len(deck) // 3
    body, note = evidence.fit_document(deck, budget)

    assert len(body) <= budget + 500          # 안내 문구 몫만 여유
    assert "## [s.1]" in body, "첫 낱장이 사라졌다"
    assert "결론: 구리 두께" in body, "마지막 낱장(결론)이 사라졌다 — 앞에서 자른 것과 같다"
    assert "실리지 않았다" in body, "빠진 구간을 모델에게 알리지 않았다"
    assert "~" in note, f"어느 낱장이 빠졌는지 안 밝혔다: {note!r}"
    # 종전 방식(앞에서 절단)과 대조 — 같은 예산에서 결론이 없다.
    assert "결론: 구리 두께" not in deck[:budget]


def test_낱장_경계에서만_자른다():
    """슬라이드가 반토막 나면 문장이 끊긴 채로 근거가 된다."""
    import evidence

    deck = _deck(60)
    body, _ = evidence.fit_document(deck, len(deck) // 4)
    head, tail = body.split("[⋯ 가운데", 1)
    # 앞부분은 완결된 낱장으로 끝난다(다음 낱장 표지 직전에서 끊겼다).
    assert head.rstrip().endswith("본문") or head.rstrip().endswith("본문 "), head[-40:]


def test_낱장_표지가_없으면_글자수로_자르고_그렇다고_말한다():
    import evidence

    body, note = evidence.fit_document("가" * 5000, 1000)
    assert len(body) == 1000
    assert "낱장 표지가 없어" in note


def test_심의_근거도_같은_방식으로_맞춘다():
    """챗과 심의가 같은 함수를 쓴다 — 한쪽만 고치면 같은 문서가 화면마다 다르게 잘린다."""
    import deliberation as d

    deck = _deck(200)
    assert len(deck) > d._EVID_ITEM_MAX, "시험용 문서가 상한보다 작아 의미가 없다"
    opts = d._resolve_opts({"evidence": [{"source": "업로드 문서", "result": deck}]})
    kept = opts.evidence[0]["result"]
    assert "결론: 구리 두께" in kept, "심의 근거에서 결론이 날아갔다"
    assert "원문" in kept and "낱장이 빠짐" in kept, "심의 쪽에 빠진 구간 표시가 없다"


# ── 심의 근거 예산도 컨텍스트를 넘으면 안 된다 ──────────────────────────────────────
# 챗은 400 이 나면 그 발화 하나가 죽지만, 심의는 좌석이 동시에 돌아 **전원이 같이 죽는다**.
def test_좌석_프롬프트가_컨텍스트를_넘지_않는다():
    """근거 천장(160,000자)만 보면 좌석 프롬프트가 GLM 128K 를 넘는다 — 실측 198,000토큰."""
    import deliberation as d

    for ctx in (128000, 200000, 1000000):
        app._ctx_cache["n"] = ctx
        d._evid_cache.clear()
        worst = d._evid_budget() + d._SEAT_CTX       # 근거 + 직전 라운드(한국어 최악)
        assert app._est_tokens("가" * worst) < ctx, (
            f"컨텍스트 {ctx:,}토큰인데 좌석 프롬프트만 "
            f"{app._est_tokens('가' * worst):,}토큰이다 — 라운드가 통째로 400 이 난다"
        )
    d._evid_cache.clear()


def test_심의_예산도_큰_컨텍스트에서_더_준다():
    import deliberation as d

    got = {}
    for ctx in (128000, 200000):
        app._ctx_cache["n"] = ctx
        d._evid_cache.clear()
        got[ctx] = d._evid_budget()
    assert got[200000] > got[128000]
    assert got[200000] <= d._EVID_BUDGET, "천장을 넘었다"
    d._evid_cache.clear()


def test_env_는_낮추기만_한다(monkeypatch):
    """낡은 .env 한 줄이 컨텍스트를 조용히 넘겨 400 을 만들면 안 된다 — 실측으로 그렇게
    터졌다(dev .env 의 HIST_BUDGET=16000 이 16,384 창을 넘겼다).
    탐지가 틀려 창을 **크게** 잡아야 하면 그건 LLM_CONTEXT_TOKENS 로 고칠 일이다."""
    import importlib

    app._ctx_cache["n"] = 128000
    # 낮추는 쪽 — 그대로 먹힌다.
    monkeypatch.setenv("DELIB_EVID_BUDGET", "5000")
    import deliberation as d

    importlib.reload(d)
    d._evid_cache.clear()
    assert d._evid_budget() == 5000

    # 올리는 쪽 — 컨텍스트가 이긴다.
    monkeypatch.setenv("DELIB_EVID_BUDGET", "9999999")
    importlib.reload(d)
    d._evid_cache.clear()
    assert d._evid_budget() < 9999999

    monkeypatch.delenv("DELIB_EVID_BUDGET")
    importlib.reload(d)
    d._evid_cache.clear()


def test_이력_env_도_낮추기만_한다(monkeypatch):
    app._ctx_cache["n"] = 16384
    monkeypatch.setenv("HIST_BUDGET", "200000")     # 창보다 훨씬 큰 낡은 설정
    big = app._hist_budget_chars()
    monkeypatch.delenv("HIST_BUDGET")
    assert big == app._hist_budget_chars(), "env 가 컨텍스트를 넘겨 올렸다"


# ── 이력 압축 — 오래된 턴을 버리지 않는다 ──────────────────────────────────────────
def _hist(n: int, each: int = 3000):
    out = [{"role": "user", "content": "이번 과제는 폴더블 힌지 FPCB 굽힘 수명 20만회다."}]
    for i in range(2, n + 1):
        out.append({"role": "user" if i % 2 else "assistant",
                    "content": f"턴 {i}. " + ("설명 문장이 이어집니다. " * (each // 12))})
    return out


def test_예산_안이면_전부_원문이다():
    st = {}
    msgs = app._history_messages(_hist(4, 500), st)
    assert st["compacted"] == 0 and len(msgs) == 4


def test_넘치면_버리지_않고_압축해_넣는다():
    """종전에는 오래된 턴을 통째로 버렸다 — 모델은 그런 대화가 있었다는 것조차 몰랐고,
    사용자는 '아까 말했잖아' 가 왜 안 통하는지 알 수 없었다."""
    hist = _hist(40)
    st = {}
    msgs = app._history_messages(hist, st)

    assert st["compacted"] > 0, "이 시험용 이력이 예산을 안 넘는다 — 시험이 무의미하다"
    assert st["kept"] > 0, "최근 턴까지 압축되면 대화가 끊긴다"
    digest = msgs[0][1]
    assert "이전 대화" in digest and "압축" in digest
    assert "폴더블 힌지 FPCB" in digest, "첫 턴(과제 규정)이 사라졌다"
    assert "지어내지 말고" in digest, "압축본이라는 걸 모델에게 안 알렸다"
    # 최근 턴은 원문 그대로다.
    assert f"턴 {len(hist)}" in msgs[-1][1]


def test_압축본도_예산_안에_들어간다():
    """압축이 예산을 넘기면 압축하는 의미가 없다 — 400 은 똑같이 난다."""
    st = {}
    msgs = app._history_messages(_hist(60), st)
    total = sum(len(c) for _, c in msgs)
    assert total <= st["budget"] * 1.05, f"{total:,}자가 예산 {st['budget']:,}자를 넘겼다"


def test_사용자_턴을_어시스턴트_턴보다_많이_남긴다():
    """요구·결정은 사용자 턴에 있다. 같은 글자를 쓸 거면 그쪽을 남기는 게 복원력이 높다."""
    dropped = [("user", "사" * 4000), ("assistant", "어" * 4000)]
    out = app._compact_turns(dropped, 2000)
    assert out.count("사") > out.count("어")


def test_이력과_문서_예산이_동시에_컨텍스트에_들어간다(monkeypatch):
    """둘은 같은 창을 다툰다. 각각은 맞는데 **합치면 넘는** 경우가 실제로 있었다
    (실측 128,000 창에서 142토큰 초과 — 토큰↔글자 왕복 변환 오차)."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    for ctx in (128000, 200000, 1000000):
        app._ctx_cache["n"] = ctx
        hist_chars = app._hist_budget_chars(has_docs=True)   # 문서를 붙인 턴의 이력 몫
        hist_tokens = app._est_tokens("가" * hist_chars)        # 한국어 최악
        doc_chars = app._doc_total_chars([{"text": "가" * 100}], hist_tokens)
        doc_tokens = app._est_tokens("가" * doc_chars)
        total = hist_tokens + doc_tokens + app.DOC_RESERVE_TOKENS
        assert total <= ctx, (
            f"컨텍스트 {ctx:,}토큰인데 이력({hist_tokens:,}) + 문서({doc_tokens:,}) + "
            f"예비분({app.DOC_RESERVE_TOKENS:,}) = {total:,}토큰이다"
        )


def test_짧은_표본에서도_예산이_부풀지_않는다():
    """토큰 수를 정수로 거쳐 나누면 짧은 표본의 절사 오차가 증폭된다 — 실측으로 1M 창에서
    1,066,412토큰짜리 이력 예산이 나왔다(즉 컨텍스트를 넘는 예산)."""
    for sample in ("가", "가나다라", "ab", "한글 섞인 mixed text"):
        for tokens in (1000, 100000, 839800):
            chars = app._chars_for_tokens(sample, tokens)
            # 되돌릴 때 **같은 구성**으로 만들어야 한다 — 첫 글자만 반복하면 혼합 표본이
            # 순한글이 되어 시험 자체가 틀린다(이 테스트가 처음에 거기서 틀렸다).
            back = app._est_tokens((sample * (chars // len(sample) + 1))[:chars])
            assert back <= tokens * 1.05, f"{sample!r}/{tokens}: {chars:,}자 → {back:,}토큰"


def test_이력_예산이_컨텍스트를_넘지_않는다(monkeypatch):
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    for ctx in (128000, 200000, 1000000):
        app._ctx_cache["n"] = ctx
        for has_docs in (False, True):
            chars = app._hist_budget_chars(has_docs=has_docs)
            tok = app._est_tokens("가" * chars)       # 한국어 최악
            assert tok < ctx, f"컨텍스트 {ctx:,}인데 이력 예산만 {tok:,}토큰(docs={has_docs})"


# ── 띵킹 모드 — 좌석 프롬프트에 문서가 들어가되 질문에는 안 섞인다 ──────────────────
def test_띵킹_좌석_프롬프트에_문서가_실린다():
    import thinking

    seat = {"key": "pcb-expert", "role": "PCB 설계", "knowledge": "카드 발췌"}
    docs = app._doc_block([_doc("설계.hwax.md", 500)])
    p = thinking._judge_prompt(seat, "굽힘 수명이 왜 부족한가", docs)
    assert "사용자가 붙인 문서" in p
    assert "굽힘 수명이 왜 부족한가" in p
    # 문서가 없으면 빈 자리가 생기지 않는다(프롬프트가 지저분해지면 형식 준수율이 떨어진다).
    assert "사용자가 붙인 문서" not in thinking._judge_prompt(seat, "질문")


def test_띵킹_문서_몫은_챗보다_작다():
    """좌석이 동시에 여러 명 돌아 문서가 좌석 수만큼 배로 든다 — 챗과 같은 몫을 주면
    한 발화에 수백만 토큰이 나간다."""
    import thinking

    assert 0 < thinking.THINK_DOC_SHARE < 1


def test_문서는_전문가_발굴_검색어에_안_섞인다():
    """q 에 문서를 붙이면 recommend_agents 검색어가 수십만 자가 되어 발굴이 통째로 망가진다.
    좌석 프롬프트에만 실어야 한다."""
    import inspect

    import thinking

    src = inspect.getsource(thinking.run_thinking)
    assert "_docs = _doc_block" in src
    assert "q += _docs" not in src and "q = q +" not in src

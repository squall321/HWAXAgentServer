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
    assert app._doc_budget_tokens(0) == 128000 - app.DOC_RESERVE_TOKENS
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
    assert some == none_ - 40000
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


def test_핸드오프_발췌가_심의_예산에_맞다():
    """심의 예산(_EVID_BUDGET)만 올리고 발췌 길이를 안 올리면, 예산은 크고 실제로 실리는
    건 그대로다 — 종전에 정확히 그 상태였다(11KB 예산에 2.6KB 만 도착)."""
    import deliberation as d

    assert app.HANDOFF_RESULT_CHARS * d._EVID_ITEMS >= d._EVID_BUDGET, (
        f"발췌 {app.HANDOFF_RESULT_CHARS}자 × {d._EVID_ITEMS}건 = "
        f"{app.HANDOFF_RESULT_CHARS * d._EVID_ITEMS:,}자로 예산 {d._EVID_BUDGET:,}자를 못 채운다"
    )


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


def test_사람이_못박으면_그_값이_이긴다(monkeypatch):
    """운영에서 컨텍스트 조회가 틀리거나 더 줄이고 싶을 때 손잡이가 있어야 한다."""
    import importlib

    monkeypatch.setenv("DELIB_EVID_BUDGET", "5000")
    import deliberation as d

    importlib.reload(d)
    assert d._evid_budget() == 5000
    monkeypatch.delenv("DELIB_EVID_BUDGET")
    importlib.reload(d)

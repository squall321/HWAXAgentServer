# 챗 경로(app.py)의 고정 상한을 모델 창에서 유도한다 — 챗→심의 핸드오프·도구 결과·수치 대조 원문·유령 ID 게이트
#
# 사용자 지침(2026-10-10) — 운영 모델은 GLM-5.2, 창 1,048,576 토큰이다. 챗에서 심의로 넘기는 도구 결과(result_full)가
# 4,000자였고(심의는 항목 150,000자를 받는다), 결정문 수치 대조의 원문은 결과마다 앞 120,000자였으며(모델은 200,000자까지
# 봤다), 유령 ID 게이트는 정수 2,000개를 넘으면 그 턴의 도구 결과 ID 를 더 배우지 않았다. 좁은 창(dev 16K)은 종전대로다.
#
# 앞의 **특성화** 시험은 구현 전 코드에서 초록인 것을 먼저 본 것이다.
#
#   실행:  .venv/bin/python -m pytest tests/test_chat_window_caps.py -q
import asyncio
import json
import re
import types
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

_SMALL = (16384, 128000, 200000)
_BIG = 1048576


@pytest.fixture(autouse=True)
def _restore_ctx():
    saved = dict(app._ctx_cache)
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


def _win(tokens):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()


_RESULT = "\n".join(f"| 행{i:05d} | 값 {i} |" for i in range(4000)) + "\n| 끝 | [결과-끝] |"


# ── 챗→심의 핸드오프(result_full·detail_full) ───────────────────────────────────────────
@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_핸드오프는_종전대로_4000자다(tokens):
    _win(tokens)
    assert app._handoff_text(_RESULT) == app._tool_preview(_RESULT, 4000)


def test_넓은_창의_핸드오프는_심의가_받는_항목_길이까지_싣는다():
    """엔진은 근거 항목을 150,000자까지 받는데 챗이 넘기는 날것은 4,000자였다 — 표의 앞 몇 줄만 심의에 갔다."""
    _win(_BIG)
    got = app._handoff_text(_RESULT)
    assert "[결과-끝]" in got and "…#" not in got, len(got)
    huge = "가" * (d._EVID_ITEM_MAX + 5000)
    cut = app._handoff_text(huge)
    assert len(cut) <= d._EVID_ITEM_MAX + 10 and cut.endswith(cut[-7:]) and "…#" in cut


def test_핸드오프_길이를_명시하면_그_값이다(monkeypatch):
    monkeypatch.setattr(app, "HANDOFF_RESULT_CHARS", 1000)
    monkeypatch.setattr(app, "_HANDOFF_SET", True)
    _win(_BIG)
    assert len(app._handoff_text(_RESULT)) <= 1010


# ── 도구 결과 상한 ─────────────────────────────────────────────────────────────────
def test_도구_결과_상한_0_은_무제한이다(monkeypatch):
    """주석은 '0 이면 무제한' 인데 코드는 0자로 잘라 경고만 남겼다."""
    monkeypatch.setattr(app, "TOOL_RESULT_MAX", 0)
    assert app._cap("가" * 50) == "가" * 50


def test_도구_결과를_자르면_원문_길이_표식이_붙는다():
    s = app._cap("가" * 300, 100)
    assert s.startswith("가" * 100) and app._CAP_MARK in s and "300자" in s


# ── 결정문 수치 대조의 원문(turn_out) ────────────────────────────────────────────────
@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_수치_대조_원문은_종전대로_결과마다_120000자_합_400000자다(tokens):
    _win(tokens)
    out: list = []
    for _ in range(5):
        app._turn_out_add(out, "가" * 150000)
    assert [len(x) for x in out] == [120000] * 4, [len(x) for x in out]


def test_넓은_창의_수치_대조_원문은_모델이_본_결과를_자르지_않는다():
    _win(_BIG)
    out: list = []
    for _ in range(5):
        app._turn_out_add(out, "가" * 150000 + " 9191.5")
    assert len(out) == 5 and all(x.endswith("9191.5") for x in out), [len(x) for x in out]


# ── 유령 ID 게이트 — 출처 정수 상한 ────────────────────────────────────────────────────
def _learned(tokens, seeds):
    _win(tokens)
    tok = app._turn_ids.set(set(range(100000, 100000 + seeds)))
    try:
        app._learn_ids('{"id": 77}')
        return 77 in app._turn_ids.get()
    finally:
        app._turn_ids.reset(tok)


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_유령_ID_게이트는_종전대로_정수_2000개에서_멈춘다(tokens):
    assert _learned(tokens, 10) and not _learned(tokens, 2000)


def test_넓은_창의_유령_ID_게이트는_긴_이력에서도_도구_결과의_ID_를_배운다():
    """이력이 길면 씨앗만으로 2,000개를 넘어, 그 턴에 도구가 돌려준 ID 를 다음 호출이 쓰면 '출처 없다' 로 막혔다."""
    assert _learned(_BIG, 5000)


def test_도구_결과가_잘리면_챗_스트림이_상태줄로_알린다():
    """모델만 알던 절단을 사람도 본다 — 완료 이벤트 근처에서 표지(_CAP_MARK)를 보고 상태줄을 낸다."""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    i = src.index("_turn_out_add(turn_out, _txt)")
    blk = src[i:i + 900]
    assert "_CAP_MARK in _txt" in blk and '"knob": "TOOL_RESULT_MAX"' in blk, blk


# ── 챗 이력 — 항목 하나를 24,000자에서 끊지 않는다(넓은 창) ─────────────────────────────────────
def _long(tag, n):
    return "".join(f"<{i:04d}>" + "가" * 34 for i in range(n // 40)) + f" [{tag}]"


_ITEM = _long("항목-끝", 60000)


@pytest.mark.parametrize("tokens", (128000, 200000))
def test_좁은_창의_이력_항목은_종전대로_24000자에서_끊긴다(tokens):
    _win(tokens)
    msgs = app._history_messages([{"role": "user", "content": "질문"}, {"role": "assistant", "content": _ITEM}])
    body = msgs[-1][1]
    assert body.startswith(_ITEM[:24000]) and _ITEM[:24001] not in body


def test_넓은_창의_이력_항목은_통째로_실린다():
    """포털은 이제 항목을 창 하나까지 받는데(ee24499) 에이전트가 24,000자에서 '…' 로 끊어, 이력의 결정문 뒤쪽이 빠졌다."""
    _win(_BIG)
    msgs = app._history_messages([{"role": "user", "content": "질문"}, {"role": "assistant", "content": _ITEM}])
    assert msgs[-1][1] == _ITEM


def test_좁은_창에서_이력_항목을_끊으면_원문_길이를_적는다():
    _win(128000)
    body = app._history_messages([{"role": "assistant", "content": _ITEM}])[-1][1]
    assert f"{len(_ITEM):,}자" in body, body[-80:]


@pytest.mark.parametrize("tokens", (_BIG,))
def test_가장_최근_이력_항목도_예산을_넘으면_원문_길이를_적고_줄인다(tokens):
    """kept 가 비어 있으면 첫(가장 최근) 항목을 예산과 상관없이 통째로 넣었다. 포털은 항목을 4,194,304자까지 받으므로
    창을 넘는 붙여넣기 한 번이 그 턴을 400 으로 죽이고, 실패한 턴의 메시지가 다음 턴의 최신 이력이 되어 이후 턴과 자동
    재시도까지 같은 400 으로 끝났다(새 대화를 열기 전까지). 좁은 창(16K)은 이력 예산이 아니라 창을 실제로 넘길 때 줄인다 —
    아래 test_좁은_창도_가장_최근_항목이_창을_넘기면_원문_길이를_적고_줄인다(m1 2차 AF1-dev-hist-cut)."""
    _win(tokens)
    big = "가" * 1_500_000 + "[항목-끝]"
    st: dict = {}
    msgs = app._history_messages([{"role": "user", "content": "질문"}, {"role": "user", "content": big}], st)
    total = sum(len(c) for _, c in msgs)
    assert total <= st["budget"], f"{total:,}자가 이력 예산 {st['budget']:,}자를 넘었다"
    assert app._est_tokens("".join(c for _, c in msgs)) < tokens
    assert f"{len(big):,}자 중 앞" in msgs[-1][1], msgs[-1][1][-80:]
    assert st.get("cut") == len(big), st


@pytest.mark.parametrize("tokens,chars", ((16384, 1500), (128000, 64260), (200000, 128520)))
def test_좁은_창의_이력_예산은_종전_값_그대로다(monkeypatch, tokens, chars):
    """특성화 — 구현 전 코드에서 뜬 값이다. 좁은 박스는 .env 로 TOOL_RESULT_MAX 를 낮춘다(도구 결과 몫을 떼지 않는다)."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    _win(tokens)
    assert app._hist_budget_chars("가") == chars


def test_넓은_창의_이력_몫은_도구_결과_하나가_들어갈_자리를_남긴다(monkeypatch):
    """1M 챗 루프는 이력에 (창 − 예비분)의 85% 를 줘 남는 몫이 약 149K 토큰인데, 도구 결과 하나가 TOOL_RESULT_MAX
    200,000자(≈190K 토큰)까지 온다 — 이력이 찬 대화에서 큰 조회 한 번이면 루프 안에서 창을 넘었다(400). 자동 재시도도
    같은 이력으로 다시 돌아 같은 결과였다. 문서가 있는 턴의 몫은 바꾸지 않는다(문서가 나머지를 쓴다)."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    _win(_BIG)
    hist_tok = app._est_tokens("가" * app._hist_budget_chars("가"))
    tool_tok = app._est_tokens("가" * app.TOOL_RESULT_MAX)
    assert hist_tok + app.DOC_RESERVE_TOKENS + tool_tok <= _BIG, (hist_tok, tool_tok)


def test_가장_최근_이력_항목을_줄였으면_챗이_경고로_알린다():
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    i = src.index("_hist_msgs = _history_messages(req.history, _hstat")
    blk = src[i:i + 1500]
    assert '_hstat.get("cut")' in blk and '"history_item_cut"' in blk, blk


def test_자동_재시도와_강제_재시도도_문서가_있는_턴의_이력_몫을_쓴다():
    """문서가 시스템 프롬프트에 실렸는데 재시도만 이력을 문서 없는 몫(0.85)으로 다시 만들어 창을 넘겼다."""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "*_history_messages(req.history), " not in src and "*_history_messages(req.history)," not in src
    assert src.count("has_docs=bool(req.documents)") >= 3, src.count("has_docs=bool(req.documents)")


# ── 빈 응답 구제·이어하기 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_구제_발췌는_종전대로_12000자다(tokens):
    _win(tokens)
    assert app._rescue_excerpt(["가" * 20000]) == "가" * 12000


def test_넓은_창의_구제_발췌는_도구_결과를_12000자에서_끊지_않는다():
    _win(_BIG)
    assert app._rescue_excerpt(["가" * 20000, "나" * 50000]) == "가" * 20000 + "\n" + "나" * 50000


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_이어하기는_끊긴_답의_앞_2000자를_싣는다(tokens):
    _win(tokens)
    got = app._resume_part(_ITEM)
    assert got.startswith(_ITEM[:2000]) and _ITEM[:2001] not in got


def test_넓은_창의_이어하기는_끊긴_답을_전부_싣는다():
    _win(_BIG)
    assert app._resume_part(_ITEM) == _ITEM


# ── 화두 뽑기·되묻기·좌석 축 — 대화 뒷부분을 먼저 남긴다 ────────────────────────────────────
def _hist_recent_last():
    return [{"role": "user", "content": "배경 질문"},
            {"role": "assistant", "content": "가" * 15000},
            {"role": "user", "content": "그럼 12um 로 낮춰도 되나 [최근-물음]"},
            {"role": "assistant", "content": "조건부로 된다 [최근-답]"}]


def _capture_llm(monkeypatch, answer="{}"):
    seen = []

    async def _llm(_obj, _system, human):
        seen.append(human)
        return answer
    monkeypatch.setattr(app, "_llm_text", _llm)
    monkeypatch.setattr(app.app.state, "llm", object(), raising=False)
    return seen


@pytest.mark.parametrize("tokens", (16384, _BIG))
def test_화두_뽑기는_예산을_최근_대화부터_쓴다(monkeypatch, tokens):
    """프롬프트는 '대화 뒷부분의 쟁점을 우선해라' 인데 예산을 오래된 쪽부터 깎아, 긴 답 하나 뒤의 물음이 빠졌다."""
    import asyncio
    _win(tokens)
    seen = _capture_llm(monkeypatch, '{"topic": "물음", "why": "", "options": []}')
    asyncio.run(app._derive_topic(_hist_recent_last(), "fb"))
    assert "[최근-물음]" in seen[0] and "[최근-답]" in seen[0], seen[0][-300:]


def test_넓은_창의_화두_뽑기는_긴_답을_2000자에서_끊지_않는다(monkeypatch):
    import asyncio
    _win(_BIG)
    seen = _capture_llm(monkeypatch, '{"topic": "물음", "why": "", "options": []}')
    asyncio.run(app._derive_topic([{"role": "user", "content": "질문"},
                                   {"role": "assistant", "content": _long("긴답-끝", 9000)}], "fb"))
    assert "[긴답-끝]" in seen[0]


def test_되묻기도_최근_대화를_먼저_싣는다(monkeypatch):
    import asyncio
    _win(16384)
    seen = _capture_llm(monkeypatch, '{"slots": {}}')
    monkeypatch.setattr(app, "_clarify_applicable", lambda *_a: True)
    asyncio.run(app.deliberate_clarify(app.ClarifyRequest(message="원인", job="diagnosis",
                                                          history=_hist_recent_last())))
    assert "[최근-물음]" in seen[0], seen[0][-300:]


def test_좌석_축_추출은_오래된_60건이_아니라_최근_대화를_본다(monkeypatch):
    import asyncio
    _win(16384)
    seen = _capture_llm(monkeypatch, "")

    async def _roster(_tools):
        return [{"domain": "mech", "agents": 3}]
    monkeypatch.setattr(app, "_domain_roster", _roster)
    hist = [{"role": "user", "content": f"옛 발화 {i}"} for i in range(80)] + [
        {"role": "user", "content": "마지막 쟁점 [최근-쟁점]"}]
    asyncio.run(app._seat_axes(object(), {}, "화두", hist))
    assert "[최근-쟁점]" in seen[0], seen[0][-300:]


def _run_aux(monkeypatch, which, hist):
    """화두 뽑기·되묻기·좌석 축 하나를 돌려 LLM 에 간 프롬프트를 돌려준다."""
    import asyncio
    if which == "topic":
        seen = _capture_llm(monkeypatch, '{"topic": "물음", "why": "", "options": []}')
        asyncio.run(app._derive_topic(hist, "fb"))
    elif which == "clarify":
        seen = _capture_llm(monkeypatch, '{"slots": {}}')
        monkeypatch.setattr(app, "_clarify_applicable", lambda *_a: True)
        asyncio.run(app.deliberate_clarify(app.ClarifyRequest(message="원인", job="diagnosis", history=hist)))
    else:
        seen = _capture_llm(monkeypatch, "")

        async def _roster(_tools):
            return [{"domain": "mech", "agents": 3}]
        monkeypatch.setattr(app, "_domain_roster", _roster)
        asyncio.run(app._seat_axes(object(), {}, "화두", hist))
    return seen[0]


_AUX = ("topic", "clarify", "axes")


@pytest.mark.parametrize("which", _AUX)
def test_넓은_창의_보조_호출은_이력_예산_전부를_싣지_않고_뺀_것을_적는다(monkeypatch, which):
    """화두 뽑기·되묻기·좌석 축은 대화 뒷부분의 쟁점만 보면 되는데 1백만 토큰 창에서 챗 이력 예산 전부(약 88만 자)를
    받았다 — 긴 대화에서 '심의로 넘기기' 한 번에 세 호출이 각각 수십만 토큰을 프리필해, 심의와 같은 LLM 을 쓰면 포털의
    600초 한도에 걸려 화두·되묻기·축이 기본값으로 내려앉았다. 필요한 만큼(창의 몇 %)만 싣고, 뺀 수를 프롬프트에 적는다."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    _win(_BIG)
    hist = [{"role": "user" if k % 2 else "assistant", "content": f"<턴{k:02d}>" + "가" * 30000}
            for k in range(19)] + [{"role": "user", "content": "<턴19> 힌지만 본다 [최근-쟁점]"}]
    prompt = _run_aux(monkeypatch, which, hist)
    assert "[최근-쟁점]" in prompt
    assert len(prompt) <= app._aux_budget(12000) + 3000, f"{len(prompt):,}자 — 보조 호출이 이력 예산을 다 썼다"
    assert len(prompt) < app._hist_budget_chars() // 4, len(prompt)
    assert "앞선 대화" in prompt and "싣지 않았다" in prompt, "뺀 대화 수를 프롬프트에 적지 않았다"


@pytest.mark.parametrize("which", _AUX)
def test_보조_호출의_가장_최근_발화가_몫을_넘으면_원문_길이를_적고_줄인다(monkeypatch, which):
    """첫 줄(가장 최근 발화)은 예산과 상관없이 통째로 실렸다 — 넓은 창은 줄 상한도 없어 붙여넣기 한 번이 그대로 갔다."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    _win(_BIG)
    big = "가" * 1_000_000 + "[끝]"
    prompt = _run_aux(monkeypatch, which, [{"role": "user", "content": big}])
    assert len(prompt) <= app._aux_budget(12000) + 3000, len(prompt)
    assert f"{len(big):,}자 중 앞" in prompt


# ── VOC 미리보기·페르소나 역할 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("tokens", (16384, _BIG))
def test_VOC_한_건은_좁은_창에서만_600자에서_끊는다(tokens):
    _win(tokens)
    v = {"content_translated": "가" * 900 + " [VOC-끝]"}
    got = app._voc_row(v, "kw")["text"]
    assert ("[VOC-끝]" in got) == (tokens == _BIG), got[-60:]


# ── 출력 상한 절단 — 챗도 finish_reason 을 읽는다 ────────────────────────────────────────
def test_finish_reason_을_두_자리_어디서든_읽는다():
    from types import SimpleNamespace as NS
    assert app._finish_reason(NS(response_metadata={"finish_reason": "length"})) == "length"
    assert app._finish_reason(NS(response_metadata={}, additional_kwargs={"finish_reason": "length"})) == "length"
    assert app._finish_reason(NS()) == "" and app._finish_reason(None) == ""


def test_챗_스트림이_출력_상한_절단을_경고한다():
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    i = src.index('elif kind == "on_chat_model_end":')
    blk = src[i:i + 900]
    assert '_finish_reason(out_msg) == "length"' in blk and '"code": "output_cut"' in blk, blk


# ── 붙인 문서 — 앞부분만 실었을 때도 상태줄이 말한다 ───────────────────────────────────────
def test_문서를_앞부분만_실으면_상태줄이_말한다():
    _win(16384)
    block = app._doc_block([{"name": "memo.txt", "text": "가" * 200000}], 0)
    assert "자만 실림" in block, "시험 전제 — 표지 없는 긴 글은 앞부분만 실린다"
    step = app._doc_status(block, 1, 0)
    assert "앞부분만" in step, step


def test_문서를_줄이지_않으면_상태줄은_종전_그대로다():
    _win(_BIG)
    block = app._doc_block([{"name": "memo.txt", "text": "가" * 2000}], 0)
    assert app._doc_status(block, 1, 0) == "붙인 문서 1건을 읽습니다"


def test_직접_실행_경로도_핸드오프용_날것을_싣는다():
    """확정 종결의 직접 실행 결과는 220자 미리보기뿐이라 챗→심의 근거로 한 토막만 갔다."""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    blk = src[src.index('f"도구 직접 실행: {_tn}"'):][:2200]
    assert "_handoff_text(_out)" in blk and '"result_full": _hand3' in blk, blk


# ── 도구 설명 상한 — 0 은 무제한이다(TOOL_RESULT_MAX 와 같은 약속) ─────────────────────────────────────
def test_도구_설명_상한_0_은_무제한이다(monkeypatch):
    """TOOL_DESC_MAX=0 이면 설명이 전부 '…' 한 글자가 됐고, desc_max=0 은 2,400 으로 떨어졌다."""
    monkeypatch.setattr(app, "TOOL_DESC_MAX", 0)
    assert app._cap_desc("가" * 50) == "가" * 50
    monkeypatch.setattr(app, "TOOL_DESC_MAX", 2400)
    assert app._cap_desc("가" * 5000, 0) == "가" * 5000
    assert app._cap_desc("가" * 5000) == "가" * 2400 + "…", "기본 동작(바인딩 보호 2,400자)은 그대로다"


# ── 글자 구성 표본 — 앞 20,000자만 보고 글 전체의 토큰을 재지 않는다 ──────────────────────────
def _tok_oracle(s: str) -> int:
    """시험용 토큰 수 — 엔진 환산(한글 1.05자/토큰·그 밖 3.6자/토큰)을 글 **전체**에 독립으로 적용한다."""
    ko = len(re.sub(r"[^가-힯一-鿿぀-ヿ㄰-㆏]", "", s))
    return int(ko / 1.05 + (len(s) - ko) / 3.6)


def _hist_tok_room():
    """문서 없는 챗 턴에서 이력(또는 그 자리에 싣는 글)이 쓸 수 있는 토큰 — 창 − 예비분 − 도구 결과 몫."""
    return app._model_context_tokens() - app.DOC_RESERVE_TOKENS - app._loop_room_tokens()


def test_보고서_정리본은_영문_머리_대화에서도_창을_넘기지_않는다(monkeypatch):
    """대화 앞 20,000자(로그·코드)의 밀도(3.6자/토큰)로 예산을 환산해, 뒤가 한국어인 긴 대화에서 창의 세 배를 싣고 400 으로
    정리본이 빈 문자열(실패)이 됐다."""
    import asyncio
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    _win(_BIG)
    seen = []

    class _Agent:
        async def ainvoke(self, inp, config=None):
            seen.append(inp["messages"][0][1])
            return {"messages": []}

    async def _agent_for(*_a, **_k):
        return _Agent()
    monkeypatch.setattr(app, "_agent_for", _agent_for)
    hist = [{"role": "user", "content": "log " + "x" * 30000}] + [
        {"role": "assistant" if k % 2 else "user", "content": "가" * 300000} for k in range(10)]
    cut: list = []
    asyncio.run(d._tidy_markdown(app.app, [], hist, "", "u", cut=cut))
    assert seen and _tok_oracle(seen[0]) <= _hist_tok_room(), f"{_tok_oracle(seen[0]):,}토큰 — 창을 넘는다"
    assert cut and "중 앞" in cut[0], cut


def test_빈_응답_구제_발췌도_JSON_머리_결과에서_창을_넘기지_않는다():
    _win(_BIG)
    got = app._rescue_excerpt(['{"rows": "' + "x" * 30000 + '"}', "가" * 2_500_000])
    assert _tok_oracle(got) <= _hist_tok_room(), f"{_tok_oracle(got):,}토큰"
    assert len(got) >= 12000


def test_챗_이력도_영문_머리_항목_뒤의_한국어_이력으로_창을_넘기지_않는다(monkeypatch):
    """이력 예산을 최근 6항목의 앞 20,000자로 환산한다 — 여섯째 항목이 영문 로그면 한국어 이력이 창의 몇 배로 실렸다."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    _win(_BIG)
    hist = [{"role": "user", "content": "log " + "x" * 25000}] + [
        {"role": "assistant" if k % 2 == 0 else "user", "content": f"<{k}>" + "가" * 400000} for k in range(5)]
    st: dict = {}
    msgs = app._history_messages(hist, st)
    assert _tok_oracle("".join(c for _, c in msgs)) <= _hist_tok_room(), st
    assert "<4>" in msgs[-1][1], "가장 최근 항목이 빠졌다"


def test_최근_항목을_줄일_때도_줄인_글_자체의_토큰으로_맞춘다(monkeypatch):
    """줄일 길이를 항목 전체의 평균 밀도로 정하면, 앞이 한국어이고 뒤가 영문인 항목은 앞부분만 남긴 글이 몫을 넘는다."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    _win(_BIG)
    hist = [{"role": "user", "content": "log " + "x" * 25000}] + [
        {"role": "assistant", "content": f"짧은 답 {k}"} for k in range(4)] + [
        {"role": "user", "content": "가" * 600000 + "x" * 2_000_000}]
    st: dict = {}
    msgs = app._history_messages(hist, st)
    assert st.get("cut") == 2_600_000, st
    assert _tok_oracle("".join(c for _, c in msgs)) <= app._hist_budget_tokens(), st


def test_문서_예산에서_빼는_이력_토큰도_글_전체로_잰다():
    """문서 예산은 '창 − 예비분 − 실린 이력' 인데 실린 이력을 앞 20,000자 표본으로 쟀다 — 압축본이나 오래된 영문 로그가
    머리에 있으면 한국어 이력을 3분의 1 로 세어, 문서가 그만큼 더 실려 창을 넘었다."""
    mixed = "x" * 30000 + "가" * 300000
    assert app._est_tokens_all(mixed) == _tok_oracle(mixed) > app._est_tokens(mixed) * 3
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert '_hist_tokens = _est_tokens_all("".join(c for _, c in _hist_msgs))' in src


# ── 좁은 창의 가장 최근 이력 항목 — 창을 실제로 넘길 때만 줄인다(m1 2차 AF1-dev-hist-cut) ─────────────────────────────
# dev 박스는 창이 16,384토큰이고 .env 의 TOOL_SCHEMA_BUDGET=12000 이라 예비분(DOC_RESERVE_TOKENS 28,000)이 창보다 크다 — 이력 몫이
# 바닥(1,200토큰·원문 몫 960토큰)이다. 가장 최근 항목을 그 몫에 맞춰 줄이자 3,000자 남짓한 보통 답이 매 턴 약 1,000자로 잘리고
# '나눠서 다시 보내 주세요' 경고가 떴다(모델의 답인데 사용자에게 다시 보내라고 했다). 종전(968fba2 전)엔 항목 상한까지 실렸다.
def _dev_box(monkeypatch):
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    monkeypatch.setattr(app, "DOC_RESERVE_TOKENS", 28000)
    monkeypatch.setattr(app, "HIST_ITEM_MAX", 4000)
    _win(16384)


_ANSWER = "답변 " * 998 + "[답-끝]"        # 2,999자 — 보통 답 하나


def test_좁은_창의_가장_최근_항목은_창을_넘기지_않으면_통째로_싣는다(monkeypatch):
    _dev_box(monkeypatch)
    st: dict = {}
    msgs = app._history_messages([{"role": "user", "content": "질문"}, {"role": "assistant", "content": _ANSWER}], st)
    assert msgs[-1] == ("assistant", _ANSWER), msgs[-1][1][-80:]
    assert "cut" not in st, st


def test_좁은_창도_가장_최근_항목이_창을_넘기면_원문_길이를_적고_줄인다(monkeypatch):
    """항목 상한(HIST_ITEM_MAX 기본 24,000자)이 16K 창보다 큰 박스 — 한국어 24,000자는 약 22,900토큰이다. 그대로 실으면 그 턴과
    다음 턴들이 400 으로 끝난다(968fba2 의 시나리오). 좁은 창에서도 이것은 줄이고 원문 길이를 적는다."""
    monkeypatch.delenv("HIST_BUDGET", raising=False)
    _win(16384)
    big = "가" * 1_500_000 + "[항목-끝]"
    st: dict = {}
    msgs = app._history_messages([{"role": "user", "content": "질문"}, {"role": "user", "content": big}], st)
    assert _tok_oracle("".join(c for _, c in msgs)) < 16384 // 2 + 1000, st
    assert f"{len(big):,}자 중 앞" in msgs[-1][1], msgs[-1][1][-80:]
    assert st.get("cut") == len(big) and st.get("cut_role") == "user", st


class _QuietAgent:
    async def astream_events(self, inputs, version=None, config=None):
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content="네.")}}

    async def ainvoke(self, payload, config=None):
        return {"messages": [types.SimpleNamespace(type="ai", content="네.")]}


def _chat_warnings(monkeypatch, history):
    async def _fake_agent_for(*_a, **_k):
        return _QuietAgent()

    monkeypatch.setattr(app, "_agent_for", _fake_agent_for)
    req = app.ChatRequest(message="이어서", groups=["portal-admin"], history=history)

    async def run():
        return [c.decode() if isinstance(c, bytes) else str(c) async for c in app._agent_stream(app.app, req)]

    out = []
    for chunk in asyncio.run(run()):
        if chunk.startswith("event: warning\n"):
            out.append(json.loads(chunk.split("data: ", 1)[1]))
    return [w for w in out if w.get("code") == "history_item_cut"]


def test_직전_답을_줄였으면_경고는_직전_답의_앞부분만_실었다고_말한다(monkeypatch):
    """줄인 것이 모델의 답이면 '나눠서 다시 보내 주세요' 는 틀린 말이다 — 사용자가 보낸 글이 아니다."""
    _win(_BIG)
    big = "가" * 1_500_000 + "[답-끝]"
    warn = _chat_warnings(monkeypatch, [{"role": "user", "content": "질문"}, {"role": "assistant", "content": big}])
    assert len(warn) == 1, warn
    assert "직전 답의 앞부분만 이력에 실었" in warn[0]["message"] and "다시 보내" not in warn[0]["message"], warn


def test_사용자_글을_줄였으면_경고는_종전_문구다(monkeypatch):
    _win(_BIG)
    big = "가" * 1_500_000 + "[글-끝]"
    warn = _chat_warnings(monkeypatch, [{"role": "user", "content": big}])
    assert len(warn) == 1 and "나눠서 다시 보내 주세요" in warn[0]["message"], warn

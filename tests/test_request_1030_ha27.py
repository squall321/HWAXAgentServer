# 자기검증 관문의 에이전트 몫 — 원문 붙잡기 · 검토자 입력 · [짚은 점] · 넣음 · 짚음 · 버림 · /health 를 실제 래퍼 · 감싸개로 본다(HWAXPortal docs/request-1030 HA-27)
"""10-30 재시험 넷(OT-04 · 08 · 09 · DW-05)에서 검토자가 맞는 초안을 틀리게 고쳤다 — 검토자 사본은 접기 · 자르기를 지나 원문의 수가 빠졌고,
바꿀 곳을 넣기 전에 근거와 다시 대조하는 걸음이 없었다. 설계 HWAXPortal docs/request-1030/design-ha27.md 12절의 W · M · P · J · E · H 와
design-integration.md C-13(실패한 결과는 색인에서 뺀다)을 본다. 순수 모듈 시험은 tests/test_sc_gate.py 다.

  실행:  .venv/bin/python -m pytest tests/test_request_1030_ha27.py -q
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402
import sc_gate as g  # noqa: E402
from test_chat_self_check import _Reviewer, _events, _inner, _run, _text  # noqa: E402
from test_request_1029 import _cache, _tool_of  # noqa: E402

# 40개 항목 뒤에 total 34 — 도구 결과 상한(TOOL_RESULT_MAX)에 걸리면 모델 · 검토자 사본에서 total 이 빠진다(OT-04 의 꼴)
BIG_LIST = json.dumps({"items": [{"id": f"M-{i:03d}", "name": f"개발모델 {i}호"} for i in range(40)], "total": 34}, ensure_ascii=False)
BOX0 = {"texts": [], "chars": 0, "partial": False}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """계수는 모듈 상태다 — 시험마다 0 에서 센다. 손잡이는 기본값(관문 1 · 원문 600만 자)에서 시작한다."""
    monkeypatch.setattr(a, "_SC_STAT", {"counts": a.Counter(), "ms": [], "last_error": None})
    monkeypatch.setattr(a, "_HUB_FOLD_STAT", {"count": 0, "chars_in": 0, "chars_out": 0})
    monkeypatch.delenv("CHAT_SELF_CHECK_GATE", raising=False)
    monkeypatch.delenv("CHAT_SELF_CHECK_RAW_CHARS", raising=False)
    _cache(monkeypatch, {}, {})


def _held(tool, args=None, box=None):
    """그릇을 건 채 도구를 한 번 부른다 → (돌려받은 글, 그릇)."""
    box = dict(BOX0, texts=[]) if box is None else box
    tok = a._turn_sc_raw.set(box)
    try:
        return asyncio.run(tool.ainvoke(args or {})), box
    finally:
        a._turn_sc_raw.reset(tok)


# ── W 붙잡기 — capped 의 R5 ──────────────────────────────────────────────────────────────────────────────────
def test_W1_상한_너머의_원문을_그릇에_붙잡는다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_RESULT_MAX", 100)
    out, box = _held(_tool_of("list_models", BIG_LIST))
    assert a._CAP_MARK in out and '"total": 34' not in out, "모델 · 검토자 사본에서는 total 이 잘렸다"
    assert box["texts"] == [BIG_LIST] and box["chars"] == len(BIG_LIST) and not box["partial"]


def test_W2_턴_밖에서는_붙잡지_않는다():
    assert a._turn_sc_raw.get() is None, "심의 · 카탈로그 길 — 그릇이 없다"
    out = asyncio.run(_tool_of("list_models", '{"total": 34}').ainvoke({}))
    assert out.endswith('{"total": 34}')


def test_W3_허브_응답을_접기_전의_원문을_붙잡는다():
    big = json.dumps({"type": "failure_mode", "fields": [{"path": f"in.f{i}", "label": "x" * 400} for i in range(300)]})
    assert len(big) > a._HUB_BIG_CHARS
    out, box = _held(_tool_of("object_fields", big))
    assert out.startswith("[포털 챗이 허브 응답을 접었다") and box["texts"] == [big]


def test_W4_실패_차단된_호출은_붙잡지_않는다():
    async def boom(**_kw):
        raise RuntimeError("백엔드 다운 3건")
    failed = a._prep_tool(StructuredTool(name="list_models", description="d", coroutine=boom,
                                         args_schema={"type": "object", "properties": {}}), chat=True)
    out, box = _held(failed)
    assert out.startswith(a._TOOL_FAIL_MARK) and box["texts"] == []

    async def never(**_kw):
        raise AssertionError("막힌 호출은 부르지 않는다")
    get = a._prep_tool(StructuredTool(name="get_model", description="d", coroutine=never,
                                      args_schema={"type": "object", "properties": {"id": {"type": "integer"}}}), chat=True)
    tok = a._turn_ids.set({1})
    try:
        out, box = _held(get, {"id": 999})
    finally:
        a._turn_ids.reset(tok)
    assert a._PHANTOM_ID_MARK in out and box["texts"] == [], "유령 ID 관문이 막은 호출"


def test_W5_턴_합_상한에서_앞부분만_싣고_partial(monkeypatch):
    monkeypatch.setenv("CHAT_SELF_CHECK_RAW_CHARS", "500")
    box = dict(BOX0, texts=[])
    for _ in range(3):
        _held(_tool_of("list_models", "가" * 400), box=box)
    assert [len(t) for t in box["texts"]] == [400, 100] and box["partial"] and box["chars"] == 500


def test_W5_결과_하나의_상한(monkeypatch):
    monkeypatch.setattr(a, "_SC_RAW_ITEM", 300)
    _out, box = _held(_tool_of("list_models", "나" * 400))
    assert [len(t) for t in box["texts"]] == [300] and box["partial"]


def test_손잡이_읽기(monkeypatch):
    for v, want in (("1", "1"), ("", "1"), ("num", "num"), (" NUM ", "num"), ("0", "0"), ("yes", "1")):
        monkeypatch.setenv("CHAT_SELF_CHECK_GATE", v)
        assert a._sc_gate_mode() == want, v
    for v, want in (("500", 500), ("", 6_000_000), ("x", 6_000_000), ("-5", 0)):
        monkeypatch.setenv("CHAT_SELF_CHECK_RAW_CHARS", v)
        assert a._sc_raw_cap() == want, v
    monkeypatch.setenv("CHAT_SELF_CHECK_RAW_CHARS", "0")
    assert a._sc_raw_box() is None, "0 이면 붙잡지 않는다(모델 사본으로만 대조)"


class _CallAgent:
    """도구 한 번(진짜 래퍼를 부른다) → 답. test_chat_self_check._ToolAgent 와 같은 꼴의 사건이되 래퍼 도구를 실제로 지난다(원문 붙잡기 R5)."""

    def __init__(self, tool, answer):
        self.tool, self.answer = tool, answer

    async def astream_events(self, inputs, version=None, config=None):
        yield {"event": "on_tool_start", "name": self.tool.name, "data": {"input": {}}, "run_id": "r1"}
        out = await self.tool.ainvoke({})
        yield {"event": "on_tool_end", "name": self.tool.name, "data": {"output": out}, "run_id": "r1"}
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content=self.answer)}}

    async def ainvoke(self, payload, config=None):
        raise AssertionError("답이 있는 턴은 구제 호출을 하지 않는다")


def _with_agent(monkeypatch, agent):
    async def built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", built)
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")


def _inner_box(monkeypatch, agent) -> dict:
    """감싸개 없이 안쪽 턴만 돌려 box 를 본다."""
    _with_agent(monkeypatch, agent)
    box: dict = {}

    async def go():
        return [c async for c in a._agent_stream_turn(a.app, a.ChatRequest(message="개발모델 몇 개?", groups=["portal-admin"]), box)]
    asyncio.run(go())
    return box


def test_W6_관문을_끄면_턴이_넘기는_원문은_없다(monkeypatch):
    box = _inner_box(monkeypatch, _CallAgent(_tool_of("list_models", BIG_LIST), "개발모델은 34개입니다."))
    assert box["raw"]["texts"] == [BIG_LIST], "켜면 원문을 넘긴다"
    monkeypatch.setenv("CHAT_SELF_CHECK_GATE", "0")
    box = _inner_box(monkeypatch, _CallAgent(_tool_of("list_models", BIG_LIST), "개발모델은 34개입니다."))
    assert box["raw"] is None and box["draft"] == "개발모델은 34개입니다."


# ── M 검토자 입력 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_M1_결과마다_몫을_나눠_작은_결과가_빠지지_않는다():
    got = a._sc_fit_results(["가" * 200_000, "끝 결과 34"], 3_000)
    assert got[1] == ("끝 결과 34", False)
    assert got[0][1] is True and "자 중 앞" in got[0][0] and a._est_tokens_all(got[0][0]) <= 3_000
    assert a._sc_fit_results(["짧다", "짧다"], 3_000) == [("짧다", False), ("짧다", False)], "몫 안이면 그대로"


def _body(monkeypatch, outputs, calls=None, draft="최대 응력은 321.5 MPa 입니다.", idx=None, **req) -> str:
    monkeypatch.setattr(a, "_model_context_tokens", lambda: 16_000)
    calls = [(f"t{i}", "") for i in range(1, len(outputs) + 1)] if calls is None else calls
    r = a.ChatRequest(message="응력은?", groups=["portal-admin"], history=[], **req)
    return a._self_check_messages(r, draft, calls, outputs, idx)[1].content


def test_M2_잘린_결과의_머리에_까닭을_밝힌다(monkeypatch):
    big = a._cap("측정값 행 " * 60_000, 200_000)
    body = _body(monkeypatch, [big, "작은 결과 34"])
    assert "─ 결과 1 (잘림 — 도구 결과 상한 · 검토 몫: 뒷부분은 보이지 않는다) ─" in body
    assert "─ 결과 2 ─\n작은 결과 34" in body, "작은 결과는 큰 결과 때문에 빠지지 않는다"
    assert a._est_tokens_all(body) <= 16_000 - a._SC_OUT_CAP


def test_M3_허브_접기와_근거_몫의_잘림도_밝힌다(monkeypatch):
    big = json.dumps({"type": "failure_mode", "fields": [{"path": f"in.f{i}", "label": "x" * 400} for i in range(300)]})
    folded = "[이 결과는 X 앱의 object_fields 이다]\n" + a._fold_hub_result("object_fields", big)
    cut = a._cut_mark("가" * 300, 100)
    body = _body(monkeypatch, [folded, cut])
    assert "─ 결과 1 (잘림 — 허브 응답 접기" in body and "─ 결과 2 (잘림 — 근거 몫: 뒷부분은 보이지 않는다) ─" in body


def test_M4_지시는_짚은_점과_잘린_결과와_관문_줄을_싣는다(monkeypatch):
    body = _body(monkeypatch, ["max 312.5"])
    assert "[짚은 점]" in body and "'(잘림' 이 붙은 결과" in body and "서버가 [바꿀 곳] 의 쌍을 조회 결과 원문" in body
    assert "원문 값을 바꾸는 쌍은" in body and "고친 답 전체를 다시 쓰지 마라" in body
    assert body.index("[답 초안]") < body.index("다음을 검토하라") < body.index("서버가 [바꿀 곳]"), "관문 줄은 맨 끝"
    assert "'(근거 없음 — 추정)' 으로 표시하라" in body, "HA-12 가 찾는 글귀는 남는다"
    monkeypatch.setenv("CHAT_SELF_CHECK_GATE", "num")
    num = _body(monkeypatch, ["max 312.5"])
    assert "서버가 [바꿀 곳] 의 쌍을 조회 결과 원문" in num and "원문 값을 바꾸는 쌍은" not in num, "수 관문만이면 값 줄을 말하지 않는다"
    monkeypatch.setenv("CHAT_SELF_CHECK_GATE", "0")
    assert "서버가 [바꿀 곳] 의 쌍" not in _body(monkeypatch, ["max 312.5"]), "꺼져 있으면 사실이 아니라 싣지 않는다"
    monkeypatch.delenv("CHAT_SELF_CHECK_GATE")
    no_tool = _body(monkeypatch, [], calls=[], draft="설명입니다." * 50)
    assert "'(잘림' 이 붙은 결과" not in no_tool and "서버가 [바꿀 곳] 의 쌍" not in no_tool, "도구 없는 턴은 ② 를 바꿔 끼운다"
    assert "[짚은 점]" in no_tool and "고친 답 전체를 다시 쓰지 마라" in no_tool


def test_M5_코드_대조는_원문에_있는_수를_빼고_색인이_없으면_종전이다(monkeypatch):
    idx = g.RawIndex.build(['{"v": 321.5}'])
    with_idx = _body(monkeypatch, ["다른 결과"], draft="응력은 321.5 MPa", idx=idx)
    assert "`321.5`" not in with_idx
    assert "`321.5`" in _body(monkeypatch, ["다른 결과"], draft="응력은 321.5 MPa"), "idx 가 없으면 지금 그대로"


# ── P 읽기 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_P1_짚은_점을_읽고_고친_점에_섞이지_않는다():
    out = "[판정] 고침\n[고친 점]\n- a\n[짚은 점]\n- b\n- 없음\n[바꿀 곳]\n[원문]\nx\n[새 글]\ny"
    assert a._self_check_flags(out) == ["b"]
    assert a._self_check_parse(out) == ("fixed", ["a"], [("x", "y")])


def test_P2_바꿀_곳_뒤에_온_짚은_점은_새_글에_섞이지_않는다():
    out = "[판정] 고침\n[고친 점]\n- a\n[바꿀 곳]\n[원문]\nx\n[새 글]\ny\n[짚은 점]\n- b"
    assert a._self_check_parse(out)[2] == [("x", "y")] and a._self_check_flags(out) == ["b"]
    assert a._self_check_flags("[판정] 고칠 것 없음\n[고친 점]\n- 없음\n") == []


# ── J 판정 · 상태 ─────────────────────────────────────────────────────────────────────────────────────────────
def _idx(raw):
    return g.RawIndex.build([raw])


def test_J1_색인도_짚은_점도_없으면_종전_꼴_그대로():
    draft = "결론은 이렇다. " * 30 + "끝 문장은 A 다."
    good = [("끝 문장은 A 다.", "끝 문장은 B 다.")]
    assert a._self_check_judge(draft, "fixed", ["x"], good, False) == (draft.replace("A 다.", "B 다."), {"status": "fixed", "fixes": ["x"]})
    assert a._self_check_judge(draft, "fixed", ["x"], good, False, idx=None, flags=()) == \
        (draft.replace("A 다.", "B 다."), {"status": "fixed", "fixes": ["x"]})


def test_J2_버림만_남으면_초안이_ok_로_나가고_버린_것은_meta_에():
    draft = "개발모델은 34개입니다. 끝."
    ans, sc = a._self_check_judge(draft, "fixed", ["34→13"], [("개발모델은 34개", "개발모델은 13개")], False, idx=_idx('{"total": 34}'))
    assert ans == draft and sc["status"] == "ok" and "flags" not in sc
    assert len(sc["dropped"]) == 1 and "34" in sc["dropped"][0] and sc["dropped"][0].startswith("34→13 — 버림: ")
    assert sc["gate"]["drop_sourced_number"] == 1 and sc["gate"]["applied"] == 0 and sc["gate"]["reviewer_fixed"] is True


def test_J3_짚음만_남으면_flagged_이고_fixes_는_flags_와_같다():
    draft = "최대 321.5 MPa 이다."
    ans, sc = a._self_check_judge(draft, "fixed", ["x"], [("321.5 MPa", "318.0 MPa")], False, idx=_idx('{"max": 312.5}'))
    assert ans == draft and sc["status"] == "flagged" and sc["fixes"] == sc["flags"] and "318.0" in sc["flags"][0]
    assert sc["reason"] and sc["gate"]["demote_unsourced_number"] == 1


def test_J4_섞이면_넣음_쌍만_넣고_짚은_쌍은_flags_로():
    draft = "최대 321.5 MPa 이다. 결론은 위험하다."
    ans, sc = a._self_check_judge(draft, "fixed", ["a", "b"],
                                  [("321.5 MPa", "312.5 MPa"), ("위험하다.", "위험하다. 안전율은 2.4 이다.")], False, idx=_idx('{"max": 312.5}'))
    assert ans == "최대 312.5 MPa 이다. 결론은 위험하다." and sc["status"] == "fixed" and sc["fixes"] == ["a"]
    assert sc["flags"][0].startswith("b — 반영 안 함") and sc["gate"]["applied"] == 1


def test_J5_판정_ok_와_짚은_점은_flagged():
    draft = "답이다 12.5"
    ans, sc = a._self_check_judge(draft, "ok", [], [], False, flags=["의심"])
    assert ans == draft and sc["status"] == "flagged" and sc["fixes"] == ["의심"] and sc["reason"] == "검토자가 고치지 않고 짚기만 했다"
    assert "gate" not in sc, "관문이 돌지 않은 턴(색인 없음)"


def test_J6_넣음_쌍을_못_찾으면_suspect_이고_짚은_곳도_싣는다():
    draft = "최대 321.5 MPa 이다."
    ans, sc = a._self_check_judge(draft, "fixed", ["a", "b"], [("없는 글", "x"), ("321.5 MPa", "318.0 MPa")], False,
                                  idx=_idx('{"max": 312.5}'), flags=["검토자 의심"])
    assert ans == draft and sc["status"] == "suspect" and "찾지 못했다" in sc["reason"]
    assert sc["flags"][0] == "검토자 의심" and sc["flags"][1].startswith("b — 반영 안 함")


def test_고친_점과_쌍의_수가_다르면_코드가_설명을_만든다():
    draft = "개발모델은 34개입니다. 시험은 7건이다."
    ans, sc = a._self_check_judge(draft, "fixed", ["하나뿐인 설명"], [("개발모델은 34개", "개발모델은 13개"), ("7건이다.", "7건이다(조회 결과 1).")],
                                  False, idx=_idx('{"total": 34, "tests": 7}'))
    assert ans == "개발모델은 34개입니다. 시험은 7건이다(조회 결과 1)." and sc["status"] == "fixed"
    assert sc["fixes"] == ["'7건이다.' → '7건이다(조회 결과 1).'"] and sc["dropped"][0].startswith("'개발모델은 34개' → '개발모델은 13개' — 버림")


def test_관문이_걸러내지_않으면_고친_점은_검토자_것_그대로():
    draft = "최대 321.5 MPa 이다."
    ans, sc = a._self_check_judge(draft, "fixed", ["321.5 → 312.5(조회 결과 1)"], [("321.5 MPa", "312.5 MPa")], False,
                                  idx=_idx('{"max": 312.5}'))
    assert ans == "최대 312.5 MPa 이다." and sc == {"status": "fixed", "fixes": ["321.5 → 312.5(조회 결과 1)"],
                                                 "gate": {"applied": 1, "reviewer_flags": 0, "reviewer_fixed": True, "raw_partial": False}}


# ── C-13 — 실패한 결과는 색인에서 빼고 UUID 조각은 수가 아니다 ───────────────────────────────────────────────────────
def test_C13_거절_글과_실패_봉투는_근거가_아니다():
    """I-4 의 꼴 — 사본에 숫자로 시작하는 UUID 를 든 거절 글이 있어도 그 조각이 강한 수 3 이 되지 않는다(design-integration C-13)."""
    req = a.ChatRequest(message="불량 몇 건?", groups=[])
    outs = [f"{a._TOOL_FAIL_MARK} 3abcdef0-1234-4abc-9def-0123456789ab 는 다른 허브의 id 다 — 3 번 다시 풀어라",
            '{"ok": false, "error": "3건 조회 실패"}', "[이 결과는 X 앱의 t 이다]\nerror: 3건 실패", '{"defects": 5}']
    idx = a._sc_index(req, outs, None)
    assert not idx.has(3) and idx.has(5, strong=True)
    assert [tuple(d) for d in g.gate_patches([("불량 3건", "불량 5건")], idx, draft="불량 3건이다.")] == [("keep", "keep", "")]
    raw = {"texts": ['{"ok": false, "error": "3건"}', '{"defects": 5}'], "chars": 40, "partial": True}
    idx2 = a._sc_index(req, [], raw)
    assert not idx2.has(3) and idx2.has(5, strong=True) and idx2.partial, "그릇의 실패 봉투도 빼고, 그릇의 partial 을 잇는다"


def test_사본의_절단_표지_수는_근거가_아니고_덧붙인_근거는_이력까지():
    src = '{"rows": "' + "x" * 300 + '"}'
    big = a._cap(src, 100)
    assert a._CAP_MARK in big and f"{len(src):,}자" in big
    req = a.ChatRequest(message="기준 41.5 로", groups=[], documents=[{"name": "d", "text": "문서 값 77.5"}], history=[
        {"role": "user", "content": "앞 질문 55.5"}, {"role": "assistant", "content": "근거 없는 답 66.5"},
        {"role": "assistant", "content": f"조회한 답 88.5\n{a._EVIDENCE_MARK}", "evidence": [{"tool": "t", "result": "앞 턴 결과 98.5"}]}])
    idx = a._sc_index(req, [big, a._cut_mark("가" * 300, 100)], None)
    assert not idx.has(len(src)) and not idx.has(100) and not idx.has(300), "도구 결과 상한 · 근거 몫 표지의 자 수는 근거가 아니다"
    assert all(idx.has(v, strong=True) for v in (41.5, 77.5, 55.5, 88.5, 98.5))
    assert not idx.has(66.5), "근거 블록 없는 어시스턴트 발화는 근거가 아니다(세탁 길)"


# ── E 턴 끝까지 ───────────────────────────────────────────────────────────────────────────────────────────────
OT04_REVIEW = "[판정] 고침\n[고친 점]\n- 34 → 13\n[바꿀 곳]\n[원문]\n개발모델은 34개\n[새 글]\n개발모델은 13개"


def _ot04_turn(monkeypatch):
    monkeypatch.setattr(a, "TOOL_RESULT_MAX", 200)
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    _with_agent(monkeypatch, _CallAgent(_tool_of("list_models", BIG_LIST), "개발모델은 34개입니다."))
    rv = _Reviewer(OT04_REVIEW)
    monkeypatch.setattr(a.app.state, "chat_llm", rv, raising=False)

    async def go():
        req = a.ChatRequest(message="개발모델 몇 개야?", groups=["portal-admin"], history=[])
        return [c async for c in a._agent_stream(a.app, req)]
    evs = _events(asyncio.run(go()))
    return evs, rv, [d for e, d in evs if e == "meta"][-1]["self_check"], [d for e, d in evs if e == "result"][-1]["content"]


def test_E1_OT04_를_턴으로_초안의_맞는_값이_그대로_나간다(monkeypatch):
    evs, rv, sc, result = _ot04_turn(monkeypatch)
    assert result.startswith("개발모델은 34개입니다."), result[:200]
    assert sc["status"] == "ok" and len(sc["dropped"]) == 1 and "34" in sc["dropped"][0]
    assert sc["gate"]["drop_sourced_number"] == 1 and sc["gate"]["raw_chars"] == len(BIG_LIST) and sc["gate"]["raw_partial"] is False
    assert a._sc_health()["gate"]["counts"]["drop_sourced_number"] == 1 and a._sc_health()["gate"]["turns"] == 1
    assert "(잘림 — 도구 결과 상한" in rv.calls[0][0][1].content, "검토자 사본이 잘렸다는 것을 검토자가 안다"
    assert "개발모델은 13개" not in _text(evs, "token")


def test_E2_관문을_끄면_종전처럼_바꿀_곳을_넣는다(monkeypatch):
    monkeypatch.setenv("CHAT_SELF_CHECK_GATE", "0")
    _evs, _rv, sc, result = _ot04_turn(monkeypatch)
    assert result.startswith("개발모델은 13개입니다.") and sc["status"] == "fixed" and "gate" not in sc


def test_E3_원문_값으로_고치는_감싸개_턴은_그대로_고친다(monkeypatch):
    """test_chat_self_check 의 가짜 안쪽 턴(box 에 raw 가 없다 — 사본으로만 대조)에서 321.5 → 312.5 는 출처 있는 고침이라 넣는다."""
    draft = "A 부품의 최대 응력은 321.5 MPa 이고 안전율은 1.8 입니다. B 부품은 여유가 큽니다."
    rv = _Reviewer("[판정] 고침\n[고친 점]\n- 321.5 → 312.5\n[바꿀 곳]\n[원문]\n최대 응력은 321.5 MPa\n[새 글]\n최대 응력은 312.5 MPa")
    evs = _run(monkeypatch, _inner(draft, calls=[("query_stress", "part=A")], outputs=["max 312.5 MPa, SF 1.8"]), rv)
    sc = evs[-3][1]["self_check"]
    assert _text(evs, "token").startswith(draft.replace("321.5", "312.5")) and sc["status"] == "fixed"
    assert sc["gate"]["applied"] == 1 and sc["gate"]["raw_chars"] == 0, "그릇이 없는 턴 — 사본으로만 대조했다"


def test_검토_준비가_실패하면_초안을_내고_failed(monkeypatch):
    def bad(*_a, **_k):
        raise ValueError("색인 실패")
    monkeypatch.setattr(a, "_sc_index", bad)
    draft = "A 부품의 최대 응력은 321.5 MPa 입니다."
    rv = _Reviewer("[판정] 고침\n[바꿀 곳]\n[원문]\n321.5\n[새 글]\n999.9")
    evs = _run(monkeypatch, _inner(draft, calls=[("q", "")], outputs=["x"]), rv)
    sc = evs[-3][1]["self_check"]
    assert _text(evs, "token").startswith(draft) and sc["status"] == "failed" and "ValueError" in sc["reason"] and rv.calls == []


# ── H /health ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_H1_fixed_비율과_관문_계수(monkeypatch):
    gate = {"applied": 1, "drop_sourced_number": 2, "demote_coupled": 1, "reviewer_flags": 1, "reviewer_fixed": True, "raw_partial": True}
    for st, gt in (("fixed", gate), ("ok", {"applied": 0, "reviewer_flags": 0, "reviewer_fixed": False}), ("flagged", None), ("skipped", None)):
        a._sc_record({"status": st, "ms": 7, **({"gate": gt} if gt else {})})
    h = a._sc_health()
    assert h["counts"] == {"fixed": 1, "ok": 1, "flagged": 1, "skipped": 1} and h["fixed_ratio"] == 0.333
    assert h["gate"]["mode"] == "1" and h["gate"]["turns"] == 2 and h["gate"]["reviewer_fixed"] == 1 and h["gate"]["raw_partial"] == 1
    assert h["gate"]["counts"] == {"applied": 1, "drop_sourced_number": 2, "demote_weak_source": 0, "drop_marks_sourced": 0,
                                   "demote_removes_value": 0, "demote_unsourced_number": 0, "demote_coupled": 1, "reviewer_flags": 1}
    assert h["last_error"] is None, "flagged 는 실패가 아니다"
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})   # 창을 박아 둔다 — /health 가 LLM 서버에 묻지 않게
    assert a.health()["self_check"] == h


def test_H2_건너뛴_턴만이면_fixed_비율은_없다(monkeypatch):
    a._sc_record({"status": "skipped"})
    assert a._sc_health()["fixed_ratio"] is None
    monkeypatch.setattr(a, "_SC_STAT", {"counts": a.Counter(), "ms": [], "last_error": None})
    h = a._sc_health()
    assert h["fixed_ratio"] is None and h["gate"]["turns"] == 0
    monkeypatch.setenv("CHAT_SELF_CHECK_GATE", "num")
    assert a._sc_health()["gate"]["mode"] == "num"

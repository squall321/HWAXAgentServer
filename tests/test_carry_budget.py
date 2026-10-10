# 양보 불가 조항·후보안의 합과 지정 좌석 역할을 창 몫으로 회계한다 — 받는 범위 안의 요청으로 좌석 프롬프트가 창을 넘지 않게
#
# 넓은 창(1백만 토큰)은 조항·후보안 항목 하나를 이어하기 요약 길이(29,077자)까지, 지정 좌석 역할도 그 길이까지 받는데(dcb1551)
# 그 합이 어느 몫(_ROOM_SHARE)에도 없었다. 조항 12건·후보안 8건이면 58만 자가 매 라운드 전 좌석에 실려 근거·챗 맥락·직전
# 라운드가 찬 심의에서 좌석 프롬프트가 창을 넘었다(engine-budget A23-3). 항목 상한은 그대로 두고 합을 carry 몫으로, 역할은
# role 몫으로 지킨다. 다단 심의는 조항을 _resolve_opts 없이 넘기므로 스트림이 싣는 자리에서 지킨다.
#
#   실행:  .venv/bin/python -m pytest tests/test_carry_budget.py -q
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

# _pin_context — 창 크기를 시험마다 되돌리는 자동 픽스처다(이름째 가져와야 이 파일에도 걸린다).
from test_delib_silent_drops import _cards, _pin_context, _Tool  # noqa: E402, F401
from test_window_budget_b import _BIG, _SEATS, _Llm, _run, _seat_plain, _win  # noqa: E402

_NN = [f"<조항{i}>" + "조" * 29000 + f"</조항{i}>" for i in range(12)]
_OPT = [f"<안{i}>" + "안" * 29000 + f"</안{i}>" for i in range(8)]


def _carry_of(human):
    """좌석 프롬프트에서 조항 블록과 후보안 블록의 몸통."""
    nn = human.split("[이전 심의의 양보 불가 조항", 1)[1].split("이 조항을 뒤집으려면", 1)[0]
    op = human.split("[후보안 — 이 중에서 고른다", 1)[1].split("\n\n[", 1)[0] if "[후보안 — " in human else ""
    return nn, op


def _seat_human(llm, key="mech-a"):
    return next(h for s, h in llm.prompts if f"당신은 '{key}' 전문가" in s)


def test_넓은_창의_조항과_후보안은_합을_carry_몫에_담고_건수는_그대로다(monkeypatch):
    events, llm = _run(monkeypatch, _BIG, non_negotiables=_NN, options=_OPT)
    nn, op = _carry_of(_seat_human(llm))
    assert all(f"<조항{i}>" in nn for i in range(12)) and all(f"<안{i}>" in op for i in range(8)), "건수가 줄었다"
    assert len(nn) + len(op) <= d._carry_max() + 400, (len(nn) + len(op), d._carry_max())    # 줄 머리·안내문 여유
    assert re.search(r"…\[29,011자 중 앞 [\d,]+자\]", nn), nn[:300]
    cards = [c for c in _cards(events, included=False) if c["source"] == "조항·후보안 몫 초과"]
    assert cards and "조항 12건·후보안 8건" in cards[0]["text"] and "건수는 그대로" in cards[0]["text"], cards


def test_몫_안이면_조항과_후보안을_줄이지_않는다(monkeypatch):
    events, llm = _run(monkeypatch, _BIG, non_negotiables=["<조항0>" + "조" * 9000 + "</조항0>"],
                       options=["<안0>A</안0>", "<안1>B</안1>"])
    nn, op = _carry_of(_seat_human(llm))
    assert "</조항0>" in nn and "</안1>" in op
    assert not [c for c in _cards(events, included=False) if c["source"] == "조항·후보안 몫 초과"]


def test_다단_심의처럼_요청_해석을_거치지_않은_조항도_몫에_담는다(monkeypatch):
    """해석 설계 2단은 1단 좌석의 조항(항목마다 이어하기 요약 길이까지)을 opts 에 바로 넣는다 — _resolve_opts 를 안 거친다."""
    _win(_BIG)
    llm = _Llm(_seat_plain, "결정: 진행한다")

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", llm.text)
    opts = d._resolve_opts({"personas": _SEATS, "rounds": 2, "free_tools": 0, "voc": "off", "rescreen": 0,
                            "persona_knowledge": 0, "save_report": 0})
    opts.continue_non_negotiables = list(_NN)          # run_sim_deliberation 의 opts_b 와 같은 길

    async def go():
        stub = SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=None))
        return [delib_jobs._parse_sse(c) async for c in d._deliberation_stream(stub, "해석 설계", [], opts)]

    events = asyncio.run(go())
    nn, _op = _carry_of(_seat_human(llm))
    assert all(f"<조항{i}>" in nn for i in range(12)) and len(nn) <= d._carry_max() + 400, len(nn)
    assert [c for c in _cards(events, included=False) if c["source"] == "조항·후보안 몫 초과"]


def test_좁은_창은_종전_항목_상한이_합을_묶어_줄이지_않는다():
    """좁은 창의 carry 바닥은 항목 상한의 최악(1,200자×12 + 400자×8)이다 — _resolve_opts 를 거친 값은 늘 든다."""
    _win(128000)
    o = d._resolve_opts({"non_negotiables": ["조" * 3000] * 12, "options": ["안" * 1000] * 8})
    nn, op, note = d._fit_carry(o.continue_non_negotiables, o.options)
    assert (nn, op, note) == (o.continue_non_negotiables, o.options, ""), note
    assert d._carry_max() == 1200 * 12 + 400 * 8


# ── 지정 좌석 역할 — role 몫(_role_req_max). 엔진과 잡 원장이 같은 길이다 ──────────────────────────────────────
@pytest.mark.parametrize("tokens,want", [(128000, 2000), (_BIG, None)])
def test_지정_좌석_역할은_role_몫까지_받고_넘으면_알린다(tokens, want):
    _win(tokens)
    want = want or d._role_req_max()
    o = d._resolve_opts({"personas": [{"key": "mech-a", "role": "역" * 29077}]})
    assert len(o.continue_personas[0]["role"]) == want and "personas" in o.req_cut, (len(o.continue_personas[0]["role"]),
                                                                                    o.req_cut)
    if tokens == _BIG:
        assert 2000 < want < d._summary_max(), want     # 넓은 창은 창에서 유도 — 종전 29,077자보다는 작다


def _ledger_role(monkeypatch, tmp_path, tokens, role):
    """잡을 세워 원장(seat_roles)에 적힌 역할을 받는다 — 엔진은 돌리지 않는다."""
    _win(tokens)

    async def _entry(*_a, **_k):
        yield d._sse("done", {})

    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(d, "run_deliberation", _entry)

    async def go():
        rec = delib_jobs.start(object(), "default", "q", delib_opts={"personas": [{"key": "cust-x", "role": role}]})
        await delib_jobs._TASKS[rec["id"]]
        return rec

    return asyncio.run(go())["seat_roles"]["cust-x"]


@pytest.mark.parametrize("tokens", (128000, _BIG))
def test_원장의_좌석_역할은_엔진이_받는_길이와_같다(monkeypatch, tmp_path, tokens):
    """다르면 이어하기가 역할을 엔진이 받은 것과 다른 길이로 되넘긴다(985ae57)."""
    role = "역" * 29077
    got = _ledger_role(monkeypatch, tmp_path, tokens, role)
    _win(tokens)
    assert got == d._resolve_opts({"personas": [{"key": "cust-x", "role": role}]}).continue_personas[0]["role"]
    assert len(got) == d._role_req_max(), len(got)

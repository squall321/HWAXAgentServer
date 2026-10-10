# 심의 뒤 절반(라운드·의장·후처리·보고서 저장·다단 승계)의 고정 상한을 모델 창에서 유도한다 — 좁은 창은 종전 그대로
#
# 사용자 지침(2026-10-10) — 운영 모델은 GLM-5.2, 창 1,048,576 토큰이다. 16K·128K 시절에 박은 글자 상한(기록용 발언
# 12,000자, 핵심 요약이 읽는 결정문 앞 6,000자, 쉬운 설명 7,000자, 다단 승계 8,000/20,000자, RA 보고서 '결과' 칸
# 1,500자 …)이 1백만 토큰 창에서도 걸려 글자가 잘렸다. 좁은 창(dev 16K)은 종전 동작을 지킨다(바닥값).
#
# 앞의 **특성화** 시험은 구현 전 코드에서 초록인 것을 먼저 본 것이다. 스트림을 실제로 돌리고 LLM 호출만 가로챈다.
#
#   실행:  .venv/bin/python -m pytest tests/test_window_budget_b.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _steps, _Tool  # noqa: E402, F401

_SMALL = (16384, 128000, 200000)
_BIG = 1048576          # GLM-5.2
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]


def _win(tokens):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()
    d._free_tok_cache.clear()


def _long(tag, n):
    """n 자 남짓의 글 — 꼬리에 tag 를 둔다(뒤가 잘렸는지 꼬리표로 본다)."""
    body = "".join(f"<{i:04d}>" + "가" * 34 for i in range(n // 40))
    return body + f" [{tag}]"


class _Llm:
    """좌석·의장·그 밖(핵심 요약·쉬운 설명 등)을 가른다. 그 밖의 호출은 (system, human) 으로 적어 둔다."""

    request_timeout = None
    max_retries = 0
    max_tokens = None

    def __init__(self, seat, chair):
        self.seat, self.chair, self.others = seat, chair, []

    async def text(self, _obj, system, human):
        m_ = d.re.search(r"당신은 '([^']+)' 전문가", system)
        if m_:
            return json.dumps(self.seat(m_.group(1)), ensure_ascii=False)
        if "엔지니어링 톤" in system:
            return self.chair
        self.others.append((system, human))
        return "- 요약 한 줄"


def _seat_plain(key):
    return {"lens": f"{key} 관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
            "position_short": "요약", "concede": [], "rebut": [], "deepen": f"{key} 심화",
            "final_position": f"{key} 의 최종 입장", "non_negotiable": "", "vote": "진행", "stance": "동의"}


def _run(monkeypatch, tokens, *, seat=_seat_plain, chair="결정: 진행한다", tools=None, entry=None, **req):
    """심의를 끝까지 돌려 (이벤트, LLM 대역)을 받는다."""
    _win(tokens)
    llm = _Llm(seat, chair)
    tools = {"agent_search": _Tool("agent_search")} if tools is None else tools

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", llm.text)
    stub = SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=None))
    opts = {"personas": _SEATS, "rounds": 2, "free_tools": 0, "voc": "off", "rescreen": 0,
            "persona_knowledge": 0, "save_report": 0, **req}

    async def go():
        return [delib_jobs._parse_sse(c)
                async for c in (entry or d.run_deliberation)(stub, "힌지 크랙 원인", [], opts)]

    return asyncio.run(go()), llm


def _turns(events):
    return [data for ev, data in events if ev == "delib" and data.get("kind") == "turn"]


# ── 기록용 발언(say_full)·RA 회의록 — 좁은 창은 종전 12,000자, 넓은 창은 끊지 않는다 ─────────────────
def _seat_long(key):
    return {**_seat_plain(key), "lens": _long(f"{key}-끝", 20000)}


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_기록용_발언은_종전대로_12000자에서_끊긴다(monkeypatch, tokens):
    events, _llm = _run(monkeypatch, tokens, seat=_seat_long)
    t = next(x for x in _turns(events) if x["round"] == 1 and x["persona"] == "mech-a")
    assert len(t["say_full"]) == 12000 and t["say_full_chars"] > 12000, (len(t["say_full"]), t["say_full_chars"])


def test_넓은_창의_기록용_발언은_끊지_않는다(monkeypatch):
    events, _llm = _run(monkeypatch, _BIG, seat=_seat_long)
    t = next(x for x in _turns(events) if x["round"] == 1 and x["persona"] == "mech-a")
    assert "[mech-a-끝]" in t["say_full"], f"1백만 토큰 창에서도 기록용 발언을 {len(t['say_full']):,}자에서 끊었다"
    assert len(t["say_full"]) == t["say_full_chars"]


def _saved_blocks(monkeypatch, tokens, **kw):
    save = _Tool("create_report_draft", '{"report_id": 7}')
    events, llm = _run(monkeypatch, tokens, save_report=1,
                       tools={"agent_search": _Tool("agent_search"), "create_report_draft": save}, **kw)
    assert len(save.calls) == 1, "보고서를 저장하지 않았다 — 시험 전제가 깨졌다"
    return save.calls[0]["blocks"], events


def test_넓은_창의_RA_회의록은_발언을_끊지_않는다(monkeypatch):
    blocks, _ev = _saved_blocks(monkeypatch, _BIG, seat=_seat_long)
    assert "[mech-a-끝]" in "\n".join(blocks["minutes"]), "RA 회의록이 발언을 12,000자에서 끊었다"


@pytest.mark.parametrize("tokens", (16384, _BIG))
def test_RA_결과_칸은_마지막_심화_라운드의_좌석을_전부_싣는다(monkeypatch, tokens):
    """'결과' 칸이 그 라운드 전사의 앞 1,500자였다 — 첫 좌석의 발언 일부만 남았다. 항목 상한(2,000자)은 나눠서 지킨다."""
    seats = [{"key": f"dom{i:02d}-seat", "role": "역할"} for i in range(1, 7)]
    blocks, _ev = _saved_blocks(monkeypatch, tokens, personas=seats, rounds=3,
                                seat=lambda k: {**_seat_plain(k), "deepen": _long(f"{k}-심화끝", 600)})
    got = "\n".join(blocks["results"])
    for s in seats:
        assert f"[{s['key']}-심화끝]" in got, f"RA '결과' 칸에 {s['key']} 의 심화가 없다"
    assert all(len(x) <= d._RA_ITEM_MAX for x in blocks["results"])


def test_RA_블록_건수_상한에_걸리면_원장에_알린다(monkeypatch):
    """문단·회의록 건수 상한(RA 보호값)은 두되, 걸렸으면 서버 로그만이 아니라 카드로 남긴다."""
    monkeypatch.setattr(d, "_RA_MINUTES_MAX", 3)
    blocks, events = _saved_blocks(monkeypatch, 128000)
    cards = [c for c in _cards(events, included=False) if c["source"] == "보고서 블록 상한 초과"]
    assert cards and "회의록" in cards[0]["text"], [c["source"] for c in _cards(events, included=False)]


def test_넓은_창의_MCP_전사_full_은_발언_전부를_표식_없이_준다(monkeypatch, tmp_path):
    """Claude 사용자가 받는 길 — 잡 원장에 쌓고 deliberate_transcript(full=true) 로 회수한다."""
    _win(_BIG)
    llm = _Llm(_seat_long, "결정: 진행한다")

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", llm.text)
    stub = SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=None))
    opts = {"personas": _SEATS, "rounds": 2, "free_tools": 0, "voc": "off", "rescreen": 0,
            "persona_knowledge": 0, "save_report": 0}
    j = {"id": "t-wide", "status": "running", "question": "q", "turns": [], "checkpoint": None,
         "started_at": delib_jobs._now()}
    monkeypatch.setitem(delib_jobs._JOBS, j["id"], j)
    asyncio.run(delib_jobs._drive(j, d.run_deliberation(stub, "힌지 크랙 원인", [], opts)))
    res = asyncio.run(m.deliberate_transcript(job_id="t-wide", round=1, seat="mech-a", full=True))
    t = res["turns"][0]
    assert "[mech-a-끝]" in t["say"] and "say_clipped" not in t, (len(t["say"]), t.get("say_clipped"))
    assert not res["note"], res["note"]


def test_전사_도구_설명이_full_의_해석_우려를_없다고_하지_않는다():
    """기록용 발언은 해석 목록·우려·양보 불가 조항까지 싣는다(5a56dd3) — 설명이 아직 '여기 없다' 고 적었다."""
    desc = next(t.description for t in asyncio.run(m.mcp.list_tools()) if t.name == "deliberate_transcript")
    assert "여기 없다" not in desc, desc
    assert "해석" in desc and "우려" in desc, desc

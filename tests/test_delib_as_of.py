# 심의 기준일(delib_opts.as_of) — 준 날까지의 지식카드만 찾고 날짜 없는 카드는 '날짜 미확인' 으로 남기며, 메아리가 없으면 카드를 버린다
#
# HWAXPortal docs/request-1027 HA-11 · 사용자 결정 10-31 A-keep(docs/delib-engine-feedback D-15). AIDataHub main 의 agent_search 는
# as_of · as_of_undated 를 받고, 주면 히트마다 valid_from 을 · applied_config 에 as_of 메아리를 싣는다. 옛 AIDataHub 는 모르는 인자를 말없이
# 버리므로 그 메아리가 거름이 걸렸다는 유일한 증거다.
#
# **실제 MCP 서버를 띄워서** 본다 — 이 박스의 빈 포트에 새 판(as_of 를 아는 agent_search)과 옛 판(모르는 agent_search)을 하나씩 올리고, 엔진이
# 쓰는 그 경로(_tools_by_name → MultiServerMCPClient → _prep_tool)로 부른다. 옛 판이 인자를 버리는 것도 진짜 FastMCP 가 한다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_as_of.py -q
import asyncio
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _pin_context, _SEATS, _steps, _stream, _Tool  # noqa: E402, F401

AS_OF = "2025-06-01"
_SEAT_JSON = json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A", "B"],
                         "position_short": "요약", "concede": [], "rebut": ["반박"], "deepen": "심화",
                         "final_position": "최종", "non_negotiable": "", "vote": "진행", "stance": "동의"}, ensure_ascii=False)
SEEN: dict = {"new": [], "old": []}      # 허브가 받은 인자 — 판마다


def _hits(agent_type: str, q: str, *, dated: bool) -> list:
    """좌석마다 카드 둘 — 날짜가 있는 것 하나와 없는 것 하나. q='big' 이면 본문을 길게(결과 상한 시험)."""
    body = "S-N 곡선 · 10^6 회 · 균열 진전 " * (300 if q == "big" else 1)
    out = [{"record_id": f"{agent_type}-r1", "section_id": "s1", "title": "힌지 피로", "snippet": body,
            "score": 0.9, "tags": ["confidence:fact"], "causal_status": "validated"},
           {"record_id": f"{agent_type}-r2", "section_id": "s1", "title": "날짜 없는 카드", "snippet": "벤딩 1만 회",
            "score": 0.8, "tags": [], "causal_status": "validated"}]
    if dated:
        out[0]["valid_from"], out[1]["valid_from"] = "2025-03-01", None
    return out


def _serve(mcp: FastMCP) -> tuple:
    with socket.socket() as s:                  # 빈 포트 — 떠 있는 서비스(9009·9110 등)와 겹치지 않게 커널이 고른다
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host="127.0.0.1", port=port,
                                           log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "시험용 MCP 서버가 뜨지 않았다"
    return server, thread, {"gateway": {"url": f"http://127.0.0.1:{port}/", "transport": "streamable_http"}}


@pytest.fixture(scope="module")
def hubs():
    """AIDataHub 대역 둘 — new 는 main 의 agent_search 시그니처(as_of · as_of_undated), old 는 그 인자가 없는 옛 판이다."""
    new = FastMCP("hwax-as-of-new", stateless_http=True, streamable_http_path="/")

    @new.tool()
    async def agent_search(agent_type: str, q: str, mode: str = "semantic", top_k: int | None = None,
                           as_of: str = "", as_of_undated: Literal["keep", "exclude"] = "keep") -> dict:
        """agent-scoped search(새 판)."""
        SEEN["new"].append({"agent_type": agent_type, "q": q, "mode": mode, "as_of": as_of, "as_of_undated": as_of_undated})
        cfg = {"top_k": 10, "agent_record_scope": 9}
        if as_of:
            cfg.update({"as_of": as_of, "as_of_excluded_later": 3, "as_of_undated_kept": 7})
        return {"agent_type": agent_type, "query": q, "mode": mode, "hits": _hits(agent_type, q, dated=bool(as_of)),
                "hit_count": 2, "refused": False, "applied_config": cfg}

    @new.tool()
    async def semantic_search(q: str, agent_type: str = "") -> dict:
        """기준일을 받지 않는 다른 검색 — 감싸지 않는다."""
        SEEN["new"].append({"tool": "semantic_search", "q": q})
        return {"hits": [], "count": 0}

    old = FastMCP("hwax-as-of-old", stateless_http=True, streamable_http_path="/")

    @old.tool(name="agent_search")
    async def agent_search_old(agent_type: str, q: str, mode: str = "semantic", top_k: int | None = None) -> dict:
        """agent-scoped search(옛 판 — as_of 를 모른다)."""
        SEEN["old"].append({"agent_type": agent_type, "q": q, "mode": mode})
        return {"agent_type": agent_type, "query": q, "mode": mode, "hits": _hits(agent_type, q, dated=False),
                "hit_count": 2, "refused": False, "applied_config": {"top_k": 10, "agent_record_scope": 9}}

    s_new, t_new, c_new = _serve(new)
    s_old, t_old, c_old = _serve(old)
    yield {"new": c_new, "old": c_old}
    for srv, th in ((s_new, t_new), (s_old, t_old)):
        srv.should_exit = True
        th.join(timeout=10)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """사내 프록시가 걸린 박스에서도 루프백은 곧장 가고, 도구 앱 지도는 떠 있는 게이트웨이(:9110)에 묻지 않는다."""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(app, "_tools_map", lambda: {})
    SEEN["new"].clear()
    SEEN["old"].clear()


def _load_call(conns, args: dict, *, as_of=AS_OF, result_max=None, name="agent_search"):
    """엔진이 도구를 부르는 그 길로 한 번 부른 결과 글 — 허브가 받은 인자는 SEEN 에 남는다."""
    from app import CATALOG_RESULT_MAX
    stub = SimpleNamespace(state=SimpleNamespace(connections=conns))

    async def go():
        # 기준일이 없으면 그 인자를 싣지 않는다 — 종전 그대로인지 보는 시험은 구현 전 코드에서도 같은 부름이어야 한다
        tools = await d._tools_by_name(stub, ["g"], CATALOG_RESULT_MAX if result_max is None else result_max,
                                       **({"as_of": as_of} if as_of else {}))
        return await d._call(tools, name, args)
    return asyncio.run(go())


# ── 꼴 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("v,ok", [
    ("2025-06-01", True), ("2024-02-29", True),
    ("2025-6-1", False), (" 2025-06-01", False), ("2025-06-01 ", False), ("2025-02-30", False), ("2025-13-01", False),
    ("2025-06-01T09:00:00", False), ("20250601", False), ("", False), (None, False), (20250601, False), (True, False),
    ("２０２５-０６-０１", False), ("٢٠٢٥-٠٦-٠١", False), ("2025-06-01\n", False),      # 전각 · 아라비아 숫자 · 끝 줄바꿈
])
def test_기준일은_YYYY_MM_DD_로_달력에_있는_날만_받는다(v, ok):
    assert d._as_of_ok(v) is ok


def test_요청에서_읽는다_안_주면_종전대로다():
    assert d._resolve_opts({"as_of": AS_OF}).as_of == AS_OF
    for empty in ({}, {"as_of": ""}, {"as_of": None}):
        o = d._resolve_opts(empty)
        assert o.as_of == "" and "as_of" not in o.req_unread, empty
    assert d._DEFAULT_OPTS.as_of == ""
    o = d._resolve_opts({"as_of": "2025-02-30"})       # 입구를 거치지 않고 읽는 쪽 — 거르지 않고 돈다는 것을 남긴다
    assert o.as_of == "" and "기준일 없이" in o.req_unread["as_of"], o.req_unread


# ── 입구 — 틀린 꼴은 시작하지 않는다 ─────────────────────────────────────────────
@pytest.mark.parametrize("run", ["run_deliberation", "run_test_plan", "run_sim_deliberation"])
def test_틀린_기준일은_도구를_불러오기_전에_거절한다(monkeypatch, run):
    loaded = []

    async def _never(*_a, **_k):
        loaded.append(1)
        return {}

    monkeypatch.setattr(d, "_tools_by_name", _never)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))

    async def go():
        return [delib_jobs._parse_sse(c) async for c in getattr(d, run)(stub, "힌지 크랙", [], {"as_of": "2025/06/01"})]
    events = asyncio.run(go())
    assert [ev for ev, _d in events] == ["error", "done"], events
    err = events[0][1]
    assert err["code"] == "bad_request" and "YYYY-MM-DD" in err["message"] and "시작하지 않았다" in err["message"], err
    assert not loaded, "거절할 심의가 게이트웨이에서 도구를 불러왔다"


def test_MCP_잡도_만들기_전에_거절한다(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    for store in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, store, {})
    with pytest.raises(ValueError, match="bad_request"):
        delib_jobs.start(object(), "default", "화두", delib_opts={"as_of": "2025-13-01"})
    assert not delib_jobs._JOBS and not delib_jobs._PENDING, "거절한 잡이 원장에 남았다"


# ── 도구에 박는다(실제 MCP 서버) ──────────────────────────────────────────────────
def test_새_허브는_기준일을_받고_날짜_없는_카드에_표시가_붙는다(hubs):
    out = _load_call(hubs["new"], {"agent_type": "mech-a", "q": "힌지"})
    assert SEEN["new"] == [{"agent_type": "mech-a", "q": "힌지", "mode": "semantic", "as_of": AS_OF, "as_of_undated": "keep"}]
    d_ = json.loads(out)
    assert next(iter(d_)) == "as_of_note", "기준일 안내가 맨 앞이 아니다 — 결과 상한에 잘린다"
    assert AS_OF in d_["as_of_note"] and "1장" in d_["as_of_note"], d_["as_of_note"]
    assert d_["applied_config"]["as_of"] == AS_OF
    marks = {h["record_id"]: h.get("date_note") for h in d_["hits"]}
    assert marks == {"mech-a-r1": None, "mech-a-r2": d._AS_OF_UNDATED}, marks
    line = d.knowledge_line(d_["hits"][1], limit=0)
    assert f"[{d._AS_OF_UNDATED}]" in line, line
    assert d._AS_OF_UNDATED not in d.knowledge_line(d_["hits"][0], limit=0)


def test_좌석이_빼거나_바꿔도_엔진의_기준일로_간다(hubs):
    out = _load_call(hubs["new"], {"agent_type": "mech-a", "q": "힌지", "as_of": "2030-01-01", "as_of_undated": "exclude"})
    assert [(s["as_of"], s["as_of_undated"]) for s in SEEN["new"]] == [(AS_OF, "keep")], SEEN["new"]
    assert "좌석이 넣은 as_of(2030-01-01)는 쓰지 않았다" in json.loads(out)["as_of_note"]


def test_옛_허브는_메아리가_없어_카드를_버린다(hubs):
    """FastMCP 는 모르는 인자를 말없이 버린다 — 옛 판은 거르지 않은 카드를 정상 응답으로 준다."""
    out = _load_call(hubs["old"], {"agent_type": "mech-a", "q": "힌지"})
    assert SEEN["old"] == [{"agent_type": "mech-a", "q": "힌지", "mode": "semantic"}], "시험 전제 — 옛 판이 응답했다"
    d_ = json.loads(out)
    assert next(iter(d_)) == d._AS_OF_UNCONFIRMED and d_["hits"] == [] and d_["refused"] is True, d_
    assert "2장을 싣지 않았다" in d_[d._AS_OF_UNCONFIRMED] and "메아리" in d_[d._AS_OF_UNCONFIRMED]
    assert "힌지 피로" not in out, "버린 카드가 글에 남았다"
    assert d._as_of_dropped(out)


def test_메아리가_다른_날이면_버린다():
    raw = json.dumps({"hits": [{"record_id": "r1", "valid_from": None}], "applied_config": {"as_of": "2024-01-01"}})
    got = json.loads(d._as_of_check(raw, AS_OF))
    assert got["hits"] == [] and "다른 날(2024-01-01)" in got[d._AS_OF_UNCONFIRMED], got
    # (content, artifact) 꼴 — 버리면 구조화 사본도 버린다, 맞으면 꼴과 사본을 그대로 둔다
    body, art = d._as_of_check(([{"type": "text", "text": raw}], {"structured": 1}), AS_OF)
    assert art is None and d._as_of_dropped(body)
    ok = json.dumps({"hits": [], "applied_config": {"as_of": AS_OF}})
    body, art = d._as_of_check(([{"type": "text", "text": ok}], {"structured": 1}), AS_OF)
    assert art == {"structured": 1} and json.loads(body)["as_of_note"]
    # 검색 결과 꼴이 아닌 답(오류 글)은 실린 카드가 없어 그대로 둔다
    assert d._as_of_check("agent not found: x", AS_OF) == "agent not found: x"


def test_결과_상한보다_안쪽에서_본다(hubs):
    """좌석 자유 조회는 결과를 상한에서 자른다 — 메아리(applied_config)는 끝에 있어, 자른 뒤에 보면 멀쩡한 답도 버린다."""
    new = _load_call(hubs["new"], {"agent_type": "mech-a", "q": "big"}, result_max=600)
    assert "잘렸습니다" in new and new.startswith('{"as_of_note"'), new[:200]
    old = _load_call(hubs["old"], {"agent_type": "mech-a", "q": "big"}, result_max=600)
    assert d._as_of_dropped(old) and "S-N 곡선" not in old, old[:300]


def test_기준일이_없으면_종전_그대로다(hubs):
    """특성화 — 구현 전 코드에서 초록이었다. as_of 를 안 준 심의 · 챗 · 띵킹 · 카드 대조는 같은 인자 · 같은 글이다."""
    out = _load_call(hubs["new"], {"agent_type": "mech-a", "q": "힌지"}, as_of="")
    assert SEEN["new"] == [{"agent_type": "mech-a", "q": "힌지", "mode": "semantic", "as_of": "", "as_of_undated": "keep"}]
    d_ = json.loads(out)
    assert "as_of_note" not in d_ and all("date_note" not in h and "valid_from" not in h for h in d_["hits"])


def test_다른_검색_도구는_감싸지_않는다(hubs):
    _load_call(hubs["new"], {"q": "힌지", "agent_type": "mech-a"}, name="semantic_search")
    assert SEEN["new"] == [{"tool": "semantic_search", "q": "힌지"}]


# ── 심의 — 좌석 사전 검색(실제 MCP 서버 · 실제 _tools_by_name) ──────────────────────────
def _delib(monkeypatch, conns, req_opts, *, done=False):
    """run_deliberation 을 실제로 돌린다 — 도구는 시험 허브에서 엔진의 로드 길로 받는다. done 이면 결정문까지(LLM 은 대역)."""
    async def _llm(_llm_obj, system, _human, *_img):
        return _SEAT_JSON if "전문가입니다" in system else "결정문 본문"

    monkeypatch.setattr(d, "_llm_text", _llm)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None, connections=conns))
    opts = {"personas": _SEATS, "free_tools": 0, "voc": "off", "rescreen": 0, "rounds": 2, "save_report": 0,
            "rebut_quote": 0, **req_opts}

    def _stop(ev, data):
        return ev == "done" if done else (ev == "delib" and data.get("kind") == "stage" and str(data.get("stage")) == "r1")

    async def go():
        out, gen = [], d.run_deliberation(stub, "힌지 크랙 원인", [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                out.append((ev, data))
                if _stop(ev, data):
                    break
        finally:
            await gen.aclose()
        return out
    events = asyncio.run(go())
    assert not [x for ev, x in events if ev == "error"], events
    return events


def _decision(events) -> str:
    return next(x["text"] for ev, x in events if ev == "delib" and x.get("kind") == "decision")


def test_좌석_사전_검색이_기준일로_거르고_좌석마다_센다(monkeypatch, hubs):
    events = _delib(monkeypatch, hubs["new"], {"as_of": AS_OF})
    asked = sorted((s["agent_type"], s["as_of"], s["as_of_undated"]) for s in SEEN["new"])
    assert asked == [("mech-a", AS_OF, "keep"), ("rel-b", AS_OF, "keep")], SEEN["new"]
    kn = {c["source"]: c["text"] for c in _cards(events, included=True) if c["source"].endswith("· 지식카드")}
    assert set(kn) == {"mech-a · 지식카드", "rel-b · 지식카드"}, kn
    for text in kn.values():
        assert text.startswith(f"(기준일 {AS_OF} 까지의 카드다") and f"[{d._AS_OF_UNDATED}]" in text, text
    steps = _steps(events)
    assert any(s.startswith(f"기준일 {AS_OF} — ") and "날짜로 거르지 않는 것" in s for s in steps), steps
    assert sum(f"기준일 {AS_OF} — {d._AS_OF_UNDATED} 1줄" in s for s in steps if s.startswith("지식카드 ")) == 2, steps
    seat = next(c["text"] for c in _cards(events, included=False) if c["source"] == "기준일 — 좌석 지식카드")
    for k in ("mech-a", "rel-b"):
        assert f"{k} 실은 2줄 중 {d._AS_OF_UNDATED} 1줄(좌석 범위의 날짜 없는 카드 7장 · 그 날 뒤라 뺀 카드 3장)" in seat, seat
    assert not [x for ev, x in events if ev == "warning" and x.get("code") == "as_of_unconfirmed"]


def test_옛_허브면_그_좌석의_카드를_싣지_않고_알린다(monkeypatch, hubs):
    events = _delib(monkeypatch, hubs["old"], {"as_of": AS_OF})
    assert len(SEEN["old"]) == 2, "시험 전제 — 좌석마다 물었다"
    assert not [c for c in _cards(events, included=True) if c["source"].endswith("· 지식카드")], "거름을 확인 못 한 카드를 실었다"
    warn = [x for ev, x in events if ev == "warning" and x.get("code") == "as_of_unconfirmed"]
    assert len(warn) == 1 and "2/2명" in warn[0]["message"] and "mech-a" in warn[0]["message"], warn
    card = [c for c in _cards(events, included=False) if c["source"] == "기준일 거름 확인 안 됨"]
    assert len(card) == 1 and "rel-b" in card[0]["text"], card
    rows = [x for ev, x in events if ev == "status" and x.get("knowledge")]
    assert rows and all(r["knowledge"]["degraded"] and r["knowledge"]["hits"] == 0 for r in rows), rows
    assert all("기준일 거름을 확인하지 못해" in r["step"] for r in rows), [r["step"] for r in rows]


def test_감싸지_않은_도구가_와도_사전_검색은_메아리를_다시_본다(monkeypatch):
    """도구 감싸기(_as_of_guard)를 거치지 않은 agent_search 가 메아리 없이 카드를 주면 — 사전 검색이 따로 다시 보고 싣지 않는다(닫힌 쪽)."""
    raw = json.dumps({"hits": _hits("mech-a", "힌지", dated=False), "refused": False, "applied_config": {"top_k": 10}},
                     ensure_ascii=False)
    events = _stream(monkeypatch, {"as_of": AS_OF}, tools={"agent_search": _Tool("agent_search", answer=raw)})
    assert not [c for c in _cards(events, included=True) if c["source"].endswith("· 지식카드")], "메아리 없는 카드를 실었다"
    assert [x for ev, x in events if ev == "warning" and x.get("code") == "as_of_unconfirmed"], "버리고도 말하지 않았다"


def test_기준일이_없으면_좌석_검색은_종전대로다(monkeypatch, hubs):
    """특성화 — 구현 전 코드에서 초록이었다."""
    events = _delib(monkeypatch, hubs["new"], {})
    assert sorted((s["agent_type"], s["as_of"]) for s in SEEN["new"]) == [("mech-a", ""), ("rel-b", "")]
    kn = [c["text"] for c in _cards(events, included=True) if c["source"].endswith("· 지식카드")]
    assert len(kn) == 2 and not any("기준일" in t or "날짜 미확인" in t for t in kn), kn
    assert not any(s.startswith("기준일 ") for s in _steps(events))
    assert not [c for c in _cards(events, included=False) if c["source"].startswith("기준일")]


def test_결정문_머리에_기준일_줄을_코드가_찍는다(monkeypatch, hubs):
    dec = _decision(_delib(monkeypatch, hubs["new"], {"as_of": AS_OF}, done=True))
    head = dec.split("\n\n", 1)[0]
    assert head.startswith(f"■ 기준일 — {AS_OF}. "), dec[:300]
    assert f"사전 검색 2석 — 실은 카드 4줄 가운데 {d._AS_OF_UNDATED} 2줄 · 그 날 뒤라 뺀 카드 6장" in head, head
    assert "질문에서 짐작하지 않았다" in head and "거름을 확인해 준 카드만 실었다" in head and d._AS_OF_OPEN in head, head
    assert "이 거름이 걸린 조회가 없었다" not in head, "부른 조회가 있는데 없었다고 적었다"
    dec_old = _decision(_delib(monkeypatch, hubs["old"], {"as_of": AS_OF}, done=True))
    head_old = dec_old.split("\n\n", 1)[0]
    assert "카드를 싣지 않은 좌석: mech-a, rel-b" in head_old, head_old
    assert "이 거름이 걸린 조회가 없었다" not in head_old, "메아리가 없어 버린 조회도 부른 조회다"
    assert not _decision(_delib(monkeypatch, hubs["new"], {}, done=True)).startswith("■ 기준일"), "안 준 심의에 기준일 줄을 찍었다"


def test_봉인_줄이_위고_기준일_줄이_그_아래다(monkeypatch, hubs):
    """봉인은 지식카드 · 자유 조회를 닫는다 — 기준일 줄은 거름이 걸린 조회가 없었다고 적는다."""
    dec = _decision(_delib(monkeypatch, hubs["new"], {"as_of": AS_OF, "sealed": 1}, done=True))
    first, second = dec.split("\n\n", 2)[:2]
    assert first.startswith("■ 봉인 실행 — ") and second.startswith(f"■ 기준일 — {AS_OF}. "), dec[:500]
    assert "이 거름이 걸린 조회가 없었다" in second, second
    assert SEEN["new"] == [], "봉인 심의가 지식카드를 물었다"


# ── 심의 — 좌석 자유 조회(라운드의 셈 · 알림) ──────────────────────────────────────────
def test_자유_조회의_agent_search_도_박고_라운드가_세며_한_번_알린다(monkeypatch):
    import langgraph.prebuilt

    gathered = []
    ok = d._as_of_check(json.dumps({"hits": [{"record_id": "r1", "valid_from": None}, {"record_id": "r2", "valid_from": None},
                                             {"record_id": "r3", "valid_from": "2025-01-02"}],
                                    "applied_config": {"as_of": AS_OF}}, ensure_ascii=False), AS_OF)
    bad = d._as_of_check(json.dumps({"hits": [{"record_id": "x1", "snippet": "오늘의 카드"}], "applied_config": {}}), AS_OF)

    async def _llm(_llm_obj, system, _human, *_img):
        return _SEAT_JSON if "전문가입니다" in system else "결정문 본문"

    async def _gather(_agent, persona, *_a, **_k):
        gathered.append(persona["key"])
        out = ok if persona["key"] == "mech-a" else bad
        return persona["key"], [("agent_search", '{"q": "힌지"}', out, f"{persona['key']}-c{len(gathered)}")], "- 조회", ""

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: object())
    events = _stream(monkeypatch, {"rounds": 2, "save_report": 0, "rebut_quote": 0, "free_tools": 1, "persona_knowledge": 0,
                                   "as_of": AS_OF}, tools={"agent_search": _Tool("agent_search")},
                     until=lambda ev, _data: ev == "done")
    warn = [x for ev, x in events if ev == "warning" and x.get("code") == "as_of_unconfirmed"]
    assert len(warn) == 1 and "rel-b" in warn[0]["message"] and "mech-a" not in warn[0]["message"], warn
    assert len([c for c in _cards(events, included=False) if c["source"] == "기준일 거름 확인 안 됨 — 자유 조회"]) == 1
    assert not [c for c in _cards(events, included=True) if c.get("tool") == "agent_search" and "rel-b" in c["source"]], (
        "버린 답을 근거로 실었다")
    n_mech, n_rel = gathered.count("mech-a"), gathered.count("rel-b")
    assert n_mech and n_rel, gathered
    head = _decision(events).split("\n\n", 1)[0]
    assert f"자유 조회의 agent_search {n_mech + n_rel}건 — 결과에 {d._AS_OF_UNDATED} {2 * n_mech}장" in head, head
    assert "카드를 싣지 않은 좌석: rel-b" in head, head


def test_두_도구_로드가_모두_기준일을_받는다(monkeypatch):
    """엔진이 부르는 로드(사전 검색 · 지정 도구)와 좌석 자유 조회의 로드 — 하나라도 빠지면 그 길의 agent_search 가 오늘의 카드를 받는다."""
    import langgraph.prebuilt
    loads = []

    async def _tools(*_a, **kw):
        loads.append(kw.get("as_of"))
        return {"agent_search": _Tool("agent_search")}

    async def _gather(_agent, persona, *_a, **_k):
        return persona["key"], [], "", ""

    async def _llm(_llm_obj, system, _human, *_img):
        return _SEAT_JSON if "전문가입니다" in system else "결정문 본문"

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: object())
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))
    monkeypatch.setattr(d, "_tools_by_name", _tools)

    async def go(req):
        gen = d.run_deliberation(stub, "힌지", [], {"personas": _SEATS, "voc": "off", "rescreen": 0, "free_tools": 1,
                                                    "rounds": 2, "save_report": 0, "rebut_quote": 0, **req})
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                if ev == "delib" and data.get("kind") == "stage" and str(data.get("stage")) == "r1":
                    break
        finally:
            await gen.aclose()
    asyncio.run(go({"as_of": AS_OF}))
    assert loads == [AS_OF, AS_OF], loads
    loads.clear()
    asyncio.run(go({}))
    assert loads == ["", ""], loads


# ── MCP — 안내 · 잡 기록 · 이어하기 ────────────────────────────────────────────────
def test_MCP_안내에_있고_이어하기가_이어받는다(monkeypatch, tmp_path):
    from test_delib_sealed import _continue, _run
    from test_mcp_start_desc import _listed
    for text in (_listed("deliberate_start"), asyncio.run(m.deliberate_jobs())["options"]["advanced"]):
        part = text[text.index("as_of("):][:600]
        assert "YYYY-MM-DD" in part and d._AS_OF_UNDATED in part and "결정문 머리" in part and d._AS_OF_OPEN in part, part
    assert "as_of" in m._CONT_CARRY
    assert d._resolve_opts(m._build_opts(advanced={"as_of": AS_OF})).as_of == AS_OF
    r = _run(monkeypatch, tmp_path, job="diagnosis", advanced={"as_of": AS_OF})
    assert r.job["opts"]["as_of"] == AS_OF, r.job["opts"]
    assert asyncio.run(m.deliberate_result(r.job["id"]))["applied_opts"]["as_of"] == AS_OF
    # 하네스의 가짜 agent_search 는 메아리가 없다 — 엔진이 그 좌석의 카드를 버리고 결정문 머리에 적는다
    assert r.job["decision"].startswith(f"■ 기준일 — {AS_OF}. ") and "카드를 싣지 않은 좌석: mech-a, rel-b" in r.job["decision"]
    assert r.args["agent_search"].get("as_of") is None, "하네스 전제 — 가짜 도구는 감싸지 않은 사전이다"
    c = _continue(r)
    assert c.job["opts"]["as_of"] == AS_OF, f"이어하기가 기준일을 놓았다 — {c.job['opts']}"
    assert c.job["decision"].startswith(f"■ 기준일 — {AS_OF}. ")
    c2 = _continue(r, advanced={"as_of": "2024-12-31"})
    assert c2.job["opts"]["as_of"] == "2024-12-31", "이번 회차에 다시 건 값이 이기지 않았다"


# ── 반대 검토 10-31 — '날짜 미확인' 표지가 잘리지 않는다 ────────────────────────────────────────────────────────────────
# 실측 좌석 카드 절 길이(dev 읽기 — 중앙값 1,858자 · p90 2,309자)의 카드 10장을 좌석이 두 번 조회한다. 창 200,000(cae00 GLM)이면 발언 턴의 조회
# 블록 3,500자 · 공용 항목 840자 · 근거 카드 2,000자라 결과의 앞부분만 실린다. 종전엔 '날짜 미확인' 표지를 히트 맨 끝에 달아 제목은 보이고
# 표지만 잘려 나갔는데 결정문 머리는 '표시했다' 고 적었다.
_LONG = 1_900
_UNDATED_TITLE = re.compile(r'"title": "(UNDATED-[a-z]+-\d+)"')


@pytest.fixture
def _cae00_window():
    app._ctx_cache["n"] = 200_000         # _pin_context(128,000) 뒤에 덮는다 — cae00 GLM 의 창
    d._evid_cache.clear()
    yield
    d._evid_cache.clear()


@pytest.fixture(scope="module")
def long_hub():
    """새 판 허브 — 히트 10장(짝수 번째가 날짜 없는 카드), 절 본문 _LONG 자. 받은 인자를 SEEN['long'] 에 적는다."""
    SEEN["long"] = []
    hub = FastMCP("hwax-as-of-long", stateless_http=True, streamable_http_path="/")

    @hub.tool()
    async def agent_search(agent_type: str, q: str, mode: str = "semantic", top_k: int | None = None,
                           as_of: str = "", as_of_undated: Literal["keep", "exclude"] = "keep") -> dict:
        """agent-scoped search(새 판 · 긴 절)."""
        SEEN["long"].append({"agent_type": agent_type, "q": q, "as_of": as_of, "as_of_undated": as_of_undated})
        hits = []
        for i in range(10):
            und = i % 2 == 0
            h = {"record_id": f"{agent_type}-{q}-{i}", "section_id": "s1", "title": f"{'UNDATED' if und else 'DATED'}-{q}-{i}",
                 "section_title": "본문", "data_type": "DOC", "snippet": "가" * _LONG, "score": 0.9 - i / 100, "tags": [],
                 "causal_status": "validated"}
            if as_of:
                h["valid_from"] = None if und else "2025-03-01"
            hits.append(h)
        cfg = {"top_k": 10, "as_of": as_of, "as_of_excluded_later": 4, "as_of_undated_kept": 5} if as_of else {"top_k": 10}
        return {"agent_type": agent_type, "query": q, "mode": mode, "hits": hits, "hit_count": 10, "refused": False,
                "applied_config": cfg}

    srv, th, conns = _serve(hub)
    yield conns
    srv.should_exit = True
    th.join(timeout=10)


def _unmarked(text: str) -> tuple[list, list]:
    """글에 보이는 날짜 없는 카드 제목 — (표지와 함께 보이는 것, 표지 없이 보이는 것). 표지는 그 히트 객체 안, 제목보다 앞에 있어야 한다
    (본문 snippet 에는 중괄호가 없어 제목 앞의 가장 가까운 '{' 가 그 히트의 머리다)."""
    ok, bad = [], []
    for m in _UNDATED_TITLE.finditer(text):
        head = text.rfind("{", 0, m.start())
        (ok if head >= 0 and text.startswith('{"date_note": "' + d._AS_OF_UNDATED + '"', head) else bad).append(m.group(1))
    return ok, bad


class _LookupAgent:
    """좌석 자유 조회 대역 — 받은 도구(엔진의 로드 길에서 온 진짜 MCP 도구)로 agent_search 를 차례로 부른다. calls 는 [(q, 넣은 인자 더)]."""

    def __init__(self, tools, calls):
        self.tools, self.calls = {t.name: t for t in tools}, calls

    async def astream(self, inp, config=None, stream_mode="values"):
        from langchain_core.messages import AIMessage, ToolMessage
        key = "mech-a" if "'mech-a'" in inp["messages"][0][1] else "rel-b"
        msgs = []
        for j, (q, extra) in enumerate(self.calls):
            call = {"name": "agent_search", "args": {"agent_type": key, "q": q, **extra}, "id": f"c-{key}-{j}", "type": "tool_call"}
            msgs.append(AIMessage(content="", tool_calls=[call]))
            out = await self.tools["agent_search"].ainvoke(call)
            msgs.append(out if isinstance(out, ToolMessage) else ToolMessage(str(out), name="agent_search", tool_call_id=call["id"]))
            yield {"messages": list(msgs)}
        msgs.append(AIMessage(content="조회 요약"))
        yield {"messages": list(msgs)}


def _seat_run(monkeypatch, conns, req_opts, calls, *, planner=None):
    """run_deliberation 을 끝까지 — 도구는 시험 허브에서 엔진의 로드 길로 받고, 좌석 자유 조회는 _LookupAgent 가 calls 대로 부른다.
    (이벤트, 1라운드 좌석 발언 프롬프트들)."""
    import langgraph.prebuilt
    prompts = []

    async def _llm(_o, system, human, *_img):
        if "도구 호출 계획자" in system:
            return planner or '{"skip": true}'
        if "전문가입니다" in system:
            prompts.append(human)
            return _SEAT_JSON
        return "결정문 본문"

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, tools, **_k: _LookupAgent(tools, calls))
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None, connections=conns))
    opts = {"personas": _SEATS, "free_tools": 1, "voc": "off", "rescreen": 0, "rounds": 2, "save_report": 0,
            "rebut_quote": 0, "persona_knowledge": 0, **req_opts}

    async def go():
        out, gen = [], d.run_deliberation(stub, "힌지 크랙 원인", [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                out.append((ev, data))
                if ev == "done":
                    break
        finally:
            await gen.aclose()
        return out
    events = asyncio.run(go())
    assert not [x for ev, x in events if ev == "error"], events
    return events, prompts


def test_긴_카드를_두_번_조회해도_제목이_보이는_자리마다_날짜_미확인_표지가_보인다(monkeypatch, long_hub, _cae00_window):
    events, prompts = _seat_run(monkeypatch, long_hub, {"as_of": AS_OF}, [("qa", {}), ("qb", {})])
    r1 = prompts[:2]
    assert len(r1) == 2
    for p in r1:
        ok, bad = _unmarked(p)
        assert ok or bad, "시험 전제 — 1라운드 발언 프롬프트에 날짜 없는 카드의 제목이 보인다"
        assert not bad, f"발언 턴에 표지 없이 보이는 날짜 없는 카드: {bad}"
    cards = [c for c in _cards(events, included=True) if c.get("tool") == "agent_search"]
    assert len(cards) == 4, [c["source"] for c in cards]
    for c in cards:
        ok, bad = _unmarked(c["text"])
        assert (ok or bad) and not bad, (c["source"], ok, bad)
    head = _decision(events).split("\n\n", 1)[0]
    assert f"자유 조회의 agent_search 4건 — 결과에 {d._AS_OF_UNDATED} 20장" in head, head


def test_지정_도구로_실은_긴_카드도_제목_앞에_표지가_있다(monkeypatch, long_hub, _cae00_window):
    events, prompts = _seat_run(monkeypatch, long_hub, {"as_of": AS_OF, "tools": ["agent_search"], "free_tools": 0}, [],
                                planner='{"agent_type": "mech-a", "q": "qd"}')
    blk = prompts[0][prompts[0].index("[사용자 지정 도구 정량 결과"):]
    ok, bad = _unmarked(blk)
    assert (ok or bad) and not bad, (ok, bad)


# ── 반대 검토 10-31 — 기준일 없는 심의의 좌석은 날로 거르지 못한다(A-keep — 심의가 날을 댈 때만 거른다) ──────────────────────────────
def test_기준일_없는_심의에서는_좌석이_넣은_as_of_가_허브에_닿지_않는다(monkeypatch, long_hub):
    """A-keep — 심의가 날을 댈 때만 거른다. 좌석이 화두의 날짜를 보고 스스로 as_of · 'exclude' 를 넣으면 허브가 그대로 걸렀고(날짜 없는 카드까지
    뺀다) 엔진은 아무것도 세지 않아 결정문에 흔적이 없었다."""
    SEEN["long"].clear()
    events, _p = _seat_run(monkeypatch, long_hub, {}, [("qa", {"as_of": "2024-01-01", "as_of_undated": "exclude"})])
    seat_calls = [s for s in SEEN["long"] if s["q"] == "qa"]
    assert seat_calls and all((s["as_of"], s["as_of_undated"]) == ("", "keep") for s in seat_calls), seat_calls
    cards = [c for c in _cards(events, included=True) if c.get("tool") == "agent_search"]
    assert cards and all("좌석이 넣은 as_of(2024-01-01)" in c["text"] and "보내지 않았다" in c["text"] for c in cards), \
        [c["text"][:200] for c in cards]
    assert not _decision(events).startswith("■ 기준일"), "기준일을 주지 않은 심의에 기준일 줄을 찍었다"


def test_심의의_로드는_좌석에_기준일_칸을_보이지_않고_넣어도_걷는다(hubs):
    """도구 하나로 본다 — 심의의 로드(as_of 를 '' 로 준다)는 스키마에서 두 칸을 지우고 넣은 값을 걷는다. 심의 밖의 로드(as_of 를 안 준다 — 챗 ·
    띵킹 · 카드 대조)는 종전 그대로다."""
    stub = SimpleNamespace(state=SimpleNamespace(connections=hubs["new"]))

    async def go(**kw):
        tools = await d._tools_by_name(stub, ["g"], 2_000, **kw)
        t = tools["agent_search"]
        props = sorted((t.args_schema or {}).get("properties", {}))
        out = await d._call(tools, "agent_search", {"agent_type": "mech-a", "q": "힌지", "as_of": "2024-01-01",
                                                     "as_of_undated": "exclude"})
        return props, out
    props, out = asyncio.run(go(as_of=""))
    assert "as_of" not in props and "as_of_undated" not in props and {"agent_type", "q"} <= set(props), props
    assert SEEN["new"][-1]["as_of"] == "" and SEEN["new"][-1]["as_of_undated"] == "keep", SEEN["new"]
    got = json.loads(out)
    assert next(iter(got)) == "as_of_note" and "as_of" not in got["applied_config"], got
    props, _out = asyncio.run(go(as_of=AS_OF))
    assert "as_of" not in props and "as_of_undated" not in props, "기준일 심의도 엔진이 박는 칸이다 — 좌석이 고를 칸이 아니다"
    assert (SEEN["new"][-1]["as_of"], SEEN["new"][-1]["as_of_undated"]) == (AS_OF, "keep")
    props, _out = asyncio.run(go())
    assert {"as_of", "as_of_undated"} <= set(props), "심의 밖의 로드 스키마가 바뀌었다"
    assert (SEEN["new"][-1]["as_of"], SEEN["new"][-1]["as_of_undated"]) == ("2024-01-01", "exclude"), "심의 밖의 로드가 바뀌었다"


# ── 반대 검토 10-31 — 지정 도구로 고른 agent_search 도 기준일 셈에 든다 ───────────────────────────────────────────────────
@pytest.mark.parametrize("which", ["old", "new"])
def test_지정_도구로_고른_agent_search_도_기준일_셈에_들고_옛_허브면_다시_시키지_않는다(monkeypatch, hubs, which):
    async def _llm(_o, system, human, *_img):
        if "도구 호출 계획자" in system:
            return '{"agent_type": "mech-a", "q": "힌지"}'
        return _SEAT_JSON if "전문가입니다" in system else "결정문 본문"

    monkeypatch.setattr(d, "_llm_text", _llm)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None, connections=hubs[which]))
    opts = {"personas": _SEATS, "free_tools": 0, "voc": "off", "rescreen": 0, "rounds": 2, "save_report": 0,
            "rebut_quote": 0, "persona_knowledge": 0, "tools": ["agent_search"], "as_of": AS_OF}

    async def go():
        out, gen = [], d.run_deliberation(stub, "힌지 크랙 원인", [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                out.append((ev, data))
                if ev == "done":
                    break
        finally:
            await gen.aclose()
        return out
    events = asyncio.run(go())
    head = _decision(events).split("\n\n", 1)[0]
    assert "이 거름이 걸린 조회가 없었다" not in head, f"지정 도구로 부른 조회가 있는데 없었다고 적었다 — {head}"
    warn = [x for ev, x in events if ev == "warning" and x.get("code") == "as_of_unconfirmed"]
    miss = [c["text"] for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행"]
    if which == "old":
        assert len(SEEN["old"]) == 1, f"메아리가 없어 버린 답을 일반 실패처럼 다시 시켰다 — {SEEN['old']}"
        assert len(warn) == 1 and "지정 도구" in warn[0]["message"], warn
        assert miss and "기준일" in miss[0] and "호출 실패·인자를 못 정해 건너뜀" not in miss[0], miss
        assert "지정 도구 agent_search 1건" in head and "싣지 않았다" in head, head
    else:
        assert [s["as_of"] for s in SEEN["new"]] == [AS_OF] and not warn and not miss, (SEEN["new"], warn, miss)
        assert f"지정 도구 agent_search 1건 — 결과에 {d._AS_OF_UNDATED} 1장" in head, head


# ── 반대 검토 10-31 — 포털이 이 판인지 본다 ───────────────────────────────────────────────────────────────────────────
def test_health_가_기준일을_받는_판이라고_알린다(monkeypatch):
    """포털이 기준일 심의를 넘기기 전에 이 표지를 본다 — 옛 엔진은 delib_opts.as_of 를 말없이 버려(_resolve_opts 에 칸이 없다) 기준일을 준 소급
    검증이 오늘의 카드로 돌았다(반대 검토 10-31 — 포털을 먼저 올리고 에이전트 서버 재기동이 미뤄진 창). 이름을 바꾸면 포털이 모든 기준일 심의를
    막는다(HWAXPortal backend/app/agent/routes.py 가 이 이름을 읽는다)."""
    monkeypatch.setattr(app, "_model_context_tokens", lambda: 128000)       # 모델 서버에 묻지 않는다
    assert app.health()["delib_as_of"] is True

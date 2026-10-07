# 심의가 조용히 버리거나 헛돌던 것들의 회귀 테스트 — 버렸으면 화면(SSE)과 잡 원장(MCP)에 남는다
#
# S26U 잠재리스크 심사 팀이 엔진을 밖에서 관찰해 보낸 것들이다(2026-10-07). 절반이 같은
# 모양이었다 — 잘리거나 버려지는데 아무도 모른다.
#
# **스트림을 실제로 돌려서** 본다. 이 결함들은 "이벤트가 나가느냐" 의 문제라, 함수만 떼어
# 시험하면 '카드를 만드는 코드는 있는데 그 줄에 도달하지 않는다' 를 못 잡는다(근거가 전부
# 버려지면 `if opts.evidence:` 블록에 아예 안 들어간다 — 정확히 그 모양이었다).
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_silent_drops.py -q
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_context():
    """모델 컨텍스트를 고정한다 — 안 하면 예산이 그 박스에 떠 있는 모델을 따라가고,
    모델이 안 떠 있으면 /v1/models 를 5초 기다린다(tests/test_doc_attach 와 같은 이유)."""
    saved = dict(app._ctx_cache)
    app._ctx_cache["n"] = 128000
    d._evid_cache.clear()
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


class _Tool:
    """_call 이 기대하는 최소 모양 — 받은 인자를 적어 두고 정해 둔 답을 준다."""

    coroutine = None      # 자유 조회 준비(_wrap_cached)가 보는 자리 — 없으면 그대로 둔다

    def __init__(self, name, answer='{"hits": []}'):
        self.name, self._answer, self.calls = name, answer, []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._answer


_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]


def _at_round1(ev, data):
    return ev == "delib" and data.get("kind") == "stage" and str(data.get("stage")) == "r1"


def _stream(monkeypatch, req_opts, *, tools=None, until=_at_round1, llm=None):
    """run_deliberation 을 **실제로** 돌려 이벤트 목록을 받는다. `until` 이 참이 되면 멈춘다
    (기본은 1라운드 문턱 — 거기까지는 LLM 이 필요 없다).

    좌석은 지정으로 주고(발굴 생략) 자유 조회·VOC·재심사를 끈다 — 전부 망을 타는 단계다.
    `llm` 은 심의 LLM 자리에 끼울 객체다(엔진이 그 설정값을 읽는 시험용 — 호출은 시험이 가로챈다).
    """
    tools = {"agent_search": _Tool("agent_search")} if tools is None else tools

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    stub = SimpleNamespace(state=SimpleNamespace(llm=llm or object(), delib_llm=None))
    opts = {"personas": _SEATS, "free_tools": 0, "voc": "off", "rescreen": 0, **req_opts}

    async def go():
        out, gen = [], d.run_deliberation(stub, "힌지 크랙 원인", [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                out.append((ev, data))
                if until(ev, data):
                    break
        finally:
            await gen.aclose()
        return out

    events = asyncio.run(go())
    # run_deliberation 은 예외를 error 이벤트로 삼킨다 — 하네스가 깨진 것을 통과로 읽지 않게 한다.
    errs = [data for ev, data in events if ev == "error"]
    assert not errs, f"스트림이 오류로 끝났다 — {errs}"
    assert events and until(*events[-1]), "멈출 지점까지 가지 못했다 — 하네스가 낡았다"
    return events


def _steps(events):
    return [data.get("step", "") for ev, data in events if ev == "status"]


# ── 1-1. 합성 지정석은 지식카드를 조회하지 않는다 ────────────────────────────────
def test_합성_지정석은_지식카드를_조회하지_않는다(monkeypatch):
    """`delib-*` 지정석은 레지스트리에 없는 키다 — 물으면 매번 404 이고, 폴백까지 돌아
    좌석 하나에 최대 2 × KNOWLEDGE_TIMEOUT_S 를 쓴다(실사용 로그에서 74회)."""
    search = _Tool("agent_search")
    events = _stream(monkeypatch, {"chair_template": "risk-review"}, tools={"agent_search": search})
    seated = next(data["personas"] for ev, data in events if ev == "status" and data.get("personas"))
    assert "delib-baseline-defender" in seated, "지정석이 앉지 않았다 — 시험 전제가 깨졌다"
    asked = {c["agent_type"] for c in search.calls}
    assert asked == {"mech-a", "rel-b"}, f"합성 지정석까지 조회했다 — {sorted(asked)}"


def test_지식카드_인원수는_실제로_물어본_좌석만_센다(monkeypatch):
    events = _stream(monkeypatch, {"chair_template": "risk-review"})
    steps = _steps(events)
    head = next(s for s in steps if s.startswith("페르소나별 지식카드 검색"))
    assert "2명" in head, head                       # 3석 중 합성 1석을 뺀 수
    tail = next(s for s in steps if s.startswith("지식카드 주입"))
    assert "/2명" in tail, tail
    assert not any("delib-baseline-defender" in s for s in steps if s.startswith("지식카드 ")), (
        "묻지 않은 좌석을 '관련 지식 없음' 으로 적었다 — 못 물어본 것과 없는 것은 다르다")


def test_이어하기로_승계된_지정석도_조회하지_않는다(monkeypatch):
    """이어하기는 좌석을 그대로 넘긴다 — origin 이 떨어져 와도(구 호출자) 키로 알아본다."""
    search = _Tool("agent_search")
    seats = _SEATS + [{"key": "delib-redteam", "role": "red-team", "origin": "adversary"},
                      {"key": "delib-contrarian", "role": "반대"}]
    _stream(monkeypatch, {"personas": seats}, tools={"agent_search": search})
    assert {c["agent_type"] for c in search.calls} == {"mech-a", "rel-b"}


def test_검색_오류_문구가_키_한가운데서_끊기지_않는다():
    """80자에서 끊겨 'agent not found: del' 로 보였고, 사람이 그걸 **키를 쪼개는 버그**로 읽었다."""
    err = ("✖ 도구 agent_search 호출 실패: Error calling tool 'agent_search': "
           '{"detail": "agent not found: delib-baseline-defender"}')
    assert len(err) > 80 and "delib-baseline-defender" not in err[:80], "시험 문구가 종전 상한 안에 든다"
    tool = _Tool("agent_search", answer="(tool agent_search error: " + err + ")")
    hits, note = asyncio.run(d._agent_search_hits({"agent_search": tool}, "delib-baseline-defender", "질의"))
    assert hits == []
    assert note.count("delib-baseline-defender") == 2, note      # 첫 시도와 폴백 둘 다 온전히


# ── 1-2. 본문이 `result` 가 아닌 키에 있어도 버리지 않는다 ───────────────────────
def _ev(req_evidence):
    return d._resolve_opts({"evidence": req_evidence})


@pytest.mark.parametrize("key", ["result", "text", "content", "excerpt", "summary", "body", "output", "data"])
def test_본문_키가_달라도_근거로_받는다(key):
    """`result` 만 읽어서, 본문을 `text` 에 넣은 25건이 통째로 사라진 심의가 끝까지 돌았다."""
    o = _ev([{"source": "E1-CH-015", key: "스프링백 0.42mm"}])
    assert [e["result"] for e in o.evidence] == ["스프링백 0.42mm"]
    assert o.evidence_dropped_empty == 0


def test_본문_키는_순서대로_찾고_빈_값은_건너뛴다():
    o = _ev([{"result": "  ", "text": "", "content": "셋째", "data": "여덟째"}])
    assert o.evidence[0]["result"] == "셋째"
    assert _ev([{"text": "둘째", "result": "첫째"}]).evidence[0]["result"] == "첫째"


def test_객체_본문은_JSON_으로_싣는다():
    """repr 로 실으면 홑따옴표·None·True 가 섞여 좌석이 수치를 다시 못 읽는다."""
    o = _ev([{"source": "표", "data": {"부품": "힌지", "ok": True, "gap": None, "값": [1.5, 2]}}])
    body = o.evidence[0]["result"]
    assert body == '{"부품": "힌지", "ok": true, "gap": null, "값": [1.5, 2]}'
    assert _ev([{"result": ["a", {"b": 1}]}]).evidence[0]["result"] == '["a", {"b": 1}]'


def test_성패_표시는_본문이_아니다():
    """`{"result": true, "data": …}` 모양 — result 를 본문으로 집으면 진짜 본문을 가린다."""
    o = _ev([{"result": True, "data": {"gap_mm": 0.12}}, {"result": 0.42}, {"result": False}])
    assert [e["result"] for e in o.evidence] == ['{"gap_mm": 0.12}', "0.42"]
    assert o.evidence_dropped_empty == 1


def test_본문이_없는_항목과_객체가_아닌_항목을_센다():
    o = _ev([{"source": "빈것"}, {"result": ""}, {"data": {}}, {"output": []}, "문자열 항목", None,
             {"result": "유일한 본문"}])
    assert [e["result"] for e in o.evidence] == ["유일한 본문"]
    assert o.evidence_dropped_empty == 6


def test_버린_것이_없으면_0_이고_기본값에도_있다():
    assert _ev([{"result": "x"}]).evidence_dropped_empty == 0
    assert d._DEFAULT_OPTS.evidence_dropped_empty == 0
    assert d._resolve_opts({"evidence": "목록이 아님"}).evidence_dropped_empty == 0


def _cards(events, included=None):
    return [data for ev, data in events if ev == "delib" and data.get("kind") == "evidence"
            and (included is None or data.get("included") is included)]


def test_전부_버려져도_카드가_나간다(monkeypatch):
    """종전 카드 자리는 `if opts.evidence:` 안이다 — 전부 버려지면 그 블록에 아예 안 들어가서,
    가장 나쁜 경우(근거 0건으로 돈 심의)에 가장 조용했다."""
    events = _stream(monkeypatch, {"evidence": [{"source": f"E{i}"} for i in range(25)]})
    out = _cards(events, included=False)
    assert len(out) == 1, [c.get("source") for c in out]
    assert "25건" in out[0]["text"] and "result" in out[0]["text"], out[0]["text"]
    assert not _cards(events, included=True), "본문이 없는데 좌석에 준 근거가 있다"


def test_일부만_버려지면_카드는_하나고_나머지는_실린다(monkeypatch):
    events = _stream(monkeypatch, {"evidence": [{"result": "본문 A"}, {"source": "빈것"}, "항목 아님",
                                                {"text": "본문 B"}]})
    out = _cards(events, included=False)
    assert len(out) == 1 and "4건 중 2건" in out[0]["text"], out
    assert [c["text"] for c in _cards(events, included=True)] == ["본문 A", "본문 B"]


def test_버린_것이_없으면_카드도_없다(monkeypatch):
    events = _stream(monkeypatch, {"evidence": [{"result": "본문 A"}]})
    assert _cards(events, included=False) == []


# ── 좌석에 주지 않은 근거는 MCP 호출자에게도 보인다 ──────────────────────────────
# 웹은 근거 패널에 '제외' 카드가 뜨지만 MCP 호출자가 보는 것은 잡 원장뿐이다. 원장이 근거
# 카드를 아예 안 적어서, 위 카드를 내도 deliberate_status / deliberate_result 로는 안 보였다.
def _mcp_view(monkeypatch, events):
    """이벤트를 잡 원장에 반영한 뒤 **실제 MCP 도구 함수**가 돌려주는 것을 받는다."""
    import mcp_server

    job = {"id": "t-silent-drops", "status": "running", "question": "q", "started_at": 0.0}
    for ev, data in events:
        delib_jobs._apply(job, ev, data)
    monkeypatch.setitem(delib_jobs._JOBS, job["id"], job)
    return {fn.__name__: asyncio.run(fn(job["id"]))
            for fn in (mcp_server.deliberate_status, mcp_server.deliberate_result)}


def test_버려진_근거가_진행_조회와_결과_회수에_보인다(monkeypatch):
    events = _stream(monkeypatch, {"evidence": [{"source": f"E{i}"} for i in range(25)]})
    for name, out in _mcp_view(monkeypatch, events).items():
        notes = out.get("evidence_omitted") or []
        assert [n["source"] for n in notes] == ["사전 근거 본문 없음"], (name, out)
        assert "25건" in notes[0]["text"] and "result" in notes[0]["text"], (name, notes)


def test_예산_초과로_빠진_근거도_원장에_남는다(monkeypatch):
    """종전부터 있던 카드다 — 화면에는 떴지만 MCP 호출자는 볼 길이 없었다."""
    big = "가" * (d._evid_budget() // 2)
    events = _stream(monkeypatch, {"evidence": [{"result": big}, {"result": big}, {"result": big}]})
    for name, out in _mcp_view(monkeypatch, events).items():
        srcs = [n["source"] for n in out.get("evidence_omitted") or []]
        assert srcs == ["사전 근거 예산 초과"], (name, srcs)


def test_좌석에_준_근거는_원장에_싣지_않는다(monkeypatch):
    """넣은 근거까지 적으면 원장이 근거 본문만큼 커진다 — 빠진 것만 적는다."""
    events = _stream(monkeypatch, {"evidence": [{"result": "본문 A"}, {"text": "본문 B"}]})
    assert _cards(events, included=True), "시험 전제 — 좌석에 준 카드가 있어야 한다"
    for name, out in _mcp_view(monkeypatch, events).items():
        assert out.get("evidence_omitted") == [], (name, out)


# ── 키 없는 지정 좌석 — 말없이 버리고 서버가 고른 패널로 돌았다 ──────────────────────────
# 좌석 항목은 {key, role} 인데 MCP 클라이언트가 받는 글 어디에도 그 모양이 없었다(Args 독스트링은 안 간다).
# recommend_agents 는 좌석 키를 `agent_type` 으로 돌려준다 — 그 줄을 그대로 넘긴 21석 패널이 전부 걸러지고
# 서버가 발굴한 좌석으로 끝까지 돌았다. 카드도 경고도 없었다(본문 키가 달라 근거 25건이 사라진 것과 같은 모양).
_NO_KEY = "지정 좌석 키 없음"


def _seated_keys(events):
    return next(data["personas"] for ev, data in events if ev == "status" and data.get("personas"))


def test_키_없는_좌석은_빼고_앉히되_몇_석을_왜_뺐는지_남긴다(monkeypatch):
    events = _stream(monkeypatch, {"personas": _SEATS + [{"name": "therm-c"}, {"id": "disp-d"}, {"key": ""}]})
    assert _seated_keys(events) == ["mech-a", "rel-b"]
    out = [c for c in _cards(events, included=False) if c["source"] == _NO_KEY]
    assert len(out) == 1, [c["source"] for c in _cards(events, included=False)]
    for want in ("5석 중 3석", "key", "name", "agent_type"):
        assert want in out[0]["text"], (want, out[0]["text"])
    for name, view in _mcp_view(monkeypatch, events).items():
        assert _NO_KEY in [x.get("source") for x in view["evidence_omitted"]], (name, view["evidence_omitted"])


def test_전부_키가_없으면_서버가_발굴한_패널로_돈다는_것을_말한다(monkeypatch):
    """포털·리스크 러너는 MCP 도구를 안 거치고 엔진을 바로 부른다 — 거기서는 발굴로 넘어가되 그 사실이 남아야
    한다. 이 카드는 지정 좌석 갈래 **밖**에서 나가야 한다(전부 걸러지면 그 갈래에 아예 안 들어간다)."""
    import json as _json

    rec = _Tool("recommend_agents", _json.dumps({"agents": [{"agent_type": "auto-x1"}, {"agent_type": "auto-x2"}]}))
    monkeypatch.setattr(d, "_COUNTER_SEATS", 0)
    events = _stream(monkeypatch, {"personas": [{"name": "x"}, {"name": "y"}, {"name": "z"}]},
                     tools={"recommend_agents": rec, "agent_search": _Tool("agent_search")})
    assert _seated_keys(events) == ["auto-x1", "auto-x2"], "시험 전제 — 발굴한 좌석이 앉는다"
    out = [c for c in _cards(events, included=False) if c["source"] == _NO_KEY]
    assert len(out) == 1 and "3석 중 3석" in out[0]["text"] and "발굴" in out[0]["text"], out


def test_추천_도구가_돌려준_agent_type_으로_보낸_좌석은_앉는다(monkeypatch):
    events = _stream(monkeypatch, {"personas": [{"agent_type": "mech-a"}, {"key": "rel-b", "role": "신뢰성"}]})
    assert _seated_keys(events) == ["mech-a", "rel-b"]
    assert not [c for c in _cards(events, included=False) if c["source"] == _NO_KEY]


def test_키가_다_있으면_좌석_카드가_없다(monkeypatch):
    events = _stream(monkeypatch, {})
    assert not [c for c in _cards(events, included=False) if c["source"] == _NO_KEY]
    assert d._DEFAULT_OPTS.seats_no_key is None and d._resolve_opts({"personas": _SEATS}).seats_no_key is None


def test_MCP_로_열_때_쓸_좌석이_하나도_없으면_시작하지_않는다(monkeypatch):
    """한 시간짜리 심의가 지정하지 않은 패널로 돌고 보고서까지 남는다 — 시작 전에 막을 수 있는 유일한 자리다."""
    import mcp_server

    started = []
    monkeypatch.setattr(delib_jobs, "start", lambda *a, **k: started.append((a, k)) or {})
    monkeypatch.setattr(mcp_server, "_APP", object())
    sent = [{"name": f"mech-s{i:02d}", "role": "역할"} for i in range(21)]
    with pytest.raises(Exception) as e:
        asyncio.run(mcp_server.mcp.call_tool("deliberate_start", {"question": "힌지 크랙", "personas": sent}))
    for want in ("key", "agent_type", "name"):
        assert want in str(e.value), (want, str(e.value))
    assert not started, "거절해야 할 요청이 잡을 만들었다"


def test_MCP_로_열_때_일부만_키가_없으면_엔진까지_넘겨_엔진이_세게_한다():
    """도구가 먼저 걸러 버리면 엔진은 몇 석이 빠졌는지 모른다 — 알릴 수도 없다."""
    import mcp_server

    o = mcp_server._build_opts(personas=_SEATS + [{"name": "therm-c"}, "문자열 항목"])
    r = d._resolve_opts(o)
    assert [p["key"] for p in r.continue_personas] == ["mech-a", "rel-b"]
    assert r.seats_no_key and r.seats_no_key[:2] == (4, 2), r.seats_no_key


def test_원장에_남기는_건수에는_상한이_있고_넘으면_그렇다고_적는다():
    job = {"id": "t-cap"}
    for i in range(delib_jobs.OMITTED_MAX + 5):
        delib_jobs._apply(job, "delib", {"kind": "evidence", "source": f"s{i}", "included": False,
                                         "text": "x" * 5000})
    ex = job["evidence_omitted"]
    head, note, tail = ex[:delib_jobs.OMITTED_MAX], ex[delib_jobs.OMITTED_MAX], ex[delib_jobs.OMITTED_MAX + 1:]
    assert "note" in note and "5건" in note["note"], note
    assert ex[0]["source"] == "s0", "먼저 온 것(라운드 전에 오는 사전 근거 드롭)이 밀려나면 안 된다"
    assert [x["source"] for x in head] == [f"s{i}" for i in range(delib_jobs.OMITTED_MAX)]
    assert [x["source"] for x in tail] == [f"s{delib_jobs.OMITTED_MAX + i}" for i in range(5)], tail
    assert len(ex[0]["text"]) < 5000


def test_상한을_넘겨도_맨_나중에_온_알림은_남고_사이에서_빠진_수를_적는다():
    """먼저 온 것만 남기면 **맨 끝에 오는 알림**이 늘 빠진다. 좌석별 자유 조회 실패 카드는 좌석 × 라운드로
    불어나는데(20석 2라운드면 40건), 의장 전사를 줄였다는 카드는 심의 맨 끝에 온다 — 패널이 클수록 둘이
    같이 나고, 호출자는 의장이 줄인 전사로 결정문을 썼다는 것을 원장에서 못 본다."""
    job, n = {"id": "t-tail"}, delib_jobs.OMITTED_MAX + 40
    for i in range(n):
        delib_jobs._apply(job, "delib", {"kind": "evidence", "source": f"s{i}", "included": False, "text": "x"})
    delib_jobs._apply(job, "delib", {"kind": "evidence", "source": "의장 전사 상한 초과", "included": False,
                                     "text": "줄였다", "knob": "DELIB_DECISION_CTX"})
    ex = job["evidence_omitted"]
    assert ex[-1] == {"source": "의장 전사 상한 초과", "text": "줄였다 (설정 DELIB_DECISION_CTX)"}, ex[-1]
    assert [x["source"] for x in ex[:3]] == ["s0", "s1", "s2"], "앞쪽이 밀려났다"
    assert len(ex) == delib_jobs.OMITTED_MAX + 1 + delib_jobs.OMITTED_TAIL, len(ex)
    note = ex[delib_jobs.OMITTED_MAX]["note"]
    # 상한 뒤로 41건이 왔고 그중 마지막 OMITTED_TAIL 건만 남았다 — 사이에서 빠진 수를 적는다.
    assert "41건" in note and f"{41 - delib_jobs.OMITTED_TAIL}건" in note, note
    assert sum(1 for x in ex if "note" in x) == 1, "안내 줄이 여러 번 쌓였다"


def test_상한을_딱_채우면_안내_줄이_없다():
    job = {"id": "t-exact"}
    for i in range(delib_jobs.OMITTED_MAX):
        delib_jobs._apply(job, "delib", {"kind": "evidence", "source": f"s{i}", "included": False, "text": "x"})
    assert len(job["evidence_omitted"]) == delib_jobs.OMITTED_MAX
    assert not [x for x in job["evidence_omitted"] if "note" in x]


# ── 1-3. 건수 상한을 넘긴 근거도 조용히 사라지지 않는다 ──────────────────────────
def _many(n, prefix="본문"):
    return [{"source": f"S{i}", "result": f"{prefix} {i}"} for i in range(1, n + 1)]


def test_건수_상한을_넘긴_수를_센다():
    o = _ev(_many(d._EVID_ITEMS + 3))
    assert len(o.evidence) == d._EVID_ITEMS and o.evidence_over == 3
    assert o.evidence[-1]["result"] == f"본문 {d._EVID_ITEMS}", "앞에서부터 채워야 한다(앞쪽이 더 관련 있다)"
    assert d._DEFAULT_OPTS.evidence_over == 0 and _ev(_many(d._EVID_ITEMS)).evidence_over == 0


def test_빈_항목이_자리를_먹지_않는다():
    """상한을 먼저 자르면 빈 항목 다섯이 앞자리를 먹고, 뒤의 **본문 있는** 다섯이 밀려난다 —
    피할 수 있는 드롭이다(JS 파이프라인도 거르고 나서 자른다)."""
    o = _ev([{"source": "빈것"}] * 5 + _many(d._EVID_ITEMS))
    assert len(o.evidence) == d._EVID_ITEMS
    assert (o.evidence_dropped_empty, o.evidence_over) == (5, 0)


def test_건수_초과가_카드와_원장에_숫자와_설정_이름으로_남는다(monkeypatch):
    n = d._EVID_ITEMS + 3
    events = _stream(monkeypatch, {"evidence": _many(n)})
    out = [c for c in _cards(events, included=False) if c["source"] == "사전 근거 건수 초과"]
    assert len(out) == 1, [c["source"] for c in _cards(events, included=False)]
    for want in (f"{n}건", "3건", f"{d._EVID_ITEMS}건", "DELIB_EVID_ITEMS"):
        assert want in out[0]["text"], (want, out[0]["text"])
    assert len(_cards(events, included=True)) == d._EVID_ITEMS
    for name, view in _mcp_view(monkeypatch, events).items():
        assert "사전 근거 건수 초과" in [x["source"] for x in view["evidence_omitted"]], (name, view)


def test_본문_없음과_건수_초과가_같이_나도_합계가_맞는다(monkeypatch):
    n = d._EVID_ITEMS + 2
    events = _stream(monkeypatch, {"evidence": ["항목 아님"] + _many(n)})
    by = {c["source"]: c["text"] for c in _cards(events, included=False)}
    assert f"근거 {n + 1}건 중 1건" in by["사전 근거 본문 없음"], by
    assert f"{n}건 중 뒤쪽 2건" in by["사전 근거 건수 초과"], by


# ── 1-14. '공용 근거 … 예산 밖 N건 생략' 은 주입한 근거가 잘렸다는 말로 읽혔다 ──────
def _share_line(monkeypatch, calls_per_seat=1):
    """1라운드 자유 조회까지 실제로 돌려, 좌석끼리 나눠 보는 조회 결과의 상태줄을 받는다."""
    import langgraph.prebuilt

    async def _fake_gather(_agent, persona, *_a, **_k):   # (좌석, 호출목록, 주입 블록, 실패사유)
        k = persona["key"]
        return (k, [("list_materials", f'{{"q": "{k}-{i}"}}', f"{k} 가 찾은 값 {i} " + "가" * 500, "")
                    for i in range(calls_per_seat)], "조회 요약", "")

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *_a, **_k: object())
    monkeypatch.setattr(d, "_free_gather_one", _fake_gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))     # 게이트웨이 /tools-map 을 타지 않게
    events = _stream(monkeypatch, {"free_tools": 1},
                     tools={"agent_search": _Tool("agent_search"),
                            "list_materials": _Tool("list_materials")},
                     until=lambda ev, data: ev == "status" and "좌석당 최대" in data.get("step", ""))
    return events[-1][1]["step"]


def test_좌석끼리_나눠_보는_조회_결과를_근거라고_부르지_않는다(monkeypatch):
    """실사용 팀이 이 줄의 '예산 밖 N건 생략' 을 보고 **자기가 주입한 근거**가 잘린다고 믿었다.
    이 줄이 세는 것은 다른 좌석의 자유 조회 결과다 — 사전 근거와는 통도 예산도 다르다."""
    step = _share_line(monkeypatch)
    assert step.startswith("다른 좌석의 조회 결과"), step
    assert "공용 근거" not in step, step
    assert "1건 전달" in step, step                       # 두 좌석이 하나씩 조회 → 서로 1건씩 받는다


def test_예산_밖_생략은_사전_근거와_별개라고_밝힌다(monkeypatch):
    step = _share_line(monkeypatch, calls_per_seat=30)
    assert "예산 밖 최대" in step and "건 생략" in step, step      # 시험 전제 — 실제로 넘쳤다
    assert "사전 근거와는 별개" in step, step


def test_원장에_남길_때_잘랐으면_잘랐다고_적는다():
    """표식 없이 자르면 MCP 호출자는 꼬리가 원래 없던 줄 안다 — 이 파일이 잡으려는 바로 그 모양이다."""
    job = {"id": "t-clip"}
    delib_jobs._apply(job, "delib", {"kind": "evidence", "source": "지식카드 조회 강등", "included": False,
                                     "text": "가" * 5000})
    delib_jobs._apply(job, "delib", {"kind": "evidence", "source": "짧은 것", "included": False, "text": "그대로"})
    long, short = job["evidence_omitted"]
    assert long["text"].startswith("가" * delib_jobs.OMITTED_TEXT_MAX) and "전체 5,000자" in long["text"], long
    assert short["text"] == "그대로"


def test_원장에는_설정_이름을_붙이고_잘려도_남는다():
    """화면 글에는 설정 이름을 싣지 않는다(웹 사용자가 읽는다). 그 이름이 필요한 것은 MCP 호출자인데 그가
    보는 것은 이 원장뿐이다 — 카드가 따로 실어 준 이름(knob)을 여기서 붙인다. 글을 자른 **뒤에** 붙인다."""
    job = {"id": "t-knob"}
    for text in ("짧은 글", "가" * 5000):
        delib_jobs._apply(job, "delib", {"kind": "evidence", "source": "상한 초과", "included": False,
                                         "text": text, "knob": "DELIB_SOME_KNOB"})
    delib_jobs._apply(job, "delib", {"kind": "evidence", "source": "이름 없음", "included": False, "text": "글"})
    short, long, plain = job["evidence_omitted"]
    assert short["text"] == "짧은 글 (설정 DELIB_SOME_KNOB)", short
    assert long["text"].endswith("(설정 DELIB_SOME_KNOB)") and "전체 5,000자" in long["text"], long["text"][-80:]
    assert plain["text"] == "글"


# ── 경고는 도는 동안에도 보이고, 창 밖으로 밀려난 수가 남는다 ───────────────────────
# 원장은 경고를 최근 10건·상태줄을 최근 30줄만 둔다. 경고는 결과 회수에만 실려 도는 동안은 볼 길이
# 없었고, 좌석별 실패 경고가 쌓이면 맨 먼저 온 경고(자격 강등 — 이 심의가 서비스 계정 시야로 돈다)가
# 말없이 밀려났다.
def _ledger(monkeypatch, warnings=0, steps=0):
    import mcp_server

    job = {"id": "t-windows", "status": "running", "question": "q", "started_at": 0.0}
    for i in range(warnings):
        delib_jobs._apply(job, "warning", {"code": "w", "message": f"경고 {i}"})
    for i in range(steps):
        delib_jobs._apply(job, "status", {"step": f"단계 {i}"})
    monkeypatch.setitem(delib_jobs._JOBS, job["id"], job)
    return (asyncio.run(mcp_server.deliberate_status(job["id"])),
            asyncio.run(mcp_server.deliberate_result(job["id"])))


def test_경고가_진행_조회에도_보인다(monkeypatch):
    status, result = _ledger(monkeypatch, warnings=2)
    assert status["warnings"] == ["경고 0", "경고 1"] == result["warnings"], status.get("warnings")
    assert status["warnings_total"] == 2 == result["warnings_total"]


def test_창_밖으로_밀려난_경고와_상태줄은_수로_남는다(monkeypatch):
    status, result = _ledger(monkeypatch, warnings=13, steps=45)
    assert status["warnings"] == [f"경고 {i}" for i in range(3, 13)] and status["warnings_total"] == 13
    assert result["steps"] == [f"단계 {i}" for i in range(15, 45)] and result["steps_total"] == 45
    assert result["warnings_total"] == 13


def test_밀려난_것이_없으면_수가_목록_길이와_같다(monkeypatch):
    status, result = _ledger(monkeypatch, steps=3)
    assert (status["warnings"], status["warnings_total"]) == ([], 0)
    assert (len(result["steps"]), result["steps_total"]) == (3, 3)

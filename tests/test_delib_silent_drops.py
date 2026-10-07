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


def _stream(monkeypatch, req_opts, *, tools=None, until=_at_round1):
    """run_deliberation 을 **실제로** 돌려 이벤트 목록을 받는다. `until` 이 참이 되면 멈춘다
    (기본은 1라운드 문턱 — 거기까지는 LLM 이 필요 없다).

    좌석은 지정으로 주고(발굴 생략) 자유 조회·VOC·재심사를 끈다 — 전부 망을 타는 단계다.
    """
    tools = {"agent_search": _Tool("agent_search")} if tools is None else tools

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))
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


def test_원장에_남기는_건수에는_상한이_있고_넘으면_그렇다고_적는다():
    job = {"id": "t-cap"}
    for i in range(delib_jobs.OMITTED_MAX + 5):
        delib_jobs._apply(job, "delib", {"kind": "evidence", "source": f"s{i}", "included": False,
                                         "text": "x" * 5000})
    ex = job["evidence_omitted"]
    assert len(ex) == delib_jobs.OMITTED_MAX + 1 and "note" in ex[-1], ex[-1]
    assert ex[0]["source"] == "s0", "먼저 온 것(라운드 전에 오는 사전 근거 드롭)이 밀려나면 안 된다"
    assert len(ex[0]["text"]) < 5000


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

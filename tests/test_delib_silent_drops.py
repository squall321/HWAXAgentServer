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

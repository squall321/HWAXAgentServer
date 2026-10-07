# 지식카드 조회를 요청 단위로 끄고 켠다 — 서버를 다시 띄우지 않고 한 심의만 지식카드 없이 돌린다
#
# 종전엔 환경변수(DELIB_PERSONA_KNOWLEDGE)뿐이었다. 끄려면 agent-server 를 다시 띄워야 하는데,
# 그건 그 시각 심의 중인 전원에게 걸린다. 그 시점 자료만으로 다시 심사하는 소급 검증은 지식카드를
# 빼야 한다 — 카드는 오늘의 카드라 나중에 밝혀진 것이 섞여 있다(S26U 실사용 피드백 1-5).
#
# **스트림을 실제로 돌려서** 본다. 손잡이를 받기만 하고 조회 자리가 여전히 환경변수를 읽으면
# 죽은 토글이 된다(test_rescreen_opt 가 잡은 모양 그대로).
#
#   실행:  .venv/bin/python -m pytest tests/test_persona_knowledge_opt.py -q
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_sealed import _run  # noqa: E402
from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_UNREAD = "요청 값 해석 불가"


# ── 받기 ─────────────────────────────────────────────────────────────────────
def test_기본은_환경변수_값이다(monkeypatch):
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    assert d._resolve_opts({}).persona_knowledge == 1
    monkeypatch.setenv("DELIB_PERSONA_KNOWLEDGE", "0")
    assert d._resolve_opts({}).persona_knowledge == 0
    assert d._resolve_opts(None).persona_knowledge == 0


def test_요청으로_끄고_켠다(monkeypatch):
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    assert d._resolve_opts({"persona_knowledge": 0}).persona_knowledge == 0
    assert d._resolve_opts({"persona_knowledge": "0"}).persona_knowledge == 0
    monkeypatch.setenv("DELIB_PERSONA_KNOWLEDGE", "0")
    assert d._resolve_opts({"persona_knowledge": 1}).persona_knowledge == 1


def test_해석_못_하는_값은_기본값을_지킨다(monkeypatch):
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    assert d._resolve_opts({"persona_knowledge": "끔"}).persona_knowledge == 1
    assert d._resolve_opts({"persona_knowledge": None}).persona_knowledge == 1


# ── 쓰기 — 조회 자리가 요청값을 읽는다 ───────────────────────────────────────────
def _searched(monkeypatch, req_opts):
    search = _Tool("agent_search")
    events = _stream(monkeypatch, req_opts, tools={"agent_search": search})
    return {c["agent_type"] for c in search.calls}, _steps(events)


def test_켜져_있으면_좌석마다_조회한다(monkeypatch):
    """시험 전제 — 이게 안 서면 아래 '끄면 안 부른다' 는 아무것도 증명하지 못한다."""
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    asked, steps = _searched(monkeypatch, {})
    assert asked == {"mech-a", "rel-b"}
    assert any(s.startswith("페르소나별 지식카드 검색") for s in steps)


def test_요청으로_끄면_한_번도_조회하지_않는다(monkeypatch):
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    asked, steps = _searched(monkeypatch, {"persona_knowledge": 0})
    assert asked == set(), f"껐는데 지식카드를 조회했다 — {sorted(asked)}"
    assert not any("지식카드" in s for s in steps), steps


def test_환경변수가_꺼져_있어도_요청으로_켜면_조회한다(monkeypatch):
    """조회 자리가 환경변수를 직접 읽으면 여기서 0건이 된다 — 요청 손잡이가 죽은 토글이다."""
    monkeypatch.setenv("DELIB_PERSONA_KNOWLEDGE", "0")
    asked, _ = _searched(monkeypatch, {"persona_knowledge": 1})
    assert asked == {"mech-a", "rel-b"}
    asked, _ = _searched(monkeypatch, {})
    assert asked == set(), "환경변수 기본값(끔)이 안 먹는다"


# ── MCP 호출자에게 안내된다 ─────────────────────────────────────────────────────
def test_MCP_advanced_로_넘기면_엔진까지_간다(monkeypatch):
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    assert d._resolve_opts(m._build_opts(advanced={"persona_knowledge": 0})).persona_knowledge == 0


# ── 켜고 끄는 손잡이는 낱말로도 받고, 못 읽은 값은 알린다 ─────────────────────────────
# voc 는 off 로 끄고 persona_knowledge 는 0 으로 끈다 — 안내에 나란히 적혀 있어, 소급 검증을 손으로 꾸리는
# 호출자는 둘 다 "off" 로 보낸다. int("off") 는 예외라 기본값(켜짐)이 그대로 남았고, 오늘의 지식카드가
# 조회돼 실렸다. 그러고도 잡 기록(applied_opts)에는 보낸 값 "off" 가 걸린 값처럼 적혔다.
@pytest.mark.parametrize("word", ["off", "OFF", " false ", "no"])
def test_끄는_낱말로_보내도_꺼진다(monkeypatch, word):
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    monkeypatch.delenv("DELIB_FREE_TOOLS", raising=False)
    o = d._resolve_opts(m._build_opts(advanced={"persona_knowledge": word, "free_tools": word,
                                                 "rescreen": word, "save_report": word}))
    assert (o.persona_knowledge, o.free_tools, o.rescreen, o.save_report) == (0, 0, 0, 0)
    assert o.req_unread == {}, "읽은 값을 못 읽었다고 적었다"


@pytest.mark.parametrize("word", ["on", "True", "yes"])
def test_켜는_낱말로_보내도_켜진다(monkeypatch, word):
    monkeypatch.setenv("DELIB_PERSONA_KNOWLEDGE", "0")
    o = d._resolve_opts({"persona_knowledge": word, "evidence_prepass": word})
    assert (o.persona_knowledge, o.evidence_prepass) == (1, 1) and o.req_unread == {}


def test_낱말로_꺼도_조회_자리까지_간다(monkeypatch):
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    asked, steps = _searched(monkeypatch, {"persona_knowledge": "off"})
    assert asked == set(), f"off 로 껐는데 지식카드를 조회했다 — {sorted(asked)}"
    assert not any("지식카드" in s for s in steps), steps


def test_못_읽은_값은_기본값으로_돌고_그렇다고_알린다(monkeypatch):
    """기본값을 지키는 것은 종전 그대로다(위 '끔' 시험). 달라진 것은 **말한다**는 것이다 — 안 그러면 호출자는
    자기가 끈 줄 안다."""
    monkeypatch.delenv("DELIB_PERSONA_KNOWLEDGE", raising=False)
    search = _Tool("agent_search")
    events = _stream(monkeypatch, {"persona_knowledge": "끔"}, tools={"agent_search": search})
    assert {c["agent_type"] for c in search.calls} == {"mech-a", "rel-b"}, "못 읽은 값인데 기본값(켜짐)을 버렸다"
    out = [c for c in _cards(events, included=False) if c["source"] == _UNREAD]
    assert len(out) == 1 and "persona_knowledge=끔" in out[0]["text"] and "1" in out[0]["text"], out
    for name, view in _mcp_view(monkeypatch, events).items():
        assert _UNREAD in [x.get("source") for x in view["evidence_omitted"]], (name, view["evidence_omitted"])


def test_개수_손잡이는_낱말을_받지_않고_못_읽었다고_적는다():
    """rounds·tool_budget 은 켜고 끄는 값이 아니다 — "on" 을 1 로 읽으면 엉뚱한 수가 걸린다."""
    o = d._resolve_opts({"rounds": "on", "tool_budget": "많이", "timeout_s": "5m"})
    assert (o.rounds, o.tool_budget, o.timeout_s) == (3, d._DEFAULT_OPTS.tool_budget, None)
    assert set(o.req_unread) == {"rounds", "tool_budget", "timeout_s"}, o.req_unread


def test_voc_와_의장_틀도_못_읽으면_적는다():
    """voc 를 0·False·none 으로 끄려 한 호출자는 auto 로 돈다 — 소급 검증에 최근 VOC 가 섞인다."""
    for bad in (0, False, "none", "disabled"):
        o = d._resolve_opts({"voc": bad})
        assert o.voc == "auto" and "voc" in o.req_unread, (bad, o.req_unread)
    assert "chair_template" in d._resolve_opts({"chair_template": "nope"}).req_unread
    for fine in ({"voc": "OFF"}, {"voc": None}, {"voc": ""}, {"chair_template": "diagnosis"},
                 {"chair_template": None}, {"timeout_s": 60}, {}):
        assert d._resolve_opts(fine).req_unread == {}, fine
    assert d._DEFAULT_OPTS.req_unread == {}


def test_못_읽은_값이_없으면_카드도_없다(monkeypatch):
    events = _stream(monkeypatch, {"persona_knowledge": "off", "free_tools": 0, "rounds": 2})
    assert not [c for c in _cards(events) if c["source"] == _UNREAD]


def test_잡_기록에는_보낸_값이_아니라_걸린_값이_남는다(monkeypatch, tmp_path):
    """applied_opts 는 '무엇이 실제로 걸렸는지' 다 — 보낸 값을 그대로 적으면 못 읽은 값·상한에서 줄인 값이
    걸린 것처럼 읽힌다."""
    monkeypatch.delenv("DELIB_FREE_TOOLS", raising=False)
    r = _run(monkeypatch, tmp_path, job="risk-review",
             advanced={"persona_knowledge": "off", "free_tools": "끔", "tool_budget": 50, "voc": 0})
    applied = asyncio.run(m.deliberate_result(r.job["id"]))["applied_opts"]
    assert (applied["persona_knowledge"], applied["free_tools"], applied["tool_budget"], applied["voc"]) == (
        0, 1, 6, "auto"), applied
    assert "agent_search" not in r.tools, "off 로 껐는데 지식카드를 조회했다"
    card = next(x for x in r.job["evidence_omitted"] if x.get("source") == _UNREAD)
    assert "free_tools=끔" in card["text"] and "voc=0" in card["text"], card

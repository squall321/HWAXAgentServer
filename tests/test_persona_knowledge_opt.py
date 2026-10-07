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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context, _steps, _stream, _Tool  # noqa: E402, F401


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

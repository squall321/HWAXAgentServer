# 좌석 지식카드 조회의 동시 실행 상한(DELIB_KNOWLEDGE_CONCURRENCY) — 20석이 한꺼번에 쏘지 않는다
#
# 지식카드 조회는 좌석 수만큼을 **한 번에** 보냈다. 20석 심의 둘이 겹치면 AIDataHub 에 40건이 한꺼번에
# 떨어지고, 느려진 조회가 타임아웃 → 폴백으로 또 한 번씩 쏜다(S26U 피드백 1-8). 좌석별 진행 줄은
# 그대로 두고 한 번에 도는 수만 묶는다.
#
# **스트림을 실제로 돌려서** 조회 도구가 동시에 몇 번 불리는지 센다.
#
#   실행:  .venv/bin/python -m pytest tests/test_knowledge_conc.py -q
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context, _steps, _stream, _Tool  # noqa: E402, F401


class _Counting(_Tool):
    """동시에 몇 건이 돌고 있는지 세는 agent_search. 한 건은 `hold` 초 동안 돈다."""

    def __init__(self, hold=0.01):
        super().__init__("agent_search", '{"hits": [{"title": "카드", "snippet": "본문", "record_id": "r1"}]}')
        self.hold, self.now, self.peak = hold, 0, 0

    async def ainvoke(self, args):
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await asyncio.sleep(self.hold)
            return await super().ainvoke(args)
        finally:
            self.now -= 1


def _seats(n):
    return [{"key": f"mech-s{i:02d}", "role": "기구"} for i in range(1, n + 1)]


def _lookup(monkeypatch, n, tool=None):
    tool = tool or _Counting()
    events = _stream(monkeypatch, {"personas": _seats(n)}, tools={"agent_search": tool})
    return tool, events


def test_20석이어도_한_번에_6건까지만_돈다(monkeypatch):
    tool, events = _lookup(monkeypatch, 20)
    assert tool.peak == 6, f"동시에 {tool.peak}건이 돌았다(상한 6 — 못 미치면 직렬로 돌린 것이다)"
    assert {c["agent_type"] for c in tool.calls} == {p["key"] for p in _seats(20)}, "안 물어본 좌석이 있다"
    steps = _steps(events)
    done = [s for s in steps if s.startswith("지식카드 ") and "/20 — " in s]
    assert len(done) == 20, f"좌석별 진행 줄이 {len(done)}개다 — 끝나는 대로 한 줄씩 알려야 한다"
    assert any(s.startswith("지식카드 주입 — 20/20명") for s in steps), steps[-3:]


def test_상한은_설정으로_바꾼다(monkeypatch):
    monkeypatch.setattr(d, "_KN_CONC", 2)
    tool, _ = _lookup(monkeypatch, 9)
    assert tool.peak == 2 and len(tool.calls) == 9


def test_0_은_무제한이다(monkeypatch):
    """이 파일의 다른 손잡이와 같은 뜻이다 — 0 을 넣으면 세마포어가 영영 안 열리는 것이 아니라."""
    monkeypatch.setattr(d, "_KN_CONC", 0)
    tool, _ = _lookup(monkeypatch, 20)
    assert tool.peak == 20 and len(tool.calls) == 20


def test_좌석이_상한보다_적으면_전부_한_번에_돈다(monkeypatch):
    tool, _ = _lookup(monkeypatch, 4)
    assert tool.peak == 4


def test_차례를_기다린_시간은_조회_제한시간에_안_들어간다(monkeypatch):
    """제한시간(KNOWLEDGE_TIMEOUT_S)은 **조회 한 건**의 것이다. 줄 선 시간까지 재면 뒤쪽 좌석이 조회를
    시작도 못 하고 '시간 초과' 로 강등된다 — 상한을 넣어서 새로 생길 수 있는 조용한 실패다."""
    monkeypatch.setattr(d, "_KN_CONC", 1)
    monkeypatch.setattr(d, "KNOWLEDGE_TIMEOUT_S", 0.4)
    tool, events = _lookup(monkeypatch, 12, _Counting(hold=0.05))     # 한 줄로 0.6초 — 제한시간보다 길다
    assert tool.peak == 1 and len(tool.calls) == 12, "강등돼 폴백으로 다시 물었다"
    assert not [data for ev, data in events if ev == "warning"], "기다리기만 한 좌석이 강등됐다"
    assert any(s.startswith("지식카드 주입 — 12/12명") for s in _steps(events))


def test_심의를_잇달아_돌려도_된다(monkeypatch):
    """세마포어를 모듈에 하나 두면 첫 심의의 이벤트 루프에 묶여, 다음 심의가 줄을 서는 순간 죽는다
    (요청마다 루프가 같은 서버에서는 안 보이고 시험·재기동 경계에서만 터진다)."""
    for _ in range(2):
        tool, _ = _lookup(monkeypatch, 20)
        assert tool.peak == 6 and len(tool.calls) == 20


def test_기본값은_6_이고_환경변수로_바꾼다():
    def loaded(value):
        env = {k: v for k, v in os.environ.items() if k != "DELIB_KNOWLEDGE_CONCURRENCY"}
        if value is not None:
            env["DELIB_KNOWLEDGE_CONCURRENCY"] = value
        r = subprocess.run([sys.executable, "-c", "import deliberation as d; print(d._KN_CONC)"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
        assert r.returncode == 0, r.stderr[-600:]
        return int(r.stdout.split()[-1])

    assert loaded(None) == 6
    assert loaded("3") == 3
    assert loaded("여섯") == 6, "오타 값이 서버 기동을 죽였다"

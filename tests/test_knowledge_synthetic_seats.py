# 합성 지정석의 지식카드 조회를 좌석 키 단위로 되살린다(DELIB_KNOWLEDGE_SYNTHETIC_SEATS)
#
# 합성 지정석(`delib-*`·origin adversary)은 레지스트리에 없는 키라 지식카드를 묻지 않는다 — 물으면 매번
# 404 였다(S26U 피드백 1-1). 그런데 그 건너뛰기가 무조건이면, 나중에 반대석을 AIDataHub 에 등록하고
# 레코드를 묶어도 엔진이 묻지 않아 코드를 다시 고쳐야 한다. 등록한 키를 설정에 적으면 그 좌석만 묻는다
# (HWAXPortal docs/delib-engine-feedback D-13). 기본은 빈 값 — 종전처럼 전부 건너뛴다.
#
# **스트림을 실제로 돌려서** 조회 도구가 어느 좌석으로 불렸는지 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_knowledge_synthetic_seats.py -q
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_sealed import _ALLOWED, _REOPEN, _run  # noqa: E402
from test_delib_silent_drops import _pin_context, _SEATS, _steps, _stream, _Tool  # noqa: E402, F401

DEFENDER = "delib-baseline-defender"        # 리스크 심사의 지정 반대석(_CHAIR_ADVERSARY)
_HIT = '{"hits": [{"title": "과거 기각 선례", "snippet": "기각 12건", "record_id": "r1"}]}'


def _asked(monkeypatch, listed, req_opts):
    monkeypatch.setattr(d, "_KN_SYNTH", frozenset(listed))
    search = _Tool("agent_search", _HIT)
    events = _stream(monkeypatch, req_opts, tools={"agent_search": search})
    return {c["agent_type"] for c in search.calls}, events


def test_설정에_적은_합성_지정석은_지식카드를_조회한다(monkeypatch):
    asked, events = _asked(monkeypatch, [DEFENDER], {"chair_template": "risk-review"})
    assert asked == {"mech-a", "rel-b", DEFENDER}, f"등록했다고 적은 지정석을 묻지 않았다 — {sorted(asked)}"
    steps = _steps(events)
    assert "3명" in next(s for s in steps if s.startswith("페르소나별 지식카드 검색")), steps
    assert any(s.startswith("지식카드 주입 — 3/3명") for s in steps), steps
    cards = [data.get("source") for ev, data in events if ev == "delib" and data.get("kind") == "evidence"]
    assert f"{DEFENDER} · 지식카드" in cards, f"받은 발췌가 그 좌석의 카드로 나가지 않았다 — {cards}"


def test_적지_않은_합성_지정석은_종전대로_조회하지_않는다(monkeypatch):
    """하나를 적었다고 합성 좌석 전부가 열리면, 등록 안 된 나머지가 다시 매번 404 다."""
    seats = _SEATS + [{"key": DEFENDER, "role": "기준선 옹호", "origin": "adversary"},
                      {"key": "delib-redteam", "role": "red-team", "origin": "adversary"},
                      {"key": "delib-contrarian", "role": "반대"}]
    asked, _ = _asked(monkeypatch, [DEFENDER], {"personas": seats})
    assert asked == {"mech-a", "rel-b", DEFENDER}, sorted(asked)


def test_기본값은_빈_값이라_합성_지정석을_전부_건너뛴다(monkeypatch):
    asked, _ = _asked(monkeypatch, [], {"chair_template": "risk-review"})
    assert asked == {"mech-a", "rel-b"}, sorted(asked)


def test_합성이_아닌_좌석은_적지_않아도_조회한다(monkeypatch):
    """이 설정은 합성 지정석을 **더 여는** 목록이다 — 조회할 좌석을 고르는 허용 목록이 아니다."""
    asked, _ = _asked(monkeypatch, [DEFENDER], {})
    assert asked == {"mech-a", "rel-b"}, sorted(asked)


# ── 봉인은 이것도 닫는다 ─────────────────────────────────────────────────────────
def test_봉인_옵션이면_적은_지정석도_조회하지_않는다(monkeypatch):
    """봉인은 지식카드 조회를 통째로 닫는다 — 이 설정이 그 뒤에서 한 석을 다시 열면 소급 검증에
    오늘의 카드가 섞인다."""
    asked, events = _asked(monkeypatch, [DEFENDER],
                           {"chair_template": "risk-review", "sealed": 1, "persona_knowledge": 1})
    assert asked == set(), f"봉인했는데 지식카드를 조회했다 — {sorted(asked)}"
    assert not [s for s in _steps(events) if s.startswith(("페르소나별 지식카드", "지식카드 "))]


def test_봉인_Job_은_적은_지정석이_앉아도_아무것도_조회하지_않는다(monkeypatch, tmp_path):
    """실제 MCP 도구로 봉인 리스크 심사를 끝까지 돌린다 — 지정 반대석이 앉고, 호출자가 전부 열어도."""
    monkeypatch.setattr(d, "_KN_SYNTH", frozenset([DEFENDER]))
    r = _run(monkeypatch, tmp_path, advanced=dict(_REOPEN))
    assert DEFENDER in [s["key"] for s in r.job["seats"]], "지정석이 앉지 않았다 — 시험 전제가 깨졌다"
    assert "agent_search" not in r.tools and set(r.tools) <= _ALLOWED, sorted(set(r.tools))


def test_같은_요청을_봉인_없이_돌리면_적은_지정석을_조회한다(monkeypatch, tmp_path):
    """위 시험의 전제 — 이 하네스에서 봉인만 빼면 그 좌석 조회가 실제로 돈다."""
    monkeypatch.setattr(d, "_KN_SYNTH", frozenset([DEFENDER]))
    r = _run(monkeypatch, tmp_path, job="risk-review", advanced={"free_tools": 0, "voc": "off"})
    assert "agent_search" in r.tools
    assert r.args["agent_search"]["agent_type"] in {"mech-a", "rel-b", DEFENDER}
    asked = [s for s in r.steps if s.startswith("지식카드 ") and DEFENDER in s]
    assert asked, f"지정석의 조회 진행 줄이 없다 — {[s for s in r.steps if '지식카드' in s]}"


# ── 설정 읽기 ────────────────────────────────────────────────────────────────
def _loaded(value, code="import deliberation as d; print(sorted(d._KN_SYNTH))"):
    env = {k: v for k, v in os.environ.items() if k != "DELIB_KNOWLEDGE_SYNTHETIC_SEATS"}
    if value is not None:
        env["DELIB_KNOWLEDGE_SYNTHETIC_SEATS"] = value
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stderr[-600:]
    return r.stdout.strip().splitlines()[-1]


def test_환경변수는_쉼표_목록이고_기본은_빈_값이다():
    assert _loaded(None) == "[]"
    assert _loaded("") == "[]"
    assert _loaded(DEFENDER) == f"['{DEFENDER}']"
    assert _loaded(f" {DEFENDER} , ,delib-redteam,") == f"['{DEFENDER}', 'delib-redteam']"


def test_도구_안내가_설정_이름과_지금_적힌_키를_알려_준다():
    """반대석을 등록하고도 지식이 안 실리면 호출자가 볼 곳은 도구 안내뿐이다 — 서버 설정이라고 감추지 않는다."""
    code = ("import asyncio, mcp_server as m\n"
            "t = next(t for t in asyncio.run(m.mcp.list_tools()) if t.name == 'deliberate_start')\n"
            "at = t.description.index('DELIB_KNOWLEDGE_SYNTHETIC_SEATS')\n"
            "print(t.description[at:at + 120].replace(chr(10), ' '))\n")
    assert "지금 적힌 키: 없음" in _loaded(None, code)
    assert f"지금 적힌 키: {DEFENDER}, delib-redteam)" in _loaded(f"delib-redteam,{DEFENDER}", code)

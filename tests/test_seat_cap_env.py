# 좌석 상한을 설정(DELIB_MAX_SEATS)으로 바꾼다 — 21·22석 패널을 코드 수정 없이 돌린다
#
# 상한 20 이 코드에 박혀 있어서, 실사용 팀의 21석 패널은 마지막 좌석이 매번 잘렸다(S26U 피드백 1-12).
# MCP(deliberate_start)와 리스크 앱은 포털 스키마를 안 거치므로 엔진 설정만으로 풀린다. 포털 웹은
# 20 그대로다 — 더 좁은 쪽이라 안전하고, 넘치면 422 로 소리 내 막힌다(HWAXPortal
# docs/delib-engine-feedback D-3).
#
#   실행:  .venv/bin/python -m pytest tests/test_seat_cap_env.py -q
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context, _steps, _stream  # noqa: E402, F401


def _seats(n):
    return [{"key": f"mech-s{i:02d}", "role": "기구"} for i in range(1, n + 1)]


# ── 설정 ─────────────────────────────────────────────────────────────────────
def _loaded(value):
    env = {k: v for k, v in os.environ.items() if k != "DELIB_MAX_SEATS"}
    if value is not None:
        env["DELIB_MAX_SEATS"] = value
    r = subprocess.run([sys.executable, "-c", "import deliberation as d; print(d.MAX_REQ_SEATS)"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stderr[-600:]
    return int(r.stdout.split()[-1])


def test_기본값은_20_이고_설정으로_바꾼다():
    assert _loaded(None) == 20
    assert _loaded("22") == 22
    assert _loaded("12") == 12
    assert _loaded("스물둘") == 20, "오타 값이 서버 기동을 죽이거나 상한을 없앴다"


def test_0_과_음수는_무제한이_아니다():
    """슬라이스에 0 이 들어가면 지정 좌석을 **전부** 버리고 발굴이 말없이 대신 앉힌다. 이 파일의 다른
    손잡이는 0 이 무제한이라 그렇게 넣기 쉽다 — 심의가 서는 최소(두 석)로 올린다."""
    assert _loaded("0") == 2
    assert _loaded("-3") == 2


# ── 자르기 ───────────────────────────────────────────────────────────────────
def test_상한까지만_앉히고_넘친_좌석을_들고_간다(monkeypatch):
    monkeypatch.setattr(d, "MAX_REQ_SEATS", 3)
    o = d._resolve_opts({"personas": _seats(5)})
    assert [p["key"] for p in o.continue_personas] == ["mech-s01", "mech-s02", "mech-s03"]
    assert o.seats_clamped == ["mech-s04", "mech-s05"]


def _seated(events):
    return next(data["personas"] for ev, data in events if ev == "status" and data.get("personas"))


def test_상한을_올리면_22석이_다_앉는다(monkeypatch):
    monkeypatch.setattr(d, "MAX_REQ_SEATS", 22)
    events = _stream(monkeypatch, {"personas": _seats(22)})
    assert _seated(events) == [s["key"] for s in _seats(22)]
    assert not any("좌석 상한" in s for s in _steps(events)), "다 앉혔는데 잘랐다고 했다"


def test_기본_상한에서는_21번째가_빠지고_그렇다고_말한다(monkeypatch):
    events = _stream(monkeypatch, {"personas": _seats(21)})
    assert _seated(events) == [s["key"] for s in _seats(20)]
    line = next(s for s in _steps(events) if "좌석 상한" in s)
    assert "20석" in line and "mech-s21" in line, line


def test_지정_반대석은_상한_밖에서_한_석_더_앉는다(monkeypatch):
    """20석을 청하면 21석으로 돈다 — 상한은 **청한 좌석**에 걸리고 엔진이 얹는 지정석은 그 밖이다."""
    events = _stream(monkeypatch, {"personas": _seats(20), "chair_template": "risk-review"})
    seated = _seated(events)
    assert len(seated) == d.MAX_REQ_SEATS + 1 and seated[-1] == "delib-baseline-defender"
    assert not any("좌석 상한" in s for s in _steps(events))


# ── 안내 ─────────────────────────────────────────────────────────────────────
def test_메뉴가_지금_상한을_상수에서_읽어_알려_준다(monkeypatch):
    monkeypatch.setattr(d, "MAX_REQ_SEATS", 22)
    menu = asyncio.run(m.deliberate_jobs())
    assert menu["limits"]["seats"] == 22
    assert "≤22" in menu["options"]["personas"] and "≤20" not in menu["options"]["personas"]
    assert "최대 20" not in (m.deliberate_start.__doc__ or ""), "독스트링에 손으로 적은 상한이 남아 있다"

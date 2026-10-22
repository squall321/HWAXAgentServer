# 동시 심의 한도를 재기동 없이 — 박스 덮개(state/limits.json)가 env 위에 덮이고 판정 때마다 읽힌다(HWAXPortal docs/delib-ops R4)
#
# 왜 생겼나. 세 한도가 import 때 한 번 읽는 상수라 한도 하나를 바꾸는 데 재기동이 들었고, 재기동은 도는 잡과 줄 선 잡을 닫았다
# (2026-10-22 새벽 — 2 → 6 으로 올리는 데 반나절). 올리면 그 자리에서 줄이 빠져야 하고, 내리면 도는 잡은 그대로여야 한다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_limits.py -q
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402

from test_delib_job_queue import _caps, _end, _play, _st, _start, _tick, eng  # noqa: E402, F401
from test_delib_silent_drops import _pin_context  # noqa: E402, F401


def test_덮개가_없으면_env_값이고_출처는_env(eng, monkeypatch):
    _caps(monkeypatch, 3, per_user=2, queue=7)
    lim = delib_jobs.limits()
    assert (lim["max_running"], lim["max_running_per_user"], lim["queue_max"]) == (3, 2, 7)
    assert lim["source"] == {"max_running": "env", "max_running_per_user": "env", "queue_max": "env", "max_seats_running": "env"}
    assert lim["max_seats_running"] == 0 and "error" not in lim, "좌석 합 상한은 기본 꺼짐이다(종전 — 잡 수만 센다)"


def test_덮개는_준_칸만_덮고_사용자별은_전역을_따르던_박스면_덮인_전역을_따른다(eng, monkeypatch):
    _caps(monkeypatch, 2)                            # env 의 사용자별은 전역을 따르던 값(2)
    monkeypatch.setattr(delib_jobs, "_PER_USER", 0)
    out = delib_jobs.set_limits({"max_running": 6}, by="운영", reason="10-22 증설")
    assert (out["max_running"], out["max_running_per_user"], out["queue_max"]) == (6, 6, 20)
    assert out["source"]["max_running"] == "file" and out["by"] == "운영" and out["reason"] == "10-22 증설"
    on_disk = json.loads(delib_jobs.limits_path().read_text(encoding="utf-8"))
    assert on_disk["max_running"] == 6 and "max_running_per_user" not in on_disk
    out = delib_jobs.set_limits({"max_running_per_user": 2, "queue_max": 0}, by="운영")
    assert (out["max_running"], out["max_running_per_user"], out["queue_max"]) == (6, 2, 0), "앞의 덮개는 남는다"
    out = delib_jobs.set_limits({"max_running_per_user": None}, by="운영")
    assert out["max_running_per_user"] == 6 and out["source"]["max_running_per_user"] == "env", "null 은 그 칸의 덮개를 지운다"
    out = delib_jobs.set_limits({}, by="운영", reset=True)
    assert out["max_running"] == 2 and not delib_jobs.limits_path().exists()


def test_env_가_사용자별을_따로_적었으면_전역만_덮어도_사용자별은_그대로다(eng, monkeypatch):
    _caps(monkeypatch, 4, per_user=1)
    monkeypatch.setattr(delib_jobs, "_PER_USER", 1)
    assert delib_jobs.set_limits({"max_running": 8}, by="운영")["max_running_per_user"] == 1


@pytest.mark.parametrize("vals", [{"max_running": 0}, {"max_running": True}, {"queue_max": -1}, {"max_running": 60000},
                                  {"max_running": "6"}, {"bogus": 1}])
def test_틀린_값은_거절하고_파일을_건드리지_않는다(eng, monkeypatch, vals):
    _caps(monkeypatch, 2)
    with pytest.raises(ValueError):
        delib_jobs.set_limits(vals, by="운영")
    assert not delib_jobs.limits_path().exists()
    with pytest.raises(ValueError, match="by"):
        delib_jobs.set_limits({"max_running": 3}, by=" ")


def test_깨진_덮개는_읽지_않고_사유를_싣는다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    delib_jobs.limits_path().parent.mkdir(parents=True, exist_ok=True)
    delib_jobs.limits_path().write_text('{"max_running": 0}', encoding="utf-8")
    lim = delib_jobs.limits()
    assert lim["max_running"] == 2 and "깨졌다" in lim["error"] and lim["source"]["max_running"] == "env"
    delib_jobs.limits_path().write_text('{"max_running": 5}', encoding="utf-8")
    assert delib_jobs.limits()["max_running"] == 5 and "error" not in delib_jobs.limits(), "고치면 다음 판정부터 읽는다"


def test_올리면_그_자리에서_줄이_빠지고_내리면_도는_잡은_그대로_새_시작만_준다(eng, monkeypatch):
    _caps(monkeypatch, 1, queue=5)

    async def scenario():
        a, b, c = _start("가"), _start("나"), _start("다")
        await _tick()
        assert [_st(x) for x in (a, b, c)] == ["running", "queued", "queued"]
        delib_jobs.set_limits({"max_running": 3}, by="운영")       # 재기동 없이
        await _tick()
        assert [_st(x) for x in (a, b, c)] == ["running"] * 3 and eng.started == ["가", "나", "다"]
        delib_jobs.set_limits({"max_running": 1}, by="운영")       # 내린다 — 도는 셋은 끊지 않는다
        await _tick()
        assert [_st(x) for x in (a, b, c)] == ["running"] * 3
        d4 = _start("라")
        await _tick()
        assert _st(d4) == "queued", "내린 한도 위에서는 새 시작이 줄을 선다"
        await _end(eng, a)
        await _end(eng, b)
        assert _st(d4) == "queued", "도는 수(1)가 아직 내린 한도(1)에 걸린다"
        await _end(eng, c)
        assert _st(d4) == "running"
    _play(scenario)


def test_덮개에서_온_한도의_거절_문구는_덮개를_말한다(eng, monkeypatch):
    _caps(monkeypatch, 1, queue=0)
    delib_jobs.set_limits({"max_running": 1}, by="운영")

    async def scenario():
        _start("가")
        await _tick()
        with pytest.raises(RuntimeError) as e:
            _start("나")
        assert "전역 1건 — 박스 덮개 POST /limits" in str(e.value) and "DELIB_JOB_MAX_RUNNING" not in str(e.value)
        assert "DELIB_JOB_MAX_RUNNING" not in str(e.value)
    _play(scenario)


def test_POST_limits_는_칸을_검사하고_GET_과_health_가_같은_값을_낸다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    c = TestClient(app.app)
    assert c.post("/limits", json={"max_running": 4, "by": "운영", "oops": 1}).status_code == 422
    assert c.post("/limits", json={"by": "운영"}).status_code == 422, "바꿀 것이 없다"
    assert c.post("/limits", json={"max_running": 0, "by": "운영"}).status_code == 422
    assert c.post("/limits", json={"max_running": 4}).status_code == 422, "by 가 없다"
    assert c.post("/limits", json={"reset": "yes", "by": "운영"}).status_code == 422
    r = c.post("/limits", json={"max_running": 4, "queue_max": 9, "by": "운영", "reason": "증설"})
    assert r.status_code == 200 and r.json()["max_running"] == 4 and r.json()["queue_max"] == 9
    assert c.get("/limits").json()["max_running"] == 4
    h = c.get("/health").json()["limits"]
    assert (h["max_running"], h["queue_max"], h["source"]["max_running"]) == (4, 9, "file")
    assert c.post("/limits", json={"reset": True, "by": "운영"}).json()["max_running"] == 2

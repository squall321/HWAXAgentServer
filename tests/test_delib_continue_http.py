# 웹 '이어하기' 가 엔진 deliberate_continue 를 부르는 창구(HWAXPortal docs/request-1125 WD-05) — POST /deliberate/continue
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_continue_http.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402

from test_delib_silent_drops import _pin_context  # noqa: E402, F401

SECRET = "Bearer gw-secret"
ME = "me@example.com"


@pytest.fixture
def bridge(monkeypatch, _pin_context):
    import seat_box
    monkeypatch.setattr(seat_box, "secret", lambda _app: SECRET)
    got: list = []

    async def _cont(ctx=None, **kw):
        hdr = ctx.request_context.request.headers
        got.append((kw, hdr.get("x-hwax-user"), hdr.get("x-hwax-groups")))
        if kw["previous_job_id"] == "nope":
            raise ValueError("그런 심의 잡이 없다: nope — deliberate_list 로 확인하라")
        return {"job_id": "diagnosis-20261128-101010-abcdef", "status": "queued", "continued_from": kw["previous_job_id"]}
    assert a._delib_mcp_module is not None
    monkeypatch.setattr(a._delib_mcp_module, "deliberate_continue", _cont)
    return got


def test_WD05_웹_이어하기는_같은_잡_계보의_다음_잡을_그_사람_이름으로_연다(bridge):
    c = TestClient(a.app)
    hdr = {"Authorization": SECRET, "x-hwax-user": ME, "x-hwax-groups": "lab"}
    r = c.post("/deliberate/continue", json={"previous_job_id": "diagnosis-20261110-101010-aaaaaa", "human_note": "새 관측"}, headers=hdr)
    assert r.status_code == 200 and r.json()["continued_from"] == "diagnosis-20261110-101010-aaaaaa"
    assert bridge == [({"previous_job_id": "diagnosis-20261110-101010-aaaaaa", "human_note": "새 관측"}, ME, "lab")]


def test_WD05_자격_신원_칸을_따진다(bridge):
    c = TestClient(a.app)
    assert c.post("/deliberate/continue", json={"previous_job_id": "x"}).status_code == 401
    assert c.post("/deliberate/continue", json={"previous_job_id": "x"}, headers={"Authorization": SECRET}).status_code == 403
    hdr = {"Authorization": SECRET, "x-hwax-user": ME}
    r = c.post("/deliberate/continue", json={"previous_job_id": "x", "bogus": 1}, headers=hdr)
    assert r.status_code == 422 and r.json()["code"] == "unknown_fields"
    assert c.post("/deliberate/continue", json={}, headers=hdr).status_code == 422
    r = c.post("/deliberate/continue", json={"previous_job_id": "nope"}, headers=hdr)
    assert r.status_code == 422 and "그런 심의 잡이 없다" in r.json()["message"]
    assert bridge[-1][0] == {"previous_job_id": "nope"}

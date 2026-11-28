# MCP 로 연 심의를 잡 주인의 포털 대화로(HWAXPortal docs/request-1125 WD-03) — 에이전트 서버의 대화 거리 창구 GET /delib/conversations
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_conversations.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402

from test_delib_silent_drops import _pin_context  # noqa: E402, F401

ME = "me@example.com"


def _job(jid, **kw):
    base = {"id": jid, "job": "diagnosis", "status": "done", "question": "S26 화면 깨짐 원인", "user": ME, "owner_aff": "CAEG",
            "started_at": 100.0, "finished_at": 200.0, "started_by": "human", "decision": "결론 — 모서리 응력.",
            "turns": [{"round": 1, "persona": "기구", "say": "줄인", "say_full": "기구 1R 전문"}, {"round": 1, "persona": "재료", "say": "재료 1R"}],
            "opts": {"evidence": [{"source": "VOC", "tool": "voc_search", "result": "1,234건 " * 10}]}, "report_id": 58}
    return {**base, **kw}


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    for j in (_job("diagnosis-20261110-101010-aaaaaa"),
              _job("diagnosis-20261110-111010-bbbbbb", continues="diagnosis-20261110-101010-aaaaaa", finished_at=300.0, question="이어서"),
              _job("risk-review-20261110-121010-cccccc", started_by="app", finished_at=250.0),
              _job("default-20261110-131010-dddddd", status="running", finished_at=None),
              _job("default-20261110-141010-eeeeee", user="", finished_at=260.0),
              _job("delegate-20261110-151010-ffffff", kind="delegate", finished_at=270.0)):
        delib_jobs._persist(j)
    return tmp_path


def test_WD03_끝난_MCP_심의를_화두_발언_결정문_행과_뿌리_잡으로_낸다(ledger):
    out = delib_jobs.conversation_items(0.0, 20)
    assert out["contract"] == delib_jobs.CONV_CONTRACT and out["complete"] is True
    ids = [it["job_id"] for it in out["items"]]
    assert ids == ["diagnosis-20261110-101010-aaaaaa", "diagnosis-20261110-111010-bbbbbb"], "리스크 앱 잡 · 도는 잡 · 주인 없음 · 위임은 뺀다"
    first, second = out["items"]
    assert first["kind"] == "deliberation" and first["source"] == "mcp" and first["user"] == ME
    assert [r["kind"] for r in first["rows"]] == ["question", "seat", "seat", "decision"]
    assert first["rows"][1]["content"] == "기구 1R 전문" and first["rows"][-1]["content"] == "결론 — 모서리 응력."
    assert first["meta"]["evidence"] == [{"source": "VOC", "tool": "voc_search", "chars": len("1,234건 " * 10)}], "근거는 본문 없이"
    assert first["root_job_id"] == second["root_job_id"] == "diagnosis-20261110-101010-aaaaaa", "이어하기 잡은 같은 대화에 붙는다"
    assert second["meta"]["continues"] == "diagnosis-20261110-101010-aaaaaa" and out["next_since"] == 300.0


def test_WD03_since_와_limit(ledger):
    out = delib_jobs.conversation_items(250.0, 20)
    assert [it["job_id"] for it in out["items"]] == ["diagnosis-20261110-111010-bbbbbb"]
    page = delib_jobs.conversation_items(0.0, 1)
    assert len(page["items"]) == 1 and page["complete"] is False and page["next_since"] == 200.0


def test_WD03_창구는_공유_비밀만_받고_사람_신원은_거절한다(ledger, monkeypatch, _pin_context):
    import seat_box
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    assert c.get("/delib/conversations").status_code == 401
    ok = c.get("/delib/conversations", params={"since": 0}, headers={"Authorization": "Bearer gw-secret"})
    assert ok.status_code == 200 and len(ok.json()["items"]) == 2
    assert c.get("/delib/conversations", headers={"Authorization": "Bearer gw-secret", "x-hwax-user": ME}).status_code == 403
    assert c.get("/delib/conversations", params={"since": -1}, headers={"Authorization": "Bearer gw-secret"}).status_code == 422

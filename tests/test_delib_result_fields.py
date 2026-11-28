# deliberate_result 의 칸 고르기(HWAXPortal docs/request-1125 2절 B-1) — fields 를 주면 그 칸만, 비우면 종전 그대로
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_result_fields.py -q
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import delib_jobs  # noqa: E402
import mcp_server as m  # noqa: E402

JID = "diagnosis-20261110-101010-abcdef"


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    delib_jobs._persist({"id": JID, "job": "diagnosis", "status": "done", "question": "화두", "user": "", "started_at": 1.0,
                         "finished_at": 2.0, "decision": "결론 문단", "turns": [], "report_id": 7})


def _body(r):
    return r if isinstance(r, dict) else json.loads(r)


def test_B1_fields_를_주면_그_칸만_싣는다(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    full = _body(asyncio.run(m.deliberate_result(JID)))
    got = _body(asyncio.run(m.deliberate_result(JID, fields=["decision", "nope"])))
    assert set(got) - {"fields_all", "fields_unknown", "page"} == {"job_id", "status", "decision"}
    assert got["decision"] == full["decision"] and got["fields_unknown"] == ["nope"] and "decision" in got["fields_all"]
    summ = _body(asyncio.run(m.deliberate_result(JID, view="summary", fields=["report_id"])))
    assert summ["report_id"] == 7 and "decision" not in summ


def test_B1_비우면_종전과_같다(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert _body(asyncio.run(m.deliberate_result(JID))) == _body(asyncio.run(m.deliberate_result(JID, fields=[])))

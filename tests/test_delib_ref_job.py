# 근거 참조 kind delib_job — 앞 심의 결정문을 참조 한 줄로(HWAXPortal docs/request-1125 WD-01)
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_ref_job.py -q
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import delib_refs  # noqa: E402

ME = "me@example.com"
JID = "diagnosis-20261110-101010-abcdef"
LONG = "\n\n".join(f"문단 {i} " + "가" * 1000 for i in range(53))       # 약 5.3만 자 결정문


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    delib_jobs._persist({"id": JID, "job": "diagnosis", "status": "done", "question": "화두", "user": ME, "owner_aff": "CAEG",
                         "started_at": 1.0, "finished_at": 2.0, "decision": LONG,
                         "turns": [{"round": 1, "persona": "기구", "say": "줄인", "say_full": "기구 발언 전문"}]})


def _resolve(ev, user=ME, groups=()):
    async def boom(*_a, **_k):
        raise AssertionError("delib_job 은 게이트웨이를 부르지 않는다")
    delib_refs.call_alias, keep = boom, delib_refs.call_alias
    try:
        return asyncio.run(delib_refs.resolve(object(), ev, groups=list(groups), user=user))
    finally:
        delib_refs.call_alias = keep


def test_WD01_결정문_전문을_참조_한_줄로_펼친다(ledger):
    out = _resolve([{"ref": {"kind": "delib_job", "id": JID}}])
    assert len(out["evidence"]) == 1 and out["evidence"][0]["result"] == LONG and out["omitted"] == []
    rec = out["refs"][0]
    assert rec["ok"] and rec["chars"] == len(LONG) and rec["cite"]["source_kind"] == "delib_job" and rec["cite"]["scope"] == JID


def test_WD01_view_요약과_전문(ledger):
    summ = _resolve([{"ref": {"kind": "delib_job", "id": f"{JID}:summary"}}])["evidence"][0]["result"]
    assert summ.startswith("문단 0") and "문단 3" not in summ and "문단 2" in summ
    full = _resolve([{"ref": {"kind": "delib_job", "id": f"{JID}:full"}}])["evidence"][0]
    assert "기구 발언 전문" in full["result"] and json.loads(full["args"]) == {"job_id": JID, "view": "full"}


def test_WD01_남의_잡_없는_잡_틀린_view_는_빼고_까닭(ledger):
    out = _resolve([{"ref": {"kind": "delib_job", "id": JID}}], user="x@other.com")
    assert out["evidence"] == [] and "볼 수 없다" in out["omitted"][0]["text"]
    assert "볼 수 없다" in _resolve([{"ref": {"kind": "delib_job", "id": "nope-1"}}])["omitted"][0]["text"]
    assert "view 는" in _resolve([{"ref": {"kind": "delib_job", "id": f"{JID}:bogus"}}])["omitted"][0]["text"]
    assert _resolve([{"ref": {"kind": "delib_job", "id": JID}}], user="admin@x.com", groups=["portal-admin"])["evidence"], "관리자는 푼다"

# 끝난 심의 결정문을 RA 로 저장(HWAXPortal docs/request-1125 WD-09) — deliberate_save_report: 그 사람 이름 · 한 번만 · 엔진 보고서 꼴 · 거절 갈래
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_save_report.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

ME, YOU = "me@example.com", "you@example.com"
JID = "diagnosis-20261110-101010-abcdef"


def _ctx(user=ME, aff="CAEG"):
    hdr = {"x-hwax-groups": "연구소", "x-hwax-affiliation": aff}
    if user:
        hdr["x-hwax-user"] = user
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=hdr)))


@pytest.fixture
def kit(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace()))
    calls: list = []
    who: list = []

    async def _co(**kw):
        calls.append(kw)
        return json.dumps({"report_id": 58, "title": kw.get("title"), "url": "/w/ws1/reports/58"})
    ra = StructuredTool(name="create_report_draft", description="RA", coroutine=_co,
                        args_schema={"type": "object", "properties": {"title": {"type": "string"}, "blocks": {"type": "object"}}})

    async def _tbn(app_, groups, result_max=None, desc_max=None, user="", user_pat="", svc_fallback=True, as_of=None):
        who.append(user)
        return {"create_report_draft": ra}
    monkeypatch.setattr(d, "_tools_by_name", _tbn)
    job = {"id": JID, "job": "diagnosis", "status": "done", "question": "S26 낙하 화면 깨짐의 원인", "user": ME, "owner_aff": "CAEG",
           "started_at": 1.0, "finished_at": 2.0, "decision": "결론 — 모서리 응력.\n\n권고 — 리브 보강.", "report_id": None,
           "turns": [{"round": 1, "persona": "기구", "say": "1R 발언"}, {"round": 2, "persona": "기구", "say": "줄인 2R", "say_full": "2R 발언 전문"},
                     {"round": 2, "persona": "재료", "say": "재료 2R"}]}
    delib_jobs._persist(job)
    return SimpleNamespace(calls=calls, who=who, job=job, mp=monkeypatch)


def test_WD09_끝난_잡을_그_사람_이름으로_한_번만_저장한다(kit):
    out = asyncio.run(m.deliberate_save_report(JID, ctx=_ctx()))
    assert out["report_id"] == 58 and out["report_url"] == "/w/ws1/reports/58" and out["already"] is False
    assert kit.who == [ME] and len(kit.calls) == 1
    blocks = kit.calls[0]["blocks"]
    flat = json.dumps(blocks, ensure_ascii=False)
    assert "결론 — 모서리 응력." in flat and "2R 발언 전문" in flat and "재료 2R" in flat and "S26 낙하 화면 깨짐의 원인" in flat
    again = asyncio.run(m.deliberate_save_report(JID, ctx=_ctx()))
    assert again["already"] is True and again["report_id"] == 58 and len(kit.calls) == 1, "다시 불러도 새로 만들지 않는다"
    saved = delib_jobs.get(JID)
    assert saved["report_saves"][ME]["report_id"] == 58 and saved["report_id"] == 58, "주인이면 잡의 보고서 번호도 채운다"


def test_WD09_같은_소속_다른_사람은_제_이름으로_따로_저장한다(kit):
    out = asyncio.run(m.deliberate_save_report(JID, ctx=_ctx(YOU)))
    assert out["already"] is False and kit.who == [YOU]
    assert delib_jobs.get(JID)["report_id"] is None, "남의 잡의 보고서 번호는 바꾸지 않는다"


def test_WD09_시작_때_저장한_주인은_그_번호를_돌려받는다(kit):
    delib_jobs._persist({**kit.job, "report_id": 41, "report_url": "/w/ws1/reports/41"})
    out = asyncio.run(m.deliberate_save_report(JID, ctx=_ctx()))
    assert out == {"job_id": JID, "report_id": 41, "report_url": "/w/ws1/reports/41", "source": "start", "already": True,
                   "note": "이미 보고서 #41 로 저장했다 — 다시 저장하지 않았다"} and kit.calls == []


@pytest.mark.parametrize("patch, why", [({"status": "running"}, "끝나지 않았다"), ({"decision": None}, "결정문이 없다"),
                                        ({"chair_template": "risk-review"}, "리스크 심사")])
def test_WD09_저장할_수_없는_잡은_까닭과_함께_거절한다(kit, patch, why):
    delib_jobs._persist({**kit.job, **patch})
    with pytest.raises(ValueError, match=why):
        asyncio.run(m.deliberate_save_report(JID, ctx=_ctx()))
    assert kit.calls == []


def test_WD09_신원_없음_볼_수_없는_잡은_거절한다(kit):
    with pytest.raises(ValueError, match="신원 없는 호출"):
        asyncio.run(m.deliberate_save_report(JID, ctx=_ctx(user="")))
    with pytest.raises(ValueError, match="그런 심의 잡이 없다"):
        asyncio.run(m.deliberate_save_report(JID, ctx=_ctx("x@other.com", aff="OTHER")))


def test_WD09_웹_챗에서는_늘_확인_카드다():
    assert a._confirm_class("deliberate_save_report")[0] == "confirm"
    assert a._confirm_class("hwaxdeliberation_deliberate_save_report")[0] == "confirm"

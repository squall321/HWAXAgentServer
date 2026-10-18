# 같은 소속 심의 열람 — 상태 · 결과 · 발언 · 이어하기 · 목록이 주인 · 같은 소속 · 관리자 · 신원 없는 내부 호출만(HWAXPortal docs/followups-1017 결정 6)
"""사용자 결정(10-18, 질문 카드) — 소속은 **잡을 열 때** 것(게이트웨이가 실어 준 포털 원장의 그때 값), 주인이 안 적힌 옛 잡은 **신원 없는
내부 호출과 관리자만**, 목록에는 **같은 소속 것도 주인 표시와 함께**. 빈 소속끼리는 같은 소속이 아니다. 조사(10-18) — 상태 · 결과 · 발언
조회에 주인 확인이 없었고, 이어하기가 남의 결정문을 내 잡으로 옮기는 우회로였다.

  실행:  .venv/bin/python -m pytest tests/test_delib_same_aff.py -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import _caps, _end, _play, _start, eng  # noqa: E402, F401

ME, MATE, OUTSIDER, ADMIN = "me@example.com", "mate@example.com", "out@example.com", "boss@example.com"


def _ctx(user, aff="", groups=""):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={
        "x-hwax-user": user, **({"x-hwax-affiliation": aff} if aff else {}), **({"x-hwax-groups": groups} if groups else {})})))


def test_볼_수_있는_사람의_규칙():
    job = {"user": "me%2Bdrop@example.com", "owner_aff": "CAEG"}
    assert delib_jobs.can_read(job, "") is True, "신원 없는 내부 호출(운영 · 워크플로)"
    assert delib_jobs.can_read(job, "me+drop@example.com") is True, "주인 — 원장의 퍼센트 인코딩 원문과 같은 꼴로 푼다"
    assert delib_jobs.can_read(job, MATE, "CAEG") is True, "같은 소속"
    assert delib_jobs.can_read(job, OUTSIDER, "OTHER") is False, "다른 소속"
    assert delib_jobs.can_read(job, OUTSIDER, "") is False, "소속을 모르는 호출자"
    assert delib_jobs.can_read({"user": ME, "owner_aff": ""}, MATE, "") is False, "빈 소속끼리는 같은 소속이 아니다"
    assert delib_jobs.can_read(job, OUTSIDER, "OTHER", viewer_admin=True) is True, "관리자"
    old = {"user": "", "owner_aff": ""}
    assert delib_jobs.can_read(old, ME, "CAEG") is False, "주인 없는 옛 잡 — 신원 있는 사람은 못 본다"
    assert delib_jobs.can_read(old, "") is True and delib_jobs.can_read(old, ME, viewer_admin=True) is True


def test_조회_도구는_다른_소속을_막고_같은_소속은_연다(eng, monkeypatch):
    _caps(monkeypatch, 5)

    async def scenario():
        mine = await m.deliberate_start("내 화두 — 보스 보강", ctx=_ctx(ME, "CAEG"))
        rec = delib_jobs.get(mine["job_id"])
        assert rec["owner_aff"] == "CAEG", "잡을 열 때 소속을 적는다"
        await _end(eng, rec)
        jid = mine["job_id"]
        for tool in (m.deliberate_status, m.deliberate_result, m.deliberate_transcript):
            with pytest.raises(ValueError, match="볼 수 없다"):
                await tool(jid, ctx=_ctx(OUTSIDER, "OTHER"))
            assert (await tool(jid, ctx=_ctx(MATE, "CAEG"))), f"{tool.__name__} — 같은 소속은 본다"
            assert (await tool(jid, ctx=_ctx(OUTSIDER, "OTHER", groups="portal-admin"))), f"{tool.__name__} — 관리자"
            assert (await tool(jid)), f"{tool.__name__} — 신원 없는 내부 호출"
        with pytest.raises(ValueError, match="볼 수 없다"):
            await m.deliberate_continue(jid, human_note="이어서", ctx=_ctx(OUTSIDER, "OTHER"))
        assert len(delib_jobs._JOBS) == 1, "볼 수 없는 심의를 이어하기로 옮기지 않았다(새 잡이 서지 않았다)"
    _play(scenario)


def test_목록은_내_것과_같은_소속_것을_주인과_함께(eng, monkeypatch):
    _caps(monkeypatch, 9)

    async def scenario():
        await m.deliberate_start("내 것", ctx=_ctx(ME, "CAEG"))
        await m.deliberate_start("같은 소속 동료 것", ctx=_ctx(MATE, "CAEG"))
        await m.deliberate_start("다른 소속 것", ctx=_ctx(OUTSIDER, "OTHER"))
        _start("주인 없는 옛 잡")
        rows = (await m.deliberate_list(ctx=_ctx(ME, "CAEG")))["jobs"]
        got = {r["question"]: (r["mine"], r["owner"]) for r in rows}
        assert got == {"내 것": (True, ME), "같은 소속 동료 것": (False, MATE)}, got
        admin = {r["question"] for r in (await m.deliberate_list(ctx=_ctx(OUTSIDER, "OTHER", groups="portal-admin")))["jobs"]}
        assert admin == {"내 것", "같은 소속 동료 것", "다른 소속 것", "주인 없는 옛 잡"}, "관리자는 전부"
        internal = {r["question"] for r in (await m.deliberate_list())["jobs"]}
        assert len(internal) == 4, "신원 없는 내부 호출은 전부(운영이 쓰는 길)"
    _play(scenario)

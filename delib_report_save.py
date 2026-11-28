# 끝난 심의 잡의 결정문을 나중에 Report Archive 보고서로 남긴다 — deliberate_save_report(job_id)의 몸(HWAXPortal docs/request-1125 WD-09)
"""시작 때 save_report 를 끄고 돈 심의 · 나중에 남기기로 한 심의를 챗이 결정문 블록을 다시 쓰지 않고 RA 에 넣는다.

**같은 길.** 심의가 끝날 때 엔진이 저장하는 길(deliberation 의 create_report_draft — 블록 꼴 · 항목 나눔(_ra_blocks) · 제목 · 시스템 양식
(ra_formats.draft_args("deliberation")) · 결과 판정(_ra_save_outcome) · 주소(_ra_report_url))을 그대로 쓴다. 쓰는 신원은 부른 사람이다(MCP 로 연 심의
잡과 같은 길 — 게이트웨이 공유 비밀 + 그 사람의 신원 머리, deliberation._tools_by_name). 리스크 앱의 서비스 계정 쓰기 길(RK-01)과 겹치지 않는다 —
리스크 심사 계열 잡은 그 앱이 보고서를 다루므로 받지 않는다.

**한 번만.** 같은 사람이 같은 잡을 다시 저장하면 앞서 받은 보고서 번호를 돌려준다(원장 report_saves[사람]). 주인이면 시작 때 저장한 번호
(report_id)도 같은 것으로 본다. 나란한 두 부름은 잡 · 사람마다 자물쇠 하나로 한쪽만 RA 를 부른다.

**확인.** 웹 챗에서 이 도구는 늘 확인 카드다(app._CONFIRM_ALWAYS_TOOLS). 개인 Claude 는 쓰기 표지(readOnlyHint 거짓)를 보고 사람에게 묻는다.
"""
from __future__ import annotations

import asyncio
import time

import delib_jobs

_LOCKS: dict = {}
_LIVE = ("queued", "running")


def _paragraphs(text: str) -> list:
    return [p.strip() for p in str(text or "").split("\n\n") if p.strip()]


def blocks_of(job: dict) -> dict:
    """원장의 잡 → RA 블록(엔진 저장과 같은 칸 — background · results · recommendation · minutes). 결과 칸은 마지막 라운드 좌석 발언, 회의록은 발언
    전문(저장 상한 안 — say_full 이 있으면 그것)이다."""
    import deliberation as d  # noqa: PLC0415 — 무거운 엔진은 부를 때 읽는다
    turns = [t for t in (job.get("turns") or []) if isinstance(t, dict)]

    def _rnd(t):
        try:
            return int(t.get("round") or t.get("rnd") or 0)
        except (TypeError, ValueError):
            return 0
    last = max((_rnd(t) for t in turns), default=0)
    clip = d._transcript_clip()

    def _say(t):
        return d._clip_to(str(t.get("say_full") or t.get("say") or ""), clip)
    seats = list(dict.fromkeys(str(t.get("persona") or t.get("key") or "?") for t in turns))
    rounds = last or len({_rnd(t) for t in turns}) or 0
    return {
        "background": [f"심의 주제: {job.get('question') or ''}",
                       f"심의 잡 {job.get('id')} · 종류 {job.get('job') or 'default'} — 끝난 뒤 deliberate_save_report 로 저장"],
        "results": [f"• [{t.get('persona') or t.get('key') or '?'}] {_say(t)}" for t in turns if _rnd(t) == last and _say(t)],
        "recommendation": _paragraphs(job.get("decision") or job.get("result_text"))[:d._RA_PARAS_MAX],
        "minutes": ([f"참여: {', '.join(seats)}"] if seats else []) + ([f"{rounds}라운드 심의."] if rounds else [])
                   + [f"[{t.get('persona') or t.get('key') or '?'}] {_say(t)}" for t in turns][:d._RA_MINUTES_MAX],
    }


def check(job: dict) -> str:
    """저장할 수 있는 잡인가 — 까닭(빈 글이면 된다)."""
    import delib_subject  # noqa: PLC0415
    if delib_jobs.is_task(job):
        return "심의가 아닌 잡(위임 등)은 보고서로 남기지 않는다"
    if delib_subject.risk_family(job.get("chair_template"), job.get("started_by")):
        return "리스크 심사 계열 잡은 리스크 앱이 보고서를 다룬다 — 리스크 앱의 종합 보고서 길을 쓴다"
    if job.get("status") in _LIVE:
        return f"아직 끝나지 않았다(status={job.get('status')}) — 끝난 뒤 저장한다"
    if not (job.get("decision") or job.get("result_text")):
        return "결정문이 없다 — 의장이 결정문을 내기 전에 멈춘 잡이다"
    return ""


def already(job: dict, user: str) -> dict | None:
    """이 사람이 이미 저장한 보고서 — {report_id, report_url, source}(없으면 None)."""
    me = delib_jobs._norm(user)
    got = (job.get("report_saves") or {}).get(me)
    if isinstance(got, dict) and got.get("report_id") is not None:
        return {"report_id": got["report_id"], "report_url": got.get("report_url"), "source": "save_report"}
    if me and me == delib_jobs._owner(job) and job.get("report_id") is not None:
        return {"report_id": job["report_id"], "report_url": job.get("report_url"), "source": "start"}
    return None


async def save(app, job_id: str, *, user: str, groups: list) -> dict:
    """끝난 잡의 결정문을 그 사람 이름으로 RA 에 저장한다 → {job_id, report_id, report_url, already, note}. 실패는 ValueError(부르는 쪽이 고칠 글)."""
    import deliberation as d  # noqa: PLC0415
    import ra_formats  # noqa: PLC0415
    me = delib_jobs._norm(user)
    lock = _LOCKS.setdefault((job_id, me), asyncio.Lock())
    async with lock:
        job = delib_jobs.get(job_id)
        if not job:
            raise ValueError(f"그런 심의 잡이 없다: {job_id}")
        why = check(job)
        if why:
            raise ValueError(why)
        prev = already(job, user)
        if prev is not None:
            return {"job_id": job_id, **prev, "already": True,
                    "note": f"이미 보고서 #{prev['report_id']} 로 저장했다 — 다시 저장하지 않았다"}
        tools = await d._tools_by_name(app, list(groups or []), user=user)
        if not any(n == "create_report_draft" or n.endswith("_create_report_draft") for n in (tools or {})):
            raise ValueError("Report Archive 쓰기 도구(create_report_draft)가 이 사람의 도구에 없다 — RA 연결(토큰 등록)을 확인한다")
        name = next(n for n in sorted(tools) if n == "create_report_draft" or n.endswith("_create_report_draft"))
        raw = await d._call(tools, name, {**ra_formats.draft_args("deliberation"), "title": d._ra_report_title(job.get("question") or ""),
                                          "blocks": d._ra_blocks(blocks_of(job)), "tags": ["심의", "mcp-deliberation"]})
        rid, note = d._ra_save_outcome(raw, None, True)
        if rid is None:
            raise ValueError(note.strip().lstrip("⚠").strip() or "Report Archive 저장에 실패했다")
        url = d._ra_report_url(d._parse_json(raw), rid)
        live = delib_jobs._JOBS.get(job_id)
        rec = live if live is not None else job
        rec.setdefault("report_saves", {})[me] = {"report_id": rid, "report_url": url, "at": time.time()}
        if me == delib_jobs._owner(rec) and rec.get("report_id") is None:
            rec["report_id"], rec["report_url"] = rid, url
        delib_jobs._persist(rec)
        return {"job_id": job_id, "report_id": rid, "report_url": url, "already": False, "note": note.strip()}

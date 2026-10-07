# 이어하기가 호출자가 지어 준 좌석의 역할을 줄어든 사본으로 다시 앉히지 않는지 — 좌석이 받은 프롬프트로 본다
#
# 이어하기는 이전 잡의 좌석(seats)을 그대로 넘긴다. 그 좌석 목록은 엔진의 personas 이벤트에서 온 것이고,
# 그 이벤트의 역할은 소개 카드용으로 앞 280자만 실린다. 레지스트리에 있는 좌석은 엔진이 원본을 다시 읽어
# 오지만, 호출자가 지어 준 좌석(레지스트리에 없는 키)은 원본이 없어 그 280자가 곧 역할이 됐다 — 첫 회차에는
# 900자짜리 역할로 발언한 좌석이 이어하기에서는 앞 280자만 받았고, 아무 알림도 없었다.
#
# deliberate_start → 원장 파일 → deliberate_continue → 또 deliberate_continue 를 **실제 코드로** 잇는다. 가짜는
# 도구 목록과 LLM 호출뿐이고, 엔진은 1라운드 전원 발언까지 실제로 돈 뒤 결정문 한 줄로 닫는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_continue_seat_role.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _pin_context  # noqa: E402, F401

_TAIL = "[끝] 수치 없는 주장은 기각을 요구하라."
_ROLE = "[앞] 힌지 접착 계면 전문. " + "가" * 700 + _TAIL            # 호출자가 지어 준 좌석의 역할
_REG_ROLE = "[레지스트리 원본] 신뢰성 전문. " + "다" * 600 + " [레지스트리 끝]"
_SAY = json.dumps({"lens": "관점", "reads": ["해석"], "recommendation": "권장", "concerns": ["a", "b"],
                   "position_short": "입장"}, ensure_ascii=False)
_REAL_RUN = d.run_deliberation


class _Session:
    """get_agent_session — rel-b 만 레지스트리에 있다. custom-a 는 없는 키라 404 다."""

    name, coroutine = "get_agent_session", None

    async def ainvoke(self, args):
        if args.get("agent_type") == "rel-b":
            return json.dumps({"data": {"description": _REG_ROLE}}, ensure_ascii=False)
        raise RuntimeError('{"detail": "agent not found: ' + str(args.get("agent_type")) + '"}')


def _three_runs(monkeypatch, tmp_path, personas):
    """시작 → 이어하기 → 이어하기. 회차마다 {좌석 키: 그 좌석의 시스템 프롬프트} 를 돌려준다."""
    runs = []

    async def _tools(*_a, **_k):
        return {"get_agent_session": _Session()}

    async def _llm(_obj, system, _human):
        for key in ("custom-a", "rel-b"):
            if system.startswith(f"당신은 '{key}' 전문가입니다."):
                runs[-1][key] = system
        return _SAY

    async def _entry(app_, q, groups, opts, *a):
        """실제 엔진을 1라운드 전원 발언까지 돌리고 결정문으로 닫는다(의장 단계는 이 결함과 무관하다)."""
        runs.append({})
        seats = turns = 0
        gen = _REAL_RUN(app_, q, groups, {**(opts or {}), "free_tools": 0, "voc": "off", "rescreen": 0,
                                           "persona_knowledge": 0}, *a)
        try:
            async for chunk in gen:
                yield chunk
                ev, data = delib_jobs._parse_sse(chunk)
                assert ev != "error", data
                if ev == "delib" and data.get("kind") == "personas":
                    seats = len(data["personas"])
                if ev == "delib" and data.get("kind") == "turn":
                    turns += 1
                    if seats and turns >= seats:
                        break
        finally:
            await gen.aclose()
        yield d._delib("decision", text="접착 계면 박리가 지배 원인.")
        yield d._sse("done", {})

    monkeypatch.setattr(d, "_tools_by_name", _tools)
    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "run_deliberation", _entry)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)))

    async def go():
        first = await m.deliberate_start("힌지 크랙 원인", personas=personas)
        await delib_jobs._TASKS[first["job_id"]]
        assert delib_jobs.get(first["job_id"])["status"] == "done", delib_jobs.get(first["job_id"])
        delib_jobs._JOBS.clear()       # 재기동 뒤처럼 — 메모리에 없으면 원장 파일에서 읽는다
        second = await m.deliberate_continue(first["job_id"], "두께를 다시 보라")
        await delib_jobs._TASKS[second["job_id"]]
        delib_jobs._JOBS.clear()
        third = await m.deliberate_continue(second["job_id"], "한 번 더")      # 이어하기의 이어하기
        await delib_jobs._TASKS[third["job_id"]]
        return [delib_jobs.get(x["job_id"]) for x in (first, second, third)]

    jobs = asyncio.run(go())
    assert len(runs) == 3 and all(set(r) == {"custom-a", "rel-b"} for r in runs), [sorted(r) for r in runs]
    return runs, jobs


def test_이어하기는_호출자가_지어_준_좌석의_역할을_통째로_다시_앉힌다(monkeypatch, tmp_path):
    assert 280 < len(_ROLE) <= d._ROLE_REQ_MAX
    runs, jobs = _three_runs(monkeypatch, tmp_path, [{"key": "custom-a", "role": _ROLE},
                                                     {"key": "rel-b", "role": ""}])
    assert _TAIL in runs[0]["custom-a"], "시험 전제 — 첫 회차는 역할 전문으로 발언한다"
    shown = next(s for s in jobs[0]["seats"] if s["key"] == "custom-a")
    assert len(shown["role"]) == 280, "시험 전제 — 원장의 좌석 목록에는 소개용 280자만 있다"
    for n, r in enumerate(runs[1:], start=2):
        assert _TAIL in r["custom-a"], f"{n}회차 좌석 프롬프트에 역할 뒷부분이 없다 — 소개용 280자 사본으로 앉았다"
        assert _REG_ROLE in r["rel-b"], f"{n}회차 레지스트리 좌석이 원본으로 복원되지 않았다"


def test_좌석_목록의_모양과_화면용_역할은_그대로다(monkeypatch, tmp_path):
    """원장의 seats 는 화면·목록이 읽는 계약이다 — 역할 전문은 따로 적고 그 목록은 건드리지 않는다."""
    _runs, jobs = _three_runs(monkeypatch, tmp_path, [{"key": "custom-a", "role": _ROLE},
                                                      {"key": "rel-b", "role": ""}])
    for job in jobs:
        assert [s["key"] for s in job["seats"]] == ["custom-a", "rel-b"]
        assert all(len(s["role"]) <= 280 for s in job["seats"])
        assert delib_jobs.summary(job)["seats"] == ["custom-a", "rel-b"]
        assert job["opts"]["personas"] == 2, "잡 기록의 손잡이에는 좌석 수만 남는다"

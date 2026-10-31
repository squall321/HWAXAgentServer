# 이어하기의 좌석 — 지어 준 좌석의 역할을 줄어든 사본으로 앉히지 않는지, 재심사 신규 좌석이 1라운드에 이전 결론 없이 앉고 2라운드 교차심문에 걸리는지 — 좌석이 받은 프롬프트로 본다
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
import re
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


# ── 재심사로 새로 앉은 좌석(HWAXPortal docs/deliberation-quality F10 · F12) ─────────────────────────────────
# 이어하기는 유임 좌석 전원에 재심사 신규 좌석을 더한다(F10 — '원 질문 + 이전 결론 + 사람 의견' 으로 recommend_agents 를 다시
# 불러, 이미 앉은 도메인은 빼고 DELIB_RESCREEN_SEATS 명까지). 신규 좌석은 1라운드에 이전 결론을 받지 않는다(F12 — 앵커링
# 차단, 2라운드부터 받는다). 그 독립 판단이 이전 결론과 어긋나면 이어하기의 소득인데, 2라운드 교차심문에서 아무도 그 좌석을
# 겨냥하지 않으면 무사통과한다 — 관련도(용어 겹침)로 표적을 고르면 홀로 다른 말을 하는 신규 좌석이 거기 걸리기 쉬워
# _cross_targets 가 끝에서 보정한다. 이어하기 한 번을 실제 코드로 돌려 좌석이 받은 프롬프트로 본다.

_PREV = "이전결론표지 Q7 — 접착 계면 박리가 지배 원인."         # 첫 회차 결정문 = 이어하기의 이전 결론(continue_summary)
_NOTE = "산화 쪽 원인도 따져 보라"                             # 이어하기의 사람 의견
_CONT_MARK = "[이전 심의 요약 — 이어서 논의]"                  # 엔진 _cont_block 이 이전 결론 앞에 다는 표지
_CARRY = [{"key": "mech-hinge", "role": "힌지 기구"}, {"key": "rel-fatigue", "role": "피로 신뢰성"},
          {"key": "mat-adhesive", "role": "접착 재료"}]
_NEW = "chem-oxide"                                            # 재심사가 더하는 좌석 — 아직 앉지 않은 도메인
# 1라운드 발언 — 유임 셋은 같은 말을 하고 신규 좌석은 겹치는 말이 없다(관련도만으로는 아무도 신규 좌석을 고르지 않는다).
_R1_CARRY = {"lens": "힌지 접착 계면 박리 두께 피로 균열 진전", "reads": ["힌지 접착 계면 박리"],
             "recommendation": "힌지 접착 두께 재검토", "concerns": ["계면 박리", "피로 균열"], "position_short": "힌지 접착"}
_R1_NEW = {"lens": "산화피막 갈바닉 전위차 염수분무 부식", "reads": ["산화피막 갈바닉"],
           "recommendation": "염수분무 시험", "concerns": ["갈바닉 부식", "전위차"], "position_short": "산화피막"}
_R2 = {"concede": [], "rebut": [{"target": "mech-hinge", "quote": "인용", "counter": "반박", "basis": "근거"}],
       "deepen": "심화 논증 첫째. 둘째. 셋째."}


def _kind_of(prompt: str) -> str:
    """좌석 발언 프롬프트의 라운드 성격 — 엔진의 라운드별 지시문으로 가른다."""
    if "다른 전문가 입장에 수용(concede)" in prompt:
        return "deepen"
    if "최종 입장(final_position" in prompt:
        return "converge"
    return "initial" if "당신의 관점(lens" in prompt else "?"


def _continue_once(monkeypatch, tmp_path, carry, recs, *, rounds=3, stop_round=2, advanced=None):
    """시작(1라운드 뒤 결정문 _PREV 로 닫는다) → 이어하기 한 번(재심사 켬, 사람 의견 _NOTE). recs 는 recommend_agents 가 돌려줄
    좌석 키다. 이어하기는 stop_round 라운드의 전원 발언까지 실제로 돌고 닫는다(None 이면 엔진이 스스로 끝낼 때까지 — 체크포인트).

    반환 SimpleNamespace(prompts={(좌석, 라운드 성격): 이어하기에서 그 좌석이 받은 첫 발언 프롬프트},
                          queries=recommend_agents 가 받은 질의들, job=이어하기 잡 원장, events=이어하기 사건들)."""
    runs, queries = [], []

    class _Rec:
        name, coroutine = "recommend_agents", None

        async def ainvoke(self, args):
            queries.append(str(args.get("q") or ""))
            return json.dumps([{"agent_type": k} for k in recs])

    async def _tools(*_a, **_k):
        return {"get_agent_session": _Session(), "recommend_agents": _Rec()}

    async def _llm(_obj, system, human, *_a):
        seat = re.match(r"당신은 '([^']+)' 전문가입니다\.", system)
        if not seat:
            return "결정문"
        kind = _kind_of(human)
        runs[-1]["prompts"].setdefault((seat.group(1), kind), human)      # 재시도(인용 검증)는 첫 프롬프트에 꼬리만 붙는다
        if kind == "initial":
            return json.dumps(_R1_NEW if seat.group(1) == _NEW else _R1_CARRY, ensure_ascii=False)
        return json.dumps(_R2, ensure_ascii=False)

    async def _entry(app_, q, groups, opts, *a, **k):
        """실제 엔진을 돌린다 — 시작 회차는 1라운드, 이어하기는 stop_round 라운드의 전원 발언까지(의장 단계는 이 시험과 무관하다)."""
        first = not runs
        runs.append({"prompts": {}, "events": []})
        stop, seats, turns = (1 if first else stop_round), 0, 0
        gen = _REAL_RUN(app_, q, groups, {**(opts or {}), "free_tools": 0, "voc": "off", "persona_knowledge": 0}, *a, **k)
        try:
            async for chunk in gen:
                yield chunk
                ev, data = delib_jobs._parse_sse(chunk)
                runs[-1]["events"].append((ev, data))
                assert ev != "error", data
                if ev == "delib" and data.get("kind") == "personas":
                    seats = len(data["personas"])
                if stop and ev == "delib" and data.get("kind") == "turn" and data.get("round") == stop:
                    turns += 1
                    if turns >= seats:
                        break
            else:
                return                     # 엔진이 스스로 끝냈다(체크포인트) — 결정문 · done 을 이미 냈다
        finally:
            await gen.aclose()
        yield d._delib("decision", text=_PREV if first else "이어하기 결론")
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
        first = await m.deliberate_start("힌지 크랙 원인", personas=carry)
        await delib_jobs._TASKS[first["job_id"]]
        assert delib_jobs.get(first["job_id"])["decision"] == _PREV, "시험 전제 — 첫 회차 결정문이 이전 결론이다"
        delib_jobs._JOBS.clear()       # 재기동 뒤처럼 — 메모리에 없으면 원장 파일에서 읽는다
        second = await m.deliberate_continue(first["job_id"], _NOTE, rounds=rounds,
                                             advanced={"rescreen": 1, **(advanced or {})})
        await delib_jobs._TASKS[second["job_id"]]
        return delib_jobs.get(second["job_id"])

    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    assert len(runs) == 2, len(runs)
    return SimpleNamespace(prompts=runs[1]["prompts"], queries=queries, job=job, events=runs[1]["events"])


def _target_block(prompt: str) -> str:
    """심화 라운드 프롬프트의 교차심문 표적 원문 구역 — '[당신의 지정 반박 표적 …]' 부터 '[다른 전문가 한 줄 입장]' 앞까지."""
    head = prompt.index("[당신의 지정 반박 표적")
    return prompt[head:prompt.index("[다른 전문가 한 줄 입장]", head)]


def test_재심사_신규_좌석은_1라운드에_이전_결론_없이_앉고_2라운드에_받는다(monkeypatch, tmp_path):
    """F12 — 1라운드 신규 좌석 프롬프트에 이전 결론 표지가 없고 유임엔 있으며, 2라운드는 둘 다 있다(deliberation-quality 77)."""
    r = _continue_once(monkeypatch, tmp_path, _CARRY, ["mech-other", _NEW])
    seated = [(s["key"], s["origin"]) for s in r.job["seats"]]
    assert seated == [(p["key"], "carry") for p in _CARRY] + [(_NEW, "new")], (
        f"시험 전제 — 유임 셋에 착석하지 않은 도메인 한 석이 더해져야 한다: {seated}")
    new1 = r.prompts[(_NEW, "initial")]
    assert _CONT_MARK not in new1 and _PREV not in new1, "신규 좌석이 1라운드에 이전 결론을 받았다 — 앵커링 차단(F12)이 안 걸렸다"
    assert "이번 회차에 새로 합류했다" in new1, "신규 좌석 1라운드 지시문(독립 판단 요구)이 없다"
    assert _NOTE in new1, "가린 것은 이전 결론뿐이다 — 사람 의견은 신규 좌석도 1라운드부터 받는다"
    for p in _CARRY:
        carry1 = r.prompts[(p["key"], "initial")]
        assert _CONT_MARK in carry1 and _PREV in carry1, f"유임 {p['key']} 이 1라운드에 이전 결론을 받지 못했다"
        assert "이번 회차에 새로 합류했다" not in carry1
    for key in [p["key"] for p in _CARRY] + [_NEW]:
        r2 = r.prompts[(key, "deepen")]
        assert _CONT_MARK in r2 and _PREV in r2, f"{key} 가 2라운드에 이전 결론을 받지 못했다"


def test_재심사_신규_좌석의_1라운드_입장이_2라운드_교차심문_표적에_실린다(monkeypatch, tmp_path):
    """신규 좌석의 1라운드 입장은 다른 좌석과 겹치는 말이 없어 관련도만으로는 아무도 고르지 않는다 — 커버리지 보정이 표적 칸 하나를
    내준다(deliberation-quality 78). 쟁점 · 소수의견으로 남는지는 실모델 실행에서 따로 읽는다."""
    carry_terms = d._terms(" ".join(json.dumps(v, ensure_ascii=False) for v in _R1_CARRY.values()))
    new_terms = d._terms(" ".join(json.dumps(v, ensure_ascii=False) for v in _R1_NEW.values()))
    assert not carry_terms & new_terms, "시험 전제 — 신규 좌석의 1라운드 발언이 유임 좌석과 겹치는 말이 없어야 한다"
    r = _continue_once(monkeypatch, tmp_path, _CARRY, ["mech-other", _NEW])
    blocks = {key: _target_block(r.prompts[(key, "deepen")]) for key in [p["key"] for p in _CARRY] + [_NEW]}
    hit = [key for key, blk in blocks.items() if key != _NEW and f"— {_NEW} —" in blk and _R1_NEW["lens"] in blk]
    assert hit, "2라운드에 신규 좌석을 겨냥한 좌석이 없다 — 그 좌석의 1라운드 입장이 반박을 한 번도 받지 않고 지나간다"
    for key in [p["key"] for p in _CARRY]:
        assert any(f"— {key} —" in blk for k2, blk in blocks.items() if k2 != key), f"{key} 를 겨냥한 좌석이 없다"


def test_원_이어하기_꼴_재심사는_유임_전원에_착석하지_않은_도메인_둘까지만_더한다(monkeypatch, tmp_path):
    """원 이어하기(6석 · 두 도메인 · 사람 의견 · 1라운드 체크포인트)와 같은 꼴(deliberation-quality 42 의 회귀 시험) — 유임 전원이
    그대로 앉고, 추천 가운데 이미 앉은 좌석 · 앉은 도메인은 빠지며, 새 좌석은 DELIB_RESCREEN_SEATS(2)까지다."""
    carry = [{"key": k, "role": ""} for k in ("disp-a", "disp-b", "disp-c", "disp-d", "sw-a", "sw-b")]
    recs = ["disp-a", "disp-x", "sw-x", "svc-ops", "field-data", "rel-chem"]
    assert d._RESCREEN_SEATS == 2, "시험 전제 — 재심사 신규 좌석 상한의 기본값"
    r = _continue_once(monkeypatch, tmp_path, carry, recs, stop_round=None,
                       advanced={"stop_after_round": 1, "save_report": 0})
    seated = [(s["key"], s["origin"]) for s in r.job["seats"]]
    assert seated == [(p["key"], "carry") for p in carry] + [("svc-ops", "new"), ("field-data", "new")], seated
    assert len(r.queries) == 1, r.queries
    q = r.queries[0]
    assert q.startswith("힌지 크랙 원인") and f"[이전 결론]\n{_PREV}" in q and f"[사람 의견]\n{_NOTE}" in q, (
        "재심사 질의는 '원 질문 + 이전 결론 + 사람 의견' 이다")
    steps = [x.get("step", "") for ev, x in r.events if ev == "status"]
    assert "이어하기 재심사 — 신규 좌석 svc-ops, field-data" in steps, steps
    assert (r.job.get("decision") or "").startswith("[체크포인트"), "1라운드 체크포인트로 끝나야 한다(보고서 저장 없음)"
    assert {k for k, _kind in r.prompts} == {k for k, _o in seated}, "새 좌석이 1라운드에 발언하지 않았다"


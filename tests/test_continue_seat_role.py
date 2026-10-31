# 이어하기의 좌석 — 지어 준 좌석의 역할을 줄어든 사본으로 앉히지 않는지, 재심사가 사람 의견의 짧은 축으로 새 도메인을 찾는지, 재심사 신규 좌석이 1라운드에 이전 결론 없이 앉고 2라운드 교차심문에 걸리는지 — 좌석이 받은 프롬프트로 본다
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

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _pin_context, _Tool  # noqa: E402, F401

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
# 이어하기는 유임 좌석 전원에 재심사 신규 좌석을 더한다(F10 — 사람 의견(과 원 질문)에서 뽑은 짧은 축마다 recommend_agents 를
# 깊게(top_k) 불러 후보를 합치고, 앉은 좌석 · 앉은 도메인(넓은 접두는 둘째 마디까지)은 빼고 DELIB_RESCREEN_SEATS 명까지).
# 신규 좌석은 1라운드에 이전 결론을 받지 않는다(F12 — 앵커링 차단, 2라운드부터 받는다). 그 독립 판단이 이전 결론과
# 어긋나면 이어하기의 소득인데, 2라운드 교차심문에서 아무도 그 좌석을
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


def _continue_once(monkeypatch, tmp_path, carry, recs, *, rounds=3, stop_round=2, advanced=None, note=_NOTE,
                   axes="산화피막 부식", domains=None):
    """시작(1라운드 뒤 결정문 _PREV 로 닫는다) → 이어하기 한 번(재심사 켬, 사람 의견 note). 이어하기는 stop_round 라운드의 전원
    발언까지 실제로 돌고 닫는다(None 이면 엔진이 스스로 끝낼 때까지 — 체크포인트).

    recs — recommend_agents 가 돌려줄 좌석 키(목록이면 질의와 상관없이 그대로, 함수면 받은 인자로 지은 목록 — AIDataHub 응답 꼴
    {agents: [...]} 로 싸서 준다). axes — 재심사 검색 축을 묻는 LLM 의 답(예외를 주면 그 예외를 올린다). domains — 주면
    list_agent_domains 가 그 행들을 돌려준다(MCP 가 목록 원소마다 글을 따로 실어 이어 붙인 꼴 그대로).

    반환 SimpleNamespace(prompts={(좌석, 라운드 성격): 이어하기에서 그 좌석이 받은 첫 발언 프롬프트},
                          queries=recommend_agents 가 받은 질의들, calls=그 인자들, axis_prompts=검색 축을 물은 프롬프트들,
                          job=이어하기 잡 원장, events=이어하기 사건들)."""
    runs, queries, calls, axis_prompts = [], [], [], []

    class _Rec:
        name, coroutine = "recommend_agents", None

        async def ainvoke(self, args):
            queries.append(str(args.get("q") or ""))
            calls.append(dict(args))
            keys = recs(args) if callable(recs) else recs
            return json.dumps({"agents": [{"agent_type": k} for k in keys]})

    class _Domains:
        name, coroutine = "list_agent_domains", None

        async def ainvoke(self, _args):
            return "".join(json.dumps(r) for r in domains)

    async def _tools(*_a, **_k):
        return {"get_agent_session": _Session(), "recommend_agents": _Rec(),
                **({"list_agent_domains": _Domains()} if domains is not None else {})}

    async def _llm(_obj, system, human, *_a):
        if system.startswith("당신은 심의체 좌석 구성을 검토하는 조정자"):      # 재심사 검색 축(_rescreen_axes)
            axis_prompts.append(human)
            if isinstance(axes, BaseException):
                raise axes
            return axes
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
        second = await m.deliberate_continue(first["job_id"], note, rounds=rounds,
                                             advanced={"rescreen": 1, **(advanced or {})})
        await delib_jobs._TASKS[second["job_id"]]
        return delib_jobs.get(second["job_id"])

    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    assert len(runs) == 2, len(runs)
    return SimpleNamespace(prompts=runs[1]["prompts"], queries=queries, calls=calls, axis_prompts=axis_prompts, job=job,
                           events=runs[1]["events"])


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


# 원 이어하기(deliberation-quality 42)의 꼴 — 한 도메인 넷(disp) · 넓은 접두(sw) 둘, 사람 의견이 지금 좌석 밖(필드 · 서비스)을 다루라 한다.
_B17_CARRY = [{"key": k, "role": ""} for k in ("disp-a", "disp-b", "disp-c", "disp-d", "sw-lum-ctl", "sw-tone-mgr")]
_B17_NOTE = "열화를 필드에서 추적하는 체계와 서비스 교체 기준을 정하라"


def _rescreen_once(monkeypatch, tmp_path, carry, recs, **kw):
    """이어하기 한 번을 1라운드 체크포인트까지(보고서 저장 없음) — 아래 재심사 시험들이 쓴다."""
    return _continue_once(monkeypatch, tmp_path, carry, recs, stop_round=None,
                          advanced={"stop_after_round": 1, "save_report": 0}, **kw)


def _new_seats(r) -> list:
    return [s["key"] for s in r.job["seats"] if s["origin"] == "new"]


def _steps(r) -> list:
    return [x.get("step", "") for ev, x in r.events if ev == "status"]


def test_원_이어하기_꼴_재심사는_유임_전원에_사람_의견의_축으로_새_도메인_둘까지만_더한다(monkeypatch, tmp_path):
    """원 이어하기(6석 · 두 도메인 · 사람 의견 · 1라운드 체크포인트)와 같은 꼴(deliberation-quality 42 의 회귀 시험) — 유임 전원이
    그대로 앉고, 사람 의견에서 뽑은 짧은 축마다 깊게 물어 합친 후보 가운데 앉은 좌석 · 앉은 도메인(sw 는 둘째 마디까지)은 빠지며,
    새 좌석은 축마다 깊이 차례로 DELIB_RESCREEN_SEATS(2)까지다."""
    axes = ["필드 열화 추적", "서비스 교체 판정", "보상 테이블 배포"]
    lists = {axes[0]: ["disp-x", "sw-field-mon", "sw-field-rel", "xd-field-tri"],
             axes[1]: ["disp-y", "xd-svc-ops", "sw-lum-x"],
             axes[2]: ["sw-tone-y", "cam-tune", "disp-z"]}         # 둘째 깊이에서 셋이 다 새 도메인 — 상한이 셋째를 막는다
    assert d._RESCREEN_SEATS == 2, "시험 전제 — 재심사 신규 좌석 상한의 기본값"
    r = _rescreen_once(monkeypatch, tmp_path, _B17_CARRY, lambda a: lists.get(a["q"], ["disp-x", "cam-tune"]),
                       note=_B17_NOTE, axes="\n".join(axes))
    seated = [(s["key"], s["origin"]) for s in r.job["seats"]]
    assert seated == [(p["key"], "carry") for p in _B17_CARRY] + [("sw-field-mon", "new"), ("xd-svc-ops", "new")], seated
    assert sorted(r.queries) == sorted(axes), f"재심사 질의는 검색 축 셋이다(한 덩이 질의가 아니다): {r.queries}"
    assert all(c.get("top_k") == d._RESCREEN_TOP_K for c in r.calls), r.calls
    assert len(r.axis_prompts) == 1 and _B17_NOTE in r.axis_prompts[0] and _PREV not in r.axis_prompts[0], (
        "검색 축은 사람 의견(과 원 질문)에서 짓는다 — 이전 결론은 싣지 않는다")
    assert "이어하기 재심사 — 신규 좌석 sw-field-mon(필드 열화 추적), xd-svc-ops(서비스 교체 판정)" in _steps(r), _steps(r)
    assert (r.job.get("decision") or "").startswith("[체크포인트"), "1라운드 체크포인트로 끝나야 한다(보고서 저장 없음)"
    assert {k for k, _kind in r.prompts} == {k for k, _o in seated}, "새 좌석이 1라운드에 발언하지 않았다"


def test_재심사는_추천을_도구_기본_다섯명보다_깊게_받는다(monkeypatch, tmp_path):
    """① recommend_agents 는 top_k 를 안 주면 5명만 준다(AIDataHub mcp_runtime.recommend_agents) — 앉은 도메인이 그 다섯을
    채우면 새 도메인이 보이지 않았다(10-31 dev 재실행 — 다섯 중 넷이 앉은 도메인)."""
    ranked = ["disp-p", "sw-lum-p", "disp-q", "sw-tone-p", "disp-r", "rel-field-q", "cam-q"]
    r = _rescreen_once(monkeypatch, tmp_path, _B17_CARRY, lambda a: ranked[:int(a.get("top_k") or 5)],
                       note=_B17_NOTE, axes="필드 열화 추적")
    assert _new_seats(r) == ["rel-field-q", "cam-q"], f"다섯 뒤의 새 도메인을 보지 못했다: {_new_seats(r)}"
    assert [c.get("top_k") for c in r.calls] == [d._RESCREEN_TOP_K] and 20 <= d._RESCREEN_TOP_K <= 40, r.calls


def test_재심사는_사람_의견에서_뽑은_짧은_축으로_묻고_이전_결론은_싣지_않는다(monkeypatch, tmp_path):
    """② '원 질문 + 이전 결론 + 사람 의견' 한 덩이는 임베딩이 원 어휘(지금 좌석) 쪽으로 평균된다 — 사람 의견이 연 방향(필드)의
    좌석은 짧은 축 질의에서만 위로 온다. 가짜 추천은 그 성질 그대로다."""
    def rec(a):
        q = a["q"]
        if _PREV in q or len(q) > 200:                    # 한 덩이 질의 — 지금 좌석의 이웃만 돌아온다
            return ["disp-x", "sw-lum-x", "cam-near"]
        return ["xd-field-tri", "disp-x"] if "필드" in q else ["cam-near"]

    r = _rescreen_once(monkeypatch, tmp_path, _B17_CARRY, rec, note=_B17_NOTE, axes="필드 열화 추적\n교체 판정 기준")
    assert _new_seats(r) == ["xd-field-tri", "cam-near"], f"사람 의견이 연 방향의 좌석이 앉지 않았다: {_new_seats(r)}"
    assert r.queries == ["필드 열화 추적", "교체 판정 기준"], r.queries
    assert not any(_PREV in q or "[이전 결론]" in q for q in r.queries), "재심사 질의에 이전 결론이 실렸다"
    assert "힌지 크랙 원인" in r.axis_prompts[0], "원 질문은 축을 짓는 맥락으로 간다"


@pytest.mark.parametrize("axes,why", [(RuntimeError("LLM 연결 실패"), "RuntimeError"), ("  \n", "빈 답")],
                         ids=["llm-error", "empty"])
def test_재심사_축을_못_지으면_한_덩이로_묻지_않고_유임_전원으로_가며_그렇다고_알린다(monkeypatch, tmp_path, axes, why):
    """축을 묻는 LLM 이 실패하거나 빈 답이면 재심사는 신규 좌석 없이 끝난다 — 사람 의견 · 이전 결론을 한 덩이로 묻는 길로 돌아가지
    않는다(그 길이 고친 결함이다). 유임 좌석은 그대로 앉고, 왜 신규 좌석이 없는지 상태줄에 남는다."""
    r = _rescreen_once(monkeypatch, tmp_path, _B17_CARRY, ["disp-x", "xd-field-tri"], note=_B17_NOTE, axes=axes)
    assert r.queries == [], f"축 없이 추천을 물었다 — 한 덩이 질의로 돌아갔다: {r.queries}"
    assert [(s["key"], s["origin"]) for s in r.job["seats"]] == [(p["key"], "carry") for p in _B17_CARRY]
    assert f"이어하기 재심사 — 검색 축을 짓지 못해({why}) 신규 좌석 없이 유임 전원으로 간다" in _steps(r), _steps(r)


def test_재심사는_넓은_접두를_둘째_마디까지_앉은_도메인으로_본다(monkeypatch, tmp_path):
    """③ 첫 마디 접두로 거르면 sw(수백 명) 좌석 하나가 sw-field-* · sw-voc-* 를 다 막는다. 넓은 접두(sw · xd 와 지금 풀에서 접두
    하나에 많이 모인 것 — list_agent_domains)는 둘째 마디까지 견주고, 좁은 접두(disp)는 종전대로 첫 마디다. 신규 좌석끼리도
    단위가 겹치지 않는다."""
    carry = [{"key": k, "role": ""} for k in ("disp-a", "sw-lum-ctl", "big-core-a")]
    domains = [{"domain": "sw", "agent_count": 408}, {"domain": "big", "agent_count": 60},
               {"domain": "disp", "agent_count": 19}]
    recs = ["sw-lum-x", "disp-new", "sw-field-mon", "sw-field-rel", "big-edge-b", "rel-x"]
    r = _rescreen_once(monkeypatch, tmp_path, carry, recs, note=_B17_NOTE, axes="필드 열화 추적", domains=domains)
    assert _new_seats(r) == ["sw-field-mon", "big-edge-b"], (
        f"앉은 sw · big 좌석이 다른 마디의 좌석까지 막았거나, 같은 단위가 둘 앉았다: {_new_seats(r)}")


@pytest.mark.parametrize("modifiers", [[], ["anon1r"]], ids=["new-only", "anon1r"])
def test_1라운드_조회_턴도_발언_턴과_같이_이전_결론을_가린다(monkeypatch, modifiers):
    """자유 조회(기본 켬)는 1라운드 발언 앞에 좌석마다 조회 턴을 따로 돈다. 그 턴의 논의 발췌에 이전 결론이 실리면 신규 좌석은
    무엇을 찾을지부터 그 결론에 끌려가고, 조회 요약이 그대로 1라운드 발언에 들어간다 — 발언 프롬프트만 가린 F12 가 새는 길이다
    (Claude 길 hwax-deliberate.js 는 1라운드 조회와 발언이 한 턴이라 BASE_BLIND 하나로 가린다). anon1r 면 유임 좌석도 가린다."""
    import langgraph.prebuilt

    import app as agent_app

    ctx_by_seat, prompts = {}, {}

    async def _tools(*_a, **_k):
        return {"search_records": _Tool("search_records")}

    async def _llm(_obj, system, human, *_a):
        seat = re.match(r"당신은 '([^']+)' 전문가입니다\.", system)
        if seat:
            prompts.setdefault(seat.group(1), human)
        return json.dumps(_R1_CARRY, ensure_ascii=False)

    async def _gather(_agent, persona, _question, ctx, *_a, **_k):
        ctx_by_seat[persona["key"]] = ctx
        return persona["key"], [], "", ""

    monkeypatch.setattr(d, "_tools_by_name", _tools)
    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(agent_app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *_a, **_k: object())
    seats = [{**p, "origin": "carry"} for p in _CARRY] + [{"key": _NEW, "role": "", "origin": "new"}]
    opts = {"personas": seats, "continue_summary": _PREV, "human_note": _NOTE, "modifiers": modifiers,
            "rescreen": 0, "free_tools": 1, "voc": "off", "persona_knowledge": 0, "stop_after_round": 1,
            "save_report": 0}
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))

    async def go():
        return [delib_jobs._parse_sse(c) async for c in _REAL_RUN(stub, "힌지 크랙 원인", [], opts)]

    events = asyncio.run(go())
    assert not [x for ev, x in events if ev == "error"], events[-3:]
    assert set(ctx_by_seat) == {p["key"] for p in seats}, f"시험 전제 — 전원이 1라운드 조회 턴을 돌았다: {sorted(ctx_by_seat)}"
    blind = {p["key"] for p in seats if "anon1r" in modifiers or p["origin"] == "new"}
    for key, ctx in ctx_by_seat.items():
        said = prompts[key]
        if key in blind:
            assert _CONT_MARK not in ctx and _PREV not in ctx, f"{key} 의 1라운드 조회 턴에 이전 결론이 실렸다 — 발언만 가렸다"
            assert _NOTE in ctx, "가린 것은 이전 결론뿐이다 — 사람 의견은 조회 턴에도 간다"
            assert _CONT_MARK not in said and _PREV not in said, "시험 전제 — 발언 프롬프트는 이미 가린다"
        else:
            assert _CONT_MARK in ctx and _PREV in ctx, f"유임 {key} 의 조회 턴에서 이전 결론이 빠졌다"
            assert _CONT_MARK in said and _PREV in said

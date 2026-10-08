# MCP 심의 잡의 진행 조회가 '살아 있는지' 를 보이는지 — 마지막 이벤트 뒤 경과(idle_s)와 그 이벤트(last_step)
#
# 진행 조회는 단계·상태줄·걸린 시간만 줬다. 22석 패널이 공유 LLM 에 줄을 서면 같은 단계가 30분씩 이어지는데,
# 그것이 LLM 을 기다리는 것인지 멈춘 것인지 호출자는 알 길이 없었다 — 그래서 다시 시작했고, 같은 심의가 둘 돌았다.
# 시작 안내는 '보통 수 분~수십 분' 이라고 했다.
#
# **실제 잡 태스크를 돌려서** 본다 — 시계만 손으로 돌린다.
#
# 견줄 값(quiet_ok_s)은 처음에 'LLM 호출 한 번' 으로 쟀다. 좌석 발언 하나는 호출을 (1 + 파싱 재시도)번 잇고
# 그 사이에 이벤트가 없다 — 마지막 좌석이 파싱 재시도 중인 **멀쩡한** 심의가 그 값을 넘겼고(1,500초 × 3번 =
# 4,500초 > 3,608초), 안내문대로 읽은 호출자는 멈췄다고 보고 접거나 다시 시작한다. 맨 아래 시험이 실제 엔진으로
# 그 침묵을 만든다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_job_idle.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다 — 문이 열릴 때까지 '도는 중' 으로 남는 엔진과 빈 원장(eng 는 이름째 가져와 이 파일에도 건다).
from test_delib_job_queue import _end, _play, _start, _tick, eng  # noqa: E402, F401
from test_delib_silent_drops import _Tool, _pin_context  # noqa: E402, F401


@pytest.fixture(autouse=True)
def _engine_defaults(monkeypatch):
    """엔진 기본 손잡이를 코드 기본값에 못박는다 — 조용해도 되는 시간이 파싱 재시도 수에서 나오므로, 박스의
    환경(DELIB_PARSE_RETRIES·DELIB_REBUT_QUOTE·DELIB_CHAIR_BESTOF)이 아래 숫자를 흔들지 않게 한다."""
    monkeypatch.setattr(d, "_PARSE_RETRIES", 1)
    monkeypatch.setattr(d, "_REBUT_QUOTE", 1)       # 인용 계약이 켜지면 파싱 재시도 하한이 2 다
    monkeypatch.setattr(d, "_CHAIR_BESTOF", 1)


class _Clock:
    """손으로 돌리는 시계 — 벽시계에 기대면 바쁜 박스에서 흔들린다."""

    def __init__(self, monkeypatch):
        self.t = 1_000_000.0
        monkeypatch.setattr(delib_jobs, "_now", lambda: self.t)


def _llm(read=1800.0, retries=1):
    return SimpleNamespace(request_timeout=httpx.Timeout(read if read else None, connect=10.0), max_retries=retries)


def test_도는_잡은_마지막_이벤트_뒤_경과와_그_이벤트를_보인다(eng, monkeypatch):
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(delib_llm=_llm())))

    async def scenario():
        job = _start("힌지 크랙")
        await _tick()                                   # 엔진이 '힌지 크랙 시작' 을 내고 문 앞에서 기다린다
        first = await m.deliberate_status(job["id"])
        clock.t += 1500                                 # LLM 호출 한 번을 25분째 기다리는 중
        waiting = await m.deliberate_status(job["id"])
        await _end(eng, job)
        return first, waiting, await m.deliberate_status(job["id"])

    first, waiting, done = _play(scenario)
    assert first["status"] == "running" and first["idle_s"] == 0.0 and first["last_step"] == "힌지 크랙 시작", first
    assert waiting["idle_s"] == 1500.0 and waiting["elapsed_s"] == 1500.0, waiting
    # 무엇과 견줄지 같이 준다 — 호출 한 번의 최악(한도 1,800초 × 2회 시도 + 재시도 대기 = 3,608초)에 좌석 발언
    # 하나가 잇는 호출 수(1 + 파싱 재시도 2)를 곱한다. 그 안이면 기다리는 중이다.
    assert waiting["quiet_ok_s"] == 10824.0 and waiting["idle_s"] < waiting["quiet_ok_s"]
    assert done["status"] == "done" and "idle_s" not in done and "quiet_ok_s" not in done, done


def test_좌석_발언과_근거도_마지막_이벤트로_적힌다(monkeypatch):
    """상태줄(step)은 라운드가 도는 동안 그대로다 — 그것만 보면 좌석이 하나씩 발언하는 것과 멈춘 것이 같아 보인다."""
    _Clock(monkeypatch)
    job = {"id": "t", "status": "running", "question": "q", "started_at": delib_jobs._now(),
           "updated_at": delib_jobs._now()}

    def feed(event, data):
        delib_jobs._apply(job, *delib_jobs._parse_sse(d._sse(event, data)))
        return delib_jobs.summary(job)

    assert feed("status", {"step": "2라운드 — 상호 반박·수치 심화"})["last_step"] == "2라운드 — 상호 반박·수치 심화"
    out = feed("delib", {"kind": "turn", "round": 2, "display_round": 5, "persona": "mech-a", "say": "발언"})
    assert out["last_step"] == "좌석 발언 — mech-a (5R)" and out["step"] == "2라운드 — 상호 반박·수치 심화", out
    assert feed("delib", {"kind": "evidence", "source": "rel-b · get_material", "text": "값",
                          "included": True})["last_step"] == "근거 — rel-b · get_material"
    assert feed("delib", {"kind": "stage", "stage": "decide"})["last_step"] == "단계 — decide"
    assert feed("warning", {"code": "seat_lost", "message": "3라운드 좌석 유실"})["last_step"].startswith("경고 — 3라운드")
    # 진행이 아닌 이벤트는 마지막 이벤트를 덮지 않는다.
    for event, data in (("token", {"delta": "가"}), ("ping", {"idle_s": 15, "ts": 1}), ("done", {})):
        assert feed(event, data)["last_step"].startswith("경고 — 3라운드"), event


def test_조용해도_되는_시간은_그_잡에_걸린_한도로_잰다(eng, monkeypatch):
    """요청 timeout_s 로 돈 잡은 그 값이다 — 서버 기본값으로 재면 7,200초를 청한 잡이 멀쩡히 기다리는 중에
    '넘었다' 로 읽힌다."""
    _Clock(monkeypatch)
    asked = iter(range(100))

    async def scenario(state, **start_kw):
        monkeypatch.setattr(m, "_APP", SimpleNamespace(state=state))
        job = _start(f"화두 {next(asked)}", **start_kw)      # 화두마다 문이 하나다 — 같은 화두를 또 쓰면 열린 문으로 지나간다
        await _tick()
        out = await m.deliberate_status(job["id"])
        await _end(eng, job)
        return out["quiet_ok_s"]

    assert _play(lambda: scenario(SimpleNamespace(delib_llm=_llm(1800.0, 1)))) == 10824.0     # 3 × 3,608
    assert _play(lambda: scenario(SimpleNamespace(delib_llm=_llm(600.0, 2)))) == 5424.0       # 3 × 1,808
    assert _play(lambda: scenario(SimpleNamespace(delib_llm=_llm(1800.0, 1)),
                                  delib_opts={"timeout_s": 7200})) == 43224.0                 # 3 × 14,408
    assert _play(lambda: scenario(SimpleNamespace(delib_llm=_llm(0, 1)))) is None, "한도가 꺼진 박스에 수를 지어냈다"
    assert _play(lambda: scenario(SimpleNamespace(delib_llm=None))) is None


@pytest.mark.parametrize("delib_opts,want", [
    ({"parse_retries": 0, "rebut_quote": 0}, 3608.0),       # 파싱 재시도를 끈 잡은 호출 한 번이다
    ({"parse_retries": 5}, 21648.0),                        # 5번 청한 잡은 호출 여섯이 이어진다
    ({"parse_retries": 0}, 10824.0),                        # 인용 계약(기본 켜짐)이 재시도 하한을 2 로 올린다
    ({"parse_retries": 0, "rebut_quote": 0, "search_sources": ["web"]}, 10824.0),   # 웹 리서치가 인용 계약을 켠다
    ({"parse_retries": 0, "rebut_quote": 0, "chair_bestof": 3}, 7216.0),    # 의장 후보 묶음 뒤에 심판 호출이 붙는다
])
def test_조용해도_되는_시간은_그_잡의_파싱_재시도_수를_곱한다(eng, monkeypatch, delib_opts, want):
    """좌석 발언 하나가 잇는 호출 수는 잡마다 다르다 — 서버 기본값으로 세면 재시도를 늘려 청한 잡이 멀쩡히
    도는 중에 '넘었다' 로 읽힌다. 수는 **엔진이 읽은 값**이다(인용 계약이 켜지면 재시도 하한이 2 다)."""
    _Clock(monkeypatch)
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(delib_llm=_llm(1800.0, 1))))

    async def scenario():
        job = _start("화두", delib_opts=delib_opts)
        await _tick()
        out = await m.deliberate_status(job["id"])
        await _end(eng, job)
        return out["quiet_ok_s"]

    assert _play(scenario) == want


def test_줄_선_잡과_끝난_잡에는_경과가_없다(eng, monkeypatch):
    from test_delib_job_queue import _caps

    _Clock(monkeypatch)
    _caps(monkeypatch, 1)

    async def scenario():
        a, b = _start("가"), _start("나")
        await _tick()
        return await m.deliberate_status(a["id"]), await m.deliberate_status(b["id"])

    running, queued = _play(scenario)
    assert "idle_s" in running and queued["status"] == "queued"
    assert "idle_s" not in queued and "last_step" not in queued and "quiet_ok_s" not in queued, queued


def test_안내가_수_시간과_idle_s_를_말한다(eng, monkeypatch):
    async def scenario():
        out = await m.deliberate_start("화두")
        await _tick()
        return out["note"], {t.name: t.description for t in await m.mcp.list_tools()}

    note, listed = _play(scenario)
    assert "수 시간" in note and "idle_s" in note and "quiet_ok_s" in note and "다시 시작하지 마라" in note, note
    assert "수십 분" not in note, "대형 패널은 수 시간이다 — 종전 안내가 남아 있다"
    # quiet_ok_s 는 상한이 아니다(자유 조회는 좌석마다 호출을 더 길게 잇는다) — '안이면 기다리는 중' 만 적으면
    # 호출자는 그 역('넘으면 멈췄다')으로 읽고 멀쩡한 심의를 접는다. 넘은 뒤에 무엇을 할지까지 적는다.
    assert "넘었다고 멈춘 것은 아니다" in note, note
    for name in ("deliberate_start", "deliberate_status"):
        for want in ("idle_s", "last_step", "quiet_ok_s", "수 시간", "DELIB_TIMEOUT_S", "파싱 재시도",
                     "넘었다고 멈춘 것은 아니다", "deliberate_cancel"):
            assert want in listed[name], (name, want)


# ── 실제 엔진으로 — 마지막 좌석이 파싱 재시도 중인 멀쩡한 심의 ─────────────────────────────
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}, {"key": "mat-c", "role": "재료"}]
_GOOD = json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
                    "position_short": "요약", "final_position": "최종", "non_negotiable": "",
                    "vote": "진행", "stance": "동의"}, ensure_ascii=False)


def test_파싱_재시도_중인_멀쩡한_심의는_조용해도_되는_시간_안에_있다(monkeypatch, tmp_path):
    """실제 run_deliberation 을 실제 잡으로 돌린다 — 가짜는 LLM 본문과 시계뿐이다.

    마지막 좌석(mat-c)의 1라운드 발언이 JSON 이 아닌 글을 두 번 내고 세 번째에 제대로 낸다. 호출마다 1,500초가
    걸리는데 시도 1회 한도(1,800초) 안이다 — 시간 초과도 SDK 재시도도 없는 멀쩡한 심의다. 그 사이 엔진은
    이벤트를 내지 않으므로 idle_s 는 4,500초까지 간다."""
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    for name in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, name, {})
    monkeypatch.setattr(delib_jobs, "_CLOSING", False)

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    llm = SimpleNamespace(**vars(_llm()), max_tokens=None)
    stub = SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=llm))
    monkeypatch.setattr(m, "_APP", stub)
    seen = SimpleNamespace(job=None, calls=0, polls=[])

    async def _text(_llm_obj, system, _human):
        if "전문가입니다" not in system:
            return "결정문 본문"                          # 의장·요약
        if "'mat-c' 전문가" not in system or seen.calls >= 3:
            return _GOOD
        for _ in range(200):                            # 다른 두 좌석의 발언이 원장에 닿은 뒤부터 조용하다
            if len(seen.job.get("turns") or []) >= 2:
                break
            await asyncio.sleep(0)
        seen.calls += 1
        clock.t += 1500.0                               # 이 호출 한 번이 1,500초 걸렸다(한도 안 — 성공)
        seen.polls.append(await m.deliberate_status(seen.job["id"]))    # 호출이 돌아오는 순간에 물었다
        return _GOOD if seen.calls == 3 else "죄송합니다, 다시 정리하겠습니다."     # JSON 이 아니면 파싱 재시도

    monkeypatch.setattr(d, "_llm_text", _text)

    async def scenario():
        seen.job = delib_jobs.start(stub, "default", "힌지 크랙 원인", delib_opts={
            "personas": _SEATS, "free_tools": 0, "voc": "off", "rescreen": 0, "rounds": 2,
            "save_report": 0, "persona_knowledge": 0})
        await asyncio.wait([delib_jobs._TASKS[seen.job["id"]]], timeout=20)
        await _tick()
        return seen.job

    job = _play(scenario)
    assert seen.calls == 3, "파싱 재시도가 돌지 않았다 — 하네스가 낡았다"
    assert [p["idle_s"] for p in seen.polls] == [1500.0, 3000.0, 4500.0], seen.polls
    assert len({p["last_step"] for p in seen.polls}) == 1, "그 사이에 이벤트가 났다 — 조용한 구간이 아니다"
    for p in seen.polls:
        assert p["status"] == "running" and p["idle_s"] <= p["quiet_ok_s"], (
            f"멀쩡히 도는 심의가 조용해도 되는 시간을 넘겼다 — idle_s {p['idle_s']} > quiet_ok_s {p['quiet_ok_s']}")
    assert job["status"] == "done" and not job["error"], job

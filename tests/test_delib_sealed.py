# 봉인 실행(risk-review-sealed) — 호출자가 준 자료 밖의 정보가 들어올 길이 전부 닫히고, 다시 열 수 없다
#
# 소급 검증용이다. '사람이 찾기 전에, 그때 있던 자료만으로 심사가 찾았겠는가' 를 보려면 엔진이
# 심의 도중 바깥에서 가져오는 것이 하나도 없어야 한다. 손잡이를 하나씩 끄게 두었더니 실사용에서
# VOC 자동 환기가 새어 들어갔다(S26U 피드백 1-6, 2026-10-07).
#
# **실제로 돌려서** 본다. 닫는 목록을 표와 대조하는 것으로는 부족하다 — 표에 없는 조회 자리가
# 엔진에 새로 생기면 표는 그대로 옳아 보인다. 그래서 어떤 이름을 물어도 '있다' 고 답하고 부른 것을
# 전부 적는 도구 사전(_Tripwire)을 깔고 심의를 끝까지 돌려, 부른 도구가 허용 목록 안인지를 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_sealed.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context, _Tool  # noqa: E402, F401

SEALED = "risk-review-sealed"
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]
_SEAT_JSON = json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
                         "position_short": "요약", "final_position": "최종 입장", "non_negotiable": "",
                         "vote": "진행", "stance": "동의", "relevant": True, "reason": "연관"},
                        ensure_ascii=False)
# 호출자가 봉인을 풀어 보려고 보내는 것 전부 — 유입 경로마다 하나씩이다.
_REOPEN = {"voc": "always", "evidence_prepass": 1, "free_tools": 1, "persona_knowledge": 1,
           "rescreen": 1, "sealed": 0}
# 봉인해도 부르는 것 — 좌석 역할 복원(누가 앉는가)과 결정문 저장(나가는 쪽)뿐이다.
_ALLOWED = {"get_agent_session", "create_report_draft"}


class _Tripwire(dict):
    """어떤 이름을 물어도 '있다' 고 답하고, 부른 도구 이름을 순서대로 적는 도구 사전."""

    _ANSWERS = {"recommend_agents": '[{"agent_type": "mech-a"}, {"agent_type": "rel-b"}]',
                "alert_check": '{"summary": "경보 요약"}',
                "create_report_draft": '{"report_id": 7}'}

    def __init__(self):
        super().__init__()
        self.called, self.args = [], {}          # 부른 이름(순서대로) · 이름별 마지막 인자

    def __bool__(self):                  # 비어 보이면 엔진이 '게이트웨이 불통' 으로 선다
        return True

    def __contains__(self, _name):
        return True

    def get(self, name, default=None):
        log, last = self.called, self.args

        class _Rec(_Tool):
            async def ainvoke(self, args):
                log.append(self.name)
                last[self.name] = args
                return await super().ainvoke(args)

        return _Rec(name, self._ANSWERS.get(name, '{"hits": []}'))

    def items(self):
        """자유 조회 준비가 훑는 목록. 뒤엣것은 리스크 심사 의장일 때만 열리는 도구다(_RISK_KEEP_TOOLS —
        읽기 접두사에 안 걸려 그 조건이 유일한 통로다)."""
        return [(n, self.get(n)) for n in ("list_materials", "pcb_warpage_surrogate")]


def _run(monkeypatch, tmp_path, *, job=SEALED, personas=_SEATS, advanced=None, **start_kw):
    """**실제 MCP 도구 함수**(deliberate_start)로 열어 **실제 엔진**을 끝까지 돌린다.

    반환: SimpleNamespace(job=잡 원장, tools=부른 도구 이름들, chair=의장이 받은 프롬프트,
                          free=자유 조회를 돈 좌석들, bound=자유 조회에 묶인 도구 이름들,
                          steps=상태줄 전부)
    잡 원장은 tmp_path 로 돌린다 — 실 원장(/data 쪽)에 시험 잡을 쓰지 않는다."""
    import langgraph.prebuilt

    wire, seen, free, bound, steps = _Tripwire(), [], [], [], []
    real_apply = delib_jobs._apply

    def _apply(job, event, data):        # 원장의 steps 는 최근 30줄만 남는다 — 전부 따로 적는다
        if event == "status" and data.get("step"):
            steps.append(str(data["step"]))
        return real_apply(job, event, data)

    async def _fake_tools(*_a, **_k):
        return wire

    async def _fake_llm(_llm, system, human):
        seen.append((system, human))
        return "결정문 본문" if "엔지니어링 톤" in system else _SEAT_JSON

    async def _fake_gather(_agent, persona, *_a, **_k):
        free.append(persona["key"])
        return persona["key"], [], "", ""

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", _fake_llm)
    monkeypatch.setattr(d, "_free_gather_one", _fake_gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})            # 게이트웨이 /tools-map 을 타지 않게
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent",
                        lambda _llm, tools, **_k: bound.extend(t.name for t in tools) or object())
    monkeypatch.setattr(delib_jobs, "_apply", _apply)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)))

    async def go():
        out = await m.deliberate_start("힌지 크랙 원인", job=job, rounds=2, personas=personas,
                                       advanced=advanced, **start_kw)
        await delib_jobs._TASKS[out["job_id"]]
        return delib_jobs._JOBS[out["job_id"]]

    job_rec = asyncio.run(go())
    assert job_rec["status"] == "done", f"심의가 끝까지 못 갔다 — {job_rec.get('error')}"
    chair = next((h for s, h in seen if "엔지니어링 톤" in s), "")
    return SimpleNamespace(job=job_rec, tools=wire.called, args=wire.args, chair=chair, free=free,
                           bound=bound, steps=steps)


# ── Job 표 ───────────────────────────────────────────────────────────────────
def test_봉인_Job_이_표에_있다():
    spec = delib_jobs.JOBS[SEALED]
    assert (spec["engine"], spec["chair"], spec["group"], spec["label"]) == (
        "general", "risk-review", "판단", "리스크 심사(봉인)")
    assert (spec.get("opts") or {}).get("sealed") == 1


def test_별칭으로도_찾는다():
    assert delib_jobs.resolve_job("sealed") == SEALED
    assert delib_jobs.resolve_job(" Risk-Sealed ") == SEALED


# ── 닫기 — 손잡이 ────────────────────────────────────────────────────────────
def _closed(o):
    return {"voc": o.voc, "evidence_prepass": o.evidence_prepass, "tools": o.delib_tools,
            "free_tools": o.free_tools, "persona_knowledge": o.persona_knowledge,
            "search_sources": o.search_sources, "rescreen": o.rescreen}


_ALL_CLOSED = {"voc": "off", "evidence_prepass": 0, "tools": [], "free_tools": 0,
               "persona_knowledge": 0, "search_sources": [], "rescreen": 0}


def test_봉인은_유입_손잡이를_전부_닫는다(monkeypatch):
    for env in ("DELIB_FREE_TOOLS", "DELIB_PERSONA_KNOWLEDGE"):       # 환경 기본값이 켜짐이어도
        monkeypatch.setenv(env, "1")
    o = d._resolve_opts({"sealed": 1})
    assert o.sealed == 1 and _closed(o) == _ALL_CLOSED
    assert o.sealed_reopen == [], "호출자가 아무것도 안 열었는데 열려던 것이 있다고 적었다"


def test_닫는_목록과_시험이_아는_목록이_같다():
    """엔진에 닫는 손잡이가 늘면 이 시험의 대조표(_ALL_CLOSED)도 같이 늘어야 한다."""
    assert {k: c for k, (c, _l) in d._SEALED_CLOSE.items()} == _ALL_CLOSED


def test_닫힌_값을_요청끼리_나눠_쓰지_않는다():
    """닫는 값의 빈 목록이 표의 **그 객체**면, 잡 기록 하나의 opts 를 고치는 순간 표가 바뀌고
    다음 봉인 심의부터 그 길이 열린 채 돈다."""
    first, _ = d._seal({"sealed": 1})
    first["tools"].append("report_query")
    first["search_sources"].append("web")
    assert _closed(d._resolve_opts({"sealed": 1})) == _ALL_CLOSED
    assert {k: c for k, (c, _l) in d._SEALED_CLOSE.items()} == _ALL_CLOSED


@pytest.mark.parametrize("key,value", [
    ("voc", "always"), ("voc", "auto"), ("evidence_prepass", 1), ("tools", ["report_query"]),
    ("free_tools", 1), ("persona_knowledge", 1), ("search_sources", ["web", "scholar"]),
    ("rescreen", 1)])
def test_호출자가_같이_보낸_값으로_다시_열_수_없다(key, value):
    o = d._resolve_opts({key: value, "sealed": 1})
    assert _closed(o) == _ALL_CLOSED, f"{key}={value!r} 로 봉인이 풀렸다"
    assert len(o.sealed_reopen) == 1 and o.sealed_reopen[0].startswith(f"{key}="), o.sealed_reopen
    assert (o.rebut_quote, o.chair_cite) == (d._REBUT_QUOTE, d._CHAIR_CITE), (
        "웹 리서치가 닫혔는데 그것이 강제하는 인용 계약은 걸렸다")


def test_닫힌_값을_그대로_보낸_것은_열려던_것이_아니다():
    o = d._resolve_opts({"sealed": 1, "voc": "OFF", "free_tools": "0", "tools": [], "rescreen": 0,
                         "search_sources": None})
    assert o.sealed_reopen == []


@pytest.mark.parametrize("value", [1, "1", True, 2, "yes", "false", [1], {"on": 1}])
def test_봉인_값은_닫힌_쪽으로_읽는다(value):
    """해석 못 하는 값을 '봉인 아님' 으로 읽으면, 잡 기록에는 sealed 가 남고 심의는 열린 채 돈다."""
    o = d._resolve_opts({"sealed": value, "voc": "always"})
    assert o.sealed == 1 and o.voc == "off"


@pytest.mark.parametrize("value", [0, "0", False, None, ""])
def test_봉인하지_않으면_호출자_손잡이는_그대로다(value):
    o = d._resolve_opts({"sealed": value, "voc": "always", "tools": ["report_query"], "free_tools": 1})
    assert o.sealed == 0 and (o.voc, o.delib_tools, o.free_tools) == ("always", ["report_query"], 1)
    assert d._DEFAULT_OPTS.sealed == 0 and d._resolve_opts({}).sealed == 0


# ── 닫기 — 실제로 돌려서 ─────────────────────────────────────────────────────────
def test_봉인하지_않은_리스크_심사는_바깥_자료를_가져온다(monkeypatch, tmp_path):
    """시험 전제 — 같은 요청을 봉인 없이 돌리면 경로마다 실제로 도구를 부른다. 이게 안 서면
    아래 '봉인하면 아무것도 안 부른다' 는 하네스가 눈이 멀어서 통과한 것일 수 있다."""
    r = _run(monkeypatch, tmp_path, job="risk-review", tools=["report_query"], advanced=dict(_REOPEN))
    for name, path in (("alert_check", "VOC 환기"), ("query_voc", "VOC 환기"),
                       ("hybrid_search", "사전 검색"), ("report_query", "지정 도구"),
                       ("agent_search", "지식카드")):
        assert name in r.tools, f"{path} 경로가 하네스에서 안 돈다 — {name} 을 부르지 않았다"
    assert "mech-a" in r.free and "list_materials" in r.bound, "자유 조회 경로가 하네스에서 안 돈다"
    assert "pcb_warpage_surrogate" in r.bound, "리스크 심사가 더 여는 조회 도구 경로가 하네스에서 안 돈다"
    assert delib_jobs.summary(r.job)["sealed"] is False
    assert "봉인" not in (r.job["decision"] or "") and "봉인" not in r.chair
    assert not any(s.startswith("봉인") for s in r.steps)


def test_봉인하면_호출자가_전부_열어도_바깥_자료를_가져오지_않는다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, tools=["report_query"], search_sources=["web"],
             advanced=dict(_REOPEN))
    assert set(r.tools) <= _ALLOWED, f"봉인했는데 부른 도구가 있다 — {sorted(set(r.tools) - _ALLOWED)}"
    assert r.free == [] and r.bound == [], f"봉인했는데 자유 조회를 준비했다 — {r.free} {r.bound}"
    assert "get_agent_session" in r.tools, "시험 전제 — 좌석 역할 복원은 돈다"


def test_좌석을_안_주면_발굴은_돌지만_그것뿐이다(monkeypatch, tmp_path):
    """좌석 발굴은 닫지 않는다(닫으면 앉을 사람이 없다). 부르는 것은 추천과 역할 복원뿐이다."""
    r = _run(monkeypatch, tmp_path, personas=None, advanced=dict(_REOPEN))
    assert set(r.tools) <= _ALLOWED | {"recommend_agents"}, sorted(set(r.tools))
    assert "recommend_agents" in r.tools and r.free == []
    assert "mech-a" in [s["key"] for s in r.job["seats"]]


def test_이어하기로_들어와도_재심사가_좌석을_더_얹지_않는다(monkeypatch, tmp_path):
    """이전 요약·사람 의견이 있으면 재심사가 recommend_agents 로 좌석을 더 부른다 — 봉인은 닫는다."""
    r = _run(monkeypatch, tmp_path, human_note="두께를 다시 보라",
             advanced={**_REOPEN, "continue_summary": "이전 결론"})
    assert "recommend_agents" not in r.tools, r.tools
    assert [s["key"] for s in r.job["seats"]] == ["mech-a", "rel-b", "delib-baseline-defender"]


# ── 남기기 — 잡 기록·화면·결정문 ─────────────────────────────────────────────────
def test_잡_기록이_봉인과_닫힌_값을_보여_준다(monkeypatch, tmp_path):
    """applied_opts 에 호출자가 보낸 값(voc=always)이 그대로 남으면, 걸린 것과 적힌 것이 다르다."""
    r = _run(monkeypatch, tmp_path, tools=["report_query"], search_sources=["web"],
             advanced=dict(_REOPEN))
    echo = r.job["opts"]
    assert echo["sealed"] == 1
    assert {k: echo[k] for k in _ALL_CLOSED} == _ALL_CLOSED, echo
    assert delib_jobs.summary(r.job)["sealed"] is True
    assert asyncio.run(m.deliberate_result(r.job["id"]))["applied_opts"]["voc"] == "off"


def test_시작할_때_닫은_경로를_상태줄로_밝힌다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path)
    assert r.steps[0].startswith("봉인 실행"), f"봉인 상태줄이 맨 앞에 없다 — {r.steps[:3]}"
    line = r.steps[0]
    for _closed_value, label in d._SEALED_CLOSE.values():
        assert label in line, f"닫은 경로 '{label}' 이 상태줄에 없다"
    assert d._SEALED_OPEN in line, "닫지 않은 것을 감추면 전부 닫혔다고 읽힌다"


def test_호출자가_열려던_손잡이는_MCP_호출자에게_보인다(monkeypatch, tmp_path):
    """말없이 무시하면 호출자는 자기가 고른 도구·VOC 가 돈 줄 안다."""
    r = _run(monkeypatch, tmp_path, tools=["report_query"], search_sources=["web"],
             advanced=dict(_REOPEN))
    for view in (asyncio.run(m.deliberate_status(r.job["id"])),
                 asyncio.run(m.deliberate_result(r.job["id"]))):
        card = next((x for x in view["evidence_omitted"] if x.get("source") == "봉인이 닫은 요청"), None)
        assert card, view["evidence_omitted"]
        for want in ("voc=always", "report_query", "free_tools=1", "persona_knowledge=1",
                     "evidence_prepass=1", "rescreen=1", "search_sources="):
            assert want in card["text"], (want, card["text"])


def test_열려던_것이_없으면_카드도_없다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path)
    assert [x for x in r.job["evidence_omitted"] if x.get("source") == "봉인이 닫은 요청"] == []


def test_의장_프롬프트와_결정문에_봉인이_찍힌다(monkeypatch, tmp_path):
    """의장에게 적으라고만 하면 빠뜨린 결정문이 봉인 없이 돈 것과 똑같이 생긴다 — 코드가 찍는다."""
    r = _run(monkeypatch, tmp_path)
    assert "[봉인 실행" in r.chair
    head = (r.job["decision"] or "").split("\n\n")[0]
    assert head.startswith("■ 봉인 실행"), head[:80]
    for text in (r.chair, head):
        for _closed_value, label in d._SEALED_CLOSE.values():
            assert label in text, f"닫은 경로 '{label}' 이 빠졌다"
        assert d._SEALED_OPEN in text
    assert r.job["decision"].count("■ 봉인 실행") == 1


def test_저장되는_보고서에도_봉인이_찍혀_있다(monkeypatch, tmp_path):
    """결정문은 RA 보고서로 남는다 — 저장한 **뒤에** 찍으면 화면에는 있고 남는 기록에는 없다."""
    r = _run(monkeypatch, tmp_path)
    saved = r.args["create_report_draft"]["blocks"]["recommendation"]
    assert saved[0].startswith("■ 봉인 실행"), saved[0][:80]


def test_다른_단발_심의도_봉인_손잡이로_닫힌다(monkeypatch, tmp_path):
    """봉인 표식은 손잡이를 따라간다 — Job 이름이 아니라. 표식만 붙고 길이 열려 있으면 거짓 기록이다."""
    r = _run(monkeypatch, tmp_path, job="diagnosis", tools=["report_query"],
             advanced={**_REOPEN, "sealed": 1})
    assert set(r.tools) <= _ALLOWED and r.free == []
    assert delib_jobs.summary(r.job)["sealed"] is True and r.job["chair_template"] == "diagnosis"
    assert r.job["decision"].startswith("■ 봉인 실행")


# ── 닫지 못하는 경로는 서지 않는다 ───────────────────────────────────────────────
def _first_events(gen_fn, monkeypatch):
    loaded = []

    async def _fake_tools(*_a, **_k):
        loaded.append(1)
        return _Tripwire()

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))

    async def go():
        return [delib_jobs._parse_sse(c) async for c in gen_fn(stub, "낙하 충격", [], {"sealed": 1})]

    return asyncio.run(go()), loaded


@pytest.mark.parametrize("entry", ["run_sim_deliberation", "run_test_plan"])
def test_다단_심의는_봉인을_거절한다(entry, monkeypatch):
    """해석 설계·시험 설계는 사내 자산 현황을 **실제로 조회해** 깐다. 닫지 못한 채 '봉인' 이라고
    적힌 결정문을 내느니 서지 않는다."""
    events, loaded = _first_events(getattr(d, entry), monkeypatch)
    assert [ev for ev, _ in events] == ["error", "done"], events
    assert events[0][1]["code"] == "sealed_unsupported" and "봉인" in events[0][1]["message"]
    assert loaded == [], "거절하기 전에 도구를 불러왔다"


@pytest.mark.parametrize("job", ["sim-plan", "test-plan", "build-plan"])
def test_다단_Job_에_봉인을_청하면_시작하지_않는다(job, monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})

    async def go():
        return delib_jobs.start(object(), job, "낙하 충격", delib_opts={"sealed": 1})

    with pytest.raises(ValueError, match="봉인"):
        asyncio.run(go())
    assert delib_jobs._JOBS == {} and list(tmp_path.iterdir()) == []


# ── 이어하기 ─────────────────────────────────────────────────────────────────
def _prev(monkeypatch, tmp_path, sealed):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    prev = {"id": "prev-1", "job": SEALED if sealed else "risk-review", "status": "done",
            "question": "힌지 크랙 원인", "decision": "이전 결정문", "seats": list(_SEATS),
            "opts": {"sealed": 1} if sealed else {"voc": "auto"}, "started_at": 0.0}
    monkeypatch.setattr(delib_jobs, "_JOBS", {prev["id"]: prev})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    return prev


def test_봉인_없이_돈_심의를_봉인으로_이어받지_않는다(monkeypatch, tmp_path):
    """이전 결정문이 요약으로 실린다 — 봉인 없이 돈 회차의 결정문에는 그때 조회한 VOC·지식카드가
    녹아 있다. 그걸 싣고 '봉인' 이라고 적으면 거짓이다."""
    _prev(monkeypatch, tmp_path, sealed=False)
    with pytest.raises(ValueError, match="봉인"):
        asyncio.run(m.deliberate_continue("prev-1", "두께를 다시 보라", job=SEALED))
    assert list(delib_jobs._JOBS) == ["prev-1"], "거절했는데 잡이 생겼다"


def test_봉인으로_돈_심의는_봉인으로_이어진다(monkeypatch, tmp_path):
    _prev(monkeypatch, tmp_path, sealed=True)
    got = {}

    async def _fake_entry(_app, q, _groups, opts, *_a):
        got.update(opts or {})
        yield d._sse("done", {})

    monkeypatch.setattr(d, "run_deliberation", _fake_entry)
    monkeypatch.setattr(m, "_APP", object())

    async def go():
        out = await m.deliberate_continue("prev-1", "두께를 다시 보라")
        await delib_jobs._TASKS[out["job_id"]]
        return out

    out = asyncio.run(go())
    assert out["job"] == SEALED and got["sealed"] == 1 and got["continue_summary"] == "이전 결정문"
    assert _closed(d._resolve_opts(got)) == _ALL_CLOSED


# ── 이어하기 — 이전 회차에 걸린 봉인과 유입 손잡이를 이어받는다 ─────────────────────────────
# 이어하기는 이전 회차의 손잡이를 하나도 이어받지 않았다. 봉인을 **손잡이로**(advanced.sealed) 건 심의를
# 이어가면 Job 이름에 봉인이 없어 열린 채 돌았다 — VOC·지식카드·자유 조회를 다 하고, 그 결과를 첫 장에
# '봉인 실행' 이 찍힌 보고서에 페이지로 이어 붙였다. 호출자에게는 막을 인자도 알림도 없었다.
# voc=off · free_tools=0 · persona_knowledge=0 · save_report=False 도 같이 풀렸다.
_INFLOW = {"alert_check", "daily_briefing", "query_voc", "agent_search", "hybrid_search", "recommend_agents"}


def _continue(r, note="두께를 다시 보라", **kw):
    """_run 이 끝낸 잡을 **실제 MCP 도구 함수**(deliberate_continue)로 이어 실제 엔진을 끝까지 돌린다.

    반환: SimpleNamespace(out=도구 응답, job=이어하기 잡 원장, tools·free=이어하기 회차에서만 부른 것)."""
    n_tools, n_free = len(r.tools), len(r.free)

    async def go():
        out = await m.deliberate_continue(r.job["id"], note, **kw)
        await delib_jobs._TASKS[out["job_id"]]
        return out, delib_jobs._JOBS[out["job_id"]]

    out, job2 = asyncio.run(go())
    assert job2["status"] == "done", f"이어하기가 끝까지 못 갔다 — {job2.get('error')}"
    return SimpleNamespace(out=out, job=job2, tools=r.tools[n_tools:], free=r.free[n_free:])


@pytest.mark.parametrize("start,cont", [
    ({"job": "diagnosis", "advanced": {"sealed": 1}}, {}),              # 손잡이로 봉인 — Job 이름에는 봉인이 없다
    ({"job": SEALED}, {"job": "risk-review"}),                           # 봉인 Job 을 봉인 없는 Job 으로 바꿔 잇기
    ({"job": SEALED}, {"job": "default"}),
    ({"job": SEALED}, {"advanced": {"sealed": 0, "voc": "always"}}),     # 이어가면서 풀어 보려 해도
    # 아래 둘은 Job 표가 봉인을 다시 걸어 주지 않는 길이다 — 이어하기가 제 손으로 다시 걸어야 한다.
    ({"job": "diagnosis", "advanced": {"sealed": 1}}, {"advanced": {"sealed": 0, "voc": "always"}}),
    ({"job": SEALED}, {"job": "risk-review", "advanced": {"sealed": "0"}}),
])
def test_봉인으로_돈_심의는_어떻게_이어도_봉인이다(monkeypatch, tmp_path, start, cont):
    r = _run(monkeypatch, tmp_path, **start)
    assert delib_jobs.summary(r.job)["sealed"] is True, "시험 전제 — 첫 회차가 봉인으로 돌았다"
    c = _continue(r, **cont)
    assert c.out["sealed"] is True and c.job["opts"]["sealed"] == 1, c.job["opts"]
    assert set(c.tools) <= _ALLOWED | {"get_report", "update_report_draft"}, (
        f"봉인을 이어받지 않고 바깥 자료를 가져왔다 — {sorted(set(c.tools) & _INFLOW)}")
    assert c.free == [], f"봉인을 이어받지 않고 자유 조회를 돌았다 — {c.free}"
    assert (c.job["decision"] or "").startswith("■ 봉인 실행"), (c.job["decision"] or "")[:60]
    assert c.out["appending_to_report"] == 7 and "update_report_draft" in c.tools, "봉인 보고서에 봉인 회차를 잇는다"


def test_이어가면서_봉인을_풀려던_손잡이는_닫히고_그렇다고_남는다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, job="diagnosis", advanced={"sealed": 1})
    c = _continue(r, advanced={"sealed": 0, "voc": "always", "free_tools": 1})
    card = next(x for x in c.job["evidence_omitted"] if x.get("source") == "봉인이 닫은 요청")
    assert "voc=always" in card["text"] and "free_tools=1" in card["text"], card


def test_봉인으로_돈_심의는_다단_Job_으로_이어받지_않는다(monkeypatch, tmp_path):
    """다단 심의는 사내 자산 현황을 조회해 깐다 — 봉인이 서지 않으니 봉인 보고서에 이어 붙이지 않는다."""
    r = _run(monkeypatch, tmp_path)
    before = set(delib_jobs._JOBS)
    for job in ("sim-plan", "test-plan", "build-plan"):
        with pytest.raises(ValueError, match="봉인"):
            asyncio.run(m.deliberate_continue(r.job["id"], "해석으로 확인하자", job=job))
    assert set(delib_jobs._JOBS) == before, "거절했는데 잡이 생겼다"


def test_봉인_없이_돈_심의를_손잡이로_봉인해_이어받지도_않는다(monkeypatch, tmp_path):
    """Job 이름으로 막던 것과 같은 까닭이다 — 이전 결정문에 그때 조회한 바깥 자료가 녹아 있다."""
    _prev(monkeypatch, tmp_path, sealed=False)
    with pytest.raises(ValueError, match="봉인"):
        asyncio.run(m.deliberate_continue("prev-1", "두께를 다시 보라", advanced={"sealed": 1}))
    assert list(delib_jobs._JOBS) == ["prev-1"]


def test_끄고_돈_유입_손잡이와_보고서_저장_안_함도_이어받는다(monkeypatch, tmp_path):
    """봉인까지는 아니어도 VOC·자유 조회·지식카드를 끄고 저장 없이 돈 심의다 — 이어가면 전부 켜진 채 돌았고,
    저장하지 말라던 심의가 보고서를 만들었다."""
    r = _run(monkeypatch, tmp_path, job="diagnosis", save_report=False,
             advanced={"voc": "off", "free_tools": 0, "persona_knowledge": 0})
    assert not (set(r.tools) & (_INFLOW | {"create_report_draft"})) and r.free == [], "시험 전제 — 첫 회차가 닫고 돌았다"
    c = _continue(r, keep_seats=True)
    # 좌석 재심사(recommend_agents)는 세지 않는다 — 이어하기에서만 도는 단계라 첫 회차에 끈 적이 없고, 누가
    # 앉는지만 정한다(봉인만 그것까지 닫는다).
    leaked = set(c.tools) & ((_INFLOW - {"recommend_agents"}) | {"create_report_draft", "update_report_draft"})
    assert not leaked and c.free == [], f"이어하기가 끈 손잡이를 다시 켰다 — {sorted(leaked)} {c.free}"
    echo = c.job["opts"]
    assert (echo["voc"], echo["free_tools"], echo["persona_knowledge"], echo["save_report"]) == ("off", 0, 0, 0), echo
    assert c.out["sealed"] is False and "봉인" not in (c.job["decision"] or ""), "봉인하지 않은 심의에 봉인을 찍었다"


def test_이어가면서_손잡이를_바꾸려면_advanced_로_다시_건다(monkeypatch, tmp_path):
    """이어받는 것이 기본이고, 호출자가 다시 정하면 그 값이 이긴다(봉인만 빼고)."""
    r = _run(monkeypatch, tmp_path, job="diagnosis", save_report=False,
             advanced={"voc": "off", "free_tools": 0, "persona_knowledge": 0})
    c = _continue(r, advanced={"persona_knowledge": 1, "save_report": 1})
    assert "agent_search" in c.tools and "create_report_draft" in c.tools, sorted(set(c.tools))
    assert "query_voc" not in c.tools and c.free == [], "바꾸지 않은 손잡이까지 풀렸다"


def test_아무것도_끄지_않고_돈_심의는_종전대로_이어진다(monkeypatch, tmp_path):
    """대조군 — 이어받을 것이 없으면 이어하기는 엔진 기본값으로 돈다."""
    r = _run(monkeypatch, tmp_path, job="diagnosis")
    c = _continue(r)
    assert "agent_search" in c.tools and c.free, "기본값으로 돈 심의의 이어하기가 닫힌 채 돌았다"
    assert c.out["sealed"] is False


# ── 찾을 수 있다 ─────────────────────────────────────────────────────────────
def test_메뉴와_도구_설명에서_찾을_수_있다():
    menu = asyncio.run(m.deliberate_jobs())
    row = next(j for j in menu["jobs"] if j["job"] == SEALED)
    assert row["label"] == "리스크 심사(봉인)" and "소급" in row["when"]
    tools = {t.name: t.description for t in asyncio.run(m.mcp.list_tools())}
    assert SEALED in m._INSTRUCTIONS and SEALED in (m.deliberate_start.__doc__ or "")
    for text in (tools["deliberate_start"], menu["options"]["advanced"]):
        assert "sealed" in text and SEALED in text, text[-400:]
        for _closed_value, label in d._SEALED_CLOSE.values():
            assert label in text, f"닫는 경로 '{label}' 이 안내에 없다 — 엔진 표에서 읽어 적는다"
        assert d._SEALED_OPEN in text
    assert f"{len(delib_jobs.JOBS)}가지" in tools["deliberate_jobs"], tools["deliberate_jobs"]


def test_이어하기_설명이_무엇을_이어받는지와_봉인은_못_푼다는_것을_말한다():
    """이어받는 것이 기본이 됐다 — 적어 두지 않으면 호출자는 이어하기가 왜 VOC 를 안 보는지 모른다."""
    tool = next(t for t in asyncio.run(m.mcp.list_tools()) if t.name == "deliberate_continue")
    for key in m._CONT_CARRY:
        assert key in tool.description, f"이어받는 손잡이 {key} 가 설명에 없다"
    for want in ("이어받는다", "advanced", "풀 수 없다"):
        assert want in tool.description, want
    assert "advanced" in tool.inputSchema["properties"], "설명은 advanced 로 바꾸라는데 인자가 없다"

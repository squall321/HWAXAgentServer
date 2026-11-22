# 분석 루틴 실행기의 전문가 팀(HWAXPortal docs/expert-teams PLAN 2-2 · 3-2 · ET-2 · ET-4 · ET-5) — 실행 보기의 팀 사본을 LLM 단계 · 심의 단계에 쓰는지 본다
"""실행기 시험(test_routine_executor)의 가짜 게이트웨이 · 대본 챗 모델을 그대로 쓴다 — 실행 보기에 팀 사본(루틴 기본 view.team · 단계 step.team)을
얹는 것만 더한다. 페르소나(app._persona_meta)와 지식카드(app._knowledge_excerpt)는 진짜 꼴의 가짜로 바꿔 낀다.

  실행:  .venv/bin/python -m pytest tests/test_routine_team.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import app as a  # noqa: E402
import routine as rt  # noqa: E402
from test_routine_executor import (FakeGateway, ScriptLLM, TMAP, TOOLS, _ai, _app_ns, _env, _ev, _reasoning, _req, _routine,  # noqa: E402
                                   _run, _step, _tc, _user_tool, _wire, kit)  # noqa: F401 — kit 은 시험 고정물이다

_NO = object()


class TeamGateway(FakeGateway):
    """포털 run_view 꼴 그대로 — 팀 칸을 쓰는 루틴만 실행 보기에 team(사본 · null) · team_why 가 있다. 단계 team · team_why 는 계획 행(_step)에 싣는다."""

    def __init__(self, steps, *, team=_NO, team_why="", **kw):
        super().__init__(steps, **kw)
        self.team, self.team_why = team, team_why

    def view(self) -> dict:
        v = super().view()
        if self.team is not _NO:
            v["team"] = self.team
        if self.team_why:
            v["team_why"] = self.team_why
        return v


def _snap(tid="tm_" + "a" * 32, name="힌지 낙하 검토팀", lead=("mech-a", "기구 A"), helpers=(("cae-b", "CAE B"),), apps=(), tools=()):
    """포털 teams.resolve 의 사본 꼴."""
    return {"id": tid, "name": name, "version": 3, "lead": {"key": lead[0], "name": lead[1]},
            "helpers": [{"key": k, "name": n} for k, n in helpers], "apps": list(apps), "tools": list(tools), "resolved_at": 1763800000}


def _meta(role="", name="", operator=False, apps=(), key_tools=(), note=""):
    """app._persona_meta 의 꼴."""
    return {"key": "", "name": name, "role": role, "operator": operator, "apps": list(apps), "key_tools": list(key_tools), "note": note}


PERSONAS = {
    "mech-a": _meta("기구 신뢰성 — 힌지 낙하 판단 기준", "기구 A"),
    "cae-b": _meta("CAE 해석 — 낙하 해석 판단 기준", "CAE B"),
    "mat-c": _meta("재료 물성 — 피로 판단 기준", "재료 C"),
    "op-sf": _meta("StepForge 운영자 — sf_get 으로 형상을 연다", "SF 운영자", operator=True, apps=("step_forge",), key_tools=("sf_get",)),
    "ghost": _meta(),                                                     # 이 사람의 시야에서 비어 왔다
    "broken": _meta(note="게이트웨이에 연결하지 못했습니다"),             # 못 물어봤다
}
KB = {"mech-a": ("- [R1] 힌지 낙하 1.2m 에서 축 파손", ""), "cae-b": ("- [R7] 낙하 해석 메시 0.3mm", ""),
      "mat-c": ("", ""), "op-sf": ("운영자는 찾지 않는다", "")}


def _fakes(mp, kb=None, personas=None, delay=0.0):
    """페르소나 · 지식카드 가짜 — 부른 것을 적는다. delay 로 지식 검색이 한꺼번에 도는지(동시 수) 본다."""
    log = {"meta": [], "kb": [], "inflight": 0, "max": 0}
    kb = dict(KB, **(kb or {}))
    pers = dict(PERSONAS, **(personas or {}))

    async def meta(app_, groups, key):
        log["meta"].append((list(groups), key))
        return dict(pers.get(key) or _meta())

    async def know(app_, groups, key, query, budget):
        log["kb"].append((key, query, budget))
        log["inflight"] += 1
        log["max"] = max(log["max"], log["inflight"])
        try:
            await asyncio.sleep(delay)
        finally:
            log["inflight"] -= 1
        return kb[key]

    mp.setattr(a, "_persona_meta", meta)
    mp.setattr(a, "_knowledge_excerpt", know)
    return log


def _boom(mp):
    async def meta(*_a, **_k):
        raise AssertionError("팀이 없는데 페르소나를 읽었다")

    async def know(*_a, **_k):
        raise AssertionError("팀이 없는데 지식카드를 찾았다")
    mp.setattr(a, "_persona_meta", meta)
    mp.setattr(a, "_knowledge_excerpt", know)


def _sys(llm, sid) -> str:
    return str(llm.turns("react", sid)[0][2][0].content)


def _norm(evs) -> list:
    """프레임 비교용 — 호출 짝 키 · 시각 · 걸린 시간은 실행마다 다르다."""
    return [(e, {k: v for k, v in d.items() if k not in ("call", "ts", "ms")}) for e, d in evs]


def _status(evs) -> list:
    return [d["step"] for e, d in evs if e == "status"]


def _warns(evs) -> list:
    return [(d.get("code"), d.get("message")) for e, d in evs if e == "warning"]


def _scenario(gw_kw: dict, step_team=_NO):
    """platform 단계 둘 + 심의 단계 하나 — 단계 팀은 voc 단계에만 덧씌울 수 있다."""
    steps = [_step("voc", backends=("signalforge",), allowed={"signalforge": ["search_voc"]}, keys=("model_key",), goal="S26 스웰링 VOC 를 찾는다"),
             _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}, uses=("voc",)),
             _step("delib", kind="deliberation", uses=("ft",), delib={"job": "mechanism", "chair_template": "diagnosis"})]
    if step_team is not _NO:
        steps[0]["team"] = step_team
    gw = TeamGateway(steps, impls={("signalforge", "search_voc"): lambda x: ("VOC 12건 — 모델 SM-S938", False)}, **gw_kw)
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="스웰링"))], "ft": [_ai(_tc("failuretrace_search", q="x"))]},
                    envelope={"voc": [_env(summary="VOC [voc.c1]", keys={"model_key": "SM-S938"})], "ft": [_env()],
                              "delib": [_env(summary="결정 [delib.c2]")]},
                    insight=["인사이트 [voc.c1]."])
    return gw, llm


# ── 팀이 없으면 팀 전과 같다 ───────────────────────────────────────────────────────────────────────────────────
def test_팀_칸이_없으면_페르소나도_지식카드도_읽지_않고_시스템_글_심의_인자가_팀_전_꼴이다(kit):
    _boom(kit)
    gw, llm = _scenario({})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(routine={"id": "rt-1", "inputs": {"model": "S26"}, "note": "경쟁사 비교도"})))
    assert _routine(evs, "done")[-1]["state"] == "done"
    for sid in ("voc", "ft"):
        s = _sys(llm, sid)
        assert "[전문가 팀" not in s and "지식카드" not in s
        assert s.endswith("- 끝나면 한국어로 짧게 정리한다 — 알아낸 것 · 넘길 키 후보 · 빈 곳."), "단계 시스템 글 끝에 아무것도 붙지 않는다"
    start = gw.rcalls("delib")[0]["arguments"]
    assert set(start) == {"question", "job", "advanced"} and "personas" not in start, "좌석 칸이 없다 — 서버가 발굴한다"
    assert "전문가 팀" not in _reasoning(evs) and not any("전문가 팀" in s for s in _status(evs)) and _warns(evs) == []
    assert not any(d.get("detail") for d in _routine(evs, "step") if d["step"] != "delib")


def test_팀이_null_이고_단계_팀이_없으면_팀_칸이_없는_실행과_프레임_LLM_입력_게이트웨이_요청이_같다(kit):
    """포털은 팀 칸을 쓰는 루틴의 팀을 못 풀면 team: null 을 싣는다(까닭이 없을 수도 있다) — 그 실행은 팀 칸이 없는 실행과 같게 돈다."""
    _boom(kit)
    runs = []
    for kw in ({}, {"team": None}):
        gw, llm = _scenario(kw)
        _wire(kit, gw, llm)
        evs = _ev(_run(_app_ns(llm), _req()))
        runs.append((_norm(evs), [(k, s, [(m.type, str(m.content)) for m in ms]) for k, s, ms, _kw in llm.seen],
                     [(n, x) for n, x, _h in gw.log]))
    assert runs[0] == runs[1]


# ── 단계 덮어쓰기 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_단계_팀은_사본이면_그것_false_면_없음_칸이_없으면_루틴_기본이다(kit):
    log = _fakes(kit)
    steps = [_step("a1", allowed={"signalforge": ["search_voc"]}),
             _step("a2", allowed={"signalforge": ["search_voc"]}),
             _step("a3", allowed={"signalforge": ["search_voc"]})]
    steps[1]["team"] = _snap(tid="tm_" + "b" * 32, name="재료 검토팀", lead=("mat-c", "재료 C"), helpers=())
    steps[2]["team"] = False
    gw = TeamGateway(steps, team=_snap())
    llm = ScriptLLM(react={s: [_ai(_tc("search_voc", q=s))] for s in ("a1", "a2", "a3")})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    s1, s2, s3 = _sys(llm, "a1"), _sys(llm, "a2"), _sys(llm, "a3")
    assert "[전문가 팀 — 「힌지 낙하 검토팀」" in s1 and "너는 'mech-a' 전문가로서" in s1
    assert "[전문가 팀 — 「재료 검토팀」" in s2 and "너는 'mat-c' 전문가로서" in s2 and "mech-a" not in s2
    assert "[전문가 팀" not in s3 and "지식카드" not in s3
    assert s3.endswith("빈 곳."), "false 는 '이 단계는 팀 없이' 다 — 루틴 기본으로 메우지 않는다"
    assert [k for _g, k in log["meta"]] == ["mech-a", "cae-b", "mat-c"]
    details = {d["step"]: d["detail"] for d in _routine(evs, "step") if d.get("detail")}
    assert details == {"a1": "전문가 팀 「힌지 낙하 검토팀」 — 주 기구 A(mech-a) · 보조 CAE B(cae-b)",
                       "a2": "전문가 팀 「재료 검토팀」 — 주 재료 C(mat-c)"}


# ── LLM 단계의 팀 블록 ──────────────────────────────────────────────────────────────────────────────────────
def test_LLM_단계는_주_보조의_역할과_지식카드_발췌를_싣고_지식은_한꺼번에_찾는다(kit):
    log = _fakes(kit, delay=0.05)
    gw, llm = _scenario({"team": _snap(helpers=(("cae-b", "CAE B"), ("mat-c", "재료 C")))})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(routine={"id": "rt-1", "inputs": {"model": "S26"}, "note": "경쟁사 비교도"})))
    s = _sys(llm, "voc")
    # 앞부분은 팀 전과 같고 팀 블록은 끝에 붙는다
    assert s.index("[규칙]") < s.index("[전문가 팀 — 「힌지 낙하 검토팀」 · 이 단계는 이 팀의 판단으로 돈다]")
    assert "너는 'mech-a' 전문가로서 이 단계를 맡는다" in s and "기구 신뢰성 — 힌지 낙하 판단 기준" in s
    assert "[mech-a 지식카드 — 이 단계 할 일 연관 발췌]\n- [R1] 힌지 낙하 1.2m 에서 축 파손" in s
    assert "[함께 선택된 보조 전문가 — 판단 기준만 빌려준다]\n- cae-b · 도메인 전문가\nCAE 해석 — 낙하 해석 판단 기준" in s
    assert "[보조 cae-b 지식카드 — 이 단계 할 일 연관 발췌]\n- [R7] 낙하 해석 메시 0.3mm" in s
    assert "[보조 mat-c 지식카드]\n이 단계 할 일과 연관된 보유 지식을 찾지 못했다(사내 지식카드에 관련 내용이 없다)." in s
    assert "정리는 'mech-a' 한 사람의 목소리로 쓴다" in s and "어느 전문가의 지식카드인지" in s
    assert "발췌에 없는 것을 있는 것처럼 말하지 마라" in s and "넘길 키는 여전히 이 단계 도구 결과에" in s
    assert "이견:" in s and "부르겠다고 말하지도 않는다" in s
    # 질의 — 할 일 + 입력 값 + 덧붙인 말, 몫 — 주 전문가 CHAT_KNOWLEDGE_BUDGET · 도메인 보조 _helper_knowledge_share(둘)
    voc = [x for x in log["kb"] if x[1].startswith("S26 스웰링 VOC 를 찾는다")]
    assert voc == [("mech-a", "S26 스웰링 VOC 를 찾는다 S26 경쟁사 비교도", a.CHAT_KNOWLEDGE_BUDGET),
                   ("cae-b", "S26 스웰링 VOC 를 찾는다 S26 경쟁사 비교도", a._helper_knowledge_share(2)),
                   ("mat-c", "S26 스웰링 VOC 를 찾는다 S26 경쟁사 비교도", a._helper_knowledge_share(2))]
    assert log["max"] == 3, "주 · 보조의 지식카드를 한꺼번에 찾는다"
    # 그 사람의 그룹으로 페르소나를 읽는다
    assert {tuple(g) for g, _k in log["meta"]} == {("연구소",)}
    st = _status(evs)
    assert f"주 전문가 기구 A(mech-a) 지식카드 {len(KB['mech-a'][0]):,}자" in st
    assert "보조 재료 C(mat-c) — 이 단계 할 일과 연관된 지식카드 없음" in st
    assert _warns(evs) == [], "0건은 경고가 아니다 — '없다' 이지 '못 물어봤다' 가 아니다"
    assert "(이 단계는 전문가 팀 「힌지 낙하 검토팀」 — 주 기구 A(mech-a) · 보조 CAE B(cae-b), 재료 C(mat-c) 의 판단으로 돈다" in _reasoning(evs)


def test_지식카드_강등과_검색_도구_없음은_못_물어봤다로_밝히고_경고를_낸다(kit):
    _fakes(kit, kb={"mech-a": ("", "hybrid 검색 30초 초과"), "cae-b": ("", None)})
    gw, llm = _scenario({"team": _snap()})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    s = _sys(llm, "voc")
    assert "[mech-a 지식카드]\n지식 조회를 마치지 못해 이 단계에는 이 전문가의 사내 발췌가 없다(없다는 뜻이 아니다)." in s
    assert "[보조 cae-b 지식카드]\n지식 조회를 마치지 못해" in s and "찾지 못했다" not in s
    w = [m for c, m in _warns(evs) if c == "knowledge_degraded"]
    assert any("주 전문가 기구 A(mech-a)" in m and "hybrid 검색 30초 초과" in m and "없다는 뜻이 아니다" in m for m in w)
    assert any("보조 CAE B(cae-b)" in m and a._HELPER_KB_NO_TOOL in m for m in w), "사유 None(검색 도구를 받지 못함)도 못 물어본 것이다"
    assert any("hybrid 검색 30초 초과" in x for x in _status(evs))


def test_그_사람이_못_보는_보조는_빼고_주_전문가를_못_보면_팀_없이_돈다(kit):
    log = _fakes(kit)
    gw, llm = _scenario({"team": _snap(helpers=(("broken", ""), ("ghost", "유령"), ("cae-b", "CAE B")))})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    s = _sys(llm, "voc")
    assert "- cae-b · 도메인 전문가" in s and "broken" not in s and "ghost" not in s
    assert {k for k, _q, _b in log["kb"]} == {"mech-a", "cae-b"}, "뺀 사람의 지식카드는 찾지 않는다"
    w = [m for c, m in _warns(evs) if c == "team_member_dropped"]
    assert any("보조 broken 는 뺐다" in m and "역할 문서를 불러오지 못했다(게이트웨이에 연결하지 못했습니다)" in m for m in w)
    assert any("보조 유령(ghost) 는 뺐다" in m and "시야에서 보이지 않는다" in m for m in w)
    # 주 전문가를 못 보면 — 목소리가 없다, 팀 없이(보조만으로 메우지 않는다)
    log2 = _fakes(kit)
    gw2, llm2 = _scenario({"team": _snap(lead=("ghost", "유령"), helpers=(("cae-b", "CAE B"),))})
    _wire(kit, gw2, llm2)
    evs2 = _ev(_run(_app_ns(llm2), _req()))
    assert "[전문가 팀" not in _sys(llm2, "voc") and log2["kb"] == []
    assert any("주 전문가 유령(ghost) 를 쓰지 못해 이 단계는 팀 없이 돈다" in m for c, m in _warns(evs2) if c == "team_member_dropped")
    assert "personas" not in gw2.rcalls("delib")[0]["arguments"], "심의 단계도 팀 없이 — 서버가 발굴한다"


def test_운영자는_지식카드를_찾지_않고_그_앱_도구를_울타리_밖으로_묶지_않으며_사본의_apps_tools_도_읽지_않는다(kit):
    log = _fakes(kit)
    tools = dict(TOOLS, sf_get=_user_tool("sf_get"))
    tmap = dict(TMAP, sf_get="step_forge")
    # 보조 운영자 — 운영자의 입구 도구(sf_get)와 사본의 apps · tools(failuretrace)가 있어도 voc 단계 울타리는 signalforge:search_voc 하나다
    gw, llm = _scenario({"team": _snap(helpers=(("op-sf", "SF 운영자"), ("cae-b", "CAE B")), apps=("failuretrace",),
                                       tools=("failuretrace_search", "sf_get"))})
    _wire(kit, gw, llm, tools=tools, tmap=tmap)
    _ev(_run(_app_ns(llm), _req()))
    assert llm.bound[0] == ["search_voc"], llm.bound
    s = _sys(llm, "voc")
    assert "- op-sf · MCP 운영자(이 단계에서는 그 앱 도구를 묶지 않는다)" in s and "[보조 op-sf 지식카드" not in s
    assert "op-sf" not in {k for k, _q, _b in log["kb"]}
    assert [b for k, _q, b in log["kb"] if k == "cae-b"][0] == a._helper_knowledge_share(1), "몫은 도메인 보조 수로 나눈다"
    # 주 전문가가 운영자 — 지식카드를 찾지 않고 역할의 판단 기준만, 도구는 그대로
    log2 = _fakes(kit)
    gw2, llm2 = _scenario({"team": _snap(lead=("op-sf", "SF 운영자"), helpers=())})
    _wire(kit, gw2, llm2, tools=tools, tmap=tmap)
    _ev(_run(_app_ns(llm2), _req()))
    s2 = _sys(llm2, "voc")
    assert llm2.bound[0] == ["search_voc"] and log2["kb"] == []
    assert "'op-sf' 는 HE팀 MCP 운영자다 — 이 단계에서는 그 운영자가 모는 앱의 도구를 더 묶지 않는다" in s2
    assert "지식카드" not in s2, "찾지 않았으면 지식카드 규칙도 싣지 않는다"


def test_역할은_상한에서_표식을_달고_줄인다(kit):
    _fakes(kit, personas={"mech-a": _meta("가" * (a.PERSONA_ROLE_MAX + 50), "기구 A"),
                          "cae-b": _meta("나" * (a.PERSONA_HELPER_ROLE_MAX + 50), "CAE B")})
    gw, llm = _scenario({"team": _snap()})
    _wire(kit, gw, llm)
    _ev(_run(_app_ns(llm), _req()))
    s = _sys(llm, "voc")
    assert f"…[{a.PERSONA_ROLE_MAX + 50:,}자 중 앞 {a.PERSONA_ROLE_MAX:,}자]" in s
    assert f"…[{a.PERSONA_HELPER_ROLE_MAX + 50:,}자 중 앞 {a.PERSONA_HELPER_ROLE_MAX:,}자]" in s


def test_포털이_적은_팀_까닭은_실행_보기는_한_번_단계는_그_단계에서_남긴다(kit):
    _fakes(kit)
    gw, llm = _scenario({"team": None, "team_why": "팀 tm_x 를 찾을 수 없다 — 지워졌거나 이 사람에게 보이지 않는다"},
                        step_team=False)
    gw.plan[0]["team_why"] = "팀 「재료 검토팀」 의 보조 재료 C(mat-c) 는 이 사람이 쓸 수 없어(그 앱 권한이 없다) 뺐다"
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    st = _status(evs)
    assert st.count("전문가 팀 — 팀 tm_x 를 찾을 수 없다 — 지워졌거나 이 사람에게 보이지 않는다") == 1
    assert "전문가 팀 — 팀 「재료 검토팀」 의 보조 재료 C(mat-c) 는 이 사람이 쓸 수 없어(그 앱 권한이 없다) 뺐다" in st
    assert all("[전문가 팀" not in _sys(llm, sid) for sid in ("voc", "ft"))


# ── 심의 단계 ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_심의_단계는_팀을_좌석으로_넘기고_못_보는_사람은_뺀다(kit):
    log = _fakes(kit)
    gw, llm = _scenario({"team": _snap(helpers=(("cae-b", "CAE B"), ("ghost", ""), ("mat-c", "재료 C")))})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    start = gw.rcalls("delib")[0]["arguments"]
    assert start["personas"] == [{"key": "mech-a"}, {"key": "cae-b"}, {"key": "mat-c"}]
    assert start["job"] == "mechanism" and start["advanced"] == {"chair_template": "diagnosis"}, "틀은 그대로 — 지정석은 엔진이 더한다"
    assert log["kb"] and not [x for x in log["kb"] if x[1].startswith("delib 를 조회한다")], \
        "심의 단계는 지식카드를 찾지 않는다(엔진이 좌석마다 찾는다) — 찾은 것은 앞 LLM 단계들이다"
    assert any("전문가 팀 「힌지 낙하 검토팀」 3명을 심의 좌석으로 넘긴다" in s for s in _status(evs))


def test_리스크_심사_계열_심의에는_팀을_앉히지_않는다(kit):
    for delib in ({"job": "risk-review"}, {"job": "risk-review-sealed"}, {"job": "sealed"},
                  {"job": "default", "chair_template": "risk-review"}, {"job": "RISK-REVIEW"}):
        log = _fakes(kit)
        steps = [_step("delib", kind="deliberation", delib=delib)]
        gw = TeamGateway(steps, team=_snap())
        llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c2]")]})
        _wire(kit, gw, llm)
        evs = _ev(_run(_app_ns(llm), _req()))
        start = gw.rcalls("delib")[0]["arguments"]
        assert "personas" not in start, delib
        assert log["meta"] == [], "리스크 심사에는 페르소나도 읽지 않는다"
        line = "리스크 심사에는 팀을 앉히지 않는다(D-18) — 전문가 팀 「힌지 낙하 검토팀」 을 좌석으로 넘기지 않았다"
        assert line in _status(evs) and line in _reasoning(evs), delib


def test_심의_단계의_false_팀과_팀_없는_심의는_좌석_칸이_없다(kit):
    _boom(kit)
    steps = [_step("delib", kind="deliberation", delib={"job": "mechanism"})]
    steps[0]["team"] = False
    gw = TeamGateway(steps, team=_snap())
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c2]")]})
    _wire(kit, gw, llm)
    _ev(_run(_app_ns(llm), _req()))
    assert set(gw.rcalls("delib")[0]["arguments"]) == {"question", "job"}


def test_사본_꼴이_틀리면_팀이_없는_것이다():
    ctx = type("C", (), {})()
    ctx.view = {"team": _snap()}
    assert rt._step_team(ctx, {})["id"] == _snap()["id"]
    assert rt._step_team(ctx, {"team": None})["id"] == _snap()["id"], "null 은 칸이 없는 것과 같다 — 루틴 기본"
    assert rt._step_team(ctx, {"team": False}) is None
    for bad in ({"lead": {}}, {"lead": {"key": "  "}}, {"lead": "mech-a"}, {"name": "x"}):
        assert rt._step_team(ctx, {"team": bad}) is None, bad
    ctx.view = {"team": None}
    assert rt._step_team(ctx, {}) is None
    ctx.view = {}
    assert rt._step_team(ctx, {}) is None


def test_리스크_판정은_포털_delib_is_risk_와_같고_엔진_별칭까지_닫는다():
    assert rt._risk_delib({"job": "risk-review"}) and rt._risk_delib({"job": "risk-review-sealed"})
    assert rt._risk_delib({"job": "sealed"}) and rt._risk_delib({"job": "risk-sealed"})
    assert rt._risk_delib({"job": "default", "chair_template": "risk-review"})
    assert not rt._risk_delib({"job": "mechanism", "chair_template": "diagnosis"}) and not rt._risk_delib({})


def test_지식카드_질의는_할_일_입력_덧붙인_말이고_다_비면_제목이다():
    ctx = type("C", (), {})()
    ctx.view, ctx.note = {"inputs": {"model": "S26", "empty": "", "none": None, "n": 3}}, "경쟁사"
    assert rt._team_query(ctx, {"goal": "할 일"}) == "할 일 S26 3 경쟁사"
    ctx.view, ctx.note = {}, ""
    assert rt._team_query(ctx, {"goal": "", "title": "제목"}) == "제목"


def test_시험이_쓴_가짜는_진짜_꼴이다():
    """가짜의 꼴이 진짜와 다르면 판정 결함을 숨긴다 — _persona_meta 의 칸 · _knowledge_excerpt 의 인자 순서를 진짜 서명과 맞댄다."""
    import inspect
    assert list(inspect.signature(a._knowledge_excerpt).parameters) == ["app", "groups", "agent_type", "query", "budget"]
    assert list(inspect.signature(a._persona_meta).parameters) == ["app", "groups", "agent_type"]
    src = inspect.getsource(a._persona_meta)
    for k in ("key", "name", "role", "operator", "apps", "key_tools", "note"):
        assert f'"{k}"' in src, k
    assert json.dumps(_meta()) and set(_meta()) == {"key", "name", "role", "operator", "apps", "key_tools", "note"}

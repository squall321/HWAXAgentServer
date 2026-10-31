# 코어 어휘표 personas 칸(persona_links)이 줄마다 검증되어 /health 에 실리고, 그림자 기록만 남기며 고르기는 바꾸지 않는지(HWAXPortal docs/request-1029 design-e5 E5-4)
"""형제 리포 실파일 대조 — 포털 어휘표에 personas 칸이 있으면 그 줄이 조직도 라벨(orgTaxonomy) · 어휘표 types · 게이트웨이 tool_areas.json 과
어긋나지 않는지 본다(칸이 아직 없으면 건너뛴다). 지문 — personas 칸도 표의 일부라 지문에 든다(게이트웨이와 같은 셈).
동작 변화 0 — personas 가 있든 없든 띵킹 소집 · 예심 · 본심의 결과 글이 같다. 그림자는 박스 자리에만, 질문 글 없이.

  실행:  .venv/bin/python -m pytest tests/test_persona_links.py -q
"""
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as a  # noqa: E402
import deliberation  # noqa: E402
import persona_links as pl  # noqa: E402
import thinking as t  # noqa: E402

PORTAL = ROOT.parent / "HWAXPortal"
GATEWAY = ROOT.parent / "HWAXMcpGateway"
TYPES = {"고장모드": "failure_mode", "시험항목": "test_item", "PLM 과제": "plm_task", "개발모델": "plm_model", "물성": "material_property",
         "관리 기준": "criterion"}
PERSONAS = {
    "domains": {"rel": {"core_types": ["test_item", "failure_mode"], "areas": ["knowledge"], "apps": []},
                "mech": {"core_types": ["failure_mode"], "areas": ["cad"], "apps": ["heax-step_forge"]},
                "pcb": {"core_types": ["nope_type"], "areas": [], "apps": []},          # 없는 타입 — 줄을 버린다
                "he": {"core_types": [], "areas": [], "apps": []},                      # 운영자 — 버린다
                "zzz": {"core_types": [], "areas": [], "apps": []},                     # 조직도에 없다 — 버린다
                "sim": {"core_types": ["failure_mode"], "areas": ["teleport"]}},        # 없는 영역 — 버린다
    "experts": {"rel-thermal-shock": {"apps": ["heax-thermal_shock_mcp"]}, "he-cad-step": {"apps": ["heax-step_forge"]},
                "mech-drop": {"apps": ["no-such-app"]}},
    "data_types": ["plm_task", "plm_model", "bogus"],
    "no_tools": ["sw", "rel"],
    "extra": 1,
}
ORG = frozenset({"rel", "mech", "pcb", "sim", "sw", "xd", "he", "cam"})
AREAS = frozenset({"knowledge", "cad", "calc", "sim"})
APPS = frozenset({"heax-step_forge", "heax-thermal_shock_mcp", "ai-data-hub"})


def test_줄마다_검증하고_틀린_줄만_버린다():
    v = pl.validate(PERSONAS, types_map=TYPES, domains_known=ORG, areas_known=AREAS, apps_known=APPS)
    assert v["present"] is True
    assert set(v["domains"]) == {"rel", "mech"} and v["domains"]["mech"]["apps"] == ["heax-step_forge"]
    assert set(v["experts"]) == {"rel-thermal-shock"} and v["data_types"] == ["plm_task", "plm_model"] and v["no_tools"] == ["sw"]
    why = {d["at"]: d["why"] for d in v["dropped"]}
    assert "어휘표 types" in why["personas.domains.pcb"] and "운영자" in why["personas.domains.he"]
    assert "조직도" in why["personas.domains.zzz"] and "도구 지도" in why["personas.domains.sim"]
    assert "운영자" in why["personas.experts.he-cad-step"] and "도구 지도" in why["personas.experts.mech-drop"]
    assert "personas.data_types.bogus" in why and "domains 에도" in why["personas.no_tools.rel"] and "모르는 칸" in why["personas.extra"]
    assert v["missing"] == ["cam", "pcb", "sim", "xd"], "줄도 no_tools 도 없는 조직도 도메인(운영자 뺌)"
    assert v["unchecked"] == []
    u = pl.validate(PERSONAS, types_map=TYPES)
    assert set(u["unchecked"]) == {"domains", "areas", "apps"} and "sim" in u["domains"] and "zzz" in u["domains"], \
        "대조할 표가 없으면 그 대조만 건너뛴다"
    assert pl.validate(None, types_map=TYPES)["present"] is False
    assert pl.validate([1], types_map=TYPES)["dropped"][0]["at"] == "personas"


def test_켠_쪽이_고를_도구와_좌석_덮음():
    v = pl.validate(PERSONAS, types_map=TYPES, domains_known=ORG, areas_known=AREAS, apps_known=APPS)
    names = ["stepforge_list_parts", "stepforge_section", "thermal_predict", "search_records", "drop_submit", "aidh_get"]
    app = {"stepforge_list_parts": "heax-step_forge", "stepforge_section": "heax-step_forge", "thermal_predict": "heax-thermal_shock_mcp",
           "search_records": "ai-data-hub", "drop_submit": "smart-twin-mcp", "aidh_get": "ai-data-hub"}.get
    area = {"search_records": "knowledge", "aidh_get": "knowledge", "stepforge_section": "cad", "stepforge_list_parts": "cad"}.get
    got = pl.link_tools(v, key="mech-hinge", names=names, question="힌지 section 단면", n=12, tool_app=app, tool_area=area)
    assert got["link"] == "domain" and got["top"] == ["stepforge_list_parts", "stepforge_section"], "입구(list) 먼저, 그다음 질문 낱말"
    ex = pl.link_tools(v, key="rel-thermal-shock", names=names, question="", n=12, tool_app=app, tool_area=area)
    assert ex["link"] == "expert" and ex["top"] == ["search_records", "aidh_get", "thermal_predict"], \
        "전문가 덮어쓰기 ∪ 도메인 줄 — 입구(search) 먼저, 나머지는 이름 차례"
    assert pl.link_tools(v, key="sw-ui", names=names, question="", n=12, tool_app=app, tool_area=area) == \
        {"link": "no_tools", "top": [], "n_cand": 0}
    assert pl.link_tools(v, key="cam-ois", names=names, question="", n=12, tool_app=app, tool_area=area)["link"] == "none", \
        "줄이 없으면 우선 도구를 주지 않는다(D-E5-5)"
    c = pl.seat_cover(v, question="고장 모드 말고 관리기준 · 시험 항목 · 고장모드를 본다", seats=["mech-drop", "sw-ui"], want=5)
    assert c["types"] == ["failure_mode", "test_item", "criterion"], "띄어쓰기는 보지 않는다('시험 항목' = '시험항목')"
    assert c["dq"] == ["mech", "rel"] and c["cover"] == ["rel"] and c["short"] == 3
    assert c["data_only"] is False
    d = pl.seat_cover(v, question="PLM 과제 개발모델 목록", seats=[], want=5)
    assert d["types"] == ["plm_task", "plm_model"] and d["data_only"] is True and d["dq"] == []


def _vocab(tmp_path, personas=None, overlay=None):
    base = {"version": 1, "canonical": {"app": "rootdesigndatahub", "label": "자주검증 Datahub"}, "rule_words": ["고장모드"],
            "search": [], "relations": [], "types": TYPES, "apps": {}}
    if personas is not None:
        base["personas"] = personas
    p = tmp_path / "core_vocab.json"
    p.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    loc = tmp_path / "core_vocab.local.json"
    if overlay is not None:
        loc.write_text(json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
    else:
        loc.unlink(missing_ok=True)
    return p, loc, base


@pytest.fixture
def links_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SEAT_EVAL_DIR", str(tmp_path / "seat-eval"))
    monkeypatch.setattr(pl, "ORG_TAXONOMY_PATH", tmp_path / "orgTaxonomy.json")
    (tmp_path / "orgTaxonomy.json").write_text(json.dumps({"domain_label": {d: d for d in ORG}}), encoding="utf-8")
    pl._ORG.update(mtime=None, domains=None)
    pl._SHADOW.update(day=None, written=0, error=None)
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": 1.0, "map": {"stepforge_section": "heax-step_forge", "thermal_predict":
                                                                    "heax-thermal_shock_mcp", "search_records": "ai-data-hub"},
                                               "areas": {"stepforge_section": "cad", "search_records": "knowledge"},
                                               "area_meta": {k: {} for k in AREAS}, "routine_read": None})
    monkeypatch.setattr(a, "_APPS_CACHE", {"apps": {k: {} for k in APPS}, "at": 1.0})
    monkeypatch.setattr(a, "_PERSONA_LINKS", {"key": None, "v": None})
    return tmp_path


def _use(monkeypatch, p, loc):
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    monkeypatch.setattr(a, "CORE_VOCAB_LOCAL_PATH", loc)
    monkeypatch.setattr(a, "_CORE_VOCAB", {"mtime": None, "data": None, "error": "아직 읽지 않음", "overlay": None})


def test_health_와_덧씌우기와_지문(links_env, monkeypatch):
    p, loc, base = _vocab(links_env)
    _use(monkeypatch, p, loc)
    h = a._core_vocab_health()
    assert h["personas"] == {"present": False}, "칸이 없으면 없다고만"
    ov = {"personas": {"domains": {"rel": {"core_types": ["test_item"], "areas": ["knowledge"], "apps": []}}, "no_tools": ["sw"]}}
    p, loc, base = _vocab(links_env, overlay=ov)
    _use(monkeypatch, p, loc)
    h = a._core_vocab_health()
    ph = h["personas"]
    assert (ph["present"], ph["domains"], ph["no_tools"], ph["dropped_n"], ph["unchecked"]) == (True, 1, 1, 0, [])
    assert ph["missing"] == ["cam", "mech", "pcb", "sim", "xd"] and ph["shadow"]["on"] is True
    merged = {**base, **ov}
    want = hashlib.sha256(json.dumps({k: v for k, v in merged.items() if k != "_doc"}, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()[:12]
    assert h["fingerprint"] == want and h["base"] != want, "personas 도 표의 일부라 지문에 든다(게이트웨이와 같은 셈)"
    assert "personas" in h["overlay"]["changed"][0]
    # 틀린 줄이 표 전체를 버리지 않는다 — 코어 규칙 글은 그대로 어휘표에서 나온다
    p, loc, _b = _vocab(links_env, personas={"domains": {"zzz": {"core_types": ["bad"]}}})
    _use(monkeypatch, p, loc)
    h = a._core_vocab_health()
    assert h["loaded"] is True and h["personas"]["dropped_n"] == 1 and h["personas"]["domains"] == 0
    assert "[코어 데이터 규칙] 고장모드" in a._system_prompt()


def test_그림자는_박스에만_질문_글_없이_고르기는_그대로(links_env, monkeypatch):
    p, loc, _b = _vocab(links_env, personas=PERSONAS)
    _use(monkeypatch, p, loc)
    prefer = ["search_records", "stepforge_section"]
    before = list(prefer)
    assert a._persona_shadow_tools("mech-drop", {"key": "mech-drop"}, "힌지 단면 비밀질문", "힌지 단면 비밀질문",
                                   ["stepforge_section", "search_records", "thermal_predict"], prefer) is None
    assert prefer == before, "지금 고른 우선 도구는 그대로"
    deliberation._persona_shadow("delib", "고장모드 비밀질문 시험항목", ["mech-drop", "pcb-warp"], 5)
    files = list((links_env / "seat-eval" / "shadow").glob("s-*.jsonl"))
    assert len(files) == 1 and oct(files[0].stat().st_mode & 0o777) == "0o600"
    body = files[0].read_text(encoding="utf-8")
    assert "비밀질문" not in body, "질문 글은 적지 않는다(qh 만)"
    recs = [json.loads(x) for x in body.splitlines()]
    assert recs[0]["site"] == "chat" and recs[0]["now"] == before and recs[0]["link"] == "domain" and recs[0]["link_top"] == ["stepforge_section"]
    assert recs[1]["site"] == "delib" and recs[1]["dq"] == ["mech", "rel"] and recs[1]["cover"] == ["rel"] and recs[1]["short"] == 3
    assert a._core_vocab_health()["personas"]["shadow"]["written"] == 2
    monkeypatch.setenv("PERSONA_SHADOW_LOG", "0")
    deliberation._persona_shadow("delib", "고장모드", ["mech-drop"], 5)
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 2, "끄면 적지 않는다"
    monkeypatch.delenv("PERSONA_SHADOW_LOG")

    def boom(*_a, **_k):
        raise OSError("디스크가 찼다")
    import seat_box  # noqa: PLC0415
    monkeypatch.setattr(seat_box, "append_jsonl", boom)
    deliberation._persona_shadow("delib", "고장모드", ["mech-drop"], 5)       # 삼킨다
    a._persona_shadow_tools("mech-drop", {"key": "mech-drop"}, "q", "q", ["search_records"], ["search_records"])
    assert "디스크가 찼다" in pl._SHADOW["error"]


def _think_run(monkeypatch):
    """test_thinking_session 의 가짜 — 소집 둘 · 예심 · 본심(답 하나 · 기권 하나)."""
    class _Rec:
        async def ainvoke(self, args):
            return json.dumps({"agents": [{"agent_type": "mech-hinge", "name": "힌지", "desc_match": 0.6},
                                          {"agent_type": "rel-drop", "name": "낙하", "desc_match": 0.3}]})

    class _Search:
        async def ainvoke(self, args):
            return json.dumps({"hits": [{"title": "힌지 카드", "record_id": "R-1", "snippet": "두께 0.8mm 시험"}]})

    class _Sess:
        async def ainvoke(self, _args):
            return json.dumps({"data": {"agent_type": "mech-hinge", "description": "힌지 설계 전문가"}})

    async def _tools(*_a, **_k):
        return {"recommend_agents": _Rec(), "agent_search": _Search(), "get_agent_session": _Sess()}

    class _LLM:
        def __init__(self):
            self.n = 0

        async def ainvoke(self, msgs):
            self.n += 1
            v = {"verdict": "answer", "scope": "힌지", "answer": "고장모드 기준으로 0.8mm 는 …", "basis": []} if "mech-hinge" in msgs[-1][1] \
                else {"verdict": "pass", "scope": "", "refer": []}
            return SimpleNamespace(content=json.dumps(v, ensure_ascii=False), response_metadata={})
    monkeypatch.setattr(t, "_tools_by_name", _tools)

    async def go():
        return [c async for c in t.run_thinking(SimpleNamespace(state=SimpleNamespace(llm=_LLM())), "힌지 고장모드 기준은?", [])]
    return b"".join(asyncio.run(go())).decode()


def test_띵킹은_personas_가_있든_없든_같은_결과다(links_env, monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    off = _think_run(monkeypatch)
    p, loc, _b = _vocab(links_env, personas=PERSONAS)
    _use(monkeypatch, p, loc)
    on = _think_run(monkeypatch)
    assert on == off, "그림자는 고르기 · 답을 바꾸지 않는다"
    recs = [json.loads(x) for f in (links_env / "seat-eval" / "shadow").glob("s-*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
    assert [r["site"] for r in recs] == ["think"] and recs[0]["seated"] == ["mech", "rel"] and recs[0]["types"] == ["failure_mode"]


def test_형제_리포_실파일_대조():
    """포털 어휘표의 personas 줄이 조직도 · types · 게이트웨이 표와 어긋나지 않고, 조직도 도메인마다 줄이 있거나 no_tools 에 있다(design-e5 3.1)."""
    vf = PORTAL / "backend" / "config" / "core_vocab.json"
    org = PORTAL / "frontend" / "src" / "components" / "chat" / "orgTaxonomy.json"
    ta = GATEWAY / "tool_areas.json"
    if not (vf.is_file() and org.is_file() and ta.is_file()):
        pytest.skip("형제 리포(포털 · 게이트웨이)가 없다")
    vocab = json.loads(vf.read_text(encoding="utf-8"))
    if "personas" not in vocab:
        pytest.skip("포털 어휘표에 아직 personas 칸이 없다(E5-4 포털 몫)")
    tad = json.loads(ta.read_text(encoding="utf-8"))
    o = json.loads(org.read_text(encoding="utf-8"))
    knowledge = {d for c in o["categories"] if c.get("root") == "knowledge" for d in c.get("domains") or []} | {"sw"}
    v = pl.validate(vocab["personas"], types_map=vocab.get("types") or {}, domains_known=frozenset(o["domain_label"]),
                    areas_known=frozenset(x["key"] for x in tad["areas"]), apps_known=frozenset(tad["apps"]))
    assert v["dropped"] == [], v["dropped"]
    assert not (knowledge - set(v["domains"]) - set(v["no_tools"])), "지식 도메인마다 줄이 있거나 no_tools 에 있다"

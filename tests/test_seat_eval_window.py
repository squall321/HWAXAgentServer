# /seat-eval 창구와 러너(tools/seat_eval.py)가 비밀 · 읽기 전용 · 엔진 함수 그대로 · 기록 · 지표 · 짝 비교를 지키는지(HWAXPortal docs/request-1029 design-e5 E5-1)
"""가짜 추천은 AIDataHub recommend_agents MCP 응답 꼴 그대로다(agents · ranked_total · more_available · coverage · fallback, 항목마다 score ·
desc_match · matched_* · why · low_confidence, 운영자 he-* 가 섞인다). 도구 사전에는 쓰기 도구도 넣어 두고 — 창구가 그것을 부르지 않고 막아 세는지
(blocked · read_only), 엔진이 스스로 받는 사전도 같은 사전인지 본다.

  실행:  .venv/bin/python -m pytest tests/test_seat_eval_window.py -q
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as a  # noqa: E402
import deliberation  # noqa: E402
import seat_probe  # noqa: E402
import thinking  # noqa: E402
from tools import seat_eval as se  # noqa: E402

SECRET = "Bearer hwaxgw_seat_eval_test_token"
POOL = ["mech-drop", "mech-hinge", "he-cad-step", "pcb-warp", "rel-drop", "sw-ui", "he-data-aidh", "sim-drop"]


def _rec_payload(q: str, top_k: int) -> dict:
    """AIDataHub mcp_runtime.recommend_agents 의 응답 꼴 — 순위는 질의와 무관하게 고정, 운영자 둘이 섞인다."""
    order = ["he-cad-step", "mech-drop", "mech-hinge", "he-data-aidh", "pcb-warp", "rel-drop", "sim-drop", "sw-ui"]
    agents = [{"agent_type": k, "name": k, "description": f"{k} 설명", "common_tags": [], "data_types": [],
               "score": round(2.0 - i * 0.1, 4), "matched_records": 3, "matched_sections": 5, "matched_samples": 1,
               "why": "의미검색 …", "desc_match": {"mech-drop": 0.5, "sw-ui": 0.0}.get(k, 0.2), "low_confidence": False,
               "sample_queries": []} for i, k in enumerate(order)]
    return {"query": q, "agents": agents[:top_k], "ranked_total": len(agents), "more_available": top_k < len(agents),
            "query_over_model_limit": None, "relevant_tools": [], "coverage": {"domains": [], "domain_count": 0, "hint": ""},
            "fallback": False}


class _T:
    def __init__(self, name, fn, seen):
        self.name, self._fn, self._seen = name, fn, seen
        self.description = name

    async def ainvoke(self, args):
        self._seen.append(self.name)
        return json.dumps(self._fn(args), ensure_ascii=False)


def _fake_tools(seen: list) -> dict:
    return {
        "recommend_agents": _T("recommend_agents", lambda x: _rec_payload(x["q"], int(x.get("top_k") or 5)), seen),
        "get_agent_session": _T("get_agent_session", lambda x: {"agent_type": x["agent_type"], "name": x["agent_type"],
                                                                  "description": f"{x['agent_type']} 역할", "system_prompt": "",
                                                                  "response_config": {}}, seen),
        "agent_search": _T("agent_search", lambda x: {"hits": ([{"title": "카드", "record_id": "R-1", "snippet": "s", "score": 0.7}]
                                                                if x["agent_type"] != "sw-ui" else []), "refused": False}, seen),
        "list_agents": _T("list_agents", lambda x: [{"agent_type": k, "name": k, "updated_at": "2026-10-30"} for k in POOL], seen),
        "list_agent_domains": _T("list_agent_domains", lambda x: [{"domain": d, "agent_count": 2} for d in ("mech", "pcb", "rel", "sw")],
                                 seen),
        # 쓰기 도구 — 창구가 부르면 안 된다
        "ra_save_report": _T("ra_save_report", lambda x: {"ok": True}, seen),
        "submit_job": _T("submit_job", lambda x: {"ok": True}, seen),
    }


@pytest.fixture
def env(monkeypatch, tmp_path):
    seen: list = []

    async def _tbn(_app, _groups, *_a, **_k):
        return _fake_tools(seen)
    monkeypatch.setenv("SEAT_EVAL_DIR", str(tmp_path / "seat-eval"))
    monkeypatch.setattr(a.app.state, "connections", {"gateway": {"url": "http://gw.test/mcp", "headers": {"Authorization": SECRET}}},
                        raising=False)
    monkeypatch.setattr(a.app.state, "persona_cache", {}, raising=False)
    monkeypatch.setattr(a, "_tools_by_name", _tbn)
    tm = {"stepforge_list_parts": "heax-step_forge", "stepforge_section": "heax-step_forge", "search_records": "ai-data-hub",
          "drop_submit": "smart-twin-mcp"}
    monkeypatch.setattr(a, "_tools_map", lambda: tm)
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": 1.0, "map": tm, "areas": {"stepforge_list_parts": "cad", "stepforge_section": "cad",
                                                                                "search_records": "knowledge", "drop_submit": "sim"},
                                               "area_meta": {"cad": {}, "knowledge": {}, "sim": {}}, "routine_read": None})
    monkeypatch.setattr(seat_probe, "_aidh_health", lambda base: {"version": "0.9", "build": "dev", "embedder": "bge_m3",
                                                                  "embed_slot": {"active": "m3", "model": "bge-m3"}})
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})      # 창을 묻지 않는다(LLM 서버를 두드리지 않는다)
    seat_probe._BUSY["on"] = False
    seat_probe._VER_CACHE.update(at=0.0, ver=None, pool=None)
    return {"seen": seen, "client": TestClient(a.app), "dir": tmp_path / "seat-eval"}


def _post(c, body, auth=SECRET):
    return c.post("/seat-eval", json=body, headers={"Authorization": auth} if auth else {})


def test_비밀이_없거나_다르면_거절한다(env, monkeypatch):
    c = env["client"]
    body = {"items": [{"qid": "A-1", "q": "낙하 크랙"}]}
    assert _post(c, body, auth=None).status_code == 401
    assert _post(c, body, auth="Bearer wrong").status_code == 401
    monkeypatch.setattr(a.app.state, "connections", {"gateway": {"url": "x", "headers": {}}}, raising=False)
    r = _post(c, body)
    assert r.status_code == 503 and "공유 비밀이 없다" in r.json()["error"]
    assert env["seen"] == [], "거절된 요청은 도구를 하나도 부르지 않는다"
    for path in ("/seat-questions",):
        assert c.get(path).status_code == 503
    monkeypatch.setattr(a.app.state, "connections", {"gateway": {"url": "x", "headers": {"Authorization": SECRET}}}, raising=False)
    assert c.get("/seat-questions", headers={"Authorization": SECRET}).status_code == 200
    assert c.get("/seat-questions").status_code == 401 and c.get("/seat-questions", headers={"Authorization": "Bearer x"}).status_code == 401


def test_엔진_함수를_그대로_부르고_읽기_도구만_쓴다(env, monkeypatch):
    calls = {"discover": [], "summon": [], "screen": [], "prefer": []}
    orig = {"d": deliberation._discover, "s": thinking._summon, "sc": thinking._screen_one, "p": a._seat_tool_prefer}

    async def spy_d(tools, q, limit, *k, **kw):
        calls["discover"].append((q, limit))
        return await orig["d"](tools, q, limit, *k, **kw)

    async def spy_s(tools, q, top_k, exclude):
        calls["summon"].append((q, top_k, set(exclude)))
        return await orig["s"](tools, q, top_k, exclude)

    async def spy_sc(tools, seat, q):
        calls["screen"].append((seat["key"], q))
        return await orig["sc"](tools, seat, q)

    def spy_p(names, persona, q, n):
        calls["prefer"].append((persona.get("key"), q, n))
        return orig["p"](names, persona, q, n)
    monkeypatch.setattr(deliberation, "_discover", spy_d)
    monkeypatch.setattr(thinking, "_summon", spy_s)
    monkeypatch.setattr(thinking, "_screen_one", spy_sc)
    monkeypatch.setattr(a, "_seat_tool_prefer", spy_p)
    r = _post(env["client"], {"items": [{"qid": "A-1", "q": "힌지 낙하 크랙", "expert": "mech-drop"}],
                              "layers": ["rec", "discover", "screen", "tools"], "run": "t1"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["layers"] == ["rec", "discover", "summon", "screen", "tools"], "screen 은 summon 을 데려온다"
    assert calls["discover"] == [("힌지 낙하 크랙", deliberation.N_PERSONAS)]
    assert calls["summon"] == [("힌지 낙하 크랙", thinking.CANDIDATES, set())]
    assert [k for k, _q in calls["screen"]] == ["mech-drop", "mech-hinge", "pcb-warp", "rel-drop", "sim-drop", "sw-ui"]
    assert calls["prefer"] == [("mech-drop", "힌지 낙하 크랙", a._PERSONA_PREFER_N)]
    assert set(env["seen"]) <= seat_probe.READ_TOOLS and "ra_save_report" not in env["seen"] and "submit_job" not in env["seen"]
    assert set(out["calls"]) <= seat_probe.READ_TOOLS and out["read_only"] is True
    it = out["items"][0]
    # 운영자를 섞은 순위 40 그대로 — 운영자 표지와 신호
    rec = it["rec"]
    assert [x["key"] for x in rec["ranked"]][:3] == ["he-cad-step", "mech-drop", "mech-hinge"] and rec["ranked"][0]["op"] is True
    assert rec["ranked"][1]["desc_match"] == 0.5 and rec["ranked_total"] == 8 and rec["low_confidence"] is False
    # 심의 첫 좌석 — _discover 는 top_k 를 넘기지 않아 추천 기본 5명을 받은 뒤 운영자를 거른다. 운영자 둘이 섞이면 3석이다(design-e5 1.4
    # '모자란 좌석' — 창구는 고치지 않고 그대로 잰다). 직접 부른 엔진 함수와 같다.
    want = asyncio.run(orig["d"](_fake_tools([]), "힌지 낙하 크랙", deliberation.N_PERSONAS))
    assert it["discover"]["seats"] == [s["key"] for s in want] == ["mech-drop", "mech-hinge", "pcb-warp"]
    assert it["discover"]["want"] == deliberation.N_PERSONAS == 5
    assert it["guard"] == {"operators": 0, "unknown": 0}
    sc = it["screen"]
    assert sc["asked"] == ["mech-drop", "mech-hinge", "pcb-warp", "rel-drop", "sim-drop"][:thinking.MAX_ANSWERS]
    assert next(x for x in sc["seats"] if x["key"] == "sw-ui")["passed"] is False, "근거도 어휘도 없는 좌석은 예심에서 빠진다"
    assert "knowledge" not in json.dumps(sc), "카드 발췌는 싣지 않는다 — 신호만"
    assert it["tools"]["expert"] == "mech-drop" and isinstance(it["tools"]["prefer"], list)
    ver = out["version"]
    assert ver["pool"]["n"] == len(POOL) and ver["pool"]["operators"] == 2 and len(ver["pool"]["fp"]) == 12
    assert ver["pool"]["updated"] is True, "갱신 시각까지 지문에 든다(list_agents 전체 꼴)"
    assert ver["aidh"]["embed_slot"] == {"active": "m3", "model": "bge-m3"} and ver["tools_map"]["n"] == 4
    assert ver["knobs"]["DELIB_PERSONAS"] == deliberation.N_PERSONAS
    # 기록 — 박스 자리에, 질의 글 없이, 0600
    f = env["dir"] / "runs" / "t1.jsonl"
    body = f.read_text(encoding="utf-8")
    assert '"qid": "A-1"' in body and "힌지 낙하 크랙" not in body
    assert oct(f.stat().st_mode & 0o777) == "0o600" and (env["dir"] / "runs" / "t1.version.jsonl").is_file()


def test_읽기_전용_사전은_읽기_도구만_부르고_그_밖은_막아_센다():
    seen, calls, blocked = [], [], []
    ro = seat_probe.read_only(_fake_tools(seen), calls, blocked)
    assert set(ro) == set(_fake_tools([])), "모든 도구를 감싼다 — 쓰기 도구를 부르려 한 일이 보이게"
    out = asyncio.run(deliberation._call(ro, "ra_save_report", {"x": 1}))
    assert out.startswith("(tool ra_save_report error") and seen == [] and calls == [] and blocked == ["ra_save_report"]
    asyncio.run(deliberation._call(ro, "list_agents", {}))
    assert calls == ["list_agents"] and seen == ["list_agents"] and blocked == ["ra_save_report"]


def test_꼴이_틀리면_422_도는_중이면_409_LLM_겹은_심의가_없을_때만(env, monkeypatch):
    c = env["client"]
    for bad in ({"items": []}, {"items": [{"qid": "x", "q": "q", "who": 1}]}, {"items": [{"qid": "x", "q": "q"}], "layers": ["nope"]},
                {"items": [{"qid": "x", "q": "q"}], "extra": 1}, {"items": [{"qid": "x", "q": "q"}, {"qid": "x", "q": "q"}]},
                {"items": [{"qid": "x", "q": "q"}], "concurrency": 9}):
        assert _post(c, bad).status_code == 422, bad
    seat_probe._BUSY["on"] = True
    try:
        assert _post(c, {"items": [{"qid": "x", "q": "q"}]}).status_code == 409
    finally:
        seat_probe._BUSY["on"] = False
    monkeypatch.setattr(a, "_delib_load", lambda: (1, 0))
    r = _post(c, {"items": [{"qid": "x", "q": "q"}], "layers": ["judge"]})
    assert r.status_code == 409 and "심의가 도는 중" in r.json()["error"]
    assert _post(c, {"items": [{"qid": "x", "q": "q"}], "layers": ["rec"]}).status_code == 200, "결정적 겹은 심의 중에도 잰다"
    assert env["seen"].count("recommend_agents") == 1


def test_LLM_겹은_엔진_함수_그대로_본심_반대_좌석_축(env, monkeypatch):
    from types import SimpleNamespace

    class _LLM:
        async def ainvoke(self, msgs):
            human = msgs[-1][1]
            if "심의 좌석 편성자" in msgs[0][1]:
                out = "pcb | 보드 휨 영향\nzzz | 없는 도메인\nrel | 낙하 신뢰성"
            elif "원인 축을 3개" in human:
                out = "보드 휨\n소프트웨어 반응"
            elif "[당신]\nmech-drop" in human:
                out = json.dumps({"verdict": "answer", "scope": "낙하", "answer": "힌지 보스 응력이 원인으로 보인다 " * 3, "basis": []},
                                 ensure_ascii=False)
            else:
                out = json.dumps({"verdict": "pass", "scope": "", "refer": ["보드 휨"]}, ensure_ascii=False)
            return SimpleNamespace(content=out, response_metadata={})
    monkeypatch.setattr(a.app.state, "llm", _LLM(), raising=False)
    monkeypatch.setattr(a.app.state, "delib_llm", None, raising=False)
    r = _post(env["client"], {"items": [{"qid": "L-1", "q": "힌지 낙하 크랙"}], "layers": ["judge", "counter", "axes"]})
    assert r.status_code == 200, r.text
    it = r.json()["items"][0]
    assert "error" not in it, it
    assert r.json()["layers"] == ["discover", "summon", "screen", "judge", "counter", "axes"]
    assert it["judge"]["answered"] == ["mech-drop"] and it["judge"]["no_answer"] is False
    assert {s["key"]: s["verdict"] for s in it["judge"]["seats"]}["mech-hinge"] == "pass"
    assert [x["domain"] for x in it["axes"]["axes"]] == ["pcb", "rel"], "분류에 없는 도메인은 _seat_axes 가 버린다"
    assert all(len(x["seats"]) <= 3 and not any(k.startswith("he-") for k in x["seats"]) for x in it["axes"]["axes"])
    assert isinstance(it["counter"]["seats"], list) and it["counter"]["limit"] == deliberation._COUNTER_SEATS
    assert set(r.json()["calls"]) <= seat_probe.READ_TOOLS


def test_health_에_창구와_모으기_칸(env):
    h = env["client"].get("/health").json()
    assert h["seat_eval"] == {"secret": True, "busy": False}
    assert h["seat_questions"]["collect"] is False and h["seat_questions"]["days"] == 60


# ── 러너 ─────────────────────────────────────────────────────────────────────
def test_McNemar_는_정확_이항과_연속성_보정_카이제곱():
    assert se.mcnemar(0, 0) == 1.0
    assert abs(se.mcnemar(0, 10) - 2 / 1024) < 1e-9
    assert abs(se.mcnemar(5, 20) - 0.005110) < 1e-4, se.mcnemar(5, 20)     # χ² = 14² / 25 = 7.84
    assert se.mcnemar(10, 10) == 1.0


def test_지표_갈래_짝_비교():
    rows = [{"qid": "q1", "answers": ["mech-drop"], "lenient": ["mech-drop", "mech-hinge"], "kind": "owned", "confuser": "mech-hinge",
             "split": "test"},
            {"qid": "q2", "answers": ["sw-ui"], "lenient": ["sw-ui"], "kind": "owned", "confuser": "mech-drop", "split": "dev"},
            {"qid": "n1", "answers": [], "lenient": [], "kind": "none", "none": "out_of_pool"}]

    def res(order, low=False, seats=None):
        return {"rec": {"ranked": [{"key": k, "op": k.startswith("he-"), "desc_match": 0.5 if k == "mech-drop" else 0.1}
                                   for k in order], "low_confidence": low},
                "discover": {"seats": seats or [k for k in order if not k.startswith("he-")][:5], "want": 5}}
    off = {"q1": res(["he-x", "mech-hinge", "mech-drop"]), "q2": res(["mech-drop", "sw-ui"]), "n1": res(["mech-drop"], low=False)}
    on = {"q1": res(["mech-drop", "mech-hinge"]), "q2": res(["mech-drop", "sw-ui"]), "n1": res(["mech-drop"], low=True)}
    m = se.item_metrics(rows[0], off["q1"])
    assert (m["acc1_strict"], m["acc3_strict"], m["confused"], m["acc1_lenient"]) == (False, True, True, True)
    assert m["rr10_strict"] == 0.5 and m["discover_hit5"] is True and m["short"] is True
    s = se.summarize("A", rows, off)
    assert s["groups"]["all"]["acc1_strict"] == 0.0 and "branch:sw" in s["groups"]
    assert not any(g.startswith("split:") for g in s["groups"]), "A 의 dev · test 는 묶음을 나눠 낸다(A:dev · A:test — report)"
    assert s["groups"]["band:hi"]["n"] == 1, "정답 desc_match 0.40 이상"
    assert s["none"]["low_conf"]["recall"] == 0.0
    s_on = se.summarize("A", rows, on)
    assert s_on["none"]["low_conf"] == {"n": 3, "pos": 1, "precision": 1.0, "recall": 1.0, "f05": 1.0, "wrong_none_owned": 0.0}
    p = se.paired(rows, off, on)
    assert p["acc1_strict"]["b"] == 0 and p["acc1_strict"]["c"] == 1 and p["acc1_strict"]["diff"] == 0.5
    assert p["confused"]["off"] == 1.0 and p["confused"]["on"] == 0.5
    v0 = [{"at": "2026-10-31T10:00", "pool": {"fp": "a"}, "aidh": {"embed_slot": {"active": "m3"}}}]
    assert se.same_version(v0, v0) == []
    assert se.same_version(v0, [{"at": "2026-11-01T10:00", "pool": {"fp": "b"}, "aidh": {"embed_slot": {"active": "m3"}}}]) == ["pool", "day"]


def test_N3_도메인_지우기와_두_사람_일치():
    rows = [{"qid": "q1", "answers": ["mech-drop"], "lenient": ["mech-drop"], "kind": "owned"}]
    res = {"q1": {"rec": {"ranked": [{"key": "mech-drop", "desc_match": 0.6}, {"key": "mech-hinge", "desc_match": 0.5},
                                     {"key": "pcb-warp", "desc_match": 0.45}, {"key": "sw-ui", "desc_match": 0.1}]}}}
    assert se.domain_removed(rows, res, [])["none_rate"] == 0.0, "pcb-warp 0.45 가 남는다"
    assert se.domain_removed(rows, res, [{"a": "mech-drop", "b": "pcb-warp", "rel": "sibling"}])["none_rate"] == 1.0
    h = [{"qid": "h1", "src": "persona", "answers": ["mech-drop"], "lenient": ["mech-drop"], "kind": "owned", "none": None,
          "picked": ["mech-drop"], "others": [{"primary": ["mech-drop"], "by": "b"}]},
         {"qid": "h2", "src": "thinking", "answers": [], "lenient": [], "kind": "none", "none": "core_data", "picked": [],
          "others": [{"primary": [], "none": "core_data", "by": "b"}]}]
    ag = se.agreement(h)
    assert ag == {"n": 2, "primary_n": 1, "primary_overlap": 1.0, "none_n": 1, "none_agree": 1.0, "domain_kappa": 1.0}, \
        "둘이 다 맞게 단 두 짝 — 없음/없음 짝은 주 전문가 겹침의 분모가 아니다"
    assert se.picked_agreement(h) == {"n": 1, "picked_in_label": 1.0}


def test_러너는_MCP_설정의_비밀로_창구를_부르고_결과를_질문_글_없이_둔다(env, monkeypatch, tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"gateway": {"url": "x", "headers": {"Authorization": SECRET}}}), encoding="utf-8")
    monkeypatch.setenv("MCP_CONFIG", str(cfg))
    auth = se.auth_header()
    assert auth == SECRET
    c = env["client"]
    monkeypatch.setattr(se, "call", lambda url, au, body, timeout=900.0: _post(c, body, auth=au).json())
    sets = tmp_path / "sets"
    sets.mkdir()
    (sets / "A.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [
        {"qid": "A-1", "q": "힌지 낙하 크랙 원인", "answer": "mech-drop", "answers_ok": ["mech-drop"], "confuser": "mech-hinge",
         "split": "test", "domain": "mech", "branch": "hw"},
        {"qid": "A-2", "q": "UI 버벅임 원인", "answer": "sw-ui", "answers_ok": ["sw-ui"], "confuser": "mech-drop", "split": "dev",
         "domain": "sw", "branch": "sw"}]), encoding="utf-8")
    (sets / "P.jsonl").write_text(json.dumps({"qid": "P-1", "q": "기구 낙하 해석", "answers_ok": ["mech-drop"], "answer": "mech-drop",
                                              "author": "pcb-warp", "domain": "mech", "branch": "hw"}, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
    out = tmp_path / "run"
    assert se.main(["run", "--sets", str(sets), "--only", "A,P", "--run", "smoke", "--out", str(out), "--batch", "1",
                    "--split", "all"]) == 0
    body = (out / "A.results.jsonl").read_text(encoding="utf-8")
    assert "힌지 낙하 크랙" not in body and '"qid": "A-1"' in body
    rep = json.loads((out / "report.json").read_text(encoding="utf-8"))
    t, d = rep["sets"]["A:test"]["groups"]["all"], rep["sets"]["A:dev"]["groups"]["all"]
    assert (t["n"], t["acc1_strict"], d["n"], d["acc1_strict"]) == (1, 1.0, 1, 0.0), (t, d)     # mech-drop 은 1위(운영자 뺌), sw-ui 는 7위
    assert rep["sets"]["P"]["groups"]["all"]["summon_recall"] == 1.0, "위임 홉 top_k 3 안"
    assert json.dumps(rep, ensure_ascii=False).count("힌지") == 0, "보고에는 숫자만"
    assert (env["dir"] / "runs" / "smoke-A.jsonl").is_file(), "창구도 박스 자리에 남긴다"
    assert os.path.exists(out / "A.version.json")


# ── 반대 검토(10-31) — 짚인 것마다 하나씩(HWAXPortal docs/request-1029 context-notes C-48) ─────────────────────────────
def _a_rows(n_dev: int, n_test: int) -> list[dict]:
    """A 꼴 줄 — 정답 mech-a<i>(갈래 hw), 혼동 상대 mech-c<i>. split 은 앞 n_dev 줄이 dev."""
    out = []
    for i in range(n_dev + n_test):
        out.append({"qid": f"A-{i:03d}", "answers": [f"mech-a{i}"], "lenient": [f"mech-a{i}"], "kind": "owned",
                    "confuser": f"mech-c{i}", "split": "dev" if i < n_dev else "test"})
    return out


def _res(order: list[str], desc: dict | None = None) -> dict:
    return {"rec": {"ranked": [{"key": k, "op": False, "desc_match": (desc or {}).get(k, 0.1)} for k in order],
                    "low_confidence": False}}


def _write_run(d: Path, name: str, results: dict, versions: list, sets: Path | None, split: str = "test") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "run.json").write_text(json.dumps({"run": d.name, "sets": str(sets) if sets else None, "by": None, "split": split}),
                                encoding="utf-8")
    (d / f"{name}.results.jsonl").write_text("".join(json.dumps({"qid": q, **r}) + "\n" for q, r in results.items()),
                                             encoding="utf-8")
    (d / f"{name}.version.json").write_text(json.dumps(versions), encoding="utf-8")


def _write_sets(d: Path, rows: list[dict]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "A.jsonl").write_text("".join(json.dumps({"qid": r["qid"], "q": "질의", "answer": r["answers"][0],
                                                   "answers_ok": r["answers"], "confuser": r["confuser"], "split": r["split"],
                                                   "domain": "mech", "branch": "hw"}) + "\n" for r in rows), encoding="utf-8")
    return d


def test_R9_McNemar_는_b_와_c_가_같으면_1이다():
    assert se.mcnemar(13, 13) == 1.0 and se.mcnemar(30, 30) == 1.0, "연속성 보정은 0 아래로 내려가지 않는다"
    assert abs(se.mcnemar(5, 20) - 0.005110) < 1e-4 and se.mcnemar(12, 13) == 1.0


def test_R8_두_사람_일치는_없음_짝을_따로_세고_고른_전문가는_지목_출처만_없음도_어긋남으로():
    h = [{"qid": "h1", "answers": ["mech-drop"], "kind": "owned", "none": None, "others": [{"primary": ["mech-drop"], "by": "b"}]},
         {"qid": "h2", "answers": [], "kind": "none", "none": "core_data", "others": [{"primary": [], "none": "core_data", "by": "b"}]},
         {"qid": "h3", "answers": ["pcb-warp"], "kind": "owned", "none": None, "others": [{"primary": [], "none": "out_of_pool", "by": "b"}]}]
    ag = se.agreement(h)
    assert ag["n"] == 3 and ag["primary_n"] == 1 and ag["primary_overlap"] == 1.0, "주 전문가 겹침은 둘 다 주 전문가를 단 짝에서만"
    assert ag["none_n"] == 2 and ag["none_agree"] == 0.5, "'없음' 은 따로 — 둘 다 없음이면 맞음, 한쪽만이면 어긋남"
    rows = [{"qid": "p1", "src": "persona", "kind": "owned", "picked": ["a-1"], "lenient": ["a-1"]},
            {"qid": "p2", "src": "persona", "kind": "none", "picked": ["a-2"], "lenient": []},
            {"qid": "d1", "src": "delib", "kind": "owned", "picked": ["a-3"], "lenient": ["a-3"]}]
    assert se.picked_agreement(rows) == {"n": 2, "picked_in_label": 0.5}, \
        "지목 출처만(design-e5 2.4) · 표시가 '없음' 이면 어긋남이다"


def test_R1_A_는_dev_와_test_를_섞지_않는다(tmp_path, monkeypatch):
    rows = _a_rows(50, 50)
    off = {r["qid"]: _res([r["confuser"], "x-1", r["answers"][0]]) for r in rows}
    # 켠 쪽은 dev 정답만 1위로 올린다(dev 로 손본 연결이 dev 에서만 나아진 꼴)
    on = {r["qid"]: (_res([r["answers"][0], r["confuser"]]) if r["split"] == "dev" else off[r["qid"]]) for r in rows}
    sets = _write_sets(tmp_path / "sets", rows)
    v = [{"at": "2026-10-31T10:00", "pool": {"fp": "a"}}]
    _write_run(tmp_path / "off", "A", off, v, sets, split="all")
    _write_run(tmp_path / "on", "A", on, v, sets, split="all")
    cmp_ = se.compare(tmp_path / "off", tmp_path / "on")
    assert cmp_["split"] == "test" and list(cmp_["sets"]) == ["A:test"], "기본은 A-test 다(관문 2 · 3)"
    t = cmp_["sets"]["A:test"]["paired"]
    assert t["acc1_strict"]["n"] == 50 and t["acc1_strict"]["b"] == 0 and t["acc1_strict"]["c"] == 0
    d = se.compare(tmp_path / "off", tmp_path / "on", split="dev")["sets"]["A:dev"]["paired"]
    assert d["acc1_strict"]["c"] == 50 and d["confused"]["b"] == 50
    groups = cmp_["sets"]["A:test"]["groups"]
    assert "branch:hw" in groups and "band:lo" in groups, "갈래 · 어휘 칸마다 짝 비교(관문 3)"
    rep = se.report(tmp_path / "on")
    assert set(rep["sets"]) == {"A:dev", "A:test"} and "all" in rep["sets"]["A:test"]["groups"]
    assert rep["sets"]["A:test"]["n"] == 50 and rep["sets"]["A:dev"]["groups"]["all"]["acc1_strict"] == 1.0
    assert not any(g.startswith("split:") for g in rep["sets"]["A:test"]["groups"]), "한 묶음 안에 dev · test 가 섞이지 않는다"
    # 손보기 실행(--split dev)은 dev 만 보내고 dev 만 낸다
    sent = []
    monkeypatch.setattr(se, "call", lambda url, au, body, timeout=900.0: (sent.extend(i["qid"] for i in body["items"]) or
                                                                          {"read_only": True, "items": [], "calls": {}, "blocked": {}}))
    monkeypatch.setattr(se, "auth_header", lambda path=None: "Bearer x")
    out = tmp_path / "tune"
    assert se.main(["run", "--sets", str(sets), "--only", "A", "--split", "dev", "--out", str(out), "--batch", "50"]) == 0
    assert len(sent) == 50 and all(q in {r["qid"] for r in rows if r["split"] == "dev"} for q in sent)
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))["split"] == "dev"
    assert set(json.loads((out / "report.json").read_text(encoding="utf-8"))["sets"]) <= {"A:dev"}, "dev 실행은 A-test 숫자를 내지 않는다"


def test_R3_도구_실패는_겹마다_적고_러너는_오류로_세어_분모에서_뺀다(env, monkeypatch):
    class _Slow:
        name, description = "recommend_agents", "x"

        async def ainvoke(self, args):
            raise TimeoutError("AIDataHub 응답 없음")

    async def _tbn(_app, _groups, *_a, **_k):
        t = _fake_tools(env["seen"])
        t["recommend_agents"] = _Slow()
        return t
    monkeypatch.setattr(a, "_tools_by_name", _tbn)
    r = _post(env["client"], {"items": [{"qid": "A-1", "q": "힌지 낙하 크랙"}], "layers": ["rec", "discover", "summon", "screen"]})
    assert r.status_code == 200, r.text
    it = r.json()["items"][0]
    for lay in ("rec", "discover", "summon"):
        assert it[lay].get("tool_error"), (lay, it[lay])
    row = {"qid": "A-1", "answers": ["mech-drop"], "lenient": ["mech-drop"], "kind": "owned", "confuser": "mech-hinge", "split": "test"}
    m = se.item_metrics(row, it)
    assert not ({"acc1_strict", "discover_hit5", "short", "summon_recall", "screen_pass", "asked_hit"} & set(m)), m
    s = se.summarize("A", [row], {"A-1": it})
    assert s["guard"]["errors"] == 1 and s["dropped"]["queries"] == 1
    # AIDataHub 의 빈 폴백(fallback true)도 실패다
    fb = {"rec": {"ranked": [], "fallback": True, "low_confidence": False}}
    assert "rec" in se.failed_layers(fb) and "acc1_strict" not in se.item_metrics(row, fb)
    # 짝 비교에서도 빠진다 — 끈 쪽이 실패한 질의는 그 지표의 분모에 없다
    ok = _res(["mech-drop"])
    assert se.paired([row], {"A-1": fb}, {"A-1": ok}).get("acc1_strict") is None


def test_R4_어휘_칸은_한_번_정해_양쪽이_같은_칸을_쓴다(tmp_path):
    rows = [{"qid": "q1", "answers": ["mech-a"], "lenient": ["mech-a"], "kind": "owned", "split": "test"},
            {"qid": "q2", "answers": ["mech-b"], "lenient": ["mech-b"], "kind": "owned", "split": "test"}]
    off = {"q1": _res(["mech-a"], {"mech-a": 0.5}), "q2": _res(["x-1", "x-2"])}                    # q2 의 정답은 40 밖
    on = {"q1": _res(["mech-a"], {"mech-a": 0.5}), "q2": _res(["x-1", "x-2", "x-3", "mech-b"], {"mech-b": 0.5})}
    g = se.paired_groups("A", rows, off, on, {})
    assert g["band:hi"]["acc1_strict"]["n"] == 1, "q2 는 끈 쪽 칸(lo)에 머문다 — 켠 쪽이 40 안으로 올려도 칸이 바뀌지 않는다"
    assert g["band:lo"]["hit5_strict"]["c"] == 1 and g["band:lo"]["acc1_strict"]["b"] == 0
    # 평가셋에 적어 둔 칸(bands.json)이 있으면 그것을 쓴다 — report 도 같은 칸
    sets = _write_sets(tmp_path / "sets", [{**r, "confuser": "x-9"} for r in rows])
    v = [{"at": "2026-10-31T10:00"}]
    _write_run(tmp_path / "off", "A", off, v, sets)
    _write_run(tmp_path / "on", "A", on, v, sets)
    assert se.main(["bands", str(tmp_path / "off")]) == 0
    assert json.loads((sets / "bands.json").read_text(encoding="utf-8")) == {"q1": "hi", "q2": "lo"}
    assert se.main(["bands", str(tmp_path / "on")]) == 0
    assert json.loads((sets / "bands.json").read_text(encoding="utf-8"))["q2"] == "lo", "한 번 정한 칸은 다시 쓰지 않는다"
    rep = se.report(tmp_path / "on")
    assert rep["sets"]["A:test"]["groups"]["band:lo"]["n"] == 1 and rep["sets"]["A:test"]["groups"]["band:hi"]["n"] == 1


def test_R4_A_머리_숫자는_H_의_칸_비율로_무게를_맞춘다():
    groups = {"band:hi": {"n": 10, "acc1_strict": 0.8, "hit5_strict": 1.0}, "band:lo": {"n": 30, "acc1_strict": 0.2, "hit5_strict": 0.5}}
    w = se.weighted(groups, {"hi": 0.25, "lo": 0.75})
    assert w["acc1_strict"] == 0.35 and w["hit5_strict"] == 0.625
    h_rows = [{"qid": f"h{i}", "answers": ["a-1"], "kind": "owned"} for i in range(4)] + [{"qid": "n1", "answers": [], "kind": "none"}]
    res = {f"h{i}": _res(["a-1"], {"a-1": 0.5 if i == 0 else 0.1}) for i in range(4)}
    assert se.band_shares(h_rows, res, {}) == {"hi": 0.25, "lo": 0.75, "n": 4}


def test_R5_판_지문은_묶음마다_견주고_AIDataHub_커밋과_추천_코드도_본다():
    base = {"at": "2026-10-31T10:00", "pool": {"fp": "a"}, "aidh": {"version": "0.7.0", "repo": "aaaa", "recommend_svc": "r1"}}
    later = {**base, "pool": {"fp": "b"}}
    assert se.drift([base, base, later]) == ["pool"], "한 실행 안에서 바뀐 칸"
    got = se.same_version([base, base], [base, later])
    assert "on:pool" in got and "pool" in got, got
    assert "aidh.repo" in se.same_version([base], [{**base, "aidh": {**base["aidh"], "repo": "bbbb"}}])
    assert "aidh.recommend_svc" in se.same_version([base], [{**base, "aidh": {**base["aidh"], "recommend_svc": "r2"}}])
    assert se.same_version([base, base], [base]) == []


def test_R5_보고는_묶음_지문을_모두_두고_실행_안의_바뀜을_알린다(tmp_path):
    rows = _a_rows(0, 2)
    sets = _write_sets(tmp_path / "sets", rows)
    v0 = {"at": "2026-10-31T10:00", "pool": {"fp": "a"}, "aidh": {"repo": "aaaa"}}
    _write_run(tmp_path / "r", "A", {r["qid"]: _res([r["answers"][0]]) for r in rows}, [v0, {**v0, "pool": {"fp": "b"}}], sets)
    rep = se.report(tmp_path / "r")
    assert len(rep["version"]["A"]) == 2 and rep["drift"]["A"] == ["pool"]


def test_R5_창구의_판_지문에_AIDataHub_추천_코드_지문(env, monkeypatch, tmp_path):
    svc = tmp_path / "AIDataHub" / "api_server" / "src" / "api" / "services" / "recommend_svc.py"
    svc.parent.mkdir(parents=True)
    svc.write_text("# 지어낸 점수식\nW = 2.0\n", encoding="utf-8")
    monkeypatch.setattr(seat_probe, "ROOT", tmp_path / "HWAXAgentServer")        # 형제 리포 자리를 임시로
    r = _post(env["client"], {"items": [{"qid": "A-1", "q": "힌지 낙하 크랙"}], "layers": ["rec"]})
    aidh = r.json()["version"]["aidh"]
    assert "repo" in aidh and aidh["recommend_svc"] == seat_probe.sha12(svc.read_bytes()), aidh


def test_R7_띵킹_지연은_그_질의의_첫_추천_호출로_잰다(env, monkeypatch):
    order = []

    async def _tbn(_app, _groups, *_a, **_k):
        t = _fake_tools(env["seen"])
        rec = t["recommend_agents"]

        class _Rec:
            name, description = "recommend_agents", "x"

            async def ainvoke(self, args):
                order.append(int(args.get("top_k") or 5))
                return await rec.ainvoke(args)
        t["recommend_agents"] = _Rec()
        return t
    monkeypatch.setattr(a, "_tools_by_name", _tbn)
    body = {"items": [{"qid": "A-1", "q": "처음 보는 질의 하나"}], "layers": ["rec", "discover", "summon", "screen"]}
    it = _post(env["client"], body).json()["items"][0]
    assert order[0] == thinking.CANDIDATES, f"소집이 그 질의의 첫 추천 호출이어야 한다 — 차례 {order}"
    assert it["summon"]["first"] is True
    it2 = _post(env["client"], body).json()["items"][0]
    assert it2["summon"]["first"] is False, "이 창구가 이미 보낸 질의는 첫 호출이 아니다"
    row = {"qid": "A-1", "answers": ["mech-drop"], "lenient": ["mech-drop"], "kind": "owned", "split": "test"}
    s = se.summarize("A", [row], {"A-1": it})
    assert s["think_ms"]["first"]["n"] == 1 and s["think_ms"]["repeat"] is None


def test_R11_읽기_전용은_막은_이름이_0일_때만이고_엔진이_따로_받는_도구도_같은_사전이다(env, monkeypatch):
    monkeypatch.setattr(a, "_tools_by_name", deliberation._tools_by_name)       # 진짜 함수 — _persona_meta 도 이것을 부른다

    async def _load(_scoped):
        return list(_fake_tools(env["seen"]).values())
    monkeypatch.setattr(deliberation, "_load_tools", _load)
    r = _post(env["client"], {"items": [{"qid": "T-1", "q": "", "expert": "mech-drop"}], "layers": ["tools"]})
    out = r.json()
    assert r.status_code == 200 and out["read_only"] is True and out["blocked"] == {}, out
    assert out["calls"].get("get_agent_session") == 1, f"_persona_meta 의 조회도 같은 읽기 전용 사전을 지난다 — {out['calls']}"

    async def _sneaky(app_, groups, key):
        tools = await a._tools_by_name(app_, groups, a.CATALOG_RESULT_MAX)
        await deliberation._call(tools, "ra_save_report", {"x": 1})
        return {"key": key, "operator": False, "note": ""}
    monkeypatch.setattr(a, "_persona_meta", _sneaky)
    a.app.state.persona_cache = {}
    out = _post(env["client"], {"items": [{"qid": "T-2", "q": "", "expert": "mech-drop"}], "layers": ["tools"]}).json()
    assert out["read_only"] is False and out["blocked"] == {"ra_save_report": 1}, out
    assert "ra_save_report" not in env["seen"], "막은 도구는 부르지 않는다"

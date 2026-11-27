# 목적 → 묶음 → 기능 MCP — 게이트웨이 목적 묶음 표(/tools-map purposes)를 웹 챗 목적 예산 · 좌석 · 위임 · 루틴 그림자가 같은 규칙으로 읽는가(HWAXPortal docs/mcp-modernize M3)
"""왜 — cae00 은 도구가 2,000개 가깝다. 웹 챗은 무캡 길에서 도구 설명에 창의 45% 를 쓰고(Qwen 262K 이면 약 8.8만 토큰 · Claude 1M 이면 약 42만 토큰),
좌석 · 위임 · 루틴은 목적과 상관없이 이름 접두 · 읽기 전부 · 백엔드 전부로 묶는다. 게이트웨이가 목적 묶음 정본 표를 내므로 쓰는 쪽이 그 표로 목적을 고르고
입구 · 단계를 먼저 묶는다. 기본값을 바꾸는 순위 변화라 그림자로 먼저 잰다.

  · 표 읽기 · 목적 고르기는 게이트웨이 search_tools 목적 층과 같은 규칙이다(트리거 조각이 모두 빈칸 뺀 질의에 들면 · 점수 · 덮음).
  · 손잡이를 끄면(off) 묶음이 종전 길과 같고 목적 셈을 하지 않는다. 그림자(shadow — 기본)도 묶음은 종전과 같다.
  · 그림자 셈이 맞다 — 지금 묶음의 수 · 토큰이 실제 묶음과 같고, 목적 예산 수 · 토큰이 on 으로 묶었을 때와 같다. 절단 기록(_TOOL_CUT)을 건드리지 않는다.
  · on 은 입구 · 단계를 먼저 묶고 절대 예산을 지킨다(창의 45% 를 넘지 않는다). 캡 길은 캡을 지킨다.
  · 좌석 그림자는 좌석 도구를 바꾸지 않고 리스크 심사는 셈하지 않는다. 울타리 밖 목적 단계를 센다.
  · 위임 · 루틴 그림자는 묶음 수 · 토큰을 센다.

  진짜 꼴 — tests/fixtures/purposes/tools_map_purposes_1127.json 은 dev 게이트웨이 /tools-map 의 purposes 칸 그대로다(목적 72 · 이 박스에 없는 단계 name null).

  실행:  .venv/bin/python -m pytest tests/test_purpose_budget.py -q
"""
import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402
import offload  # noqa: E402
import purposes as P  # noqa: E402
import routine  # noqa: E402

RAW = json.loads((ROOT / "tests" / "fixtures" / "purposes" / "tools_map_purposes_1127.json").read_text(encoding="utf-8"))["purposes"]
TABLE = P.parse(RAW)


def _tool(name: str, desc: str = "", pad: int = 600):
    return SimpleNamespace(name=name, description=f"{desc} " + "x" * pad, args_schema={"type": "object", "properties": {}})


def _pool(filler: int = 300) -> list:
    """표의 단계 이름(이 박스에 있는 것) 전부 + 무관한 도구 filler 개 + 안내대 — 2,000개 규모를 흉내 낸 줄(게이트웨이 순서는 무관한 것이 앞)."""
    names = list(dict.fromkeys(s["name"] for p in TABLE["items"] for s in p["steps"] if s["name"]))
    guides = [_tool(n, "안내", pad=100) for n in ("search_tools", "invoke_tool", "list_tool_apps")]
    filler_tools = [_tool(f"filler_tool_{i}", f"무관한 도구 {i} 데이터 조회") for i in range(filler)]
    return filler_tools + [_tool(n, f"{n} 도구") for n in names if n not in ("search_tools", "invoke_tool", "list_tool_apps")] + guides


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    saved = dict(app._ctx_cache)
    monkeypatch.setattr(app, "_semantic_order", lambda q, tools: [])          # 오프라인 — 어휘 순위만
    monkeypatch.setattr(app, "_group_of", lambda n: ("", ""))
    monkeypatch.setitem(app._TOOLS_MAP_CACHE, "purposes", TABLE)
    monkeypatch.setattr(app, "CHAT_TOOL_PURPOSE", "shadow")
    monkeypatch.setattr(app, "CHAT_PURPOSE_BUDGET", 0)
    monkeypatch.setattr(app, "CHAT_TOOL_SCHEMA_BUDGET", 0)
    monkeypatch.setattr(app, "_PURPOSE_CHAT", {"turns": 0, "matched": 0, "entry_missing_now": 0, "entry_missing_purpose": 0,
                                               "tokens_now": 0, "tokens_purpose": 0, "errors": 0, "error": None, "last": None})
    monkeypatch.setattr(app, "_SEM_LAST", {"v": None})
    app._TOOL_CUT.clear()
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    app._TOOL_CUT.clear()


def _names(tools) -> list:
    return [t.name for t in tools]


# ── 표 읽기 · 고르기 ────────────────────────────────────────────────────────────
def test_표는_진짜_꼴을_읽고_이_박스에_없는_단계는_이름이_없다():
    assert TABLE and len(TABLE["items"]) == 72 and TABLE["fingerprint"]
    core = next(p for p in TABLE["items"] if p["key"] == "core-object-lookup")
    assert core["entry"] is None, "입구(정본 허브)가 이 박스에 없다"
    assert [s["name"] for s in core["steps"]].count(None) >= 1 and "ontology_lookup" in [s["name"] for s in core["steps"]]
    drp = next(p for p in TABLE["items"] if p["key"] == "drop-prep")
    assert drp["entry"] == "list_projects" and ("낙하", "해석", "준비") in drp["trig"]
    assert P.table_health(TABLE)["live"] < 72 and P.table_health(None) == {"present": False}
    # 꼴이 틀린 목적 하나는 그 목적만 버린다 — 표 전체를 버리지 않는다
    bad = {"items": [{"key": "Bad Key"}, *RAW["items"][:2]], "fingerprint": "x"}
    assert [p["key"] for p in P.parse(bad)["items"]] == [RAW["items"][0]["key"], RAW["items"][1]["key"]]
    assert P.parse(None) is None and P.parse({"items": "x"}) is None


def test_고르기는_게이트웨이_목적_층_규칙이다():
    got = P.match("설계 리스크 심사 진행해줘", TABLE)
    assert [h["key"] for h in got][:1] == ["design-risk-review"]
    # 맞은 조각(합집합) 설계 · 리스크 · 심사 = 7자 / 빈칸 뺀 질의 '설계리스크심사진행해줘' 11자
    assert got[0]["cover"] == round(7 / 11, 3) and got[0]["score"] == len("리스크심사") or got[0]["score"] == len("설계리스크")
    assert got[0]["entry"] == "risk_next_step" and got[0]["names"][0] == "risk_next_step"
    # 도구 하나를 겨눈 긴 문장은 덮음이 작아 목적이 없다(게이트웨이 weights.purpose_cover 0.2)
    assert P.match("SUS304 물성을 표로 정리하고 온도별 탄성계수와 열팽창계수를 함께 비교해 그래프로 그려 보고서에 붙여 줘", TABLE) == []
    # 대소문자 · 빈칸은 접는다
    assert [h["key"] for h in P.match("ABD 적층안", TABLE)][:1] == ["laminate-evaluate"]
    assert P.match("", TABLE) == [] and P.match("설계 리스크", None) == []


def test_시야_밖_단계만_있는_목적은_고르지_않는다():
    drp = next(p for p in TABLE["items"] if p["key"] == "drop-prep")
    seen = {s["name"] for s in drp["steps"] if s["name"]}
    assert "drop-prep" in [h["key"] for h in P.match("낙하 해석 준비", TABLE)]
    assert "drop-prep" not in [h["key"] for h in P.match("낙하 해석 준비", TABLE, avail={"filler"})]
    # 입구가 시야 밖이면 보이는 첫 단계가 입구다(게이트웨이 _purpose_live)
    got = P.match("낙하 해석 준비", TABLE, avail=seen - {"list_projects"})
    assert got[0]["entry"] == got[0]["names"][0] != "list_projects"


def test_뒤이은_발화는_앞_사람_발화로_고른다():
    hits, i = P.match_first(("그럼 다음은?", "설계 리스크 심사 진행해줘"), TABLE)
    assert i == 1 and hits[0]["key"] == "design-risk-review"
    assert P.match_first(("안녕",), TABLE) == ([], -1)


def test_먼저_묶을_이름은_입구_먼저_겹침_없이():
    hits = P.match("지난주 낙하 결과로 보고서 작성해줘", TABLE)
    assert len(hits) == 3
    lead, ents = P.lead(hits), P.entries(hits)
    assert lead[:len(ents)] == ents and len(lead) == len(set(lead))
    assert set(lead) == {n for h in hits for n in h["names"]}


def test_fit_은_fit_schema_와_같은_셈이고_절단_기록을_건드리지_않는다():
    pool = _pool(40)
    kept, tok = P.fit(pool, 5000, app._tool_schema_cost, keep=("search_tools",))
    assert app._TOOL_CUT == {}
    want = app._fit_schema(pool, 5000, keep=("search_tools",))
    assert _names(kept) == _names(want) and tok == sum(app._tool_schema_cost(t) for t in want)


# ── 웹 챗 ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cap", [0, 40])
def test_손잡이를_끄면_종전_길과_같고_목적_셈을_하지_않는다(monkeypatch, cap):
    monkeypatch.setattr(app, "TOOL_MAX", cap)
    app._ctx_cache["n"] = 262_144 - 65_536
    pool, q = _pool(), "SM-S931 낙하 해석 준비해줘"
    plain = _names(app._select_tools(pool, q, _plain=True))
    monkeypatch.setattr(app, "CHAT_TOOL_PURPOSE", "off")
    monkeypatch.setattr(app, "_chat_purpose_hits", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("off 가 목적을 골랐다")))
    assert _names(app._select_tools(pool, q)) == plain
    assert app._PURPOSE_CHAT["turns"] == 0


@pytest.mark.parametrize("cap", [0, 40])
def test_그림자는_묶음을_바꾸지_않고_셈이_맞다(monkeypatch, cap, capsys):
    monkeypatch.setattr(app, "TOOL_MAX", cap)
    app._ctx_cache["n"] = 262_144 - 65_536
    pool, q = _pool(), "SM-S931 낙하 해석 준비해줘"
    plain = _names(app._select_tools(pool, q, _plain=True))
    cut_before = dict(app._TOOL_CUT)
    app._TOOL_CUT.clear()
    got = app._select_tools(pool, q)
    assert _names(got) == plain, "그림자도 묶음은 종전 그대로"
    assert app._TOOL_CUT == cut_before, "그림자 셈이 절단 기록을 바꾸지 않는다(종전 길의 절단만 남는다)"
    rec = app._PURPOSE_CHAT["last"]
    assert rec["purposes"][0]["key"] == "drop-prep" and rec["src"] == "message"
    assert rec["now"] == len(got) and rec["tokens_now"] == sum(app._tool_schema_cost(t) for t in got)
    # 목적 예산 셈은 on 으로 묶은 것과 같다(같은 어휘 순위)
    monkeypatch.setattr(app, "CHAT_TOOL_PURPOSE", "on")
    on = app._select_tools(pool, q)
    assert rec["purpose"] == len(on) and rec["tokens_purpose"] == sum(app._tool_schema_cost(t) for t in on)
    hits = P.match(q, TABLE, {t.name for t in pool})
    ents = P.entries(hits)
    assert rec["entries"] == len(ents) and rec["entries_now"] == sum(1 for n in ents if n in plain)
    assert rec["entries_purpose"] == len(ents) == sum(1 for n in ents if n in _names(on))
    assert app._PURPOSE_CHAT["turns"] == 1 and app._PURPOSE_CHAT["matched"] == 1
    assert "[agent] purpose shadow: 목적 drop-prep" in capsys.readouterr().out


def test_on_은_입구와_단계를_먼저_묶고_절대_예산을_지킨다(monkeypatch):
    monkeypatch.setattr(app, "TOOL_MAX", 0)
    monkeypatch.setattr(app, "CHAT_TOOL_PURPOSE", "on")
    app._ctx_cache["n"] = 936_000                          # Claude 턴 — 창의 45% 는 약 42만 토큰
    pool = _pool(1500)
    q = "설계 리스크 심사 진행해줘"
    got = app._select_tools(pool, q)
    names = _names(got)
    hits = P.match(q, TABLE, {t.name for t in pool})
    assert names[:3] == ["search_tools", "invoke_tool", "list_tool_apps"], "안내대는 맨 앞(접두 캐시)"
    assert set(P.lead(hits)) <= set(names), "목적 입구 · 단계가 다 묶인다"
    # 고정 몫(안내대 → 온톨로지 코어 — 정해진 차례, W-3) 바로 뒤가 목적이다(콕 집은 것이 없을 때)
    front = [n for n in app._GUIDE_FRONT + app._onto_core() if n in {t.name for t in pool}]
    lead = [n for n in P.lead(hits) if n not in front]
    assert names[:len(front)] == front and names[len(front):len(front) + len(lead)] == lead
    assert sum(app._tool_schema_cost(t) for t in got) <= app._purpose_budget() == 40_000
    assert app._TOOL_CUT["budget"] == 40_000, "on 은 진짜 절단이라 기록에 남는다"
    # 콕 집은 것(first)은 목적보다 앞이다
    got2 = _names(app._select_tools(pool, q, first=["filler_tool_7"]))
    assert got2[len(front)] == "filler_tool_7" and got2[len(front) + 1] == lead[0]


def test_목적_예산은_손잡이를_따르고_창의_45퍼센트를_넘지_않는다(monkeypatch):
    app._ctx_cache["n"] = 936_000
    assert app._purpose_budget() == 40_000
    monkeypatch.setattr(app, "CHAT_TOOL_SCHEMA_BUDGET", 30_000)
    assert app._purpose_budget() == 30_000
    monkeypatch.setattr(app, "CHAT_PURPOSE_BUDGET", 25_000)
    assert app._purpose_budget() == 25_000
    app._ctx_cache["n"] = 20_000                          # 작은 창 — 45% 는 9,000
    assert app._purpose_budget() == 9_000


def test_캡_길의_on_은_캡을_지키고_입구는_남긴다(monkeypatch):
    monkeypatch.setattr(app, "TOOL_MAX", 40)
    monkeypatch.setattr(app, "TOOL_SCHEMA_BUDGET", 12_000)
    monkeypatch.setattr(app, "CHAT_TOOL_PURPOSE", "on")
    app._ctx_cache["n"] = 936_000
    pool = _pool()
    for q in ("SM-S931 낙하 해석 준비해줘", "힌지 크랙 불량 원인 규명해줘", "지난주 낙하 결과로 보고서 작성해줘"):
        names = _names(app._select_tools(pool, q))
        assert len(names) <= 40, (q, len(names))
        ents = P.entries(P.match(q, TABLE, {t.name for t in pool}))
        assert set(ents) <= set(names), (q, ents)


def test_목적_표가_없으면_목적이_없고_그림자는_예산만_견준다(monkeypatch):
    monkeypatch.setattr(app, "TOOL_MAX", 0)
    monkeypatch.setitem(app._TOOLS_MAP_CACHE, "purposes", None)
    app._ctx_cache["n"] = 936_000
    got = app._select_tools(_pool(), "설계 리스크 심사 진행해줘")
    rec = app._PURPOSE_CHAT["last"]
    assert rec["purposes"] == [] and rec["now"] == len(got) and rec["tokens_purpose"] <= 40_000
    assert app._PURPOSE_CHAT["matched"] == 0


def test_그림자가_터져도_묶음은_종전대로(monkeypatch):
    monkeypatch.setattr(app, "TOOL_MAX", 0)
    app._ctx_cache["n"] = 262_144 - 65_536
    pool, q = _pool(), "설계 리스크 심사"
    plain = _names(app._select_tools(pool, q, _plain=True))
    monkeypatch.setattr(app, "_select_purpose", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("셈 실패")))
    assert _names(app._select_tools(pool, q)) == plain
    assert app._PURPOSE_CHAT["errors"] == 1 and "셈 실패" in app._PURPOSE_CHAT["error"]


def test_턴의_문맥_변수로_이번_발화를_먼저_본다(monkeypatch):
    monkeypatch.setattr(app, "TOOL_MAX", 0)
    app._ctx_cache["n"] = 262_144 - 65_536

    async def go():
        app._turn_purpose_q.set(("그럼 다음은?", "힌지 크랙 불량 원인 규명해줘"))
        return await asyncio.to_thread(app._select_tools, _pool(), "긴 선별 질의 — 앞 대화와 이번 발화를 이어 붙인 것")
    asyncio.run(go())
    rec = app._PURPOSE_CHAT["last"]
    assert rec["purposes"][0]["key"] == "quality-root-cause" and rec["src"] == "history"


def test_tools_map_이_목적_표를_받고_깨진_표는_지도를_막지_않는다(monkeypatch):
    import urllib.request

    body = {"map": {"risk_next_step": "heax-hwax_risk"}, "areas": {}, "area_meta": [], "apps": [], "purposes": RAW}

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _R(json.dumps(body).encode()))
    monkeypatch.setitem(app._TOOLS_MAP_CACHE, "map", {})
    monkeypatch.setitem(app._TOOLS_MAP_CACHE, "at", 0.0)
    monkeypatch.setitem(app._TOOLS_MAP_CACHE, "retry_at", 0.0)
    app._tools_map()
    assert app._purpose_table()["fingerprint"] == RAW["fingerprint"] and len(app._purpose_table()["items"]) == 72
    body["purposes"] = {"items": [{"key": "ok-key", "steps": "깨짐"}]}
    monkeypatch.setitem(app._TOOLS_MAP_CACHE, "at", 0.0)
    monkeypatch.setattr(app._purposes, "parse", lambda _r: (_ for _ in ()).throw(ValueError("깨진 표")))
    app._tools_map()
    assert app._purpose_table() is None and app._TOOLS_MAP_CACHE["map"] == {"risk_next_step": "heax-hwax_risk"}


def test_health_에_목적_칸이_있다(monkeypatch):
    monkeypatch.setattr(app, "TOOL_MAX", 0)
    app._ctx_cache["n"] = 936_000
    app._select_tools(_pool(), "설계 리스크 심사 진행해줘")
    h = app._purpose_health()
    assert h["table"]["present"] and h["table"]["count"] == 72
    c = h["chat"]
    assert c["mode"] == "shadow" and c["budget"] == 40_000 and c["budget_knob"] == "default" and c["turns"] == 1
    assert c["last"]["purposes"][0]["key"] == "design-risk-review"
    assert h["seat"]["mode"] in ("off", "shadow") and h["offload"]["mode"] in ("off", "shadow") and h["routine"]["mode"] in ("off", "shadow")
    assert "@" not in json.dumps(h, ensure_ascii=False), "무인증 칸 — 사람 · 질의를 싣지 않는다"


# ── 심의 좌석 ──────────────────────────────────────────────────────────────────
def _seat_pool() -> dict:
    """좌석 후보 — 접두 울타리를 지난 것만(진짜 _free_tool_ok + 닫기 목록)."""
    return {t.name: t for t in _pool(100) if t.name.lower() not in d._FREE_DENY and d._free_tool_ok(t.name)}


_PERSONAS = [{"key": "quality", "role": "품질 불량 분석 전문가", "name": "품질"}, {"key": "sim", "role": "낙하 충격 해석 전문가", "name": "해석"}]


def test_좌석_그림자는_좌석_도구를_바꾸지_않고_울타리_밖_단계를_센다(monkeypatch, capsys):
    monkeypatch.setattr(d, "DELIB_SEAT_PURPOSE", "shadow")
    monkeypatch.setattr(d, "_SEAT_PURPOSE", {"rounds": 0, "matched": 0, "skipped": {}, "errors": 0, "error": None, "last": None})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    pool, q = _seat_pool(), "힌지 크랙 불량 원인 규명"
    keys = list(pool)
    now = {p["key"]: list(d._tools_for_seat(pool, p, q, 8000)) for p in _PERSONAS}
    rec = d._seat_purpose_shadow(pool, _PERSONAS, q, "diagnosis", 8000, (), now)
    assert list(pool) == keys and {p["key"]: list(d._tools_for_seat(pool, p, q, 8000)) for p in _PERSONAS} == now, "좌석 도구 · 후보는 그대로"
    assert [(h["key"], h["src"]) for h in rec["purposes"]] == [("quality-root-cause", "question"), ("knowledge-search", "chair")]
    lead = P.lead(P.by_keys(["quality-root-cause", "knowledge-search"], TABLE))
    assert rec["steps"] == len(lead) and rec["in_pool"] == sum(1 for n in lead if n in pool) and rec["fenced"] == rec["steps"] - rec["in_pool"]
    assert rec["fenced"] > 0, "품질 원인 입구(defect_profile)는 좌석 접두 울타리 밖이다(11-27 dev)"
    assert len(rec["seats"]) == 2 and all(s["now"] == len(now[s["key"]]) for s in rec["seats"])
    assert d.seat_purpose_health()["matched"] == 1
    assert "[deliberation] seat purpose shadow: 목적 quality-root-cause(question)" in capsys.readouterr().out


def test_좌석_그림자는_리스크_심사를_셈하지_않고_끄면_아무것도_안_한다(monkeypatch):
    monkeypatch.setattr(d, "_SEAT_PURPOSE", {"rounds": 0, "matched": 0, "skipped": {}, "errors": 0, "error": None, "last": None})
    monkeypatch.setattr(d, "DELIB_SEAT_PURPOSE", "shadow")
    pool = _seat_pool()
    assert d._seat_purpose_shadow(pool, _PERSONAS, "설계 리스크 심사", "risk-review", 8000, (), {}) is None
    assert d.seat_purpose_health()["skipped"] == {"risk-review": 1} and d.seat_purpose_health()["rounds"] == 0
    monkeypatch.setattr(d, "DELIB_SEAT_PURPOSE", "off")
    monkeypatch.setattr(app, "_purpose_table", lambda: (_ for _ in ()).throw(AssertionError("off 가 표를 읽었다")))
    assert d._seat_purpose_shadow(pool, _PERSONAS, "힌지 크랙 불량 원인", "diagnosis", 8000, (), {}) is None


def test_라운드는_1라운드에만_좌석_그림자를_부른다(monkeypatch):
    """엔진 스트림을 실제로 돌린다 — 그림자는 좌석 도구를 정한 뒤 1라운드에 한 번, 그 좌석 · 후보 · 의장 틀로 불린다."""
    import langgraph.prebuilt
    from test_delib_silent_drops import _stream, _Tool  # noqa: PLC0415 — 같은 하네스

    seen = []

    async def _llm(_llm_obj, system, _human, *_img):
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A"], "position_short": "요약",
                           "concede": [], "rebut": [], "deepen": "", "final_position": "최종", "non_negotiable": "", "vote": "진행",
                           "stance": "동의"}, ensure_ascii=False) if "전문가입니다" in system else "결정문 본문"

    async def _gather(_agent, persona, question, *_a, **_k):
        return persona["key"], [], "- 조회 요약", ""
    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: object())
    monkeypatch.setattr(d, "_seat_purpose_shadow", lambda *a, **k: seen.append(a))
    _stream(monkeypatch, {"rounds": 2, "save_report": 0, "rebut_quote": 0, "free_tools": 1, "persona_knowledge": 0,
                          "chair_template": "diagnosis"},
            tools={"agent_search": _Tool("agent_search"), "search_defects": _Tool("search_defects")}, until=lambda ev, _d: ev == "done")
    assert len(seen) == 1, seen
    pool, personas, question, chair, budget, pin, now = seen[0]
    keys = [p["key"] for p in personas]          # 원인규명 틀은 반증 지정석을 저절로 더한다
    assert set(pool) == {"agent_search", "search_defects"} and keys[:2] == ["mech-a", "rel-b"]
    assert question == "힌지 크랙 원인" and chair == "diagnosis" and pin == () and set(now) == set(keys)


# ── 위임 · 루틴 ────────────────────────────────────────────────────────────────
def test_위임_그림자는_묶음_수와_토큰을_센다(monkeypatch):
    monkeypatch.setattr(offload, "PURPOSE_SHADOW", "shadow")
    monkeypatch.setattr(offload, "_PURPOSE", P.Tally())
    app._ctx_cache["n"] = 262_144 - 65_536
    allowed = {t.name: t for t in _pool(600)}
    rec = offload._purpose_shadow("SM-S931 낙하 결과 최악 방향과 파트 위험 정리", allowed)
    assert rec["now"] == len(allowed) and rec["tokens_now"] == sum(app._tool_schema_cost(t) for t in allowed.values())
    assert rec["purposes"][0]["key"] == "drop-result" and rec["tokens_purpose"] <= 40_000 < rec["tokens_now"]
    assert rec["entries_purpose"] == rec["entries"] == 1
    h = offload.purpose_health()
    assert h["runs"] == 1 and h["matched"] == 1 and "purpose_shadow" not in offload.health(), "스위치 칸에는 섞지 않는다"
    monkeypatch.setattr(offload, "PURPOSE_SHADOW", "off")
    assert offload._purpose_shadow("낙하 결과", allowed) is None and offload.purpose_health()["runs"] == 1


def test_루틴_그림자는_단계_제목과_할_일로_고르고_실패는_삼킨다(monkeypatch):
    monkeypatch.setattr(routine, "ROUTINE_PURPOSE", "shadow")
    monkeypatch.setattr(routine, "_PURPOSE", P.Tally())
    app._ctx_cache["n"] = 262_144 - 65_536
    tools = _pool(50)
    rec = routine._purpose_shadow({"id": "s1", "title": "재료 물성 찾기", "goal": "SUS304 물성 값"}, tools)
    assert rec["step"] == "s1" and rec["purposes"][0]["key"] == "material-find" and rec["now"] == len(tools)
    monkeypatch.setattr(app, "_purpose_bind_count", lambda *_a: (_ for _ in ()).throw(RuntimeError("셈 실패")))
    assert routine._purpose_shadow({"id": "s2", "title": "x"}, tools) is None
    assert routine.purpose_health()["errors"] == 1 and routine.purpose_health()["runs"] == 1

# SIREN 을 심의 · 리스크 심사에서 — 외부 연결 읽기의 좌석 자유 조회 · 잡 줄 · 지정 도구 차례 · 쪽 번호 수치 대조(HWAXPortal docs/siren-delib)
"""HWAXPortal docs/siren-delib — 요청서 HA-36 · HA-37 · HA-38 과 RK-12 의 엔진 몫(사용자 결정 11-06 넷).

  · HA-36 — 선언 백엔드(박스 권한표 connect:)의 **확인된** 읽기(게이트웨이 /tools-map 의 panel_read)를 좌석 자유 조회에 들인다. 겹침 접두가 붙은
    것(허브 앱과 두 벌 — 운영 사본)은 뺀다. 리스크 심사 · 리스크 앱 패널에도 들인다(사용자 결정) — 앱이 실은 앱 목록의 좁히기에서도 남긴다.
    기준일 심의는 들이지 않는다(그쪽 데이터를 날짜로 거르지 못한다). 잡 안에서 백엔드마다 동시 호출을 줄 세우고, 429 뒤엔 그 잡에서 안 부른다.
  · HA-37 — 지정 도구 차례를 원래 이름 · 앱 접두를 뗀 꼴로 본다(siren_search_cases 가 siren_get_case 보다 먼저).
  · HA-38 — 천 단위 쉼표는 뒤 묶음이 모두 세 자리일 때만 수 하나다(쪽 번호 p.2,3,10,11,23 을 3101123 으로 읽지 않는다).
  · RK-12 엔진 몫 — 지정 도구가 외부 연결에서 미연결(등록 토큰 없음 · 401) · 한도 초과(429)로 끝나면 다시 시키지 않고 경고 사건(conn_unregistered ·
    conn_rate_limited)으로 낸다(리스크 앱 러너가 그 code 로 패널에 남긴다). 외부 연결 결과에는 trust 안내 한 줄(_conn_note)이 붙는다.

  실행:  .venv/bin/python -m pytest tests/test_siren_delib.py -q
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402
import deliberation as d  # noqa: E402
from evidence import sig_numbers, unsourced_numbers  # noqa: E402

from test_delib_silent_drops import _pin_context, _stream, _Tool  # noqa: E402, F401
from test_ontology_first_f0 import _load_map  # noqa: E402

_SIREN = ("siren_list_groups", "siren_search_cases", "siren_get_case", "siren_list_docs", "siren_get_doc")
_FAIL = "✖"


# ── HA-38 쪽 번호 수치 대조 ─────────────────────────────────────────────────────────────────────
def test_HA38_쉼표로_잇댄_쪽_번호는_수_하나가_아니다():
    assert [r for r, _ in sig_numbers("근거 p.2,3,10,11,23 와 p.18,35")] == [], "쪽 번호는 모두 작은 수다 — 대조할 것이 없다"
    assert unsourced_numbers("근거는 p.2,3,10,11,23 쪽이다", '{"evidence":[{"page":2},{"page":3},{"page":10}]}') == []
    # 쉼표 뒤가 세 자리가 아니면 끊어서 하나씩 본다
    got = [r for r, _ in sig_numbers("값 12,3456 과 101,45")]
    assert got == ["3456", "101"], got


@pytest.mark.parametrize("text,want", [("합계 1,234,567 건", ["1,234,567"]), ("비용 12,345.67 원", ["12,345.67"]),
                                       ("2,270 건 중 99", ["2,270"]), ("응력 48039.32MPa", ["48039.32"]),
                                       ("1,234,56", ["1,234"]), ("v1.2.3", [])])
def test_HA38_세_자리_묶음의_천_단위_쉼표는_그대로_수_하나다(text, want):
    assert [r for r, _ in sig_numbers(text)] == want


# ── HA-37 지정 도구 차례 ───────────────────────────────────────────────────────────────────────
def _cache(monkeypatch, **over):
    base = {"at": time.time(), "map": {}, "areas": {}, "area_meta": {}, "routine_read": None, "orig": {},
            "declared": frozenset(), "family": frozenset(), "panel_read": None}
    base.update(over)
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", base)


def test_HA37_앱_이름을_앞에_붙인_목록_검색이_먼저다(monkeypatch):
    _cache(monkeypatch, map={**{n: "siren" for n in _SIREN}, "heaxstep_forge_list_jobs": "heax-step_forge",
                             "plm_defect_search_cases": "plm-defect", "interface_graph": "heax-hwax_risk"},
           orig={"heaxstep_forge_list_jobs": "list_jobs"})
    picked = ["siren_get_case", "interface_graph", "list_interfaces", "siren_list_groups", "siren_search_cases"]
    got = sorted(picked, key=d._list_first)
    assert got == ["list_interfaces", "siren_list_groups", "siren_search_cases", "siren_get_case", "interface_graph"], got
    assert d._list_first("heaxstep_forge_list_jobs") == 0, "겹침 접두는 원래 이름으로"
    assert d._list_first("plm_defect_search_cases") == 0, "앱 키의 하이픈은 밑줄로도 본다"
    assert d._list_first("siren_get_case") == 1 and d._list_first("get_detail") == 1


def test_HA37_리스크_앱이_정한_차례는_같은_무리_안에서_그대로다(monkeypatch):
    _cache(monkeypatch, map={n: "siren" for n in _SIREN})
    risk = ["list_interfaces", "interface_graph", "siren_list_groups", "siren_search_cases", "siren_get_case"]
    assert sorted(risk, key=d._list_first) == ["list_interfaces", "siren_list_groups", "siren_search_cases",
                                               "interface_graph", "siren_get_case"]


def test_HA37_지도가_없으면_노출_이름으로만(monkeypatch):
    _cache(monkeypatch, map={}, orig=None)
    assert d._list_first("siren_search_cases") == 1 and d._list_first("search_reports") == 0


# ── HA-36 선언 백엔드의 확인된 읽기 ─────────────────────────────────────────────────────────────
def test_HA36_확인된_읽기_가운데_제_이름으로_붙은_선언_도구만(monkeypatch):
    _cache(monkeypatch, map={**{n: "siren" for n in _SIREN}, "siren_update_case": "siren", "stepforgeprod_list_parts": "stepforge-prod",
                             "list_parts": "heax-step_forge"},
           orig={"stepforgeprod_list_parts": "list_parts"}, declared=frozenset({"siren", "stepforge-prod"}),
           panel_read=frozenset(_SIREN) | {"stepforgeprod_list_parts", "list_parts"})
    got = a._conn_free_reads()
    assert got == frozenset(_SIREN), sorted(got)
    _cache(monkeypatch, map={n: "siren" for n in _SIREN}, declared=frozenset({"siren"}), panel_read=None)
    assert a._conn_free_reads() == frozenset(), "panel_read 를 모르는 옛 게이트웨이 — 들이지 않는다"
    _cache(monkeypatch, map={n: "siren" for n in _SIREN}, declared=frozenset({"siren"}), panel_read=frozenset(_SIREN), orig=None)
    assert a._conn_free_reads() == frozenset(), "orig 를 모르면 겹침을 가를 수 없다 — 들이지 않는다"


def test_HA36_도구_지도_답의_panel_read_를_캐시에_옮긴다(monkeypatch):
    body = {"map": {n: "siren" for n in _SIREN}, "apps": [{"app": "siren", "declared": True}], "areas": {}, "area_meta": [],
            "routine_read": list(_SIREN), "panel_read": list(_SIREN[:2]), "orig": {}}
    _load_map(monkeypatch, body)
    assert a._TOOLS_MAP_CACHE["panel_read"] == frozenset(_SIREN[:2])
    assert a._conn_free_reads() == frozenset(_SIREN[:2]), "routine_read(이름 낱말 갈래 포함)가 아니라 panel_read 로 고른다"


_POOL_MAP = {**{n: "siren" for n in _SIREN}, "siren_update_case": "siren", "stepforgeprod_list_parts": "stepforge-prod",
             "agent_search": "ai-data-hub", "list_parts": "heax-step_forge", "interface_graph": "heax-hwax_risk"}


def _pool(monkeypatch, *, chair="default", apps=(), started_by=None, as_of="", panel_read=True) -> set:
    """심의를 좌석 발굴 문턱까지 실제로 돌려 자유 조회 준비가 좌석 에이전트에게 준 도구 이름을 받는다(test_ontology_first_f0._pool 꼴)."""
    import delib_jobs
    import langgraph.prebuilt

    body = {"map": dict(_POOL_MAP), "apps": [{"app": "siren", "declared": True}, {"app": "stepforge-prod", "declared": True}],
            "areas": {}, "area_meta": [], "routine_read": [], "orig": {"stepforgeprod_list_parts": "list_parts"},
            **({"panel_read": [*_SIREN, "stepforgeprod_list_parts"]} if panel_read else {})}
    _load_map(monkeypatch, body)
    given = []

    def _fake_agent(_llm, tools, **_k):
        given.append({t.name for t in tools})
        return SimpleNamespace()

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", _fake_agent)
    monkeypatch.setattr(d, "_app_of_tools", lambda: dict(_POOL_MAP))
    tools = {n: _Tool(n) for n in _POOL_MAP}

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    opts = {"personas": [{"key": "mech-a", "role": "기구"}], "free_tools": 1, "voc": "off", "rescreen": 0, "persona_knowledge": 0,
            **({"chair_template": chair} if chair else {}), **({"apps": list(apps)} if apps else {}),
            **({"started_by": started_by} if started_by else {}), **({"as_of": as_of} if as_of else {})}

    async def go():
        gen = d.run_deliberation(SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)), "카메라 모듈 소음", [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                assert ev != "error", data
                if ev == "delib" and data.get("kind") == "stage" and data.get("stage") == "discover":
                    break
        finally:
            await gen.aclose()

    asyncio.run(go())
    assert given, "시험 전제 — 자유 조회 준비가 좌석 에이전트를 지었다"
    return given[0]


@pytest.mark.parametrize("chair,started_by", [("default", None), ("diagnosis", None), ("risk-review", None), ("default", "app")])
def test_HA36_선언_백엔드의_확인된_읽기가_좌석_후보에_든다(monkeypatch, chair, started_by):
    got = _pool(monkeypatch, chair=chair, started_by=started_by)
    assert set(_SIREN) <= got, sorted(got)
    assert "siren_update_case" not in got, "게이트웨이가 읽기로 확인하지 않은 것은 들지 않는다"
    assert "stepforgeprod_list_parts" not in got, "겹침 접두(허브 앱과 두 벌)는 들지 않는다"


def test_HA36_기준일_심의와_옛_게이트웨이는_들이지_않는다(monkeypatch):
    assert not set(_SIREN) & _pool(monkeypatch, as_of="2026-10-01"), "그쪽 데이터를 그 날로 거르지 못한다 — 닫힌 쪽"
    assert not set(_SIREN) & _pool(monkeypatch, panel_read=False)


def test_HA36_앱이_실은_앱_목록의_좁히기에서도_남고_사람이_고른_앱이면_고른_것만(monkeypatch):
    got = _pool(monkeypatch, chair="risk-review", apps=("heax-hwax_risk",))
    assert set(_SIREN) <= got, sorted(got)
    assert set(_SIREN) <= _pool(monkeypatch, chair="default", apps=("heax-hwax_risk",), started_by="app")
    human = _pool(monkeypatch, chair="default", apps=("heax-step_forge",))
    assert not set(_SIREN) & human, "사람이 고른 앱 밖은 좁힌다(종전 그대로)"
    assert set(_SIREN) <= _pool(monkeypatch, chair="default", apps=("siren",)), "사람이 SIREN 을 고르면 남는다"


# ── 잡 줄(_ConnGate) ───────────────────────────────────────────────────────────────────────────
def _struct(name, answer, log=None, delay=0.0, live=None):
    async def co(**kw):
        if log is not None:
            log.append((name, dict(kw)))
        if live is not None:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        try:
            if delay:
                await asyncio.sleep(delay)
            return answer(kw) if callable(answer) else answer
        finally:
            if live is not None:
                live["now"] -= 1
    return StructuredTool(name=name, description=f"{name} 도구", coroutine=co,
                          args_schema={"type": "object", "properties": {"query": {"type": "string"}}})


def test_잡_줄은_선언_백엔드마다_동시_호출을_줄_세운다(monkeypatch):
    _cache(monkeypatch, map={"siren_search_cases": "siren", "list_parts": "heax-step_forge"}, declared=frozenset({"siren"}))
    gate, live = d._ConnGate(2), {"now": 0, "max": 0}
    t = gate.wrap(_struct("siren_search_cases", '{"hits": []}', delay=0.05, live=live))
    other = _struct("list_parts", "{}")
    assert gate.wrap(other) is other and other.coroutine.__name__ == "co", "선언 백엔드가 아니면 감싸지 않는다"

    async def go():
        await asyncio.gather(*(t.ainvoke({"query": str(i)}) for i in range(6)))
    asyncio.run(go())
    assert live["max"] == 2, live


def test_잡_줄은_429_뒤에_그_백엔드를_다시_부르지_않는다(monkeypatch):
    _cache(monkeypatch, map={n: "siren" for n in _SIREN}, declared=frozenset({"siren"}))
    gate, log = d._ConnGate(2), []
    limited = (f"{_FAIL} 도구 siren_search_cases 호출 실패: siren: u@corp.com — SIREN 의 호출 한도를 넘었습니다(429 — 20초 뒤에 다시). "
               "토큰 문제가 아닙니다")
    s = gate.wrap(_struct("siren_search_cases", limited, log))
    g = gate.wrap(_struct("siren_get_case", '{"case": 1}', log))
    first = asyncio.run(s.ainvoke({"query": "소음"}))
    assert first == limited and gate.limited == {"siren": "siren_search_cases"}
    again = asyncio.run(g.ainvoke({"query": "1"}))
    assert again.startswith(_FAIL) and "이 심의에서는 siren 를 다시 부르지 않는다" in again and "429" in again
    assert log == [("siren_search_cases", {"query": "소음"})], "막은 호출은 그쪽에 가지 않는다"
    assert gate.fresh() == [("conn_rate_limited", "siren", "siren_search_cases")] and gate.fresh() == [], "알림은 한 번만"


def test_옛_게이트웨이의_429_문구도_한도_초과로_읽고_미등록과_가른다():
    old429 = (f"{_FAIL} 도구 siren_search_cases 호출 실패: siren: u — 등록 토큰으로 호출하지 못했습니다 (McpError('429 rate_limited')). "
              "포털 '개인 토큰 › 외부 연결'(/tokens?tab=connect)에서 SIREN 토큰을 다시 등록하세요(만료·폐기 가능성).")
    assert d._conn_rate_limited(old429) and not d._conn_unregistered(old429)
    unreg = (f"{_FAIL} 도구 siren_list_groups 호출 실패: siren: u — SIREN 토큰이 포털에 등록되어 있지 않습니다. 포털 '개인 토큰 › 외부 연결'"
             "(/tokens?tab=connect)에서 SIREN 토큰(siren_…)을 등록한 뒤 다시 시도하세요.")
    assert d._conn_unregistered(unreg) and not d._conn_rate_limited(unreg)
    r401 = (f"{_FAIL} 도구 siren_list_groups 호출 실패: siren: u — SIREN 이 등록 토큰을 받지 않았습니다(401 — 만료 · 폐기 · 틀린 토큰). "
            "포털 '개인 토큰 › 외부 연결'(/tokens?tab=connect)에서 SIREN 토큰을 다시 등록하세요.")
    assert d._conn_unregistered(r401)
    assert not d._conn_rate_limited('{"hits": [{"page": 429}]}'), "성공한 결과 속 429 는 한도 초과가 아니다"
    # 상태 꼴만 — 사람 이메일 · 인자를 되울린 글 속 429 에 걸리지 않는다(반대 검토 11-06)
    mail = (f"{_FAIL} 도구 siren_list_groups 호출 실패: siren: kim429@corp.com — SIREN 토큰이 포털에 등록되어 있지 않습니다. 포털 '개인 토큰 › "
            "외부 연결'(/tokens?tab=connect)에서 SIREN 토큰(siren_…)을 등록한 뒤 다시 시도하세요.")
    assert d._conn_unregistered(mail) and not d._conn_rate_limited(mail)
    echo = f"{_FAIL} 도구 siren_get_case 호출 실패: siren: u — SIREN 서버가 오류를 냈거나 (원래 오류: McpError('404 case_not_found: C-429'))"
    assert not d._conn_rate_limited(echo) and not d._conn_unregistered(echo)
    assert d._conn_rate_limited(f"{_FAIL} 도구 x 호출 실패: Client error '429 Too Many Requests' for url 'http://s/mcp'")


def test_좌석_조회의_외부_연결_결과에는_trust_안내가_머리에_붙는다(monkeypatch):
    _cache(monkeypatch, map={n: "siren" for n in _SIREN}, declared=frozenset({"siren"}))
    gate = d._ConnGate(2)
    ok = asyncio.run(gate.wrap(_struct("siren_search_cases", '{"hits": [{"trust": "AI 초안 — 미검증"}]}'), note=True).ainvoke({}))
    assert ok.startswith("[외부 연결 siren 의 결과 —") and "후보로만" in ok and ok.endswith('"AI 초안 — 미검증"}]}')
    for raw in ("[]", '{"ok": false, "error": "x"}', f"{_FAIL} 도구 siren_get_case 호출 실패: boom"):
        assert asyncio.run(gate.wrap(_struct("siren_get_case", raw), note=True).ainvoke({})) == raw, raw
    plain = asyncio.run(d._ConnGate(2).wrap(_struct("siren_list_docs", '{"docs": []}')).ainvoke({}))
    assert plain == '{"docs": []}', "note 를 주지 않으면(엔진의 지정 도구 로드) 그대로 — 머리줄이 싣는다"
    # 신뢰 · 확인 칸이 없는 외부 연결 결과(물성 DB 등)에는 붙이지 않는다 — 측정값까지 후보로 낮추지 않게(반대 검토 11-06)
    mnx = asyncio.run(gate.wrap(_struct("siren_list_docs", '{"material": "Al6061", "E_GPa": 68.9}'), note=True).ainvoke({}))
    assert mnx == '{"material": "Al6061", "E_GPa": 68.9}'


def test_좌석_조회의_미연결은_그_잡에서_기억해_다시_부르지_않는다(monkeypatch):
    _cache(monkeypatch, map={n: "siren" for n in _SIREN}, declared=frozenset({"siren"}))
    gate, log = d._ConnGate(2), []
    unreg = (f"{_FAIL} 도구 siren_search_cases 호출 실패: siren: u — SIREN 토큰이 포털에 등록되어 있지 않습니다. 포털 '개인 토큰 › 외부 연결'"
             "(/tokens?tab=connect)에서 SIREN 토큰(siren_…)을 등록한 뒤 다시 시도하세요.")
    s_ = gate.wrap(_struct("siren_search_cases", unreg, log))
    g_ = gate.wrap(_struct("siren_get_case", '{"case": 1}', log))
    assert asyncio.run(s_.ainvoke({"query": "a"})) == unreg and gate.noconn == {"siren": "siren_search_cases"}
    again = asyncio.run(g_.ainvoke({"query": "b"}))
    assert d._conn_unregistered(again) and "다음 심의부터" in again and len(log) == 1, "막은 호출은 그쪽에 가지 않는다"
    assert gate.fresh() == [("conn_unregistered", "siren", "siren_search_cases")]


def test_외부_연결_안내_머리가_있어도_조회_턴의_절단_표식을_읽는다():
    """반대 검토 11-06 — 머리에 안내 한 줄이 붙어도 원문 길이(조회 턴 래퍼가 자른 자리)를 잃지 않는다(engine-budget A23-6)."""
    capped = "x" * 2000 + "\n…[경고: 도구 출력이 18,190자였고 2,000자에서 잘렸습니다. 필요한 부분만 다시 조회하라]"
    assert d._capped_at(capped) == (18190, 2000), "시험 전제 — 조회 턴 래퍼(app._cap)의 표식 꼴"
    assert d._capped_at(d._conn_note("siren") + "\n" + capped) == (18190, 2000)


# ── 지정 도구 — 미연결 · 한도 초과는 다시 시키지 않고 경고 사건으로 ─────────────────────────────────────
def _designated(monkeypatch, answers: dict, picked):
    """지정 도구로 SIREN 셋을 돌려 이벤트 · 그쪽이 받은 호출 · 계획자 프롬프트를 받는다(엔진 로드의 도구는 코루틴이 있는 StructuredTool — 줄이 감싼다)."""
    _cache(monkeypatch, map={n: "siren" for n in _SIREN}, declared=frozenset({"siren"}))
    monkeypatch.setattr(a, "_APPS_CACHE", {"apps": {"siren": {"app": "siren", "label": "SIREN"}}, "at": time.time()})
    log, plans = [], {}
    tools = {n: _struct(n, answers[n], log) for n in picked}
    tools["agent_search"] = _Tool("agent_search")

    async def _llm(_obj, system, human):
        if "도구 호출 계획자" in system:
            name = human.split("[도구]\n", 1)[1].split(":", 1)[0]
            plans.setdefault(name, []).append(human)
            return '{"query": "카메라 모듈 소음"}'
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["a", "b"], "position_short": "입장"},
                          ensure_ascii=False)

    monkeypatch.setattr(d, "_llm_text", _llm)
    events = _stream(monkeypatch, {"tools": list(picked), "persona_knowledge": 0}, tools=tools)
    return events, log, plans


def _warnings(events, code):
    return [data for ev, data in events if ev == "warning" and data.get("code") == code]


def _card(events, source):
    return [data for ev, data in events if ev == "delib" and data.get("kind") == "evidence" and data.get("source") == source]


def test_RK12_미등록이면_다시_시키지_않고_conn_unregistered_로_알린다(monkeypatch):
    unreg = ("siren: u@corp.com — SIREN 토큰이 포털에 등록되어 있지 않습니다. 포털 '개인 토큰 › 외부 연결'(/tokens?tab=connect)에서 "
             "SIREN 토큰(siren_…)을 등록한 뒤 다시 시도하세요.")
    picked = ("siren_list_groups", "siren_search_cases", "siren_get_case")
    events, log, plans = _designated(monkeypatch, {n: f"{_FAIL} 도구 {n} 호출 실패: {unreg}" for n in picked}, picked)
    assert [n for n, _kw in log] == ["siren_list_groups"], "첫 거절 뒤의 SIREN 도구는 그쪽에 가지 않는다(같은 거절이다)"
    assert list(plans) == ["siren_list_groups"] and len(plans["siren_list_groups"]) == 1, "계획자에게 다시 시키지 않는다"
    (w,) = _warnings(events, "conn_unregistered")
    assert w["backends"] == ["siren"] and w["tools"] == list(picked) and "등록" in w["message"]
    assert w["labels"] == ["SIREN"], "게이트웨이 앱 라벨 — 리스크 앱이 'SIREN 미연결' 로 보인다"
    (card,) = _card(events, "지정 도구 미실행")
    assert card["included"] is False and "외부 연결 미연결" in card["text"] and "siren_get_case" in card["text"]
    assert not _warnings(events, "conn_rate_limited")


def test_RK12_한도_초과면_그_잡에서_그_백엔드의_뒤_도구를_부르지_않는다(monkeypatch):
    picked = ("siren_list_groups", "siren_search_cases", "siren_get_case")
    answers = {"siren_list_groups": '{"groups": ["camera"]}',
               "siren_search_cases": f"{_FAIL} 도구 siren_search_cases 호출 실패: siren: u — SIREN 의 호출 한도를 넘었습니다(429 — 잠시 뒤에 다시).",
               "siren_get_case": '{"case": 1}'}
    events, log, plans = _designated(monkeypatch, answers, picked)
    assert [n for n, _kw in log] == ["siren_list_groups", "siren_search_cases"], "429 뒤의 사례 카드는 그쪽에 가지 않는다"
    assert "siren_get_case" not in plans, "인자를 짓는 LLM 도 부르지 않는다"
    (w,) = _warnings(events, "conn_rate_limited")
    assert w["backends"] == ["siren"] and w["tools"] == ["siren_search_cases", "siren_get_case"]
    (card,) = _card(events, "지정 도구 미실행")
    assert "호출 한도 초과(429" in card["text"]


def test_RK12_성공한_외부_연결_결과는_trust_안내를_머리줄에_싣는다(monkeypatch):
    picked = ("siren_list_groups", "siren_search_cases", "siren_get_case")
    answers = {"siren_list_groups": '{"groups": ["camera"]}', "siren_search_cases": '{"hits": [{"id": "C-7", "trust": "AI 초안 — 미검증"}]}',
               "siren_get_case": '{"case": "C-7"}'}
    events, log, plans = _designated(monkeypatch, answers, picked)
    assert [n for n, _kw in log] == list(picked)
    seen = plans["siren_get_case"][0]
    assert seen.count("[외부 연결 siren 의 결과 —") == 1, "신뢰 칸이 있는 결과(사례 검색)의 머리줄에만 — 그룹 목록에는 없다"
    assert '"C-7"' in seen
    assert not _warnings(events, "conn_unregistered") and not _warnings(events, "conn_rate_limited")

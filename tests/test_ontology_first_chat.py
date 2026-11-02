# 코어 어휘표 ontology 칸 — 챗 고정 몫 · 규칙 ⑧ · 입구 표지(챗 로드만) · /health, 기본 표에서는 프롬프트 · 도구가 바이트까지 그대로(HWAXPortal docs/ontology-first)
"""HWAXPortal docs/ontology-first PLAN §4 · OF-5 · OF-6 · OF-14 ①④.

  · 고정 몫(_ONTO_CORE)의 정본이 어휘표 ontology.apps(reserve 인 앱의 tools — (백엔드, 원래 이름))로 옮겨 갔다. 추적 기본값은 _ONTO_CORE 를 같은 차례로
    옮긴 것이라 기본 표에서 고정 몫 · 시스템 프롬프트 · 묶는 도구와 그 설명이 바이트까지 같다(사용자 결정 OF-14 ④).
  · entry 가 있으면 코어 규칙 끝에 ⑧ 한 줄(앱 이름 없이)과 그 입구 도구 설명의 '온톨로지 입구' 표지가 선다 — 표지는 챗 로드에만(심의 · 리스크 좌석의 로드는
    같은 래퍼를 타지만 달지 않는다), 순위(어휘 · 의미)는 표지와 상관없다.
  · 검사는 앱 줄마다 — 틀린 줄만 버리고, 허브 가족의 entry 는 버린다(줄은 남는다). /health core_vocab.ontology 가 말한다.

  실행:  .venv/bin/python -m pytest tests/test_ontology_first_chat.py -q
"""
import asyncio
import json
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402
import onto_links  # noqa: E402

_REAL = a.CORE_VOCAB_PATH          # 형제 포털 리포의 정본 — conftest 가 시험마다 '없음' 으로 돌리기 전의 기본 경로
_TASK = {"list_tasks": "heax-kooremapper_mcp", "task_revisions": "heax-kooremapper_mcp", "compare_task_revisions": "heax-kooremapper_mcp",
         "find_plm_task": "heax-step_forge"}
# cae00 꼴 — 허브 둘이 붙어 가족 도구는 접두, 과제 도구는 맨 이름(운영 사본은 선언 백엔드라 그쪽만 접두 — OF-3)
_CAE00 = {**{n: ("rootdesigndatahub" if n.startswith("rootdesigndatahub_") else "failuretrace")
             for n in a._ONTO_CORE if n.startswith(("rootdesigndatahub_", "failuretrace_"))},
          **_TASK, "stepforgeprod_find_plm_task": "stepforge-prod", "onto_search": "heax-step_forge", "list_parts": "heax-step_forge"}
_CAE00_ORIG = {n: n.split("_", 1)[1] for n, bk in _CAE00.items() if bk in ("rootdesigndatahub", "failuretrace")} | \
    {"stepforgeprod_find_plm_task": "find_plm_task"}
# dev 꼴 — 허브가 없다
_DEV = {**_TASK, "onto_search": "heax-step_forge", "list_parts": "heax-step_forge", "semantic_search_x": "ai-data-hub"}
# 반대 검토 11-02(OF-23)의 두 꼴 — 지도로 풀면 옛 고정 몫과 갈렸다. (i) 허브 StepForge · DynaForge 가 내려가 선언 운영 사본이 과제 도구의 맨 이름을 쥔 박스
# (옛 고정 몫은 그 맨 이름을 세운다 — 지도로 풀면 넷을 뺐다). (ii) 정적 백엔드 하나가 list_tasks 를 더해 둘 다 접두가 붙은 박스(옛 고정 몫의 맨 이름은
# 이 박스에 없어 아무것도 세우지 않는다 — 지도로 풀면 heaxkooremapper_mcp_list_tasks 를 세웠다).
_HUBS_DOWN = {**{n: bk for n, bk in _CAE00.items() if bk in ("rootdesigndatahub", "failuretrace")},
              **{n: "dynaforge-prod" for n in ("list_tasks", "task_revisions", "compare_task_revisions")},
              "find_plm_task": "stepforge-prod", "list_parts": "stepforge-prod"}
_HUBS_DOWN_ORIG = {n: o for n, o in _CAE00_ORIG.items() if n.startswith(("rootdesigndatahub_", "failuretrace_"))}
_COLLIDE = {**{n: bk for n, bk in _DEV.items() if n != "list_tasks"},
            "heaxkooremapper_mcp_list_tasks": "heax-kooremapper_mcp", "taskmanager_list_tasks": "taskmanager"}
_COLLIDE_ORIG = {"heaxkooremapper_mcp_list_tasks": "list_tasks", "taskmanager_list_tasks": "list_tasks"}
_BOXES = {"empty": ({}, None), "cae00": (_CAE00, _CAE00_ORIG), "dev": (_DEV, {}), "hubs_down": (_HUBS_DOWN, _HUBS_DOWN_ORIG),
          "static_collide": (_COLLIDE, _COLLIDE_ORIG)}
_LABELS = {"heax-step_forge": "StepForge", "heax-kooremapper_mcp": "DynaForge", "stepforge-prod": "StepForge 운영",
           "dynaforge-prod": "DynaForge 운영", "taskmanager": "TaskManager"}


def _real_vocab() -> dict:
    if not _REAL.exists():
        pytest.skip(f"형제 포털 리포의 코어 어휘표가 없다: {_REAL}")
    d = json.loads(_REAL.read_text(encoding="utf-8"))
    assert "ontology" in d, "형제 포털 리포의 어휘표에 ontology 칸이 없다 — 이 판(docs/ontology-first)의 포털과 함께 돌린다"
    return d


def _use(monkeypatch, tmp_path, body: dict, overlay: dict | None = None):
    p = tmp_path / "core_vocab.json"
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    loc = tmp_path / "core_vocab.local.json"
    if overlay is not None:
        loc.write_text(json.dumps(overlay, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(a, "CORE_VOCAB_LOCAL_PATH", loc)
    monkeypatch.setattr(a, "_CORE_VOCAB", {"mtime": None, "data": None, "error": "없음", "overlay": None})


def _map(monkeypatch, tmap: dict, orig=None, labels=None):
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": time.time(), "map": dict(tmap), "areas": {}, "area_meta": {}, "routine_read": None,
                                                "orig": orig, "declared": frozenset(), "family": frozenset()})
    monkeypatch.setattr(a, "_APPS_CACHE", {"apps": {k: {"app": k, "label": v} for k, v in (labels or {}).items()}, "at": time.time()})


def _tool(name: str, desc: str = "설명"):
    return StructuredTool(name=name, description=desc, args_schema={"type": "object", "properties": {"q": {"type": "string"}}},
                          coroutine=lambda **_k: None)


def _bare(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "ontology"}


# ── 기본 표 — 바이트까지 그대로 ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("box", tuple(_BOXES))
def test_기본_표의_고정_몫은_옛_ONTO_CORE_그대로다(monkeypatch, tmp_path, box):
    """지도 꼴과 상관없이 — 입구가 없으면(기본) 지도를 보지 않고 옛 이름 규칙으로 푼다(OF-23)."""
    _use(monkeypatch, tmp_path, _real_vocab())
    tmap, orig = _BOXES[box]
    _map(monkeypatch, tmap, orig)
    assert a._core_vocab()["onto"]["present"] and a._core_vocab()["onto"]["entries"] == (), "기본 표에는 입구가 없다(OF-14 ④)"
    assert a._onto_core() == a._ONTO_CORE, a._onto_core()


def test_기본_표의_시스템_프롬프트는_ontology_칸이_없던_때와_바이트까지_같다(monkeypatch, tmp_path):
    d = _real_vocab()
    _use(monkeypatch, tmp_path, _bare(d))
    before = a._system_prompt()
    _use(monkeypatch, tmp_path, d)
    assert a._system_prompt() == before and "⑧" not in before and onto_links.MARK not in before


@pytest.mark.parametrize("box", ("cae00", "dev", "hubs_down", "static_collide"))
@pytest.mark.parametrize("cap", (0, 6))
def test_기본_표에서_묶는_도구와_설명은_옛_고정_몫과_바이트까지_같다(monkeypatch, tmp_path, cap, box):
    """캡 없는 길(운영 TOOL_MAX=0 — 예산을 넘겨 관련도로 자르는 길까지)과 캡 길 모두. 이름 · 차례 · 챗 로드의 설명이 같다 — 지도 꼴마다(OF-23)."""
    d = _real_vocab()
    tmap, orig = _BOXES[box]
    _map(monkeypatch, tmap, orig, labels=_LABELS)
    monkeypatch.setattr(a, "TOOL_MAX", cap)
    monkeypatch.setattr(a, "CHAT_TOOL_SCHEMA_BUDGET", 300)        # 캡 없는 길도 예산을 넘겨 자르게 — 고정 몫(keep)이 드러난다
    monkeypatch.setattr(a, "_semantic_order", lambda *_a, **_k: [])

    def run(body):
        _use(monkeypatch, tmp_path, body)
        tools = [a._prep_tool(_tool(n), chat=True) for n in sorted(tmap)]
        return [(t.name, t.description) for t in a._select_tools(tools, "과제 차수 비교")]
    assert run(d) == run(_bare(d))


def test_기본_표에서_챗_로드에도_표지가_없다(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, _real_vocab())
    _map(monkeypatch, _DEV, {}, labels={"heax-step_forge": "StepForge"})
    t = a._prep_tool(_tool("find_plm_task"), chat=True)
    assert t.description.startswith("[StepForge] ") and onto_links.MARK not in t.description


# ── 박스 덧씌우기로 입구를 켠다 ────────────────────────────────────────────────────────────────────
_SF_ENTRY = {"ontology": {"apps": {"heax-step_forge": {"tools": ["find_plm_task", "onto_search"], "entry": "onto_search"}}}}


def test_입구를_켜면_규칙_8_과_표지가_서고_고정_몫에_든다(monkeypatch, tmp_path):
    d = _real_vocab()
    _use(monkeypatch, tmp_path, d)
    base_rules = a._system_prompt()
    _use(monkeypatch, tmp_path, d, overlay=_SF_ENTRY)
    _map(monkeypatch, _CAE00, _CAE00_ORIG, labels={"heax-step_forge": "StepForge"})
    sp = a._system_prompt()
    assert sp != base_rules and sp.replace(a._ONTO_RULE, "") == base_rules, "규칙 끝에 ⑧ 한 줄만 붙는다 — 나머지는 바이트까지 같다"
    assert "StepForge" not in a._ONTO_RULE and "onto_search" not in a._ONTO_RULE, "규칙에 앱 · 도구 이름을 적지 않는다(앞부분 캐시 W-3)"
    assert a._core_vocab()["onto"]["entries"] == (("heax-step_forge", "onto_search"),)
    assert a._onto_core() == a._ONTO_CORE + ("onto_search",), "덧씌우기의 tools 차례로 고정 몫 끝에(merge patch — 목록은 통째로)"
    chat = a._prep_tool(_tool("onto_search", "온톨로지에서 이름을 푼다"), chat=True)
    assert chat.description.startswith(f"[StepForge · {onto_links.MARK}] "), chat.description
    seat = a._prep_tool(_tool("onto_search", "온톨로지에서 이름을 푼다"))
    assert seat.description.startswith("[StepForge] ") and onto_links.MARK not in seat.description, "심의 · 리스크 좌석 로드에는 표지가 없다"
    other = a._prep_tool(_tool("find_plm_task"), chat=True)
    assert onto_links.MARK not in other.description, "입구가 아닌 도구에는 없다"
    h = a._core_vocab_health()["ontology"]
    assert h["entries"] == ["heax-step_forge:onto_search"] and h["reserve"] == len(a._ONTO_CORE) + 1 and h["missing"] == [], h


def test_표지는_순위를_바꾸지_않는다(monkeypatch, tmp_path):
    """'온톨로지' 를 부른 질의가 입구 도구를 끌어올리면 '온톨로지가 있는 플랫폼으로 보낸다'(버린 안 — OF-1 (나))가 된다. 어휘 순위는 표지를 떼고 센다."""
    _use(monkeypatch, tmp_path, _real_vocab(), overlay=_SF_ENTRY)
    _map(monkeypatch, _CAE00, _CAE00_ORIG, labels={"heax-step_forge": "StepForge"})
    marked = a._prep_tool(_tool("onto_search", "이름을 푼다"), chat=True)
    plain = a._prep_tool(_tool("onto_search", "이름을 푼다"))
    assert marked.description != plain.description
    for q in ("온톨로지 입구", "StepForge 온톨로지", "고장모드 목록"):
        qt = a._tok_query(q)
        assert a._lex_profile(marked, qt) == a._lex_profile(plain, qt), q


def test_옛_게이트웨이_orig_없음에도_입구는_접두_꼴까지_알아본다(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, _real_vocab(), overlay=_SF_ENTRY)
    _map(monkeypatch, {"heaxstep_forge_onto_search": "heax-step_forge"}, None, labels={"heax-step_forge": "StepForge"})
    t = a._prep_tool(_tool("heaxstep_forge_onto_search"), chat=True)
    assert t.description.startswith(f"[StepForge · {onto_links.MARK}] ") and "heaxstep_forge_onto_search" in a._onto_core()


def test_허브_가족의_entry_는_버리고_줄은_남긴다(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, _real_vocab(), overlay={"ontology": {"apps": {"rootdesigndatahub": {"entry": "object_resolve"}}}})
    _map(monkeypatch, _CAE00, _CAE00_ORIG)
    v = a._core_vocab()["onto"]
    assert v["entries"] == () and [x["app"] for x in v["dropped"]] == ["rootdesigndatahub"], v["dropped"]
    assert "⑧" not in a._system_prompt() and a._onto_core() == a._ONTO_CORE, "가족 줄의 고정 몫은 그대로다"


def test_틀린_앱_줄만_버리고_나머지는_돈다(monkeypatch, tmp_path):
    bad = {"ontology": {"apps": {"Bad Key": {"tools": ["x"]}, "signalforge": {"tools": "resolve_product_keyword"},
                                 "heax-kooremapper_mcp": {"entry": "nope"}, "mx-white-paper": {"tools": ["wiki_search"], "reserve": "yes"},
                                 "heax-step_forge": {"tools": ["find_plm_task", "onto_search"], "entry": "onto_search", "weight": 3}}}}
    _use(monkeypatch, tmp_path, _real_vocab(), overlay=bad)
    _map(monkeypatch, _CAE00, _CAE00_ORIG)
    d = a._core_vocab()
    assert d["fingerprint"] != d["base_fingerprint"], "덧씌우기는 받아들였다(표 전체를 버리지 않는다 — 게이트웨이와 지문이 같게)"
    v = d["onto"]
    assert sorted(x["app"] for x in v["dropped"]) == sorted(["Bad Key", "signalforge", "heax-kooremapper_mcp", "mx-white-paper", "heax-step_forge"])
    assert list(v["apps"]) == ["rootdesigndatahub", "failuretrace"] and v["entries"] == ()
    assert a._onto_core() == tuple(n for n in a._ONTO_CORE if n.startswith(("rootdesigndatahub_", "failuretrace_")))
    assert "⑧" not in a._system_prompt(), "버린 줄의 입구는 규칙을 세우지 않는다"
    assert a._core_vocab_health()["ontology"]["dropped"], "버린 줄과 까닭은 /health 에"


def test_이_박스_도구_지도에_없는_이름을_health_가_말하고_입구를_켜면_남의_도구는_앞세우지_않는다(monkeypatch, tmp_path):
    """입구가 없으면(기본) 고정 몫은 옛 이름 규칙 그대로다 — 그 이름이 이 박스에서 다른 앱의 것이어도 옛 고정 몫처럼 세운다(바이트 동일 — OF-23).
    지도로 푸는 것(남의 도구는 세우지 않는다 — OF-17)은 입구를 켠 박스만이다. /health 의 missing 은 둘 다 지도로 센다."""
    _use(monkeypatch, tmp_path, _real_vocab())
    _map(monkeypatch, {**_DEV, "list_tasks": "other-app"}, {})
    assert a._onto_core() == a._ONTO_CORE, "기본 — 옛 고정 몫 그대로"
    h = a._core_vocab_health()["ontology"]
    assert "heax-kooremapper_mcp:list_tasks" in h["missing"] and "rootdesigndatahub:object_resolve" in h["missing"], h["missing"]
    assert h["reserve"] == len(a._ONTO_CORE), "/health 의 고정 몫 수는 실제로 세우는 수다"
    _use(monkeypatch, tmp_path, _real_vocab(), overlay=_SF_ENTRY)
    names = a._onto_core()
    assert "list_tasks" not in names, "입구를 켠 박스 — 그 이름은 이 박스에서 다른 앱의 것이다(남의 도구를 고정 몫에 세우지 않는다)"
    assert a._core_vocab_health()["ontology"]["reserve"] == len(names)


def test_어휘표가_없으면_ONTO_CORE_이고_health_는_칸이_없다고_말한다():
    assert a._onto_core() == a._ONTO_CORE and a._onto_entries() == ()
    assert a._core_vocab_health() == {"loaded": False, "error": "없음"}


def test_입구를_켜고_끄면_챗_에이전트_묶음을_새로_짓는다(monkeypatch, tmp_path):
    """표지는 묶을 때 도구 설명에 달린다 — 입구 쌍이 캐시 열쇠에 없으면 켠 뒤에도 30분(AGENT_CACHE_TTL_S) 동안 표지 없는 묶음이 돌아 규칙 ⑧ 과 어긋났다."""
    built = []

    def _fake_react(_llm, tools, **_k):
        built.append({t.name: t.description for t in tools})
        return types.SimpleNamespace()

    class _StubClient:
        def __init__(self, _c):
            pass

        async def get_tools(self):
            return [_tool("onto_search", "이름을 푼다"), _tool("list_parts")]

    monkeypatch.setattr(a, "create_react_agent", _fake_react)
    monkeypatch.setattr(a, "MultiServerMCPClient", _StubClient)
    monkeypatch.setattr(a, "TOOL_MAX", 0)
    _map(monkeypatch, _DEV, {}, labels={"heax-step_forge": "StepForge"})
    state = types.SimpleNamespace(llm=object(), connections={"gateway": {"url": "http://gw/mcp", "headers": {}}},
                                  agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    monkeypatch.setattr(a, "_chat_llm_of", lambda _app: object())
    d = _real_vocab()

    def build():
        return asyncio.run(a._agent_for(types.SimpleNamespace(state=state), ["admin"], user="u@x"))

    _use(monkeypatch, tmp_path, d)
    build()
    _use(monkeypatch, tmp_path, d, overlay=_SF_ENTRY)
    build()
    build()                                    # 같은 입구 — 캐시를 쓴다
    assert len(built) == 2 and len(state.agent_cache) == 2, (len(built), list(state.agent_cache))
    assert onto_links.MARK not in built[0]["onto_search"] and built[1]["onto_search"].startswith(f"[StepForge · {onto_links.MARK}]")


def test_스냅샷_폴백은_입구가_다를_때_지은_묶음을_쓰지_않는다(monkeypatch, tmp_path):
    """게이트웨이 불통 때의 스냅샷 폴백도 입구 쌍으로 가른다 — 캐시 열쇠에는 입구가 있었고 스냅샷 열쇠에는 없어, 입구를 켠 뒤 불통이면 규칙 ⑧ 은 서는데
    켜기 전에 지은(표지 없는) 묶음이 돌았다(반대 검토 11-02 — OF-24). 같은 입구로 지은 스냅샷은 그대로 쓴다."""
    built = []

    def _fake_react(_llm, tools, **_k):
        built.append({t.name: t.description for t in tools})
        return types.SimpleNamespace()
    down = {"on": False}

    class _StubClient:
        def __init__(self, _c, **_k):
            pass

        async def get_tools(self):
            if down["on"]:
                raise RuntimeError("gateway down 503")
            return [_tool("onto_search", "이름을 푼다"), _tool("list_parts")]

    monkeypatch.setattr(a, "create_react_agent", _fake_react)
    monkeypatch.setattr(a, "MultiServerMCPClient", _StubClient)
    monkeypatch.setattr(a, "TOOL_MAX", 0)
    monkeypatch.setattr(a, "_chat_llm_of", lambda _app: object())
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))      # 다시 받기(_get_tools_retry)의 기다림을 건너뛴다
    _map(monkeypatch, _DEV, {}, labels={"heax-step_forge": "StepForge"})
    state = types.SimpleNamespace(llm=object(), connections={"gateway": {"url": "http://gw/mcp", "headers": {}}},
                                  agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    d = _real_vocab()

    def build() -> dict:
        asyncio.run(a._agent_for(types.SimpleNamespace(state=state), ["admin"], user="u@x"))
        return built[-1]

    _use(monkeypatch, tmp_path, d)
    assert onto_links.MARK not in build()["onto_search"], "입구 없음 — 표지 없는 묶음이 스냅샷에 남는다"
    _use(monkeypatch, tmp_path, d, overlay=_SF_ENTRY)
    down["on"] = True
    got = build()
    assert "⑧" in a._system_prompt(), "시험 전제 — 입구를 켜 규칙 ⑧ 이 섰다"
    assert got == {}, ("입구가 다를 때 지은 스냅샷은 쓰지 않는다 — 규칙 ⑧ 과 표지 없는 입구 도구가 함께 돌지 않게", got)
    down["on"] = False
    assert build()["onto_search"].startswith(f"[StepForge · {onto_links.MARK}]")
    state.agent_cache.clear()                 # 캐시 창(AGENT_CACHE_TTL_S)이 지난 것처럼 — 다시 지을 때 불통이면 스냅샷 폴백을 탄다
    down["on"] = True
    assert build()["onto_search"].startswith(f"[StepForge · {onto_links.MARK}]"), "같은 입구로 지은 스냅샷은 그대로 쓴다"


def test_표지는_설명_상한_뒤에_달아_상한_안의_글이_표지와_상관없이_같다(monkeypatch, tmp_path):
    """표지를 상한(TOOL_DESC_MAX) 앞에서 달면 상한에 닿는 긴 설명은 꼬리 낱말을 잃어 어휘 점수가 바뀌었다 — 표지는 순위의 신호가 아닌데(OF-18) 그 길로
    순위가 갈렸다(반대 검토 11-02 — OF-25). 상한으로 자른 뒤에 단다 — 표지를 떼면 표지 없는 설명과 글자 그대로 같다."""
    _use(monkeypatch, tmp_path, _real_vocab(), overlay=_SF_ENTRY)
    _map(monkeypatch, _CAE00, _CAE00_ORIG, labels={"heax-step_forge": "StepForge"})
    head = len("[StepForge] ")
    desc = "가" * (a.TOOL_DESC_MAX - head - 5) + " 해석과제"        # 라벨까지 정확히 상한 — 앞에서 단 표지(10자)는 꼬리 낱말을 밀어냈다
    marked = a._prep_tool(_tool("onto_search", desc), chat=True)
    plain = a._prep_tool(_tool("onto_search", desc))
    assert marked.description.startswith(f"[StepForge · {onto_links.MARK}] "), marked.description[:40]
    assert plain.description.endswith(" 해석과제") and onto_links.strip_mark(marked.description) == plain.description
    others = [_tool(f"t{i}", f"도구 {i} 설명") for i in range(5)]
    qt = a._tok_query("해석과제")
    sm = a._lex_scores([marked, *others], qt).get("onto_search", 0)
    assert sm == a._lex_scores([plain, *others], qt).get("onto_search", 0) and sm > 0, sm
    long_ = a._prep_tool(_tool("onto_search", desc + " 더 긴 꼬리"), chat=True)
    assert onto_links.strip_mark(long_.description) == a._prep_tool(_tool("onto_search", desc + " 더 긴 꼬리")).description, \
        "상한을 넘는 설명도 자른 글이 같다 — 표지는 그 뒤에 붙는다"

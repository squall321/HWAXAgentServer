# 좌석 자유 조회가 게이트웨이의 겹침 접두 이름을 원래 이름으로 판정한다 — 리스크 심사 · 선언 백엔드 · 허브 가족은 그대로(HWAXPortal docs/ontology-first F0)
"""HWAXPortal docs/ontology-first PLAN §3 · OF-3 · OF-4 · OF-14 ②.

엔진은 자유 조회 후보를 노출 이름의 머리(_FREE_ALLOW)로 골라, 겹침 때문에 게이트웨이가 접두를 단 읽기를 모두 버렸다 — dev 11-02 에
aidatahub_semantic_search · signalforge_semantic_search(semantic_ 은 일부러 연 머리다) · heaxstep_forge_list_jobs · ste_list_jobs 가 빠져 있었다.
게이트웨이 /tools-map 의 orig(노출 이름 → 원래 이름) · 앱 줄의 declared 로 원래 이름을 판정한다. 빼는 셋:
  · 리스크 심사(와 리스크 앱이 띄운 패널) — D-18 의 이름 검사가 맨 이름이라 원래 이름으로 보면 reportarchive_list_reports 가 새로 열린다.
  · 선언 백엔드(connect: — 운영 사본 등) — 등록하지 않은 사람에게는 부를 때 거절되고, 허브 앱과 같은 도구가 두 벌이 된다.
  · 허브 가족 — 늘 접두다. 좌석에 허브를 열지는 온톨로지 결정(PLAN §5.4)의 몫이다.
orig 칸이 없는 옛 게이트웨이와 붙으면 지금 그대로다.

  실행:  .venv/bin/python -m pytest tests/test_ontology_first_f0.py -q
"""
import asyncio
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

from test_delib_silent_drops import _Tool  # noqa: E402

# 게이트웨이 /tools-map 의 꼴(HWAXMcpGateway wt1102/ontology-build — orig · 앱 줄의 declared · role). dev 11-02 의 겹침 넷과 cae00 의 갈래를 섞었다.
_MAP = {
    "agent_search": "ai-data-hub",
    "aidatahub_semantic_search": "ai-data-hub", "signalforge_semantic_search": "signalforge",
    "heaxstep_forge_list_jobs": "heax-step_forge", "ste_list_jobs": "ste",
    "heaxstep_forge_cancel_job": "heax-step_forge",            # 원래 이름 cancel_job — 읽기가 아니다
    "smarttwinmcp_catalog_run": "smart-twin-mcp",              # 원래 이름이 닫기 목록(catalog_run)
    "stepforgeprod_list_parts": "stepforge-prod",              # 선언 백엔드(운영 사본)
    "rootdesigndatahub_get_guide": "rootdesigndatahub",        # 허브 가족(정본) — 늘 접두
    "reportarchive_list_reports": "reportarchive",             # 정적 백엔드끼리 겹침(DynaForge 와) — D-18 의 RA 보고서 읽기
    "heaxmaterialtwin_web_get_material": "heax-materialtwin_web",   # 물성 목록 도구가 겹쳐 접두가 붙은 꼴
    "list_parts": "heax-step_forge",
}
_ORIG = {"aidatahub_semantic_search": "semantic_search", "signalforge_semantic_search": "semantic_search",
         "heaxstep_forge_list_jobs": "list_jobs", "ste_list_jobs": "list_jobs", "heaxstep_forge_cancel_job": "cancel_job",
         "smarttwinmcp_catalog_run": "catalog_run", "stepforgeprod_list_parts": "list_parts",
         "rootdesigndatahub_get_guide": "get_guide", "reportarchive_list_reports": "list_reports",
         "heaxmaterialtwin_web_get_material": "get_material"}
_APPS = [{"app": "stepforge-prod", "declared": True}, {"app": "rootdesigndatahub", "role": "canonical"},
         {"app": "failuretrace", "role": "sibling"}, {"app": "reportarchive", "role": "noncore"}, {"app": "ai-data-hub"}]
_OPENED = {"aidatahub_semantic_search", "signalforge_semantic_search", "heaxstep_forge_list_jobs", "ste_list_jobs",
           "heaxmaterialtwin_web_get_material"}
_CLOSED = {"heaxstep_forge_cancel_job", "smarttwinmcp_catalog_run", "stepforgeprod_list_parts", "rootdesigndatahub_get_guide"}


def _gateway_reply(*, orig=True) -> dict:
    return {"map": dict(_MAP), "apps": [dict(r) for r in _APPS], "areas": {}, "area_meta": [], "routine_read": [],
            **({"orig": dict(_ORIG)} if orig else {})}


def _load_map(monkeypatch, body: dict) -> None:
    """app._tools_map 이 가짜 게이트웨이 답을 받게 한다 — 받은 칸을 캐시에 옮기는 셈을 그대로 지난다."""
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": 0.0, "map": {}, "areas": {}, "area_meta": {}, "routine_read": None})

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_e):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=5: _Resp(json.dumps(body).encode()))
    assert a._tools_map() == body["map"], "전제 — 가짜 게이트웨이의 맵을 받았다"


# ── 맵 칸 ────────────────────────────────────────────────────────────────────────────────────
def test_도구_지도의_orig_declared_role_을_캐시에_옮긴다(monkeypatch):
    _load_map(monkeypatch, _gateway_reply())
    c = a._TOOLS_MAP_CACHE
    assert c["orig"] == _ORIG and c["declared"] == frozenset({"stepforge-prod"})
    assert c["family"] == frozenset({"rootdesigndatahub", "failuretrace"}), "role 이 canonical · sibling 인 앱만 — 비코어는 아니다"


def test_orig_칸이_없는_옛_게이트웨이면_None_이다(monkeypatch):
    _load_map(monkeypatch, _gateway_reply(orig=False))
    assert a._TOOLS_MAP_CACHE["orig"] is None and a._free_judge_names() == {}


def test_원래_이름으로_볼_것은_겹침_접두뿐이고_선언_백엔드와_허브_가족은_뺀다(monkeypatch):
    _load_map(monkeypatch, _gateway_reply())
    got = a._free_judge_names()
    assert "stepforgeprod_list_parts" not in got, "선언 백엔드 — 등록하지 않은 사람에게는 거절된다"
    assert "rootdesigndatahub_get_guide" not in got, "허브 가족 — 늘 접두(좌석에 허브를 열지는 PLAN §5.4)"
    assert got["aidatahub_semantic_search"] == "semantic_search" and got["ste_list_jobs"] == "list_jobs"
    assert set(got) == set(_ORIG) - {"stepforgeprod_list_parts", "rootdesigndatahub_get_guide"}


def test_허브_가족은_게이트웨이_role_이_없어도_어휘표의_허브로_뺀다(monkeypatch):
    """게이트웨이 표가 잠깐 역할을 못 실은 때(어휘표를 못 읽은 게이트웨이)도 에이전트 서버의 허브 표(_hub_apps — 어휘표가 없으면 기본 셋)로 뺀다."""
    _load_map(monkeypatch, {**_gateway_reply(), "apps": [{"app": "stepforge-prod", "declared": True}]})
    assert "rootdesigndatahub" in a._hub_apps() and "rootdesigndatahub_get_guide" not in a._free_judge_names()


# ── 좌석 자유 조회 후보 ─────────────────────────────────────────────────────────────────────────
def _pool(monkeypatch, chair="default", *, orig=True, apps=(), started_by=None) -> set:
    """심의를 좌석 발굴 문턱까지 실제로 돌려 자유 조회 준비가 좌석 에이전트에게 준 도구 이름을 받는다(tests/test_risk_registry_closed._seat_given 꼴)."""
    import langgraph.prebuilt

    _load_map(monkeypatch, _gateway_reply(orig=orig))
    given = []

    def _fake_agent(_llm, tools, **_k):
        given.append({t.name for t in tools})
        return SimpleNamespace()

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", _fake_agent)
    monkeypatch.setattr(d, "_app_of_tools", lambda: dict(_MAP))
    tools = {n: _Tool(n) for n in _MAP}

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    opts = {"personas": [{"key": "mech-a", "role": "기구"}], "free_tools": 1, "voc": "off", "rescreen": 0, "persona_knowledge": 0,
            **({"chair_template": chair} if chair else {}), **({"apps": list(apps)} if apps else {}),
            **({"started_by": started_by} if started_by else {})}

    async def go():
        gen = d.run_deliberation(SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)), "힌지 크랙 원인", [], opts)
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


@pytest.mark.parametrize("chair", ("default", "diagnosis"))
def test_리스크_심사가_아니면_겹쳐_접두가_붙은_읽기가_후보에_든다(monkeypatch, chair):
    got = _pool(monkeypatch, chair)
    assert _OPENED <= got, sorted(got)
    assert not _CLOSED & got, ("원래 이름이 쓰기 · 닫기 목록이거나 선언 백엔드 · 허브 가족이면 들지 않는다", sorted(_CLOSED & got))
    assert "reportarchive_list_reports" in got, "리스크 심사가 아닌 심의는 RA 보고서 읽기를 맨 이름 그대로 받는다 — 겹친 꼴도 같다"


def test_리스크_심사는_지금_판정_그대로라_RA_보고서_읽기가_새로_열리지_않는다(monkeypatch):
    got = _pool(monkeypatch, "risk-review")
    assert "reportarchive_list_reports" not in got, "D-18 — 원래 이름(list_reports)으로 보면 앞 패널 초안을 읽는 길이 열린다"
    assert not (_OPENED | _CLOSED) & got, sorted(got)


def test_리스크_앱이_띄운_패널도_지금_판정_그대로다(monkeypatch):
    got = _pool(monkeypatch, "default", started_by="app")
    assert not (_OPENED | _CLOSED) & got and "reportarchive_list_reports" not in got, sorted(got)


def test_orig_칸이_없는_옛_게이트웨이와_붙으면_후보가_지금과_같다(monkeypatch):
    old = _pool(monkeypatch, "default", orig=False)
    assert old == {"agent_search", "list_parts"}, sorted(old)


def test_앱을_좁혀도_겹쳐_접두가_붙은_물성_읽기는_남는다(monkeypatch):
    """앱 지정의 물성 목록(_MATERIAL_TOOLS)도 원래 이름으로 본다 — 지정한 앱 밖이라도 물성 조회는 늘 남는다(물성은 기억이 아니라 조회 기록)."""
    got = _pool(monkeypatch, "default", apps=("heax-step_forge",))
    assert "heaxmaterialtwin_web_get_material" in got and "agent_search" in got, sorted(got)
    assert "aidatahub_semantic_search" not in got, "지정 앱 밖의 다른 읽기는 좁혀진다"
    assert {"heaxstep_forge_list_jobs", "list_parts"} <= got

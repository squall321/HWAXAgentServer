# 챗 도구 래퍼의 다른 허브 id 막이 — 이 턴의 허브 결과에서 UUID → 허브를 배우고, 다른 허브에서만 본 id 를 실은 허브 도구 호출은 보내지 않고 key 로 다시 풀라고 돌려준다(HWAXPortal docs/request-1030 HA-30)
"""10-30 OT-07 — failuretrace_graph_neighbors 에 자주검증 허브의 id 를 넘겨 APP-GRAPH-0001 이었다(10-27 N-13 도 같은 꼴). 허브 id 는 설치마다
따로 만든 UUID 라 같은 객체도 허브마다 id 가 다르고 key 만 같다. 코어 규칙 ⑤ 는 프롬프트 글뿐이었고 유령 ID 막이는 정수만 봤다.

설계서 HWAXPortal docs/request-1030/design-ha30.md 8절의 시험(T1 ~ T22)과 합치는 몫 둘(design-integration C-6 막은 호출을 blocked 로 ·
C-8 /health xhub_gate)을 본다. 도구는 기록하는 코루틴 도구다 — 동기 @tool 은 도구 래퍼(_cap_tool)가 감싸지 않는다.

  실행:  .venv/bin/python -m pytest tests/test_request_1030_ha30.py -q
"""
import asyncio
import io
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from pydantic import Field  # noqa: E402

import app as a  # noqa: E402
from test_chat_loop_fit import _agent  # noqa: E402 — 앱이 실제로 만드는 길(_agent_for)로 에이전트를 짓는다(게이트웨이 클라이언트만 스텁)

_REAL = a.CORE_VOCAB_PATH          # 형제 포털 리포의 정본 — conftest 가 시험마다 '없음' 으로 돌리기 전의 기본 경로

U_RD = "0b6f1c3e-6a51-4c1e-9f0e-1d2c3b4a5f60"       # 정본(자주검증)의 JNT-SLD-01
U_FT = "7c2d9e41-3b8a-4f6d-a0c5-8e9f1a2b3c4d"       # FailureTrace 의 같은 key
U_PART = "3a4b5c6d-7e8f-4a1b-8c2d-9e0f1a2b3c4d"     # 정본의 PRT-PCB
U_USER = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"     # 아무 결과에도 없다
U_EDGE = "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b"


def _hit(u, key="JNT-SLD-01", label="솔더 접합 불량 + 크랙", typ="failure_mode", **extra) -> dict:
    return {"id": u, "key": key, "label": label, "type_slug": typ, "aliases": [], "status": "active", "matched_by": "key",
            "hint": "", **extra}


def _resolve(obj: dict) -> str:
    return json.dumps({"match": "exact", "object": obj, "candidates": [], "hint": "", "truncated": False}, ensure_ascii=False)


RD_RESOLVE = _resolve(_hit(U_RD))
FT_RESOLVE = _resolve(_hit(U_FT))
GRAPH_ERR = json.dumps({"error": "[APP-GRAPH-0001] 객체를 찾을 수 없습니다."}, ensure_ascii=False)
RD_GRAPH = json.dumps({"focus": U_RD,
                       "nodes": [{"id": U_RD, "key": "JNT-SLD-01", "label": "솔더 접합 불량 + 크랙", "type_slug": "failure_mode"},
                                 {"id": U_PART, "key": "PRT-PCB", "label": "PCB", "type_slug": "part"}],
                       "edges": [{"id": U_EDGE, "relation": "occurs_in_part", "src": U_RD, "dst": U_PART}], "truncated": False},
                      ensure_ascii=False)
FT_GRAPH = json.dumps({"focus": U_FT, "nodes": [{"id": U_FT, "key": "JNT-SLD-01", "label": "솔더 접합 불량", "type_slug": "failure_mode"}],
                       "edges": [], "truncated": False}, ensure_ascii=False)
RESOLVE_ARGS = {"type_slug": "failure_mode", "name": "JNT-SLD-01"}

MAP = {"rootdesigndatahub_object_resolve": "rootdesigndatahub", "rootdesigndatahub_graph_neighbors": "rootdesigndatahub",
       "failuretrace_object_resolve": "failuretrace", "failuretrace_graph_neighbors": "failuretrace",
       "failuretrace_objects_list": "failuretrace", "failuretrace_search": "failuretrace",
       "caegroupdatahub_object_get": "caegroupdatahub", "search_voc": "signalforge", "invoke_tool": "_gateway"}
LABELS = {"rootdesigndatahub": "자주검증 Datahub(rootdesign)", "failuretrace": "FailureTrace", "caegroupdatahub": "CAE 그룹 Datahub",
          "signalforge": "SignalForge VOC"}

# 설계서 3.5 의 OT-07 글 그대로 — 모델이 받는 정확한 문구
WANT_OT07 = (
    "✖ 도구 failuretrace_graph_neighbors 호출 실패(보내지 않았다): object_id=0b6f1c3e-6a51-4c1e-9f0e-1d2c3b4a5f60 는 다른 허브의 id 다 — "
    "이 턴에 자주검증 Datahub(rootdesign) 의 rootdesigndatahub_object_resolve 결과에서 나왔다(key 「JNT-SLD-01」 · type_slug failure_mode). "
    "허브마다 같은 객체도 id 가 달라 FailureTrace 에서는 이 id 로 찾지 못한다. failuretrace_object_resolve(type_slug=\"failure_mode\", "
    "name=\"JNT-SLD-01\") 로 FailureTrace 의 id 를 받아 그 id 로 다시 불러라(그 타입이 없다고 하면 failuretrace_search(q=\"JNT-SLD-01\")). "
    "candidates 면 고르지 말고 사용자에게 묻고, none 이면 'FailureTrace 에서 찾지 못했다' 고 말하라. 인자 문제가 아니다 — 같은 id 로 다시 "
    "부르지 마라.")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """막은 수는 모듈 상태다 — 시험마다 0 에서 센다. 손잡이는 기본(켬)으로 둔다(셸이 CHAT_XHUB_ID_GATE=0 을 내보내도 흔들리지 않게)."""
    monkeypatch.setattr(a, "_XHUB_STAT", {"blocked": 0})
    monkeypatch.setattr(a, "_XHUB_GATE", True)


@pytest.fixture
def turn():
    """이 턴의 상태 — 챗 턴(_agent_stream_turn)이 거는 것과 같은 빈 사전(W-5 시험의 _turn_tool_calls 와 같은 꼴)."""
    st: dict = {}
    tok = a._turn_hub_ids.set(st)
    yield st
    a._turn_hub_ids.reset(tok)


def _cache(monkeypatch, mapping, labels):
    """도구 → 앱 캐시를 앉힌다 — 묻는 함수도 막는다(떠 있는 dev 게이트웨이에 묻지 않게). tests/test_request_1029.py 의 _cache 와 같은 꼴."""
    apps = {k: {"app": k, "label": v} for k, v in labels.items()}
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "map", mapping)
    monkeypatch.setitem(a._APPS_CACHE, "apps", apps)
    monkeypatch.setattr(a, "_tools_map", lambda: mapping)
    monkeypatch.setattr(a, "_gw_apps", lambda: apps)


def _rec(name, result, log, *, mcp=False):
    """기록하는 도구 — 받은 인자 · 그때 턴 상태가 아는 id(상태가 없으면 None)를 적고 정해 둔 글(또는 인자로 고른 글)을 돌려준다. 사전 스키마라
    넘긴 인자가 그대로 간다. mcp 면 운영 꼴(content_and_artifact — 내용 블록 목록과 아티팩트)로 돌려준다."""
    async def call(**kw):
        st = a._turn_hub_ids.get()
        log.append((name, dict(kw), None if st is None else set(st)))
        text = result(kw) if callable(result) else result
        return ([{"type": "text", "text": text}], None) if mcp else text
    return a._prep_tool(StructuredTool(name=name, description="d", coroutine=call, args_schema={"type": "object", "properties": {}},
                                       **({"response_format": "content_and_artifact"} if mcp else {})),
                        arg_defaults=a._CHAT_ARG_DEFAULTS, chat=True)


def _call(tool, args):
    return asyncio.run(tool.ainvoke(args))


def _names(log) -> list:
    return [n for n, *_ in log]


# ── 8.2 단위 시험 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_T1_OT07_다른_허브의_id_를_실은_호출은_보내지_않고_key_로_다시_풀라고_돌려준다(monkeypatch, turn, capsys):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert _names(log) == ["rootdesigndatahub_object_resolve"], "failuretrace 도구는 불리지 않는다"
    assert out == WANT_OT07, out
    assert not a._result_ok(out), "실패로 센다 — 화면 활동이 실패로 그린다"
    assert a._PHANTOM_ID_MARK not in out, "유령 ID 표지는 심의가 읽는 다른 계약이다"
    assert "[이 결과는" not in out, "막은 글에는 앱 머리가 없다(부르지 않았다)"
    assert turn[U_RD] == {"apps": {"rootdesigndatahub"}, "hub": "rootdesigndatahub", "tool": "rootdesigndatahub_object_resolve",
                          "key": "JNT-SLD-01", "type": "failure_mode", "label": "솔더 접합 불량 + 크랙"}
    logs = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[gate]")]
    assert logs == ["[gate] failuretrace_graph_neighbors(object_id=0b6f1c3e…) 차단 — rootdesigndatahub 의 id 를 failuretrace 에 실었다"
                    "(다른 허브 id 1개)"], logs
    assert a._xhub_gate_health() == {"on": True, "blocked": 1}


def test_T1_운영_꼴_도구도_두_칸_꼴로_돌려준다(monkeypatch, turn):
    """게이트웨이 도구는 MCP 어댑터의 content_and_artifact 꼴이다 — 막은 글도 (글, None) 둘로 돌려줘야 LangChain 이 받는다."""
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log, mcp=True), RESOLVE_ARGS)
    assert U_RD in turn, "내용 블록 목록으로 온 결과에서도 배운다"
    t = _rec("failuretrace_graph_neighbors", FT_GRAPH, log, mcp=True)
    out = asyncio.run(t.coroutine(object_id=U_RD))
    assert out == (WANT_OT07, None) and _names(log) == ["rootdesigndatahub_object_resolve"]


def test_T2_제_허브의_id_는_그대로_부른다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    out = _call(_rec("rootdesigndatahub_graph_neighbors", RD_GRAPH, log), {"object_id": U_RD})
    assert log[-1][:2] == ("rootdesigndatahub_graph_neighbors", {"object_id": U_RD}), "인자 그대로 불린다"
    assert out.endswith(RD_GRAPH) and out.startswith("[이 결과는 자주검증 Datahub(rootdesign) 앱의")


def test_T3_대상_허브에서_다시_푼_id_로는_부른다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    _call(_rec("failuretrace_object_resolve", FT_RESOLVE, log), RESOLVE_ARGS)
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_FT})
    assert _names(log) == ["rootdesigndatahub_object_resolve", "failuretrace_object_resolve", "failuretrace_graph_neighbors"]
    assert log[-1][1] == {"object_id": U_FT} and out.endswith(FT_GRAPH)
    assert turn[U_FT]["apps"] == {"failuretrace"} and turn[U_RD]["apps"] == {"rootdesigndatahub"}


def test_T4_두_허브_결과에_다_나온_공유_id_는_지난다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    _call(_rec("failuretrace_object_resolve", _resolve(_hit(U_FT, external_ids={"core": U_RD})), log), RESOLVE_ARGS)
    _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert _names(log)[-1] == "failuretrace_graph_neighbors" and log[-1][1] == {"object_id": U_RD}
    assert turn[U_RD]["apps"] == {"rootdesigndatahub", "failuretrace"}
    assert turn[U_RD]["hub"] == "rootdesigndatahub" and turn[U_RD]["tool"] == "rootdesigndatahub_object_resolve", "처음 본 허브 · 도구"


def test_T5_invoke_tool_경유도_안쪽_이름으로_막는다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    inv = _rec("invoke_tool", FT_GRAPH, log)
    got = [_call(inv, {"name": "failuretrace_graph_neighbors", "arguments": {"object_id": U_RD}}),
           _call(inv, {"name": "failuretrace_graph_neighbors", "arguments": json.dumps({"object_id": U_RD})})]
    assert _names(log) == ["rootdesigndatahub_object_resolve"], "경유로도 보내지 않는다"
    assert got == [WANT_OT07, WANT_OT07], "arguments 가 JSON 글이어도 같은 글(칸 이름까지)이다"


def test_T6_경유_결과도_안쪽_도구의_허브로_배운다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("invoke_tool", RD_RESOLVE, log), {"name": "rootdesigndatahub_object_resolve", "arguments": RESOLVE_ARGS})
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert _names(log) == ["invoke_tool"] and out == WANT_OT07
    assert turn[U_RD]["tool"] == "rootdesigndatahub_object_resolve" and turn[U_RD]["apps"] == {"rootdesigndatahub"}


def test_T7_숨은_자리의_다른_허브_id_도_막고_모르는_id_는_글에_싣지_않는다(monkeypatch, turn):
    """objects_list 의 conditions[].value 에 실린 다른 허브 id 는 오류가 아니라 0건으로 돌아와 '없다' 로 읽힌다 — 칸 이름이 아니라 인자 전체에서
    찾는다. 대문자로 줘도 잡는다."""
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    out = _call(_rec("failuretrace_objects_list", '{"items": [], "total": 0}', log),
                {"type_slug": "pd_defect", "conditions": [{"field": "failure_mode", "op": "in", "value": f"{U_RD.upper()}|{U_USER}"}]})
    assert _names(log) == ["rootdesigndatahub_object_resolve"]
    assert f"conditions={U_RD} {a._XHUB_ID_MARK}" in out and U_USER not in out, out
    assert out.count(a._XHUB_ID_MARK) == 1 and "failuretrace_object_resolve(type_slug=\"failure_mode\", name=\"JNT-SLD-01\")" in out


def test_T8_허브가_아닌_대상은_막지_않는다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    _call(_rec("search_voc", '{"results": []}', log), {"q": U_RD})
    assert _names(log) == ["rootdesigndatahub_object_resolve", "search_voc"]


def test_T9_허브가_아닌_도구의_결과에서는_배우지_않는다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("search_voc", json.dumps({"results": [{"id": U_RD, "key": "JNT-SLD-01"}]}), log), {"q": "솔더"})
    _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert _names(log) == ["search_voc", "failuretrace_graph_neighbors"] and U_RD not in turn, turn
    assert log[1][2] == set(), "부를 때 상태는 비어 있었다 — search_voc 의 결과는 배우지 않았다"


def test_T10_오류_결과의_id_는_배우지_않는다(monkeypatch, turn):
    """오류 글은 받은 id 를 되풀이한다 — 배우면 그 허브 것으로 잘못 적어 다음 호출이 지나간다."""
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    _call(_rec("failuretrace_object_resolve", json.dumps({"error": f"[APP-OBJECTS-0001] 객체를 찾을 수 없습니다: {U_RD}"},
                                                         ensure_ascii=False), log), RESOLVE_ARGS)
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert _names(log) == ["rootdesigndatahub_object_resolve", "failuretrace_object_resolve"] and out == WANT_OT07
    assert turn[U_RD]["apps"] == {"rootdesigndatahub"}


def test_T11_이_턴에_어디서도_보지_못한_id_는_지난다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    ft = _rec("failuretrace_graph_neighbors", FT_GRAPH, log)
    _call(ft, {"object_id": U_USER})                                         # 빈 상태
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    _call(ft, {"object_id": U_USER})                                         # 허브 결과가 있어도 그 id 는 모른다
    assert _names(log) == ["failuretrace_graph_neighbors", "rootdesigndatahub_object_resolve", "failuretrace_graph_neighbors"]


def test_T12_턴_밖에서는_배우지도_막지도_않는다(monkeypatch):
    _cache(monkeypatch, MAP, LABELS)
    assert a._turn_hub_ids.get() is None, "전제 — 심의 · 카탈로그 · 루틴 · 시험의 직접 호출은 턴 상태가 없다"
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert _names(log) == ["rootdesigndatahub_object_resolve", "failuretrace_graph_neighbors"] and out.endswith(FT_GRAPH)
    assert all(st is None for *_, st in log) and a._xhub_gate_health()["blocked"] == 0


def test_T13_맵이_빈_때는_이름_접두로_가려_똑같이_막는다(monkeypatch, turn):
    _cache(monkeypatch, {}, {})
    assert a._hub_of("caegroupdatahub_object_get") == "caegroupdatahub" and a._hub_of("graph_neighbors") == ""
    assert a._hub_tool("caegroupdatahub", "object_resolve") == "caegroupdatahub_object_resolve"
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert _names(log) == ["rootdesigndatahub_object_resolve"] and out == WANT_OT07


def test_T14_맵이_있으면_맵이_가른다(monkeypatch):
    """허브 하나만 붙은 박스는 접두가 없는 맨 이름이다 — 맵이 그 백엔드를 말한다."""
    _cache(monkeypatch, {"object_resolve": "rootdesigndatahub", "search_voc": "signalforge"}, {})
    assert a._hub_of("object_resolve") == "rootdesigndatahub" and a._hub_of("search_voc") == ""
    assert a._hub_tool("rootdesigndatahub", "object_resolve") == "object_resolve", "맵의 맨 이름이 그 허브 것이면 맨 이름"
    assert a._hub_tool("failuretrace", "object_resolve") == "failuretrace_object_resolve", "아니면 접두 이름(게이트웨이 별칭)"


def test_T15_key_를_모르는_id_는_여럿_풀기_길을_준다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_graph_neighbors", json.dumps({"edges": [{"id": U_EDGE, "src": U_RD, "dst": U_PART}]}), log),
          {"object_id": U_RD})
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_PART})
    assert _names(log) == ["rootdesigndatahub_graph_neighbors"]
    assert "key 를 결과에서 읽지 못했다" in out and "failuretrace_objects_resolve_many" in out and 'name="' not in out, out
    assert "key 를 모르는 id 는 앞 허브의 결과에서 그 객체의 key 를 찾는다" in out


def test_T16_관련_객체_꼴의_이름표로_정확한_다음_호출을_준다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    got = {"object": _hit(U_RD), "related": [{"relation_id": U_EDGE, "relation": "occurs_in_part", "object_id": U_PART,
                                              "object_key": "PRT-PCB", "object_label": "PCB", "object_type_slug": "part"}]}
    _call(_rec("rootdesigndatahub_object_get", json.dumps(got, ensure_ascii=False), log), {"object_id": U_RD})
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_PART})
    assert _names(log) == ["rootdesigndatahub_object_get"]
    assert 'failuretrace_object_resolve(type_slug="part", name="PRT-PCB")' in out and "key 「PRT-PCB」 · type_slug part" in out
    assert "rootdesigndatahub_object_get 결과에서 나왔다" in out


def test_T17_접은_결과는_접기_머리를_떼고_읽고_모델이_보지_않은_id_는_배우지_않는다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    nodes = [{"id": f"{i:08x}-1111-4222-8333-{i:012x}", "key": f"N-{i}", "label": f"노드 {i} " + "x" * 120, "type_slug": "part"}
             for i in range(600)]
    big = json.dumps({"focus": U_RD, "nodes": nodes, "edges": [], "truncated": False}, ensure_ascii=False)
    assert len(big) > a._HUB_BIG_CHARS, "전제 — 접기가 걸리는 크기"
    log = []
    seen = _call(_rec("rootdesigndatahub_graph_neighbors", big, log), {"object_id": U_RD})
    assert "[포털 챗이 허브 응답을 접었다" in seen, "전제 — 접혔다"
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": nodes[10]["id"]})
    assert _names(log) == ["rootdesigndatahub_graph_neighbors"]
    assert "key 「N-10」 · type_slug part" in out and 'failuretrace_object_resolve(type_slug="part", name="N-10")' in out, out
    assert nodes[49]["id"] in turn and nodes[50]["id"] not in turn and nodes[599]["id"] not in turn, "접혀 빠진 노드는 모델이 보지 않았다"


def test_T18_여럿이면_앞_셋만_줄로_싣고_나머지는_수로_적는다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    vs = [f"{i:08x}-2222-4333-8444-{i:012x}" for i in range(1, 6)]
    graph = json.dumps({"focus": vs[0], "nodes": [{"id": v, "key": f"FM-{k}", "label": f"모드 {k}", "type_slug": "failure_mode"}
                                                  for k, v in enumerate(vs, 1)], "edges": []}, ensure_ascii=False)
    log = []
    _call(_rec("rootdesigndatahub_graph_neighbors", graph, log), {"object_id": vs[0]})
    out = _call(_rec("failuretrace_objects_list", '{"items": []}', log),
                {"type_slug": "pd_defect", "conditions": [{"field": "failure_mode", "op": "in", "value": "|".join(vs)}]})
    assert _names(log) == ["rootdesigndatahub_graph_neighbors"]
    assert out.count(a._XHUB_ID_MARK) == 3 and "그 밖에 2개도 다른 허브의 id 다." in out, out
    assert "failuretrace_objects_resolve_many" in out and 'name="' not in out
    assert [out.index(f"conditions={v} {a._XHUB_ID_MARK}") for v in vs[:3]] == sorted(
        out.index(f"conditions={v} {a._XHUB_ID_MARK}") for v in vs[:3]), "세 줄의 칸은 모두 conditions · 인자에 나온 차례"
    assert vs[3] not in out and vs[4] not in out


def test_T19_상한이_있어도_이미_있는_id_에는_허브를_더한다(monkeypatch, turn):
    """상한이 거짓 막음을 만들지 않게 — 넘은 새 id 는 모르는 id 가 되어 지나가고, 이미 적은 id 는 공유 id 로 바뀔 수 있다."""
    _cache(monkeypatch, MAP, LABELS)
    monkeypatch.setattr(a, "_HUB_IDS_MAX", 2)
    log = []
    _call(_rec("rootdesigndatahub_graph_neighbors", RD_GRAPH, log), {"object_id": U_RD})     # U_RD · U_PART · U_EDGE 차례
    assert set(turn) == {U_RD, U_PART}, turn
    _call(_rec("failuretrace_object_resolve", _resolve(_hit(U_RD)), log), RESOLVE_ARGS)
    assert turn[U_RD]["apps"] == {"rootdesigndatahub", "failuretrace"}
    _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_EDGE})
    assert _names(log)[-1] == "failuretrace_graph_neighbors", "상한 밖의 id 는 모르는 id 다 — 지난다"


def test_이름표는_공백을_줄이고_큰따옴표를_바꾸고_80자로_자른다(monkeypatch, turn):
    """글에 싣는 key · label 은 다음 호출 인자 글(name=\"…\")에 들어간다 — 따옴표 하나가 그 글을 깨지 않게."""
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", _resolve(_hit(U_RD, key='A  "B"\nC' + "k" * 100)), log), RESOLVE_ARGS)
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    want = ("A 'B' C" + "k" * 100)[:80]
    assert turn[U_RD]["key"] == want and f'name="{want}")' in out, out


def test_key_가_없고_이름만_있으면_search_로_찾게_한다(monkeypatch, turn):
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", _resolve({"id": U_RD, "label": "솔더 접합 불량"}), log), RESOLVE_ARGS)
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert "(key 없음 · 이름 「솔더 접합 불량」)" in out, out
    assert 'failuretrace_search(q="솔더 접합 불량") 로 FailureTrace 에서 그 객체를 찾아 그 id 로 다시 불러라.' in out


def test_막이_로그_줄은_바로_흘린다(monkeypatch, turn):
    """손 기동 · 감시 크론으로 뜬 서버는 stdout 이 블록 버퍼링된다 — 재시험(OT-07)이 로그 줄로 막은 수를 본다(HA-34 접기 줄과 같다)."""
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)

    class _Stdout(io.StringIO):
        flushed = ""

        def flush(self):
            self.flushed = self.getvalue()
            super().flush()
    rec = _Stdout()
    monkeypatch.setattr(sys, "stdout", rec)
    _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert "[gate] failuretrace_graph_neighbors(object_id=0b6f1c3e…) 차단" in rec.flushed, rec.getvalue()


# ── 허브 표(코어 어휘표의 canonical · role sibling) ─────────────────────────────────────────────────────────────────────
def test_어휘표가_없으면_허브는_기본표_셋이다():
    assert a._hub_apps() == {"rootdesigndatahub": "자주검증 Datahub(rootdesign)", "failuretrace": "FailureTrace",
                             "caegroupdatahub": "CAE 그룹 Datahub"}


def test_형제_포털의_정본에서_허브를_읽고_시험_사본은_넣지_않는다(monkeypatch):
    if not _REAL.exists():
        pytest.skip("형제 포털 리포가 곁에 없다")
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", _REAL)
    assert "hubs" in a._core_vocab(), "기본표가 아니라 어휘표에서 읽었다"
    vocab = json.loads(_REAL.read_text(encoding="utf-8"))
    want = {vocab["canonical"]["app"]: vocab["canonical"]["label"],
            **{k: v["label"] for k, v in vocab["apps"].items() if v.get("role") == "sibling"}}
    assert a._hub_apps() == want
    assert set(want) == {"rootdesigndatahub", "failuretrace", "caegroupdatahub"} and "stepforge-prod" not in want and \
        "dynaforge-prod" not in want, "role staging(시험 사본)은 허브가 아니다"


def test_임시_어휘표의_sibling_이_허브가_되고_health_꼴은_그대로다(monkeypatch, tmp_path):
    p = tmp_path / "core_vocab.json"
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    p.write_text(json.dumps({"version": 1, "canonical": {"app": "hub", "label": "허브"}, "rule_words": ["낱말"], "relations": [],
                             "apps": {"x": {"role": "sibling", "label": "X허브"}, "y": {"role": "staging", "label": "Y"},
                                      "z": {"role": "noncore", "label": "Z"}}}, ensure_ascii=False), encoding="utf-8")
    assert a._hub_apps() == {"hub": "허브", "x": "X허브"}
    assert set(a._core_vocab_health()) == {"loaded", "version", "rule_words", "canonical", "fingerprint", "base", "overlay"}, \
        "/health core_vocab 은 칸을 골라 싣는다 — hubs 는 싣지 않는다"
    assert a._hub_of("x_object_resolve") == "x" and a._hub_of("failuretrace_object_resolve") == "", "어휘표가 허브를 정한다"


# ── 합치는 몫 — C-6 막은 호출의 회계 · C-8 /health ────────────────────────────────────────────────────────────────────
class _ScriptAgent:
    """스트림 사건을 정해 둔 대로 내는 에이전트 — 막힌 호출 하나 뒤 '조회하겠습니다' 로 끝난다. 다시 부르면(강제 재시도) 센다."""

    def __init__(self, blocked_text: str):
        self.blocked_text = blocked_text
        self.again = 0

    async def astream_events(self, inputs, version=None, config=None):
        yield {"event": "on_tool_start", "name": "failuretrace_graph_neighbors", "run_id": "r1",
               "data": {"input": {"object_id": U_RD}}}
        yield {"event": "on_tool_end", "name": "failuretrace_graph_neighbors", "run_id": "r1",
               "data": {"input": {"object_id": U_RD}, "output": ToolMessage(content=self.blocked_text, tool_call_id="r1")}}
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content="FailureTrace 에서 조회하겠습니다")}}

    async def ainvoke(self, payload, config=None):
        self.again += 1
        return {"messages": [types.SimpleNamespace(type="ai", content="다시 돌린 답")]}


def test_C6_다른_허브_id_로_막힌_호출만_있으면_도구를_쓰지_않은_턴으로_센다(monkeypatch):
    """막은 호출을 '도구를 썼다' 로 세면 그 턴이 no-tool 로 잡히지 않아 강제 재시도 · 확정 종결이 서지 않는다(유령 ID 막이와 같다)."""
    tok = a._turn_hub_ids.set({U_RD: {"apps": {"rootdesigndatahub"}, "hub": "rootdesigndatahub",
                                      "tool": "rootdesigndatahub_object_resolve", "key": "JNT-SLD-01", "type": "failure_mode", "label": ""}})
    try:
        _cache(monkeypatch, MAP, LABELS)
        blocked = a._cross_hub_id_msg("failuretrace_graph_neighbors", {"object_id": U_RD})
    finally:
        a._turn_hub_ids.reset(tok)
    assert blocked and a._XHUB_ID_MARK in blocked[:400], "전제 — 표지가 글 머리 400자 안이다"
    agent = _ScriptAgent(blocked)

    async def _built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", _built)
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    req = a.ChatRequest(message="JNT-SLD-01 의 연결을 보여줘", groups=["portal-admin"], history=[])

    async def run():
        return [c.decode() if isinstance(c, bytes) else str(c) async for c in a._agent_stream(a.app, req)]
    steps = [json.loads(c.split("data: ", 1)[1]).get("step") for c in asyncio.run(run()) if c.startswith("event: status\n")]
    assert "도구를 실제로 호출하도록 다시 시도합니다" in steps and agent.again == 1, steps


def test_C8_health_에_xhub_gate_가_자기검증_다음_코어_어휘표_앞에_실린다(monkeypatch, turn):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})   # 창을 박아 둔다 — /health 가 LLM 서버에 묻지 않게
    c = TestClient(a.app)
    h = c.get("/health").json()
    assert h["xhub_gate"] == {"on": True, "blocked": 0}
    keys = list(h)
    assert keys.index("self_check") < keys.index("xhub_gate") < keys.index("core_vocab"), "design-integration 5절의 차례"
    _cache(monkeypatch, MAP, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert c.get("/health").json()["xhub_gate"] == {"on": True, "blocked": 1}
    monkeypatch.setattr(a, "_XHUB_GATE", False)
    assert c.get("/health").json()["xhub_gate"] == {"on": False, "blocked": 1}, "손잡이를 끈 박스를 밖에서 가린다"


# ── 8.3 실제 경로(한 턴) ─────────────────────────────────────────────────────────────────────────────────────────────
class _HubLLM(BaseChatModel):
    """대본대로 도구를 부르고 답하는 모델 — 받은 메시지의 도구 결과 수로 차례를 안다. 받은 메시지를 호출마다 적는다. 스트림 길로도 같은 답을
    한 조각으로 낸다(운영 기본이 스트리밍이다 — test_chat_loop_fit 의 _LoopLLM 과 같은 꼴)."""

    script: list = Field(default_factory=list)       # [(도구 이름, 인자) | 답 글]
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "hub-script"

    def bind_tools(self, tools, **_kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        self.seen.append(list(messages))
        done = sum(isinstance(m, ToolMessage) for m in messages)
        step = self.script[done] if done < len(self.script) else "대본 끝"
        msg = (AIMessage(content=step) if isinstance(step, str)
               else AIMessage(content="", tool_calls=[{"name": step[0], "args": step[1], "id": f"h{done + 1}"}]))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        msg = self._generate(messages, stop, run_manager, **kw).generations[0].message
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content, tool_call_chunks=[
            {"name": c["name"], "args": json.dumps(c["args"], ensure_ascii=False), "id": c["id"], "index": i}
            for i, c in enumerate(msg.tool_calls)]))


OT07_SCRIPT = [("rootdesigndatahub_object_resolve", RESOLVE_ARGS), ("failuretrace_graph_neighbors", {"object_id": U_RD}),
               ("failuretrace_object_resolve", RESOLVE_ARGS), ("failuretrace_graph_neighbors", {"object_id": U_FT}),
               "FailureTrace 의 JNT-SLD-01 은 불량 기록 하나와 이어져 있다."]


def _hub_turns(monkeypatch, llm, log, ft_graph=FT_GRAPH):
    """앱이 짓는 길(_agent_for)로 에이전트를 지어 두고 _agent_stream 을 부르는 함수를 돌려준다 — 캐시는 짓기 전에 앉힌다(_prep_tool 이 묻는다)."""
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    _cache(monkeypatch, MAP, LABELS)
    tools = (_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), _rec("failuretrace_graph_neighbors", ft_graph, log),
             _rec("failuretrace_object_resolve", FT_RESOLVE, log))
    agent = _agent(monkeypatch, llm, tools=tools)

    async def _built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", _built)

    def one(message):
        req = a.ChatRequest(message=message, groups=["portal-admin"], history=[])

        async def run():
            return [c.decode() if isinstance(c, bytes) else str(c) async for c in a._agent_stream(a.app, req)]
        evs = []
        for c in asyncio.run(run()):
            head, _, body = c.partition("\ndata: ")
            evs.append((head.removeprefix("event: "), json.loads(body)))
        return evs
    return one


def _done(evs, name) -> list:
    return [d for e, d in evs if e == "status" and d.get("step") == f"도구 완료: {name}"]


def test_T20_OT07_한_턴에서_막힌_뒤_대상_허브에서_다시_풀어_부른다(monkeypatch):
    log = []
    llm = _HubLLM(script=list(OT07_SCRIPT))
    evs = _hub_turns(monkeypatch, llm, log)("JNT-SLD-01 의 불량 기록 연결을 FailureTrace 에서 보여줘")
    assert [kw for n, kw, _ in log if n == "failuretrace_graph_neighbors"] == [{"object_id": U_FT}], log
    last = [m for m in llm.seen[2] if isinstance(m, ToolMessage)][-1].content
    assert a._XHUB_ID_MARK in last and 'failuretrace_object_resolve(type_slug="failure_mode", name="JNT-SLD-01")' in last, last
    assert [d["ok"] for d in _done(evs, "failuretrace_graph_neighbors")] == [False, True]
    assert all(st is not None for *_, st in log), "턴 안의 도구는 턴 상태를 본다"
    result = [d for e, d in evs if e == "result"]
    assert result and "불량 기록 하나와 이어져 있다" in result[-1]["content"], result
    assert not [d for e, d in evs if e == "error"], evs


def test_T21_손잡이를_끄면_종전처럼_보내고_허브의_오류를_받는다(monkeypatch):
    monkeypatch.setattr(a, "_XHUB_GATE", False)
    log = []
    llm = _HubLLM(script=list(OT07_SCRIPT))

    def ft(kw):
        return GRAPH_ERR if kw.get("object_id") == U_RD else FT_GRAPH
    evs = _hub_turns(monkeypatch, llm, log, ft_graph=ft)("JNT-SLD-01 의 불량 기록 연결을 FailureTrace 에서 보여줘")
    assert [kw for n, kw, _ in log if n == "failuretrace_graph_neighbors"] == [{"object_id": U_RD}, {"object_id": U_FT}]
    second = [m for m in llm.seen[2] if isinstance(m, ToolMessage)][1].content
    assert "[APP-GRAPH-0001]" in second and a._XHUB_ID_MARK not in second, second
    assert all(st is None for *_, st in log), "턴 상태를 걸지 않는다 — 배우지도 막지도 않는다"
    assert not [d for e, d in evs if e == "error"], evs


def test_T22_앞_턴에_배운_것은_다음_턴에_남지_않는다(monkeypatch):
    log = []
    llm = _HubLLM(script=list(OT07_SCRIPT))
    one = _hub_turns(monkeypatch, llm, log)
    one("JNT-SLD-01 의 불량 기록 연결을 FailureTrace 에서 보여줘")
    llm.script, llm.seen = [("failuretrace_graph_neighbors", {"object_id": U_RD}), "FailureTrace 에서 받은 결과다."], []
    log.clear()
    evs = one("아까 그 id 로 FailureTrace 그래프를 다시 보여줘")
    assert log and log[0][:2] == ("failuretrace_graph_neighbors", {"object_id": U_RD}), "새 턴은 빈 상태 — 그 id 를 모른다"
    assert log[0][2] == set(), "턴마다 새 사전"
    assert [d["ok"] for d in _done(evs, "failuretrace_graph_neighbors")] == [True]


# ── 반대 검토 2차(rv2) — 이 호출이 실어 보낸 id 의 메아리는 배우지 않는다 · invoke_tool 의 안쪽 이름 앞 공백 ──────────────────────────────────
# StandardPlatform 백엔드가 실제로 내는 성공 꼴의 '없다' 답 — 받은 id 를 되풀이한다(modules/objects/resolve.py · search/routes.py · objects/schemas.py
# ResolveOneOut · objects/routes.py 의 0건 진단). 꼴은 StandardPlatform main 778db2f(0.4.67)의 응답 모델 그대로다 — 묶음 풀이의 줄도 hint 로, 0건
# 진단의 message · next_steps 도 그 조건 글로 한 번 더 되풀이한다. 0.4.66(418af00)부터 search 는 게시판 글(posts)도 싣고, 글의 snippet 은 본문에서
# q 가 든 자리 앞뒤라(board/services.py snippet) 그 id 를 적은 글이 있으면 거기서도 되풀이된다. 글의 id 는 그 허브의 글이다.
U_POST = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b"     # FailureTrace 게시판 글
_SEARCH_NONE = {"q": U_RD, "total": 0, "types": [], "records": 0, "items": [], "limit": 20, "offset": 0, "min_query": 2}
_COND = f"failure_mode eq {U_RD}"
RV2_ECHO = {
    "resolve_none_hint": ("failuretrace_object_resolve", {"type_slug": "failure_mode", "name": U_RD},
                          json.dumps({"match": "none", "object": None, "candidates": [], "hint": f"그 id 의 고장모드이(가) 없습니다: {U_RD}",
                                      "truncated": False}, ensure_ascii=False)),
    "search_q": ("failuretrace_search", {"q": U_RD},
                 json.dumps({**_SEARCH_NONE, "posts": [], "posts_total": 0}, ensure_ascii=False)),
    "search_post_snippet": ("failuretrace_search", {"q": U_RD},
                            json.dumps({**_SEARCH_NONE, "posts_total": 1, "posts": [
                                {"id": U_POST, "title": "솔더 접합 크랙 재현 메모", "author_name": "작성자 A", "created_at": "2026-10-29T08:00:00Z",
                                 "comment_count": 1, "snippet": f"…정본에서 받은 id {U_RD} 로 다시 찾아 보니 그래프에 이어진 부품이…"}]},
                                       ensure_ascii=False)),
    "resolve_many_name": ("failuretrace_objects_resolve_many", {"type_slug": "failure_mode", "names": [U_RD]},
                          json.dumps({"items": [{"match": "none", "object": None, "candidates": [],
                                                 "hint": f"그 id 의 고장모드이(가) 없습니다: {U_RD}", "truncated": False, "name": U_RD}],
                                      "counts": {"exact": 0, "candidates": 0, "none": 1}}, ensure_ascii=False)),
    "list_diagnosis": ("failuretrace_objects_list", {"type_slug": "pd_defect", "conditions": [{"field": "failure_mode", "op": "eq", "value": U_RD}]},
                       json.dumps({"items": [], "total": 0, "limit": 50, "offset": 0, "files": {}, "diagnosis": {
                           "total": 0, "reason": "filters",
                           "message": f"불량에 보이는 것은 12건인데 조건이 좁아 0건이 됐습니다. 「{_COND}」 하나만 빼면 12건입니다.",
                           "type_total": 12, "hidden": 0,
                           "filters": [{"name": "f.failure_mode.eq#0", "label": _COND, "remaining": 12, "unknown": 0}],
                           "next_steps": [f"「{_COND}」 을(를) 빼고 다시 묻는다.",
                                          "칸 이름이 맞는지 object_fields 로 확인한다 — 값이 비어 있는 칸일 수 있다."]}},
                                  ensure_ascii=False)),
}
MAP_RV2 = {**MAP, "failuretrace_objects_resolve_many": "failuretrace", "rootdesigndatahub_object_get": "rootdesigndatahub"}


@pytest.mark.parametrize("form", list(RV2_ECHO))
def test_rv2_다른_허브의_없다_답에_되풀이된_id_는_그_허브_것으로_배우지_않는다(monkeypatch, turn, form):
    """사람이 친 id(이 턴의 허브 결과에 아직 없다)를 먼저 FailureTrace 에 물어 '없다' 를 받은 뒤 진짜 주인 허브(정본)를 부른다 — 종전엔 메아리를
    FailureTrace 것으로 배워 정본 호출이 막히고 '정본에서는 이 id 로 찾지 못한다' 는 거짓 안내를 받았다."""
    _cache(monkeypatch, MAP_RV2, LABELS)
    tool, args, body = RV2_ECHO[form]
    log = []
    _call(_rec(tool, body, log), args)
    assert U_RD not in turn, "메아리는 배우지 않는다"
    out = _call(_rec("rootdesigndatahub_graph_neighbors", RD_GRAPH, log), {"object_id": U_RD})
    assert _names(log) == [tool, "rootdesigndatahub_graph_neighbors"] and a._XHUB_ID_MARK not in out, out[:300]


@pytest.mark.parametrize("echo_first", [True, False])
def test_rv2_메아리는_공유_id_를_만들지_않아_OT07_꼴이_막힌다(monkeypatch, turn, echo_first):
    _cache(monkeypatch, MAP_RV2, LABELS)
    log = []
    echo = _rec("failuretrace_search", RV2_ECHO["search_q"][2], log)
    owner = _rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log)
    for t, args in ((echo, {"q": U_RD}), (owner, RESOLVE_ARGS))[::(1 if echo_first else -1)]:
        _call(t, args)
    assert turn[U_RD]["apps"] == {"rootdesigndatahub"}, turn[U_RD]
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert "failuretrace_graph_neighbors" not in _names(log) and a._XHUB_ID_MARK in out


def test_rv2_실어_보낸_id_라도_허브가_객체_행으로_돌려주면_배운다(monkeypatch, turn):
    _cache(monkeypatch, MAP_RV2, LABELS)
    log = []
    got = json.dumps({"object": _hit(U_RD), "relations": []}, ensure_ascii=False)
    _call(_rec("rootdesigndatahub_object_get", got, log), {"object_id": U_RD})
    assert turn[U_RD]["apps"] == {"rootdesigndatahub"} and turn[U_RD]["key"] == "JNT-SLD-01"
    out = _call(_rec("failuretrace_graph_neighbors", FT_GRAPH, log), {"object_id": U_RD})
    assert a._XHUB_ID_MARK in out


def test_rv2_invoke_tool_의_안쪽_이름_앞_공백으로도_막이를_비켜_가지_못한다(monkeypatch, turn):
    """게이트웨이는 invoke_tool 의 name 을 strip() 해서 찾는다 — 막이도 같은 이름으로 본다."""
    _cache(monkeypatch, MAP_RV2, LABELS)
    log = []
    _call(_rec("rootdesigndatahub_object_resolve", RD_RESOLVE, log), RESOLVE_ARGS)
    out = _call(_rec("invoke_tool", FT_GRAPH, log), {"name": " failuretrace_graph_neighbors", "arguments": {"object_id": U_RD}})
    assert _names(log) == ["rootdesigndatahub_object_resolve"] and a._XHUB_ID_MARK in out
    assert a._hub_of("  failuretrace_graph_neighbors ") == "failuretrace"


def test_rv2_실제_턴_사람이_친_id_를_먼저_다른_허브에_물어도_주인_허브는_닿는다(monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    _cache(monkeypatch, MAP_RV2, LABELS)
    log = []
    tool, args, body = RV2_ECHO["resolve_none_hint"]
    tools = (_rec(tool, body, log), _rec("rootdesigndatahub_graph_neighbors", RD_GRAPH, log))
    llm = _HubLLM(script=[(tool, args), ("rootdesigndatahub_graph_neighbors", {"object_id": U_RD}), "답 글."])
    agent = _agent(monkeypatch, llm, tools=tools)

    async def _built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", _built)
    req = a.ChatRequest(message=f"{U_RD} 이 고장모드가 FailureTrace 에 있는지 보고, 정본에서는 무엇과 이어져 있는지 알려줘",
                        groups=["portal-admin"], history=[])

    async def run():
        return [c async for c in a._agent_stream(a.app, req)]
    asyncio.run(run())
    assert _names(log) == [tool, "rootdesigndatahub_graph_neighbors"], "정본 그래프 호출이 닿았다"
    last = [m for m in llm.seen[2] if isinstance(m, ToolMessage)][-1].content
    assert a._XHUB_ID_MARK not in last and U_PART in last, last[:300]

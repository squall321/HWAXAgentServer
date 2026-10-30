# 다른 허브의 id 막이(HWAXPortal docs/request_261030.md HA-30)를 설계서만 보고 쓴 블랙박스 시험 — 구현 가지는 보지 않았다
"""10-30 OT-07 — failuretrace_graph_neighbors 에 자주검증 허브의 id 를 넘겨 [APP-GRAPH-0001] 이었다(코어 규칙 ⑤ 가 있는데도). 허브 id 는
설치마다 따로 만든 UUID 라 같은 객체도 허브마다 id 가 다르고 key 는 같다. 설계(HWAXPortal docs/request-1030/design-ha30.md)는 이번 턴에 허브
도구가 성공으로 돌려준 결과에서 UUID → 허브를 배우고, 허브 도구 인자 어디에든 다른 허브에서만 본 UUID 가 있으면 보내지 않고 '그 허브의
object_resolve(type_slug, key) 로 다시 풀어라' 를 돌려준다. 코드가 대신 다시 풀지 않는다(설계 5절).

설계서 8절의 시험(T1 ~ T22)과 3절의 이름 · 손잡이 · 글 · 차례, design-integration.md 의 래퍼 차례(G3 → G4)와 /health 칸(C-8)을 그대로
옮겼다. 설계가 열어 둔 꼴은 맨 위 '가정' 절의 도우미 하나씩에 가뒀다 — 구현과 어긋나면 그 도우미만 고친다. 꾸밈은 기존 시험의 길이다 —
도구 → 앱 캐시는 tests/test_request_1029.py 의 _cache, 실제 경로는 tests/test_chat_loop_fit.py 의 _agent(앱이 만드는 _agent_for · 게이트웨이
클라이언트만 스텁)와 받은 도구 결과 수로 차례를 아는 대본 모델이다.

  실행:  .venv/bin/python -m pytest tests/test_bb_ha30.py -q
"""
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as a  # noqa: E402

_REAL = a.CORE_VOCAB_PATH          # 형제 포털 리포의 정본 어휘표 — conftest 가 시험마다 '없음' 으로 돌리기 전의 기본 경로


# ── 가정 — 설계가 열어 둔 꼴(구현과 어긋나면 여기만 고친다) ──────────────────────────────────────────────────────────────
@contextlib.contextmanager
def _hub_turn():
    """챗 턴 하나의 허브 id 상태를 건다(설계 3.1 · 8.1 — `_turn_hub_ids.set({})`). 값은 {uuid(소문자): {apps, hub, tool, key, type, label}}."""
    st: dict = {}
    tok = a._turn_hub_ids.set(st)
    try:
        yield st
    finally:
        a._turn_hub_ids.reset(tok)


def _apps(st: dict, uid: str) -> set:
    """그 id 를 본 허브들(설계 3.1 의 apps — 집합). 목록으로 둬도 읽는다."""
    return set(st[uid]["apps"])


def _first_tool(st: dict, uid: str) -> str:
    """그 id 를 처음 낸 도구 이름(설계 3.1 의 tool — 경유면 안쪽 이름)."""
    return st[uid]["tool"]


def _uuid_list(text: str) -> list:
    """글의 UUID(설계 3.3 의 4 — 소문자로 맞추고 차례를 지켜 겹침을 뺀다). _uuids(글) 가 차례 있는 모음을 돌려준다고 본다."""
    return list(a._uuids(text))


def _xhub_health(client) -> dict:
    """/health 의 다른 허브 id 막이 칸(design-integration 5절 · C-8 — `xhub_gate = {on, blocked}`)."""
    return client.get("/health").json()["xhub_gate"]


def _same_but_field(got: str, want: str, field: str) -> bool:
    """invoke_tool 의 arguments 가 JSON 글이면 칸(그 id 가 든 맨 위 인자 이름)을 못 가를 수 있다 — 설계 3.5 의 2('못 가르면 arguments')와
    8.2 T5('같은 글이다')를 함께 받는다. 글을 풀어 칸을 찾으면 dict 로 부른 것과 같은 글이고, 풀지 않으면 칸만 arguments 다."""
    return got == want or got == want.replace(f"{field}=", "arguments=", 1)


def _gate_log(capsys) -> list[str]:
    """막은 호출의 로그 줄(설계 3.4 의 5 — `[gate] <도구>(<칸>=<id 앞 8자>…) 차단 — <출처> 의 id 를 <대상> 에 실었다(다른 허브 id N개)`).
    유령 ID 막이처럼 표준 출력에 찍는다고 보지만 표준 오류도 함께 읽는다."""
    cap = capsys.readouterr()
    return [ln for ln in (cap.out + "\n" + cap.err).splitlines() if ln.startswith("[gate] ") and "다른 허브 id" in ln]


# ── 값 · 결과 글(설계 8.1) ──────────────────────────────────────────────────────────────────────────────────────────────
U_RD = "0b6f1c3e-6a51-4c1e-9f0e-1d2c3b4a5f60"      # 정본(자주검증) 고장모드 JNT-SLD-01
U_FT = "7c2d9e41-3b8a-4f6d-a0c5-8e9f1a2b3c4d"      # FailureTrace 의 같은 key
U_PART = "3a4b5c6d-7e8f-4a1b-8c2d-9e0f1a2b3c4d"    # 정본 부품 PRT-PCB
U_USER = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a"    # 아무 결과에도 없다(사람이 친 id · 앞 턴의 id)
U_EDGE = "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b"    # 간선 id
U_REL = "5f4e3d2c-1b0a-4c9d-8e7f-6a5b4c3d2e1f"     # 관계 id
U_DEF = "2b3c4d5e-6f70-4812-9a3b-4c5d6e7f8091"     # FailureTrace 의 불량 기록

# 허브 표 — 어휘표가 없을 때(conftest 기본)의 기본표이자 정본 어휘표의 정본 · sibling 라벨(설계 3.2)
HUB_LABEL = {"rootdesigndatahub": "자주검증 Datahub(rootdesign)", "failuretrace": "FailureTrace", "caegroupdatahub": "CAE 그룹 Datahub"}

# 게이트웨이 /tools-map 의 노출 이름 → 백엔드 키(설계 8.1 의 맵). 아래 다섯은 이 파일의 시험이 더 부르는 허브 도구다.
MAP = {"rootdesigndatahub_object_resolve": "rootdesigndatahub", "rootdesigndatahub_graph_neighbors": "rootdesigndatahub",
       "failuretrace_object_resolve": "failuretrace", "failuretrace_graph_neighbors": "failuretrace",
       "failuretrace_objects_list": "failuretrace", "failuretrace_search": "failuretrace",
       "caegroupdatahub_object_get": "caegroupdatahub", "search_voc": "signalforge", "invoke_tool": "_gateway",
       "rootdesigndatahub_object_get": "rootdesigndatahub", "rootdesigndatahub_objects_list": "rootdesigndatahub",
       "rootdesigndatahub_search": "rootdesigndatahub", "failuretrace_relation_add": "failuretrace",
       "failuretrace_objects_delete": "failuretrace"}


def _hit(uid, key="JNT-SLD-01", label="솔더 접합 불량 + 크랙", type_slug="failure_mode", **extra) -> dict:
    return {"id": uid, "key": key, "label": label, "type_slug": type_slug, **extra}


def _resolve(obj: dict) -> str:
    return json.dumps({"match": "exact", "object": {**obj, "aliases": [], "status": "active", "matched_by": "key", "hint": ""},
                       "candidates": [], "hint": "", "truncated": False}, ensure_ascii=False)


RD_RESOLVE = _resolve(_hit(U_RD))
FT_RESOLVE = _resolve(_hit(U_FT))
GRAPH_ERR = json.dumps({"error": "[APP-GRAPH-0001] 객체를 찾을 수 없습니다."}, ensure_ascii=False)
RD_GRAPH = json.dumps({"focus": U_RD, "nodes": [_hit(U_RD), _hit(U_PART, "PRT-PCB", "PCB", "part")],
                       "edges": [{"id": U_EDGE, "relation": "occurs_in_part", "src": U_RD, "dst": U_PART}], "truncated": False},
                      ensure_ascii=False)
FT_GRAPH = json.dumps({"focus": U_FT, "nodes": [_hit(U_FT), _hit(U_DEF, "PD-0001", "불량 기록", "pd_defect")], "edges": [],
                       "truncated": False}, ensure_ascii=False)
RESOLVE_ARGS = {"type_slug": "failure_mode", "name": "JNT-SLD-01"}
WHO_RD = "key 「JNT-SLD-01」 · type_slug failure_mode"

# OT-07 을 다시 지은 경우에 모델이 받는 글(설계 3.5 — 정확한 문구)
OT07 = (f"{a._TOOL_FAIL_MARK} 도구 failuretrace_graph_neighbors 호출 실패(보내지 않았다): object_id={U_RD} 는 다른 허브의 id 다 — 이 턴에 "
        "자주검증 Datahub(rootdesign) 의 rootdesigndatahub_object_resolve 결과에서 나왔다(key 「JNT-SLD-01」 · type_slug failure_mode). "
        "허브마다 같은 객체도 id 가 달라 FailureTrace 에서는 이 id 로 찾지 못한다. "
        'failuretrace_object_resolve(type_slug="failure_mode", name="JNT-SLD-01") 로 FailureTrace 의 id 를 받아 그 id 로 다시 불러라'
        '(그 타입이 없다고 하면 failuretrace_search(q="JNT-SLD-01")). '
        "candidates 면 고르지 말고 사용자에게 묻고, none 이면 'FailureTrace 에서 찾지 못했다' 고 말하라. "
        "인자 문제가 아니다 — 같은 id 로 다시 부르지 마라.")


# 글의 조각(설계 3.5 의 1 ~ 6) — 대상 허브의 도구 이름은 이 파일의 허브 셋에서 '<허브 키>_<도구>' 다(_hub_tool)
def _head(target: str) -> str:
    return f"{a._TOOL_FAIL_MARK} 도구 {target} 호출 실패(보내지 않았다): "


def _line(field: str, uid: str, src: str, src_tool: str, who: str) -> str:
    return f"{field}={uid} 는 다른 허브의 id 다 — 이 턴에 {HUB_LABEL[src]} 의 {src_tool} 결과에서 나왔다({who})."


def _why(dst: str) -> str:
    return f"허브마다 같은 객체도 id 가 달라 {HUB_LABEL[dst]} 에서는 이 id 로 찾지 못한다."


def _fix_resolve(dst: str, t: str, k: str) -> str:
    return (f'{dst}_object_resolve(type_slug="{t}", name="{k}") 로 {HUB_LABEL[dst]} 의 id 를 받아 그 id 로 다시 불러라'
            f'(그 타입이 없다고 하면 {dst}_search(q="{k}")).')


def _fix_search(dst: str, k: str) -> str:
    return f'{dst}_search(q="{k}") 로 {HUB_LABEL[dst]} 에서 그 객체를 찾아 그 id 로 다시 불러라.'


def _fix_many(dst: str) -> str:
    return (f"각 객체의 key 로 {dst}_object_resolve(type_slug, name=key) — 여럿이면 {dst}_objects_resolve_many(type_slug, names=[…]) — "
            f"를 불러 {HUB_LABEL[dst]} 의 id 를 받아 다시 불러라. key 를 모르는 id 는 앞 허브의 결과에서 그 객체의 key 를 찾는다.")


def _tail(dst: str) -> str:
    return (f"candidates 면 고르지 말고 사용자에게 묻고, none 이면 '{HUB_LABEL[dst]} 에서 찾지 못했다' 고 말하라. "
            "인자 문제가 아니다 — 같은 id 로 다시 부르지 마라.")


# ── 꾸밈 ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _cache(monkeypatch, mapping, labels=None):
    """도구 → 앱 캐시를 앉힌다 — 묻는 함수도 막는다(tests/test_request_1029.py 의 _cache 와 같다). 게이트웨이 앱 라벨은 기본으로 비워 둔다 —
    글의 허브 라벨은 게이트웨이 라벨이 아니라 허브 표(_hub_apps — 어휘표 · 기본표)의 것이다(설계 3.2 · 3.5)."""
    apps = {k: {"app": k, "label": v} for k, v in (labels or {}).items()}
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "map", mapping)
    monkeypatch.setitem(a._APPS_CACHE, "apps", apps)
    monkeypatch.setattr(a, "_tools_map", lambda: mapping)
    monkeypatch.setattr(a, "_gw_apps", lambda: apps)


@pytest.fixture(autouse=True)
def _box(monkeypatch):
    """켠 손잡이와 설계 8.1 의 맵. 셸이 CHAT_XHUB_ID_GATE=0 을 내보냈어도 이 파일은 켠 꼴을 본다 — 끈 꼴은 제 손으로 끈다(T21)."""
    monkeypatch.setattr(a, "_XHUB_GATE", True, raising=False)
    _cache(monkeypatch, MAP)


def _tool(name, reply, *, mcp=False, chat=True):
    """기록하는 도구 — 받은 인자를 적고 정해 둔 글(또는 인자로 고른 글)을 돌려준다. 챗 경로로 싼다(설계 8.1 — 코루틴 도구여야 _cap_tool 이
    감싼다). mcp 면 게이트웨이 도구의 운영 꼴(content_and_artifact — 내용 블록 목록과 아티팩트 둘)이다. reply 가 예외면 그것을 올린다."""
    calls: list = []

    async def call(**kw):
        calls.append(dict(kw))
        if isinstance(reply, Exception):
            raise reply
        text = reply(kw) if callable(reply) else reply
        return ([{"type": "text", "text": text}], None) if mcp else text
    raw = StructuredTool(name=name, description="d", coroutine=call, args_schema={"type": "object", "properties": {}},
                         **({"response_format": "content_and_artifact"} if mcp else {}))
    return a._prep_tool(raw, arg_defaults=a._CHAT_ARG_DEFAULTS if chat else None, chat=chat), calls


def _run(tool, args):
    return asyncio.run(tool.ainvoke(args))


# ── 래퍼 단위 시험(설계 8.2) ────────────────────────────────────────────────────────────────────────────────────────
def test_T1_OT07_다른_허브에서만_본_id_를_실은_허브_도구는_부르지_않고_key_로_다시_풀라고_돌려준다(capsys):
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn() as st:
        _run(rd, RESOLVE_ARGS)
        out = _run(ft, {"object_id": U_RD})
    assert ft_calls == [], "보내지 않는다 — 허브가 [APP-GRAPH-0001] 로 돌려줄 호출이다"
    assert out.startswith(_head("failuretrace_graph_neighbors")), out
    for must in (f"object_id={U_RD} 는 다른 허브의 id 다", "자주검증 Datahub(rootdesign) 의 rootdesigndatahub_object_resolve 결과",
                 WHO_RD, 'failuretrace_object_resolve(type_slug="failure_mode", name="JNT-SLD-01")',
                 'failuretrace_search(q="JNT-SLD-01")', "FailureTrace 에서는 이 id 로 찾지 못한다"):
        assert must in out, must
    assert a._result_ok(out) is False, "✖ 머리 — 화면 활동이 실패로 그린다"
    assert a._PHANTOM_ID_MARK not in out, "유령 ID 표지는 심의가 '실행되지 않은 호출' 로 읽는 다른 계약이다(설계 3.5)"
    assert "[이 결과는" not in out, "막은 호출은 앱 머리 앞에서 끝난다"
    assert out == OT07, "설계 3.5 의 정확한 문구"
    assert _apps(st, U_RD) == {"rootdesigndatahub"} and _first_tool(st, U_RD) == "rootdesigndatahub_object_resolve"
    logs = _gate_log(capsys)
    assert len(logs) == 1, logs
    assert logs[0].startswith("[gate] failuretrace_graph_neighbors(") and "0b6f1c3e" in logs[0], logs[0]
    assert "차단" in logs[0] and "다른 허브 id 1개" in logs[0], logs[0]


def test_T1_같은_id_로_거듭_불러도_같은_글로_막고_W5_되풀이_계수에는_들지_않는다():
    """설계 3.4 끝 · 9.2 Q7 — 결정적으로 같은 글이고, 막은 호출은 _chat_result_note 앞에서 끝나 '번째 불렀다' 계수에 들지 않는다."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    cnt: Counter = Counter()
    tok = a._turn_tool_calls.set(cnt)
    try:
        with _hub_turn():
            _run(rd, RESOLVE_ARGS)
            outs = [_run(ft, {"object_id": U_RD}) for _ in range(4)]
    finally:
        a._turn_tool_calls.reset(tok)
    assert ft_calls == [] and outs == [OT07] * 4
    assert cnt["failuretrace_graph_neighbors"] == 0 and cnt["rootdesigndatahub_object_resolve"] == 1, cnt


def test_T2_제_허브의_id_는_인자_그대로_보낸다():
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    rg, rg_calls = _tool("rootdesigndatahub_graph_neighbors", RD_GRAPH)
    ft, _ = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn():
        _run(rd, RESOLVE_ARGS)
        assert _run(ft, {"object_id": U_RD}) == OT07
        out = _run(rg, {"object_id": U_RD})
    assert rg_calls == [{"object_id": U_RD}] and out.endswith(RD_GRAPH), out[:300]


def test_T3_대상_허브에서_key_로_다시_푼_id_는_보낸다():
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    fr, _ = _tool("failuretrace_object_resolve", FT_RESOLVE)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn() as st:
        _run(rd, RESOLVE_ARGS)
        assert _run(ft, {"object_id": U_RD}) == OT07
        _run(fr, RESOLVE_ARGS)
        out = _run(ft, {"object_id": U_FT})
    assert ft_calls == [{"object_id": U_FT}] and out.endswith(FT_GRAPH)
    assert _apps(st, U_FT) == {"failuretrace"}


def test_T4_두_허브_결과에_다_나온_공유_id_는_막지_않는다():
    """설계 4절 · R1 — 복제 설치면 같은 UUID 가 대상 허브에서도 진짜 그 객체일 수 있다."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    fr, _ = _tool("failuretrace_object_resolve", _resolve(_hit(U_FT, external_ids={"core": U_RD})))
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn() as st:
        _run(rd, RESOLVE_ARGS)
        _run(fr, RESOLVE_ARGS)
        _run(ft, {"object_id": U_RD})
    assert ft_calls == [{"object_id": U_RD}]
    assert _apps(st, U_RD) == {"rootdesigndatahub", "failuretrace"}


def test_여러_허브에서_본_id_는_처음_본_허브와_도구를_출처로_말한다():
    """설계 3.1 — hub 는 처음 본 허브, tool 은 처음 낸 도구다. 대상(FailureTrace)이 apps 에 없으면 막는다."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    cae, _ = _tool("caegroupdatahub_object_get", json.dumps({"object": _hit(U_RD)}, ensure_ascii=False))
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn() as st:
        _run(rd, RESOLVE_ARGS)
        _run(cae, {"key": "JNT-SLD-01"})
        out = _run(ft, {"object_id": U_RD})
    assert _apps(st, U_RD) == {"rootdesigndatahub", "caegroupdatahub"}
    assert _first_tool(st, U_RD) == "rootdesigndatahub_object_resolve"
    assert ft_calls == [] and out == OT07


def test_T5_invoke_tool_로_불러도_안쪽_이름으로_막는다_인자가_JSON_글이어도():
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    inv, inv_calls = _tool("invoke_tool", FT_GRAPH)
    with _hub_turn():
        _run(rd, RESOLVE_ARGS)
        as_dict = _run(inv, {"name": "failuretrace_graph_neighbors", "arguments": {"object_id": U_RD}})
        as_text = _run(inv, {"name": "failuretrace_graph_neighbors", "arguments": json.dumps({"object_id": U_RD})})
    assert inv_calls == [], "경유도 게이트웨이에 닿기 전에 끝낸다"
    assert as_dict == OT07, "대상 이름은 안쪽 이름이다"
    assert _same_but_field(as_text, OT07, "object_id"), as_text


def test_T6_invoke_tool_로_받은_허브_결과도_안쪽_도구의_허브로_배운다():
    inv, _ = _tool("invoke_tool", RD_RESOLVE)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn() as st:
        _run(inv, {"name": "rootdesigndatahub_object_resolve", "arguments": RESOLVE_ARGS})
        out = _run(ft, {"object_id": U_RD})
    assert _first_tool(st, U_RD) == "rootdesigndatahub_object_resolve" and _apps(st, U_RD) == {"rootdesigndatahub"}
    assert ft_calls == [] and out == OT07


def test_T7_숨은_자리_조건_값의_id_도_대문자로_줘도_잡고_모르는_id_는_말하지_않는다():
    """숨은 자리의 다른 허브 id 는 오류가 아니라 0건으로 돌아와 '없다' 로 읽힌다 — 칸 이름이 아니라 인자 전체의 UUID 로 본다(설계 2.4)."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    ol, ol_calls = _tool("failuretrace_objects_list", json.dumps({"items": [], "total": 0}))
    with _hub_turn():
        _run(rd, RESOLVE_ARGS)
        out = _run(ol, {"type_slug": "pd_defect",
                        "conditions": [{"field": "failure_mode", "op": "in", "value": f"{U_RD.upper()}|{U_USER}"}]})
    assert ol_calls == [] and out.startswith(_head("failuretrace_objects_list")), out[:200]
    assert _line("conditions", U_RD, "rootdesigndatahub", "rootdesigndatahub_object_resolve", WHO_RD) in out, out
    assert U_USER not in out.lower(), "모르는 id 는 막지 않는다 — 글에도 싣지 않는다"
    assert _why("failuretrace") in out and _fix_resolve("failuretrace", "failure_mode", "JNT-SLD-01") in out and _tail("failuretrace") in out


@pytest.mark.parametrize("tool, args, field", [
    ("failuretrace_search", {"q": U_RD}, "q"),
    ("failuretrace_object_resolve", {"type_slug": "failure_mode", "name": U_RD}, "name"),
    ("failuretrace_objects_delete", {"ids": [U_RD]}, "ids"),
    ("failuretrace_relation_add", {"object_id": U_USER, "relation": "occurs_in_part", "dst_object_id": U_RD}, "dst_object_id"),
])
def test_어느_칸이든_다른_허브_id_면_막고_칸은_그_id_가_든_맨_위_인자_이름이다(tool, args, field):
    """설계 2.4 · 3.5 의 2 — 검색의 q · resolve 의 name · 목록 ids · 쓰기의 대상 칸도 같다(쓰기도 예외 없음 — R6)."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    t, calls = _tool(tool, "{}")
    with _hub_turn():
        _run(rd, RESOLVE_ARGS)
        out = _run(t, args)
    assert calls == [] and out.startswith(_head(tool)), out[:200]
    assert _line(field, U_RD, "rootdesigndatahub", "rootdesigndatahub_object_resolve", WHO_RD) in out, out
    assert U_USER not in out, "같은 호출의 모르는 id 는 말하지 않는다"


def test_T8_허브가_아닌_도구에는_다른_허브의_id_를_실어도_보낸다():
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    voc, voc_calls = _tool("search_voc", json.dumps({"total": 0, "results": [], "notes": []}))
    with _hub_turn():
        _run(rd, RESOLVE_ARGS)
        _run(voc, {"q": U_RD})
    assert voc_calls == [{"q": U_RD}]


def test_T9_허브가_아닌_도구의_결과에서는_배우지_않는다():
    voc, _ = _tool("search_voc", json.dumps({"total": 1, "results": [{"id": U_RD, "text": "솔더 크랙"}]}, ensure_ascii=False))
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn() as st:
        _run(voc, {"q": "솔더"})
        _run(ft, {"object_id": U_RD})
    assert ft_calls == [{"object_id": U_RD}] and U_RD not in st


def test_T10_오류_결과와_실패한_호출에서는_배우지_않는다():
    """설계 3.3 의 3 — 오류 글은 받은 id 를 되풀이할 수 있어, 배우면 그 허브 것으로 잘못 적는다."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    fr, fr_calls = _tool("failuretrace_object_resolve",
                         json.dumps({"error": f"[APP-OBJECTS-0001] 객체를 찾을 수 없습니다: {U_RD} (요청 {U_USER})"}, ensure_ascii=False))
    fo, _ = _tool("failuretrace_objects_list", RuntimeError(f"허브 백엔드 오류 — {U_PART}"))
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn() as st:
        _run(rd, RESOLVE_ARGS)
        _run(fr, RESOLVE_ARGS)
        failed = _run(fo, {"type_slug": "part"})
        out = _run(ft, {"object_id": U_RD})
    assert fr_calls and failed.startswith(a._TOOL_FAIL_MARK), failed
    assert ft_calls == [] and out == OT07
    assert _apps(st, U_RD) == {"rootdesigndatahub"}
    assert U_USER not in st and U_PART not in st, "오류 봉투 · ✖ 글의 id 는 배우지 않는다"


def test_T11_이_턴_허브_결과에_없는_id_는_보낸다():
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn():
        _run(ft, {"object_id": U_USER})            # 이 턴에 허브 결과가 아직 없다
        _run(rd, RESOLVE_ARGS)
        _run(ft, {"object_id": U_USER})            # 허브 결과는 있지만 그 id 는 거기 없었다(사람이 친 id · 앞 턴의 id)
    assert ft_calls == [{"object_id": U_USER}] * 2


def test_T12_턴_밖에서는_배우지도_막지도_않는다():
    """심의 · 카탈로그 · 루틴 · 띵킹 · 턴 밖의 시험은 턴 상태가 None 이다 — 유령 ID 막이와 같은 fail-open(설계 3.1)."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    assert a._turn_hub_ids.get() is None
    _run(rd, RESOLVE_ARGS)
    out = _run(ft, {"object_id": U_RD})
    assert ft_calls == [{"object_id": U_RD}] and out.endswith(FT_GRAPH) and a._turn_hub_ids.get() is None


def test_T13_맵이_비면_이름_접두로_허브를_가려_똑같이_막는다(monkeypatch):
    _cache(monkeypatch, {}, {})
    assert a._hub_of("caegroupdatahub_object_get") == "caegroupdatahub"
    assert a._hub_of("rootdesigndatahub_object_resolve") == "rootdesigndatahub"
    assert a._hub_of("graph_neighbors") == "" and a._hub_of("search_voc") == "", "접두가 없으면 허브로 못 가른다(R4 — fail-open)"
    assert a._hub_tool("caegroupdatahub", "object_resolve") == "caegroupdatahub_object_resolve"
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn():
        _run(rd, RESOLVE_ARGS)
        out = _run(ft, {"object_id": U_RD})
    assert ft_calls == [] and out == OT07


def test_T14_맵에_있으면_맵의_앱으로_가르고_대상_도구_이름도_맵을_따른다(monkeypatch):
    _cache(monkeypatch, {"object_resolve": "rootdesigndatahub", "search_voc": "signalforge",
                         "failuretrace_graph_neighbors": "failuretrace"})
    assert a._hub_of("object_resolve") == "rootdesigndatahub", "허브 하나 박스의 맨 이름"
    assert a._hub_of("search_voc") == ""
    assert a._hub_of("failuretrace_object_get") == "failuretrace", "맵에 없는 이름(별칭)은 접두로 가른다"
    assert a._hub_tool("rootdesigndatahub", "object_resolve") == "object_resolve", "맵의 맨 이름이 그 허브 것이면 맨 이름"
    assert a._hub_tool("failuretrace", "graph_neighbors") == "failuretrace_graph_neighbors"
    assert a._hub_tool("failuretrace", "object_resolve") == "failuretrace_object_resolve", "맵에 없으면 <접두>_<도구>(별칭은 늘 닿는다)"


def test_T15_key_를_모르는_id_는_그렇다고_말하고_여럿_꼴로_풀게_한다():
    rg, _ = _tool("rootdesigndatahub_graph_neighbors", json.dumps({"edges": [{"id": U_EDGE, "src": U_RD, "dst": U_PART}]}))
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn():
        _run(rg, {"object_id": U_RD})
        out = _run(ft, {"object_id": U_PART})
    assert ft_calls == []
    assert _line("object_id", U_PART, "rootdesigndatahub", "rootdesigndatahub_graph_neighbors", "key 를 결과에서 읽지 못했다") in out, out
    assert _fix_many("failuretrace") in out and 'name="' not in out, out


def test_key_가_없고_이름만_있으면_이름과_type_으로_푼다():
    """설계 3.5 의 2 · 5 — 누구는 'key 없음 · 이름 「L」 · type_slug T', 고칠 길의 이름은 key 가 없으면 label 이다."""
    rs, _ = _tool("rootdesigndatahub_search",
                  json.dumps({"items": [{"id": U_PART, "label": "PCB 메인 보드", "type_slug": "part"}]}, ensure_ascii=False))
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn():
        _run(rs, {"q": "PCB"})
        out = _run(ft, {"object_id": U_PART})
    assert ft_calls == []
    assert _line("object_id", U_PART, "rootdesigndatahub", "rootdesigndatahub_search", "key 없음 · 이름 「PCB 메인 보드」 · type_slug part") in out, out
    assert _fix_resolve("failuretrace", "part", "PCB 메인 보드") in out, out


def test_type_을_모르면_그_조각을_빼고_검색으로_찾게_한다():
    """설계 3.5 의 2 · 5 — 하나이고 이름만 알면(type 을 모르면) search 로 찾는다. type_slug 가 null 인 이름표는 type 을 모르는 것이다."""
    rs, _ = _tool("rootdesigndatahub_search",
                  json.dumps({"items": [{"id": U_PART, "key": "PRT-PCB", "label": "PCB", "type_slug": None}]}, ensure_ascii=False))
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn():
        _run(rs, {"q": "PCB"})
        out = _run(ft, {"object_id": U_PART})
    assert ft_calls == []
    assert _line("object_id", U_PART, "rootdesigndatahub", "rootdesigndatahub_search", "key 「PRT-PCB」") in out, out
    assert _fix_search("failuretrace", "PRT-PCB") in out and 'type_slug="' not in out, out


def test_글에_싣는_이름은_공백을_한_칸으로_큰따옴표를_작은따옴표로_80자로():
    """설계 3.5 의 5 — 이름이 그대로 실리면 고칠 길의 호출 꼴(name="…")이 깨진다."""
    key = 'PCB  "메인"\t보드-' + "가" * 100
    norm = re.sub(r"\s+", " ", key).replace('"', "'")
    rs, _ = _tool("rootdesigndatahub_search", json.dumps({"items": [_hit(U_PART, key, "PCB", "part")]}, ensure_ascii=False))
    ft, _ = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn():
        _run(rs, {"q": "PCB"})
        out = _run(ft, {"object_id": U_PART})
    for pat in (r'failuretrace_object_resolve\(type_slug="part", name="([^"]*)"\)', r'failuretrace_search\(q="([^"]*)"\)'):
        m = re.search(pat, out)
        assert m, (pat, out)
        assert m.group(1).startswith(norm[:80]) and len(m.group(1)) <= 81, (m.group(1), norm[:80])   # 자른 뒤 '…' 하나는 받는다


def test_T16_관련_객체_꼴의_id_도_key_와_type_으로_말한다():
    og, _ = _tool("rootdesigndatahub_object_get", json.dumps({
        "object": _hit(U_RD),
        "related": [{"relation_id": U_REL, "relation": "occurs_in_part", "object_id": U_PART, "object_key": "PRT-PCB",
                     "object_label": "PCB", "object_type_slug": "part"}]}, ensure_ascii=False))
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn():
        _run(og, {"object_id": U_RD})
        out = _run(ft, {"object_id": U_PART})
    assert ft_calls == []
    assert _line("object_id", U_PART, "rootdesigndatahub", "rootdesigndatahub_object_get", "key 「PRT-PCB」 · type_slug part") in out, out
    assert _fix_resolve("failuretrace", "part", "PRT-PCB") in out, out


def _nid(i: int) -> str:
    return f"{0xa0000000 + i:08x}-1111-4222-8333-{i:012x}"


def test_T17_접은_허브_응답에서는_모델이_본_노드만_배우고_머리_줄을_떼고_이름표를_읽는다():
    nodes = [_hit(_nid(i), f"FM-{i:04d}", "솔더 크랙 사례 " * 8 + str(i)) for i in range(600)]
    big = json.dumps({"focus": _nid(0), "nodes": nodes, "edges": [], "truncated": False}, ensure_ascii=False)
    assert len(big) > a._HUB_BIG_CHARS, "시험 전제 — 접기가 걸리는 크기"
    rg, _ = _tool("rootdesigndatahub_graph_neighbors", big)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    with _hub_turn() as st:
        got = _run(rg, {"object_id": _nid(0)})
        assert "[포털 챗이 허브 응답을 접었다" in got, "시험 전제 — 접혔다"
        assert _nid(7) in st and _nid(49) in st
        assert _nid(50) not in st and _nid(599) not in st, "접혀 빠진 노드(51번째부터)는 모델이 보지 않은 id 다"
        out = _run(ft, {"object_id": _nid(7)})
    assert ft_calls == []
    assert _line("object_id", _nid(7), "rootdesigndatahub", "rootdesigndatahub_graph_neighbors",
                 "key 「FM-0007」 · type_slug failure_mode") in out, out
    assert _fix_resolve("failuretrace", "failure_mode", "FM-0007") in out


def test_T18_여럿이면_앞_셋만_보이고_나머지는_수로_말하고_여럿_꼴로_풀게_한다(capsys):
    vs = [f"{0xc0000000 + i:08x}-2222-4333-8444-{i:012x}" for i in range(1, 6)]
    rg, _ = _tool("rootdesigndatahub_graph_neighbors", json.dumps(
        {"focus": vs[0], "nodes": [_hit(v, f"FM-{i}", f"고장 {i}") for i, v in enumerate(vs, 1)], "edges": []}, ensure_ascii=False))
    ol, ol_calls = _tool("failuretrace_objects_list", json.dumps({"items": [], "total": 0}))
    with _hub_turn():
        _run(rg, {"object_id": vs[0]})
        capsys.readouterr()
        out = _run(ol, {"type_slug": "pd_defect", "conditions": [{"field": "failure_mode", "op": "in", "value": "|".join(vs)}]})
    assert ol_calls == []
    assert out.count("는 다른 허브의 id 다") == 3 and out.count("conditions=") == 3, out
    for i, v in enumerate(vs[:3], 1):
        assert _line("conditions", v, "rootdesigndatahub", "rootdesigndatahub_graph_neighbors",
                     f"key 「FM-{i}」 · type_slug failure_mode") in out, (i, out)
    assert out.index(vs[0]) < out.index(vs[1]) < out.index(vs[2]), "인자에 나온 차례"
    assert vs[3] not in out and vs[4] not in out, "보이는 id 는 앞 셋(_XHUB_SHOW)"
    assert "그 밖에 2개도 다른 허브의 id 다." in out
    assert _fix_many("failuretrace") in out and 'name="' not in out, out
    logs = _gate_log(capsys)
    assert len(logs) == 1 and "다른 허브 id 5개" in logs[0], logs


def test_T19_상한이_차면_새_id_는_적지_않지만_이미_있는_id_에_허브는_더한다(monkeypatch):
    monkeypatch.setattr(a, "_HUB_IDS_MAX", 2)
    ids = (U_PART, U_USER, U_DEF)
    rows = [_hit(u, f"K-{i}", f"부품 {i}", "part") for i, u in enumerate(ids)]
    rl, _ = _tool("rootdesigndatahub_objects_list", json.dumps({"items": rows, "total": 3}, ensure_ascii=False))
    fl, _ = _tool("failuretrace_objects_list", json.dumps({"items": [rows[0], rows[2]], "total": 2}, ensure_ascii=False))
    with _hub_turn() as st:
        _run(rl, {"type_slug": "part"})
        assert set(st) == {U_PART, U_USER}, "셋째는 상한 밖"
        _run(fl, {"type_slug": "part"})
    assert set(st) == {U_PART, U_USER}, "새 id 는 상한 미만일 때만"
    assert _apps(st, U_PART) == {"rootdesigndatahub", "failuretrace"}, "이미 있는 id 에 허브를 더하는 것은 늘 한다(거짓 막음을 만들지 않게)"
    assert _apps(st, U_USER) == {"rootdesigndatahub"}


def test_셋째_허브로도_반대_방향으로도_건너면_막는다():
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    cae, cae_calls = _tool("caegroupdatahub_object_get", "{}")
    fr, _ = _tool("failuretrace_object_resolve", FT_RESOLVE)
    rg, rg_calls = _tool("rootdesigndatahub_graph_neighbors", RD_GRAPH)
    with _hub_turn():
        _run(rd, RESOLVE_ARGS)
        to_cae = _run(cae, {"object_id": U_RD})
        _run(fr, RESOLVE_ARGS)
        to_rd = _run(rg, {"object_id": U_FT})
    assert cae_calls == [] and rg_calls == []
    assert to_cae.startswith(_head("caegroupdatahub_object_get")), to_cae
    for must in (_line("object_id", U_RD, "rootdesigndatahub", "rootdesigndatahub_object_resolve", WHO_RD), _why("caegroupdatahub"),
                 _fix_resolve("caegroupdatahub", "failure_mode", "JNT-SLD-01"), _tail("caegroupdatahub")):
        assert must in to_cae, (must, to_cae)
    assert to_rd.startswith(_head("rootdesigndatahub_graph_neighbors")), to_rd
    for must in (_line("object_id", U_FT, "failuretrace", "failuretrace_object_resolve", WHO_RD), _why("rootdesigndatahub"),
                 _fix_resolve("rootdesigndatahub", "failure_mode", "JNT-SLD-01"), _tail("rootdesigndatahub")):
        assert must in to_rd, (must, to_rd)


def test_운영_꼴_게이트웨이_도구도_배우고_막는다_막은_글은_내용과_아티팩트_둘로():
    """게이트웨이 도구는 content_and_artifact 꼴이다 — 막은 호출도 2-튜플이어야 LangChain 이 받는다(설계 3.4 의 5)."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE, mcp=True)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH, mcp=True)
    with _hub_turn() as st:
        _run(rd, RESOLVE_ARGS)
        out = _run(ft, {"object_id": U_RD})
    assert _apps(st, U_RD) == {"rootdesigndatahub"}
    assert ft_calls == [] and out == OT07


def test_코드가_읽는_길의_래퍼도_턴_상태가_있으면_배우고_막는다():
    """design-integration 2절 — 승인 실행 같은 chat=False 래퍼도 G4 를 다시 지나고 R7(배우기)이 돈다. 턴 상태가 가른다."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE, chat=False)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH, chat=False)
    with _hub_turn() as st:
        _run(rd, RESOLVE_ARGS)
        out = _run(ft, {"object_id": U_RD})
    assert _apps(st, U_RD) == {"rootdesigndatahub"} and ft_calls == [] and out == OT07


def test_유령_ID_막이가_먼저다_출처_없는_정수와_다른_허브_id_가_함께면_유령_ID_글이다():
    """design-integration 2절 — G3(유령 ID · 정수) → G4(다른 허브 id · UUID). 영역이 겹치지 않고 차례는 G3 가 먼저다."""
    rd, _ = _tool("rootdesigndatahub_object_resolve", RD_RESOLVE)
    ft, ft_calls = _tool("failuretrace_graph_neighbors", FT_GRAPH)
    tok = a._turn_ids.set(set())
    try:
        with _hub_turn():
            _run(rd, RESOLVE_ARGS)
            out = _run(ft, {"object_id": U_RD, "test_id": 987654})
    finally:
        a._turn_ids.reset(tok)
    assert ft_calls == [] and a._PHANTOM_ID_MARK in out and "는 다른 허브의 id 다" not in out, out


def test_부르기_전_검사와_배우기는_턴_상태_허브_글을_보고_래퍼와_같은_글을_낸다():
    """설계 3.3 · 3.4 의 이름 그대로 — _learn_hub_ids(name, text) 와 _cross_hub_id_msg(target, args) -> str | None."""
    assert a._cross_hub_id_msg("failuretrace_graph_neighbors", {"object_id": U_RD}) is None, "턴 밖"
    a._learn_hub_ids("rootdesigndatahub_object_resolve", RD_RESOLVE)          # 턴 밖 — 아무것도 하지 않는다
    with _hub_turn() as st:
        assert a._cross_hub_id_msg("failuretrace_graph_neighbors", {"object_id": U_RD}) is None, "이 턴에 허브 결과가 아직 없다"
        a._learn_hub_ids("rootdesigndatahub_object_resolve", RD_RESOLVE)
        a._learn_hub_ids("rootdesigndatahub_object_resolve", {"items": [{"id": U_USER}]})   # 글이 아니면 끝낸다
        a._learn_hub_ids("search_voc", json.dumps({"id": U_PART}))                         # 허브가 아닌 도구
        assert set(st) == {U_RD}
        assert a._cross_hub_id_msg("search_voc", {"q": U_RD}) is None, "허브가 아닌 대상"
        assert a._cross_hub_id_msg("rootdesigndatahub_graph_neighbors", {"object_id": U_RD}) is None, "제 허브"
        assert a._cross_hub_id_msg("failuretrace_graph_neighbors", {"object_id": U_USER}) is None, "모르는 id"
        assert a._cross_hub_id_msg("failuretrace_graph_neighbors", {"object_id": U_RD}) == OT07
        as_text = a._cross_hub_id_msg("failuretrace_graph_neighbors", json.dumps({"object_id": U_RD}))
    assert as_text and _same_but_field(as_text, OT07, "object_id"), as_text


def test_UUID_꼴은_앞뒤가_16진수가_아닐_때만_잡고_소문자로_차례대로_한_번씩():
    """설계 3.3 의 4 — `(?<![0-9A-Fa-f])…(?![0-9A-Fa-f])`. 더 긴 16진 글(해시 등) 안의 조각은 id 가 아니다. HA-27(C-13)이 같은 꼴을 쓴다."""
    s = f"a{U_USER} {U_RD.upper()}|{U_FT},{U_RD} x{U_PART}0 ({U_DEF})"
    assert [m.group(0).lower() for m in a._UUID_RE.finditer(s)] == [U_RD, U_FT, U_RD, U_DEF]
    assert _uuid_list(s) == [U_RD, U_FT, U_DEF]


# ── 허브 표 — 코어 어휘표의 정본 · sibling(설계 3.2 · 8.2 끝) ──────────────────────────────────────────────────────────
def _vocab(p: Path, apps: dict, canonical=("hub", "허브")) -> None:
    p.write_text(json.dumps({"version": 7, "canonical": {"app": canonical[0], "label": canonical[1]}, "rule_words": ["낱말"],
                             "relations": [], "apps": apps}, ensure_ascii=False), encoding="utf-8")


def _bump(p: Path, k: int) -> None:
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + k * 1_000_000_000))


def test_허브_표는_어휘표가_없으면_기본_셋이다():
    assert a._hub_apps() == HUB_LABEL


def test_허브_표는_정본_어휘표의_정본과_형제다_시험_사본은_아니다(monkeypatch):
    if not _REAL.exists():
        pytest.skip("형제 포털 리포의 어휘표가 없다")
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", _REAL)
    hubs = a._hub_apps()
    assert hubs == HUB_LABEL, "라벨은 어휘표의 것이다"
    assert "stepforge-prod" not in hubs and "dynaforge-prod" not in hubs, "role staging(시험 사본)은 허브가 아니다(Q6)"


def test_허브_표는_어휘표에서_만들고_허브_판정이_그_표를_따르며_깨지면_기본표다(monkeypatch, tmp_path):
    p = tmp_path / "core_vocab.json"
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    _vocab(p, {"x": {"role": "sibling", "label": "X허브"}, "y": {"role": "staging", "label": "Y 사본"},
               "z": {"role": "noncore", "label": "Z"}})
    assert a._hub_apps() == {"hub": "허브", "x": "X허브"}
    h = a._core_vocab_health()
    assert "hubs" not in h and {"loaded", "version", "rule_words", "canonical", "fingerprint", "base", "overlay"} <= set(h), \
        "/health core_vocab 은 칸을 골라 싣는다 — 허브 표가 새지 않고 종전 칸은 그대로다"
    _cache(monkeypatch, {})
    assert a._hub_of("x_graph_neighbors") == "x" and a._hub_of("hub_object_resolve") == "hub"
    assert a._hub_of("failuretrace_graph_neighbors") == "" and a._hub_of("y_object_get") == "", "허브는 표가 정한다"
    p.write_text("{깨진", encoding="utf-8")
    _bump(p, 1)
    assert a._hub_apps() == HUB_LABEL


def test_박스_덧씌우기로_sibling_을_더하면_허브가_늘고_접두는_키에서_빼기표를_뺀_것이다(monkeypatch, tmp_path):
    """설계 3.2 — core_vocab.local.json 으로 커밋 없이 허브가 는다. 게이트웨이의 겹친 이름 접두는 백엔드 키에서 '-' 를 뺀 것이다."""
    p, loc = tmp_path / "core_vocab.json", tmp_path / "core_vocab.local.json"
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    monkeypatch.setattr(a, "CORE_VOCAB_LOCAL_PATH", loc)
    _vocab(p, {"x": {"role": "sibling", "label": "X허브"}})
    loc.write_text(json.dumps({"apps": {"w-hub": {"role": "sibling", "label": "W허브"}}}, ensure_ascii=False), encoding="utf-8")
    assert a._hub_apps() == {"hub": "허브", "x": "X허브", "w-hub": "W허브"}
    _cache(monkeypatch, {})
    assert a._hub_of("whub_graph_neighbors") == "w-hub"
    assert a._hub_tool("w-hub", "object_resolve") == "whub_object_resolve"


# ── 손잡이 — CHAT_XHUB_ID_GATE(설계 3.6 — 기동 때 읽는다, 0 이면 끈다) ──────────────────────────────────────────────────
@pytest.mark.parametrize("val, on", [(None, True), ("1", True), ("0", False)])
def test_손잡이_CHAT_XHUB_ID_GATE_는_기동_때_읽고_0_이면_끈다(val, on):
    env = {k: v for k, v in os.environ.items() if k != "CHAT_XHUB_ID_GATE"}
    if val is not None:
        env["CHAT_XHUB_ID_GATE"] = val
    r = subprocess.run([sys.executable, "-c", "import app; print('XHUB_GATE=%s' % bool(app._XHUB_GATE))"], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=300)
    assert f"XHUB_GATE={on}" in r.stdout, (r.stdout + r.stderr)[-3000:]


# ── 실제 경로 — 앱이 만드는 에이전트로 한 턴(설계 8.3) ──────────────────────────────────────────────────────────────
class _HubLLM(BaseChatModel):
    """대본대로 허브 도구를 부르는 모델 — 이번 사람 발화의 낱말로 대본을 고르고, 그 뒤의 도구 결과 수로 몇 번째 걸음인지 안다. 받은 메시지를
    호출마다 (발화, 메시지) 로 적는다. 스트림 길(on_chat_model_stream)로도 같은 답을 한 조각으로 낸다(운영 기본이 스트리밍이다)."""

    scripts: dict = Field(default_factory=dict)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "hub-script"

    def bind_tools(self, tools, **_kw):
        return self

    def _reply(self, messages) -> AIMessage:
        k = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        said = str(messages[k].content)
        self.seen.append((said, list(messages)))
        script = next(v for w, v in self.scripts.items() if w in said)
        done = sum(isinstance(m, ToolMessage) for m in messages[k + 1:])
        step = script[min(done, len(script) - 1)]
        if isinstance(step, str):
            return AIMessage(content=step)
        name, args = step
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"hub{len(self.seen)}"}])

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **_kw):
        msg = self._reply(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content, tool_call_chunks=[
            {"name": c["name"], "args": json.dumps(c["args"], ensure_ascii=False), "id": c["id"], "index": i}
            for i, c in enumerate(msg.tool_calls)]))


OT07_Q = "JNT-SLD-01 의 불량 기록 연결을 FailureTrace 에서 보여줘"
OT07_ANSWER = "FailureTrace 에서 JNT-SLD-01 에 이어진 불량 기록 PD-0001 을 찾았다."
OT07_SCRIPT = [("rootdesigndatahub_object_resolve", RESOLVE_ARGS),
               ("failuretrace_graph_neighbors", {"object_id": U_RD}),      # 정본의 id 를 그대로 — OT-07
               ("failuretrace_object_resolve", RESOLVE_ARGS),
               ("failuretrace_graph_neighbors", {"object_id": U_FT}),
               OT07_ANSWER]
TURN2_Q = "둘째 턴 — 아까 그 id 로 FailureTrace 그래프를 다시 봐"
TURN2_SCRIPT = [("failuretrace_graph_neighbors", {"object_id": U_RD}), "둘째 턴의 답이다."]


def _raw_tool(name, reply, rec: list):
    """감싸지 않은 게이트웨이 도구 대역 — 앱이 _agent_for 에서 챗 경로로 싼다. 불릴 때 인자와 그때의 턴 상태를 적는다(설계 8.3 T21)."""
    async def call(**kw):
        rec.append((name, dict(kw), a._turn_hub_ids.get()))
        return reply(kw) if callable(reply) else reply
    return StructuredTool(name=name, description=f"{name} 허브 도구", coroutine=call, args_schema={"type": "object", "properties": {}})


def _agent_of(monkeypatch, llm, tools):
    """앱이 실제로 만드는 길(_agent_for)로 에이전트를 만든다 — 게이트웨이 클라이언트만 스텁이다(tests/test_chat_loop_fit.py 의 _agent 와 같다)."""
    class _StubClient:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            return list(tools)

    monkeypatch.setattr(a, "MultiServerMCPClient", _StubClient)
    state = types.SimpleNamespace(
        llm=llm, connections={"gateway": {"url": "http://gw/mcp", "headers": {"Authorization": "Bearer x"}}},
        agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    return asyncio.run(a._agent_for(types.SimpleNamespace(state=state), ["admin"]))


@pytest.fixture
def real_path(monkeypatch):
    """창을 박아 두고(LLM 서버에 묻지 않게 — 묻는 때가 없는 값이라 다시 묻지 않는다) 오류 턴의 자동 재시도를 끈다(대본이 처음부터 다시 돌지 않게)."""
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")


def _ot07_agent(monkeypatch):
    """OT-07 의 허브 셋 — 정본 resolve · FailureTrace resolve · FailureTrace 그래프(정본 id 면 허브의 APP-GRAPH-0001)."""
    rec: list = []
    tools = [_raw_tool("rootdesigndatahub_object_resolve", RD_RESOLVE, rec),
             _raw_tool("failuretrace_object_resolve", FT_RESOLVE, rec),
             _raw_tool("failuretrace_graph_neighbors", lambda kw: FT_GRAPH if kw.get("object_id") == U_FT else GRAPH_ERR, rec)]
    llm = _HubLLM(scripts={"FailureTrace 에서 보여줘": OT07_SCRIPT, "둘째 턴": TURN2_SCRIPT})
    agent = _agent_of(monkeypatch, llm, tools)

    async def _built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", _built)
    return llm, rec


def _chat(message: str) -> list:
    req = a.ChatRequest(message=message, groups=["portal-admin"], history=[])

    async def run():
        return [c async for c in a._agent_stream(a.app, req)]
    return [(e, d) for e, d in map(a._sse_parse, asyncio.run(run())) if e]


def _tool_msgs(llm, i: int) -> list:
    return [m for m in llm.seen[i][1] if isinstance(m, ToolMessage)]


def test_T20_실제_경로_OT07_한_턴_다른_허브_id_는_막히고_다시_푼_id_로_닿는다(monkeypatch, real_path):
    llm, rec = _ot07_agent(monkeypatch)
    evs = _chat(OT07_Q)
    assert not [d for e, d in evs if e == "error"], evs
    graph = [kw for n, kw, _s in rec if n == "failuretrace_graph_neighbors"]
    assert graph == [{"object_id": U_FT}], "정본 id 로는 보내지 않고 다시 푼 FailureTrace id 로만 보냈다"
    blocked = _tool_msgs(llm, 2)[-1].content           # 모델의 셋째 호출이 받은 마지막 도구 결과
    assert "는 다른 허브의 id 다" in blocked and 'failuretrace_object_resolve(type_slug="failure_mode", name="JNT-SLD-01")' in blocked, blocked
    done = [d for e, d in evs if e == "status" and d.get("step") == "도구 완료: failuretrace_graph_neighbors"]
    assert [d.get("ok") for d in done] == [False, True], done
    result = [d for e, d in evs if e == "result"]
    assert result and OT07_ANSWER in result[-1]["content"], result
    states = [s for _n, _kw, s in rec]
    assert len(states) == 3 and all(isinstance(s, dict) for s in states), "턴이 상태를 건다"
    assert len({id(s) for s in states}) == 1, "한 턴에 dict 하나 — 자식 문맥에서 더한 것이 다음 호출에 보인다(설계 3.1)"


def test_T21_손잡이를_끄면_종전대로_보내고_턴_상태도_없다(monkeypatch, real_path):
    monkeypatch.setattr(a, "_XHUB_GATE", False)
    llm, rec = _ot07_agent(monkeypatch)
    evs = _chat(OT07_Q)
    assert not [d for e, d in evs if e == "error"], evs
    graph = [kw for n, kw, _s in rec if n == "failuretrace_graph_neighbors"]
    assert graph == [{"object_id": U_RD}, {"object_id": U_FT}], "끄면 종전처럼 정본 id 도 보낸다"
    second = _tool_msgs(llm, 2)[-1].content
    assert "[APP-GRAPH-0001]" in second and "는 다른 허브의 id 다" not in second, second
    assert rec and all(s is None for _n, _kw, s in rec), "끄면 턴 상태를 걸지 않는다 — 종전과 글자 하나 다르지 않다"


def test_T22_앞_턴에_배운_것은_다음_턴에_남지_않는다(monkeypatch, real_path):
    llm, rec = _ot07_agent(monkeypatch)
    _chat(OT07_Q)
    n1 = len(rec)
    evs = _chat(TURN2_Q)
    assert not [d for e, d in evs if e == "error"], evs
    turn2 = rec[n1:]
    assert [(n, kw) for n, kw, _s in turn2] == [("failuretrace_graph_neighbors", {"object_id": U_RD})], "요청 행의 범위는 '이번 턴'"
    st1, st2 = rec[0][2], turn2[0][2]
    assert isinstance(st2, dict) and st2 is not st1, "턴마다 새 상태"
    assert U_RD in st1 and U_RD not in st2
    last = [m for said, msgs in llm.seen if "둘째 턴" in said for m in msgs if isinstance(m, ToolMessage)]
    assert last and "[APP-GRAPH-0001]" in last[-1].content and "는 다른 허브의 id 다" not in last[-1].content


# ── 합칠 때의 맞물림(design-integration C-8 — HA-30 합침에서 든다) ─────────────────────────────────────────────────────
def test_C8_health_의_xhub_gate_는_손잡이와_막은_수를_싣는다(monkeypatch, real_path):
    """손잡이를 끈 박스를 밖에서 가린다(C-8). 막은 수는 실제 경로 한 턴(OT-07 — 막음 하나)으로 하나 는다."""
    c = TestClient(a.app)
    h0 = _xhub_health(c)
    assert h0["on"] is True and isinstance(h0["blocked"], int), h0
    _ot07_agent(monkeypatch)
    _chat(OT07_Q)
    assert _xhub_health(c)["blocked"] == h0["blocked"] + 1
    monkeypatch.setattr(a, "_XHUB_GATE", False)
    assert _xhub_health(c)["on"] is False

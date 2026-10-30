# 허브 응답 접기의 흔적 — 글이 바뀐 접기만 로그 한 줄과 /health 의 hub_fold 에 남는지 본다(HWAXPortal docs/request-1030 HA-34)
"""접은 표지는 LLM 에 가는 결과 글 머리에만 붙어, 10-29 · 10-30 재시험이 접기가 실제로 걸렸는지 가리지 못했다. 큰 허브 응답을 도구 래퍼로
흘려(invoke_tool 의 안쪽 이름 포함) 한 번 접을 때 한 번 세고 한 줄 남는지, 접지 않는 응답은 아무것도 바꾸지 않는지, /health 에 칸이 실리는지 본다.

  실행:  .venv/bin/python -m pytest tests/test_hub_fold_stats.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402

_ZERO = {"count": 0, "chars_in": 0, "chars_out": 0}
# 60,000 자를 넘는 object_fields 꼴 — 긴 목록 하나(300개)를 앞 50개로 접는다(W-5 시험과 같은 꼴)
BIG = json.dumps({"type": "failure_mode", "fields": [{"path": f"in.f{i}", "label": "x" * 400} for i in range(300)]})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """계수는 모듈 상태다 — 시험마다 0 에서 센다(앞 시험이 접은 것이 남지 않게)."""
    monkeypatch.setattr(a, "_HUB_FOLD_STAT", dict(_ZERO))


def _tool(name, text):
    async def call(**_kw):
        return text
    return a._prep_tool(StructuredTool(name=name, description="d", coroutine=call,
                                       args_schema={"type": "object", "properties": {}}))


def _fold_logs(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if "허브 응답 접음" in ln]


def test_큰_허브_응답을_접으면_한_번_세고_로그_한_줄을_남긴다(capsys):
    assert len(BIG) > a._HUB_BIG_CHARS
    folded = len(a._fold_hub_lists(BIG))           # 세지 않는 안쪽 함수로 접은 뒤 자 수를 미리 잰다
    assert folded < len(BIG)
    out = asyncio.run(_tool("rootdesigndatahub_object_fields", BIG).ainvoke({}))
    assert out.startswith("[포털 챗이 허브 응답을 접었다")
    assert a._hub_fold_health() == {"count": 1, "chars_in": len(BIG), "chars_out": folded}
    assert _fold_logs(capsys.readouterr().out) == [
        f"[agent] 허브 응답 접음 — rootdesigndatahub_object_fields: 원문 {len(BIG):,}자 → {folded:,}자"]
    # invoke_tool 경유는 안쪽 이름으로 접고, 로그에도 안쪽 이름이 남는다
    via = asyncio.run(_tool("invoke_tool", BIG).ainvoke({"name": "failuretrace_graph_neighbors", "arguments": {}}))
    assert via.startswith("[포털 챗이 허브 응답을 접었다")
    assert a._hub_fold_health() == {"count": 2, "chars_in": 2 * len(BIG), "chars_out": 2 * folded}
    assert _fold_logs(capsys.readouterr().out) == [
        f"[agent] 허브 응답 접음 — failuretrace_graph_neighbors: 원문 {len(BIG):,}자 → {folded:,}자"]


def test_관계와_메타를_걷는_접기도_센다(capsys):
    summ = json.dumps({"total": 1, "group_options": ["x" * 5000]})
    out = asyncio.run(_tool("failuretrace_objects_summary", summ).ainvoke({}))
    h = a._hub_fold_health()
    assert h["count"] == 1 and h["chars_in"] == len(summ) and h["chars_out"] == len(out) < len(summ), (h, len(out))
    assert len(_fold_logs(capsys.readouterr().out)) == 1


def test_접지_않는_응답은_아무것도_바꾸지_않는다(capsys):
    small = json.dumps({"fields": [{"path": f"in.f{i}"} for i in range(300)]})     # 목록은 길어도 60,000 자 아래
    assert len(small) <= a._HUB_BIG_CHARS
    cases = (("rootdesigndatahub_object_fields", small),                         # 작은 응답
             ("failuretrace_objects_summary", '{"total": 3}'),                      # 걷을 칸이 없다
             ("object_get", json.dumps({"id": "P-1", "related": {"uses": [{"id": "m1"}]}})),  # 관계가 앞 몇 개 안
             ("object_fields", "표 글 " * 20000),                                    # JSON 이 아니다
             ("list_materials", BIG))                                               # 허브 도구가 아니다
    for name, text in cases:
        assert a._fold_hub_result(name, text) == text, name
        out = asyncio.run(_tool(name, text).ainvoke({}))
        assert "[포털 챗이 허브 응답을 접었다" not in out, name
    assert a._hub_fold_health() == _ZERO
    assert _fold_logs(capsys.readouterr().out) == []


def test_health_에_hub_fold_가_실린다(monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})   # 창을 박아 둔다 — /health 가 LLM 서버에 묻지 않게
    c = TestClient(a.app)
    assert c.get("/health").json()["hub_fold"] == _ZERO, "기동 뒤 접은 적이 없으면 0 이다"
    asyncio.run(_tool("rootdesigndatahub_object_fields", BIG).ainvoke({}))
    assert c.get("/health").json()["hub_fold"] == a._hub_fold_health() == {
        "count": 1, "chars_in": len(BIG), "chars_out": len(a._fold_hub_lists(BIG))}

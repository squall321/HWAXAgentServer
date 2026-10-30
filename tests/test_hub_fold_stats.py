# 허브 응답 접기의 흔적 — 글이 줄어든 접기만 로그 한 줄과 /health 의 hub_fold 에 남는지 본다(HWAXPortal docs/request-1030 HA-34)
"""접은 표지는 LLM 에 가는 결과 글 머리에만 붙어, 10-29 · 10-30 재시험이 접기가 실제로 걸렸는지 가리지 못했다. 큰 허브 응답을 도구 래퍼로
흘려(invoke_tool 경유는 로그에 그 길까지) 한 번 접을 때 한 번 세고 한 줄 남는지, 접지 않거나 접어도 글이 줄지 않은 응답은 세지도 찍지도 않는지,
그 줄을 바로 흘리는지, /health 에 칸이 실리는지 본다.

  실행:  .venv/bin/python -m pytest tests/test_hub_fold_stats.py -q
"""
import asyncio
import io
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
    # invoke_tool 경유는 안쪽 이름으로 접고, 로그는 그 길까지 남긴다 — 큰 응답이 경유로 왔는지를 로그만으로 가른다(요청서 6절 R-07)
    via = asyncio.run(_tool("invoke_tool", BIG).ainvoke({"name": "failuretrace_graph_neighbors", "arguments": {}}))
    assert via.startswith("[포털 챗이 허브 응답을 접었다")
    assert a._hub_fold_health() == {"count": 2, "chars_in": 2 * len(BIG), "chars_out": 2 * folded}
    assert _fold_logs(capsys.readouterr().out) == [
        f"[agent] 허브 응답 접음 — invoke_tool(failuretrace_graph_neighbors): 원문 {len(BIG):,}자 → {folded:,}자"]


def _mcp_tool(name, text):
    """운영 꼴 — MCP 어댑터 도구는 (내용 블록 목록, 아티팩트) 둘을 돌려준다(content_and_artifact). 게이트웨이 도구는 이 갈래로 온다."""
    async def call(**_kw):
        return [{"type": "text", "text": text}], None
    return a._prep_tool(StructuredTool(name=name, description="d", coroutine=call, response_format="content_and_artifact",
                                       args_schema={"type": "object", "properties": {}}))


def test_운영_꼴로_invoke_tool_을_거쳐도_한_번_세고_그_길을_찍는다(capsys):
    summ = json.dumps({"total": 1, "group_options": ["x" * 5000]})
    for inner, text in (("failuretrace_graph_neighbors", BIG), ("rootdesigndatahub_objects_summary", summ)):   # 긴 목록 접기 · 칸 걷기
        out = asyncio.run(_mcp_tool("invoke_tool", text).ainvoke({"name": inner, "arguments": {}}))
        assert out.startswith("[포털 챗이 허브 응답을 접었다"), inner
        assert _fold_logs(capsys.readouterr().out) == [
            f"[agent] 허브 응답 접음 — invoke_tool({inner}): 원문 {len(text):,}자 → {len(out):,}자"]
    h = a._hub_fold_health()
    assert h["count"] == 2 and h["chars_in"] == len(BIG) + len(summ) and h["chars_out"] < h["chars_in"], h


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


def test_접어도_글이_줄지_않으면_세지도_찍지도_않는다(capsys):
    """메타 칸이 비었거나 작은 응답은 걷어도 머리 줄이 더 길어 글이 는다. 그것까지 세면 횟수가 objects_summary · objects_list 호출 수가 되고
    chars_out 이 chars_in 을 넘는다(반대 검토 — 33자 응답이 '원문 33자 → 62자' 로 찍혔다). 결과 글은 종전 그대로다(HA-04 접기는 바꾸지 않는다)."""
    cases = (("failuretrace_objects_summary", json.dumps({"total": 1, "group_options": []})),
             ("rootdesigndatahub_objects_list", json.dumps({"total": 1, "ref_labels": {"a": "b"}, "items": [{"id": 1}]})))
    for name, text in cases:
        out = asyncio.run(_tool(name, text).ainvoke({}))
        assert out.startswith("[포털 챗이 허브 응답을 접었다") and len(out) > len(text), (name, "전제 — 접기가 글을 바꿨는데 늘었다")
    assert a._hub_fold_health() == _ZERO
    assert _fold_logs(capsys.readouterr().out) == []


class _Stdout(io.StringIO):
    """흘린 데까지 — flush 가 불린 때의 글을 남긴다."""
    flushed = ""

    def flush(self):
        self.flushed = self.getvalue()
        super().flush()


def test_접기_로그_줄은_바로_흘린다(monkeypatch):
    """부팅 유닛 · 감시 크론(services.sh heal) · 손 기동(start.sh -d)으로 뜬 서버는 PYTHONUNBUFFERED 가 없어 파일로 가는 stdout 이 블록 버퍼링
    된다 — 흘리지 않으면 재시험 직후 로그에 그 줄이 없다(반대 검토, 프로세스가 끝날 때에야 찍혔다)."""
    rec = _Stdout()
    monkeypatch.setattr(sys, "stdout", rec)
    a._fold_hub_result("rootdesigndatahub_object_fields", BIG)
    assert len(_fold_logs(rec.getvalue())) == 1
    assert _fold_logs(rec.flushed) == _fold_logs(rec.getvalue()), "찍은 줄이 버퍼에 남았다"


def test_health_에_hub_fold_가_실린다(monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})   # 창을 박아 둔다 — /health 가 LLM 서버에 묻지 않게
    c = TestClient(a.app)
    assert c.get("/health").json()["hub_fold"] == _ZERO, "기동 뒤 접은 적이 없으면 0 이다"
    asyncio.run(_tool("rootdesigndatahub_object_fields", BIG).ainvoke({}))
    assert c.get("/health").json()["hub_fold"] == a._hub_fold_health() == {
        "count": 1, "chars_in": len(BIG), "chars_out": len(a._fold_hub_lists(BIG))}

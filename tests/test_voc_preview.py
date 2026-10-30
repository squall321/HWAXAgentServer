# 'VOC 먼저 보기' 엔드포인트 — 못 씀·실패·정말 없음을 뭉개지 않는지, 검색어 교차·중복 제거
#
#   실행:  .venv/bin/python -m pytest tests/test_voc_preview.py -q
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402


def _blocks(rows):
    return "".join(json.dumps(r, ensure_ascii=False) for r in rows)


def _voc(i, kw):
    return {"id": i, "content_translated": f"{kw} issue {i}", "product_name": "Fold3",
            "platform_name": "YouTube", "country_code": "KR", "published_at": "2026-09-01T00:00:00",
            "sentiment_label": "negative", "sentiment_score": -0.7, "source_url": f"https://x/{i}"}


def _run(monkeypatch, tools, call, req):
    async def fake_tools(*_a, **_k):
        return tools
    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", call)
    return asyncio.run(a.deliberate_voc_preview(a.VocPreviewRequest(**req)))


def test_도구가_없으면_unavailable(monkeypatch):
    out = _run(monkeypatch, {"other": object()}, None, {"message": "힌지", "keywords": ["hinge"]})
    assert out["unavailable"] is True and out["items"] == []


def test_검색어를_주면_추출을_건너뛰고_교차로_섞는다(monkeypatch):
    data = {"hinge": [_voc(1, "hinge"), _voc(2, "hinge"), _voc(3, "hinge")],
            "crease": [_voc(3, "crease"), _voc(4, "crease")]}          # id 3 은 두 검색어에 겹친다

    async def call(tools, name, args):
        assert name == "search_voc"
        return _blocks(data[args["keyword"]])

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "무시", "keywords": ["hinge", "crease"]})
    assert out["keywords"] == ["hinge", "crease"]
    assert [x["id"] for x in out["items"]] == [1, 3, 2, 4], "검색어별 번갈아 + 중복 제거"
    x = out["items"][0]
    assert (x["product"], x["platform"], x["date"], x["score"]) == ("Fold3", "YouTube", "2026-09-01", -0.7)
    assert not out["degraded"] and not out["partial"]


def test_전부_실패하면_degraded_로_말한다(monkeypatch):
    async def call(tools, name, args):
        return "(tool search_voc error: boom)"

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["a", "b"]})
    assert out["degraded"] is True and out["items"] == []


def test_정말_없으면_degraded_가_아니다(monkeypatch):
    async def call(tools, name, args):
        return ""                                   # 물어봤고 결과가 비었다

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["zzz"]})
    assert out["items"] == [] and out["degraded"] is False


# ── SignalForge 새 반환 꼴 — search_voc 는 늘 객체 하나 {total, results, strategy, match_mode, notes} 다(HA-33) ──────────
# 종전엔 그 객체가 한 건으로 읽혀 본문이 비고 걸러져, 실패 표시 없이 0건이었다. 그러면 화면은 '직접 봤다' 로 쳐서 심의의
# 자동 VOC 환기까지 껐다(포털 VocFirstPanel 의 used).
def _envelope(rows, notes=(), strategy="semantic"):
    """FastMCP 는 객체 반환을 들여쓴 JSON 글 하나로 보낸다(SignalForge mcp-server/server.py search_voc)."""
    return json.dumps({"total": len(rows), "results": rows, "strategy": strategy,
                       "match_mode": strategy, "notes": list(notes)}, ensure_ascii=False, indent=2)


@pytest.mark.parametrize("shape", ["envelope", "blocks", "array"])
def test_새_꼴은_results_를_펼치고_옛_꼴도_그대로_읽는다(monkeypatch, shape):
    data = {"hinge": [_voc(1, "hinge"), _voc(2, "hinge"), _voc(3, "hinge")],
            "crease": [_voc(3, "crease"), _voc(4, "crease")]}
    enc = {"envelope": _envelope, "blocks": _blocks, "array": lambda rows: json.dumps(rows, ensure_ascii=False)}[shape]

    async def call(tools, name, args):
        return enc(data[args["keyword"]])

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "무시", "keywords": ["hinge", "crease"]})
    assert [x["id"] for x in out["items"]] == [1, 3, 2, 4], (shape, out)
    assert out["items"][0]["text"] == "hinge issue 1" and out["items"][0]["product"] == "Fold3"
    assert not out["degraded"] and not out["partial"] and "note" not in out


def test_펼친_뒤에도_0건이면_notes_를_미리보기에_싣는다(monkeypatch):
    why = "키워드 'zzz' 에 맞는 글이 없다(시도한 경로: phrase, all_tokens, fts)"
    data = {"hinge": _envelope([_voc(1, "hinge")]), "zzz": _envelope([], [why], "keyword")}

    async def call(tools, name, args):
        return data[args["keyword"]]

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["hinge", "zzz"]})
    assert [x["id"] for x in out["items"]] == [1]
    assert "'zzz'" in out["note"] and why in out["note"], out.get("note")
    assert not out["degraded"] and not out["partial"], "물어봤고 정말 없다 — 실패가 아니다"

    # 전부 0건이어도 같다 — 화면은 '일치하는 VOC 가 없습니다' 아래에 이 까닭을 보인다
    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["zzz"]})
    assert out["items"] == [] and why in out["note"]
    assert out["degraded"] is False and out["partial"] is False


@pytest.mark.parametrize("junk", [json.dumps({"message": "검색 점검 중"}, ensure_ascii=False),
                                  f"{a._TOOL_FAIL_MARK} 도구 search_voc 호출 실패: 1,800초 안에 답하지 않았다"])
def test_펼칠_것이_없는데_글이_왔으면_0건이_아니라_실패로_센다(monkeypatch, junk):
    async def call(tools, name, args):
        return junk if args["keyword"] == "bad" else _envelope([_voc(1, "ok")])

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["bad"]})
    assert out["items"] == [] and out["degraded"] is True and out["partial"] is False, out

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["ok", "bad"]})
    assert [x["id"] for x in out["items"]] == [1]
    assert out["partial"] is True and out["degraded"] is False, out


@pytest.mark.parametrize("blocks, ids", [
    ([], []),                                                                    # 옛 꼴(목록 반환)의 0건 — 블록이 하나도 없다
    ([{"type": "text", "text": _envelope([_voc(1, "zzz"), _voc(2, "zzz")])}], [1, 2]),   # 새 꼴 — 객체 하나가 글 블록 하나로
])
def test_도구_래퍼를_지난_진짜_꼴로도_읽는다(monkeypatch, blocks, ids):
    """MCP 어댑터는 (내용 블록, 아티팩트) 를 주고 도구 래퍼(_prep_tool)가 글로 만든다 — 블록이 0개면 글 '[]' 라, 실패로 세면 안 된다."""
    from langchain_core.tools import StructuredTool

    async def search(**_kw):
        return blocks, None
    t = a._prep_tool(StructuredTool(name="search_voc", description="VOC 검색", coroutine=search,
                                    response_format="content_and_artifact",
                                    args_schema={"type": "object", "properties": {}}), a.CATALOG_RESULT_MAX)

    async def fake_tools(*_a, **_k):
        return {"search_voc": t}
    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    out = asyncio.run(a.deliberate_voc_preview(a.VocPreviewRequest(message="x", keywords=["zzz"])))
    assert [x["id"] for x in out["items"]] == ids, out
    assert out["degraded"] is False and out["partial"] is False, out

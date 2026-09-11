# 'VOC 먼저 보기' 엔드포인트 — 못 씀·실패·정말 없음을 뭉개지 않는지, 검색어 교차·중복 제거
#
#   실행:  .venv/bin/python -m pytest tests/test_voc_preview.py -q
import asyncio
import json
import sys
from pathlib import Path

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

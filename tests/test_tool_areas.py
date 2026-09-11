# 도구 영역(게이트웨이 tool_areas.json)이 에이전트 도구 카탈로그에 실리는지 — 순서·미분류·권한 범위
#
#   실행:  .venv/bin/python -m pytest tests/test_tool_areas.py -q
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402


def _prime(monkeypatch, areas, meta):
    # 캐시를 '방금 받은 것' 으로 채워 네트워크를 타지 않게 한다.
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "at", time.time())
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "map", {n: "app" for n in areas})
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "areas", areas)
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "area_meta", {m["area"]: m for m in meta})


META = [{"area": "cad", "label": "CAD·형상 제어", "description": "d1"},
        {"area": "sim", "label": "시뮬레이션 실행·잡", "description": "d2"},
        {"area": "voc", "label": "VOC·시장 신호", "description": "d3"}]


def test_도구마다_영역과_라벨이_붙는다(monkeypatch):
    _prime(monkeypatch, {"find_parts": "cad", "submit_job": "sim"}, META)
    assert a._area_of("find_parts") == ("cad", "CAD·형상 제어")
    assert a._area_of("unknown") == ("", "")


def test_영역_목록은_분류표_순서이고_보이는_도구만_센다(monkeypatch):
    _prime(monkeypatch, {"s1": "sim", "s2": "sim", "s3": "sim", "c1": "cad", "v1": "voc"}, META)
    tools = {"s1": NS(), "s2": NS(), "c1": NS()}          # 권한으로 걸러져 v1·s3 는 안 보인다
    out = a._area_catalog(tools)
    assert [x["area"] for x in out] == ["cad", "sim"], "인원순이 아니라 분류표 순서여야 한다"
    assert [x["tool_count"] for x in out] == [1, 2]
    assert "voc" not in [x["area"] for x in out], "0개 영역은 싣지 않는다"


def test_미분류는_숨기지_않고_끝에_붙인다(monkeypatch):
    _prime(monkeypatch, {"c1": "cad"}, META)
    out = a._area_catalog({"c1": NS(), "new_app_tool": NS()})
    assert out[-1] == {"area": "", "label": "미분류", "desc": "영역 분류표에 없는 도구", "tool_count": 1}


def test_카탈로그_항목에_area_칸이_있다(monkeypatch):
    _prime(monkeypatch, {"c1": "cad"}, META)
    row = a._tool_catalog({"c1": NS(description="형상")})[0]
    assert row["area"] == "cad" and row["area_label"] == "CAD·형상 제어"

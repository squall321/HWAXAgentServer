# 모의해석(SIM) 카드를 부품 표로(HWAXPortal docs/request-1125 HA-19) — components 가 dict 목록이면 표 · 접촉 · 붙임 절 · 남은 원문(JSON)
#
#   실행:  .venv/bin/python -m pytest tests/test_record_sim_parts.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402


def _rec(content):
    return {"record_id": "r1", "doc_type": "SIM", "title": "낙하 모델", "content": content}


def test_HA19_부품_표와_짧은_절과_남은_원문():
    content = {"components": [{"pid": 1, "title": "케이스", "elem_class": "shell", "n_elems": 1200, "area_ext": 0.5, "volume": None,
                               "material": {"name": "PC"}}, {"pid": 2, "title": "유리", "material": "glass"}],
               "contact_edges": [[1, 2]], "attachments": [{"file": "a.k"}], "tool_call": {"name": "drop"}}
    v = a._record_view(_rec(content))
    t = v["table"]
    assert t["caption"] == "부품" and t["headers"] == list(a._SIM_PART_COLS) and t["total_rows"] == 2
    assert t["rows"][0][1] == "케이스" and t["rows"][0][-1] == "PC" and t["rows"][1][-1] == "glass"
    titles = [s["title"] for s in v["sections"]]
    assert titles == ["접촉", "붙임", "원문(JSON)"]
    raw = v["sections"][-1]["text"]
    assert '"tool_call"' in raw and '"components"' not in raw and v["total_sections"] == 3 and v["total_chars"] > 0


def test_HA19_components_가_없으면_종전_원문_보기():
    v = a._record_view(_rec({"tool_call": {"name": "drop"}}))
    assert v["table"] is None and [s["title"] for s in v["sections"]] == ["원문(JSON)"]

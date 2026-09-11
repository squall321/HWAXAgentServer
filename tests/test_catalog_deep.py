# 전문가 심층 보기 — 역할 문서 전문·지식카드 목록(검색·쪽·총수)·카드 본문(문서·표·그 밖)
#
#   실행:  .venv/bin/python -m pytest tests/test_catalog_deep.py -q
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402

PLAIN_SESSION = {"agent_type": "rel-drop-impact", "name": "낙하 전문가", "description": "낙하 충격",
                 "system_prompt": "낙하 전문가 역할", "response_config": {}}


# ── 심층 보기: 역할 문서·지식카드 목록·카드 본문 ─────────────────────────────
def test_역할_문서는_허브_공용_안내를_떼고_자동_틀은_비운다():
    doc = "나는 낙하 전문가다.\n\n## 범위\n- 낙하"
    assert a._role_doc(doc + "\n\n---\n\n## How to access this hub — use the MCP tools\n...") == doc
    assert a._role_doc('You are an assistant for "x" (`x`) inside the hub.') == ""
    assert a._role_doc(doc) == doc


def _stub_records(monkeypatch, handler):
    async def fake_tools(*_a, **_k):
        return {"list_records": object(), "get_record": object(), "get_agent_session": object()}

    async def fake_call(tools, name, args):
        return handler(name, args)

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", fake_call)


def test_지식카드_목록은_검색어와_쪽을_넘기고_총수를_준다(monkeypatch):
    seen = {}

    def handler(name, args):
        seen.update(args)
        return json.dumps({"total": 2729, "count": 1, "offset": 50,
                           "items": [{"id": "DOC-MX-MAT-2026-0000001918", "title": "SCS-0/SAS", "data_type": "DOC",
                                      "doc_type": "material_card", "year": 2026, "tags": ["category:ceramic"],
                                      "summary": "ceramic"}]})

    _stub_records(monkeypatch, handler)
    out = asyncio.run(a.catalog_agent_records(a.AgentRecordsRequest(key="material-twin-analyst", q="SiC",
                                                                       offset=50, limit=999)))
    assert seen == {"agents": ["material-twin-analyst"], "limit": a.CATALOG_PAGE_MAX, "offset": 50, "q": "SiC"}
    assert out["total"] == 2729 and out["items"][0]["doc_type"] == "material_card"


def test_지식카드_목록_실패는_빈_목록이_아니라_오류다(monkeypatch):
    _stub_records(monkeypatch, lambda n, args: "(tool list_records error: timeout)")
    out = asyncio.run(a.catalog_agent_records(a.AgentRecordsRequest(key="x")))
    assert out["error"] == "records_failed", "실패를 0건으로 주면 '지식이 없는 전문가'로 읽힌다"


def test_카드_본문은_문서·표·모르는_모양을_다_보인다(monkeypatch):
    docs = {
        "DOC-1": {"id": "DOC-1", "title": "문서", "data_type": "DOC", "summary": "요약",
                  "content": {"sections": [{"section_id": "1", "level": 1, "title": "본문", "content_text": "굽힘강도 = 265 MPa"}],
                              "sources": [{"title": "Bansal (1997)", "doi": "10.1016/x", "year": 1997}]}},
        "DATA-1": {"id": "DATA-1", "title": "표", "data_type": "DATA",
                   "content": {"headers": ["시료", "하중"], "rows": [["S1", 12.3]] * (a.RECORD_ROWS_MAX + 5),
                               "caption": "Sheet1"}},
        "SIM-1": {"id": "SIM-1", "title": "해석", "data_type": "SIM", "content": {"tool_call": {"args": {"e": 1.0}}}},
    }
    _stub_records(monkeypatch, lambda n, args: json.dumps(docs[args["record_id"]], ensure_ascii=False))
    d = asyncio.run(a.catalog_record(a.RecordRequest(id="DOC-1")))
    assert d["sections"][0]["text"] == "굽힘강도 = 265 MPa" and d["sources"][0]["doi"] == "10.1016/x"
    t = asyncio.run(a.catalog_record(a.RecordRequest(id="DATA-1")))
    assert t["table"]["total_rows"] == a.RECORD_ROWS_MAX + 5 and len(t["table"]["rows"]) == a.RECORD_ROWS_MAX
    assert t["table"]["rows"][0] == ["S1", 12.3], "숫자는 숫자로 둔다"
    assert t["truncated"] is True, "잘린 표를 전부인 척 보이지 않는다"
    s = asyncio.run(a.catalog_record(a.RecordRequest(id="SIM-1")))
    assert s["sections"][0]["title"] == "원문(JSON)" and '"tool_call"' in s["sections"][0]["text"]


def test_상세는_역할_문서와_지식카드_총수를_준다(monkeypatch):
    sess = {**PLAIN_SESSION, "system_prompt": "낙하 전문가 역할\n\n---\n\n## How to access this hub\n안내"}

    def handler(name, args):
        if name == "get_agent_session":
            return json.dumps(sess, ensure_ascii=False)
        return json.dumps({"total": 57, "items": [{"id": "DOC-1", "title": "t", "data_type": "DOC"}]})

    _stub_records(monkeypatch, handler)
    out = asyncio.run(a.catalog_agent(a.AgentDetailRequest(key="rel-drop-impact")))
    assert out["prompt"] == "낙하 전문가 역할"
    assert out["records_total"] == 57 and len(out["records"]) == 1

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
        "SIM-BIG": {"id": "SIM-BIG", "title": "큰 해석", "data_type": "SIM",
                    "content": {"inputs": {"deck": "x" * (a.RECORD_TEXT_MAX + 10)}}},
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
    j = s["sections"][0]["text"]
    assert j.startswith("```json\n") and j.endswith("\n```"), "모르는 모양은 코드 블록으로 보인다"
    big = asyncio.run(a.catalog_record(a.RecordRequest(id="SIM-BIG")))
    assert big["truncated"] is True and big["sections"][0]["text"].endswith("\n```"), "잘라도 펜스는 닫는다(자른 뒤 감싼다)"


# ── 블록 구조 본문(blocks·subsections·children)과 RAG 절 폴백 ───────────────────
def _card_calls(monkeypatch, cards, rag=None):
    """get_record 는 cards[id] 를, get_record_sections 는 rag(record_id) 를 준다. 부른 도구와 인자를 적는다."""
    calls = []

    def handler(name, args):
        calls.append((name, args))
        if name == "get_record":
            return json.dumps(cards[args["record_id"]], ensure_ascii=False)
        assert name == "get_record_sections", name
        return rag(args["record_id"]) if rag else None

    _stub_records(monkeypatch, handler)
    return calls


def test_MX_트리_카드는_블록과_하위_절을_마크다운으로_푼다(monkeypatch):
    """MXWhitePaper DocumentJSON — 글은 절의 blocks[] 에, 하위 절은 subsections 에 있다. 종전엔 최상위 절의
    content_text 만 읽어 제목만 늘어선 빈 카드로 열렸다(mx-whitepaper-analyst 273장)."""
    n = a.RECORD_ROWS_MAX + 3
    card = {"id": "DOC-MX-WP-1", "title": "백서", "data_type": "DOC", "content": {"sections": [
        {"id": "s1", "level": 1, "number": "1", "title": "개요", "layout": "full", "blocks": [
            {"type": "paragraph", "text": "모바일 AP 발열"},
            {"type": "table", "headers": ["항목", "값|단위"], "rows": [["두께", "0.8 |\nmm"], ["폭", 3]]},
            {"type": "code", "language": "python", "code": "print(1)"},
            {"type": "callout", "title": "주의", "text": "과열", "variant": "warning"},
            {"type": "math", "expression": "Q = mc\\Delta T"},
            {"type": "kpi-cards", "items": [{"label": "x"}]}],          # 글 칸이 없는 드문 블록은 빠진다
         "subsections": [{"id": "s1-1", "level": 2, "number": "1.1", "title": "세부", "subsections": [], "blocks": [
             {"type": "quote", "text": "인용문"},
             {"type": "table", "headers": ["n"], "rows": [[i] for i in range(n)]}]}]},
        {"id": "s2", "level": 1, "number": "2", "title": "맺음", "blocks": [
            {"type": "paragraph", "text": "끝"},
            {"type": "table", "headers": ["m"], "rows": [[i] for i in range(a.RECORD_ROWS_MAX)]}]}]}}   # 꼭 200행 — 안 잘렸다
    calls = _card_calls(monkeypatch, {"DOC-MX-WP-1": card})
    v = asyncio.run(a.catalog_record(a.RecordRequest(id="DOC-MX-WP-1")))
    assert [(s["title"], s["level"]) for s in v["sections"]] == [("개요", 1), ("세부", 2), ("맺음", 1)]
    assert v["sections"][0]["text"] == "\n\n".join([
        "모바일 AP 발열",
        "| 항목 | 값｜단위 |\n| --- | --- |\n| 두께 | 0.8 ｜ mm |\n| 폭 | 3 |",   # 칸 속 '|' 는 전각 — md.ts 는 ASCII 로 가른다
        "```python\nprint(1)\n```",
        "**주의** 과열",
        "$$\nQ = mc\\Delta T\n$$"])
    sub = v["sections"][1]["text"]
    assert sub.startswith("> 인용문\n\n| n |\n| --- |\n| 0 |\n")
    assert f"| {a.RECORD_ROWS_MAX - 1} |" in sub and f"| {a.RECORD_ROWS_MAX} |" not in sub
    assert sub.endswith(f"*표 앞 {a.RECORD_ROWS_MAX}행만 싣는다 — 전체 {n}행.*"), "잘린 표를 전부인 척 보이지 않는다"
    last = v["sections"][2]["text"]
    assert last.startswith("끝\n\n| m |\n| --- |\n| 0 |\n") and last.endswith(f"| {a.RECORD_ROWS_MAX - 1} |")
    assert "표 앞" not in v["sections"][0]["text"] + last, "안 잘린 표에 잘렸다고 적었다"
    assert [s["id"] for s in v["sections"]] == ["", "", ""], "절 id 는 채우지 않는다(한 카드 안에서 겹친다 — 화면의 React 키)"
    assert v["truncated"] is False and [c[0] for c in calls] == ["get_record"], "글이 있는 카드는 RAG 절을 부르지 않는다"


def test_KooRemapper_카드는_children_과_표_그림_ref_를_푼다(monkeypatch):
    """허브 변환기 DocumentJSON(kooremapper-iga) — 하위 절은 children, 표·그림은 content.tables·figures 에 두고
    블록이 ref 로 가리킨다. 코드 글은 code 가 아니라 text 에 있다. 절 id 가 한 카드 안에서 겹친다(실측 6장 중 2장)."""
    card = {"id": "DOC-HE-TST-1", "title": "IGA", "data_type": "DOC", "content": {
        "schema_version": "1.0", "toc": [], "sources": [], "meta": {},
        "tables": [{"id": "T1", "caption": "격자 수", "headers": ["방향", "개수"], "rows": [["u", 12]]},
                   {"id": "T2", "rows": [["축", "값"], ["x", 1]]}],                 # 머리행이 없으면 첫 행이 머리행이다
        "figures": [{"id": "F1", "caption": "제어점 배치"}],
        "sections": [{"id": "1", "level": 1, "title": "모델", "table_refs": ["T1"], "figure_refs": ["F1"], "blocks": [
            {"type": "paragraph", "text": "IGA 쉘"}, {"type": "table", "ref": "T1"}, {"type": "figure", "ref": "F1"},
            {"type": "code", "text": "*IGA_SHELL"}, {"type": "table", "ref": "T2"},
            {"type": "table", "ref": "T9"}],                                             # 못 찾는 ref 는 건너뛴다
            "children": [{"id": "1", "level": 2, "title": "입력", "children": [], "blocks": [
                {"type": "list_item", "marker": "1.", "text": "형상"}, {"type": "list_item", "text": "재료"}]}]}]}}
    _card_calls(monkeypatch, {"DOC-HE-TST-1": card})
    v = asyncio.run(a.catalog_record(a.RecordRequest(id="DOC-HE-TST-1")))
    assert [(s["title"], s["level"]) for s in v["sections"]] == [("모델", 1), ("입력", 2)]
    assert v["sections"][0]["text"] == ("IGA 쉘\n\n**격자 수**\n\n| 방향 | 개수 |\n| --- | --- |\n| u | 12 |"
                                        "\n\n*[그림] 제어점 배치*\n\n```\n*IGA_SHELL\n```"
                                        "\n\n| 축 | 값 |\n| --- | --- |\n| x | 1 |")
    assert v["sections"][1]["text"] == "1. 형상\n\n- 재료"


_STUB = {"id": "DOC-MX-WP-9", "title": "자리표시", "data_type": "DOC",
         "content": {"sections": [{"section_id": "1", "level": 1, "title": "본문", "content_text": ""}]}}


def test_글이_없는_자리표시_카드는_RAG_절로_채운다(monkeypatch):
    """동기화 자리표시 — content 엔 빈 '본문' 절 하나뿐이고 글은 RAG 절(record_sections)에만 있다(MX 154장 중 149장).
    목록 도구라 원소마다 content 하나로 와서 _call 이 이어 붙인다. 첫 행도 빈 '본문' 이다(0309 실측)."""
    rows = [{"section_id": "1", "level": 1, "title": "본문", "content_text": ""},
            {"section_id": "2", "level": 1, "title": "가", "content_text": "RAG 글 하나"},
            {"section_id": "3", "level": 2, "title": "나", "content_text": "RAG 글 둘"}]
    calls = _card_calls(monkeypatch, {"DOC-MX-WP-9": _STUB},
                        rag=lambda rid: "".join(json.dumps(r, ensure_ascii=False) for r in rows))
    v = asyncio.run(a.catalog_record(a.RecordRequest(id="DOC-MX-WP-9")))
    assert calls[1:] == [("get_record_sections", {"record_id": "DOC-MX-WP-9"})]
    assert [(s["id"], s["title"], s["level"], s["text"]) for s in v["sections"]] == [
        ("2", "가", 1, "RAG 글 하나"), ("3", "나", 2, "RAG 글 둘")], "빈 행은 버리고 글이 있는 절만 싣는다"
    assert "error" not in v and v["truncated"] is False


def test_어디에도_글이_없으면_빈_카드이고_RAG_조회_실패는_실패다(monkeypatch):
    """글이 정말 없는 카드는 절을 비운다 — 읽기 칸이 '본문이 비어 있는 카드입니다.' 를 띄운다(제목만 늘어놓지 않는다).
    조회 실패는 빈 카드가 아니라 실패다 — _call 의 '(tool … error', _cap_tool 의 ✖, 평문 'Error executing tool'."""
    for empty in (None, "", "[]", json.dumps({"section_id": "1", "level": 1, "title": "본문", "content_text": ""})):
        _card_calls(monkeypatch, {"DOC-MX-WP-9": _STUB}, rag=lambda rid, e=empty: e)
        v = asyncio.run(a.catalog_record(a.RecordRequest(id="DOC-MX-WP-9")))
        assert "error" not in v and v["sections"] == [] and v["table"] is None and v["truncated"] is False, (empty, v)
    for bad in ("(tool get_record_sections error: timeout)",
                a._TOOL_FAIL_MARK + " 도구 get_record_sections 호출 실패: backend session down",
                "Error executing tool get_record_sections: boom"):
        _card_calls(monkeypatch, {"DOC-MX-WP-9": _STUB}, rag=lambda rid, b=bad: b)
        v = asyncio.run(a.catalog_record(a.RecordRequest(id="DOC-MX-WP-9")))
        assert v.get("error") == "record_failed" and v.get("id") == "DOC-MX-WP-9", (bad, v)
    # 표 카드는 글 절이 없어도 빈 카드가 아니다 — RAG 절을 부르지 않는다.
    calls = _card_calls(monkeypatch, {"DATA-1": {"id": "DATA-1", "title": "표", "data_type": "DATA",
                                               "content": {"headers": ["a"], "rows": [[1]]}}})
    asyncio.run(a.catalog_record(a.RecordRequest(id="DATA-1")))
    assert [c[0] for c in calls] == ["get_record"]


def _page(rid, at=None):
    """읽기 칸 한 쪽 — at 은 앞 쪽의 next 를 그대로 돌려준다(포털 fetchRecord 와 같은 꼴)."""
    req = a.RecordRequest(id=rid, **({"section_offset": at["section"], "char_offset": at["char"]} if at else {}))
    return asyncio.run(a.catalog_record(req))


def _total(v):
    return v.get("total_sections"), v.get("total_chars")


def test_긴_카드는_총량과_이어_읽을_위치를_주고_따라가면_원문과_같다(monkeypatch):
    """6만 자에서 말없이 잘리던 카드(열어 본 카드 38%) — 총량(total_sections·total_chars)과 다음 위치(next)를 준다.
    상한은 대개 절 중간에서 걸린다 — 절 번호만 넘기면 그 절 앞을 되풀이하거나 뒤를 건너뛰어 글자 위치를 함께 넘긴다."""
    assert a.RECORD_TEXT_MAX == 60_000, "아래 위치는 6만 자 상한 기준이다"
    secs = [{"section_id": str(i), "level": 1, "title": f"절{i}",
             "content_text": "".join(f"{i}{j:07d}" for j in range(3125))} for i in range(5)]    # 25,000자씩 5절
    rag = [{"section_id": "1", "level": 1, "title": "본문", "content_text": ""},
           {"section_id": "2", "level": 1, "title": "가", "content_text": "가" * 50_000},
           {"section_id": "3", "level": 2, "title": "나", "content_text": "나" * 20_000}]
    cards = {"DOC-LONG": {"id": "DOC-LONG", "title": "긴 카드", "data_type": "DOC", "content": {"sections": secs}},
             "DOC-EDGE": {"id": "DOC-EDGE", "title": "경계", "data_type": "DOC", "content": {"sections": [
                 {"section_id": "1", "level": 1, "title": "꽉 참", "content_text": "가" * 60_000},
                 {"section_id": "2", "level": 1, "title": "빈 절", "content_text": ""}]}},
             "DOC-MX-WP-9": _STUB,
             "SIM-1": {"id": "SIM-1", "title": "해석", "data_type": "SIM", "content": {"tool_call": {"e": 1}}}}
    calls = _card_calls(monkeypatch, cards,
                        rag=lambda rid: "".join(json.dumps(r, ensure_ascii=False) for r in rag))

    # 1) 5절·125,000자 — next 를 따라 세 쪽에 다 받고, 이어 붙이면 원문과 글자까지 같다.
    pages, at = [], None
    while len(pages) < 10:
        pages.append(_page("DOC-LONG", at))
        at = pages[-1].get("next")
        if not at:
            break
    assert [_total(p) for p in pages] == [(5, 125_000)] * 3, "총량은 쪽마다 카드 전체 값이다"
    assert [p.get("next") for p in pages] == [{"section": 2, "char": 10_000}, {"section": 4, "char": 20_000}, None]
    assert [p["truncated"] for p in pages] == [True, True, False]
    assert [sum(len(s["text"]) for s in p["sections"]) for p in pages] == [60_000, 60_000, 5_000]
    got = []
    for prev, p in zip([None, *pages], pages):
        add = list(p["sections"])
        if prev and prev["next"]["char"] > 0:      # 절 중간에서 이어 받은 첫 조각은 앞 절에 붙인다(포털 loadMore 규칙)
            got[-1] = {**got[-1], "text": got[-1]["text"] + add.pop(0)["text"]}
        got += add
    assert [(s["title"], s["text"]) for s in got] == [(s["title"], s["content_text"]) for s in secs]

    # 1-2) 한 절이 두 쪽을 넘으면 가운데 쪽은 그 절 안에서 시작해 그 절 안에서 끝난다(cam-aa-process 첫 카드의 꼴).
    one = "".join(f"{j:07d};" for j in range(16_250))                                   # 130,000자 한 절
    cards["DOC-ONE"] = {"id": "DOC-ONE", "title": "한 절", "data_type": "DOC",
                        "content": {"sections": [{"section_id": "1", "level": 1, "title": "통", "content_text": one}]}}
    texts, nexts, at = [], [], None
    while len(nexts) < 10:
        p = _page("DOC-ONE", at)
        texts.append("".join(s["text"] for s in p["sections"]))
        nexts.append(p.get("next"))
        at = nexts[-1]
        if not at:
            break
    assert nexts == [{"section": 0, "char": 60_000}, {"section": 0, "char": 120_000}, None] and "".join(texts) == one

    # 2) 상한이 절 경계에 맞으면 다음 쪽은 다음 절의 처음이다. 그 쪽에 빈 절만 남아도 빈 카드가 아니다(카드 전체로 판정).
    e1 = _page("DOC-EDGE")
    assert e1.get("next") == {"section": 1, "char": 0} and e1["truncated"] is True and _total(e1) == (2, 60_000)
    e2 = _page("DOC-EDGE", e1.get("next"))
    assert [(s["title"], s["text"]) for s in e2["sections"]] == [("빈 절", "")] and e2.get("next") is None
    assert e2["truncated"] is False and "get_record_sections" not in [c[0] for c in calls]

    # 3) 끝을 넘긴 위치는 빈 쪽이다(원문(JSON) 보기로 바뀌지 않는다). 음수는 0 으로 막는다.
    far = _page("DOC-LONG", {"section": 99, "char": 0})
    assert far["sections"] == [] and far.get("next") is None and far["truncated"] is False and _total(far) == (5, 125_000)
    neg = _page("DOC-LONG", {"section": -3, "char": -7})
    assert neg["sections"] == pages[0]["sections"] and neg.get("next") == pages[0]["next"]

    # 4) 모르는 모양(SIM)의 원문(JSON) 보기는 글이 있는 카드다 — 총량이 0 이면 빈 카드로 판정돼 RAG 로 갈린다.
    s = _page("SIM-1")
    assert [x["title"] for x in s["sections"]] == ["원문(JSON)"] and _total(s)[0] == 1 and _total(s)[1] > 0
    assert "get_record_sections" not in [c[0] for c in calls]

    # 5) 자리표시 카드의 RAG 절도 같은 위치로 이어 읽는다 — 총량·next 는 RAG 절 기준이다.
    r1 = _page("DOC-MX-WP-9")
    assert [(x["title"], len(x["text"])) for x in r1["sections"]] == [("가", 50_000), ("나", 10_000)]
    assert r1.get("next") == {"section": 1, "char": 10_000} and r1["truncated"] is True and _total(r1) == (2, 70_000)
    r2 = _page("DOC-MX-WP-9", r1.get("next"))
    assert [(x["title"], x["text"]) for x in r2["sections"]] == [("나", "나" * 10_000)] and r2.get("next") is None
    assert r2["truncated"] is False and _total(r2) == (2, 70_000)

    # 6) 빈 카드 표지 — 절 0개·총량 0·next 없음.
    rag.clear()
    z = _page("DOC-MX-WP-9")
    assert (z["sections"], _total(z), z.get("next"), z["truncated"]) == ([], (0, 0), None, False)


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

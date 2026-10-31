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


def test_notes_가_글_하나로_와도_글자로_쪼개지_않는다(monkeypatch):
    """SF 는 notes 를 목록으로 주지만 꼴이 이미 한 번 바뀐 계약이다(HA-33) — 글 하나로 와도 통째로 싣는다."""
    why = "해석한 조건에 맞는 글이 없다(최근 90일)"

    async def call(tools, name, args):
        return json.dumps({"total": 0, "results": [], "strategy": "semantic", "notes": why}, ensure_ascii=False)

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["zzz"]})
    assert out["note"] == f"'zzz' 0건 — {why}", out.get("note")


@pytest.mark.parametrize("bare", [
    [{**_voc(i, "x"), "content_translated": None} for i in (1, 2)],   # 번역 전 글 — 본문 칸이 비어 온다
    ["모르는 행", 2],                                                   # 모르는 행 꼴 — 읽을 행이 없어도 받은 수는 싣는다
])
def test_받은_행에_본문이_없으면_실패가_아니라_받은_수와_까닭을_싣는다(monkeypatch, bare):
    """SF 키워드 경로는 번역문(content_translated)만 골라 번역 전 글은 본문 없이 온다. SF 는 답했고 펼칠 것도 있었으니 실패(화면의
    '못 물어봄')가 아니라 '0건 + 까닭' 이다 — 받은 수를 안 실으면 '정말 없음' 과 같아 보인다(HA-33 반대 검토)."""
    why = "'phrase' 로 0건이라 'all_tokens'(낱말 모두 · 순서 무관)로 찾았다"
    data = {"hinge": _envelope([_voc(9, "hinge")]), "힌지 소음": _envelope(bare, [why], "keyword")}

    async def call(tools, name, args):
        return data[args["keyword"]]

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["힌지 소음"]})
    assert out["items"] == [] and out["degraded"] is False and out["partial"] is False, out
    assert "'힌지 소음'" in out["note"] and "받은 2건" in out["note"] and why in out["note"], out.get("note")

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["hinge", "힌지 소음"]})
    assert [x["id"] for x in out["items"]] == [9] and out["partial"] is False and "받은 2건" in out["note"], out


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


# ── SignalForge SG-04 주의 줄 — 행이 있어도 미리보기에 싣는다(HWAXPortal docs/request-1030 HA-33 덧붙임, SignalForge ec40075) ────────
# SF 해석(shared/ontology.py interpret)은 해석한 제품 코드가 PLM 프로젝트 여럿에 걸리거나 질의가 다른 프로젝트를 짚으면 notes 끝에
# '주의 — <plm_caution.note>' 를 달고, search_voc 봉투의 notes 가 그 해석 notes 다(mcp-server/server.py search_voc — 봉투에 plm_caution 칸은
# 없다). 종전엔 notes 를 0건일 때만 실어, 다른 제품의 VOC 가 섞였을 수 있다는 말이 행이 보이는 미리보기에 닿지 않았다.
# 아래 글은 SF plm_caution 의 note 꼴 그대로다. 프로젝트 이름은 SF 시험처럼 지어낸 이름(알파 · 베타), 제품 코드도 지어낸 것이다.
_SG04 = " (제품 행을 프로젝트별로 나눌지는 데이터 결정 대기 — SG-04)"
_MODEL_NOTE = "모델 하나로 봤다 — 같은 세대 전부는 '<모델> 시리즈' 로 검색"     # 같은 notes 에 오는 다른 해석 줄(ontology.py)


def _caution_multi(code, projects):
    """제품 코드가 PLM 프로젝트 여럿의 개발모델에 걸리는 갈래."""
    return (f"주의 — {code} 는 PLM 프로젝트 {len(projects)}개({', '.join(sorted(projects))})의 개발모델에 걸린다 — 이 제품 코드의 "
            "VOC 는 그 프로젝트들을 가르지 않고 함께 센다" + _SG04)


def _caution_foreign(code, named, own):
    """질의의 이름 · 모델이 짚은 프로젝트가 해석한 제품의 프로젝트가 아닌 갈래."""
    return (f"주의 — 질의의 이름·모델은 PLM 프로젝트 {named} 인데 해석한 제품({code})의 개발모델은 프로젝트 {own} 것이다 — "
            "다른 제품의 VOC 일 수 있다" + _SG04)


C_PX1 = _caution_multi("PX1", ["알파", "베타"])
C_PX2 = _caution_foreign("PX2", "알파", "베타")


@pytest.mark.parametrize("notes", [[_MODEL_NOTE, C_PX1, "사전에 없는 표현이라 LLM 이 증상을 추론했다: 발열"], C_PX1],
                         ids=["list", "text"])
def test_SG04_주의_줄은_행이_있어도_미리보기에_싣고_다른_notes_는_싣지_않는다(monkeypatch, notes):
    """notes 가 글 하나로 와도(꼴이 한 번 바뀐 계약이다 — 위 시험) 그 줄을 통째로 본다."""
    async def call(tools, name, args):
        rows = [_voc(1, "px1 hinge"), _voc(2, "px1 hinge")]
        return json.dumps({"total": 2, "results": rows, "strategy": "semantic", "match_mode": "semantic", "notes": notes},
                          ensure_ascii=False, indent=2)

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["px1 hinge"]})
    assert [x["id"] for x in out["items"]] == [1, 2]
    assert out["note"] == f"'px1 hinge' {C_PX1}", out.get("note")
    assert not out["degraded"] and not out["partial"]


def test_SG04_같은_주의는_검색어끼리_한_번만_싣고_받은_검색어를_모두_단다(monkeypatch):
    data = {"px1 hinge": _envelope([_voc(1, "px1 hinge"), _voc(2, "px1 hinge")], [C_PX1]),
            "px1 crease": _envelope([_voc(3, "px1 crease")], [_MODEL_NOTE, C_PX1]),
            "px2 battery": _envelope([_voc(4, "px2 battery")], [C_PX2]),
            "screen flicker": _envelope([_voc(5, "screen flicker")], [_MODEL_NOTE])}

    async def call(tools, name, args):
        return data[args["keyword"]]

    monkeypatch.setattr(a, "_VOC_PREVIEW_MAX", 3)
    kws = list(data) + ["px1 hinge"]              # 사람이 같은 검색어를 두 번 넣어도 검색어 표지는 한 번
    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": kws})
    assert out["note"] == (f"'px1 hinge' · 'px1 crease' {C_PX1} / 'px2 battery' {C_PX2} / 5건 중 3건만 보입니다 — 검색어를 좁혀 보세요."), \
        "주의 줄이 맨 앞 · 받은 차례대로 · 같은 줄은 한 번"
    assert out["note"].count(C_PX1) == 1 and _MODEL_NOTE not in out["note"]


def test_SG04_0건_검색어의_글은_종전_그대로고_주의_줄로_실린_글만_뺀다(monkeypatch):
    """0건 갈래는 notes 를 통째로 싣던 그대로다(주의 줄도 그 안에 든다) — 그 검색어로 따로 주의 줄을 만들지 않는다. 행이 있는 다른 검색어가 같은
    주의를 받았으면 그 검색어의 줄이 따로 서고, 0건 줄에서는 그 글만 뺀다 — 150자 남짓한 같은 주의가 안내 한 줄에 두 번 나왔다(반대 검토 10-31).
    0건 검색어는 주의 줄의 검색어 표지에 달지 않는다(보이는 행이 없다). 다른 까닭이 없어도 0건이었다는 것은 남긴다."""
    why = "해석한 조건에 맞는 글이 없다(최근 90일) — 기간·시장·채널을 넓히거나 semantic=False 로 키워드 검색을 해 보라"
    data = {"px1 hinge": _envelope([_voc(1, "px1 hinge")], [C_PX1]), "px1 zzz": _envelope([], [C_PX1, why]),
            "px1 none": _envelope([], [C_PX1])}

    async def call(tools, name, args):
        return data[args["keyword"]]

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["px1 zzz"]})
    assert out["items"] == [] and out["note"] == f"'px1 zzz' 0건 — {C_PX1} · {why}", out.get("note")

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["px1 hinge", "px1 zzz"]})
    assert [x["id"] for x in out["items"]] == [1]
    assert out["note"] == f"'px1 hinge' {C_PX1} / 'px1 zzz' 0건 — {why}", out.get("note")
    assert out["note"].count(C_PX1) == 1
    assert not out["degraded"] and not out["partial"]

    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["px1 hinge", "px1 none"]})
    assert out["note"] == f"'px1 hinge' {C_PX1} / 'px1 none' 0건", out.get("note")


def test_SG04_주의를_받은_검색어의_행이_다른_표지로_보이면_그_표지를_단다(monkeypatch):
    """화면은 행마다 그 행을 먼저 돌려준 검색어를 표지로 단다(번갈아 뽑기가 행 id 로 겹침을 뺀다). 주의를 받은 검색어의 행이 모두 다른 검색어
    표지로 보이면 '그 검색어' 만 적힌 주의는 보이는 행에 걸리지 않는 말로 읽혔다(반대 검토 10-31) — 그 주의가 걸리는 보이는 행의 표지를 덧붙이고,
    상한에 잘려 하나도 안 보이면 그렇다고 적는다. 행이 모두 제 검색어 표지로 보이면 종전 그대로다(위 시험들)."""
    async def call(tools, name, args):
        return data[args["keyword"]]

    rows = [_voc(1, "x"), _voc(2, "x")]
    data = {"hinge": _envelope(rows, [_MODEL_NOTE]), "px1 hinge": _envelope(rows, [C_PX1])}
    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["hinge", "px1 hinge"]})
    assert [(x["id"], x["keyword"]) for x in out["items"]] == [(1, "hinge"), (2, "hinge")]
    assert out["note"] == f"'px1 hinge'(보이는 행 표지 'hinge') {C_PX1}", out.get("note")

    data = {"hinge": _envelope([_voc(1, "hinge")]), "px1 hinge": _envelope([_voc(1, "x"), _voc(2, "px1 hinge")], [C_PX1])}
    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["hinge", "px1 hinge"]})
    assert [(x["id"], x["keyword"]) for x in out["items"]] == [(1, "hinge"), (2, "px1 hinge")]
    assert out["note"] == f"'px1 hinge'(보이는 행 표지 'hinge' · 'px1 hinge') {C_PX1}", out.get("note")

    monkeypatch.setattr(a, "_VOC_PREVIEW_MAX", 1)
    data = {"hinge": _envelope([_voc(1, "hinge"), _voc(2, "hinge")]), "px1 hinge": _envelope([_voc(3, "px1 hinge")], [C_PX1])}
    out = _run(monkeypatch, {"search_voc": object()}, call, {"message": "x", "keywords": ["hinge", "px1 hinge"]})
    assert [x["id"] for x in out["items"]] == [1]
    assert out["note"] == f"'px1 hinge'(보이는 행 없음) {C_PX1} / 3건 중 1건만 보입니다 — 검색어를 좁혀 보세요.", out.get("note")

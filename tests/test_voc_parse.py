# VOC 환기가 목록 도구 결과를 실제로 읽는지 — 원소별 블록이 이어져 오는 형태(실측)로 고정한다
#
# 2026-09-11 에 잡은 무음 결함: query_voc·get_top_issues 가 항목마다 content 블록을 따로 주고
# _call 이 그걸 `{…}{…}` 로 이어 붙이는데, _parse_json 은 LLM 출력용이라 마지막 객체 하나만
# 취했다. 그래서 환기문에 부정 VOC 원문도, 제품별 이슈도 한 줄 없이 경보 요약만 남았다.
#
#   실행:  .venv/bin/python -m pytest tests/test_voc_parse.py -q
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402


def _blocks(rows):
    return "".join(json.dumps(r, ensure_ascii=False, indent=2) for r in rows)   # _call 이 만드는 모양 그대로


def test_이어진_블록을_모두_읽는다():
    rows = [{"id": i, "content_translated": f"voc {i}"} for i in range(5)]
    assert [r["id"] for r in d._parse_json_multi(_blocks(rows))] == [0, 1, 2, 3, 4]


def test_parse_json_은_목록_결과에_쓰면_안_된다():
    # 이 테스트는 함정 자체를 기록한다 — 누가 다시 _parse_json 으로 되돌리면 아래 환기 테스트가 깨진다.
    rows = [{"id": i} for i in range(5)]
    assert d._parse_json(_blocks(rows)) == {"id": 4}


def test_불량_환기가_VOC_원문과_이슈를_싣는다(monkeypatch):
    alerts = {"summary": "경보 1건", "high_negative_ratio": [{"product_code": "GZF3"}]}
    issues = [{"category": c, "total_count": 10} for c in ("hinge", "crease", "battery")]
    vocs = [{"product_code": "GZF3", "sentiment_score": -0.8, "content_translated": f"hinge broke after {i} weeks"}
            for i in range(1, 4)]

    async def fake_call(tools, name, args):
        return {"alert_check": json.dumps(alerts), "get_top_issues": _blocks(issues),
                "query_voc": _blocks(vocs)}.get(name, "")

    async def fake_llm_text(llm, system, human):
        return '{"relevant": true, "reason": "힌지 파손"}'

    monkeypatch.setattr(d, "_call", fake_call)
    monkeypatch.setattr(d, "_llm_text", fake_llm_text)
    display, inject, used = asyncio.run(d._defect_briefing({}, None, "폴드 힌지 파손 원인"))
    assert "hinge, crease, battery" in display, "제품별 이슈가 비었다"
    assert display.count("부정 VOC") == 3, "부정 VOC 원문이 비었다"
    assert "hinge broke after 1 weeks" in inject

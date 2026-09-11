# 권한(entitlements) 게이트 — 포털이 준 권한으로 심의·Thinking·전문가 기능을 막고, 안 보낸 옛 호출은 막지 않는다
#
#   실행:  .venv/bin/python -m pytest tests/test_entitlements.py -q
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402


def _req(message, **kw):
    return a.ChatRequest(message=message, **kw)


def test_권한을_안_보낸_옛_호출은_막지_않는다():
    assert a._denied_feature(_req("/심의 힌지 파손 원인")) is None


def test_심의_계열_트리거는_심의_권한이_필요하다():
    for m in ("/심의 힌지 파손 원인", "/시뮬심의 낙하 크랙", "/시험계획 OCA 물성"):
        assert a._denied_feature(_req(m, entitlements=["feat:chat"])) == "feat:deliberation", m
    assert a._denied_feature(_req("/심의 x", entitlements=["feat:chat", "feat:deliberation"])) is None


def test_Thinking·전문가_지정·전문가_검색():
    assert a._denied_feature(_req("질문", thinking=True, entitlements=["feat:chat"])) == "feat:thinking"
    assert a._denied_feature(_req("질문", pinned_agent="rel-drop-impact",
                                  entitlements=["feat:chat"])) == "feat:expert-chat"
    assert a._denied_feature(_req("전문가 뭐 있어", entitlements=["feat:chat"])) == "feat:expert-chat"
    assert a._denied_feature(_req("그냥 질문", entitlements=["feat:chat"])) is None


def test_막히면_심의를_돌리지_않고_대화에_남는_한_줄로_답한다(monkeypatch):
    ran = []
    monkeypatch.setattr(a, "run_deliberation", lambda *x, **k: ran.append(1))
    resp = asyncio.run(a.chat(_req("/심의 힌지", entitlements=["feat:chat"])))

    async def body():
        return b"".join([c async for c in resp.body_iterator]).decode("utf-8")

    out = asyncio.run(body())
    assert not ran, "권한이 없으면 심의 엔진을 아예 부르지 않는다"
    assert "전문가 심의" in out and "내 권한" in out and "event: done" in out

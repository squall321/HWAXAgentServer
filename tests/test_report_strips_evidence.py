# /보고서 가 챗 답 끝의 근거 블록(실제로 조회한 도구 목록)을 RA 보고서에 싣지 않는지 — 사용자 요청 11-12(포털 화면은 접고 복사는 그대로)
import asyncio
import json

import app as A
import deliberation as D


class _Tool:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return json.dumps({"id": 7, "title": "t"})


def _save(monkeypatch, history):
    tool = _Tool()

    async def fake_tools(app, groups, user="", user_pat=""):
        return {"create_report_draft": tool}
    monkeypatch.setattr(D, "_tools_by_name", fake_tools)

    async def run():
        return [c async for c in D.run_report_save(None, "", history, [], user="u@corp.com")]
    asyncio.run(run())
    assert tool.calls, "보고서를 저장했다"
    return json.dumps(tool.calls[-1], ensure_ascii=False)


def test_표지는_에이전트_서버의_근거_블록과_같은_글이다():
    block = A._evidence_block([("get_part", "id=3")], [])
    assert block.startswith("\n\n---\n" + A._EVIDENCE_MARK) and D._CHAT_EVIDENCE_SPLIT == "\n---\n" + A._EVIDENCE_MARK


def test_보고서에는_근거_블록이_들어가지_않고_답_본문은_그대로_든다(monkeypatch):
    body = "결론: 12um 는 위험하다. 피로 수명이 절반이다."
    answer = body + A._evidence_block([("get_part", "id=3"), ("fatigue_life", "")], ["99.9"])
    sent = _save(monkeypatch, [{"role": "user", "content": "구리 두께를 낮춰도 되나"},
                               {"role": "assistant", "content": answer}])
    assert "12um 는 위험하다" in sent and "피로 수명이 절반이다" in sent
    assert "실제로 조회한 것" not in sent and "get_part" not in sent and "99.9" not in sent


def test_근거_블록이_없는_답과_사용자_글은_그대로다():
    assert D._strip_chat_evidence("그냥 답이다.\n\n---\n다른 구분선") == "그냥 답이다.\n\n---\n다른 구분선"
    assert D._strip_chat_evidence("") == ""

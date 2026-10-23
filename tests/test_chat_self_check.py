# 챗 자기검증 — 초안을 근거와 대조해 한 번 고친 뒤 본문으로 내는지(HWAXPortal docs/chat-self-check)
"""사용자 결정(10-23, SC-D1) — 검증한 답만 본문에(초안은 '생각 중' 칸) · 같은 챗 모델 · 항상(근거도 수치도 없는 짧은 답만 건너뜀).

  · 감싸개(SC-D2) — 안쪽 턴의 token 은 reasoning 으로 돌리고 result · meta · done 은 붙잡았다가, 검증 뒤 고친 답을 token 으로 낸다.
  · 고친 답이 수상하면(끊김 · 빔 · 지나치게 짧음 · 그림 잃음) 초안을 그대로 내고 까닭을 meta.self_check 에 적는다.
  · 근거 블록은 고친 답으로 다시 센다(고친 답에서 사라진 수치는 '출처 없음' 에 남지 않는다).
  · 오류 · 조기 종료 · 글이 아닌 결과는 초안을 본문으로 그대로 흘린다(종전과 같은 끝).
  · 끄면(CHAT_SELF_CHECK=0) 종전 경로 그대로 — LLM 한 번.
"""
import asyncio
import json
import signal
import socket
import subprocess
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
from langchain_core.messages import AIMessageChunk  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

import app as a  # noqa: E402
import review_gate as rg  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _events(chunks) -> list:
    out = []
    for c in chunks:
        ev, data = a._sse_parse(c)
        if ev:
            out.append((ev, data))
    return out


def _text(evs, kind: str) -> str:
    return "".join(d.get("delta", "") for e, d in evs if e == kind)


# ── 순수 함수 ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_sse_parse_는_우리_프레임만_읽고_나머지는_빈_값이다():
    assert a._sse_parse(a._sse("token", {"delta": "가\n나"})) == ("token", {"delta": "가\n나"})
    assert a._sse_parse(b": ping\n\n") == ("", {})
    assert a._sse_parse(a._sse("token", {"delta": "x"}) + a._sse("done", {})) == ("", {}), "두 프레임이 붙은 조각은 그대로 흘린다"
    assert a._sse_parse(b"event: token\ndata: {broken\n\n") == ("", {})


def test_기본은_켬이고_0_만_끈다(monkeypatch):
    monkeypatch.delenv("CHAT_SELF_CHECK", raising=False)
    assert a._self_check_on() is True, "사용자 결정 SC-D1 — 항상 검증한다"
    for v, on in (("1", True), ("", True), ("0", False)):
        monkeypatch.setenv("CHAT_SELF_CHECK", v)
        assert a._self_check_on() is on, v


def test_판정_읽기():
    assert a._self_check_parse("[판정] 고칠 것 없음\n[고친 점]\n- 없음\n[답]\n") == ("ok", [], "")
    v, fx, ans = a._self_check_parse("[판정] **고침**\n[고친 점]\n- 12.5 를 근거대로 15.2 로\n- 빠진 ② 를 채움\n[답]\n고친 답 전체")
    assert (v, fx, ans) == ("fixed", ["12.5 를 근거대로 15.2 로", "빠진 ② 를 채움"], "고친 답 전체")
    assert a._self_check_parse("[판정] 고침 없음\n[답]\n")[0] == "ok"
    assert a._self_check_parse("판정을 못 하겠습니다")[0] is None


def test_가드는_수상한_고친_답_대신_초안을_내고_까닭을_적는다():
    draft = "결론은 이렇다. " * 30 + "\n![응력](/artifacts/a.png)"
    ans_ok = draft.replace("이렇다", "저렇다")
    assert a._self_check_judge(draft, None, [], "", False) == (draft, {"status": "unreadable",
                                                                      "reason": "검토자 답을 읽지 못했다 — 초안을 그대로 냈다"})
    assert a._self_check_judge(draft, "ok", [], "", False) == (draft, {"status": "ok"})
    assert a._self_check_judge(draft, "fixed", ["x"], ans_ok, False) == (ans_ok, {"status": "fixed", "fixes": ["x"]})
    for ans, cut, why in ((ans_ok, True, "끊겼다"), ("", False, "비었다"), ("짧다", False, "지나치게 짧다"),
                          (ans_ok.replace("![응력](/artifacts/a.png)", ""), False, "그림 1개를 잃었다")):
        final, check = a._self_check_judge(draft, "fixed", ["x"], ans, cut)
        assert final == draft and check["status"] == "suspect" and why in check["reason"] and check["fixes"] == ["x"], (why, check)
    assert a._self_check_judge(draft, "fixed", ["x"], draft + "\n", False) == (draft, {"status": "ok"}), "바뀐 글자가 없으면 고친 것이 아니다"


def test_건너뛰기는_이번_턴_근거도_수치도_없는_짧은_답만():
    req = a.ChatRequest(message="안녕", groups=[], history=[])
    assert a._self_check_skip(req, "안녕하세요! 무엇을 도와드릴까요?", []) is True
    assert a._self_check_skip(req, "체결 토크는 12.5 N·m 입니다.", []) is False, "수치(소수 · 100 이상)가 있으면 검증한다"
    assert a._self_check_skip(req, "가" * a._SELF_CHECK_SHORT, []) is False, "긴 답은 검증한다"
    assert a._self_check_skip(req, "네.", [("search", "")]) is False, "조회한 턴은 검증한다"
    doc = a.ChatRequest(message="요약해", groups=[], history=[], documents=[{"name": "a.md", "text": "본문"}])
    assert a._self_check_skip(doc, "네.", []) is False, "문서를 붙인 턴은 검증한다"
    img = a.ChatRequest(message="이건?", groups=[], history=[], images=[{"name": "a.png", "mime": "image/png", "data": "x"}])
    assert a._self_check_skip(img, "네.", []) is False, "그림을 올린 턴은 검증한다"


def test_검토자_입력은_창_안에_들고_자른_근거에는_표지가_붙는다(monkeypatch):
    monkeypatch.setattr(a, "_model_context_tokens", lambda: 16_000)
    big = "측정값 행 " * 40_000
    req = a.ChatRequest(message="최대 응력은?", groups=[], history=[
        {"role": "user", "content": "오래된 물음 " * 5000}, {"role": "assistant", "content": "오래된 답"},
        {"role": "user", "content": "바로 앞 물음"}, {"role": "assistant", "content": "바로 앞 답"}])
    draft = "최대 응력은 321.5 MPa 입니다."
    msgs = a._self_check_messages(req, draft, [("query_stress", "part=A")], [big])
    body = msgs[1].content
    assert a._est_tokens_all(msgs[0].content + body) <= 16_000 - 2048, "고친 답을 쓸 몫을 남긴다"
    long_draft = "최대 응력은 321.5 MPa 입니다. " * 250
    out_room = int(a._est_tokens_all(long_draft) * 1.3) + 1024
    lone = a._self_check_messages(a.ChatRequest(message="최대 응력은?", groups=[], history=[]), long_draft,
                                  [("query_stress", "part=A")], [big])
    used = a._est_tokens_all(lone[0].content + lone[1].content)
    assert used + out_room <= 16_000, (used, out_room)
    assert used + out_room > 16_000 - 3_000, ("문서 · 앞 대화가 없으면 조회 결과가 남는 몫을 다 쓴다(55% 에서 멈추지 않는다)", used)
    assert "[이번 턴 조회 결과]\n1. query_stress(part=A)" in body and "자 중 앞" in body, "자른 근거에는 _cut_mark 표지"
    assert "바로 앞 물음" in body and "바로 앞 답" in body, "앞 대화는 최근 것부터 싣는다"
    assert "[코드 대조 —" in body and "`321.5`" in body, "조회 결과에 없는 수치를 코드가 먼저 짚어 준다"
    assert body.index("[답 초안]") < body.index("다음을 검토하라"), "지시는 초안 뒤 — 맨 끝에 둔다"
    nocall = a._self_check_messages(a.ChatRequest(message="설명해", groups=[], history=[]), "설명입니다." * 50, [], [])[1].content
    assert "도구를 부르지 않았다" in nocall and "[코드 대조 —" not in nocall


def test_몫_나누기는_드는_쪽을_다_주고_남는_몫을_넘치는_쪽에():
    assert a._sc_shares({"res": 100, "doc": 0, "hist": 50}, 1000) == {"res": 100, "doc": 0, "hist": 50}
    got = a._sc_shares({"res": 5000, "doc": 0, "hist": 0}, 1000)
    assert got == {"res": 1000, "doc": 0, "hist": 0}, "혼자 넘치면 전부"
    got = a._sc_shares({"res": 5000, "doc": 5000, "hist": 100}, 1000)
    assert got["hist"] == 100 and got["res"] + got["doc"] <= 900 and got["res"] > got["doc"], got
    assert a._sc_shares({"res": 5000, "doc": 300, "hist": 5000}, 1000) == {"res": 550, "doc": 250, "hist": 200}, "셋 다 넘치면 제 몫대로"
    assert a._sc_shares({"res": 5000, "doc": 270, "hist": 0}, 1000) == {"hist": 0, "doc": 270, "res": 730}, \
        "doc 는 제 몫(250)을 넘지만 앞 대화가 비운 몫을 나눈 뒤(312) 안이라 다 받는다"


def test_앞_대화는_최근_것부터_예산까지():
    hist = [("user", "옛 물음 " * 2000), ("assistant", "옛 답"), ("user", "새 물음"), ("assistant", "새 답")]
    got = a._sc_recent(hist, 300)
    assert got.endswith("사람: 새 물음\n어시스턴트: 새 답"), got[-80:]
    assert "옛 답" in got and "자 중 앞" in got, "넘치는 항목은 앞부분만(표지) 싣고 그보다 오래된 것은 뺀다"
    big_last = [("user", "물음"), ("assistant", "긴 답 " * 3000)]
    assert a._sc_recent(big_last, 150) == "", "가장 최근 항목이 넘치는데 남은 몫이 200 토큰 이하면 싣지 않는다"
    assert a._sc_recent(big_last, 400).startswith("어시스턴트: 긴 답") and "물음" not in a._sc_recent(big_last, 400)


# ── 감싸개(가짜 안쪽 턴 + 가짜 검토자) ───────────────────────────────────────────────────────────────────────────

class _Reviewer:
    """app.state.chat_llm 자리 — astream 이 정해 준 글을 조각으로 흘린다. 받은 메시지 · 인자를 남긴다."""

    def __init__(self, text: str = "", *, finish: str = "stop", boom: Exception | None = None, model: str = "qwen-x"):
        self.text, self.finish, self.boom, self.model = text, finish, boom, model
        self.calls: list = []

    async def astream(self, msgs, **kw):
        self.calls.append((msgs, kw))
        if self.boom is not None:
            raise self.boom
        for i in range(0, len(self.text), 7):
            yield AIMessageChunk(content=self.text[i:i + 7], response_metadata={"model_name": self.model})
        yield AIMessageChunk(content="", response_metadata={"finish_reason": self.finish, "model_name": self.model})


def _inner(draft: str, *, calls=(), outputs=(), result_type="text", error=False, meta=True, early=False):
    async def fake(app_, req, box=None):
        yield a._sse("status", {"step": "조회 중", "tool": "q"})
        yield a._sse("reasoning", {"delta": "생각 1"})
        half = len(draft) // 2
        yield a._sse("token", {"delta": draft[:half]})
        if error:
            yield a._sse("error", {"message": "LLM 실패"})
            return
        yield a._sse("reasoning", {"delta": "생각 2"})
        yield a._sse("token", {"delta": draft[half:]})
        if early:
            yield a._sse("result", {"type": "text", "content": draft})
            yield a._sse("done", {})
            return
        if box is not None:
            box.update(draft=draft, calls=list(calls), outputs=list(outputs))
        if meta:
            yield a._sse("meta", {"models": {"qwen-x": 2}, "chat_model": "qwen-x", "thinking": True, "effort": "xhigh",
                                  "fallback_to": None})
        yield a._sse("result", {"type": result_type, "content": draft})
        yield a._sse("done", {})
    return fake


def _run(monkeypatch, inner, reviewer, message="질문", **req_kw):
    monkeypatch.setattr(a, "_agent_stream_turn", inner)
    monkeypatch.setattr(a.app.state, "chat_llm", reviewer, raising=False)
    req = a.ChatRequest(message=message, groups=["portal-admin"], history=[], **req_kw)

    async def go():
        return [c async for c in a._self_checked_turn(a.app, req)]
    return _events(asyncio.run(go()))


def test_고친_답만_본문으로_나가고_초안은_생각_칸에_남는다(monkeypatch):
    draft = "A 부품의 최대 응력은 321.5 MPa 이고 안전율은 1.8 입니다. " * 3
    fixed = draft.replace("321.5", "312.5")
    rv = _Reviewer(f"[판정] 고침\n[고친 점]\n- 321.5 → 312.5(조회 결과 1)\n[답]\n{fixed}")
    evs = _run(monkeypatch, _inner(draft, calls=[("query_stress", "part=A")], outputs=["max 312.5 MPa, SF 1.8"]), rv)
    kinds = [e for e, _ in evs]
    first_tok = kinds.index("token")
    assert kinds.index("status") < first_tok and any(e == "status" and "검증하는 중" in d.get("step", "") for e, d in evs)
    assert all(k != "token" for k in kinds[:first_tok]), "본문은 검증 뒤에야 나간다"
    body = _text(evs, "token")
    assert body.startswith(fixed.strip() + "\n\n---"), body[:200]
    assert a._EVIDENCE_MARK in body and "`query_stress`" in body, "근거 블록은 고친 답 뒤에 다시 붙는다"
    assert "321.5" not in body, "고친 답에서 사라진 수치는 '출처 없음' 에도 남지 않는다"
    think = _text(evs, "reasoning")
    assert think.index("생각 1") < think.index(a._DRAFT_HEAD.strip()) < think.index("생각 2") < think.index(a._CHECK_HEAD.strip())
    assert think.count(a._DRAFT_HEAD.strip()) == 2, "도구 뒤 다시 쓰기 시작한 초안에도 머리를 단다"
    assert draft[:20] in think and "[판정] 고침" in think and "[고친 점]\n- 321.5 → 312.5" in think
    assert fixed.strip() not in think, "고친 답 전체는 '생각' 칸에 다시 흘리지 않는다(본문으로 나간다)"
    assert think.endswith("[답] — 고친 답은 본문에 냅니다")
    assert any(e == "status" and d.get("step", "").startswith("고친 답을 쓰는 중") for e, d in evs)
    assert kinds[-3:] == ["meta", "result", "done"]
    meta = evs[-3][1]
    assert meta["self_check"]["status"] == "fixed" and meta["self_check"]["fixes"] == ["321.5 → 312.5(조회 결과 1)"]
    assert meta["models"] == {"qwen-x": 3} and meta["chat_model"] == "qwen-x", "검토자 호출도 이 턴의 모델 호출이다"
    assert isinstance(meta["self_check"]["ms"], int)
    assert evs[-2][1] == {"type": "text", "content": body}
    msgs, kw = rv.calls[0]
    assert "max 312.5 MPa" in msgs[1].content and draft in msgs[1].content
    assert kw == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}, "검토자는 생각을 끈다(CHAT_SELF_CHECK_THINK=0)"


def test_고칠_것_없으면_초안이_본문이다(monkeypatch):
    for draft in ("결론: 체결 토크 12.5 N·m 이면 충분합니다.", "".join(f"{i}번 항목은 12.5 N·m 입니다.\n" for i in range(120))):
        evs = _run(monkeypatch, _inner(draft), _Reviewer("[판정] 고칠 것 없음\n[고친 점]\n- 없음\n[답]\n"))
        assert _text(evs, "token") == draft and evs[-3][1]["self_check"]["status"] == "ok", len(draft)


def test_답_표지가_조각_경계에_걸쳐도_찾는다(monkeypatch):
    draft = "토크는 12.5 N·m 입니다. " * 5
    fixed = draft.replace("12.5", "15.2")
    for step in (1, 2, 3, 5):
        rv = _Reviewer(f"[판정] 고침\n[고친 점]\n- 12.5 → 15.2\n[답]\n{fixed}")

        async def chop(msgs, _s=step, _rv=rv, **kw):     # 조각 크기를 바꿔 [답] 이 두 조각에 갈리게
            _rv.calls.append((msgs, kw))
            for i in range(0, len(_rv.text), _s):
                yield AIMessageChunk(content=_rv.text[i:i + _s], response_metadata={"model_name": "qwen-x"})
            yield AIMessageChunk(content="", response_metadata={"finish_reason": "stop", "model_name": "qwen-x"})
        rv.astream = chop
        evs = _run(monkeypatch, _inner(draft), rv)
        think = _text(evs, "reasoning").split(a._CHECK_HEAD.strip(), 1)[1]
        assert think.strip() == "[판정] 고침\n[고친 점]\n- 12.5 → 15.2\n[답] — 고친 답은 본문에 냅니다", (step, think)
        assert _text(evs, "token") == fixed.strip(), step


def test_검토자가_실패하면_초안이_나가고_까닭을_적는다(monkeypatch):
    draft = "토크는 12.5 N·m 입니다."
    evs = _run(monkeypatch, _inner(draft), _Reviewer(boom=RuntimeError("down")))
    assert _text(evs, "token") == draft
    sc = evs[-3][1]["self_check"]
    assert sc["status"] == "failed" and "RuntimeError" in sc["reason"]
    assert evs[-3][1]["models"] == {"qwen-x": 2}, "실패한 검토자 호출은 답한 모델로 세지 않는다"


def test_꼴_밖의_검토자_답은_초안을_내고_끝_글자까지_생각_칸에(monkeypatch):
    draft = "토크는 12.5 N·m 입니다."
    evs = _run(monkeypatch, _inner(draft), _Reviewer("검토했지만 형식을 모르겠습니다 — 끝"))
    assert _text(evs, "token") == draft and evs[-3][1]["self_check"]["status"] == "unreadable"
    assert _text(evs, "reasoning").endswith(a._CHECK_HEAD + "검토했지만 형식을 모르겠습니다 — 끝"), "쥐고 있던 끝 두 자도 흘린다"


def test_끊긴_검토자_답은_초안을_낸다(monkeypatch):
    draft = "토크는 12.5 N·m 입니다. " * 5
    evs = _run(monkeypatch, _inner(draft), _Reviewer("[판정] 고침\n[고친 점]\n- x\n[답]\n" + draft[:-3], finish="length"))
    assert _text(evs, "token") == draft and evs[-3][1]["self_check"]["status"] == "suspect"


def test_짧은_잡담은_검토자를_부르지_않는다(monkeypatch):
    rv = _Reviewer("[판정] 고침\n[답]\n바뀐 답")
    evs = _run(monkeypatch, _inner("안녕하세요! 무엇을 도와드릴까요?"), rv)
    assert rv.calls == [] and _text(evs, "token") == "안녕하세요! 무엇을 도와드릴까요?"
    assert evs[-3][1]["self_check"]["status"] == "skipped"
    assert not any(e == "status" and "검증" in d.get("step", "") for e, d in evs)


def test_오류_길은_초안을_본문으로_흘리고_오류를_낸다(monkeypatch):
    rv = _Reviewer("[판정] 고침\n[답]\n바뀐 답")
    evs = _run(monkeypatch, _inner("토크는 12.5 N·m", error=True), rv)
    draft = "토크는 12.5 N·m"
    assert rv.calls == [] and [e for e, _ in evs][-2:] == ["token", "error"]
    assert _text(evs, "token") == draft[:len(draft) // 2], "오류 전까지 쓴 초안이 본문에 남는다(종전 화면과 같다)"


def test_초안을_넘기지_않고_끝난_턴은_종전_끝_그대로다(monkeypatch):
    rv = _Reviewer("[판정] 고침\n[답]\n바뀐 답")
    for text, inner in (("조기 종료 12.5", _inner("조기 종료 12.5", early=True)),
                        ("핸드오프 12.5", _inner("핸드오프 12.5", result_type="handoff"))):
        evs = _run(monkeypatch, inner, rv)
        assert rv.calls == [] and _text(evs, "token") == text, "초안 글을 본문으로 그대로 흘린다"
        assert [e for e, _ in evs][-2:] == ["result", "done"]
        assert not any("self_check" in d for e, d in evs if e == "meta")


def test_meta_가_없던_턴도_self_check_를_싣는다(monkeypatch):
    evs = _run(monkeypatch, _inner("토크는 12.5 N·m 입니다.", meta=False), _Reviewer("[판정] 고칠 것 없음\n[답]\n"))
    meta = [d for e, d in evs if e == "meta"]
    assert meta == [{"models": {"qwen-x": 1}, "self_check": meta[0]["self_check"]}] and meta[0]["self_check"]["status"] == "ok"


class _ToolAgent:
    """도구 한 번 → 답(수치 하나는 조회 결과와 어긋난다). test_chat_stall._FakeAgent 와 같은 꼴의 사건."""

    async def astream_events(self, inputs, version=None, config=None):
        yield {"event": "on_tool_start", "name": "query_stress", "data": {"input": {"part": "A"}}}
        yield {"event": "on_tool_end", "name": "query_stress", "data": {"output": "A 부품 최대 응력 312.5 MPa"}}
        yield {"event": "on_chat_model_stream",
               "data": {"chunk": types.SimpleNamespace(content="A 부품의 최대 응력은 321.5 MPa 입니다.")}}

    async def ainvoke(self, payload, config=None):
        raise AssertionError("답이 있는 턴은 구제 호출을 하지 않는다")


def test_도구를_부른_실제_턴_근거_블록은_고친_답에만_한_번(monkeypatch):
    async def built(*_a, **_k):
        return _ToolAgent()

    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")
    monkeypatch.setattr(a, "_agent_for", built)
    rv = _Reviewer("[판정] 고침\n[고친 점]\n- 321.5 → 312.5(조회 결과 1)\n[답]\nA 부품의 최대 응력은 312.5 MPa 입니다.")
    monkeypatch.setattr(a.app.state, "chat_llm", rv, raising=False)

    async def go():
        req = a.ChatRequest(message="A 부품 최대 응력은?", groups=["portal-admin"], history=[])
        return [c async for c in a._agent_stream(a.app, req)]
    evs = _events(asyncio.run(go()))
    result = [d for e, d in evs if e == "result"][-1]["content"]
    assert result.startswith("A 부품의 최대 응력은 312.5 MPa 입니다.") and result.count(a._EVIDENCE_MARK) == 1, result
    assert "`query_stress`" in result and "⚠" not in result, "고친 답의 수치는 조회 결과에 있다 — 출처 없음 경고가 없다"
    assert a._EVIDENCE_MARK not in _text(evs, "reasoning"), "안쪽 턴은 근거 블록을 붙이지 않는다(감싸개가 고친 답으로 센다)"
    human = rv.calls[0][0][1].content
    assert "A 부품 최대 응력 312.5 MPa" in human and "`321.5`" in human, "조회 원문과 코드 대조가 검토자에게 간다"
    assert a._EVIDENCE_MARK not in human.split("[답 초안]", 1)[1], "초안에는 근거 블록이 없다"


# ── 실제 경로(_agent_stream · 기동 절차가 만든 챗 LLM · 가짜 LLM 서버) ─────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 9009
    return port


@pytest.fixture
def fake_llm(tmp_path, monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    port, log = _free_port(), tmp_path / "fake.log"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    with open(log, "w") as out:
        p = subprocess.Popen([PY, str(ROOT / "tests" / "fakes" / "llm_server.py"), "--port", str(port), "--ctx", "262144",
                              "--model", "qwen3.8-flash-next", "--script", "oracle"],
                             cwd=str(tmp_path), env=env, stdout=out, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    t0 = time.monotonic()
    with httpx.Client(timeout=2, trust_env=False) as c:
        while True:
            try:
                if c.get(f"{base}/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if p.poll() is not None or time.monotonic() - t0 > 60:
                pytest.fail(f"가짜 LLM 서버가 뜨지 않았다 — {log.read_text()[-1500:]}")
            time.sleep(0.1)
    yield base
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=10)


@tool
def noop(q: str) -> str:
    """아무것도 하지 않는다."""
    return q


def _turn(monkeypatch, base: str, message: str) -> list:
    """test_chat_llm 과 같은 꾸밈 — 기동 절차가 만든 챗 전용 LLM(생각 켬)으로 에이전트를 짓고 _agent_stream 한 턴."""
    import test_chat_llm as tcl  # noqa: PLC0415 — 같은 꾸밈을 다시 쓰지 않는다
    monkeypatch.setattr(a, "VLLM_BASE_URL", f"{base}/v1")
    monkeypatch.setattr(a, "VLLM_MODEL", "qwen3.8-flash-next")
    st = tcl._boot(monkeypatch, CHAT_LLM_BASE_URL=f"{base}/v1", CHAT_LLM_MODEL="qwen3.8-flash-next", CHAT_LLM_MAX_RETRIES="0")

    class _StubClient:
        def __init__(self, _connections):
            pass

        async def get_tools(self):
            return [noop]

    monkeypatch.setattr(a, "MultiServerMCPClient", _StubClient)
    state = types.SimpleNamespace(
        llm=st.llm, chat_llm=st.chat_llm, connections={"gateway": {"url": "http://gw/mcp", "headers": {"Authorization": "Bearer x"}}},
        agent_cache={}, tool_degraded={}, tool_load_error={}, tool_snapshot={})
    agent = asyncio.run(a._agent_for(types.SimpleNamespace(state=state), ["admin"]))

    async def built(*_a, **_k):
        return agent

    monkeypatch.setattr(a, "_agent_for", built)
    monkeypatch.setattr(a, "_CHAT_LOAD", rg.ChatLoad())
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(a.app.state, "llm_nostream", st.llm_nostream, raising=False)
    monkeypatch.setattr(a.app.state, "chat_llm_cfg", None, raising=False)
    monkeypatch.setattr(a.app.state, "chat_llm", st.chat_llm, raising=False)
    monkeypatch.setattr(a.app.state, "chat_llm_info", st.chat_llm_info, raising=False)
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")

    async def go():
        req = a.ChatRequest(message=message, groups=["portal-admin"], history=[])
        return [c async for c in a._agent_stream(a.app, req)]
    return _events(asyncio.run(go()))


def _script(base: str, texts: list) -> None:
    with httpx.Client(timeout=5, trust_env=False) as c:
        c.post(f"{base}/_script", json={"script": [{"behavior": "oracle", "text": t} for t in texts]}).raise_for_status()


def _stats(base: str) -> dict:
    with httpx.Client(timeout=5, trust_env=False) as c:
        return c.get(f"{base}/_stats").json()


def test_실제_경로_챗_턴_한_번에_LLM_두_번_고친_답이_본문(monkeypatch, fake_llm):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    draft = "M3 나사의 권장 체결 토크는 12.5 N·m 입니다."
    fixed = "M3 나사의 권장 체결 토크는 12.5 N·m (근거 없음 — 추정) 입니다."
    _script(fake_llm, [draft, f"[판정] 고침\n[고친 점]\n- 12.5 는 근거가 없어 추정으로 표시\n[답]\n{fixed}"])
    evs = _turn(monkeypatch, fake_llm, "M3 나사 체결 토크는?")
    st = _stats(fake_llm)
    assert st["calls"] == 2, st
    assert "[답 초안]" in st["humans"][1] and draft in st["humans"][1] and "M3 나사 체결 토크는?" in st["humans"][1]
    assert _text(evs, "token") == fixed
    think = _text(evs, "reasoning")
    assert draft in think and think.startswith("생각: "), "챗 호출은 생각을 켠 채(xhigh) — 초안은 생각 칸에"
    assert a._CHECK_HEAD + "[판정]" in think, "검토자 호출은 생각을 껐다(생각 글 없이 곧바로 판정)"
    meta = [d for e, d in evs if e == "meta"][-1]
    assert meta["self_check"]["status"] == "fixed" and meta["models"] == {"qwen3.8-flash-next": 2}
    assert [e for e, _ in evs][-1] == "done"


def test_끄면_종전_경로_LLM_한_번(monkeypatch, fake_llm):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    monkeypatch.setenv("CHAT_SELF_CHECK", "0")
    draft = "M3 나사의 권장 체결 토크는 12.5 N·m 입니다."
    _script(fake_llm, [draft, "[판정] 고침\n[답]\n바뀐 답"])
    evs = _turn(monkeypatch, fake_llm, "M3 나사 체결 토크는?")
    assert _stats(fake_llm)["calls"] == 1 and _text(evs, "token") == draft
    assert not any("self_check" in d for e, d in evs if e == "meta")

# 띵킹 세션 맥락 — 앞 대화 · 조회 근거를 좌석 본심에만 싣고, 소집 · 예심 검색어에는 대화 화두만 더하는지(HWAXPortal docs/chat-depth-context)
"""왜 — 띵킹은 앞 대화를 세기만 하고 버려, 챗에서 플랫폼을 파헤친 뒤 띵킹을 켜도 전문가가 질문 한 줄만 보고 처음부터 검색했다.
사용자 결정(10-18): "thinking 을 켜면 전문가 자문 모드이고 각자 맥락을 다 가지고 와야 한다".

  · 세션 블록 — [c:N] 사람 · [a:N] 챗 · [e:N] 조회 근거, 최근부터 예산 안에, 뺀 수를 말한다.
  · 본심 프롬프트에만 — 소집(recommend_agents) · 예심(agent_search) 검색어에는 화두 한 문장만 더한다(맥락을 섞지 않는다).
  · THINK_WITH_HISTORY=0 이면 종전(질문만 · 화두 안 뽑음).
  · THINK_LLM=chat 이면 챗 전용 LLM 과 그 창(작은 쪽 − 출력 몫)으로 돌고, 끝나면 창을 되돌린다.

  실행:  .venv/bin/python -m pytest tests/test_thinking_session.py -q
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import external_llm  # noqa: E402
import thinking as t  # noqa: E402

TOPIC = "힌지 두께를 0.8mm 로 낮추면 낙하 크랙을 막을 수 있는가"
HISTORY = [
    {"role": "user", "content": "힌지 부품 두께가 얼마야?"},
    {"role": "assistant", "content": "현재 힌지 두께는 1.0mm 입니다.",
     "evidence": [{"tool": "stepforge_part_info", "args": '{"part": "HINGE-01"}', "result": "thickness=1.0mm · material=SUS301"},
                  {"tool": "materialtwin_lookup", "result": "SUS301 yield=520MPa"}]},
]


@pytest.fixture(autouse=True)
def _wide_window():
    saved = dict(app._ctx_cache)
    app._ctx_cache["n"] = 1_048_576
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)


def _run(monkeypatch, history, *, with_history=True, state=None):
    rec_q, search_q, prompts, topic_calls = [], [], [], []

    class _Rec:
        async def ainvoke(self, args):
            rec_q.append(args["q"])
            return json.dumps({"agents": [{"agent_type": "mech-hinge", "name": "힌지", "desc_match": 0.6}]})

    class _Search:
        async def ainvoke(self, args):
            search_q.append(args["q"])
            return json.dumps({"hits": [{"title": "힌지 카드", "record_id": "R-1", "snippet": "두께 0.8mm 시험"}]})

    class _Sess:
        async def ainvoke(self, _args):
            return json.dumps({"data": {"agent_type": "mech-hinge", "description": "힌지 설계 전문가"}})

    async def _tools(*_a, **_k):
        return {"recommend_agents": _Rec(), "agent_search": _Search(), "get_agent_session": _Sess()}

    async def _topic(h, q):
        topic_calls.append(q)
        return {"topic": TOPIC}

    class _LLM:
        async def ainvoke(self, msgs):
            prompts.append(msgs[-1][1])
            return SimpleNamespace(content=json.dumps({"verdict": "answer", "scope": "힌지", "answer": "0.8mm 면 [e:1] 기준 ...",
                                                       "basis": ["[e:1]"]}), response_metadata={})

    monkeypatch.setattr(t, "_tools_by_name", _tools)
    monkeypatch.setattr(t, "WITH_HISTORY", with_history)
    monkeypatch.setattr(app, "_derive_topic", _topic)
    st = state or SimpleNamespace(llm=_LLM())

    after: list = []

    async def go():
        got = [c async for c in t.run_thinking(SimpleNamespace(state=st), "그럼 이건 어때?", [], history=history)]
        after.append(external_llm.ctx_override.get())    # 같은 태스크에서 — asyncio.run 밖에서는 덮어쓴 값이 안 보인다
        return got

    chunks = asyncio.run(go())
    _run.ctx_after = after[0]
    return b"".join(chunks).decode(), rec_q, search_q, prompts, topic_calls


def test_세션_블록은_사람_챗_근거를_표지와_함께_최근부터_싣는다():
    text, st = t._session_block(HISTORY, 100_000)
    assert text.startswith("[세션 맥락") and "[c:1] 사람 — 힌지 부품 두께가 얼마야?" in text
    assert "[a:1] 챗 — 현재 힌지 두께는 1.0mm 입니다." in text
    assert '[e:1] [stepforge_part_info({"part": "HINGE-01"})] thickness=1.0mm · material=SUS301' in text
    assert "[e:2] [materialtwin_lookup] SUS301 yield=520MPa" in text
    assert st == {"conv": 2, "conv_gone": 0, "ev": 2, "ev_gone": 0, "trimmed": 0}
    # 예산이 작으면 오래된 근거부터 빠지고 그 수를 말한다
    long_hist = HISTORY + [{"role": "user", "content": "또?"},
                           {"role": "assistant", "content": "더 봤다",
                            "evidence": [{"tool": "big_tool", "result": "B" * 3000}]}]
    text, st = t._session_block(long_hist, 2600)
    assert st["ev_gone"] >= 1 and "예산 밖이라 뺐다" in text, (st, text[-300:])


def test_세션_맥락은_본심에만_싣고_검색어에는_화두만_더한다(monkeypatch):
    out, rec_q, search_q, prompts, topic_calls = _run(monkeypatch, HISTORY)
    assert topic_calls == ["그럼 이건 어때?"], "앞 대화가 있으면 화두를 뽑는다"
    assert rec_q and all(q == f"{TOPIC}\n그럼 이건 어때?" for q in rec_q), rec_q
    assert search_q and all(q == f"{TOPIC}\n그럼 이건 어때?" for q in search_q), search_q
    assert all("세션 맥락" not in q and "thickness=1.0mm" not in q for q in rec_q + search_q), "검색어에 맥락을 섞지 않는다"
    p = prompts[0]
    assert p.startswith("[세션 맥락") and "thickness=1.0mm" in p and "[c:1] 사람 — 힌지 부품 두께가 얼마야?" in p
    assert "[질문]\n그럼 이건 어때?" in p and f"[대화의 화두 — 앞 대화에서 뽑은 물음]\n{TOPIC}" in p
    assert "[e:N] 으로 인용" in p, "세션 근거를 인용하라는 지시"
    assert "세션 맥락 — 앞선 대화 2건 · 조회 근거 2건을 좌석마다 실었다" in out
    assert "_세션 맥락 — 앞선 대화 2건 · 조회 근거 2건을 좌석마다 실었다_" in out, "종합(대화 저장본)에도 남는다"


def test_끄면_종전대로_질문만이고_화두도_뽑지_않는다(monkeypatch):
    out, rec_q, search_q, prompts, topic_calls = _run(monkeypatch, HISTORY, with_history=False)
    assert topic_calls == [] and rec_q == ["그럼 이건 어때?"] and set(search_q) == {"그럼 이건 어때?"}
    assert "세션 맥락" not in prompts[0] and "thickness" not in prompts[0]
    assert "앞선 대화 2건은 싣지 않는다(THINK_WITH_HISTORY=0)" in out


def test_앞_대화가_없으면_화두를_뽑지_않는다(monkeypatch):
    out, rec_q, _s, prompts, topic_calls = _run(monkeypatch, [])
    assert topic_calls == [] and rec_q == ["그럼 이건 어때?"] and not prompts[0].startswith("[세션 맥락")


def test_THINK_LLM_chat_은_챗_전용_LLM_과_그_창으로_돌고_끝나면_되돌린다(monkeypatch):
    seen_ctx = []
    used = []

    class _ChatLLM:
        async def ainvoke(self, msgs):
            used.append("chat")
            seen_ctx.append(app._model_context_tokens())
            return SimpleNamespace(content=json.dumps({"verdict": "pass", "scope": "", "refer": []}), response_metadata={})

    chat = _ChatLLM()
    monkeypatch.setattr(t, "THINK_LLM", "chat")
    monkeypatch.setattr(app, "_chat_context_tokens", lambda: 262_144)
    monkeypatch.setattr(app, "_chat_llm_of", lambda _app: chat)
    st = SimpleNamespace(llm=object(), chat_llm=chat,
                         chat_llm_cfg={"url": "u", "model": "q", "key": "k", "output_reserve": 65_536})
    _run(monkeypatch, [], state=st)
    assert used == ["chat"] and seen_ctx == [262_144 - 65_536], seen_ctx
    assert not _run.ctx_after, "끝나면 창 덮어쓰기를 되돌린다(같은 태스크에서 본다)"

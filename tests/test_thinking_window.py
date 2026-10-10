# 띵킹 모드의 좌석 지식카드 예산을 모델 창에서 유도하고, 뺀 수와 싣지 않은 대화를 알린다 — 좁은 창은 종전 2,500자
#
# 사용자 지침(2026-10-10) — 운영 창 1,048,576 토큰. 띵킹 좌석은 지식카드 발췌를 2,500자에서 끊고(top_k 10 의 뒤 몇 건이 빠진다)
# 몇 건을 뺐는지 어디에도 남기지 않았다. 앞선 대화(history)는 받기만 하고 좌석에 싣지 않는데 그 사실을 말하지 않았다.
#
#   실행:  .venv/bin/python -m pytest tests/test_thinking_window.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402
import thinking as t  # noqa: E402

_BIG = 1048576


@pytest.fixture(autouse=True)
def _restore_ctx():
    saved = dict(app._ctx_cache)
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


def _win(tokens):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()


class _Tools(dict):
    def __init__(self, payload):
        super().__init__()

        class _T:
            async def ainvoke(_self, _args):  # noqa: ANN001
                return payload
        self["agent_search"] = _T()


def _screen(tokens):
    _win(tokens)
    body = json.dumps({"hits": [{"title": f"t{i}", "snippet": "가" * 500 + f" [카드끝{i}]"} for i in range(10)]},
                      ensure_ascii=False)
    seat = {"key": "dom-x", "name": "x", "domain": "dom", "score": 1.0,
            "desc_match": 0.5, "sections": 0, "records": 0, "why": ""}
    return asyncio.run(t._screen_one(_Tools(body), seat, "질의"))


@pytest.mark.parametrize("tokens", (16384, 128000, 200000))
def test_좁은_창의_띵킹_지식카드는_종전대로_2500자다(tokens):
    """예산은 종전 2,500자 그대로다. 같은 몫(바닥 300자)으로 514자 줄 10장 중 7장을 싣는다 — 종전엔 앞 4장(GA-3)."""
    r = _screen(tokens)
    assert len(r["knowledge"]) <= 2500 < len(r["knowledge"]) + 400, len(r["knowledge"])
    assert [i for i in range(10) if f"[t{i}]" in r["knowledge"]] == list(range(7))


def test_넓은_창의_띵킹_지식카드는_2500자에서_끊지_않는다():
    r = _screen(_BIG)
    assert all(f"[카드끝{i}]" in r["knowledge"] for i in range(10)), r["knowledge"][-200:]


def test_띵킹_지식카드를_예산에서_빼면_예심_사유에_적는다():
    r = _screen(128000)
    assert "10건 중 7건 실음(7건은 앞부분만 · 예산 2,500자)" in r["reason"], r["reason"]


def test_띵킹이_앞선_대화를_싣지_않으면_그렇게_말한다(monkeypatch):
    """history 를 받기만 하고 좌석에 싣지 않았다 — 사용자는 앞 대화가 반영된 줄 안다."""
    async def _no_tools(*_a, **_k):
        return {}

    monkeypatch.setattr(t, "_tools_by_name", _no_tools)

    async def go():
        return [c async for c in t.run_thinking(SimpleNamespace(state=SimpleNamespace(llm=object())), "질문", [],
                                                history=[{"role": "user", "content": "앞 대화"}] * 3)]

    out = b"".join(asyncio.run(go())).decode()
    assert "앞선 대화 3건" in out, out[:400]

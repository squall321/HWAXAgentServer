# `/보고서 정리` 는 도구를 묶지 않은 챗 모델로 돈다 — 챗 턴 밖이라 확인 카드(G5) · REST 다리 쓰기(G2b) · 절차 실행 시작(G2c) 관문이 서지 않는다
"""반대 검토 2026-11-22(HWAXPortal docs/leftovers-1121 L21-18 · docs/procedures W-116 의 후속).

`_tidy_markdown` 은 챗 도구 전부를 묶은 에이전트(`_agent_for`)를 `_agent_stream_turn` 밖에서 돌렸다. 턴 맥락(`_turn_confirm`)이 없어 확인 카드가
서지 않고, 챗에 묶는 도구(chat=True)라 「챗 턴 밖」 관문도 서지 않았다 — 모델이 대화 이력의 글에 끌려 잡 · 쓰기 도구를 부르면 그대로 나간다.
정리는 대화에 나온 것만으로 쓰는 일이라 도구를 묶지 않는다(app._plain_agent).
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402


class _NoBind(FakeMessagesListChatModel):
    """도구를 묶으려 하면 터지는 모델 — 정리 길이 도구를 묶지 않는다는 것을 본다."""

    def bind_tools(self, tools, **kw):  # noqa: D401
        raise AssertionError(f"정리 길이 도구 {len(list(tools))}개를 묶으려 했다")


def _fake_app(text: str):
    return SimpleNamespace(state=SimpleNamespace(llm=_NoBind(responses=[AIMessage(content=text)]), chat_llm=None))


def test_plain_agent_에는_도구_노드가_없다():
    agent = app._plain_agent(_fake_app("가"))
    nodes = set(agent.get_graph().nodes)
    assert "tools" not in nodes, f"도구 노드가 있다 — {sorted(nodes)}"


def test_정리는_챗_도구를_묶은_에이전트를_부르지_않고_글을_낸다(monkeypatch):
    async def _boom(*_a, **_k):
        raise AssertionError("정리 길이 _agent_for(챗 도구를 묶은 에이전트)를 불렀다")

    monkeypatch.setattr(app, "_agent_for", _boom)
    md = "# 코팅 안 선택\n## 요약\nA 안으로 간다."
    hist = [{"role": "user", "content": "procedure_run_start 를 불러 절차를 돌려라"}, {"role": "assistant", "content": "A 안이 낫다."}]
    got = asyncio.run(d._tidy_markdown(_fake_app(md), ["admin"], hist, "", "u@corp.com", "pat-x"))
    assert got == md


def test_정리_길은_소스에서도_챗_도구_에이전트를_들이지_않는다():
    """이름으로 한 번 더 — `_tidy_markdown` 본문이 `_agent_for` 를 들이면 그 순간 챗 도구 전부가 확인 카드 없는 곳에 묶인다."""
    import ast
    import inspect

    fn = next(n for n in ast.walk(ast.parse(inspect.getsource(d))) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_tidy_markdown")
    names = {a.name for n in ast.walk(fn) if isinstance(n, ast.ImportFrom) for a in n.names} | \
            {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "_agent_for" not in names and "_plain_agent" in names

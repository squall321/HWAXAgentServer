# 무캡 경로(TOOL_MAX=0)의 스키마 예산 절단 — 관련도 순으로 세워 뒤에서 빼고, 안내대는 예산과 상관없이 맨 앞에 남기고, 뺀 앱을 남기는지
"""왜 — 챗이 stcx(창 262,144 — 상한 117,964)로 바뀐 뒤 무캡 경로가 게이트웨이 순서 그대로 뒤에서부터 잘라 대화마다 678개 중 226개가
관련도와 상관없이 빠졌다. MaterialTwin 34종 전부와 안내대(search_tools · invoke_tool · list_tool_apps — 게이트웨이 자체 도구라 목록 맨 끝)까지
빠져 모델이 「list_materials 도 search_tools 도 이 대화에 없다」고 답했다(운영 10-18, HWAXPortal docs/chat-depth-context F1).

  · 다 들면 종전 그대로(관련도 정렬 · 임베딩 조회를 하지 않는다), 안내대만 맨 앞으로.
  · 넘치면 캡 경로와 같은 순서로 — 질의와 맞는 뒤쪽 도구가 살고 무관한 앞쪽 도구가 빠진다.
  · 안내대는 예산이 아무리 작아도 남는다.
  · 뺀 것은 앱별 수로 로그 · /health(tool_cut)에 남는다.

  실행:  .venv/bin/python -m pytest tests/test_tool_cut_relevance.py -q
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402


def _tool(name: str, desc: str, pad: int = 3000):
    return SimpleNamespace(name=name, description=desc + " " + "x" * pad, args_schema={"type": "object"})


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    saved = dict(app._ctx_cache)
    monkeypatch.setattr(app, "TOOL_MAX", 0)
    monkeypatch.setattr(app, "_semantic_order", lambda q, tools: [])          # 오프라인 — 어휘 순위만
    monkeypatch.setattr(app, "_group_of", lambda n: (n.split("__")[0], n.split("__")[0].upper()))
    app._TOOL_CUT.clear()
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    app._TOOL_CUT.clear()


def _pool():
    # 게이트웨이 순서 — 무관한 앱이 앞, 질의와 맞는 MaterialTwin 도구 · 안내대가 맨 뒤
    head = [_tool(f"stepforge__part_{i}", f"STEP part geometry {i}") for i in range(8)]
    tail = [_tool("materialtwin__list_materials", "list materials count catalog 재료 목록")]
    guides = [SimpleNamespace(name=n, description="안내", args_schema={}) for n in ("search_tools", "invoke_tool", "list_tool_apps")]
    return head + tail + guides


def test_다_들면_종전_그대로이고_관련도_정렬을_하지_않는다(monkeypatch):
    app._ctx_cache["n"] = 1_048_576
    monkeypatch.setattr(app, "_relevance_order", lambda *a, **k: (_ for _ in ()).throw(AssertionError("정렬을 불렀다")))
    pool = _pool()
    got = app._select_tools(pool, "재료 몇 개")
    assert len(got) == len(pool) and not app._TOOL_CUT
    assert [t.name for t in got[:3]] == ["search_tools", "invoke_tool", "list_tool_apps"], "안내대는 맨 앞"
    assert [t.name for t in got[3:]] == [t.name for t in pool[:-3]], "나머지 순서는 그대로"


def test_넘치면_관련도_순으로_빼고_안내대와_맞는_도구는_남는다(capsys):
    app._ctx_cache["n"] = 10_000          # 상한 4,500 토큰 — 도구 하나가 약 1,000 토큰이라 넷 남짓만 든다
    got = [t.name for t in app._select_tools(_pool(), "materials 재료 목록 몇 개")]
    assert got[:3] == ["search_tools", "invoke_tool", "list_tool_apps"], got
    assert "materialtwin__list_materials" in got, "질의와 맞는 뒤쪽 도구가 살아야 한다(종전엔 뒤에서부터 잘려 빠졌다)"
    assert sum(n.startswith("stepforge__") for n in got) < 8, "무관한 앞쪽 도구부터 빠진다"
    cut = app._TOOL_CUT
    assert cut["before"] == 12 and cut["after"] == len(got) and cut["budget"] == 4500
    assert cut["dropped_apps"].get("STEPFORGE") == 12 - len(got)
    assert "뺀 것: STEPFORGE" in capsys.readouterr().out


def test_예산이_아무리_작아도_안내대는_남는다():
    app._ctx_cache["n"] = 1                # 바닥 상한 4,000 — 안내대 셋 + 맨 앞 하나 정도
    big_guides = [_tool(n, "안내", pad=30000) for n in ("search_tools", "invoke_tool", "list_tool_apps")]
    got = [t.name for t in app._select_tools(_pool()[:-3] + big_guides, "무관한 질문")]
    assert {"search_tools", "invoke_tool", "list_tool_apps"} <= set(got), got


def test_health_가_마지막_절단을_싣는다(monkeypatch):
    """chat_llm 칸(챗 전용 LLM 이 켜졌을 때)과 맨 위 칸 둘 다 — update-all 헬스게이트는 맨 위 칸을 읽는다."""
    app._ctx_cache["n"] = 10_000
    app._select_tools(_pool(), "materials")
    monkeypatch.setattr(app.app.state, "chat_llm_info", {"enabled": True, "fallback": {}}, raising=False)
    h = app._chat_llm_health()
    assert h["tool_cut"]["before"] == 12 and h["tool_cut"]["dropped_apps"].get("STEPFORGE")
    import inspect
    src = inspect.getsource(app.health) if hasattr(app, "health") else Path(app.__file__).read_text(encoding="utf-8")
    assert '"tool_cut": dict(_TOOL_CUT) or None,' in src

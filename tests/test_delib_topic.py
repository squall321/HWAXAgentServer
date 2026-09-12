# 챗 대화 → 심의 화두. 실패가 브리프를 막지 않는지가 핵심이다(되묻기와 같은 규율)
"""왜 첫 발화를 그냥 쓰면 안 되나 — 대화는 움직인다.

처음엔 "굽힘 수명이 왜 부족하냐"(원인 규명)로 시작해도, 오가는 사이 쟁점은 "구리를 12um 로
낮출 것인가"(안 선택)로 옮겨 간다. 첫 발화를 화두로 넣으면 심의가 **이미 지나온 자리**를
다시 판다.

여기서 보는 것은 두 가지다 — ① 실패해도 fallback 으로 열린다 ② 결론이 아니라 물음을 쓴다.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402


@pytest.fixture(autouse=True)
def _llm_present():
    """app.state.llm 을 채워 둔다.

    없으면 `_llm_text(app.state.llm, …)` 의 **인자 평가에서** AttributeError 가 나고, 그게
    except 에 잡혀 전부 llm_failed 가 된다 — 가짜 LLM 이 불리지도 않는다. 실제 운영에서
    그 예외가 fallback 으로 떨어지는 건 맞는 동작이라 코드는 그대로 둔다."""
    # `import app` 은 **모듈**이고 FastAPI 인스턴스는 app.app 이다(모듈 안에서는 app 이
    # 인스턴스를 가리킨다). 여기서 헷갈리면 픽스처가 조용히 아무것도 안 한다.
    app.app.state.llm = object()
    yield


def _run(**kw):
    return asyncio.run(app.deliberate_topic(app.TopicRequest(**kw)))


def test_이력이_없으면_fallback_을_그대로_준다():
    """LLM 을 부르지도 않는다 — 부를 재료가 없다."""
    r = _run(history=[], fallback="원래 질문")
    assert r["topic"] == "원래 질문" and r["error"] == "no_history"


def test_LLM_이_죽어도_브리프는_열린다(monkeypatch):
    """화두 제안 실패가 심의 진입을 막으면 안 된다 — 제안은 초안일 뿐이다."""
    async def boom(*a, **k):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(app, "_llm_text", boom)
    r = _run(history=[{"role": "user", "content": "무언가 물었다"}], fallback="첫 발화")
    assert r["topic"] == "첫 발화" and r["error"] == "llm_failed"


def test_빈_응답도_fallback_이다(monkeypatch):
    async def empty(*a, **k):
        return '{"topic": "   "}'

    monkeypatch.setattr(app, "_llm_text", empty)
    r = _run(history=[{"role": "user", "content": "q"}], fallback="첫 발화")
    assert r["topic"] == "첫 발화" and r["error"] == "empty"


def test_대안은_두_개까지_길이도_자른다(monkeypatch):
    async def many(*a, **k):
        return ('{"topic": "' + "가" * 900 + '", "why": "' + "나" * 500
                + '", "options": ["a", "b", "c", "d"]}')

    monkeypatch.setattr(app, "_llm_text", many)
    r = _run(history=[{"role": "user", "content": "q"}], fallback="fb")
    assert len(r["topic"]) == 600 and len(r["why"]) == 300 and len(r["options"]) == 2


def test_프롬프트가_결론_금지와_뒷부분_우선을_요구한다(monkeypatch):
    """P1 — 브리프에 결론이 들어가면 심의가 그것을 전제로 깔고 시작한다."""
    seen = {}

    async def cap(_llm, _sys, user, *a, **k):
        seen["p"] = user
        return '{"topic": "그래서 무엇을 할 것인가"}'

    monkeypatch.setattr(app, "_llm_text", cap)
    _run(history=[{"role": "user", "content": "어떤 대화"}], fallback="fb")
    assert "답이 아니라" in seen["p"] and "물음" in seen["p"]
    assert "뒷부분" in seen["p"], "뒤쪽 쟁점을 우선하라는 지시가 빠졌다"
    assert "지어내지 마라" in seen["p"]


def test_이력이_길어도_예산_안에서_자른다(monkeypatch):
    seen = {}

    async def cap(_llm, _sys, user, *a, **k):
        seen["p"] = user
        return '{"topic": "t"}'

    monkeypatch.setattr(app, "_llm_text", cap)
    _run(history=[{"role": "user", "content": "가" * 5000} for _ in range(60)], fallback="fb")
    assert len(seen["p"]) < 30000, f"프롬프트가 {len(seen['p']):,}자 — 예산이 안 걸렸다"


# ── /심의 직접 입력 경로 ──────────────────────────────────────────────────────────
# `/심의 <질문>` 은 사람이 대놓고 적은 의도라 그대로 쓴다. 짧거나 비었을 때만 대화에서 뽑는다.
class _Req:
    def __init__(self, message, history=None):
        self.message = message
        self.history = history or []
        self.groups, self.delib_opts, self.user_email, self.user_pat = [], None, "", ""


async def _collect(req):
    return [c async for c in app._delib_with_topic(req)]


def test_적어_준_화두는_그대로_쓴다(monkeypatch):
    seen = {}

    async def fake_delib(_app, q, *a, **k):
        seen["q"] = q
        if False:
            yield b""

    monkeypatch.setattr(app, "run_deliberation", fake_delib)
    asyncio.run(_collect(_Req("/심의 구리 두께를 12um 로 낮출 것인가",
                              [{"role": "user", "content": "다른 얘기"}])))
    assert seen["q"] == "구리 두께를 12um 로 낮출 것인가", "사람이 적은 화두를 덮어썼다"


def test_짧으면_대화에서_뽑고_무엇을_썼는지_밝힌다(monkeypatch):
    seen = {}

    async def fake_delib(_app, q, *a, **k):
        seen["q"] = q
        if False:
            yield b""

    async def fake_topic(*a, **k):
        return {"topic": "저온에서도 12um 로 갈 것인가", "why": "", "options": []}

    monkeypatch.setattr(app, "run_deliberation", fake_delib)
    monkeypatch.setattr(app, "_derive_topic", fake_topic)
    out = asyncio.run(_collect(_Req("/심의", [{"role": "user", "content": "긴 대화"}])))
    assert seen["q"] == "저온에서도 12um 로 갈 것인가"
    # 조용히 바꾸면 안 된다 — 무엇을 화두로 삼았는지 보여야 사람이 틀린 걸 잡는다.
    assert any(b"\xed\x99\x94\xeb\x91\x90\xeb\xa5\xbc" in c for c in out), "화두를 밝히지 않았다"


def test_뽑지도_못하고_적힌_것도_없으면_되묻는다(monkeypatch):
    async def fake_topic(*a, **k):
        return {"topic": "", "why": "", "options": [], "error": "llm_failed"}

    async def boom(*a, **k):
        raise AssertionError("빈 화두로 심의를 돌리면 안 된다")
        yield b""

    monkeypatch.setattr(app, "_derive_topic", fake_topic)
    monkeypatch.setattr(app, "run_deliberation", boom)
    out = b"".join(asyncio.run(_collect(_Req("/심의", [{"role": "user", "content": "x"}]))))
    assert b"done" in out and "한 줄로 적어".encode() in out


def test_이력이_없으면_짧아도_그냥_간다(monkeypatch):
    """첫 발화로 /심의 를 치는 경우 — 뽑을 대화가 없다. 막지 않는다."""
    seen = {}

    async def fake_delib(_app, q, *a, **k):
        seen["q"] = q
        if False:
            yield b""

    monkeypatch.setattr(app, "run_deliberation", fake_delib)
    asyncio.run(_collect(_Req("/심의 왜?", [])))
    assert seen["q"] == "왜?"

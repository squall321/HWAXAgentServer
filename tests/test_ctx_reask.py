# 창을 굳히지 않고 주기로 다시 묻는다 · 사람이 적은 창은 상한이다 · 섞인 풀은 가장 작은 창(HWAXPortal docs/delib-ops R5)
#
# 왜 생겼나. 창은 /v1/models 의 첫 답으로 프로세스 내내 굳었고 LLM_CONTEXT_TOKENS 는 '물어보기 실패 때' 값일 뿐이었다. GLM 두 대(창
# 300K · 1M)를 한 풀(nginx least_conn)로 묶자 첫 답이 1M 이면 300K 서버로 간 큰 좌석 호출이 400 이었다(10-20 free-gather 2건 · 10-22
# 박스가 /v1/models 를 작은 서버로만 보내 막았다). 다시 물은 답은 **줄일 때만** 받는다 — 넓히면 같은 400 이 다시 나고, 창이 오르내리면
# 카드 대조의 판(limits_rev)이 그때마다 바뀌어 리스크 앱의 실행이 보류된다.
#
#   실행:  .venv/bin/python -m pytest tests/test_ctx_reask.py -q
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

M = 1_048_576
K300 = 300_000


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _models(n):
    return {"data": [{"id": app.VLLM_MODEL, "max_model_len": n}]}


class _SyncThread:
    """뒤에서 다시 묻기를 그 자리에서 돌린다 — 판정만 본다(기다리지 않는 것은 진짜 스레드로 따로 본다)."""

    def __init__(self, target=None, name=None, daemon=None):
        self._t = target

    def start(self):
        self._t()


@pytest.fixture
def ask(monkeypatch):
    """캐시를 비우고 /models(httpx.get)와 시계를 가짜로 건다 — answers 는 차례로 줄 답(예외면 올린다 · 끝까지 가면 마지막 답을 되풀이),
    calls 는 물은 수, now 는 시계. 상한(env)은 꺼 두고 한 번에 세 번 묻고 600초마다 다시 묻는다(기본값)."""
    saved = dict(app._ctx_cache)
    for c in (app._ctx_cache, d._evid_cache, d._free_tok_cache):
        c.clear()
    st = {"calls": 0, "now": 1000.0, "answers": [_models(M)]}

    def _get(_url, **_k):
        st["calls"] += 1
        a = st["answers"].pop(0) if len(st["answers"]) > 1 else st["answers"][0]
        if isinstance(a, Exception):
            raise a
        return _Resp(a)

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(app.time, "monotonic", lambda: st["now"])
    monkeypatch.setattr(app, "threading", types.SimpleNamespace(Thread=_SyncThread))      # app 의 이름만 — threading 모듈은 그대로
    monkeypatch.setattr(app, "_CTX_FALLBACK", 128_000)
    monkeypatch.setattr(app, "_CTX_ENV_SET", False)
    monkeypatch.setattr(app, "_CTX_SAMPLES", 3)
    monkeypatch.setattr(app, "_CTX_TTL_S", 600.0)
    yield st
    for c in (app._ctx_cache, d._evid_cache, d._free_tok_cache):
        c.clear()
    app._ctx_cache.update(saved)


def _cap(monkeypatch, n):
    monkeypatch.setattr(app, "_CTX_FALLBACK", n)
    monkeypatch.setattr(app, "_CTX_ENV_SET", True)


def test_사람이_적은_창은_서버_답의_상한이고_출처는_env(ask, monkeypatch):
    _cap(monkeypatch, K300)
    assert app._model_context_tokens() == K300
    st = app._model_context_state()
    assert st["tokens"] == K300 and st["source"] == "env" and st["retrying"] is False
    h = app._context_health()
    assert (h["server"], h["env_cap"], h["source"], h["tokens"]) == (M, K300, "env", K300)


def test_서버_답이_상한보다_작으면_서버_답이고_출처는_server(ask, monkeypatch):
    _cap(monkeypatch, K300)
    ask["answers"] = [_models(200_000)]
    assert app._model_context_tokens() == 200_000 and app._model_context_state()["source"] == "server"


def test_상한을_안_적은_박스는_종전대로_서버_답이다(ask):
    assert app._model_context_tokens() == M and app._model_context_state()["source"] == "server"
    assert app._context_health()["env_cap"] is None


def test_한_번_물을_때_여러_번_물어_가장_작은_창을_쓴다(ask):
    ask["answers"] = [_models(M), _models(K300), _models(M)]
    assert app._model_context_tokens() == K300 and ask["calls"] == 3
    assert app._context_health()["samples"] == [M, K300, M]


def test_뒤_물음이_실패하면_받은_답만_쓴다(ask):
    ask["answers"] = [_models(K300), httpx.ConnectError("refused")]
    assert app._model_context_tokens() == K300 and ask["calls"] == 2, "실패한 뒤로는 더 묻지 않는다"


def test_주기가_지나면_다시_묻고_작아진_창은_받는다(ask):
    assert app._model_context_tokens() == M and ask["calls"] == 3
    ask["now"] += 599
    assert app._model_context_tokens() == M and ask["calls"] == 3, "주기 전에 다시 물었다"
    ask["answers"] = [_models(K300)]
    ask["now"] += 2
    assert app._model_context_tokens() == M, "다시 묻는 호출은 지금 값을 준다(묻기는 뒤에서)"
    assert ask["calls"] == 6 and app._model_context_tokens() == K300, "줄어든 창을 받지 않았다"
    assert app._model_context_state()["source"] == "server"


def test_다시_물은_답이_크면_넓히지_않고_wider_에_적는다(ask):
    ask["answers"] = [_models(K300)]
    assert app._model_context_tokens() == K300
    ask["answers"] = [_models(M)]
    ask["now"] += 601
    app._model_context_tokens()
    assert app._model_context_tokens() == K300, "다시 물은 큰 답으로 넓혔다 — 섞인 풀에서 작은 서버로 간 큰 호출이 400 이다"
    h = app._context_health()
    assert (h["wider"], h["server"], h["tokens"]) == (M, M, K300)
    ask["answers"] = [_models(K300)]
    ask["now"] += 601
    app._model_context_tokens()
    assert app._context_health()["wider"] is None, "작은 서버가 다시 답했는데 '더 큰 창' 표시가 남았다"


def test_상한이_걸린_박스는_다시_물어도_상한_위로_가지_않는다(ask, monkeypatch):
    _cap(monkeypatch, K300)
    ask["answers"] = [_models(200_000)]
    assert app._model_context_tokens() == 200_000
    ask["answers"] = [_models(M)]
    ask["now"] += 601
    app._model_context_tokens()
    assert app._model_context_tokens() == 200_000 and app._context_health()["wider"] == K300


def test_다시_묻기가_실패하면_지금_값을_두고_잠깐_뒤_다시_묻는다(ask):
    assert app._model_context_tokens() == M
    ask["answers"] = [httpx.ConnectError("refused")]
    ask["now"] += 601
    assert app._model_context_tokens() == M and ask["calls"] == 4
    assert app._model_context_tokens() == M and app._model_context_state()["retrying"] is False, "알던 창을 잃었다"
    ask["now"] += app._CTX_RETRY_S - 1
    app._model_context_tokens()
    assert ask["calls"] == 4, "실패한 뒤 곧바로 다시 물었다 — 죽은 엔드포인트를 호출마다 두드린다"
    ask["answers"] = [_models(K300)]
    ask["now"] += 2
    app._model_context_tokens()
    assert app._model_context_tokens() == K300


def test_창이_줄면_창에서_유도한_예산도_따라_준다(ask):
    before = (app._model_context_tokens(), d._pre_budget(), d._free_tool_tokens())
    ask["answers"] = [_models(32_768)]
    ask["now"] += 601
    app._model_context_tokens()
    assert app._model_context_tokens() == 32_768
    assert d._pre_budget() < before[1] and d._free_tool_tokens() < before[2], "유도값이 옛 창에 남았다"


def test_주기_0_이면_종전처럼_첫_답을_굳힌다(ask, monkeypatch):
    monkeypatch.setattr(app, "_CTX_TTL_S", 0.0)
    app._model_context_tokens()
    ask["answers"] = [_models(K300)]
    ask["now"] += 10 ** 7
    assert app._model_context_tokens() == M and ask["calls"] == 3


def test_시험이_박아_둔_창은_다시_묻지_않는다(ask, monkeypatch):
    monkeypatch.setattr(app, "_ctx_cache", {"n": 32_768, "src": "server"})
    ask["now"] += 10 ** 7
    assert app._model_context_tokens() == 32_768 and ask["calls"] == 0


def test_다시_묻는_동안_호출자는_기다리지_않고_묻기는_하나만_돈다(ask, monkeypatch):
    monkeypatch.setattr(app, "threading", threading)
    assert app._model_context_tokens() == M
    gate, entered = threading.Event(), threading.Event()

    def _slow(_url, **_k):
        ask["calls"] += 1
        entered.set()
        gate.wait(10)
        return _Resp(_models(K300))

    monkeypatch.setattr(httpx, "get", _slow)
    ask["now"] += 601
    t0 = time.perf_counter()
    assert app._model_context_tokens() == M
    assert time.perf_counter() - t0 < 0.5, "다시 묻는 동안 호출자가 기다렸다"
    assert entered.wait(5)
    for _ in range(5):
        assert app._model_context_tokens() == M
    assert ask["calls"] == 4, "다시 묻기가 겹쳐 돌았다"
    gate.set()
    for t in [t for t in threading.enumerate() if t.name == "ctx-refresh"]:
        t.join(10)
    assert app._model_context_tokens() == K300


def test_health_가_창의_근거를_싣는다(ask, monkeypatch):
    _cap(monkeypatch, K300)
    h = TestClient(app.app).get("/health").json()
    assert h["context_tokens"] == K300
    c = h["context"]
    assert (c["tokens"], c["source"], c["server"], c["env_cap"], c["reask_s"], c["samples_per_ask"]) == (K300, "env", M, K300, 600.0, 3)
    assert c["asked_s_ago"] == 0.0 and c["samples"] == [M, M, M]


@pytest.mark.parametrize("env_set,source", [(False, "fallback"), (True, "env")])
def test_못_물었으면_가정한_창과_다시_묻는_중을_보인다(ask, monkeypatch, env_set, source):
    """LLM 서버가 안 닿으면 엔진은 가정한 창으로 돈다 — /health.context 를 비워 두면 '창을 모른다' 로 읽힌다(dev 실측 10-22)."""
    monkeypatch.setattr(app, "_CTX_ENV_SET", env_set)
    ask["answers"] = [httpx.ConnectError("refused")]
    assert app._model_context_tokens() == 128_000
    h = app._context_health()
    assert (h["tokens"], h["source"], h["retrying"]) == (128_000, source, True) and h["retry_in_s"] == app._CTX_RETRY_S
    ask["answers"] = [_models(K300)]
    ask["now"] += app._CTX_RETRY_S + 1
    app._model_context_tokens()
    h = app._context_health()
    want = 128_000 if env_set else K300           # 사람이 적은 값은 서버 답의 상한이다(R5)
    assert (h["tokens"], h["retrying"]) == (want, False) and "retry_in_s" not in h

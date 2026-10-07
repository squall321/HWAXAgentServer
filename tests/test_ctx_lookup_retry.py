# 모델 컨텍스트 조회(/v1/models)가 실패했을 때 기본값을 프로세스 내내 굳히지 않는지 본다
#
# 조회가 예외로 끝나도 기본값(LLM_CONTEXT_TOKENS, 없으면 128,000)을 성공한 값과 똑같이 캐시했다. 첫 호출자는
# /health 이고, 재기동 직후에는 LLM 이 아직 안 떠 있다 — 그 한 번의 실패로 창이 16K 인 박스가 내내 128K 로
# 계산됐다. 의장 전사 상한을 창에서 유도하게 된 뒤로는(_decision_ctx) 그 값이 의장 프롬프트를 실제 창 밖으로
# 밀어, 라운드를 다 돌고도 결정문이 400 으로 죽는다. 실패는 잠깐만 믿고 다시 묻는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_ctx_lookup_retry.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

_REAL = 16384          # 이 박스의 실제 창 — 기본값(128,000)보다 훨씬 작다


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def lookup(monkeypatch):
    """캐시를 비우고 조회(httpx.get)와 시계를 가짜로 건다. 반환: SimpleNamespace 같은 dict —
    calls(조회 횟수) · now(시계) · answers(차례로 줄 답, 예외면 올린다)."""
    saved = dict(app._ctx_cache)
    for c in (app._ctx_cache, d._evid_cache, d._free_tok_cache):
        c.clear()
    st = {"calls": 0, "now": 1000.0, "answers": []}

    def _get(_url, **_k):
        st["calls"] += 1
        ans = st["answers"].pop(0) if len(st["answers"]) > 1 else st["answers"][0]
        if isinstance(ans, Exception):
            raise ans
        return _Resp(ans)

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(app.time, "monotonic", lambda: st["now"])
    monkeypatch.setattr(app, "_CTX_FALLBACK", 128000)
    monkeypatch.setattr(d, "_DECISION_CTX", None)      # 의장 상한을 창에서 유도하는 박스
    yield st
    for c in (app._ctx_cache, d._evid_cache, d._free_tok_cache):
        c.clear()
    app._ctx_cache.update(saved)


def _models(**extra):
    return {"data": [{"id": app.VLLM_MODEL, **extra}]}


def test_실패한_조회는_굳히지_않고_잠깐_뒤_다시_묻는다(lookup):
    lookup["answers"] = [httpx.ConnectError("connection refused"), _models(max_model_len=_REAL)]
    assert app._model_context_tokens() == 128000
    assert "n" not in app._ctx_cache, "실패한 조회의 기본값을 성공한 값처럼 굳혔다"
    assert app._model_context_tokens() == 128000 and lookup["calls"] == 1, (
        "다시 묻기 전인데 호출마다 조회했다 — 죽은 엔드포인트를 호출마다 5초씩 기다린다")
    lookup["now"] += app._CTX_RETRY_S + 1
    assert app._model_context_tokens() == _REAL and lookup["calls"] == 2
    lookup["now"] += app._CTX_RETRY_S * 100
    assert app._model_context_tokens() == _REAL and lookup["calls"] == 2, "성공한 값을 다시 물었다"


def test_조회가_살아나면_거기서_유도한_예산도_실제_창을_따른다(lookup):
    """창 값만 고치고 유도값의 캐시가 남으면, 좌석 근거 예산·자유 조회 스키마 예산은 여전히 128K 기준이다."""
    lookup["answers"] = [httpx.ConnectError("connection refused"), _models(max_model_len=_REAL)]
    assumed = (d._pre_budget(), d._free_tool_tokens(), d._decision_ctx(3000, 3))
    assert assumed[2] > d._DECISION_CTX_MIN, "시험 전제 — 가정한 128K 창에서는 의장 상한이 바닥보다 크다"
    lookup["now"] += app._CTX_RETRY_S + 1
    assert d._decision_ctx(3000, 3) == d._DECISION_CTX_MIN, "의장 전사 상한이 가정한 창에 남았다"
    assert d._pre_budget() < assumed[0], "좌석 사전 컨텍스트 예산이 가정한 창에 남았다"
    assert d._free_tool_tokens() < assumed[1], "자유 조회 스키마 예산이 가정한 창에 남았다"
    assert lookup["calls"] == 2, "유도값을 잴 때마다 다시 물었다"


def test_답은_왔는데_창_크기가_없으면_기본값을_굳힌다(lookup):
    """이건 실패가 아니다 — 다시 물어도 같은 답이다. 그 박스의 창은 LLM_CONTEXT_TOKENS 로 알린다."""
    lookup["answers"] = [_models()]
    assert [app._model_context_tokens() for _ in range(3)] == [128000] * 3
    assert lookup["calls"] == 1 and app._ctx_cache.get("n") == 128000


def test_성공한_조회는_한_번만_묻는다(lookup):
    lookup["answers"] = [_models(max_model_len=200000)]
    assert [app._model_context_tokens() for _ in range(3)] == [200000] * 3 and lookup["calls"] == 1
    first = d._pre_budget()
    assert d._pre_budget() == first and d._free_tool_tokens() == d._free_tool_tokens()

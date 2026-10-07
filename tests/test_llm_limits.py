# LLM 호출의 시간 한도와 재시도 — 설정이 없어도 걸리고, 요청에 실제로 실려 나가는지
#
# 설정이 하나도 없는 박스에서 LLM 호출에는 시간 한도가 **없었다**. 주석과 env 키트는 '미설정 = 라이브러리
# 기본 600초' 라고 적었지만 langchain-openai 가 timeout=None 을 명시로 넘겨 openai 의 기본값이 안 걸린다
# (요청에 실린 타임아웃 넷이 전부 None). 멈춘 호출 하나가 심의 잡 자리를 영영 붙들었다. 반대로 키트를 적용한
# 박스는 600초라, 20석 넘는 패널이 공유 LLM 에 줄을 서면 큐 대기만으로 넘겼다.
#
# **기동 절차(app.lifespan)를 실제로 열고**, 만들어진 LLM 으로 호출을 보내 요청에 실린 값을 본다.
# 망은 타지 않는다 — httpx 의 보내는 자리에서 요청을 가로챈다.
#
#   실행:  .venv/bin/python -m pytest tests/test_llm_limits.py -q
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402

_KNOBS = ("LLM_TIMEOUT_S", "LLM_CONNECT_TIMEOUT_S", "LLM_MAX_RETRIES",
          "DELIB_TIMEOUT_S", "DELIB_LLM_MAX_RETRIES",
          "DELIB_TEMPERATURE", "DELIB_MAX_TOKENS", "DELIB_REASONING_EFFORT")
_OK = {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
       "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}


def _boot(monkeypatch, **env):
    """기동 절차를 열었다 닫고 app.state 를 돌려준다. 박스의 설정이 시험을 흔들지 않게 손잡이를 전부 지우고
    시험이 준 것만 건다."""
    @asynccontextmanager
    async def _no_session_manager(_app):          # MCP 세션 매니저는 프로세스에 한 번만 열 수 있다 — 건너뛴다
        yield

    for k in _KNOBS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(a, "_load_mcp_config", dict)                 # 실 게이트웨이 설정을 읽지 않는다
    monkeypatch.setattr(a._DELIB_MCP.router, "lifespan_context", _no_session_manager)
    api = FastAPI()

    async def go():
        async with a.lifespan(api):
            pass

    try:
        asyncio.run(go())
    finally:
        delib_jobs.closing(False)                 # 종료 절차가 '내려가는 중' 을 세워 두었다 — 다음 시험이 잡을 띄운다
    return api.state


def _sent(monkeypatch, llm, *, fail=None):
    """그 LLM 으로 한 번 부르고, httpx 가 보내려던 요청마다의 타임아웃을 돌려준다. fail 을 주면 매 시도가
    그 예외로 끝난다(SDK 재시도가 몇 번 도는지 센다)."""
    seen = []

    async def _send(_self, request, **_kw):
        seen.append(dict(request.extensions.get("timeout") or {}))
        if fail is not None:
            raise fail("가로챈 요청", request=request)
        return httpx.Response(200, json=_OK, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", _send)
    err = None
    try:
        asyncio.run(llm.ainvoke([("human", "안녕")]))
    except Exception as exc:  # noqa: BLE001 — 실패의 모양은 시험이 본다
        err = exc
    return seen, err


def test_설정이_없어도_심의_호출은_1800초_연결은_10초로_나간다(monkeypatch):
    st = _boot(monkeypatch)
    seen, err = _sent(monkeypatch, st.delib_llm)
    assert err is None, err
    assert seen == [{"connect": 10.0, "read": 1800.0, "write": 1800.0, "pool": 1800.0}], (
        f"종전엔 넷 다 None(무제한)이었다 — {seen}")
    assert st.delib_timeout_s == 1800.0


def test_설정이_없어도_챗_호출은_900초_연결은_10초로_나간다(monkeypatch):
    st = _boot(monkeypatch)
    seen, err = _sent(monkeypatch, st.llm)
    assert err is None, err
    assert seen == [{"connect": 10.0, "read": 900.0, "write": 900.0, "pool": 900.0}], seen
    assert st.delib_llm is not st.llm, "심의가 챗 LLM 을 그대로 쓴다 — 한도와 재시도 횟수가 달라야 한다"


def test_심의는_한_번만_더_시도하고_챗은_두_번_더_시도하게_만들어진다(monkeypatch):
    """SDK 재시도는 타임아웃이면 생성을 처음부터 다시 한다. 심의의 논리 호출 1회 최악은 (1+재시도)×한도라,
    바깥 한도(리스크 앱 벽시계·침묵 한도)가 이 횟수 위에 계산돼 있다."""
    st = _boot(monkeypatch, DELIB_TIMEOUT_S=5)
    assert (st.delib_llm.max_retries, st.llm.max_retries) == (1, 2)
    seen, err = _sent(monkeypatch, st.delib_llm, fail=httpx.ReadTimeout)
    assert type(err).__name__ == "APITimeoutError", repr(err)
    assert len(seen) == 2, f"심의 LLM 이 {len(seen)}번 시도했다 — 종전 SDK 기본은 3번이다"
    assert all(t["read"] == 5.0 and t["connect"] == 10.0 for t in seen), seen


def test_재시도_횟수는_설정으로_바꾼다(monkeypatch):
    st = _boot(monkeypatch, DELIB_LLM_MAX_RETRIES=0, LLM_MAX_RETRIES=0)
    for llm in (st.delib_llm, st.llm):
        seen, err = _sent(monkeypatch, llm, fail=httpx.ReadTimeout)
        assert len(seen) == 1 and type(err).__name__ == "APITimeoutError", (len(seen), repr(err))


def test_심의_한도는_챗_한도를_물려받지_않는다(monkeypatch):
    """종전엔 DELIB_TIMEOUT_S 가 없으면 LLM_TIMEOUT_S 를 물려받았다 — 챗 한도를 줄인 박스에서 심의가 같이 줄었다."""
    st = _boot(monkeypatch, LLM_TIMEOUT_S=120)
    assert _sent(monkeypatch, st.llm)[0][0]["read"] == 120.0
    assert _sent(monkeypatch, st.delib_llm)[0][0]["read"] == 1800.0


def test_요청_단위_한도는_read_만_바꾸고_연결과_재시도는_그대로다(monkeypatch):
    st = _boot(monkeypatch, LLM_CONNECT_TIMEOUT_S=7, DELIB_LLM_MAX_RETRIES=1)
    llm = st.mk_delib_llm(7200.0)
    assert llm.max_retries == 1
    seen, _err = _sent(monkeypatch, llm)
    assert seen == [{"connect": 7.0, "read": 7200.0, "write": 7200.0, "pool": 7200.0}], seen


@pytest.mark.parametrize("knob,which", [("DELIB_TIMEOUT_S", "delib_llm"), ("LLM_TIMEOUT_S", "llm")])
def test_0_은_무제한이고_기동_로그가_그_손잡이를_말한다(monkeypatch, capsys, knob, which):
    """끄는 길은 남긴다(명시한 0). 다만 연결 한도는 그대로 걸리고, 기동 로그가 무엇이 꺼졌는지 말한다."""
    st = _boot(monkeypatch, **{knob: 0})
    out = capsys.readouterr().out
    assert f"⚠ {knob}=0" in out, out
    seen, _err = _sent(monkeypatch, getattr(st, which))
    assert seen == [{"connect": 10.0, "read": None, "write": None, "pool": None}], seen


def test_기동_로그에_걸린_한도와_손잡이_이름이_나온다(monkeypatch, capsys):
    _boot(monkeypatch)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("[agent] LLM 한도"))
    for want in ("900", "1800", "10", "LLM_TIMEOUT_S", "DELIB_TIMEOUT_S", "LLM_CONNECT_TIMEOUT_S",
                 "LLM_MAX_RETRIES", "DELIB_LLM_MAX_RETRIES"):
        assert want in line, (want, line)
    assert "=0 — LLM 호출에 시간 한도가 없다" not in out

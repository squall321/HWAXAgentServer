# 사람별 LLM 토큰(usage_tokens)과 '누구의 일' 감사 머리 — 자리 · 세기 · 저장 · 창구 · 서비스 자격 게이트웨이 호출의 머리를 본다(HWAXPortal docs/ops-dashboard OD-52 · OD-53)
"""  · 자리(사람 · 종류)는 태스크 문맥을 따른다 — 그 문맥에서 만든 태스크가 물려받고 다른 요청으로 새지 않는다.
  · LLM 마다 붙인 기록기가 끝난 결과의 사용량만 읽는다. 사용량이 없으면 '모름'(지어내지 않는다) · 실패한 호출은 세지 않는다.
  · 메모리 → sqlite(0600) · 400일 · 쓰다 실패하면 잃지 않는다 · 창구가 사람 × 날 × 종류 × 모델로 낸다(뺄 계정 빼고).
  · 신원 머리가 없는 서비스 자격 도구 로딩에만 '누구의 일' 머리가 붙는다 — 신원이 있으면 붙지 않는다.

  실행:  .venv/bin/python -m pytest tests/test_usage_tokens.py -q
"""
import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, LLMResult  # noqa: E402

import app as a  # noqa: E402
import deliberation as d  # noqa: E402
import usage_tokens as ut  # noqa: E402

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=KST).timestamp()
ME = "me@example.com"


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(ut, "STORE_DIR", tmp_path / "usage")
    monkeypatch.setattr(ut, "_ensure_thread", lambda: None)      # 시험은 더하는 스레드 없이 flush 를 손으로 부른다
    ut._reset_for_tests()
    yield tmp_path / "usage"
    ut._reset_for_tests()


def _result(usage=None, model="glm-5", via="metadata") -> LLMResult:
    msg = AIMessage(content="비밀 답")
    if usage is not None and via == "metadata":
        msg.usage_metadata = {"input_tokens": usage[0], "output_tokens": usage[1], "total_tokens": usage[0] + usage[1],
                              **({"input_token_details": {"cache_read": usage[2]}} if len(usage) > 2 else {})}
    out = {"model_name": model}
    if usage is not None and via == "llm_output":
        out["token_usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]}
    return LLMResult(generations=[[ChatGeneration(message=msg)]], llm_output=out)


def _call(rec, usage=None, *, model="glm-5", via="metadata", error=False, start_model="req-model"):
    rid = object()
    rec.on_chat_model_start({}, [[]], run_id=rid, invocation_params={"model": start_model})
    if error:
        rec.on_llm_error(RuntimeError("x"), run_id=rid)
    else:
        rec.on_llm_end(_result(usage, model=model, via=via), run_id=rid)


def _db(store):
    with closing(sqlite3.connect(store / "llm-tokens.sqlite")) as c:
        return c.execute("SELECT day, user, kind, model, calls, unknown, tokens_in, tokens_out, cache_read FROM tokens ORDER BY 1, 2, 3, 4").fetchall()


# ── 자리 ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_자리는_태스크_문맥을_따르고_이메일_꼴만_받는다():
    async def go():
        async def child():
            return ut.current()
        ut.tag(user="Me%40Example.com ", kind="chat")
        t = asyncio.create_task(child())
        ut.tag(kind="delib")                      # 만든 뒤에 바꾼 종류는 이미 만든 태스크에 닿지 않는다
        return await t, ut.current()

    got_child, got_here = asyncio.run(go())
    assert got_child == (ME, "chat") and got_here == (ME, "delib")
    assert ut.current() == ("", "other"), "다른 문맥(요청)으로 새지 않는다"

    async def bad():
        ut.tag(user="이메일 아님", kind="weird")
        return ut.current()
    assert asyncio.run(bad()) == ("", "other")


# ── 세기 ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_기록기는_끝난_결과의_사용량을_자리의_사람_종류로_센다(store):
    rec = ut._recorder_class()()

    async def go():
        ut.tag(user=ME, kind="think")
        _call(rec, (100, 20, 64), via="metadata")
        _call(rec, (50, 5), via="llm_output", model="qwen-chat")
        _call(rec, None)                                       # 사용량이 없다(흘려 받은 호출) — 모름
        _call(rec, (9, 9), error=True)                         # 실패한 호출은 세지 않는다
        _call(rec, None, model="", start_model="")             # 모델 이름도 모른다
    asyncio.run(go())
    ut.flush()
    day = datetime.now(KST).date().isoformat()                 # 기록기는 끝난 때(지금)의 KST 날에 센다
    assert _db(store) == [(day, ME, "think", "(unknown)", 1, 1, 0, 0, 0),
                          (day, ME, "think", "glm-5", 2, 1, 100, 20, 64),
                          (day, ME, "think", "qwen-chat", 1, 0, 50, 5, 0)]


def test_자리가_없으면_이름_없는_줄이고_이상한_모델_이름은_한_칸으로_접는다(store):
    ut.record("<script>", {"prompt": 3, "completion": 4}, now=NOW)
    ut.record("ok-model", {"prompt": True, "completion": -1}, now=NOW)       # 수가 아닌 칸은 모름이다
    ut.flush(now=NOW)
    assert _db(store) == [("2026-11-20", "", "other", "(other)", 1, 0, 3, 4, 0), ("2026-11-20", "", "other", "ok-model", 1, 1, 0, 0, 0)]


def test_기록기를_붙여도_LLM_의_요청은_그대로다():
    from langchain_openai import ChatOpenAI
    plain = ChatOpenAI(base_url="http://127.0.0.1:1/v1", api_key="EMPTY", model="x")
    wrapped = ut.instrument(ChatOpenAI(base_url="http://127.0.0.1:1/v1", api_key="EMPTY", model="x"))
    assert plain._get_request_payload([("user", "hi")]) == wrapped._get_request_payload([("user", "hi")])
    assert len(wrapped.callbacks) == 1 and not getattr(wrapped, "streaming", False)
    assert ut.instrument(None) is None


# ── 저장 ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_저장은_더하고_400일_뒤_지우고_파일은_주인만_읽는다(store):
    ut.record("m", {"prompt": 1, "completion": 1}, user=ME, kind="chat", now=NOW)
    ut.flush(now=NOW)
    ut.record("m", {"prompt": 2, "completion": 2}, user=ME, kind="chat", now=NOW)
    ut.flush(now=NOW)
    assert _db(store) == [("2026-11-20", ME, "chat", "m", 2, 0, 3, 3, 0)]
    assert (store / "llm-tokens.sqlite").stat().st_mode & 0o777 == 0o600 and store.stat().st_mode & 0o777 == 0o700
    old = NOW - 86400 * 400
    ut.record("m", {"prompt": 1}, user=ME, kind="chat", now=old)
    ut._STATE["pruned_day"] = None
    ut.flush(now=NOW)
    assert [r[0] for r in _db(store)] == ["2026-11-20"], "오늘까지 400일만 둔다"


def test_쓰다_실패하면_셈을_잃지_않는다(store, monkeypatch):
    ut.record("m", {"prompt": 5, "completion": 1}, user=ME, kind="chat", now=NOW)
    real = ut._open
    monkeypatch.setattr(ut, "_open", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    assert ut.flush(now=NOW) == 0 and ut.health()["errors"] == 1 and "disk I/O" in ut.health()["last_error"]
    monkeypatch.setattr(ut, "_open", real)
    ut.flush(now=NOW)
    assert _db(store) == [("2026-11-20", ME, "chat", "m", 1, 0, 5, 1, 0)]


# ── 창구 ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_창구는_토큰_줄과_적기_시작한_날을_내고_뺄_계정을_뺀다(store, monkeypatch, tmp_path):
    import delib_jobs
    import usage_rollup as ur
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path / "jobs")
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    for who in (ME, "tester@example.com"):
        ut.record("glm-5", {"prompt": 10, "completion": 2}, user=who, kind="delib", now=NOW)
    ut.record("glm-5", None, user="", kind="other", now=NOW)
    v = ur.view({"since": "2026-11-19", "until": "2026-11-21", "exclude": ["Tester@Example.com"]})
    assert v["llm"] == [{"day": "2026-11-20", "user": "", "kind": "other", "model": "glm-5", "calls": 1, "unknown": 1, "tokens_in": 0,
                         "tokens_out": 0, "cache_read": 0},
                        {"day": "2026-11-20", "user": ME, "kind": "delib", "model": "glm-5", "calls": 1, "unknown": 0, "tokens_in": 10,
                         "tokens_out": 2, "cache_read": 0}]
    assert v["llm_since"] == datetime.now(KST).date().isoformat(), "처음 더한 날 — 그 전은 기록 전"
    assert "비밀 답" not in json.dumps(v, ensure_ascii=False)


def test_건강_칸은_센_수와_흘려_받는_호출의_사용량_요청을_말한다(monkeypatch):
    ut.record("m", None, now=NOW)
    h = TestClient(a.app).get("/health").json()["usage_tokens"]
    assert h["recorded"] == 1 and h["pending"] == 1 and h["stream_usage"] is False


# ── 누구의 일 감사 머리 ─────────────────────────────────────────────────────────────────────────────────────
CONNS = {"gateway": {"url": "http://gw.invalid/mcp", "transport": "streamable_http", "headers": {"Authorization": "Bearer gw"}}}


def test_신원이_없는_연결에만_누구의_일_머리를_싣는다():
    h = a._with_groups(CONNS, ["CAEG"], audit_for="Me%40Example.com")["gateway"]["headers"]
    assert h[a.AUDIT_FOR_HEADER] == "me@example.com" and a.USER_HEADER not in h
    h = a._with_groups(CONNS, ["CAEG"], user="you@example.com", audit_for=ME)["gateway"]["headers"]
    assert a.AUDIT_FOR_HEADER not in h and h[a.USER_HEADER] == "you@example.com", "신원이 있으면 그것이 호출자다 — 붙이지 않는다"
    for bad in ("", "이메일 아님", None):
        assert a.AUDIT_FOR_HEADER not in a._with_groups(CONNS, [], audit_for=bad)["gateway"]["headers"]
    assert CONNS["gateway"]["headers"] == {"Authorization": "Bearer gw"}, "입력을 고치지 않는다"


def test_서비스_자격_도구_로딩은_그_자리의_사람을_싣고_신원이_있으면_싣지_않는다(monkeypatch):
    seen = []

    async def load(scoped):
        seen.append(scoped["gateway"]["headers"])
        return []
    monkeypatch.setattr(d, "_load_tools", load)
    app_ = SimpleNamespace(state=SimpleNamespace(connections=CONNS))

    async def go(user=""):
        ut.tag(user=ME, kind="delib")
        return await d._tools_by_name(app_, ["CAEG"], user=user)
    asyncio.run(go())
    asyncio.run(go(user="you@example.com"))
    asyncio.run(d._tools_by_name(app_, ["CAEG"]))            # 자리가 없는 문맥(시스템 일)
    assert [h.get(a.AUDIT_FOR_HEADER) for h in seen] == [ME, None, None]


def test_머리_이름은_게이트웨이와_같다():
    gw = ROOT.parent / "HWAXMcpGateway" / "gateway.py"
    if not gw.exists():
        pytest.skip("형제 리포 HWAXMcpGateway 가 없다")
    assert f'AUDIT_FOR_HEADER = "{a.AUDIT_FOR_HEADER.lower()}"' in gw.read_text(encoding="utf-8")


def test_창구가_자리를_단다():
    """심의 준비 · 카탈로그 창구는 포털이 실은 user_email 로 자리를 단다 — 그 창구의 LLM 토큰 · 서비스 자격 호출이 그 사람의 일이 된다."""
    import ast
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"deliberate_experts": "delib", "deliberate_voc_preview": "delib", "deliberate_topic": "delib", "deliberate_clarify": "delib",
            "catalog_agent": "other", "catalog_agent_records": "other", "catalog_record": "other", "chat": "chat"}
    got = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name in want:
            for c in ast.walk(n):
                if (isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "tag" and getattr(c.func.value, "id", "") == "_usage_tokens"):
                    kw = {k.arg: k.value for k in c.keywords}
                    got[n.name] = (ast.unparse(kw["user"]), kw["kind"].value)
    assert got == {k: ("req.user_email", v) for k, v in want.items()}, got
    for cls in ("ExpertsRequest", "VocPreviewRequest", "ClarifyRequest", "TopicRequest", "AgentDetailRequest", "AgentRecordsRequest",
                "RecordRequest"):
        assert "user_email" in getattr(a, cls).model_fields, cls


def test_앱의_LLM_은_모두_기록기를_달고_흘려_받는_호출의_사용량_요청은_손잡이로만_켠다(monkeypatch):
    """기동 절차가 짓는 LLM(챗 · 심의 · 띵킹 · 챗 전용)에 기록기가 붙는지, 손잡이(LLM_STREAM_USAGE)가 꺼져 있으면 요청 꼴이 그대로인지 — 켜면 그때만
    stream_usage 를 싣는다."""
    from test_llm_limits import _boot

    def names(llm):
        return [type(c).__name__ for c in (getattr(llm, "callbacks", None) or [])]
    monkeypatch.setattr(a, "LLM_STREAM_USAGE", False)
    st = _boot(monkeypatch)
    assert "UsageTokensRecorder" in names(st.llm) and "UsageTokensRecorder" in names(st.delib_llm)
    assert st.llm.stream_usage is None, "꺼져 있으면 라이브러리 기본 그대로(주소를 준 서버는 흘려 받는 호출에 사용량을 묻지 않는다)"
    monkeypatch.setattr(a, "LLM_STREAM_USAGE", True)
    st = _boot(monkeypatch)
    assert st.llm.stream_usage is True and st.delib_llm.stream_usage is True


def test_자리는_읽힐_때_한_번_정하고_시험_격리가_임시_자리로_돌린다():
    """더하는 스레드가 깨어날 때마다 환경을 다시 읽지 않는다 — 그리고 이 시험 세션의 자리는 conftest 가 정한 임시 자리다(이 박스의 실 자리가 아니다)."""
    import importlib
    fresh = importlib.reload(ut)
    try:
        assert str(fresh.STORE_DIR) == os.environ["USAGE_TOKENS_DIR"] and fresh.STORE_DIR != ROOT / "usage"
        assert "/data" not in str(fresh.STORE_DIR)
    finally:
        importlib.reload(ut)


def test_더할_것이_없으면_파일을_만들지_않는다(store):
    assert ut.flush() == 0 and ut.rows("2026-11-01", "2026-11-30") == ([], None)
    assert not store.exists(), "읽기 · 빈 더하기가 자리를 만들지 않는다"

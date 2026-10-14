# 카드 대조 실행기(run)가 관문·ping·보충·오류 분류를 계약대로 하는지 가짜 LLM 대본으로 실제 제너레이터를 돌려 본다
#
# 요청 1건 = 논리 호출 1건이다. 줄을 세우지 않고(자리가 없으면 곧바로 review_busy), 끊기면 LLM 을 접고 자리를 돌려주며,
# 빠진 행만 다시 묻는다. 한 줄이 깨져도 그 행만 잃고(30행이 1행으로 줄지 않는다), 보충 중의 LLM 실패는 받은 행을 버리지
# 않는다(P-08). 오류 코드의 부류(infra·retryable)는 앱이 차감·재시도를 가르는 근거라 상태 코드별로 못박는다(P-13).
# 거동 이름은 WP5b 3.6.2 의 것이다(C8 거동 1·2·4·8·14 와 연쇄).
#
# 요청은 실제 생산자(지어낸 카드 → normalize_card → assign_aliases, 실제 build_prompt)로 만든다.
#
#   실행:  .venv/bin/python -m pytest tests/test_card_review_run.py -q
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
from fakes.llm import FAKE_QUOTE, Act, FakeLLM  # noqa: E402
from test_card_review_prompts import CANARY, GAPS, _digest, _pack, _req, _unit  # noqa: E402

LIM = cr.limits(1_048_576, "server", 0, env={}, model="fake-glm")
# 지어낸 카드 열 장(점검형 여섯)에 늘린 점검형 넷 — 묶음 하나(K=8)를 채울 수 있게.
PACK = _pack(random.Random(1), n=4, size=(300, 600))


def _verdict(n=8, variant="fwd", unit=None, lim=LIM):
    pack = PACK
    chk = [c for c in pack if c["checkable"]][:n]
    req = _req("verdict", chk, _digest(pack, [c["alias"] for c in chk]), unit=unit or _unit(0, canary=CANARY),
               variant=variant)
    return cr.prepare(req, lim)


def _sweep(n_units=30):
    pack = PACK
    manifest = [{"unit_id": f"u-{i:02d}", "kind": "bundle", "title": f"단위 {i}", "text": "후면 커버 둘레 변경"}
                for i in range(n_units)]
    return cr.prepare(_req("sweep", pack, [], manifest=manifest), LIM)


def _offcard():
    pack = PACK
    return cr.prepare(_req("offcard", [c for c in pack if not c["checkable"]], _digest(pack), unit=_unit(0)), LIM)


def _app(llm):
    return SimpleNamespace(state=SimpleNamespace(delib_llm=llm))


def _frames(chunks):
    return [delib_jobs._parse_sse(c) for c in chunks]


def _go(prep, script, *, app=None, llm=None):
    llm = llm or FakeLLM(script)
    app = app or _app(llm)

    async def go():
        return [c async for c in cr.run(app, prep)]

    return _frames(asyncio.run(go())), llm, app


def _last(frames):
    return [(e, d) for e, d in frames if e in ("result", "error")][-1]


def _names(frames):
    return [e for e, _ in frames if e != "ping"]


# ── 거동 1 oracle ─────────────────────────────────────────────────────────────
def test_정답이면_한_번에_끝나고_결과_모양이_계약대로다(capsys):
    prep = _verdict(8)
    fr, llm, app = _go(prep, ["oracle"])
    assert _names(fr) == ["status", "result", "done"]
    assert fr[0][1] == {"step": "attempt", "n": 1}
    res = _last(fr)[1]
    assert set(res) == {"kind", "variant", "rows", "missing", "extras", "quality", "meta"}
    q = res["quality"]
    assert (q["expected"], q["ok"], q["flagged"], q["missing"], q["attempts"], q["repaired"]) == (8, 8, 0, 0, 1, False)
    assert q["truncated"] is False and q["tail_broken"] is False and "repair_error" not in q
    assert res["missing"] == [] and res["extras"] == {"inj": []}
    m = res["meta"]
    assert (m["model"], m["ctx_tokens"], m["ctx_source"], m["limits_rev"]) == ("fake-glm", 1_048_576, "server",
                                                                               LIM["limits_rev"])
    assert m["prompt_rev"] == cr.PROMPT_REV["verdict"] and m["norm_rev"] == cr.NORM_REV
    assert m["input_sha"] == prep["ctx"]["input_sha"] and m["prompt_chars"] == prep["ctx"]["prompt_chars"]
    assert m["finish_reason"] == "stop" and len(m["llm_ms"]) == 1 and m["basis"] == "cards"
    u = m["usage"]
    assert u["llm_calls"] == 1 and u["tokens_in"] > 0 and u["per_attempt"] == [{"n": 1, **{k: u[k] for k in
                                                                                               ("tokens_in", "tokens_out")}}]
    assert m["aliases"]["cards"] == {r["k"]: r["record_id"] for r in res["rows"]}
    # 행마다 별칭을 되돌렸고 인용 대조를 지났다.
    assert all(r["checks"]["quote"] == "ok" and r["refs"] for r in res["rows"])
    # 카나리아는 프롬프트에 있고(조용히 빠지지 않았다) 서버 표준 출력에는 없다(본문을 로그에 찍지 않는다).
    assert CANARY in llm.calls[0]["human"]
    out = capsys.readouterr().out
    assert CANARY not in out and "attempts=1" in out
    assert app.state.review_gate.active == 0 and app.state.review_gate.totals["ok"] == 1


def test_프롬프트의_카드_수는_K_이하이고_L_별칭_수는_보낸_줄_수와_같다():
    prep = _verdict(8)
    _fr, llm, _app_ = _go(prep, ["oracle"])
    human = llm.calls[0]["human"]
    unit = prep["req"]["unit"]
    assert len(re.findall(r"^\[K\d+\] 종류 ", human, re.M)) <= LIM["verdict"]["k"]
    n_lines = len(unit["lines"]) + len(unit["context"]) + len(unit["evidence"])
    assert len(re.findall(r"^L\d+ \[", human, re.M)) == n_lines


# ── 거동 2 broken_line ────────────────────────────────────────────────────────
def test_한_줄이_깨지면_그_행만_빠지고_그_단위만_다시_묻는다():
    prep = _sweep(30)
    fr, llm, _ = _go(prep, [Act("broken_line", at=16), "oracle"])
    res = _last(fr)[1]
    assert _names(fr) == ["status", "status", "result", "done"]
    first = [r for r in res["rows"] if r["from_attempt"] == 1]
    assert len(first) == 29, f"깨진 줄 하나가 {30 - len(first)}행을 잃었다(30행이 한 행으로 줄면 안 된다)"
    hint = llm.calls[1]["human"].split("(보충 ", 1)[1]
    assert "U17" in hint and "읽히지 않은 줄 1개" in hint
    assert sorted(set(re.findall(r"\bU\d+\b", hint))) == ["U17"], "보충이 빠진 단위 말고 다른 것도 물었다"
    q = res["quality"]
    assert (q["missing"], q["bad_lines"], q["attempts"], q["repaired"]) == (0, 1, 2, True)


def test_끝내_못_채우면_missing_으로_돌아온다():
    prep = _sweep(30)
    fr, llm, _ = _go(prep, [Act("broken_line", at=16)] * 3)
    res = _last(fr)[1]
    assert res["missing"] == ["U17"] and res["quality"]["missing"] == 1 and res["quality"]["attempts"] == 3
    assert len(llm.calls) == 3


# ── 거동 4 truncate ───────────────────────────────────────────────────────────
def test_절단이면_읽힌_행을_살리고_빠진_것만_묻는다():
    prep = _verdict(8)
    fr, llm, _ = _go(prep, ["truncate", "oracle"])
    res = _last(fr)[1]
    q = res["quality"]
    assert q["truncated"] is True and q["tail_broken"] is True and q["missing"] == 0
    kept = [r["k"] for r in res["rows"] if r["from_attempt"] == 1]
    assert kept == ["K01", "K02", "K03", "K04"], kept
    hint = llm.calls[1]["human"].split("(보충 ", 1)[1]
    assert set(re.findall(r"\bK\d+\b", hint)) == {"K05", "K06", "K07", "K08"}
    assert res["meta"]["finish_reason"] == "stop"


def test_첫_시도가_절단이고_0행이면_다시_묻지_않는다():
    prep = _verdict(3)
    fr, llm, app = _go(prep, ["truncate_empty", "oracle"])
    assert _names(fr) == ["status", "error", "done"]
    err = _last(fr)[1]
    assert (err["code"], err["infra"], err["retryable"], err["knob"], err["attempt"]) == (
        "truncated_empty", False, False, "DELIB_MAX_TOKENS", 1)
    assert len(llm.calls) == 1
    assert app.state.review_gate.totals["error"] == 1


def test_finish_reason_을_모르면_절단은_None_이다():
    prep = _verdict(2)
    fr, _llm, _ = _go(prep, [Act(text=None, finish_reason=None)], llm=FakeLLM([Act("oracle")], usage=False))
    res = _last(fr)[1]
    assert res["meta"]["usage"] is None, "사용량을 모르는데 0 으로 셌다"
    # 대본 기본 finish 는 stop 이라 이 대역에서는 False — 못 읽는 경우는 아래 raw 대본으로 본다.
    raw = FakeLLM([Act(text='{"t":"v","k":"K01","applies":"no","judgement":"na","cq":"","l":[],"why":"x","need":""}',
                       finish_reason=None)])
    fr, _llm, _ = _go(_verdict(1), None, llm=raw)
    assert _last(fr)[1]["quality"]["truncated"] is None


# ── 거동 8 skip_middle ────────────────────────────────────────────────────────
def test_가운데가_빠지면_빠진_카드만_다시_묻고_합친다():
    prep = _verdict(8)
    fr, llm, _ = _go(prep, ["skip_middle", "oracle"])
    res = _last(fr)[1]
    hint = llm.calls[1]["human"].split("(보충 ", 1)[1]
    assert set(re.findall(r"\bK\d+\b", hint)) == {"K03", "K04", "K05"}
    assert [r["k"] for r in res["rows"] if r["t"] == "v"] == ["K01", "K02", "K06", "K07", "K08", "K03", "K04", "K05"]
    assert {r["k"]: r["from_attempt"] for r in res["rows"]}["K04"] == 2
    assert res["quality"]["missing"] == 0 and res["quality"]["attempts"] == 2


def test_보충이_소진되면_가진_것으로_끝낸다():
    prep = _verdict(8)
    fr, llm, _ = _go(prep, ["skip_middle"] * 3)
    res = _last(fr)[1]
    assert len(llm.calls) == 3 and res["quality"]["attempts"] == 3
    # 보충마다 지적한 셋 가운데 가운데 1/3 이 또 빠진다 — 끝내 못 채운 카드가 missing 이다.
    assert res["missing"] and res["quality"]["missing"] == len(res["missing"])
    assert res["quality"]["expected"] == 8


def test_보충_횟수_손잡이(monkeypatch):
    monkeypatch.setenv("CARD_REVIEW_PARSE_RETRIES", "0")
    fr, llm, _ = _go(_verdict(8), ["skip_middle", "oracle"])
    assert len(llm.calls) == 1 and _last(fr)[1]["quality"]["missing"] == 3


# ── 보충 중 실패(P-08) ────────────────────────────────────────────────────────
def test_보충_중_LLM_이_실패해도_받은_행을_버리지_않는다():
    prep = _verdict(8)
    fr, _llm, app = _go(prep, ["skip_middle", "timeout"])
    e, res = _last(fr)
    assert e == "result", "보충 실패로 첫 시도의 행까지 버렸다"
    assert len(res["rows"]) == 5 and res["missing"] == ["K03", "K04", "K05"]
    assert res["quality"]["repair_error"]["code"] == "llm_timeout" and res["quality"]["repair_error"]["attempt"] == 2
    u = res["meta"]["usage"]
    assert u["llm_calls"] == 2 and u["tokens_in"] is None, "실패한 시도의 사용량을 0 으로 셌다"
    assert u["per_attempt"][1] == {"n": 2, "tokens_in": None, "tokens_out": None}
    assert app.state.review_gate.totals["tokens_unknown_calls"] == 1


def test_받은_행이_없는데_보충이_실패하면_error_다():
    fr, _llm, _ = _go(_verdict(3), ["prose_only", "http_503"])
    e, err = _last(fr)
    assert e == "error" and (err["code"], err["infra"], err["attempt"]) == ("llm_error", True, 2)


def test_전_시도_0행이면_no_rows():
    fr, llm, _ = _go(_verdict(3), ["prose_only"])
    e, err = _last(fr)
    assert (e, err["code"], err["infra"], err["retryable"]) == ("error", "no_rows", False, True)
    assert len(llm.calls) == 3 and err["raw_excerpt"].startswith("검토해 보니")


# ── 거동 14 — 오류 부류 ───────────────────────────────────────────────────────
@pytest.mark.parametrize("behavior,code,infra,retryable,status", [
    ("timeout", "llm_timeout", True, True, None),
    ("unreachable", "llm_unreachable", True, True, None),
    ("http_503", "llm_error", True, True, 503),
    ("http_500", "llm_error", True, True, 500),
    ("http_429", "llm_error", True, True, 429),
    ("http_401", "llm_misconfigured", True, False, 401),
    ("http_404", "llm_misconfigured", True, False, 404),
    ("http_400", "llm_rejected", False, False, 400),
    ("http_413", "llm_rejected", False, False, 413),
    ("http_422", "llm_rejected", False, False, 422),
])
def test_LLM_실패의_부류(behavior, code, infra, retryable, status):
    fr, llm, app = _go(_verdict(2), [behavior])
    assert _names(fr) == ["status", "error", "done"]
    err = _last(fr)[1]
    assert (err["code"], err["infra"], err["retryable"], err.get("status")) == (code, infra, retryable, status)
    assert err["message"] and err["attempt"] == 1 and len(llm.calls) == 1
    if code == "llm_timeout":
        assert err["knob"] == "DELIB_TIMEOUT_S · DELIB_LLM_MAX_RETRIES"
    if code == "llm_misconfigured":
        assert "VLLM_MODEL" in err["knob"]
    assert app.state.review_gate.active == 0


def _near_window_unit(lim, prep_kind="verdict"):
    """조립 길이가 창 한도(P_hard) 바로 밑에 닿는 단위 — 한글 본문으로 채운다(추정 토큰이 글자 수에 가깝다)."""
    base = _verdict(1, lim=lim)
    room = lim["p_hard"] - base["ctx"]["prompt_chars"] - 200
    u = _unit(0)
    u["lines"].append({"ref": "c:big", "text": "가" * room})
    return u


def test_창_근처의_400_은_context_overflow_이고_다시_쪼개지_않는다():
    lim = cr.limits(16_384, "server", 0, env={}, model="fake-glm")
    prep = _verdict(1, unit=_near_window_unit(lim), lim=lim)
    fr, llm, _ = _go(prep, ["http_400"])
    err = _last(fr)[1]
    assert (err["code"], err["infra"], err["retryable"], err["knob"]) == ("context_overflow", False, False,
                                                                         "LLM_CONTEXT_TOKENS")
    assert err["est_tokens"] + err["out_tokens"] >= 0.85 * err["ctx_tokens"] and err["ctx_tokens"] == 16_384
    assert len(llm.calls) == 1
    # 같은 400 이라도 작은 프롬프트면 창 초과가 아니다(문구에 기대지 않는다).
    fr, _llm, _ = _go(_verdict(1, lim=lim), ["http_400"])
    assert _last(fr)[1]["code"] == "llm_rejected"


def test_LLM_이_없으면_misconfigured():
    async def go():
        return [c async for c in cr.run(_app(None), _verdict(1))]

    fr = _frames(asyncio.run(go()))
    assert _last(fr)[1]["code"] == "llm_misconfigured"


# ── 관문 ─────────────────────────────────────────────────────────────────────
def _concurrent(app, n, prep):
    async def one():
        return _frames([c async for c in cr.run(app, prep)])

    async def go():
        return await asyncio.gather(*(one() for _ in range(n)))

    return asyncio.run(go())


def test_동시_12건이면_LLM_동시_수는_8_이하이고_나머지는_곧바로_바쁨(monkeypatch):
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    llm = FakeLLM([Act("slow", delay=0.2)])
    app = _app(llm)
    outs = _concurrent(app, 12, _verdict(2))
    codes = [_last(f)[1].get("code", "ok") for f in outs]
    assert llm.peak <= 8 and codes.count("ok") == 8 and codes.count("review_busy") == 4, (llm.peak, codes)
    busy = next(_last(f)[1] for f in outs if _last(f)[1].get("code") == "review_busy")
    assert (busy["infra"], busy["retryable"], busy["retry_after_s"], busy["limit"], busy["reason"]) == (
        True, True, 30, 8, "normal")
    assert app.state.review_gate.active == 0 and app.state.review_gate.totals["busy"] == 4


def test_사람이_띄운_심의가_돌면_동시_수는_4_이하(monkeypatch):
    monkeypatch.setattr(a, "_delib_load", lambda: (1, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 1)
    llm = FakeLLM([Act("slow", delay=0.2)])
    outs = _concurrent(_app(llm), 12, _verdict(2))
    assert llm.peak <= 4 and sum(1 for f in outs if _last(f)[0] == "result") == 4
    busy = next(_last(f)[1] for f in outs if _last(f)[1].get("code") == "review_busy")
    assert busy["reason"] == "delib_human" and busy["limit"] == 4


def test_리스크_앱이_띄운_패널만_돌면_동시_수는_8_이다(monkeypatch):
    """#26 — 쟁점 패널(started_by=app)은 delib_active 에는 들지만 관문의 절반 판정에는 들지 않는다."""
    monkeypatch.setattr(a, "_delib_load", lambda: (2, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    llm = FakeLLM([Act("slow", delay=0.2)])
    app = _app(llm)
    outs = _concurrent(app, 12, _verdict(2))
    assert sum(1 for f in outs if _last(f)[0] == "result") == 8
    assert cr.gate_of(app).snapshot()["limit_reason"] == "normal"


def test_비우기_중이면_1초_안에_review_busy_draining_이고_LLM_을_부르지_않는다(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    llm = FakeLLM(["oracle"])
    app = _app(llm)
    cr.gate_of(app).drain.set_on(scope="all", by="update-all", ttl_s=600)
    t0 = time.monotonic()
    fr, _llm, _ = _go(_verdict(2), None, app=app, llm=llm)
    assert time.monotonic() - t0 < 1.0 and llm.calls == []
    assert _names(fr) == ["error", "done"]
    err = fr[0][1]
    assert (err["code"], err["reason"], err["retry_after_s"], err["limit"], err["infra"]) == (
        "review_busy", "draining", 60, 0, True) and "비우기" in err["message"]
    cr.gate_of(app).drain.set_on(scope="panels", by="update-all", ttl_s=600)
    fr, _llm, _ = _go(_verdict(2), None, app=app, llm=llm)
    assert _last(fr)[0] == "result", "비우기 panels 는 카드 대조를 막지 않는다"


def test_자리가_없으면_1초_안에_error_done_이고_LLM_을_부르지_않는다(monkeypatch):
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    llm = FakeLLM(["oracle"])
    app = _app(llm)
    gate = cr.gate_of(app)
    held = [gate.try_acquire("x") for _ in range(8)]
    t0 = time.monotonic()
    fr, _llm, _ = _go(_verdict(2), None, app=app, llm=llm)
    assert time.monotonic() - t0 < 1.0
    assert _names(fr) == ["error", "done"] and fr[0][1]["code"] == "review_busy" and llm.calls == []
    for t in held:
        gate.release(t, "ok")


def test_카드_대조만의_출력_상한은_그_호출에만_건다(monkeypatch):
    """CARD_REVIEW_MAX_TOKENS(M3.md 4.6) — run 이 llm.bind(max_tokens=…) 로 그 호출에만 건다. 심의 LLM 인스턴스의 상한은
    그대로다(심의 의장·좌석 예산을 움직이지 않는다). 없으면 묶지 않는다."""
    monkeypatch.setenv("CARD_REVIEW_MAX_TOKENS", "32768")
    llm = FakeLLM(["oracle"], max_tokens=4_096)
    fr, llm, _ = _go(_verdict(2), None, llm=llm)
    assert _last(fr)[0] == "result"
    assert llm.calls[0]["bound"] == {"max_tokens": 32_768} and llm.max_tokens == 4_096
    monkeypatch.delenv("CARD_REVIEW_MAX_TOKENS")
    fr, llm2, _ = _go(_verdict(2), ["oracle"])
    assert _last(fr)[0] == "result" and "bound" not in llm2.calls[0]


@pytest.mark.parametrize("env,delib_mt,want", [({"CARD_REVIEW_MAX_TOKENS": "32768"}, 8_192, (32_768, "card_review")),
                                               ({}, 8_192, (8_192, "delib")), ({}, None, (None, "none"))])
def test_limits_now_는_출력_상한의_출처를_싣는다(monkeypatch, env, delib_mt, want):
    monkeypatch.delenv("CARD_REVIEW_MAX_TOKENS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(a, "_model_context_state", lambda: {"tokens": 1_048_576, "source": "server", "retrying": False})
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    lim = asyncio.run(cr.limits_now(_app(FakeLLM(["oracle"], max_tokens=delib_mt))))
    assert (lim["llm_max_tokens"], lim["max_tokens_source"]) == want


# ── ping · 취소 ──────────────────────────────────────────────────────────────
def test_LLM_이_도는_동안_ping_이_흐른다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.05)
    fr, _llm, _ = _go(_verdict(2), [Act("slow", delay=0.3)])
    pings = [d for e, d in fr if e == "ping"]
    assert len(pings) >= 3, f"0.3초 동안 0.05초 간격인데 ping 이 {len(pings)}번"
    assert all(set(p) == {"idle_s", "ts"} for p in pings)
    names = [e for e, _ in fr]
    assert names[0] == "status" and names[-2:] == ["result", "done"]


def test_heartbeat_0_이면_ping_없이_기다린다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    fr, _llm, _ = _go(_verdict(2), [Act("slow", delay=0.1)])
    assert [e for e, _ in fr] == ["status", "result", "done"]


def test_구독을_닫으면_LLM_태스크가_취소되고_자리가_돌아온다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.02)
    llm = FakeLLM([Act("slow", delay=5)])
    app = _app(llm)

    async def go():
        gen = cr.run(app, _verdict(2))
        got = [await gen.__anext__(), await gen.__anext__()]       # status, ping — LLM 이 도는 중이다
        assert app.state.review_gate.active == 1 and llm.active == 1
        await gen.aclose()
        await asyncio.sleep(0)
        return got

    got = asyncio.run(go())
    assert _frames(got)[0][0] == "status"
    assert llm.cancelled == 1 and llm.active == 0, "닫힌 구독의 LLM 호출이 계속 돈다"
    g = app.state.review_gate
    assert g.active == 0 and g.totals["cancelled"] == 1
    assert g.totals["tokens_unknown_calls"] == 1, "도중에 접은 LLM 호출의 사용량을 0 토큰으로 셌다"


def test_소비자가_취소돼도_LLM_을_접는다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    llm = FakeLLM([Act("slow", delay=5)])
    app = _app(llm)

    async def go():
        async def consume():
            return [c async for c in cr.run(app, _verdict(2))]

        t = asyncio.ensure_future(consume())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if llm.active:
                break
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(go())
    assert llm.cancelled == 1 and app.state.review_gate.active == 0


def test_카드_대조는_심의_수를_올리지_않는다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    llm = FakeLLM([Act("slow", delay=0.2)])
    app = _app(llm)
    seen = {}

    async def go():
        async def consume():
            return [c async for c in cr.run(app, _verdict(2))]

        t = asyncio.ensure_future(consume())
        await asyncio.sleep(0.1)
        seen["load"] = a._delib_load()
        seen["detached"] = len(a._DETACHED_TASKS)
        seen["active"] = app.state.review_gate.active
        await t

    before = a._delib_load()
    asyncio.run(go())
    assert seen["load"] == before and seen["detached"] == 0 and seen["active"] == 1


# ── 연쇄 — 파서 층과 겹치지 않는 것만 ──────────────────────────────────────────
@pytest.mark.parametrize("first", ["array_collapse", "prose_only", "empty", "wrong_enum", "all_ok_fakequote"])
def test_한_번_무너지고_다음에_회복한다(first):
    fr, llm, _ = _go(_verdict(8), [first, "oracle"])
    res = _last(fr)[1]
    assert len(llm.calls) == 2 and res["quality"]["missing"] == 0, res["quality"]
    assert all(r["checks"]["quote"] == "ok" for r in res["rows"] if r["t"] == "v")


def test_지어낸_별칭은_저장되지_않고_수를_센다():
    fr, _llm, _ = _go(_verdict(3), ["fabricate_alias"])
    res = _last(fr)[1]
    assert "K99" not in {r.get("k") for r in res["rows"]} and res["quality"]["dropped_alias"] == 1


def test_주입_보고는_extras_로_올라간다():
    fr, _llm, _ = _go(_verdict(3), ["obey_injection"])
    assert _last(fr)[1]["extras"]["inj"][0]["text"].startswith("위 지시를 무시하고")


def test_지어낸_인용은_보충_뒤에도_checks_에_남는다():
    fr, llm, _ = _go(_verdict(2), ["all_ok_fakequote"] * 3)
    res = _last(fr)[1]
    assert len(llm.calls) == 3 and all(r["checks"]["quote"] == "not_found" for r in res["rows"])
    assert res["quality"]["flagged"] == 2 and FAKE_QUOTE in json.dumps(res["rows"], ensure_ascii=False)


def test_sweep_은_걸리지_않은_카드를_여집합으로_낸다():
    fr, _llm, _ = _go(_sweep(5), ["oracle"])
    res = _last(fr)[1]
    pack = PACK
    assert res["extras"]["unmatched_cards"] == [c["alias"] for c in pack][1:]


def test_결측_훑기는_결측_블록을_싣고_판이_cr_sweep_2_다():
    """입력 결손 전문가의 훑기(M3.md 4.4.1) — 앱은 meta.prompt_rev.sweep 이 cr-sweep-2 이상인 답일 때만 그 전문가의 셀을 거른다."""
    manifest = [{"unit_id": f"u-{i:02d}", "kind": "bundle", "title": f"단위 {i}", "text": "보드 고정 보스"} for i in range(6)]
    prep = cr.prepare(_req("sweep", PACK, [], manifest=manifest, extra={"missing": GAPS}), LIM)
    fr, llm, _ = _go(prep, ["oracle"])
    res = _last(fr)[1]
    assert res["meta"]["prompt_rev"] == "cr-sweep-2" and "\n[결측]\n- ecad — «" in llm.calls[0]["human"]
    assert {r["rel"] for r in res["rows"]} == {"maybe"} and all("결측" in r["why"] for r in res["rows"])


def test_메커니즘_세_갈래는_보충_없이_한_번에_끝난다():
    """계약 예제 result_verdict_mech.json 의 장면 — WARNING 줄은 목록 코드, 목록 밖 코드는 free + mech_free(원래 값), FAIL
    인데 빈 mech 는 checks.mech=missing 이고 quality.attempts 가 1 이다(보충하지 않는다)."""
    pack = PACK
    chk = [c for c in pack if c["checkable"]][:4]
    mechs = [{"code": "mechanical.drop_stress", "label": "낙하 응력"}, {"code": "interface.seal_loss", "label": "실링"}]
    prep = cr.prepare(_req("verdict", chk, _digest(pack, [c["alias"] for c in chk]), unit=_unit(0), variant="fwd",
                           extra={"mechanisms": mechs}), LIM)
    fr, llm, _ = _go(prep, ["mech_mix"])
    res = _last(fr)[1]
    assert res["quality"]["attempts"] == 1 and len(llm.calls) == 1 and res["meta"]["prompt_rev"] == "cr-verdict-2"
    got = [(r["judgement"], r["mech"], r["mech_free"], r["checks"]["mech"]) for r in res["rows"]]
    assert got[:3] == [("WARNING", "mechanical.drop_stress", "", "ok"), ("FAIL", "free", "not.in.taxonomy", "free"),
                       ("FAIL", "", "", "missing")] and got[3][3] == "ok"


def _noinput(pack=PACK, unit=None, extra=None):
    return cr.prepare(_req("noinput", pack, _digest(pack), unit=unit or _unit(0),
                           extra=extra or {"mechanisms": [{"code": "pcb.board_flex", "label": "보드 휨"}]}), LIM)


def test_입력_결손_정답은_한_번에_끝나고_결과_판과_basis_가_noinput_이다():
    fr, llm, _ = _go(_noinput(), ["oracle"])
    assert _names(fr) == ["status", "result", "done"]
    res = _last(fr)[1]
    q1 = [r for r in res["rows"] if r["t"] == "q1"]
    q2 = [r for r in res["rows"] if r["t"] == "q2"]
    assert [r["impact"] for r in q1] == ["yes", "yes"] and len(q2) == 2 and res["rows"][-1] == {
        **res["rows"][-1], "t": "more", "more": "no"}
    assert res["meta"]["basis"] == "noinput" and res["meta"]["prompt_rev"] == "cr-noinput-1"
    assert res["quality"]["maybe_more"] is False and res["quality"]["expected"] == 3 and res["missing"] == []
    assert all(r["mech"] == "pcb.board_flex" and r["checks"]["quote"] == "ok" for r in q1)


def test_입력_결손_보충은_빠진_more_와_틀린_인용만_다시_묻는다():
    """계약 예제 result_noinput_repaired 의 장면 — 첫 답에 more 줄이 없고 인용 하나가 원문에 없다 → 둘째 프롬프트의 지적
    블록에 그 둘만 있고 attempts 2."""
    fr, llm, _ = _go(_noinput(), ["noinput_flawed", "oracle"])
    res = _last(fr)[1]
    assert res["quality"]["attempts"] == 2 and res["missing"] == []
    hint = llm.calls[1]["human"].split("(보충 2/3")[1]
    assert "more 줄(더 있나)" in hint and "K01" in hint and "q2 줄" not in hint and "K02" not in hint
    q1 = [r for r in res["rows"] if r["t"] == "q1"]
    assert len(q1) == 2 and all(r["checks"]["quote"] == "ok" for r in q1) and FAKE_QUOTE not in json.dumps(q1,
                                                                                                         ensure_ascii=False)


def test_입력_결손_영향이_상한이면_앞의_것을_남기고_더_있을_수_있다():
    """계약 예제 result_noinput_capped 의 장면."""
    fr, _llm, _ = _go(_noinput(), ["noinput_many"])
    res = _last(fr)[1]
    n = LIM["noinput"]["items_max"]
    assert sum(1 for r in res["rows"] if r["t"] == "q1") == n
    assert res["quality"]["dropped_over_cap"] == 2 and res["quality"]["maybe_more"] is True


def test_카드_0장_전문가의_입력_결손은_역할만으로_unknown_을_낸다():
    prep = cr.prepare(_req("noinput", [], [], unit=_unit(0), extra={"mechanisms": [{"code": "pcb.board_flex"}]}), LIM)
    fr, _llm, _ = _go(prep, ["oracle"])
    res = _last(fr)[1]
    assert [r["impact"] for r in res["rows"] if r["t"] == "q1"] == ["unknown"] and res["meta"]["basis"] == "noinput"


def test_offcard_결과():
    fr, _llm, _ = _go(_offcard(), ["oracle"])
    res = _last(fr)[1]
    assert {r["t"] for r in res["rows"]} == {"f", "m", "more"} and res["quality"]["maybe_more"] is False
    assert res["meta"]["bg_dropped"] == 0


def test_보충을_거쳐도_제목이_빈_카드_밖_줄_둘이_둘_다_남는다():
    """RB-1 재현 그대로 — 1차 답은 제목이 빈 f 줄 둘(경로 A·B)과 m 줄(more 줄이 빠져 보충이 돈다), 2차 답은 more 줄 하나.
    옛 실행기는 [('경로 B', 1), ('경로 B', 1)] 을 냈다(앞 행이 사라지고 남은 행이 두 번, dup 0)."""
    f1 = {"t": "f", "dir": "risk", "title": "", "mech": "free", "mech_free": "A", "path": "경로 A", "l": ["L01"],
          "src": "exp", "q": "", "sev": "중대", "conf": "medium", "check_kind": "test", "check": "시험 A"}
    f2 = {**f1, "mech_free": "B", "path": "경로 B", "check": "시험 B"}
    first = "\n".join(json.dumps(r, ensure_ascii=False) for r in (f1, f2, {"t": "m", "seen": []}))
    fr, llm, _ = _go(_offcard(), [Act(text=first), Act(text=json.dumps({"t": "more", "more": "no"}))])
    res = _last(fr)[1]
    assert len(llm.calls) == 2 and res["quality"]["attempts"] == 2
    assert [(r["path"], r["from_attempt"]) for r in res["rows"] if r["t"] == "f"] == [("경로 A", 1), ("경로 B", 1)]
    assert res["quality"]["dup"] == 0 and res["missing"] == []


def test_한_줄_배열의_깨진_카드_밖_줄은_보충으로_되찾는다():
    """RB-2 재현 — 답 한 줄 [f(계면 박리), f(열 변형 — path 안 따옴표가 깨짐), m, more:no]. 옛 실행기는 bad_lines 0 · 보충 없이
    '열 변형' 을 잃었다. 이제 보충이 돌고 지적 블록이 그 원소(1번째 줄의 2번째 객체, 머리)를 가리킨다."""
    f = lambda t, p: {"t": "f", "dir": "risk", "title": t, "mech": "free", "mech_free": "x", "path": p,  # noqa: E731
                      "l": ["L01"], "src": "exp", "q": "", "sev": "중대", "conf": "medium", "check_kind": "test",
                      "check": "시험"}
    parts = [json.dumps(r, ensure_ascii=False) for r in
             (f("계면 박리", "경로 A"), f("열 변형", "경로 B"), {"t": "m", "seen": []}, {"t": "more", "more": "no"})]
    parts[1] = parts[1].replace('"path": "경로 B"', '"path": "그는 "경로 B" 라 했다"')
    fixed = json.dumps(f("열 변형", "경로 B"), ensure_ascii=False)
    fr, llm, _ = _go(_offcard(), [Act(text="[" + ",".join(parts) + "]"), Act(text=fixed)])
    res = _last(fr)[1]
    assert len(llm.calls) == 2, "깨진 원소가 있으면 보충이 돌아야 한다"
    hint = llm.calls[1]["human"].split("(보충 ", 1)[1]
    assert "읽히지 않은 줄 1개" in hint and "1번째 줄의 2번째 객체 «" in hint and "열 변형" in hint
    assert [r["title"] for r in res["rows"] if r["t"] == "f"] == ["계면 박리", "열 변형"]
    assert (res["quality"]["bad_lines"], res["quality"]["tail_broken"]) == (1, False)


def test_solo_결과():
    fr, _llm, _ = _go(_verdict(1, variant="solo"), ["oracle"])
    res = _last(fr)[1]
    assert [r["t"] for r in res["rows"]] == ["v", "c"] and res["variant"] == "solo"


# ── 스트림 전 거절(prepare) ───────────────────────────────────────────────────
def test_판이_다르면_409():
    pack = PACK
    req = _req("verdict", [c for c in pack if c["checkable"]][:1], [], unit=_unit(0), variant="fwd")
    cr.prepare({**req, "expect": {"prompt_rev": cr.PROMPT_REV["verdict"], "limits_rev": LIM["limits_rev"]}}, LIM)
    for exp in ({"prompt_rev": "cr-verdict-0"}, {"limits_rev": "deadbeef"}):
        with pytest.raises(cr.RequestError) as e:
            cr.prepare({**req, "expect": exp}, LIM)
        assert (e.value.status, e.value.code) == (409, "rev_mismatch")
        assert e.value.body()["limits_rev"] == LIM["limits_rev"]


def test_창이_모자라면_ctx_too_small_이고_종류를_먼저_본다():
    small = cr.limits(4_096, "server", 0, env={})
    assert not all(small["runnable"].values())
    pack = PACK
    req = _req("verdict", [c for c in pack if c["checkable"]][:1], [], unit=_unit(0), variant="fwd")
    with pytest.raises(cr.RequestError) as e:
        cr.prepare(req, small)
    assert e.value.code == "ctx_too_small" and e.value.body()["need_ctx_tokens"]
    with pytest.raises(cr.RequestError) as e:
        cr.prepare({**req, "kind": "cross"}, small)
    assert e.value.code == "unsupported_kind"


def test_엔진_내부_오류도_끝_프레임으로_닫는다(monkeypatch, capsys):
    """파싱·검사 쪽 결함이 스트림을 끝 프레임 없이 끊으면 앱은 그것을 인프라 끊김으로 보고 차감 없이 영영 다시 낸다."""
    def boom(*_a, **_k):
        raise KeyError("x")

    monkeypatch.setattr(cr, "check_rows", boom)
    fr, _llm, app = _go(_verdict(2), ["oracle"])
    assert _names(fr) == ["status", "error", "done"]
    err = _last(fr)[1]
    assert (err["code"], err["infra"], err["retryable"]) == ("internal_error", False, False)
    g = app.state.review_gate
    assert g.active == 0 and g.totals["error"] == 1 and g.last_error.startswith("internal_error")


def test_결과의_quality_가_갈린_판정과_뒤집힘을_센다():
    """RB3-1 — 같은 카드의 판정이 갈렸으면 걸린 줄을 남기고 quality.dup_conflict 로, 보충이 판정을 바꿔 갈아 끼웠으면
    quality.flipped 로 센다(행의 checks.dup_conflict·flipped_from 의 수). 앱은 이 둘을 사람이 볼 표지로 둔다."""
    prep = _verdict(2)
    ctx = prep["ctx"]
    a, b = ctx["judge"]

    def v(k, judgement, cq=None):
        return json.dumps({"t": "v", "k": k, "applies": "yes" if judgement != "na" else "no", "judgement": judgement,
                           "cq": cq or ctx["cards"][k]["text"][:30], "l": ["L01"], "why": "변경 줄과 카드를 맞대었다",
                           "need": ""}, ensure_ascii=False)

    first = "\n".join([v(a, "FAIL"), v(a, "OK"), v(b, "OK", cq=FAKE_QUOTE)])
    fr, llm, _app_ = _go(prep, [Act(text=first), Act(text=v(b, "FAIL"))])
    e, res = _last(fr)
    assert e == "result" and len(llm.calls) == 2, (e, res)
    by = {r["k"]: r for r in res["rows"]}
    assert by[a]["judgement"] == "FAIL" and by[a]["checks"]["dup_conflict"] is True
    assert by[b]["judgement"] == "FAIL" and tuple(by[b]["flipped_from"]["verdict"]) == ("yes", "OK"), by[b]
    assert (res["quality"]["dup_conflict"], res["quality"]["flipped"]) == (1, 1), res["quality"]

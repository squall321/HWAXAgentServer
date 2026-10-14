# 카드 대조 다섯 종류(sweep·verdict·offcard·noinput·summary)가 운영 봉투를 똑같이 지키는지 한 파일로 못박는다 — 심의 수에 안 들고, 끊기면 LLM 을 접고, 자리가 없거나 비우기면 1초 안에 끝나고, 조용한 동안 ping 을 내고, 모르는 사용량은 null 이다
#
# M3 E3-10(WP5a A3). 종류가 늘 때마다 같은 봉투를 다시 확인하지 않으면 새 종류 하나가 delib_active 를 올려 update-all 의 재기동을
# 막거나(카드 대조는 재기동을 막지 않는다 — C-25), 끊긴 구독의 LLM 을 끝까지 돌리거나, 비우기 중에 새 호출을 받는다.
#   ① 도는 동안 delib_active 0 · review_active 1
#   ② 구독을 끊으면 LLM 태스크가 cancelled() 이고 review_active 0 — 카드 밖 호출은 검색 단계에서 끊어도 같다
#   ③ 자리 없음(관문이 찼다)·비우기(scope all)는 1초 안에 error → done 이고 LLM 을 부르지 않는다
#   ④ heartbeat 의 세 배 동안 조용하면 ping 이 둘 이상
#   ⑤ LLM 이 사용량을 주지 않으면 meta.usage 는 null 이다(0 토큰으로 치지 않는다)
# 요청은 실제 생산자(지어낸 카드 → 실제 build_pack 의 정규화 → 실제 build_prompt)로 만들고 실행기는 실제 run() 이다.
#
#   실행:  .venv/bin/python -m pytest tests/test_card_review_ops_contract.py -q
import asyncio
import json
import random
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
from fakes.llm import Act, FakeLLM  # noqa: E402
from test_card_review_prompts import GAPS, MECHS, _digest, _pack, _req, _unit  # noqa: E402

LIM = cr.limits(1_048_576, "server", 0, env={}, model="fake-glm")
PACK = _pack(random.Random(1), n=4, size=(300, 600))
KINDS = ("sweep", "verdict", "offcard", "noinput", "summary")


def _request(kind: str) -> dict:
    chk = [c for c in PACK if c["checkable"]]
    bg = [c for c in PACK if not c["checkable"]]
    if kind == "sweep":
        manifest = [{"unit_id": f"u-{i:02d}", "kind": "bundle", "title": f"단위 {i}", "text": "보스 위치"} for i in range(6)]
        return _req("sweep", PACK, [], manifest=manifest, extra={"missing": GAPS})
    if kind == "verdict":
        return _req("verdict", chk[:3], _digest(PACK, [c["alias"] for c in chk[:3]]), unit=_unit(0), variant="fwd",
                    extra={"mechanisms": MECHS[:5]})
    if kind == "offcard":
        return _req("offcard", bg, _digest(PACK, [c["alias"] for c in bg]), unit=_unit(0))
    if kind == "noinput":
        return _req("noinput", PACK, _digest(PACK), unit=_unit(0), extra={"mechanisms": MECHS[:5]})
    rows = [{"ref": f"reg:{i:012x}", "text": f"중대/WARNING · mechanical.drop_stress · «부품{i}»"} for i in range(8)]
    return {"kind": "summary", "variant": None, "target_key": "t", "expert": None,
            "summary": {"scope": {"kind": "domain", "key": "mech", "label": "기구"}, "rows": rows,
                        "must_cover": [r["ref"] for r in rows[:2]], "blocks": []}}


def _app(llm):
    return SimpleNamespace(state=SimpleNamespace(delib_llm=llm))


@pytest.fixture(autouse=True)
def _no_delib(monkeypatch):
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)


class _Probe(FakeLLM):
    """LLM 이 도는 순간의 관문·심의 수를 적는 대본 LLM."""

    def __init__(self, app_ref, *a_, **k):
        super().__init__(*a_, **k)
        self.app_ref, self.during = app_ref, []

    async def ainvoke(self, msgs, *a_, **k):
        self.during.append((cr.gate_of(self.app_ref["app"]).active, a._delib_load()[0]))
        return await super().ainvoke(msgs, *a_, **k)


def _drain(gen_factory):
    async def go():
        return [delib_jobs._parse_sse(c) async for c in gen_factory()]

    return asyncio.run(go())


# ① ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", KINDS)
def test_도는_동안_심의_수에_들지_않고_review_active_는_1이다(kind):
    ref = {}
    llm = _Probe(ref, ["oracle"])
    app = _app(llm)
    ref["app"] = app
    frames = _drain(lambda: cr.run(app, cr.prepare(_request(kind), LIM)))
    assert frames[-2][0] == "result" and frames[-2][1]["kind"] == kind, frames[-2]
    assert llm.during and all(d == (1, 0) for d in llm.during), llm.during
    assert cr.gate_of(app).active == 0


# ② ──────────────────────────────────────────────────────────────────────────
def _cancel_mid(prep, app, *, nexts=1):
    async def go():
        gen = cr.run(app, prep)
        for _ in range(nexts):
            await gen.__anext__()
        await asyncio.sleep(0.05)
        await gen.aclose()
        await asyncio.sleep(0.05)

    asyncio.run(go())


@pytest.mark.parametrize("kind", KINDS)
def test_구독을_끊으면_LLM_을_접고_자리를_돌려준다(kind, monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.02)
    llm = FakeLLM([Act("slow", delay=5.0)])
    app = _app(llm)
    _cancel_mid(cr.prepare(_request(kind), LIM), app, nexts=2)       # status → ping(LLM 이 도는 중) → 끊기
    assert llm.cancelled == 1 and llm.active == 0, (llm.cancelled, llm.active)
    g = cr.gate_of(app)
    assert g.active == 0 and g.totals["cancelled"] == 1


def test_카드_밖_호출은_검색_단계에서_끊어도_검색을_접고_자리를_돌려준다(monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.02)
    state = {"cancelled": 0}

    class Slow:
        async def ainvoke(self, _args):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                state["cancelled"] += 1
                raise

    async def tools_by_name(*_a, **_k):
        return {"agent_search": Slow()}

    monkeypatch.setattr(cr, "_tools_by_name", tools_by_name)
    req = {**_req("offcard", [], [], unit=_unit(0), extra={"search": {"queries": ["보스 간극"]}}),
           "user_pat": "pat", "user_email": "u@x.com", "groups": []}
    llm = FakeLLM(["oracle"])
    app = _app(llm)
    _cancel_mid(cr.prepare(req, LIM), app, nexts=2)                  # status(search) → ping → 끊기
    assert state["cancelled"] == 1 and llm.calls == []
    assert cr.gate_of(app).active == 0 and cr.gate_of(app).totals["cancelled"] == 1


# ③ ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("why", ["busy", "draining"])
def test_자리가_없거나_비우기면_1초_안에_error_done_이고_LLM_을_부르지_않는다(kind, why, monkeypatch, tmp_path):
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    llm = FakeLLM(["oracle"])
    app = _app(llm)
    gate = cr.gate_of(app)
    held = [gate.try_acquire("x") for _ in range(8)] if why == "busy" else []
    if why == "draining":
        gate.drain.set_on(scope="all", by="update-all", ttl_s=60)
    t0 = time.monotonic()
    frames = _drain(lambda: cr.run(app, cr.prepare(_request(kind), LIM)))
    for t in held:
        gate.release(t, "ok")
    assert time.monotonic() - t0 < 1.0 and llm.calls == []
    assert [e for e, _ in frames] == ["error", "done"]
    err = frames[0][1]
    assert err["code"] == "review_busy" and err["reason"] == ("normal" if why == "busy" else "draining")
    assert err["retry_after_s"] == (30 if why == "busy" else 60) and (err["infra"], err["retryable"]) == (True, True)


# ④ ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", KINDS)
def test_조용한_동안_ping_을_낸다(kind, monkeypatch):
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.05)
    frames = _drain(lambda: cr.run(_app(FakeLLM([Act("slow", delay=0.2)])), cr.prepare(_request(kind), LIM)))
    pings = [d for e, d in frames if e == "ping"]
    assert len(pings) >= 2 and all(set(p) == {"idle_s", "ts"} for p in pings), pings
    assert frames[-2][0] == "result"


# ⑤ ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", KINDS)
def test_사용량을_모르면_null_이다(kind):
    frames = _drain(lambda: cr.run(_app(FakeLLM(["oracle"], usage=False)), cr.prepare(_request(kind), LIM)))
    res = frames[-2][1]
    assert res["meta"]["usage"] is None, json.dumps(res["meta"], ensure_ascii=False)[:300]


# ⑥ ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", KINDS)
def test_자리를_잡은_뒤_무엇이_터져도_자리를_돌려준다(kind, monkeypatch):
    """RB3-9 — try_acquire 와 try: 사이(llm.bind·손잡이 읽기)에서 터지면 자리가 샜다. 이제 끝 프레임을 내고 자리를 돌려준다."""
    monkeypatch.setenv("CARD_REVIEW_MAX_TOKENS", "4096")

    class BadBind(FakeLLM):
        def bind(self, **_kw):
            raise RuntimeError("bind 실패")

    app = _app(BadBind(["oracle"]))
    try:
        frames = _drain(lambda: cr.run(app, cr.prepare(_request(kind), LIM)))
    except RuntimeError as exc:            # 스트림 밖으로 샌 예외 — 끝 프레임이 없었다
        frames = [("raised", str(exc))]
    assert [e for e, _ in frames] == ["error", "done"] and frames[0][1]["code"] == "internal_error", frames
    g = cr.gate_of(app)
    assert g.active == 0 and g.totals["error"] == 1, (g.active, g.totals)

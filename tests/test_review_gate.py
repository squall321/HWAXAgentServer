# 카드 대조 관문(review_gate)이 줄을 세우지 않고 상한에서 곧바로 '바쁨' 을 돌려주며, 심의가 돌면 상한을 낮추고, 끝을 빠짐없이 세는지 본다
#
# 관문은 수만 센다(asyncio 객체가 없다) — 처음 쓴 이벤트 루프에 묶이지 않고, 획득·반납이 await 없이 끝난다. 자리를 두 번
# 돌려줘도 한 번만 세야 한다(제너레이터의 finally 가 두 길로 올 수 있다). 사용량을 모르는 호출은 0 토큰이 아니라 '모름'
# 으로 센다.
#
#   실행:  .venv/bin/python -m pytest tests/test_review_gate.py -q
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import review_gate as rg  # noqa: E402


def test_손잡이_기본값과_잘못된_값():
    """D-34 — 평소 8, 심의가 돌면 절반(max(1, 평소 // 2)) = 4."""
    assert rg.knobs({}) == {"concurrency": 8, "with_delib": 4, "retry_after_s": 30}
    bad = {"CARD_REVIEW_CONCURRENCY": "넷", "CARD_REVIEW_CONCURRENCY_WITH_DELIB": "0", "CARD_REVIEW_RETRY_AFTER_S": "-1"}
    assert rg.knobs(bad) == {"concurrency": 8, "with_delib": 4, "retry_after_s": 30}
    # 심의 중 상한이 평소보다 크면 평소 값으로 묶는다(심의가 돌 때 더 받는 설정은 뜻이 거꾸로다).
    assert rg.knobs({"CARD_REVIEW_CONCURRENCY": "3", "CARD_REVIEW_CONCURRENCY_WITH_DELIB": "5"})["with_delib"] == 3
    # 평소 값을 낮추면 심의 중 기본값(절반)도 따라 내려간다. 평소 1 이면 1 이다.
    assert rg.knobs({"CARD_REVIEW_CONCURRENCY": "6"})["with_delib"] == 3
    assert rg.knobs({"CARD_REVIEW_CONCURRENCY": "1"})["with_delib"] == 1
    # 적으면 그 값이다.
    assert rg.knobs({"CARD_REVIEW_CONCURRENCY_WITH_DELIB": "6"})["with_delib"] == 6
    # engine-ops E3-7 수용 기준 — WITH_DELIB=6, CONC=4 면 4.
    assert rg.knobs({"CARD_REVIEW_CONCURRENCY": "4", "CARD_REVIEW_CONCURRENCY_WITH_DELIB": "6"})["with_delib"] == 4


def test_상한에서_기다리지_않고_바쁨():
    g = rg.ReviewGate(lambda: (2, "normal"), retry_after_s=30)
    t1, t2 = g.try_acquire("a"), g.try_acquire("b")
    busy = g.try_acquire("c")
    assert isinstance(t1, rg.Ticket) and isinstance(t2, rg.Ticket)
    assert isinstance(busy, rg.Busy) and (busy.limit, busy.active, busy.retry_after_s, busy.reason) == (2, 2, 30, "normal")
    g.release(t1, "ok")
    assert isinstance(g.try_acquire("d"), rg.Ticket), "자리가 났는데 다시 잡지 못했다"
    assert g.totals["busy"] == 1 and g.totals["calls"] == 3


def test_심의가_돌면_상한이_내려가고_도는_호출은_빼앗지_않는다():
    running = {"n": 0}
    g = rg.from_env(lambda: running["n"], env={})
    held = [g.try_acquire(str(i)) for i in range(8)]
    assert all(isinstance(t, rg.Ticket) for t in held)
    assert g.snapshot()["limit"] == 8 and g.snapshot()["limit_reason"] == "normal"
    running["n"] = 1
    snap = g.snapshot()
    assert (snap["limit"], snap["limit_reason"], snap["active"], snap["delib_human"]) == (4, "delib_human", 8, 1)
    for t in held[:4]:
        g.release(t, "ok")
    assert isinstance(g.try_acquire("x"), rg.Busy), "심의 중 상한 4 인데 다섯째 자리를 줬다"
    g.release(held[4], "ok")
    assert isinstance(g.try_acquire("y"), rg.Ticket)


def test_두_번_돌려줘도_한_번만_센다():
    g = rg.ReviewGate(lambda: (4, "normal"))
    t = g.try_acquire("a")
    g.release(t, "ok")
    g.release(t, "cancelled")
    assert g.active == 0 and g.totals["ok"] == 1 and g.totals["cancelled"] == 0


def test_끝을_센다_오류_사용량_모름():
    g = rg.ReviewGate(lambda: (4, "normal"))
    g.release(g.try_acquire("a"), "ok", {"tokens_in": 100, "tokens_out": 20}, repaired=True)
    g.release(g.try_acquire("b"), "context_overflow", None, unknown_calls=1)
    g.release(g.try_acquire("c"), "ok", {"tokens_in": 5, "tokens_out": 1}, unknown_calls=1, truncated=True)
    g.release(g.try_acquire("d"), "cancelled")
    g.note("over_budget")
    t = g.totals
    assert (t["calls"], t["ok"], t["error"], t["cancelled"]) == (4, 2, 1, 1)
    assert (t["context_overflow"], t["over_budget"], t["truncated"], t["repaired"]) == (1, 1, 1, 1)
    assert (t["tokens_in"], t["tokens_out"], t["tokens_unknown_calls"]) == (105, 21, 2)
    assert g.last_error.startswith("context_overflow @ ")
    assert set(g.snapshot()["totals"]) == set(rg.TOTAL_KEYS)


def test_상한을_못_읽으면_가장_좁게_연다():
    def boom():
        raise RuntimeError("x")

    g = rg.ReviewGate(boom)
    assert isinstance(g.try_acquire("a"), rg.Ticket)
    busy = g.try_acquire("b")
    assert isinstance(busy, rg.Busy) and busy.limit == 1 and busy.reason == "limit_error:RuntimeError"


# ── M3 — 상한 표(8/4/0)와 비우기 깃발 ─────────────────────────────────────────
@pytest.mark.parametrize("conc,with_delib,human,scope,want", [
    (8, 4, 0, None, (8, "normal")),
    (8, 4, 1, None, (4, "delib_human")),
    (8, 4, 3, "panels", (4, "delib_human")),
    (8, 4, 0, "panels", (8, "normal")),          # 비우기 panels 는 카드 대조를 막지 않는다
    (8, 4, 0, "all", (0, "draining")),
    (8, 4, 2, "all", (0, "draining")),           # 비우기가 이긴다
])
def test_limit_for_표(conc, with_delib, human, scope, want):
    assert rg.limit_for(conc, with_delib, human, scope) == want


def test_관문은_비우기일_때만_0_을_내고_자리_없음의_대기는_60초다(tmp_path):
    clock = {"t": 1_000.0}
    d = rg.DrainState(str(tmp_path / "drain.json"), clock=lambda: clock["t"], delib_load=lambda: (0, 0))
    g = rg.from_env(lambda: 0, env={}, drain=d)
    held = g.try_acquire("a")
    d.set_on(scope="all", by="update-all", ttl_s=60)
    snap = g.snapshot()
    assert (snap["limit"], snap["limit_reason"], snap["active"]) == (0, "draining", 1), "도는 자리는 빼앗지 않는다"
    busy = g.try_acquire("b")
    assert isinstance(busy, rg.Busy) and (busy.limit, busy.reason, busy.retry_after_s) == (0, "draining", 60)
    g.release(held, "ok")
    d.set_on(scope="panels", by="update-all", ttl_s=60)
    assert isinstance(g.try_acquire("c"), rg.Ticket), "비우기 panels 인데 카드 대조를 막았다"
    clock["t"] += 61
    assert d.snapshot()["on"] is False and g.snapshot()["limit"] == 8
    # 손잡이 실수로 상한 함수가 0 을 내도 관문은 닫히지 않는다(0 은 draining 사유일 때만).
    assert rg.ReviewGate(lambda: (0, "normal")).limit() == (1, "normal")


def _drain(tmp_path, delib=(0, 0)):
    clock = {"t": 10_000.0}
    load = {"v": delib}
    d = rg.DrainState(str(tmp_path / "drain.json"), clock=lambda: clock["t"], delib_load=lambda: load["v"])
    return d, clock, load


def test_깃발_수명은_늘리기만_하고_수명_뒤에_풀린다(tmp_path):
    d, clock, _ = _drain(tmp_path)
    assert d.snapshot() == rg.OFF
    s1 = d.set_on(scope="all", by="update-all", reason="§2", ttl_s=1_800)
    assert (s1["on"], s1["scope"], s1["until"], s1["by"], s1["set_at"]) == (True, "all", 11_800.0, "update-all", 10_000.0)
    clock["t"] += 100
    assert d.set_on(scope="all", by="update-all", ttl_s=60)["until"] == 11_800.0, "짧은 ttl 이 수명을 줄였다"
    assert d.set_on(scope="all", by="update-all", ttl_s=1_800)["until"] == 11_900.0
    clock["t"] = 11_899
    assert d.snapshot()["on"] is True
    clock["t"] = 11_900
    assert d.snapshot()["on"] is False and not (tmp_path / "drain.json").exists(), "수명 뒤에도 파일이 남았다"


def test_사람의_고정은_hold_아닌_요청으로_안_바뀌고_다른_by_의_off_는_409(tmp_path):
    d, clock, _ = _drain(tmp_path)
    d.set_on(scope="all", by="human:koo", reason="점검", hold=True)
    for kw in ({"scope": "all", "by": "update-all", "ttl_s": 1_800},
               {"scope": "panels", "by": "update-all", "until_idle": {"grace_s": 1_800, "max_s": 46_800}}):
        with pytest.raises(rg.DrainError) as e:
            d.set_on(**kw)
        assert (e.value.status, e.value.code) == (409, "drain_held") and e.value.body()["drain"]["hold"] is True
    with pytest.raises(rg.DrainError) as e:
        d.set_off(by="update-all")
    assert e.value.code == "drain_held"
    with pytest.raises(rg.DrainError):
        d.set_off(by="human:koo")                 # 고정은 세운 사람도 force 로만 푼다
    clock["t"] += 10 ** 7
    assert d.snapshot()["hold"] is True, "수명 없는 고정이 시간으로 풀렸다"
    assert d.set_off(by="human:koo", force=True) == rg.OFF and d.snapshot()["on"] is False
    # 다른 주체가 세운 깃발 — off 는 409, force 는 푼다. 같은 주체면 푼다. 꺼진 깃발의 off 는 멱등.
    d.set_on(scope="all", by="update-all", ttl_s=60)
    with pytest.raises(rg.DrainError):
        d.set_off(by="human:koo")
    assert d.set_off(by="update-all")["on"] is False
    assert d.set_off(by="anyone")["on"] is False


def test_until_idle_은_심의가_0_이_된_뒤_grace_에_풀리고_다시_서면_시계를_지운다(tmp_path):
    d, clock, load = _drain(tmp_path, delib=(1, 0))
    d.set_on(scope="panels", by="update-all", until_idle={"grace_s": 100, "max_s": 10_000})
    clock["t"] += 50
    snap = d.snapshot()
    assert snap["on"] and snap["until_idle"]["idle_since"] is None and snap["until_idle"]["max_until"] == 20_000.0
    load["v"] = (0, 0)
    assert d.snapshot()["until_idle"]["idle_since"] == 10_050.0
    clock["t"] += 60
    load["v"] = (0, 2)                            # 줄 선 심의도 심의다 — 시계를 지운다
    assert d.snapshot()["until_idle"]["idle_since"] is None
    load["v"] = (0, 0)
    clock["t"] += 10
    assert d.snapshot()["until_idle"]["idle_since"] == 10_120.0
    clock["t"] = 10_219
    assert d.snapshot()["on"] is True
    clock["t"] = 10_220
    assert d.snapshot()["on"] is False


def test_until_idle_은_max_s_를_넘기지_않는다(tmp_path):
    d, clock, _ = _drain(tmp_path, delib=(1, 0))
    d.set_on(scope="panels", by="update-all", until_idle={"grace_s": 1_800, "max_s": 46_800})
    clock["t"] += 46_799
    assert d.snapshot()["on"] is True
    clock["t"] += 1
    assert d.snapshot()["on"] is False, "심의가 끝나지 않아도 max_s 에서 풀린다"


def test_until_idle_은_ttl_수명을_줄이지_않고_scope_는_마지막_요청의_것이다(tmp_path):
    d, clock, _ = _drain(tmp_path)
    d.set_on(scope="all", by="update-all", ttl_s=1_800)
    s = d.set_on(scope="panels", by="update-all", until_idle={"grace_s": 10, "max_s": 100})
    assert s["scope"] == "panels" and s["until"] == 11_800.0
    clock["t"] += 500                              # 심의 0 · grace·max 지남 — ttl 이 아직 산다
    assert d.snapshot()["on"] is True
    clock["t"] = 11_800
    assert d.snapshot()["on"] is False


def test_깃발은_파일에서_되살아나고_깨진_파일은_꺼짐이다(tmp_path, capsys):
    d, clock, _ = _drain(tmp_path)
    d.set_on(scope="all", by="update-all", ttl_s=600)
    again = rg.DrainState(d.path, clock=lambda: clock["t"], delib_load=lambda: (0, 0))
    assert again.snapshot()["on"] is True and again.active_scope() == "all"
    for junk in ("{not json", "[]", '{"scope": "everything"}', '{"scope": "all", "until": "내일"}',
                 '{"scope": "all", "until_idle": {"grace_s": 1}}'):
        (tmp_path / "drain.json").write_text(junk, encoding="utf-8")
        assert again.snapshot() == rg.OFF, junk
        assert not (tmp_path / "drain.json").exists(), "깨진 파일을 지우지 않았다"
    assert "깨졌다" in capsys.readouterr().out


def test_비우기_요청의_모양(tmp_path):
    d, _, _ = _drain(tmp_path)
    for kw in ({"scope": "some", "by": "x", "ttl_s": 1}, {"scope": "all", "by": "", "ttl_s": 1},
               {"scope": "all", "by": "x"}, {"scope": "all", "by": "x", "ttl_s": 0},
               {"scope": "all", "by": "x", "ttl_s": 1, "until_idle": {"grace_s": 1, "max_s": 2}},
               {"scope": "all", "by": "x", "until_idle": {"grace_s": 5, "max_s": 2}},
               {"scope": "all", "by": "x", "ttl_s": True}):
        with pytest.raises(rg.DrainError) as e:
            d.set_on(**kw)
        assert (e.value.status, e.value.code) == (422, "bad_request"), kw
    assert d.snapshot()["on"] is False, "거절한 요청이 깃발을 세웠다"


def test_깃발_경로는_ARTIFACT_DIR_에서_유도한다():
    assert rg.drain_state_path({"DRAIN_STATE_PATH": "/x/y.json", "ARTIFACT_DIR": "/a"}) == "/x/y.json"
    assert rg.drain_state_path({"ARTIFACT_DIR": "/a"}) == "/a/review-drain.json"
    assert rg.drain_state_path({}).endswith("/artifacts/review-drain.json")


def test_사람_심의_수를_못_세면_모름이다():
    def boom():
        raise RuntimeError("x")

    assert rg.ReviewGate(lambda: (8, "normal"), delib_human=boom).snapshot()["delib_human"] == -1


# ── 적대 검토 3라운드(RB3-5·8·10·12·13·15) ─────────────────────────────────────
def test_수명_없는_고정은_앞_깃발의_TTL_을_물려받지_않는다(tmp_path):
    """RB3-5 — update-all 의 TTL 깃발 위에 사람이 수명 없이 hold 를 세우면 그 until 을 물려받아 TTL 에 함께 풀렸다."""
    d, clock, _ = _drain(tmp_path)
    d.set_on(scope="all", by="update-all", ttl_s=1_800)
    s = d.set_on(scope="all", by="human:koo", reason="점검", hold=True)
    assert (s["hold"], s["by"], s["until"], s["until_idle"]) == (True, "human:koo", None, None), s
    clock["t"] += 1_800 + 10 ** 6
    assert d.snapshot()["on"] is True and d.snapshot()["hold"] is True, "수명 없는 고정이 앞 TTL 에 풀렸다"


@pytest.mark.parametrize("junk", [
    '{"scope": "all", "until_idle": {"grace_s": 10, "max_s": 100, "idle_since": "어제"}, "set_at": 1}',
    '{"scope": "all", "until_idle": {"grace_s": 10, "max_s": 100}, "set_idle_at": "x", "set_at": 1}',
    '{"scope": "all", "until": 99999999, "hold": "yes"}',
    '{"scope": "all", "until": Infinity}',
    '{"scope": "all", "until": 99999999, "set_at": NaN}',
    '{"scope": "all", "until_idle": {"grace_s": Infinity, "max_s": 100}}',
    '{"scope": "all", "until": 99999999, "by": 7}',
    '{"scope": "all", "until": true}',
])
def test_칸_하나가_깨진_깃발_파일은_꺼짐이고_관문_health_끄기가_멀쩡하다(tmp_path, junk):
    """RB3-8 — 숫자가 아닌 idle_since·set_idle_at, 글 hold, Infinity 가 판정(_live)에서 터져 /health 의 capabilities 가 비고
    관문 상한이 1 이 되고 팩·GET·끄기가 500 이었다."""
    d, _clock, _ = _drain(tmp_path)
    g = rg.from_env(lambda: 0, env={}, drain=d)
    (tmp_path / "drain.json").write_text(junk, encoding="utf-8")
    assert g.snapshot()["limit"] == 8 and g.snapshot()["limit_reason"] == "normal", junk
    assert d.snapshot() == rg.OFF and not (tmp_path / "drain.json").exists()
    (tmp_path / "drain.json").write_text(junk, encoding="utf-8")
    assert d.set_off(by="update-all") == rg.OFF


def test_판정이_터져도_꺼짐으로_읽고_파일을_지운다(tmp_path):
    d, _clock, _ = _drain(tmp_path)
    d.set_on(scope="all", by="update-all", ttl_s=600)

    def boom(*_a):
        raise ZeroDivisionError("x")

    d._live = boom
    try:
        snap = d.snapshot()
    except Exception as exc:  # noqa: BLE001 — 판정 예외가 밖으로 샜다(/health·관문·끄기가 500)
        snap = exc
    assert snap == rg.OFF and d.active_scope() is None, snap
    assert not (tmp_path / "drain.json").exists(), "판정이 터진 깃발 파일을 남겼다"


def test_읽을_수_없는_깃발_파일의_경고는_한_번이다(tmp_path, capsys):
    """RB3-8 — EACCES 등으로 못 읽으면 꺼짐으로 읽되 /health 마다 한 줄씩 쌓지 않는다."""
    (tmp_path / "drain.json").mkdir()                       # 디렉터리 — 읽기가 OSError 다
    d = rg.DrainState(str(tmp_path / "drain.json"), clock=lambda: 1.0, delib_load=lambda: (0, 0))
    for _ in range(5):
        assert d.snapshot() == rg.OFF
    assert capsys.readouterr().out.count("읽지 못했다") == 1


@pytest.mark.parametrize("kw", [{"ttl_s": float("inf")}, {"ttl_s": float("nan")},
                                {"until_idle": {"grace_s": 10, "max_s": float("inf")}},
                                {"until_idle": {"grace_s": float("nan"), "max_s": 100}}])
def test_무한_ttl_max_s_는_거절한다(tmp_path, kw):
    """RB3-8 — ttl_s Infinity 를 받아 써 두면 그 파일을 다시 읽지 못했다."""
    d, _, _ = _drain(tmp_path)
    try:
        d.set_on(scope="all", by="x", **kw)
        got = None
    except Exception as exc:  # noqa: BLE001 — 422 가 아닌 예외(쓰기 실패)도 받아 단언으로 본다
        got = exc
    assert isinstance(got, rg.DrainError) and (got.status, got.code) == (422, "bad_request"), (kw, got)
    assert not (tmp_path / "drain.json").exists()


def test_깃발_쓰기는_고유한_임시_파일이고_읽고_쓰기는_한_줄로_선다(tmp_path, monkeypatch):
    """RB3-10 — 임시 파일 이름이 tmp-{pid} 하나라 같은 프로세스의 동시 쓰기가 서로의 임시 파일을 덮거나 os.replace 가
    FileNotFoundError 였다. 같은 경로의 DrainState 둘이 동시에 써도 겹치지 않는다(잠금)."""
    import threading

    names = []
    real_replace = rg.os.replace
    monkeypatch.setattr(rg.os, "replace", lambda a, b: (names.append(a), real_replace(a, b))[1])
    d1, clock, _ = _drain(tmp_path)
    d2 = rg.DrainState(d1.path, clock=lambda: clock["t"], delib_load=lambda: (0, 0))
    d1.set_on(scope="all", by="update-all", ttl_s=60)
    d1.set_on(scope="all", by="update-all", ttl_s=120)
    assert len(names) == 2 and names[0] != names[1], names
    inside = {"n": 0, "overlap": False}
    gate = threading.Event()
    real_dump = rg.json.dump

    def slow_dump(obj, f, **kw):
        inside["n"] += 1
        if inside["n"] > 1:
            inside["overlap"] = True
        gate.wait(0.3)                     # 다른 쪽이 이 사이에 들어오면 겹침이다
        real_dump(obj, f, **kw)
        inside["n"] -= 1

    monkeypatch.setattr(rg.json, "dump", slow_dump)
    errs = []

    def go(d, ttl):
        try:
            d.set_on(scope="all", by="update-all", ttl_s=ttl)
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    ts = [threading.Thread(target=go, args=(d, t)) for d, t in ((d1, 300), (d2, 400))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs and not inside["overlap"], (errs, inside)
    assert d1.snapshot()["until"] == 10_400.0
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], "임시 파일이 남았다"


def test_다른_주체의_hold_아닌_요청은_주인을_바꾸지_않고_넓히기만_한다(tmp_path):
    """RB3-12 — 살아 있는 깃발에 다른 by 가 hold 아닌 요청을 내면 주인이 바뀌어, 원래 주인(update-all)이 끄지 못했다."""
    d, clock, _ = _drain(tmp_path)
    d.set_on(scope="all", by="update-all", reason="§2", ttl_s=1_800)
    s = d.set_on(scope="panels", by="deploy-x", reason="다른 일", ttl_s=3_600)
    assert (s["by"], s["reason"], s["scope"], s["until"]) == ("update-all", "§2", "all", 13_600.0), s
    with pytest.raises(rg.DrainError):
        d.set_off(by="deploy-x")
    d.set_on(scope="panels", by="update-all", ttl_s=60)          # 주인은 좁힐 수 있다(끝맺음 all → panels)
    assert d.snapshot()["scope"] == "panels"
    assert d.set_on(scope="all", by="deploy-x", ttl_s=60)["scope"] == "all", "남이 넓히는 것은 받는다"
    assert d.set_off(by="update-all")["on"] is False


def test_TTL_깃발에도_절대_수명이_있다(tmp_path):
    """RB3-13 — 벽시계로 도는 TTL 깃발이 늘리기만 하면 영영 선다. 요청 하나는 6시간까지, 처음 TTL 을 세운 때부터 6시간이면
    늘려도 풀리고, 시계가 뒤로 가 남은 수명이 6시간보다 길어 보이면 믿지 않는다. 사람의 고정은 이 상한을 받지 않는다."""
    d, clock, _ = _drain(tmp_path)
    with pytest.raises(rg.DrainError) as e:
        d.set_on(scope="all", by="x", ttl_s=rg.DRAIN_TTL_MAX_S + 1)
    assert e.value.status == 422
    start = t = clock["t"]
    while t < start + rg.DRAIN_TTL_MAX_S:                       # 1,700초마다 1,800초로 늘린다(update-all 의 § 머리)
        clock["t"] = t
        assert d.set_on(scope="all", by="update-all", ttl_s=1_800)["on"] is True
        t += 1_700
    clock["t"] = start + rg.DRAIN_TTL_MAX_S
    assert d.snapshot()["on"] is False, "늘리기만 한 TTL 깃발이 절대 수명을 넘겨 섰다"
    d.set_on(scope="all", by="update-all", ttl_s=600)
    clock["t"] -= 86_400                                       # 시계가 하루 뒤로 — 남은 수명 24시간+ 은 믿지 않는다
    assert d.snapshot()["on"] is False
    d.set_on(scope="all", by="human:koo", hold=True, ttl_s=rg.DRAIN_TTL_MAX_S * 4)
    clock["t"] += rg.DRAIN_TTL_MAX_S * 2
    assert d.snapshot()["on"] is True, "사람의 고정은 TTL 상한을 받지 않는다"


def test_by_는_120자에서_끊는다(tmp_path):
    """RB3-15 — /health·/drain 이 by 를 되비춘다. 끄기도 같은 규칙으로 끊어 주인을 알아본다."""
    d, _, _ = _drain(tmp_path)
    long_by = "update-all:" + "가" * 5_000
    s = d.set_on(scope="all", by=long_by, reason="r" * 5_000, ttl_s=60)
    assert len(s["by"]) == rg.DRAIN_BY_MAX and len(s["reason"]) == rg.DRAIN_REASON_MAX
    stored = json.loads((tmp_path / "drain.json").read_text(encoding="utf-8"))
    assert len(stored["by"]) == rg.DRAIN_BY_MAX, "파일에 끊지 않은 by 를 썼다"
    try:
        off = d.set_off(by=long_by)
    except rg.DrainError as exc:
        off = exc.body()
    assert off == rg.OFF, off


def test_until_idle_도_시계가_뒤로_가면_남은_수명을_믿지_않는다(tmp_path):
    """RB3-13 — until_idle 의 상한(세운 때 + max_s)도 시계가 뒤로 가면 길어진다. 남은 상한이 max_s 보다 길면 믿지 않는다."""
    d, clock, _ = _drain(tmp_path, delib=(1, 0))
    d.set_on(scope="panels", by="update-all", until_idle={"grace_s": 100, "max_s": 1_000})
    assert d.snapshot()["on"] is True
    clock["t"] -= 86_400
    assert d.snapshot()["on"] is False

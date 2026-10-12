# 카드 대조 예산(limits)·묶음 계획(plan)·창 출처(_model_context_state)가 창에서 유도되고 단조이며 쪼개지 않고 거절하는지 본다
#
# 한 호출에 무엇을 얼마나 실을지는 박스의 창에서 유도한다(고정 천장 없음 — PLAN 4절). 창이 모자라면 다시 쪼개지 않고
# ctx_too_small 이다(D-22). 창 크기가 어디서 왔는지(server·env·fallback)는 앱이 '가정한 창' 위에서 몇 시간짜리 계획을
# 굳히지 않게 하는 신호라, 실제 조회 함수를 거쳐 본다(손으로 쓴 사전이 아니라 — PLAN 9절).
#
#   실행:  .venv/bin/python -m pytest tests/test_card_review_budget.py -q
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

import app  # noqa: E402
import card_review as cr  # noqa: E402

_NO_ENV: dict = {}


def _lim(ctx, mt=0, env=_NO_ENV):
    return cr.limits(ctx, "server", mt, env=env)


# ── 세 박스의 값 ─────────────────────────────────────────────────────────────
def test_운영_1M_창_미설정_출력의_값():
    lim = _lim(1_048_576)
    assert lim["api_rev"] == "cr-1" and lim["kinds"] == ["sweep", "verdict", "offcard"]
    assert lim["out_tokens"] == 16_000 and lim["llm_max_tokens"] is None, "미설정 출력 상한을 0 으로 실었다"
    assert lim["p_hard"] == 1_008_310
    v, s, o = lim["verdict"], lim["sweep"], lim["offcard"]
    assert (v["unit_body_max"], v["card_budget"], v["k"], v["k_out"]) == (105_871, 119_387, 8, 19)
    assert (s["units_per_page_max"], s["manifest_item_max"], s["manifest_page_max"]) == (64, 1_420, 90_903)
    assert o["findings_max"] == 14
    assert lim["unit_body_max"] == v["unit_body_max"], "1M 창에서는 verdict 단위 몫이 더 작다"
    assert all(lim["runnable"].values()) and "ctx_too_small" not in lim


def test_128K_창_출력_8192_의_값():
    lim = _lim(128_000, 8_192)
    assert lim["p_hard"] == 109_368 and lim["llm_max_tokens"] == 8_192
    v, s, o = lim["verdict"], lim["sweep"], lim["offcard"]
    assert (v["k"], v["k_out"]) == (8, 9), "출력 상한 8,192 면 K_out 이 9 다(설계서 3.3)"
    assert s["units_per_page_max"] == 31 and o["findings_max"] == 6
    assert lim["unit_body_max"] == 11_603 and v["card_budget"] == 13_085


def test_dev_16K_창의_값():
    lim = _lim(16_384)
    assert lim["p_hard"] == 11_999 and lim["out_tokens"] == 4_096
    v, s, o = lim["verdict"], lim["sweep"], lim["offcard"]
    assert (v["unit_body_max"], v["card_budget"], v["k"], v["k_out"]) == (3_007, 3_392, 4, 4)
    assert (s["units_per_page_max"], s["manifest_item_max"]) == (14, 800)
    assert o["findings_max"] == 1, "dev 출력 몫(4,096)으로는 카드 밖 리스크를 한 호출에 한 줄씩만 받는다(P-18)"
    assert all(lim["runnable"].values()), "16K 창은 작아도 돈다 — 카드·단위가 몫 안이면"


def test_넓은_창에서_낮은_고정_천장이_걸리지_않는다():
    """설계서의 min(2000,…)·min(2600,…)·known 1,200·F ≤ 8·항목 800 같은 천장이 1백만 토큰 창에서 걸리면 그만큼 버린다."""
    lim = _lim(1_048_576)
    assert lim["verdict"]["role_max"] > 2_000 and lim["sweep"]["role_max"] > 2_000
    assert lim["verdict"]["digest_max"] > 2_600 and lim["offcard"]["digest_max"] > 2_600
    assert lim["offcard"]["known_max"] > 1_200 and lim["offcard"]["mech_max"] > 1_200
    assert lim["offcard"]["findings_max"] > 8 and lim["sweep"]["manifest_item_max"] > 800
    assert lim["verdict"]["hint_max"] > 600


# ── 단조·닫힘 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mt", [0, 4_096, 8_192, 32_768])
def test_창이_커지면_단위_상한이_줄지_않는다(mt):
    rng = random.Random(f"mono-{mt}")
    wins = sorted({16_384, 2_000_000, *(rng.randint(16_384, 2_000_000) for _ in range(300))})
    prev = None
    for ctx in wins:
        lim = _lim(ctx, mt)
        if prev is not None:
            assert lim["unit_body_max"] >= prev["unit_body_max"], (ctx, mt)
            for kind in cr.KINDS:
                assert lim[kind]["body"] >= prev[kind]["body"], (ctx, mt, kind)
        # 단위를 싣는 종류마다 본문이 단위 상한 이상이다(P-02 덧붙임).
        assert lim["verdict"]["body"] >= lim["unit_body_max"] and lim["offcard"]["body"] >= lim["unit_body_max"]
        prev = lim


def test_틀·블록·지적_몫과_본문의_합이_P_를_넘지_않는다():
    """본문 = P − 고정 몫이고 고정 몫은 틀 여유값 + 블록 몫 + 지적 몫 이상이다 — 몫 안의 조각만 실으면 P 안이다."""
    for ctx in (12_000, 16_384, 33_333, 128_000, 777_777, 1_048_576):
        lim = _lim(ctx)
        for kind in cr.KINDS:
            k = lim[kind]
            blocks = sum(v for n, v in k.items() if n.endswith("_max") and n.split("_")[0] in
                         ("role", "digest", "domains", "known", "mech", "hint"))
            assert k["fixed"] + k["body"] == k["p"], (ctx, kind)
            assert k["fixed"] >= cr.TEMPLATE_ALLOW[kind] + blocks, (ctx, kind)
            assert k["p"] <= lim["p_hard"], (ctx, kind)


def test_limits_는_네트워크를_타지_않는다(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("limits() 가 창을 직접 물었다 — 라우트가 to_thread 로 묻고 넘겨야 한다")

    monkeypatch.setattr(httpx, "get", _boom)
    assert _lim(1_048_576)["p_hard"] > 0


def test_limits_rev_는_예산_입력에만_따른다():
    a, b = _lim(1_048_576), _lim(1_048_576)
    assert a["limits_rev"] == b["limits_rev"]
    assert _lim(1_048_575)["limits_rev"] != a["limits_rev"]
    assert _lim(1_048_576, 8_192)["limits_rev"] != a["limits_rev"]
    assert _lim(1_048_576, env={"CARD_REVIEW_K": "4"})["limits_rev"] != a["limits_rev"]
    assert cr.limits(1_048_576, "fallback", 0, retrying=True, env={})["limits_rev"] == a["limits_rev"], (
        "창 출처는 예산이 아니다 — 같은 창이면 같은 판이다")


def test_손잡이():
    assert _lim(1_048_576, env={"CARD_REVIEW_K": "3"})["verdict"]["k"] == 3
    assert _lim(1_048_576, env={"CARD_REVIEW_K": "40"})["verdict"]["k"] == 19, "K 는 출력 몫(K_out)을 넘지 않는다"
    assert _lim(1_048_576, env={"CARD_REVIEW_K": "여덟"})["verdict"]["k"] == 8
    bigger = _lim(1_048_576, env={"CARD_REVIEW_SHARE_VERDICT": "0.4"})
    assert bigger["verdict"]["p"] > _lim(1_048_576)["verdict"]["p"]
    assert _lim(1_048_576, env={"CARD_REVIEW_SHARE_SWEEP": "1.5"})["sweep"]["p"] == _lim(1_048_576)["sweep"]["p"]


def test_창이_모자라면_쪼개지_않고_ctx_too_small_과_필요한_창을_싣는다():
    lim = _lim(8_192)
    assert not all(lim["runnable"].values())
    need = lim["ctx_too_small"]["need_ctx_tokens"]
    assert need and need > 8_192 and "다시 쪼개지 않는다" in lim["ctx_too_small"]["message"]
    assert all(_lim(need)["runnable"].values()) and not all(_lim(need - 1)["runnable"].values()), (
        "need_ctx_tokens 가 돌 수 있는 가장 작은 창이 아니다")


# ── plan() ──────────────────────────────────────────────────────────────────
def _cards(rng, n):
    return [{"alias": f"K{i + 1:02d}" if n < 100 else f"K{i + 1:03d}", "record_id": f"DOC-T-{i:04d}",
             "checkable": rng.random() < 0.6, "chars": rng.randint(200, 95_000)} for i in range(n)]


def _units(rng, n):
    return [{"unit_id": f"u-{i:03d}", "manifest_chars": rng.randint(60, 4_000),
             "body_chars": rng.randint(300, 300_000)} for i in range(n)]


@pytest.mark.parametrize("ctx,env", [(1_048_576, {}), (128_000, {}), (16_384, {}),
                                     (1_048_576, {"CARD_REVIEW_SHARE_OFFCARD": "0.05", "CARD_REVIEW_SHARE_VERDICT": "1"})])
@pytest.mark.parametrize("seed", range(12))
def test_plan_의_불변식(ctx, env, seed):
    rng = random.Random(f"plan-{ctx}-{seed}")
    lim = _lim(ctx, env=env)
    cards, units = _cards(rng, rng.randint(0, 120)), _units(rng, rng.randint(0, 150))
    out = cr.plan(cards, units, lim)
    assert out == cr.plan(cards, units, lim), "같은 입력에 다른 계획"
    assert out["limits_rev"] == lim["limits_rev"]
    v, s = lim["verdict"], lim["sweep"]
    over = out["oversize"]["kinds"]
    by_alias = {c["alias"]: c for c in cards}

    seen_v = [a for d in out["verdict_decks"] for a in d["aliases"]]
    want_v = [c["alias"] for c in cards if c["checkable"] and "verdict" not in over.get(c["alias"], [])]
    assert seen_v == want_v, "점검형 카드가 정확히 한 묶음이거나 oversize 여야 한다(팩 순서 그대로)"
    for d in out["verdict_decks"]:
        assert 1 <= len(d["aliases"]) <= v["k"]
        assert d["chars"] == sum(by_alias[a]["chars"] for a in d["aliases"])
        assert d["chars"] <= v["card_budget"] or (d["soft_over"] and len(d["aliases"]) == 1)
        assert v["fixed"] + lim["unit_body_max"] + d["chars"] <= lim["p_hard"], "상한 크기 단위와 함께 창을 넘는 묶음"
    seen_s = [a for d in out["sweep_decks"] for a in d["aliases"]]
    assert seen_s == [c["alias"] for c in cards if "sweep" not in over.get(c["alias"], [])]
    for d in out["sweep_decks"]:
        assert d["chars"] <= s["card_budget"] or (d["soft_over"] and len(d["aliases"]) == 1)
        assert s["fixed"] + s["manifest_page_max"] + d["chars"] <= lim["p_hard"]
    for a, ks in over.items():
        n = by_alias[a]["chars"]
        if "verdict" in ks:
            assert v["fixed"] + lim["unit_body_max"] + n > lim["p_hard"]
        if "sweep" in ks:
            assert s["fixed"] + s["manifest_page_max"] + n > lim["p_hard"]
    assert sorted(over) == out["oversize"]["cards"]

    paged = [u for p in out["manifest_pages"] for u in p]
    assert paged == [u["unit_id"] for u in units if u["unit_id"] not in out["oversize"]["manifest"]]
    for p in out["manifest_pages"]:
        assert 1 <= len(p) <= s["units_per_page_max"]
    uo, us = set(out["oversize"]["units"]), set(out["soft_over"]["units"])
    for u in units:
        n = u["body_chars"]
        assert (u["unit_id"] in uo) == (v["fixed"] + n + v["card_budget"] > lim["p_hard"]
                                        or lim["offcard"]["fixed"] + n > lim["p_hard"])
        assert (u["unit_id"] in us) == (u["unit_id"] not in uo and n > lim["unit_body_max"])


def test_plan_deck_id_는_묶음의_카드로_정해진다():
    lim = _lim(1_048_576)
    cards = [{"alias": f"K{i:02d}", "record_id": f"R{i}", "checkable": True, "chars": 1_500} for i in range(1, 12)]
    a = cr.plan(cards, [], lim)["verdict_decks"]
    assert [len(d["aliases"]) for d in a] == [8, 3], "1M 창에서 1,500자 카드는 K 로 닫힌다"
    b = cr.plan([{**c, "record_id": c["record_id"] + "x"} if c["alias"] == "K11" else c for c in cards], [], lim)
    assert b["verdict_decks"][0]["deck_id"] == a[0]["deck_id"] and b["verdict_decks"][1]["deck_id"] != a[1]["deck_id"]


# ── 창 출처 ──────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def lookup(monkeypatch):
    """창 조회 캐시를 비우고 조회(httpx.get)와 시계를 갈아 끼운다 — 실제 _model_context_tokens·_state 를 거친다."""
    saved = dict(app._ctx_cache)
    app._ctx_cache.clear()
    st = {"calls": 0, "now": 5000.0, "answers": []}

    def _get(_url, **_k):
        st["calls"] += 1
        ans = st["answers"].pop(0) if len(st["answers"]) > 1 else st["answers"][0]
        if isinstance(ans, Exception):
            raise ans
        return _Resp(ans)

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(app.time, "monotonic", lambda: st["now"])
    monkeypatch.setattr(app, "_CTX_FALLBACK", 128_000)
    yield st, monkeypatch
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)


def _models(**extra):
    return {"data": [{"id": app.VLLM_MODEL, **extra}]}


def test_서버가_창을_알려_주면_server(lookup):
    st, _ = lookup
    st["answers"] = [_models(max_model_len=1_048_576)]
    assert app._model_context_state() == {"tokens": 1_048_576, "source": "server", "retrying": False}


@pytest.mark.parametrize("env_set,source", [(True, "env"), (False, "fallback")])
def test_창_칸_없는_답은_사람이_적은_값이면_env_아니면_fallback(lookup, env_set, source):
    st, mp = lookup
    mp.setattr(app, "_CTX_ENV_SET", env_set)
    st["answers"] = [_models()]
    assert app._model_context_state() == {"tokens": 128_000, "source": source, "retrying": False}
    assert app._model_context_state()["source"] == source and st["calls"] == 1, "답을 받은 뒤에도 다시 물었다"


@pytest.mark.parametrize("env_set,source", [(True, "env"), (False, "fallback")])
def test_조회_예외는_다시_묻는_중이고_살아나면_server(lookup, env_set, source):
    st, mp = lookup
    mp.setattr(app, "_CTX_ENV_SET", env_set)
    st["answers"] = [httpx.ConnectError("connection refused"), _models(max_model_len=16_384)]
    assert app._model_context_state() == {"tokens": 128_000, "source": source, "retrying": True}
    st["now"] += app._CTX_RETRY_S + 1
    assert app._model_context_state() == {"tokens": 16_384, "source": "server", "retrying": False}


def test_창_출처가_limits_에_그대로_실린다(lookup):
    st, _ = lookup
    st["answers"] = [httpx.ConnectError("down")]
    s = app._model_context_state()
    lim = cr.limits(s["tokens"], s["source"], 0, retrying=s["retrying"], env={})
    assert (lim["ctx_tokens"], lim["ctx_source"], lim["ctx_retrying"]) == (128_000, "fallback", True)

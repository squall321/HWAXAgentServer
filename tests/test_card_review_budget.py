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
    assert lim["api_rev"] == "cr-1" and lim["kinds"] == ["sweep", "verdict", "offcard", "noinput", "summary"]
    assert lim["cell_kinds"] == ["sweep", "verdict", "offcard", "noinput"]
    assert lim["out_tokens"] == 16_000 and lim["llm_max_tokens"] is None, "미설정 출력 상한을 0 으로 실었다"
    assert lim["max_tokens_source"] == "none"
    assert lim["p_hard"] == 1_008_310
    v, s, o = lim["verdict"], lim["sweep"], lim["offcard"]
    # M3 — verdict 에 메커니즘 몫(P 의 5%)이 고정 몫으로 들어 단위 상한이 105,871 → 99,402 로 줄었다(M2 와 다른 판이다).
    assert (v["unit_body_max"], v["card_budget"], v["k"], v["k_out"]) == (99_402, 112_093, 8, 19)
    assert v["mech_max"] == int(v["p"] * 0.05) == 13_762
    assert (s["units_per_page_max"], s["manifest_item_max"], s["manifest_page_max"]) == (64, 1_403, 89_802)
    assert o["findings_max"] == 14
    assert lim["unit_body_max"] == v["unit_body_max"], "1M 창에서는 verdict 단위 몫이 더 작다"
    assert all(lim["runnable"].values()) and "ctx_too_small" not in lim


def test_128K_창_출력_8192_의_값():
    lim = _lim(128_000, 8_192)
    assert lim["p_hard"] == 109_368 and lim["llm_max_tokens"] == 8_192 and lim["max_tokens_source"] == "delib"
    v, s, o = lim["verdict"], lim["sweep"], lim["offcard"]
    assert (v["k"], v["k_out"]) == (8, 9), "출력 상한 8,192 면 K_out 이 9 다(설계서 3.3)"
    assert s["units_per_page_max"] == 31 and o["findings_max"] == 6
    assert lim["unit_body_max"] == 10_813 and v["card_budget"] == 12_195
    assert lim["noinput"]["items_max"] == 7 and lim["summary"]["cover_max"] == 8


def test_dev_16K_창의_값():
    lim = _lim(16_384)
    assert lim["p_hard"] == 11_999 and lim["out_tokens"] == 4_096
    v, s, o = lim["verdict"], lim["sweep"], lim["offcard"]
    assert (v["unit_body_max"], v["card_budget"], v["k"], v["k_out"]) == (2_725, 3_074, 4, 4)
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
            # 출력 몫에서 유도하는 수는 창이 커져도 줄지 않는다(같은 출력 상한).
            for kind, key in (("noinput", "items_max"), ("summary", "cover_max"), ("summary", "narrative_max"),
                              ("offcard", "longdoc_max"), ("verdict", "mech_max")):
                assert lim[kind][key] >= prev[kind][key], (ctx, mt, kind, key)
        # 단위를 싣는 종류마다 본문이 단위 상한 이상이다(P-02 덧붙임).
        for kind in ("verdict", "offcard", "noinput"):
            assert lim[kind]["body"] >= lim["unit_body_max"], (ctx, mt, kind)
        prev = lim


def test_틀·블록·지적_몫과_본문의_합이_P_를_넘지_않는다():
    """본문 = P − 고정 몫이고 고정 몫은 틀 여유값 + 블록 몫 + 지적 몫 이상이다 — 몫 안의 조각만 실으면 P 안이다."""
    for ctx in (12_000, 16_384, 33_333, 128_000, 777_777, 1_048_576):
        lim = _lim(ctx)
        for kind in cr.KINDS:
            k = lim[kind]
            blocks = sum(v for n, v in k.items() if n.endswith("_max") and n.split("_")[0] in
                         ("role", "digest", "domains", "known", "mech", "hint", "missing", "blocks"))
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


def _bump(v):
    """상수 하나를 다른 값으로 — 수는 키우고, 글은 덧붙이고, 사전·튜플은 첫 잎을 바꾼다."""
    if isinstance(v, bool):
        return not v
    if isinstance(v, int):
        return v + 1
    if isinstance(v, float):
        return v * 1.01
    if isinstance(v, str):
        return v + "-x"
    if isinstance(v, dict):
        k = next(iter(v))
        return {**v, k: _bump(v[k])}
    raise TypeError(type(v))


@pytest.mark.parametrize("name", cr.LIMITS_CONSTANTS)
def test_limits_rev_는_예산_상수마다_바뀐다(monkeypatch, name):
    """RB-6 — 판이 손잡이와 계수 셋만 담아, 행 길이 어림·단위 몫 같은 상수만 고친 배포에서 판은 같은데 실효 K·단위 상한이
    바뀌었다(ROW_CHARS·VERDICT_UNIT_SHARE 를 바꾸자 K 8→4, unit_body_max 105,871→67,577, 판은 그대로). 상수는 어느 종류의
    판(limits_rev_by_kind)에든 든다 — 종류 이름이 열쇠인 상수는 종류마다 따로 본다(RB3-6)."""
    v = getattr(cr, name)
    keys = list(v) if name in cr._REV_KEYED else [None]
    for key in keys:
        before = _lim(1_048_576)["limits_rev_by_kind"]
        monkeypatch.setattr(cr, name, {**v, key: _bump(v[key])} if key else _bump(v))
        after = _lim(1_048_576)["limits_rev_by_kind"]
        assert after != before, f"{name}[{key}] 를 바꿨는데 어느 종류의 판도 같다"
        if key:
            assert after[key] != before[key], f"{name}[{key}] 가 {key} 의 판에 들지 않는다"
        monkeypatch.setattr(cr, name, v)


def _global_names(fn) -> set:
    """함수(안쪽 함수 포함)가 읽는 전역 이름."""
    out, stack = set(), [fn.__code__]
    while stack:
        code = stack.pop()
        out |= set(code.co_names)
        stack += [c for c in code.co_consts if hasattr(c, "co_names")]
    return out


def test_예산_식이_읽는_모듈_상수는_전부_판에_든다():
    """새 상수를 예산 식에 더하고 LIMITS_CONSTANTS 에 안 더하면 판이 그 상수를 모른다 — 식의 함수들이 읽는 모듈 상수를 모아
    대조한다. 손잡이 기본값(K_DEFAULT·SHARE_DEFAULT)은 손잡이 값으로, 다른 판(PROMPT_REV·NORM_REV·API_REV)은 따로 싣는다."""
    other = {"KINDS", "CELL_KINDS", "API_REV", "PROMPT_REV", "NORM_REV", "K_DEFAULT", "SHARE_DEFAULT", "_SHARE_ENV",
             "LIMITS_CONSTANTS"}
    read = set()
    for fn in (cr._budget, cr._need_ctx, cr.limits, cr.plan, cr._pack_decks, cr.unit_size, cr._knob_k,
               cr._knob_shares, cr._limits_constants):
        read |= _global_names(fn)
    consts = {n for n in read if n in vars(cr) and not callable(vars(cr)[n])
              and type(vars(cr)[n]).__name__ != "module"}
    assert consts - other <= set(cr.LIMITS_CONSTANTS), f"판에 없는 예산 상수 — {sorted(consts - other - set(cr.LIMITS_CONSTANTS))}"
    assert set(cr.LIMITS_CONSTANTS) <= consts | {"LIMITS_FORMULA_REV"}, "식이 읽지 않는 이름이 판에 있다"


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

    def cells_ok(lm):
        return all(lm["runnable"][k] for k in cr.CELL_KINDS)

    assert cells_ok(_lim(need)) and not cells_ok(_lim(need - 1)), "need_ctx_tokens 가 셀 일이 도는 가장 작은 창이 아니다"


# ── M3 — 새 종류 둘(noinput·summary)·출력 상한·판 ─────────────────────────────────
def test_M3_새_종류의_몫과_판_1M():
    """M3.md 4.3 — 종류 다섯·cell_kinds 넷, 새 종류의 몫은 출력 몫에서 유도한다(items_max 15 · narrative 2,520 · cover 18 ·
    cover_hard 54 — 1M 창·출력 상한 미설정). 긴 문서 몫은 카드 밖 P 의 25%, 판 다섯은 M3 값이다."""
    lim = _lim(1_048_576)
    n, su, o = lim["noinput"], lim["summary"], lim["offcard"]
    assert set(n) == {"p", "fixed", "body", "role_max", "digest_max", "known_max", "mech_max", "hint_max",
                      "unit_body_max", "card_budget", "items_max"}
    assert set(su) == {"p", "fixed", "body", "hint_max", "blocks_max", "line_max", "narrative_max", "cover_max",
                       "cover_hard_max"}
    assert n["items_max"] == 15 and n["unit_body_max"] == n["body"] and n["card_budget"] == n["body"] - lim["unit_body_max"]
    assert (su["line_max"], su["narrative_max"], su["cover_max"], su["cover_hard_max"]) == (400, 2_520, 18, 54)
    assert o["longdoc_max"] == int(o["p"] * 0.25) == 82_575
    assert lim["runnable"] == dict.fromkeys(cr.KINDS, True)
    assert lim["search"] == {"timeout_s": 60.0}
    assert lim["prompt_rev"] == {"sweep": "cr-sweep-2", "verdict": "cr-verdict-2", "offcard": "cr-offcard-2",
                                 "noinput": "cr-noinput-1", "summary": "cr-summary-1"}
    assert cr.LIMITS_FORMULA_REV == "cr-l2" and cr.UNSUPPORTED_KINDS == ("cross", "hunt")
    # 카드 밖 고정 몫은 M2 그대로다 — 긴 문서 발췌는 본문에서 쓴다(검색하지 않는 전문가의 배경 카드 자리를 줄이지 않는다).
    assert (o["fixed"], o["body"]) == (109_297, 221_004)


@pytest.mark.parametrize("ctx", [16_384, 131_072, 1_048_576])
def test_새_종류의_식(ctx):
    """M3.md 4.3 의 식 그대로 — rows_out = OUTCAP × 0.6 × 1.05."""
    for mt in (0, 4_096, 32_768):
        lim = _lim(ctx, mt)
        if not lim["out_tokens"] < ctx:
            continue
        rows_out = (mt or lim["out_tokens"]) * cr.OUT_USE * 1.05
        su = lim["summary"]
        assert su["narrative_max"] == int(rows_out * 0.25)
        assert su["cover_max"] == max(1, int((rows_out - su["narrative_max"] - 300) // 400))
        assert su["cover_hard_max"] == su["cover_max"] * (1 + cr.PARSE_RETRIES_DEFAULT)
        assert lim["noinput"]["items_max"] == max(1, int((rows_out - 800) // 600))


def test_출력_상한_손잡이_CARD_REVIEW_MAX_TOKENS():
    """카드 대조만의 출력 상한 — 있으면 그것(card_review), 없으면 심의 LLM 의 상한(delib), 둘 다 없으면 none. 32,768 이면
    k_out 이 19 → 39, cover_max 가 18 → 37(M3.md 4.3 의 식), items_max 가 15 → 33 으로 오른다."""
    assert cr.resolve_max_tokens(8_192, {"CARD_REVIEW_MAX_TOKENS": "32768"}) == (32_768, "card_review")
    assert cr.resolve_max_tokens(8_192, {}) == (8_192, "delib")
    assert cr.resolve_max_tokens(None, {}) == (0, "none")
    assert cr.resolve_max_tokens(True, {}) == (0, "none"), "bool 은 상한이 아니다"
    for bad in ("서른", "-5", "0", ""):
        assert cr.resolve_max_tokens(4_096, {"CARD_REVIEW_MAX_TOKENS": bad}) == (4_096, "delib"), bad
    mt, src = cr.resolve_max_tokens(None, {"CARD_REVIEW_MAX_TOKENS": "32768"})
    lim = cr.limits(1_048_576, "server", mt, env={}, max_tokens_source=src)
    assert (lim["llm_max_tokens"], lim["max_tokens_source"]) == (32_768, "card_review")
    assert lim["verdict"]["k_out"] == 39 and lim["summary"]["cover_max"] == 37 and lim["noinput"]["items_max"] == 33
    assert lim["limits_rev"] != _lim(1_048_576)["limits_rev"], "출력 상한은 판에 든다"


def test_요약만_못_도는_박스는_ctx_too_small_이_아니다(monkeypatch):
    """꼭 다룰 행 줄이 하나도 안 들면 요약만 runnable=false 이고 셀 일은 돈다. 출력 상한만으로는 이 박스가 생기지 않는다
    (RB3-14 — 셀 종류가 행 한 줄을 낼 출력이면 요약도 한 줄은 든다) — 요약 머리 상수를 키워 만든다."""
    monkeypatch.setattr(cr, "SUMMARY_HEAD", 10 ** 7)
    lim = _lim(1_048_576)
    assert lim["runnable"]["summary"] is False
    assert all(lim["runnable"][k] for k in cr.CELL_KINDS) and "ctx_too_small" not in lim
    assert lim["summary"]["cover_max"] == 1, "보고하는 cover_max 는 1 아래로 내려가지 않는다(식의 max(1, …))"


def test_ctx_too_small_은_셀_종류만_말한다():
    lim = _lim(8_192)
    assert "ctx_too_small" in lim
    named = lim["ctx_too_small"]["message"].split("카드 대조(")[1].split(")")[0].split("·")
    assert set(named) <= set(cr.CELL_KINDS) and named, named


def test_검색_기한_손잡이():
    assert cr.search_timeout_s({}) == 60.0
    assert cr.search_timeout_s({"CARD_REVIEW_SEARCH_TIMEOUT_S": "15"}) == 15.0
    for bad in ("0", "-3", "육십"):
        assert cr.search_timeout_s({"CARD_REVIEW_SEARCH_TIMEOUT_S": bad}) == 60.0
    a = _lim(1_048_576, env={"CARD_REVIEW_SEARCH_TIMEOUT_S": "15"})
    assert a["search"] == {"timeout_s": 15.0} and a["limits_rev"] == _lim(1_048_576)["limits_rev"], (
        "검색 기한은 시간 손잡이다 — 예산의 모양이 아니라 판에 들지 않는다")


def test_새_종류의_몫_손잡이():
    base = _lim(1_048_576)
    assert _lim(1_048_576, env={"CARD_REVIEW_SHARE_NOINPUT": "0.4"})["noinput"]["p"] > base["noinput"]["p"]
    assert _lim(1_048_576, env={"CARD_REVIEW_SHARE_SUMMARY": "0.2"})["summary"]["p"] < base["summary"]["p"]


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
    # 단위는 이 계획이 실제로 낸 묶음과 견준다(RB-7) — 가장 큰 카드 한 장과도(또는 offcard 홀로) 넘으면 oversize, 묶음째로만
    # 넘으면 그 쌍이 pair_over 다.
    card_max = max((by_alias[a]["chars"] for a in seen_v), default=0)
    pairs = {(p["deck_id"], p["unit_id"]): p for p in out["pair_over"]}
    for u in units:
        n = u["body_chars"]
        assert (u["unit_id"] in uo) == (lim["offcard"]["fixed"] + n > lim["p_hard"]
                                        or (bool(seen_v) and v["fixed"] + n + card_max > lim["p_hard"]))
        assert (u["unit_id"] in us) == (u["unit_id"] not in uo and n > lim["unit_body_max"])
        for d in out["verdict_decks"]:
            need = v["fixed"] + n + d["chars"]
            assert ((d["deck_id"], u["unit_id"]) in pairs) == (u["unit_id"] not in uo and need > lim["p_hard"])
    assert all(p["have"] == lim["p_hard"] < p["need"] for p in out["pair_over"])
    s_cards = max((d["chars"] for d in out["sweep_decks"]), default=len(cr._NO_CARDS))
    mo = set(out["oversize"]["manifest"])
    for u in units:
        assert (u["unit_id"] in mo) == (s["fixed"] + u["manifest_chars"] + s_cards > lim["p_hard"])


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


# ── 종류마다의 판(RB3-6) ──────────────────────────────────────────────────────
def _view(lim: dict, kind: str):
    """그 종류의 예산으로 앱이 얼리는 것 — 종류 칸·돌 수 있나·단위 상한·단위 크기 식·목록 항목."""
    out = {"k": lim[kind], "run": lim["runnable"][kind]}
    if kind in ("verdict", "offcard", "noinput"):
        out.update(ub=lim["unit_body_max"], us=lim["unit_size"])
    if kind == "sweep":
        out["mi"] = lim["manifest_item"]
    return out


def _bumps():
    """손잡이·상수를 하나씩 바꾼 limits() 부르기 — (이름, 부르는 함수)."""
    out = [(f"env:{k}", lambda k=k, v=v: _lim(1_048_576, env={k: v})) for k, v in (
        ("CARD_REVIEW_K", "5"), ("CARD_REVIEW_SHARE_VERDICT", "0.3"), ("CARD_REVIEW_SHARE_SWEEP", "0.6"),
        ("CARD_REVIEW_SHARE_OFFCARD", "0.35"), ("CARD_REVIEW_SHARE_NOINPUT", "0.35"), ("CARD_REVIEW_SHARE_SUMMARY", "0.6"),
        ("CARD_REVIEW_PARSE_RETRIES", "4"))]
    out += [("mt", lambda: _lim(1_048_576, 40_000)), ("ctx", lambda: _lim(1_048_575))]
    return out


@pytest.mark.parametrize("name,call", _bumps(), ids=[n for n, _ in _bumps()])
def test_종류의_예산이_바뀌면_그_종류의_판이_바뀐다_손잡이(name, call):
    """오라클 — 종류마다 앱이 얼리는 예산(_view)이 바뀌면 그 종류의 판이 바뀌어야 한다(판이 예산 변화를 놓치지 않는다)."""
    base, now = _lim(1_048_576), call()
    for k in cr.KINDS:
        if _view(now, k) != _view(base, k):
            assert now["limits_rev_by_kind"][k] != base["limits_rev_by_kind"][k], (name, k)


def _const_cases():
    out = []
    for name in cr.LIMITS_CONSTANTS:
        v = getattr(cr, name)
        for key in (list(v) if name in cr._REV_KEYED else [None]):
            out.append((name, key))
    return out


@pytest.mark.parametrize("name,key", _const_cases(), ids=[f"{n}-{k}" for n, k in _const_cases()])
def test_종류의_예산이_바뀌면_그_종류의_판이_바뀐다_상수(monkeypatch, name, key):
    base = _lim(1_048_576)
    v = getattr(cr, name)
    monkeypatch.setattr(cr, name, {**v, key: _bump(v[key])} if key else _bump(v))
    now = _lim(1_048_576)
    for k in cr.KINDS:
        if _view(now, k) != _view(base, k):
            assert now["limits_rev_by_kind"][k] != base["limits_rev_by_kind"][k], (name, key, k)


SUMMARY_ONLY = [("env", "CARD_REVIEW_SHARE_SUMMARY", "0.6"), ("env", "CARD_REVIEW_PARSE_RETRIES", "4"),
                ("const", "SUMMARY_HEAD", None), ("const", "NARRATIVE_SHARE", None),
                ("keyed", "TEMPLATE_ALLOW", "summary"), ("keyed", "BODY_MIN", "summary"),
                ("keyed", "_BLOCK_SHARE", "summary"), ("keyed", "ROW_CHARS", "summary")]


@pytest.mark.parametrize("how,name,arg", SUMMARY_ONLY, ids=[f"{h}-{n}" for h, n, _ in SUMMARY_ONLY])
def test_요약에만_닿는_입력은_셀_종류의_판과_전체_판을_흔들지_않는다(monkeypatch, how, name, arg):
    """RB3-6 — 판이 하나뿐이라 요약에만 닿는 손잡이·상수가 도는 셀을 처음부터 돌렸다(#37·리허설 3.3-8). 이제 요약의 판만
    바뀐다. 시도 수(CARD_REVIEW_PARSE_RETRIES)는 cover_hard_max 를 움직이므로 요약의 판에 든다."""
    base = _lim(1_048_576)
    if how == "env":
        now = _lim(1_048_576, env={name: arg})
    else:
        v = getattr(cr, name)
        monkeypatch.setattr(cr, name, {**v, arg: _bump(v[arg])} if arg else _bump(v))
        now = _lim(1_048_576)
    assert now["limits_rev_by_kind"]["summary"] != base["limits_rev_by_kind"]["summary"], name
    for k in cr.CELL_KINDS:
        assert now["limits_rev_by_kind"][k] == base["limits_rev_by_kind"][k], (name, k)
    assert now["limits_rev"] == base["limits_rev"], f"{name} 가 전체 판(M2 앱이 견주는 값)을 흔들었다"


@pytest.mark.parametrize("how,name,arg,still", [
    ("env", "CARD_REVIEW_SHARE_SWEEP", "0.6", ("verdict", "offcard", "noinput", "summary")),
    ("const", "SWEEP_OUT_HEAD", None, ("verdict", "offcard", "noinput", "summary")),
    ("const", "LONGDOC_SHARE", None, ("sweep", "verdict", "noinput", "summary")),
    ("const", "NOINPUT_OUT_HEAD", None, ("sweep", "verdict", "offcard", "summary")),
    ("env", "CARD_REVIEW_K", "5", ("sweep", "offcard", "noinput", "summary")),
])
def test_한_종류에만_닿는_입력은_다른_종류의_판을_흔들지_않는다(monkeypatch, how, name, arg, still):
    base = _lim(1_048_576)
    if how == "env":
        now = _lim(1_048_576, env={name: arg})
    else:
        monkeypatch.setattr(cr, name, _bump(getattr(cr, name)))
        now = _lim(1_048_576)
    for k in still:
        assert now["limits_rev_by_kind"][k] == base["limits_rev_by_kind"][k], (name, k)


def test_단위_상한을_함께_쓰는_세_종류는_서로의_몫에_판이_같이_움직인다():
    """verdict·offcard·noinput 은 단위 상한(셋의 본문 가운데 작은 것)을 함께 쓴다 — noinput 의 몫이 그 상한을 정하면 verdict 의
    판도 바뀌어야 한다(앱이 얼린 단위가 verdict 에서 over_budget 이 되지 않게)."""
    base = _lim(1_048_576)
    now = _lim(1_048_576, env={"CARD_REVIEW_SHARE_NOINPUT": "0.05"})
    assert now["unit_body_max"] != base["unit_body_max"]
    for k in ("verdict", "offcard", "noinput"):
        assert now["limits_rev_by_kind"][k] != base["limits_rev_by_kind"][k], k
    assert now["limits_rev_by_kind"]["sweep"] == base["limits_rev_by_kind"]["sweep"]


def test_판_상수는_어느_종류의_판에든_든다():
    """LIMITS_CONSTANTS 의 이름은 종류 판 무리(_REV_BASE·_REV_KEYED·_REV_UNIT·_REV_ONLY·_REV_CELL) 가운데 하나에 든다."""
    grouped = set(cr._REV_BASE) | set(cr._REV_KEYED) | set(cr._REV_UNIT) | set(cr._REV_CELL)
    grouped |= {n for names in cr._REV_ONLY.values() for n in names}
    assert set(cr.LIMITS_CONSTANTS) == grouped, sorted(set(cr.LIMITS_CONSTANTS) ^ grouped)


def test_run_의_기대_판은_종류_판이면_그_종류만_전체_판이면_전체를_견준다():
    """RB3-6 — expect.limits_rev_kind(M3 앱)는 요청 종류의 판과, expect.limits_rev(M2 앱)는 전체 판과 견준다. 요약 손잡이가
    바뀌어도 셀 종류의 판을 보낸 셀 호출은 지나고, 요약의 옛 판을 보낸 요약 호출만 409 다."""
    from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: PLC0415
    from test_card_review_summary import summary_req  # noqa: PLC0415

    base = _lim(1_048_576)
    changed = _lim(1_048_576, env={"CARD_REVIEW_SHARE_SUMMARY": "0.6"})
    pack = _pack()
    off = _req("offcard", [c for c in pack if not c["checkable"]], _digest(pack), unit=_unit(0))
    off_kind = {**off, "expect": {"limits_rev_kind": base["limits_rev_by_kind"]["offcard"]}}
    assert cr.validate_run_request(off_kind, changed) == [], "요약 손잡이가 카드 밖 호출을 409 로 막았다"
    m2 = {**off, "expect": {"limits_rev": base["limits_rev"]}}
    assert cr.validate_run_request(m2, changed) == [], "요약 손잡이가 M2 앱의 전체 판을 흔들었다"
    sm = {**summary_req(n_rows=5, n_cover=1), "expect": {"limits_rev_kind": base["limits_rev_by_kind"]["summary"]}}
    errs = cr.validate_run_request(sm, changed)
    assert len(errs) == 1 and (errs[0]["status"], errs[0]["code"]) == (409, "rev_mismatch"), errs
    err = errs[0]
    assert err["limits_rev_kind"] == changed["limits_rev_by_kind"]["summary"] and err["expect"] == sm["expect"]
    wrong = {**off, "expect": {"limits_rev_kind": base["limits_rev_by_kind"]["verdict"]}}
    errs = cr.validate_run_request(wrong, base)
    assert errs and errs[0]["code"] == "rev_mismatch", "다른 종류의 판을 보냈는데 지났다"
    long_want = {**off, "expect": {"limits_rev_kind": "x" * 5_000}}
    errs = cr.validate_run_request(long_want, base)
    assert len(errs) == 1 and len(errs[0]["expect"]["limits_rev_kind"]) < 100 and len(errs[0]["message"]) < 600, (
        "기대 판을 끊지 않고 되비췄다(RB3-15)")


@pytest.mark.parametrize("mt", [300, 800, 2_000])
def test_출력_상한이_행_한_줄도_못_내면_셀_종류는_돌지_않고_올릴_값을_말한다(mt):
    """RB3-14 — CARD_REVIEW_MAX_TOKENS 300·800 이면 k_out=0 인데도 셀 종류가 runnable 이었다. 출력 몫이 행 한 줄도 못 내는
    종류는 runnable=false 이고 ctx_too_small{reason: output_cap, need_max_tokens} 다(창을 키워도 소용없다)."""
    from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: PLC0415

    lim = _lim(1_048_576, mt)
    assert not all(lim["runnable"][k] for k in cr.CELL_KINDS), lim["runnable"]
    small = lim["ctx_too_small"]
    assert (small["reason"], small["need_ctx_tokens"]) == ("output_cap", None) and small["need_max_tokens"] > mt
    assert "CARD_REVIEW_MAX_TOKENS" in small["message"]
    ok = _lim(1_048_576, small["need_max_tokens"])
    assert all(ok["runnable"][k] for k in cr.CELL_KINDS) and "ctx_too_small" not in ok and ok["verdict"]["k_out"] >= 1
    pack = _pack()
    req = _req("offcard", [c for c in pack if not c["checkable"]], _digest(pack), unit=_unit(0))
    errs = cr.validate_run_request(req, lim)
    assert len(errs) == 1 and (errs[0]["code"], errs[0].get("reason")) == ("ctx_too_small", "output_cap"), errs


def test_창이_모자란_ctx_too_small_은_reason_window_다():
    small = _lim(8_192)["ctx_too_small"]
    assert small["reason"] == "window" and small["need_ctx_tokens"] > 8_192
    assert small["reason_by_kind"] == dict.fromkeys(cr.CELL_KINDS, "window")


def test_ctx_too_small_의_사유는_종류마다이고_필요한_창과_출력_상한을_함께_싣는다():
    """RB3B-10 — 창 8,192·출력 상한 1,000 이면 출력 상한이 모자란 것은 offcard·noinput 이고 sweep·verdict 는 창이 모자란데,
    사유 하나(output_cap)를 모든 셀 종류의 422 에 복사했다. 그 값(3,175)으로 올리면 그제야 창이 모자라다(11,710)고 했다 —
    두 걸음. 사유는 종류마다이고, 창은 올린 출력 상한으로 재 둘을 함께 싣는다."""
    from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: PLC0415

    lim = _lim(8_192, 1_000)
    small = lim["ctx_too_small"]
    assert small["reason_by_kind"] == {"sweep": "window", "verdict": "window", "offcard": "output_cap",
                                       "noinput": "output_cap"}, small["reason_by_kind"]
    assert small["reason"] == "output_cap" and small["need_max_tokens"] == 3_175
    raised = _lim(8_192, small["need_max_tokens"])["ctx_too_small"]
    assert raised["reason"] == "window" and small["need_ctx_tokens"] == raised["need_ctx_tokens"] > 8_192, (small, raised)
    assert all(_lim(small["need_ctx_tokens"], small["need_max_tokens"])["runnable"][k] for k in cr.CELL_KINDS)
    assert "3,175" in small["message"] and f"{small['need_ctx_tokens']:,}" in small["message"]
    pack = _pack()
    reqs = {"sweep": _req("sweep", pack, [], manifest=[{"unit_id": "u", "kind": "bundle", "title": "t", "text": "x"}]),
            "offcard": _req("offcard", [c for c in pack if not c["checkable"]], _digest(pack), unit=_unit(0))}
    for kind, want in (("sweep", "window"), ("offcard", "output_cap")):
        errs = cr.validate_run_request(reqs[kind], lim)
        assert len(errs) == 1 and errs[0]["code"] == "ctx_too_small", errs
        e = errs[0]
        assert (e["reason"], e["need_ctx_tokens"], e["need_max_tokens"]) == (want, small["need_ctx_tokens"], 3_175), e


def test_plan_은_계획과_같은_때의_종류별_판을_싣는다():
    """결정(RB3B) — plan 응답은 전역 limits_rev 만 실어, 앱이 계획과 같은 때의 종류별 판을 한 번에 받을 길이 없었다. 읽기
    전용으로 limits_rev_by_kind 를 더한다(plan 의 expect 는 여전히 limits_rev_kind 를 받지 않는다)."""
    lim = _lim(1_048_576)
    out = cr.plan([{"alias": "K01", "record_id": "R1", "checkable": True, "chars": 500}],
                  [{"unit_id": "u", "manifest_chars": 300, "body_chars": 2_000}], lim)
    assert out["limits_rev_by_kind"] == lim["limits_rev_by_kind"] and out["limits_rev"] == lim["limits_rev"]
    with pytest.raises(cr.RequestError) as e:
        cr.check_shape(cr.PlanRequest, {"cards": [], "units": [], "expect": {"limits_rev_kind": "x"}})
    assert e.value.code == "unknown_fields"



def test_거절은_호출자_값을_80자_안으로_되비춘다():
    """RB3-15 — 422 가 종류·모르는 칸 이름·must_cover 의 ref 를 길이·개수 제한 없이 되비췄다."""
    from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: PLC0415
    from test_card_review_summary import summary_req  # noqa: PLC0415

    lim = _lim(1_048_576)
    pack = _pack()
    base = _req("offcard", [c for c in pack if not c["checkable"]], _digest(pack), unit=_unit(0))
    many = {**base, **{("칸" * 3_000) + str(i): 1 for i in range(50)}}
    errs = cr.validate_run_request(many, lim)
    assert len(errs) == 1, errs
    e = errs[0]
    assert e["code"] == "unknown_fields" and e["fields_n"] == 50 and len(e["fields"]) == 20, str(e)[:300]
    assert all(len(f) <= 100 for f in e["fields"]) and len(e["message"]) < 3_000 and len(str(e["errors"])) < 6_000
    for bad in ({**base, "kind": "k" * 5_000}, {**base, "kind": "hunt" + "x" * 5_000}):
        errs = cr.validate_run_request(bad, lim)
        assert len(errs) == 1 and len(errs[0]["message"]) < 300, errs[0]["message"][:120]
    sm = summary_req(n_rows=3, n_cover=1)
    sm["summary"]["must_cover"] = ["r" * 5_000 + str(i) for i in range(5)]
    errs = cr.validate_run_request(sm, lim)
    assert len(errs) == 1 and errs[0]["code"] == "bad_request" and len(errs[0]["message"]) < 800

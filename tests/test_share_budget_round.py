# 다른 좌석의 조회 결과 예산(_share_budget) — 풀이 쌓이는 만큼 라운드에 비례해 늘고, 창은 넘지 않는다
#
# 좌석들의 자유 조회 결과는 라운드를 넘어 한 풀에 쌓이는데 좌석에 실어 주는 예산은 고정이었다 —
# 라운드가 갈수록 '예산 밖 N건 생략' 의 비율이 커졌다(S26U 피드백 1-13).
#
# **스트림을 실제로 돌려서** 라운드마다 실린 수와 빠진 수를 상태줄에서 읽는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_share_budget_round.py -q
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_SEATS = [{"key": f"dom{i:02d}-seat", "role": "역할"} for i in range(1, 7)]
_LINE = re.compile(r"다른 좌석의 조회 결과 — 좌석당 최대 (\d+)건 전달(?: · 예산 밖 최대 (\d+)건 생략)? \(1인당 ([\d,]+)자")


def _ctx(tokens):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()


def _bound():
    return max(600, int(d._pre_budget() * 0.15))


# ── 예산 ─────────────────────────────────────────────────────────────────────
def test_넓은_창에서는_라운드마다_는다():
    _ctx(1000000)
    got = [d._share_budget(r) for r in range(1, 9)]
    assert got == sorted(set(got)), f"라운드가 가도 늘지 않는다 — {got}"
    assert got[0] == d._SHARE_BUDGET, "1라운드는 종전 값 그대로여야 한다"
    assert d._share_budget() == got[0], "라운드를 안 준 호출은 1라운드 값이다"


@pytest.mark.parametrize("tokens", [16384, 128000, 200000, 1000000])
def test_어느_라운드에서도_컨텍스트_쪽_상한을_넘지_않는다(tokens):
    _ctx(tokens)
    for r in range(1, 9):
        assert d._share_budget(r) <= _bound(), (tokens, r, d._share_budget(r), _bound())
        assert d._share_budget(r) >= d._share_budget(max(1, r - 1)), "라운드가 갔는데 줄었다"


@pytest.mark.parametrize("tokens", [16384, 128000])
def test_좁은_창은_1라운드부터_상한이라_늘지_않는다(tokens):
    """좁은 창에서는 컨텍스트 쪽 상한이 천장(_SHARE_BUDGET)보다 작다 — 거기서 더 주면 좌석이 넘친다."""
    _ctx(tokens)
    assert _bound() < d._SHARE_BUDGET, "시험 전제 — 이 창에서는 컨텍스트 쪽 상한이 먼저 걸린다"
    assert {d._share_budget(r) for r in range(1, 9)} == {_bound()}


# ── 스트림 — 풀이 쌓여도 빠지는 비율이 커지지 않는다 ────────────────────────────────
def _run(monkeypatch, tokens, rounds=4):
    """좌석마다 라운드마다 조회 셋을 하는 심의를 끝까지 돌려, 라운드별 (실린 수, 빠진 수, 1인당 예산)을
    받는다. 풀은 라운드마다 좌석 × 3건씩 는다."""
    import langgraph.prebuilt

    _ctx(tokens)
    calls = {"n": 0}

    async def _llm(_llm_obj, system, _human):
        if "전문가입니다" not in system:
            return "결정문 본문"
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A", "B"],
                           "position_short": "요약", "concede": [], "rebut": ["반박"], "deepen": "심화",
                           "final_position": "최종", "non_negotiable": "", "vote": "진행", "stance": "동의"},
                          ensure_ascii=False)

    async def _gather(_agent, persona, *_a, **_k):
        out = []
        for _ in range(3):
            calls["n"] += 1
            out.append(("get_material", json.dumps({"id": calls["n"]}), "값 " + "x" * 380, f"c{calls['n']}"))
        return persona["key"], out, "- 조회 요약", ""

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: object())
    events = _stream(monkeypatch, {"personas": _SEATS, "rounds": rounds, "save_report": 0, "rebut_quote": 0,
                                   "free_tools": 1, "persona_knowledge": 0},
                     tools={"agent_search": _Tool("agent_search")}, until=lambda ev, _data: ev == "done")
    rows = [_LINE.search(s) for s in _steps(events)]
    rows = [(int(m.group(1)), int(m.group(2) or 0), int(m.group(3).replace(",", ""))) for m in rows if m]
    assert len(rows) == rounds, f"라운드마다 한 줄이어야 한다 — {rows}"
    return rows


def test_넓은_창에서는_라운드가_가도_빠지는_비율이_커지지_않는다(monkeypatch):
    rows = _run(monkeypatch, 1000000)
    budgets = [b for _n, _d, b in rows]
    assert budgets == [d._SHARE_BUDGET * r for r in (1, 2, 3, 4)], f"호출부가 라운드를 안 넘긴다 — {budgets}"
    rate = [dropped / (shown + dropped) for shown, dropped, _b in rows]
    assert rate[0] > 0, "시험 전제 — 1라운드부터 예산이 모자라야 한다"
    # 조회가 도는 라운드(수렴 전)끼리 본다. 고정 예산이면 0.3 → 0.6 → 0.75 로 는다.
    assert max(rate[1:3]) <= rate[0] + 0.05, f"라운드가 갈수록 더 많이 빠진다 — {[round(x, 2) for x in rate]}"


def test_좁은_창에서는_좌석에_싣는_양이_라운드가_가도_그대로다(monkeypatch):
    rows = _run(monkeypatch, 128000)
    assert {b for _n, _d, b in rows} == {_bound()}, rows

# 챗 경로(app.py)의 고정 상한을 모델 창에서 유도한다 — 챗→심의 핸드오프·도구 결과·수치 대조 원문·유령 ID 게이트
#
# 사용자 지침(2026-10-10) — 운영 모델은 GLM-5.2, 창 1,048,576 토큰이다. 챗에서 심의로 넘기는 도구 결과(result_full)가
# 4,000자였고(심의는 항목 150,000자를 받는다), 결정문 수치 대조의 원문은 결과마다 앞 120,000자였으며(모델은 200,000자까지
# 봤다), 유령 ID 게이트는 정수 2,000개를 넘으면 그 턴의 도구 결과 ID 를 더 배우지 않았다. 좁은 창(dev 16K)은 종전대로다.
#
# 앞의 **특성화** 시험은 구현 전 코드에서 초록인 것을 먼저 본 것이다.
#
#   실행:  .venv/bin/python -m pytest tests/test_chat_window_caps.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

_SMALL = (16384, 128000, 200000)
_BIG = 1048576


@pytest.fixture(autouse=True)
def _restore_ctx():
    saved = dict(app._ctx_cache)
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


def _win(tokens):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()


_RESULT = "\n".join(f"| 행{i:05d} | 값 {i} |" for i in range(4000)) + "\n| 끝 | [결과-끝] |"


# ── 챗→심의 핸드오프(result_full·detail_full) ───────────────────────────────────────────
@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_핸드오프는_종전대로_4000자다(tokens):
    _win(tokens)
    assert app._handoff_text(_RESULT) == app._tool_preview(_RESULT, 4000)


def test_넓은_창의_핸드오프는_심의가_받는_항목_길이까지_싣는다():
    """엔진은 근거 항목을 150,000자까지 받는데 챗이 넘기는 날것은 4,000자였다 — 표의 앞 몇 줄만 심의에 갔다."""
    _win(_BIG)
    got = app._handoff_text(_RESULT)
    assert "[결과-끝]" in got and "…#" not in got, len(got)
    huge = "가" * (d._EVID_ITEM_MAX + 5000)
    cut = app._handoff_text(huge)
    assert len(cut) <= d._EVID_ITEM_MAX + 10 and cut.endswith(cut[-7:]) and "…#" in cut


def test_핸드오프_길이를_명시하면_그_값이다(monkeypatch):
    monkeypatch.setattr(app, "HANDOFF_RESULT_CHARS", 1000)
    monkeypatch.setattr(app, "_HANDOFF_SET", True)
    _win(_BIG)
    assert len(app._handoff_text(_RESULT)) <= 1010


# ── 도구 결과 상한 ─────────────────────────────────────────────────────────────────
def test_도구_결과_상한_0_은_무제한이다(monkeypatch):
    """주석은 '0 이면 무제한' 인데 코드는 0자로 잘라 경고만 남겼다."""
    monkeypatch.setattr(app, "TOOL_RESULT_MAX", 0)
    assert app._cap("가" * 50) == "가" * 50


def test_도구_결과를_자르면_원문_길이_표식이_붙는다():
    s = app._cap("가" * 300, 100)
    assert s.startswith("가" * 100) and app._CAP_MARK in s and "300자" in s


# ── 결정문 수치 대조의 원문(turn_out) ────────────────────────────────────────────────
@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_수치_대조_원문은_종전대로_결과마다_120000자_합_400000자다(tokens):
    _win(tokens)
    out: list = []
    for _ in range(5):
        app._turn_out_add(out, "가" * 150000)
    assert [len(x) for x in out] == [120000] * 4, [len(x) for x in out]


def test_넓은_창의_수치_대조_원문은_모델이_본_결과를_자르지_않는다():
    _win(_BIG)
    out: list = []
    for _ in range(5):
        app._turn_out_add(out, "가" * 150000 + " 9191.5")
    assert len(out) == 5 and all(x.endswith("9191.5") for x in out), [len(x) for x in out]


# ── 유령 ID 게이트 — 출처 정수 상한 ────────────────────────────────────────────────────
def _learned(tokens, seeds):
    _win(tokens)
    tok = app._turn_ids.set(set(range(100000, 100000 + seeds)))
    try:
        app._learn_ids('{"id": 77}')
        return 77 in app._turn_ids.get()
    finally:
        app._turn_ids.reset(tok)


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_유령_ID_게이트는_종전대로_정수_2000개에서_멈춘다(tokens):
    assert _learned(tokens, 10) and not _learned(tokens, 2000)


def test_넓은_창의_유령_ID_게이트는_긴_이력에서도_도구_결과의_ID_를_배운다():
    """이력이 길면 씨앗만으로 2,000개를 넘어, 그 턴에 도구가 돌려준 ID 를 다음 호출이 쓰면 '출처 없다' 로 막혔다."""
    assert _learned(_BIG, 5000)


def test_도구_결과가_잘리면_챗_스트림이_상태줄로_알린다():
    """모델만 알던 절단을 사람도 본다 — 완료 이벤트 근처에서 표지(_CAP_MARK)를 보고 상태줄을 낸다."""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    i = src.index("_turn_out_add(turn_out, _txt)")
    blk = src[i:i + 900]
    assert "_CAP_MARK in _txt" in blk and '"knob": "TOOL_RESULT_MAX"' in blk, blk

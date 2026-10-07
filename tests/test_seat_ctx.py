# 좌석 프롬프트 상한(_fit_rows)의 회귀 테스트 — 평범한 라운드는 그대로, 폭주만 좌석별 같은 몫으로
#
#   실행:  .venv/bin/python -m pytest tests/test_seat_ctx.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402


def _rows(n, each):
    return [(f"k{i}", "가" * each) for i in range(n)]


def test_상한_안이면_한_글자도_안_바뀐다():
    rows = _rows(12, 2000)          # 발언 중앙값 2,000자 × 12석 = 24K
    out, share = d._fit_rows(rows, 48000)
    assert share == 0 and out == rows


def test_상한_0_은_무제한이다():
    rows = _rows(15, 10000)
    out, share = d._fit_rows(rows, 0)
    assert share == 0 and out == rows


def test_넘치면_좌석마다_같은_몫이고_전원이_남는다():
    # 실측 폭주 — 15석 × 약 11,000자(수렴 직전 라운드 164,854자)
    rows = _rows(15, 11000)
    out, share = d._fit_rows(rows, 48000)
    assert share == 48000 // 15
    assert [k for k, _ in out] == [k for k, _ in rows], "좌석이 빠졌다 — 머리·꼬리 절단이 아니어야 한다"
    body = sum(len(t) for _, t in out)
    assert body <= 48000 + 15 * 40, f"상한을 크게 넘었다({body:,}자)"   # 좌석당 표지 문구 여유


def test_짧은_발언은_자르지_않는다():
    # 몫보다 짧은 좌석은 원문 그대로 — 긴 좌석 때문에 짧은 좌석까지 깎이면 안 된다.
    rows = [("long", "가" * 60000), ("short", "나" * 300)]
    out, share = d._fit_rows(rows, 20000)
    assert dict(out)["short"] == "나" * 300
    assert len(dict(out)["long"]) < 60000


def test_잘린_좌석엔_몇_자_중_몇_자인지_남긴다():
    out, share = d._fit_rows([("a", "가" * 9000), ("b", "나" * 9000)], 4000)
    assert "9,000자 중 앞 2,000자" in dict(out)["a"]


def test_좌석이_아주_많아도_몫에_바닥이_있다():
    # 좌석 100석이면 몫이 480자인데, 그러면 발언이 한 문장도 안 남는다 → floor 1,200자.
    out, share = d._fit_rows(_rows(100, 5000), 48000)
    assert share == 1200


def test_기본값은_평범한_20석_라운드를_건드리지_않는다():
    # 상한을 올린 좌석 수(20)에서 중앙값 발언이면 자르지 않아야 한다 — 품질 회귀 방지선.
    assert d._SEAT_CTX >= 20 * 2000
    assert d.MAX_REQ_SEATS == 20


# ── 짧은 발언이 안 쓴 몫은 긴 발언에 돌린다 ──────────────────────────────────────────
def _uneven():
    # 발언 길이는 고르지 않다 — 긴 좌석 10석(2,900자)과 짧은 좌석 11석(700자), 합 36,700자.
    return [(f"long{i}", "가" * 2900) for i in range(10)] + [(f"short{i}", "나" * 700) for i in range(11)]


def test_짧은_발언이_남긴_몫을_긴_발언이_쓴다():
    """그냥 좌석 수로 나누면 짧은 발언이 안 쓴 몫이 버려진다 — 예산이 7,000자 남는데도 긴 발언을
    1,333자에서 끊었다(실을 수 있는 것을 버린다)."""
    out, share = d._fit_rows(_uneven(), 28000)
    kept = sum(min(len(t), share) for _, t in _uneven())
    assert kept <= 28000, f"예산을 넘겼다({kept:,}자)"
    assert kept >= 28000 - 21, f"예산 28,000자 중 {kept:,}자만 실었다 — 남는 몫을 버렸다"
    assert share == (28000 - 11 * 700) // 10
    got = dict(out)
    assert all(got[f"short{i}"] == "나" * 700 for i in range(11)), "짧은 발언을 건드렸다"
    assert all(got[f"long{i}"].startswith("가" * share + " …[2,900자 중 앞") for i in range(10))


def test_전부_몫보다_길면_종전과_같다():
    rows = [("a", "가" * 9000), ("b", "나" * 5000), ("c", "다" * 7000)]
    _out, share = d._fit_rows(rows, 6000)
    assert share == 2000


def test_어떤_길이_분포에서도_예산_안이고_남기지_않는다():
    import random

    rng = random.Random(20261007)
    for _ in range(300):
        n = rng.randint(1, 25)
        rows = [(f"k{i}", "가" * rng.choice((rng.randint(1, 400), rng.randint(400, 3000), rng.randint(3000, 9000))))
                for i in range(n)]
        total = sum(len(t) for _, t in rows)
        budget = rng.randint(n, max(n + 1, total + 500))
        out, share = d._fit_rows(rows, budget, floor=1)
        if total <= budget:
            assert share == 0 and out == rows
            continue
        kept = sum(min(len(t), share) for _, t in rows)
        assert share >= 1 and kept <= budget, (budget, share, kept)
        assert kept > budget - n, f"예산 {budget:,}자 중 {kept:,}자만 실었다(좌석 {n}석)"
        assert all(got == t for (_, t), (_, got) in zip(rows, out) if len(t) <= share), "몫보다 짧은 발언을 건드렸다"
        assert [k for k, _ in out] == [k for k, _ in rows], "좌석이 빠지거나 순서가 바뀌었다"

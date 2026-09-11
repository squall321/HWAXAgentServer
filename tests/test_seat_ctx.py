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

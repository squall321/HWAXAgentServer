# 수치 대조의 같은 값 판정(HWAXPortal docs/request-1125 OF-3) · id 꼴 빼기(WD-12) 시험 — 챗 · 결정문 · 위임이 같이 쓰는 evidence 판정
#
#   실행:  .venv/bin/python -m pytest tests/test_evidence_r1125.py -q
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import evidence as ev  # noqa: E402
import offload  # noqa: E402


# ── OF-3 같은 값의 다른 꼴 ──────────────────────────────────────────────────────────────────────────────────────
def test_OF3_SI_접두_환산은_같은_값이다():
    src = "탄성계수 2350000000 Pa · 항복 강도 276000000 Pa"
    assert ev.unsourced_numbers("탄성계수는 2.35 GPa, 항복은 276 MPa 다", src, limit=0) == []
    assert ev.unsourced_numbers("탄성계수는 2,350,000,000 Pa", "E = 2.35 GPa", limit=0) == []


def test_OF3_반올림은_답의_소수_자리로_같은_값이다():
    assert ev.unsourced_numbers("합계 5,717.12", "sum=5717.116", limit=0) == []
    assert ev.unsourced_numbers("합계 5,717", "sum=5717.116", limit=0) == []
    assert ev.unsourced_numbers("합계 5,717.13", "sum=5717.116", limit=0) == ["5,717.13"], "반올림으로 안 나오는 값은 어긋남"


def test_OF3_비와_퍼센트는_퍼센트가_붙었을_때만_같은_값이다():
    assert ev.unsourced_numbers("불량률 12.5%", "defect_ratio: 0.125", limit=0) == []
    assert ev.unsourced_numbers("불량률 0.125", "불량률 12.5 %", limit=0) == []
    assert ev.unsourced_numbers("값 12.5", "ratio 0.125", limit=0) == ["12.5"], "% 가 어디에도 없으면 ×100 으로 맞추지 않는다"


def test_OF3_쉼표와_숫자_묶음_공백을_뗀_원문으로_본다():
    assert ev.unsourced_numbers("총 2350000 건", "총 2 350 000 건", limit=0) == []
    assert ev.unsourced_numbers("총 2,350,000 건", "총 2 350 000 건", limit=0) == []


def test_OF3_일부러_틀린_수는_여전히_잡힌다():
    src = "탄성계수 2350000000 Pa · 합계 5717.116 · 비율 0.125"
    bad = ev.unsourced_numbers("탄성계수 2.53 GPa · 합계 5,771.12 · 비율 21.5% · 새 값 9876.5", src, limit=0)
    assert bad == ["2.53", "5,771.12", "21.5", "9876.5"]


def test_OF3_위임_숫자_대조는_종류_계산한_수_같은_값을_따로_싣는다():
    src = "작년 1200 건 · 올해 1500 건 · E 2350000000 Pa · 합계 5717.116 · 질량 12345.6"
    ans = "작년 1,200건에서 올해 1,500건으로 25.0% 늘었다(계산). E 2.35 GPa · 합계 5,717.12 · 질량 123456 · 이상한 값 9876.5 · 1,240.3"
    got = offload.numbers(ans, src)
    assert got["mismatched"] == ["123456", "9876.5", "1,240.3"]
    assert got["kinds"] == {"123456": "환산 의심", "9876.5": "원문에 없음", "1,240.3": "원문에 없음"}
    assert got["derived"] == [{"value": "25.0", "op": "증가율 %", "from": ["1,500", "1,200"]}]
    assert {e["value"] for e in got["equivalent"]} == {"2.35", "5,717.12"}
    assert got["checked"] == 8


def test_OF3_반올림_의심은_가까운_값이_있을_때다():
    pool = ev.SourcePool("측정 5717.116")
    assert pool.suspect("5720.0") == "반올림 의심" and pool.suspect("6100.5") == "원문에 없음"


def test_OF3_원문이_없으면_종전처럼_모두_어긋남이고_빈_칸은_싣지_않는다():
    assert offload.numbers("값 123.4", "") == {"checked": 1, "mismatched": ["123.4"]}
    assert offload.numbers("값 123.4", "값 123.4") == {"checked": 1, "mismatched": []}

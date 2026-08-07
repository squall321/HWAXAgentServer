# 좌석 구성 계층의 회귀 테스트 — 커버리지 게이트·승계·앵커링 차단이 조용히 죽는 것을 막는다.
#
# 좌석 계층은 실패해도 심의가 정상 완료되므로(사람이 덜 앉을 뿐) 회귀를 눈으로 못 잡는다.
# 그래서 순수 함수 단위로 고정해 둔다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_seats.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402


# ── 도메인 판정 ──────────────────────────────────────────────────────────────
def test_도메인은_첫_하이픈_앞이다():
    assert d._dom_of("rel-chemical-corrosion") == "rel"
    assert d._dom_of("mech-psa-bonding") == "mech"
    assert d._dom_of("disp-color-calibration") == "disp"


def test_하이픈_없는_키와_빈값():
    assert d._dom_of("thermal") == "thermal"
    assert d._dom_of("") == ""
    assert d._dom_of(None) == ""


def test_하네스와_같은_규칙을_쓴다():
    """계측(tools/delib_metrics)과 구현(deliberation)의 도메인 판정이 어긋나면
    '다양성이 올랐다'는 측정이 실제 좌석 구성과 다른 것을 재게 된다."""
    from tools.delib_metrics import _domain
    for k in ("disp-upc", "sw-color-management", "rel-chemical-corrosion", "thermal"):
        assert d._dom_of(k) == _domain(k), k


# ── 좌석 기록 ────────────────────────────────────────────────────────────────
def test_좌석_기록에_성격과_도메인_수가_남는다():
    note = d._seat_note([
        {"key": "disp-burnin", "origin": "primary"},
        {"key": "disp-upc", "origin": "primary"},
        {"key": "rel-chemical-corrosion", "origin": "counter"},
    ])
    assert "주 도메인 2명" in note and "반대 도메인 1명" in note
    assert "도메인 2종" in note and "disp" in note and "rel" in note


def test_이어하기_좌석도_라벨이_구분된다():
    note = d._seat_note([
        {"key": "sw-color-management", "origin": "carry"},
        {"key": "svc-field-ops", "origin": "new"},
    ])
    assert "유임 1명" in note and "이어하기 신규 1명" in note


def test_origin_이_없으면_주_도메인으로_본다():
    assert "주 도메인 1명" in d._seat_note([{"key": "disp-upc"}])


# ── 양보 불가 조항 승계(F11) ─────────────────────────────────────────────────
def test_승계_조항을_요청에서_읽는다():
    o = d._resolve_opts({"non_negotiables": ["리셋 전 교체 판정 금지", "파형 미고정 측정 무효"]})
    assert o.continue_non_negotiables == ["리셋 전 교체 판정 금지", "파형 미고정 측정 무효"]


def test_구_필드명도_받는다():
    o = d._resolve_opts({"continue_non_negotiables": ["조항 하나"]})
    assert o.continue_non_negotiables == ["조항 하나"]


def test_기본은_빈_목록이다():
    """미지정 이어하기가 조항을 지어내면 안 된다."""
    assert d._resolve_opts({}).continue_non_negotiables == []
    assert d._resolve_opts({"non_negotiables": "문자열은 무시"}).continue_non_negotiables == []


def test_건수와_길이를_제한한다():
    o = d._resolve_opts({"non_negotiables": [f"조항{i}" for i in range(20)] + ["x" * 3000]})
    assert len(o.continue_non_negotiables) == 12
    assert all(len(x) <= 1200 for x in o.continue_non_negotiables)


def test_빈_문자열은_버린다():
    o = d._resolve_opts({"non_negotiables": ["실제 조항", "", "   "]})
    assert o.continue_non_negotiables == ["실제 조항"]


def test_좌석_지정과_독립이다():
    """초판은 조항 파싱을 personas 분기 안에 넣어, 좌석을 안 주면 조항도 사라졌다."""
    o = d._resolve_opts({"non_negotiables": ["조항"]})
    assert o.continue_non_negotiables == ["조항"] and o.continue_personas == []


# ── 손잡이 기본값 ────────────────────────────────────────────────────────────
def test_좌석_손잡이는_기본_켜짐이고_탈출구가_있다():
    """깊이 회복 손잡이 7종과 달리 좌석 손잡이는 프롬프트 제약을 늘리지 않으므로 기본 켜짐이다.
    회귀 시 코드 롤백 없이 0 으로 되돌릴 수 있어야 한다."""
    assert d._COUNTER_SEATS >= 1 and d._RESCREEN == 1 and d._RESCREEN_SEATS >= 1


def test_깊이_회복_손잡이는_기본_꺼짐을_유지한다():
    """단일 변수 A/B 원칙(GLM-DELIB-TUNING-REVIEW §T-서열) — 임의 활성화 금지."""
    for name in ("_EVIDENCE_PREPASS", "_REBUT_QUOTE", "_PROSE_FIRST", "_CROSS_EXAM",
                 "_ANCHOR", "_CHAIR_CITE"):
        assert getattr(d, name) == 0, f"{name} 이 켜져 있다 — A/B 판정이 오염된다"
    assert d._CHAIR_BESTOF == 1


if __name__ == "__main__":  # pytest 없이도 돌릴 수 있게
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as exc:
                fails += 1
                print(f"  ✗ {name}: {exc}")
    print(f"\n{'실패 ' + str(fails) + '건' if fails else '전부 통과'}")
    sys.exit(1 if fails else 0)

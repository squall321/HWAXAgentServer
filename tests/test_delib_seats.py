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


# ── 이어하기 블록 구성(F11) ──────────────────────────────────────────────────
def test_승계_조항이_구속_문구와_함께_실린다():
    """조항만 나열하면 참고 자료로 읽힌다 — 구속력과 폐기 조건을 함께 박아야 한다."""
    b = d._cont_block("이전 결론", ["리셋 전 교체 금지", "파형 미고정 측정 무효"], "")
    assert "리셋 전 교체 금지" in b and "파형 미고정 측정 무효" in b
    assert "구속력" in b and "새 근거" in b


def test_조항_전건이_빠짐없이_들어간다():
    """승계 누락은 조용히 일어나므로 건수로 확인한다."""
    nn = [f"조항 {i}" for i in range(1, 8)]
    b = d._cont_block("요약", nn, "의견")
    assert all(x in b for x in nn)


def test_조항이_없으면_조항_블록도_없다():
    b = d._cont_block("이전 결론", [], "")
    assert "양보 불가" not in b and "이전 결론" in b


def test_신규_심의는_빈_블록이다():
    assert d._cont_block("", [], "") == ""


def test_사람_의견은_방향_지시로_들어간다():
    assert "반드시 반영" in d._cont_block("", [], "이 관측을 다뤄라")


# ── 근거 프로파일(F2) ────────────────────────────────────────────────────────
def test_조회_0건이면_가설_단계로_표기된다():
    """S26U 결정문은 정량 계측 0건 위에서 확정 결론과 같은 형식으로 나왔다 — 그걸 막는 표기다."""
    note = d._evidence_note({"tool": 0, "knowledge": 30, "voc": 1, "prepass": 0})
    assert "가설 단계" in note and "도구 조회 0건" in note


def test_조회가_있으면_가설_표기가_없다():
    note = d._evidence_note({"tool": 12, "knowledge": 30, "voc": 1, "prepass": 2})
    assert "가설 단계" not in note and "도구 조회 12건" in note


def test_모든_근거_종류가_프로파일에_남는다():
    note = d._evidence_note({"tool": 3, "knowledge": 4, "voc": 5, "prepass": 6})
    for n in ("3건", "4건", "5건", "6건"):
        assert n in note


# ── 시뮬레이션 심의(2단) ─────────────────────────────────────────────────────
def test_시뮬_트리거를_일반_심의와_구분한다():
    assert d.is_sim_deliberation("/시뮬심의 액자형 수축")
    assert d.is_sim_deliberation("/시뮬레이션심의 낙하 크랙")
    assert not d.is_sim_deliberation("/심의 일반 주제")
    assert not d.is_sim_deliberation("시뮬심의 접두사 없음")


def test_시뮬_트리거를_떼어낸다():
    assert d.strip_sim_trigger("/시뮬심의  액자형 수축") == "액자형 수축"


def test_의장_템플릿_3종이_있고_기본은_종전이다():
    """미지정이 종전과 같아야 기존 심의에 회귀가 없다."""
    assert set(d._CHAIR_ITEMS) == {"default", "mechanism", "sim-plan"}
    assert d._resolve_opts({}).chair_template == "default"
    assert d._resolve_opts({"chair_template": "sim-plan"}).chair_template == "sim-plan"


def test_모르는_템플릿은_기본으로_떨어진다():
    """오타·구버전 클라이언트가 의장 프롬프트를 비우지 못하게."""
    assert d._resolve_opts({"chair_template": "없는것"}).chair_template == "default"
    assert d._resolve_opts({"chair_template": 123}).chair_template == "default"


def test_해석계획서_템플릿이_식별성과_한계를_강제한다():
    """이 둘이 '그럴듯한 계획서'와 '실제로 돌릴 수 있는 계획'을 가른다."""
    t = d._CHAIR_ITEMS["sim-plan"]
    assert "식별성" in t and "퇴화" in t
    assert "답할 수 없는 것" in t
    assert "비워두지 마라" in t


def test_메커니즘_템플릿이_2단_입력을_뽑는다():
    """상태변수·지배방정식 후보·미지 파라미터가 없으면 해석 설계가 시작될 수 없다."""
    t = d._CHAIR_ITEMS["mechanism"]
    for k in ("상태변수", "지배방정식", "미지 파라미터", "반증 관측"):
        assert k in t, k


def test_고정_CAE_좌석은_발굴에_맡기지_않는다():
    """현상 어휘에 끌려 방법론·검증 좌석이 빠지는 것을 막는다."""
    assert "xd-cae-modeling" in d._SIM_FIXED_CAE and "xd-cae-post" in d._SIM_FIXED_CAE


def test_물리_유임_좌석이_최소_1석은_보장된다():
    """CAE 만 모으면 틀린 물리를 아름답게 계산한다."""
    assert d._SIM_CARRY >= 1


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

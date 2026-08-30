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


def test_의장_템플릿_8종이_있고_기본은_종전이다():
    """미지정이 종전(default)과 같아야 기존 심의에 회귀가 없다. 신규 3종·build-plan 미러 포함(JS 와 정합)."""
    assert set(d._CHAIR_ITEMS) == {
        "default", "mechanism", "sim-plan", "test-plan",
        "diagnosis", "option-select", "credibility", "build-plan",
    }
    assert d._resolve_opts({}).chair_template == "default"
    assert d._resolve_opts({"chair_template": "sim-plan"}).chair_template == "sim-plan"
    assert d._resolve_opts({"chair_template": "credibility"}).chair_template == "credibility"


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
    """단일 변수 A/B 원칙(GLM-DELIB-TUNING-REVIEW §T-서열) — 임의 활성화 금지.
    _REBUT_QUOTE 는 인용 반박 계약이 운영 표준으로 승격돼 기본 ON(deliberation.py:86-87)이라 제외한다."""
    for name in ("_EVIDENCE_PREPASS", "_PROSE_FIRST", "_CROSS_EXAM", "_ANCHOR", "_CHAIR_CITE"):
        assert getattr(d, name) == 0, f"{name} 이 켜져 있다 — A/B 판정이 오염된다"
    assert d._REBUT_QUOTE == 1  # 의도된 기본 ON(운영 표준) — 끄려면 DELIB_REBUT_QUOTE=0
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


# ── 시험 계획 심의 ────────────────────────────────────────────────────────────
# 해석은 물성이 없으면 시작할 수 없다. "무엇을 먼저 측정할 것인가" 는 우선순위 문제이고,
# 우선순위 없는 목록은 계획서가 아니라 희망 목록이다.
def test_시험계획_트리거가_다른_모드와_겹치지_않는다():
    assert d.is_test_plan("/시험계획 낙하 물성 확보")
    assert d.is_test_plan("/DOE 폴더블 벤딩")
    assert not d.is_test_plan("/심의 낙하 불량 원인")
    assert not d.is_test_plan("/시뮬심의 결로 메커니즘")
    assert not d.is_test_plan("시험계획 세워줘")          # 트리거는 슬래시로 시작한다
    assert not d.is_deliberation("/시험계획 x")
    assert not d.is_sim_deliberation("/시험계획 x")


def test_시험계획_트리거_제거():
    assert d.strip_test_plan_trigger("/시험계획 낙하 해석용 물성 확보") == "낙하 해석용 물성 확보"


def test_시험계획서_템플릿이_우선순위와_미확보를_강제한다():
    """(3) 우선순위와 (9) 미확보가 이 문서의 값어치다 — 비면 희망 목록이 된다."""
    t = d._CHAIR_ITEMS["test-plan"]
    assert "우선순위" in t and "하나만 먼저 한다면" in t
    assert "확보되지 않는 것" in t
    assert "비워두지 마라" in t
    # 중복 측정 방지 — 이미 실측이 있는 항목을 다시 재면 자원 낭비다
    assert "다시 측정하지 마라" in t
    # 장기 항목 선착수 — 경시 시험은 결과까지 수개월이라 임계경로가 된다
    assert "먼저 착수" in t


def test_시험계획_고정좌석이_다섯_있다():
    """계측·해석·프로그램에 더해 sim 상관·통계신뢰성까지 5석 고정(스파인 보강). 하나라도 빠지면
    못 재는 것을 계획하거나·안 중요한 것을 1순위로 올리거나·대조 대상 없는 시험이 된다."""
    assert len(d._TEST_FIXED) == 5
    assert any("test" in k or "measure" in k for k in d._TEST_FIXED)
    assert any("cae" in k for k in d._TEST_FIXED)
    assert any("program" in k for k in d._TEST_FIXED)
    assert any("correlation" in k for k in d._TEST_FIXED)         # sim 상관 계약
    assert any("stats" in k or "reliab" in k for k in d._TEST_FIXED)  # 통계·신뢰성


# ── 심의 방법 메뉴 — 신규 Job 엔진·얹을 층·지정 좌석·1R 블라인드 (2026-08) ─────────────
def test_신규_Job_엔진_3종이_비어있지_않다():
    for k in ("diagnosis", "option-select", "credibility"):
        assert k in d._CHAIR_ITEMS and d._CHAIR_ITEMS[k].strip()


def test_지정_반대석은_신규_Job_에만_있다():
    """credibility=red-team, diagnosis=반증, option-select=반대. 좌석 구조로 반대 역할 보장."""
    assert set(d._CHAIR_ADVERSARY) == {"credibility", "diagnosis", "option-select"}
    assert d._CHAIR_ADVERSARY["credibility"]["key"] == "delib-redteam"
    for v in d._CHAIR_ADVERSARY.values():
        assert v["role"].strip() and v["key"].startswith("delib-")


def test_얹을_층_화이트리스트_dedup_cap():
    """중복 제거·순서 보존·화이트리스트·최대 5(JS MOD_LIST 와 정합)."""
    o = d._resolve_opts({"modifiers": ["voi", "voi", "없는것", "premortem", "toulmin",
                                       "eliminative", "anon1r", "voi"]})
    assert o.modifiers == ["voi", "premortem", "toulmin", "eliminative", "anon1r"]
    assert d._resolve_opts({}).modifiers == []
    assert d._resolve_opts({"modifiers": "문자열"}).modifiers == []


def test_얹을_층_주입_블록은_켠_것만_담고_모르는_건_버린다():
    note = d._modifier_note(["voi", "없는것", "premortem"])
    assert "얹을 층" in note and "교착 정산" in note and "사전부검" in note
    assert "없는것" not in note
    assert d._modifier_note([]) == "" and d._modifier_note(None) == ""


def test_1R_블라인드는_요약만_감추고_양보불가_조항은_유지한다():
    """FINDING #1 회귀 가드 — base_blind 가 쓰는 _cont_block('', 조항, human) 은 요약을 감추되
    조항은 유지해야 한다. 조항은 매 라운드 구속력이라 1R 블라인드에서도 빠지면 안 된다(JS BASE_BLIND 정합)."""
    blind = d._cont_block("", ["저온 UTG 두께 0.03T 유지"], "사람 의견")
    assert "저온 UTG 두께" in blind          # 조항 유지
    assert "구속력을 가진다" in blind          # 구속 문구 유지
    assert "[이전 심의 요약" not in blind      # 요약은 감춤(앵커링 차단)
    assert "사람 의견" in blind               # human_note 유지
    assert d._cont_block("", [], "") == ""     # 조항·요약·human 모두 없으면 빈 문자열(무해)

# 교차심문 표적 배정의 회귀 테스트 — 관련도 선정과 커버리지 보정이 조용히 죽는 것을 막는다.
#
# 이 배정이 망가져도 심의는 정상 완료된다(엉뚱한 상대를 반박할 뿐). 그래서 눈으로 못 잡는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_cross_targets.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402


# ── 용어 추출 ────────────────────────────────────────────────────────────────
def test_용어는_영문기술어_수치단위_두자한글만():
    t = d._terms("솔더 크랙이 -40°C 에서 MTF 저하로 이어진다. 그 값 12.5mm")
    assert "솔더" in t and "크랙" in t      # 조사 '이' 가 떨어져야 한다
    assert "저하" in t                      # '저하로' → '저하'
    assert "mtf" in t                      # 영문은 소문자로 정규화
    assert "12.5mm" in t and "40°c" in t   # 수치+단위는 한 덩어리
    assert "그" not in t                    # 한 글자 한글은 신호가 아니다


def test_조사가_달라도_같은_용어다():
    # 이걸 안 하면 같은 것을 논하는 두 좌석의 겹침이 0 이 된다.
    a = d._terms("솔더 크랙이 접합부에서 발생")
    b = d._terms("솔더 크랙은 접합부의 문제")
    assert {"솔더", "크랙", "접합부"} <= (a & b)


def test_조사만으로_된_토큰은_원형을_남긴다():
    # 떼고 나면 빈 문자열이 되는 것(띄어 쓴 조사)은 지우지 않는다 — 흔해서 IDF 가 0 으로 만든다.
    assert "에서" in d._terms("-40°C 에서")


# ── 관련도 선정 ──────────────────────────────────────────────────────────────
def test_용어가_겹치는_좌석을_고른다():
    texts = {
        "a": "솔더 크랙 열피로 사이클 -40°C 125°C",
        "b": "솔더 크랙 열피로 사이클 접합부 수명",   # a 와 대량 겹침
        "c": "렌즈 곡률 광학 MTF 해상력 평가",
        "d": "렌즈 곡률 광학 MTF 필드 밸런스",       # c 와 대량 겹침
    }
    tg = d._cross_targets(texts, 1)
    assert tg["a"] == ["b"] and tg["b"] == ["a"]
    assert tg["c"] == ["d"] and tg["d"] == ["c"]


def test_전원이_쓰는_말은_점수를_못_만든다():
    # '설계·해석'은 네 좌석 모두가 쓴다(df=n → IDF 0). 변별은 고유 용어로만 나야 한다.
    texts = {
        "a": "설계 해석 솔더 크랙",
        "b": "설계 해석 솔더 크랙",
        "c": "설계 해석 렌즈 곡률",
        "d": "설계 해석 렌즈 곡률",
    }
    tg = d._cross_targets(texts, 1)
    assert tg["a"] == ["b"] and tg["c"] == ["d"]


def test_표적_수는_k_를_따른다():
    texts = {k: f"{k} 고유용어{k} 공통 주제" for k in "abcde"}
    assert all(len(v) == 2 for v in d._cross_targets(texts, 2).values())
    assert all(len(v) == 3 for v in d._cross_targets(texts, 3).values())


def test_자기_자신은_표적이_아니다():
    texts = {k: "같은 말 같은 말" for k in "abcd"}
    for k, v in d._cross_targets(texts, 2).items():
        assert k not in v


# ── 커버리지 보정 ────────────────────────────────────────────────────────────
def test_아무도_안_겨냥한_좌석이_없다():
    # 'hub' 가 모두와 겹쳐 인기 표적이 되고, 'lone'(반대도메인 좌석 상황)은 아무와도 안 겹친다.
    texts = {
        "hub": "솔더 크랙 렌즈 곡률 배터리 스웰링 안테나 이득",
        "s1": "솔더 크랙 접합부",
        "s2": "렌즈 곡률 광학",
        "s3": "배터리 스웰링 팽창",
        "lone": "규제 인증 문서 서식",
    }
    tg = d._cross_targets(texts, 1)
    targeted = {b for v in tg.values() for b in v}
    assert "lone" in targeted, f"홀로 튄 좌석이 무사통과했다 — {tg}"
    assert targeted == set(texts), f"겨냥 안 된 좌석이 남았다 — {set(texts) - targeted}"


def test_보정이_새_구멍을_만들지_않는다():
    # 여러 배치에서 전원이 최소 1회는 겨냥돼야 한다(보정이 메우며 다른 데를 비우면 실패).
    import random

    rng = random.Random(7)
    words = ["솔더", "크랙", "렌즈", "곡률", "배터리", "스웰링", "안테나", "이득", "열피로", "접합부"]
    for trial in range(40):
        n = rng.randint(3, 14)
        texts = {f"k{i}": " ".join(rng.sample(words, rng.randint(1, 5))) for i in range(n)}
        for k in (1, 2, 3):
            tg = d._cross_targets(texts, k)
            targeted = {b for v in tg.values() for b in v}
            assert targeted == set(texts), f"trial={trial} k={k} 누락={set(texts) - targeted}"


def test_보정이_한_좌석에_몰리지_않는다():
    # 관련도만 보고 보정하면 인기 좌석이 표적 4~5명을 떠안는다(15석 실측에서 실제로 그랬다).
    # 'hub' 가 전원과 겹치고 고아가 셋이다 — 그래도 아무도 k+1 을 크게 넘기면 안 된다.
    texts = {
        "hub": "솔더 크랙 렌즈 곡률 배터리 스웰링 안테나 이득 열피로 접합부",
        "s1": "솔더 크랙 접합부 열피로",
        "s2": "렌즈 곡률 광학 해상력",
        "s3": "배터리 스웰링 팽창 전해액",
        "x1": "규제 인증 서식",
        "x2": "물류 포장 파렛트",
        "x3": "회계 감가 상각",
    }
    tg = d._cross_targets(texts, 2)
    loads = [len(v) for v in tg.values()]
    assert max(loads) <= 3, f"한 좌석에 몰렸다 — {tg}"
    assert {b for v in tg.values() for b in v} == set(texts)


def test_좌석이_하나면_표적이_없다():
    assert d._cross_targets({"a": "혼자"}, 2) == {"a": []}
    assert d._cross_targets({}, 2) == {}


def test_두_좌석이면_서로를_겨냥한다():
    tg = d._cross_targets({"a": "솔더 크랙", "b": "렌즈 곡률"}, 2)
    assert tg == {"a": ["b"], "b": ["a"]}   # k=2 라도 상대가 하나뿐이면 하나다


# ── 배정은 결정적이어야 한다(같은 입력 = 같은 배정) ──────────────────────────
def test_배정은_결정적이다():
    texts = {k: f"{k} 공통 주제 고유{k}" for k in "abcdef"}
    assert d._cross_targets(texts, 2) == d._cross_targets(texts, 2)

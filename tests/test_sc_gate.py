# 자기검증 관문의 순수 모듈 — 수 뽑기 · 원문 색인 · 쌍 판정이 설계의 보기대로 도는지(HWAXPortal docs/request-1030/design-ha27.md 12절 G · I · D)
"""10-30 재시험 넷(OT-04 · 08 · 09 · DW-05)의 꼴을 본뜬 쌍이 버림 · 짚음이 되고, 원문 값으로 고치는 정당한 고침은 넣음이 되는지 본다.
app 을 읽지 않는다(sc_gate 는 순수 모듈이다) — 단 UUID · 카드 번호 가림이 app 의 정규식과 같은 꼴인지 보는 시험 하나만 app 을 읽는다(C-13).

  실행:  .venv/bin/python -m pytest tests/test_sc_gate.py -q
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sc_gate as g  # noqa: E402

N = g.num_tokens


def num(s):
    return N(s)[0]


def _gate(raw, pairs, **kw):
    idx = g.RawIndex.build([raw] if raw else [])
    return [tuple(d) for d in g.gate_patches(pairs, idx, **kw)]


# ── G 수 뽑기 ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_G1_수_뽑기는_열쇠_반올림_폭_퍼센트를_낸다():
    t = N("개발모델 34개 · 13건 · 1,812초 · 12.5% · 1.2만 건 · 약 1,800초")
    assert [x.key for x in t] == ["34", "13", "1812", "12.5%", "12000", "1800"]
    assert [x.tol for x in t] == [0.5, 0.5, 0.5, 0.05, 500, 50]
    assert [x.pct for x in t] == [False, False, False, True, False, False]


def test_G2_모델_코드_버전_IP_그림_주소_인용_목록_번호는_수가_아니다():
    assert N("SM-S928B · Al6061-T6 · v1.2.3 · 10.0.0.1 · ![g](/agent/artifacts/a1b2.png) · (조회 결과 2)\n1. 첫째") == []
    assert N("조회 결과 1 에 따르면 · 결과 #3 · https://x/34") == []


def test_인용이_아닌_결과_뒤의_개수는_수다():
    """'조회 결과 34건' 은 인용 표지가 아니라 개수다 — 가리면 OT-04 꼴(34 → 13)이 관문을 비켜 간다."""
    assert [x.key for x in N("조회 결과 34건 · 결과 55% · 결과 12.5")] == ["34", "55%", "12.5"]
    assert _gate('{"total": 34}', [("조회 결과 34건", "조회 결과 13건")])[0][:2] == ("drop", "drop_sourced_number")


def test_G3_날짜와_식별자의_수는_맥락을_단다():
    assert [(x.value, x.ctx) for x in N("2026-10-30 · APP-GRAPH-0001 · 34개")] == [
        (2026, "date"), (10, "date"), (30, "date"), (1, "id"), (34, "num")]
    assert not num("APP-GRAPH-0001").small and not N("2026-10-30")[1].small, "날짜 · 식별자 조각은 작은 정수 규칙 밖이다"


def test_G4_쉼표는_셋씩일_때만_천_단위이고_붙은_한글_뒤도_수다():
    assert [x.key for x in N("1,2,3")] == ["1", "2", "3"]
    assert [x.key for x in N("12,345,678")] == ["12345678"]
    assert [x.key for x in N("모델34개")] == ["34"]
    assert [x.key for x in N("값은 12.5.")] == ["12.5"], "문장 끝 점은 수에 붙지 않는다"


def test_G5_범위의_퍼센트는_앞_수에도_붙는다():
    assert [x.pct for x in N("5~10%")] == [True, True]
    assert [x.key for x in N("5 ~ 10%")] == ["5%", "10%"]
    assert N("5~10%")[1].tol == 0.5, "범위의 물결은 어림 표지가 아니다"
    assert N("~1,800")[0].tol == 50, "수 앞이 아닌 물결은 어림이다"


def test_배수와_어림_꼬리와_부호():
    assert [x.key for x in N("3천만 원 · 2억 · 5천여 명 · −12.5")] == ["30000000", "200000000", "5000", "12.5"]
    assert N("5천여 명")[0].tol == 500 and N("1,800여 건")[0].tol == 50
    assert [x.key for x in N("3만큼 · 계약 100건")] == ["3", "100"] and N("계약 100건")[0].tol == 0.5, \
        "'만큼' 은 배수가 아니고, 낱말 속 '약'(계약)은 어림 표지가 아니다"


def test_C13_UUID_와_확인_카드_번호는_수가_아니다():
    """UUID 의 첫 묶음이 숫자로 시작하면 그 수(0 ~ 99)가 작은 정수로 샜다 — 하이픈 뒤 묶음도(design-integration C-13)."""
    assert N("다른 허브의 id 3f2a9c1e-1234-4abc-9def-0123456789ab 는 쓰지 마라") == []
    assert N("39a2c1e0-0000-4000-8000-000000000001") == []
    assert N("확인 번호 cf-7f3a9c2e41d0 · 30분") == [N("30분")[0]]
    idx = g.RawIndex.build(["✖ 3f2a9c1e-1234-4abc-9def-0123456789ab 는 다른 허브의 id 다", '{"id": "12345678-aaaa-4bbb-8ccc-000000000003"}'])
    assert not idx.has(3) and not idx.has(1234) and not idx.has(12345678), "원문 색인도 UUID 조각을 수로 세지 않는다"


def test_C13_가림_정규식은_app_의_UUID_카드_번호와_같은_꼴이다():
    """sc_gate 는 app 을 읽지 않으므로 같은 보기로 묶는다 — HA-30(_UUID_RE) · HA-29(_CF_ID_RE)가 main 에 든 뒤에야 비교할 상대가 생긴다."""
    import app as a
    samples = ["3f2a9c1e-1234-4abc-9def-0123456789ab", "x3f2a9c1e-1234-4abc-9def-0123456789ab", "3F2A9C1E-1234-4ABC-9DEF-0123456789AB",
               "3f2a9c1e-1234-4abc-9def-0123456789abc", "cf-7f3a9c2e41d0", "xcf-7f3a9c2e41d0", "cf-7f3a9c2e41d0a", "cf-7F3A9C2E41D0",
               "1234-5678", "APP-GRAPH-0001"]
    pairs = [(n, getattr(a, n, None), mine) for n, mine in (("_UUID_RE", g.UUID_RE), ("_CF_ID_RE", g.CF_ID_RE))]
    if all(theirs is None for _n, theirs, _m in pairs):
        pytest.skip("app 에 _UUID_RE · _CF_ID_RE 가 아직 없다(HA-30 · HA-29 합침 전) — 합친 뒤에는 이 시험이 돌아야 한다")
    for name, theirs, mine in pairs:
        assert theirs is not None, f"app.{name} 가 없다 — 한쪽만 합쳐졌다"
        assert [bool(theirs.search(s)) for s in samples] == [bool(mine.search(s)) for s in samples], name


# ── I 원문 색인 ───────────────────────────────────────────────────────────────────────────────────────────────
def test_I1_강한_수_약한_수_배열_길이():
    idx = g.RawIndex.build(['{"date": "2026-10-13", "total": 34, "rows": [1, 2, 3]}'])
    assert idx.has(34, strong=True)
    assert not idx.has(13, strong=True) and idx.has(13), "글 칸 속 날짜 조각은 약한 수다"
    assert idx.has(3, strong=True), "배열 길이 · 값"
    assert idx.has(2026)


def test_I2_반올림_폭과_퍼센트():
    idx = g.RawIndex.build(['{"t": 1812.4, "r": 0.125, "s": 33.6}'])
    assert idx.has(num("1,812초")) and idx.has(num("12.5%")) and idx.has(num("34"))
    assert not idx.has(num("33")), "33.6 은 33 의 폭(±0.5) 밖이다"
    assert not idx.has(num("12.50")), "% 가 아니면 0.125 와 견주지 않는다"


def test_I3_셈은_기준_값의_합_차_비율_곱이다():
    e = g.RawIndex.build([])
    base = [34.0, 13.0]
    assert [g.sourced(num(s), e, base) for s in ("47", "21", "2.6", "38.2%", "442")] == [True] * 5
    assert not g.sourced(num("99"), e, base)
    assert not g.derivable(num("47"), []), "기준 값이 없으면 셈도 없다"
    budget = [0]
    assert not g.derivable(num("47"), base, budget), "셈 상한을 다 쓰면 '셈으로 안 나온다'(짚음 쪽)"


def test_I4_원문_값은_공백을_걷고_견준다():
    idx = g.RawIndex.build(['[{"item": "X-01", "dept": "품질기술팀"}, {"item": "X-02", "dept": "신뢰성그룹"}]'])
    assert idx.values_removed("X-01 담당은 품질기술팀", "X-01 담당은 신뢰성그룹") == ["품질기술팀"]
    assert idx.values_removed("품질기술팀 담당", "담당 품질 기술팀") == [], "띄어쓰기만 바꾼 고침은 걸리지 않는다"
    assert idx.values_removed("품질기술팀 담당", "품질기술팀장 담당") == [], "다른 낱말 안에 든 값은 줄지 않은 것으로 센다"
    assert "x-01" not in idx.values, "숫자 비중이 50% 이상인 글은 원문 값이 아니다"


def test_I5_접은_배열의_길이는_개수가_아니다():
    idx = g.RawIndex.build(['[1, 2, "…[전체 300개 중 앞 2개만 실었다 — 나머지는 조건을 좁혀 다시 조회]"]'])
    assert not idx.has(3, strong=True)


def test_머리_줄은_건너뛰고_JSON_배열은_머리로_읽지_않는다():
    head = '[이 결과는 DynaForge 앱의 list_models 이다]\n[포털 챗이 허브 응답을 접었다 — 긴 목록 1곳에서 항목 250건 뺐다 · 원문 123,456자]\n'
    idx = g.RawIndex.build([head + '{"total": 34}'])
    assert idx.has(34, strong=True) and not idx.has(250) and not idx.has(123456), "코드가 붙인 머리의 수는 근거가 아니다"
    arr = g.RawIndex.build(['[{"n": 7}, {"n": 8}]\n'])
    assert arr.has(7, strong=True) and arr.has(2, strong=True), "한 줄짜리 JSON 배열은 머리가 아니다"


def test_잘린_JSON_은_글_칸과_맨_수를_갈라_읽고_이어_붙은_개체도_센다():
    cut = '{"total": 34, "rows": [{"date": "2026-10-03", "name": "품질기술팀"}, {"date": "2026-10-0'
    idx = g.RawIndex.build([cut])
    assert idx.has(34, strong=True) and idx.has(3) and not idx.has(3, strong=True), "잘린 사본의 글 칸 속 날짜 조각도 약한 수다"
    assert "품질기술팀" in idx.values_in("담당은 품질기술팀")
    two = g.RawIndex.build(['{"a": 1}{"b": 2}{"c": 3}'])
    assert two.has(3, strong=True) and two.has(2, strong=True)
    esc = g.RawIndex.build(['{"total": 34, "rows": [{"dept": "\\ud488\\uc9c8\\uae30\\uc220\\ud300"'])
    assert esc.values_in("품질기술팀 담당") == ["품질기술팀"], "\\u 로 쓴 한글 글 칸도 원문 값이다"


def test_덧붙인_근거는_수만_강하게():
    idx = g.RawIndex.build([], ["사람이 말한 기준 41.5 와 | 품질기술팀 |"])
    assert idx.has(41.5, strong=True) and idx.values == [], "사람 발화 · 이력은 수만 — 원문 값 목록에 넣지 않는다"


def test_상한을_넘으면_partial():
    big = json.dumps(list(range(g.NUMS_MAX + 10)))
    assert g.RawIndex.build([big]).partial
    assert g.RawIndex.build(['{"a": 1}'], partial=True).partial, "그릇이 상한에 걸렸으면 색인도 partial"
    assert not g.RawIndex.build(['{"a": 1}']).partial


# ── D 쌍 판정 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_D1_OT04_꼴_원문의_개수를_다른_수로_바꾸면_버린다():
    raw = json.dumps({"total": 34, "page_size": 13, "items": [{"id": f"M-{i:03d}"} for i in range(13)]})
    assert _gate(raw, [("개발모델은 34개", "개발모델은 13개")]) == [("drop", "drop_sourced_number", "34")]


def test_D2_OT09_꼴_원문의_수를_근거_없음으로_바꾸면_버린다():
    assert _gate('{"limit": 20}', [("기준은 20 이다", "기준은 (근거 없음 — 추정) 이다")])[0][:2] == ("drop", "drop_sourced_number")


def test_D3_원문의_수에_근거_없음을_달면_버리고_원문에_없으면_넣는다():
    pair = [("기준은 20 이다", "기준은 20(근거 없음 — 추정) 이다")]
    assert _gate('{"limit": 20}', pair)[0][:2] == ("drop", "drop_marks_sourced")
    assert _gate('{"limit": 25}', pair) == [("keep", "keep", "")]


def test_D4_DW05_꼴_직전_기간_값을_지우면_버린다():
    assert _gate('{"prev": 120, "cur": 150}', [("직전 120 → 이번 150 (+30)", "이번 150")]) == [("drop", "drop_sourced_number", "120")]


def test_D5_원문_값으로_고치면_넣고_원문에_없는_새_수는_짚는다():
    raw, draft = '{"max_stress": 312.5, "sf": 1.8}', "최대 응력은 321.5 MPa 이고 안전율은 1.8 입니다."
    assert _gate(raw, [("321.5 MPa", "312.5 MPa")], draft=draft) == [("keep", "keep", "")]
    assert _gate(raw, [("321.5 MPa", "318.0 MPa")], draft=draft) == [("demote", "demote_unsourced_number", "318.0")]


def test_D6_글_칸_속_조각으로만_맞는_작은_정수를_지우면_짚는다():
    assert _gate('{"date": "2026-10-03"}', [("불량 3건", "불량 5건")]) == [("demote", "demote_weak_source", "3")]


def test_D7_OT08_꼴_원문_값을_다른_값으로_바꾸면_짚고_수_관문만이면_넣는다():
    raw = '[{"item": "X-01", "dept": "품질기술팀"}, {"item": "X-02", "dept": "신뢰성그룹"}]'
    pair = [("X-01 담당은 품질기술팀", "X-01 담당은 신뢰성그룹")]
    assert _gate(raw, pair) == [("demote", "demote_removes_value", "품질기술팀")]
    assert _gate(raw, pair, values=False) == [("keep", "keep", "")]


def test_D8_작은_정수의_셈은_그_줄의_기준_값만():
    raw, draft = '{"a": 34, "b": 13, "c": 20}', "모델 34개, 시험 13개.\n다른 줄 20개."
    assert _gate(raw, [("시험 13개.", "시험 13개. 합계 47개.")], draft=draft) == [("keep", "keep", "")]
    assert _gate(raw, [("다른 줄 20개.", "다른 줄 20개, 합계 47개.")], draft=draft) == [("demote", "demote_unsourced_number", "47")]


def test_D9_반영하지_않은_고침과_같은_수를_쓰는_쌍도_짚는다():
    got = _gate('{"total": 34, "page": 13}', [("모델 34개", "모델 13개"), ("입니다.", "입니다. 그중 13개가 시험 중입니다.")])
    assert [d[:2] for d in got] == [("drop", "drop_sourced_number"), ("demote", "demote_coupled")] and got[1][2] == "13"


def test_D10_수도_값도_없는_고침은_넣는다():
    assert _gate("", [("这个 기준", "이 기준")]) == [("keep", "keep", "")]


def test_D11_원문에_없는_수는_지워도_된다():
    assert _gate('{"x": 1}', [("약 1,500건(추정)이다.", "")]) == [("keep", "keep", "")]


def test_까닭은_고정_글에_걸린_수만():
    d = g.Decision("drop", "drop_sourced_number", "34")
    assert g.reason(d) == "초안의 수치 34 는 조회 결과 원문에 있다"
    assert set(g.WHY) == set(g.CODES)


def test_sourced_in_은_원문이나_셈으로_나오는_수의_숫자_글자다():
    idx = g.RawIndex.build(['{"v": 321.5, "a": 1200, "b": 300}'])
    got = g.sourced_in("응력은 321.5 MPa · 1,200 과 300 의 합 1,500건 · 다른 값 777.7 · 비율 12.5%", idx)
    assert got == {"321.5", "1200", "300", "1500"}, got
    assert g.sourced_in("합 1,500건", idx) == set(), "기준 값은 글에 나온 원문 값뿐이다 — 원문 전체의 쌍으로 셈하지 않는다"


# ── L1 크기(느슨하게 — 크기 회귀 감지용) ─────────────────────────────────────────────────────────────────────────
def test_L1_600만_자_원문_색인과_쌍_20개가_10초_안에_끝난다():
    rows = [{"id": f"P-{i:06d}", "date": f"2026-10-{i % 28 + 1:02d}", "dept": f"부서{i % 97}팀", "v": i * 1.5, "n": i}
            for i in range(60_000)]
    big = json.dumps({"total": 60_000, "rows": rows}, ensure_ascii=False)
    while len(big) < 6_000_000:
        big += " "
    t0 = time.monotonic()
    idx = g.RawIndex.build([big[:2_000_000], big[2_000_000:4_000_000], big[4_000_000:6_000_000]])
    pairs = [(f"값은 {i * 7}.5 이다", f"값은 {i * 7 + 1}.5 이다") for i in range(20)]
    out = g.gate_patches(pairs, idx, draft="\n".join(p[0] for p in pairs))
    assert len(out) == 20 and time.monotonic() - t0 < 10, time.monotonic() - t0

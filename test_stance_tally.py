# 스탠스 정규화와 수렴 집계 — 침묵을 동의로 세지 않는지, 좌석 유실이 만장일치를 만들지 않는지
import deliberation as d


# ── 정규화 ────────────────────────────────────────────────────────────────────
def test_empty_or_missing_stance_is_abstention_not_conditional_agreement():
    """⚠ 회귀 방지 — 예전엔 이것들이 전부 '조건부 동의'였다. stance 는 요구 키가 아니라
    안 내도 통과하고, 구조화 실패로 강등된 발언에는 stance 자체가 없다. 그래서 한 마디도
    못 한 좌석에 '조건부 동의' 칩이 붙었다."""
    for empty in (None, "", "   ", "미표명", "기권", "유보", "해당 없음", "N/A", "abstain"):
        assert d._norm_stance(empty) == "미표명", empty


def test_explicit_stances_still_classify():
    assert d._norm_stance("동의합니다") == "동의"
    assert d._norm_stance("반대합니다") == "반대"
    assert d._norm_stance("조건부로 동의") == "조건부 동의"
    # 부정 표현이 동의로 새지 않는다(먼저 매칭한다).
    assert d._norm_stance("동의하지 않습니다") == "반대"


def test_undecidable_nonempty_text_stays_conditional():
    # 내용은 있는데 판별이 안 되는 경우는 보수적으로 조건부다 — 미표명과 구분한다.
    assert d._norm_stance("음... 글쎄요 상황에 따라 다릅니다") == "조건부 동의"


# ── 집계 ──────────────────────────────────────────────────────────────────────
def _tally(seated: int, last: list[dict]) -> dict:
    """deliberation 의 집계식과 같은 계산(코드가 바뀌면 이 테스트가 먼저 깨진다)."""
    key = {"동의": "agree", "조건부 동의": "conditional", "반대": "oppose", "미표명": "abstain"}
    t = {"agree": 0, "conditional": 0, "oppose": 0, "abstain": 0,
         "responded": len(last), "total": seated}
    for o in last:
        t[key[d._norm_stance(o.get("stance"))]] += 1
    t["abstain"] += max(0, seated - len(last))
    return t


def _unanimous(t: dict) -> bool:
    return t["total"] > 0 and t["agree"] == t["total"] and t["abstain"] == 0


def test_seat_loss_no_longer_produces_false_unanimity():
    """⚠ 회귀 방지 — 5석 착석 / 3석만 응답, 셋 다 동의였을 때 예전 분모는 응답자 수라
    '만장일치 3/3' 이 나갔다. 빠진 두 좌석의 판단이 통째로 사라진 채로."""
    t = _tally(5, [{"stance": "동의"}] * 3)
    assert t["total"] == 5 and t["responded"] == 3 and t["abstain"] == 2
    assert _unanimous(t) is False


def test_real_unanimity_still_reported():
    t = _tally(4, [{"stance": "동의"}] * 4)
    assert _unanimous(t) is True


def test_degraded_turn_without_stance_counts_as_abstention():
    # 구조화 실패로 강등된 발언 — stance 키가 없다.
    t = _tally(3, [{"stance": "동의"}, {"stance": "동의"},
                   {"say": "(구조화 실패 — 원문 강등, 일부만 보존) …"}])
    assert t["abstain"] == 1 and t["conditional"] == 0
    assert _unanimous(t) is False


def test_key_map_covers_every_label_norm_can_return():
    """_KEY 에 없는 라벨이 나오면 집계가 KeyError 로 즉사한다 — 라벨을 늘릴 때 함께 늘렸는지."""
    key = {"동의", "조건부 동의", "반대", "미표명"}
    samples = [None, "", "동의", "반대", "조건부", "알 수 없는 말", "기권", "abstain"]
    assert {d._norm_stance(x) for x in samples} <= key

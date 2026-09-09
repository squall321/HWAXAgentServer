# RA 저장 응답에서 보고서 id 를 뽑는 파서 — 응답 모양 셋을 다 받는지(무음 오보 재발 방지)
import deliberation as d


def test_top_level_report_id_is_the_deployed_shape():
    # 실측(2026-09-06) — RA 배포본이 실제로 주는 모양. 종전 파서는 이걸 못 읽어
    # 저장이 됐는데도 '저장 실패'라고 말했다.
    assert d._ra_report_id({"report_id": 58, "title": "t", "page_count": 1}) == 58


def test_nested_report_id_still_works():
    assert d._ra_report_id({"report": {"id": 12}}) == 12


def test_bare_id_works():
    assert d._ra_report_id({"id": 7}) == 7


def test_string_id_is_coerced():
    assert d._ra_report_id({"report_id": "58"}) == 58


def test_missing_or_bad_shapes_are_none_not_crash():
    for bad in ({}, None, "(tool create_report_draft error: boom)", {"report": "x"},
                {"report_id": None}, {"report_id": "없음"}, []):
        assert d._ra_report_id(bad) is None, bad

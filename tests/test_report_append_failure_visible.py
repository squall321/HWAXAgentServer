# 심의 보고서의 저장 · 이어붙이기가 실패하면 사람이 결정문에서 본다 — 포털이 되살린 심의의 이어하기가 append_to_report_id 를 싣는다(포털 docs/wda-deck WD-D68)
"""왜 — 포털이 새로고침 · 다른 기기에서 되살린 심의 카드에 보고서 번호를 남기면서(포털 WD-D35), 이어하기가 그 번호로 이어붙인다. 그 보고서가
지워졌거나 볼 권한이 없으면 이어붙이기가 실패한다 — 도구의 거절은 이미 결정문 끝에 '저장 실패' 로 붙는다(_ra_save_outcome). 그러나 도구 밖에서 터진
예외는 로그뿐이었다(결정문에 아무 말이 없다). 새 보고서로 대신 저장하지는 않는다 — 원래 기기의 이어하기도 그렇게 하지 않는다.
"""
import deliberation as D
from test_ha09_s1_resume import world, _oracle  # noqa: F401 — 가짜 세계와 끊지 않는 실행
from test_ha09_s3_post_idempotent import OPTS3, w3  # noqa: F401


def test_지운_보고서와_권한_없는_보고서에_이어붙이기는_실패로_말한다():
    for raw in ("(tool update_report_draft error: Report not found: 58)",
                "(tool update_report_draft error: Out of workspace scope)"):
        rid, note = D._ra_save_outcome(raw, 58, True)
        assert rid is None and "저장 실패" in note and "이어붙임" not in note, note


def test_이어붙이기가_도구_밖에서_터져도_결정문에_말하고_새로_저장하지_않는다(w3, monkeypatch):
    def boom(_blocks):
        raise RuntimeError("블록 조립이 터졌다")

    monkeypatch.setattr(D, "_ra_blocks", boom)
    o = _oracle(w3, monkeypatch, opts={**OPTS3, "append_to_report_id": 41})
    led = o["led"]
    assert "⚠ Report Archive 보고서 #41 에 이 회차를 이어붙이지 못했다(RuntimeError)" in led["decision"], led["decision"][-300:]
    assert led["report_id"] is None and o["reports"] == [], "새 보고서로 대신 저장하지 않는다"


def test_새_저장이_도구_밖에서_터져도_결정문에_말한다(w3, monkeypatch):
    monkeypatch.setattr(D, "_ra_blocks", lambda _b: (_ for _ in ()).throw(ValueError("x")))
    o = _oracle(w3, monkeypatch, opts=OPTS3)
    assert "⚠ Report Archive 저장 실패(ValueError)" in o["led"]["decision"] and o["led"]["report_id"] is None

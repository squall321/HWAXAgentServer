# 운영 요청 모음 11-24(HWAXPortal docs/request-1125 1-3) — 웹 챗 관문 WD-02 · WD-06 · WD-07 · WD-13 시험(가짜는 test_confirm_gate 의 것)
#
#   실행:  .venv/bin/python -m pytest tests/test_chat_gate_r1125.py -q
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app as a  # noqa: E402

from test_confirm_gate import _gw, _held, _run, gate, maps, turn_ctx  # noqa: E402, F401


# ── WD-07 RA 보고서 쓰기는 운영자와 상관없이 늘 카드 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["create_report_draft", "update_report_draft", "add_report_tags", "publish_report",
                                  "create_report_from_format", "reportarchive_create_report_draft"])
def test_WD07_RA_쓰기는_운영자_정의_없이도_confirm_이다(gate, name):
    kind, hit = a._confirm_class(name)
    assert kind == "confirm" and name.endswith(hit)


def test_WD07_운영자를_고르지_않은_챗에서_RA_초안을_만들라고_해도_카드가_먼저다(turn_ctx):
    reached: list = []
    t = _gw("create_report_draft", reached, props={"title": {"type": "string"}})
    turn_ctx("RA 초안 만들어줘")
    out = _run(t, {"title": "S26 낙하 정리"})
    assert _held(out) and reached == [], "주문 말로도 통과하지 않는다(confirm)"


def test_WD07_RA_읽기는_그대로_간다(turn_ctx):
    reached: list = []
    t = _gw("get_report", reached, props={"report_id": {"type": "integer"}})
    turn_ctx("보고서 보여줘")
    assert not _held(_run(t, {"report_id": 7})) and reached == [("get_report", {"report_id": 7})]

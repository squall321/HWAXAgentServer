# 심의 원장이 결과를 칸으로 남긴다 — 결정문 사건의 수치 대조(numcheck) · 결과 사건의 좌석별 표(votes) · 집계 · 만장일치 · RA 보고서 주소
"""HWAXPortal docs/token-saving P4a · TS-5(사용자 결정 11-02) — deliberate_result(view="summary") 가 옮길 칸.

왜 생겼나. 엔진은 결정문 수치 대조(evidence.unsourced_numbers)를 결정문 끝의 경고 줄(보이는 것 6건)로만 남겼고, 결과 사건의 집계(tally · unanimous) ·
RA 가 준 보고서 주소(url)는 웹 화면에만 가고 잡 원장(_apply)이 버렸다. 좌석별 표는 어디에도 없었다(위키 봉투만 만들었다). 요약은 글을 짓지 않고
원장에 있는 칸만 옮겨야 하므로 엔진이 그 판정을 칸으로 내고 원장이 받는다 — 결정문 사건 numcheck{unsourced(전부), unsourced_count, checked},
결과 사건 votes[{seat, stance, responded}](집계와 같은 좌석 · 같은 정규화). 보고서 단계를 되살린 실행(HA-09 S3)은 대조를 다시 하지 않으니
끊기기 전 줄(e:report_intent)에 실어 둔 것을 쓴다.

가짜는 바깥에만 있다(tests/test_ha09_s1_resume.py 의 world — LLM · 게이트웨이 도구 · 좌석 자유 조회). 엔진 · 잡 원장 · 진행 일지는 진짜다.

  실행:  .venv/bin/python -m pytest tests/test_delib_ledger_fields.py -q
"""
import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import deliberation as d  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import ROUNDS, TAGS, _cut, _eng, _kind, _oracle, world  # noqa: E402, F401 — world 는 픽스처
from test_ha09_s3_post_idempotent import OPTS3, _resume_at, w3  # noqa: E402, F401 — w3 는 픽스처

_DISSENT = "rel-drop"
_RA_URL = "/w/team-a/reports/77"


@pytest.fixture
def wd(world, monkeypatch):
    """S1 가짜 위에 — 한 좌석(rel-drop)이 마지막 라운드에 반대하고, 의장 결정문에 근거에 없는 수치가 하나 있고, RA 가 보고서 주소를 준다."""
    real_llm, real_answer = d._llm_text, world.answer

    async def llm_text(llm, system, human, *img):
        out = await real_llm(llm, system, human, *img)
        tg = TAGS.get() or {}
        if _kind(system) == "seat" and tg.get("seat") == _DISSENT and tg.get("round") == ROUNDS:
            return out.replace('"stance": "동의"', '"stance": "반대"')
        if _kind(system) == "chair":
            return out + "\n- 보스 두께를 13.75 mm 로 바꾼다"
        return out

    def answer(name, args):
        if name == "create_report_draft":
            world.reports.append(copy.deepcopy(args))
            return json.dumps({"report_id": 77, "url": _RA_URL})
        return real_answer(name, args)

    monkeypatch.setattr(d, "_llm_text", llm_text)
    monkeypatch.setattr(world, "answer", answer)
    return world


# ── 엔진이 칸으로 남긴다 — 결과 사건의 좌석별 표 · 보고서 주소, 결정문 사건의 수치 대조 ─────────────────────────────────────
def test_엔진은_좌석별_표와_수치_대조와_보고서_주소를_원장에_칸으로_남긴다(wd, monkeypatch):
    o = _oracle(wd, monkeypatch)
    led = o["led"]
    assert led["status"] == "done" and led["report_id"] == 77, led.get("error")
    votes = {v["seat"]: v for v in led["votes"]}
    assert set(votes) == {s["key"] for s in led["seats"]}, "좌석별 표가 착석 전원이 아니다"
    assert votes[_DISSENT] == {"seat": _DISSENT, "stance": "반대", "responded": True}, votes[_DISSENT]
    assert all(v["stance"] == "동의" and v["responded"] for k, v in votes.items() if k != _DISSENT), votes
    t = led["tally"]
    assert (t["oppose"], t["agree"], t["total"]) == (1, len(votes) - 1, len(votes)) and led["unanimous"] is False, t
    assert led["report_url"] == _RA_URL
    nc = led["numcheck"]
    assert "13.75" in nc["unsourced"] and nc["unsourced_count"] == len(nc["unsourced"]) and nc["checked"] >= nc["unsourced_count"], nc
    # 칸은 결정문 끝의 경고 줄과 같은 판정이다(보이는 것은 6건까지 · 총 건수)
    line = next(ln for ln in led["decision"].splitlines() if ln.startswith("> ⚠ 다음 수치는"))
    assert re.findall(r"`([^`]+)`", line) == nc["unsourced"][:6], line
    assert re.search(rf"(?:총 )?{nc['unsourced_count']}건\)", line), line


def test_수치가_다_근거에_있으면_수치_대조는_0건이고_경고_줄이_없다(world, monkeypatch):
    o = _oracle(world, monkeypatch)
    nc, dec = o["led"]["numcheck"], o["led"]["decision"]
    assert nc["unsourced"] == [] and nc["unsourced_count"] == 0, nc
    assert "다음 수치는 심의에 제시된 근거에서 확인되지 않았습니다" not in dec


# ── 이어 돈 잡도 같은 칸 — 보고서 저장 도중 끊겨 이으면 수치 대조를 다시 하지 않으니 끊기기 전 줄의 것을 쓴다 ─────────────────────
def test_보고서_저장_도중_끊겨_같은_잡으로_이어도_수치_대조와_표결이_끊지_않은_실행과_같다(w3, monkeypatch, tmp_path):
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    assert "13.75" in o["led"]["numcheck"]["unsourced"], "시험 전제 — 결정문에 근거 없는 수치가 있다"
    _out, res, _wiki = _resume_at(w3, monkeypatch, tmp_path, "intent", _cut(snaps, _eng("e:report_intent")))
    a, b = res["led"], o["led"]
    assert a["numcheck"] == b["numcheck"], "되살린 실행의 수치 대조가 끊지 않은 실행과 다르다(또는 없다)"
    assert (a["votes"], a["tally"], a["unanimous"]) == (b["votes"], b["tally"], b["unanimous"])

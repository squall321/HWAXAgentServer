# 심의 결과 사건(outcome)에 RA 가 준 보고서 주소를 싣는다 — 포털의 결과 카드가 그 주소로 바로 연다(포털 docs/wda-deck WD-D78)
"""왜 — 포털의 결과 카드는 보고서 번호만 받아, 열 때마다 포털이 그 사람 명의로 RA 보고서를 통째로(16 MB 상한) 읽어 공간을 찾았다. 그 길은 새 탭
이동이라 15분 세션을 되살리지 못해 긴 심의 뒤에는 401 JSON 이 떴다. RA 쓰기 응답(ai-draft POST · PATCH)은 이미 `url: "/w/<공간>/reports/<번호>"`
를 준다 — 그것을 outcome 에 싣고(꼴과 번호가 맞을 때만), 이어하기 일지(e:report_saved)에도 남겨 되살린 실행이 같은 주소를 낸다.

  실행:  .venv/bin/python -m pytest tests/test_ra_report_url_outcome.py -q
"""
import copy
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as D  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import _cut, _eng, _oracle, world  # noqa: E402, F401 — world 는 픽스처
from test_ha09_s3_post_idempotent import OPTS3, _resume_at, w3  # noqa: E402, F401 — w3 는 픽스처


@pytest.mark.parametrize("url,rid,want", [
    ("/w/team-a/reports/77", 77, True),
    ("/w/team-a/reports/78", 77, False),                  # 다른 보고서
    ("/w/../reports/77", 77, False),                      # 경로를 바꾸는 공간 이름
    ("/w/./reports/77", 77, False),
    ("//evil.example/w/x/reports/77", 77, False),
    ("https://evil.example/w/x/reports/77", 77, False),
    ("/w/a b/reports/77", 77, False),
    ("/w/a/reports/77?x=1", 77, False),
    ("/w/a/reports/77", None, False),
    (77, 77, False),
])
def test_주소는_꼴과_번호가_맞을_때만(url, rid, want):
    got = D._ra_report_url({"report_id": rid, "url": url}, rid)
    assert (got == url) if want else got is None


def _outcomes(lines) -> list[dict]:
    return [rec["d"] for rec in lines if rec.get("k") == "delib" and (rec.get("d") or {}).get("kind") == "outcome"]


def _spy_journal(monkeypatch) -> list[dict]:
    """이 뒤로 진행 일지에 적히는 줄 — 끝난 잡은 일지를 원장에 접어 지우므로 적히는 순간에 모은다."""
    got: list[dict] = []
    orig = delib_jobs._Journal._write

    def spy(self, data, sync):
        orig(self, data, sync)
        got.append(json.loads(data))

    monkeypatch.setattr(delib_jobs._Journal, "_write", spy)
    return got


@pytest.fixture
def w4(w3, monkeypatch):
    """Report Archive 가 쓰기 응답에 보고서 주소를 준다(ai-draft POST 는 최상위 url · PATCH 는 report 와 url)."""
    ids = itertools.count(77)
    real = w3.answer

    def answer(name, args):
        if name == "create_report_draft":
            w3.reports.append(copy.deepcopy(args))
            n = next(ids)
            return json.dumps({"report_id": n, "title": "t", "page_count": 1, "url": f"/w/team-a/reports/{n}"})
        if name == "update_report_draft":
            w3.reports.append(copy.deepcopy(args))
            return json.dumps({"report": {"id": args["report_id"]}, "url": f"/w/team-b/reports/{args['report_id']}"})
        return real(name, args)

    monkeypatch.setattr(w3, "answer", answer)
    return w3


@pytest.mark.parametrize("append", [False, True])
def test_결과_사건에_RA_가_준_보고서_주소를_싣는다(w4, monkeypatch, append):
    snaps: list = []
    _oracle(w4, monkeypatch, snaps, opts={**OPTS3, **({"append_to_report_id": 41} if append else {})})
    (o,) = _outcomes([rec for rec, _disk_ in snaps])
    assert o["report_id"] == (41 if append else 77)
    assert o["url"] == ("/w/team-b/reports/41" if append else "/w/team-a/reports/77")


def test_주소가_없으면_싣지_않는다(w3, monkeypatch):
    snaps: list = []
    _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    (o,) = _outcomes([rec for rec, _disk_ in snaps])
    assert o["report_id"] == 77 and "url" not in o, "옛 RA — 포털이 번호로 찾아 연다"


def test_보고서를_저장한_뒤_끊고_이으면_같은_주소를_다시_저장하지_않고_낸다(w4, monkeypatch, tmp_path):
    snaps: list = []
    _oracle(w4, monkeypatch, snaps, opts=OPTS3)
    snap = _cut(snaps, _eng("e:report_saved"))
    got = _spy_journal(monkeypatch)
    _out, res, _wiki = _resume_at(w4, monkeypatch, tmp_path, "saved", snap)
    assert res["reports"] == [], "다시 저장하지 않는다"
    (o,) = _outcomes(got)
    assert o["report_id"] == 77 and o["url"] == "/w/team-a/reports/77", "이은 실행의 결과 사건도 같은 주소(일지 e:report_saved 에서)"

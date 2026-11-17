# 구조화 결정 출력(DL-03) — deliberate_result(view="decision_json") 의 risk_spec · 표결 · 인용 · 만장일치 문장 대조, HTTP 창구, 기본 답은 그대로
"""HWAXPortal docs/request-1117 contracts §3 (DL-03).

  · risk_spec 은 리스크 앱(HWAXRisk backend/app/narrative.py parse_risk_spec)과 같은 규칙 — 마지막 ```json 펜스, 없거나 깨지면 끝의 마지막 균형
    중괄호, schema == "risk_spec". 형제 리포가 곁에 있으면 펜스 정규식이 글자 그대로 같은지 본다.
  · votes 는 원장의 tally · unanimous · votes(by_seat) 그대로, cites 는 [e:N|KEY] · [e:N] 의 수.
  · consistency — 결정문이 만장일치를 말하는데 반대 · 조건부 · 미표명이 있으면 match 거짓. 부정('…지 못했다')은 주장이 아니다.
  · 기본 deliberate_result(full · summary)의 답에는 이 칸이 없다.

합성 원장만 쓴다(엔진 · 게이트웨이를 부르지 않는다).

  실행:  .venv/bin/python -m pytest tests/test_dl03_decision_json.py -q
"""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import delib_decision as dd  # noqa: E402
import delib_jobs  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_same_aff import _ctx  # noqa: E402

ME, MATE, OUT = "me@example.com", "mate@example.com", "out@example.com"
_SPEC = {"schema": "risk_spec", "version": "1.0", "findings": [{"id": "F-1", "severity": "높음", "claim": "합성 판정"}], "gains": []}
_TALLY_SPLIT = {"agree": 3, "conditional": 1, "oppose": 1, "abstain": 0, "responded": 5, "total": 5}
_TALLY_ALL = {"agree": 3, "conditional": 0, "oppose": 0, "abstain": 0, "responded": 3, "total": 3}


def _votes(tally):
    rows = []
    for stance, k in (("동의", "agree"), ("조건부 동의", "conditional"), ("반대", "oppose")):
        rows += [{"seat": f"s{len(rows) + i}", "stance": stance, "responded": True} for i in range(tally[k])]
    return rows


def _job(**kw) -> dict:
    now = delib_jobs._now()
    job = {"id": "risk-review-20261117-000000-aaaaaa", "job": "risk-review", "kind": "risk-review", "label": "리스크 심사",
           "status": "done", "question": "합성 화두", "chair_template": "risk-review", "opts": {}, "stage": "report",
           "seats": [{"key": "s0"}], "turns": [], "round": 2, "total_rounds": 2,
           "decision": ("## 결정\n전원 합의로 채택한다 [e:1|BND-1] [e:2|BND-1] [e:3].\n\n```json\n"
                        + json.dumps(_SPEC, ensure_ascii=False) + "\n```"),
           "tally": _TALLY_SPLIT, "unanimous": False, "votes": _votes(_TALLY_SPLIT),
           "error": None, "warnings": [], "evidence_omitted": [], "started_at": now - 10, "updated_at": now, "finished_at": now,
           "user": ME, "owner_aff": "TEAM-A"}
    job.update(kw)
    delib_jobs._JOBS[job["id"]] = job
    return job


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    for name in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, name, {})
    return _job


# ── 파서 — 리스크 앱과 같은 규칙 ─────────────────────────────────────────────────────────────────────────────
def test_마지막_펜스를_쓰고_깨지면_끝의_균형_중괄호로_읽는다():
    a, b = {"schema": "risk_spec", "n": 1}, {"schema": "risk_spec", "n": 2}
    assert dd.parse_risk_spec(f"```json\n{json.dumps(a)}\n```\n본문\n```json\n{json.dumps(b)}\n```")["n"] == 2
    broken = "```json\n{\"schema\": \"risk_spec\", \"n\": }\n```\n끝에 " + json.dumps({"schema": "risk_spec", "n": 3, "x": {"y": 1}})
    assert dd.parse_risk_spec(broken)["n"] == 3, "펜스가 깨지면 끝의 균형 블록"
    assert dd.parse_risk_spec("```json\n{\"schema\": \"sim_spec\"}\n```") is None, "schema 가 다르면 없음"
    assert dd.parse_risk_spec("") is None and dd.parse_risk_spec(None) is None and dd.parse_risk_spec("{ 깨짐") is None


def test_펜스_규칙이_리스크_앱과_글자_그대로_같다():
    src = ROOT.parent / "HWAXRisk" / "backend" / "app" / "narrative.py"
    if not src.exists():
        pytest.skip(f"형제 리포 없음: {src}")
    text = src.read_text(encoding="utf-8")
    want = re.search(r'^_FENCE_RE = re\.compile\(r"(.+)"\)$', text, re.M)
    assert want and want.group(1) == dd._FENCE_RE.pattern, "리스크 앱 parse_risk_spec 의 펜스 정규식이 바뀌었다 — 두 리포를 함께 고친다"
    assert 'obj.get("schema") != "risk_spec"' in text


# ── 만장일치 문장 ────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,claim", [
    ("패널은 전원 합의로 B안을 고른다.", True),
    ("이견 없이 채택했다.", True),
    ("만장일치로 통과.", True),
    ("The panel agreed unanimously.", True),
    ("전원 합의에 이르지 못했다.", False),
    ("만장일치는 아니다.", False),
    ("전원 합의는 없었다.", False),
    ("다수가 동의했고 한 좌석은 조건부다.", False),
    # 반대 검토 M4 — 뒤 · 앞의 부정
    ("표결은 만장일치가 아닌 조건부 동의 다수로 정리됐다.", False),
    ("전원 합의는 없다. 다수가 조건부다.", False),
    ("만장일치 아님 — 조건부 2석.", False),
    ("The decision was not unanimous.", False),
    ("비만장일치로 정리했다.", False),
    ("준비 만장일치로 넘겼다.", True),
    ("이견이 없고 전원 찬성했다.", True),
    ("```json\n{\"claim\": \"전원 합의\"}\n```\n본문은 다수 의견이다.", False),
])
def test_만장일치_문장을_가린다(text, claim):
    assert (dd.consensus_claim(text) is not None) is claim, text


def test_낱말_목록은_한_곳에_있다():
    assert "전원 합의" in dd.CONSENSUS_PHRASES and "만장일치" in dd.CONSENSUS_PHRASES and "이견 없이" in dd.CONSENSUS_PHRASES
    assert "모두 동의" in dd.CONSENSUS_PHRASES


# ── decision_json ───────────────────────────────────────────────────────────────────────────────────────────
def test_리스크_심사는_risk_spec_에_표결과_인용을_붙인다(ledger):
    job = ledger()
    out = dd.build(job)
    assert (out["job_id"], out["job"], out["template"], out["view"]) == (job["id"], "risk-review", "risk-review", "decision_json")
    dj = out["decision_json"]
    assert dj["risk_spec"] == _SPEC
    assert dj["votes"] == {"tally": _TALLY_SPLIT, "unanimous": False, "by_seat": job["votes"]}
    assert dj["cites"] == [{"key": "BND-1", "n": 2}, {"key": "e:3", "n": 1}]
    c = out["consistency"]
    assert c["match"] is False and c["tally"] == _TALLY_SPLIT and "전원 합의" in c["consensus_claim"]
    assert "반대 1" in c["note"] and "조건부 1" in c["note"]
    assert "parse_error" not in out


def test_만장일치_표결이면_맞는다(ledger):
    job = ledger(tally=_TALLY_ALL, unanimous=True, votes=_votes(_TALLY_ALL))
    assert dd.build(job)["consistency"]["match"] is True


def test_미표명_응답_못_한_좌석이_있으면_만장일치가_아니다(ledger):
    t = {"agree": 2, "conditional": 0, "oppose": 0, "abstain": 1, "responded": 2, "total": 3}
    job = ledger(tally=t, unanimous=False, votes=_votes(t) + [{"seat": "lost", "stance": "미표명", "responded": False}])
    c = dd.build(job)["consistency"]
    assert c["match"] is False and "미표명" in c["note"]


def test_봉인_심사도_같다_risk_spec_을_못_읽으면_null_과_까닭(ledger):
    job = ledger(job="risk-review-sealed", decision="## 결정\n펜스가 없다.")
    out = dd.build(job)
    assert out["decision_json"] is None and "risk_spec" in out["parse_error"]
    assert out["consistency"]["match"] is None, "문장도 없으니 견줄 것이 없다"


def test_다른_심의는_risk_spec_이_null_인_같은_꼴이다(ledger):
    job = ledger(id="default-20261117-000000-bbbbbb", job="default", kind="default", chair_template=None,
                 decision="## 결정\n이견 없이 채택한다 [e:1].\n```json\n{\"schema\": \"risk_spec\"}\n```")
    out = dd.build(job)
    assert out["decision_json"]["risk_spec"] is None, "리스크 심사가 아니면 펜스를 읽지 않는다"
    assert out["decision_json"]["cites"] == [{"key": "e:1", "n": 1}]
    assert out["consistency"]["match"] is False


def test_표결이_없으면_null_과_까닭(ledger):
    job = ledger(tally=None, votes=None, unanimous=None)
    out = dd.build(job)
    assert out["decision_json"]["votes"] is None and out["consistency"]["match"] is None and out["consistency"]["tally"] is None


def test_끝나지_않은_잡은_결정문이_없다고_말한다(ledger):
    job = ledger(status="running", decision=None, result_text=None)
    out = dd.build(job)
    assert out["decision_json"] is None and "끝나지 않았다" in out["parse_error"]


# ── MCP · 기본 답 ───────────────────────────────────────────────────────────────────────────────────────────
def test_MCP_view_decision_json_과_기본_답(ledger):
    job = ledger()
    out = asyncio.run(m.deliberate_result(job["id"], view="decision_json", ctx=_ctx(ME)))
    assert out == dd.build(job)
    full = asyncio.run(m.deliberate_result(job["id"], ctx=_ctx(ME)))
    summ = asyncio.run(m.deliberate_result(job["id"], view="summary", ctx=_ctx(ME)))
    for o in (full, summ):
        assert "decision_json" not in o and "consistency" not in o, "기본 답 · 요약에는 새 칸이 없다"
    with pytest.raises(ValueError):
        asyncio.run(m.deliberate_result(job["id"], view="decision_json", ctx=_ctx(OUT, "TEAM-B")))
    assert asyncio.run(m.deliberate_result(job["id"], view="decision_json", ctx=_ctx(MATE, "TEAM-A")))["decision_json"] is not None


def test_긴_결정은_쪽으로_나눈다(ledger, monkeypatch):
    import mcp_pages
    monkeypatch.setattr(mcp_pages, "PAGE_CHARS", 4000)
    big = {**_SPEC, "findings": [{"id": f"F-{i}", "claim": "가" * 300} for i in range(40)]}
    job = ledger(decision="## 결정\n본문\n```json\n" + json.dumps(big, ensure_ascii=False) + "\n```")
    p1 = asyncio.run(m.deliberate_result(job["id"], view="decision_json", ctx=_ctx(ME)))
    assert p1["page"]["parts"] > 1 and p1["page"]["next"] == {"job_id": job["id"], "view": "decision_json", "part": 2}


# ── HTTP 창구 ───────────────────────────────────────────────────────────────────────────────────────────────
def test_HTTP_창구는_공유_비밀과_신원_머리로_같은_답을_준다(ledger, monkeypatch):
    from fastapi.testclient import TestClient
    import app as a
    import seat_box
    job = ledger()
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    auth = {"Authorization": "Bearer gw-secret"}
    assert c.post("/deliberate/result/decision", json={"job_id": job["id"]}).status_code == 401
    r = c.post("/deliberate/result/decision", json={"job_id": job["id"]}, headers={**auth, "x-hwax-user": ME})
    assert r.status_code == 200 and r.json() == json.loads(json.dumps(dd.build(job), ensure_ascii=False))
    r = c.post("/deliberate/result/decision", json={"job_id": job["id"]},
               headers={**auth, "x-hwax-user": OUT, "x-hwax-affiliation": "TEAM-B"})
    assert r.status_code == 403
    assert c.post("/deliberate/result/decision", json={"job_id": "none"}, headers=auth).status_code == 404
    r = c.post("/deliberate/result/decision", json={"job_id": job["id"], "view": "x"}, headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "unknown_fields"

# 실사용 질문 모으기(seat_questions)가 기본 꺼짐 · 사람 · 메일 · 대화 id 지우기 · 박스 파일에만 · 화면 응답의 누설 0 · 사람 표시 규약을 지키는지(HWAXPortal docs/request-1029 design-e5 E5-2)
"""모으는 길 셋 — 챗 턴(지목 전문가 첫 발화 · 띵킹 · 사람이 좌석을 고른 심의 · '/전문가'), MCP deliberate_start(지정 좌석), 포털이 밀어 넣는 지난 날들.
화면 쪽(GET /seat-questions)에는 순위 · 점수 · 지금 좌석 · 사람이 고른 전문가가 실리지 않는다(누설 규칙 L6).

  실행:  .venv/bin/python -m pytest tests/test_seat_questions.py -q
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as a  # noqa: E402
import seat_questions as sq  # noqa: E402

SECRET = "Bearer hwaxgw_seat_q_test"
LEAK_KEYS = {"rank", "score", "scores", "desc_match", "low_confidence", "seats", "seat", "picked", "recommended", "ranked", "top",
             "user", "user_email", "email", "conversation_id", "ts"}


@pytest.fixture
def box(monkeypatch, tmp_path):
    monkeypatch.setenv("SEAT_EVAL_DIR", str(tmp_path / "seat-eval"))
    monkeypatch.setattr(a.app.state, "connections", {"gateway": {"url": "x", "headers": {"Authorization": SECRET}}}, raising=False)
    sq._STATE.update(day=None, written=0, error=None)
    sq._VIEW_CACHE.update(key=None, rows=None, filtered=None)
    return tmp_path / "seat-eval" / "questions"


def _rows(d: Path) -> list[dict]:
    out = []
    for f in sorted(d.glob("q-*.jsonl")):
        out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return out


def _keys(obj) -> set:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(_keys(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_keys(v) for v in obj)) if obj else set()
    return set()


def test_기본은_꺼져_있고_아무것도_적지_않는다(box):
    assert sq.enabled() is False
    assert sq.note("thinking", "낙하 크랙 원인이 뭔가요?", user="koo.park@example.com") is False
    assert not box.exists()
    st, out = sq.ingest([{"src": "thinking", "text": "낙하 크랙 원인이 뭔가요?"}])
    assert st == 409 and "SEAT_QUESTIONS_COLLECT" in out["error"]
    req = a.ChatRequest(message="/띵킹 낙하 크랙 원인이 뭔가요?", user_email="koo.park@example.com")
    a._seat_q_note(req, "human")
    assert not box.exists(), "챗 턴도 꺼져 있으면 적지 않는다"


def test_켜면_사람_메일_대화_id_를_지우고_날짜만_적는다(box, monkeypatch):
    monkeypatch.setenv("SEAT_QUESTIONS_COLLECT", "1")
    text = ("koo.park 입니다. koo.park@example.com 로 회신 — 대화 0123456789abcdef0123456789abcdef 에서 본 "
            "힌지 크랙 원인을 010-1234-5678 로 알려 주세요 1b4e28ba-2fa1-11d2-883f-0016d3cca427")
    assert sq.note("persona", text, user="koo.park@example.com", picked=["mech-hinge", "BAD KEY"]) is True
    rows = _rows(box)
    assert len(rows) == 1
    r = rows[0]
    assert set(r) == {"qid", "d", "src", "text", "picked"}, "시각 · 사람 · 대화를 가리키는 칸이 없다"
    for gone in ("koo.park", "koo", "park", "example.com", "0123456789abcdef", "010-1234-5678", "1b4e28ba"):
        assert gone not in r["text"], (gone, r["text"])
    assert "[이름]" in r["text"] and "[메일]" in r["text"] and "[id]" in r["text"] and "[번호]" in r["text"]
    assert r["picked"] == ["mech-hinge"] and r["src"] == "persona" and r["qid"] == sq.qid_of(r["text"])
    f = next(box.glob("q-*.jsonl"))
    assert oct(f.stat().st_mode & 0o777) == "0o600" and oct(box.stat().st_mode & 0o777) == "0o700"
    assert sq.note("thinking", "그럼?") is False, "8자 아래는 남기지 않는다"
    assert sq.note("nope", "출처가 틀린 발화는 남기지 않는다") is False


def test_챗_턴의_출처_가름은_chat_의_갈래와_같다():
    src = a._seat_q_source
    R = a.ChatRequest
    assert src(R(message="힌지 두께 기준은?", pinned_agent="mech-hinge"), "human") == ("persona", "힌지 두께 기준은?", ["mech-hinge"])
    assert src(R(message="그럼 0.8mm 는?", pinned_agent="mech-hinge",
                 history=[{"role": "user", "content": "힌지 두께 기준은?"}, {"role": "assistant", "content": "1.0mm"}]), "human") is None, \
        "앞선 사람 발화가 있으면 첫 발화가 아니다"
    assert src(R(message="/띵킹 낙하 크랙 원인"), "human") == ("thinking", "낙하 크랙 원인", [])
    assert src(R(message="낙하 크랙 원인", thinking=True), "human") == ("thinking", "낙하 크랙 원인", [])
    opts = {"personas": [{"key": "mech-drop", "role": "낙하"}, {"agent_type": "rel-drop"}]}
    assert src(R(message="/심의 보스 보강할까?", delib_opts=opts), "human") == ("delib", "보스 보강할까?", ["mech-drop", "rel-drop"])
    assert src(R(message="/심의 보스 보강할까?"), "human") is None, "서버가 고른 좌석의 심의는 아니다"
    assert src(R(message="/심의 보스 보강할까?", delib_opts=opts), "app") is None, "앱이 띄운 리스크 패널은 아니다"
    assert src(R(message="/전문가 낙하 해석"), "human") == ("expert_cmd", "낙하 해석", [])
    assert src(R(message="낙하 해석 전문가 추천해 줘"), "human") is None, "자연어 전문가 찾기는 '/전문가' 질의가 아니다"
    assert src(R(message="/도구 메시"), "human") is None and src(R(message="그냥 챗 질문입니다"), "human") is None


def test_챗_턴과_MCP_심의_시작이_적는다(box, monkeypatch):
    monkeypatch.setenv("SEAT_QUESTIONS_COLLECT", "1")

    async def _fake_thinking(*_a, **_k):
        yield a._sse("result", {"type": "text", "content": "ok"})
        yield a._sse("done", {})
    monkeypatch.setattr(a, "run_thinking", _fake_thinking)
    r = TestClient(a.app).post("/chat", json={"message": "/띵킹 힌지 낙하 크랙 원인이 뭔가", "user_email": "x@y.zz"})
    assert r.status_code == 200 and "ok" in r.text
    import delib_jobs  # noqa: PLC0415
    import mcp_server as m  # noqa: PLC0415
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace()))
    monkeypatch.setattr(delib_jobs, "start", lambda *a_, **k: {"job_id": "j1"})
    monkeypatch.setattr(delib_jobs, "summary", lambda rec: {"job_id": "j1", "question": "보스 보강 화두", "status": "running"})
    asyncio.run(m.deliberate_start("보스 보강 여부를 정하자", personas=[{"key": "mech-drop", "role": "낙하"}]))
    asyncio.run(m.deliberate_start("좌석을 서버에 맡긴 화두입니다"))
    got = sorted((x["src"], x["text"], tuple(x.get("picked") or ())) for x in _rows(box))
    assert got == [("delib", "보스 보강 여부를 정하자", ("mech-drop",)), ("thinking", "힌지 낙하 크랙 원인이 뭔가", ())]


def test_화면_응답에는_순위_점수_좌석_고른_전문가가_없다(box, monkeypatch):
    monkeypatch.setenv("SEAT_QUESTIONS_COLLECT", "1")
    for src, t in [("persona", "PCB 휨 기준을 어떻게 잡나요"), ("thinking", "낙하 시 힌지 크랙 원인은"), ("delib", "보스 보강 여부를 정하자"),
                   ("expert_cmd", "낙하 해석 전문가"), ("thinking", "그럼 그건 어떻게 돼요?"), ("thinking", "낙하 시 힌지 크랙 원인은?"),
                   ("persona", "짧다")]:
        sq.note(src, t, picked=["mech-drop"])
    c = TestClient(a.app)
    v = c.get("/seat-questions", headers={"Authorization": SECRET}).json()
    assert v["collect"] is True and v["total"] == 4
    assert v["filtered"] == {"short": 0, "not_standalone": 1, "near_dup": 1}, "짧은 것은 적을 때 이미 뺐다"
    assert [i["src"] for i in v["items"]] == ["persona", "thinking", "delib", "expert_cmd"], "출처를 번갈아"
    assert not (_keys(v) & LEAK_KEYS), _keys(v) & LEAK_KEYS
    assert c.get("/seat-questions?src=nope", headers={"Authorization": SECRET}).status_code == 422


def test_사람_표시는_규약대로_받고_질문_글과_함께_남긴다(box, monkeypatch):
    monkeypatch.setenv("SEAT_QUESTIONS_COLLECT", "1")
    sq.note("persona", "PCB 휨 기준을 어떻게 잡나요", picked=["pcb-warp"])
    sq.note("thinking", "과제 코드 A123 의 고장모드 목록")
    qa, qb = sorted(r["qid"] for r in _rows(box))
    c = TestClient(a.app)
    H = {"Authorization": SECRET}
    pcb = next(r["qid"] for r in _rows(box) if r["src"] == "persona")
    core = qa if qb == pcb else qb
    bad = [{"qid": pcb, "by": "a"},                                                   # 셋 다 없음
           {"qid": pcb, "by": "a", "primary": ["pcb-warp"], "none": "core_data"},      # 둘
           {"qid": pcb, "by": "a", "primary": ["a1", "a2", "a3", "a4"]},                # 넷
           {"qid": pcb, "by": "a", "primary": ["pcb-warp"], "also": ["pcb-warp"]},      # 겹침
           {"qid": pcb, "by": "a", "none": "nobody"},
           {"qid": pcb, "by": "a", "primary": ["pcb-warp"], "via": {"pcb-warp": "guess"}},
           {"qid": pcb, "by": "a", "primary": ["pcb-warp"], "rank": 1}]
    for b in bad:
        assert c.post("/seat-questions/label", json=b, headers=H).status_code == 422, b
    assert c.post("/seat-questions/label", json={"qid": "H-000000000000", "by": "a", "unsure": True}, headers=H).status_code == 404
    ok = c.post("/seat-questions/label", json={"qid": pcb, "by": "a", "primary": ["pcb-warp"], "also": ["mech-drop"],
                                               "via": {"pcb-warp": "tree", "mech-drop": "search"}}, headers=H)
    assert ok.status_code == 200, ok.text
    assert c.post("/seat-questions/label", json={"qid": core, "by": "a", "none": "core_data"}, headers=H).status_code == 200
    assert c.post("/seat-questions/label", json={"qid": pcb, "by": "b", "primary": ["mech-drop"]}, headers=H).status_code == 200
    v = c.get("/seat-questions?by=b", headers=H).json()
    row = next(i for i in v["items"] if i["qid"] == pcb)
    assert row["mine"]["primary"] == ["mech-drop"] and row["others"] == 1, "남의 표시는 수로만 — 겹쳐 다는 사람이 답을 보지 않는다"
    assert not (_keys(v) & LEAK_KEYS)
    h = {r["qid"]: r for r in sq.export(by="a")}
    assert h[pcb]["q"] == "PCB 휨 기준을 어떻게 잡나요" and h[pcb]["primary"] == ["pcb-warp"] and h[pcb]["picked"] == ["pcb-warp"]
    assert h[pcb]["others"][0]["by"] == "b" and h[core]["none"] == "core_data"
    # 날짜 파일이 지워져도 표시와 글은 남는다(기준선 H 그 자체)
    for f in box.glob("q-*.jsonl"):
        f.unlink()
    sq._VIEW_CACHE.update(key=None)
    assert {r["qid"] for r in sq.export(by="a")} == {pcb, core}
    assert c.post("/seat-questions/label", json={"qid": pcb, "by": "a", "remove": True}, headers=H).json()["removed"] is True
    assert {r["qid"] for r in sq.export(by="a")} == {core}
    monkeypatch.setenv("SEAT_QUESTIONS_LABELERS", "a,b")
    assert c.post("/seat-questions/label", json={"qid": core, "by": "c", "unsure": True}, headers=H).status_code == 422, \
        "다는 사람은 박스 설정이 정한다"


def test_포털이_밀어_넣는_지난_날들(box, monkeypatch):
    import time
    monkeypatch.setenv("SEAT_QUESTIONS_COLLECT", "1")
    c = TestClient(a.app)
    H = {"Authorization": SECRET}
    now = time.time()
    r = c.post("/seat-questions/ingest", json={"items": [
        {"src": "persona", "text": "열충격 수명 예측 모델을 어떻게 고르나요", "ts": now - 86400 * 3, "picked": ["rel-thermal"]},
        {"src": "persona", "text": "열충격 수명 예측 모델을 어떻게 고르나요", "ts": now - 86400 * 2},
        {"src": "thinking", "text": "너무 오래된 질문입니다 백일 전", "ts": now - 86400 * 100},
        {"src": "thinking", "text": "짧음"}]}, headers=H)
    assert r.status_code == 200 and r.json() == {"added": 1, "dup": 1, "old": 1, "short": 1}, r.text
    for bad in ({"items": [{"src": "persona", "text": "질문", "conversation_id": "abc"}]},
                {"items": [{"src": "persona", "text": "질문", "user_email": "x@y.z"}]},
                {"items": []}, {"rows": []}):
        assert c.post("/seat-questions/ingest", json=bad, headers=H).status_code == 422, bad
    assert c.post("/seat-questions/ingest", json={"items": [{"src": "persona", "text": "열충격 질문입니다"}]}).status_code == 401
    rows = _rows(box)
    assert len(rows) == 1 and rows[0]["picked"] == ["rel-thermal"] and "ts" not in rows[0]


def test_날짜_파일은_굴러가는_창으로_지운다(box, monkeypatch):
    monkeypatch.setenv("SEAT_QUESTIONS_COLLECT", "1")
    monkeypatch.setenv("SEAT_QUESTIONS_DAYS", "60")
    box.mkdir(parents=True)
    (box / "q-2000-01-01.jsonl").write_text('{"qid":"H-aaaaaaaaaaaa","d":"2000-01-01","src":"thinking","text":"옛 질문 하나 둘"}\n',
                                            encoding="utf-8")
    assert sq.note("thinking", "새 질문 하나 둘 셋") is True
    assert not (box / "q-2000-01-01.jsonl").exists()
    assert os.environ.get("SEAT_QUESTIONS_COLLECT") == "1"

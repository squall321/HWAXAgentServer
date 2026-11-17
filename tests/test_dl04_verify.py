# 결정문 원천 대조(DL-04) — 수(작은 정수까지) · ID · 부품 번호 · 넷 이름을 자리와 함께 뽑아 근거 항목 key 나 'no source' 를 대고, 쪽 이음을 본다
"""HWAXPortal docs/request-1117 (DL-04).

  · 낱말 종류는 delib_verify.PATTERNS 한 곳 — 겹치면 net → id → part → num 차례, 한글 조사가 붙어도 낱말이다. 근거 표지 · 줄 머리 순번 · 엔진이 끝에
    붙이는 안내(tail_from 부터)는 보지 않는다.
  · 출처는 잡을 열 때 원장에 남긴 근거 낱말 색인(evidence_index — 본문 없음) · 화두 · 사람 의견. 판정하지 않는다('53 → 53' 을 드러낼 뿐).
  · 쪽 이음 — deliberate_result 의 full · summary 쪽을 이으면 원문과 같다. 요약의 numcheck 는 그대로다.

  실행:  .venv/bin/python -m pytest tests/test_dl04_verify.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import delib_verify as V  # noqa: E402
import mcp_pages  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import _caps, _end, _play, _tick, eng  # noqa: E402, F401
from test_delib_same_aff import _ctx  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

ME, OUT = "me@example.com", "out@example.com"


def _kinds(text, kinds=V.KINDS):
    return [(k, v) for k, v, _p in V.tokens(text, kinds)]


# ── 낱말 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_종류마다_가른다_한글_조사가_붙어도_낱말이다():
    text = "VDD_1V8 넷과 GND, R12의 값은 3.5mm 이고 ABC-12-003 과 ABCD2 는 코드다. 품번 XYZ12345AB 는 1,234개 중 2개다."
    assert _kinds(text) == [("net", "VDD_1V8"), ("net", "GND"), ("part", "R12"), ("num", "3.5"), ("id", "ABC-12-003"),
                            ("id", "ABCD2"), ("part", "XYZ12345AB"), ("num", "1234"), ("num", "2")]


def test_비교값_둘과_문장_끝_소수를_자리와_함께_드러낸다():
    toks = V.tokens("간격 53 → 53 이다. 응력은 412.5.")
    assert [(k, v) for k, v, _p in toks] == [("num", "53"), ("num", "53"), ("num", "412.5")]
    assert toks[0][2] != toks[1][2], "같은 값이라도 자리가 다르다"


def test_근거_표지와_줄_머리_순번은_값이_아니다():
    text = "1. 첫째 결론 [e:3|K-1] 와 [e:4]\n## 2. 둘째\n- 값 7"
    assert _kinds(text) == [("num", "7")]


def test_scope_밖의_종류도_자리는_차지한다():
    assert _kinds("R12 와 3", ("num",)) == [("num", "3")], "R12 의 12 를 수로 다시 읽지 않는다"
    with pytest.raises(ValueError):
        V.parse_scope("num,nope")


# ── 색인 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_색인은_엔진처럼_본문_있는_항목만_번호를_매긴다(monkeypatch):
    ev = [{"source": "빈", "result": ""}, {"source": "가", "key": "K-1", "result": "R12 는 3.5"}, {"source": "나", "result": "VDD_1V8 와 7"}]
    idx = V.index(ev)
    assert [(e["n"], e["key"], e["source"]) for e in idx] == [(1, "K-1", "가"), (2, "", "나")]
    h = V.token_hash
    assert idx[0]["t"] == {"num": [h("num", "3.5")], "part": [h("part", "R12")]}
    assert idx[1]["t"] == {"num": [h("num", "7")], "net": [h("net", "VDD_1V8")]}
    dumped = json.dumps(idx, ensure_ascii=False)
    for plain in ("R12", "VDD_1V8", "3.5", "result"):
        assert plain not in dumped, f"색인에 낱말 글자 {plain} 가 남았다 — 지문만 남긴다"
    monkeypatch.setattr(V, "INDEX_ITEM_MAX", 1)
    assert V.index(ev)[0].get("cut") is True


# ── 대조 ────────────────────────────────────────────────────────────────────────────────────────────────────
def _job(**kw):
    now = delib_jobs._now()
    ev = [{"source": "s1", "key": "K-1", "result": "간격 53 mm · R12 · VDD_1V8"}, {"source": "s2", "result": "ABC-12-003 판"}]
    job = {"id": "default-20261117-020202-dddddd", "job": "default", "status": "done", "question": "R12 의 간격 9 는?",
           "chair_template": None, "opts": {"human_note": "사람 의견 값 77"}, "stage": "report", "seats": [], "turns": [],
           "decision": ("## 결정\nR12 간격 53 → 53 [e:1|K-1]. ABC-12-003 과 U7 · 1532 · 9 · 77 · 4321.\n\n"
                        "> ⚠ 다음 수치는 심의에 제시된 근거에서 확인되지 않았습니다 — 의장이 …(1건): `4321`\n\n📄 Report Archive 보고서 #77 저장."),
           "evidence_index": V.index(ev), "started_at": now - 5, "updated_at": now, "finished_at": now, "user": ME, "owner_aff": "T-A",
           "numcheck": {"unsourced": ["4321"], "unsourced_count": 1, "checked": 3}, "error": None, "warnings": [], "evidence_omitted": []}
    job.update(kw)
    delib_jobs._JOBS[job["id"]] = job
    return job


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    for name in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, name, {})
    return _job


def test_낱말마다_자리와_근거_key_를_대고_없으면_no_source(ledger):
    job = ledger()
    out = V.verify(job, forbidden=["53", "1532"])
    rows = {(r["kind"], r["value"]): r for r in out["tokens"]}
    assert rows[("part", "R12")]["src"] == ["K-1", "question"]
    assert rows[("num", "53")]["src"] == ["K-1"] and len(rows[("num", "53")]["pos"]) == 2, "53 → 53 — 두 자리, 판정하지 않는다"
    assert rows[("id", "ABC-12-003")]["src"] == ["e:2"], "key 없는 항목은 e:N"
    assert rows[("part", "U7")]["src"] == V.NO_SOURCE and rows[("num", "1532")]["src"] == V.NO_SOURCE
    assert rows[("num", "9")]["src"] == ["question"] and rows[("num", "77")]["src"] == ["human_note"]
    assert ("num", "1") not in rows, "근거 표지 [e:1|K-1] 의 1 은 값이 아니다"
    text = job["decision"]
    assert out["tail_from"] == text.find("\n\n> ⚠") and out["checked_chars"] == out["tail_from"]
    assert sum(len(r["pos"]) for r in out["tokens"] if r["value"] == "4321") == 1, "엔진이 붙인 경고 · 보고서 안내는 보지 않는다"
    for r in out["tokens"]:
        for p in r["pos"]:
            assert text[p:p + len(r["value"])].replace(",", "") == r["value"], "자리는 결정문 원문의 글자 번호다"
    assert out["totals"]["num"]["no_source"] == 2 and out["totals"]["part"] == {"tokens": 2, "unique": 2, "no_source": 1, "no_source_hits": 1}
    f = {x["value"]: x for x in out["forbidden"]}
    assert f["53"]["count"] == 2 and f["1532"]["count"] == 1 and all(text[p:p + 2] == "53" for p in f["53"]["pos"])
    assert out["index"]["present"] is True and out["index"]["pseudo_sources"] == ["question", "human_note"]


def test_색인_이전_잡은_출처를_모른다고_말한다(ledger):
    job = ledger(evidence_index=None)
    del job["evidence_index"]
    out = V.verify(job)
    assert out["index"]["present"] is False and "근거 항목 출처는 모른다" in out["note"]


def test_결정문이_없으면_말한다(ledger):
    out = V.verify(ledger(status="running", decision=None))
    assert out["tokens"] == [] and "끝나지 않았다" in out["note"]


def test_잡을_열면_근거_색인을_원장에_남긴다(eng, monkeypatch):
    _caps(monkeypatch, 2)

    async def scenario():
        out = await m.deliberate_start("색인", evidence=[{"source": "s", "key": "K-1", "result": "R12 와 3.5"}], ctx=_ctx(ME))
        job = delib_jobs.get(out["job_id"])
        assert job["evidence_index"] == [{"n": 1, "key": "K-1", "source": "s", "chars": 9,
                                          "t": {"num": [V.token_hash("num", "3.5")], "part": [V.token_hash("part", "R12")]}}]
        assert "R12" not in json.dumps(job["evidence_index"]) and "evidence_index_pending" not in job
        on_disk = json.loads(delib_jobs._path(job["id"]).read_text(encoding="utf-8"))
        assert on_disk["evidence_index"] == job["evidence_index"] and "evidence_index" not in delib_jobs.summary(job)
        out2 = await m.deliberate_start("근거 없음", ctx=_ctx(ME))
        assert "evidence_index" not in delib_jobs.get(out2["job_id"]), "근거가 없는 잡의 원장은 종전 그대로"
        await _tick()

    _play(scenario)


def test_큰_근거의_색인은_잡_태스크가_스레드에서_짓는다(eng, monkeypatch):
    _caps(monkeypatch, 2)
    monkeypatch.setattr(V, "INDEX_SYNC_CHARS", 5)
    threads = []
    real = asyncio.to_thread

    async def spy(fn, *a, **kw):
        threads.append(getattr(fn, "__name__", fn))
        return await real(fn, *a, **kw)
    monkeypatch.setattr(delib_jobs.asyncio, "to_thread", spy)

    async def scenario():
        out = await m.deliberate_start("큰 근거", evidence=[{"source": "s", "key": "K-1", "result": "R12 와 3.5 · 길다"}], ctx=_ctx(ME))
        job = delib_jobs.get(out["job_id"])
        assert job.get("evidence_index_pending") is True and "evidence_index" not in job, "시작 자리에서 짓지 않는다"
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "evidence_index" in job:
                break
        assert threads == ["index"] and job["evidence_index"][0]["t"]["part"] == [V.token_hash("part", "R12")]
        assert "evidence_index_pending" not in job and eng.started == ["큰 근거"]
        await _end(eng, job)

    _play(scenario)


@pytest.mark.parametrize("name,text", [
    ("dashes", "-" * 1_000_000), ("dots", "." * 1_000_000), ("a.", "a." * 500_000), ("a-", "a-" * 500_000),
    ("A_", "A_" * 500_000), ("upper", "A" * 1_000_000), ("digits", "1" * 1_000_000), ("a.-1", "a.-" * 333_333 + "1"),
])
def test_낱말_뽑기는_1MB_의_악성_글에도_1초_안이다(name, text):
    import time
    t = time.perf_counter()
    V.tokens(text)
    assert time.perf_counter() - t < 1.0, f"{name} — 정규식이 제곱으로 느려졌다(반대 검토 H1)"
    t = time.perf_counter()
    V.index([{"result": text}])
    assert time.perf_counter() - t < 1.0, f"{name} — 색인(항목마다 앞 {V.SCAN_ITEM_MAX:,}자)"


def test_숫자_없는_점_하이픈_이음은_id_가_아니다():
    assert _kinds("risk-review 와 e.g 와 org.example.svc 그리고 a.b.c1") == [("id", "a.b.c1")]


# ── 쪽 이음 ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_쪽으로_나눈_결정문을_이으면_원문과_같다(ledger, monkeypatch):
    monkeypatch.setattr(mcp_pages, "PAGE_CHARS", 4000)
    job = ledger(decision="## 결정\n" + "".join(f"줄 {i} — R{i % 90 + 1} 값 {i}.\n" for i in range(900)))
    out = m.verify_job(job, scope="num", forbidden=None)
    for view in ("full", "summary"):
        p = out["pages"][view]
        assert p["parts"] > 1 and p["ok"] is True and p["gaps"] == [] and p["overlaps"] == [], (view, p)
        assert p["joined_chars"] == p["text_chars"] == len(job["decision"])
    assert out["pages"]["page_chars"] == 4000


def test_이음_점검은_빈틈과_겹침을_자리로_준다():
    text = "가" * 30
    pages = {1: {"decision": text[:10], "page": {"parts": 3, "pieces": [{"path": "decision", "from": 0, "to": 10, "of": 30}]}},
             2: {"decision": text[12:20], "page": {"parts": 3, "pieces": [{"path": "decision", "from": 12, "to": 20, "of": 30}]}},
             3: {"decision": text[18:30], "page": {"parts": 3, "pieces": [{"path": "decision", "from": 18, "to": 30, "of": 30}]}}}
    p = V.page_join({"decision": text}, lambda n: pages[n])
    assert p["ok"] is False and p["gaps"] == [{"part": 2, "from": 10, "to": 12}] and p["overlaps"] == [{"part": 3, "from": 18, "to": 20}]


# ── MCP · HTTP ─────────────────────────────────────────────────────────────────────────────────────────────
def test_MCP_대조와_요약의_numcheck_는_그대로다(ledger):
    job = ledger()
    out = asyncio.run(m.deliberate_verify(job["id"], ctx=_ctx(ME)))
    assert out["job_id"] == job["id"] and out["scope"] == list(V.KINDS) and out["pages"]["full"]["ok"] is True
    summ = asyncio.run(m.deliberate_result(job["id"], view="summary", ctx=_ctx(ME)))
    assert summ["numcheck"] == job["numcheck"], "요약의 수치 대조는 그대로다"
    with pytest.raises(ValueError):
        asyncio.run(m.deliberate_verify(job["id"], ctx=_ctx(OUT, "T-B")))
    one = asyncio.run(m.deliberate_verify(job["id"], scope="part", ctx=_ctx(ME)))
    assert {r["kind"] for r in one["tokens"]} == {"part"} and set(one["totals"]) == {"part"}


def test_MCP_대조가_길면_쪽으로_나누고_다음_쪽_인자를_준다(ledger, monkeypatch):
    monkeypatch.setattr(mcp_pages, "PAGE_CHARS", 4000)
    job = ledger(decision="## 결정\n" + " ".join(f"R{i} {i}.5" for i in range(1, 600)))
    p1 = asyncio.run(m.deliberate_verify(job["id"], scope="num,part", forbidden=["1.5"], ctx=_ctx(ME)))
    assert p1["page"]["parts"] > 1
    assert p1["page"]["next"] == {"job_id": job["id"], "scope": "num,part", "forbidden": ["1.5"], "part": 2}


def test_HTTP_대조(ledger, monkeypatch):
    from fastapi.testclient import TestClient
    import app as a
    import seat_box
    job = ledger()
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    auth = {"Authorization": "Bearer gw-secret", "x-hwax-user": ME}
    r = c.post("/deliberate/verify", json={"job_id": job["id"], "scope": "num", "forbidden": ["53"]}, headers=auth)
    assert r.status_code == 200 and r.json()["forbidden"][0]["count"] == 2 and "page" not in r.json()
    assert c.post("/deliberate/verify", json={"job_id": job["id"], "scope": "x"}, headers=auth).status_code == 422
    assert c.post("/deliberate/verify", json={"job_id": job["id"]}).status_code == 401

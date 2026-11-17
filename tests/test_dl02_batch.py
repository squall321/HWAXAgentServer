# 심의 묶음 실행(DL-02) — 같은 key 두 번 0 · 끝난 key 다시 0 · 멈춘 뒤 새로 띄우지 않음 · 재시도 상한 · 동시 한도 · 좌석 유실 표지 · 재기동 · 끝남 표시
"""HWAXPortal docs/request-1117 (DL-02) — delib_batch 머리말의 규칙을 실제 asyncio 태스크(test_delib_job_queue 의 문 달린 가짜 엔진)로 본다.

가짜는 엔진(문이 열릴 때까지 '도는 중')과 게이트웨이 한 번 부르기(delib_refs.call_alias)뿐이다 — 잡 원장 · 줄 · 묶음 원장 · MCP 도구는 진짜다.

  실행:  .venv/bin/python -m pytest tests/test_dl02_batch.py -q
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import delib_batch as B  # noqa: E402
import delib_jobs  # noqa: E402
import delib_refs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import _caps, _end, _play, _tick, eng  # noqa: E402, F401
from test_delib_same_aff import _ctx  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

ME, MATE, OUT = "me@example.com", "mate@example.com", "out@example.com"


@pytest.fixture
def bt(eng, monkeypatch):
    monkeypatch.setattr(B, "_BATCHES", {})
    monkeypatch.setattr(B, "_INDEX", None)          # 묶음 색인도 시험마다 새 원장 폴더에서 다시 읽는다
    monkeypatch.setattr(B, "_FEEDING", set())
    monkeypatch.setattr(B, "_APP", m._APP)
    _caps(monkeypatch, 6, per_user=6)
    return eng


def _items(*qs, **kw):
    return [{"key": f"K-{i + 1}", "question": q, **kw} for i, q in enumerate(qs)]


def _new(name="묶음-가", items=None, **kw) -> dict:
    return B.create(m._APP, name=name, items=_items("가", "나", "다") if items is None else items, user=ME, groups=["CAEG"],
                    owner_aff="TEAM-A", **kw)


def _state(b) -> list:
    return [it["state"] for it in b["items"]]


def _job_of(b, key) -> dict:
    it = next(x for x in b["items"] if x["key"] == key)
    return delib_jobs.get(it["job_id"])


def _on_disk(b) -> dict:
    return json.loads(B._path(b["id"]).read_text(encoding="utf-8"))


def _elapse(b) -> None:
    """물러서기(retry_at)가 다 지난 것으로 — 시험이 시계를 기다리지 않는다."""
    for it in b["items"]:
        it.pop("retry_at", None)


# ── 만들기 ────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("items,msg", [
    ([], "비지 않은"),
    ([{"key": "A", "question": "q"}, {"key": "A", "question": "r"}], "둘이다"),
    ([{"key": "a b", "question": "q"}], "key"),
    ([{"key": "A", "question": " "}], "비었다"),
    ([{"key": "A", "question": "q", "personas": []}], "모르는 칸"),
    ([{"key": "A", "question": "q", "job": "nope"}], "알 수 없는 심의 종류"),
    ([{"key": "A", "question": "q", "evidence": [{"ref": {"kind": "dynaforge_report", "id": "D"}}]}], "kind not opened yet"),
])
def test_틀린_묶음은_만들지_않는다(bt, items, msg):
    with pytest.raises(ValueError, match=msg):
        _new(items=items)
    assert B._BATCHES == {} and not B._dir().exists()


def test_신원_없는_호출은_묶음을_시작하지_못한다(bt):
    with pytest.raises(ValueError, match="신원"):
        B.create(m._APP, name="x", items=_items("가"), user="", groups=[])


def test_동시_수는_보수적인_기본과_사람별_한도_안이다(bt, monkeypatch):
    b = _new()
    assert b["concurrency"] == B.CONCURRENCY_DEFAULT == 1 and b["retries"] == 1 and b["stall_s"] == 0
    _caps(monkeypatch, 6, per_user=2)
    b2 = _new(name="묶음-나", concurrency=6)
    assert b2["concurrency"] == 2 and "사람별 한도" in b2["notes"][0]
    b3 = _new(name="묶음-다", stall_s=30)
    assert b3["stall_s"] == B.STALL_FLOOR_S and "하한" in b3["notes"][0]


# ── 먹이기 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_한도_안에서_하나씩_띄우고_끝나면_다음을_띄운다(bt):
    async def scenario():
        b = _new()
        await B.feed(b)
        await _tick()
        assert _state(b) == ["running", "pending", "pending"] and bt.started == ["가"]
        await B.feed(b)
        assert bt.started == ["가"], "같은 key 를 두 번 띄우지 않고 한도(1)를 넘지 않는다"
        await _end(bt, _job_of(b, "K-1"))
        await B.feed(b)
        await _tick()
        assert _state(b) == ["done", "running", "pending"] and bt.started == ["가", "나"]
        await _end(bt, _job_of(b, "K-2"))
        await B.feed(b)
        await _tick()
        await _end(bt, _job_of(b, "K-3"))
        await B.feed(b)
        assert _state(b) == ["done"] * 3 and b["status"] == "done" and b["finished_at"]
        assert bt.peak == 1, "동시에 둘이 돈 적이 없다"
        v = B.view(b)
        assert v["finished"] is True and "모두 끝남" in v["notice"] and v["counts"]["done"] == 3
        assert all(it["evidence"] is None for it in b["items"])
        job = _job_of(b, "K-2")
        assert job["batch"] == {"id": b["id"], "key": "K-2", "token": job["batch"]["token"]} and job["user"] == ME
        assert _on_disk(b)["status"] == "done"

    _play(scenario)


def test_실패는_retries_번까지만_다시_띄운다(bt):
    async def scenario():
        b = _new(items=_items("가"), retries=1)
        bt.fail.add("가")
        await B.feed(b)
        await _tick()
        await _end(bt, _job_of(b, "K-1"), fail=True)
        await B.feed(b)                      # 실패한 시도 — 물러선다(같은 틱에 다시 띄우지 않는다)
        assert b["items"][0]["state"] == "pending" and b["items"][0]["retry_at"] > delib_jobs._now() and bt.started == ["가"]
        _elapse(b)
        await B.feed(b)                      # 물러서기가 지났다 — 다시 띄운다(문이 이미 열려 곧바로 또 실패한다)
        await _tick()
        await B.feed(b)
        (it,) = b["items"]
        assert it["state"] == "failed" and it["tries"] == 2, it
        assert [a["status"] for a in it["attempts"]] == ["started", "error", "started", "error"]
        assert bt.started == ["가", "가"], "시도는 1 + retries 번"

    _play(scenario)


def test_다시_띄운_시도가_되면_끝난다(bt):
    async def scenario():
        b = _new(items=_items("가"), retries=2)
        bt.fail.add("가")
        await B.feed(b)
        await _tick()
        await _end(bt, _job_of(b, "K-1"), fail=True)
        bt.fail.discard("가")
        await B.feed(b)
        _elapse(b)
        await B.feed(b)
        await _tick()
        await B.feed(b)
        assert b["items"][0]["state"] == "done" and b["items"][0]["tries"] == 2 and b["status"] == "done"

    _play(scenario)


def test_사람이_접은_잡은_다시_띄우지_않는다(bt):
    async def scenario():
        b = _new(items=_items("가"), retries=3)
        await B.feed(b)
        await _tick()
        job = _job_of(b, "K-1")
        delib_jobs.cancel(job["id"], by=ME)
        await asyncio.wait([delib_jobs._TASKS.get(job["id"])] if job["id"] in delib_jobs._TASKS else [asyncio.sleep(0)])
        await _tick()
        await B.feed(b)
        assert b["items"][0]["state"] == "cancelled" and bt.started == ["가"]

    _play(scenario)


def test_멈추면_새로_띄우지_않고_도는_잡은_지켜본다(bt):
    async def scenario():
        b = _new()
        await B.feed(b)
        await _tick()
        out = m.batch_stop(b["id"], user=ME)
        assert out["status"] == "stopping" and out["in_engine"][0]["key"] == "K-1" and "deliberate_cancel" in out["note"]
        assert _state(b) == ["running", "stopped", "stopped"]
        await _end(bt, _job_of(b, "K-1"))
        await B.feed(b)
        assert b["status"] == "stopped" and _state(b) == ["done", "stopped", "stopped"] and bt.started == ["가"]
        assert "멈춤" in b["notice"]
        with pytest.raises(PermissionError):
            m.batch_stop(b["id"], user=OUT)

    _play(scenario)


def test_같은_이름은_도는_동안_거절하고_다시_시작하면_끝난_key_는_건너뛴다(bt):
    async def scenario():
        b = _new()
        with pytest.raises(ValueError, match="아직 돈다"):
            _new()
        await B.feed(b)
        await _tick()
        await _end(bt, _job_of(b, "K-1"))
        await B.feed(b)
        m.batch_stop(b["id"], user=ME)
        await _tick()
        await _end(bt, _job_of(b, "K-2"))
        await B.feed(b)
        assert b["status"] == "stopped" and _state(b) == ["done", "done", "stopped"]
        again = _new()
        assert again["previous"] == b["id"] and _state(again) == ["skipped", "skipped", "pending"]
        assert again["items"][0]["done_in"] == {"batch_id": b["id"], "job_id": b["items"][0]["job_id"]}
        await B.feed(again)
        await _tick()
        assert bt.started == ["가", "나", "다"], "끝난 key 는 다시 돌지 않는다"
        await _end(bt, _job_of(again, "K-3"))
        await B.feed(again)
        assert again["status"] == "done" and again["notice"]

    _play(scenario)


def test_줄이_차면_시도로_세지_않고_기다린다(bt, monkeypatch):
    _caps(monkeypatch, 1, per_user=1, queue=0)

    async def scenario():
        other = delib_jobs.start(m._APP, "default", "남의 심의", user_email=OUT)
        await _tick()
        b = _new(items=_items("가"))
        await B.feed(b)
        (it,) = b["items"]
        assert it["state"] == "pending" and it["tries"] == 0 and "token" not in it and _on_disk(b)["items"][0]["state"] == "pending"
        await _end(bt, other)
        await B.feed(b)
        await _tick()
        assert it["state"] == "running" and it["tries"] == 1

    _play(scenario)


def test_새_심의_멈춤이면_기다린다(bt, monkeypatch):
    monkeypatch.setattr(delib_jobs, "pause_info", lambda: {"on": True, "retry_after_s": 120})

    async def scenario():
        b = _new(items=_items("가"))
        await B.feed(b)
        assert b["items"][0]["state"] == "pending" and bt.started == []

    _play(scenario)


# ── 좌석 유실 ─────────────────────────────────────────────────────────────────────────────────────────────
def _script(lost: bool) -> list:
    seats = [{"key": "s-a"}, {"key": "s-b"}]
    ev = [d._sse("delib", {"kind": "personas", "personas": seats, "totalRounds": 2})]
    for r in (1, 2):
        for s in ("s-a", "s-b"):
            if lost and r == 2 and s == "s-b":
                continue
            ev.append(d._sse("delib", {"kind": "turn", "persona": s, "round": r, "say": f"{s} {r}R", "stance": "동의"}))
    if lost:
        ev.append(d._sse("delib", {"kind": "evidence", "source": "좌석 유실", "included": False, "text": "2라운드 좌석 유실 — s-b"}))
    tally = {"agree": 1 if lost else 2, "conditional": 0, "oppose": 0, "abstain": 1 if lost else 0, "responded": 1 if lost else 2, "total": 2}
    votes = [{"seat": "s-a", "stance": "동의", "responded": True}, {"seat": "s-b", "stance": "미표명" if lost else "동의", "responded": not lost}]
    ev.append(d._sse("delib", {"kind": "outcome", "report_id": None, "tally": tally, "unanimous": not lost, "votes": votes}))
    return ev


def test_좌석이_하나라도_빠지면_믿을_수_없는_결정이다(bt):
    bt.script["가"], bt.script["나"] = _script(lost=False), _script(lost=True)

    async def scenario():
        b = _new(items=_items("가", "나"), concurrency=2)
        await B.feed(b)
        await _tick()
        await _end(bt, _job_of(b, "K-1"))
        await _end(bt, _job_of(b, "K-2"))
        await B.feed(b)
        ok, bad = b["items"]
        assert ok["trust"] == "trustworthy" and ok["seat_loss"]["lost"] == []
        assert bad["trust"] == "not_trustworthy" and bad["seat_loss"]["lost"] == ["s-b"]
        assert bad["seat_loss"]["by_round"] == {"2": ["s-b"]} and bad["seat_loss"]["notices"] == 1 and bad["seat_loss"]["unanswered"] == ["s-b"]
        assert b["status"] == "done" and "믿을 수 없는 결정 1건" in b["notice"] and B.view(b)["counts"]["not_trustworthy"] == 1

    _play(scenario)


def test_좌석_정보가_없으면_모른다():
    assert B._trust(B.seat_loss({"status": "done"})) == "unknown"
    assert B._trust(B.seat_loss({"tally": {"total": 3, "responded": 2}})) == "not_trustworthy", "표결의 응답 수가 착석보다 적다"


# ── 근거 참조 · 멈춤 감지 ─────────────────────────────────────────────────────────────────────────────────
def test_항목의_참조는_띄울_때_주인_신원으로_푼다(bt, monkeypatch):
    seen = []
    ev = [{"source": "s", "tool": "t", "args": "", "result": "묶음 본문 1", "key": "R-1"}]
    body = {"bundle_id": "BND-1", "fingerprint": delib_refs.fingerprint(ev), "evidence": ev}

    async def call(_app, *, groups, user, user_pat, alias, args):
        seen.append((user, list(groups), alias, args))
        return False, json.dumps(body, ensure_ascii=False)
    monkeypatch.setattr(delib_refs, "call_alias", call)

    async def scenario():
        b = _new(items=[{"key": "K-1", "question": "가", "job": "risk-review", "evidence": [{"ref": {"kind": "risk_bundle", "id": "BND-1"}}],
                         "advanced": {"rounds": 2, "voc": "off"}}])
        await B.feed(b)
        await _tick()
        assert seen == [(ME, ["CAEG"], "heaxhwax_risk_risk_get_evidence_bundle", {"bundle_id": "BND-1"})]
        opts = bt.seen["가"].opts
        assert opts["evidence"] == ev and opts["rounds"] == 2 and opts["voc"] == "off"
        assert _job_of(b, "K-1")["evidence_refs"][0]["ok"] is True

    _play(scenario)


def test_멈춤_감지를_켜면_오래_조용한_잡을_접고_다시_띄운다(bt, monkeypatch):
    monkeypatch.setattr(m, "_quiet_ok_s", lambda _job: 0.0)

    async def scenario():
        b = _new(items=_items("가"), stall_s=B.STALL_FLOOR_S, retries=1)
        await B.feed(b)
        await _tick()
        job = _job_of(b, "K-1")
        job["updated_at"] = delib_jobs._now() - B.STALL_FLOOR_S - 5
        await B.feed(b)                       # 접는다
        await _tick()
        assert job["status"] == "cancelled"
        await B.feed(b)                       # 실패한 시도로 센다(물러선다)
        _elapse(b)
        await B.feed(b)                       # 다시 띄운다
        await _tick()
        (it,) = b["items"]
        assert it["state"] == "running" and it["tries"] == 2 and "멈춤 감지" in it["attempts"][1]["why"]

    _play(scenario)


def test_멈춤_감지는_기본으로_꺼져_있다(bt):
    async def scenario():
        b = _new(items=_items("가"))
        await B.feed(b)
        await _tick()
        job = _job_of(b, "K-1")
        job["updated_at"] = delib_jobs._now() - 10 * 86400
        await B.feed(b)
        await _tick()
        assert job["status"] == "running", "조용한 것이 멈춘 것은 아니다 — 켜지 않으면 시간으로 접지 않는다"

    _play(scenario)


# ── 재기동 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_재기동하면_원장에서_묶음을_올린다(bt):
    async def scenario():
        b = _new()
        await B.feed(b)
        await _tick()
        B._BATCHES.clear()
        assert B.bind(m._APP) == 1 and B._BATCHES[b["id"]]["items"][0]["state"] == "running"

    _play(scenario)


def test_띄우는_중에_끊긴_항목은_표지로_잡을_찾아_잇는다(bt):
    async def scenario():
        b = _new(items=_items("가", "나"))
        it = b["items"][0]
        it.update(state="starting", token="tok-1")
        job = delib_jobs.start(m._APP, "default", "가", user_email=ME, batch={"id": b["id"], "key": "K-1", "token": "tok-1"})
        b["items"][1].update(state="starting", token="tok-none")
        B._reconcile(b)
        assert (it["state"], it["job_id"], it["tries"]) == ("running", job["id"], 1), "새로 띄우지 않고 그 잡을 잇는다"
        assert b["items"][1]["state"] == "pending" and "token" not in b["items"][1], "잡이 없으면 띄우지 않은 것이다"
        await _tick()

    _play(scenario)


def test_끊긴_잡을_제자리로_못_이으면_실패한_시도로_다시_띄운다(bt):
    async def scenario():
        b = _new(items=_items("가"), retries=1)
        await B.feed(b)
        await _tick()
        job = _job_of(b, "K-1")
        task = delib_jobs._TASKS[job["id"]]
        delib_jobs.closing(True)
        task.cancel()
        await asyncio.wait([task])
        delib_jobs.closing(False)
        assert job["status"] == "interrupted"
        delib_jobs._drop_args(job["id"])      # 시작 인자 파일이 없다 — 제자리 이어하기를 못 한다
        await B.feed(b)
        _elapse(b)
        await B.feed(b)
        await _tick()
        (it,) = b["items"]
        assert it["attempts"][1]["status"] == "interrupted" and "제자리로 잇지 못했다" in it["attempts"][1]["why"]
        assert it["state"] == "running" and it["tries"] == 2

    _play(scenario)


# ── MCP · HTTP ──────────────────────────────────────────────────────────────────────────────────────────────
def test_MCP_시작_진행_멈춤(bt):
    async def scenario():
        out = await m.deliberate_batch_start("묶음-엠", _items("가", "나"), ctx=_ctx(ME, "TEAM-A", "CAEG"))
        assert out["status"] == "running" and out["in_engine"] == [], "첫 먹이기는 요청 밖에서 돈다(응답을 붙잡지 않는다)"
        assert "items" not in out and "batch_id" in out and "deliberate_batch_status" in out["note"]
        await asyncio.gather(*list(B._KICKS))
        st = await m.deliberate_batch_status(out["batch_id"], ctx=_ctx(MATE, "TEAM-A"))
        assert st["counts"]["running"] == 1 and st["items"][0]["live"]["status"] in ("running", "queued")
        with pytest.raises(PermissionError):
            await m.deliberate_batch_status(out["batch_id"], ctx=_ctx(OUT, "TEAM-B"))
        with pytest.raises(ValueError):
            await m.deliberate_batch_start("묶음-엠", _items("가"), ctx=_ctx(""))
        stopped = await m.deliberate_batch_stop(out["batch_id"], ctx=_ctx(ME))
        assert stopped["status"] == "stopping"
        await _tick()

    _play(scenario)


def test_설명은_보수적인_기본과_끝남_표시를_말한다():
    tools = {t.name: t for t in asyncio.run(m.mcp.list_tools())}
    for n in ("deliberate_batch_start", "deliberate_batch_status", "deliberate_batch_stop"):
        assert n in tools
    start = tools["deliberate_batch_start"]
    assert start.inputSchema["properties"]["concurrency"]["default"] == 1
    assert "시간 초과" in start.inputSchema["properties"]["concurrency"]["description"]
    assert "finished" in start.description and "알리지 않는다" in start.description
    assert start.inputSchema["properties"]["stall_s"]["default"] == 0


def test_HTTP_창구(bt, monkeypatch):
    from fastapi.testclient import TestClient
    import app as a
    import seat_box
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    auth = {"Authorization": "Bearer gw-secret"}
    body = {"name": "묶음-에이치", "items": _items("가")}
    assert c.post("/deliberate/batch/start", json=body).status_code == 401
    r = c.post("/deliberate/batch/start", json=body, headers=auth)
    assert r.status_code == 422 and "신원" in r.json()["message"]
    r = c.post("/deliberate/batch/start", json={**body, "extra": 1}, headers={**auth, "x-hwax-user": ME})
    assert r.status_code == 422 and r.json()["code"] == "unknown_fields"
    b = _new(name="묶음-에이치")
    r = c.post("/deliberate/batch/status", json={"batch_id": b["id"]}, headers={**auth, "x-hwax-user": ME})
    assert r.status_code == 200 and r.json()["batch_id"] == b["id"]
    assert c.post("/deliberate/batch/status", json={"batch_id": b["id"]},
                  headers={**auth, "x-hwax-user": OUT, "x-hwax-affiliation": "TEAM-B"}).status_code == 403
    assert c.post("/deliberate/batch/status", json={"batch_id": "nope"}, headers=auth).status_code == 404
    assert c.post("/deliberate/batch/stop", json={"batch_id": b["id"]}, headers={**auth, "x-hwax-user": OUT}).status_code == 403
    r = c.post("/deliberate/batch/stop", json={"batch_id": b["id"]}, headers={**auth, "x-hwax-user": ME})
    assert r.status_code == 200 and r.json()["status"] == "stopped"


# ── 반대 검토 고침(M1 · M2 · M6 · M7 · L1 ~ L4 · L6) ───────────────────────────────────────────────────────────
def test_엔진이_받지_못하는_항목은_묶음을_만들기_전에_거절한다(bt):
    with pytest.raises(ValueError, match="엔진이 이 항목을 받지 못한다"):
        _new(items=[{"key": "A", "question": "q", "advanced": {"personas": 1}}, {"key": "B", "question": "r"}])
    assert B._BATCHES == {}
    assert not B._dir().exists() or not list(B._dir().glob("*.json")), "아무것도 남기지 않는다"


def test_띄우다_뜻밖의_오류는_실패한_시도이고_묶음을_막지_않는다(bt, monkeypatch):
    real = delib_jobs.start

    def boom(app, job_kind, question, **kw):
        if question == "가" and not kw.get("dry_run"):
            raise TypeError("'int' object is not iterable")
        return real(app, job_kind, question, **kw)
    monkeypatch.setattr(delib_jobs, "start", boom)

    async def scenario():
        out = await m.batch_start(name="묶음-오류", items=_items("가", "나"), concurrency=1, retries=0, stall_s=0, user=ME, groups=[], aff="")
        b = B.get(out["batch_id"])
        await asyncio.gather(*list(B._KICKS))
        await _tick()
        assert b["items"][0]["state"] == "failed" and "TypeError" in b["items"][0]["error"], b["items"][0]
        assert b["items"][1]["state"] == "running" and bt.started == ["나"], "다음 항목은 그대로 띄운다"

    _play(scenario)


def test_다_건너뛴_묶음은_원장에도_끝으로_남고_이름을_잠그지_않는다(bt):
    async def scenario():
        b1 = _new(items=_items("가"))
        await B.feed(b1)
        await _tick()
        await _end(bt, _job_of(b1, "K-1"))
        await B.feed(b1)
        b2 = _new(items=_items("가"))
        assert b2["status"] == "done" and _on_disk(b2)["status"] == "done" and B._index()[b2["id"]]["status"] == "done"
        b3 = _new(items=_items("가"))
        assert b3["status"] == "done", "같은 이름이 잠기지 않는다"

    _play(scenario)


def test_멈추면_엔진_줄에_서_있기만_한_잡은_빼고_도는_잡은_둔다(bt, monkeypatch):
    _caps(monkeypatch, 1, per_user=1)

    async def scenario():
        other = delib_jobs.start(m._APP, "default", "앞서 도는 내 심의", user_email=ME)
        await _tick()
        b = _new(items=_items("가", "나"), concurrency=1)
        _caps(monkeypatch, 1, per_user=1)
        await B.feed(b)
        it = b["items"][0]
        job = delib_jobs.get(it["job_id"])
        assert job["status"] == "queued" and job["id"] in delib_jobs._PENDING
        m.batch_stop(b["id"], user=ME)
        assert it["state"] == "stopped" and job["status"] == "cancelled" and job["id"] not in delib_jobs._PENDING
        assert b["status"] == "stopped" and other["status"] == "running", "멈춤은 내 묶음의 줄 선 잡만 뺀다"
        await _end(bt, other)
        assert "가" not in bt.started, "멈춘 뒤 줄 선 잡이 돌기 시작하지 않는다"

    _play(scenario)


def test_참조를_다_풀지_못한_항목은_띄우지_않고_실패한_시도로_센다(bt, monkeypatch):
    async def call(_app, *, groups, user, user_pat, alias, args):
        return True, "forbidden: 권한이 없다"
    monkeypatch.setattr(delib_refs, "call_alias", call)

    async def scenario():
        b = _new(items=[{"key": "K-1", "question": "가", "evidence": [{"ref": {"kind": "risk_bundle", "id": "BND-1"}}]}], retries=1)
        await B.feed(b)
        assert b["items"][0]["tries"] == 1 and b["items"][0]["state"] == "pending", "한 틱에 시도 하나"
        await B.feed(b)
        assert b["items"][0]["tries"] == 1, "물러서는 동안은 다시 띄우지 않는다"
        _elapse(b)
        await B.feed(b)
        (it,) = b["items"]
        assert it["state"] == "failed" and it["tries"] == 2 and bt.started == [], "근거 없이 돌지 않는다"
        assert all(a["status"] == "failed" and "근거 참조를 다 풀지 못했다" in a["why"] for a in it["attempts"])

    _play(scenario)


def test_멈춘_묶음의_띄우는_중_항목은_멈춤으로_닫힌다(bt):
    b = _new(items=_items("가"))
    b["items"][0].update(state="starting", token="tok-gone")
    b["status"] = "stopping"
    B._reconcile(b)
    B._finish_if_done(b)
    assert b["items"][0]["state"] == "stopped" and b["status"] == "stopped"


def test_멈춤_줄_참이면_참조를_풀지_않고_기다린다(bt, monkeypatch):
    calls = []

    async def call(*a, **kw):
        calls.append(1)
        return False, "{}"
    monkeypatch.setattr(delib_refs, "call_alias", call)
    monkeypatch.setattr(delib_jobs, "pause_info", lambda: {"on": True, "retry_after_s": 120})

    async def scenario():
        b = _new(items=[{"key": "K-1", "question": "가", "evidence": [{"ref": {"kind": "risk_bundle", "id": "BND-1"}}]}])
        for _ in range(3):
            await B.feed(b)
        assert calls == [] and b["items"][0]["state"] == "pending" and b["items"][0]["tries"] == 0

    _play(scenario)


def test_끝난_key_는_같은_이름의_앞_묶음_전부에서_모은다(bt):
    async def scenario():
        b1 = _new(items=_items("가", "나"))
        await B.feed(b1)
        await _tick()
        await _end(bt, _job_of(b1, "K-1"))
        m.batch_stop(b1["id"], user=ME)          # K-2 가 뜨기 전에 멈춘다
        await B.feed(b1)
        assert b1["status"] == "stopped" and _state(b1) == ["done", "stopped"]
        b2 = _new(items=_items("가", "나"))
        await B.feed(b2)
        await _tick()
        await _end(bt, _job_of(b2, "K-2"))
        await B.feed(b2)
        assert b2["status"] == "done"
        b3 = _new(items=_items("가", "나", "다"))
        assert _state(b3) == ["skipped", "skipped", "pending"], "K-1 은 첫 묶음 · K-2 는 둘째 묶음에서 끝났다"
        assert b3["items"][0]["done_in"]["batch_id"] == b1["id"] and b3["items"][1]["done_in"]["batch_id"] == b2["id"]

    _play(scenario)


def test_묶음은_관리자_표지를_적지_않고_주인이_부를_때_그룹을_고친다(bt):
    b = B.create(m._APP, name="묶음-그룹", items=_items("가"), user=ME, groups=["CAEG", "portal-admin"], owner_aff="TEAM-A")
    assert b["groups"] == ["CAEG"] and B.ADMIN_GROUP == m._ADMIN_GROUP
    m.batch_read(b["id"], user=MATE, aff="TEAM-A", admin=False, groups=["OTHER"])
    assert b["groups"] == ["CAEG"], "주인이 아니면 고치지 않는다"
    m.batch_read(b["id"], user=ME, aff="TEAM-A", admin=False, groups=["TEAM-ONLY", "portal-admin"])
    assert b["groups"] == ["TEAM-ONLY"] and _on_disk(b)["groups"] == ["TEAM-ONLY"]


def test_묶음_id_꼴이_아니면_파일을_보지_않는다(bt):
    for bad in ("../x", "batch-1", "batch-20261117-000000-ZZZZZZ", ""):
        assert B.get(bad) is None
        with pytest.raises(ValueError):
            B.stop(bad, by=ME)


def test_보존_기한이_지난_끝난_묶음은_지운다_만들기는_원장을_다시_읽지_않는다(bt, monkeypatch):
    old = _new(name="묶음-옛")
    m.batch_stop(old["id"], user=ME)
    assert old["status"] == "stopped"
    old["finished_at"] = delib_jobs._now() - B.KEEP_S - 10
    B._persist(old)
    loads = []
    real = B._load
    monkeypatch.setattr(B, "_load", lambda bid: loads.append(bid) or real(bid))
    _new(name="묶음-새")
    assert loads == [], "만들기는 메모리 색인만 본다"
    B.bind(m._APP)
    assert not B._path(old["id"]).exists() and old["id"] not in B._index()


def test_본문_근거_합이_상한을_넘으면_받지_않는다(bt, monkeypatch):
    monkeypatch.setattr(B, "INLINE_MAX", 300)
    with pytest.raises(ValueError, match="참조"):
        _new(items=[{"key": "A", "question": "q", "evidence": [{"source": "s", "result": "x" * 400}]}])
    with pytest.raises(ValueError, match="참조"):        # 본문 칸 하나가 아니라 항목 전부(JSON 길이)를 센다(2차 검토 6)
        _new(items=[{"key": "A", "question": "q", "evidence": [{"source": "s", "result": "x", "text": "y" * 200, "args": "z" * 200}]}])
    with pytest.raises(ValueError, match="참조"):
        _new(items=[{"key": "A", "question": "q", "advanced": {"knowledge_query": "w" * 400}}])
    _new(name="묶음-참조", items=[{"key": "A", "question": "q", "evidence": [{"ref": {"kind": "risk_bundle", "id": "B-1"}}]}])


# ── 끝난 묶음 창구(HWAXPortal docs/request-1117 contracts §7 — 포털 알림) ───────────────────────────────────────
def _finished(name: str, at: float, items=None) -> dict:
    """끝난 묶음 하나 — 멈춰 닫고(stopped) 끝난 시각을 at 으로 박아 원장에 쓴다."""
    b = _new(name=name, items=items or _items("가"))
    m.batch_stop(b["id"], user=ME)
    assert b["status"] == "stopped"
    b["finished_at"] = at
    B._persist(b)
    return b


def test_끝난_묶음은_끝난_시각과_수_알림을_원장에_남긴다(bt):
    async def scenario():
        b = _new(items=_items("가"))
        await B.feed(b)
        await _tick()
        await _end(bt, _job_of(b, "K-1"))
        await B.feed(b)
        disk = _on_disk(b)
        assert disk["status"] == "done" and isinstance(disk["finished_at"], float) and disk["notice"].startswith("묶음 「묶음-가」")
        (row,) = B.finished_since(0)["batches"]
        assert row == {"batch_id": b["id"], "owner": ME, "name": "묶음-가", "status": "done", "finished_at": disk["finished_at"],
                       "counts": {"done": 1, "failed": 0, "stopped": 0, "skipped": 0, "not_trustworthy": 0}, "notice": disk["notice"]}

    _play(scenario)


def test_끝난_항목_없이_실패만_있으면_failed(bt):
    async def scenario():
        b = _new(items=_items("가"), retries=0)
        bt.fail.add("가")
        await B.feed(b)
        await _tick()
        await _end(bt, _job_of(b, "K-1"), fail=True)
        await B.feed(b)
        assert b["status"] == "failed" and "모두 실패" in b["notice"] and B.view(b)["finished"] is True
        (row,) = B.finished_since(0)["batches"]
        assert row["status"] == "failed" and row["counts"]["failed"] == 1

    _play(scenario)


def test_창구는_끝난_시각_차례로_쪽을_나누고_같은_since_는_같은_답이다(bt):
    t0 = float(int(delib_jobs._now()) - 10_000)      # 보존 기한(30일) 안의 시각
    a, b, c = _finished("묶음-1", t0 + 1), _finished("묶음-2", t0 + 2), _finished("묶음-3", t0 + 3)
    _new(name="묶음-도는")                           # 도는 묶음은 싣지 않는다
    p1 = B.finished_since(0, 2)
    assert [x["batch_id"] for x in p1["batches"]] == [a["id"], b["id"]] and p1["next_since"] == t0 + 2
    assert B.finished_since(0, 2) == p1, "같은 since 는 같은 답"
    p2 = B.finished_since(p1["next_since"], 2)
    assert [x["batch_id"] for x in p2["batches"]] == [b["id"], c["id"]], "경계의 묶음은 다시 나온다(포털이 batch_id 로 한 번만)"
    assert B.finished_since(t0 + 3.5) == {"batches": [], "next_since": t0 + 3.5}
    assert B.finished_since(0, 10 ** 6)["batches"][-1]["batch_id"] == c["id"] and len(B.finished_since(0, 10 ** 6)["batches"]) == 3


def test_창구는_재기동을_넘기고_보존이_지난_묶음은_사라진다(bt):
    old = _finished("묶음-옛", delib_jobs._now() - B.KEEP_S - 60)
    keep = _finished("묶음-남음", delib_jobs._now() - 60)
    B._BATCHES.clear()
    B._INDEX = None                                   # 재기동 — 원장 폴더에서 다시 읽는다
    rows = B.finished_since(0)["batches"]
    assert [x["batch_id"] for x in rows] == [keep["id"]] and rows[0]["finished_at"] == keep["finished_at"]
    assert not B._path(old["id"]).exists(), "보존 기한이 지난 끝난 묶음은 지웠다"


def test_HTTP_끝난_묶음_창구는_공유_비밀만_받는다(bt, monkeypatch):
    from fastapi.testclient import TestClient
    import app as a
    import seat_box
    at = float(int(delib_jobs._now()) - 100) + 0.5
    b = _finished("묶음-창구", at)
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    url, auth = "/deliberate/batch/finished", {"Authorization": "Bearer gw-secret"}
    assert c.post(url, json={"since": 0}).status_code == 401, "신원 머리만으로는 받지 않는다"
    assert c.post(url, json={"since": 0}, headers={"x-hwax-user": ME}).status_code == 401
    assert c.post(url, json={"since": 0}, headers={"Authorization": "Bearer pat-EXAMPLE-person"}).status_code == 401, "사람 PAT 는 받지 않는다"
    r = c.post(url, json={"since": 0}, headers={**auth, "x-hwax-user": ME})
    assert r.status_code == 403 and r.json()["code"] == "service_only"
    r = c.post(url, json={"since": 0, "limit": 5}, headers=auth)
    assert r.status_code == 200 and r.json() == {"batches": [{
        "batch_id": b["id"], "owner": ME, "name": "묶음-창구", "status": "stopped", "finished_at": at,
        "counts": {"done": 0, "failed": 0, "stopped": 1, "skipped": 0, "not_trustworthy": 0}, "notice": b["notice"]}], "next_since": at}
    for bad in ({"since": -1}, {"since": "0"}, {"since": 0, "limit": 0}, {"since": 0, "limit": 501}, {"since": 0, "x": 1}, {}):
        assert c.post(url, json=bad, headers=auth).status_code == 422, bad
    monkeypatch.setattr(seat_box, "secret", lambda _app: "")
    assert c.post(url, json={"since": 0}, headers=auth).status_code == 503


# ── 2차 검토(1 · 5 · 7 · 이음새) ──────────────────────────────────────────────────────────────────────────────────
def test_한_틱에_같은_항목의_시도를_둘_쓰지_않는다(bt, monkeypatch):
    calls = []

    async def call(_app, *, groups, user, user_pat, alias, args):
        calls.append(args["job_id"])
        return True, "도구 오류: 일시 오류"
    monkeypatch.setattr(delib_refs, "call_alias", call)

    async def scenario():
        b = _new(items=[{"key": f"K-{i}", "question": f"q{i}", "evidence": [{"ref": {"kind": "stepforge_job", "id": f"j{i}"}}]}
                        for i in range(5)], retries=3)
        await B.feed(b)                       # 틱 하나
        assert sorted(calls) == [f"j{i}" for i in range(5)], "항목마다 한 번만 불렀다"
        assert [it["tries"] for it in b["items"]] == [1] * 5 and all(it["state"] == "pending" for it in b["items"])
        assert all(it["retry_at"] > delib_jobs._now() + B.RETRY_BACKOFF_S - 5 for it in b["items"]), "물러선다"
        assert b["status"] == "running"
        await B.feed(b)
        assert len(calls) == 5, "물러서는 동안 다시 부르지 않는다"

    _play(scenario)


def test_게이트웨이_불통은_시도가_아니라_물러서기다(bt, monkeypatch):
    async def call(*_a, **_kw):
        raise ConnectionError("게이트웨이 내려감")
    monkeypatch.setattr(delib_refs, "call_alias", call)
    monkeypatch.setattr(B, "UNREACHABLE_WAITS_MAX", 2)

    async def scenario():
        b = _new(items=[{"key": "K-1", "question": "q", "evidence": [{"ref": {"kind": "stepforge_job", "id": "j1"}}]}], retries=0)
        await B.feed(b)
        (it,) = b["items"]
        assert (it["state"], it["tries"], it["waits"]) == ("pending", 0, 1) and "불통" in it["last_error"] and not it["attempts"]
        _elapse(b)
        await B.feed(b)
        assert (it["tries"], it["waits"]) == (0, 2)
        _elapse(b)
        await B.feed(b)                       # 기다림 상한을 넘었다 — 실패한 시도(retries 0 이라 끝)
        assert it["state"] == "failed" and it["tries"] == 1 and b["status"] == "failed"

    _play(scenario)


def test_남이_줄을_채워도_상한에_안_걸린_사람의_묶음은_간다(bt, monkeypatch):
    _caps(monkeypatch, 3, per_user=1, queue=1)

    async def scenario():
        a1 = delib_jobs.start(m._APP, "default", "남 1", user_email=OUT)
        a2 = delib_jobs.start(m._APP, "default", "남 2", user_email=OUT)       # 남의 사람별 상한 — 줄에 선다(줄이 찬다)
        await _tick()
        assert a1["status"] == "running" and a2["status"] == "queued" and len(delib_jobs._PENDING) == 1
        assert delib_jobs.accepts(ME) is True and delib_jobs.accepts(OUT) is False, "같은 판정 — 이 사람은 상한에 안 걸렸다"
        b = _new(items=_items("가"))
        await B.feed(b)
        await _tick()
        assert b["items"][0]["state"] == "running" and _job_of(b, "K-1")["status"] == "running"

    _play(scenario)


def test_빈_본문_칸의_참조도_묶음에서_다_풀려야_띄운다(bt, monkeypatch):
    async def call(*_a, **_kw):
        return True, "forbidden: 권한이 없다"
    monkeypatch.setattr(delib_refs, "call_alias", call)

    async def scenario():
        b = _new(items=[{"key": "K-1", "question": "가", "evidence": [{"ref": {"kind": "stepforge_job", "id": "j1"}, "result": None}]}],
                 retries=0)
        await B.feed(b)
        assert b["items"][0]["state"] == "failed" and bt.started == [], "빈 본문 칸이 있어도 참조다(2차 검토 7)"

    _play(scenario)


def test_멈춤도_주인의_그룹을_고친다(bt):
    b = B.create(m._APP, name="묶음-멈춤그룹", items=_items("가"), user=ME, groups=["CAEG"], owner_aff="TEAM-A")
    m.batch_stop(b["id"], user=ME, groups=["TEAM-ONLY", "portal-admin"])
    assert _on_disk(b)["groups"] == ["TEAM-ONLY"]


def test_보존_정리는_틱마다_하루_간격으로_돈다(bt):
    old = _finished("묶음-하루", delib_jobs._now() - B.KEEP_S - 60)
    B._LAST_PRUNE = delib_jobs._now()
    asyncio.run(B.tick())
    assert B._path(old["id"]).exists(), "하루가 안 지났으면 지우지 않는다"
    B._LAST_PRUNE = delib_jobs._now() - B.PRUNE_EVERY_S - 1
    asyncio.run(B.tick())
    assert not B._path(old["id"]).exists() and old["id"] not in B._index()


def test_HTTP_끝난_묶음_창구는_무한대_since_를_받지_않는다(bt, monkeypatch):
    from fastapi.testclient import TestClient
    import app as a
    import seat_box
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    for raw in (b'{"since": Infinity}', b'{"since": NaN}'):
        r = c.post("/deliberate/batch/finished", content=raw, headers={"Authorization": "Bearer gw-secret", "content-type": "application/json"})
        assert r.status_code == 422, raw

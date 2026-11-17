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
        await B.feed(b)                      # 실패 → 다시 띄운다(문이 이미 열려 곧바로 또 실패한다)
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
        await B.feed(b)                       # 실패한 시도로 세고 다시 띄운다
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
        await _tick()
        (it,) = b["items"]
        assert it["attempts"][1]["status"] == "interrupted" and "제자리로 잇지 못했다" in it["attempts"][1]["why"]
        assert it["state"] == "running" and it["tries"] == 2

    _play(scenario)


# ── MCP · HTTP ──────────────────────────────────────────────────────────────────────────────────────────────
def test_MCP_시작_진행_멈춤(bt):
    async def scenario():
        out = await m.deliberate_batch_start("묶음-엠", _items("가", "나"), ctx=_ctx(ME, "TEAM-A", "CAEG"))
        assert out["status"] == "running" and out["in_engine"] == [{"key": "K-1", "job_id": out["in_engine"][0]["job_id"]}]
        assert "items" not in out and "batch_id" in out and "deliberate_batch_status" in out["note"]
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

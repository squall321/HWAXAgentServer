# 근거 참조(DL-01) — evidence 의 {ref: {kind, id}} 를 그 사람 신원으로 풀어 그 자리에 펼치고, 지문 대조 · 빠진 까닭 · 잡 원장 기록 · 이어하기 · 미리 보기를 본다
"""HWAXPortal docs/request-1117 contracts §1 · §2 (DL-01).

  · risk_bundle → 게이트웨이 별칭 heaxhwax_risk_risk_get_evidence_bundle(bundle_id=id) 를 부른 사람의 신원 · 그룹으로(새 토큰 없음). 답의 evidence 를
    그 자리에 차례대로 펼친다. 지문은 sha256(정규 JSON) — 다르면 싣지 않고 evidence_omitted 에 까닭.
  · 권한 · 없음 · 불통 · 꼴 다름은 그 항목만 빼고 까닭을 남긴다(심의는 연다). 꼴이 틀린 참조 · 열지 않은 kind 는 잡을 만들기 전에 거절한다.
  · 잡 원장 evidence_refs(본문 없음) · 시작 인자 파일에는 펼친 근거. 끝난 잡을 근거 없이 이으면 앞 잡의 참조를 다시 풀고 지문이 같은지 적는다.
  · deliberate_resolve_evidence · POST /deliberate/evidence/resolve 는 크기 · 지문만(잡 없음).

가짜는 게이트웨이 한 번 부르기(delib_refs.call_alias)와 엔진(test_delib_job_queue 의 문 달린 생성기)뿐이다.

  실행:  .venv/bin/python -m pytest tests/test_dl01_evidence_refs.py -q
"""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import delib_refs as r  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import _caps, _end, _play, _tick, eng  # noqa: E402, F401
from test_delib_same_aff import _ctx  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

ME = "me@example.com"
ALIAS = "heaxhwax_risk_risk_get_evidence_bundle"
_INDEX = {"source": "묶음 BND-A 색인", "tool": "risk_build_evidence_bundle", "args": "", "key": "IDX",
          "result": "행 코드: R-001 · R-002\n이 목록 밖의 행은 이 근거에 싣지 않았다(없다는 뜻이 아니다)"}
_ROW1 = {"source": "BND-A · R-001", "tool": "risk_build_evidence_bundle", "args": "", "key": "R-001", "result": "판정 칸 값 12.5"}
_ROW2 = {"source": "BND-A · R-002", "tool": "risk_build_evidence_bundle", "args": "", "key": "R-002", "result": "판정 칸 값 7"}
_BUNDLE_EV = [_INDEX, _ROW1, _ROW2]


def _fp(ev) -> str:
    return hashlib.sha256(json.dumps(ev, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _bundle(bid="BND-A", ev=None, fp=None) -> dict:
    ev = _BUNDLE_EV if ev is None else ev
    return {"bundle_id": bid, "target_key": "diff:합성", "version": 3, "fingerprint": fp or _fp(ev), "evidence": ev}


class _GW:
    """게이트웨이 한 번 부르기 가짜 — 묶음 id 마다 답(글) · 도구 오류 · 예외 · 늦음을 정한다. 받은 자격 · 별칭 · 인자를 적는다."""

    def __init__(self):
        self.answers, self.calls = {}, []

    async def __call__(self, _app, *, groups, user, user_pat, alias, args):
        self.calls.append({"groups": list(groups), "user": user, "user_pat": user_pat, "alias": alias, "args": dict(args)})
        rid = args.get("bundle_id") or args.get("file_id") or args.get("job_id")
        a = self.answers.get(rid, (False, json.dumps({"error": "E404", "message": "그런 묶음이 없다"})))
        if isinstance(a, BaseException):
            raise a
        if a == "slow":
            await asyncio.sleep(5)
        return a


@pytest.fixture
def gw(monkeypatch):
    g = _GW()
    g.answers["BND-A"] = (False, json.dumps(_bundle(), ensure_ascii=False))
    monkeypatch.setattr(r, "call_alias", g)
    return g


def _ev_inline(key="IN-1", text="사람이 붙인 원천 0.42"):
    return {"source": "호출자", "tool": "manual", "args": "", "result": text, "key": key}


# ── 꼴 ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_지문은_계약의_식이다():
    assert r.fingerprint(_BUNDLE_EV) == _fp(_BUNDLE_EV)
    assert r.fingerprint([{"b": 1, "a": "한"}]) == hashlib.sha256('[{"a":"한","b":1}]'.encode()).hexdigest()


@pytest.mark.parametrize("item,msg", [
    ({"ref": {"kind": "dynaforge_report", "id": "D1"}}, "DF-01"),
    ({"ref": {"kind": "nope", "id": "X"}}, "가운데 하나다"),
    ({"ref": {"kind": "risk_bundle"}}, "{kind, id}"),
    ({"ref": {"kind": "risk_bundle", "id": "a b"}}, "ref.id"),
    ({"ref": {"kind": "risk_bundle", "id": "B"}, "result": "본문"}, "다른 칸"),
    ({"ref": "risk_bundle:B"}, "{kind, id}"),
])
def test_꼴이_틀린_참조와_열지_않은_kind_는_거절한다(item, msg):
    with pytest.raises(r.RefError) as e:
        r.parse([_ev_inline(), item])
    assert msg in str(e.value)


def test_참조가_없으면_종전_길이다():
    assert r.parse([_ev_inline()]) is None and r.parse(None) is None and not r.has_refs([_ev_inline()])


# ── 풀이 ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_풀린_묶음은_그_자리에_차례대로_펼친다(gw):
    ev = [_ev_inline("IN-1"), {"ref": {"kind": "risk_bundle", "id": "BND-A"}}, _ev_inline("IN-2", "뒤 원천")]
    res = asyncio.run(r.resolve(object(), ev, groups=["CAEG"], user=ME))
    assert res["evidence"] == [ev[0], *_BUNDLE_EV, ev[2]]
    (rec,) = res["refs"]
    assert rec["ok"] is True and rec["items"] == 3 and rec["fingerprint"] == rec["bundle_fingerprint"] == _fp(_BUNDLE_EV)
    assert rec["chars"] == sum(len(x["result"]) for x in _BUNDLE_EV) and rec["version"] == 3 and rec["target_key"] == "diff:합성"
    assert res["omitted"] == []
    (call,) = gw.calls
    assert call == {"groups": ["CAEG"], "user": ME, "user_pat": "", "alias": ALIAS, "args": {"bundle_id": "BND-A"}}, "그 사람 신원 · 새 토큰 없음"


@pytest.mark.parametrize("answer,why", [
    ((False, json.dumps(_bundle(fp="0" * 64))), "지문이 다르다"),
    ((False, json.dumps({"error": "E404", "message": "없다"})), "리스크 앱이 거절했다"),
    ((True, "forbidden: 이 앱을 쓸 권한이 없다"), "권한이 없다"),
    ((True, "unknown tool: heaxhwax_risk_risk_get_evidence_bundle"), "도구가 없다"),
    ((False, "<html>"), "JSON"),
    ((False, json.dumps({"bundle_id": "BND-A", "fingerprint": "x", "evidence": "글"})), "답 꼴이 다르다"),
    ((False, json.dumps(_bundle(bid="BND-Z"))), "다른 묶음이 왔다"),
    (ConnectionError("끊김"), "불통"),
])
def test_못_푼_참조는_빼고_까닭을_남긴다(gw, answer, why):
    gw.answers["BND-A"] = answer
    ev = [{"ref": {"kind": "risk_bundle", "id": "BND-A"}}, _ev_inline()]
    res = asyncio.run(r.resolve(object(), ev, groups=[], user=ME))
    assert res["evidence"] == [ev[1]], "그 항목만 빠진다"
    (rec,) = res["refs"]
    assert rec["ok"] is False and why in rec["why"], rec
    (row,) = res["omitted"]
    assert row["source"] == r.OMIT_SOURCE and row["text"].startswith("ref:risk_bundle:BND-A — ") and why in row["text"]


def test_실린_참조마다_인용을_단다_판은_답이_말할_때만(gw):
    one = [{"ref": {"kind": "risk_bundle", "id": "BND-A"}}]
    (rec,) = asyncio.run(r.resolve(object(), one, groups=[], user=ME))["refs"]
    assert rec["cite"] == {"source_platform": "hwax_risk", "source_kind": "risk_bundle", "source_id": "BND-A", "source_version": None,
                           "scope": "diff:합성"}, "묶음 답에 cite 가 없으면 판은 null · 자리는 target_key"
    gw.answers["BND-A"] = (False, json.dumps({**_bundle(), "cite": {"source_version": "rev-7", "scope": "proj:합성"}}, ensure_ascii=False))
    (rec,) = asyncio.run(r.resolve(object(), one, groups=[], user=ME))["refs"]
    assert rec["cite"]["source_version"] == "rev-7" and rec["cite"]["scope"] == "proj:합성"
    gw.answers["BND-A"] = (False, json.dumps(_bundle(fp="0" * 64)))
    (rec,) = asyncio.run(r.resolve(object(), one, groups=[], user=ME))["refs"]
    assert "cite" not in rec, "못 푼 참조에는 인용이 없다"


# ── reportarchive_file — Report Archive download_file(file_id) ──────────────────────────────────────────────
_RA_ID = "0a1b2c3d-0000-4000-8000-00000000abcd"


def _ra(data: bytes, mime="text/markdown", name="note.md", **kw) -> tuple:
    import base64
    return False, json.dumps({"file_id": _RA_ID, "filename": name, "mime_type": mime, "size": len(data), "is_image": False,
                              "encoding": "base64", "data_base64": base64.b64encode(data).decode("ascii"), **kw}, ensure_ascii=False)


def test_RA_글_파일은_한_항목으로_싣는다(gw):
    gw.answers[_RA_ID] = _ra("# 합성 메모\n값 3.5".encode("utf-8"))
    res = asyncio.run(r.resolve(object(), [{"ref": {"kind": "reportarchive_file", "id": _RA_ID}}], groups=["CAEG"], user=ME))
    (item,) = res["evidence"]
    assert item == {"source": "Report Archive 파일 · note.md", "tool": "download_file", "args": json.dumps({"file_id": _RA_ID}),
                    "result": "# 합성 메모\n값 3.5"}
    (rec,) = res["refs"]
    assert rec["ok"] and rec["items"] == 1 and rec["chars"] == len(item["result"]) and rec["fingerprint"] == _fp([item])
    assert rec["cite"] == {"source_platform": "reportarchive", "source_kind": "reportarchive_file", "source_id": _RA_ID,
                           "source_version": None, "scope": None}
    assert gw.calls[-1]["alias"] == "reportarchive_download_file" and gw.calls[-1]["args"] == {"file_id": _RA_ID}


@pytest.mark.parametrize("answer,why", [
    (_ra(b"\x89PNG", mime="image/png", name="fig.png", is_image=True), "글이 아닌 파일"),
    (_ra(b"\xff\xfe\x00", mime="text/plain", name="x.txt"), "UTF-8"),
    ((False, json.dumps({"file_id": _RA_ID, "filename": "big.md", "mime_type": "text/markdown", "size": 9, "error": "파일이 너무 큽니다"})),
     "Report Archive 가 거절했다"),
    ((False, json.dumps({"error": "없는 파일"})), "Report Archive 가 거절했다"),
])
def test_RA_글이_아니거나_거절된_파일은_뺀다(gw, answer, why):
    gw.answers[_RA_ID] = answer
    res = asyncio.run(r.resolve(object(), [{"ref": {"kind": "reportarchive_file", "id": _RA_ID}}], groups=[], user=ME))
    assert res["evidence"] == [] and why in res["refs"][0]["why"], res["refs"]


# ── stepforge_job — StepForge job_status(job_id, full=true) ───────────────────────────────────────────────
_SF_ID = "0190a1b2c3d4e5f60718293"


def _sf(status="done", **kw) -> tuple:
    body = {"id": _SF_ID, "project_id": "p-합성", "kind": "measure", "status": status, "params": {"part": "부품-가"},
            "result": {"gap_mm": 0.12, "job": {"geo_key": "g-1234"}}, "log_path": "/서버/경로", "runner": {"build": "x"}, **kw}
    return False, json.dumps(body, ensure_ascii=False)


def test_StepForge_끝난_잡은_결과를_한_항목으로_싣고_판은_형상_키다(gw):
    gw.answers[_SF_ID] = _sf(result_stale=False)
    res = asyncio.run(r.resolve(object(), [{"ref": {"kind": "stepforge_job", "id": _SF_ID}}], groups=[], user=ME))
    (item,) = res["evidence"]
    body = json.loads(item["result"])
    assert body["result"] == {"gap_mm": 0.12, "job": {"geo_key": "g-1234"}} and body["result_stale"] is False and body["status"] == "done"
    assert "log_path" not in body and "runner" not in body, "서버 경로 · 러너 표식은 싣지 않는다"
    assert item["tool"] == "job_status" and json.loads(item["args"]) == {"job_id": _SF_ID, "full": True}
    (rec,) = res["refs"]
    assert rec["cite"] == {"source_platform": "step_forge", "source_kind": "stepforge_job", "source_id": _SF_ID,
                           "source_version": "g-1234", "scope": "p-합성"}
    assert gw.calls[-1]["alias"] == "heaxstep_forge_job_status" and gw.calls[-1]["args"] == {"job_id": _SF_ID, "full": True}


@pytest.mark.parametrize("answer,why", [
    (_sf(status="running"), "끝나지 않았다"),
    ((True, f"Error executing tool job_status: 잡 를 찾을 수 없다: {_SF_ID}"), "없다"),
    (_sf(id="other"), "다른 잡이 왔다"),
])
def test_StepForge_끝나지_않았거나_없는_잡은_뺀다(gw, answer, why):
    gw.answers[_SF_ID] = answer
    res = asyncio.run(r.resolve(object(), [{"ref": {"kind": "stepforge_job", "id": _SF_ID}}], groups=[], user=ME))
    assert res["evidence"] == [] and why in res["refs"][0]["why"], res["refs"]


def test_늦은_참조는_불통으로_뺀다(gw, monkeypatch):
    monkeypatch.setattr(r, "RESOLVE_TIMEOUT_S", 0.05)
    gw.answers["BND-A"] = "slow"
    res = asyncio.run(r.resolve(object(), [{"ref": {"kind": "risk_bundle", "id": "BND-A"}}], groups=[], user=ME))
    assert res["evidence"] == [] and "불통" in res["refs"][0]["why"]


# ── 심의 시작 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_시작은_펼친_근거로_돌고_원장에_풀이_기록을_남긴다(eng, gw, monkeypatch):
    _caps(monkeypatch, 2)
    ev = [_ev_inline("IN-1"), {"ref": {"kind": "risk_bundle", "id": "BND-A"}}, {"ref": {"kind": "risk_bundle", "id": "BND-GONE"}}]

    async def scenario():
        out = await m.deliberate_start("참조 화두", job="risk-review", evidence=ev, ctx=_ctx(ME, "TEAM-A", "CAEG"))
        await _tick()
        job = delib_jobs.get(out["job_id"])
        seen = eng.seen["참조 화두"].opts["evidence"]
        assert seen == [ev[0], *_BUNDLE_EV], "엔진은 펼친 근거를 받는다(못 푼 것은 빠진다)"
        assert [x["ok"] for x in job["evidence_refs"]] == [True, False] and "evidence" not in json.dumps(job["evidence_refs"])
        assert job["evidence_omitted"][0]["text"].startswith("ref:risk_bundle:BND-GONE — ")
        assert job["opts"]["evidence"] == 4, "원장의 손잡이 메아리는 펼친 건수"
        args = json.loads(delib_jobs._args_path(job["id"]).read_text(encoding="utf-8"))
        assert args["opts"]["evidence"] == seen, "시작 인자 파일은 펼친 근거 — 재기동 · 끊긴 잡의 이어하기가 같은 근거로 돈다"
        assert out["evidence_refs"] == job["evidence_refs"] and "근거 참조 2건" in out["note"] and "빠짐 1건" in out["note"]
        assert all(c["user"] == ME and c["groups"] == ["CAEG"] for c in gw.calls)
        await _end(eng, job)

    _play(scenario)


def test_참조가_없는_시작은_원장이_종전_그대로다(eng, gw, monkeypatch):
    _caps(monkeypatch, 2)

    async def scenario():
        out = await m.deliberate_start("본문만", evidence=[_ev_inline()], ctx=_ctx(ME))
        job = delib_jobs.get(out["job_id"])
        assert "evidence_refs" not in job and "evidence_refs" not in out and job["evidence_omitted"] == []
        assert gw.calls == []
        await _tick()
        await _end(eng, job)

    _play(scenario)


def test_열지_않은_kind_는_잡을_만들기_전에_거절한다(eng, gw, monkeypatch):
    _caps(monkeypatch, 2)
    with pytest.raises(ValueError, match="kind not opened yet"):
        asyncio.run(m.deliberate_start("거절", evidence=[{"ref": {"kind": "dynaforge_report", "id": "D1"}}], ctx=_ctx(ME)))
    assert delib_jobs._JOBS == {} and gw.calls == []


def test_멈춘_서버는_게이트웨이를_부르기_전에_거절한다(eng, gw, monkeypatch):
    monkeypatch.setattr(delib_jobs, "pause_info", lambda: {"on": True, "retry_after_s": 120, "reason": "점검"})
    with pytest.raises(delib_jobs.DelibPaused):
        asyncio.run(m.deliberate_start("멈춤", evidence=[{"ref": {"kind": "risk_bundle", "id": "BND-A"}}], ctx=_ctx(ME)))
    assert gw.calls == []


# ── 이어하기 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_끝난_잡을_근거_없이_이으면_앞_참조를_다시_풀고_지문을_견준다(eng, gw, monkeypatch):
    _caps(monkeypatch, 2)

    async def scenario():
        first = await m.deliberate_start("앞 회차", evidence=[{"ref": {"kind": "risk_bundle", "id": "BND-A"}}], ctx=_ctx(ME))
        await _tick()
        a = delib_jobs.get(first["job_id"])
        await _end(eng, a)
        assert a["status"] == "done" and a["decision"]
        again = await m.deliberate_continue(a["id"], human_note="사람 의견", ctx=_ctx(ME))
        b = delib_jobs.get(again["job_id"])
        args = json.loads(delib_jobs._args_path(b["id"]).read_text(encoding="utf-8"))
        assert args["opts"]["evidence"] == _BUNDLE_EV, "이어하기가 앞 회차의 근거 본문을 다시 싣는다"
        await _tick()           # 같은 화두의 문은 이미 열려 있다 — 이 회차는 곧바로 끝난다
        assert eng.seen["앞 회차"].opts["evidence"] == _BUNDLE_EV, "이어 돈 회차의 엔진도 펼친 근거를 받는다"
        (rec,) = b["evidence_refs"]
        assert rec["ok"] and rec["previous"] == {"job_id": a["id"], "fingerprint": _fp(_BUNDLE_EV), "same": True}
        assert "앞 잡의 근거 참조를 다시 풀어 실었다" in again["note"]
        # 묶음이 바뀐 뒤 같은 참조를 주면 지문이 다르다고 적는다
        changed = [*_BUNDLE_EV, {**_ROW1, "key": "R-003", "result": "새 행"}]
        gw.answers["BND-A"] = (False, json.dumps(_bundle(ev=changed), ensure_ascii=False))
        third = await m.deliberate_continue(a["id"], evidence=[{"ref": {"kind": "risk_bundle", "id": "BND-A"}}], ctx=_ctx(ME))
        c = delib_jobs.get(third["job_id"])
        assert c["evidence_refs"][0]["previous"]["same"] is False and "지문이 다른 참조 1건" in third["note"]
        await _tick()

    _play(scenario)


def test_끊긴_잡에_참조를_주면_요약_길로_새_잡에_싣는다(eng, gw, monkeypatch):
    _caps(monkeypatch, 2)
    now = delib_jobs._now()
    prev = {"id": "default-20261117-010101-cccccc", "job": "default", "kind": "default", "label": "자유 심의", "status": "interrupted",
            "question": "끊긴 화두", "chair_template": None, "opts": {}, "stage": "r2", "round": 2, "total_rounds": 3, "seats": [],
            "turns": [], "decision": None, "error": delib_jobs.INTERRUPTED_MSG, "warnings": [], "evidence_omitted": [],
            "started_at": now - 100, "updated_at": now - 50, "finished_at": now - 50, "user": ME, "owner_aff": ""}
    delib_jobs._JOBS[prev["id"]] = prev

    async def scenario():
        out = await m.deliberate_continue(prev["id"], evidence=[{"ref": {"kind": "risk_bundle", "id": "BND-A"}}], ctx=_ctx(ME))
        assert out["resumed"]["mode"] == "summary" and "evidence" in out["resumed"]["why"]
        job = delib_jobs.get(out["job_id"])
        assert job["evidence_refs"][0]["ok"] and out["evidence_refs"] == job["evidence_refs"]
        await _tick()

    _play(scenario)


# ── 미리 보기 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_미리_보기는_크기와_지문만_준다(eng, gw):
    ev = [_ev_inline("IN-1"), {"ref": {"kind": "risk_bundle", "id": "BND-A"}}, {"ref": {"kind": "risk_bundle", "id": "BND-GONE"}}]
    out = asyncio.run(m.deliberate_resolve_evidence(ev, ctx=_ctx(ME)))
    assert out["items_in"] == 3 and [x["ok"] for x in out["refs"]] == [True, False]
    assert out["inline"] == {"items": 1, "chars": len(ev[0]["result"])}
    want = [ev[0], *_BUNDLE_EV]
    assert out["expanded"] == {"items": 4, "chars": sum(len(x["result"]) for x in want), "fingerprint": _fp(want)}
    assert out["limits"] == m._evid_limits() and out["over"] == {} and len(out["omitted"]) == 1
    assert "판정 칸 값" not in json.dumps(out, ensure_ascii=False), "본문은 싣지 않는다"
    assert delib_jobs._JOBS == {}, "잡을 만들지 않는다"
    with pytest.raises(ValueError):
        asyncio.run(m.deliberate_resolve_evidence([], ctx=_ctx(ME)))


def test_미리_보기는_상한을_넘으면_까닭을_단다(eng, gw, monkeypatch):
    monkeypatch.setattr(m, "_evid_limits", lambda: {"evidence_items": 2, "evidence_item_chars": 10, "evidence_total_chars": 10})
    out = asyncio.run(m.deliberate_resolve_evidence([{"ref": {"kind": "risk_bundle", "id": "BND-A"}}], ctx=_ctx(ME)))
    assert set(out["over"]) == {"items", "chars"}


def test_HTTP_미리_보기는_머리의_신원으로_푼다(eng, gw, monkeypatch):
    from fastapi.testclient import TestClient
    import app as a
    import seat_box
    monkeypatch.setattr(seat_box, "secret", lambda _app: "Bearer gw-secret")
    c = TestClient(a.app)
    body = {"evidence": [{"ref": {"kind": "risk_bundle", "id": "BND-A"}}]}
    assert c.post("/deliberate/evidence/resolve", json=body).status_code == 401
    rr = c.post("/deliberate/evidence/resolve", json=body,
                headers={"Authorization": "Bearer gw-secret", "x-hwax-user": ME, "x-hwax-groups": "CAEG,feat%3Achat"})
    assert rr.status_code == 200 and rr.json()["refs"][0]["ok"] is True
    assert gw.calls[-1]["user"] == ME and gw.calls[-1]["groups"] == ["CAEG", "feat:chat"]
    bad = c.post("/deliberate/evidence/resolve", json={"evidence": [{"ref": {"kind": "dynaforge_report", "id": "D"}}]},
                 headers={"Authorization": "Bearer gw-secret"})
    assert bad.status_code == 422 and "kind not opened yet" in bad.json()["message"]


def test_설명은_kind_목록을_모듈에서_읽는다():
    tools = {t.name: t for t in asyncio.run(m.mcp.list_tools())}
    assert "deliberate_resolve_evidence" in tools
    for k in r.KINDS:
        assert k in tools["deliberate_start"].description and k in tools["deliberate_resolve_evidence"].description
    for k in r.NOT_OPENED:
        assert k in tools["deliberate_start"].description

# 외부 응답자 계약 예제(/health·limits·plan·거절·run 프레임·MCP 과제 도구의 응답·심의 프레임)를 실제 라우트·도구와 가짜 일꾼으로 떠 tests/fixtures/external_llm/contract/ 에 못박는다 — 리스크 앱(X-2)과 워크플로(X-3)가 이 바이트로 짠다
#
# 예제는 손으로 쓰지 않는다 — 실제 앱(app.app)의 라우트를 TestClient 로, 실제 MCP 도구 함수(review_task_*)를 가짜 일꾼으로 부른다
# (tests/fakes/worker.py — 답은 가짜 LLM 대본). 실제 Claude 는 부르지 않는다(X-D1). 과제 id·시계는 고정한 것으로 바꿔 끼워
# 다시 떠도 같은 바이트가 나온다(meta.llm_ms 는 0 으로 가린다).
#
# GLM 길의 예제(tests/fixtures/card_review/contract/)와 디렉터리를 나눈다 — 리스크 앱이 그쪽을 바이트째 복사해 견주므로(HWAXRisk
# test_card_contract_copies), 외부 길을 더하는 것이 그쪽 사본을 낡게 만들지 않게 한다. index.json 이 파일마다 무엇인지 적는다.
#
#   다시 뜨기:  EXTERNAL_LLM_CONTRACT_UPDATE=1 .venv/bin/python -m pytest tests/test_external_llm_contract.py -q
#   실행:       .venv/bin/python -m pytest tests/test_external_llm_contract.py -q
import asyncio
import copy
import itertools
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from fastapi.testclient import TestClient  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import external_llm as x  # noqa: E402
import mcp_server as m  # noqa: E402
from fakes.llm import FakeLLM, act, default_oracle, respond  # noqa: E402
from fakes.worker import FakeWorker, ctx_of  # noqa: E402
from test_card_review_contract import REQUESTS  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "external_llm" / "contract"
GLM_CONTRACT = ROOT / "tests" / "fixtures" / "card_review" / "contract"
_UPDATE = os.environ.get("EXTERNAL_LLM_CONTRACT_UPDATE") == "1"
OWNER = "owner@x.com"
# 포털 중계가 덮어쓰는 신원 칸 넷 — 예제 요청 파일에는 싣지 않고 보낼 때만 더한다(GLM 예제와 같다). 외부 길은 user_email 이 있어야 한다.
IDENT = {"groups": ["g"], "user_email": OWNER, "user_pat": "user-pat", "entitlements": ["feat:deliberation"]}
T0 = 1_760_400_000.0          # 고정한 벽시계(유닉스 초) — answer_deadline·created_at 이 다시 떠도 같다


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _ids():
    n = itertools.count(1)
    return lambda: f"xt-{next(n):04d}"


def _leases():
    n = itertools.count(1)
    return lambda: f"ls-{next(n):04d}"


def scaled(k: float = 1000.0):
    t0 = time.monotonic()
    return lambda: 1000.0 + (time.monotonic() - t0) * k


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def _mask(event: str, data: dict) -> dict:
    data = copy.deepcopy(data)
    if event == "result":
        data["meta"]["llm_ms"] = [0] * len(data["meta"]["llm_ms"])
    return data


def _setup(monkeypatch, board, *, enabled=True):
    if enabled:
        monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    else:
        monkeypatch.delenv("EXTERNAL_LLM_ENABLED", raising=False)
    monkeypatch.setattr(a.app.state, "external_board", board, raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    monkeypatch.setattr(m, "_APP", a.app)
    return TestClient(a.app)


def _run(client, body):
    r = client.post("/card-review/run", json=body)
    assert r.status_code == 200, r.text[:300]
    frames = [delib_jobs._parse_sse(c.encode()) for c in r.text.split("\n\n") if c.strip()]
    return [(e, _mask(e, data)) for e, data in frames]


def _sse(frames) -> str:
    return b"".join(a._sse(e, data) for e, data in frames).decode()


def _worker_log(w: FakeWorker) -> list:
    """일꾼이 본 것 — 과제 머리 · 쪽 머리(글 길이만) · 낸 조각의 응답. 프롬프트 글은 run 의 요청에서 다시 만들 수 있어 싣지 않는다."""
    return [{"head": s["head"], "pages": s["pages"], "submits": s.get("submits")} for s in w.seen]


def _scripted(*behaviors):
    left = list(behaviors)

    def brain(_head, system, human):
        b = left.pop(0) if len(left) > 1 else left[0]
        return respond(act(b), system, human, default_oracle)[0]
    return brain


def produce(monkeypatch) -> dict:
    files: dict = {}
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a, "_delib_human", lambda: 0)
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    d._evid_cache.clear()
    fixed = lambda: x.Board(x.Settings(), clock=Clock(), wall=lambda: T0, new_id=_ids(), new_lease=_leases())  # noqa: E731

    # ── 도구 목록(켜진 서버의 tools/list) ──
    on = FastMCP(name="contract")
    m._register_external(on, env={"EXTERNAL_LLM_ENABLED": "1"})
    files["mcp_tools.json"] = _dump([{"name": t.name, "title": t.title, "description": t.description,
                                      "inputSchema": t.inputSchema} for t in asyncio.run(on.list_tools())])

    # ── /health — 꺼짐(종전 그대로)·켜짐 ──
    c = _setup(monkeypatch, None, enabled=False)
    h = c.get("/health").json()
    files["health_off.json"] = _dump({"capabilities": h["capabilities"], "external_llm_present": "external_llm" in h})
    c = _setup(monkeypatch, fixed())
    h = c.get("/health").json()
    files["health_on.json"] = _dump({"capabilities": h["capabilities"], "external_llm": h["external_llm"]})

    # ── limits · plan ──
    files["limits_external.json"] = _dump(c.get("/card-review/limits", params={"llm": "external"}).json())
    plan_req = {**json.loads((GLM_CONTRACT / "plan_req.json").read_text(encoding="utf-8")), "llm": "external"}
    r = c.post("/card-review/plan", json=plan_req)
    assert r.status_code == 200, r.text
    files["plan_external_req.json"] = _dump(plan_req)
    files["plan_external.json"] = _dump(r.json())

    # ── 스트림을 열기 전의 거절 ──
    one = {**REQUESTS["verdict_one"], **IDENT}
    rejects = {}
    c = _setup(monkeypatch, None, enabled=False)
    for where, call in (("limits", lambda: c.get("/card-review/limits", params={"llm": "external"})),
                        ("plan", lambda: c.post("/card-review/plan", json={"cards": [], "units": [], "llm": "external"})),
                        ("run", lambda: c.post("/card-review/run", json={**one, "llm": "external"}))):
        r = call()
        rejects[f"external_llm_disabled.{where}"] = {"status": r.status_code, "body": r.json()}
    c = _setup(monkeypatch, fixed())
    lim_glm = c.get("/card-review/limits").json()
    for name, call in (
            ("external_owner_missing.run", lambda: c.post("/card-review/run",
                                                          json={**one, "llm": "external", "user_email": ""})),
            ("bad_request.limits", lambda: c.get("/card-review/limits", params={"llm": "claude"})),
            ("bad_request.run", lambda: c.post("/card-review/run", json={**one, "llm": "claude"})),
            ("rev_mismatch.run", lambda: c.post("/card-review/run", json={
                **one, "llm": "external", "expect": {"limits_rev_kind": lim_glm["limits_rev_by_kind"]["verdict"]}}))):
        r = call()
        rejects[name] = {"status": r.status_code, "body": r.json()}
    files["rejects.json"] = _dump({"_how": {
        "external_llm_disabled.*": "EXTERNAL_LLM_ENABLED 가 꺼진 서버에 llm=external(limits 는 ?llm=external)",
        "external_owner_missing.run": "req_verdict_one + 신원 칸에서 user_email 을 비웠다",
        "bad_request.*": "llm 에 모르는 값('claude') — 받는 값은 glm·external 뿐이다(앱의 'claude' 는 엔진에 'external' 로 보낸다)",
        "rev_mismatch.run": "GLM limits 의 limits_rev_by_kind.verdict 를 expect 로 실은 외부 run — 두 길의 판은 갈린다"},
        **rejects})

    # ── run — 가짜 일꾼(실제 MCP 도구 함수)이 답한다 ──
    index = {}
    for scene, req_name, brain in (("verdict_ok", "verdict_fwd", None),
                                   ("verdict_repaired", "verdict_fwd", _scripted("skip_middle", "oracle"))):
        c = _setup(monkeypatch, fixed())
        body = {**REQUESTS[req_name], "llm": "external"}
        files[f"req_{scene}.json"] = _dump(body)
        with FakeWorker(OWNER, **({"brain": brain} if brain else {})).thread() as w:
            frames = _run(c, {**body, **IDENT})
        files[f"run_{scene}.sse"] = _sse(frames)
        event, last = frames[-2]
        files[f"{event}_{scene}.json"] = _dump(last)
        files[f"worker_{scene}.json"] = _dump(_worker_log(w))
        index[scene] = {"request": f"req_{scene}.json", "frames": f"run_{scene}.sse", "final": f"{event}_{scene}.json",
                        "worker": f"worker_{scene}.json", "tasks": len(w.seen)}

    # ── run — 한도(기존 error 프레임, 코드가 보인다) ──
    for code, settings, hold in (("external_worker_absent", x.Settings(wait_s=300.0), False),
                                 ("external_answer_timeout", x.Settings(wait_s=3000.0, answer_s=1200.0), True)):
        c = _setup(monkeypatch, x.Board(settings, clock=scaled(), tick=0.02, new_id=_ids()))
        if hold:
            with FakeWorker(OWNER, hold=True, poll_s=0.05).thread():
                frames = _run(c, {**REQUESTS["verdict_one"], "llm": "external", **IDENT})
        else:
            frames = _run(c, {**REQUESTS["verdict_one"], "llm": "external", **IDENT})
        files[f"run_{code}.sse"] = _sse(frames)
        event, last = frames[-2]
        assert (event, last["code"]) == ("error", code), last
        files[f"error_{code}.json"] = _dump(last)
        index[code] = {"request": "req_verdict_one(GLM 계약 예제) + llm=external", "frames": f"run_{code}.sse",
                       "final": f"error_{code}.json",
                       "how": "일꾼 없음" if not hold else "일꾼이 집고 답하지 않는다(한 번 되돌린 뒤 두 번째 집힘도 넘김)",
                       "settings": {"wait_s": settings.wait_s, "answer_s": settings.answer_s}}
    files["index.json"] = _dump(index)

    # ── MCP 과제 도구의 응답 모양 ──
    files["mcp_examples.json"] = _dump(_mcp_examples(monkeypatch))
    # ── 심의(쟁점 패널) 프레임 ──
    files["delib_frames.json"] = _dump(_delib_frames(monkeypatch))
    return files


def _mcp_examples(monkeypatch) -> dict:
    """한 과제로 도구 응답 모양 전부 — 머리 · 쪽 · 나눠 내기 · 어긋난 offset · 되돌린 과제 · 다시 집힌 과제에 묵은 임대로 낸 조각 ·
    없는 과제 · 빈 줄 · 긴 프롬프트의 쪽 사슬."""
    clock = Clock()
    # 벽시계는 줄의 시계를 따라간다 — 되돌린 뒤 다시 집은 머리의 answer_deadline 이 실제처럼 뒤로 간다.
    board = x.Board(x.Settings(), clock=clock, wall=lambda: T0 + (clock.t - 1000.0), new_id=_ids(), new_lease=_leases(),
                    tick=0.02)
    stub = SimpleNamespace(state=SimpleNamespace(external_board=board))
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    monkeypatch.setattr(m, "_APP", stub)
    me = ctx_of(OWNER)
    system = "당신은 사내 전문가 'mech-a' 한 명입니다. 출력은 한 줄에 JSON 객체 하나씩입니다(JSON Lines)."
    human = "[점검 카드]\n[K01] 종류 check «보스 둘레 간극은 0.3 mm 이상»\n\n[변경]\nL01 [c:1] «보스 간극 0.5 → 0.2 mm»\n"

    async def go():
        out = {"next_empty": await m.review_task_next(ctx=me)}
        model = x.model_for(stub, owner=OWNER, kind="verdict", target_key="t-ab12", label="verdict/fwd mech-a u-01")
        call = asyncio.ensure_future(model.bind(max_tokens=32_000).ainvoke([("system", system), ("human", human)]))
        while not board.counts()["queued"]:
            await asyncio.sleep(0.01)
        head = out["next_head"] = await m.review_task_next(ctx=me)
        tid, lease1 = head["task_id"], head["lease"]
        out["read_page"] = await m.review_task_read(tid, lease1, 0, ctx=me)
        line = '{"t":"v","k":"K01","applies":"yes","judgement":"FAIL","cq":"보스 둘레 간극은 0.3 mm 이상","l":["L01"],'
        out["submit_partial"] = await m.review_task_submit(tid, lease1, line, offset=0, ctx=me)
        out["submit_offset_mismatch"] = await m.review_task_submit(tid, lease1, "…", offset=3, ctx=me)
        clock.t += 1201                                   # 답 한도를 넘겼다 — 한 번 줄로 되돌아간다(임대가 지워진다)
        out["read_not_claimed"] = await m.review_task_read(tid, lease1, 0, ctx=me)
        again = out["next_head_claim2"] = await m.review_task_next(ctx=me)
        lease2 = again["lease"]
        rest = '"why":"간극 하한을 넘는다","need":""}'
        await m.review_task_submit(again["task_id"], lease2, line, offset=0, ctx=me)
        # 앞 집기(lease1)의 늦은 조각 — 다시 집힌 과제의 버퍼에 닿지 않는다(두 일꾼의 답이 이어 붙지 않는다).
        out["submit_stale_lease"] = await m.review_task_submit(tid, lease1, '"v":"OK"}', final=True, ctx=me)
        out["submit_final"] = await m.review_task_submit(again["task_id"], lease2, rest, offset=len(line), final=True,
                                                         ctx=me)
        out["read_task_not_found"] = await m.review_task_read(tid, lease2, 0, ctx=me)
        out["submit_other_owner"] = await m.review_task_submit(tid, lease2, "x", final=True, ctx=ctx_of("other@x.com"))
        msg = await call
        out["engine_received"] = {"content": msg.content, "response_metadata": msg.response_metadata}
        # 긴 프롬프트(훑기 크기 — 약 12만 자)의 쪽 사슬 — 글은 빼고 쪽 머리만
        long_call = asyncio.ensure_future(x.model_for(stub, owner=OWNER, kind="sweep").ainvoke(
            [("system", system), ("human", ("[검토 단위 목록]\nU01 [bundle] «후면 커버 둘레 접착 계면 변경 — 줄바꿈과 \"따옴표\"»\n" * 2000))]))
        while not board.counts()["queued"]:
            await asyncio.sleep(0.01)
        h2 = await m.review_task_next(ctx=me)
        chain, off = [], 0
        while off is not None:
            p = await m.review_task_read(h2["task_id"], h2["lease"], off, ctx=me)
            chain.append({k: p[k] for k in ("offset", "next_offset", "chars")} | {"text_chars": len(p["text"])})
            off = p["next_offset"]
        out["read_chain_long"] = {"head": h2, "pages": chain}
        out["submit_answer_too_large"] = {k: v for k, v in (await m.review_task_submit(
            h2["task_id"], h2["lease"], "x" * (x.ANSWER_HARD_MAX + 1), ctx=me)).items()}
        long_call.cancel()
        await asyncio.gather(long_call, return_exceptions=True)
        return out

    return asyncio.run(go())


def _delib_frames(monkeypatch) -> dict:
    """쟁점 패널 모양의 심의를 실제로 돌려 외부 응답자에 걸린 프레임만 — 시작 상태줄 · 알림 카드 · 결과 칸 · 한도의 cause · 거절."""
    from test_external_llm_delib import PANEL, Q, _panel  # noqa: PLC0415 — 같은 하네스를 쓴다

    out = {}
    monkeypatch.setenv("EXTERNAL_LLM_ENABLED", "1")
    events, *_ = _panel(monkeypatch)
    out["start_status"] = next(data for ev, data in events if ev == "status"
                               and data.get("step", "").startswith("외부 응답자 실행"))
    out["notice_card"] = next(data for ev, data in events if ev == "delib" and data.get("kind") == "evidence"
                              and data.get("source") == "외부 응답자 실행 — evidence_only")
    res = next(data for ev, data in events if ev == "result")
    out["result"] = {**{k: v for k, v in res.items() if k != "content"},
                     "content": res["content"].split("\n", 1)[0] + "\n…(결정문 — 줄임)"}
    events, *_ = _panel(monkeypatch, worker=False, board=x.Board(x.Settings(wait_s=300.0), clock=scaled(), tick=0.02))
    out["seat_lost_absent"] = next(data for ev, data in events if ev == "warning" and data.get("code") == "seat_lost")
    out["chair_failed_absent"] = next(data for ev, data in events if ev == "error")
    events, *_ = _panel(monkeypatch, user="", worker=False)
    out["refused_owner_missing"] = events[0][1]
    monkeypatch.delenv("EXTERNAL_LLM_ENABLED")
    events, *_ = _panel(monkeypatch, worker=False)
    out["refused_disabled"] = events[0][1]
    out["_how"] = {"request": {"question": Q, "delib_opts": PANEL},
                   "path": "포털 /agent/chat 의 /심의(리스크 앱 쟁점 패널) — 여기서는 run_deliberation 을 바로 돌렸다",
                   "absent": "일꾼 없음 · EXTERNAL_LLM_WAIT_S=300(시계를 빨리 돌렸다)"}
    return out


def test_예제는_실제_라우트와_도구가_낸_것과_같다(monkeypatch):
    got = produce(monkeypatch)
    if _UPDATE:
        OUT.mkdir(parents=True, exist_ok=True)
        for p in OUT.iterdir():
            if p.name not in got:
                p.unlink()
        for name, text in got.items():
            (OUT / name).write_text(text, encoding="utf-8")
    have = sorted(p.name for p in OUT.iterdir())
    assert have == sorted(got), f"예제 목록이 다르다 — 없는 것 {sorted(set(got) - set(have))}, 남은 것 {sorted(set(have) - set(got))}"
    for name, text in got.items():
        assert (OUT / name).read_text(encoding="utf-8") == text, (
            f"{name} 가 실제 라우트·도구의 산출과 다르다 — 엔진을 고쳤으면 리스크 앱(X-2)·워크플로(X-3) 시험과 같이 보고 "
            "EXTERNAL_LLM_CONTRACT_UPDATE=1 로 다시 뜬다")


# ── 예제가 조용히 얇아지지 않게 ─────────────────────────────────────────────────
def _load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def test_외부_길의_오류_코드마다_예제가_있다():
    for code in cr.EXTERNAL_ERRORS:
        err = _load(f"error_{code}.json")
        assert err["code"] == code and (err["infra"], err["retryable"]) == cr.EXTERNAL_ERRORS[code]
        assert err["knob"] in ("EXTERNAL_LLM_WAIT_S", "EXTERNAL_LLM_ANSWER_S")
    rejects = _load("rejects.json")
    assert {k: v["status"] for k, v in rejects.items() if k != "_how"} == {
        "external_llm_disabled.limits": 409, "external_llm_disabled.plan": 409, "external_llm_disabled.run": 409,
        "external_owner_missing.run": 422, "bad_request.limits": 422, "bad_request.run": 422, "rev_mismatch.run": 409}
    assert all(v["body"]["code"] == k.split(".")[0] for k, v in rejects.items() if k != "_how")


def test_예제가_계약의_칸을_싣는다():
    h = _load("health_on.json")
    assert h["capabilities"][-1] == "external_llm" and _load("health_off.json") == {
        "capabilities": ["card_review", "drain"], "external_llm_present": False}
    assert set(h["external_llm"]) == {"ctx_tokens", "max_tokens", "wait_s", "answer_s", "call_worst_s", "queued",
                                      "claimed", "evid_budget"}
    lim = _load("limits_external.json")
    assert (lim["llm"], lim["ctx_source"], lim["max_tokens_source"]) == ("external", "external", "external")
    assert _load("plan_external.json")["limits_rev"] == lim["limits_rev"]
    ok = _load("result_verdict_ok.json")
    assert (ok["meta"]["llm"], ok["meta"]["limits_rev_kind"]) == ("external", lim["limits_rev_by_kind"]["verdict"])
    assert _load("result_verdict_repaired.json")["quality"]["attempts"] == 2
    head = _load("mcp_examples.json")["next_head"]
    assert set(head) == {"task_id", "lease", "kind", "max_tokens", "chars", "parts", "answer_deadline", "answer_s",
                         "claim", "created_at", "target_key", "label"}
    ex = _load("mcp_examples.json")
    assert ex["read_task_not_found"] == ex["submit_other_owner"], "남의 과제와 없는 과제는 같은 답이다"
    assert ex["next_head_claim2"]["lease"] != head["lease"] and ex["submit_stale_lease"]["code"] == "not_claimed"
    assert ex["engine_received"]["content"].count('"t":"v"') == 1, "묵은 임대의 조각이 답에 이어 붙었다"
    assert [p["next_offset"] is None for p in ex["read_chain_long"]["pages"]].count(True) == 1
    assert {t["name"] for t in _load("mcp_tools.json")} == {"review_task_next", "review_task_read", "review_task_submit"}
    frames = _load("delib_frames.json")
    assert (frames["result"]["llm"], frames["result"]["tool_mode"]) == ("external", "evidence_only")
    assert frames["seat_lost_absent"]["cause"] == frames["chair_failed_absent"]["cause"] == "external_worker_absent"
    assert (frames["refused_disabled"]["code"], frames["refused_owner_missing"]["code"]) == (
        "external_llm_disabled", "external_owner_missing")

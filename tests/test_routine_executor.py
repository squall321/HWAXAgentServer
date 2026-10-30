# 분석 루틴 실행기(routine.py) — 가짜 게이트웨이(원 세션 JSON-RPC 꼴 그대로 · 포털 실행 상태 기계)와 대본 챗 모델로 R3-design §4 의 시험 목록을 본다
"""HWAXPortal docs/analysis-routines/R3-design.md §4 '시험' 이 정본이다.

가짜는 진짜 꼴로 만든다(가짜의 꼴이 다르면 판정 결함을 숨긴다). 게이트웨이는 원 세션 그대로다 — initialize 의 mcp-session-id 머리 ·
notifications/initialized 202 · tools/call 의 SSE 답(`event: message\\ndata: {JSON-RPC}`) · 모르는 세션 404 · DELETE. 루틴 도구의 답은
HWAXMcpGateway gateway.py 의 실물 꼴이다 — routine_call 성공은 머리 TextContent JSON({routine_call, step, tool, ok, chars[, pages, next]}) +
본문 블록, 거절은 `ROUTINE_ERROR: …` 한 블록과 isError. 포털 원장은 routine_routes 의 규칙(예산 · 상한 · 봉투 고치기 · 보고서)을 따른다.
챗 모델은 대본대로 답한다 — 단계 ReAct(시스템 글의 〔단계 id〕) · 봉투 턴(도구 없는 따로 턴) · 인사이트 턴을 가른다.

  실행:  .venv/bin/python -m pytest tests/test_routine_executor.py -q
"""
import ast
import asyncio
import itertools
import json
import re
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402
from pydantic import Field  # noqa: E402

import app as a  # noqa: E402
import routine as rt  # noqa: E402

_ORIG_USER_TOOLS = rt._user_tools        # 시험이 바꿔 끼우기 전의 진짜 로더
PAT = "chat-pat-user-1"                 # 사람 PAT(머리는 latin-1 이라 ASCII)
GW_TOKEN = "gw-shared-secret"           # 연결 설정의 게이트웨이 공유 비밀 — 루틴 길에 실리면 안 된다
RUN_ID = "0123456789abcdef0123456789abcdef"
PORTAL_ROUTES = ROOT.parent / "HWAXPortal" / "backend" / "app" / "agent" / "routes.py"
CLOSED = ("ok", "empty", "denied", "not_connected", "unavailable", "failed", "skipped")
STATES = ("ok", "empty", "ambiguous", "denied", "not_connected", "unavailable", "failed", "skipped")


# ── 가짜 게이트웨이 + 포털 원장 ───────────────────────────────────────────────────────────────────────────────
def _step(sid, *, kind="platform", backends=("signalforge",), allowed=None, goal=None, uses=(), keys=(), calls=8, seconds=300,
          state="pending", delib=None, blocked=None):
    """포털 plan_steps 의 행 꼴(routines.py) — 실행 보기가 상태 칸을 얹는다."""
    if kind == "deliberation":
        backends = ("hwax-deliberation",)
        allowed = allowed or {"hwax-deliberation": ["deliberate_start", "deliberate_status", "deliberate_result"]}
    return {"id": sid, "title": f"단계 {sid}", "kind": kind, "backends": list(backends),
            "allowed": allowed if allowed is not None else {b: [] for b in backends}, "goal": goal or f"{sid} 를 조회한다",
            "uses": list(uses), "expect": {"keys": list(keys), "fields": []}, "budget": {"calls": calls, "seconds": seconds},
            "blocked": blocked or {"empty": "continue", "denied": "skip", "unavailable": "skip"}, "deliberation": delib,
            "readiness": {"state": "allowed", "reason": ""}, "_state": state}


def _frame(obj) -> str:
    """MCP SDK streamable HTTP 의 SSE 답 한 덩이."""
    return f"event: message\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _ok(obj) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}], "isError": False}


def _refuse(msg: str) -> dict:
    return {"content": [{"type": "text", "text": f"ROUTINE_ERROR: {msg}"}], "isError": True}


class FakeGateway:
    PAGE = 60000                        # 게이트웨이 ROUTINE_PAGE_CHARS — 시험이 줄인다

    def __init__(self, steps, *, purpose="S26 스웰링 시장불량 — 원인 · 시험 · 대책", title="시장불량 분석",
                 sections=None, upload="auto", impls=None, state="pending", inputs=None, statuses=None, pat=PAT):
        self.plan = steps
        self.stage = {s["id"]: {"state": s["_state"], "calls": 0, "env": None, "flags": [], "reason": None} for s in steps}
        self.purpose, self.title, self.upload, self.pat = purpose, title, upload, pat
        self.sections = sections if sections is not None else [s["id"] for s in steps] + ["insight"]
        self.state, self.reason = state, None   # pending = 아직 시작 전(새 실행), 그 밖은 이어서 시험이 정한 상태
        self.note = ""                           # 사용자가 덧붙인 말 — 새 실행의 routine_run_start(note)를 실행에 남긴다(포털 RunIn.note)
        self.ra_report_id = None                 # 이 실행의 RA 초안 — 게이트웨이가 만들면 포털 /ra 로 실행 행에 남는다
        self.ra_made = 0                         # 게이트웨이가 RA 초안을 만든 수
        self.report_out: dict = {}               # 렌더한 보고서의 저장본(포털 report_json) — 끝난 실행에 다시 내면 이것을 돌려준다
        self.inputs = inputs or {"model": "S26"}
        self.impls = dict(impls or {})
        self.statuses = list(statuses or [{"status": "done"}])
        self.limit = None
        self.limits = {"minutes": 60, "calls": 60}   # 실행 상한(포털 plan.limits) — run.calls 는 원장 줄 수다
        self.cancelled: list = []                     # deliberate_cancel 로 접은 job_id
        self.ledger: list = []
        self.log: list = []              # (name, arguments, headers) — tools/call 마다
        self.auths: list = []
        self.sessions: list = []
        self.deleted = 0
        self.inflight = self.max_inflight = 0
        self.delay: dict = {}
        self.start_args = None
        self.step_done_sent: list = []
        self.sections_sent = None
        self.report_md = ""

    # 원 세션
    async def handle(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        self.auths.append(auth)
        if auth != f"Bearer {self.pat}":
            return httpx.Response(401, json={"error": "expired"})
        if request.method == "DELETE":
            self.deleted += 1
            return httpx.Response(200)
        body = json.loads(request.content or b"{}")
        method = body.get("method")
        if method == "initialize":
            sid = f"sess-{len(self.sessions) + 1}"
            self.sessions.append(sid)
            return httpx.Response(200, headers={"mcp-session-id": sid, "content-type": "text/event-stream"},
                                  text=_frame({"jsonrpc": "2.0", "id": body.get("id"), "result": {
                                      "protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}}))
        if method == "notifications/initialized":
            return httpx.Response(202)
        if request.headers.get("mcp-session-id") not in self.sessions:
            return httpx.Response(404, json={"jsonrpc": "2.0", "id": "server-error",
                                             "error": {"code": -32600, "message": "Session not found"}})
        if method != "tools/call":
            return httpx.Response(400)
        p = body.get("params") or {}
        name, args = p.get("name"), p.get("arguments") or {}
        self.log.append((name, args, dict(request.headers)))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(self.delay.get(name, 0.002))
            res = await self.tool(name, args)
        finally:
            self.inflight -= 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=_frame({"jsonrpc": "2.0", "id": body.get("id"), "result": res}))

    def names(self) -> list:
        return [n for n, _a, _h in self.log]

    def rcalls(self, step=None) -> list:
        return [x for n, x, _h in self.log if n == "routine_call" and (step is None or x.get("step") == step)]

    # 포털 실행 보기(routine_routes.run_view 꼴)
    def view(self) -> dict:
        steps = []
        for s in self.plan:
            st = self.stage[s["id"]]
            env = st["env"] or {}
            row = {k: v for k, v in s.items() if not k.startswith("_")}
            row.update(state=st["state"], calls=st["calls"], reason=st["reason"] or "", summary=env.get("summary") or "",
                       keys=env.get("keys") or {}, flags=st["flags"], findings=env.get("findings") or [])
            if st["state"] == "ambiguous":
                row["candidates"] = env.get("candidates") or []
            steps.append(row)
        # 포털 _limit — 도는 실행만, 호출 상한(원장 줄 수)을 먼저 본다(둘 다 닿았으면 calls). 시험이 세우는 limit 은 시간 상한이다
        kind, limit = None, None
        if self.state == "running":
            if len(self.ledger) >= self.limits["calls"]:
                kind, limit = "calls", f"호출 {self.limits['calls']}번"
            elif self.limit:
                kind, limit = "time", self.limit
        return {"contract": 1,
                "run": {"id": RUN_ID, "routine_id": "rt-1", "version_no": 1, "title": self.title, "executor": "chat", "state": self.state,
                        "reason": self.reason, "calls": len(self.ledger), "started_at": 0, "deadline": 0, "ended_at": None,
                        "ra_report_id": self.ra_report_id, "ra_error": None, "limits": dict(self.limits), "limit_reached": limit,
                        "limit_kind": kind, "report_ready": bool(self.report_md)},
                "note": self.note, "inputs": self.inputs, "purpose": self.purpose, "steps": steps,
                "next_step": next((s["id"] for s in steps if s["state"] in ("pending", "running", "ambiguous")), None),
                "report": {"title": f"{self.title} 보고서", "sections": self.sections, "upload": self.upload}}

    async def _inner(self, bk, orig, args):
        if (bk, orig) == ("hwax-deliberation", "deliberate_start"):
            return json.dumps({"job_id": "mechanism-20261030-101010-abc123", "status": "queued"}, ensure_ascii=False), False
        if (bk, orig) == ("hwax-deliberation", "deliberate_result"):
            return json.dumps({"job_id": args.get("job_id"), "status": "done",
                               "decision": "결정 — 셀 스웰링의 1차 원인은 전해액 분해 가스다(근거 1234.5 ppm)"}, ensure_ascii=False), False
        fn = self.impls.get((bk, orig))
        if fn is None:
            return f"{orig} 결과 — 조회 {json.dumps(args, ensure_ascii=False)}", False
        out = fn(args)
        return (await out) if asyncio.iscoroutine(out) else out

    async def tool(self, name, a_) -> dict:   # noqa: C901 — 게이트웨이 · 포털 규칙을 한 곳에 그대로 옮긴다
        run = self.view()["run"]
        if name == "routine_run_start":
            if a_.get("run_id"):
                if self.state in ("running", "interrupted"):
                    self.state = "running"
                    return _ok(self.view())
                if self.state == "waiting":
                    return _refuse("고를 것이 있다 — 고른 뒤 이어서")
                return _refuse(f"실행이 {self.state} 다")
            self.start_args = a_
            if self.state == "pending":
                self.state = "running"
                self.note = str(a_.get("note") or "").strip()
            return _ok(self.view())
        if name == "routine_get":
            if a_.get("run_id") != RUN_ID:
                return _refuse("실행을 찾을 수 없습니다")
            if a_.get("call_id"):
                c = next((c for c in self.ledger if c["call_id"] == a_["call_id"]), None)
                if c is None:
                    return _refuse(f"원장에 호출 {a_['call_id']!r} 가 없다")
                pages = max(1, -(-len(c["result"]) // self.PAGE))
                page = int(a_.get("page") or 1)
                if page > pages:
                    return _refuse(f"쪽은 {pages}쪽까지다")
                return _ok({"call_id": c["call_id"], "step": c["step"], "tool": f"{c['backend']}:{c['tool']}", "ok": c["ok"],
                            "chars": len(c["result"]), "truncated": False, "page": page, "pages": pages,
                            "text": c["result"][(page - 1) * self.PAGE: page * self.PAGE]})
            return _ok(self.view())
        if name == "routine_call":
            step, nm, inner = a_.get("step"), str(a_.get("name") or ""), a_.get("arguments") or {}
            if run["state"] != "running":
                return _refuse(f"실행이 {run['state']} 상태다 — 도구를 부르지 않는다")
            ps = next((s for s in self.plan if s["id"] == step), None)
            if ps is None:
                return _refuse(f"이 실행에 단계 {step!r} 가 없다")
            st = self.stage[step]
            if st["state"] not in ("pending", "running"):
                return _refuse(f"단계 {step} 는 이미 닫혔다({st['state']})")
            # 심의 단계의 결정문 읽기는 단계 예산에서 빠진다(R3-design §8 — 실행 호출 상한은 그대로)
            if st["calls"] >= ps["budget"]["calls"] and not (ps["kind"] == "deliberation" and nm == "hwax-deliberation:deliberate_result"):
                return _refuse(f"단계 {step} 의 호출 예산({ps['budget']['calls']}번)을 다 썼다 — routine_step_done 으로 닫는다")
            # 시간 상한 뒤에도 결정문 읽기는 연다(R3-D5) — 실행 호출 상한은 그대로다(게이트웨이 _routine_call)
            if run["limit_reached"] and not (run["limit_kind"] == "time" and nm == "hwax-deliberation:deliberate_result"):
                return _refuse(f"실행 상한에 닿았다({run['limit_reached']}) — 남은 단계를 닫고 보고서를 낸다")
            bk, _, orig = nm.partition(":")
            if not orig or bk not in ps["backends"] or orig not in (ps["allowed"].get(bk) or []):
                return _refuse(f"울타리 밖 — 단계 {step} 는 {', '.join(ps['backends'])} 의 허용 도구만 부른다")
            text, is_err = await self._inner(bk, orig, inner)
            if self.state != "running":
                # 포털 add_call — 게이트웨이가 안쪽 도구를 부른 뒤 원장 쓰기에서 실행 상태를 다시 본다(꼬리 없는 글 · 원장에 안 남는다)
                return _refuse(f"실행이 {self.state} 상태다")
            st["calls"] += 1
            st["state"] = "running"
            cid = f"{step}.c{st['calls']}"
            ok = not is_err
            self.ledger.append({"call_id": cid, "step": step, "backend": bk, "tool": orig, "args": inner, "ok": ok, "result": text})
            head = {"routine_call": cid, "step": step, "tool": f"{bk}:{orig}", "ok": ok, "chars": len(text)}
            if len(text) > self.PAGE:
                pages = -(-len(text) // self.PAGE)
                head.update(pages=pages, next=f"routine_get(run_id, call_id={cid}, page=2) — 이 답은 1/{pages}쪽")
                body = [{"type": "text", "text": text[:self.PAGE]}]
            else:
                body = [{"type": "text", "text": text}]
            return {"content": [{"type": "text", "text": json.dumps(head, ensure_ascii=False)}] + body, "isError": is_err}
        if name == "routine_step_done":
            return self._step_done(a_)
        if name == "routine_report_submit":
            if self.state == "done" and self.report_out:
                return _ok(self._ra(dict(self.report_out), a_))   # 끝난 실행 — 포털은 다시 렌더하지 않고 저장본을 돌려준다(R3-D12)
            if self.state != "running":
                return _refuse(f"실행이 {self.state} 상태다 — 보고서는 도는 실행에서만 낸다")
            open_ = [sid for sid, st in self.stage.items() if st["state"] not in CLOSED]
            if open_:
                return _refuse(f"아직 닫히지 않은 단계: {', '.join(open_)} — routine_step_done 으로 닫는다")
            self.sections_sent = dict(a_.get("sections") or {})
            md = "\n\n".join([f"# {self.title} 보고서"] + [self._section_md(sec) for sec in self.sections]
                             + ["## 근거 부록\n\n" + "\n".join(f"- [{c['call_id']}] {c['backend']}:{c['tool']}" for c in self.ledger)])
            self.state, self.report_md = "done", md
            counts: dict = {}
            for st in self.stage.values():
                counts[st["state"]] = counts.get(st["state"], 0) + 1
            self.report_out = {"run_id": RUN_ID, "coverage": {"counts": counts, "not_ok": []}, "warnings": [], "report_chars": len(md),
                               "ra_upload": self.upload, "report_md": md}
            return _ok(self._ra(dict(self.report_out), a_))
        if name == "deliberate_status":
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return _ok({"job_id": a_.get("job_id"), "job": "mechanism", **st})
        if name == "deliberate_cancel":          # 엔진 delib_jobs.cancel 의 답 꼴(도는 잡은 cancelling)
            self.cancelled.append(a_.get("job_id"))
            return _ok({"job_id": a_.get("job_id"), "status": "cancelling", "note": "취소를 요청했다."})
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}

    def _ra(self, out: dict, a_: dict) -> dict:
        """게이트웨이 _routine_report 의 RA 길 — 이미 만든 초안이 있으면 그 id(다시 만들지 않는다) · 저절로이거나 '끝에 묻는다' 에 upload=true 면
        그 사람 이름으로 만든다 · '끝에 묻는다' 는 ra_next(사람에게 묻는다)."""
        if self.ra_report_id:
            out.update(ra_report_id=self.ra_report_id, ra_note="이 실행의 Report Archive 초안은 이미 있다 — 다시 만들지 않고 그 id 를 돌려준다")
        elif self.upload == "auto" or (self.upload == "ask" and a_.get("upload") is True):
            self.ra_made += 1
            self.ra_report_id = out["ra_report_id"] = "ra-77"
        elif self.upload == "ask":
            out["ra_next"] = "사람에게 Report Archive 초안으로 올릴지 묻고, 올리기로 하면 같은 run_id 로 upload=true 를 붙여 다시 낸다"
        return out

    def _section_md(self, sec) -> str:
        """포털 render_report 의 절 — 인사이트는 실행자가 쓴 글, 단계 절은 실행자 글(없으면 저장한 봉투의 요약 · 상태 줄)에 봉투의 넘긴 키 ·
        발견을 늘 붙인다."""
        text = str((self.sections_sent or {}).get(sec) or "").strip()
        if sec == "insight":
            return f"## 인사이트\n\n{text or '_(쓰지 않았다)_'}"
        st = self.stage.get(sec) or {}
        env = st.get("env") or {}
        body = text or (env.get("summary") or "").strip() or f"_(이 단계는 {st.get('state')})_"
        extra = (["넘긴 키 — " + " · ".join(f"{k}={v}" for k, v in env["keys"].items())] if env.get("keys") else []) + \
            [f"- {f['text']} " + " ".join(f"[{r}]" for r in f.get("refs") or []) for f in env.get("findings") or []]
        return f"## {sec}\n\n{body}" + ("\n\n" + "\n".join(extra) if extra else "")

    def _step_done(self, a_) -> dict:
        """포털 step_done 그대로 — Envelope 검증(넘으면 422) · 호출 없는 ok 는 skipped · 근거 없는 키는 뺀다(성공한 호출 결과에 있고 그 호출 인자에는
        없는 값만 — C5) · 원장에 없는 인용은 뗀다."""
        step = a_.get("step")
        env = {k: a_[k] for k in ("status", "reason", "summary", "keys", "findings", "candidates") if k in a_}
        self.step_done_sent.append((step, env))
        bad = []
        if env.get("status") not in STATES:
            bad.append("status")
        if len(env.get("reason") or "") > 500 or len(env.get("summary") or "") > 6000:
            bad.append("길이")
        if len(env.get("keys") or {}) > 16 or len(env.get("findings") or []) > 20 or len(env.get("candidates") or []) > 20:
            bad.append("개수")
        for f in env.get("findings") or []:
            if set(f) - {"text", "refs"} or not (1 <= len(f.get("text") or "") <= 500) or len(f.get("refs") or []) > 10:
                bad.append("findings")
        if bad:
            return _refuse("단계 봉투가 틀렸다 — " + " / ".join(bad))
        if self.state not in ("running", "waiting"):
            return _refuse(f"실행이 {self.state} 상태다")
        ps = next(s for s in self.plan if s["id"] == step)
        st = self.stage[step]
        if st["state"] not in ("pending", "running", "ambiguous"):
            return _refuse(f"단계 {step} 는 이미 닫혔다({st['state']})")
        calls = [c for c in self.ledger if c["step"] == step]
        sources = [(c["result"], json.dumps(c["args"], ensure_ascii=False)) for c in calls if c["ok"]]
        flags, status = [], env["status"]
        if status in ("ok", "empty") and not calls:
            status = "skipped"
            flags.append("호출 없이 닫으려 했다 — 건너뜀으로 고쳤다")
        if status == "ok" and calls and not any(c["ok"] for c in calls):
            status = "failed"
            flags.append("결과가 다 오류다 — 실패로 고쳤다")
        want, keys, unsourced, echoed = set(ps["expect"]["keys"]), {}, [], []
        for k, v in (env.get("keys") or {}).items():
            v = str(v)[:200]
            if want and k not in want:
                continue
            if v and any(v in res and v not in args for res, args in sources):
                keys[k] = v
            elif v and any(v in c["result"] for c in calls):
                echoed.append(k)
            else:
                unsourced.append(k)
        if unsourced:
            flags.append(f"결과에 글자 그대로 없는 키는 넘기지 않는다: {', '.join(unsourced)}")
        if echoed:
            flags.append(f"받은 인자를 되돌려 준 값 · 실패한 호출에만 있는 키는 넘기지 않는다: {', '.join(echoed)}")
        known = {c["call_id"] for c in self.ledger}
        findings = [{"text": f["text"], "refs": [r for r in f.get("refs") or [] if r in known]} for f in env.get("findings") or []]
        if status == "ambiguous" and not env.get("candidates"):
            status = "failed"
            flags.append("후보 없이 '여럿' 이라 했다 — 실패로 고쳤다")
        st.update(state=status, reason=env.get("reason") or None, flags=flags,
                  env={"status": env["status"], "summary": env.get("summary") or "", "keys": keys, "findings": findings,
                       "candidates": env.get("candidates") or []})
        if status == "empty" and ps["blocked"]["empty"] == "stop":
            self.state, self.reason = "failed", f"단계 {step}({ps['title']}) 결과 없음 — '멈춤' 으로 짠 단계라 실행을 멈췄다"
        elif status == "ambiguous":
            self.state, self.reason = "waiting", f"단계 {step} — 사람이 후보를 고른다"
        elif self.state == "waiting":
            self.state, self.reason = "running", None
        v = self.view()
        nxt = next((s for s in v["steps"] if s["state"] in ("pending", "running", "ambiguous")), None)
        return _ok({"step": step, "state": status, "flags": flags, "run_state": self.state, "next_step": nxt,
                    **({} if nxt else {"report": v["report"]})})


# ── 대본 챗 모델 · 사람의 도구 ────────────────────────────────────────────────────────────────────────────────
_TC_IDS = itertools.count(1)


def _tc(name, cid=None, **args) -> dict:
    return {"name": name, "args": args, "id": cid or f"tc-{next(_TC_IDS)}"}


def _ai(*calls, text="") -> AIMessage:
    return AIMessage(content=text, tool_calls=list(calls))


def _env(status="ok", summary="요약", keys=None, findings=None, candidates=None, reason="") -> str:
    return json.dumps({"status": status, "reason": reason, "summary": summary, "keys": keys or {}, "findings": findings or [],
                       "candidates": candidates or []}, ensure_ascii=False)


class ScriptLLM(BaseChatModel):
    """대본대로 답하는 챗 모델 — 단계 ReAct · 봉투 턴 · 인사이트 턴을 시스템 글로 가른다. 대본 항목은 AIMessage · 글 · 부를 함수(메시지 → 그것들,
    기다릴 수 있다)다. 묶인 도구 이름 · 받은 메시지 · kwargs 를 적는다."""

    react: dict = Field(default_factory=dict)
    envelope: dict = Field(default_factory=dict)
    insight: list = Field(default_factory=list)
    bound: list = Field(default_factory=list)
    descs: list = Field(default_factory=list)     # 묶을 때마다 {이름: 설명}
    seen: list = Field(default_factory=list)      # (kind, step, messages, kwargs)

    @property
    def _llm_type(self) -> str:
        return "routine-script"

    def bind_tools(self, tools, **_kw):
        self.bound.append(sorted(t.name for t in tools))
        self.descs.append({t.name: str(getattr(t, "description", "") or "") for t in tools})
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        raise NotImplementedError("비동기 길만 쓴다")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
        sys_txt = str(messages[0].content) if messages else ""
        human = str(messages[1].content) if len(messages) > 1 else ""
        if "단계 봉투" in sys_txt:
            kind, sid = "envelope", re.search(r"\[단계\] 〔([a-z0-9_]+)〕", human).group(1)
            queue = self.envelope.setdefault(sid, [])
            item = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else _env())
        elif "'인사이트' 절" in sys_txt:
            kind, sid = "insight", None
            item = self.insight.pop(0) if len(self.insight) > 1 else (self.insight[0] if self.insight else "인사이트 본문.")
        else:
            kind, sid = "react", re.search(r"단계 〔([a-z0-9_]+)〕", sys_txt).group(1)
            queue = self.react.setdefault(sid, [])
            item = queue.pop(0) if queue else AIMessage(content=f"{sid} 정리 — 끝.")
        self.seen.append((kind, sid, list(messages), dict(kw)))
        if callable(item):
            item = item(messages)
            if asyncio.iscoroutine(item):
                item = await item
        msg = item if isinstance(item, AIMessage) else AIMessage(content=str(item))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def turns(self, kind, sid=None) -> list:
        return [x for x in self.seen if x[0] == kind and (sid is None or x[1] == sid)]


TMAP = {"search_voc": "signalforge", "voc_trend": "signalforge", "voc_write_note": "signalforge", "failuretrace_search": "failuretrace",
        "plm_get": "plm", "search_tools": "_gateway", "invoke_tool": "_gateway",
        "deliberate_start": "hwax-deliberation", "deliberate_status": "hwax-deliberation", "deliberate_result": "hwax-deliberation"}
DIRECT: list = []          # 사람의 도구를 직접 부르면 여기 남는다(울타리를 건너뛴 것)


def _user_tool(name):
    async def _direct(**kw):
        DIRECT.append((name, kw))
        raise AssertionError(f"{name} 을 직접 불렀다 — routine_call 을 거쳐야 한다")
    return StructuredTool(name=name, description=f"{name} 설명", coroutine=_direct,
                          args_schema={"type": "object", "properties": {"q": {"type": "string"}, "model": {"type": "string"}}})


TOOLS = {n: _user_tool(n) for n in TMAP}


def _app_ns(llm):
    """에이전트 app 대역 — 연결 설정에는 게이트웨이 공유 비밀이 있다(루틴 길에 실리면 안 된다)."""
    return types.SimpleNamespace(state=types.SimpleNamespace(
        connections={"gateway": {"url": "http://gw.test/mcp", "transport": "streamable_http",
                                 "headers": {"Authorization": f"Bearer {GW_TOKEN}"}}},
        llm=llm, chat_llm_cfg=None))


def _req(routine=None, **kw):
    base = dict(message="루틴: 시장불량 분석", conversation_id="conv-1", groups=["연구소"], user_email="Koo@Example.com", user_pat=PAT,
                entitlements=["feat:chat", "feat:routines"], routine=routine if routine is not None else {"id": "rt-1", "inputs": {"model": "S26"}})
    base.update(kw)
    return a.ChatRequest(**base)


@pytest.fixture
def kit(monkeypatch):
    """창은 박아 두고(LLM 서버에 묻지 않는다) · 심의 멈춤 없음 · 진행 조회 간격 0 · 레지스트리 비움."""
    monkeypatch.setattr(a, "_ctx_cache", {"n": 128_000, "src": "server"})
    monkeypatch.setattr(a, "_delib_pause_info", lambda: None)
    monkeypatch.setattr(rt, "ROUTINE_DELIB_POLL_S", 0)
    monkeypatch.setattr(rt, "ROUTINE_MAX_RUNNING", 2)
    monkeypatch.setattr(rt, "ROUTINE_RESULT_CHARS", 0)
    DIRECT.clear()
    rt._RUNS.clear()
    yield monkeypatch
    rt._RUNS.clear()


def _wire(mp, gw, llm, tools=None, tmap=None, sync_seams=False):
    """이음매를 건다 — 게이트웨이 전송 · LLM · 사람의 도구 · /tools-map. sync_seams 면 이음매를 동기 함수로 바꿔 낀다(둘 다 받아야 한다)."""
    mp.setattr(rt, "GW_TRANSPORT", httpx.MockTransport(gw.handle))
    seen = {}

    def _llm(app_):
        return llm, None

    async def _tools(app_, groups, user, user_pat):
        seen["tools"] = (list(groups), user, user_pat)
        return dict(tools if tools is not None else TOOLS)

    def _tools_sync(app_, groups, user, user_pat):
        seen["tools"] = (list(groups), user, user_pat)
        return dict(tools if tools is not None else TOOLS)

    async def _bmap(app_):
        return dict(tmap if tmap is not None else TMAP)

    mp.setattr(rt, "_routine_llm", _llm)
    mp.setattr(rt, "_user_tools", _tools_sync if sync_seams else _tools)
    mp.setattr(rt, "_tool_backends", (lambda app_: dict(tmap if tmap is not None else TMAP)) if sync_seams else _bmap)
    return seen


def _run(app_, req):
    async def go():
        return [fr async for fr in rt.run_routine(app_, req)]
    return asyncio.run(go())


def _run_bounded(app_, req, timeout: float = 15.0):
    """_run 과 같되 시간 상한 안에서 — 기다림의 끝(멈춤 · 포기 · 쉼)을 보는 시험이 쓴다. 그 끝이 빠진 실행기는 매달리는데, 시험은 매달리지
    않고 붉게 끝난다."""
    async def go():
        return [fr async for fr in rt.run_routine(app_, req)]
    return asyncio.run(asyncio.wait_for(go(), timeout))


def _ev(chunks) -> list:
    """(사건, 자료) — 프레임 꼴은 `event: x\\ndata: {json}\\n\\n` 하나씩이어야 한다."""
    out = []
    for c in chunks:
        txt = c.decode()
        assert re.fullmatch(r"event: [a-z_]+\ndata: .+\n\n", txt, re.S) and txt.count("\n\n") == 1, txt[:200]
        head, data = txt.split("\n", 1)
        out.append((head[7:], json.loads(data[6:])))
    return out


def _routine(evs, kind=None) -> list:
    return [d for e, d in evs if e == "routine" and (kind is None or d.get("kind") == kind)]


def _text(evs) -> str:
    return "".join(d["delta"] for e, d in evs if e == "token")


def _reasoning(evs) -> str:
    return "".join(d["delta"] for e, d in evs if e == "reasoning")


def _portal_parse():
    """포털 중계의 SSE 파서(_parse_sse_frame) — 형제 리포 원문에서 떼어 그대로 돌린다(손으로 베끼지 않는다). 곁에 없으면 None."""
    if not PORTAL_ROUTES.is_file():
        return None
    tree = ast.parse(PORTAL_ROUTES.read_text(encoding="utf-8"))
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_parse_sse_frame"), None)
    assert fn is not None, "포털 routes.py 에서 _parse_sse_frame 을 못 찾았다 — 모양이 바뀌었으면 이 시험도 고친다"
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(PORTAL_ROUTES), "exec"), {"Request": object}, ns)
    return ns["_parse_sse_frame"]


# ── 시험 ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _two_steps():
    return [_step("voc", backends=("signalforge",), allowed={"signalforge": ["search_voc", "voc_trend"]}, keys=("model_key",)),
            _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}, uses=("voc",))]


def test_단계_도구는_허용_목록_그대로이고_안내대_도구가_없으며_호출은_전부_routine_call_로_간다(kit):
    gw = FakeGateway(_two_steps(), impls={("signalforge", "search_voc"): lambda x: ("VOC 12건 — 모델 SM-S938 · 스웰링 7건", False)})
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="스웰링"))], "ft": [_ai(_tc("failuretrace_search", model="SM-S938"))]},
                    envelope={"voc": [_env(summary="스웰링 VOC 7건 [voc.c1]", keys={"model_key": "SM-S938"},
                                           findings=[{"text": "스웰링이 VOC 의 절반이다", "refs": ["voc.c1"]}])],
                              "ft": [_env(summary="불량 이력 [ft.c1]", findings=[{"text": "3분기에 몰린다", "refs": ["ft.c1"]}])]},
                    insight=["스웰링 VOC 가 3분기 불량과 겹친다 [voc.c1] [ft.c1]."])
    seen = _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    # 묶인 이름 집합 — 단계마다 허용 목록 그대로(다른 백엔드 · 쓰기 · 안내대 도구 없음). 봉투 · 인사이트 턴은 도구를 묶지 않는다.
    assert llm.bound == [["search_voc", "voc_trend"], ["failuretrace_search"]], llm.bound
    # 백엔드 도구는 이름으로 부르지 않는다 — 전부 routine_call(백엔드:원래 이름 — 노출 이름의 접두를 뗀다)
    assert set(gw.names()) <= {"routine_run_start", "routine_get", "routine_call", "routine_step_done", "routine_report_submit"}
    assert [x["name"] for x in gw.rcalls()] == ["signalforge:search_voc", "failuretrace:search"]
    assert gw.rcalls()[0]["arguments"] == {"q": "스웰링"} and DIRECT == []
    # 새 실행은 게이트웨이가 만든다(D1) — 대화 id · 입력이 실린다
    assert gw.start_args == {"routine_id": "rt-1", "inputs": {"model": "S26"}, "conversation_id": "conv-1"}
    assert seen["tools"] == (["연구소"], "Koo@Example.com", PAT)
    # 사람 PAT 로만 — 연결 설정의 공유 비밀은 한 번도 안 실렸다 · 신원 머리 · 세션은 닫았다
    assert gw.auths and set(gw.auths) == {f"Bearer {PAT}"}
    hdr = gw.log[0][2]
    assert hdr["x-hwax-user"] == "koo@example.com" and hdr["x-hwax-groups"] == "%EC%97%B0%EA%B5%AC%EC%86%8C"
    assert gw.deleted == 1
    # 보고서 — 실행기는 인사이트 절만 보낸다(LLM). 단계 절은 포털이 저장한 봉투(요약 · 넘긴 키 · 발견)로 짓는다 — 두 실행자의 한 정본
    assert gw.sections_sent == {"insight": "스웰링 VOC 가 3분기 불량과 겹친다 [voc.c1] [ft.c1]."}
    assert "## voc\n\n스웰링 VOC 7건 [voc.c1]\n\n넘긴 키 — model_key=SM-S938\n- 스웰링이 VOC 의 절반이다 [voc.c1]" in gw.report_md
    assert gw.report_md.count("스웰링이 VOC 의 절반이다") == 1, "발견은 한 번만 실린다(실행기가 지어 보낸 절에 포털이 봉투 발견을 또 붙이지 않게)"
    assert _text(evs) == gw.report_md
    names = [e for e, _d in evs]
    tail = [(e, d.get("kind")) for e, d in evs[names.index("result") - 1:]]
    assert tail == [("routine", "report"), ("result", None), ("routine", "done"), ("done", None)], tail
    assert _routine(evs, "done")[-1]["state"] == "done" and _routine(evs, "report")[0]["ra_report_id"] == "ra-77"
    # 앞 단계(uses)의 요약 · 키 · 발견(인용 id)이 다음 단계 프롬프트에 실린다
    ft_sys = str(llm.turns("react", "ft")[0][2][0].content)
    assert "스웰링 VOC 7건 [voc.c1]" in ft_sys and "model_key=SM-S938" in ft_sys and "[voc.c1]" in ft_sys
    # 사람이 쓴 글이 없을 때 화면이 보내는 '루틴: <제목>' 은 덧붙인 말이 아니다 — 실행에 남기지도 프롬프트에 싣지도 않는다(C3)
    assert "[사용자가 덧붙인 말]" not in ft_sys and "note" not in gw.start_args


def test_SSE_꼴은_포털_중계가_읽는_꼴이고_routine_프레임마다_run_id_가_있다(kit):
    gw = FakeGateway(_two_steps())
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))], "ft": [_ai(_tc("failuretrace_search", q="b"))]},
                    envelope={"voc": [_env(findings=[{"text": "발견", "refs": ["voc.c1"]}])], "ft": [_env()]})
    _wire(kit, gw, llm)
    chunks = _run(_app_ns(llm), _req())
    evs = _ev(chunks)
    parse = _portal_parse()
    if parse is not None:
        assert [parse(c.decode().rstrip("\n")) for c in chunks] == [(e, d) for e, d in evs]
    assert evs[-1] == ("done", {})
    assert all(d.get("run_id") == RUN_ID for d in _routine(evs))
    plan = _routine(evs, "plan")[0]
    assert set(plan) == {"kind", "run_id", "routine_id", "version_no", "title", "purpose", "state", "resumed", "steps", "report"}
    assert plan["resumed"] is False and plan["state"] == "running"
    assert plan["steps"][0] == {"id": "voc", "title": "단계 voc", "kind": "platform", "state": "pending", "backends": ["signalforge"],
                                "label": "signalforge"}
    assert plan["report"] == {"title": "시장불량 분석 보고서", "sections": ["voc", "ft", "insight"], "upload": "auto"}
    step = _routine(evs, "step")[0]
    assert step == {"kind": "step", "run_id": RUN_ID, "step": "voc", "title": "단계 voc", "state": "running"}
    done = _routine(evs, "step_done")[0]
    assert set(done) == {"kind", "run_id", "step", "state", "reason", "summary", "flags", "calls"} and done["calls"] == 1
    # 단계 글은 '생각' 칸 — 단계마다 머리 줄
    assert "\n\n### 〔voc〕 단계 voc\n" in _reasoning(evs) and "\n\n### 〔ft〕 단계 ft\n" in _reasoning(evs)
    # 도구 호출은 status 시작 · 끝(call 로 짝) + routine_step · routine_call(끝에만), tool 은 노출 이름 · via 는 백엔드 키
    st = [d for e, d in evs if e == "status" and d.get("tool") == "search_voc"]
    assert len(st) == 2 and st[0]["call"] == st[1]["call"] and "routine_call" not in st[0]
    assert (st[0]["routine_step"], st[0]["via"], st[1]["routine_call"], st[1]["ok"]) == ("voc", "signalforge", "voc.c1", True)
    assert st[1]["step"] == "도구 완료: search_voc" and "ms" in st[1] and "ts" in st[1]


def test_plan_프레임의_라벨은_계획_행의_라벨이고_비었거나_없으면_백엔드_키다():
    """포털 plan_steps 가 고를 때의 앱 라벨(label)을 싣는다 — 비지 않았으면 그것(앞뒤 공백은 뗀다). 공백뿐 · 글이 아님 · 없음(옛 포털)이면
    백엔드 키 그대로, 심의 단계는 '심의'."""
    view = {"run": {"id": RUN_ID}, "steps": [
        {"id": "voc", "kind": "platform", "backends": ["signalforge"], "label": " VOC 분석(SignalForge) "},
        {"id": "ft", "kind": "platform", "backends": ["failuretrace", "plm"], "label": "   "},
        {"id": "refs", "kind": "platform", "backends": ["plm"], "label": None},
        {"id": "delib", "kind": "deliberation", "backends": ["hwax-deliberation"]}]}
    labels = [s["label"] for s in rt._plan_frame(view, False)["steps"]]
    assert labels == ["VOC 분석(SignalForge)", "failuretrace · plm", "plm", "심의"], labels


def test_모델이_한_번에_낸_병렬_호출도_게이트웨이에는_하나씩_나간다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc", "voc_trend"]})])
    gw.delay["routine_call"] = 0.03
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", "p1", q="1"), _tc("search_voc", "p2", q="2"), _tc("voc_trend", "p3", q="3"))]},
                    envelope={"voc": [_env(findings=[{"text": "셋", "refs": ["voc.c1", "voc.c2", "voc.c3"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert gw.max_inflight == 1, "병렬 tool_calls 가 게이트웨이에 겹쳐 나갔다(예산 검사와 원장 쓰기 사이의 경합)"
    assert [c["call_id"] for c in gw.ledger] == ["voc.c1", "voc.c2", "voc.c3"]
    ends = [d for e, d in evs if e == "status" and str(d.get("step", "")).startswith("도구 완료")]
    assert sorted(d["routine_call"] for d in ends) == ["voc.c1", "voc.c2", "voc.c3"]
    assert gw.step_done_sent[0][1]["findings"][0]["refs"] == ["voc.c1", "voc.c2", "voc.c3"]


def test_병렬_호출이_예산을_넘기면_거절_뒤에_줄_선_호출은_보내지_않는다(kit):
    """예산 검사(부르기 전)와 원장 쓰기(부른 뒤) 사이의 경합 — 잠금을 기다리던 호출은 앞 호출이 받은 예산 거절을 보고 보내지 않는다."""
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}, calls=1)])
    gw.delay["routine_call"] = 0.02
    llm = ScriptLLM(react={"voc": [_ai(*[_tc("search_voc", f"p{i}", q=str(i)) for i in range(1, 4)])]},
                    envelope={"voc": [_env(findings=[{"text": "하나", "refs": ["voc.c1"]}])]})
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req())
    assert len(gw.rcalls()) == 2 and len(gw.ledger) == 1, "셋째는 둘째가 받은 예산 거절 뒤라 게이트웨이에 가지 않는다"
    outs = sorted(str(m.content) for m in llm.turns("react", "voc")[1][2] if getattr(m, "type", "") == "tool")
    assert outs[0].startswith("ROUTINE_ERROR: 단계 voc 의 호출 예산(1번)") and outs[1].startswith("ROUTINE_ERROR:")
    assert all("더 부르지 말고 정리하라" in o for o in outs[:2]) and outs[2].startswith("[근거 voc.c1] ")


def test_호출_없이_했다고_한_봉투는_보내되_프레임은_포털이_고친_상태다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})])
    llm = ScriptLLM(react={"voc": [AIMessage(content="조회할 필요가 없다 — 다 안다.")]}, envelope={"voc": [_env(status="ok", summary="다 했다")]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert gw.rcalls() == []
    assert gw.step_done_sent[0][1]["status"] == "ok", "실행기가 본 상태는 그대로 보낸다"
    done = _routine(evs, "step_done")[0]
    assert done["state"] == "skipped" and any("호출 없이" in f for f in done["flags"]), done


def test_결과에_글자_그대로_없는_키와_없는_인용은_한_번_다시_묻고_그래도_틀리면_빼고_까닭에_적는다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}, keys=("model_key", "plant"))],
                     impls={("signalforge", "search_voc"): lambda x: ("모델 SM-S938 · 공장 미상", False)})
    bad = _env(keys={"model_key": "SM-S938", "plant": "구미"}, findings=[{"text": "지어낸 인용", "refs": ["zz.c9", "voc.c1"]}])
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="x"))]}, envelope={"voc": [bad, bad]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    turns = llm.turns("envelope", "voc")
    assert len(turns) == 2, "봉투 검사가 틀리면 한 번 다시 묻는다"
    assert "zz.c9" in str(turns[1][2][1].content) and "다시" in str(turns[1][2][1].content)
    # 봉투 턴은 도구 없는 따로 턴 — 생각을 끄고 출력 상한을 건다
    assert turns[0][3]["max_tokens"] == 4096 and turns[0][3]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    sent = gw.step_done_sent[0][1]
    assert sent["keys"] == {"model_key": "SM-S938"}, "결과에 없는 값(구미)은 보내지 않는다"
    assert "결과에 글자 그대로 없는 키는 뺐다: plant" in sent["reason"]
    assert sent["findings"] == [{"text": "지어낸 인용", "refs": ["voc.c1"]}], "이 단계 call_id 가 아닌 인용은 뗀다"
    assert _routine(evs, "step_done")[0]["state"] == "ok" and gw.view()["steps"][0]["keys"] == {"model_key": "SM-S938"}


def test_다시_물어_고친_봉투는_그대로_쓰고_끝내_JSON_을_못_쓰면_코드_봉투다(kit):
    gw = FakeGateway(_two_steps(), impls={("signalforge", "search_voc"): lambda x: ("SM-S938", False)})
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="x"))], "ft": [_ai(_tc("failuretrace_search", q="y"))]},
                    envelope={"voc": [_env(keys={"model_key": "SM-S999"}), _env(keys={"model_key": "SM-S938"})],
                              "ft": ["JSON 이 아니다", "여전히 글이다"]})
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req())
    assert gw.step_done_sent[0][1]["keys"] == {"model_key": "SM-S938"} and "뺐다" not in gw.step_done_sent[0][1]["reason"]
    ft = gw.step_done_sent[1][1]
    assert ft["status"] == "ok" and "코드가" in ft["reason"] and ft["summary"] == "ft 정리 — 끝.", "성공 호출이 있으면 ok · 요약은 AI 메모"


def test_예산_거절_뒤에는_그_단계에서_더_부르지_않는다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}, calls=2)])
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q=str(i))) for i in range(1, 5)]},
                    envelope={"voc": [_env(findings=[{"text": "둘", "refs": ["voc.c1", "voc.c2"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    sent = gw.rcalls("voc")
    assert len(sent) == 3 and len(gw.ledger) == 2, "셋째가 예산 거절을 받으면 넷째는 게이트웨이에 보내지 않는다"
    fourth_in = llm.turns("react", "voc")[3][2]
    refusal = str(fourth_in[-1].content)
    assert "호출 예산(2번)을 다 썼다" in refusal and "더 부르지 말고 정리하라" in refusal
    assert "더 부르지 말고 정리하라" in str(llm.turns("react", "voc")[4][2][-1].content), "넷째는 로컬에서 같은 거절"
    assert _routine(evs, "step_done")[0]["state"] == "ok"
    refused = [d for e, d in evs if e == "status" and d.get("ok") is False and "호출 예산" in d.get("result_preview", "")]
    assert len(refused) == 1 and "routine_call" not in refused[0], "거절은 원장에 없다 — call_id 를 싣지 않는다"


def test_포털이_원장_쓰기에서_실행_상태로_거절하면_그_단계에서_더_부르지_않는다(kit):
    """사람이 멈춤을 누른 사이 — 게이트웨이 앞 검사는 지났고, 안쪽 도구가 돈 뒤 포털 add_call 이 '실행이 cancelled 상태다' 로 거절한다(게이트웨이
    앞 검사 글의 꼬리 '— 도구를 부르지 않는다' 가 없다). 이 글도 멈춤이다 — 같은 단계의 다음 호출은 게이트웨이에 보내지 않고 로컬에서 같은 거절로
    답한다(보내면 안쪽 도구만 돌고 원장에는 남지 않는다)."""
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc", "voc_trend"]})])
    ran = []

    def _cancel_while_running(x):
        ran.append(x)
        gw.state = "cancelled"                  # 안쪽 도구가 도는 사이 사람이 멈춤을 눌렀다
        return "VOC 3건", False

    gw.impls[("signalforge", "search_voc")] = _cancel_while_running
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a")), _ai(_tc("voc_trend", q="b")), _ai(_tc("search_voc", q="c"))]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert len(gw.rcalls("voc")) == 1 and len(ran) == 1 and gw.ledger == [], "포털 거절 뒤의 호출은 게이트웨이에 보내지 않는다"
    outs = [str(m.content) for m in llm.turns("react", "voc")[-1][2] if getattr(m, "type", "") == "tool"]
    assert len(outs) == 3 and outs[0].startswith("ROUTINE_ERROR: 실행이 cancelled 상태다"), outs
    assert all("더 부르지 말고 정리하라" in o for o in outs), "포털 글 그대로 모델에 가고, 뒤의 둘은 로컬에서 같은 거절로 답한다"
    assert _routine(evs, "done")[-1]["state"] == "cancelled" and "routine_report_submit" not in gw.names()


def test_심의_시작이_포털의_실행_상태_거절이면_봉투를_보내지_않고_그_상태로_끝낸다(kit):
    """deliberate_start 도 같다 — 심의가 서는 사이 사람이 멈추면 포털 add_call 이 꼬리 없는 '실행이 cancelled 상태다' 로 거절한다. 실행이 도는
    상태가 아니므로 '시작 실패' 봉투(routine_step_done)도 진행 조회도 보내지 않고 맨 위가 그 상태로 끝낸다."""
    gw = FakeGateway([_step("delib", kind="deliberation", delib={"job": "mechanism", "chair_template": None})])
    real = gw._inner

    async def _cancel_on_start(bk, orig, args):
        out = await real(bk, orig, args)
        gw.state = "cancelled"
        return out

    gw._inner = _cancel_on_start
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert [x["name"] for x in gw.rcalls()] == ["hwax-deliberation:deliberate_start"]
    assert gw.step_done_sent == [] and "deliberate_status" not in gw.names(), "실행이 멈췄다 — 봉투도 진행 조회도 보내지 않는다"
    assert _routine(evs, "done")[-1]["state"] == "cancelled" and evs[-1] == ("done", {})


def test_시간_예산에_닿으면_작은_ReAct_를_멈추고_그때까지의_호출로_닫는다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}, seconds=1.5)])

    async def _stall(_msgs):
        await asyncio.sleep(30)
        return AIMessage(content="닿지 않는다")

    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a")), _stall]},
                    envelope={"voc": [_env(summary="하나 봤다 [voc.c1]", findings=[{"text": "한 건", "refs": ["voc.c1"]}])]})
    _wire(kit, gw, llm)
    t0 = time.monotonic()
    evs = _ev(_run(_app_ns(llm), _req()))
    assert time.monotonic() - t0 < 10
    sent = gw.step_done_sent[0][1]
    assert "시간 예산" in sent["reason"] and sent["findings"][0]["refs"] == ["voc.c1"]
    assert "voc.c1" in str(llm.turns("envelope", "voc")[0][2][1].content), "봉투 턴이 그때까지의 호출을 본다"
    assert _routine(evs, "done")[-1]["state"] == "done"


def test_심의_단계는_start_를_routine_call_로_진행은_deliberate_status_를_직접_결과는_routine_call_로(kit):
    steps = [_step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}),
             _step("delib", kind="deliberation", uses=("ft",), delib={"job": "mechanism", "chair_template": "diagnosis"}, calls=8)]
    gw = FakeGateway(steps, statuses=[{"status": "running", "round": 1, "total_rounds": 3, "last_step": "좌석 발언"},
                                      {"status": "running", "round": 2, "total_rounds": 3, "last_step": "좌석 발언"},
                                      {"status": "done", "round": 3, "total_rounds": 3}])
    llm = ScriptLLM(react={"ft": [_ai(_tc("failuretrace_search", q="x"))]},
                    envelope={"ft": [_env()], "delib": [_env(summary="전해액 분해 가스 [delib.c2]",
                                                             findings=[{"text": "1234.5 ppm", "refs": ["delib.c2"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(message="경쟁사 비교도", routine={"id": "rt-1", "inputs": {"model": "S26"}, "note": "경쟁사 비교도"})))
    order = [(n, x.get("name")) for n, x, _h in gw.log if n in ("routine_call", "deliberate_status")]
    assert order == [("routine_call", "failuretrace:search"), ("routine_call", "hwax-deliberation:deliberate_start"),
                     ("deliberate_status", None), ("deliberate_status", None), ("deliberate_status", None),
                     ("routine_call", "hwax-deliberation:deliberate_result")], order
    start = gw.rcalls("delib")[0]["arguments"]
    assert start["job"] == "mechanism" and start["advanced"] == {"chair_template": "diagnosis"}
    assert "evidence" not in start and "save_report" not in start, "근거 · save_report 는 게이트웨이가 채운다"
    assert start["question"].startswith("S26 스웰링 시장불량") and "[이 단계의 할 일] delib 를 조회한다" in start["question"]
    assert "[사용자가 덧붙인 말] 경쟁사 비교도" in start["question"]
    status_args = [x for n, x, _h in gw.log if n == "deliberate_status"]
    assert status_args[0] == {"job_id": "mechanism-20261030-101010-abc123"}
    assert gw.rcalls("delib")[1]["arguments"] == {"job_id": "mechanism-20261030-101010-abc123"}
    assert gw.stage["delib"]["calls"] == 2, "진행 조회는 원장 · 예산을 쓰지 않는다"
    details = [d.get("detail") for d in _routine(evs, "step") if d["step"] == "delib" and d.get("detail")]
    assert details[:2] == ["라운드 1/3 · 좌석 발언", "라운드 2/3 · 좌석 발언"], details
    assert "1234.5 ppm" in str(llm.turns("envelope", "delib")[0][2][1].content), "봉투 턴이 결정문을 본다"
    assert gw.step_done_sent[1][1]["findings"][0]["refs"] == ["delib.c2"]
    assert _routine(evs, "step_done")[1]["state"] == "ok"


def test_심의가_done_으로_끝나지_않으면_job_id_와_함께_실패로_닫는다(kit):
    steps = [_step("delib", kind="deliberation", delib={"job": "mechanism", "chair_template": None})]
    gw = FakeGateway(steps, statuses=[{"status": "running", "round": 1, "total_rounds": 3}, {"status": "error", "error": "LLM 끊김"}])
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    _ev(_run(_app_ns(llm), _req()))
    assert [x["name"] for x in gw.rcalls()] == ["hwax-deliberation:deliberate_start"], "결과는 읽지 않는다"
    sent = gw.step_done_sent[0][1]
    assert sent["status"] == "failed" and "mechanism-20261030-101010-abc123" in sent["reason"] and "error" in sent["reason"]
    assert "advanced" not in gw.rcalls()[0]["arguments"], "의장 틀이 없으면 advanced 를 싣지 않는다"


def test_고를_것이_있으면_gate_로_끝나고_다음_단계로_가지_않는다(kit):
    gw = FakeGateway([_step("anchor", allowed={"signalforge": ["search_voc"]}, keys=("model_key",)),
                      _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]})],
                     impls={("signalforge", "search_voc"): lambda x: ("후보 — SM-S938 · SM-S936", False)})
    cands = [{"label": "갤럭시 S26 울트라", "key": "SM-S938"}, {"label": "갤럭시 S26+", "key": "SM-S936"}]
    llm = ScriptLLM(react={"anchor": [_ai(_tc("search_voc", q="S26"))]}, envelope={"anchor": [_env(status="ambiguous", candidates=cands)]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert [d["state"] for d in _routine(evs, "step_done")] == ["ambiguous"]
    gate = _routine(evs, "gate")
    assert len(gate) == 1 and gate[0]["step"] == "anchor" and gate[0]["candidates"] == cands and gate[0]["message"]
    assert _routine(evs, "done")[-1]["state"] == "waiting" and evs[-1] == ("done", {})
    assert _text(evs) == gate[0]["message"], "고를 것 안내가 답 글이다(중계가 대화에 남긴다)"
    assert gw.rcalls("ft") == [] and "routine_report_submit" not in gw.names()


def test_이어서는_routine_run_start_run_id_로_열고_끝난_단계를_건너뛴다(kit):
    steps = _two_steps()
    steps[0]["_state"] = "ok"
    gw = FakeGateway(steps, state="running")
    gw.stage["voc"]["env"] = {"status": "ok", "summary": "앞서 본 VOC [voc.c1]", "keys": {"model_key": "SM-S938"},
                              "findings": [{"text": "앞 발견", "refs": ["voc.c1"]}], "candidates": []}
    gw.ledger.append({"call_id": "voc.c1", "step": "voc", "backend": "signalforge", "tool": "search_voc", "args": {}, "ok": True,
                      "result": "SM-S938"})
    llm = ScriptLLM(react={"ft": [_ai(_tc("failuretrace_search", q="y"))]}, envelope={"ft": [_env()]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="이어서")))
    assert [n for n in gw.names() if n.startswith("routine_run")] == ["routine_run_start"]
    assert next(x for n, x, _h in gw.log if n == "routine_run_start") == {"run_id": RUN_ID}
    assert gw.start_args is None, "새 실행을 만들지 않는다"
    assert _routine(evs, "plan")[0]["resumed"] is True
    assert [x["step"] for x in gw.rcalls()] == ["ft"]
    # 앞 프로세스가 닫은 단계의 절도 포털이 저장한 봉투로 짓는다 — 실행기는 인사이트만 보내고, 인사이트 턴은 그 단계의 요약 · 발견을 본다
    assert set(gw.sections_sent) == {"insight"}
    assert "## voc\n\n앞서 본 VOC [voc.c1]\n\n넘긴 키 — model_key=SM-S938\n- 앞 발견 [voc.c1]" in gw.report_md
    assert "앞서 본 VOC [voc.c1]" in str(llm.turns("insight")[0][2][1].content)


@pytest.mark.parametrize("state,expect", [("done", "이미 끝났다"), ("cancelled", "취소"), ("failed", "실패")])
def test_이어서_요청이_끝난_실행이면_다시_돌지_않고_그_상태로_알린다(kit, state, expect):
    gw = FakeGateway(_two_steps(), state=state)
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID})))
    assert "routine_run_start" not in gw.names() and gw.rcalls() == []
    assert _routine(evs, "plan")[0]["resumed"] is True and expect in _text(evs)
    assert _routine(evs, "done")[-1]["state"] == state and evs[-1] == ("done", {})


OLD_JOB = "mechanism-20261029-090000-old111"      # 앞 프로세스가 세운 심의
NEW_JOB = "mechanism-20261030-101010-abc123"      # 가짜 엔진이 새로 세우는 심의(FakeGateway._inner)


def _resumed_delib(statuses) -> FakeGateway:
    """앞 프로세스가 심의 단계를 running 으로 남겼다 — 원장 첫 호출(delib.c1)이 성공한 deliberate_start(OLD_JOB)다."""
    gw = FakeGateway([_step("delib", kind="deliberation", delib={"job": "mechanism", "chair_template": None}, state="running")],
                     state="running", statuses=statuses)
    gw.stage["delib"]["calls"] = 1
    gw.ledger.append({"call_id": "delib.c1", "step": "delib", "backend": "hwax-deliberation", "tool": "deliberate_start",
                      "args": {"question": "q", "job": "mechanism"}, "ok": True,
                      "result": json.dumps({"job_id": OLD_JOB, "status": "queued"}, ensure_ascii=False)})
    return gw


def test_이어서_앞_프로세스가_세운_심의는_다시_세우지_않고_그_잡을_잇는다(kit):
    gw = _resumed_delib([{"status": "running", "round": 2, "total_rounds": 3}, {"status": "running", "round": 3, "total_rounds": 3},
                         {"status": "done"}])
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c2]", findings=[{"text": "전해액 분해 가스", "refs": ["delib.c2"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="이어서")))
    assert [x["name"] for x in gw.rcalls()] == ["hwax-deliberation:deliberate_result"], "두 번째 deliberate_start 를 보내지 않는다"
    assert gw.rcalls()[0]["arguments"] == {"job_id": OLD_JOB}
    assert {x["job_id"] for n, x, _h in gw.log if n == "deliberate_status"} == {OLD_JOB}, "진행은 앞 잡을 직접 본다"
    assert [x for n, x, _h in gw.log if n == "routine_get" and x.get("call_id")][0] == {"run_id": RUN_ID, "call_id": "delib.c1"}
    sent = gw.step_done_sent[0][1]
    assert sent["status"] == "ok" and sent["findings"][0]["refs"] == ["delib.c2"] and "새로 세웠다" not in sent["reason"]
    assert f"앞 프로세스가 세운 심의 {OLD_JOB} 를 잇는다" in _reasoning(evs) and _routine(evs, "done")[-1]["state"] == "done"


@pytest.mark.parametrize("dead", ["error", "cancelled", "interrupted"])
def test_이어서_앞_프로세스의_심의가_끝나_버렸으면_새로_세우고_까닭에_적는다(kit, dead):
    gw = _resumed_delib([{"status": dead}, {"status": "running", "round": 1, "total_rounds": 3}, {"status": "done"}])
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c3]", findings=[{"text": "전해액 분해 가스", "refs": ["delib.c3"]}])]})
    _wire(kit, gw, llm)
    _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID})))
    assert [x["name"] for x in gw.rcalls()] == ["hwax-deliberation:deliberate_start", "hwax-deliberation:deliberate_result"]
    assert [x["job_id"] for n, x, _h in gw.log if n == "deliberate_status"] == [OLD_JOB, NEW_JOB, NEW_JOB]
    assert gw.rcalls()[1]["arguments"] == {"job_id": NEW_JOB}
    sent = gw.step_done_sent[0][1]
    assert sent["status"] == "ok" and f"앞 프로세스가 세운 심의 {OLD_JOB} 가 {dead} 로 끝나 새로 세웠다" in sent["reason"], sent


def test_이어서_앞_프로세스가_부른_호출을_싣고_프롬프트에_보이며_봉투가_인용한다(kit):
    """플랫폼 단계를 running 으로 남겼다 — 원장의 호출(머리 + 첫 쪽)을 이 단계 호출 기록에 싣는다. 작은 ReAct 프롬프트는 '앞 프로세스가 이미
    부른 것' 으로 보이고(여러 쪽이면 routine_page 를 처음부터 묶는다), 봉투 턴은 그 call_id 를 인용하며 키도 그 결과에서 찾는다. 남은 예산은
    게이트웨이가 지킨다."""
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc", "voc_trend"]}, keys=("model_key",), calls=3, state="running")],
                     state="running", impls={("signalforge", "voc_trend"): lambda x: ("분기별 추이 — 3분기 7건", False)})
    gw.PAGE = 40
    gw.stage["voc"]["calls"] = 2
    gw.ledger += [{"call_id": "voc.c1", "step": "voc", "backend": "signalforge", "tool": "search_voc", "args": {"q": "스웰링"}, "ok": True,
                   "result": "VOC 12건 — 모델 SM-S938 · 스웰링 7건 " + "가" * 50},
                  {"call_id": "voc.c2", "step": "voc", "backend": "signalforge", "tool": "voc_trend", "args": {}, "ok": False,
                   "result": "추이 조회 실패 — 기간이 비었다"}]
    llm = ScriptLLM(react={"voc": [_ai(_tc("voc_trend", q="3분기")), _ai(_tc("search_voc", q="또"))]},
                    envelope={"voc": [_env(summary="VOC [voc.c1] · 추이 [voc.c3]", keys={"model_key": "SM-S938"},
                                           findings=[{"text": "스웰링이 3분기에 몰린다", "refs": ["voc.c1", "voc.c3"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="이어서")))
    sys0 = str(llm.turns("react", "voc")[0][2][0].content)
    assert "[앞 프로세스가 이미 부른 것" in sys0 and "[근거 voc.c1] signalforge:search_voc · 성공\nVOC 12건 — 모델 SM-S938" in sys0
    assert "[근거 voc.c2] signalforge:voc_trend · 실패" in sys0 and "호출 3번(앞 프로세스가 2번 썼다 — 남은 것은 1번)" in sys0
    assert "routine_page(call_id=\"voc.c1\", page=2)" in sys0 and llm.bound[0] == ["routine_page", "search_voc", "voc_trend"], llm.bound
    # 남은 예산은 게이트웨이가 지킨다 — 셋째는 원장에 들고, 넷째는 예산 거절(그 뒤 멈춤)
    assert [x["name"] for x in gw.rcalls()] == ["signalforge:voc_trend", "signalforge:search_voc"] and len(gw.ledger) == 3
    env_in = str(llm.turns("envelope", "voc")[0][2][1].content)
    assert "[이 단계의 call_id] voc.c1, voc.c2, voc.c3" in env_in and "### voc.c1 · signalforge:search_voc · 성공" in env_in
    sent = gw.step_done_sent[0][1]
    assert sent["findings"] == [{"text": "스웰링이 3분기에 몰린다", "refs": ["voc.c1", "voc.c3"]}], sent["findings"]
    assert sent["keys"] == {"model_key": "SM-S938"}, "앞 프로세스의 결과에 있는 값도 키가 된다"
    assert _routine(evs, "step_done")[0]["calls"] == 3, "앞 프로세스 몫을 두 번 세지 않는다"


def test_실행_상한에_닿으면_남은_단계를_건너뜀으로_닫고_보고서로_간다(kit):
    steps = [_step("voc", allowed={"signalforge": ["search_voc"]}), _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}),
             _step("refs", backends=("plm",), allowed={"plm": ["get"]})]
    gw = FakeGateway(steps)

    def _then_limit(_msgs):
        gw.limit = "시간 60분"          # 첫 단계 도중 상한에 닿는다
        return AIMessage(content="정리")

    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a")), _then_limit]}, envelope={"voc": [_env()]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert [x["step"] for x in gw.rcalls()] == ["voc"]
    assert [(s, e["status"], e["reason"]) for s, e in gw.step_done_sent[1:]] == [
        ("ft", "skipped", "실행 상한 — 시간 60분"), ("refs", "skipped", "실행 상한 — 시간 60분")]
    assert [d["state"] for d in _routine(evs, "step_done")] == ["ok", "skipped", "skipped"]
    assert "routine_report_submit" in gw.names() and _routine(evs, "done")[-1]["state"] == "done"
    assert set(gw.sections_sent) == {"insight"}, "단계 절은 보내지 않는다 — 건너뛴 단계도 포털이 상태 줄로 채운다"
    assert "## ft\n\n_(이 단계는 skipped)_" in gw.report_md


def test_실행_호출_상한에_닿으면_호출_상한으로_남은_단계를_건너뜀으로_닫는다(kit):
    """포털 _limit 은 원장 줄 수로 호출 상한을 먼저 본다(limit_kind calls) — 첫 단계의 호출로 상한에 닿으면 남은 단계는 부르지 않고 '실행 상한 —
    호출 N번' 으로 닫고 보고서로 간다(시간 상한과 같은 길)."""
    steps = [_step("voc", allowed={"signalforge": ["search_voc"]}), _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]})]
    gw = FakeGateway(steps)
    gw.limits = {"minutes": 60, "calls": 1}
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]}, envelope={"voc": [_env()]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert [x["step"] for x in gw.rcalls()] == ["voc"] and len(gw.ledger) == 1, gw.rcalls()
    assert [(s, e["status"], e["reason"]) for s, e in gw.step_done_sent[1:]] == [("ft", "skipped", "실행 상한 — 호출 1번")]
    assert "routine_report_submit" in gw.names() and _routine(evs, "done")[-1]["state"] == "done"


def test_지켜보는_사이_다른_실행자가_마지막_호출을_쓰면_결정문_읽기도_실행_호출_상한에_걸린다(kit):
    """결정문 읽기의 상한 예외는 시간 상한뿐이다(limit_kind time — R3-design §8, 실행 호출 상한은 그대로). 심의를 지켜보는 사이 같은 원장을 쓰는
    다른 실행자(개인 Claude)가 마지막 호출을 쓰면 결정문 읽기는 호출 상한 거절을 받고 원장은 상한을 넘지 않는다 — 실행기는 단계를 닫고 보고서로 간다."""
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}),
                      _step("delib", kind="deliberation", delib={"job": "mechanism", "chair_template": None})],
                     statuses=[{"status": "running"}, {"status": "done"}])
    gw.limits = {"minutes": 60, "calls": 3}
    real = gw.tool

    async def _tool(name, args):
        if name == "deliberate_status" and len(gw.ledger) == 2:      # 시작까지 두 줄 — 다른 실행자의 routine_call 이 셋째 줄을 쓴다
            gw.stage["delib"]["calls"] += 1
            gw.ledger.append({"call_id": "delib.c2", "step": "delib", "backend": "hwax-deliberation", "tool": "deliberate_status",
                              "args": {"job_id": args.get("job_id")}, "ok": True, "result": "{}"})
        return await real(name, args)

    gw.tool = _tool
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert len(gw.ledger) == 3 and "deliberate_result" not in [c["tool"] for c in gw.ledger], [c["call_id"] for c in gw.ledger]
    assert dict(gw.step_done_sent)["delib"]["status"] != "ok", "결정문 없이 심의 단계를 ok 로 닫았다"
    assert "routine_report_submit" in gw.names() and _routine(evs, "done")[-1]["state"] == "done"


def test_쪽이_여럿인_결과가_나오면_routine_page_를_더_묶고_읽은_쪽의_값도_키가_된다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc", "voc_trend"]}, keys=("model_key",))],
                     impls={("signalforge", "search_voc"): lambda x: ("가" * 70 + "모델 SM-S938 끝", False)})
    gw.PAGE = 50
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a")), _ai(_tc("routine_page", call_id="voc.c1", page=2))]},
                    envelope={"voc": [_env(keys={"model_key": "SM-S938"})]})
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req())
    assert llm.bound == [["search_voc", "voc_trend"], ["routine_page", "search_voc", "voc_trend"]], llm.bound
    first = str(llm.turns("react", "voc")[1][2][-1].content)
    assert first.startswith("[근거 voc.c1] ") and "routine_page(call_id=\"voc.c1\", page=2)" in first
    reads = [x for n, x, _h in gw.log if n == "routine_get" and x.get("call_id")]
    assert reads == [{"run_id": RUN_ID, "call_id": "voc.c1", "page": 2}]
    assert str(llm.turns("react", "voc")[2][2][-1].content).startswith("[근거 voc.c1 · 2/2쪽] ")
    assert gw.step_done_sent[0][1]["keys"] == {"model_key": "SM-S938"}, "둘째 쪽에만 있는 값 — 읽은 쪽도 대조한다"
    assert len(gw.ledger) == 1, "쪽 읽기는 원장 호출이 아니다"


def test_도구_결과는_근거_머리를_달고_상한에서_자르면_그렇다고_적는다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})],
                     impls={("signalforge", "search_voc"): lambda x: ("나" * 5000, False)})
    kit.setattr(rt, "ROUTINE_RESULT_CHARS", 1000)
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]})
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req())
    shown = str(llm.turns("react", "voc")[1][2][-1].content)
    assert shown.startswith("[근거 voc.c1] " + "나" * 1000) and "5,000자 가운데 앞 1,000자만" in shown and "원장 voc.c1" in shown


def test_유령_ID_는_게이트웨이에_보내지_않고_결과에서_본_ID_는_보낸다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})],
                     impls={("signalforge", "search_voc"): lambda x: ("목록 — test_id 4417", False)})
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="목록")), _ai(_tc("search_voc", test_id=9999)),
                                   _ai(_tc("search_voc", test_id=4417))]})
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req())
    assert [x["arguments"] for x in gw.rcalls()] == [{"q": "목록"}, {"test_id": 4417}], "지어낸 9999 는 보내지 않는다 · 결과의 4417 은 간다"
    assert a._PHANTOM_ID_MARK in str(llm.turns("react", "voc")[2][2][-1].content)


def test_살아_있는_실행에_다시_붙으면_plan_과_기록을_받고_작업은_하나다_브라우저가_떠나도_돈다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})])
    hold = {}

    async def _wait(_msgs):
        await hold["ev"].wait()
        return _ai(_tc("search_voc", q="a"))

    llm = ScriptLLM(react={"voc": [_wait]}, envelope={"voc": [_env(findings=[{"text": "f", "refs": ["voc.c1"]}])]})
    _wire(kit, gw, llm)

    async def go():
        hold["ev"] = asyncio.Event()
        first = rt.run_routine(_app_ns(llm), _req())
        got1 = []
        async for fr in first:
            got1.append(fr)
            if b'"kind": "step"' in fr:
                break
        await first.aclose()                       # 첫 브라우저가 떠났다 — 작업은 돈다
        assert rt.active_count() == 1
        assert rt._RUNS[RUN_ID].subs == set(), "떠난 구독은 바로 빠진다(GC 를 기다리며 큐가 프레임을 쌓지 않는다)"
        other = [fr async for fr in rt.run_routine(_app_ns(llm), _req(routine={"run_id": RUN_ID}, user_email="else@x.com"))]
        second = rt.run_routine(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="이어서"))
        got2 = [await second.__anext__()]
        hold["ev"].set()
        got2 += [fr async for fr in second]
        await asyncio.sleep(0)
        return got1, other, got2

    got1, other, got2 = asyncio.run(go())
    assert _ev(other) == [("error", {"code": "routine_gateway", "message": "실행을 찾을 수 없다"}), ("done", {})], "남의 실행에는 붙지 않는다"
    e2 = _ev(got2)
    assert e2[0][0] == "routine" and e2[0][1]["kind"] == "plan" and e2[0][1]["resumed"] is True, "다시 붙기는 지금 원장 모양의 plan 부터"
    assert [d["kind"] for d in _routine(e2)][:2] == ["plan", "step"], "그다음 지금까지 낸 기록(옛 plan 은 다시 보내지 않는다)"
    assert len(_routine(e2, "plan")) == 1 and len(_routine(_ev(got1), "plan")) == 1
    assert _routine(e2, "done")[-1]["state"] == "done" and e2[-1] == ("done", {})
    assert gw.names().count("routine_run_start") == 1 and len(gw.rcalls()) == 1, "작업은 하나 — 두 탭이 같은 실행을 두 번 돌지 않는다"
    assert gw.report_md and rt.active_count() == 0


def test_다시_붙기_기록에는_큰_칸을_빼고_처음_구독은_다_받는다():
    """넓은 창의 result_full 은 한 칸이 15만 자까지다 — 400 프레임 기록이 실행마다 수십 MB 가 되지 않게 기록 사본에서만 뺀다."""
    live = rt._Live("u@x.com", RUN_ID)
    ctx = rt._Ctx(types.SimpleNamespace(state=None), _req(), live)
    q: asyncio.Queue = asyncio.Queue()
    live.subs.add(q)
    big = {"step": "도구 완료: search_voc", "tool": "search_voc", "result_preview": "가" * 10, "result_full": "가" * 5000}
    ctx.emit("status", big, slim=("result_full",))
    ctx.emit("status", {"step": "짧다", "tool": "x"}, slim=("result_full",))
    assert _ev([q.get_nowait()]) == [("status", big)], "구독자는 다 받는다"
    hist = _ev(list(live.history))
    assert hist[0] == ("status", {k: v for k, v in big.items() if k != "result_full"}) and hist[1][1] == {"step": "짧다", "tool": "x"}


def test_동시_상한을_넘으면_routine_busy_로_거절하고_멈춤_중이면_delib_paused_로_거절한다(kit):
    kit.setattr(rt, "ROUTINE_MAX_RUNNING", 1)
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})])
    hold = {}

    async def _wait(_msgs):
        await hold["ev"].wait()
        return AIMessage(content="끝")

    llm = ScriptLLM(react={"voc": [_wait]})
    _wire(kit, gw, llm)

    async def go():
        hold["ev"] = asyncio.Event()
        first = rt.run_routine(_app_ns(llm), _req())
        async for fr in first:
            if b'"kind": "step"' in fr:
                break
        busy = [fr async for fr in rt.run_routine(_app_ns(llm), _req())]
        load = a._delib_load()[0], a._routine_health(), a._delib_human()
        hold["ev"].set()
        rest = [fr async for fr in first]
        return busy, load, rest

    busy, load, rest = asyncio.run(go())
    assert _ev(busy) == [("error", {"code": "routine_busy", "message": _ev(busy)[0][1]["message"], "retryable": True,
                                    "retry_after_s": 60}), ("done", {})]
    assert load == (1, {"routine_active": 1, "routine_parked": 0, "routine_max": 1}, 0), "delib_active 에는 더하고 카드 대조 반감에는 안 센다"
    assert _ev(rest)[-1] == ("done", {}) and gw.names().count("routine_run_start") == 1
    import delib_jobs  # noqa: PLC0415
    info = {"on": True, "scope": "delib", "until": None, "retry_after_s": 300, "reason": "update-all", "by": "update-all"}
    kit.setattr(a, "_delib_pause_info", lambda: info)
    paused = _ev(_run(_app_ns(llm), _req()))
    assert paused == [("error", {"code": "delib_paused", "message": delib_jobs.pause_message(info), "retry_after_s": 300,
                                 "infra": True, "retryable": True}), ("done", {})]
    assert gw.names().count("routine_run_start") == 1


def test_사람_PAT_가_없거나_꼴이_틀리면_게이트웨이를_부르지_않고_거절한다(kit):
    gw = FakeGateway(_two_steps())
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    no_pat = _ev(_run(_app_ns(llm), _req(user_pat="")))
    assert [e for e, _d in no_pat] == ["error", "done"] and no_pat[0][1]["code"] == "routine_no_pat"
    for bad in ({}, {"id": ""}, {"inputs": {"a": 1}}, {"id": "r", "version": 0}, {"id": "r", "inputs": [1]}, {"run_id": 5},
                {"id": "r", "note": 5}, {"id": "r", "note": "가" * 2001}, {"run_id": RUN_ID, "upload_ra": "true"}):
        got = _ev(_run(_app_ns(llm), _req(routine=bad)))
        assert [e for e, _d in got] == ["error", "done"] and got[0][1]["code"] == "routine_bad_request", bad
    assert gw.log == [] and gw.auths == []


def test_사람_토큰이_거절되면_서비스_계정으로_떨어지지_않고_멈춘다(kit):
    gw = FakeGateway(_two_steps(), pat="another-pat")             # 이 PAT 는 401(만료 · 폐기)
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert [e for e, _d in evs] == ["error", "done"], "실행 id 를 모를 때(시작 실패)는 error 만"
    assert evs[0][1]["code"] == "routine_gateway" and "401" in evs[0][1]["message"]
    assert gw.auths and all(x == f"Bearer {PAT}" for x in gw.auths), "공유 비밀(GW_TOKEN)로 다시 붙지 않는다"


def test_도구_목록을_사람_토큰으로_못_받으면_서비스_계정으로_다시_받지_않고_running_으로_멈춘다(kit):
    gw = FakeGateway(_two_steps())
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    kit.setattr(rt, "_user_tools", _ORIG_USER_TOOLS)   # 진짜 로더(app._with_groups → app._get_tools_retry) — 어댑터만 대역
    seen = []

    class _Stub:                                       # 어댑터 대역 — 받은 연결 설정을 적고 401 로 실패한다
        def __init__(self, conns):
            seen.append(conns)

        async def get_tools(self):
            raise RuntimeError("Client error '401 Unauthorized' for url 'http://gw.test/mcp'")

    kit.setattr(a, "MultiServerMCPClient", _Stub)
    evs = _ev(_run(_app_ns(llm), _req()))
    err = [d for e, d in evs if e == "error"]
    assert len(err) == 1 and err[0]["code"] == "routine_gateway" and "서비스 계정" in err[0]["message"]
    assert _routine(evs, "done")[-1]["state"] == "running" and evs[-1] == ("done", {}), "원장은 남는다 — 이어서 가능"
    assert len(seen) == 1 and seen[0]["gateway"]["headers"]["Authorization"] == f"Bearer {PAT}"
    assert all(GW_TOKEN not in json.dumps(c, default=str) for c in seen)


def test_내_도구_목록이_비면_단계를_건너뜀으로_닫지_않고_running_으로_멈춘다(kit):
    """게이트웨이가 막 떠 도구 0 인 과도 상태(_get_tools_retry 는 마지막 시도의 빈 목록을 그대로 돌려준다) — 열린 단계를 다 skipped 로
    닫고 보고서를 내면 실행이 done 이 되어 '이어서' 를 못 한다. /tools-map 이 빌 때와 같이 멈춘다."""
    gw = FakeGateway(_two_steps())
    llm = ScriptLLM()
    _wire(kit, gw, llm, tools={})
    evs = _ev(_run(_app_ns(llm), _req()))
    err = [d for e, d in evs if e == "error"]
    assert len(err) == 1 and err[0]["code"] == "routine_gateway" and "도구 목록이 비었다" in err[0]["message"], err
    assert _routine(evs, "done")[-1]["state"] == "running" and evs[-1] == ("done", {}), "원장은 남는다 — 이어서 가능"
    assert gw.step_done_sent == [] and "routine_report_submit" not in gw.names() and gw.state == "running"
    assert llm.seen == [], "모델을 부르지 않는다"


def test_실행기_오류는_error_와_안내_글_running_끝으로_닫는다(kit):
    gw = FakeGateway(_two_steps())
    llm = ScriptLLM()
    _wire(kit, gw, llm)

    def _boom(*_a, **_k):
        raise ValueError("프롬프트를 못 지었다")

    kit.setattr(rt, "_step_prompt", _boom)
    evs = _ev(_run(_app_ns(llm), _req()))
    names = [e for e, _d in evs]
    assert names[-5:] == ["error", "token", "result", "routine", "done"], names
    assert evs[-5][1]["code"] == "routine_internal" and "ValueError" in evs[-5][1]["message"]
    assert _routine(evs, "done")[-1] == {"kind": "done", "run_id": RUN_ID, "state": "running",
                                         "reason": _routine(evs, "done")[-1]["reason"]}
    assert "이어서" in _text(evs) and rt.active_count() == 0 and gw.deleted == 1


@pytest.mark.parametrize("second,expect", [("셀 스웰링 [voc.c1].", "셀 스웰링 [voc.c1]."),
                                           ("셀 스웰링 9999.9 와 8888.8 [voc.c1].", "셀 스웰링 7777.7 [voc.c1].")])
def test_인사이트에_출처_없는_수치가_있으면_한_번_고쳐_쓰고_더_나쁘면_첫_글을_쓴다(kit, second, expect):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})],
                     impls={("signalforge", "search_voc"): lambda x: ("VOC 12건", False)})
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]}, insight=["셀 스웰링 7777.7 [voc.c1].", second])
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req())
    assert len(llm.turns("insight")) == 2
    assert "7777.7" in str(llm.turns("insight")[1][2][1].content), "고쳐 쓰기는 출처 없는 수치 목록을 준다"
    assert gw.sections_sent["insight"] == expect


def test_이음매를_동기_함수로_바꿔_끼워도_돈다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})])
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]})
    _wire(kit, gw, llm, sync_seams=True)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert _routine(evs, "done")[-1]["state"] == "done" and len(gw.rcalls()) == 1


def test_게이트웨이_세션이_만료되면_한_번_다시_연다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})])
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]})
    _wire(kit, gw, llm)
    real = gw.tool

    async def _restart_once(name, args):
        if name == "routine_call" and len(gw.sessions) == 1:
            gw.sessions.clear()                # 게이트웨이 재기동 — 옛 세션 id 는 404
            gw.sessions.append("sess-dead")
        return await real(name, args)

    gw.tool = _restart_once
    evs = _ev(_run(_app_ns(llm), _req()))
    assert _routine(evs, "done")[-1]["state"] == "done"
    assert len(gw.sessions) == 2 and len(gw.ledger) == 1


def test_chat_끝점은_routine_이_있으면_슬래시보다_먼저_루틴_가지로_가고_ping_감싸개를_쓰지_않는다(monkeypatch):
    seen = {}

    def _no_ping(gen):
        seen["pinged"] = True
        return gen

    async def _fake_run(app_, req):
        seen["routine"] = req.routine
        yield a._sse("done", {})

    monkeypatch.setattr(a, "_pinged_stream", _no_ping)
    monkeypatch.setattr(rt, "run_routine", _fake_run)
    monkeypatch.setattr(a, "_delib_pause_info", lambda: None)

    async def body(resp):
        return [c async for c in resp.body_iterator]

    r = asyncio.run(a.chat(a.ChatRequest(message="/심의 낙하", routine={"id": "rt-1"}, entitlements=["feat:routines"])))
    assert asyncio.run(body(r)) == [a._sse("done", {})]
    assert seen == {"routine": {"id": "rt-1"}}, "루틴 가지가 슬래시보다 먼저고 _pinged_stream 으로 감싸지 않는다(브라우저가 떠나도 돈다)"
    seen.clear()
    denied = asyncio.run(a.chat(a.ChatRequest(message="x", routine={"id": "rt-1"}, entitlements=["feat:chat"])))
    text = "".join(c.decode() for c in asyncio.run(body(denied)))
    assert "'분석 루틴' 을(를) 쓸 권한이 없습니다" in text and seen == {}, "feat:routines 가 없으면 실행기를 부르지 않는다"
    assert a._denied_feature(a.ChatRequest(message="x", routine={"id": "r"})) is None, "entitlements 가 없는 옛 호출은 막지 않는다"
    assert a._denied_feature(a.ChatRequest(message="x", routine={}, entitlements=[])) == "feat:routines"
    assert a._denied_feature(a.ChatRequest(message="x", entitlements=[])) is None, "루틴이 아닌 턴은 그대로"


def test_health_가_도는_루틴을_따로_싣고_delib_active_에도_더한다(monkeypatch, tmp_path):
    from fakes.llm import FakeLLM  # noqa: PLC0415

    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    rt._RUNS.clear()
    live = rt._Live("u@x.com", RUN_ID)
    live.task = types.SimpleNamespace(done=lambda: False)
    rt._RUNS[RUN_ID] = live
    try:
        h = TestClient(a.app).get("/health").json()
    finally:
        rt._RUNS.clear()
    assert (h["routine_active"], h["routine_max"]) == (1, rt.ROUTINE_MAX_RUNNING)
    assert h["delib_active"] == 1, "update-all · start.sh 가 고침 없이 루틴을 기다린다"
    assert h["card_review"]["delib_human"] == 0, "카드 대조 반감에는 안 센다(루틴은 챗 LLM 이다)"


# ── 심의 단계의 견고함(R3-design §8 — 반대 검토 뒤: 잠깐의 거절 · 쉼 · 끊김 · 멈춤 · 이어서 · 상한) ─────────────────────────────
JOB2 = "mechanism-20261030-090000-second"          # 앞 잡이 죽어 새로 세운 둘째 심의
_PAUSE = {"on": True, "scope": "delib", "until": None, "retry_after_s": 2400, "reason": "update-all", "by": "update-all"}
_QUEUE_FULL = ("Error executing tool deliberate_start: 동시 실행 상한에 걸렸고(전역 2건 — DELIB_JOB_MAX_RUNNING) 대기열도 찼다(20/20건 — "
               "DELIB_JOB_QUEUE_MAX) — 내 진행 중 0건 · 내 대기 0건 · 전체 진행 2/2. 줄이 빠진 뒤 다시 하라.")


def _pause_text(retry_after_s: int) -> str:
    """엔진의 새 심의 멈춤 거절 — delib_jobs.pause_message 를 FastMCP 가 도구 오류로 싼 글 그대로."""
    import delib_jobs  # noqa: PLC0415
    return "Error executing tool deliberate_start: " + delib_jobs.pause_message({"retry_after_s": retry_after_s, "reason": "update-all"})


def _delib_gw(**kw) -> FakeGateway:
    return FakeGateway([_step("delib", kind="deliberation", delib={"job": "mechanism", "chair_template": None}, calls=kw.pop("calls", 8))],
                       **kw)


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _probe_views(gw, on_view=None) -> list:
    """실행 보기 읽기(routine_get · call_id 없음)마다 (도는 실행 수, 쉬는 실행 수)를 적는다 — on_view 가 있으면 그 읽기의 답을 짓기 전에 부른다."""
    seen: list = []
    real = gw.tool

    async def _tool(name, args):
        if name == "routine_get" and not args.get("call_id"):
            seen.append((rt.active_count(), rt.parked_count()))
            if on_view is not None:
                on_view()
        return await real(name, args)

    gw.tool = _tool
    return seen


def _paused_mid_run(kit):
    """앞 단계(voc)가 도는 사이 update-all 이 새 심의를 멈춘다 — 그다음이 심의 단계다. (게이트웨이, 멈춤 스위치). 엔진도 멈춤을 지킨다(멈춘
    채 부르면 deliberate_start 가 도구 오류로 거절한다 — 원장에는 실패 한 줄)."""
    paused = {"on": False}
    kit.setattr(a, "_delib_pause_info", lambda: _PAUSE if paused["on"] else None)

    def _voc(_x):
        paused["on"] = True
        return "VOC 3건", False

    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}),
                      _step("delib", kind="deliberation", delib={"job": "mechanism", "chair_template": None})],
                     impls={("signalforge", "search_voc"): _voc})
    real = gw._inner

    async def _engine(bk, orig, args):
        if orig == "deliberate_start" and paused["on"]:
            return _pause_text(2400), True
        return await real(bk, orig, args)

    gw._inner = _engine
    return gw, paused


@pytest.mark.parametrize("busy", ["pause", "queue"])
def test_심의_시작이_엔진의_잠깐_거절이면_실패로_닫지_않고_쉬었다가_다시_세운다(kit, busy):
    """새 심의 멈춤(update-all 이 건다 — 도는 루틴이 delib_active 에 들어 멈춤을 부른다) · 줄 가득은 잠깐의 거절이다. 단계를 failed 로 닫으면
    실행이 심의 없이 done 으로 끝나고(RA 초안까지) 그 실행은 이을 수 없다. 쉬었다(엔진의 retry_after_s, 없으면 60초에서) 다시 세우고, 쉬는
    동안은 도는 실행으로 세지 않는다(update-all 의 기다림이 수렴하게)."""
    kit.setattr(rt, "_DELIB_RETRY_S", 0.3)
    gw = _delib_gw()
    real = gw._inner
    refused: list = []

    async def _busy_once(bk, orig, args):
        if orig == "deliberate_start" and not refused:
            refused.append(1)
            return (_pause_text(1) if busy == "pause" else _QUEUE_FULL), True
        return await real(bk, orig, args)

    gw._inner = _busy_once
    seen = _probe_views(gw)
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c3]", findings=[{"text": "전해액 분해 가스", "refs": ["delib.c3"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert [x["name"] for x in gw.rcalls()] == ["hwax-deliberation:deliberate_start"] * 2 + ["hwax-deliberation:deliberate_result"]
    assert [c["ok"] for c in gw.ledger] == [False, True, True], "거절된 시작은 원장에 실패 한 줄로 남고, 다시 세운 시작이 잇는다"
    assert gw.step_done_sent[0][1]["status"] == "ok" and _routine(evs, "step_done")[0]["state"] == "ok", gw.step_done_sent
    details = [d.get("detail") for d in _routine(evs, "step") if d.get("detail")]
    assert "심의가 잠시 멈춰 있다 — 1초 뒤 다시" in details, details
    assert (0, 1) in seen, f"쉬는 동안은 도는 실행으로 세지 않는다 — {seen}"
    assert _routine(evs, "done")[-1]["state"] == "done" and rt.active_count() == rt.parked_count() == 0


def test_이_프로세스의_새_심의_멈춤이면_부르지_않고_쉬다가_풀리는_대로_세운다(kit):
    """심의 엔진은 이 프로세스에 붙어 있다 — 멈춤이 걸린 채 부르면 거절되고 원장 한 줄 · 예산 하나만 든다. 부르지 않고 쉬다가 멈춤이 풀리면
    retry_after_s(멈춤이 저절로 풀리는 때 — update-all 은 2,400초를 건다)를 기다리지 않고 곧바로 세운다."""
    gw, paused = _paused_mid_run(kit)
    parked_reads: list = []

    def _lift():
        if rt.parked_count():
            parked_reads.append(1)
            if len(parked_reads) >= 3:
                paused["on"] = False       # update-all 이 끝나 멈춤을 내렸다

    seen = _probe_views(gw, _lift)
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]},
                    envelope={"delib": [_env(summary="결정 [delib.c2]", findings=[{"text": "가스", "refs": ["delib.c2"]}])]})
    _wire(kit, gw, llm)
    t0 = time.monotonic()
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert time.monotonic() - t0 < 10, "retry_after_s(2,400초)를 기다리지 않고 멈춤이 풀리는 대로 세운다"
    starts = [c for c in gw.ledger if c["tool"] == "deliberate_start"]
    assert len(starts) == 1 and starts[0]["ok"], "멈춘 동안은 부르지 않는다 — 거절된 시작이 원장에 없다"
    details = [d.get("detail") for d in _routine(evs, "step") if d["step"] == "delib" and d.get("detail")]
    assert details[0] == "심의가 잠시 멈춰 있다 — 2400초 뒤 다시", details
    assert (0, 1) in seen and len(parked_reads) >= 3, seen
    assert [d["state"] for d in _routine(evs, "step_done")] == ["ok", "ok"] and _routine(evs, "done")[-1]["state"] == "done"


@pytest.mark.parametrize("ending", ["limit", "cancel"])
def test_쉬는_사이_실행_상한에_닿거나_사람이_멈추면_심의를_세우지_않고_끝낸다(kit, ending):
    """기다림은 실행 상한까지다 — 상한에 닿으면 심의 단계를 실행 상한 건너뜀으로 닫고 보고서로 간다(_close_by_limit 과 같은 꼴). 쉬는 사이 사람이
    멈추면 세운 심의가 없으니 접을 것도 없이 그 상태로 끝난다."""
    gw, _paused = _paused_mid_run(kit)

    def _end():
        if rt.parked_count():
            if ending == "limit":
                gw.limit = "시간 60분"
            else:
                gw.state = "cancelled"

    _probe_views(gw, _end)
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert not [c for c in gw.ledger if c["tool"] == "deliberate_start"] and gw.cancelled == []
    assert not [d for e, d in evs if e == "error"], [d for e, d in evs if e == "error"]
    if ending == "limit":
        sent = dict(gw.step_done_sent)["delib"]
        assert sent["status"] == "skipped" and "실행 상한 — 시간 60분" in sent["reason"] and "세우지 못한 채" in sent["reason"], sent
        assert _routine(evs, "done")[-1]["state"] == "done" and "routine_report_submit" in gw.names()
    else:
        assert [s for s, _e in gw.step_done_sent] == ["voc"] and "routine_report_submit" not in gw.names()
        assert _routine(evs, "done")[-1]["state"] == "cancelled" and evs[-1] == ("done", {})


@pytest.mark.parametrize("cap,starts", [(2, False), (3, True)])
def test_실행_호출_상한에_시작과_결정문_읽기_몫이_없으면_심의를_세우지_않는다(kit, cap, starts):
    """결정문 읽기는 단계 예산에서는 빠지지만 실행 호출 상한은 그대로다 — 하나만 남은 채 세우면 몇 시간의 GLM 을 쓰고 결정문을 못 읽는다
    (save_report 는 거짓이라 RA 에도 없다). 둘이 남으면 세운다."""
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}),
                      _step("delib", kind="deliberation", delib={"job": "mechanism", "chair_template": None})])
    gw.limits = {"minutes": 60, "calls": cap}
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]},
                    envelope={"delib": [_env(summary="결정 [delib.c2]", findings=[{"text": "가스", "refs": ["delib.c2"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    names = [x["name"] for x in gw.rcalls()]
    sent = dict(gw.step_done_sent)["delib"]
    if starts:
        assert names[1:] == ["hwax-deliberation:deliberate_start", "hwax-deliberation:deliberate_result"] and sent["status"] == "ok"
    else:
        assert names == ["signalforge:search_voc"], "시작을 부르지 않는다"
        assert sent["status"] == "skipped" and "실행 호출 상한에 1번만 남아" in sent["reason"], sent
    assert _routine(evs, "done")[-1]["state"] == "done"


def test_결정문_읽기는_단계_예산에서_빠져_예산을_시작에_다_써도_모든_쪽을_읽는다(kit):
    """심의 단계의 결정문 읽기(deliberate_result · 쪽 포함)는 단계 호출 예산에서 빠진다(R3-design §8) — 예산 1번이 시작에 들어도 결정문을
    끝까지 읽는다. 실행기가 단계 예산으로 쪽 읽기를 먼저 끊으면 결정문 뒤쪽이 봉투에서 빠진다."""
    gw = _delib_gw(calls=1)
    real = gw._inner

    async def _two_parts(bk, orig, args):
        if orig == "deliberate_result":
            part = int(args.get("part") or 1)
            text = "결정 앞쪽 — 셀 스웰링의 1차 원인" if part == 1 else "결정 뒤쪽 — 대책은 벤트 설계 변경(근거 4321.0 ppm)"
            return json.dumps({"job_id": args.get("job_id"), "status": "done", "decision": text, "page": {"part": part, "parts": 2}},
                              ensure_ascii=False), False
        return await real(bk, orig, args)

    gw._inner = _two_parts
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c2] [delib.c3]", findings=[{"text": "벤트", "refs": ["delib.c3"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert [(x["name"], x["arguments"].get("part")) for x in gw.rcalls()][1:] == [
        ("hwax-deliberation:deliberate_result", None), ("hwax-deliberation:deliberate_result", 2)], gw.rcalls()
    env_in = str(llm.turns("envelope", "delib")[0][2][1].content)
    assert "4321.0 ppm" in env_in and "까지만 읽었다" not in env_in, "결정문 뒤쪽까지 봉투 턴이 본다"
    assert _routine(evs, "step_done")[0]["state"] == "ok"


@pytest.mark.parametrize("blips", [1, 2])
def test_심의를_기다리는_사이_실행_보기가_잠깐_끊겨도_기다림을_잇고_결정문을_읽는다(kit, blips):
    """포털 · 게이트웨이 재기동의 몇 초 — 실행 보기 읽기 한두 번의 실패로 몇 시간짜리 심의의 실행기를 멈추지 않는다(잇달아 세 번이면 멈춘다)."""
    gw = _delib_gw(statuses=[{"status": "running"}] * 3 + [{"status": "done"}])
    real = gw.tool
    left = {"n": blips}

    async def _blip(name, args):
        if name == "routine_get" and not args.get("call_id") and "deliberate_status" in gw.names() and left["n"]:
            left["n"] -= 1
            return _refuse("포털에 닿지 못했다(ConnectError)")
        return await real(name, args)

    gw.tool = _blip
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c2]", findings=[{"text": "가스", "refs": ["delib.c2"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert left["n"] == 0 and not [d for e, d in evs if e == "error"], [d for e, d in evs if e == "error"]
    assert [x["name"] for x in gw.rcalls()][-1] == "hwax-deliberation:deliberate_result"
    assert _routine(evs, "step_done")[0]["state"] == "ok" and _routine(evs, "done")[-1]["state"] == "done"


@pytest.mark.parametrize("status_too", [False, True])
def test_실행_보기가_잇달아_세_번_안_읽히면_running_으로_멈추고_단계를_실패로_닫지도_심의를_접지도_않는다(kit, status_too):
    """긴 끊김 — 실행기는 running 으로 멈추고('이어서' 가 같은 잡을 잇는다) 단계를 실패로 닫지 않는다. 진행 조회도 함께 안 되면(게이트웨이가
    통째로 안 닿는다) 그것은 심의의 실패('모름')가 아니다."""
    gw = _delib_gw(statuses=[{"status": "running"}])
    real = gw.tool

    async def _down(name, args):
        if "deliberate_status" in gw.names():
            if name == "routine_get" and not args.get("call_id"):
                return _refuse("포털에 닿지 못했다(ConnectError)")
            if name == "deliberate_status" and status_too:
                return _err("Error executing tool deliberate_status: 심의 백엔드에 닿지 못했다")
        return await real(name, args)

    gw.tool = _down
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    err = [d for e, d in evs if e == "error"]
    assert len(err) == 1 and err[0]["code"] == "routine_gateway" and "포털에 닿지 못했다" in err[0]["message"], err
    assert _routine(evs, "done")[-1]["state"] == "running" and evs[-1] == ("done", {})
    assert gw.step_done_sent == [] and gw.cancelled == [], "단계를 닫지도 심의를 접지도 않는다 — 이어서가 같은 잡을 잇는다"
    assert gw.names().count("deliberate_status") == 3, "세 번까지 넘기고 멈춘다"


def test_실행_보기_끊김은_잇달아_셀_때만_멈추고_사이에_한_번_읽히면_수를_되돌린다(kit):
    """앞 심의(d1)의 기다림 끝에서 실행 보기가 두 번 안 읽히고 잡이 끝났다 — 단계 사이의 직접 읽기는 읽혔다. 다음 심의(d2)의 첫 읽기 하나가
    끊기면 '잇달아 세 번' 이 아니다. 기다림 속 읽기(_view_soft)만 수를 되돌리면 앞 기다림의 두 번이 남아 그 한 번에 실행기가 running 으로 멈춘다
    (update-all 이 포털 · 게이트웨이를 차례로 재기동하면 끊김 둘이 그렇게 겹친다)."""
    delib = {"job": "mechanism", "chair_template": None}
    gw = FakeGateway([_step("d1", kind="deliberation", delib=delib), _step("d2", kind="deliberation", delib=delib)],
                     statuses=[{"status": "running"}, {"status": "running"}, {"status": "done"}, {"status": "running"}, {"status": "done"}])
    real = gw.tool
    left = {"d1": 2, "d2": 1}       # 단계마다 기다림 속 실행 보기 읽기(진행 조회 바로 뒤의 읽기)를 앞에서부터 이만큼 끊는다

    async def _blip(name, args):
        if name == "routine_get" and not args.get("call_id") and gw.names()[-2] == "deliberate_status":
            sid = gw.rcalls()[-1]["step"]
            if left[sid]:
                left[sid] -= 1
                return _refuse("포털에 닿지 못했다(ConnectError)")
        return await real(name, args)

    gw.tool = _blip
    llm = ScriptLLM(envelope={s: [_env(summary=f"결정 [{s}.c2]", findings=[{"text": "가스", "refs": [f"{s}.c2"]}])] for s in ("d1", "d2")})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert left == {"d1": 0, "d2": 0}, f"시험 전제 — 끊김을 다 냈다 {left}"
    assert not [d for e, d in evs if e == "error"], [d for e, d in evs if e == "error"]
    assert [(d["step"], d["state"]) for d in _routine(evs, "step_done")] == [("d1", "ok"), ("d2", "ok")], _routine(evs, "step_done")
    assert _routine(evs, "done")[-1]["state"] == "done" and evs[-1] == ("done", {})


def test_진행_조회만_잇달아_세_번_실패하면_모름으로_닫고_결정문은_읽지_않는다(kit):
    """심의 백엔드만 안 닿는다(실행 보기는 읽힌다) — 세 번에서 그치고 단계를 job_id 와 함께 실패로 닫는다(끝없이 묻지 않는다)."""
    gw = _delib_gw()
    real = gw.tool

    async def _gone(name, args):
        if name == "deliberate_status":
            return _err(f"Error executing tool deliberate_status: 그런 심의 잡이 없다: {args.get('job_id')} — deliberate_list 로 확인하라")
        return await real(name, args)

    gw.tool = _gone
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert gw.names().count("deliberate_status") == 3
    assert [x["name"] for x in gw.rcalls()] == ["hwax-deliberation:deliberate_start"], "결정문을 읽지 않는다"
    sent = gw.step_done_sent[0][1]
    assert sent["status"] == "failed" and "진행 조회가 잇달아 실패했다" in sent["reason"] and NEW_JOB in sent["reason"], sent
    assert _routine(evs, "done")[-1]["state"] == "done" and gw.cancelled == []


@pytest.mark.parametrize("job,state,folds", [("running", "cancelled", True), ("queued", "cancelled", True), ("running", "interrupted", False)])
def test_심의를_기다리는_사이_실행이_멈추면_진행_조회를_그치고_다시_이을_수_없으면_세운_심의를_접는다(kit, job, state, folds):
    """사람이 멈추면(cancelled) 세운 심의 — 도는 것이든 줄 선 것이든 — 를 접는다(GLM 자리를 쥐고 몇 시간씩 도는데 결정문을 읽을 실행이 없다).
    이어 갈 수 있는 멈춤(interrupted)이면 접지 않는다 — '이어서' 가 같은 잡을 잇는다. 줄 선 심의를 기다리는 동안은 쉼이다."""
    gw = _delib_gw(statuses=[{"status": job}])
    real = gw.tool
    polls: list = []

    async def _stop_at_2(name, args):
        if name == "deliberate_status":
            polls.append((rt.active_count(), rt.parked_count()))
            if len(polls) == 2:
                gw.state = state
        return await real(name, args)

    gw.tool = _stop_at_2
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert len(polls) == 2, "실행이 멈추면 진행 조회를 그친다"
    assert gw.cancelled == ([NEW_JOB] if folds else []), gw.cancelled
    assert [x["name"] for x in gw.rcalls()] == ["hwax-deliberation:deliberate_start"] and gw.step_done_sent == []
    assert _routine(evs, "done")[-1]["state"] == state and evs[-1] == ("done", {})
    assert polls[1] == ((0, 1) if job == "queued" else (1, 0)), "줄 선 심의를 기다리는 동안만 쉼이다"
    assert (f"심의 {NEW_JOB} 를 접었다" in _reasoning(evs)) is folds


def test_실행기가_내려가도_세운_심의는_접지_않는다(kit):
    """에이전트 서버 재기동(작업 취소) — 실행은 running 으로 남고 '이어서' 가 같은 잡을 잇는다. 접으면 이을 잡이 없다. 취소는 진행 조회 사이의
    쉼에 건다(게이트웨이 답을 받는 순간에 걸면 Python 3.10 의 asyncio.wait_for 가 취소를 삼킨다 — 이 시험이 볼 것이 아니다)."""
    kit.setattr(rt, "ROUTINE_DELIB_POLL_S", 0.5)
    gw = _delib_gw(statuses=[{"status": "running"}])
    llm = ScriptLLM()
    _wire(kit, gw, llm)

    async def go():
        frames: list = []

        async def pump():
            async for fr in rt.run_routine(_app_ns(llm), _req()):
                frames.append(fr)

        t = asyncio.ensure_future(pump())
        while gw.names().count("deliberate_status") < 2:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)                 # 둘째 조회 뒤의 쉼(0.5초) 가운데 — 재기동이 작업을 취소한다
        await rt.shutdown()
        await asyncio.wait_for(t, 10)
        return frames

    evs = _ev(asyncio.run(asyncio.wait_for(go(), 15)))
    assert gw.cancelled == [] and gw.step_done_sent == []
    err = [d for e, d in evs if e == "error"]
    assert len(err) == 1 and err[0]["code"] == "routine_internal", err
    assert _routine(evs, "done")[-1]["state"] == "running" and evs[-1] == ("done", {})


@pytest.mark.parametrize("first", ["dead", "refused"])
def test_이어서는_원장의_마지막으로_성공한_심의_시작을_잇는다(kit, first):
    """앞 잡이 죽어 새로 세운 둘째 잡(c2)이 아직 돈다 — 첫 호출(c1)만 보면 c1 이 죽었으니 셋째를 세운다(GLM 을 두 번 쓰고 둘째의 결정문은
    루틴에서 사라진다). 거절된 시작(c1 · ok 거짓 — 새 심의 멈춤)도 건너뛰고 뒤의 성공한 시작을 잇는다."""
    gw = _resumed_delib([{"status": "running"}, {"status": "done"}])
    if first == "refused":
        gw.ledger[0].update(ok=False, result=_pause_text(600))
    gw.stage["delib"]["calls"] = 2
    gw.ledger.append({"call_id": "delib.c2", "step": "delib", "backend": "hwax-deliberation", "tool": "deliberate_start",
                      "args": {"question": "q", "job": "mechanism"}, "ok": True,
                      "result": json.dumps({"job_id": JOB2, "status": "queued"}, ensure_ascii=False)})
    real = gw.tool

    async def _per_job(name, args):
        if name == "deliberate_status" and args.get("job_id") == OLD_JOB:
            return _ok({"job_id": OLD_JOB, "job": "mechanism", "status": "interrupted"})
        return await real(name, args)

    gw.tool = _per_job
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c3]", findings=[{"text": "가스", "refs": ["delib.c3"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="이어서")))
    assert [x["name"] for x in gw.rcalls()] == ["hwax-deliberation:deliberate_result"], "셋째 심의를 세우지 않는다"
    assert gw.rcalls()[0]["arguments"] == {"job_id": JOB2}
    assert {x["job_id"] for n, x, _h in gw.log if n == "deliberate_status"} == {JOB2}, "둘째 잡만 본다"
    sent = gw.step_done_sent[0][1]
    assert sent["status"] == "ok" and "새로 세웠다" not in sent["reason"], sent
    assert f"앞 프로세스가 세운 심의 {JOB2} 를 잇는다" in _reasoning(evs) and _routine(evs, "done")[-1]["state"] == "done"


def test_인사이트를_쓰는_사이_사람이_멈추면_취소로_끝내고_이어서를_말하지_않는다(kit):
    """보고서 제출이 '실행이 cancelled 상태다' 로 거절된다 — 실행기 오류(running · '이어서')가 아니라 사람이 멈춘 끝이다."""
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})])

    def _cancel_while_writing(_msgs):
        gw.state = "cancelled"               # 인사이트를 쓰는 사이 사람이 멈춤을 눌렀다
        return "스웰링 VOC 를 봤다 [voc.c1]."

    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]}, insight=[_cancel_while_writing])
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert not [d for e, d in evs if e == "error"], [d for e, d in evs if e == "error"]
    assert _routine(evs, "done")[-1]["state"] == "cancelled" and evs[-1] == ("done", {})
    assert "이어서" not in _text(evs) and "취소" in _text(evs), _text(evs)
    assert gw.report_md == "" and gw.sections_sent is None


def test_실행_상한으로_남은_단계를_닫는_사이_사람이_멈추면_취소로_끝낸다(kit):
    """상한 닫기의 둘째 단계 닫기가 '실행이 cancelled 상태다' 로 거절된다 — 실행기 오류가 아니라 사람이 멈춘 끝이다."""
    steps = [_step("voc", allowed={"signalforge": ["search_voc"]}), _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}),
             _step("refs", backends=("plm",), allowed={"plm": ["get"]})]
    gw = FakeGateway(steps)

    def _then_limit(_msgs):
        gw.limit = "시간 60분"
        return AIMessage(content="정리")

    real_done = gw._step_done

    def _done_then_cancel(a_):
        out = real_done(a_)
        if a_.get("step") == "ft":
            gw.state = "cancelled"          # 상한 닫기 도중 사람이 멈춤을 눌렀다
        return out

    gw._step_done = _done_then_cancel
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a")), _then_limit]}, envelope={"voc": [_env()]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert [s for s, _e in gw.step_done_sent] == ["voc", "ft", "refs"], "refs 닫기가 실행 상태로 거절됐다"
    assert not [d for e, d in evs if e == "error"], [d for e, d in evs if e == "error"]
    assert _routine(evs, "done")[-1]["state"] == "cancelled" and "이어서" not in _text(evs)
    assert "routine_report_submit" not in gw.names()


def test_닫았는데_단계가_안_닫히면_같은_단계를_되풀이하지_않고_멈춘다(kit):
    """포털이 ok 로 답하고도 단계가 열려 있다(원장 어긋남) — 같은 단계를 다시 돌면 끝없이 LLM · 호출을 쓴다. 한 번에 멈추고 원장을 보라고 한다."""
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})])
    sent: list = []

    def _ok_but_open(a_):
        sent.append(a_.get("step"))
        return _ok({"step": a_.get("step"), "state": "ok", "flags": [], "run_state": "running", "next_step": None})

    gw._step_done = _ok_but_open
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert sent == ["voc"], sent
    err = [d for e, d in evs if e == "error"]
    assert len(err) == 1 and err[0]["code"] == "routine_gateway" and "단계 voc 가 닫히지 않았다" in err[0]["message"], err
    assert _routine(evs, "done")[-1]["state"] == "running" and evs[-1] == ("done", {})


def test_쉬는_루틴은_health_에_따로_싣고_delib_active_에_더하지_않으며_동시_상한에는_센다(kit, tmp_path):
    """쉬는 실행(새 심의 멈춤 · 줄 선 심의를 기다린다)을 delib_active 에 더하면 update-all 의 기다림이 끝나지 않는다(멈춤이 그 심의를 붙잡고
    있다). 동시 상한에는 센다 — 멈춤이 풀리면 한꺼번에 깨어난다."""
    from fakes.llm import FakeLLM  # noqa: PLC0415

    kit.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    kit.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    kit.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    kit.setattr(a.app.state, "review_gate", None, raising=False)
    kit.setattr(rt, "ROUTINE_MAX_RUNNING", 2)
    for key, parked in ((RUN_ID, False), ("run-parked", True)):
        live = rt._Live("u@x.com", key)
        live.task = types.SimpleNamespace(done=lambda: False)
        live.parked = parked
        rt._RUNS[key] = live
    h = TestClient(a.app).get("/health").json()
    assert (h["routine_active"], h["routine_parked"], h["routine_max"]) == (1, 1, 2), h
    assert h["delib_active"] == 1, "쉬는 실행은 delib_active 에 더하지 않는다"
    llm = ScriptLLM()
    _wire(kit, FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})]), llm)
    busy = _ev(_run(_app_ns(llm), _req()))
    assert busy[0][1]["code"] == "routine_busy", "동시 상한은 쉬는 실행도 센다"


def test_잠깐_거절_뒤_기다림은_retry_after_s_이고_없으면_60초에서_두_배씩_10분까지다(kit):
    """거절된 시작도 원장 한 줄 · 단계 예산 하나를 쓴다 — 줄 가득이 이어지면 60 · 120 · 240 · 480 · 600초로 늘려 줄이 빠지기 전에 예산을 다
    쓰지 않는다. 엔진이 retry_after_s 를 주면(새 심의 멈춤) 그 값이고, 이 프로세스의 멈춤은 부르지 않고 그 값만큼(풀리면 곧바로) 쉰다."""
    waits: list = []

    async def _no_wait(_ctx, _sid, _title, wait_s, _cause, *, until_unpaused):
        waits.append((wait_s, until_unpaused))
        return "go", ""

    kit.setattr(rt, "_delib_park", _no_wait)
    gw = _delib_gw()
    real = gw._inner
    refusals = [_QUEUE_FULL] * 5 + [_pause_text(900)]

    async def _busy(bk, orig, args):
        if orig == "deliberate_start" and refusals:
            return refusals.pop(0), True
        return await real(bk, orig, args)

    gw._inner = _busy
    pause = {"n": 0}

    def _pause_once():
        pause["n"] += 1
        return _PAUSE if pause["n"] == 2 else None          # 첫 확인은 run_routine(새 시작), 둘째는 심의 단계의 첫 확인

    kit.setattr(a, "_delib_pause_info", _pause_once)
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c8]", findings=[{"text": "가스", "refs": ["delib.c8"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert waits == [(2400.0, True), (60.0, False), (120.0, False), (240.0, False), (480.0, False), (600.0, False), (900.0, False)], waits
    assert [c["ok"] for c in gw.ledger] == [False] * 6 + [True, True] and _routine(evs, "step_done")[0]["state"] == "ok"


def test_게이트웨이가_통째로_끊긴_동안의_진행_조회_실패는_모름으로_세지_않는다(kit):
    """게이트웨이 재기동 — 처음엔 다 안 닿고(진행 조회 · 실행 보기), 돌아온 뒤에도 심의 백엔드 연결이 한 번 늦는다. 실행 보기가 안 읽힌 때의 진행
    조회 실패까지 세면 세 번째에 '모름' 으로 단계를 실패로 닫아 도는 잡이 루틴에서 사라진다. 실행 보기가 읽힌 때만 센다."""
    gw = _delib_gw(statuses=[{"status": "running"}, {"status": "done"}])
    real = gw.tool
    n = {"status": 0, "view": 0}

    async def _restart(name, args):
        if name == "deliberate_status":
            n["status"] += 1
            if n["status"] <= 3:
                return _err("Error executing tool deliberate_status: 심의 백엔드에 아직 닿지 못했다")
        if name == "routine_get" and not args.get("call_id") and n["status"]:
            n["view"] += 1
            if n["view"] <= 2:
                return _refuse("포털에 닿지 못했다(ConnectError)")
        return await real(name, args)

    gw.tool = _restart
    llm = ScriptLLM(envelope={"delib": [_env(summary="결정 [delib.c2]", findings=[{"text": "가스", "refs": ["delib.c2"]}])]})
    _wire(kit, gw, llm)
    evs = _ev(_run_bounded(_app_ns(llm), _req()))
    assert n["status"] == 5 and not [d for e, d in evs if e == "error"]
    assert gw.step_done_sent[0][1]["status"] == "ok", gw.step_done_sent[0][1]
    assert _routine(evs, "done")[-1]["state"] == "done"


# ── 덧붙인 말 · RA 초안 올리기 · 키의 근거 · 봉투 꼴 · 이어서의 유령 ID · 인자 기본값(R3-design §8 — 반대 검토 뒤 둘째 묶음) ───────────────────
NOTE = "3분기만 보고 구미 공장은 빼 줘"
_CANDS = [{"label": "갤럭시 S26 울트라", "key": "SM-S938"}, {"label": "갤럭시 S26+", "key": "SM-S936"}]


def _pick(gw, step, key) -> None:
    """포털 /pick 그대로 — 고른 후보를 그 단계의 첫 기대 키로 넘기고 단계를 ok 로, 실행을 running 으로(화면은 그 뒤 '이어서' 를 보낸다).
    기다리는(waiting) 실행의 고를 것이 있는 단계에서 후보에 있는 키만 고른다 — 멈춘 · 끝난 실행을 되살리지 않는다(포털 409 · 422 의 글)."""
    st = gw.stage[step]
    if gw.state != "waiting":
        raise RuntimeError(f"실행이 {gw.state} 다 — 고를 수 없다")
    if st["state"] != "ambiguous":
        raise RuntimeError(f"단계 {step} 는 고를 것이 없다({st['state']})")
    if key not in {c["key"] for c in (st["env"] or {}).get("candidates") or []}:
        raise RuntimeError("후보에 없는 키다")
    env = dict(st["env"] or {})
    name = (next(s for s in gw.plan if s["id"] == step)["expect"]["keys"] or ["key"])[0]
    env["keys"] = {**(env.get("keys") or {}), name: key}
    st.update(state="ok", env=env, flags=st["flags"] + [f"사람이 고른 후보 — {name}={key}"])
    gw.state, gw.reason = "running", None


def test_덧붙인_말은_실행에_남아_고른_뒤_이어서도_단계_심의_인사이트에_그대로_실린다(kit):
    """화면은 고른 뒤 '루틴 이어서: <제목>' 을 글로 보낸다 — 그 글이 덧붙인 말을 덮으면 남은 단계 · 심의 · 보고서가 사람의 조건(3분기만 · 구미
    빼고)을 잃는다. 새 실행은 note 를 routine_run_start 로 실행에 남기고, 이어서는 실행 보기의 note 를 싣는다(요청 글은 쓰지 않는다 — C3)."""
    steps = [_step("anchor", allowed={"signalforge": ["search_voc"]}, keys=("model_key",)),
             _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}, uses=("anchor",)),
             _step("delib", kind="deliberation", uses=("ft",), delib={"job": "mechanism", "chair_template": None})]
    gw = FakeGateway(steps, impls={("signalforge", "search_voc"): lambda x: ("후보 — SM-S938 · SM-S936", False)})
    llm = ScriptLLM(react={"anchor": [_ai(_tc("search_voc", q="S26"))], "ft": [_ai(_tc("failuretrace_search", q="SM-S938"))]},
                    envelope={"anchor": [_env(status="ambiguous", candidates=_CANDS)], "ft": [_env()],
                              "delib": [_env(summary="결정 [delib.c2]", findings=[{"text": "가스", "refs": ["delib.c2"]}])]})
    _wire(kit, gw, llm)
    first = _ev(_run(_app_ns(llm), _req(message=NOTE, routine={"id": "rt-1", "inputs": {"model": "S26"}, "note": NOTE})))
    assert _routine(first, "done")[-1]["state"] == "waiting" and gw.start_args["note"] == NOTE, gw.start_args
    assert f"[사용자가 덧붙인 말] {NOTE}" in str(llm.turns("react", "anchor")[0][2][0].content)
    _pick(gw, "anchor", "SM-S938")
    resumed = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="루틴 이어서: 시장불량 분석")))
    assert _routine(resumed, "done")[-1]["state"] == "done", _routine(resumed, "done")
    seen = {"단계": str(llm.turns("react", "ft")[0][2][0].content), "심의 질문": gw.rcalls("delib")[0]["arguments"]["question"],
            "인사이트": str(llm.turns("insight")[0][2][1].content)}
    for where, text in seen.items():
        assert f"[사용자가 덧붙인 말] {NOTE}" in text and "루틴 이어서" not in text, (where, text[:500])
    with pytest.raises(RuntimeError, match="실행이 done 다 — 고를 수 없다"):
        _pick(gw, "anchor", "SM-S936")        # 앞 메시지에 남은 고르기 단추 — 끝난 실행을 되살리지 않는다(R3-design §8)
    assert gw.state == "done"


def test_덧붙인_말은_실행_보기의_note_이고_그_칸이_없으면_새_실행_요청의_note_이며_요청_글은_쓰지_않는다(kit):
    """실행 보기의 note 가 정본이다(이어서 · 고른 뒤 잇기도 같은 말). note 칸을 모르는 옛 포털이면 새 실행 요청의 note — 이어서 요청에는 note 가
    없으니 비고, 요청 글('루틴 이어서: …')로 메우지 않는다."""
    resume = rt._Ctx(types.SimpleNamespace(state=None), _req(routine={"run_id": RUN_ID}, message="루틴 이어서: 시장불량 분석"),
                     rt._Live("u@x.com", RUN_ID))
    assert resume.note == ""
    resume.adopt({"run": {"id": RUN_ID}, "note": " 구미 공장은 빼 줘 "})
    assert resume.note == "구미 공장은 빼 줘"
    old = rt._Ctx(types.SimpleNamespace(state=None), _req(routine={"id": "rt-1", "note": "3분기만"}, message="3분기만"),
                  rt._Live("u@x.com", "pending-x"))
    old.adopt({"run": {"id": RUN_ID}})              # note 칸이 없는 옛 포털의 실행 보기
    assert old.note == "3분기만"


def _done_ask_run(kit):
    """'끝에 묻는다' 루틴을 끝까지 돌린 실행 — 보고서 프레임에는 사람에게 물으라는 ra_next 만 있고 RA 초안은 없다."""
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]})], upload="ask")
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="a"))]})
    _wire(kit, gw, llm)
    rep = _routine(_ev(_run(_app_ns(llm), _req())), "report")[0]
    assert rep["ra_upload"] == "ask" and rep.get("ra_next") and "ra_report_id" not in rep and gw.ra_made == 0, rep
    return gw, llm


def test_끝난_실행에_upload_ra_를_보내면_저장한_보고서로_RA_초안을_올리고_다시_보내도_두_번_만들지_않는다(kit):
    """화면의 'RA 초안 올리기'(ra.upload 'ask') — 끝난 실행에 routine{run_id, upload_ra} 를 보내면 routine_report_submit(upload=true) 하나만
    부른다(다시 돌지 않는다). routine{report} · 짧은 안내 · routine{done, done} 이고, 보고서 글은 다시 흘리지 않는다(화면이 이미 보인다)."""
    gw, llm = _done_ask_run(kit)
    n = len(gw.log)
    evs = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID, "upload_ra": True}, message="RA 초안 올리기")))
    new = [(name, x) for name, x, _h in gw.log[n:]]
    assert [x for name, x in new if name == "routine_report_submit"] == [{"run_id": RUN_ID, "upload": True}], new
    assert not {"routine_run_start", "routine_call", "routine_step_done"} & {name for name, _x in new}, "끝난 실행을 다시 돌지 않는다"
    assert [d["kind"] for d in _routine(evs)] == ["plan", "report", "done"] and _routine(evs, "plan")[0]["resumed"] is True
    names = [e for e, _d in evs]
    tail = [(e, d.get("kind")) for e, d in evs[names.index("routine", names.index("status")):]]
    assert tail == [("routine", "report"), ("token", None), ("result", None), ("routine", "done"), ("done", None)], tail
    assert _routine(evs, "report")[0]["ra_report_id"] == "ra-77" and gw.ra_made == 1
    assert "Report Archive 초안을 올렸다 — ra-77" in _text(evs) and gw.report_md not in _text(evs)
    assert _routine(evs, "done")[-1]["state"] == "done" and not [d for e, d in evs if e == "error"]
    again = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID, "upload_ra": True})))
    assert gw.ra_made == 1 and _routine(again, "report")[0]["ra_report_id"] == "ra-77" and "이미 있다" in _text(again)


def test_RA_초안을_못_올리면_까닭을_말하고_끝난_실행_그대로_닫는다(kit):
    """저장한 보고서가 없는 끝난 실행(R3 전) — 포털이 거절한다. 실행기 오류처럼 running · '이어서' 로 끝내지 않는다(끝난 실행이다)."""
    gw = FakeGateway(_two_steps(), state="done", upload="ask")
    llm = ScriptLLM()
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID, "upload_ra": True})))
    assert not [d for e, d in evs if e == "error"] and not _routine(evs, "report") and gw.ra_made == 0
    assert _routine(evs, "done")[-1]["state"] == "done" and evs[-1] == ("done", {})
    assert "올리지 못했다" in _text(evs) and "이어서" not in _text(evs), _text(evs)


@pytest.mark.parametrize("state", ["running", "waiting"])
def test_끝나지_않은_실행의_upload_ra_는_보지_않고_평소대로_잇는다(kit, state):
    steps = _two_steps()
    steps[0]["_state"] = "ok" if state == "running" else "ambiguous"
    gw = FakeGateway(steps, state=state, upload="ask")
    gw.stage["voc"]["env"] = {"status": steps[0]["_state"], "summary": "앞서 본 VOC", "keys": {}, "findings": [], "candidates": _CANDS}
    llm = ScriptLLM(react={"ft": [_ai(_tc("failuretrace_search", q="y"))]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req(routine={"run_id": RUN_ID, "upload_ra": True})))
    submits = [x for name, x, _h in gw.log if name == "routine_report_submit"]
    if state == "running":
        assert "routine_run_start" in gw.names() and [x["step"] for x in gw.rcalls()] == ["ft"]
        assert len(submits) == 1 and "upload" not in submits[0] and gw.ra_made == 0, submits
        assert _routine(evs, "done")[-1]["state"] == "done"
    else:
        assert submits == [] and _routine(evs, "gate")[0]["candidates"] == _CANDS and _routine(evs, "done")[-1]["state"] == "waiting"


def _park_others(n: int) -> None:
    """남의 쉬는 실행 n 개 — 줄 선 심의를 몇 시간씩 기다린다(살아 있되 쉰다). 동시 상한은 쉬는 실행도 센다."""
    for i in range(n):
        live = rt._Live("other@x.com", f"run-other-{i}")
        live.task = types.SimpleNamespace(done=lambda: False)
        live.parked = True
        rt._RUNS[live.key] = live


def _resumable(state="running", **kw) -> FakeGateway:
    """앞 단계(voc)를 닫고 다음 단계(ft)가 남은 실행 — 이어서는 새 일(챗 LLM · 게이트웨이)이다."""
    steps = _two_steps()
    steps[0]["_state"] = "ok"
    gw = FakeGateway(steps, state=state, **kw)
    gw.stage["voc"]["env"] = {"status": "ok", "summary": "앞서 본 VOC", "keys": {}, "findings": [], "candidates": []}
    return gw


@pytest.mark.parametrize("gate", ["pause", "cap"])
def test_끝난_실행의_RA_초안_올리기와_끝난_실행_안내는_새_일이_아니라_새_심의_멈춤과_동시_상한에_걸리지_않는다(kit, gate):
    """화면의 'RA 초안 올리기' · 끝난 실행의 '이어서' 는 실행기 자리를 쓰지 않고 심의도 세우지 않는다 — update-all 의 새 심의 멈춤(약 40분)이나
    남의 쉬는 실행 둘이 상한을 채운 동안에도 된다. 문(새 심의 멈춤 · 동시 상한)은 실행 보기를 읽어 도는 실행을 이을 때만 본다."""
    gw, llm = _done_ask_run(kit)
    if gate == "pause":
        kit.setattr(a, "_delib_pause_info", lambda: _PAUSE)
    else:
        _park_others(2)
    up = _ev(_run_bounded(_app_ns(llm), _req(routine={"run_id": RUN_ID, "upload_ra": True}, message="RA 초안 올리기")))
    assert not [d for e, d in up if e == "error"], [d for e, d in up if e == "error"]
    assert gw.ra_made == 1 and _routine(up, "report")[0]["ra_report_id"] == "ra-77", _routine(up)
    assert "Report Archive 초안을 올렸다 — ra-77" in _text(up) and _routine(up, "done")[-1]["state"] == "done"
    again = _ev(_run_bounded(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="루틴 이어서: 시장불량 분석")))
    assert not [d for e, d in again if e == "error"] and "이미 끝났다" in _text(again), again
    assert _routine(again, "done")[-1]["state"] == "done" and again[-1] == ("done", {})
    assert gw.names().count("routine_run_start") == 1 and gw.state == "done", "끝난 실행을 다시 돌지 않는다(처음 시작 하나뿐)"


@pytest.mark.parametrize("upload", [True, None])
@pytest.mark.parametrize("gate", ["pause", "cap"])
def test_도는_실행을_잇는_요청은_upload_ra_가_붙어도_새_심의_멈춤과_동시_상한에_걸리고_시작을_부르지_않는다(kit, gate, upload):
    """문은 upload_ra 깃발이 아니라 원장의 실행 상태로 정한다 — 도는 실행에 붙은 upload_ra 는 보지 않고 그대로 잇는 새 일이다(깃발만 보고 문을
    열면 멈춤 · 상한을 건너뛰어 남은 단계를 돈다). 거절은 새 시작과 같은 꼴(error · done)이고 routine{done, running} 을 내지 않는다."""
    import delib_jobs  # noqa: PLC0415

    gw = _resumable(upload="ask")
    llm = ScriptLLM(react={"ft": [_ai(_tc("failuretrace_search", q="y"))]})
    _wire(kit, gw, llm)
    if gate == "pause":
        kit.setattr(a, "_delib_pause_info", lambda: _PAUSE)
        want = ("error", {"code": "delib_paused", "message": delib_jobs.pause_message(_PAUSE), "retry_after_s": 2400, "infra": True,
                          "retryable": True})
    else:
        _park_others(2)
        want = ("error", {"code": "routine_busy", "message": "분석 루틴이 이미 2개 돈다(동시 상한 2) — 잠시 뒤 다시 보낸다", "retryable": True,
                          "retry_after_s": 60})
    spec = {"run_id": RUN_ID, **({"upload_ra": True} if upload else {})}
    evs = _ev(_run_bounded(_app_ns(llm), _req(routine=spec, message="루틴 이어서: 시장불량 분석")))
    assert evs == [want, ("done", {})], evs
    assert gw.names() == ["routine_get"], "실행 보기만 읽고 시작(routine_run_start) · 단계 호출을 부르지 않는다"
    assert gw.state == "running" and gw.ra_made == 0 and llm.seen == []


def test_도는_실행을_잇는_요청의_동시_상한은_자기를_빼고_센다(kit):
    """이어서 요청은 실행 보기를 읽기 전에 레지스트리에 오른다 — 자기를 세면 다른 실행이 상한보다 하나 적을 때도 거절한다."""
    gw = _resumable()
    llm = ScriptLLM(react={"ft": [_ai(_tc("failuretrace_search", q="y"))]})
    _wire(kit, gw, llm)
    _park_others(rt.ROUTINE_MAX_RUNNING - 1)
    evs = _ev(_run_bounded(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="루틴 이어서: 시장불량 분석")))
    assert not [d for e, d in evs if e == "error"], [d for e, d in evs if e == "error"]
    assert "routine_run_start" in gw.names() and [x["step"] for x in gw.rcalls()] == ["ft"]
    assert _routine(evs, "done")[-1]["state"] == "done"


@pytest.mark.parametrize("case", ["failed_call", "echo", "sourced"])
def test_키의_근거는_성공한_호출_결과이고_받은_인자를_되돌려_준_값과_실패한_호출의_글은_근거가_아니다(kit, case):
    """모델이 SM-S999 를 추측해 부르면 도구가 그 인자를 되돌려 준다({"query": "SM-S999", "items": []}), 실패한 호출의 오류 글에도 값이 섞인다 —
    그 글자를 근거로 치면 지어낸 키가 다음 단계로 넘어간다(AR-D2). 근거 규칙을 실어 한 번 다시 묻고, 그래도면 빼고 까닭에 적는다(C5). 두 규칙은
    따로 본다 — 실패한 호출은 인자에 그 값이 없어도 근거가 아니다."""
    out = {"failed_call": ("조회 실패 — 후보 SM-S999 의 원본을 읽다가 끊겼다", True),
           "echo": (json.dumps({"query": "SM-S999", "items": []}), False),
           "sourced": ("후보 — 모델 SM-S999 · 스웰링 3건", False)}[case]
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}, keys=("model_key",))],
                     impls={("signalforge", "search_voc"): lambda x: out})
    env = _env(keys={"model_key": "SM-S999"})
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="SM-S999" if case == "echo" else "S26"))]}, envelope={"voc": [env, env]})
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req())
    turns, sent = llm.turns("envelope", "voc"), gw.step_done_sent[0][1]
    if case == "sourced":
        assert len(turns) == 1 and sent["keys"] == {"model_key": "SM-S999"}, sent
    else:
        assert len(turns) == 2 and "성공한 호출 결과" in str(turns[1][2][1].content), "근거 규칙을 실어 한 번 다시 묻는다"
        assert sent["keys"] == {} and "결과에 글자 그대로 없는 키는 뺐다: model_key" in sent["reason"], sent
        assert gw.view()["steps"][0]["keys"] == {}


@pytest.mark.parametrize("bad", [["SM-S938", "SM-S936"], [{"name": "S26 울트라", "model": "SM-S938"}], [{"label": "S26 울트라", "key": " "}]])
def test_꼴이_틀린_후보는_한_번_다시_묻고_고치면_고를_것으로_멈춘다(kit, bad):
    """원 꼴만 보면 비지 않은 목록이라 검사를 지나고, 다듬기에서 후보가 다 빠져 고를 것이 조용히 사라졌다(다음 단계가 풀리지 않은 키로 돌았다)."""
    gw = FakeGateway([_step("anchor", allowed={"signalforge": ["search_voc"]}, keys=("model_key",)),
                      _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}, uses=("anchor",))],
                     impls={("signalforge", "search_voc"): lambda x: ("후보 — SM-S938 · SM-S936", False)})
    llm = ScriptLLM(react={"anchor": [_ai(_tc("search_voc", q="S26"))]},
                    envelope={"anchor": [_env(status="ambiguous", candidates=bad), _env(status="ambiguous", candidates=_CANDS)]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    turns = llm.turns("envelope", "anchor")
    assert len(turns) == 2 and '"label"' in str(turns[1][2][1].content), "꼴을 실어 한 번 다시 묻는다"
    assert _routine(evs, "gate")[0]["candidates"] == _CANDS and _routine(evs, "done")[-1]["state"] == "waiting"
    assert gw.rcalls("ft") == []


def test_목록으로_쓴_키는_한_번_다시_묻고_고치면_넘긴다(kit):
    gw = FakeGateway([_step("voc", allowed={"signalforge": ["search_voc"]}, keys=("model_key",))],
                     impls={("signalforge", "search_voc"): lambda x: ("모델 SM-S938", False)})
    llm = ScriptLLM(react={"voc": [_ai(_tc("search_voc", q="S26"))]},
                    envelope={"voc": [_env(keys=["SM-S938"]), _env(keys={"model_key": "SM-S938"})]})
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req())
    assert len(llm.turns("envelope", "voc")) == 2 and gw.step_done_sent[0][1]["keys"] == {"model_key": "SM-S938"}


def test_다시_물어도_후보를_못_쓴_여럿은_까닭과_함께_실패로_닫고_목록_키는_넘기지_않았다고_적는다(kit):
    """고를 것을 조용히 잃지 않는다 — 실패로 닫고 까닭(후보를 못 썼다)을 적는다. 포털이 '후보 없이 여럿' 으로 고치면 모델이 후보를 낸 사실이
    가려진다."""
    gw = FakeGateway([_step("anchor", allowed={"signalforge": ["search_voc"]}, keys=("model_key",)),
                      _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}, uses=("anchor",))],
                     impls={("signalforge", "search_voc"): lambda x: ("후보 — SM-S938 · SM-S936", False)})
    bad = _env(status="ambiguous", keys=["SM-S938"], candidates=["SM-S938", "SM-S936"])
    llm = ScriptLLM(react={"anchor": [_ai(_tc("search_voc", q="S26"))], "ft": [_ai(_tc("failuretrace_search", q="x"))]},
                    envelope={"anchor": [bad, bad]})
    _wire(kit, gw, llm)
    evs = _ev(_run(_app_ns(llm), _req()))
    assert len(llm.turns("envelope", "anchor")) == 2
    sent = gw.step_done_sent[0][1]
    assert sent["status"] == "failed" and sent["candidates"] == [] and sent["keys"] == {}, sent
    assert "고를 후보 {label, key} 를 쓰지 못했다" in sent["reason"] and "keys 가 객체가 아니라(list) 넘기지 않았다" in sent["reason"], sent
    done = _routine(evs, "step_done")[0]
    assert done["state"] == "failed" and not any("후보 없이" in f for f in done["flags"]), done
    assert not _routine(evs, "gate")


def test_이어서도_앞_단계_요약_발견이_보이는_ID_로_부르는_호출은_막지_않고_인용_번호와_지어낸_ID_는_막는다(kit):
    """이어서(새 작업)는 앞 작업이 도구 결과에서 배운 ID 를 모른다 — 프롬프트가 보이는 앞 단계 요약 · 발견(4417 · 5523)과 덧붙인 말(7781)의 ID 로
    부르면 막지 않는다. 인용 [voc.c1] 의 1 · 요청 글('루틴 이어서: <제목>')에만 있는 9999 는 그대로 막는다(유령 ID)."""
    steps = [_step("voc", allowed={"signalforge": ["search_voc"]}, state="ok"),
             _step("ft", backends=("failuretrace",), allowed={"failuretrace": ["search"]}, uses=("voc",))]
    gw = FakeGateway(steps, state="running")
    gw.note = "시험 7781 도 같이 본다"
    gw.stage["voc"]["env"] = {"status": "ok", "summary": "시험 목록 — test_id 4417 이 스웰링을 재현했다 [voc.c1]", "keys": {},
                              "findings": [{"text": "시험 5523 은 재현하지 못했다", "refs": ["voc.c1"]}], "candidates": []}
    gw.ledger.append({"call_id": "voc.c1", "step": "voc", "backend": "signalforge", "tool": "search_voc", "args": {}, "ok": True,
                      "result": "시험 목록 — 4417 · 5523"})
    llm = ScriptLLM(react={"ft": [_ai(_tc("failuretrace_search", test_id=n)) for n in (4417, 5523, 7781, 1, 9999)]})
    _wire(kit, gw, llm)
    _run(_app_ns(llm), _req(routine={"run_id": RUN_ID}, message="루틴 이어서: 9999 라인 시장불량 분석"))
    assert [x["arguments"] for x in gw.rcalls("ft")] == [{"test_id": 4417}, {"test_id": 5523}, {"test_id": 7781}], gw.rcalls("ft")
    outs = [str(m.content) for m in llm.turns("react", "ft")[-1][2] if getattr(m, "type", "") == "tool"]
    assert len(outs) == 5 and sum(a._PHANTOM_ID_MARK in o for o in outs) == 2, outs


def test_단계_도구에도_챗_길의_인자_기본값을_건다_plm_defect_search_는_enrich_를_안_주면_0이다(kit):
    """사용자 결정 10-27 D-3 — 챗은 plm_defect_search 의 enrich 를 0 으로 채운다(5 는 PLM 원본을 다시 불러 하베스터 · 챗 · 웹이 같이 쓰는 PLM
    한도를 먹는다). 루틴도 웹 챗에서 시작해 챗 LLM 으로 돈다 — 주지 않았을 때만 채우고(준 값은 그대로) 설명 끝에 그 글을 단다."""
    async def _direct(**kw):
        DIRECT.append(("plmdefect_plm_defect_search", kw))
        raise AssertionError("routine_call 을 거쳐야 한다")

    plm = StructuredTool(name="plmdefect_plm_defect_search", description="PLM 시장불량 검색", coroutine=_direct,
                         args_schema={"type": "object", "properties": {"q": {"type": "string"},
                                                                      "enrich": {"type": "integer", "default": 5}}})
    gw = FakeGateway([_step("plm", backends=("plm-defect",), allowed={"plm-defect": ["plm_defect_search"]})])
    llm = ScriptLLM(react={"plm": [_ai(_tc("plmdefect_plm_defect_search", q="S26 스웰링")),
                                   _ai(_tc("plmdefect_plm_defect_search", q="S26 스웰링", enrich=3))]})
    _wire(kit, gw, llm, tools={"plmdefect_plm_defect_search": plm, **TOOLS},
          tmap={"plmdefect_plm_defect_search": "plm-defect", **TMAP})
    _run(_app_ns(llm), _req())
    assert [x["arguments"] for x in gw.rcalls("plm")] == [{"q": "S26 스웰링", "enrich": 0}, {"q": "S26 스웰링", "enrich": 3}]
    assert "[이 대화에서는 enrich 를 주지 않으면 0 이다" in llm.descs[0]["plmdefect_plm_defect_search"], llm.descs
    assert DIRECT == []

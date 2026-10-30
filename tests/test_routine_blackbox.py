# 분석 루틴 R3 실행기(routine.py)를 바깥에서만 재는 시험 — 가짜 게이트웨이(원 세션 JSON-RPC · 작은 포털 원장)와 대본 챗 모델로 SSE 프레임과 원장 기록을 본다
"""HWAXPortal docs/analysis-routines/R3-design.md §1 ~ §4 의 계약을 실행기 속을 보지 않고 잰다.

실행기는 다른 손이 짓는다. 이 파일은 설계 문서와 시험 이음매(routine.GW_TRANSPORT · _gateway_url · _routine_llm · _user_tools ·
_tool_backends · run_routine · active_count · _RUNS · ROUTINE_MAX_RUNNING)만 믿는다 — 속을 읽고 맞춘 시험은 속과 함께 틀린다.

가짜는 진짜 꼴로 둔다.
- 게이트웨이는 MCP streamable HTTP 답(initialize 의 mcp-session-id 머리 · `event: message` 의 마지막 data: 줄 JSON-RPC ·
  notifications 는 202 · 모르는 세션은 404)을 낸다. 사람 PAT 가 아닌 자격(서비스 토큰 · 빈 값)은 401 로 받고 위반으로 적는다.
- routine_call 의 답은 첫 글 블록이 머리 JSON 이고 나머지가 본문이다. 거절은 `ROUTINE_ERROR: ` 로 시작하는 글 블록 하나 + isError 다.
- 포털 몫(봉투 맞대 고치기 · 예산 · 울타리 · 쪽)은 HWAXPortal app/procedures/routine_routes.py 와 HWAXMcpGateway gateway.py 의 규칙과
  글을 그대로 옮겼다(R3 에서 바뀌는 셋 — run_id 이어서 · conversation_id · deliberate_start 의 save_report=false — 도 설계대로).
  §8 에서 바뀐 것도 같은 글로 옮겼다 — 단계 울타리는 읽기로 확인된 것만(_routine_read, 모르면 거절) · 심의 단계의 결정문 읽기는 단계
  예산 밖(실행 호출 상한은 그대로, 시간 상한 뒤에도 읽는다) · 덧붙인 말(note)은 실행에 남는다 · 고르기는 기다리는 실행에서만 · 끝난 실행에
  다시 낸 보고서는 저장본(RA 'ask' 의 upload=true 다시 올리기) · 심의 접기(deliberate_cancel)는 엔진 답 꼴.
- 사람 도구는 MCP 어댑터가 만드는 꼴(StructuredTool · JSON 스키마 args_schema · content_and_artifact)이고, 직접 불리면 위반으로
  적고 터진다 — 단계 안 호출은 전부 routine_call 로 나가야 한다(울타리 · 원장이 Claude 길과 같다, R3-D3).

각 시험은 asyncio.wait_for 로 묶는다 — 틀린 실행기가 매달려도 몇십 초 안에 붉게 끝난다.

  실행:  .venv/bin/python -m pytest tests/test_routine_blackbox.py -q
"""
import asyncio
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import InjectedToolArg, StructuredTool  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import routine  # noqa: E402

GW_URL = "http://gw.test/mcp"
PAT = "pat-chat-test-routine-0001"               # 시험용 가짜 PAT — 실 토큰이 아니다
SVC = "svc-gateway-token-must-not-be-used"       # 서비스 계정 자리 — 이 값이 게이트웨이에 닿으면 위반이다
USER = "tester@example.com"
GROUPS = ["portal-admin", "CAEG"]
ENT = ["feat:chat", "feat:routines"]
RUN_ID = "a1" * 16
ROUTINE_ID = "b2" * 16
JOB_ID = "mechanism-20261030-101500-a1b2c3"
TITLE = "시장불량 원인 분석"
PURPOSE = "S26 의 스웰링 시장불량 — 원인 · 시험 · 대책"
INPUTS = {"model": "S26", "symptom": "스웰링"}
NOTE = "3분기 위주로 봐 줘"                    # 사람이 쓴 글 — 화면이 글(message)과 루틴의 note 로 함께 보낸다(R3-design §8)
GUIDE = {"search_tools", "invoke_tool", "list_tool_apps", "recommend_agents"}
STATES = ("ok", "empty", "ambiguous", "denied", "not_connected", "unavailable", "failed", "skipped")
CLOSED = ("ok", "empty", "denied", "not_connected", "unavailable", "failed", "skipped")
PAGE_CHARS = 60_000                              # 게이트웨이 ROUTINE_PAGE_CHARS · 포털 PAGE_CHARS


# ── 이음매가 동기든 비동기든 받게 — 이음매 약속이 _tool_backends 를 '동기 또는 비동기(문서로 밝힌다)' 로 두었다 ──────────────
class _AwDict(dict):
    """그대로 쓰면 사전, await 하면 같은 사전."""

    def __await__(self):
        yield from ()
        return dict(self)


class _AwTuple(tuple):
    def __await__(self):
        yield from ()
        return tuple(self)


class _AwStr(str):
    def __await__(self):
        yield from ()
        return str(self)


# ── 사람의 도구 목록(MCP 어댑터 꼴) · 노출 이름 → 백엔드(/tools-map) ──────────────────────────────────────────────
# 노출 이름은 둘 다 둔다 — 접두가 붙은 것(corehub_object_resolve — 포털 규칙 bk.replace("-","")+"_" 를 떼면 원래 이름)과 그대로인 것.
BACKENDS = {
    "corehub_object_resolve": "core-hub",
    "pd_defect_search": "failuretrace",
    "pd_defect_stats": "failuretrace",
    "pd_defect_delete": "failuretrace",          # 쓰기 — 허용 목록에 없다(묶이면 안 된다)
    "pd_defect_export": "failuretrace",          # 표지 없고 조회 낱말도 없다 — 게이트웨이가 읽기로 확인하지 못한다(모르면 거절)
    "signalforge_search_voc": "signalforge",     # 다른 백엔드 — 묶이면 안 된다
    "deliberate_start": "hwax-deliberation",
    "deliberate_status": "hwax-deliberation",
    "deliberate_result": "hwax-deliberation",
    "deliberate_cancel": "hwax-deliberation",
}
ROUTE = {"corehub_object_resolve": ("core-hub", "object_resolve"), "pd_defect_search": ("failuretrace", "pd_defect_search"),
         "pd_defect_stats": ("failuretrace", "pd_defect_stats"), "pd_defect_delete": ("failuretrace", "pd_defect_delete"),
         "pd_defect_export": ("failuretrace", "pd_defect_export"),
         "signalforge_search_voc": ("signalforge", "search_voc"),
         "deliberate_start": ("hwax-deliberation", "deliberate_start"),
         "deliberate_status": ("hwax-deliberation", "deliberate_status"),
         "deliberate_result": ("hwax-deliberation", "deliberate_result"),
         "deliberate_cancel": ("hwax-deliberation", "deliberate_cancel")}
_SCHEMAS = {
    "corehub_object_resolve": ("모델 · 과제 · 부서 이름을 코어 허브의 기준 키로 푼다", {"query": {"type": "string", "description": "찾을 이름"}}),
    "pd_defect_search": ("시장불량 기록 검색", {"model": {"type": "string"}, "symptom": {"type": "string"}}),
    "pd_defect_stats": ("시장불량 분기별 집계", {"model": {"type": "string"}}),
    "pd_defect_delete": ("시장불량 기록 지우기", {"id": {"type": "string"}}),
    "pd_defect_export": ("시장불량 기록 내보내기", {"model": {"type": "string"}}),
    "signalforge_search_voc": ("VOC 검색", {"q": {"type": "string"}}),
    "deliberate_start": ("심의 시작", {"question": {"type": "string"}, "job": {"type": "string"}}),
    "deliberate_status": ("심의 진행 상황", {"job_id": {"type": "string"}}),
    "deliberate_result": ("심의 결과", {"job_id": {"type": "string"}}),
    "deliberate_cancel": ("심의 취소", {"job_id": {"type": "string"}}),
    "search_tools": ("도구 찾기(안내대)", {"query": {"type": "string"}}),
    "invoke_tool": ("이름으로 도구 부르기(안내대)", {"name": {"type": "string"}, "arguments": {"type": "object"}}),
    "list_tool_apps": ("앱 목록(안내대)", {}),
    "recommend_agents": ("전문가 추천(안내대)", {"query": {"type": "string"}}),
}
# 앱이 단 도구 표지(MCP annotations) — 대부분의 앱은 달지 않는다(R3 §8 · context-notes C-12 실측 — MXWP 위키만 단다). 어댑터는 이것을 metadata 로 옮기고
# 게이트웨이 _routine_read 는 이것부터 본다. 표지 없는 도구는 이름 꼴로만 갈린다.
HINTS = {"pd_defect_delete": {"readOnlyHint": False, "destructiveHint": True}}

# 게이트웨이 _routine_read(닫힌 쪽 — 읽기로 확인된 것만, R3 §8) — 표지 → 읽기 목록 → 원래 이름의 조회 낱말(쓰기 꼴이 아닐 때). 낱말 · 꼴은
# gateway.py 의 _ROUTINE_READ_TOKENS · _WRITE_NAME 그대로다 — 이름 규칙은 쓰기 동사를 그대로 본다(읽기 꼬리 _READ_NAME_HINT 가 덮지 않는다, 2차).
# 막는 목록(deny)을 가장 먼저 본다. 읽기 목록(read_tools.json ∪ 박스 덧씌우기)은 박스가 연 기준 키 도구 하나만 싣는다.
_READ_TOKENS = frozenset((
    "get", "list", "search", "find", "query", "describe", "lookup", "show", "count", "status", "summary", "summarize",
    "profile", "detail", "details", "history", "compare", "read", "view", "inspect", "neighbors", "similar", "evidence", "revisions",
    "facets", "stats", "statistics", "overview", "catalog", "info", "preview", "estimate", "instructions", "guide", "advice",
    "browse", "explain"))   # fetch · resolve · trace 는 뺐다(동사로도 읽힌다 — 반대 검토 2차, R3 §8)
_WRITE_NAME = re.compile(r"(?:^|_)(create|update|delete|remove|set|save|register|upload|submit|run|rerun|start|import|ingest|apply|add|"
                         r"patch|publish|cancel|stop|train|activate|recompute|rename|confirm|unlink|bind|move|insert|propose|deprecate|"
                         r"fileback|decide|ack|attach|intake|record|forget|draft|send|purge|restore|trash|reload|reset|continue|upsert|merge|"
                         r"replace|overwrite|write)(?:_|$)")
# 코어 허브의 object_resolve 는 이름에 조회 낱말이 없다(resolve 를 뺐다) — cae00 이 read_tools.local.json 에 더하는 길을 흉내 낸다(server-setup.md 5절 0).
READ_LIST: frozenset = frozenset({"core-hub:object_resolve"})
DENY_LIST: frozenset = frozenset()
RT_DELIB_TOOLS = frozenset(("deliberate_start", "deliberate_status", "deliberate_result"))   # 심의 단계에서 판정 없이 여는 셋


def _routine_read(exposed: str, bk: str, orig: str) -> bool:
    if f"{bk}:{orig}" in DENY_LIST:
        return False
    ann = HINTS.get(exposed) or {}
    if ann.get("readOnlyHint") is False or ann.get("destructiveHint") is True:
        return False
    if ann.get("readOnlyHint") is True:
        return True
    if f"{bk}:{orig}" in READ_LIST:
        return True
    return any(tok in _READ_TOKENS for tok in orig.split("_")) and not _WRITE_NAME.search(orig)


def _user_tool(name: str, direct: list) -> StructuredTool:
    desc, props = _SCHEMAS[name]

    async def call_tool(runtime: Annotated[object | None, InjectedToolArg()] = None, **arguments):
        # 실행기가 원래 도구를 그대로 부르면 게이트웨이 울타리 · 원장을 건너뛴다 — 적고 터진다
        direct.append((name, arguments))
        raise RuntimeError(f"울타리 우회 — {name} 을 routine_call 없이 직접 불렀다")

    return StructuredTool(name=name, description=desc,
                          args_schema={"type": "object", "properties": props, "required": list(props)[:1]},
                          coroutine=call_tool, response_format="content_and_artifact",
                          metadata=dict(HINTS[name]) if name in HINTS else None)


def _tool_name(t) -> str:
    if isinstance(t, dict):
        return str(t.get("name") or (t.get("function") or {}).get("name") or "")
    return str(getattr(t, "name", "") or "")


# ── 작은 포털 원장 — 계획 행 · 실행 보기 · 원장 · 봉투 맞대기 · 보고서(포털 routine_routes.py · 게이트웨이 gateway.py 를 옮겼다) ──────
def _platform(sid, title, backend, tools, goal, *, uses=(), keys=(), calls=8, seconds=300):
    return {"id": sid, "title": title, "kind": "platform", "backends": [backend], "allowed": {backend: list(tools)},
            "goal": goal, "uses": list(uses), "expect": {"keys": list(keys), "fields": {}},
            "budget": {"calls": calls, "seconds": seconds},
            "blocked": {"empty": "continue", "denied": "skip", "unavailable": "skip"},
            "deliberation": None, "readiness": {"state": "allowed", "reason": ""}}


def _delib_step(sid, title, goal, *, uses=(), job="mechanism", chair="diagnosis", calls=8):
    return {"id": sid, "title": title, "kind": "deliberation", "backends": ["hwax-deliberation"],
            "allowed": {"hwax-deliberation": ["deliberate_start", "deliberate_status", "deliberate_result"]},
            "goal": goal, "uses": list(uses), "expect": {"keys": [], "fields": {}},
            "budget": {"calls": calls, "seconds": 1800},
            "blocked": {"empty": "continue", "denied": "skip", "unavailable": "skip"},
            "deliberation": {"job": job, "chair_template": chair}, "readiness": {"state": "allowed", "reason": ""}}


ANCHOR = _platform("anchor", "기준 키", "core-hub", ["object_resolve"], "S26 을 코어 허브의 plm_model 로 푼다",
                   keys=["model_key"], calls=4)
FT = _platform("ft", "시장불량 이력", "failuretrace", ["pd_defect_search", "pd_defect_stats"],
               "{{steps.anchor.model_key}} 의 스웰링 불량 기록을 유형 · 시기별로", uses=["anchor"], calls=6)
FT0 = _platform("ft", "시장불량 이력", "failuretrace", ["pd_defect_search", "pd_defect_stats"],
                "SM-S938 의 스웰링 불량 기록을 유형 · 시기별로", calls=6)
MECH = _delib_step("mech", "메커니즘", "불량의 메커니즘을 밝힌다", uses=["ft"])

OBJ_TEXT = json.dumps({"matches": [{"plm_model": "SM-S938", "name": "Galaxy S26"},
                                   {"plm_model": "SM-S936", "name": "Galaxy S26+"}]}, ensure_ascii=False)
DEFECT_TEXT = json.dumps({"rows": [{"id": "D-1", "symptom": "스웰링", "quarter": "Q3"},
                                   {"id": "D-2", "symptom": "스웰링", "quarter": "Q3"}], "total": 2}, ensure_ascii=False)
STATS_TEXT = json.dumps({"by_quarter": {"Q2": 1, "Q3": 7}}, ensure_ascii=False)
DECISION = "## 결정\n\n스웰링의 메커니즘은 셀 팽창에 따른 실링부 크랙이다. 3분기 고온 환경이 촉발 요인이다."


def _backend(bk, orig, _args):
    """단계 안 도구의 가짜 결과 — (isError, 글)."""
    return {("core-hub", "object_resolve"): (False, OBJ_TEXT),
            ("failuretrace", "pd_defect_search"): (False, DEFECT_TEXT),
            ("failuretrace", "pd_defect_stats"): (False, STATS_TEXT)}.get((bk, orig), (True, f"모르는 도구 {bk}:{orig}"))


class _Refuse(Exception):
    """포털 · 게이트웨이가 호출자에게 그대로 말하는 거절 — 게이트웨이가 `ROUTINE_ERROR: <글>` 로 낸다."""


def _ok(obj) -> tuple[list, bool]:
    return [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}], False


def _norm_keys(keys: dict) -> dict:
    return {str(k)[:32]: (v if isinstance(v, str) else str(v))[:200] for k, v in keys.items() if v is not None}


def _envelope_errors(env: dict) -> list[str]:
    """포털 Envelope(extra=forbid · 상한) 판정 — 넘치면 자르지 않고 422 다(routines.py Envelope · Finding · Candidate)."""
    errs = []
    if env.get("status") not in STATES:
        errs.append(f"status: Input should be one of {', '.join(STATES)}")
    for k, cap in (("reason", 500), ("summary", 6000)):
        v = env.get(k, "")
        if not isinstance(v, str) or len(v) > cap:
            errs.append(f"{k}: String should have at most {cap} characters")
    keys = env.get("keys", {})
    if not isinstance(keys, dict) or len(keys) > 16:
        errs.append("keys: Dictionary should have at most 16 items")
    fs = env.get("findings", [])
    if not isinstance(fs, list) or len(fs) > 20:
        errs.append("findings: List should have at most 20 items")
    else:
        for i, f in enumerate(fs):
            refs = f.get("refs", []) if isinstance(f, dict) else None
            if not (isinstance(f, dict) and not set(f) - {"text", "refs"} and isinstance(f.get("text"), str)
                    and 1 <= len(f["text"]) <= 500 and isinstance(refs, list) and len(refs) <= 10
                    and all(isinstance(r, str) for r in refs)):
                errs.append(f"findings.{i}: text 1 ~ 500자 · refs 10개까지 · 다른 칸 없음")
    cs = env.get("candidates", [])
    if not isinstance(cs, list) or len(cs) > 20:
        errs.append("candidates: List should have at most 20 items")
    else:
        for i, c in enumerate(cs):
            if not (isinstance(c, dict) and set(c) == {"label", "key"}
                    and all(isinstance(c[k], str) and 1 <= len(c[k]) <= 200 for k in ("label", "key"))):
                errs.append(f"candidates.{i}: label · key 1 ~ 200자 · 다른 칸 없음")
    return errs


class FakePortal:
    """포털 /routines-api 원장 + 게이트웨이 루틴 도구 여섯 + 심의 도구 넷(시작 · 진행 · 결과 · 접기) + 화면이 포털로 바로 가는 둘(멈춤 · 고르기).
    실행은 하나만 둔다(RUN_ID)."""

    def __init__(self, steps, *, sections=None, upload="auto", backend=None, delib_statuses=("running", "done"), limits=None):
        self.plan = [dict(s) for s in steps]
        self.sections = list(sections or [*(s["id"] for s in steps), "insight"])
        self.upload = upload
        self.backend = backend or _backend
        self.limits = dict(limits or {"minutes": 60, "calls": 60})
        self.time_up = False                # 마감이 지났다(포털 _limit 의 시간 상한) — 이어서(resume)가 마감을 다시 잡으면 풀린다
        self.run = None
        self.stage = {s["id"]: {"state": "pending", "calls": 0, "reason": "", "envelope": {}, "flags": []} for s in steps}
        self.ledger: list[dict] = []
        self.log: list[tuple] = []          # (도구, 인자) — tools/call 마다 차례대로(심의 진행 직접 조회 포함)
        self.call_names: list[tuple] = []   # (단계, routine_call 의 name)
        self.refusals: list[tuple] = []     # (도구, 거절 글)
        self.unexpected: list[str] = []
        self.starts: list[dict] = []        # routine_run_start 인자
        self.resumes = 0
        self.done_log: list[tuple] = []     # (단계, routine_step_done 인자)
        self.step_done: dict = {}           # 단계 → 마지막으로 받은 인자
        self.step_done_out: dict = {}       # 단계 → 포털이 돌려준 것
        self.report_args = None
        self.report_out = None
        self.reports = 0
        self.inflight = 0
        self.max_inflight = 0
        self.call_delay = 0.03              # routine_call 이 머무는 시간 — 겹쳐 나가면 보이게
        self.hold_at = None
        self.held = None
        self.release = None
        self.delib_statuses = list(delib_statuses)
        self.delib_last = None
        self.delib_start_sent = None        # 실행기가 보낸 deliberate_start 인자(게이트웨이가 채우기 전)
        self.job = None                     # 엔진의 심의 잡(시작이 받아들여진 뒤) — {"status"}
        self.polls = 0                      # deliberate_status 직접 조회 수
        self.delib_cancels: list = []       # deliberate_cancel 의 job_id(직접 부른 차례대로)
        self.cancel_at_poll = None          # 이 차례의 진행 조회 때 사람이 '멈춤'(포털 /cancel)을 누른다
        self.time_up_at_poll = None         # 이 차례의 진행 조회 때 실행 마감이 지난다
        self.foreign_call_at_poll = None    # 이 차례의 진행 조회 때 같은 원장을 쓰는 다른 실행자(개인 Claude)가 그 단계 호출을 하나 남긴다
        self.pause_on_start = False         # 첫 deliberate_start 때 운영이 새 심의를 멈춘다(update-all 의 /drain delib)
        self.paused_refusals = 0            # 엔진이 멈춤으로 거절한 시작 수
        self.report_calls: list = []        # routine_report_submit 인자(차례대로)
        self.ra_drafts: list = []           # 게이트웨이가 그 사람 이름으로 만든 RA 초안(create_report_draft 인자)

    # 시험이 쓰는 손잡이
    def arm_hold(self, step):
        """도는 이벤트 루프 안에서 부른다 — 그 단계의 첫 routine_call 을 release 까지 붙잡는다(살아 있는 실행을 만든다)."""
        self.hold_at, self.held, self.release = step, asyncio.Event(), asyncio.Event()

    def seed(self, state="running", note=""):
        """이미 있는 실행(이어서 · 다시 붙기). note — 시작 때 남긴 사용자가 덧붙인 말."""
        self.run = {"id": RUN_ID, "version_no": 3, "state": state, "reason": None, "note": note}
        return self

    def cancel(self):
        """포털 POST /runs/{id}/cancel — 화면의 '멈춤'(게이트웨이를 거치지 않는다)."""
        if self.run is None:
            raise _Refuse("실행을 찾을 수 없습니다")
        if self.run["state"] not in ("queued", "running", "waiting"):
            raise _Refuse(f"실행이 이미 {self.run['state']} 상태다")
        self.run.update(state="cancelled", reason="사람이 멈췄다")
        return {"ok": True, "state": "cancelled"}

    def pick(self, step, key):
        """포털 POST /runs/{id}/steps/{step}/pick — 화면의 고르기 단추(게이트웨이를 거치지 않는다). 기다리는(waiting) 실행에서만 고른다 —
        멈춘 · 끝난 실행의 단추가 남아 있어도 그 실행을 running 으로 되살리지 않는다(R3 §8). 고른 키는 단계의 첫 기대 키로 넘긴다."""
        if self.run is None:
            raise _Refuse("실행을 찾을 수 없습니다")
        if self.run["state"] != "waiting":
            raise _Refuse(f"실행이 {self.run['state']} 다 — 고를 수 없다")
        st = self.stage.get(step)
        if st is None:
            raise _Refuse(f"이 실행에 단계 {step!r} 가 없다")
        if st["state"] != "ambiguous":
            raise _Refuse(f"단계 {step} 는 고를 것이 없다({st['state']})")
        env = st["envelope"]
        if key not in {c["key"] for c in env.get("candidates") or []}:
            raise _Refuse("후보에 없는 키다")
        ps = next(p for p in self.plan if p["id"] == step)
        name = (ps["expect"]["keys"] or ["key"])[0]
        st.update(state="ok", reason="", envelope={**env, "keys": {**(env.get("keys") or {}), name: key}},
                  flags=list(st["flags"]) + [f"사람이 고른 후보 — {name}={key}"])
        self.run.update(state="running", reason=None)
        view = self.view()
        nxt = next((s for s in view["steps"] if s["state"] in ("pending", "running", "ambiguous")), None)
        return {"step": step, "state": "ok", "next_step": nxt, **({} if nxt else {"report": view["report"]})}

    def seed_closed(self, step, state, envelope, results=()):
        for text in results:
            ps = next(p for p in self.plan if p["id"] == step)
            bk = ps["backends"][0]
            self._ledger_add(step, bk, ps["allowed"][bk][0], {"query": "S26"}, True, text)
        self.stage[step].update(state=state, envelope=dict(envelope), reason=envelope.get("reason") or "")

    def current_step(self):
        return next((s["id"] for s in self.plan if self.stage[s["id"]]["state"] in ("pending", "running")), None)

    def calls_of(self, step):
        return [r for r in self.ledger if r["step"] == step]

    # 원장 · 보기
    def _ledger_add(self, step, bk, orig, args, ok, text):
        st = self.stage[step]
        st["calls"] += 1
        if st["state"] == "pending":
            st["state"] = "running"
        cid = f"{step}.c{st['calls']}"
        self.ledger.append({"call_id": cid, "step": step, "backend": bk, "tool": orig, "args": args, "ok": ok, "text": text})
        return cid

    def _add_call(self, step, bk, orig, args, ok, text):
        """포털 add_call — 게이트웨이가 부른 뒤 남기는 한 줄. 상태 · 예산 · 상한을 여기서도 본다(부르는 사이에 멈춘 실행은 이 거절을 받는다).
        심의 단계의 결정문 읽기는 단계 예산을 보지 않고, 시간 상한에도 받는다 — 호출 상한은 그대로다(R3-D5 · §8)."""
        ps = next(p for p in self.plan if p["id"] == step)
        st = self.stage[step]
        result_read = (bk, orig) == ("hwax-deliberation", "deliberate_result")
        if self.run["state"] != "running":
            raise _Refuse(f"실행이 {self.run['state']} 상태다")
        if st["state"] not in ("pending", "running"):
            raise _Refuse(f"단계 {step} 는 이미 닫혔다({st['state']})")
        if not (result_read and ps.get("kind") == "deliberation") and st["calls"] >= ps["budget"]["calls"]:
            raise _Refuse(f"단계 {step} 의 호출 예산({ps['budget']['calls']}번)을 다 썼다")
        kind, limit = self._limit()
        if limit and not (kind == "time" and result_read):
            raise _Refuse(f"실행 상한에 닿았다({limit})")
        return self._ledger_add(step, bk, orig, args, ok, text)

    def _render(self, text):
        keys = {f"steps.{s['id']}.{k}": v for s in self.plan
                for k, v in (self.stage[s["id"]]["envelope"].get("keys") or {}).items()}
        return re.sub(r"\{\{\s*([a-z0-9_.]+)\s*\}\}", lambda m: keys.get(m.group(1), m.group(0)), text)

    def _limit(self):
        """포털 _limit — 도는 실행이 닿은 상한 (종류, 까닭). 호출 상한을 먼저 본다(둘 다 닿았으면 calls — 시간 상한 예외인 결정문 읽기가
        호출 상한까지 넘지 않게)."""
        if self.run is None or self.run["state"] != "running":
            return None, None
        if len(self.ledger) >= self.limits["calls"]:
            return "calls", f"호출 {self.limits['calls']}번"
        if self.time_up:
            return "time", f"시간 {self.limits['minutes']}분"
        return None, None

    def view(self):
        steps = []
        for ps in self.plan:
            st = self.stage[ps["id"]]
            env = st["envelope"]
            row = {**ps, "goal": self._render(ps["goal"]), "state": st["state"], "calls": st["calls"], "reason": st["reason"] or "",
                   "summary": env.get("summary") or "", "keys": env.get("keys") or {}, "findings": env.get("findings") or [],
                   "flags": list(st["flags"])}
            if st["state"] == "ambiguous":
                row["candidates"] = env.get("candidates") or []
            steps.append(row)
        run = self.run
        kind, limit = self._limit()
        return {"contract": 1,
                "run": {"id": run["id"], "routine_id": ROUTINE_ID, "version_no": run["version_no"], "title": TITLE,
                        "executor": "chat", "state": run["state"], "reason": run.get("reason"), "calls": len(self.ledger),
                        "started_at": 1793300000.0, "deadline": 1793303600.0, "ended_at": None,
                        "ra_report_id": run.get("ra_report_id"), "ra_error": run.get("ra_error"), "limits": dict(self.limits),
                        "limit_reached": limit, "limit_kind": kind, "report_ready": bool(run.get("report_md"))},
                "note": run.get("note") or "", "inputs": dict(INPUTS), "purpose": PURPOSE, "steps": steps,
                "next_step": next((s["id"] for s in steps if s["state"] in ("pending", "running", "ambiguous")), None),
                "write_class": "gateway_read",
                "report": {"title": "S26 스웰링 분석", "sections": list(self.sections), "upload": self.upload}}

    # 게이트웨이 도구
    async def call(self, name, args):
        self.log.append((name, args))
        try:
            if name == "routine_run_start":
                return _ok(self._run_start(args))
            if name == "routine_get":
                return _ok(self._get(args))
            if name == "routine_call":
                return await self._routine_call(args)
            if name == "routine_step_done":
                return _ok(self._step_done(args))
            if name == "routine_report_submit":
                return _ok(self._report(args))
            if name == "deliberate_status":
                return self._delib_status(args)
            if name == "deliberate_cancel":
                return self._delib_cancel(args)
        except _Refuse as exc:
            self.refusals.append((name, str(exc)))
            return [{"type": "text", "text": f"ROUTINE_ERROR: {exc}"}], True
        self.unexpected.append(name)
        return [{"type": "text", "text": f"Unknown tool: {name}"}], True

    def _need_run(self, run_id):
        # 포털 _run — 없는 실행과 남의 실행은 같은 404 글이다(게이트웨이가 그 글을 그대로 ROUTINE_ERROR 로 낸다)
        if self.run is None or str(run_id or "").strip() != self.run["id"]:
            raise _Refuse("실행을 찾을 수 없습니다")

    def _run_start(self, a_):
        self.starts.append(dict(a_))
        rid = str(a_.get("run_id") or "").strip()
        if rid:                       # 이어서 — 나머지 인자는 안 본다(R3 §3.4-1 · §3.5-1 POST /runs/{id}/resume)
            self._need_run(rid)
            st = self.run["state"]
            if st == "waiting":
                raise _Refuse("고를 것이 있다 — 고른 뒤 이어서")
            if st not in ("running", "interrupted"):
                raise _Refuse(f"실행이 {st} 다")
            self.run["state"] = "running"
            self.time_up = False      # 마감이 지났으면 '지금 + 상한 분' 으로 다시 잡는다
            self.resumes += 1
            return self.view()
        if not a_.get("routine_id"):
            raise _Refuse("routine_id 또는 run_id 를 준다")
        conv, note = str(a_.get("conversation_id") or "").strip(), str(a_.get("note") or "").strip()
        if len(conv) > 64:
            raise _Refuse("conversation_id 는 64자까지다")
        if len(note) > 2000:
            raise _Refuse("note 는 2000자까지다")
        if a_["routine_id"] != ROUTINE_ID:
            raise _Refuse("그런 루틴이 없다")
        if self.run is not None:
            raise _Refuse("시험 포털은 실행을 하나만 만든다 — 두 번째 새 실행이 왔다")
        self.run = {"id": RUN_ID, "version_no": int(a_.get("version") or 1), "state": "running", "reason": None,
                    "note": note}     # 사용자가 덧붙인 말 — 실행에 남는다(포털 RunIn.note, 실행 보기의 맨 위 note)
        return self.view()

    def _get(self, a_):
        rid = str(a_.get("run_id") or "").strip()
        if not rid:
            if a_.get("routine_id") != ROUTINE_ID:
                raise _Refuse("routine_id 또는 run_id 를 준다")
            return {"contract": 1, "routine_id": ROUTINE_ID, "version_no": 3, "title": TITLE, "purpose": PURPOSE,
                    "vars": [{"key": "model", "label": "모델"}, {"key": "symptom", "label": "증상"}],
                    "steps": [dict(p) for p in self.plan], "limits": dict(self.limits), "write_class": "gateway_read",
                    "report": {"title": "{{model}} {{symptom}} 분석", "sections": list(self.sections), "ra": {"upload": self.upload}}}
        self._need_run(rid)
        if a_.get("call_id"):
            row = next((r for r in self.ledger if r["call_id"] == a_["call_id"]), None)
            if row is None:
                raise _Refuse(f"원장에 호출 {a_['call_id']!r} 가 없다")
            text, page = row["text"], max(1, int(a_.get("page") or 1))
            pages = max(1, -(-len(text) // PAGE_CHARS))
            if page > pages:
                raise _Refuse(f"쪽은 {pages}쪽까지다")
            return {"call_id": row["call_id"], "step": row["step"], "tool": f"{row['backend']}:{row['tool']}", "ok": row["ok"],
                    "chars": len(text), "truncated": False, "page": page, "pages": pages,
                    "text": text[(page - 1) * PAGE_CHARS: page * PAGE_CHARS]}
        return self.view()

    def _fence(self, ps, name):
        """게이트웨이 _routine_call 의 이름 풀기 — '백엔드:원래 이름' · 노출 이름 · 원래 이름, 울타리 밖(이 게이트웨이에 없는 도구 포함)은
        거절. (백엔드, 원래 이름, 노출 이름)을 돌려준다."""
        backends = list(ps["backends"])
        allowed = {b: set(v or []) for b, v in ps["allowed"].items()}
        hits = [b for b in backends if name in allowed.get(b, set())]
        resolved = None
        if ":" in name and name.split(":", 1)[0] in backends:
            resolved = tuple(name.split(":", 1))
        elif len(hits) > 1:
            raise _Refuse(f"'{name}' 은 {', '.join(hits)} 에 다 있다 — '백엔드:원래 이름' 으로 부른다")
        elif name in ROUTE:
            resolved = ROUTE[name]
        if resolved is None or resolved[0] not in backends:
            resolved = (hits[0], name) if hits else resolved
        exposed = _EXPOSED.get(resolved) if resolved is not None else None
        if resolved is None or resolved[0] not in backends or resolved[1] not in allowed.get(resolved[0], set()) or exposed is None:
            shown = " · ".join(f"{b}: {', '.join(sorted(v)[:10])}{' …' if len(v) > 10 else ''}" for b, v in allowed.items() if v)
            raise _Refuse(f"울타리 밖 — 단계 {ps['id']} 는 {', '.join(backends) or '(없음)'} 의 허용 도구만 부른다({shown or '허용 도구 없음'})")
        return resolved[0], resolved[1], exposed

    def _evidence(self, ps):
        return [{"source": f"{r['backend']}:{r['tool']}", "tool": r["tool"], "args": r["args"], "result": r["text"][:15000],
                 "key": r["call_id"]} for r in self.ledger if r["step"] in ps["uses"] and r["ok"]][:120]

    async def _routine_call(self, a_):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            run_id, step = str(a_.get("run_id") or "").strip(), str(a_.get("step") or "").strip()
            name, inner = str(a_.get("name") or "").strip(), a_.get("arguments") or {}
            self.call_names.append((step, name))
            if self.hold_at == step and self.held is not None and not self.held.is_set():
                self.held.set()
                await self.release.wait()
            if not (run_id and step and name):
                raise _Refuse("run_id · step · name 을 준다")
            if not isinstance(inner, dict):
                raise _Refuse("arguments 는 객체여야 한다")
            # 게이트웨이 _routine_call — 실행 보기(GET /runs/{id})로 판정하고, 부른 뒤 포털 원장(POST /runs/{id}/calls)에 남긴다
            self._need_run(run_id)
            view = self.view()
            run = view["run"]
            if run["state"] != "running":
                raise _Refuse(f"실행이 {run['state']} 상태다 — 도구를 부르지 않는다")
            ps = next((s for s in view["steps"] if s["id"] == step), None)
            if ps is None:
                raise _Refuse(f"이 실행에 단계 {step!r} 가 없다")
            if ps["state"] not in ("pending", "running"):
                raise _Refuse(f"단계 {step} 는 이미 닫혔다({ps['state']})")
            spent = ps["calls"] >= ps["budget"]["calls"]
            spent_msg = f"단계 {step} 의 호출 예산({ps['budget']['calls']}번)을 다 썼다 — routine_step_done 으로 닫는다"
            delib_step = ps.get("kind") == "deliberation"
            if spent and not delib_step:     # 심의 단계는 도구를 푼 뒤에 본다 — 결정문 읽기는 단계 예산 밖이다(R3 §8)
                raise _Refuse(spent_msg)
            bk, orig, exposed = self._fence(ps, name)
            if not (delib_step and bk == "hwax-deliberation" and orig in RT_DELIB_TOOLS) and not _routine_read(exposed, bk, orig):
                raise _Refuse(f"단계 안에서는 읽기로 확인된 도구만 부른다(v1) — '{bk}:{orig}' 는 읽기로 확인되지 않았다"
                              "(readOnlyHint · 읽기 목록 · 이름 꼴). 읽기 도구라면 앱이 readOnlyHint 를 달거나 박스의 read_tools.local.json 에 "
                              "더한다")
            if spent and (bk, orig) != ("hwax-deliberation", "deliberate_result"):
                raise _Refuse(spent_msg)
            # 실행 상한 — 시간 상한 뒤에도 이미 세운 심의의 결정문 읽기는 연다(R3-D5). 호출 상한은 그대로다
            if run["limit_reached"] and not (run["limit_kind"] == "time" and (bk, orig) == ("hwax-deliberation", "deliberate_result")):
                raise _Refuse(f"실행 상한에 닿았다({run['limit_reached']}) — 남은 단계를 닫고 보고서를 낸다")
            if (bk, orig) == ("hwax-deliberation", "deliberate_start"):
                self.delib_start_sent = dict(inner)
                if not inner.get("evidence"):
                    inner = {**inner, "evidence": self._evidence(ps)}
                inner = {"save_report": False, **inner}     # R3 §3.4-2 — 없으면 게이트웨이가 false 를 넣는다
            await asyncio.sleep(self.call_delay)    # 안쪽 도구가 도는 동안 — 이 사이에 실행이 멈추면 포털 원장 쓰기가 거절한다
            is_err, text = self._inner(bk, orig, inner)
            cid = self._add_call(step, bk, orig, inner, not is_err, text)
            head = {"routine_call": cid, "step": step, "tool": f"{bk}:{orig}", "ok": not is_err, "chars": len(text)}
            if len(text) > PAGE_CHARS:
                pages = -(-len(text) // PAGE_CHARS)
                head.update(pages=pages, next=f"routine_get(run_id, call_id={cid}, page=2) — 이 답은 1/{pages}쪽")
                body = [{"type": "text", "text": text[:PAGE_CHARS]}]
            else:
                body = [{"type": "text", "text": text}]
            return [{"type": "text", "text": json.dumps(head, ensure_ascii=False)}, *body], is_err
        finally:
            self.inflight -= 1

    def _delib_view(self, status):
        out = {"job_id": JOB_ID, "job": "mechanism", "label": "메커니즘 규명", "status": status, "question": "…",
               "chair_template": "diagnosis", "sealed": False, "stage": "좌석 발언" if status == "running" else "끝",
               "step": "라운드 1 좌석 발언" if status == "running" else "결정문", "round": 1 if status == "running" else 2,
               "total_rounds": 2, "seats": ["rel-drop", "mat-poly"], "elapsed_s": 12.5, "report_id": None, "turn_count": 3,
               "checkpoint": False, "error": "취소됨" if status == "cancelled" else None}
        if status == "running":
            out.update(idle_s=1.0, last_step="라운드 1 좌석 발언")
        return out

    def _inner(self, bk, orig, inner):
        if bk != "hwax-deliberation":
            return self.backend(bk, orig, inner)
        if orig == "deliberate_start":      # 엔진 deliberate_start 의 답(delib_jobs.summary 꼴 · FastMCP 는 들여 쓴 JSON 글)
            if self.pause_on_start and not self.paused_refusals:
                # 운영이 새 심의를 멈춘다(update-all 의 /drain delib) — 깃발은 진짜 delib_jobs 의 것이다(시험이 JOB_DIR 을 임시 자리로 돌린다)
                delib_jobs.pause_state().set_on(scope="delib", by="test:update-all", reason="루틴 시험 점검", hold=True)
            paused = delib_jobs.pause_info()
            if paused:
                # 엔진 delib_jobs.start 의 거절(DelibPaused) — FastMCP 는 도구 예외를 'Error executing tool …' 글 + isError 로 낸다
                self.paused_refusals += 1
                return True, f"Error executing tool deliberate_start: {delib_jobs.pause_message(paused)}"
            self.job = {"status": "queued"}
            return False, json.dumps({**self._delib_view("queued"), "question": inner.get("question") or "",
                                      "stage": None, "step": None, "round": None, "total_rounds": None, "seats": [],
                                      "elapsed_s": 0.0, "turn_count": 0, "queue": {"position": 1, "ahead": 0},
                                      "note": "줄을 섰다 — 1번째"}, ensure_ascii=False, indent=2)
        if inner.get("job_id") != JOB_ID or self.job is None:
            return True, f"Error executing tool {orig}: 그런 심의 잡이 없다: {inner.get('job_id')} — deliberate_list 로 확인하라"
        st = self.delib_last or "running"
        out = self._delib_view(st)
        if orig == "deliberate_result":
            out.update(decision=DECISION if st == "done" else None, plain="실링부가 부풀어 갈라진다.", warnings=[],
                       warnings_total=0, evidence_omitted=[], steps=["좌석 발굴", "라운드 1", "라운드 2", "결정문"], steps_total=4)
        return False, json.dumps(out, ensure_ascii=False, indent=2)

    def _delib_status(self, a_):
        """심의 진행 — 실행기가 routine_call 을 거치지 않고 직접 부른다(R3-D5). 대본(delib_statuses)을 앞에서부터 낸다(접힌 잡은 cancelled)."""
        if a_.get("job_id") != JOB_ID or self.job is None:
            return [{"type": "text", "text": f"Error executing tool deliberate_status: 그런 심의 잡이 없다: {a_.get('job_id')}"}], True
        self.polls += 1
        if self.job["status"] != "cancelled":
            self.job["status"] = self.delib_statuses.pop(0) if len(self.delib_statuses) > 1 else self.delib_statuses[0]
        st = self.delib_last = self.job["status"]
        if self.polls == self.cancel_at_poll:
            self.cancel()                   # 사람이 화면에서 '멈춤' — 포털 원장이 cancelled 가 된다(심의는 엔진에서 계속 돈다)
        if self.polls == self.time_up_at_poll:
            self.time_up = True             # 심의를 지켜보는 사이 실행 마감이 지났다
        out = {**self._delib_view(st), "evidence_omitted": [], "warnings": [], "warnings_total": 0}
        if self.polls == self.foreign_call_at_poll:
            # 다른 실행자의 routine_call(진행 조회) — 같은 포털 원장 규칙으로 한 줄(실행 호출 수를 하나 쓴다)
            step = next(p["id"] for p in self.plan if p["kind"] == "deliberation")
            self._add_call(step, "hwax-deliberation", "deliberate_status", {"job_id": JOB_ID}, True,
                           json.dumps(out, ensure_ascii=False, indent=2))
        if st == "running":
            out["quiet_ok_s"] = 3608
        return [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}], False

    def _delib_cancel(self, a_):
        """심의 접기 — 실행기가 routine_call 없이 직접 부른다(멈춘 실행은 원장에 못 쓴다, R3 §8). 답은 엔진 delib_jobs.cancel 꼴."""
        jid = a_.get("job_id")
        self.delib_cancels.append(jid)
        if jid != JOB_ID or self.job is None:
            return [{"type": "text", "text": f"Error executing tool deliberate_cancel: 그런 심의 잡이 없다(또는 이미 이 프로세스 밖이다): {jid}"}], True
        if self.job["status"] == "queued":
            out = {"job_id": JOB_ID, "status": "cancelled", "note": "대기열에서 뺐다 — 시작하지 않은 심의라 남은 것이 없다."}
            self.job["status"] = "cancelled"
        elif self.job["status"] != "running":
            out = {"job_id": JOB_ID, "status": self.job["status"], "note": "이미 끝난 잡이다 — 취소할 것이 없다"}
        else:
            out = {"job_id": JOB_ID, "status": "cancelling", "note": "취소를 요청했다. 잠시 뒤 deliberate_status 로 확인하라(저장은 중단된다)."}
            self.job["status"] = "cancelled"
        return [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=2)}], False

    def _step_done(self, a_):
        """포털 step_done — 봉투를 원장과 맞대 고친다(routine_routes.py 의 규칙 · 표지 글 그대로). 게이트웨이는 여섯 칸만 넘긴다."""
        step = str(a_.get("step") or "")
        if not (a_.get("run_id") and step):
            raise _Refuse("run_id 와 step 을 준다")
        self.done_log.append((step, dict(a_)))
        self.step_done[step] = dict(a_)
        env = {k: a_[k] for k in ("status", "reason", "summary", "keys", "findings", "candidates") if k in a_}
        errs = _envelope_errors(env)
        if errs:
            raise _Refuse("단계 봉투가 틀렸다 — " + " / ".join(errs[:6]))
        self._need_run(a_["run_id"])
        ps = next((p for p in self.plan if p["id"] == step), None)
        if ps is None:
            raise _Refuse(f"이 실행에 단계 {step!r} 가 없다")
        if self.run["state"] not in ("running", "waiting"):
            raise _Refuse(f"실행이 {self.run['state']} 상태다")
        st = self.stage[step]
        if st["state"] not in ("pending", "running", "ambiguous"):
            raise _Refuse(f"단계 {step} 는 이미 닫혔다({st['state']})")
        calls = self.calls_of(step)
        # 키의 근거 — 성공한 호출의 (결과, 인자 JSON)(포털 step_done 의 C5 — 실패한 호출의 글 · 받은 인자를 되돌려 준 값은 근거가 아니다)
        sources = [(c["text"], json.dumps(c["args"], ensure_ascii=False)) for c in calls if c["ok"]]
        flags: list[str] = []
        status = env["status"]
        if status in ("ok", "empty") and not calls:
            status = "skipped"
            flags.append("호출 없이 닫으려 했다 — 건너뜀으로 고쳤다")
        if status == "ok" and calls and not any(c["ok"] for c in calls):
            status = "failed"
            flags.append("결과가 다 오류다 — 실패로 고쳤다")
        want = set(ps["expect"]["keys"])
        picked = st["state"] == "ambiguous" and {c["key"] for c in (st["envelope"].get("candidates") or [])}
        keys, unsourced, echoed, unexpected = {}, [], [], []
        for k, v in _norm_keys(env.get("keys") or {}).items():
            if want and k not in want:
                unexpected.append(k)
            elif (v and any(v in res and v not in args for res, args in sources)) or (picked and v in picked):
                keys[k] = v
            elif v and any(v in c["text"] for c in calls):
                echoed.append(k)
            else:
                unsourced.append(k)
        if unexpected:
            flags.append(f"계획에 없는 키는 뺐다: {', '.join(unexpected)}")
        if unsourced:
            flags.append(f"결과에 글자 그대로 없는 키는 넘기지 않는다: {', '.join(unsourced)}")
        if echoed:
            flags.append(f"받은 인자를 되돌려 준 값 · 실패한 호출에만 있는 키는 넘기지 않는다: {', '.join(echoed)}")
        known = {c["call_id"] for c in self.ledger}
        findings, bad = [], []
        for f in env.get("findings") or []:
            refs = list(f.get("refs") or [])
            bad += [r for r in refs if r not in known]
            findings.append({"text": f["text"], "refs": [r for r in refs if r in known]})
        if bad:
            flags.append(f"원장에 없는 인용은 뗐다: {', '.join(sorted(set(bad))[:10])}")
        cands = [dict(c) for c in env.get("candidates") or []]
        if status == "ambiguous" and not cands:
            status = "failed"
            flags.append("후보 없이 '여럿' 이라 했다 — 실패로 고쳤다")
        st.update(state=status, reason=env.get("reason") or "", flags=flags,
                  envelope={"status": env["status"], "summary": env.get("summary") or "", "keys": keys, "findings": findings,
                            "candidates": cands})
        if status == "ambiguous":
            self.run.update(state="waiting", reason=f"단계 {step} — 사람이 후보를 고른다")
        elif self.run["state"] == "waiting":
            self.run.update(state="running", reason=None)
        view = self.view()
        nxt = next((s for s in view["steps"] if s["state"] in ("pending", "running", "ambiguous")), None)
        out = {"step": step, "state": status, "flags": flags, "run_state": view["run"]["state"], "next_step": nxt}
        if nxt is None:
            out["report"] = view["report"]
        self.step_done_out[step] = out
        return out

    def _ra_state(self):
        """포털 _ra_state — 실행 행의 지금 RA 결과와 올리기 방식(렌더 저장본에 넣지 않는다 — /ra 가 뒤에 고친다)."""
        return {"ra_upload": self.upload, "ra_report_id": self.run.get("ra_report_id"), "ra_error": self.run.get("ra_error")}

    def _report(self, a_):
        """게이트웨이 _routine_report — 포털 POST /runs/{id}/report 로 렌더하거나(끝난 실행이면 저장본, R3-D12) 올리기 방식대로 그 사람 이름의
        RA 초안을 만들고 POST /runs/{id}/ra 로 적는다. 초안이 이미 있으면 다시 만들지 않는다. 'ask' 는 upload=true 일 때만 올린다."""
        if not str(a_.get("run_id") or "").strip():
            raise _Refuse("run_id 를 준다")
        self._need_run(a_.get("run_id"))
        self.report_args = dict(a_)
        self.report_calls.append(dict(a_))
        made = self._render_or_saved(a_)
        mode = (made.get("ra") or {}).get("upload") or "off"
        out = {"run_id": self.run["id"], "coverage": made.get("coverage"), "warnings": made.get("warnings") or [],
               "report_chars": len(made.get("report_md") or ""), "ra_upload": mode, "report_md": made.get("report_md") or ""}
        if made.get("ra_report_id"):
            out.update(ra_report_id=str(made["ra_report_id"]),
                       ra_note="이 실행의 Report Archive 초안은 이미 있다 — 다시 만들지 않고 그 id 를 돌려준다")
        elif mode == "auto" or (mode == "ask" and a_.get("upload") is True):
            ra = made["ra"]
            self.ra_drafts.append({"template_id": "deliberation", "template_version": 1, "title": ra["title"],
                                   "blocks": ra["blocks"], "tags": ra["tags"]})
            self.run.update(ra_report_id="ra-test-0042", ra_error=None)        # 포털 /ra — 끝난 실행에만 적는다
            out["ra_report_id"] = "ra-test-0042"
        elif mode == "ask":
            out["ra_next"] = "사람에게 Report Archive 초안으로 올릴지 묻고, 올리기로 하면 같은 run_id 로 upload=true 를 붙여 다시 낸다"
        self.report_out = out
        return out

    def _render_or_saved(self, a_):
        """포털 POST /runs/{id}/report — 끝난 실행에 저장본이 있으면 다시 렌더하지 않고 그것을(새 절은 보지 않는다), 아니면 도는 실행에서
        모든 단계가 닫혔을 때만 렌더해 저장하고 실행을 끝낸다. 두 길 모두 실행 행의 RA 결과를 함께 싣는다."""
        if self.run["state"] == "done" and self.run.get("report"):
            return {"report_md": self.run.get("report_md") or "", **self.run["report"], **self._ra_state()}
        if self.run["state"] != "running":
            raise _Refuse(f"실행이 {self.run['state']} 상태다 — 보고서는 도는 실행에서만 낸다")
        open_ = [s["id"] for s in self.plan if self.stage[s["id"]]["state"] not in CLOSED]
        if open_:
            raise _Refuse(f"아직 닫히지 않은 단계: {', '.join(open_)} — routine_step_done 으로 닫는다")
        secs = a_.get("sections") or {}
        lines = ["# S26 스웰링 분석", ""]
        for sid in self.sections:
            title = next((p["title"] for p in self.plan if p["id"] == sid), "인사이트")
            env = self.stage.get(sid, {}).get("envelope", {})
            text = secs.get(sid) or (env.get("summary") or "")
            # 포털 render_report — 단계 절에는 저장한 봉투의 넘긴 키 · 발견을 늘 붙인다(단계 절의 정본은 포털이다)
            extra = (["넘긴 키 — " + " · ".join(f"{k}={v}" for k, v in env["keys"].items())] if env.get("keys") else []) + \
                [f"- {f['text']} " + " ".join(f"[{r}]" for r in f.get("refs") or []) for f in env.get("findings") or []]
            lines += [f"## {title}", "", str(text)] + ([""] + extra if extra else []) + [""]
        counts: dict = {}
        for p in self.plan:
            counts[self.stage[p["id"]]["state"]] = counts.get(self.stage[p["id"]]["state"], 0) + 1
        not_ok = [{"step": p["id"], "title": p["title"], "state": self.stage[p["id"]]["state"],
                   "reason": self.stage[p["id"]]["reason"]} for p in self.plan if self.stage[p["id"]]["state"] != "ok"]
        lines += ["## 커버리지", "", " · ".join(f"{k} {v}" for k, v in counts.items()), "", "## 근거 부록", ""]
        lines += [f"- [{r['call_id']}] {r['backend']}:{r['tool']} — {r['text'][:120]}" for r in self.ledger]
        lines += ["", "부록 메모 — " + "이 보고서는 시험 포털이 렌더한 것이다. " * 30]
        md = "\n".join(lines)
        self.reports += 1
        saved = {"coverage": {"counts": counts, "not_ok": not_ok}, "warnings": ["시험 경고 — 인사이트 둘째 문단에 인용이 없다"],
                 "ra": {"upload": self.upload, "title": "S26 스웰링 분석", "blocks": {"body": md[:2000]}, "tags": ["분석 루틴", TITLE]}}
        ra_state = self._ra_state()             # 렌더 앞의 실행 행 — 처음 렌더에는 초안 id 가 없다
        self.run.update(state="done", report_md=md, report=saved)
        return {"report_md": md, **saved, **ra_state}


# ── 가짜 게이트웨이 — MCP streamable HTTP(원 세션 JSON-RPC)를 MockTransport 로 ─────────────────────────────────────────
class FakeGateway:
    """initialize(mcp-session-id 머리) → notifications/initialized(202) → tools/call(SSE, 마지막 data: 줄이 JSON-RPC) → DELETE.
    사람 PAT 가 아닌 자격 · 틀린 Accept · Content-Type 은 진짜처럼 막고 위반으로 적는다."""

    def __init__(self, portal: FakePortal):
        self.portal = portal
        self.requests: list[dict] = []
        self.sessions: dict[str, str] = {}      # sid → open | closed | gone
        self.violations: list[str] = []
        self.inits = 0
        self.expire_once = None                 # 도구 이름 — 그 tools/call 이 처음 오면 세션을 지운다(게이트웨이 재기동 흉내)

    @staticmethod
    def _sse(rid, payload: dict, sid: str) -> httpx.Response:
        env = {"jsonrpc": "2.0", "id": rid, **payload}
        body = f"event: message\r\ndata: {json.dumps(env, ensure_ascii=False, separators=(',', ':'))}\r\n\r\n"
        return httpx.Response(200, content=body.encode("utf-8"),
                              headers={"content-type": "text/event-stream", "cache-control": "no-cache, no-transform",
                                       "mcp-session-id": sid})

    @staticmethod
    def _err(status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"jsonrpc": "2.0", "id": "server-error", "error": {"code": -32600, "message": message}})

    async def handle(self, request: httpx.Request) -> httpx.Response:
        rec = {"method": request.method, "path": request.url.path, "auth": request.headers.get("authorization"),
               "user": request.headers.get("x-hwax-user"), "groups": request.headers.get("x-hwax-groups"),
               "sid": request.headers.get("mcp-session-id"), "accept": request.headers.get("accept") or "",
               "rpc": None, "tool": None, "args": None}
        self.requests.append(rec)
        if rec["auth"] != f"Bearer {PAT}":
            self.violations.append(f"사람 PAT 가 아닌 자격으로 왔다 — {rec['auth']!r}")
            return httpx.Response(401, json={"error": "invalid_token"})
        if request.method == "DELETE":
            if rec["sid"] in self.sessions:
                self.sessions[rec["sid"]] = "closed"
            return httpx.Response(200)
        if request.method != "POST":
            return httpx.Response(405)
        if not (request.headers.get("content-type") or "").startswith("application/json"):
            self.violations.append(f"Content-Type 이 JSON 이 아니다 — {request.headers.get('content-type')!r}")
            return self._err(415, "Unsupported Media Type: Content-Type must be application/json")
        if "application/json" not in rec["accept"] or "text/event-stream" not in rec["accept"]:
            self.violations.append(f"Accept 가 둘을 다 받지 않는다 — {rec['accept']!r}")
            return self._err(406, "Not Acceptable: Client must accept both application/json and text/event-stream")
        msg = json.loads(request.content or b"{}")
        rec["rpc"] = msg.get("method")
        if rec["rpc"] == "initialize":
            sid = uuid.uuid4().hex
            self.sessions[sid] = "open"
            self.inits += 1
            return self._sse(msg.get("id"), {"result": {"protocolVersion": "2024-11-05",
                                                        "capabilities": {"tools": {"listChanged": False}},
                                                        "serverInfo": {"name": "hwax-mcp-gateway", "version": "test"}}}, sid)
        sid = rec["sid"]
        if not sid:
            return self._err(400, "Bad Request: Missing session ID")
        if self.sessions.get(sid) != "open":
            return self._err(404, "Not Found: Invalid or expired session ID")
        if rec["rpc"] == "notifications/initialized":
            return httpx.Response(202, headers={"mcp-session-id": sid})
        if rec["rpc"] == "tools/list":
            return self._sse(msg.get("id"), {"result": {"tools": []}}, sid)
        if rec["rpc"] != "tools/call":
            return self._sse(msg.get("id"), {"error": {"code": -32601, "message": "Method not found"}}, sid)
        params = msg.get("params") or {}
        rec["tool"], rec["args"] = params.get("name"), params.get("arguments") or {}
        if self.expire_once and rec["tool"] == self.expire_once:
            self.expire_once = None
            self.sessions[sid] = "gone"
            return self._err(404, "Not Found: Invalid or expired session ID")
        content, is_error = await self.portal.call(rec["tool"], rec["args"])
        return self._sse(msg.get("id"), {"result": {"content": content, "isError": is_error}}, sid)


# ── 대본 챗 모델 — 묶인 도구가 있으면 작은 ReAct, 없으면 봉투(단계가 열려 있을 때) · 인사이트(다 닫혔을 때) ─────────────────
INSIGHT = ("S26(SM-S938)의 스웰링 불량은 3분기에 몰린다 [ft.c1]. 기준 키는 코어 허브에서 풀었다 [anchor.c1].\n\n"
           "- 남은 빈 곳 — 시험 이력은 이 루틴의 단계에 없다.")
LONG_FT_SUMMARY = "스웰링 불량 기록 두 건이 모두 3분기에 몰려 있다 [ft.c1]. " + "분기별 분포를 보면 3분기 편중이 뚜렷하다. " * 40


class _Brain:
    """모델 호출마다 지금 열린 단계(포털 원장)를 보고 대본을 고른다 — 프롬프트 낱말에 기대지 않는다."""

    def __init__(self, portal: FakePortal):
        self.portal = portal
        self.react: dict = {}       # 단계 → fn(n, messages) -> AIMessage
        self.envelope: dict = {}    # 단계 → fn(이 단계 원장 줄) -> 봉투 dict
        self.insight = INSIGHT
        self.calls: list[dict] = []
        self.binds: list[tuple] = []         # (단계, 묶인 이름들)
        self.bound_tools: list[tuple] = []   # (단계, 묶인 도구 객체들)
        self._n: dict = {}

    def answer(self, bound: tuple, messages: list) -> AIMessage:
        step = self.portal.current_step()
        n = None
        if bound:
            n = self._n.get(step, 0)
            self._n[step] = n + 1
            fn = self.react.get(step)
            msg, kind = (fn(n, messages) if fn else AIMessage(content="정리했다.")), "react"
        elif step is not None:
            fn = self.envelope.get(step) or _env_plain
            msg, kind = AIMessage(content=json.dumps(fn(self.portal.calls_of(step)), ensure_ascii=False)), "envelope"
        else:
            msg, kind = AIMessage(content=self.insight), "insight"
        self.calls.append({"kind": kind, "step": step, "n": n, "bound": bound, "messages": list(messages)})
        return msg


class _ScriptLLM(BaseChatModel):
    """bind_tools 는 같은 대본을 쥔 사본(묶인 이름을 안다)을 돌려준다 — 묶인 이름은 _Brain.binds 에 남는다."""

    brain: Any = None
    bound: tuple = ()
    model_name: str = "routine-script"
    max_tokens: int | None = None
    temperature: float = 0.0
    extra_body: dict | None = None

    @property
    def _llm_type(self) -> str:
        return "routine-script"

    def bind_tools(self, tools, **_kw):
        names = tuple(_tool_name(t) for t in tools)
        self.brain.binds.append((self.brain.portal.current_step(), names))
        self.brain.bound_tools.append((self.brain.portal.current_step(), list(tools)))
        return self.model_copy(update={"bound": names})

    def _generate(self, messages, stop=None, run_manager=None, **_kw):
        return ChatResult(generations=[ChatGeneration(message=self.brain.answer(self.bound, list(messages)))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **_kw):
        return self._generate(messages, stop)


def _tc(name, args):
    return {"name": name, "args": dict(args), "id": f"tc-{uuid.uuid4().hex[:10]}"}


def _calls_then(*calls, final):
    """n 번째 호출에 calls[n] 하나를 부르고, 다 부르면 final 글로 끝낸다."""
    def fn(n, _messages):
        return AIMessage(content="", tool_calls=[_tc(*calls[n])]) if n < len(calls) else AIMessage(content=final)
    return fn


def _parallel_then(calls, *, final):
    """첫 호출에 calls 를 한 메시지의 병렬 tool_calls 로 낸다."""
    def fn(n, _messages):
        return AIMessage(content="", tool_calls=[_tc(*c) for c in calls]) if n == 0 else AIMessage(content=final)
    return fn


def _until_refused(call, *, final, cap=6):
    """마지막 도구 글에 예산 거절이 보일 때까지 같은 도구를 부른다 — 거절 글을 받은 모델은 멈춘다(받지 못하면 cap 까지 부른다)."""
    def fn(n, messages):
        tools = [m for m in messages if isinstance(m, ToolMessage)]
        if (tools and "호출 예산" in _text(tools[-1:])) or n >= cap:
            return AIMessage(content=final)
        return AIMessage(content="", tool_calls=[_tc(*call)])
    return fn


def _ids(rows):
    return [r["call_id"] for r in rows]


def _env_plain(rows):
    ids = _ids(rows)
    return {"status": "ok" if ids else "empty", "reason": "", "summary": f"정리했다 [{ids[0]}]." if ids else "부른 것이 없다.",
            "keys": {}, "findings": [{"text": "결과를 정리했다", "refs": ids[:1]}] if ids else [], "candidates": []}


def _env_anchor(rows):
    ids = _ids(rows) or ["anchor.c1"]
    return {"status": "ok", "reason": "", "summary": f"S26 은 SM-S938 로 풀린다 [{ids[0]}].", "keys": {"model_key": "SM-S938"},
            "findings": [{"text": "S26 의 PLM 모델은 SM-S938 이다", "refs": ids[:1]}], "candidates": []}


def _env_ft(rows):
    ids = _ids(rows) or ["ft.c1"]
    return {"status": "ok", "reason": "", "summary": LONG_FT_SUMMARY, "keys": {},
            "findings": [{"text": "스웰링 불량이 3분기에 몰린다", "refs": ids[:1]}], "candidates": []}


def _text(messages) -> str:
    out = []
    for m in messages:
        c = m[1] if isinstance(m, tuple) else getattr(m, "content", m)
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(x if isinstance(x, str) else str((x or {}).get("text") or "") for x in c)
        else:
            out.append(str(c))
    return "\n".join(out)


# ── 끼우기 · 돌리기 · 프레임 읽기 ───────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def rig(monkeypatch):
    """가짜 게이트웨이 · 포털 · 대본 모델 · 사람 도구를 이음매에 끼운다. rig(steps, **kw) → (portal, gw, brain, rec)."""
    routine._RUNS.clear()
    monkeypatch.setattr(a, "_ctx_cache", {"n": 262_144, "src": "server"})      # 창 — LLM 서버에 묻지 않는다
    monkeypatch.delenv("ROUTINE_MAX_RUNNING", raising=False)
    # 심의 진행을 묻는 간격(ROUTINE_DELIB_POLL_S, 기본 20초) — 환경값과 모듈 손잡이(이름에 POLL 이 든 수) 둘 다 짧게
    monkeypatch.setenv("ROUTINE_DELIB_POLL_S", "1")
    for knob in dir(routine):
        v = getattr(routine, knob)
        if (re.search(r"POLL.*(_S|SEC|SECS|SECONDS|INTERVAL)$", knob.upper()) and isinstance(v, (int, float))
                and not isinstance(v, bool)):
            monkeypatch.setattr(routine, knob, 0.05)
    # 에이전트의 MCP 연결 설정은 서비스 계정 자격을 쥐고 있다 — 실행기가 이것으로 떨어지면 가짜 게이트웨이가 위반으로 적는다
    monkeypatch.setattr(a.app.state, "connections",
                        {"gateway": {"url": GW_URL, "transport": "streamable_http", "headers": {"Authorization": f"Bearer {SVC}"}}},
                        raising=False)

    def build(steps, **kw):
        portal = FakePortal(steps, **kw)
        gw = FakeGateway(portal)
        brain = _Brain(portal)
        llm = _ScriptLLM(brain=brain)
        rec: dict = {"user_tools": [], "direct": []}

        async def user_tools(app, groups, user, user_pat):  # noqa: ARG001 — 이음매와 같은 이름(키워드로 불려도 받는다)
            rec["user_tools"].append({"groups": list(groups or []), "user": user, "user_pat": user_pat})
            return {n: _user_tool(n, rec["direct"]) for n in [*BACKENDS, *sorted(GUIDE)]}

        monkeypatch.setattr(routine, "GW_TRANSPORT", httpx.MockTransport(gw.handle))
        monkeypatch.setattr(routine, "_gateway_url", lambda app: _AwStr(GW_URL))
        monkeypatch.setattr(routine, "_routine_llm", lambda app: _AwTuple((llm, None)))
        monkeypatch.setattr(routine, "_user_tools", user_tools)
        monkeypatch.setattr(routine, "_tool_backends", lambda app: _AwDict(BACKENDS))
        return portal, gw, brain, rec

    yield build
    routine._RUNS.clear()


NEW = {"id": ROUTINE_ID, "version": 3, "inputs": dict(INPUTS), "note": NOTE}


def _req(routine_field, *, message=NOTE, pat=PAT, groups=GROUPS, conv="conv-42", entitlements=ENT, **kw):
    return a.ChatRequest(message=message, conversation_id=conv, groups=list(groups), user_email=USER, user_pat=pat,
                         entitlements=entitlements, routine=routine_field, **kw)


async def _collect(gen) -> list:
    return [c async for c in gen]


async def _pump(gen, sink: list) -> None:
    async for c in gen:
        sink.append(c)


async def _until(pred, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            return False
        await asyncio.sleep(0.02)
    return True


def _play(req, *, settle: FakeGateway | None = None, timeout: float = 20.0) -> list:
    """run_routine 을 끝까지 받는다. settle 을 주면 스트림이 끝난 뒤 그 게이트웨이의 세션이 다 닫힐 때까지 조금 기다린다."""
    async def body():
        chunks = await asyncio.wait_for(_collect(routine.run_routine(a.app, req)), timeout)
        if settle is not None:
            await _until(lambda: all(s != "open" for s in settle.sessions.values()), 2.0)
        return chunks

    return _frames(asyncio.run(body()))


def _turns(*reqs, between=None, timeout: float = 20.0) -> list:
    """같은 실행의 턴 여럿을 한 이벤트 루프에서 차례로 받는다 — 앞 턴의 실행 작업이 끝난 뒤(active_count 0) 다음 턴을 보낸다. between(i) 는
    i 번째 턴 앞에서 부른다(사람이 화면에서 누르는 것 — 고르기 따위). 턴마다 프레임 목록을 돌려준다."""
    async def body():
        out = []
        for i, req in enumerate(reqs):
            if i:
                assert await _until(lambda: routine.active_count() == 0, 5), "앞 턴의 실행 작업이 끝나지 않았다"
                if between is not None:
                    between(i)
            out.append(await asyncio.wait_for(_collect(routine.run_routine(a.app, req)), timeout))
        return out

    return [_frames(chunks) for chunks in asyncio.run(body())]


_FRAME = re.compile(r"event: ([A-Za-z_]+)\ndata: (.*)")


def _frames(chunks, strict: bool = True) -> list:
    """SSE 바이트 → [(event, data)] — 포털 _parse_sse_frame 이 읽는 꼴(event 한 줄 · data 한 줄의 JSON · 빈 줄)이어야 한다. ping 은 뺀다."""
    raw = "".join(c.decode("utf-8") if isinstance(c, (bytes, bytearray)) else str(c) for c in chunks)
    if strict:
        assert raw.endswith("\n\n"), f"스트림이 프레임 경계로 끝나지 않았다 — {raw[-300:]!r}"
    out = []
    for block in raw.split("\n\n")[:-1]:
        m = _FRAME.fullmatch(block)
        if not m:
            assert not strict, f"SSE 꼴이 아니다 — {block[:300]!r}"
            continue
        data = json.loads(m.group(2))
        assert isinstance(data, dict), block[:300]
        out.append((m.group(1), data))
    return [(ev, d) for ev, d in out if ev != "ping"]


def _rt(frames, kind=None) -> list:
    return [d for ev, d in frames if ev == "routine" and (kind is None or d.get("kind") == kind)]


def _is(kind, step=None):
    return lambda ev, d: ev == "routine" and d.get("kind") == kind and (step is None or d.get("step") == step)


def _pos(frames, pred) -> int:
    return next((i for i, (ev, d) in enumerate(frames) if pred(ev, d)), -1)


def _errors(frames) -> list:
    return [d for ev, d in frames if ev == "error"]


def _two_step_rig(rig, **kw):
    portal, gw, brain, rec = rig([ANCHOR, FT], **kw)
    brain.react["anchor"] = _calls_then(("corehub_object_resolve", {"query": "S26"}), final="모델 키 SM-S938 을 찾았다 [anchor.c1].")
    brain.react["ft"] = _calls_then(("pd_defect_search", {"model": "SM-S938", "symptom": "스웰링"}),
                                    final="스웰링 기록 두 건을 봤다 [ft.c1].")
    brain.envelope.update(anchor=_env_anchor, ft=_env_ft)
    return portal, gw, brain, rec


def _two_step(rig, *, settle=False):
    portal, gw, brain, rec = _two_step_rig(rig)
    frames = _play(_req(NEW), settle=gw if settle else None)
    return frames, portal, gw, brain, rec


def _clean(portal: FakePortal, gw: FakeGateway, rec: dict) -> None:
    """가짜가 본 위반이 없다 — 서비스 자격 · 틀린 머리 · 모르는 도구 · 원래 도구 직접 호출 · 포털 거절."""
    assert not gw.violations, gw.violations
    assert not portal.unexpected, f"설계에 없는 게이트웨이 도구를 불렀다: {portal.unexpected}"
    assert not rec["direct"], f"단계 도구를 routine_call 없이 직접 불렀다: {rec['direct']}"
    assert not portal.refusals, f"포털 · 게이트웨이가 거절했다: {portal.refusals}"


# ── 1. 두 단계 플랫폼 루틴을 끝까지 — 같은 실행을 네 쪽에서 본다(프레임 · 울타리 · 프롬프트 · 게이트웨이 대화) ─────────────────
_EXPOSED = {v: k for k, v in ROUTE.items()}


def test_두_단계_루틴이_끝까지_돌고_프레임이_계획_단계_보고서_끝_차례로_온다(rig):
    """R3 §2 · §3.2 — plan → (step · 도구 status 짝 · step_done)×2 → 보고서 token → routine{report} → result → routine{done} → done."""
    frames, portal, gw, brain, rec = _two_step(rig)
    _clean(portal, gw, rec)
    assert frames and frames[-1][0] == "done", [ev for ev, _ in frames][-6:]
    rts = _rt(frames)
    assert rts and all(d.get("run_id") == RUN_ID for d in rts), [d for d in rts if d.get("run_id") != RUN_ID][:3]
    plan = rts[0]
    assert plan["kind"] == "plan" and plan["resumed"] is False and plan["state"] == "running", plan
    assert (plan["routine_id"], plan["version_no"], plan["title"], plan["purpose"]) == (ROUTINE_ID, 3, TITLE, PURPOSE), plan
    assert [s["id"] for s in plan["steps"]] == ["anchor", "ft"], plan["steps"]
    assert plan["report"]["sections"] == ["anchor", "ft", "insight"], plan["report"]
    end = rts[-1]
    assert end["kind"] == "done" and end["state"] == "done", end

    at = {k: _pos(frames, p) for k, p in {
        "plan": _is("plan"), "step_a": _is("step", "anchor"), "done_a": _is("step_done", "anchor"),
        "step_f": _is("step", "ft"), "done_f": _is("step_done", "ft"), "token": lambda ev, d: ev == "token",
        "report": _is("report"), "result": lambda ev, d: ev == "result", "end": _is("done")}.items()}
    order = ["plan", "step_a", "done_a", "step_f", "done_f", "token", "report", "result", "end"]
    assert all(at[k] >= 0 for k in order) and [at[k] for k in order] == sorted(at[k] for k in order), at
    for sid, title in (("anchor", "기준 키"), ("ft", "시장불량 이력")):
        first = next(d for d in _rt(frames, "step") if d.get("step") == sid)
        assert first["state"] == "running" and first["title"] == title, first

    # 단계 안 도구 호출 — status 시작 · 끝 짝(call), routine_step · 노출 이름(tool) · 백엔드(via), 원장 call_id 는 끝 프레임에만
    st = [(i, d) for i, (ev, d) in enumerate(frames) if ev == "status" and d.get("routine_step")]
    ends = {d["routine_call"]: (i, d) for i, d in st if d.get("routine_call")}
    assert set(ends) == set(_ids(portal.ledger)) == {"anchor.c1", "ft.c1"}, (sorted(ends), _ids(portal.ledger))
    for row in portal.ledger:
        i_end, fin = ends[row["call_id"]]
        pair = [(i, d) for i, d in st if fin.get("call") and d.get("call") == fin["call"]]
        assert len(pair) == 2 and pair[1][0] == i_end, f"status 시작 · 끝이 call 로 짝지어지지 않았다 — {pair}"
        assert not pair[0][1].get("routine_call"), "원장 call_id 는 끝 프레임에만 싣는다"
        for _i, d in pair:
            assert (d["routine_step"], d["tool"], d.get("via")) == (row["step"], _EXPOSED[(row["backend"], row["tool"])],
                                                                  row["backend"]), d
        lo, hi = (at["step_a"], at["done_a"]) if row["step"] == "anchor" else (at["step_f"], at["done_f"])
        assert lo < pair[0][0] < i_end < hi, (row["call_id"], lo, pair[0][0], i_end, hi)

    # step_done — 상태 · 표지는 포털이 돌려준 값, calls 는 원장 줄 수, summary 는 600자까지(보낸 요약의 앞부분)
    assert len(portal.step_done["ft"]["summary"]) > 600, "시험 전제 — ft 요약이 600자를 넘어야 자르기를 본다"
    for sid in ("anchor", "ft"):
        fr = [d for d in _rt(frames, "step_done") if d["step"] == sid]
        out, sent = portal.step_done_out[sid], portal.step_done[sid]
        assert len(fr) == 1 and fr[0]["state"] == out["state"] == "ok" and fr[0]["flags"] == out["flags"], (fr, out)
        assert fr[0]["calls"] == len(portal.calls_of(sid)) == 1, fr[0]
        s = str(fr[0]["summary"])
        core = s[:-1] if s.endswith("…") else s
        assert len(s) <= 600 and core.strip() and sent["summary"].startswith(core.rstrip()), (len(s), s[:80])

    # 보고서 — token 을 이으면 포털 report_md 그대로(400자 조각), result 도 같은 글, report 프레임은 포털 답을 옮긴다
    md = portal.report_out["report_md"]
    tokens = [(i, d["delta"]) for i, (ev, d) in enumerate(frames) if ev == "token"]
    assert "".join(t for _i, t in tokens) == md, "보고서 글(token)을 이으면 포털이 렌더한 report_md 와 같아야 한다"
    assert len(tokens) >= 3 and all(0 < len(t) <= 400 for _i, t in tokens), [len(t) for _i, t in tokens]
    assert max(i for i, _t in tokens) < at["report"], "보고서 글은 report 프레임 앞에 다 나온다"
    res = [d for ev, d in frames if ev == "result"]
    assert len(res) == 1 and res[0].get("type") == "text" and res[0].get("content") == md, res
    rep = _rt(frames, "report")
    assert len(rep) == 1, rep
    assert (rep[0]["ra_upload"], rep[0].get("ra_report_id"), rep[0]["coverage"], rep[0]["warnings"]) == (
        "auto", "ra-test-0042", portal.report_out["coverage"], portal.report_out["warnings"]), rep[0]
    secs = portal.report_args["sections"]
    assert set(secs) == {"insight"}, "실행기는 인사이트 절만 보낸다 — 단계 절은 포털이 저장한 봉투로 짓는다(두 실행자의 한 정본)"
    assert all(part.strip() in secs["insight"] for part in INSIGHT.split("\n\n")), secs["insight"][:300]
    anchor = md.split("## 기준 키\n", 1)[1].split("\n## ", 1)[0]
    assert "S26 은 SM-S938 로 풀린다 [anchor.c1]." in anchor and "- S26 의 PLM 모델은 SM-S938 이다 [anchor.c1]" in anchor, anchor
    assert md.count("S26 의 PLM 모델은 SM-S938 이다") == 1, "발견은 단계 절에 한 번만 실린다"
    assert portal.reports == 1 and portal.run["state"] == "done"


def test_단계_도구는_허용_목록만_겉_그대로_묶이고_호출은_전부_routine_call_의_백엔드_원래이름으로_나간다(rig):
    """R3-D3 — 그 사람의 도구에서 허용 목록 {백엔드: [원래 이름]} 만, 이름 · 설명 · 인자 스키마 그대로. 안내대 도구 · 다른 백엔드 ·
    허용 밖(쓰기) 도구는 묶지 않는다. 속은 routine_call(name='백엔드:원래 이름') — 원래 도구를 직접 부르면 울타리 · 원장을 건너뛴다."""
    frames, portal, gw, brain, rec = _two_step(rig)
    _clean(portal, gw, rec)
    want = {"anchor": {"corehub_object_resolve"}, "ft": {"pd_defect_search", "pd_defect_stats"}}
    seen: dict = {}
    for step, names in brain.binds:
        names = set(names) - {"routine_page"}           # 결과 쪽 읽기(routine_page)는 함께 묶일 수 있다(R3 §4 묶음)
        assert not names & GUIDE, f"안내대 도구가 묶였다: {sorted(names & GUIDE)}"
        assert names <= want.get(step, set()), f"단계 {step} 에 허용 밖 도구가 묶였다: {sorted(names - want.get(step, set()))}"
        if names:
            seen.setdefault(step, set()).update(names)
    assert seen == want, seen
    for _step, tools in brain.bound_tools:
        for t in tools:
            name = _tool_name(t)
            if name in _SCHEMAS:
                desc, props = _SCHEMAS[name]
                assert getattr(t, "description", None) == desc, (name, getattr(t, "description", None))
                assert set(getattr(t, "args", None) or {}) == set(props), (name, getattr(t, "args", None))
    assert [(c["step"], c["n"]) for c in brain.calls if c["kind"] == "react"] == [("anchor", 0), ("anchor", 1), ("ft", 0), ("ft", 1)], \
        "단계마다 작은 ReAct 가 한 번씩 끝까지 돈다(다시 돌리면 단계 예산을 또 쓴다)"
    assert portal.call_names == [("anchor", "core-hub:object_resolve"), ("ft", "failuretrace:pd_defect_search")], portal.call_names
    used = {t for t, _ in portal.log}
    assert used <= {"routine_run_start", "routine_get", "routine_call", "routine_step_done", "routine_report_submit"}, used
    assert _rt(frames)[-1]["state"] == "done"


def test_단계_프롬프트에_할_일_덧붙인_말_앞_단계_근거가_실리고_도구_결과에_근거_id_가_붙는다(rig):
    """R3 §4 — 단계 프롬프트(할 일 · 사용자가 덧붙인 말 · 앞 단계의 키 · 발견 인용), 도구가 모델에 돌려주는 글('[근거 <call_id>] ' + 본문),
    봉투 턴(이 단계 호출의 call_id · 결과 발췌), 인사이트 턴(목적 · 덧붙인 말 · 단계 발견의 인용), 단계 글은 머리 줄과 함께 reasoning 으로."""
    frames, portal, gw, brain, rec = _two_step(rig)
    _clean(portal, gw, rec)

    def react(step, n):
        got = [c for c in brain.calls if c["kind"] == "react" and c["step"] == step and c["n"] == n]
        assert got, f"단계 {step} 의 작은 ReAct {n + 1}번째 모델 호출이 없다"
        return got[0]

    first_a = _text(react("anchor", 0)["messages"])
    assert ANCHOR["goal"] in first_a, "단계 프롬프트에 풀린 할 일(goal)이 없다"
    assert "3분기 위주로 봐 줘" in first_a, "사용자가 덧붙인 말이 단계 프롬프트에 없다"
    tool_msgs = [m for m in react("anchor", 1)["messages"] if isinstance(m, ToolMessage)]
    seen = _text(tool_msgs)
    assert tool_msgs and "[근거 anchor.c1]" in seen and "SM-S938" in seen, seen[:300]
    first_f = _text(react("ft", 0)["messages"])
    assert "SM-S938" in first_f and "anchor.c1" in first_f, "앞 단계(uses)의 키 · 발견 인용 id 가 다음 단계 프롬프트에 없다"
    env_a = [c for c in brain.calls if c["kind"] == "envelope" and c["step"] == "anchor"]
    assert env_a and "anchor.c1" in _text(env_a[0]["messages"]) and "SM-S938" in _text(env_a[0]["messages"]), \
        "봉투 턴에 이 단계 호출(call_id · 결과 발췌)이 없다"
    ins = [c for c in brain.calls if c["kind"] == "insight"]
    assert ins, "인사이트 턴이 없다"
    ins_text = _text(ins[0]["messages"])
    assert PURPOSE in ins_text and "3분기 위주로 봐 줘" in ins_text, "인사이트 턴에 목적 · 덧붙인 말이 없다"
    assert "anchor.c1" in ins_text and "ft.c1" in ins_text, "인사이트 턴에 단계 발견의 인용 id 가 없다"
    reasoning = "".join(str(d.get("delta") or "") for ev, d in frames if ev == "reasoning")
    assert "### 〔anchor〕 기준 키" in reasoning and "### 〔ft〕 시장불량 이력" in reasoning, reasoning[:400]
    assert reasoning.index("〔anchor〕") < reasoning.index("모델 키 SM-S938 을 찾았다") < reasoning.index("〔ft〕"), reasoning[:600]
    sent = portal.step_done["anchor"]
    assert sent["status"] == "ok" and sent.get("keys") == {"model_key": "SM-S938"}, sent
    assert [f.get("refs") for f in sent.get("findings") or []] == [["anchor.c1"]], sent.get("findings")


def test_게이트웨이에는_사람_PAT_와_신원_머리로만_원_세션_JSON_RPC_를_열고_닫는다(rig):
    """R3-D2 · §4 원 세션 — Bearer 사람 PAT · X-HWAX-User · X-HWAX-Groups, initialize → notifications/initialized → tools/call(받은 세션 id)
    → DELETE. 첫 도구는 routine_run_start(루틴 · 판 · 입력 · 대화 id). 사람 도구 목록도 사람 PAT 로 읽는다(서비스 계정으로 떨어지지 않는다)."""
    frames, portal, gw, brain, rec = _two_step(rig, settle=True)
    _clean(portal, gw, rec)
    reqs = gw.requests
    assert reqs and all(r["auth"] == f"Bearer {PAT}" for r in reqs), {r["auth"] for r in reqs}
    posts = [r for r in reqs if r["method"] == "POST"]
    assert all(unquote(r["user"] or "").lower() == USER for r in posts), {r["user"] for r in posts}
    assert all(unquote(r["groups"] or "") == ",".join(GROUPS) for r in posts), {r["groups"] for r in posts}
    assert [r["rpc"] for r in posts[:2]] == ["initialize", "notifications/initialized"], [r["rpc"] for r in posts[:3]]
    calls = [r for r in posts if r["rpc"] == "tools/call"]
    assert calls and all(gw.sessions.get(r["sid"]) for r in calls), "tools/call 은 initialize 가 준 세션 id 로 간다"
    assert calls[0]["tool"] == "routine_run_start", [r["tool"] for r in calls[:3]]
    start = calls[0]["args"]
    assert (start.get("routine_id"), start.get("version"), start.get("inputs"), start.get("conversation_id")) == (
        ROUTINE_ID, 3, INPUTS, "conv-42"), start
    assert gw.sessions and all(s == "closed" for s in gw.sessions.values()), f"끝난 뒤 닫지(DELETE) 않은 세션 — {gw.sessions}"
    assert rec["user_tools"] and all(u["user_pat"] == PAT and u["user"] == USER for u in rec["user_tools"]), rec["user_tools"]
    assert _rt(frames)[-1]["state"] == "done"


# ── 2. 단계 닫기 — 포털이 고친 값 · 키와 인용 검사 ─────────────────────────────────────────────────────────────────
def test_도구를_안_부르고_했다고_하면_봉투는_보내고_프레임은_포털이_고친_skipped_를_싣는다(rig):
    """R3-D4 — 상태는 코드가 먼저 정하고 포털이 원장과 맞대 고친다. 호출 0 인 '했다' 도 봉투는 보내고, step_done 프레임의 상태 · 표지는
    포털이 돌려준 값이다."""
    portal, gw, brain, rec = rig([ANCHOR])
    brain.react["anchor"] = lambda n, m: AIMessage(content="확인했다 — 할 일을 다 했다.")
    brain.envelope["anchor"] = lambda rows: {"status": "ok", "reason": "", "summary": "다 했다.", "keys": {}, "findings": [],
                                             "candidates": []}
    frames = _play(_req(NEW))
    _clean(portal, gw, rec)
    assert not portal.ledger and not portal.call_names, "시험 전제 — 이 모델은 도구를 부르지 않는다"
    assert [s for s, _ in portal.done_log] == ["anchor"], "호출이 없어도 봉투는 보낸다 — 고치는 것은 포털이다"
    out = portal.step_done_out["anchor"]
    fr = _rt(frames, "step_done")
    assert len(fr) == 1 and fr[0]["state"] == out["state"] == "skipped" and fr[0]["flags"] == out["flags"], (fr, out)
    assert portal.stage["anchor"]["state"] == "skipped"
    assert _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


def test_결과에_없는_키와_원장에_없는_인용은_보내지_않는다(rig):
    """R3 §4 봉투 검사 — 키 값은 이 단계 결과에 글자 그대로 있어야(없으면 실행기가 빼고 까닭에 적는다 — 포털 표지에 기대지 않는다),
    refs 는 이 단계 call_id 만. 넘어간 키는 결과에 있던 값뿐이다."""
    step = _platform("anchor", "기준 키", "core-hub", ["object_resolve"], ANCHOR["goal"], keys=["model_key", "project"], calls=4)
    portal, gw, brain, rec = rig([step])
    brain.react["anchor"] = _calls_then(("corehub_object_resolve", {"query": "S26"}), final="모델 키를 찾았다.")
    brain.envelope["anchor"] = lambda rows: {
        "status": "ok", "reason": "", "summary": "S26 은 SM-S938 이다 [anchor.c1].",
        "keys": {"model_key": "SM-S938", "project": "PRJ-404"},
        "findings": [{"text": "모델 키는 SM-S938 이다", "refs": ["anchor.c1", "zz.c9"]},
                     {"text": "과제는 따로 찾아야 한다", "refs": ["nope.c3"]}],
        "candidates": []}
    frames = _play(_req(NEW))
    _clean(portal, gw, rec)
    assert "PRJ-404" not in OBJ_TEXT, "시험 전제 — 과제 키는 결과에 없다"
    sent = portal.step_done["anchor"]
    keys = sent.get("keys") or {}
    assert keys.get("model_key") == "SM-S938", sent
    assert "project" not in keys and "project" in str(sent.get("reason") or ""), (keys, sent.get("reason"))
    refs = [r for f in sent.get("findings") or [] for r in f.get("refs") or []]
    assert "anchor.c1" in refs and not {"zz.c9", "nope.c3"} & set(refs), refs
    assert portal.view()["steps"][0]["keys"] == {"model_key": "SM-S938"}
    assert _rt(frames)[-1]["state"] == "done"


# ── 3. 호출 — 하나씩 · 예산 거절 ──────────────────────────────────────────────────────────────────────────────
def test_한_메시지의_병렬_tool_calls_도_게이트웨이에는_하나씩_나간다(rig):
    """R3-D3 잠금 — 병렬이면 예산 검사(부르기 전)와 원장 쓰기(부른 뒤) 사이에서 예산을 넘기거나 call_id 가 겹친다."""
    portal, gw, brain, rec = rig([FT0])
    portal.call_delay = 0.08
    brain.react["ft"] = _parallel_then([("pd_defect_search", {"model": "SM-S938", "symptom": "스웰링"}),
                                        ("pd_defect_stats", {"model": "SM-S938"})], final="둘 다 봤다 [ft.c1] [ft.c2].")
    brain.envelope["ft"] = _env_ft
    frames = _play(_req(NEW))
    _clean(portal, gw, rec)
    assert sorted(portal.call_names) == [("ft", "failuretrace:pd_defect_search"), ("ft", "failuretrace:pd_defect_stats")], \
        portal.call_names
    assert portal.max_inflight == 1, f"routine_call 이 {portal.max_inflight}개 겹쳐 나갔다 — 실행마다 하나씩이다"
    second = [c for c in brain.calls if c["kind"] == "react" and c["step"] == "ft" and c["n"] == 1]
    seen = _text([m for m in second[0]["messages"] if isinstance(m, ToolMessage)]) if second else ""
    assert "[근거 ft.c1]" in seen and "[근거 ft.c2]" in seen, seen[:400]
    assert _rt(frames)[-1]["state"] == "done"


def test_예산_거절_글을_받은_모델은_그_단계를_더_부르지_않고_단계는_닫힌다(rig):
    """R3 §4 — 거절(ROUTINE_ERROR)은 그 글 그대로 모델에 가고, 예산 거절이면 '더 부르지 말고 정리하라' 가 붙는다. 그 글을 읽은 모델은
    멈추고(이 대본은 글에 '호출 예산' 이 보여야 멈춘다) 단계는 봉투로 닫힌다."""
    step = _platform("ft", "시장불량 이력", "failuretrace", ["pd_defect_search"], FT0["goal"], calls=2)
    portal, gw, brain, rec = rig([step])
    brain.react["ft"] = _until_refused(("pd_defect_search", {"model": "SM-S938", "symptom": "스웰링"}),
                                       final="예산을 다 써서 지금까지로 정리한다.")
    brain.envelope["ft"] = _env_ft
    frames = _play(_req(NEW))
    assert not gw.violations and not portal.unexpected and not rec["direct"]
    budget = "단계 ft 의 호출 예산(2번)을 다 썼다 — routine_step_done 으로 닫는다"
    assert portal.refusals == [("routine_call", budget)], portal.refusals
    assert len(portal.call_names) == 3 and _ids(portal.ledger) == ["ft.c1", "ft.c2"], (portal.call_names, _ids(portal.ledger))
    react = [c for c in brain.calls if c["kind"] == "react" and c["step"] == "ft"]
    last = _text([m for m in react[-1]["messages"] if isinstance(m, ToolMessage)][-1:]) if react else ""
    assert budget in last and "더 부르지 말고" in last, f"모델이 받은 마지막 도구 글 — {last[:300]!r}"
    assert [s for s, _ in portal.done_log] == ["ft"] and len(_rt(frames, "step_done")) == 1
    assert _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


# ── 4. 고를 것 · 심의 단계 ───────────────────────────────────────────────────────────────────────────────────
CANDS = [{"label": "SM-S938 (Galaxy S26)", "key": "SM-S938"}, {"label": "SM-S936 (Galaxy S26+)", "key": "SM-S936"}]


def test_고를_것이_생기면_gate_로_끝나고_다음_단계와_보고서는_돌지_않는다(rig):
    """R3 §3.2 · §4-6 — 포털이 run_state=waiting 을 돌려주면 gate(후보) · 안내 글 · routine{done, waiting} 으로 스트림이 끝난다."""
    portal, gw, brain, rec = _two_step_rig(rig)
    brain.react["anchor"] = _calls_then(("corehub_object_resolve", {"query": "S26"}), final="S26 이 모델 둘로 풀린다.")
    brain.envelope["anchor"] = lambda rows: {"status": "ambiguous", "reason": "S26 이 모델 둘로 풀린다",
                                             "summary": f"후보가 둘이다 [{_ids(rows)[0]}].", "keys": {}, "findings": [],
                                             "candidates": [dict(c) for c in CANDS]}
    frames = _play(_req(NEW))
    _clean(portal, gw, rec)
    assert portal.run["state"] == "waiting" and portal.step_done["anchor"].get("candidates") == CANDS, portal.step_done["anchor"]
    assert _rt(frames, "step_done")[0]["state"] == "ambiguous"
    gates = _rt(frames, "gate")
    assert len(gates) == 1 and gates[0]["step"] == "anchor" and gates[0]["candidates"] == CANDS, gates
    assert isinstance(gates[0].get("message"), str) and gates[0]["message"].strip(), gates[0]
    assert _pos(frames, _is("step_done", "anchor")) < _pos(frames, _is("gate")) < _pos(frames, _is("done"))
    end = _rt(frames)[-1]
    assert end["kind"] == "done" and end["state"] == "waiting" and frames[-1][0] == "done", end
    assert portal.report_args is None and not [s for s, _ in portal.call_names if s == "ft"], portal.call_names
    res = [d for ev, d in frames if ev == "result"]
    assert res and str(res[-1].get("content") or "").strip(), "고를 것으로 멈춘 끝도 짧은 안내 글을 result 로 낸다"


def test_심의_단계는_시작을_routine_call_로_진행은_deliberate_status_를_직접_결과는_routine_call_로_부른다(rig):
    """R3-D5 — start 는 routine_call(근거 · save_report 는 게이트웨이가 채운다 — 실행기는 넣지 않는다), 진행은 원장 · 예산을 안 쓰는
    deliberate_status 직접 조회(routine{step, detail}), 결과는 routine_call(deliberate_result) 뒤 봉투로 닫는다."""
    portal, gw, brain, rec = rig([FT0, MECH])
    brain.react["ft"] = _calls_then(("pd_defect_search", {"model": "SM-S938", "symptom": "스웰링"}), final="스웰링 기록을 봤다.")
    brain.envelope["ft"] = _env_ft
    brain.envelope["mech"] = lambda rows: {"status": "ok", "reason": "", "summary": f"결정 — 실링부 크랙 [{_ids(rows)[-1]}].",
                                           "keys": {}, "findings": [{"text": "메커니즘은 실링부 크랙이다", "refs": _ids(rows)[-1:]}],
                                           "candidates": []}
    frames = _play(_req(NEW))
    _clean(portal, gw, rec)
    log = portal.log

    def at(pred):
        return next((i for i, (t, a_) in enumerate(log) if pred(t, a_)), -1)

    i_start = at(lambda t, a_: t == "routine_call" and a_.get("name") == "hwax-deliberation:deliberate_start")
    i_res = at(lambda t, a_: t == "routine_call" and a_.get("name") == "hwax-deliberation:deliberate_result")
    i_done = at(lambda t, a_: t == "routine_step_done" and a_.get("step") == "mech")
    polls = [i for i, (t, _a) in enumerate(log) if t == "deliberate_status"]
    assert i_start >= 0 and i_res >= 0 and i_done >= 0 and polls, [t for t, _ in log]
    assert i_start < polls[0] and polls[-1] < i_res < i_done, (i_start, polls, i_res, i_done)
    assert all(log[i][1].get("job_id") == JOB_ID for i in polls), [log[i][1] for i in polls]
    assert (log[i_res][1].get("arguments") or {}).get("job_id") == JOB_ID, log[i_res][1]
    assert not [a_ for t, a_ in log if t == "routine_call" and "deliberate_status" in str(a_.get("name"))], \
        "진행 조회를 routine_call 로 했다 — 줄마다 원장 한 줄 · 예산 하나를 쓴다"
    sent = log[i_start][1].get("arguments") or {}
    assert "evidence" not in sent and "save_report" not in sent, sorted(sent)
    assert sent.get("job") == "mechanism" and (sent.get("advanced") or {}).get("chair_template") == "diagnosis", sent
    q = str(sent.get("question") or "")
    assert PURPOSE in q and MECH["goal"] in q, f"심의 질문에 루틴 목적 · 할 일이 없다 — {q[:300]!r}"
    detail = [d for d in _rt(frames, "step") if d.get("step") == "mech" and str(d.get("detail") or "").strip()]
    assert detail, "심의 진행을 routine{step, detail} 로 알리지 않았다"
    assert portal.step_done_out["mech"]["state"] == "ok"
    assert _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


# ── 5. 이어서 · 다시 붙기 · 동시 상한 ────────────────────────────────────────────────────────────────────────────
_CLOSED_ANCHOR = {"status": "ok", "reason": "", "summary": "S26 은 SM-S938 로 풀린다 [anchor.c1].", "keys": {"model_key": "SM-S938"},
                  "findings": [{"text": "S26 의 PLM 모델은 SM-S938 이다", "refs": ["anchor.c1"]}], "candidates": []}


def test_이어서는_run_id_로_다시_시작하고_닫힌_단계는_다시_돌지_않는다(rig):
    """R3-D8 — 살아 있지 않은 실행의 이어서는 routine_run_start(run_id=…) 로 포털 resume 을 거친 뒤 끝난 단계를 건너뛴다(plan.resumed).
    그룹이 비면 X-HWAX-Groups 를 아예 보내지 않는다(빈 머리는 '권한 0 인 사람' 이다)."""
    portal, gw, brain, rec = _two_step_rig(rig)
    portal.seed("running").seed_closed("anchor", "ok", _CLOSED_ANCHOR, results=[OBJ_TEXT])
    frames = _play(_req({"run_id": RUN_ID}, groups=[]))
    _clean(portal, gw, rec)
    assert len(portal.starts) == 1 and portal.starts[0].get("run_id") == RUN_ID and portal.resumes == 1, portal.starts
    assert [s for s, _ in portal.call_names] == ["ft"] and [s for s, _ in portal.done_log] == ["ft"], (portal.call_names,
                                                                                                     portal.done_log)
    assert not [c for c in brain.calls if c["step"] == "anchor"], "닫힌 단계를 다시 돌렸다"
    plan = _rt(frames, "plan")
    assert plan and plan[0]["resumed"] is True and plan[0]["run_id"] == RUN_ID, plan[:1]
    assert next(s for s in plan[0]["steps"] if s["id"] == "anchor")["state"] == "ok", plan[0]["steps"]
    assert _rt(frames)[-1]["state"] == "done" and portal.reports == 1
    assert gw.requests and all(r["groups"] is None for r in gw.requests), {r["groups"] for r in gw.requests}


@pytest.mark.parametrize("state", ["waiting", "done", "failed", "cancelled"])
def test_이어서_부른_실행이_고를_것을_기다리거나_끝났으면_시작을_부르지_않고_그_상태로_끝난다(rig, state):
    """R3 §4 _drive 1 — waiting 이면 고를 것을 다시 gate 로, done · failed · cancelled 는 그 상태의 routine{done} 으로 끝난다."""
    portal, gw, brain, rec = _two_step_rig(rig)
    portal.seed(state)
    if state == "waiting":
        portal.seed_closed("anchor", "ambiguous", {**_CLOSED_ANCHOR, "status": "ambiguous", "keys": {},
                                                   "candidates": [dict(c) for c in CANDS]}, results=[OBJ_TEXT])
    frames = _play(_req({"run_id": RUN_ID}))
    assert not gw.violations and not portal.unexpected and not rec["direct"]
    assert not portal.starts and not portal.call_names and not portal.done_log and portal.report_args is None, portal.log
    rts = _rt(frames)
    assert rts and rts[-1]["kind"] == "done" and rts[-1]["state"] == state and rts[-1]["run_id"] == RUN_ID, rts[-2:]
    assert frames[-1][0] == "done"
    gates = _rt(frames, "gate")
    if state == "waiting":
        assert len(gates) == 1 and gates[0]["step"] == "anchor" and gates[0]["candidates"] == CANDS, gates
    else:
        assert not gates, gates


def test_살아_있는_실행에_다시_붙으면_지난_프레임을_받고_실행기는_하나만_돈다(rig):
    """R3-D7 · D8 — 같은 run_id 가 _RUNS 에 살아 있으면 기록 프레임을 다시 보내고 이어지는 프레임을 함께 받는다(새 작업 · 새 시작 없음)."""
    portal, gw, brain, rec = _two_step_rig(rig)
    seen: dict = {}

    async def body():
        portal.arm_hold("ft")
        f1, f2 = [], []
        t1 = asyncio.create_task(_pump(routine.run_routine(a.app, _req(NEW)), f1))
        t2 = None
        try:
            seen["held"] = await _until(portal.held.is_set, 10)
            seen["live1"] = routine.active_count()
            t2 = asyncio.create_task(_pump(routine.run_routine(a.app, _req({"run_id": RUN_ID})), f2))
            await _until(lambda: _pos(_frames(f2, strict=False), _is("step_done", "anchor")) >= 0, 5)
            seen["replay"] = list(f2)
            seen["live2"] = routine.active_count()
        finally:
            if portal.release is not None:
                portal.release.set()
        await asyncio.gather(*(t for t in (t1, t2) if t is not None))
        return f1, f2

    f1, f2 = asyncio.run(asyncio.wait_for(body(), 25))
    assert seen.get("held"), "시험 전제 — 둘째 단계의 호출에서 실행을 붙잡지 못했다"
    assert (seen["live1"], seen["live2"]) == (1, 1), seen
    replay = _frames(seen["replay"], strict=False)
    assert _pos(replay, _is("plan")) >= 0 and _pos(replay, _is("step_done", "anchor")) >= 0, \
        "다시 붙은 구독이 지난 프레임(plan · 첫 단계 닫기)을 받지 못했다"
    fr1, fr2 = _frames(f1), _frames(f2)
    for fr in (fr1, fr2):
        rts = _rt(fr)
        assert rts and rts[-1]["kind"] == "done" and rts[-1]["state"] == "done" and fr[-1][0] == "done", rts[-2:]
        assert all(d.get("run_id") == RUN_ID for d in rts)
    assert sorted(d["step"] for d in _rt(fr2, "step_done")) == ["anchor", "ft"], "다시 붙은 구독에서 단계 닫기가 빠지거나 겹쳤다"
    _clean(portal, gw, rec)
    assert len(portal.starts) == 1 and "run_id" not in portal.starts[0], portal.starts
    assert portal.call_names == [("anchor", "core-hub:object_resolve"), ("ft", "failuretrace:pd_defect_search")], portal.call_names
    assert [s for s, _ in portal.done_log] == ["anchor", "ft"] and portal.reports == 1


def test_동시_상한이_차면_새_시작은_routine_busy_로_거절하고_같은_실행에_다시_붙기는_받는다(rig, monkeypatch):
    """R3-D7 — ROUTINE_MAX_RUNNING 이상이면 새 시작은 error(routine_busy · retryable · retry_after_s 60) 뒤 done, 게이트웨이에 실행을
    만들지 않는다. 다시 붙기는 새 작업이 아니라 상한 검사 앞에서 받는다(R3 §4 run_routine 2 → 3)."""
    monkeypatch.setattr(routine, "ROUTINE_MAX_RUNNING", 1)
    monkeypatch.setenv("ROUTINE_MAX_RUNNING", "1")
    portal, gw, brain, rec = _two_step_rig(rig)
    seen: dict = {}

    async def body():
        portal.arm_hold("ft")
        f1, f3 = [], []
        t1 = asyncio.create_task(_pump(routine.run_routine(a.app, _req(NEW)), f1))
        t3 = None
        try:
            seen["held"] = await _until(portal.held.is_set, 10)
            seen["busy"] = await asyncio.wait_for(_collect(routine.run_routine(a.app, _req(NEW, conv="conv-43"))), 5)
            seen["starts"] = len(portal.starts)
            t3 = asyncio.create_task(_pump(routine.run_routine(a.app, _req({"run_id": RUN_ID})), f3))
            await _until(lambda: bool(_rt(_frames(f3, strict=False))), 5)
        finally:
            if portal.release is not None:
                portal.release.set()
        await asyncio.gather(*(t for t in (t1, t3) if t is not None))
        return f1, f3

    f1, f3 = asyncio.run(asyncio.wait_for(body(), 25))
    assert seen.get("held"), "시험 전제 — 첫 실행을 붙잡지 못했다"
    busy = _frames(seen["busy"])
    errs = _errors(busy)
    assert errs and errs[-1].get("code") == "routine_busy", errs
    assert errs[-1].get("retryable") is True and errs[-1].get("retry_after_s") == 60, errs[-1]
    assert [ev for ev, _ in busy][-2:] == ["error", "done"] and not _rt(busy), busy
    assert seen["starts"] == 1, "상한에 걸린 시작이 게이트웨이에 실행을 만들었다"
    fr3 = _frames(f3)
    assert not [e for e in _errors(fr3) if e.get("code") == "routine_busy"], "다시 붙기는 상한에 걸리지 않는다"
    assert _rt(fr3) and _rt(fr3)[-1]["state"] == "done", _rt(fr3)[-2:]
    assert _rt(_frames(f1))[-1]["state"] == "done"
    _clean(portal, gw, rec)
    assert len(portal.starts) == 1


# ── 6. 시작 전 거절 — 사람 PAT · 꼴 · 새 심의 멈춤 ──────────────────────────────────────────────────────────────────
def test_사람_PAT_가_없으면_routine_no_pat_로_거절하고_서비스_계정으로도_게이트웨이를_부르지_않는다(rig):
    """R3 §4 run_routine 1 — 루틴 도구는 사람 PAT 로만 돈다(게이트웨이가 공유 비밀을 거절한다). 서비스 계정으로 떨어지지 않는다."""
    portal, gw, brain, rec = rig([ANCHOR])
    for field in (NEW, {"run_id": RUN_ID}):
        frames = _play(_req(field, pat=""))
        errs = _errors(frames)
        assert errs and errs[-1].get("code") == "routine_no_pat" and frames[-1][0] == "done", frames
        if field is NEW:
            assert not _rt(frames), "실행 id 를 아직 모르는 시작 실패는 routine 프레임 없이 error 만 낸다"
    assert gw.requests == [] and not rec["user_tools"] and not brain.calls, (gw.requests[:2], rec["user_tools"])


def test_routine_칸에_id_도_run_id_도_없으면_routine_bad_request_로_거절한다(rig):
    portal, gw, brain, rec = rig([ANCHOR])
    frames = _play(_req({"version": 2, "inputs": {"model": "S26"}}))
    errs = _errors(frames)
    assert errs and errs[-1].get("code") == "routine_bad_request" and frames[-1][0] == "done", frames
    assert gw.requests == [] and not _rt(frames)


def test_새_심의_멈춤_중에는_시작도_이어서도_심의와_같은_꼴의_delib_paused_로_거절한다(rig, monkeypatch, tmp_path):
    """R3-D7 — /drain scope=delib 멈춤을 안 지키면 update-all 의 hwax_delib_wait 가 수렴하지 않는다. 거절 꼴은 _paused_stream 그대로."""
    portal, gw, brain, rec = rig([ANCHOR])
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path / "delib-jobs")       # 깃발 파일은 이 시험의 임시 자리에만 쓴다
    delib_jobs.pause_state().set_on(scope="delib", by="test:routine", reason="루틴 시험", hold=True)
    info = delib_jobs.pause_info()
    assert info and info["retry_after_s"] == delib_jobs.PAUSE_HOLD_RETRY_S, info
    want = _errors(_frames(asyncio.run(_collect(a._paused_stream(info)))))
    assert want and want[0]["code"] == "delib_paused", want
    frames = _play(_req(NEW))
    assert _errors(frames) == want and frames[-1][0] == "done", (_errors(frames), want)
    assert gw.requests == [] and not rec["user_tools"], "새 시작은 게이트웨이를 부르지 않고 거절한다"
    # 이어서는 실행 보기를 읽어야 새 일인지 안다(끝난 실행의 안내 · RA 초안 올리기는 멈춤 중에도 된다 — R3 §8) — 도는 실행을 잇는 것은 같은 꼴로
    # 거절하고 시작(routine_run_start)을 부르지 않는다
    portal.seed("running")
    frames = _play(_req({"run_id": RUN_ID}))
    assert _errors(frames) == want and frames[-1][0] == "done" and not _rt(frames), frames
    assert [t for t, _a in portal.log] == ["routine_get"] and not portal.starts and not rec["user_tools"], portal.log


# ── 7. app.py 고리 — /health · chat() 가지 · 권한 ───────────────────────────────────────────────────────────────
def test_health_는_도는_루틴을_routine_active_로_싣고_delib_active_에_더하되_카드_검토_반감에는_세지_않는다(rig, monkeypatch):
    """R3-D7 — delib_active 에 더하면 update-all · start.sh · deploy-all 의 기다림이 고침 없이 루틴을 기다린다. 루틴은 챗 LLM 이라
    GLM 을 보호하는 카드 검토 반감(_delib_human)과는 무관하다. 끝나면 다시 0 이다."""
    monkeypatch.setattr(routine, "ROUTINE_MAX_RUNNING", 3)
    monkeypatch.setenv("ROUTINE_MAX_RUNNING", "3")
    portal, gw, brain, rec = _two_step_rig(rig)
    client = TestClient(a.app)
    got: dict = {}

    def health() -> dict:
        r = client.get("/health")
        assert r.status_code == 200, r.text[:300]
        return r.json()

    async def body():
        got["before"] = await asyncio.to_thread(health)
        got["human_before"] = a._delib_human()
        portal.arm_hold("ft")
        f1: list = []
        t1 = asyncio.create_task(_pump(routine.run_routine(a.app, _req(NEW)), f1))
        try:
            got["held"] = await _until(portal.held.is_set, 10)
            got["live"] = await asyncio.to_thread(health)
            got["human_live"] = a._delib_human()
        finally:
            if portal.release is not None:
                portal.release.set()
        await t1
        got["drained"] = await _until(lambda: routine.active_count() == 0, 3)
        got["after"] = await asyncio.to_thread(health)
        return f1

    f1 = asyncio.run(asyncio.wait_for(body(), 25))
    assert got.get("held"), "시험 전제 — 실행을 붙잡지 못했다"
    before, live, after = got["before"], got["live"], got["after"]
    assert (before.get("routine_active"), live.get("routine_active"), after.get("routine_active")) == (0, 1, 0), \
        (before.get("routine_active"), live.get("routine_active"), after.get("routine_active"))
    assert live.get("routine_max") == 3, live.get("routine_max")
    assert live["delib_active"] == before["delib_active"] + 1, (before["delib_active"], live["delib_active"])
    assert after["delib_active"] == before["delib_active"], after["delib_active"]
    assert got["human_live"] == got["human_before"], "루틴이 카드 검토 반감(_delib_human)에 세였다"
    assert _rt(_frames(f1))[-1]["state"] == "done"


def test_챗_끝점은_루틴_칸이_있으면_슬래시_띵킹보다_먼저_루틴을_돌리고_ping_으로_감싸지_않는다(rig, monkeypatch):
    """R3 §4 app.py 고리 — routine 가지는 심의 멈춤 · 슬래시 · 띵킹보다 먼저다(뒤면 만든 실행이 아무도 돌리지 않는 running 으로 남는다).
    _pinged_stream 으로 감싸지 않는다(구독이 끊기면 턴을 취소한다 — 루틴은 브라우저가 떠나도 돈다)."""
    portal, gw, brain, rec = rig([ANCHOR])
    brain.react["anchor"] = _calls_then(("corehub_object_resolve", {"query": "S26"}), final="찾았다.")
    brain.envelope["anchor"] = _env_anchor
    hit: list = []

    async def _other(*_x, **_k):
        yield a._sse("done", {})

    def _mark(name):
        def f(*_x, **_k):
            hit.append(name)
            return _other()
        return f

    monkeypatch.setattr(a, "_delib_with_topic", _mark("심의"))
    monkeypatch.setattr(a, "run_thinking", _mark("띵킹"))
    monkeypatch.setattr(a, "_agent_stream", _mark("챗"))
    monkeypatch.setattr(a, "_pinged_stream", lambda gen: (hit.append("ping 감싸기"), gen)[1])
    req = _req(NEW, message="/심의 S26 스웰링 원인", thinking=True,
               entitlements=["feat:chat", "feat:routines", "feat:deliberation", "feat:thinking"])

    async def body():
        resp = await a.chat(req)
        assert isinstance(resp, StreamingResponse), type(resp)
        return [c async for c in resp.body_iterator]

    frames = _frames(asyncio.run(asyncio.wait_for(body(), 20)))
    assert hit == [], f"루틴 칸이 있는데 다른 가지를 탔다: {hit}"
    assert len(portal.starts) == 1 and portal.starts[0].get("routine_id") == ROUTINE_ID, portal.starts
    assert _rt(frames) and _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


def test_feat_routines_권한이_없으면_루틴을_돌리지_않고_대화에_남는_한_줄로_답한다(rig):
    """R3 §4 app.py 고리 — _denied_feature 는 routine 이 있으면 feat:routines 를 본다(entitlements 가 None 이면 막지 않는다)."""
    portal, gw, brain, rec = rig([ANCHOR])
    req = _req(NEW, entitlements=["feat:chat"])
    assert a._denied_feature(req) == "feat:routines"
    assert a._denied_feature(_req(NEW, entitlements=None)) is None, "권한을 안 보낸 옛 호출은 막지 않는다"
    assert a._denied_feature(_req(NEW)) is None

    async def body():
        resp = await a.chat(req)
        return b"".join([c if isinstance(c, bytes) else str(c).encode() async for c in resp.body_iterator]).decode("utf-8")

    out = asyncio.run(asyncio.wait_for(body(), 10))
    assert "분석 루틴" in out and "내 권한" in out and "event: done" in out, out
    assert gw.requests == [] and not portal.starts


# ── 8. 원 세션이 끊기면 — 한 번 다시 연다 ─────────────────────────────────────────────────────────────────────────
def test_게이트웨이_세션이_사라지면_한_번_다시_열고_같은_호출을_한_번만_잇는다(rig):
    """R3 §4 원 세션 — 끊기 · 503 draining · 세션 만료(404)는 한 번 다시 연다. 사라진 세션으로 간 호출은 게이트웨이에 닿지 않았으니
    다시 연 세션으로 한 번 보내고, 원장에는 한 줄만 남는다."""
    portal, gw, brain, rec = _two_step_rig(rig)
    gw.expire_once = "routine_call"
    frames = _play(_req(NEW))
    _clean(portal, gw, rec)
    assert gw.inits >= 2, "세션이 사라졌는데 다시 열지 않았다"
    assert portal.call_names == [("anchor", "core-hub:object_resolve"), ("ft", "failuretrace:pd_defect_search")], portal.call_names
    assert _ids(portal.ledger) == ["anchor.c1", "ft.c1"]
    assert _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


# ── 9. 반대 검토 뒤 바뀐 것(R3 §8) — 결정문 읽기 예외 · 심의 접기 · 멈춤 뒤 다시 · 덧붙인 말 · RA 다시 올리기 · 읽기 울타리 ──────────────
def _env_mech(rows):
    ids = _ids(rows)[-1:] or ["mech.c1"]
    return {"status": "ok", "reason": "", "summary": f"결정 — 실링부 크랙 [{ids[0]}].", "keys": {},
            "findings": [{"text": "메커니즘은 실링부 크랙이다", "refs": ids}], "candidates": []}


def _delib_rig(rig, mech=MECH, **kw):
    """시장불량 이력(플랫폼) → 메커니즘(심의). 심의 단계의 봉투는 그 단계의 마지막 호출(결정문 읽기)을 인용한다."""
    portal, gw, brain, rec = rig([FT0, mech], **kw)
    brain.react["ft"] = _calls_then(("pd_defect_search", {"model": "SM-S938", "symptom": "스웰링"}), final="스웰링 기록을 봤다.")
    brain.envelope.update(ft=_env_ft, mech=_env_mech)
    return portal, gw, brain, rec


@pytest.mark.parametrize("case", ["단계_예산", "시간_상한"])
def test_심의_단계의_결정문_읽기는_시작이_단계_예산을_다_써도_실행_마감이_지나도_된다(rig, case):
    """R3-D5 · §8 — 결정문 읽기(deliberate_result)는 심의 단계의 호출 예산 밖이고 시간 상한 뒤에도 열린다. 막히면 이미 쓴 GLM 시간이 루틴에서
    사라진다(save_report=false 라 RA 에도 없다). 단계 예산 1번을 시작이 다 쓰거나 심의를 지켜보는 사이 마감이 지나도 결정문을 원장에 남기고,
    봉투 턴이 그 결정문을 보고 그 call_id 로 인용해 단계를 닫는다."""
    mech = _delib_step("mech", "메커니즘", "불량의 메커니즘을 밝힌다", uses=["ft"], calls=1 if case == "단계_예산" else 8)
    portal, gw, brain, rec = _delib_rig(rig, mech)
    if case == "시간_상한":
        portal.time_up_at_poll = 1
    frames = _play(_req(NEW))
    _clean(portal, gw, rec)
    if case == "시간_상한":
        assert portal.time_up, "시험 전제 — 심의를 지켜보는 사이 실행 마감이 지났다"
    rows = portal.calls_of("mech")
    assert [(r["call_id"], r["tool"], r["ok"]) for r in rows] == [("mech.c1", "deliberate_start", True),
                                                                  ("mech.c2", "deliberate_result", True)], rows
    env = [c for c in brain.calls if c["kind"] == "envelope" and c["step"] == "mech"]
    assert env and "셀 팽창에 따른 실링부 크랙" in _text(env[-1]["messages"]), "봉투 턴이 결정문을 보지 못했다"
    sent = portal.step_done["mech"]
    assert [f.get("refs") for f in sent.get("findings") or []] == [["mech.c2"]], sent.get("findings")
    assert portal.step_done_out["mech"]["state"] == "ok", portal.step_done_out["mech"]
    assert portal.reports == 1 and _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


@pytest.mark.parametrize("case", ["남은_한_번", "다른_실행자"])
def test_실행_호출_상한은_결정문_읽기에도_걸리고_실행기는_매달리지_않고_보고서로_간다(rig, case):
    """R3 §8 — 결정문 읽기의 예외는 단계 예산 · 시간 상한뿐이다. 실행 호출 상한은 그대로라(포털 _limit 이 호출 상한을 먼저 본다) 원장은 상한을
    넘지 않고 결정문 없이 단계가 ok 가 되지 않는다. 심의 단계가 올 때 호출이 한 번만 남았거나, 지켜보는 사이 같은 원장을 쓰는 다른 실행자가
    마지막 호출을 썼어도 실행기는 매달리지 않고 남은 단계를 닫아 보고서로 간다(R3 §4 _drive 3)."""
    n = 2 if case == "남은_한_번" else 3
    portal, gw, brain, rec = _delib_rig(rig, limits={"minutes": 60, "calls": n})
    if case == "다른_실행자":
        portal.foreign_call_at_poll = 1
    frames = _play(_req(NEW))
    assert not gw.violations and not portal.unexpected and not rec["direct"]
    assert len(portal.ledger) <= n and "deliberate_result" not in [r["tool"] for r in portal.ledger], \
        f"실행 호출 상한({n}번)을 넘어 원장에 남았다 — {[(r['call_id'], r['tool']) for r in portal.ledger]}"
    limit = f"실행 상한에 닿았다(호출 {n}번) — 남은 단계를 닫고 보고서를 낸다"
    assert all(t == limit for _n, t in portal.refusals), portal.refusals
    assert portal.stage["mech"]["state"] in CLOSED and portal.stage["mech"]["state"] != "ok", \
        f"결정문 없이 심의 단계를 닫은 상태 — {portal.stage['mech']['state']}"
    assert portal.reports == 1 and _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


def test_심의_단계_도중_실행을_멈추면_시작한_심의를_deliberate_cancel_로_접고_cancelled_로_끝난다(rig):
    """R3 §8 — 루틴을 멈춰도 그 루틴이 시작한 심의가 몇 시간씩 돌던 결함. 사람이 화면에서 '멈춤'(포털 /cancel)을 누르면 실행기는 심의를
    지켜보는 사이(사이사이 routine_get) 그것을 알아채고 시작한 심의를 deliberate_cancel 로 접는다 — 멈춘 실행은 원장에 못 쓰므로 routine_call
    이 아니라 원 세션으로 직접 부른다. 결정문은 읽지 않고 보고서도 내지 않으며 스트림은 짧은 안내 글과 routine{done, cancelled} 로 끝난다."""
    portal, gw, brain, rec = _delib_rig(rig, delib_statuses=("running",))
    portal.cancel_at_poll = 2
    frames = _play(_req(NEW))
    assert not gw.violations and not portal.unexpected and not rec["direct"]
    assert portal.polls >= 2 and portal.run["state"] == "cancelled", "시험 전제 — 심의를 지켜보는 사이 실행을 멈췄다"
    assert portal.delib_cancels and set(portal.delib_cancels) == {JOB_ID}, portal.delib_cancels
    assert portal.job["status"] == "cancelled", "엔진에서 심의가 접히지 않았다"
    log = portal.log
    polls = [i for i, (t, _a) in enumerate(log) if t == "deliberate_status"]
    i_cancel = next(i for i, (t, _a) in enumerate(log) if t == "deliberate_cancel")
    assert i_cancel > polls[1], "멈춤 뒤에 접어야 한다"
    assert not [a_ for t, a_ in log if t == "routine_call" and "deliberate_cancel" in str(a_.get("name"))], \
        "멈춘 실행의 routine_call 은 거절된다 — 접기는 직접 부른다"
    assert [r["tool"] for r in portal.calls_of("mech")] == ["deliberate_start"], "멈춘 실행에서 결정문을 읽었다"
    assert portal.report_args is None and portal.reports == 0
    end = _rt(frames)[-1]
    assert end["kind"] == "done" and end["state"] == "cancelled" and frames[-1][0] == "done", end
    res = [d for ev, d in frames if ev == "result"]
    assert res and str(res[-1].get("content") or "").strip(), "멈춘 끝도 짧은 안내 글을 result 로 낸다"


def test_새_심의_멈춤으로_시작이_거절되면_쉬며_기다렸다_다시_시작하고_단계는_실패가_아니다(rig, monkeypatch, tmp_path):
    """R3 §8 — 잠깐의 거절(심의 멈춤 · 줄 가득)은 실패가 아니라 기다렸다 다시다. update-all 이 새 심의를 멈춘(/drain delib) 사이에 심의 단계가
    오면 엔진이 시작을 '점검 중 — 약 N분 뒤 다시' 도구 오류로 거절한다. 실행기는 단계를 실패로 닫지 않고 쉬며(parked — delib_active 에 세지
    않는다, update-all 의 기다림이 수렴하게) 멈춤이 풀리면 다시 시작해 결정문까지 간다. 같은 심의를 두 번 세우지 않는다."""
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path / "delib-jobs")       # 멈춤 깃발은 이 시험의 임시 자리에만 쓴다
    portal, gw, brain, rec = _delib_rig(rig)
    portal.pause_on_start = True
    client = TestClient(a.app)
    seen: dict = {}

    def health() -> dict:
        r = client.get("/health")
        assert r.status_code == 200, r.text[:300]
        return r.json()

    async def body():
        base = (await asyncio.to_thread(health))["delib_active"]
        sink: list = []
        task = asyncio.create_task(_pump(routine.run_routine(a.app, _req(NEW)), sink))
        try:
            seen["refused"] = await _until(lambda: portal.paused_refusals >= 1, 10)
            loop = asyncio.get_running_loop()
            end = loop.time() + 3
            while loop.time() < end and not seen.get("parked"):
                h = await asyncio.to_thread(health)
                # 쉬는 동안 — 실행은 살아 있고(스트림이 안 끝났다 · 단계가 열려 있다) delib_active 는 루틴을 세지 않는다
                seen["parked"] = (h["delib_active"] == base and not task.done()
                                  and portal.stage["mech"]["state"] == "running" and delib_jobs.pause_info() is not None)
                await asyncio.sleep(0.05)
        finally:
            delib_jobs.pause_state().set_off(by="test:update-all", force=True)
        await task
        return sink

    frames = _frames(asyncio.run(asyncio.wait_for(body(), 30)))
    assert seen.get("refused"), "시험 전제 — 엔진이 멈춤으로 시작을 거절하지 않았다"
    assert not gw.violations and not portal.unexpected and not rec["direct"]
    assert not portal.refusals, f"기다렸다 다시가 게이트웨이 · 포털 거절(예산 · 상한)에 걸렸다: {portal.refusals}"
    assert seen.get("parked"), "멈춤이 풀리길 기다리는 실행이 delib_active 에 세였다 — update-all 의 기다림이 수렴하지 않는다"
    starts = [r for r in portal.calls_of("mech") if r["tool"] == "deliberate_start"]
    assert starts and not starts[0]["ok"] and "점검 중" in starts[0]["text"], starts[:1]
    assert [r["ok"] for r in starts].count(True) == 1 and starts[-1]["ok"], "같은 심의를 두 번 세웠거나 끝내 세우지 못했다"
    assert [r["tool"] for r in portal.calls_of("mech")][-1] == "deliberate_result", _ids(portal.calls_of("mech"))
    assert portal.step_done_out["mech"]["state"] == "ok", "잠깐의 거절을 실패로 닫았다"
    assert portal.reports == 1 and _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


def test_고른_뒤_이어서_돌아도_프롬프트의_덧붙인_말은_실행에_남긴_말이고_루틴_이어서_글이_아니다(rig):
    """R3 §8 · C-12 — 사용자가 덧붙인 말은 시작 때 routine_run_start(note) 로 실행에 한 번 남고, 이어서 · 고른 뒤 잇기는 원장의 그 말을 싣는다.
    화면의 '이어서' 는 발화 글로 '루틴 이어서: <제목>' 을 보낸다(덧붙인 말이 아니다) — 이 글이 덧붙인 말을 덮던 결함. 고르기는 기다리는
    실행에서만 된다 — 끝난 실행에 남은 고르기 단추는 실행을 되살리지 않는다."""
    portal, gw, brain, rec = _two_step_rig(rig)
    brain.react["anchor"] = _calls_then(("corehub_object_resolve", {"query": "S26"}), final="S26 이 모델 둘로 풀린다.")
    brain.envelope["anchor"] = lambda rows: {"status": "ambiguous", "reason": "S26 이 모델 둘로 풀린다",
                                             "summary": f"후보가 둘이다 [{_ids(rows)[0]}].", "keys": {}, "findings": [],
                                             "candidates": [dict(c) for c in CANDS]}
    mark: dict = {}

    def between(_i):
        mark["state"] = portal.run["state"]
        portal.pick("anchor", "SM-S938")                 # 화면의 고르기 단추(포털 /pick) — 실행이 running 으로 돌아간다
        mark["calls"] = len(brain.calls)

    first, second = _turns(_req(NEW), _req({"run_id": RUN_ID}, message=f"루틴 이어서: {TITLE}"), between=between)
    _clean(portal, gw, rec)
    assert mark["state"] == "waiting" and _rt(first)[-1]["state"] == "waiting", "시험 전제 — 첫 턴은 고를 것으로 멈췄다"
    assert portal.starts[0].get("note") == NOTE and portal.run["note"] == NOTE, "덧붙인 말은 시작 때 실행에 남긴다"
    assert len(portal.starts) == 2 and portal.starts[1].get("run_id") == RUN_ID and portal.resumes == 1, portal.starts
    later = brain.calls[mark["calls"]:]
    ft = [c for c in later if c["kind"] == "react" and c["step"] == "ft"]
    ins = [c for c in later if c["kind"] == "insight"]
    assert ft and ins, [(c["kind"], c["step"]) for c in later]
    for c in (ft[0], ins[0]):
        text = _text(c["messages"])
        assert NOTE in text, f"{c['kind']} 프롬프트에 실행에 남긴 덧붙인 말이 없다"
        assert "루틴 이어서" not in text, f"{c['kind']} 프롬프트에 화면의 '루틴 이어서' 발화가 실렸다 — 덧붙인 말이 아니다"
    assert "SM-S938 의 스웰링 불량 기록을 유형 · 시기별로" in _text(ft[0]["messages"]), "고른 키로 풀린 할 일이 없다"
    assert portal.reports == 1 and _rt(second)[-1]["state"] == "done" and second[-1][0] == "done"
    with pytest.raises(_Refuse, match="실행이 done 다 — 고를 수 없다"):
        portal.pick("anchor", "SM-S936")
    assert portal.run["state"] == "done" and portal.stage["anchor"]["envelope"]["keys"] == {"model_key": "SM-S938"}


def test_끝에_묻는_루틴의_끝난_실행에_RA_올리기를_보내면_routine_report_submit_upload_true_로_한_번_올린다(rig):
    """R3 §8 · D12 — RA 'ask' 는 챗에서 막다른 길이었다(게이트웨이 안내 ra_next 는 MCP 손님용 글). 끝난 실행에 routine{run_id, upload_ra:true}
    를 보내면 실행기가 routine_report_submit(upload=true) 를 부르고, 포털은 저장본을 돌려주며(다시 렌더하지 않는다) 게이트웨이가 그 사람 이름으로
    초안을 한 번 만든다. 사람이 고르기 전(첫 턴)에는 올리지 않고, 올리기 턴은 실행을 다시 시작하거나 단계를 다시 돌지 않는다."""
    portal, gw, brain, rec = _two_step_rig(rig, upload="ask")
    mark: dict = {}

    def between(_i):
        mark.update(log=len(portal.log), drafts=len(portal.ra_drafts), reports=portal.reports, state=portal.run["state"])

    first, second = _turns(_req(NEW), _req({"run_id": RUN_ID, "upload_ra": True}, message=f"RA 초안 올리기: {TITLE}"),
                           between=between)
    _clean(portal, gw, rec)
    assert (mark["state"], mark["reports"], mark["drafts"]) == ("done", 1, 0), mark
    assert portal.report_calls[0].get("upload") is not True, "'ask' 루틴을 사람이 고르기 전에 올렸다"
    rep1 = _rt(first, "report")
    assert rep1 and rep1[-1]["ra_upload"] == "ask" and not rep1[-1].get("ra_report_id"), rep1
    tools2 = [t for t, _a in portal.log[mark["log"]:]]
    assert "routine_report_submit" in tools2 and set(tools2) <= {"routine_get", "routine_report_submit"}, tools2
    up = portal.report_calls[-1]
    assert up.get("upload") is True and up.get("run_id") == RUN_ID, up
    assert len(portal.ra_drafts) == 1 and portal.reports == 1, "저장본으로 한 번만 올린다(다시 렌더하지 않는다)"
    rep2 = _rt(second, "report")
    assert rep2 and rep2[-1].get("ra_report_id") == "ra-test-0042", rep2
    end = _rt(second)[-1]
    assert end["kind"] == "done" and end["state"] == "done" and second[-1][0] == "done", end


def test_읽기로_확인되지_않은_도구는_게이트웨이가_거절하고_그_글이_그대로_모델에_가며_원장에는_남지_않는다(rig):
    """R3 §8 반대 검토 — 단계 울타리는 '읽기로 확인된 것만'(게이트웨이 _routine_read — 모르면 읽기가 아니다). 낡은 계획(옛 포털의 이름 꼴 판정)이
    표지도 조회 낱말도 없는 도구를 허용 목록에 넣었어도 routine_call 이 막는다. 거절 글은 그대로 모델에 가고(R3 §4) 원장 줄 · 예산을 쓰지
    않으며, 모델은 읽기 도구로 이어 간다."""
    step = _platform("ft", "시장불량 이력", "failuretrace", ["pd_defect_search", "pd_defect_export"], FT0["goal"], calls=6)
    portal, gw, brain, rec = rig([step])
    brain.react["ft"] = _calls_then(("pd_defect_export", {"model": "SM-S938"}),
                                    ("pd_defect_search", {"model": "SM-S938", "symptom": "스웰링"}), final="검색으로 봤다 [ft.c1].")
    brain.envelope["ft"] = _env_ft
    frames = _play(_req(NEW))
    assert not gw.violations and not portal.unexpected and not rec["direct"]
    deny = ("단계 안에서는 읽기로 확인된 도구만 부른다(v1) — 'failuretrace:pd_defect_export' 는 읽기로 확인되지 않았다"
            "(readOnlyHint · 읽기 목록 · 이름 꼴). 읽기 도구라면 앱이 readOnlyHint 를 달거나 박스의 read_tools.local.json 에 더한다")
    assert portal.refusals == [("routine_call", deny)], portal.refusals
    assert [(r["call_id"], r["tool"]) for r in portal.ledger] == [("ft.c1", "pd_defect_search")], portal.ledger
    second = [c for c in brain.calls if c["kind"] == "react" and c["step"] == "ft" and c["n"] == 1]
    seen = _text([m for m in second[0]["messages"] if isinstance(m, ToolMessage)]) if second else ""
    assert deny in seen, f"모델이 받은 도구 글 — {seen[:300]!r}"
    assert portal.step_done_out["ft"]["state"] == "ok" and _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


def test_받은_인자를_되돌려_준_값이나_실패한_호출의_글에만_있는_값은_키로_넘기지_않는다(rig):
    """R3 §8 · C5 — 키의 근거는 그 단계의 성공한 호출 결과에 글자 그대로 있고 그 호출의 인자에는 없는 값만이다(포털 · 실행기가 같은 규칙).
    도구가 받은 인자를 되돌려 준 값({"query": "SM-S999", "matches": []})과 실패한 호출의 오류 글에만 있는 값은 근거가 아니다 — 지어낸 키 ·
    잘못 이은 제품이 다음 단계로 넘어가는 길이다. 실행기는 빼고 까닭에 적으며, 포털 원장에도 넘어간 키가 없다."""
    step = _platform("anchor", "기준 키", "core-hub", ["object_resolve"], ANCHOR["goal"], keys=["model_key", "project"], calls=4)

    def backend(bk, orig, args):
        q = str(args.get("query") or "")
        if q == "S99":
            return True, f"'{q}' 를 풀지 못했다 — 과제 PRJ-777 조회 오류"       # 실패한 호출의 글에만 있는 값
        return False, json.dumps({"query": q, "matches": []}, ensure_ascii=False)   # 받은 인자를 그대로 되돌려 준다

    portal, gw, brain, rec = rig([step], backend=backend)
    brain.react["anchor"] = _calls_then(("corehub_object_resolve", {"query": "SM-S999"}), ("corehub_object_resolve", {"query": "S99"}),
                                        final="모델 키 SM-S999 · 과제 PRJ-777 로 보인다.")
    brain.envelope["anchor"] = lambda rows: {"status": "ok", "reason": "", "summary": f"S26 을 풀었다 [{_ids(rows)[0]}].",
                                             "keys": {"model_key": "SM-S999", "project": "PRJ-777"},
                                             "findings": [{"text": "모델 키는 SM-S999 이다", "refs": _ids(rows)[:1]}], "candidates": []}
    frames = _play(_req(NEW))
    _clean(portal, gw, rec)
    assert [(r["call_id"], r["ok"]) for r in portal.ledger] == [("anchor.c1", True), ("anchor.c2", False)], "시험 전제 — 성공 하나 · 실패 하나"
    assert "SM-S999" in json.dumps(portal.ledger[0]["args"]) and "PRJ-777" in portal.ledger[1]["text"], "시험 전제 — 값은 인자 · 오류 글에만 있다"
    sent = portal.step_done["anchor"]
    assert not (sent.get("keys") or {}), f"근거가 아닌 값을 키로 보냈다 — {sent.get('keys')}"
    reason = str(sent.get("reason") or "")
    assert "model_key" in reason and "project" in reason, f"뺀 키를 까닭에 적지 않았다 — {reason!r}"
    assert portal.view()["steps"][0]["keys"] == {}
    assert _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"


def test_시간_상한에_닿은_실행을_이어서_하면_포털이_마감을_다시_잡고_남은_단계를_돈다(rig):
    """R3-D8 · §3.5-1 — 이어서(POST /runs/{id}/resume)는 시간 상한에 닿은 실행의 마감을 '지금 + 상한 분' 으로 다시 잡는다. 실행기는 그 뒤의 실행
    보기로 남은 단계를 돈다 — 이어서 앞에 읽은 '상한에 닿았다' 로 남은 단계를 건너뛰고 끝내지 않는다."""
    portal, gw, brain, rec = _two_step_rig(rig)
    portal.seed("running").seed_closed("anchor", "ok", _CLOSED_ANCHOR, results=[OBJ_TEXT])
    portal.time_up = True
    assert portal.view()["run"]["limit_kind"] == "time", "시험 전제 — 이어서 앞의 실행 보기는 시간 상한에 닿았다"
    frames = _play(_req({"run_id": RUN_ID}, message=f"루틴 이어서: {TITLE}"))
    _clean(portal, gw, rec)
    assert portal.resumes == 1 and not portal.time_up, portal.starts
    assert [s for s, _ in portal.call_names] == ["ft"] and portal.stage["ft"]["state"] == "ok", portal.stage["ft"]
    assert portal.reports == 1 and _rt(frames)[-1]["state"] == "done" and frames[-1][0] == "done"

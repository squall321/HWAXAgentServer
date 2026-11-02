# 심의 주제(과제 · 차수 · 글 · 설계 자료)를 꼴로 보고 SP 과제 key 를 서버가 스스로 부른 풀이로 다시 확인한다 — 과제 인사이트 그래프 M1
"""심의 주제 — HWAXPortal docs/project-insight-graph PLAN §3.2 · §3.4 · §4.1 · §4.3 · §4.6 (M1).

**무엇.** 사람이 심의를 열 때 고른 주제 하나를 MCP 잡 원장과 포털 대화에 싣는다. 주제의 열쇠는 (과제, 차수)다.

  · 과제 — SP 허브의 plm_task key(운영 허브는 과제명이고 과제코드는 task_code 칸이다 — HWAXPortal docs/procedures/app-project-keys.md 3절).
    부르는 쪽이 준 key 는 근거가 아니다(사용자 결정 A13 · PLAN §4.3). 서버가 스스로 부른 풀이(StepForge find_plm_task — 허브 거울)의
    결과 행 가운데 key 가 글자 그대로 같은 행이 **하나일 때만** 확인(confirmed)이다. 아니면 주장(claimed)이고 어느 과제에도 잇지 않는다.
  · 차수 — 개발 단계 코드 열한 개다(사용자 결정 11-02 PG-17). 정규화는 NFKC(전각 → 반각) · 공백 빼기 · 소문자뿐이고, 그 뒤 꼴에 맞지 않으면
    차수 모름(null)이다. '2차' · 'C2' · '양산' 을 코드로 읽지 않는다 — 원천이 그 대응을 적지 않았다(PLAN §4.6). 받은 글은 text 에 남긴다.
  · 주제가 없으면 아무것도 잇지 않는다(PLAN §3.4). 이 모듈은 주제를 짓지 않는다 — 질문 · 결정문 · 도구 결과에서 짐작하지 않는다.

**칸(원장 · 대화 meta 의 subject).** sp_task(허브 key — 없음이면 null) · sp_state(confirmed 확인 · claimed 주장 · none 없음) ·
sp_resolved_by(그 key 를 낸 서버 호출의 지문 {tool, app, at, sha256} — 확인일 때만) · sp_why(주장인 까닭) · round(차수 코드 · 모르면 null) ·
round_by(person — 사람이 골랐다 · 차수가 없으면 null) · text(사람이 적은 글) · artifacts([{kind, app, id, rev}] — 설계 자료).
부르는 쪽은 sp_task · round · text · artifacts 넷만 준다 — 상태 · 지문은 서버가 적는다(모르는 칸은 거절한다).

**RA 보고서에는 싣지 않는다**(PLAN §4.1 · PG-11) — 엔진(deliberation.py)에 주제를 넘기지 않는다. 그 길이 없으니 결정문 · 보고서 · 위키 원천에
주제가 들어갈 자리가 없다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata

log = logging.getLogger("agent.subject")

# 차수 — 개발 단계 코드 열한 개. dv · pv 는 차수(1 · 2 · 3 · r)가 있어야 하고 pre · pra · mp 에는 없다 — DynaForge 정본 규칙(KooRemapper
# platform/backend/app/reports/datahub.py:42-50) · AIDataHub eng_meta 와 같은 어휘다. 리스크 앱 웹 폼이 받는 'dv' · 'pre1' 은 차수가 아니다.
STAGES = ("pre", "dv1", "dv2", "dv3", "dvr", "pv1", "pv2", "pv3", "pvr", "pra", "mp")
ROUND_UNKNOWN = "unknown"           # deliberate_list(subject) 의 '차수 모름' 칸(사용자 결정 11-02 PG-17 ①)
CONFIRMED, CLAIMED, NONE = "confirmed", "claimed", "none"      # PLAN §3.2 의 확인 · 주장 · 없음
BY_PERSON = "person"                # round_by — 원천이 아니라 사람이 골랐다(PLAN §4.6 '사람')

FIELDS = ("sp_task", "round", "text", "artifacts")             # 부르는 쪽이 주는 칸
ART_FIELDS = ("kind", "app", "id", "rev")
SP_TASK_MAX, ROUND_MAX, TEXT_MAX, REV_MAX, ARTIFACTS_MAX, WHY_MAX = 200, 40, 500, 120, 8, 400
# 설계 자료 열쇠의 꼴(PLAN §4.4) — 대소문자를 가리지 않는 꼴이라 원천이 내는 꼴로 맞춘다(16진은 소문자 · ULID 는 대문자).
_ART_ID = {
    # StepForge project_id — 밀리초 시각 13 + uuid 10 의 16진 23자(StepForge app/db.py new_id)
    "mcad": (re.compile(r"[0-9a-f]{23}"), str.lower, "StepForge project_id(16진 23자)"),
    # ODB 허브 job_id — 파일 내용 기반 16진 16자(HWAXPortal docs/procedures/fixtures/odb-hub/reference.md 3절)
    "ecad": (re.compile(r"[0-9a-f]{16}"), str.lower, "ODB 허브 job_id(16진 16자)"),
    # DynaForge 리포트 id — ULID 26자(크록퍼드 32진 — KooRemapper reports/services.py 의 ulid.new().str)
    "cae": (re.compile(r"[0-9A-HJKMNP-TV-Z]{26}"), str.upper, "DynaForge 리포트 id(ULID 26자)"),
}
_APP = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")

# 풀이 도구 — StepForge find_plm_task(허브 코어 API 거울에서 과제코드 · 별칭 · 과제명 · 모델 · 프로젝트로 찾는다, 결과 행마다 key — StepForge
# core/sp_core.py find_tasks). 게이트웨이의 **호출 전용 별칭**(<백엔드 키에서 하이픈 뺀 것>_<도구>)으로 부른다 — 노출 이름은 박스마다 다를 수 있고
# (dev 는 맨 이름 find_plm_task · cae00 은 선언 백엔드 stepforge-prod(운영 사본)와 겹쳐 **그쪽에만** 접두가 붙는다 — HWAXPortal docs/request-1027 HA-23 의
# stepforgeprod_find_plm_task 가 운영 사본이고 허브 StepForge 는 맨 이름이다, 게이트웨이 974e7ed 부터 · HWAXPortal docs/ontology-first OF-3),
# 별칭은 겹침 · 접두와 무관하게 늘 선다(HWAXMcpGateway gateway.py _aggregate). 허브 앱 StepForge 하나를 정본으로 부른다.
RESOLVER_APP, RESOLVER_TOOL = "heax-step_forge", "find_plm_task"
RESOLVER_CALL = f"{RESOLVER_APP.replace('-', '')}_{RESOLVER_TOOL}"
# 결과 행 수 — 활성이고 글자가 같은 행이 앞이다(find_tasks 의 차례). full=true 는 StepForge 챗 접기를 끈다(행 · 칸을 줄이지 않는다 — 접는 도구에만
# 그 인자가 있고, 없는 판의 FastMCP 는 모르는 인자를 버린다).
RESOLVE_LIMIT = 50
# 한 번 부르는 기한(초) — MCP 시작 응답 안에서 기다린다(게이트웨이 호출 한도 600초보다 한참 안쪽). 넘으면 주장으로 두고 시작은 그대로 간다.
RESOLVE_TIMEOUT_S = 20.0


class SubjectError(ValueError):
    """주제의 꼴이 틀렸다 — 무엇이 틀렸는지 말한다(시작 전에 거절한다)."""


# 차수 정규화가 빼는 '공백' — 유니코드 White_Space 성질의 글자 그대로다. 파이썬 str.split 은 정보 분리 문자(U+001C ~ U+001F)까지, JS 의 \s 는
# BOM(U+FEFF)까지 공백으로 읽어 두 쪽이 갈렸다(반대 검토 11-02) — 화면(HWAXPortal frontend subject.ts SPACE_RE)이 같은 글자 묶음을 쓴다(시험이 견준다).
_SPACE_RE = re.compile("[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")


def norm_round(v) -> str | None:
    """차수 글 → 코드(STAGES) · 아니면 None(차수 모름). NFKC(전각 → 반각) · 공백(_SPACE_RE) 빼기 · 소문자만 하고 글자 그대로 견준다 — 하이픈을
    빼지 않고, '2차' · 'C2' · '양산' · 'dv' · 'pre1' 을 코드로 읽지 않는다(PLAN §4.6)."""
    if not isinstance(v, str):
        return None
    s = _SPACE_RE.sub("", unicodedata.normalize("NFKC", v)).lower()
    return s if s in STAGES else None


def _text(raw, name: str, cap: int) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise SubjectError(f"subject.{name} 는 글이다 — 받은 꼴: {type(raw).__name__}")
    s = raw.strip()
    if len(s) > cap:
        raise SubjectError(f"subject.{name} 가 {len(s):,}자다 — {cap:,}자까지 받는다")
    return s


def _artifact(i: int, a) -> dict:
    if not isinstance(a, dict):
        raise SubjectError(f"subject.artifacts[{i}] 는 {{kind, app, id, rev}} 객체다 — 받은 꼴: {type(a).__name__}")
    extra = sorted(set(a) - set(ART_FIELDS))
    if extra:
        raise SubjectError(f"subject.artifacts[{i}] 에 모르는 칸 {extra} — 받는 칸은 {' · '.join(ART_FIELDS)} 다")
    kind = a.get("kind")
    if kind not in _ART_ID:
        raise SubjectError(f"subject.artifacts[{i}].kind 는 {' · '.join(_ART_ID)} 가운데 하나다 — 받은 값: {str(kind)[:40]!r}")
    pat, fold, what = _ART_ID[kind]
    rid = a.get("id")
    rid = fold(rid.strip()) if isinstance(rid, str) else rid
    if not isinstance(rid, str) or not pat.fullmatch(rid):
        raise SubjectError(f"subject.artifacts[{i}].id 는 {what} 꼴이다 — 받은 값: {str(rid)[:40]!r}")
    out = {"kind": kind, "id": rid}
    app = a.get("app")
    if app not in (None, ""):
        if not isinstance(app, str) or not _APP.fullmatch(app.strip().lower()):
            raise SubjectError(f"subject.artifacts[{i}].app 는 게이트웨이 앱 키 꼴이다(영문 소문자 · 숫자 · _.- 80자) — 받은 값: {str(app)[:40]!r}")
        out["app"] = app.strip().lower()
    rev = _text(a.get("rev"), f"artifacts[{i}].rev", REV_MAX)
    if rev:
        out["rev"] = rev
    return out


def parse(raw) -> dict | None:
    """부르는 쪽이 준 주제 → 꼴을 맞춘 입력 {sp_task, round_raw, text, artifacts} — 비었으면 None(주제 없음). 틀리면 SubjectError.

    받는 칸은 FIELDS 넷이다 — 상태(sp_state) · 지문(sp_resolved_by) · 차수 출처(round_by)를 실어 오면 모르는 칸으로 거절한다(서버가 적는 칸을
    부르는 쪽이 지어내지 못하게). 차수는 여기서 코드로 읽지 않는다 — 꼴에 안 맞는 글을 text 에 남기려면 받은 글이 있어야 한다(_record)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SubjectError(f"subject 는 {{{', '.join(FIELDS)}}} 객체다 — 받은 꼴: {type(raw).__name__}")
    extra = sorted(set(raw) - set(FIELDS))
    if extra:
        raise SubjectError(f"subject 에 모르는 칸 {extra} — 받는 칸은 {' · '.join(FIELDS)} 다(확인 상태 · 지문 · 차수 출처는 서버가 적는다)")
    arts = raw.get("artifacts")
    if arts is None:
        arts = []
    if not isinstance(arts, list):
        raise SubjectError(f"subject.artifacts 는 목록이다 — 받은 꼴: {type(arts).__name__}")
    if len(arts) > ARTIFACTS_MAX:
        raise SubjectError(f"subject.artifacts 가 {len(arts)}건이다 — {ARTIFACTS_MAX}건까지 받는다")
    seen, artifacts = set(), []
    for i, a in enumerate(arts):
        got = _artifact(i, a)
        if (got["kind"], got["id"]) not in seen:
            seen.add((got["kind"], got["id"]))
            artifacts.append(got)
    out = {"sp_task": _text(raw.get("sp_task"), "sp_task", SP_TASK_MAX), "round_raw": _text(raw.get("round"), "round", ROUND_MAX),
           "text": _text(raw.get("text"), "text", TEXT_MAX), "artifacts": artifacts}
    if not (out["sp_task"] or out["round_raw"] or out["text"] or artifacts):
        return None
    return out


def risk_family(chair_template, started_by=None) -> bool:
    """리스크 심사가 낳은 심의인가 — 의장 틀 risk-review(봉인 심사 risk-review-sealed 도 이 틀이다) · 리스크 앱이 띄운 패널(started_by=app).
    이 심의에는 주제를 달지 않는다 — 리스크 심사의 주제는 리스크 앱 과제다(PLAN §3.1 · §4.7 M2b), 리스크 판정은 그 앱 한 곳에만 있다."""
    chair = str(chair_template or "").strip().lower()
    return chair.startswith("risk-review") or str(started_by or "").strip().lower() == "app"


def _record(parsed: dict, *, state: str, sp_task: str | None = None, by: dict | None = None, why: str = "") -> dict:
    """원장 · 대화 meta 에 싣는 주제 — 칸은 모듈 머리말. 꼴에 안 맞는 차수 글은 차수 모름(null)이고 그 글은 text 에 남는다(PLAN §4.6)."""
    raw_round = parsed.get("round_raw") or ""
    code = norm_round(raw_round) if raw_round else None
    text = parsed.get("text") or ""
    if raw_round and code is None and raw_round not in text:
        text = f"{text} · {raw_round}" if text else raw_round
    rec = {"sp_task": sp_task, "sp_state": state, "sp_resolved_by": by, "round": code,
           "round_by": BY_PERSON if code else None, "text": text, "artifacts": list(parsed.get("artifacts") or [])}
    if why:
        rec["sp_why"] = why[:WHY_MAX]
    return rec


async def call_resolver(app, *, groups, user: str, user_pat: str, args: dict) -> tuple[bool, str]:
    """풀이 도구를 **그 심의와 같은 자격으로** 한 번 부른다 — (오류인가, 글). 연결 설정(app.state.connections)을 엔진의 도구 로드와 같은 함수
    (app._with_groups)로 그 사람의 그룹 · 신원 머리와 함께 쓴다 — 웹 심의는 그 사람의 PAT, MCP 잡은 게이트웨이 공유 비밀 + 신원 머리(엔진이 그 잡의
    좌석 조회를 부르는 길과 같다). 게이트웨이가 그 사람의 플랫폼 허가로 막으면 forbidden 으로 온다(오류). 도구 목록을 받지 않고 한 번만 부른다.
    시험의 이음매다 — 바꿔 끼울 때는 실제 StepForge 응답 꼴을 낸다(tests/fixtures/subject)."""
    from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: PLC0415 — 쓸 때만

    from app import _with_groups  # noqa: PLC0415 — app 이 이 모듈을 부른다(순환 import 회피 — deliberation._with_groups 와 같은 꼴)
    conns = getattr(getattr(app, "state", None), "connections", None) or {}
    if not conns:
        raise RuntimeError("게이트웨이 연결 설정이 없다(MCP_CONFIG)")
    scoped = _with_groups(conns, sorted(str(g) for g in (groups or [])), user or "", user_pat or "")
    client = MultiServerMCPClient(scoped)
    miss = ""
    for name in scoped:
        async with client.session(name) as s:
            res = await s.call_tool(RESOLVER_CALL, args)
        text = "\n".join(str(getattr(c, "text", "") or "") for c in (res.content or []) if getattr(c, "type", "") == "text")
        # 게이트웨이는 'unknown tool: …', FastMCP 서버는 'Unknown tool: …' 로 답한다 — 대소문자를 가리지 않는다(반대 검토 11-02)
        if res.isError and text.lstrip().lower().startswith("unknown tool"):
            miss = text          # 이 연결에는 그 도구가 없다 — 다음 연결(게이트웨이가 하나면 여기서 끝난다)
            continue
        return bool(res.isError), text
    return True, miss or f"unknown tool: {RESOLVER_CALL}"


def _rows(text: str) -> tuple[list[dict], str]:
    """풀이 답 → (결과 행, 0건의 까닭) — find_tasks 꼴 {query, count, tasks[{key, label, …}], why?, sync}. query 칸은 질의를 되실은 것이라 보지 않는다."""
    try:
        body = json.loads(text)
    except ValueError:
        return [], "답을 JSON 으로 읽지 못했다"
    if not isinstance(body, dict):
        return [], "답이 객체가 아니다"
    rows = [r for r in (body.get("tasks") or []) if isinstance(r, dict)] if isinstance(body.get("tasks"), list) else []
    why = body.get("why") if isinstance(body.get("why"), dict) else {}
    return rows, str(why.get("cause") or "")


async def confirm(app, parsed: dict, *, groups, user: str, user_pat: str = "") -> dict:
    """꼴을 맞춘 입력(parse) → 원장 주제. sp_task 가 있으면 풀이 도구를 그 심의의 자격으로 한 번 불러(call_resolver) 결과 행 가운데 key 가
    글자 그대로 같은 행이 하나일 때만 확인으로 적는다 — 질의를 되실은 칸 · 부분 일치 · 과제코드 · 별칭으로 걸린 행(key 가 다르다) · 같은 key 가
    둘 이상 · 부르지 못함 · 오류 · 기한 넘김은 모두 주장(claimed)이고 까닭(sp_why)을 단다. sp_task 가 없으면 부르지 않고 없음(none)이다.
    시작을 막지 않는다 — 확인하지 못한 주제는 어느 과제에도 잇지 않을 뿐이다(PLAN §3.3 · §4.3)."""
    key = parsed.get("sp_task") or ""
    if not key:
        return _record(parsed, state=NONE)
    args = {"query": key, "limit": RESOLVE_LIMIT, "full": True}
    try:
        is_err, text = await asyncio.wait_for(call_resolver(app, groups=groups, user=user, user_pat=user_pat, args=args),
                                              RESOLVE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _record(parsed, state=CLAIMED, sp_task=key,
                       why=f"허브 과제 풀이({RESOLVER_TOOL})가 {RESOLVE_TIMEOUT_S:g}초 안에 답하지 않았다 — 확인하지 못했다")
    except Exception as exc:  # noqa: BLE001 — 풀이를 못 불러도 심의는 연다(주장으로 둔다)
        log.warning("주제 풀이 실패(%s) — 주장으로 둔다: %r", RESOLVER_CALL, exc)
        return _record(parsed, state=CLAIMED, sp_task=key, why=f"허브 과제 풀이({RESOLVER_TOOL})를 부르지 못했다({type(exc).__name__})")
    if is_err:
        return _record(parsed, state=CLAIMED, sp_task=key,
                       why=f"허브 과제 풀이({RESOLVER_TOOL})가 오류로 답했다 — {' '.join(text.split())[:200]}")
    rows, cause = _rows(text)
    exact = [r for r in rows if r.get("key") == key]
    if len(exact) == 1 and exact[0].get("active") is False:
        # 허브에서 지워졌거나 합쳐진 과제(거울은 비활성 행도 낸다 — StepForge task_view 의 active · gone_reason · merged_into). 받는 앱 자신도
        # 이 행을 hub_inactive 로 읽는다(StepForge core/sp_core.py task_for_code) — 산 과제가 아니라 확인하지 않는다(반대 검토 11-02). 합쳐진 곳
        # (merged_into)의 key 는 싣지 않는다 — 아래 '결과 행의 key' 와 같은 까닭이다.
        return _record(parsed, state=CLAIMED, sp_task=key,
                       why=f"허브에서 비활성인 과제다({str(exact[0].get('gone_reason') or '까닭 모름')[:40]}) — {RESOLVER_TOOL} 로 지금 산 과제를 "
                           "찾아 그 key 로 단다")
    if len(exact) == 1:
        by = {"tool": RESOLVER_CALL, "app": RESOLVER_APP, "at": round(time.time(), 3),
              "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        return _record(parsed, state=CONFIRMED, sp_task=key, by=by)
    if exact:
        why = f"풀이 결과에 key 가 글자 그대로 같은 행이 {len(exact)}개다 — 고르지 않는다"
    else:
        # 결과 행의 key 를 까닭에 싣지 않는다 — 이 까닭은 잡 원장에 남아 같은 소속이 읽는데, 그 사람에게 StepForge 허가가 없을 수 있다(PLAN §6.2 —
        # 좁은 근거가 넓은 자리로 옮겨 가면 샌다. 반대 검토 11-02). 부르는 쪽은 같은 풀이를 제 자격으로 다시 부르면 된다.
        why = (f"풀이 결과 {len(rows)}행 가운데 key 가 글자 그대로 같은 행이 없다" + (f"(풀이의 까닭 {cause})" if cause else "")
               + f" — {RESOLVER_TOOL} 로 찾아 결과 행의 key(운영 허브는 과제명 — 과제코드가 아니다)를 그대로 주면 확인된다")
    return _record(parsed, state=CLAIMED, sp_task=key, why=why)


def same_input(rec: dict, parsed: dict) -> bool:
    """원장의 주제(rec)와 새로 받은 입력(parse)이 같은 주제인가 — 받는 칸 넷(과제 · 차수 · 글 · 설계 자료)을 _record 와 같은 꼴로 견준다. 확인 상태
    · 지문은 보지 않는다(같은 주제를 다시 실은 이어하기를 거절하지 않으려고)."""
    want = _record(parsed, state=NONE, sp_task=parsed.get("sp_task") or None)
    return all(rec.get(k) == want.get(k) for k in ("sp_task", "round", "text", "artifacts"))


def say(rec: dict) -> str:
    """주제 한 줄 — 화면 상태 줄 · MCP 응답 안내가 같은 글을 쓴다."""
    rnd = f" · 차수 {rec['round']}" if rec.get("round") else " · 차수 모름"
    st = rec.get("sp_state")
    if st == CONFIRMED:
        return f"주제 — 과제 「{rec.get('sp_task')}」(허브 거울 풀이로 확인){rnd}"
    if st == CLAIMED:
        return (f"주제 — 과제 「{rec.get('sp_task')}」 는 확인하지 못했다 — 어느 과제에도 잇지 않는다({rec.get('sp_why') or '까닭 없음'})"
                + rnd)
    return "주제 — 과제 없음(글 · 설계 자료만 남긴다 — 어느 과제에도 잇지 않는다)" + rnd


def parse_filter(raw) -> dict:
    """deliberate_list 의 주제 거름 {sp_task, round?} → {sp_task, round} — round 는 코드 · ROUND_UNKNOWN(차수 모름 칸만) · None(모든 차수).
    차수 꼴이 아니면 거절한다(아무것도 안 걸리는 거름을 말없이 돌려주지 않는다)."""
    if not isinstance(raw, dict):
        raise SubjectError("subject 는 {sp_task, round?} 객체다")
    extra = sorted(set(raw) - {"sp_task", "round"})
    if extra:
        raise SubjectError(f"subject 거름에 모르는 칸 {extra} — 받는 칸은 sp_task · round 다")
    key = raw.get("sp_task")
    if not isinstance(key, str) or not key.strip():
        raise SubjectError("subject.sp_task(허브 과제 key)가 있어야 거른다")
    rnd = raw.get("round")
    if rnd is None or (isinstance(rnd, str) and not rnd.strip()):
        code = None
    elif isinstance(rnd, str) and rnd.strip().lower() in (ROUND_UNKNOWN, "모름"):
        code = ROUND_UNKNOWN
    else:
        code = norm_round(rnd)
        if code is None:
            raise SubjectError(f"subject.round {str(rnd)[:40]!r} 는 차수가 아니다 — {' · '.join(STAGES)} 가운데 하나다"
                               f"(차수 모름 칸만 보려면 '{ROUND_UNKNOWN}', 모든 차수는 round 를 뺀다)")
    return {"sp_task": key.strip(), "round": code}


def matches(job: dict, filt: dict) -> bool:
    """그 잡이 주제 거름에 맞나 — 확인된 주제만, 과제 key 글자 그대로, 차수는 정확히(차수 모름은 차수 모름 칸에만 — 어느 차수에도 섞지 않는다).
    리스크 심사 계열은 맞지 않는다(주제를 달지 않지만 한 번 더 막는다)."""
    s = job.get("subject")
    if not isinstance(s, dict) or s.get("sp_state") != CONFIRMED or risk_family(job.get("chair_template")):
        return False
    if s.get("sp_task") != filt["sp_task"]:
        return False
    want = filt.get("round")
    if want is None:
        return True
    if want == ROUND_UNKNOWN:
        return s.get("round") is None
    return s.get("round") == want

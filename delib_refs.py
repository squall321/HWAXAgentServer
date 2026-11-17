# 심의 근거의 참조(ref {kind, id})를 그 사람 신원으로 게이트웨이를 불러 그 자리에 펼친다 — 지문 대조 · 잡 원장 기록 · 미리 보기(DL-01)
"""HWAXPortal docs/request-1117 contracts §1 · §2 (DL-01).

**꼴.** evidence 항목은 본문({source, tool, args, result, key}) 대신 `{"ref": {"kind": …, "id": …}}` 하나일 수 있다(본문 항목과 섞어 쓴다). 참조 항목에는
ref 밖의 칸을 두지 않는다 — 본문과 참조를 한 항목에 함께 주면 어느 쪽이 근거인지 갈린다.

**푸는 길.** 시작 전에 MCP 잡과 같은 자격(게이트웨이 공유 비밀 + 그 사람의 신원 · 그룹 머리 — app._with_groups, 엔진이 좌석 조회를 부르는 길 ·
delib_subject.call_resolver 와 같다)으로 게이트웨이의 호출 전용 별칭(<백엔드 키에서 하이픈 뺀 것>_<도구>)을 한 번 부른다. 새 토큰 · 신원 길은 없다.
풀린 항목은 그 자리에 차례대로 펼친다.

kind 이름은 넷 그대로다(사용자 결정 C1 — Report Archive 의 인용 source_kind 와 같은 낱말) — risk_bundle · reportarchive_file · dynaforge_report ·
stepforge_job. 연 차례는 사용자 결정 C2(남의 판을 기다리지 않고 id 가 이미 있는 것부터).

  · risk_bundle → heax-hwax_risk 의 risk_get_evidence_bundle(bundle_id=id)(contracts §1 '받기' — 리스크 앱이 짓는 중이라 그 꼴로 짓는다) → 답의
    evidence 를 펼친다. 펼친 evidence 를 §1 의 식으로 다시 재어 묶음의 fingerprint 와 다르면 그 묶음을 싣지 않는다(조용히 싣지 않는다).
  · reportarchive_file → reportarchive 의 download_file(file_id=id)(ReportArchive mcp_server/server.py — 답 {file_id, filename, mime_type, size, is_image,
    encoding: base64, data_base64} · 실패는 {error}). 바이트를 주므로 **글 파일만** 싣는다(text/* · JSON · XML · YAML · CSV — UTF-8 로 풀리는 것).
    그림 · 바이너리는 판독 규칙이 없어 빼고 까닭을 남긴다. 항목 하나.
  · stepforge_job → heax-step_forge 의 job_status(job_id=id, full=true)(StepForge app/mcp_server.py — 없는 잡은 도구 오류 '잡 를 찾을 수 없다'). 끝난
    (status done) 잡만 싣는다 — 결과와 그 낡음 표지(result_stale 등)를 JSON 항목 하나로. 판(source_version)은 결과의 잰 때 형상 키(result.job.geo_key)다.
  · dynaforge_report 는 열지 않았다 — DynaForge 의 DF-01(리포트를 근거로 내는 도구)을 기다린다.
  · 풀이 실패(권한 · 없음 · 불통 · 답 꼴 다름 · 글이 아님 · 끝나지 않음)는 그 항목을 빼고 evidence_omitted 에 `ref:<kind>:<id> — 까닭`. 심의는 연다.
  · 꼴이 틀린 참조와 열지 않은 kind 는 **잡을 만들기 전에** 거절한다(RefError) — 그대로 열면 그 근거 없이 수 시간을 돈다.

**원장.** 풀이 결과(kind · id · 실렸나 · 항목 수 · 자수 · 지문 · 시각 · 까닭)를 잡 원장 evidence_refs 에 남긴다(본문은 남기지 않는다 — 시작 인자
파일에는 펼친 근거가 그대로 있어 재기동 · 끊긴 잡의 이어하기가 같은 근거로 돈다). 실린 참조마다 인용 cite {source_platform, source_kind, source_id,
source_version, scope} 를 단다 — source_version 은 답이 판을 말할 때만(리스크 묶음은 답의 cite.source_version), 아니면 null. scope 는 답이 말하는 자리
(리스크 묶음은 답의 cite.scope, 없으면 target_key · StepForge 잡은 project_id · RA 파일은 null). deliberate_continue 는 앞 잡의 참조를 다시 풀어 같은
근거를 싣고, 지문이 앞 잡과 같은지 적는다(previous).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time

import deliberation as _engine   # 근거 본문 고르기(_ev_bodies) · 상한 — 엔진이 읽는 대로 센다

log = logging.getLogger("agent.refs")

# kind → (게이트웨이 백엔드 키, 도구, 인자를 짓는 함수, 인용의 source_platform). 답을 읽는 함수는 아래 _CHECKS 다(모듈 머리말).
KINDS: dict[str, tuple] = {
    "risk_bundle": ("heax-hwax_risk", "risk_get_evidence_bundle", lambda rid: {"bundle_id": rid}, "hwax_risk"),
    "reportarchive_file": ("reportarchive", "download_file", lambda rid: {"file_id": rid}, "reportarchive"),
    "stepforge_job": ("heax-step_forge", "job_status", lambda rid: {"job_id": rid, "full": True}, "step_forge"),
}
# 이름은 정했지만 열지 않은 kind → 까닭(사용자 결정 C2 — 그 플랫폼의 판을 기다린다).
NOT_OPENED: dict[str, str] = {
    "dynaforge_report": "DynaForge 의 DF-01(리포트를 근거로 내는 도구)을 기다린다",
}
ID_MAX = 200
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,%d}" % ID_MAX)
REFS_MAX = 40                   # 한 근거 목록의 참조 수 — 펼친 뒤의 항목 수 상한은 엔진(_EVID_ITEMS)이 건다
RESOLVE_TIMEOUT_S = 30.0        # 참조 하나를 푸는 기한(초) — MCP 시작 응답 안에서 기다린다(게이트웨이 호출 한도 600초보다 한참 안쪽)
RESOLVE_CONCURRENCY = 8         # 한 근거 목록에서 동시에 푸는 참조 수
RESOLVE_TOTAL_S = 120.0         # 한 근거 목록을 다 푸는 전체 기한(초) — 넘은 참조는 불통으로 뺀다
OMIT_SOURCE = "근거 참조 풀이"
_FP_RE = re.compile(r"[0-9a-f]{64}")
MASK_MARK = "[가림:"             # 게이트웨이 개인정보 가림이 값 자리에 넣는 표지(HWAXMcpGateway gateway.py _pii_mask_text)


class RefError(ValueError):
    """참조의 꼴이 틀렸거나 열지 않은 kind 다 — 무엇이 틀렸는지 말한다(잡을 만들기 전에 거절한다)."""


def fingerprint(evidence) -> str:
    """묶음 지문(contracts §1) — 정규 JSON(키 차례 · 빈칸 없음 · 한글 그대로)의 sha256 hex."""
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def label(kind: str, rid: str) -> str:
    return f"ref:{kind}:{rid}"


REF_SIDE = ("source", "key")      # 참조 항목에 함께 둘 수 있는 칸 — 한 항목을 내는 kind 의 근거 표식 · 키로 쓴다(묶음 항목은 제 것을 쓴다)


def _ref_of(i: int, it) -> dict | None:
    """항목 하나가 참조면 {kind, id(, source, key)}, 아니면 None. 참조는 ref 가 **객체**이고 엔진이 고를 본문(_ev_bodies — 비지 않은 본문 칸)이
    없는 항목뿐이다 — ref 가 글이거나 본문이 있는 항목은 종전처럼 보통 근거다(반대 검토 M3 — 출처 표식으로 ref 글을 실어 보내던 호출이 깨지지
    않게). 빈 본문 칸({"result": null} · {"text": ""})은 본문이 아니다 — 그런 항목도 참조다(2차 검토 7 — 종전엔 말없이 빠졌다). 꼴이 틀리면 RefError."""
    if not isinstance(it, dict) or not isinstance(it.get("ref"), dict) or _engine._ev_bodies(it):
        return None
    extra = sorted(set(it) - {"ref", *REF_SIDE, *_engine._EVID_BODY_KEYS})
    if extra:
        raise RefError(f"evidence[{i}] 는 참조(ref) 항목인데 모르는 칸 {extra[:5]} 이 함께 있다 — 참조 항목은 {{ref: {{kind, id}}, source?, key?}} 다"
                       "(본문은 result 를 쓴 따로 된 항목으로)")
    ref = it["ref"]
    if sorted(ref) != ["id", "kind"]:
        raise RefError(f"evidence[{i}].ref 는 {{kind, id}} 객체다 — 받은 칸 {sorted(ref)[:5]}")
    kind, rid = ref.get("kind"), ref.get("id")
    if not isinstance(kind, str) or kind not in KINDS:
        if kind in NOT_OPENED:
            raise RefError(f"evidence[{i}].ref.kind={kind} 는 아직 열지 않은 kind 다(kind not opened yet — {NOT_OPENED[kind]}). 지금 받는 "
                           f"kind: {' · '.join(KINDS)}. 그 근거는 도구로 뽑아 result 에 넣는다")
        raise RefError(f"evidence[{i}].ref.kind 는 {' · '.join(KINDS)} 가운데 하나다 — 받은 값: {str(kind)[:40]!r}")
    if not isinstance(rid, str) or not _ID_RE.fullmatch(rid.strip()):
        raise RefError(f"evidence[{i}].ref.id 는 영문 · 숫자 · _.:- {ID_MAX}자 안의 글이다 — 받은 값: {str(rid)[:40]!r}")
    return {"kind": kind, "id": rid.strip(), **{k: it[k] for k in REF_SIDE if isinstance(it.get(k), str) and it[k].strip()}}


def parse(evidence) -> list | None:
    """근거 목록의 참조 자리 — [(자리, {kind, id, …} 또는 None)]. 참조가 하나도 없으면 None(종전 길 그대로). 꼴이 틀리면 RefError."""
    if not isinstance(evidence, list):
        return None
    slots = [(i, _ref_of(i, it)) for i, it in enumerate(evidence)]
    n = sum(1 for _i, r in slots if r is not None)
    if not n:
        return None
    if n > REFS_MAX:
        raise RefError(f"참조가 {n}건이다 — 한 번에 {REFS_MAX}건까지 받는다")
    return slots


def body_chars(items: list) -> int:
    """엔진이 실을 본문 자수 — 항목마다 엔진이 고르는 첫 본문(_ev_bodies)의 길이 합(본문 없는 항목은 0)."""
    n = 0
    for it in items:
        b = _engine._ev_bodies(it) if isinstance(it, dict) else []
        n += len(b[0][1]) if b else 0
    return n


async def call_alias(app, *, groups, user: str, user_pat: str, alias: str, args: dict) -> tuple[bool, str]:
    """게이트웨이 호출 전용 별칭을 **그 심의와 같은 자격으로** 한 번 부른다 — (오류인가, 글). delib_subject.call_resolver 와 같은 길이다(연결 설정을
    app._with_groups 로 그 사람의 그룹 · 신원 머리와 함께 쓴다). 시험의 이음매다 — 바꿔 끼울 때는 리스크 앱 답 꼴(contracts §1 '받기')을 낸다."""
    from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: PLC0415 — 쓸 때만

    from app import _with_groups  # noqa: PLC0415 — app 이 이 모듈을 부른다(순환 import 회피)
    conns = getattr(getattr(app, "state", None), "connections", None) or {}
    if not conns:
        raise RuntimeError("게이트웨이 연결 설정이 없다(MCP_CONFIG)")
    scoped = _with_groups(conns, sorted(str(g) for g in (groups or [])), user or "", user_pat or "")
    client = MultiServerMCPClient(scoped)
    miss = ""
    for name in scoped:
        async with client.session(name) as s:
            res = await s.call_tool(alias, args)
        text = "\n".join(str(getattr(c, "text", "") or "") for c in (res.content or []) if getattr(c, "type", "") == "text")
        if res.isError and text.lstrip().lower().startswith("unknown tool"):
            miss = text
            continue
        return bool(res.isError), text
    return True, miss or f"unknown tool: {alias}"


def _why_error(text: str) -> str:
    t = " ".join(str(text or "").split())
    low = t.lower()
    if low.startswith("unknown tool"):
        return "게이트웨이에 그 도구가 없다(그 앱의 판이 이 도구 전이거나 앱이 꺼졌다)"
    if "forbidden" in low or "403" in low or "권한" in t:
        return f"권한이 없다(게이트웨이가 막았다 — {t[:160]})"
    if "찾을 수 없" in t or "not found" in low or "404" in low:
        return f"없다 — {t[:200]}"
    return f"도구가 오류로 답했다 — {t[:200]}"


def _scalar(v):
    """인용 칸에 싣는 값 — 글 · 정수만(참 · 거짓 · 객체는 없음)."""
    return v if isinstance(v, (str, int)) and not isinstance(v, bool) else None


def _body(text: str, who: str) -> tuple[dict | None, str]:
    """답 글 → (객체, 까닭). 앱의 MCP 도구는 예외 대신 {error, message} 로 답하기도 한다(리스크 앱 · RA — 없음 · 남의 것은 같은 not found)."""
    try:
        body = json.loads(text)
    except ValueError:
        return None, "답을 JSON 으로 읽지 못했다"
    if not isinstance(body, dict):
        return None, "답이 객체가 아니다"
    if body.get("error"):
        return None, f"{who} 거절했다({' '.join(str(body.get('error')).split())[:80]}" + (
            f" — {' '.join(str(body.get('message')).split())[:160]}" if body.get("message") else "") + ")"
    return body, ""


def _check_bundle(rid: str, text: str) -> tuple[list | None, dict]:
    """risk_get_evidence_bundle 답 → (펼칠 evidence 또는 None, 기록 칸). contracts §1 '받기' 꼴 {bundle_id, target_key, version, fingerprint, evidence}
    (+ 답이 싣는 인용 cite{source_version, scope} — 있으면 판 · 자리를 그것에서 읽는다)."""
    body, why = _body(text, "리스크 앱이")
    if body is None:
        return None, {"why": why}
    ev, fp = body.get("evidence"), body.get("fingerprint")
    if not isinstance(ev, list) or not all(isinstance(x, dict) for x in ev) or not isinstance(fp, str) or not _FP_RE.fullmatch(fp):
        return None, {"why": "답 꼴이 다르다 — {bundle_id, fingerprint(sha256 hex), evidence[항목 …]} 가 아니다"}
    if str(body.get("bundle_id") or "") != rid:
        return None, {"why": f"다른 묶음이 왔다(요청 {rid} · 답 {str(body.get('bundle_id'))[:60]})"}
    got = fingerprint(ev)
    cite = body.get("cite") if isinstance(body.get("cite"), dict) else {}
    rec = {"items": len(ev), "chars": body_chars(ev), "fingerprint": got, "bundle_fingerprint": fp,
           **{k: body[k] for k in ("target_key", "version") if _scalar(body.get(k)) is not None},
           "_version": _scalar(cite.get("source_version")),
           "_scope": _scalar(cite.get("scope")) if cite.get("scope") is not None else _scalar(body.get("target_key"))}
    if got != fp:
        # 게이트웨이는 백엔드 답에 개인정보 가림(GATEWAY_PII_MASK — 메일 · 사설 주소 · IMEI · 계정 꼴을 '[가림:…]' 으로)을 건다. 묶음 글에 그 꼴이
        # 있으면 앱이 잰 지문과 늘 다르다 — 그 까닭을 따로 말한다(조용히 싣지 않는 것은 같다).
        masked = MASK_MARK in json.dumps(ev, ensure_ascii=False)
        return None, {**rec, "why": (f"지문이 다르다 — 묶음이 적은 {fp[:12]}… · 펼친 근거를 다시 잰 {got[:12]}…(묶음을 싣지 않았다)"
                                     + (" — 게이트웨이 개인정보 가림(GATEWAY_PII_MASK)이 묶음 글을 바꿨다(그 글에 '[가림:' 이 있다)" if masked else ""))}
    return ev, rec


# RA 파일 가운데 글로 싣는 것 — mime 이 이것이거나 이름 끝이 이것이면 글이다(그 밖은 판독 규칙이 없다).
_TEXT_MIMES = frozenset(("application/json", "application/xml", "application/x-yaml", "application/yaml", "application/csv",
                         "application/x-ndjson", "application/ld+json"))
_TEXT_EXTS = (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".log")


def _check_ra_file(rid: str, text: str) -> tuple[list | None, dict]:
    """download_file 답(ReportArchive mcp_server/server.py) → (항목 하나 또는 None, 기록 칸). 글 파일만 UTF-8 로 풀어 싣는다. 판은 답에 없다(null)."""
    import base64  # noqa: PLC0415 — 이 kind 만 쓴다
    body, why = _body(text, "Report Archive 가")
    if body is None:
        return None, {"why": why}
    if str(body.get("file_id") or "") != rid:
        return None, {"why": f"다른 파일이 왔다(요청 {rid} · 답 {str(body.get('file_id'))[:60]})"}
    name, mime = str(body.get("filename") or rid), str(body.get("mime_type") or "").split(";")[0].strip().lower()
    meta = {"filename": name[:200], "mime_type": mime[:80], **({"size": body["size"]} if _scalar(body.get("size")) is not None else {})}
    if body.get("encoding") != "base64" or not isinstance(body.get("data_base64"), str):
        return None, {**meta, "why": "답 꼴이 다르다 — {file_id, encoding: base64, data_base64} 가 아니다"}
    if body.get("is_image") or not (mime.startswith("text/") or mime in _TEXT_MIMES or name.lower().endswith(_TEXT_EXTS)):
        return None, {**meta, "why": f"글이 아닌 파일이다({mime or '형식 모름'}) — 그림 · 바이너리는 판독 규칙이 없어 근거로 싣지 않는다"}
    try:
        body_text = base64.b64decode(body["data_base64"], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, {**meta, "why": "파일을 UTF-8 글로 풀지 못했다"}
    item = {"source": f"Report Archive 파일 · {name[:160]}", "tool": "download_file",
            "args": json.dumps({"file_id": rid}, ensure_ascii=False), "result": body_text}
    return [item], {**meta, "items": 1, "chars": len(body_text), "fingerprint": fingerprint([item]), "_version": None, "_scope": None}


# StepForge job_status 답에서 근거로 싣는 칸 — 결과와 그 결과를 읽는 데 필요한 표지(잰 뒤 형상이 바뀌었나 · 낡음 · 맨 앞 경고)만(서버 경로 · 러너
# 표식 · 로그는 싣지 않는다).
_SF_KEEP = ("id", "kind", "project_id", "status", "finished_at", "params", "first_warning", "result", "result_stale", "result_stale_by",
            "result_stale_note", "result_unknowns", "also_in_now")


def _check_sf_job(rid: str, text: str) -> tuple[list | None, dict]:
    """job_status(job_id, full=true) 답(StepForge app/mcp_server.py _job_view) → (항목 하나 또는 None, 기록 칸). 끝난(done) 잡만. 판은 결과의 잰 때
    형상 키(result.job.geo_key — 측정 · 치수 잡의 표식), 자리는 project_id."""
    body, why = _body(text, "StepForge 가")
    if body is None:
        return None, {"why": why}
    if str(body.get("id") or "") != rid:
        return None, {"why": f"다른 잡이 왔다(요청 {rid} · 답 {str(body.get('id'))[:60]})"}
    st = body.get("status")
    meta = {"job_kind": _scalar(body.get("kind")), "status": _scalar(st)}
    if st != "done":
        return None, {**meta, "why": f"잡이 끝나지 않았다(status {str(st)[:20]}) — 끝난(done) 잡만 근거로 싣는다"}
    if "result" not in body:
        return None, {**meta, "why": "끝난 잡에 결과(result)가 없다"}
    keep = {k: body[k] for k in _SF_KEEP if k in body}
    item = {"source": f"StepForge 잡 {str(body.get('kind') or '')[:40]} · {rid}", "tool": "job_status",
            "args": json.dumps({"job_id": rid, "full": True}, ensure_ascii=False), "result": json.dumps(keep, ensure_ascii=False, default=str)}
    res = body.get("result") if isinstance(body.get("result"), dict) else {}
    mark = res.get("job") if isinstance(res.get("job"), dict) else {}
    return [item], {**meta, "items": 1, "chars": len(item["result"]), "fingerprint": fingerprint([item]),
                    "_version": _scalar(mark.get("geo_key")), "_scope": _scalar(body.get("project_id"))}


_CHECKS = {"risk_bundle": _check_bundle, "reportarchive_file": _check_ra_file, "stepforge_job": _check_sf_job}


async def _resolve_one(app, ref: dict, *, groups, user: str, user_pat: str) -> tuple[list | None, dict]:
    """참조 하나 → (펼칠 항목 또는 None, 기록). 한 항목을 내는 kind 는 참조 항목의 source · key 를 그 항목에 단다(지문은 단 뒤에 잰다)."""
    kind, rid = ref["kind"], ref["id"]
    app_key, tool, args_of, platform = KINDS[kind]
    rec: dict = {"kind": kind, "id": rid, "ok": False, "at": round(time.time(), 3)}
    side = {k: ref[k] for k in REF_SIDE if k in ref}
    if side:
        rec["side"] = side          # 이어하기가 같은 꼴로 다시 풀게(carried) — 한 항목 kind 는 이것이 지문에 든다(2차 검토 4)
    try:
        is_err, text = await asyncio.wait_for(
            call_alias(app, groups=groups, user=user, user_pat=user_pat, alias=f"{app_key.replace('-', '')}_{tool}", args=args_of(rid)),
            RESOLVE_TIMEOUT_S)
    except asyncio.TimeoutError:
        rec.update(why=f"{RESOLVE_TIMEOUT_S:g}초 안에 답하지 않았다(불통)", unreachable=True)
        return None, rec
    except Exception as exc:  # noqa: BLE001 — 하나를 못 풀어도 나머지는 푼다
        log.warning("근거 참조 %s 풀이 실패: %r", label(kind, rid), exc)
        rec.update(why=f"게이트웨이를 부르지 못했다({type(exc).__name__} — 불통)", unreachable=True)
        return None, rec
    if is_err:
        rec["why"] = _why_error(text)
        return None, rec
    ev, got = _CHECKS[kind](rid, text)
    version, scope = got.pop("_version", None), got.pop("_scope", None)
    rec.update(got)
    if ev is None:
        return None, rec
    if kind != "risk_bundle" and any(k in ref for k in REF_SIDE):
        ev = [{**ev[0], **{k: ref[k] for k in REF_SIDE if k in ref}}]
        rec["fingerprint"] = fingerprint(ev)
    rec["ok"] = True
    # 인용(사용자 결정 C1) — RA 인용의 source_kind 와 같은 낱말. 판 · 자리는 답이 말할 때만(지어내지 않는다)
    rec["cite"] = {"source_platform": platform, "source_kind": kind, "source_id": rid, "source_version": version, "scope": scope}
    return ev, rec


async def resolve(app, evidence: list, *, groups, user: str, user_pat: str = "", slots: list | None = None) -> dict:
    """참조를 풀어 펼친다 → {evidence(펼친 목록), refs[기록], omitted[evidence_omitted 줄]}. slots 는 parse 의 것(없으면 여기서 읽는다).

    **신원이 있어야 푼다**(반대 검토 M5) — 신원 없이 부르면 게이트웨이 공유 비밀만으로 가서 서비스 계정 시야가 된다(남의 묶음 · 파일 · 잡이 보일 수
    있다). 참조는 동시에(RESOLVE_CONCURRENCY) 풀고, 전체 기한(RESOLVE_TOTAL_S — 게이트웨이 호출 한도 600초보다 한참 안쪽) 안에 못 푼 것은
    불통으로 뺀다(반대 검토 L5). 펼친 차례는 근거 목록 차례 그대로다."""
    slots = slots if slots is not None else parse(evidence)
    if slots is None:
        return {"evidence": list(evidence or []), "refs": [], "omitted": []}
    if not str(user or "").strip():
        raise RefError("근거 참조는 신원(사람)이 있는 호출만 푼다 — 신원 없이 풀면 게이트웨이 공유 비밀만으로 서비스 계정 시야가 된다. 사람 PAT 로 "
                       "게이트웨이를 거쳐 부르거나 참조 대신 본문(result)을 싣는다")
    sem = asyncio.Semaphore(RESOLVE_CONCURRENCY)

    async def one(ref):
        async with sem:
            return await _resolve_one(app, ref, groups=groups, user=user, user_pat=user_pat)
    tasks = {i: asyncio.ensure_future(one(ref)) for i, ref in slots if ref is not None}
    try:
        if tasks:
            await asyncio.wait(list(tasks.values()), timeout=RESOLVE_TOTAL_S)
    finally:
        # 전체 기한을 넘었거나 부른 쪽이 접었다(요청이 끊김 · 묶음 멈춤) — 남은 게이트웨이 호출을 접고 끝날 때까지 기다린다(2차 검토 9 — 종전엔 부른
        # 쪽이 접혀도 호출이 남아 돌았다)
        late = [t for t in tasks.values() if not t.done()]
        for t in late:
            t.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)
    out, refs, omitted = [], [], []
    for i, ref in slots:
        if ref is None:
            out.append(evidence[i])
            continue
        t = tasks[i]
        if t.cancelled():
            ev, rec = None, {"kind": ref["kind"], "id": ref["id"], "ok": False, "at": round(time.time(), 3),
                             "why": f"전체 기한 {RESOLVE_TOTAL_S:g}초 안에 풀지 못했다(불통)", "unreachable": True,
                             **({"side": {k: ref[k] for k in REF_SIDE if k in ref}} if any(k in ref for k in REF_SIDE) else {})}
        elif t.exception() is not None:
            log.warning("근거 참조 %s 답 읽기 실패: %r", label(ref["kind"], ref["id"]), t.exception())
            ev, rec = None, {"kind": ref["kind"], "id": ref["id"], "ok": False, "at": round(time.time(), 3),
                             "why": f"답을 읽다 오류가 났다({type(t.exception()).__name__})"}
        else:
            ev, rec = t.result()
        refs.append(rec)
        if ev is not None:
            out.extend(ev)
        else:
            omitted.append({"source": OMIT_SOURCE, "text": f"{label(rec['kind'], rec['id'])} — {rec['why']}"})
    return {"evidence": out, "refs": refs, "omitted": omitted}


def compare(refs: list, prev_refs, prev_job_id: str) -> None:
    """이어하기 — 실린 참조마다 앞 잡의 같은 참조와 지문을 견줘 previous{job_id, fingerprint, same} 을 단다(앞 잡에 없으면 달지 않는다)."""
    before = {(r.get("kind"), r.get("id")): r for r in (prev_refs or []) if isinstance(r, dict) and r.get("ok")}
    for r in refs:
        p = before.get((r.get("kind"), r.get("id")))
        if p is not None:
            r["previous"] = {"job_id": prev_job_id, "fingerprint": p.get("fingerprint"), "same": p.get("fingerprint") == r.get("fingerprint")}


def carried(prev_refs) -> list:
    """앞 잡에서 실린 참조 → 다시 풀 근거 목록([{ref}] — 실리지 못한 참조는 잇지 않는다)."""
    return [{"ref": {"kind": r["kind"], "id": r["id"]},
             **{k: v for k, v in (r.get("side") or {}).items() if k in REF_SIDE and isinstance(v, str)}}
            for r in (prev_refs or []) if isinstance(r, dict) and r.get("ok") and r.get("kind") in KINDS and isinstance(r.get("id"), str)]


def say(refs: list) -> str:
    """응답 note 의 한 줄."""
    ok = [r for r in refs if r.get("ok")]
    bad = [r for r in refs if not r.get("ok")]
    s = f"근거 참조 {len(refs)}건을 풀었다 — 실림 {len(ok)}건(항목 {sum(int(r.get('items') or 0) for r in ok)}건 · " \
        f"{sum(int(r.get('chars') or 0) for r in ok):,}자)"
    if bad:
        s += f" · 빠짐 {len(bad)}건(까닭은 evidence_refs · deliberate_status 의 evidence_omitted)"
    changed = [r for r in ok if (r.get("previous") or {}).get("same") is False]
    if changed:
        s += f" · 앞 잡과 지문이 다른 참조 {len(changed)}건(previous.same 거짓 — 묶음이 바뀌었다)"
    return s + "."


def preview(res: dict, items_in: int, limits: dict) -> dict:
    """deliberate_resolve_evidence 의 답 — 크기 · 지문만(본문 없음 · 잡 없음)."""
    ev = res["evidence"]          # 펼친 목록 전체(참조가 낸 항목 포함)
    chars = body_chars(ev)
    over = {}
    if len(ev) > int(limits.get("evidence_items") or 0):
        over["items"] = f"펼친 항목 {len(ev)}건이 건수 상한 {limits['evidence_items']}건을 넘는다 — 뒤쪽은 좌석에 가지 않는다"
    if chars > int(limits.get("evidence_total_chars") or 0):
        over["chars"] = (f"펼친 본문 {chars:,}자가 합계 예산 {limits['evidence_total_chars']:,}자를 넘는다 — 앞 항목부터 채우다 넘치면 뒤 항목은 통째로 "
                         "빠진다")
    ref_items = sum(int(r.get("items") or 0) for r in res["refs"] if r.get("ok"))
    return {"items_in": items_in, "refs": res["refs"],
            "inline": {"items": len(ev) - ref_items, "chars": chars - sum(int(r.get("chars") or 0) for r in res["refs"] if r.get("ok"))},
            "expanded": {"items": len(ev), "chars": chars, "fingerprint": fingerprint(ev)},
            "limits": limits, "over": over, "omitted": res["omitted"],
            "note": ("미리 보기다 — 잡을 만들지 않았고 본문은 싣지 않았다. 같은 근거로 deliberate_start 를 부르면 시작 전에 다시 푼다(그 사이 묶음이 "
                     "바뀌면 지문이 달라진다).")}

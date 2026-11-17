# 심의 근거의 참조(ref {kind, id})를 그 사람 신원으로 게이트웨이를 불러 그 자리에 펼친다 — 지문 대조 · 잡 원장 기록 · 미리 보기(DL-01)
"""HWAXPortal docs/request-1117 contracts §1 · §2 (DL-01).

**꼴.** evidence 항목은 본문({source, tool, args, result, key}) 대신 `{"ref": {"kind": …, "id": …}}` 하나일 수 있다(본문 항목과 섞어 쓴다). 참조 항목에는
ref 밖의 칸을 두지 않는다 — 본문과 참조를 한 항목에 함께 주면 어느 쪽이 근거인지 갈린다.

**푸는 길.** 시작 전에 MCP 잡과 같은 자격(게이트웨이 공유 비밀 + 그 사람의 신원 · 그룹 머리 — app._with_groups, 엔진이 좌석 조회를 부르는 길 ·
delib_subject.call_resolver 와 같다)으로 게이트웨이의 호출 전용 별칭(<백엔드 키에서 하이픈 뺀 것>_<도구>)을 한 번 부른다. 새 토큰 · 신원 길은 없다.
풀린 항목은 그 자리에 차례대로 펼친다.

  · risk_bundle → heax-hwax_risk 의 risk_get_evidence_bundle(bundle_id=id) → 답의 evidence 를 펼친다. 펼친 evidence 를 §1 의 식으로 다시 재어 묶음의
    fingerprint 와 다르면 그 묶음을 싣지 않는다(조용히 싣지 않는다 — 까닭을 evidence_omitted 에).
  · 풀이 실패(권한 · 없음 · 불통 · 답 꼴 다름)는 그 항목을 빼고 evidence_omitted 에 `ref:<kind>:<id> — 까닭`. 심의는 연다.
  · 꼴이 틀린 참조와 열지 않은 kind 는 **잡을 만들기 전에** 거절한다(RefError) — 그대로 열면 그 근거 없이 수 시간을 돈다.
    reportarchive_file(RA download_file 은 바이트를 base64 로 준다 — 글 근거로 바꾸는 규칙이 아직 없다) · dynaforge_report · stepforge_job(도구 · 답 꼴을
    근거로 옮기는 규칙을 정하지 않았다)은 이름만 알고 열지 않았다.

**원장.** 풀이 결과(kind · id · 실렸나 · 항목 수 · 자수 · 지문 · 시각 · 까닭)를 잡 원장 evidence_refs 에 남긴다(본문은 남기지 않는다 — 시작 인자
파일에는 펼친 근거가 그대로 있어 재기동 · 끊긴 잡의 이어하기가 같은 근거로 돈다). deliberate_continue 는 앞 잡의 참조를 다시 풀어 같은 근거를 싣고,
지문이 앞 잡과 같은지 적는다(previous).
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

# kind → (게이트웨이 백엔드 키, 도구, 인자 이름). 답의 꼴은 contracts §1 '받기'.
KINDS: dict[str, tuple[str, str, str]] = {
    "risk_bundle": ("heax-hwax_risk", "risk_get_evidence_bundle", "bundle_id"),
}
# 이름은 알지만 열지 않은 kind — 도구 · 답 꼴을 근거로 옮기는 규칙을 확인하지 않았다(모듈 머리말).
NOT_OPENED = ("reportarchive_file", "dynaforge_report", "stepforge_job")
ID_MAX = 200
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,%d}" % ID_MAX)
REFS_MAX = 40                   # 한 근거 목록의 참조 수 — 펼친 뒤의 항목 수 상한은 엔진(_EVID_ITEMS)이 건다
RESOLVE_TIMEOUT_S = 30.0        # 참조 하나를 푸는 기한(초) — MCP 시작 응답 안에서 기다린다(게이트웨이 호출 한도 600초보다 한참 안쪽)
OMIT_SOURCE = "근거 참조 풀이"
_FP_RE = re.compile(r"[0-9a-f]{64}")


class RefError(ValueError):
    """참조의 꼴이 틀렸거나 열지 않은 kind 다 — 무엇이 틀렸는지 말한다(잡을 만들기 전에 거절한다)."""


def fingerprint(evidence) -> str:
    """묶음 지문(contracts §1) — 정규 JSON(키 차례 · 빈칸 없음 · 한글 그대로)의 sha256 hex."""
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def label(kind: str, rid: str) -> str:
    return f"ref:{kind}:{rid}"


def _ref_of(i: int, it) -> dict | None:
    """항목 하나가 참조면 {kind, id}, 아니면 None. 꼴이 틀리면 RefError."""
    if not isinstance(it, dict) or "ref" not in it:
        return None
    extra = sorted(set(it) - {"ref"})
    if extra:
        raise RefError(f"evidence[{i}] 는 참조(ref) 항목인데 다른 칸 {extra[:5]} 이 함께 있다 — 참조 항목은 {{ref: {{kind, id}}}} 하나만 둔다"
                       "(본문은 result 를 쓴 따로 된 항목으로)")
    ref = it["ref"]
    if not isinstance(ref, dict) or sorted(ref) != ["id", "kind"]:
        raise RefError(f"evidence[{i}].ref 는 {{kind, id}} 객체다 — 받은 꼴: "
                       + (f"칸 {sorted(ref)[:5]}" if isinstance(ref, dict) else type(ref).__name__))
    kind, rid = ref.get("kind"), ref.get("id")
    if not isinstance(kind, str) or kind not in KINDS:
        if kind in NOT_OPENED:
            raise RefError(f"evidence[{i}].ref.kind={kind} 는 아직 열지 않은 kind 다(kind not opened yet) — 도구 · 답 꼴을 근거로 옮기는 규칙을 "
                           f"확인한 kind 만 연다. 지금 받는 kind: {' · '.join(KINDS)}. 그 근거는 도구로 뽑아 result 에 넣는다")
        raise RefError(f"evidence[{i}].ref.kind 는 {' · '.join(KINDS)} 가운데 하나다 — 받은 값: {str(kind)[:40]!r}")
    if not isinstance(rid, str) or not _ID_RE.fullmatch(rid.strip()):
        raise RefError(f"evidence[{i}].ref.id 는 영문 · 숫자 · _.:- {ID_MAX}자 안의 글이다 — 받은 값: {str(rid)[:40]!r}")
    return {"kind": kind, "id": rid.strip()}


def parse(evidence) -> list | None:
    """근거 목록의 참조 자리 — [(자리, {kind, id} 또는 None)]. 참조가 하나도 없으면 None(종전 길 그대로). 꼴이 틀리면 RefError."""
    if not isinstance(evidence, list):
        return None
    slots = [(i, _ref_of(i, it)) for i, it in enumerate(evidence)]
    n = sum(1 for _i, r in slots if r is not None)
    if not n:
        return None
    if n > REFS_MAX:
        raise RefError(f"참조가 {n}건이다 — 한 번에 {REFS_MAX}건까지 받는다")
    return slots


def has_refs(evidence) -> bool:
    """참조가 든 근거 목록인가(꼴은 보지 않는다 — 보는 것은 parse)."""
    return isinstance(evidence, list) and any(isinstance(it, dict) and "ref" in it for it in evidence)


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
        return "게이트웨이에 그 도구가 없다(리스크 앱 판이 묶음 받기 전이거나 앱이 꺼졌다)"
    if "forbidden" in low or "403" in low or "권한" in t:
        return f"권한이 없다(게이트웨이가 막았다 — {t[:160]})"
    return f"도구가 오류로 답했다 — {t[:200]}"


def _check_bundle(rid: str, text: str) -> tuple[list | None, dict]:
    """risk_get_evidence_bundle 답 → (펼칠 evidence 또는 None, 기록 칸). contracts §1 '받기' 꼴 {bundle_id, target_key, version, fingerprint, evidence}."""
    try:
        body = json.loads(text)
    except ValueError:
        return None, {"why": "답을 JSON 으로 읽지 못했다"}
    if not isinstance(body, dict):
        return None, {"why": "답이 객체가 아니다"}
    if body.get("error"):
        # 리스크 앱의 MCP 도구는 예외 대신 {error: 코드, message} 로 답한다(없음 · 남의 과제는 같은 not found — 존재를 숨긴다)
        return None, {"why": f"리스크 앱이 거절했다({str(body.get('error'))[:40]} — {' '.join(str(body.get('message') or '').split())[:160]})"}
    ev, fp = body.get("evidence"), body.get("fingerprint")
    if not isinstance(ev, list) or not all(isinstance(x, dict) for x in ev) or not isinstance(fp, str) or not _FP_RE.fullmatch(fp):
        return None, {"why": "답 꼴이 다르다 — {bundle_id, fingerprint(sha256 hex), evidence[항목 …]} 가 아니다"}
    if str(body.get("bundle_id") or "") != rid:
        return None, {"why": f"다른 묶음이 왔다(요청 {rid} · 답 {str(body.get('bundle_id'))[:60]})"}
    got = fingerprint(ev)
    rec = {"items": len(ev), "chars": body_chars(ev), "fingerprint": got, "bundle_fingerprint": fp,
           **{k: body[k] for k in ("target_key", "version") if isinstance(body.get(k), (str, int)) and not isinstance(body.get(k), bool)}}
    if got != fp:
        return None, {**rec, "why": f"지문이 다르다 — 묶음이 적은 {fp[:12]}… · 펼친 근거를 다시 잰 {got[:12]}…(묶음을 싣지 않았다)"}
    return ev, rec


async def resolve(app, evidence: list, *, groups, user: str, user_pat: str = "", slots: list | None = None) -> dict:
    """참조를 풀어 펼친다 → {evidence(펼친 목록), refs[기록], omitted[evidence_omitted 줄]}. slots 는 parse 의 것(없으면 여기서 읽는다)."""
    slots = slots if slots is not None else parse(evidence)
    if slots is None:
        return {"evidence": list(evidence or []), "refs": [], "omitted": []}
    out, refs, omitted = [], [], []
    for i, ref in slots:
        if ref is None:
            out.append(evidence[i])
            continue
        kind, rid = ref["kind"], ref["id"]
        app_key, tool, arg = KINDS[kind]
        rec: dict = {"kind": kind, "id": rid, "ok": False, "at": round(time.time(), 3)}
        try:
            is_err, text = await asyncio.wait_for(
                call_alias(app, groups=groups, user=user, user_pat=user_pat, alias=f"{app_key.replace('-', '')}_{tool}",
                           args={arg: rid}), RESOLVE_TIMEOUT_S)
        except asyncio.TimeoutError:
            rec["why"] = f"{RESOLVE_TIMEOUT_S:g}초 안에 답하지 않았다(불통)"
        except Exception as exc:  # noqa: BLE001 — 하나를 못 풀어도 나머지는 푼다
            log.warning("근거 참조 %s 풀이 실패: %r", label(kind, rid), exc)
            rec["why"] = f"게이트웨이를 부르지 못했다({type(exc).__name__} — 불통)"
        else:
            if is_err:
                rec["why"] = _why_error(text)
            else:
                ev, got = _check_bundle(rid, text)
                rec.update(got)
                if ev is not None:
                    rec["ok"] = True
                    out.extend(ev)
        refs.append(rec)
        if not rec["ok"]:
            omitted.append({"source": OMIT_SOURCE, "text": f"{label(kind, rid)} — {rec['why']}"})
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
    return [{"ref": {"kind": r["kind"], "id": r["id"]}} for r in (prev_refs or [])
            if isinstance(r, dict) and r.get("ok") and r.get("kind") in KINDS and isinstance(r.get("id"), str)]


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

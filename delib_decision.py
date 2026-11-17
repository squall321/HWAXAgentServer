# 끝난 심의의 결정문을 구조화한다 — risk_spec(리스크 앱과 같은 규칙) · 표결 · 인용 수 · 만장일치 문장과 표결 대조(DL-03)
"""HWAXPortal docs/request-1117 contracts §3 (DL-03) — deliberate_result(view="decision_json").

답 {job_id, status, view, job, template, decision_json, consistency(, parse_error)}.

· decision_json — 의장 틀이 risk-review 계열(job risk-review · risk-review-sealed, 손잡이로 그 틀을 건 심의도)이면 결정문의 risk_spec 에 엔진이 아는
  표결 · 인용을 붙인 {risk_spec, votes, cites}. risk_spec 을 못 읽으면 decision_json 은 null 이고 까닭은 parse_error 다. 그 밖의 심의는 risk_spec 이
  null 인 같은 꼴이다.
· votes — 결과 사건이 원장에 남긴 그대로 {tally, unanimous, by_seat}(tally 칸은 엔진의 agree · conditional · oppose · abstain · responded · total —
  분모는 착석 수다). 결과 사건까지 가지 못한 잡은 null.
· cites — 결정문의 근거 표지 [e:N|KEY] · [e:N] 를 센 [{key, n}](표지에 KEY 가 없으면 key 는 'e:N'). 결정문 차례 그대로다.
· consistency — {consensus_claim, tally, match, note}. 결정문(코드 펜스 밖)에 만장일치 · 전원 합의를 말하는 문장이 있는데 표결이 만장일치가 아니면
  (반대 · 조건부 · 미표명 · 응답 못 한 좌석) match 거짓. 문장이 없거나 표결이 없으면 null — 견줄 것이 없다.

글을 짓지 않는다 — 원장에 있는 결정문 · 표결만 읽는다. 기본 deliberate_result 답(full · summary)은 그대로다.
"""
from __future__ import annotations

import json
import re

import deliberation as _engine   # 근거 키 꼴(_EVID_KEY_MAX)을 읽어 표지 정규식을 만든다 — 엔진과 같은 키 꼴

# ── parse_risk_spec — HWAXRisk backend/app/narrative.py 와 같은 규칙의 사본(리포를 import 하지 않는다 — contracts §3) ──────────────
# (1) 마지막 ```json 펜스 → (2) 없거나 깨지면 텍스트 끝에서 역방향 마지막 균형 중괄호 블록 → (3) dict 이고 schema == "risk_spec" 이 아니면 None.
# 펜스 정규식도 그쪽 그대로다(hwax-sim-deliberate.js parseSimSpec 과 같다). 규칙을 바꾸면 두 리포를 함께 고친다.
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```")


def _last_balanced_block(s: str) -> str | None:
    """텍스트 끝의 마지막 '}' 에서 역방향으로 중괄호를 세어 짝 '{' 까지의 블록을 돌려준다(중첩 JSON 방어)."""
    end = s.rfind("}")
    if end == -1:
        return None
    depth = 0
    for i in range(end, -1, -1):
        ch = s[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                return s[i:end + 1]
    return None


def parse_risk_spec(text) -> dict | None:
    """결정문 텍스트에서 risk_spec JSON 을 뽑는다. 실패는 None(예외 없음)."""
    s = "" if text is None else str(text)
    obj = None
    fences = _FENCE_RE.findall(s)
    if fences:
        try:
            obj = json.loads(fences[-1])
        except json.JSONDecodeError:
            obj = None  # 펜스 파싱 실패 → 균형 스캔 폴백
    if obj is None:
        block = _last_balanced_block(s)
        if block is None:
            return None
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict) or obj.get("schema") != "risk_spec":
        return None
    return obj


# ── 만장일치 · 전원 합의를 말하는 낱말 — 한 곳에 둔다(늘릴 때 여기만) ───────────────────────────────────────────────────────
# 표결과 견줄 '모두가 같은 편' 이라는 주장이다. '합의했다' 처럼 다수 합의일 수 있는 말은 넣지 않는다(조건부 표가 있어도 참일 수 있다).
CONSENSUS_PHRASES = (
    "전원 합의", "전원합의", "전원 동의", "전원 찬성", "전원 일치", "전원이 동의", "전원이 합의", "전원이 찬성",
    "만장일치", "만장 일치",
    "이견 없이", "이견이 없", "이견 없음", "이견은 없",
    "모두 동의", "모두가 동의", "모두 찬성", "모두가 찬성", "모든 좌석이 동의", "모든 좌석이 찬성", "모든 전문가가 동의",
    "반대 없이", "반대 의견 없이", "한목소리",
    "unanimous", "unanimously", "full consensus",
)
# 낱말 바로 뒤(이 글자 수 안)에 부정이 오면 주장이 아니다 — '전원 합의에 이르지 못했다' · '만장일치는 아니다' · '만장일치가 아닌' · '만장일치 아님' ·
# '전원 합의는 없다(없었다 · 없고 · 없음 · 없는)'. 낱말 바로 앞의 부정도 같다 — 'not unanimous' · 'no full consensus' · '비만장일치'(한글 앞붙이는
# 띄우지 않은 것만 — '준비 만장일치' 의 '비' 는 아니다). 부정 낱말은 이 두 정규식 한 곳이다(반대 검토 M4).
_NEG_WINDOW = 8
_NEG_RE = re.compile(r"아니|아닌|아님|않|못|없었|없다|없고|없음|없는|실패|not\b|n't\b", re.IGNORECASE)
_NEG_BEFORE_RE = re.compile(r"(?:\b(?:not|no|never|without)\s+(?:a\s+|an\s+)?|\bnon-?|비|非)$", re.IGNORECASE)
_PHRASE_RE = re.compile("|".join(re.escape(p) for p in sorted(CONSENSUS_PHRASES, key=len, reverse=True)), re.IGNORECASE)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_SENT_SPLIT_RE = re.compile(r"\n+|(?<=[.!?。])\s+")
_CLAIM_MAX = 300

# 근거 표지 — 엔진이 좌석 · 의장에 쓰라고 시키는 꼴 그대로([e:N] · [e:N|KEY], deliberation._EV_CITE_RE 와 같은 키 꼴 — 그쪽은 KEY 를 잡지 않는다).
CITE_RE = re.compile(rf"\[e:(\d+)(?:\|([A-Za-z0-9_.-]{{1,{_engine._EVID_KEY_MAX}}}))?\]")

RISK_TEMPLATE = "risk-review"


def decision_text(job: dict) -> str:
    """원장의 결정문 — full 의 decision 과 같은 글(decision 이 없으면 result_text)."""
    return str(job.get("decision") or job.get("result_text") or "")


def risk_family(job: dict) -> bool:
    """risk_spec 을 내는 심의인가 — 의장 틀이 risk-review(봉인 심사 risk-review-sealed 도 이 틀이다). 손잡이로 그 틀을 건 자유 심의도 같다."""
    return str(job.get("chair_template") or "").strip().lower().startswith(RISK_TEMPLATE)


def cites(text: str) -> list[dict]:
    """결정문의 근거 표지를 센다 — [{key, n}] 을 처음 나온 차례로. KEY 가 없는 표지는 key 'e:N' 이다(같은 항목을 [e:N] 과 [e:N|KEY] 로 섞어
    적으면 두 줄이다 — 엔진이 어느 쪽으로 적어도 같은 항목이라고 하지만 KEY 없는 표지만으로는 KEY 를 알 수 없다)."""
    counts: dict[str, int] = {}
    for m in CITE_RE.finditer(text or ""):
        key = m.group(2) or f"e:{m.group(1)}"
        counts[key] = counts.get(key, 0) + 1
    return [{"key": k, "n": n} for k, n in counts.items()]


def votes(job: dict) -> dict | None:
    """결과 사건의 표결 — {tally, unanimous, by_seat}. 원장에 집계가 없으면 None."""
    if not (isinstance(job.get("tally"), dict) and isinstance(job.get("votes"), list)):
        return None
    return {"tally": job["tally"], "unanimous": job.get("unanimous"), "by_seat": job["votes"]}


def consensus_claim(text: str) -> str | None:
    """결정문(코드 펜스 밖)에서 만장일치 · 전원 합의를 말하는 첫 문장(앞뒤 공백을 걷고 _CLAIM_MAX 자까지) — 없으면 None. 낱말 바로 뒤(_NEG_RE)나
    바로 앞(_NEG_BEFORE_RE)에 부정이 오면 세지 않는다(_NEG_WINDOW 자 안)."""
    prose = _CODE_FENCE_RE.sub(" ", text or "")
    for sent in _SENT_SPLIT_RE.split(prose):
        for m in _PHRASE_RE.finditer(sent):
            if _NEG_RE.search(sent[m.end():m.end() + _NEG_WINDOW]) or _NEG_BEFORE_RE.search(sent[max(0, m.start() - _NEG_WINDOW):m.start()]):
                continue
            s = " ".join(sent.split())
            return s if len(s) <= _CLAIM_MAX else s[:_CLAIM_MAX] + "…"
    return None


def _unanimous(v: dict) -> bool:
    """표결이 만장일치인가 — 엔진이 적은 unanimous 를 따르고, 없으면 같은 식(착석 전원 동의 · 미표명 0)으로 센다."""
    if isinstance(v.get("unanimous"), bool):
        return v["unanimous"]
    t = v.get("tally") or {}
    total = int(t.get("total") or 0)
    return total > 0 and int(t.get("agree") or 0) == total and int(t.get("abstain") or 0) == 0


def consistency(text: str, v: dict | None) -> dict:
    """만장일치 문장과 표결의 대조 — {consensus_claim, tally, match, note}."""
    claim = consensus_claim(text)
    tally = (v or {}).get("tally")
    if v is None:
        return {"consensus_claim": claim, "tally": None, "match": None,
                "note": "원장에 표결이 없다 — 결과 사건까지 가지 못했거나(체크포인트 · 오류 · 취소) 이 판 이전에 돈 잡이라 견줄 수 없다"}
    if claim is None:
        return {"consensus_claim": None, "tally": tally, "match": None,
                "note": "결정문에 만장일치 · 전원 합의를 말하는 문장이 없다 — 견줄 것이 없다"}
    if _unanimous(v):
        return {"consensus_claim": claim, "tally": tally, "match": True, "note": "결정문의 만장일치 문장과 표결(착석 전원 동의)이 맞는다"}
    t = tally or {}
    parts = [f"{label} {int(t.get(k) or 0)}" for k, label in (("oppose", "반대"), ("conditional", "조건부"), ("abstain", "미표명 · 응답 못 함"))
             if int(t.get(k) or 0)]
    return {"consensus_claim": claim, "tally": tally, "match": False,
            "note": "결정문은 만장일치 · 전원 합의를 말하는데 표결은 만장일치가 아니다(" + (" · ".join(parts) or "착석 전원 동의가 아니다")
                    + f" — 착석 {int(t.get('total') or 0)}석). 결정문의 그 문장을 그대로 옮기지 마라"}


def build(job: dict) -> dict:
    """deliberate_result(view="decision_json") 의 답."""
    st = job.get("status")
    text = decision_text(job)
    out = {"job_id": job.get("id"), "status": st, "view": "decision_json", "job": job.get("job") or job.get("kind"),
           "template": job.get("chair_template")}
    if not text:
        err = str(job.get("error") or "").strip()
        out.update(decision_json=None, consistency=None,
                   parse_error=(f"아직 끝나지 않았다(status {st})" if st in ("queued", "running") else
                                f"원장에 결정문이 없다(status {st}" + (f" — {err[:200]}" if err else "") + ")"))
        return out
    v = votes(job)
    body = {"risk_spec": None, "votes": v, "cites": cites(text)}
    if risk_family(job):
        spec = parse_risk_spec(text)
        if spec is None:
            out.update(decision_json=None, parse_error="결정문에서 risk_spec 을 읽지 못했다 — 마지막 ```json 펜스도, 끝의 마지막 균형 중괄호 "
                                                        "블록도 schema 가 'risk_spec' 인 JSON 객체가 아니다(리스크 앱 parse_risk_spec 과 같은 규칙)")
        else:
            body["risk_spec"] = spec
            out["decision_json"] = body
    else:
        out["decision_json"] = body
    out["consistency"] = consistency(text, v)
    return out

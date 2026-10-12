# 카드 대조 엔진 — 전문가 한 명이 자기 지식카드 전부를 검토 단위에 대조하는 단독 호출(예산·카드 정규화·파서·프롬프트·팩, 심의 엔진과 별개)
"""회의가 아니라 **대조** 다.

리스크 심사 재설계(HWAXPortal `docs/risk-review-redesign/`)의 카드 대조 엔진이다. 리스크 앱이 (전문가 × 단위 × 카드 묶음)
마다 이 모듈을 한 번 부르고, 모델은 실린 카드마다 판정 한 줄을 낸다. 인용·참조·수치는 코드가 대조한다.

원칙 넷(설계서 3.1).
1. 상태가 없다 — 팩·계획·결과를 서버가 기억하지 않는다. 앱이 팩을 얼려 두고 호출마다 그 카드를 그대로 되보낸다.
2. 요청 1건 = 논리 호출 1건. 순서·동시 수·이어 하기는 원장을 쥔 앱이 정한다.
3. 넘치기 전에 거절한다 — 창이 모자라면 다시 쪼개지 않고 `ctx_too_small` 로 멈춘다(D-22).
4. 구현은 하나, 정책은 앱 — 별칭 복원·인용 대조는 여기서 하고 등급은 앱이 정한다.

계획과 결정은 `docs/risk-review-redesign/M2.md`(4절이 계약 정본)·`reference/m2-wp3a.md`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from deliberation import _CHAIR_RESERVE, _EVID_KO_CPT, _PRE_SAFETY, _SUBSTANTIVE_RE, _first_dict, _norm_ws
from evidence import _NUM_TOK_RE, sig_numbers

# ── 판 번호 ──────────────────────────────────────────────────────────────────
API_REV = "cr-1"
KINDS = ("sweep", "verdict", "offcard")
# 예산 식의 판. 식을 바꾸면 손으로 올린다(limits_rev 에 들어간다).
LIMITS_FORMULA_REV = "cr-l1"

# ── 예산 손잡이(전부 선택 — 안 적으면 기본값) ─────────────────────────────────
K_DEFAULT = 8                     # CARD_REVIEW_K — verdict 한 호출의 카드 장수 상한(M0-b 로 다시 정한다)
SHARE_DEFAULT = {"verdict": 0.25, "sweep": 0.50, "offcard": 0.30}   # 종류별 몫 — 넘으면 거절이 아니라 soft_over
_SHARE_ENV = {"verdict": "CARD_REVIEW_SHARE_VERDICT", "sweep": "CARD_REVIEW_SHARE_SWEEP",
              "offcard": "CARD_REVIEW_SHARE_OFFCARD"}

# ── 예산 상수 ────────────────────────────────────────────────────────────────
# 틀(시스템 글·지시·고정 머리)의 **선언한 여유값**(자). 실제 틀 길이를 재지 않는다 — 재면 틀 한 글자가 단위 상한·
# limits_rev 를 흔들어 며칠짜리 실행의 열린 셀을 무른다(P-06). 시험이 '실제 틀 ≤ 여유값' 을 못박는다.
TEMPLATE_ALLOW = {"sweep": 2_400, "verdict": 3_200, "offcard": 3_600}
# 몫이 작은 창에서 P 의 바닥(틀 + 이만큼의 본문). 품질 몫과 창 한도 사이의 절벽을 없앤다(P-16).
BODY_MIN = {"sweep": 20_000, "verdict": 16_000, "offcard": 16_000}
# 프롬프트 몫(P) 가운데 각 블록에 떼어 두는 비율 — 고정 천장 대신 창에서 유도한다(PLAN 4절, C-17·C-32).
#   role 역할 원문 · digest 배경 카드 제목 · domains 영역 코드 · known 이미 낸 판정 · mech 메커니즘 목록
_BLOCK_SHARE = {
    "sweep": {"role": 0.10, "domains": 0.05},
    "verdict": {"role": 0.10, "digest": 0.05},
    "offcard": {"role": 0.10, "digest": 0.05, "known": 0.05, "mech": 0.05, "domains": 0.05},
}
# 보충 호출의 지적 블록 몫 — P 의 2%, 바닥 600자(좁은 창에서도 빠진 별칭 목록이 들어가게).
HINT_SHARE, HINT_FLOOR = 0.02, 600
VERDICT_UNIT_SHARE = 0.47         # verdict 본문 가운데 단위 몫(나머지는 카드 묶음)
SWEEP_PAGE_SHARE = 0.20           # sweep 본문 가운데 단위 목록 한 쪽의 몫
OUT_USE = 0.6                     # 출력 몫 가운데 행에 쓰는 비율(추론 토큰 여유)
ROW_CHARS = {"verdict": 520, "sweep": 150, "offcard": 600}   # 행 한 줄의 글자 어림
SWEEP_OUT_HEAD, OFFCARD_OUT_HEAD = 400, 1_400                # 출력의 고정 줄(k 여집합·m·more 줄 등) 어림
MANIFEST_ITEM_FLOOR = 800         # 목록 항목 상한의 바닥(쪽당 단위가 많아도 항목 하나는 이만큼 싣는다)
# 이 박스에서 그 종류를 돌릴 수 있다고 볼 본문 바닥(자). 단위·카드·목록 쪽이 이보다 작으면 ctx_too_small 이다 —
# 다시 쪼개지 않는다(D-22). 천장이 아니라 '이것도 안 들어가면 돌리지 않는다' 의 바닥이다.
RUN_FLOOR = 1_000
# 단위 크기 식의 수(두 리포가 식을 베끼지 않고 limits 로 받는다 — P-15). unit_size() 가 이 수를 쓴다.
UNIT_SIZE = {"per_line": 11, "base": 160}
MANIFEST_ITEM_OVERHEAD = 14       # 목록 한 줄의 별칭·괄호·구분자·«»·개행(별칭 4자까지)
_NEED_CTX_MAX = 1 << 30           # ctx_too_small 의 need_ctx_tokens 를 찾는 위 끝(토큰)


def _knob_k(env) -> int:
    raw = str(env.get("CARD_REVIEW_K") or "").strip()
    try:
        return max(1, int(raw)) if raw else K_DEFAULT
    except ValueError:
        print(f"[card_review] env CARD_REVIEW_K='{raw}' 정수 파싱 실패 — 기본값 {K_DEFAULT} 사용")
        return K_DEFAULT


def _knob_shares(env) -> dict:
    out = {}
    for kind, name in _SHARE_ENV.items():
        raw = str(env.get(name) or "").strip()
        v = SHARE_DEFAULT[kind]
        if raw:
            try:
                v = float(raw)
            except ValueError:
                print(f"[card_review] env {name}='{raw}' 숫자 파싱 실패 — 기본값 {v} 사용")
            if not 0 < v <= 1:
                print(f"[card_review] env {name}={raw} 는 (0, 1] 밖이다 — 기본값 {SHARE_DEFAULT[kind]} 사용")
                v = SHARE_DEFAULT[kind]
        out[kind] = v
    return out


def _budget(ctx: int, mt: int, k_knob: int, shares: dict) -> dict:
    """창(토큰)·출력 상한(토큰, 0=미설정)에서 종류별 예산(자)을 유도한다 — 순수. limits() 와 need_ctx 찾기가 같이 쓴다.

    본문(body)은 P 에 대해 줄지 않는 한 식으로 잡는다(블록 몫을 하나씩 내림하면 P 가 1 늘 때 본문이 1~2자 줄 수 있다 —
    창이 커지면 단위 상한이 줄지 않는다는 단조성이 깨진다)."""
    ctx = max(0, int(ctx))
    mt = max(0, int(mt or 0))
    out_t = max(mt, min(_CHAIR_RESERVE, ctx // 4))
    p_hard = max(0, int((ctx - out_t) * _EVID_KO_CPT * _PRE_SAFETY))
    outcap = mt or out_t
    rows_out = outcap * OUT_USE * _EVID_KO_CPT      # 출력 몫 가운데 행에 쓸 글자
    b: dict = {"ctx": ctx, "mt": mt, "out": out_t, "p_hard": p_hard, "outcap": outcap}
    for kind in KINDS:
        p_soft = int(ctx * _EVID_KO_CPT * shares[kind])
        p = min(p_hard, max(p_soft, TEMPLATE_ALLOW[kind] + BODY_MIN[kind]))
        frac = sum(_BLOCK_SHARE[kind].values())
        caps = {f"{blk}_max": int(p * s) for blk, s in _BLOCK_SHARE[kind].items()}
        hint = int(max(HINT_FLOOR, p * HINT_SHARE))
        body = max(0, int(min(p * (1 - frac) - HINT_FLOOR, p * (1 - frac - HINT_SHARE))) - TEMPLATE_ALLOW[kind])
        fixed = p - body if body else TEMPLATE_ALLOW[kind] + sum(caps.values()) + hint
        b[kind] = {"p": p, "p_soft": p_soft, "fixed": fixed, "body": body, **caps, "hint_max": hint}
    v, s, o = b["verdict"], b["sweep"], b["offcard"]
    v["unit_body_max"] = int(v["body"] * VERDICT_UNIT_SHARE)
    v["card_budget"] = v["body"] - v["unit_body_max"]
    v["k_out"] = int(rows_out // ROW_CHARS["verdict"])
    v["k"] = max(1, min(k_knob, v["k_out"]))
    s["manifest_page_max"] = int(s["body"] * SWEEP_PAGE_SHARE)
    s["card_budget"] = s["body"] - s["manifest_page_max"]
    s["units_per_page_max"] = max(1, int((rows_out - SWEEP_OUT_HEAD) // ROW_CHARS["sweep"]))
    s["manifest_item_max"] = max(MANIFEST_ITEM_FLOOR, s["manifest_page_max"] // s["units_per_page_max"])
    o["findings_max"] = max(1, int((rows_out - OFFCARD_OUT_HEAD) // ROW_CHARS["offcard"]))
    # 카드 밖 호출은 배경 카드를 제목으로 내릴 수 있어(bg_dropped) 본문 전부를 단위에 줄 수 있다.
    # 그래서 단위 상한은 verdict 몫과 offcard 본문 가운데 작은 쪽이다(P-02 덧붙임 — 단위를 싣는 종류마다 BODY ≥ 상한).
    o["unit_body_max"] = o["body"]
    b["unit_body_max"] = min(v["unit_body_max"], o["body"])
    o["card_budget"] = max(0, o["body"] - b["unit_body_max"])
    b["runnable"] = {
        "sweep": s["card_budget"] >= RUN_FLOOR and s["manifest_page_max"] >= s["manifest_item_max"],
        "verdict": v["unit_body_max"] >= RUN_FLOOR and v["card_budget"] >= RUN_FLOOR,
        "offcard": o["body"] >= RUN_FLOOR,
    }
    return b


def _need_ctx(ctx: int, mt: int, k_knob: int, shares: dict) -> int | None:
    """세 종류가 다 돌 수 있는 가장 작은 창(토큰). 예산은 창에 대해 줄지 않으므로 이분 탐색이다. 위 끝에서도 안 되면 None."""
    if not all(_budget(_NEED_CTX_MAX, mt, k_knob, shares)["runnable"].values()):
        return None
    lo, hi = max(1, ctx), _NEED_CTX_MAX
    while lo < hi:
        mid = (lo + hi) // 2
        if all(_budget(mid, mt, k_knob, shares)["runnable"].values()):
            hi = mid
        else:
            lo = mid + 1
    return lo


def limits(ctx_tokens: int, ctx_source: str, max_tokens: int | None, *, retrying: bool = False,
           env=os.environ, model: str = "") -> dict:
    """이 박스에서 한 호출에 무엇을 얼마나 실을 수 있나 — `GET /card-review/limits` 의 본문(관문 칸 `gate` 는 라우트가 더한다).

    순수 함수다 — 창은 부르는 쪽이 `app._model_context_state()` 로 물어 넘긴다(그 조회는 동기 httpx 라 라우트가
    asyncio.to_thread 로 돈다 — P-10). 낮은 고정 천장이 없다 — 역할·제목 목록·이미 낸 판정·메커니즘·영역 목록은
    P 의 몫, 행 수는 출력 몫에서 유도한다. 창이 모자라면 다시 쪼개지 않고 `ctx_too_small` 을 싣는다(D-22)."""
    k_knob, shares = _knob_k(env), _knob_shares(env)
    b = _budget(ctx_tokens, max_tokens or 0, k_knob, shares)
    v, s, o = b["verdict"], b["sweep"], b["offcard"]
    rev_src = {"ctx": b["ctx"], "out": b["out"], "mt": b["mt"], "k": k_knob, "share": shares,
               "template_allow": TEMPLATE_ALLOW, "body_min": BODY_MIN, "formula": LIMITS_FORMULA_REV,
               "coef": [_EVID_KO_CPT, _PRE_SAFETY, _CHAIR_RESERVE]}
    out = {
        "api_rev": API_REV, "kinds": list(KINDS), "model": model,
        "ctx_tokens": b["ctx"], "ctx_source": ctx_source, "ctx_retrying": bool(retrying),
        "llm_max_tokens": b["mt"] or None, "out_tokens": b["out"], "p_hard": b["p_hard"],
        "unit_body_max": b["unit_body_max"],
        "verdict": {k: v[k] for k in ("p", "fixed", "body", "role_max", "digest_max", "hint_max",
                                      "unit_body_max", "card_budget", "k", "k_out")},
        "sweep": {k: s[k] for k in ("p", "fixed", "body", "role_max", "domains_max", "hint_max",
                                    "manifest_page_max", "card_budget", "units_per_page_max", "manifest_item_max")},
        "offcard": {k: o[k] for k in ("p", "fixed", "body", "role_max", "digest_max", "known_max", "mech_max",
                                      "domains_max", "hint_max", "unit_body_max", "card_budget", "findings_max")},
        "runnable": dict(b["runnable"]),
        "unit_size": dict(UNIT_SIZE), "manifest_item": {"overhead": MANIFEST_ITEM_OVERHEAD},
        "limits_rev": hashlib.sha256(json.dumps(rev_src, sort_keys=True).encode()).hexdigest()[:8],
    }
    if not all(b["runnable"].values()):
        need = _need_ctx(b["ctx"], b["mt"], k_knob, shares)
        short = [k for k in KINDS if not b["runnable"][k]]
        out["ctx_too_small"] = {
            "need_ctx_tokens": need,
            "message": (f"이 박스의 창 {b['ctx']:,} 토큰으로는 카드 대조({'·'.join(short)})를 돌릴 수 없다 — 단위·카드를 다시 "
                        "쪼개지 않는다. " + (f"창이 {need:,} 토큰 이상인 LLM 이 필요하다(LLM_CONTEXT_TOKENS·VLLM_MODEL)."
                                          if need else "출력 상한(DELIB_MAX_TOKENS)이 창보다 크다 — 그 값을 줄여라.")),
        }
    return out


def _sha8(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:8]


def _pack_decks(kind: str, cards: list, budget: int, k: int | None, hard_room: int) -> tuple[list, list]:
    """카드를 팩의 고정 순서로 묶는다 — 장수(k, None 이면 없음)와 글자 합(budget)을 함께 보고 닫는다.

    한 장이 budget 보다 크면 혼자 한 묶음(soft_over)이고, 그 한 장이 hard_room(창 한도에서 틀·블록 몫·단위 상한을 뺀
    자리)마저 넘으면 묶지 않고 oversize 로 돌려준다(조용히 자르지 않는다)."""
    decks, over, cur, cur_chars = [], [], [], 0

    def close():
        nonlocal cur, cur_chars
        if cur:
            aliases = [c["alias"] for c in cur]
            decks.append({"deck_id": f"{kind[0]}-{_sha8(kind, [[c['alias'], c.get('record_id', '')] for c in cur])}",
                          "aliases": aliases, "chars": cur_chars, "soft_over": cur_chars > budget})
        cur, cur_chars = [], 0

    for c in cards:
        n = int(c.get("chars") or 0)
        if n > hard_room:
            over.append(c["alias"])
            continue
        if cur and ((k is not None and len(cur) >= k) or cur_chars + n > budget):
            close()
        cur.append(c)
        cur_chars += n
        if n > budget:          # 혼자 넘는 카드는 혼자 묶는다
            close()
    close()
    return decks, over


def plan(cards: list, units: list, lim: dict) -> dict:
    """카드 묶음과 단위 목록 쪽을 나눈다 — 순수. 입력은 팩의 카드 `[{alias, record_id, checkable, chars}]` 와 앱의 단위
    `[{unit_id, manifest_chars, body_chars}]`, 그리고 limits() 의 응답이다.

    건수 천장을 두지 않는다(verdict 의 K 만 장수로 닫는다). 창(P_hard)을 넘는 것만 oversize 이고, 몫을 넘지만 창 안이면
    soft_over 다. 같은 입력이면 deck_id 가 같다(묶음 별칭과 record_id 의 해시)."""
    v, s = lim["verdict"], lim["sweep"]
    p_hard, unit_max = lim["p_hard"], lim["unit_body_max"]
    # 묶음 한 장이 창 한도 안에 드는 자리 — 그 종류의 고정 몫과 단위(또는 목록 한 쪽)를 상한까지 실은 나머지다.
    v_room = p_hard - v["fixed"] - unit_max
    s_room = p_hard - s["fixed"] - s["manifest_page_max"]
    checkable = [c for c in cards if c.get("checkable")]
    verdict_decks, v_over = _pack_decks("verdict", checkable, v["card_budget"], v["k"], v_room)
    sweep_decks, s_over = _pack_decks("sweep", list(cards), s["card_budget"], None, s_room)
    kinds_of: dict = {}
    for kind, aliases in (("verdict", v_over), ("sweep", s_over)):
        for a in aliases:
            kinds_of.setdefault(a, []).append(kind)

    # 단위 — 단위 상한을 넘어도 창 안이면 soft_over, 상한 크기 묶음과 함께 창을 넘으면 oversize(다시 쪼개지 않는다).
    u_over, u_soft = [], []
    for u in units:
        n = int(u.get("body_chars") or 0)
        if v["fixed"] + n + v["card_budget"] > p_hard:
            u_over.append(u["unit_id"])
        elif n > unit_max:
            u_soft.append(u["unit_id"])

    # 목록 쪽 — 쪽당 단위 수와 글자 합으로 닫는다. 항목 하나가 쪽 몫을 넘으면 혼자 한 쪽, 카드 몫과 함께 창을 넘으면 oversize.
    pages, m_over, m_soft, cur, cur_chars = [], [], [], [], 0
    page_max, per_page = s["manifest_page_max"], s["units_per_page_max"]
    for u in units:
        n = int(u.get("manifest_chars") or 0)
        if s["fixed"] + n + s["card_budget"] > p_hard:
            m_over.append(u["unit_id"])
            continue
        if cur and (len(cur) >= per_page or cur_chars + n > page_max):
            pages.append(cur)
            cur, cur_chars = [], 0
        cur.append(u["unit_id"])
        cur_chars += n
        if n > page_max:
            m_soft.append(u["unit_id"])
            pages.append(cur)
            cur, cur_chars = [], 0
    if cur:
        pages.append(cur)

    return {
        "limits_rev": lim["limits_rev"],
        "verdict_decks": verdict_decks,
        "sweep_decks": sweep_decks,
        "manifest_pages": pages,
        "oversize": {"cards": sorted(kinds_of), "kinds": kinds_of, "units": u_over, "manifest": m_over},
        "soft_over": {"units": u_soft, "manifest": m_soft},
    }


# ── 카드 정규화·렌더(순수) ────────────────────────────────────────────────────
# 카드의 정규 글을 만드는 곳은 여기 하나다(C-11). 리스크 앱은 팩 응답을 통째로 얼려 같은 글로 인용을 다시 대조한다 —
# 두 리포가 글을 따로 만들면 맞는 인용이 불일치가 된다(QP2 벡터가 지킨다).
NORM_REV = "cr-n1"
CARD_DOC_TYPE = "expert_knowledge_card"
# ExpertAgents 의 일곱 종류 가운데 판정 대상(점검형)이 아닌 넷. 이 밖의 종류(모르는 것·빈 값)는 점검형으로 둔다 —
# 빼는 쪽으로 틀리지 않는다. open-observation(인과 미검증 관측, 코퍼스 0장)은 배경으로 둔다(M2.md 5절 17).
_BACKGROUND_TYPES = frozenset({"concept", "faq", "data", "open-observation"})
_TYPE_ORDER = ("design-rule", "standard-summary", "failure-case", "data", "concept", "faq", "open-observation")
_TAG_FIELDS = ("type", "confidence", "tier", "causal_status")


def _unquote_marks(s) -> str:
    """«·» 를 지운다 — 원천 글이 프롬프트의 인용 구간을 닫지 못하게(설계서 3.10)."""
    return str(s or "").replace("«", "").replace("»", "")


def _section_text(raw, sec_title: str) -> str:
    """절 본문의 정규 글 — CRLF→LF, 줄 끝 공백·앞뒤 빈 줄 떼기, 검색용 머리 줄 떼기, «» 지우기.

    머리 줄은 ExpertAgents 내보내기가 임베딩 맥락용으로 붙인 '카드 제목 — 절 제목' 줄이다(헤딩 없는 카드는 '카드 제목'
    한 줄). 인용 원문이 아니므로 뗀다 — 남기면 모델이 제목을 인용해도 통과한다."""
    lines = [ln.rstrip() for ln in str(raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    head = str(sec_title or "").strip()
    if lines and head and (lines[0].endswith(f"— {head}") or lines[0].strip() == head):
        lines.pop(0)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return _unquote_marks("\n".join(lines)).strip()


def _text_sha(sections: list) -> str:
    return hashlib.sha256("\n".join(f"{s['sid']}\t{s['title']}\n{s['text']}" for s in sections).encode()).hexdigest()


def normalize_card(rec) -> dict | None:
    """get_record 한 건 → 정규 카드(설계서 3.4 필드표 + card_id). 기록이 아니면(사전이 아니거나 id 가 없으면) None.

    값은 `content.card.*` 가 먼저이고 없으면 태그(`type:`·`confidence:`·`tier:`·`causal_status:`)다. 값이 없으면 "" 로 둔다 —
    `validated` 같은 기본값으로 채우지 않는다. 그릴 절이 하나도 없으면 sections 가 빈 카드를 돌려준다(None 이 아니다 —
    팩이 그 카드를 unrenderable 로 따로 싣는다. 조용히 빠지지 않게)."""
    if not isinstance(rec, dict) or not rec.get("id"):
        return None
    content = rec.get("content") if isinstance(rec.get("content"), dict) else {}
    meta = content.get("card") if isinstance(content.get("card"), dict) else {}
    tags: dict = {}
    for t in rec.get("tags") or []:
        k, sep, v = str(t).partition(":")
        if sep and k in _TAG_FIELDS and k not in tags:
            tags[k] = v.strip()

    def pick(field: str) -> str:
        v = meta.get(field)
        return _unquote_marks(v).strip() if v not in (None, "") else _unquote_marks(tags.get(field, "")).strip()

    sections = []
    for i, s in enumerate(content.get("sections") or []):
        s = _first_dict(s)
        raw_title = str(s.get("title") or "")
        raw = s.get("content_text") if s.get("content_text") is not None else s.get("text")
        text = _section_text(raw, raw_title)
        if text:
            sections.append({"sid": str(s.get("section_id") or s.get("sid") or i + 1),
                             "title": _unquote_marks(raw_title).strip(), "text": text})
    ctype = pick("type")
    refs = meta.get("standard_refs")
    srcs = meta.get("sources")
    return {
        "record_id": str(rec["id"]),
        "card_id": str(meta.get("card_id") or ""),
        "title": _unquote_marks(rec.get("title")).strip(),
        "type": ctype,
        "checkable": ctype not in _BACKGROUND_TYPES,
        "tier": pick("tier"),
        "confidence": pick("confidence"),
        "causal_status": pick("causal_status"),
        "review_status": _unquote_marks(meta.get("review_status")).strip(),
        "standard_refs": [_unquote_marks(r).strip() for r in refs] if isinstance(refs, list) else [],
        "sources_n": len(srcs) if isinstance(srcs, list) else 0,
        "updated": str(meta.get("updated") or ""),
        "upstream_checksum": str(meta.get("checksum") or ""),
        "sections": sections,
        "text_sha": _text_sha(sections),
    }


def card_text(card: dict) -> str:
    """인용 대조의 원문 — 절 본문을 줄바꿈으로 이은 것. 머리 줄(종류·등급·제목)은 인용 대상이 아니다."""
    return "\n".join(s["text"] for s in card.get("sections") or [])


def card_header(card: dict, alias: str) -> str:
    """카드 머리 줄 — 종류·등급·인과·검토·규격(빈 칸은 뺀다). 인용 대상이 아니지만 수치 원천에는 든다(P-17f)."""
    ctype = card.get("type") or "미상"
    bits = [f"종류 {ctype}({'점검' if card.get('checkable') else '배경'})"]
    for label, key in (("등급", "confidence"), ("인과", "causal_status"), ("검토", "review_status")):
        if card.get(key):
            bits.append(f"{label} {card[key]}")
    if card.get("standard_refs"):
        bits.append("규격 " + ", ".join(card["standard_refs"]))
    return f"[{alias}] " + " · ".join(bits)


def render_card(card: dict, alias: str) -> str:
    """프롬프트에 싣는 카드 한 장(끝 줄바꿈 포함 — 묶음 글자 수는 이 길이의 합이다)."""
    out = [card_header(card, alias), f"제목 «{card.get('title', '')}»"]
    for s in card.get("sections") or []:
        out.append(f"§{s['sid']} {s['title']}".rstrip())
        out.append(f"«{s['text']}»")
    return "\n".join(out) + "\n"


def alias_width(n: int) -> int:
    """별칭 숫자 자릿수 — 99장까지 두 자리, 그 위는 세 자리(설계서 3.4)."""
    return max(2, len(str(n)))


def assign_aliases(cards: list) -> list:
    """정규 카드를 팩의 고정 순서(종류 순 → card_id → record_id)로 세우고 K01… 별칭과 렌더 길이(chars)를 단다.
    같은 전문가의 모든 호출에서 같은 별칭이 같은 카드다."""
    order = {t: i for i, t in enumerate(_TYPE_ORDER)}
    ranked = sorted(cards, key=lambda c: (order.get(c.get("type"), len(order)), c.get("card_id") or "",
                                          c.get("record_id") or ""))
    w = alias_width(len(ranked))
    out = []
    for i, c in enumerate(ranked, 1):
        alias = f"K{i:0{w}d}"
        out.append({"alias": alias, **c, "chars": len(render_card(c, alias))})
    return out


def role_sha(role: str) -> str:
    return hashlib.sha256(str(role or "").encode()).hexdigest()


def pack_hash(agent_key: str, role_hash: str, cards: list) -> str:
    """팩의 내용 주소 — 전문가·역할·카드마다 렌더에 드는 칸 전부(별칭 순). 같은 입력이면 같다(QP2)."""
    rows = [[c.get("record_id"), c.get("card_id"), c.get("title"), c.get("type"), c.get("confidence"),
             c.get("causal_status"), c.get("review_status"), list(c.get("standard_refs") or []), c.get("text_sha")]
            for c in cards]
    return hashlib.sha256(json.dumps([agent_key, role_hash, rows], ensure_ascii=False).encode()).hexdigest()



# ── 요청 → 별칭 표(순수) ──────────────────────────────────────────────────────
# 모델은 카드를 K, 줄을 L, 단위를 U 번호로만 부른다. 이 표가 그 닫힌 집합이고, 프롬프트 렌더(build_prompt)와 출력 검사
# (check_rows)가 같은 표를 쓴다 — 렌더한 번호와 되돌리는 번호가 어긋날 길이 없다.
UNSUPPORTED_KINDS = ("noinput", "cross")             # 설계서의 다섯 종류 가운데 M2 에 없는 둘(M3·M4)
_UNIT_KINDS_UNSUPPORTED = ("snap_unit", "excluded")   # 현황 단위 틀은 M4(P-01), 제외 단위는 부를 일이 없다


class RequestError(ValueError):
    """요청이 계약에 맞지 않는다 — 스트림을 열기 전에 상태 코드(기본 422)와 code 로 거절한다."""

    def __init__(self, code: str, message: str, status: int = 422, **detail):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


def _alias(prefix: str, i: int, n: int) -> str:
    return f"{prefix}{i:0{alias_width(n)}d}"


def _str(v) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def build_ctx(req: dict, lim: dict) -> dict:
    """요청 하나 → 별칭 표(ctx). 계약에 안 맞으면 RequestError(bad_request·unsupported_kind·unsupported_unit_kind·
    empty_unit·deck_too_large). 카드는 팩의 별칭(K01…)을 그대로 쓰고, 줄은 변경 → 문맥 → 근거 순으로 L01… 을 잇는다."""
    if not isinstance(req, dict):
        raise RequestError("bad_request", "요청 본문이 객체가 아니다")
    kind = req.get("kind")
    if kind in UNSUPPORTED_KINDS:
        raise RequestError("unsupported_kind", f"호출 종류 '{kind}' 는 이 판(M2)에 없다 — sweep·verdict·offcard 만 받는다")
    if kind not in KINDS:
        raise RequestError("bad_request", f"모르는 호출 종류 '{kind}'")
    variant = req.get("variant")
    if kind == "verdict":
        if variant not in ("fwd", "rev", "solo"):
            raise RequestError("bad_request", "verdict 의 variant 는 fwd·rev·solo 가운데 하나다")
    elif variant not in (None, ""):
        raise RequestError("bad_request", f"{kind} 에는 variant 가 없다(null)")
    expert = req.get("expert") if isinstance(req.get("expert"), dict) else {}
    if not _str(expert.get("key")).strip():
        raise RequestError("bad_request", "expert.key 가 비었다")

    cards: dict = {}
    order: list = []
    for c in req.get("cards") or []:
        a = _str((c or {}).get("alias")) if isinstance(c, dict) else ""
        if not re.fullmatch(r"K\d{2,}", a) or not isinstance(c.get("sections"), list):
            raise RequestError("bad_request", "cards 의 항목은 팩 카드 그대로여야 한다(alias K01…·sections)")
        if a in cards:
            raise RequestError("bad_request", f"cards 에 같은 별칭 {a} 가 둘이다")
        cards[a] = {"record_id": _str(c.get("record_id")), "card_id": _str(c.get("card_id")),
                    "type": _str(c.get("type")), "title": _str(c.get("title")), "checkable": bool(c.get("checkable")),
                    "text": card_text(c), "sections": [(_str(x.get("sid")), _str(x.get("text")))
                                                       for x in c["sections"] if isinstance(x, dict)],
                    "header": card_header(c, a), "card": c}
        order.append(a)
    digest: dict = {}
    for d in req.get("digest") or []:
        a = _str((d or {}).get("alias")) if isinstance(d, dict) else ""
        if not re.fullmatch(r"K\d{2,}", a):
            raise RequestError("bad_request", "digest 의 항목은 {alias, type, title} 이다")
        if a not in cards:
            digest[a] = {"record_id": _str(d.get("record_id")), "card_id": _str(d.get("card_id")),
                         "type": _str(d.get("type")), "title": _str(d.get("title"))}

    unit = req.get("unit")
    manifest = req.get("manifest") or []
    lines: dict = {}
    units: dict = {}
    num_src = ""
    unit_id = ""
    if kind == "sweep":
        if unit not in (None, {}):
            raise RequestError("bad_request", "sweep 은 unit 이 아니라 manifest(단위 목록 한 쪽)를 받는다")
        if not isinstance(manifest, list) or not manifest:
            raise RequestError("bad_request", "sweep 의 manifest 가 비었다")
        for i, m in enumerate(manifest, 1):
            if not isinstance(m, dict) or not _str(m.get("unit_id")):
                raise RequestError("bad_request", "manifest 의 항목은 {unit_id, kind, title, text} 이다")
            units[_alias("U", i, len(manifest))] = {"unit_id": _str(m["unit_id"]), "kind": _str(m.get("kind")),
                                                    "title": _str(m.get("title")), "text": _str(m.get("text"))}
    else:
        if not isinstance(unit, dict):
            raise RequestError("bad_request", f"{kind} 에는 unit 이 있어야 한다")
        if _str(unit.get("kind")) in _UNIT_KINDS_UNSUPPORTED:
            raise RequestError("unsupported_unit_kind", f"단위 종류 '{unit.get('kind')}' 는 이 판에 틀이 없다(M4)")
        parts = []
        for part, key in (("line", "lines"), ("context", "context"), ("evidence", "evidence")):
            for x in unit.get(key) or []:
                if not isinstance(x, dict):
                    raise RequestError("bad_request", f"unit.{key} 의 항목은 {{ref, text}} 이다")
                parts.append((part, x))
        if not any(p in ("line", "context") for p, _ in parts):
            raise RequestError("empty_unit", "unit 에 변경 줄도 문맥 줄도 없다 — 대조할 것이 없다")
        for i, (part, x) in enumerate(parts, 1):
            lines[_alias("L", i, len(parts))] = {"ref": _str(x.get("ref")), "text": _str(x.get("text")), "part": part,
                                                 "owner_unit": _str(x.get("owner_unit"))}
        unit_id = _str(unit.get("unit_id"))
        num_src = "\n".join([_str(unit.get("title")), _str(unit.get("summary")), _str(unit.get("notes"))] +
                            [_str((m or {}).get("note")) for m in unit.get("missing") or [] if isinstance(m, dict)])

    if kind == "verdict":
        if not order:
            raise RequestError("bad_request", "verdict 에 판정할 카드가 없다")
        k = int(lim["verdict"]["k"])
        if variant == "solo" and len(order) != 1:
            raise RequestError("deck_too_large", "solo 는 카드 1장이다", have=len(order), k=1)
        if len(order) > k:
            raise RequestError("deck_too_large", f"verdict 한 호출의 카드는 {k}장까지다(이 박스의 K)",
                               have=len(order), k=k)
    extra = req.get("extra") if isinstance(req.get("extra"), dict) else {}

    def codes(items, key):
        return {_str(x.get(key)) for x in items or [] if isinstance(x, dict) and _str(x.get(key))}

    return {
        "kind": kind, "variant": variant or None, "expert": expert, "unit_id": unit_id,
        "cards": cards, "card_order": order, "digest": digest, "judge": list(order) if kind == "verdict" else [],
        "lines": lines, "units": units, "num_src": num_src,
        "mechs": codes(extra.get("mechanisms"), "code"), "domains": codes(extra.get("domains"), "key"),
        "findings_max": int(lim["offcard"]["findings_max"]), "bg_dropped": 0,
    }


# ── 출력 파서(순수) ──────────────────────────────────────────────────────────
# 한 줄에 객체 하나(JSON Lines)가 정본이다 — 긴 배열 하나는 따옴표 하나로 통째로 깨지지만, 줄 단위면 깨진 줄 하나만 잃고
# 그 행만 다시 물으면 된다. `_parse_json`(마지막 객체 하나)을 쓰지 않는다 — 30행 배열이 한 행으로 보인다(설계서 2.2).
_ROW_TYPES = {"sweep": ("u", "h", "inj"), "verdict": ("v", "c", "inj"),
              "offcard": ("f", "m", "h", "need", "none", "more", "inj")}
# (종류, 줄 종류) → 목록 칸. 나머지 칸은 문자열이다.
_LIST_FIELDS = {("verdict", "v"): ("l",), ("verdict", "c"): ("l",), ("sweep", "u"): ("k",),
                ("offcard", "f"): ("l",), ("offcard", "m"): ("seen",), ("offcard", "h"): ("l",)}
_STR_FIELDS = {
    ("verdict", "v"): ("k", "applies", "judgement", "cq", "why", "need", "sev", "path", "check_kind", "check"),
    ("verdict", "c"): ("cond", "met", "note"),
    ("sweep", "u"): ("u", "rel", "why"), ("sweep", "h"): ("u", "to", "why"),
    ("offcard", "f"): ("dir", "title", "mech", "mech_free", "path", "src", "q", "sev", "conf", "check_kind", "check"),
    ("offcard", "h"): ("to", "why"), ("offcard", "need"): ("what",), ("offcard", "none"): ("why",),
    ("offcard", "more"): ("more",),
}
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_FENCE_RE = re.compile(r"^\s*```")


def _loads_line(t: str):
    """한 줄을 객체로 — 배열 괄호가 붙은 줄([{…},·{…}])과 꼬리 쉼표를 받아 준다. 못 읽으면 None."""
    if t.startswith("[{"):
        t = t[1:]
    t = t.rstrip()
    if t.endswith("}]"):
        t = t[:-1]
    for cand in (t, t.rstrip(",").rstrip()):
        try:
            return json.loads(cand)
        except ValueError:
            continue
    return None


def _scan_objects(text: str) -> list:
    """raw_decode 로 훑어 객체를 차례로 꺼낸다 — 여러 줄로 펼친 객체·한 줄에 붙은 배열을 받는다(P-17c)."""
    dec, out, i = json.JSONDecoder(), [], 0
    while i < len(text):
        j = text.find("{", i)
        if j < 0:
            break
        try:
            o, end = dec.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(o, dict):
            out.append(o)
        i = end
    return out


def _coerce(kind: str, row: dict) -> dict:
    """칸 모양을 맞춘다 — 목록 자리에 문자열 하나가 오면 한 항목 목록으로 읽고(글자로 쪼개지 않는다), 다른 모양은 빈 값으로
    읽되 원래 값을 `_shape` 에 남긴다."""
    t = row["t"]
    out, shape = {"t": t}, {}
    for f in _LIST_FIELDS.get((kind, t), ()):
        v = row.get(f)
        if v is None:
            out[f] = []
        elif isinstance(v, str):
            out[f] = [v] if v.strip() else []
        elif isinstance(v, list):
            out[f] = [x for x in v if isinstance(x, str) and x.strip()]
            if len(out[f]) != len(v):
                shape[f] = v
        else:
            out[f], shape[f] = [], v
    for f in _STR_FIELDS.get((kind, t), ()):
        v = row.get(f)
        if v is None or isinstance(v, str):
            out[f] = (v or "").strip()
        else:
            out[f], shape[f] = "", v
    if t == "inj":
        out["at"], out["text"] = _str(row.get("at")).strip(), _str(row.get("text")).strip()[:200]
    if shape:
        out["_shape"] = shape
    return out


def parse_rows(text, kind: str) -> tuple[list, dict]:
    """모델 출력 → (행 목록, 통계). 통계는 {bad_lines, tail_broken, foreign, shape} 다.

    `<think>…</think>` 를 먼저 뗀다(추론 속 초안 줄이 행으로 읽힌다). 전체가 배열이거나 `{"rows":[…]}` 면 그 원소를 쓰고,
    아니면 줄마다 읽는다. 줄로 못 읽은 것이 있으면 나머지 글을 raw_decode 로 한 번 더 훑는다. 그 종류에 없는 줄은 버리고
    센다(foreign). `t` 가 없는데 k·applies 가 있으면 v 로 본다."""
    s = _THINK_RE.sub("", str(text or ""))
    if "</think>" in s:                 # 여는 꼬리표가 빠진 추론 — 그 앞은 초안이다
        s = s.rsplit("</think>", 1)[1]
    if "<think>" in s:                  # 닫지 못한 추론(절단) — 그 뒤는 초안이다
        s = s.split("<think>", 1)[0]
    lines = [ln for ln in s.replace("\r\n", "\n").split("\n") if not _FENCE_RE.match(ln)]
    body = "\n".join(lines).strip()
    stats = {"bad_lines": 0, "tail_broken": False, "foreign": 0, "shape": "lines"}
    objs = None
    if body[:1] in ("[", "{"):
        try:
            whole = json.loads(body)
        except ValueError:
            whole = None
        if isinstance(whole, list):
            objs, stats["shape"] = whole, "array"
        elif isinstance(whole, dict) and isinstance(whole.get("rows"), list):
            objs, stats["shape"] = whole["rows"], "rows"
        elif isinstance(whole, dict):
            objs = [whole]
    if objs is None:
        objs, good, bad = [], set(), []
        for i, ln in enumerate(lines):
            t = ln.strip()
            if not (t.startswith("{") or t.startswith("[{")):
                continue
            o = _loads_line(t)
            if isinstance(o, dict):
                objs.append(o)
                good.add(i)
            else:
                bad.append(i)
        if bad or (not objs and "{" in body):
            rest = _scan_objects("\n".join(ln for i, ln in enumerate(lines) if i not in good))
            if rest:
                objs.extend(rest)
                stats["shape"] = "scan"
            stats["bad_lines"] = max(0, len(bad) - len(rest))
            last = max((i for i, ln in enumerate(lines) if ln.strip()), default=-1)
            stats["tail_broken"] = last in bad and stats["bad_lines"] > 0
    rows = []
    for o in objs:
        if not isinstance(o, dict):
            stats["foreign"] += 1
            continue
        t = o.get("t")
        if t is None and kind == "verdict" and "k" in o and "applies" in o:
            t = "v"
        if t not in _ROW_TYPES[kind]:
            stats["foreign"] += 1
            continue
        rows.append(_coerce(kind, {**o, "t": t}))
    return rows, stats


# ── 검사(순수) ───────────────────────────────────────────────────────────────
APPLIES = ("yes", "no", "unknown")
JUDGEMENTS = ("OK", "WARNING", "FAIL", "undetermined", "na")
RELS = ("yes", "maybe", "no")
SEVS = ("경미", "중대", "치명")
CHECK_KINDS = ("tool", "sim", "test", "field")
DIRS = ("risk", "improvement")
CONFS = ("high", "medium", "low")
QUOTE_MIN, QUOTE_SUBST_MIN, NEED_SUBST_MIN = 15, 10, 6
_CURLY = str.maketrans({"“": '"', "”": '"', "„": '"', "‟": '"',
                        "‘": "'", "’": "'", "‚": "'", "‛": "'"})
# 검사 결과 가운데 '문제' 로 세는 것(행 하나의 문제 수 — 같은 별칭이 두 번이면 적은 쪽을 남긴다).
_OK = {"quote": ("ok", ""), "refs": ("ok", ""), "need": ("ok", ""), "why": ("ok", ""), "src": ("ok", "exp", ""),
       "to": ("ok", "")}


def _nq(s) -> str:
    """인용 대조의 정규화 — 공백 정규화(_norm_ws)에 더해 강조 기호(*·`)와 인용 구간 기호(«»)를 지우고 굽은 따옴표를
    곧은 따옴표로. 카드 본문에 **…** 가 많아 모델이 기호를 빼고 옮기면 정확 일치가 깨진다. 양쪽에 같은 함수를 건다."""
    s = str(s or "").translate(_CURLY)
    s = re.sub(r"[*`«»]", "", s)
    return _norm_ws(s)


def _unescape(s: str) -> str:
    """모델이 이스케이프를 한 겹 더 적은 인용(\\n·\\"·\\t)을 풀어 본다."""
    return s.replace("\\\\", "\x00").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\x00", "\\")


def _subst(s: str) -> int:
    return len(_SUBSTANTIVE_RE.findall(s or ""))


def quote_check(q: str, card: dict) -> tuple[str, str]:
    """인용 q 가 그 카드 본문에 그대로 있나 — (ok·short·not_found·empty, 인용이 걸린 절 번호). 절 경계를 넘으면 절 번호 "".

    기준은 15자 이상·실질 문자(숫자·영문·한글) 10자 이상이다(엔진 _quote_validator 와 같은 기준). 이스케이프를 푼 것도 본다."""
    if not _str(q).strip():
        return "empty", ""
    full = _nq(card.get("text", ""))
    status = "short"
    for cand in dict.fromkeys((q, _unescape(q))):
        n = _nq(cand).strip("\"' ")           # 인용을 따옴표로 감싸 옮긴 것은 감싼 따옴표를 떼고 본다
        if len(n) < QUOTE_MIN or _subst(n) < QUOTE_SUBST_MIN:
            continue
        if n in full:
            sid = next((sid for sid, text in card.get("sections") or [] if n in _nq(text)), "")
            return "ok", sid
        status = "not_found"
    return status, ""


def num_tokens(text: str) -> set:
    """글의 수치 토큰 집합(쉼표 제거) — 수치는 부분문자열이 아니라 토큰으로 대조한다(5 는 0.15 에 걸리지 않는다 — Q4)."""
    return {m.group(0).replace(",", "") for m in _NUM_TOK_RE.finditer(text or "")}


def nums_unsourced(text: str, sources: str) -> list:
    """text 의 유의 수치(evidence.sig_numbers) 가운데 sources 의 수치 토큰에 없는 것(원문에 없는 수치 — 계산값일 수 있다)."""
    have = num_tokens(sources)
    seen, out = set(), []
    for raw, norm in sig_numbers(text or ""):
        if norm not in seen:
            seen.add(norm)
            if norm not in have:
                out.append(raw)
    return out


def row_issues(row: dict) -> int:
    """행 하나의 문제 수 — checks 에서 센다(같은 별칭이 두 번이면 적은 쪽을 남기는 기준)."""
    ch = row.get("checks") or {}
    n = sum(1 for k, oks in _OK.items() if k in ch and ch[k] not in oks)
    n += len(ch.get("nums_unsourced") or []) + len(ch.get("coerced") or []) + len(ch.get("shape") or {})
    return n


def _enum(row: dict, f: str, allowed, fallback: str, checks: dict, *, empty_too: bool = False) -> None:
    """열거 밖 값은 불확실한 쪽(fallback)으로 고치고 coerced·enum 에 적는다(enum 은 보충을 일으킨다). 빈 값은 empty_too 일
    때만 고친다(그 밖에는 그 칸이 필요한지를 따로 본다)."""
    v = row.get(f, "")
    if (v or empty_too) and v not in allowed:
        checks["coerced"].append(f"{f}:{v}")
        checks.setdefault("enum", []).append(f)
        row[f] = fallback


def _resolve_l(row: dict, ctx: dict, checks: dict) -> list:
    """l 별칭 → 참조. 실리지 않은 별칭은 지우고 표지한다(다른 단위의 참조는 이 표에 없다 — Q8)."""
    keep = [a for a in row.get("l") or [] if a in ctx["lines"]]
    if len(keep) != len(row.get("l") or []):
        checks["refs"] = "unknown_alias"
    row["l"] = list(dict.fromkeys(keep))
    row["refs"] = [ctx["lines"][a]["ref"] for a in row["l"]]
    return row["l"]


def _check_v(row: dict, ctx: dict, attempt: int) -> dict:
    """verdict 의 v 줄 하나."""
    a = row["k"]
    card = ctx["cards"][a]
    checks: dict = {"quote": "ok", "quote_sid": "", "refs": "ok", "nums_unsourced": [], "need": "ok", "coerced": []}
    coerced = checks["coerced"]
    _enum(row, "applies", APPLIES, "unknown", checks, empty_too=True)
    _enum(row, "judgement", JUDGEMENTS, "undetermined", checks)
    ap, jd = row["applies"], row["judgement"]
    # 정합 — 어긋나면 불확실한 쪽으로(보충을 일으키지 않는다).
    if ap == "no" and jd != "na":
        row["applies"], row["judgement"] = "unknown", "undetermined"
        coerced.append(f"consistency:no+{jd or '-'}")
    elif ap == "unknown" and jd != "undetermined":
        row["judgement"] = "undetermined"
        coerced.append(f"consistency:unknown+{jd or '-'}")
    elif ap == "yes" and jd not in ("OK", "WARNING", "FAIL", "undetermined"):
        row["judgement"] = "undetermined"
        coerced.append(f"consistency:yes+{jd or '-'}")
    if ctx.get("variant") == "solo":
        _enum(row, "sev", SEVS, "", checks)
        _enum(row, "check_kind", CHECK_KINDS, "", checks)
    checks["quote"], checks["quote_sid"] = quote_check(row["cq"], card)
    l = _resolve_l(row, ctx, checks)
    if row["applies"] == "yes":
        if not l:
            checks["refs"] = "missing"
        elif checks["refs"] == "ok" and all(ctx["lines"][x]["part"] == "evidence" for x in l):
            checks["refs"] = "evidence_only"
    src = "\n".join([card["header"], card["title"], card["text"], ctx["num_src"]] +
                    [ctx["lines"][x]["text"] for x in l])
    checks["nums_unsourced"] = nums_unsourced(row["why"], src)
    if (row["applies"] == "unknown" or row["judgement"] == "undetermined") and _subst(row["need"]) < NEED_SUBST_MIN:
        checks["need"] = "missing"
    if "_shape" in row:
        checks["shape"] = row.pop("_shape")
    return {**row, "record_id": card["record_id"], "card_id": card["card_id"], "from_attempt": attempt,
            "checks": checks}


def _check_c(row: dict, ctx: dict, attempt: int) -> dict:
    """solo 의 조건 줄(t:c) — 그 한 장의 카드에 속한다."""
    a = ctx["judge"][0]
    checks: dict = {"refs": "ok", "coerced": []}
    _enum(row, "met", APPLIES, "unknown", checks, empty_too=True)
    _resolve_l(row, ctx, checks)
    if "_shape" in row:
        checks["shape"] = row.pop("_shape")
    return {**row, "k": a, "record_id": ctx["cards"][a]["record_id"], "from_attempt": attempt, "checks": checks}


def _check_u(row: dict, ctx: dict, attempt: int) -> dict:
    """sweep 의 u 줄 하나 — 단위 하나의 선별."""
    u = row["u"]
    checks: dict = {"why": "ok", "coerced": []}
    _enum(row, "rel", RELS, "maybe", checks, empty_too=True)
    ks = [x for x in row["k"] if x in ctx["cards"]]
    if len(ks) != len(row["k"]):
        checks["coerced"].append("k:unknown_alias")
    row["k"] = list(dict.fromkeys(ks))
    if row["rel"] == "no" and row["k"]:
        row["rel"] = "maybe"                   # 걸리는 카드를 댔으면 무관이 아니다 — 포함이 이긴다
        checks["coerced"].append("rel:no+k")
    if row["rel"] == "no" and _subst(row["why"]) < NEED_SUBST_MIN:
        checks["why"] = "short"
    if "_shape" in row:
        checks["shape"] = row.pop("_shape")
    return {**row, "unit_id": ctx["units"][u]["unit_id"],
            "record_ids": [ctx["cards"][x]["record_id"] for x in row["k"]], "from_attempt": attempt, "checks": checks}


def _check_f(row: dict, ctx: dict, attempt: int) -> dict:
    """offcard 의 f 줄 하나 — 카드 밖 리스크·개선."""
    checks: dict = {"refs": "ok", "src": "exp", "quote": "ok", "quote_sid": "", "coerced": []}
    _enum(row, "dir", DIRS, "risk", checks, empty_too=True)
    _enum(row, "sev", SEVS, "", checks)
    _enum(row, "conf", CONFS, "low", checks)
    _enum(row, "check_kind", CHECK_KINDS, "", checks)
    if row["mech"] and row["mech"] != "free" and row["mech"] not in ctx["mechs"]:
        checks["coerced"].append(f"mech:{row['mech']}")     # 목록 밖 메커니즘은 free 로 — 원래 값은 mech_free 에
        row["mech_free"] = row["mech_free"] or row["mech"]
        row["mech"] = "free"
    if not _resolve_l(row, ctx, checks):
        checks["refs"] = "missing"
    src = row["src"] or "exp"
    row["src"] = src
    if src == "exp":
        row["basis"] = "경험칙"
        checks["quote"] = "ok" if not row["q"] else "unexpected"
    elif src in ctx["cards"]:
        checks["src"] = "ok"
        row["basis"] = "카드"
        row["record_id"] = ctx["cards"][src]["record_id"]
        checks["quote"], checks["quote_sid"] = quote_check(row["q"], ctx["cards"][src])
    elif src in ctx["digest"]:
        checks["src"], checks["quote"] = "digest_only", "unchecked"   # 제목만 실린 카드 — 대조할 본문이 없다
        row["basis"] = "카드 제목"
        row["record_id"] = ctx["digest"][src]["record_id"]
    else:
        checks["src"], checks["quote"] = "unknown_alias", "unchecked"   # M2 에는 긴 문서(D)가 없다
        row["basis"] = "경험칙"
    if "_shape" in row:
        checks["shape"] = row.pop("_shape")
    return {**row, "from_attempt": attempt, "checks": checks}


def _check_side(kind: str, row: dict, ctx: dict, attempt: int) -> dict | None:
    """곁가지 줄(h·need·none·more·m) — 버릴 것이면 None."""
    t = row["t"]
    checks: dict = {"coerced": []}
    if t == "h":
        if kind == "sweep":
            if row["u"] not in ctx["units"]:
                return None
            row["unit_id"] = ctx["units"][row["u"]]["unit_id"]
        else:
            checks["refs"] = "ok"
            _resolve_l(row, ctx, checks)
        checks["to"] = "ok" if row["to"] in ctx["domains"] else "unknown"
    elif t == "m":
        keep = [c for c in row["seen"] if c in ctx["mechs"]]
        if len(keep) != len(row["seen"]):
            checks["coerced"].append("seen:unknown_code")
        row["seen"] = list(dict.fromkeys(keep))
    elif t == "more":
        _enum(row, "more", ("yes", "no"), "", checks, empty_too=True)
    if "_shape" in row:
        checks["shape"] = row.pop("_shape")
    return {**row, "from_attempt": attempt, "checks": checks}


def _dedupe(rows: list, key, verdict_of) -> tuple[list, int]:
    """같은 열쇠의 행이 둘이면 문제가 적은 쪽을 남기고, 판정이 갈렸으면 dup_conflict 를 단다."""
    kept: dict = {}
    order: list = []
    dups = 0
    for r in rows:
        k = key(r)
        if k not in kept:
            kept[k] = r
            order.append(k)
            continue
        dups += 1
        old = kept[k]
        win, lose = (r, old) if row_issues(r) < row_issues(old) else (old, r)
        if verdict_of(win) != verdict_of(lose):
            win["checks"]["dup_conflict"] = True
        kept[k] = win
    return [kept[k] for k in order], dups


def _verdict_of(r: dict):
    return (r.get("applies"), r.get("judgement")) if r["t"] == "v" else (r.get("rel"),) if r["t"] == "u" else None


def check_rows(kind: str, variant, rows: list, ctx: dict, *, attempt: int = 1) -> dict:
    """parse_rows 가 읽은 행 → 별칭을 되돌리고 행마다 checks 를 싣는다. 실리지 않은 별칭의 행은 버린다(Q5).

    돌려주는 것 — {rows, extras{inj}, stats{dropped_alias, dropped_over_cap, dup}} 에 settle() 의 셈을 더한 것.
    variant 는 ctx 의 것과 같아야 한다(별칭 표를 만든 요청의 것) — 인자로 받는 것은 부르는 쪽이 종류·변형을 눈으로 맞추게."""
    if kind != ctx["kind"] or (variant or None) != ctx.get("variant"):
        raise ValueError(f"({kind}, {variant!r}) 가 별칭 표({ctx['kind']}, {ctx.get('variant')!r})와 다르다")
    out, extra_inj = [], []
    stats = {"dropped_alias": 0, "dropped_over_cap": 0, "dup": 0}
    for raw in rows:
        r = dict(raw)
        t = r["t"]
        if t == "inj":
            extra_inj.append({"at": r.get("at", ""), "text": r.get("text", "")})
            continue
        got = None
        if kind == "verdict" and t == "v":
            if r["k"] in ctx["judge"]:
                got = _check_v(r, ctx, attempt)
        elif kind == "verdict" and t == "c":
            if ctx.get("variant") == "solo":
                got = _check_c(r, ctx, attempt)
        elif kind == "sweep" and t == "u":
            if r["u"] in ctx["units"]:
                got = _check_u(r, ctx, attempt)
        elif kind == "offcard" and t == "f":
            got = _check_f(r, ctx, attempt)
        else:
            got = _check_side(kind, r, ctx, attempt)
        if got is None:
            stats["dropped_alias"] += 1          # 실리지 않은 별칭 — 지어낸 번호가 결과에 들어갈 길이 없다
            continue
        out.append(got)
    if kind == "verdict":
        vs, stats["dup"] = _dedupe([r for r in out if r["t"] == "v"], lambda r: r["k"], _verdict_of)
        out = vs + [r for r in out if r["t"] != "v"]
    elif kind == "sweep":
        us, stats["dup"] = _dedupe([r for r in out if r["t"] == "u"], lambda r: r["u"], _verdict_of)
        out = us + [r for r in out if r["t"] != "u"]
    else:
        fs, stats["dup"] = _dedupe([r for r in out if r["t"] == "f"], lambda r: _nq(r["title"]) or id(r),
                                   lambda r: None)
        cap = ctx["findings_max"]
        stats["dropped_over_cap"] = max(0, len(fs) - cap)       # 상한을 넘는 f 줄은 버리고 센다(설계서 3.5.3 ⑦)
        side = []
        for t in ("m", "more", "none"):                        # 한 줄이어야 하는 것 — 마지막 것을 쓴다
            side += [r for r in out if r["t"] == t][-1:]
        out = fs[:cap] + side + [r for r in out if r["t"] in ("h", "need")]
    res = {"rows": out, "extras": {"inj": extra_inj}, "stats": stats}
    res.update(settle(kind, out, ctx))
    return res


def settle(kind: str, rows: list, ctx: dict) -> dict:
    """검사한 행 묶음의 셈 — {missing, problems, repair, expected, ok, flagged, stamp, unmatched_cards, maybe_more}.
    보충으로 합친 뒤(merge_rows) 다시 부른다. 문제는 남은 행의 checks 에서만 모은다(버린 행·중복에서 진 행은 지적하지 않는다).

    보충을 일으키는 문제는 빠진 행·열거 밖 값·applies∈{yes,unknown} 행(과 카드를 댄 카드 밖 줄)의 인용 문제뿐이다 —
    applies=no 행의 인용 불일치·참조 누락·수치는 표지만 남긴다(problems.md WP3a 빼는 것 5)."""
    problems: list = []
    for r in rows:
        ch = r.get("checks") or {}
        who = r.get("k") or r.get("u") or r.get("title") or r["t"]
        problems += [(who, "enum", f) for f in ch.get("enum") or []]
        if r["t"] == "v" and r.get("applies") in ("yes", "unknown") and ch.get("quote") != "ok":
            problems.append((r["k"], "quote", ch["quote"]))
        if r["t"] == "f" and ch.get("src") == "ok" and ch.get("quote") != "ok":
            problems.append((r["src"], "quote", ch["quote"]))
    res: dict = {"stamp": False, "unmatched_cards": [], "maybe_more": None}
    if kind == "verdict":
        main = [r for r in rows if r["t"] == "v"]
        have = {r["k"] for r in main}
        missing = [a for a in ctx["judge"] if a not in have]
        expected = len(ctx["judge"])
    elif kind == "sweep":
        main = [r for r in rows if r["t"] == "u"]
        have = {r["u"] for r in main}
        missing = [u for u in ctx["units"] if u not in have]
        expected = len(ctx["units"])
        used = {a for r in main for a in r["k"]}
        # 어느 단위의 k 에도 없는 카드는 서버가 여집합으로 낸다(k0 줄을 받지 않는다 — problems.md WP3a 빼는 것 4).
        res["unmatched_cards"] = [a for a in ctx["card_order"] if a not in used]
        whys = {_nq(r["why"]) for r in main}
        res["stamp"] = bool(main) and not missing and all(r["rel"] == "no" for r in main) and len(whys) <= 2
    else:
        main = [r for r in rows if r["t"] == "f"]
        ts = {r["t"] for r in rows}
        missing = [t for t, ok in (("m", "m" in ts), ("more", "more" in ts),
                                   ("f_or_none", bool(main) or "none" in ts)) if not ok]
        expected = 3                                   # 꼭 있어야 하는 줄 — m · more · (f 또는 none)
        more = next((r["more"] for r in rows if r["t"] == "more"), None)
        if len(main) >= ctx["findings_max"] or more == "yes":
            res["maybe_more"] = True                   # 상한까지 냈거나 더 있다고 했다 — 더 물을 수 있다(P-18)
        elif more == "no":
            res["maybe_more"] = False                  # more 줄이 없거나 읽히지 않으면 None(모름)이다
    problems = [("", "missing", m) for m in missing] + problems
    flagged = sum(1 for r in main if row_issues(r))
    res.update({"missing": missing, "problems": problems, "repair": bool(problems),
                "expected": expected, "ok": len(main) - flagged, "flagged": flagged})
    return res


def merge_rows(kind: str, old: list, new: list, attempt: int) -> list:
    """보충 시도의 행을 앞 행에 합친다 — 같은 열쇠면 문제가 적은 쪽을 남기고(같으면 앞 것), 남긴 판정이 앞 시도와
    다르면 `flipped_from` 을 싣는다(인용만 고치라고 했는데 FAIL 이 OK 로 바뀌는 것을 앱이 본다 — P-08). 새 행은
    `from_attempt=attempt` 다(check_rows 가 단다)."""
    def key(r):
        if r["t"] == "v":
            return ("v", r["k"])
        if r["t"] == "u":
            return ("u", r["u"])
        if r["t"] == "f":
            return ("f", _nq(r.get("title")))
        if r["t"] in ("m", "more", "none"):
            return (r["t"],)
        if r["t"] == "c":
            return ("c", _nq(r.get("cond")))
        return (r["t"], json.dumps({k: v for k, v in r.items() if k not in ("from_attempt", "checks")},
                                   ensure_ascii=False, sort_keys=True))

    merged = {key(r): r for r in old}
    order = [key(r) for r in old]
    for r in new:
        k = key(r)
        if k not in merged:
            merged[k] = r
            order.append(k)
            continue
        prev = merged[k]
        single = r["t"] in ("m", "more", "none")
        if single or row_issues(r) < row_issues(prev):
            if _verdict_of(r) != _verdict_of(prev):
                r = {**r, "flipped_from": {"verdict": _verdict_of(prev), "attempt": prev.get("from_attempt")}}
            merged[k] = r
    if kind == "offcard" and any(t == ("none",) for t in order) and any(t[0] == "f" for t in order):
        order = [t for t in order if t != ("none",)]   # 보충에서 f 가 왔으면 '없음' 줄은 낡았다
    return [merged[k] for k in order]


_MISSING_SAY = {"m": "m 줄(따져 본 메커니즘)", "more": "more 줄(더 있나)", "f_or_none": "f 줄(없으면 none 줄)"}
_HINT_SAY = {
    "missing": "줄이 없다",
    "enum": "열거 밖 값을 적었다",
    "quote:not_found": "인용(cq·q)이 그 카드 본문에 없다 — 그 카드의 «…» 안에서 15자 이상을 그대로 복사하라",
    "quote:short": "인용이 짧다 — 15자 이상(숫자·영문·한글 10자 이상)을 그대로 복사하라",
    "quote:empty": "인용이 비었다 — 판정의 근거가 된 카드 문구를 그대로 복사하라",
}


def repair_hint(problems: list, attempt: int, total: int, *, bad_lines: int = 0, max_chars: int = 0) -> str:
    """보충 호출에 붙이는 지적 블록. 문제를 종류별로 묶어 **대상 별칭을 전부** 적는다('외 n건' 으로 숨기지 않는다 —
    어느 줄을 다시 낼지 모르면 보충이 헛돈다). 시도 번호가 문구에 들어가 매번 달라진다(temperature 0 의 같은 실패 회피).
    max_chars 를 주면(limits 의 hint_max) 넘는 꼬리를 원문 길이 표식과 함께 끊는다."""
    groups: dict = {}
    for who, code, what in problems:
        if code == "missing":
            groups.setdefault("missing", []).append(_MISSING_SAY.get(str(what), str(what)))
        elif code == "enum":
            groups.setdefault("enum", []).append(f"{who}({what})")
        elif code == "quote":
            groups.setdefault(f"quote:{what}", []).append(str(who))
    head = (f"(보충 {attempt}/{total} — 직전 출력에 아래 문제가 있었다. 문제가 있는 줄만 다시 내라. "
            "이미 받은 줄은 다시 내지 않는다.)")
    lines = [head]
    for code in ("missing", "enum", "quote:not_found", "quote:short", "quote:empty"):
        if groups.get(code):
            lines.append(f"- {_HINT_SAY[code]} — {', '.join(dict.fromkeys(groups[code]))}")
    if bad_lines:
        lines.append(f"- 읽히지 않은 줄 {bad_lines}개 — 한 줄에 JSON 객체 하나로 다시 내라.")
    out = "\n".join(lines)
    if max_chars and len(out) > max_chars:
        from deliberation import _cut_mark  # noqa: PLC0415 — 거의 안 걸리는 길
        out = _cut_mark(out, max(1, max_chars - 40))
    return out

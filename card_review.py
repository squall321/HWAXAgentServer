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

from deliberation import _CHAIR_RESERVE, _EVID_KO_CPT, _PRE_SAFETY, _first_dict

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

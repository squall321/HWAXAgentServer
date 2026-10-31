# Thinking 모드 — 질문 하나를 전문가 풀에 돌려 답할 수 있는 좌석만 각자 답하게 하고, 못 하는 좌석은 넘길 곳을 지목하게 한다(회의 없음, 심의 엔진 미사용)
"""회의가 아니라 **분진(分診)** 이다.

심의(`deliberation.py`)는 좌석들을 한자리에 앉혀 라운드로 수렴시킨다. 이 모듈은 그러지
않는다. 좌석들은 서로를 읽지 않고, 표결하지 않으며, 의장 결정문도 없다. 각자 자기
근거를 보고 "내가 답할 수 있나" 를 판정한 뒤 답하거나 넘긴다.

자격 판정을 **단순 벡터 유사도로 하지 않는다.** e5 코사인은 무관한 문장에도 0.87~0.90 을
내므로(`docs/gotchas.md`) 그것만 보면 아무나 뽑힌다. 실제로 'OCA 의 산소 확산 계수' 질의에
백플레인 TFT·안테나 OTA 가 추천된 적이 있고, 랭킹 버그가 아니라 풀에 그 전문성이 아예
없던 것이었다. 그래서 네 신호를 쓴다 — 어휘 포함률(`desc_match`, 벡터 독립), 그 좌석이
실제로 소유한 근거 수(`matched_sections`), 그 좌석의 바인딩 문서 위에서 돌린 검색
(`agent_search`), 그리고 **좌석 자신의 자기판정**. 앞 셋은 예심이고 마지막이 본심이다.

설계 근거와 대안 검토는 `HWAXPortal/docs/thinking-mode/`(PLAN·checklist·context-notes).
"""
from __future__ import annotations

import asyncio
import json
import os

import call_trace
import external_llm
import vision
from deliberation import (
    _KN_CONC,
    _room,
    _wide,
    _agent_search_hits,
    _call,
    _cut_mark,
    _fit_recent,
    _first_dict,
    _llm_text,
    _parse_json,
    _persona_shadow,
    _restore_role,
    _sse,
    _tools_by_name,
    fit_knowledge_lines,
    is_operator,
    knowledge_line,
)

# ── 손잡이 ────────────────────────────────────────────────────────────────────
# 홉당 소집 후보 수. recommend_agents 의 top_k 다. 늘리면 예심 비용(agent_search 병렬)만
# 선형으로 늘고 본심 비용은 예심 통과 수에 달린다.
CANDIDATES = int(os.environ.get("THINK_CANDIDATES", "10"))
# 답변을 받을 좌석 상한. 본심 LLM 콜 수의 실질 상한이라 지연을 정한다.
MAX_ANSWERS = int(os.environ.get("THINK_MAX_ANSWERS", "5"))
# 위임 홉. 0 이면 기권 좌석의 지목을 따라가지 않는다.
HOPS = int(os.environ.get("THINK_HOPS", "1"))
# 예심 어휘 바닥값. recommend_svc 의 low_confidence 경계(0.40)보다 훨씬 낮게 둔다 —
# 여기서 자르는 것은 '답할 수 없다' 가 아니라 '본심에 보낼 값어치가 없다' 이기 때문이다.
DESC_FLOOR = float(os.environ.get("THINK_DESC_FLOOR", "0.15"))
# 동시 LLM 호출 상한. 심의 엔진에는 이 제한이 없어 좌석 수만큼 무제한 팬아웃한다.
CONCURRENCY = int(os.environ.get("THINK_CONCURRENCY", "4"))
# 좌석당 지식카드 주입 문자 예산 — 좁은 창의 값이다. 넓은 창(1백만 토큰)은 심의 좌석과 같은 창 몫에서 유도한다
# (_knowledge_budget — 약 19,000자, top_k 10건이 다 든다). 명시하면(THINK_KNOWLEDGE_BUDGET) 넓은 창에서는 낮추기만 한다.
KNOWLEDGE_BUDGET = int(os.environ.get("THINK_KNOWLEDGE_BUDGET", "2500"))
_KNOWLEDGE_BUDGET_SET = bool(os.environ.get("THINK_KNOWLEDGE_BUDGET"))


def _knowledge_budget() -> int:
    if not _wide():
        return KNOWLEDGE_BUDGET
    got = _room("knowledge", KNOWLEDGE_BUDGET)
    return min(KNOWLEDGE_BUDGET, got) if _KNOWLEDGE_BUDGET_SET else got
# 좌석 1콜 상한(초). 넘으면 그 좌석만 빠지고 나머지는 진행한다.
# 180 → 600 (2026-10-08). 심의와 같은 공유 LLM 을 쓴다 — 20석 넘는 심의가 LLM 을 차지한 동안에는 띵킹 좌석
# 호출이 큐에서 180초를 넘겨 전 좌석이 '초과' 로 빠졌다.
# 600 → 1800 (2026-10-18, 사용자 결정 — 지연보다 내용). 좌석마다 세션 맥락(앞 대화 · 조회 근거)을 싣게 되어 프롬프트가 커졌고, GLM 은
# 호출마다 평균 1.3만 토큰을 생각한다(요청서 v1.1 P6 — 평균 114초, 꼬리 16분). 심의 좌석 한도(DELIB_TIMEOUT_S)와 같다.
SEAT_TIMEOUT_S = float(os.environ.get("THINK_SEAT_TIMEOUT_S", "1800"))
# 세션 맥락 — 앞 대화([c:N] 사람 · [a:N] 챗)와 그때 도구가 조회한 근거([e:N])를 좌석 본심 프롬프트에 싣는다(사용자 결정 10-18 —
# "thinking 을 켜면 전문가 자문 모드이고 각자 맥락을 다 가지고 와야 한다", HWAXPortal docs/chat-depth-context). 0 이면 종전(질문만).
WITH_HISTORY = os.environ.get("THINK_WITH_HISTORY", "1").strip() != "0"
# 세션 맥락 몫 — 문서 예산(_doc_budget_tokens)의 비율. 문서를 붙였으면 문서 몫(THINK_DOC_SHARE)과 나눠 쓴다(합 0.9 — 나머지는 역할 ·
# 지식카드 · 지시). 안 붙였으면 거의 다 쓴다.
SESSION_SHARE = float(os.environ.get("THINK_SESSION_SHARE", "0.4"))
SESSION_SHARE_SOLO = float(os.environ.get("THINK_SESSION_SHARE_SOLO", "0.85"))
_SESSION_CONV_MIN = 0.35    # 세션 몫 가운데 대화가 적어도 가져가는 비율 — 근거가 적으면 대화가 그만큼 더 쓴다
# 좌석 본심 LLM — base(VLLM_* = GLM, 기본) | chat(챗 전용 LLM — Qwen, 넘김 포함). 사용자 결정 10-18: GLM 유지, 바꿀 손잡이만 둔다.
THINK_LLM = (os.environ.get("THINK_LLM") or "base").strip().lower()
# 위임 명사구 상한(좌석당).
REFER_MAX = 3

# 검색 모드. 2026-09-09 에 AIDataHub 를 고치기 전에는 hybrid 가 102초라 semantic 을 박아
# 두었다(fts 는 221초). 원인 둘을 고친 뒤 hybrid 0.5초 · fts 0.3초가 됐고, 결과도 좋아졌다
# — 범위를 파이썬이 아니라 SQL 로 걸게 되면서 hybrid 히트가 3건에서 6건으로 늘었다
# (전에는 전역 상위를 뽑고 나서 좌석으로 걸러 FTS 절반이 아무것도 기여하지 못했다).
# 그래도 느릴 수 있다고 가정한다 — _agent_search_hits 가 타임아웃·semantic 폴백을 맡는다.
_SEARCH_MODE = os.environ.get("THINK_SEARCH_MODE", "hybrid")
# 예심은 좌석 수만큼 병렬로 도는 단계라 개별 상한을 짧게 잡는다(심의 기본값보다 짧다).
SCREEN_TIMEOUT_S = float(os.environ.get("THINK_SCREEN_TIMEOUT_S", "12"))

# 화면·산출물의 이름은 **영문 "Thinking"** 이다(한글 음차 "띵킹" 은 쓰지 않는다).
# 다만 슬래시 트리거는 이미 쓰던 사람이 있으므로 "/띵킹" 을 계속 받는다.
_TRIGGERS = ("/띵킹", "/생각", "/thinking", "/think")


def is_thinking(message: str) -> bool:
    """슬래시 트리거 판정. 상시 모드는 ChatRequest.thinking 필드로 켠다(접두사는 첫 발화에만 붙는다)."""
    return str(message or "").strip().lower().startswith(_TRIGGERS)


def strip_thinking_trigger(message: str) -> str:
    s = str(message or "").strip()
    for t in _TRIGGERS:
        if s.lower().startswith(t):
            return s[len(t):].strip() or s
    return s


def _think(kind: str, **kw) -> bytes:
    """구조화 이벤트는 이름을 늘리지 않고 하나(`think`) 안에 kind 로 접는다(delib 선례)."""
    return _sse("think", {"kind": kind, **kw})


def _dom(key: str) -> str:
    k = str(key or "")
    return k.split("-", 1)[0] if "-" in k else k


# 지식카드 한 줄 포맷 — 챗·심의와 **같은 함수**를 쓴다. 세 곳이 각자 만들다 셋 다
# record_id 를 빠뜨렸고, 여기만 마지막까지 남아 있었다(docstring 은 "같은 규격" 이라
# 적어 놓고 실제로는 갈라져 있었다 — 선언이 틀린 자리가 가장 늦게 발견된다).
_hit_line = knowledge_line


def _as_dict(raw) -> dict:
    """MCP 응답(문자열 또는 객체)을 dict 로. 실패하면 빈 dict."""
    if isinstance(raw, dict):
        return raw
    d = _parse_json(raw if isinstance(raw, str) else json.dumps(raw, default=str))
    return d if isinstance(d, dict) else {}


# ── (0) 소집 ──────────────────────────────────────────────────────────────────
async def _summon(tools: dict, q: str, top_k: int, exclude: set) -> list[dict]:
    """recommend_agents 로 후보를 모은다. score 를 임계값으로 쓰지 않고 순위로만 쓴다.

    `_discover`(deliberation) 를 재사용하지 않는 이유 — 그 함수는 key/role/origin 만 남기고
    `desc_match`·`matched_sections` 를 버린다. 이 모드는 그 둘이 예심의 재료다.
    """
    try:
        recd = _as_dict(await _call(tools, "recommend_agents", {"q": q, "top_k": top_k}))
    except Exception as exc:  # noqa: BLE001 — 소집 실패는 빈 목록(호출부가 그 사실을 말한다)
        print(f"[thinking] recommend_agents 실패: {exc!r}")
        return []
    items = recd.get("agents") or recd.get("recommendations") or recd.get("data") or []
    out: list[dict] = []
    for it in (items if isinstance(items, list) else []):
        it = _first_dict(it)
        key = str(it.get("agent_type") or it.get("id") or "").strip()
        # 도구 운영자(HE팀)는 자기 앱으로 답하는 역할이라 '답할 수 있는 전문가' 소집 대상이 아니다.
        if not key or key in exclude or is_operator(key) or any(s["key"] == key for s in out):
            continue
        out.append({
            "key": key,
            "name": it.get("name") or key,
            "domain": _dom(key),
            "score": it.get("score"),
            # low_confidence 는 좌석별이 아니라 질의별 플래그다(recommend_svc.py:263-265 —
            # best_match 하나를 top_k 전원에게 박는다). 좌석 판정에는 원값 desc_match 를 쓴다.
            "desc_match": float(it.get("desc_match") or 0.0),
            "sections": int(it.get("matched_sections") or 0),
            "records": int(it.get("matched_records") or 0),
            "why": str(it.get("why") or "")[:300],
        })
    return out


# ── (1) 예심 ──────────────────────────────────────────────────────────────────
async def _screen_one(tools: dict, seat: dict, q: str) -> dict:
    """그 좌석의 **실제 바인딩 문서** 위에서 검색해 근거를 확보하고 예심 통과 여부를 정한다."""
    hits, note = await _agent_search_hits(tools, seat["key"], q, mode=_SEARCH_MODE,
                                          timeout_s=SCREEN_TIMEOUT_S)
    if note:
        print(f"[thinking] 예심 검색 강등({seat['key']}): {note}")
    scores = [h.get("score") for h in hits if isinstance(h, dict) and h.get("score") is not None]
    top = max(scores) if scores else None

    # 탈락은 AND 다. 한쪽만으로 자르지 않는다 —
    #  · hits>0 이고 desc_match≈0  : 어휘는 안 겹치는데 내용이 걸린 좌석(현장 용어 vs 표준 용어)
    #  · hits==0 이고 desc_match 높음: 데이터는 없는데 역할이 정확히 그것인 좌석(신규 도메인)
    # 둘 다 값이 있는 판정이라 살린다. 자르는 것은 근거도 어휘도 없을 때뿐이다.
    n = len(hits)
    passed = bool(n) or seat["desc_match"] >= DESC_FLOOR
    if note and not n:
        # 검색이 실패한 좌석을 '근거 없음' 으로 자르지 않는다 — 못 물어본 것과 없는 것은 다르다.
        passed = True
    reason = ("근거 %d건" % n) if n else (
        f"보유 근거 0건 · 어휘 일치 {seat['desc_match']:.2f}"
        + (f" · 검색 강등({note})" if note else "")
        + ("" if passed else " — 근거도 어휘도 없음"))

    # 심의 좌석과 같은 함수로 담는다 — 긴 카드는 같은 몫으로 앞부분만, 몫이 바닥보다 작아지면 순위가 낮은 쪽을 뺀다.
    # 종전엔 줄마다 700자에서 끊고 처음 넘는 줄에서 멈춰, 절 전문이 된 히트로 좁은 창에서 10건 중 3~4건만 실었다.
    _kb = _knowledge_budget()
    lines, dropped, trimmed = fit_knowledge_lines(hits, _kb)
    if dropped or trimmed:
        # 뺀 수·줄인 수를 예심 사유에 적는다 — 종전엔 '근거 10건' 이라 적고 4건만 실어, 좌석이 본 것과 화면이 달랐다.
        reason += (f" · 지식카드 {len(lines) + dropped}건 중 {len(lines)}건 실음("
                   + (f"{trimmed}건은 앞부분만 · " if trimmed else "") + f"예산 {_kb:,}자)")
    return {**seat, "hits": n, "top": top, "search_note": note,
            "passed": passed, "reason": reason, "knowledge": "\n".join(lines)}


# ── (2) 본심 ──────────────────────────────────────────────────────────────────
_JUDGE_SYS = (
    "당신은 사내 전문가 한 명입니다. 회의가 아니라 개별 자문이라 다른 전문가의 의견을 보지 못합니다. "
    "자기 도메인 밖을 아는 척하지 마세요. 유효한 JSON 객체 하나만 출력합니다. "
    # 언어를 못 박는다 — dev 모델(Qwen2.5-7B)이 중국어로 새는 것을 실측했다. 특히 refer 가
    # 중국어로 나오면 그 명사구로 재소집한 좌석이 엉뚱한 도메인이 된다(위임 사슬이 통째로 망가진다).
    "모든 값은 **한국어**로 씁니다. 기술 용어의 영문 원어 병기는 괜찮지만 중국어·일본어는 쓰지 않습니다."
)


def _judge_prompt(seat: dict, question: str, docs: str = "", session: str = "", topic: str = "") -> str:
    """좌석 본심 프롬프트. 세션 맥락(session)은 맨 앞에 둔다 — 좌석마다 같은 글이라 앞에 두면 서버가 같은 머리를 다시 쓸 수 있고
    (앞부분 캐시), '이 질문 앞에 무엇이 오갔나' 를 읽은 뒤 자기 역할 · 지식카드 · 질문으로 간다."""
    kn = seat.get("knowledge") or ""
    ev = (f"[당신의 지식카드 — 이 질문 관련 발췌]\n{kn}\n"
          if kn else
          "[당신의 지식카드]\n이 질문에 걸리는 보유 지식이 없습니다.\n")
    return (
        (f"{session}\n\n" if session else "")
        + f"[당신]\n{seat['key']} — {seat.get('role') or seat.get('name') or ''}\n\n"
        f"{ev}\n"
        f"[질문]\n{question}\n"
        + (f"[대화의 화두 — 앞 대화에서 뽑은 물음]\n{topic}\n" if topic else "")
        + (f"{docs}\n" if docs else "")
        + "\n"
        "먼저 스스로 판정하세요. **이 질문이 당신 도메인의 질문이고, 위 발췌"
        + (" · 세션 맥락" if session else "") + "이나 당신의 전문 지식으로 "
        "실질적인 답을 줄 수 있습니까?** 어휘가 익숙하다는 것과 답할 수 있다는 것은 다릅니다. "
        "인접 도메인이라 겉핥기만 가능하다면 그것은 답할 수 없는 것입니다.\n\n"
        + ("세션 맥락이 있으니 그 위에서 판단하세요 — 앞 대화에서 이미 조회된 값은 다시 지어내지 말고 [e:N] 으로 인용하고, "
           "당신의 지식카드와 다르면 다르다고 말하세요. 챗의 답([a:N])은 그때의 잠정 해석이지 결론이 아닙니다.\n"
           if session else "")
        + "답할 수 있으면 verdict=\"answer\" 로, 답을 본문에 쓰세요. 발췌에 없는 수치를 지어내지 말고, "
        "당신의 일반 전문 지식으로 말하는 부분은 (경험칙) 이라고 표기하세요.\n"
        "답할 수 없으면 verdict=\"pass\" 로, **답을 쓰지 말고** 왜 당신 소관이 아닌지를 밝히세요.\n"
        "refer 는 **이 질문을 다룰 분야**를 적는 자리입니다. 당신 전문 분야의 인접 주제를 적지 마세요 — "
        "예를 들어 안테나 전문가가 접착제 질문을 받았다면 'EM 해석'\u00b7'방사 성능' 은 틀린 답이고, "
        "'고분자 재료 물성'\u00b7'투습\u00b7투기도 측정' 처럼 **질문 쪽** 분야를 적어야 합니다. "
        "어디로 보내야 할지 모르겠으면 refer 를 **빈 배열**로 두세요. 억지로 채우면 엉뚱한 전문가가 불려옵니다.\n\n"
        "출력 형식(JSON 하나):\n"
        '{"verdict":"answer"|"pass",'
        ' "scope":"당신이 이 질문에서 맡을 수 있는 범위 한 문장",'
        ' "answer":"verdict=answer 일 때만. 6~15문장.",'
        ' "basis":["인용한 발췌의 제목 또는 세션 표지([e:N] · [c:N] · [a:N])", "..."],'
        f' "refer":["verdict=pass 일 때만. 봐야 할 분야를 **한국어** 8단어 이내 명사구로 최대 {REFER_MAX}개"]}}'
    )


async def _judge_one(llm, seat: dict, question: str, sem: asyncio.Semaphore,
                     docs: str = "", session: str = "", topic: str = "", images: list | None = None) -> dict:
    """좌석 1명의 자기판정 + (가능하면) 답변. 실패·시간초과는 pass 가 아니라 error 로 남긴다."""
    call_trace.tag(seat=seat.get("key"))      # 호출 추적 — 좌석마다 제 태스크(ensure_future)라 그 좌석의 호출에만 붙는다
    async with sem:
        try:
            # images — 좌석 모델이 그림을 받을 때만 온다(_session_images). 그림이 글 앞이라 좌석들의 앞부분(시스템 글 + 그림)이 같다.
            # 그림이 없으면 인자 셋 — 호출 모양이 종전과 같다(시험의 가짜 _llm_text 들).
            raw = await asyncio.wait_for(
                _llm_text(llm, _JUDGE_SYS, _judge_prompt(seat, question, docs, session, topic), *([images] if images else [])),
                SEAT_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {**seat, "verdict": "error", "scope": "", "answer": "",
                    # 어느 값을 올리면 되는지 같이 적는다 — 띵킹에는 설정 이름을 따로 싣는 자리가 없다.
                    "basis": [], "refer": [], "error": f"{SEAT_TIMEOUT_S:.0f}초 초과(THINK_SEAT_TIMEOUT_S)"}
        except Exception as exc:  # noqa: BLE001 — 한 좌석의 실패가 나머지를 죽이지 않게
            return {**seat, "verdict": "error", "scope": "", "answer": "",
                    "basis": [], "refer": [], "error": repr(exc)[:160]}
    o = _first_dict(_parse_json(raw)) or {}
    verdict = str(o.get("verdict") or "").strip().lower()
    answer = str(o.get("answer") or "").strip()
    # 자기판정을 못 낸 응답을 pass 로 접지 않는다 — 정당한 기권과 형식 실패는 다른 사실이고,
    # 섞으면 "아무도 답 못 한다" 는 이 모드의 핵심 출력이 오염된다.
    if verdict not in ("answer", "pass"):
        # 형식은 못 지켰지만 본문이 있으면 **버리지 않고** 답으로 강등 보존한다. JSON 을
        # 못 낸 것과 답을 못 한 것은 다르고, 여기서 버리면 실제 답이 통째로 사라진다
        # (심의 _persona_round 의 '구조화 실패 — 원문 강등' 과 같은 취급).
        body = answer or str(raw or "").strip()
        if len(body) >= 40:
            verdict, answer = "answer", body
            if not o.get("answer"):
                answer = "(형식 미준수 — 원문 그대로) " + answer
        else:
            verdict = "error"
    if verdict == "answer" and not answer:
        verdict = "error"
    refer = [str(x).strip()[:60] for x in (o.get("refer") or []) if str(x).strip()][:REFER_MAX]
    basis = [str(x).strip()[:120] for x in (o.get("basis") or []) if str(x).strip()][:6]
    return {**seat, "verdict": verdict, "scope": str(o.get("scope") or "")[:300],
            "answer": answer, "basis": basis, "refer": ([] if verdict == "answer" else refer),
            "error": "" if verdict != "error" else "자기판정을 형식대로 내지 못함",
            "raw": raw if verdict == "error" else ""}


# ── 세션 맥락 — 앞 대화와 그때 도구가 조회한 근거 ─────────────────────────────
_SESSION_HEAD = (
    "[세션 맥락 — 이 질문 앞에 사용자가 챗에서 나눈 대화와, 그때 도구로 실제로 조회한 결과다. 이 맥락 위에서 판단하라.\n"
    " · 조회 근거([e:N])는 도구가 낸 사실이다 — 그 위에서 판단하되 당신의 지식카드와 다르면 다르다고 말하라.\n"
    " · 챗의 답([a:N])은 그때의 잠정 해석이다 — 결론으로 받아들이지 말고, 근거가 없거나 틀렸으면 그렇게 말하라.\n"
    " · 사람의 말([c:N])에 원하는 것과 제약이 있다.]")
_CUT_RE = None   # 늦게 만든다(아래 _trimmed)


def _trimmed(lines: list) -> int:
    """줄 끝에 줄임 표식(' …[N자 중 앞 M자]')이 붙은 줄 수 — _fit_recent 는 마지막 하나를 줄여 싣는다."""
    import re  # noqa: PLC0415
    global _CUT_RE
    if _CUT_RE is None:
        _CUT_RE = re.compile(r" …\[[\d,]+자 중 앞 [\d,]+자\]$")
    return sum(1 for ln in lines if _CUT_RE.search(ln))


def _session_block(history: list | None, cap: int) -> tuple[str, dict]:
    """앞 대화([c:N] 사람 · [a:N] 챗)와 그때 도구가 조회한 근거([e:N] — 포털 이력 항목의 evidence)를 좌석 본심에 실을 한 덩이로,
    cap(자) 안에서. 근거를 먼저 셈해 몫을 정한다 — 대화는 cap 의 _SESSION_CONV_MIN 이상(근거가 적으면 그만큼 더), 근거는 남는 만큼.
    둘 다 **최근부터** 담는다(_fit_recent — 심의의 챗 맥락과 같은 함수). (글, {conv, conv_gone, ev, ev_gone, trimmed}) 을 돌려준다 —
    상태 줄과 종합이 '실은 수 · 뺀 수' 를 말한다(조용히 빼지 않는다)."""
    conv, evid = [], []
    ci = ai = 0
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role, text = m.get("role"), str(m.get("content") or "").strip()
        if role == "user" and text:
            ci += 1
            conv.append(f"[c:{ci}] 사람 — {text}")
        elif role == "assistant":
            if text:
                ai += 1
                conv.append(f"[a:{ai}] 챗 — {text}")
            ev = m.get("evidence")
            for e in (ev if isinstance(ev, list) else []):
                if not isinstance(e, dict):
                    continue
                tool, res = str(e.get("tool") or "").strip()[:200], str(e.get("result") or "").strip()
                if not tool or not res:
                    continue
                args = " ".join(str(e.get("args") or "").split())[:2000]
                # 업로드 근거(kind=upload)는 챗이 조회한 것이 아니라 사람이 챗 화면에서 올려 보낸 결과다(HWAXPortal docs/chat-ppt PP-D3).
                who = "사람이 올려 보낸 파일 · " if e.get("kind") == "upload" else ""
                evid.append(f"[e:{len(evid) + 1}] [{who}{tool}{f'({args})' if args else ''}] {res}")
    if not conv and not evid:
        return "", {}
    room = max(0, cap - len(_SESSION_HEAD) - 40)          # 머리 · 소제목 몫을 먼저 뗀다
    ev_total = sum(len(x) + 3 for x in evid)
    conv_budget = max(int(room * _SESSION_CONV_MIN), room - ev_total)
    conv_lines, conv_left, conv_gone = _fit_recent(conv, conv_budget, 0, lambda _i, t: f"· {t}")
    ev_lines, _ev_left, ev_gone = _fit_recent(evid, max(0, room - (conv_budget - conv_left)), 0, lambda _i, t: f"· {t}")
    out = [_SESSION_HEAD]
    if conv_lines:
        out.append("[앞 대화]")
        if conv_gone:
            out.append(f"(앞선 발화 {conv_gone}건은 예산 밖이라 뺐다 — 오래된 순으로)")
        out.extend(conv_lines)
    if ev_lines:
        out.append("[앞 대화에서 조회한 근거]")
        if ev_gone:
            out.append(f"(앞선 근거 {ev_gone}건은 예산 밖이라 뺐다 — 오래된 순으로)")
        out.extend(ev_lines)
    elif evid:
        out.append(f"(앞 대화에서 조회한 근거 {len(evid)}건은 예산 밖이라 싣지 못했다)")
    return "\n".join(out), {"conv": len(conv_lines), "conv_gone": conv_gone, "ev": len(ev_lines),
                            "ev_gone": len(evid) - len(ev_lines), "trimmed": _trimmed(conv_lines + ev_lines)}


async def _session_images(app, llm, imgs: list, question: str, box: dict):
    """앞 대화의 그림을 좌석에 — 좌석 모델(llm)이 그림을 받으면 box["parts"](최근 vision.HISTORY_IMAGES 장), 못 받으면 그림을 읽는 모델이
    옮긴 판독문을 box["text"](최근 vision.TRANSCRIBE_MAX 장, 그림 해시로 디스크에 남아 다시 읽지 않는다). 상태 줄을 낸다."""
    from app import AIDH_HTTP, ARTIFACT_DIR, _base_vision, _chat_llm_of, _chat_vision, _transcriber  # noqa: PLC0415

    seat_sees = await asyncio.to_thread(_chat_vision if llm is _chat_llm_of(app) else (lambda _a: _base_vision()), app)
    if seat_sees:
        pick = imgs[-vision.HISTORY_IMAGES:] if vision.HISTORY_IMAGES > 0 else []
        ps, miss = await asyncio.to_thread(vision.parts, [u for u, _ in pick], ARTIFACT_DIR, AIDH_HTTP, len(pick))
        if ps:
            box["parts"] = ps
        yield _sse("status", {"step": f"앞 대화의 그림 {len(ps)}장을 좌석마다 실었다(전체 {len(imgs)}장 중 최근부터"
                                      + (f" · {miss}장은 서버에서 지워졌거나 커서 못 실었다" if miss else "") + ")", "tool": None})
        return
    tr = await asyncio.to_thread(_transcriber, app)
    if tr is None:
        yield _sse("status", {"step": f"앞 대화의 그림 {len(imgs)}장 — 좌석 모델도 판독할 모델도 그림을 읽지 못해 싣지 못했다"
                                      + vision.unknown_note(),
                              "tool": None})
        return
    pick = imgs[-vision.TRANSCRIBE_MAX:] if vision.TRANSCRIBE_MAX > 0 else []
    yield _sse("status", {"step": f"앞 대화의 그림 {len(pick)}장을 판독한다(좌석 모델이 그림을 받지 않는다)", "tool": None})
    got = await vision.transcribe_all(tr, pick, ARTIFACT_DIR, AIDH_HTTP, hint=question[:300])
    lines = [f"· [g:{k}] ({why}) {t}" for k, (_u, why, t) in enumerate(got, start=1) if t]
    fail = len(got) - len(lines)
    if lines:
        box["text"] = ("[앞 대화의 그림 — 그림을 읽는 모델이 옮긴 판독문이다. 그림 자체가 아니므로 판독문에 없는 수치를 지어내지 마라. "
                       "쓰거든 [g:N] 표지를 basis 에 적어라]\n" + "\n".join(lines))
    yield _sse("status", {"step": f"앞 대화의 그림 판독문 {len(lines)}장을 좌석마다 실었다"
                                  + (f" · {fail}장은 판독하지 못했다" if fail else "")
                                  + (f" · 오래된 {len(imgs) - len(pick)}장은 상한(VISION_TRANSCRIBE_MAX) 밖" if len(imgs) > len(pick) else ""),
                          "tool": None})


def _session_note(st: dict) -> str:
    """세션 맥락을 얼마나 실었나 — 상태 줄 · 종합이 같은 글을 쓴다."""
    if not st:
        return ""
    parts = [f"앞선 대화 {st['conv']}건"]
    if st.get("ev"):
        parts.append(f"조회 근거 {st['ev']}건")
    gone = []
    if st.get("conv_gone"):
        gone.append(f"오래된 발화 {st['conv_gone']}건")
    if st.get("ev_gone"):
        gone.append(f"오래된 근거 {st['ev_gone']}건")
    return (" · ".join(parts) + "을 좌석마다 실었다"
            + (f"({' · '.join(gone)}은 예산 밖이라 뺐다)" if gone else "")
            + (f" — {st['trimmed']}건은 앞부분만" if st.get("trimmed") else ""))


# ── (4) 종합 — 코드가 조립한다. LLM 을 부르지 않는다 ──────────────────────────
def _summary_text(question: str, answered: list, passed: list, screened: list,
                  errored: list, capped: list, hops: int, session: str = "") -> str:
    out = [f"## Thinking — {len(answered)}명 답변 · {len(passed)}명 기권"]
    if session:
        out.append(f"_세션 맥락 — {session}_")
    if not answered:
        out.append("")
        # 기권과 오류를 섞어 말하지 않는다. '아무도 답할 수 없다' 는 판정 결과이고
        # '아무도 답하지 못했다' 는 장애다 — 둘을 같은 문장으로 내면 풀에 전문성이 없다는
        # 값진 신호가 LLM 불통에 묻힌다.
        if passed and not errored:
            out.append("**답할 수 있다고 판정한 전문가가 없습니다.** 아래 기권 사유가 그 이유이고, "
                       "이것은 오류가 아니라 판정 결과입니다. 풀에 이 주제의 전문성이 없거나, "
                       "질문을 그 도메인의 용어로 좁혀야 합니다.")
        elif errored and not passed:
            out.append("**전문가를 부르지 못했습니다.** 아래는 기권이 아니라 응답 실패입니다 — "
                       "질문을 바꿔도 해결되지 않습니다. 잠시 후 다시 시도하세요.")
        else:
            out.append("**답변이 없습니다.** 일부는 소관이 아니라 기권했고 일부는 응답에 실패했습니다. "
                       "아래에서 둘을 구분해 보세요 — 기권은 판정이고 실패는 장애입니다.")
    for a in answered:
        out.append("")
        out.append(f"### {a['name']} · `{a['key']}`")
        if a.get("scope"):
            out.append(f"_{a['scope']}_")
        out.append("")
        out.append(a["answer"])
        if a.get("basis"):
            out.append("")
            out.append("근거 — " + " / ".join(a["basis"]))
    if passed:
        out.append("")
        out.append("### 기권")
        for p in passed:
            line = f"- `{p['key']}` — {p.get('scope') or '소관 아님'}"
            if p.get("refer"):
                line += f" → 넘김: {', '.join(p['refer'])}"
            out.append(line)
    if screened:
        out.append("")
        out.append("### 예심 탈락 — " + ", ".join(f"`{s['key']}`({s['reason']})" for s in screened))
    if capped:
        out.append("")
        out.append(f"### 안 물어봄 — 답변 상한 {MAX_ANSWERS}명에 걸려 순위 뒤쪽 "
                   + str(len(capped)) + "명에게는 묻지 않았습니다: "
                   + ", ".join(f"`{c['key']}`" for c in capped)
                   + ". 더 듣고 싶으면 THINK_MAX_ANSWERS 를 올리거나 질문을 좁히세요.")
    if errored:
        out.append("")
        out.append("### 응답 실패(기권 아님) — "
                   + ", ".join(f"`{e['key']}`({e.get('error') or '오류'})" for e in errored))
    if hops:
        out.append("")
        out.append(f"_위임 {hops}홉을 따라 추가 소집했습니다._")
    return "\n".join(out)


# ── 스트림 본체 ───────────────────────────────────────────────────────────────
# 띵킹은 좌석이 **동시에** 여러 명 돌아 문서가 좌석 수만큼 배로 든다(자기판정 단계는 선별된
# 좌석 전원이 돈다). 챗 한 번에 주는 몫을 그대로 주면 한 발화에 수백만 토큰이 나간다.
THINK_DOC_SHARE = float(os.environ.get("THINK_DOC_SHARE", "0.5"))


async def _think_llm(app):
    """(좌석 본심 LLM, 창 덮어쓰기 토큰). THINK_LLM=chat 이고 챗 전용 LLM 이 켜져 있으면 그것(Qwen — GLM 넘김 포함)이고, 예산은 그 창으로
    잡는다(챗 턴과 같은 셈 — 두 서버 중 작은 창 − 출력 몫, app._agent_stream). 아니면 VLLM_*(GLM — 사용자 결정 10-18) 그대로다."""
    if THINK_LLM != "chat" or not getattr(app.state, "chat_llm_cfg", None):
        return app.state.llm, None
    from app import _chat_context_tokens, _chat_llm_of, _model_context_tokens  # noqa: PLC0415 — 순환 import 회피
    llm, tok = _chat_llm_of(app), None
    cc = await asyncio.to_thread(_chat_context_tokens)
    if cc:
        win = min(cc, await asyncio.to_thread(_model_context_tokens))
        out = int(app.state.chat_llm_cfg.get("output_reserve") or 0)
        tok = external_llm.ctx_override.set(win - min(out, win // 2))
    return llm, tok


async def run_thinking(app, question: str, groups: list, user: str = "", user_pat: str = "",
                       history: list | None = None, documents: list | None = None):
    """띵킹 모드 SSE 제너레이터. 어떤 경로로 끝나든 result → done 으로 닫는다. 좌석 LLM 과 그 창을 먼저 정한다 — 문서 · 세션 맥락
    예산이 그 창에서 나온다(THINK_LLM)."""
    llm, tok = await _think_llm(app)
    run = _run_thinking(app, llm, question, groups, user, user_pat, history, documents)
    try:
        async for ev in run:
            yield ev
    finally:
        await run.aclose()      # 연결이 끊겨 이 생성기가 닫혀도 본체의 정리(남은 좌석 취소)가 지금 돈다
        if tok is not None:
            try:
                external_llm.ctx_override.reset(tok)
            except ValueError:   # 다른 문맥에서 닫혔다 — 그 문맥은 곧 버려진다
                pass


async def _run_thinking(app, llm, question: str, groups: list, user: str, user_pat: str,
                        history: list | None, documents: list | None):
    """run_thinking 의 본체 — llm 은 좌석 본심 LLM 이다."""
    call_trace.tag(kind="thinking")
    q = str(question or "").strip()
    if not q:
        yield _sse("token", {"delta": "질문을 함께 적어 주세요."})
        yield _sse("result", {"type": "text", "content": "질문을 함께 적어 주세요."})
        yield _sse("done", {})
        return

    # 코드가 JSON 을 파싱하는 경로다 — LLM 보호용 절단을 걸면 응답이 중간에서 끊겨
    # '추천 0명' 이 조용히 나온다(run_agent_search 와 같은 규약).
    from app import CATALOG_RESULT_MAX  # noqa: PLC0415 — 순환 import 회피(늦은 로드)
    # 붙인 문서 — 좌석 프롬프트에만 싣는다. q(질문)에 붙이면 recommend_agents 검색어가
    # 수십만 자가 되어 전문가 발굴이 통째로 망가진다.
    _docs = ""
    if documents:
        from app import _doc_block, _doc_budget_tokens  # noqa: PLC0415
        _docs = _doc_block(documents,
                           budget_tokens=max(2000, int(_doc_budget_tokens(0) * THINK_DOC_SHARE)))
    # 앞선 대화 — 좌석 본심에 세션 맥락으로 싣는다(사용자 결정 10-18 — 종전엔 세기만 하고 버려, 챗에서 파헤친 내용 없이 전문가가
    # 질문 한 줄만 보고 처음부터 검색했다). 소집 · 예심 · 위임 검색어에는 맥락을 싣지 않고 대화의 화두 한 문장만 더한다 — 검색어가
    # 길어지면 전문가 발굴이 망가진다(붙인 문서를 검색어에 섞지 않는 위 규칙과 같은 까닭).
    _hn = sum(1 for m in (history or []) if isinstance(m, dict) and str(m.get("content") or "").strip())
    _session, _sess_st, _topic, sq = "", {}, "", q
    _img_box: dict = {}             # 앞 대화의 그림 — {"parts": 좌석에 실을 그림 조각들} 또는 {"text": 판독문 덩이}
    if _hn and WITH_HISTORY:
        from app import _chars_for_tokens, _derive_topic, _fit_tokens  # noqa: PLC0415 — 순환 import 회피
        from app import _doc_budget_tokens as _dbt  # noqa: PLC0415
        _stok = max(2000, int(_dbt(0) * (SESSION_SHARE if documents else SESSION_SHARE_SOLO)))
        _sample = "".join(str((m or {}).get("content") or "")[:20000] for m in (history or [])[-6:]) or "가나다"

        def _build(c: int) -> str:
            t, st = _session_block(history, c)
            _sess_st.clear()
            _sess_st.update(st)
            return t
        _session = _fit_tokens(_build, _chars_for_tokens(_sample, _stok), _stok, 2000)[0]
        if _session:
            yield _sse("status", {"step": "세션 맥락 — " + _session_note(_sess_st), "tool": None})
        # 앞 대화의 그림(사람이 올린 것 · 도구가 낸 것) — 좌석 모델이 그림을 받으면 그림 조각으로, 못 받으면 그림을 읽는 모델(Qwen)이 옮긴
        # 판독문을 세션 맥락에 [g:N] 으로(사용자 결정 10-18 — 탐침 뒤 자동, HWAXPortal docs/chat-depth-context T5).
        _imgs = vision.history_images(history)
        if _imgs:
            async for _ev in _session_images(app, llm, _imgs, q, _img_box):
                yield _ev
            if _img_box.get("text"):
                _session = (_session + "\n\n" if _session else "") + _img_box["text"]
        _tp = await _derive_topic(history, q)
        _topic = str(_tp.get("topic") or "").strip()
        if _topic and _topic != q:
            sq = f"{_topic}\n{q}"
            yield _sse("status", {"step": f"대화 화두 — “{_topic[:160]}” (전문가 소집 · 지식카드 검색어에 더했다)",
                                  "tool": None})
        else:
            _topic = ""
    elif _hn:
        yield _sse("status", {"step": f"띵킹은 질문만 좌석에 준다 — 앞선 대화 {_hn}건은 싣지 않는다"
                                      "(THINK_WITH_HISTORY=0)", "tool": None})
    yield _sse("status", {"step": "전문가 소집", "tool": "recommend_agents"})
    tools = await _tools_by_name(app, groups, CATALOG_RESULT_MAX, user=user, user_pat=user_pat)
    if not tools or "recommend_agents" not in tools:
        msg = ("전문가 풀에 닿지 못했습니다(MCP 게이트웨이 또는 AIDataHub 미응답). "
               "잠시 후 다시 시도하거나 일반 챗으로 물어보세요.")
        yield _sse("token", {"delta": msg})
        yield _sse("result", {"type": "text", "content": msg})
        yield _sse("done", {})
        return

    sem = asyncio.Semaphore(max(1, CONCURRENCY))
    answered: list[dict] = []
    passed: list[dict] = []
    screened_out: list[dict] = []
    errored: list[dict] = []
    capped: list[dict] = []      # 예심은 통과했지만 상한(THINK_MAX_ANSWERS)에 걸려 안 물어본 좌석
    seen_keys: set[str] = set()
    hops_done = 0

    async def _run_hop(seats: list[dict], hop: int):
        """소집된 좌석들을 예심→본심에 태우고 결과를 위 리스트에 적재한다(이벤트를 yield)."""
        nonlocal answered, passed, screened_out, errored, capped
        for s in seats:
            seen_keys.add(s["key"])
        yield _think("roster", hop=hop, seats=[
            {k: s[k] for k in ("key", "name", "domain", "score", "desc_match", "sections", "records")}
            for s in seats])

        yield _sse("status", {"step": f"예심 — 좌석별 보유 근거 확인({len(seats)}명)",
                              "tool": "agent_search"})
        # 한 번에 도는 수를 묶는다 — 심의의 좌석 지식카드 조회와 같은 호출이 같은 곳(AIDataHub)으로 간다.
        # 종전엔 소집한 좌석 수(기본 10)만큼 한꺼번에 쐈고, 심의와 겹치면 그쪽 연결 풀을 넘겼다. 상한은
        # 심의와 같은 손잡이다(DELIB_KNOWLEDGE_CONCURRENCY, 0=무제한). 자리를 잡은 **뒤에** 묻는다 —
        # 제한시간(SCREEN_TIMEOUT_S)이 줄 선 시간까지 세면 뒤쪽 좌석이 묻지도 못하고 강등된다.
        _scr_sem = asyncio.Semaphore(max(1, min(len(seats), _KN_CONC) if _KN_CONC > 0 else len(seats)))

        async def _screen_gated(s: dict) -> dict:
            async with _scr_sem:
                return await _screen_one(tools, s, sq)

        screened = await asyncio.gather(*[_screen_gated(s) for s in seats])
        live = []
        for s in screened:
            yield _think("screen", key=s["key"], hits=s["hits"], top=s["top"],
                         passed=s["passed"], reason=s["reason"])
            (live if s["passed"] else screened_out).append(s)
        if not live:
            return

        # 상한 초과분은 **버리지 않고 따로 담는다.** 조용히 자르면 '예심 통과 10명' 이라고
        # 해 놓고 5명만 물어본 사실이 어디에도 안 남아, 안 물어본 좌석이 기권한 것처럼 읽힌다.
        if len(live) > MAX_ANSWERS:
            capped.extend(live[MAX_ANSWERS:])
            live = live[:MAX_ANSWERS]
        # 역할 원문은 실제로 물어볼 좌석에게만 복원한다 — 좌석당 get_agent_session 1콜이라
        # 나머지까지 부르면 소집 인원에 비례해 낭비된다.
        roles = await asyncio.gather(*[_restore_role(tools, s["key"], s.get("name") or "")
                                       for s in live])
        for s, r in zip(live, roles):
            s["role"] = r

        yield _sse("status", {"step": f"본심 — 좌석별 자기판정·답변({len(live)}명)", "tool": None})
        tasks = [asyncio.ensure_future(_judge_one(llm, s, q, sem, _docs, _session, _topic, _img_box.get("parts")))
                 for s in live]
        try:
            for fut in asyncio.as_completed(tasks):
                r = await fut
                # ⚠ error 사유를 함께 싣는다. 예전엔 verdict 만 보내서 화면이
                #   "응답 실패(기권 아님)" 만 띄우고 **왜인지 말하지 못했다** — 사용자에겐
                #   그 좌석이 이유 없이 끊긴 것으로 보인다(타임초과인지 형식 실패인지가
                #   다음 행동을 가른다: 전자는 재시도, 후자는 질문을 바꿔야 한다).
                yield _think("verdict", key=r["key"], verdict=r["verdict"],
                             scope=r.get("scope") or "", refer=r.get("refer") or [],
                             error=r.get("error") or "")
                if r["verdict"] == "answer":
                    answered.append(r)
                    yield _think("answer", key=r["key"], name=r["name"], domain=r["domain"],
                                 text=r["answer"], basis=r.get("basis") or [])
                elif r["verdict"] == "pass":
                    passed.append(r)
                else:
                    errored.append(r)
        finally:
            # 브라우저가 창을 닫으면 GeneratorExit 이 온다 — 잔여 태스크를 남기면
            # LLM 호출이 계속 돌아 부하만 남는다(심의에서 같은 문제가 있었다).
            for t in tasks:
                if not t.done():
                    t.cancel()

    try:
        first = await _summon(tools, sq, CANDIDATES, seen_keys)
        _persona_shadow("think", sq, [s["key"] for s in first], CANDIDATES)   # 연결 표가 덮었을 도메인을 적기만 한다(E5-4)
        if not first:
            msg = ("이 질문에 맞는 전문가를 풀에서 찾지 못했습니다. "
                   "질문을 도메인 용어로 좁혀 다시 물어보세요.")
            yield _sse("token", {"delta": msg})
            yield _sse("result", {"type": "text", "content": msg})
            yield _sse("done", {})
            return
        async for ev in _run_hop(first, 0):
            yield ev

        # ── (3) 위임 — 기권 좌석이 지목한 곳으로 한 홉 더 ──────────────────────
        for hop in range(1, max(0, HOPS) + 1):
            if answered and len(answered) >= MAX_ANSWERS:
                break
            phrases, froms = [], []
            for p in passed:
                for ph in p.get("refer") or []:
                    if ph not in phrases:
                        phrases.append(ph)
                        froms.append(p["key"])
            if not phrases:
                break
            yield _sse("status", {"step": f"위임 {hop}홉 — 기권 좌석이 지목한 분야 재소집",
                                  "tool": "recommend_agents"})
            found: list[dict] = []
            seen_doms = {s["domain"] for s in (answered + passed + screened_out)}
            for ph in phrases[:REFER_MAX]:
                for cand in await _summon(tools, ph, 3, seen_keys):
                    # 이미 나온 도메인은 건너뛴다 — 같은 도메인을 다시 부르면 위임이
                    # 깊이만 파고 새 관점을 못 데려온다(심의 _counter_seats 와 같은 규율).
                    if cand["domain"] in seen_doms or any(f["key"] == cand["key"] for f in found):
                        continue
                    found.append(cand)
                    seen_doms.add(cand["domain"])
                    break
            if not found:
                break
            hops_done = hop
            yield _think("handoff", from_=froms[:REFER_MAX], phrases=phrases[:REFER_MAX],
                         seats=[f["key"] for f in found])
            async for ev in _run_hop(found, hop):
                yield ev

        text = _summary_text(q, answered, passed, screened_out, errored, capped, hops_done, _session_note(_sess_st))
        yield _think("summary", answered=len(answered), passed=len(passed),
                     screened_out=len(screened_out), errored=len(errored),
                     capped=len(capped), hops=hops_done, no_answer=not answered)
        # 구조화 이벤트를 냈어도 같은 내용을 텍스트로 낸다 — 프론트가 이 이벤트를 몰라도
        # 사용자는 답을 받고, 포털 대화 저장소는 result.content 만 보므로 저장에도 필요하다.
        yield _sse("token", {"delta": text})
        yield _sse("result", {"type": "text", "content": text})
        yield _sse("done", {})
    except Exception as exc:  # noqa: BLE001 — 부분 결과가 있으면 버리지 않는다
        print(f"[thinking] 실패: {exc!r}")
        if answered or passed:
            text = _summary_text(q, answered, passed, screened_out, errored, capped, hops_done, _session_note(_sess_st))
            text += "\n\n⚠ 처리 도중 오류가 나 여기까지만 모았습니다."
            yield _sse("token", {"delta": text})
            yield _sse("result", {"type": "text", "content": text})
        else:
            yield _sse("error", {"code": "thinking_error",
                                 "message": f"띵킹 모드 처리 중 오류: {type(exc).__name__}"})
        yield _sse("done", {})

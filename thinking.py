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

from deliberation import (
    _agent_search_hits,
    _call,
    _first_dict,
    _llm_text,
    _parse_json,
    _restore_role,
    _sse,
    _tools_by_name,
    is_operator,
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
# 좌석당 지식카드 주입 문자 예산.
KNOWLEDGE_BUDGET = int(os.environ.get("THINK_KNOWLEDGE_BUDGET", "2500"))
# 좌석 1콜 상한(초). 넘으면 그 좌석만 빠지고 나머지는 진행한다.
SEAT_TIMEOUT_S = float(os.environ.get("THINK_SEAT_TIMEOUT_S", "180"))
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


def _hit_line(h) -> str:
    """agent_search hit → `• [제목 › 섹션] 발췌` 한 줄(deliberation._hit_line 과 같은 규격)."""
    if not isinstance(h, dict):
        return str(h)[:300]
    title = h.get("title") or ""
    sec = h.get("section_title") or ""
    body = h.get("snippet") or h.get("text") or h.get("excerpt") or h.get("summary") or ""
    head = f"{title}" + (f" › {sec}" if sec else "")
    if not (head or body):
        return json.dumps(h, ensure_ascii=False, default=str)[:300]
    return f"• [{head}] {str(body).strip()}"[:700]


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

    lines, total, seen = [], 0, set()
    for h in hits:
        ln = _hit_line(h)
        if ln in seen:
            continue
        if total + len(ln) > KNOWLEDGE_BUDGET:
            break
        seen.add(ln)
        lines.append(ln)
        total += len(ln)
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


def _judge_prompt(seat: dict, question: str) -> str:
    kn = seat.get("knowledge") or ""
    ev = (f"[당신의 지식카드 — 이 질문 관련 발췌]\n{kn}\n"
          if kn else
          "[당신의 지식카드]\n이 질문에 걸리는 보유 지식이 없습니다.\n")
    return (
        f"[당신]\n{seat['key']} — {seat.get('role') or seat.get('name') or ''}\n\n"
        f"{ev}\n"
        f"[질문]\n{question}\n\n"
        "먼저 스스로 판정하세요. **이 질문이 당신 도메인의 질문이고, 위 발췌나 당신의 전문 지식으로 "
        "실질적인 답을 줄 수 있습니까?** 어휘가 익숙하다는 것과 답할 수 있다는 것은 다릅니다. "
        "인접 도메인이라 겉핥기만 가능하다면 그것은 답할 수 없는 것입니다.\n\n"
        "답할 수 있으면 verdict=\"answer\" 로, 답을 본문에 쓰세요. 발췌에 없는 수치를 지어내지 말고, "
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
        ' "basis":["인용한 발췌의 제목", "..."],'
        f' "refer":["verdict=pass 일 때만. 봐야 할 분야를 **한국어** 8단어 이내 명사구로 최대 {REFER_MAX}개"]}}'
    )


async def _judge_one(llm, seat: dict, question: str, sem: asyncio.Semaphore) -> dict:
    """좌석 1명의 자기판정 + (가능하면) 답변. 실패·시간초과는 pass 가 아니라 error 로 남긴다."""
    async with sem:
        try:
            raw = await asyncio.wait_for(
                _llm_text(llm, _JUDGE_SYS, _judge_prompt(seat, question)), SEAT_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {**seat, "verdict": "error", "scope": "", "answer": "",
                    "basis": [], "refer": [], "error": f"{SEAT_TIMEOUT_S:.0f}초 초과"}
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


# ── (4) 종합 — 코드가 조립한다. LLM 을 부르지 않는다 ──────────────────────────
def _summary_text(question: str, answered: list, passed: list, screened: list,
                  errored: list, capped: list, hops: int) -> str:
    out = [f"## Thinking — {len(answered)}명 답변 · {len(passed)}명 기권"]
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
async def run_thinking(app, question: str, groups: list, user: str = "", user_pat: str = "",
                       history: list | None = None):
    """띵킹 모드 SSE 제너레이터. 어떤 경로로 끝나든 result → done 으로 닫는다."""
    q = str(question or "").strip()
    if not q:
        yield _sse("token", {"delta": "질문을 함께 적어 주세요."})
        yield _sse("result", {"type": "text", "content": "질문을 함께 적어 주세요."})
        yield _sse("done", {})
        return

    # 코드가 JSON 을 파싱하는 경로다 — LLM 보호용 절단을 걸면 응답이 중간에서 끊겨
    # '추천 0명' 이 조용히 나온다(run_agent_search 와 같은 규약).
    from app import CATALOG_RESULT_MAX  # noqa: PLC0415 — 순환 import 회피(늦은 로드)
    yield _sse("status", {"step": "전문가 소집", "tool": "recommend_agents"})
    tools = await _tools_by_name(app, groups, CATALOG_RESULT_MAX, user=user, user_pat=user_pat)
    if not tools or "recommend_agents" not in tools:
        msg = ("전문가 풀에 닿지 못했습니다(MCP 게이트웨이 또는 AIDataHub 미응답). "
               "잠시 후 다시 시도하거나 일반 챗으로 물어보세요.")
        yield _sse("token", {"delta": msg})
        yield _sse("result", {"type": "text", "content": msg})
        yield _sse("done", {})
        return

    llm = app.state.llm
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
        screened = await asyncio.gather(*[_screen_one(tools, s, q) for s in seats])
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
        tasks = [asyncio.ensure_future(_judge_one(llm, s, q, sem)) for s in live]
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
        first = await _summon(tools, q, CANDIDATES, seen_keys)
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

        text = _summary_text(q, answered, passed, screened_out, errored, capped, hops_done)
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
            text = _summary_text(q, answered, passed, screened_out, errored, capped, hops_done)
            text += "\n\n⚠ 처리 도중 오류가 나 여기까지만 모았습니다."
            yield _sse("token", {"delta": text})
            yield _sse("result", {"type": "text", "content": text})
        else:
            yield _sse("error", {"code": "thinking_error",
                                 "message": f"띵킹 모드 처리 중 오류: {type(exc).__name__}"})
        yield _sse("done", {})

# 포털 챗 "심의 모드" — 다중 라운드 전문가 심의를 코드로 오케스트레이션하고 vLLM(프로덕션=상암 GLM)이
# 스텝별 추론을 담당한다. 코어 로직은 재사용 워크플로 hwax-deliberate.js 와 동형 — 오케스트레이션은
# 코드가, 각 페르소나 발언·라운드·의사결정은 LLM 이. 정본은 역량 있는 Claude(개인 Claude via MCP)이고,
# 이 모듈은 GLM 연결 시 포털 챗으로도 되게 하는 진입점이다.
import json
import re
import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient

DELIBERATE_TRIGGERS = ("/심의", "/deliberate", "/토의")
GROUPS_HEADER = "x-hwax-groups"
N_PERSONAS = 5

# 화두에 불량/품질 얘기가 있으면 SignalForge(VOC)에서 최근 불량 이슈를 먼저 환기한다.
_DEFECT_RE = re.compile(
    r"불량|결함|불만|품질|크랙|파손|파단|리콜|클레임|고장|하자|이슈|스웰링|swelling"
    r"|defect|failure|crack|fault|recall|complaint|quality", re.IGNORECASE)


def _has_defect_topic(question: str) -> bool:
    return bool(_DEFECT_RE.search(question or ""))


def is_deliberation(message: str) -> bool:
    m = (message or "").strip()
    return any(m.startswith(t) for t in DELIBERATE_TRIGGERS)


def strip_trigger(message: str) -> str:
    m = (message or "").strip()
    for t in DELIBERATE_TRIGGERS:
        if m.startswith(t):
            return m[len(t):].strip()
    return m


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _with_groups(connections: dict, groups: list) -> dict:
    hdr = ",".join(groups)
    out = {}
    for name, cfg in connections.items():
        cfg = dict(cfg)
        cfg["headers"] = {**cfg.get("headers", {}), GROUPS_HEADER: hdr}
        out[name] = cfg
    return out


async def _tools_by_name(app, groups: list) -> dict:
    conns = app.state.connections
    if not conns:
        return {}
    scoped = _with_groups(conns, sorted(groups))
    tools = await MultiServerMCPClient(scoped).get_tools()
    return {t.name: t for t in tools}


async def _call(tools: dict, name: str, args: dict):
    t = tools.get(name)
    if t is None:
        return None
    try:
        out = await t.ainvoke(args)
        if isinstance(out, tuple):
            out = out[0]
        # langchain MCP 어댑터는 [{'type':'text','text':'<본문>'}] content-item 리스트로 반환 → text 합치기
        if isinstance(out, list) and out and all(isinstance(i, dict) and "text" in i for i in out):
            return "".join(i.get("text", "") for i in out)
        return out
    except Exception as exc:  # noqa: BLE001 — 도구 실패가 심의를 죽이지 않게
        return f"(tool {name} error: {exc})"


def _first_dict(x):
    """AIDataHub 는 list 반환 툴을 원소별 content 로 직렬화한다 — list면 첫 dict, dict면 자신, 아니면 {}."""
    if isinstance(x, dict):
        return x
    if isinstance(x, list):
        for e in x:
            if isinstance(e, dict):
                return e
    return {}


def _parse_json(text: str):
    """LLM 출력에서 첫 JSON 객체를 관대하게 추출."""
    if isinstance(text, (dict, list)):
        return text
    s = str(text).strip()
    try:
        return json.loads(s)  # 배열/객체 전체가 유효 JSON 이면 그대로
    except Exception:
        pass
    try:
        return json.loads(s[s.index("{"): s.rindex("}") + 1])  # 객체 부분만 추출
    except Exception:
        return None


async def _llm_text(llm, system: str, human: str) -> str:
    r = await llm.ainvoke([("system", system), ("human", human)])
    return r.content if hasattr(r, "content") else str(r)


async def _persona_round(llm, persona: dict, prompt: str) -> dict:
    """페르소나 1명의 한 라운드 발언(JSON). 실패해도 텍스트로 폴백."""
    sysmsg = (f"당신은 '{persona['key']}' 전문가입니다. 전문 영역: {persona.get('role','')}. "
              f"오직 당신의 도메인 관점에서만, 구체적 수치·표준·실패모드로 발언하세요. 영역 밖은 아는 척 금지. "
              f"반드시 유효한 JSON 하나만 출력하세요.")
    txt = await _llm_text(llm, sysmsg, prompt)
    d = _parse_json(txt) or {"say": txt[:800]}
    d["persona"] = persona["key"]
    return d


def _tool_text_ok(s) -> bool:
    """도구 반환이 실제 내용인지 — 에러 문구(SQL 덤프 등)가 환기/프롬프트에 유입되지 않게 거른다."""
    if not isinstance(s, str) or not s.strip():
        return False
    head = s.lstrip()[:160]
    bad = ("(tool ", "Error executing tool", "Traceback", "ProgrammingError",
           "does not exist", "Connection refused", "Internal Server Error")
    return not any(b in head for b in bad)


def _sf_products(alerts: dict) -> list:
    """alert_check 결과에서 경보 제품 코드를 방어적으로 추출(스키마 변동 대비)."""
    out = []
    for key in ("high_negative_ratio", "negative_surge", "alerts"):
        for it in alerts.get(key) or []:
            it = _first_dict(it)
            p = it.get("product_code") or it.get("product")
            if p and p not in out:
                out.append(p)
    return out


async def _defect_briefing(tools: dict, llm, question: str):
    """SignalForge 3-콜 환기: alert_check → get_top_issues/daily_briefing 폴백 → query_voc 증거.
    반환 (환기 표시문, 심의 주입 블록 또는 "", 실제 호출한 도구명 리스트) — best-effort, 연관성은 LLM 판정."""
    parts = []
    used = ["alert_check"]   # 활동 패널용 — 이 환기에서 실제 호출한 SF 도구들
    degraded = False   # 도구가 죽어 내용을 못 받은 흔적 — 전부 죽었으면 '조회 불가' 한 줄로 진행
    raw_alert = await _call(tools, "alert_check", {})
    if isinstance(raw_alert, str) and not _tool_text_ok(raw_alert):
        degraded = True
        raw_alert = None
    alerts = _first_dict(_parse_json(raw_alert))
    summary = alerts.get("summary")
    if summary and _tool_text_ok(str(summary)):
        parts.append(f"경보 요약: {str(summary)[:300]}")
    products = _sf_products(alerts)[:2]

    if products:  # 경보 제품별 이슈 카테고리
        used.append("get_top_issues")
        for p in products:
            top = _first_dict(_parse_json(await _call(
                tools, "get_top_issues", {"product_code": p, "period_days": 7, "top_n": 5})))
            issues = top.get("issues") or top.get("top_issues") or top.get("data") or []
            if not isinstance(issues, list):
                issues = []
            names = [str((_first_dict(i)).get("category") or (_first_dict(i)).get("issue") or i)[:40]
                     for i in issues[:5] if i]
            if names:
                parts.append(f"{p} 최근 7일 이슈: {', '.join(names)}")
    else:  # 경보가 비면(MIN_VOLUME 컷 등) 데일리 브리핑으로 폴백
        used.append("daily_briefing")
        brief = await _call(tools, "daily_briefing", {})
        if isinstance(brief, str) and _tool_text_ok(brief):
            parts.append(f"데일리 브리핑: {brief.strip()[:400]}")
        elif isinstance(brief, str):
            degraded = True

    voc_args = {"sentiment": "negative", "limit": 5}
    if products:
        voc_args["product_code"] = products[0]
    used.append("query_voc")
    raw_voc = await _call(tools, "query_voc", voc_args)
    if isinstance(raw_voc, str) and not _tool_text_ok(raw_voc):
        degraded = True
        raw_voc = None
    voc = _parse_json(raw_voc)
    voc_items = voc if isinstance(voc, list) else (_first_dict(voc).get("results") or _first_dict(voc).get("data") or [])
    if not isinstance(voc_items, list):
        voc_items = []
    for i, v in enumerate(voc_items[:5], 1):
        v = _first_dict(v)
        txt = (v.get("content_translated") or v.get("content") or "")[:200]
        if txt and _tool_text_ok(txt):
            parts.append(f"부정 VOC {i}. ({v.get('product') or v.get('product_code') or '-'}"
                         f"/{v.get('sentiment_score', '-')}) {txt}")

    if not parts:
        # SignalForge 가 미가용(DB 미복원 등)이면 에러 원문 대신 한 줄로 알리고 질문 기반 진행.
        if degraded:
            return ("📡 SignalForge 조회가 지금 불가하여(서비스 미가용) 최근 불량 환기를 건너뜁니다"
                    " — 질문 기반으로 심의를 진행합니다."), "", used
        return "", "", used
    briefing = "\n".join(f"- {p}" for p in parts)

    # 연관성 판정 — 연관된 문제가 있으면 심의에 포함, 없으면 환기만 하고 질문 기반으로 진행.
    verdict = _parse_json(await _llm_text(
        llm,
        "당신은 심의 준비 보조자입니다. 반드시 유효한 JSON 하나만 출력하세요.",
        f"[화두]\n{question}\n\n[최근 고객 불만 신호(SignalForge VOC)]\n{briefing}\n\n"
        "위 불만 신호 중 화두와 실질적으로 연관된 것이 있습니까? "
        'JSON {"relevant": true|false, "reason": "한 문장"} 로만 답하세요.')) or {}
    relevant = bool(verdict.get("relevant"))
    reason = str(verdict.get("reason") or "")[:200]

    display = ("📡 SignalForge 최근 불량 이슈 환기\n" + briefing
               + f"\n→ 연관성: {'심의에 포함' if relevant else '직접 연관 없음 — 질문 기반으로 진행'}"
               + (f" ({reason})" if reason else ""))
    inject = (f"[최근 고객 불만 신호 (SignalForge VOC)]\n{briefing}\n(연관 판정: {reason})\n"
              if relevant else "")
    return display, inject, used


async def run_deliberation(app, question: str, groups: list):
    """심의 SSE 진입점 — 내부 스트림이 어떤 예외로 죽어도 반드시 error+done 을 방출한다.
    (done 없이 끊기면 프론트가 '응답 생성 중'에 갇히고, error 계약이 어긋나면 '(응답이 없습니다)'로 보인다.)"""
    try:
        async for chunk in _deliberation_stream(app, question, groups):
            yield chunk
    except Exception as exc:  # noqa: BLE001
        print(f"[deliberation] fatal: {exc!r}")
        yield _sse("error", {"code": "deliberation_error", "message": f"심의 처리 중 오류: {str(exc)[:200]}"})
        yield _sse("done", {})


async def _deliberation_stream(app, question: str, groups: list):
    """포털 챗 심의 모드의 SSE 제너레이터. 5단계 파이프라인을 코드로 돌리고 진행을 스트리밍한다."""
    llm = app.state.llm
    yield _sse("status", {"step": "심의 시작 — 전문 페르소나 발굴 중", "tool": "recommend_agents"})

    tools = await _tools_by_name(app, groups)
    if not tools:
        yield _sse("error", {"code": "gateway_unavailable",
                             "message": "게이트웨이 MCP 도구를 불러오지 못했습니다(게이트웨이 확인)."})
        yield _sse("done", {}); return

    # 0) 불량 화두면 SignalForge 최근 이슈 환기 — 연관되면 심의 컨텍스트에 포함(best-effort)
    stream_head = ""   # token 으로 먼저 흘린 앞부분(최종 result 전문에도 포함해 상태 일치 유지)
    sf_inject = ""
    if _has_defect_topic(question):
        yield _sse("status", {"step": "최근 불량 이슈 환기 — SignalForge 조회", "tool": "signalforge"})
        try:
            sf_display, sf_inject, sf_used = await _defect_briefing(tools, llm, question)
        except Exception:  # noqa: BLE001 — 환기 실패가 심의를 죽이지 않게
            sf_display, sf_inject, sf_used = "", "", []
        if sf_used:  # 활동 패널용 — 환기에서 실제 호출된 SF 도구들
            yield _sse("status", {"step": "불량 환기 완료", "tool": None, "tools_used": sf_used})
        if sf_display:
            stream_head = sf_display + "\n\n"
            yield _sse("token", {"delta": stream_head})

    # 1) 발굴 — recommend_agents
    rec = await _call(tools, "recommend_agents", {"q": question})
    recd = _parse_json(rec)
    if isinstance(recd, list):
        items = recd
    elif isinstance(recd, dict):
        items = recd.get("recommendations") or recd.get("agents") or recd.get("data") or []
    else:
        items = []
    personas = []
    for it in (items[:N_PERSONAS] if isinstance(items, list) else []):
        it = _first_dict(it)
        key = it.get("agent_type") or it.get("id")
        if not key:
            continue
        # 2) 각 페르소나 컨텍스트 — get_agent_session (list/dict 방어)
        sess = _first_dict(_parse_json(await _call(tools, "get_agent_session", {"agent_type": key})))
        sd = _first_dict(sess.get("data", sess))
        role = (sd.get("description") or sd.get("system_prompt") or "")[:400]
        personas.append({"key": key, "role": role})
    if len(personas) < 2:
        yield _sse("error", {"code": "no_personas",
                             "message": "관련 전문 페르소나를 충분히 찾지 못했습니다(AIDataHub 에이전트 등록 확인)."})
        yield _sse("done", {}); return
    yield _sse("status", {"step": "참여 전문가: " + ", ".join(p["key"] for p in personas), "tool": "get_agent_session",
                          "personas": [p["key"] for p in personas]})

    base = f"[심의 주제]\n{question}\n" + (f"\n{sf_inject}" if sf_inject else "")

    # 3) 다중 라운드 심의
    yield _sse("status", {"step": "1라운드 — 도메인별 초기 입장", "tool": None})
    r1 = await asyncio.gather(*[_persona_round(
        llm, p, base + "\n당신의 관점·권장안·이 주제에서 당신 도메인이 놓칠 리스크를 JSON {lens,recommendation,concerns:[]} 로.") for p in personas])
    r1t = "\n".join(f"• {o['persona']}: {json.dumps({k: o.get(k) for k in ('lens','recommendation','concerns')}, ensure_ascii=False)}" for o in r1)

    yield _sse("status", {"step": "2라운드 — 상호 반박·수치 심화", "tool": None})
    r2 = await asyncio.gather(*[_persona_round(
        llm, p, base + f"\n[1라운드 전원]\n{r1t}\n\n다른 전문가 입장에 수용/반박(근거:수치·표준·실패모드)하고 당신 핵심 주장을 더 깊게. JSON {{concede:[],rebut:[],deepen}} 로.") for p in personas])
    r2t = "\n".join(f"• {o['persona']}: {json.dumps({k: o.get(k) for k in ('concede','rebut','deepen')}, ensure_ascii=False)}" for o in r2)

    yield _sse("status", {"step": "3라운드 — 수렴·최종 입장", "tool": None})
    r3 = await asyncio.gather(*[_persona_round(
        llm, p, base + f"\n[2라운드 전원]\n{r2t}\n\n2R를 반영해 최종 입장·절대 양보 못 하는 제약·최종 권장으로 수렴. JSON {{final_position,non_negotiable,vote}} 로.") for p in personas])
    r3t = "\n".join(f"• {o['persona']}: {json.dumps({k: o.get(k) for k in ('final_position','vote')}, ensure_ascii=False)}" for o in r3)

    # 4) 의사결정문 합성
    yield _sse("status", {"step": "의사결정문 합성 중", "tool": None})
    decision = await _llm_text(
        llm,
        "당신은 심의체 의장입니다. 한국어 엔지니어링 톤으로 명확하게.",
        base + f"\n[2R 심화]\n{r2t}\n\n[3R 최종]\n{r3t}\n\n"
        "## 의사결정문 — (1) 결정사항(번호매김·실행가능), (2) 합의 근거(라운드로 어떻게 수렴했는지), "
        "(3) 소수의견과 처리, (4) 미해결 쟁점+담당·다음 액션, (5) 신뢰도·전제. 라운드별 심화·수렴을 드러내라.")

    # 5) Report Archive 기록(옵션·best-effort — 템플릿 있으면)
    yield _sse("status", {"step": "Report Archive 보고서 저장 중", "tool": "create_report_draft"})
    report_note = ""
    try:
        blocks = {
            "background": [f"심의 주제: {question}"]
                          + ([f"최근 고객 불만 신호(SignalForge VOC) 환기:\n{sf_inject[:1200]}"] if sf_inject else []),
            "results": [r2t[:1500]],
            "recommendation": [p.strip() for p in decision.split("\n\n") if p.strip()][:12],
            "minutes": [f"참여: {', '.join(p['key'] for p in personas)}", "3라운드 심의(R1 초기→R2 심화→R3 수렴).", r3t[:1500]],
        }
        made = _parse_json(await _call(tools, "create_report_draft", {
            "template_id": "deliberation", "template_version": 1,
            "title": f"심의 — {question[:50]}", "blocks": blocks,
            "tags": ["심의", "chat-deliberation"]}))
        rid = ((made or {}).get("report") or {}).get("id")
        if rid:
            report_note = f"\n\n📄 Report Archive 보고서 #{rid} 로 저장됨."
    except Exception as exc:  # noqa: BLE001 — 보고서 실패는 비치명적이되 무음은 피한다
        print(f"[deliberation] create_report_draft failed: {exc!r}")

    # 프론트 SSE 계약(token{delta} → result{type,content})에 맞춰 방출 — 기존 token{content}+text 는
    # chat.api.ts 가 읽지 못한다(delta undefined). result 전문에는 앞서 흘린 환기(stream_head)도 포함.
    yield _sse("token", {"delta": decision + report_note})
    yield _sse("result", {"type": "text", "content": stream_head + decision + report_note})
    yield _sse("done", {})

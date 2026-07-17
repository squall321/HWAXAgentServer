# 포털 챗 "심의 모드" — 다중 라운드 전문가 심의를 코드로 오케스트레이션하고 vLLM(프로덕션=상암 GLM)이
# 스텝별 추론을 담당한다. 코어 로직은 재사용 워크플로 hwax-deliberate.js 와 동형 — 오케스트레이션은
# 코드가, 각 페르소나 발언·라운드·의사결정은 LLM 이. 정본은 역량 있는 Claude(개인 Claude via MCP)이고,
# 이 모듈은 GLM 연결 시 포털 챗으로도 되게 하는 진입점이다.
import json
import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient

DELIBERATE_TRIGGERS = ("/심의", "/deliberate", "/토의")
GROUPS_HEADER = "x-hwax-groups"
N_PERSONAS = 5


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
        return out
    except Exception as exc:  # noqa: BLE001 — 도구 실패가 심의를 죽이지 않게
        return f"(tool {name} error: {exc})"


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


async def run_deliberation(app, question: str, groups: list):
    """포털 챗 심의 모드의 SSE 제너레이터. 5단계 파이프라인을 코드로 돌리고 진행을 스트리밍한다."""
    llm = app.state.llm
    yield _sse("status", {"step": "심의 시작 — 전문 페르소나 발굴 중", "tool": "recommend_agents"})

    tools = await _tools_by_name(app, groups)
    if not tools:
        yield _sse("error", {"content": "게이트웨이 MCP 도구를 불러오지 못했습니다(게이트웨이 확인)."})
        yield _sse("done", {}); return

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
        key = it.get("agent_type") or it.get("id")
        if not key:
            continue
        # 2) 각 페르소나 컨텍스트 — get_agent_session
        sess = _parse_json(await _call(tools, "get_agent_session", {"agent_type": key})) or {}
        sd = sess.get("data", sess)
        role = (sd.get("description") or sd.get("system_prompt") or "")[:400]
        personas.append({"key": key, "role": role})
    if len(personas) < 2:
        yield _sse("error", {"content": "관련 전문 페르소나를 충분히 찾지 못했습니다."})
        yield _sse("done", {}); return
    yield _sse("status", {"step": "참여 전문가: " + ", ".join(p["key"] for p in personas), "tool": None})

    base = f"[심의 주제]\n{question}\n"

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
    report_note = ""
    try:
        blocks = {
            "background": [f"심의 주제: {question}"],
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
    except Exception:
        pass

    yield _sse("token", {"content": decision + report_note})
    yield _sse("text", {"content": decision + report_note})
    yield _sse("done", {})

# 포털 챗 "심의 모드" — 다중 라운드 전문가 심의를 코드로 오케스트레이션하고 vLLM(프로덕션=상암 GLM)이
# 스텝별 추론을 담당한다. 코어 로직은 재사용 워크플로 hwax-deliberate.js 와 동형 — 오케스트레이션은
# 코드가, 각 페르소나 발언·라운드·의사결정은 LLM 이. 정본은 역량 있는 Claude(개인 Claude via MCP)이고,
# 이 모듈은 GLM 연결 시 포털 챗으로도 되게 하는 진입점이다.
import json
import os
import re
import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient

DELIBERATE_TRIGGERS = ("/심의", "/deliberate", "/토의")
# 대화 → RA 보고서 저장(결정적) — LLM 재량에 맡기지 않고 코드가 blocks 를 만들어 저장한다.
# "/보고서 <선택: 내 결론>" — 사용자가 직접 끌어낸 결론을 함께 주면 권고안 맨 앞에 실린다.
REPORT_TRIGGERS = ("/보고서", "/report")
GROUPS_HEADER = "x-hwax-groups"


def _env_int(name: str, default: int) -> int:
    """오타 값이 서버 기동을 죽이지 않게 — 파싱 실패는 경고 로그 후 기본값(app.py 도 공용)."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[deliberation] env {name}='{raw}' 정수 파싱 실패 — 기본값 {default} 사용")
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[deliberation] env {name}='{raw}' 숫자 파싱 실패 — 기본값 {default} 사용")
        return default


# 심의 튜닝 손잡이 — 전부 env. 절단은 층위로 구분한다.
#   모델 입력(role·라운드 직렬화·say 폴백) — 무절단 기본. 모델이 읽는 것을 자르면 발언 깊이가
#     그 상한에 갇힌다(GLM 심의 품질 검증 보고서 1차 원인). 좁은 컨텍스트 환경(dev 16K)만
#     DELIB_ROLE_CLIP 으로 방어값을 걸 수 있다.
#   기록(RA 회의록) — 온전한 발언을 남긴다. DELIB_TRANSCRIPT_CLIP 은 저장 API 보호용 여유 상한.
#   화면(회의 버블) — 가독성용 절단 유지, DELIB_CLIP_SCALE 로 배율 조절.
N_PERSONAS = _env_int("DELIB_PERSONAS", 5)          # 참여 페르소나 수
_ROLE_CLIP = _env_int("DELIB_ROLE_CLIP", 0)         # 페르소나 role 절단 — 0=무절단(기본)
_TRANSCRIPT_CLIP = _env_int("DELIB_TRANSCRIPT_CLIP", 2000)  # RA 회의록 발언당 상한(API 보호용)
_PARSE_RETRIES = _env_int("DELIB_PARSE_RETRIES", 1)  # JSON 파싱 실패 시 재호출 횟수
_CLIP_SCALE = max(0.5, _env_float("DELIB_CLIP_SCALE", 1.0))  # 회의 버블 절단 상한 배율
# 라운드 직렬화(r1t 등)는 모델 입력이지만 다인원 합산이라 무제한이면 좁은 컨텍스트(dev 16K)를
# 밀어낸다 — 값당 여유 상한만 걸고(0=무절단), 의장 프롬프트는 라운드당 별도 상한을 둔다.
_SER_CLIP = _env_int("DELIB_SER_CLIP", 700)          # 직렬화 값당 상한(자), 0=무절단
_DECISION_CTX = _env_int("DELIB_DECISION_CTX", 6000)  # 의장 프롬프트 라운드당 상한(자), 0=무제한


def _c(n: int) -> int:
    """회의 버블 절단 상한에 DELIB_CLIP_SCALE 배율 적용 — 환경별로 발언 표시 길이를 조절."""
    return int(n * _CLIP_SCALE)

# 화두에 불량/품질 얘기가 있으면 SignalForge(VOC)에서 최근 불량 이슈를 먼저 환기한다.
_DEFECT_RE = re.compile(
    r"불량|결함|불만|품질|크랙|파손|파단|리콜|클레임|고장|하자|이슈|스웰링|swelling"
    r"|defect|failure|crack|fault|recall|complaint|quality", re.IGNORECASE)


def _has_defect_topic(question: str) -> bool:
    return bool(_DEFECT_RE.search(question or ""))


def is_deliberation(message: str) -> bool:
    m = (message or "").strip()
    return any(m.startswith(t) for t in DELIBERATE_TRIGGERS)


def is_report_save(message: str) -> bool:
    m = (message or "").strip()
    return any(m.startswith(t) for t in REPORT_TRIGGERS)


def strip_report_trigger(message: str) -> str:
    m = (message or "").strip()
    for t in REPORT_TRIGGERS:
        if m.startswith(t):
            return m[len(t):].strip()
    return m


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


async def _persona_round(llm, persona: dict, prompt: str, required: tuple = ()) -> dict:
    """페르소나 1명의 한 라운드 발언(JSON). 파싱 실패 또는 요구 키 결손 시 에러 피드백으로
    재호출(DELIB_PARSE_RETRIES, 재시도마다 문구를 바꿔 temperature 0 에서도 동일 실패 반복 방지),
    최종 실패에도 원문을 say 로 보존 — 다음 라운드에 무음 유실이 없다(_ser 참조)."""
    sysmsg = (f"당신은 '{persona['key']}' 전문가입니다. 전문 영역: {persona.get('role','')}. "
              f"오직 당신의 도메인 관점에서만, 구체적 수치·표준·실패모드로 발언하세요. 영역 밖은 아는 척 금지. "
              f"반드시 유효한 JSON 하나만 출력하세요.")

    def ok(x) -> bool:
        if not isinstance(x, dict):
            return False
        return not required or any(x.get(k) not in (None, "", []) for k in required)

    txt = await _llm_text(llm, sysmsg, prompt)
    d = _parse_json(txt)
    for attempt in range(max(0, _PARSE_RETRIES)):
        if ok(d):
            break
        hint = ("직전 출력이 유효한 JSON 객체가 아니었습니다."
                if not isinstance(d, dict)
                else f"직전 JSON 에 요구 키({', '.join(required)})의 내용이 비어 있었습니다.")
        txt = await _llm_text(llm, sysmsg, prompt +
                              f"\n\n(재시도 {attempt + 1}/{_PARSE_RETRIES} — {hint} "
                              f"다른 설명 없이, 요구된 키를 실제 내용으로 채운 JSON 객체 하나만 출력하세요.)")
        d = _parse_json(txt)
    if not isinstance(d, dict):
        d = {"say": str(txt)[:800]}
    elif required and not any(d.get(k) not in (None, "", []) for k in required):
        # 요구 키 없는 dict({"response":…} 등) — 원문을 say 로 보존해 다음 라운드에 전달
        d = {**d, "say": str(d.get("say") or txt)[:800]}
    d["persona"] = persona["key"]
    return d


def _ser_val(v) -> str:
    """직렬화 값 정규화 — 배열은 이어 붙이고, DELIB_SER_CLIP 여유 상한만 건다(0=무절단)."""
    if isinstance(v, list):
        v = "; ".join(str(x) for x in v if x)
    s = str(v)
    if _SER_CLIP > 0 and len(s) > _SER_CLIP:
        s = s[:_SER_CLIP].rstrip() + "…"
    return s


def _ser(o: dict, keys: tuple, primary: str = "") -> str:
    """라운드 결과를 다음 라운드 컨텍스트용으로 직렬화. 커버리지 규칙 —
    (1) 핵심 키(primary: r1=lens, r2=deepen, r3=final_position)가 비고 say 가 있으면 say 병기
        (짧은 부수 키 하나로 폴백이 막혀 최종입장이 유실되는 구멍 방지),
    (2) 구조화 키가 전부 비면 say 원문으로 폴백 — 종전 {lens: null,…} 무음 유실 방지."""
    picked = {k: _ser_val(o.get(k)) for k in keys if o.get(k) not in (None, "", [])}
    if primary and primary not in picked and o.get("say"):
        picked["say"] = str(o.get("say"))[:800]
    if not picked and o.get("say"):
        picked = {"say": str(o.get("say"))[:800]}
    return json.dumps(picked, ensure_ascii=False)


def _cap_ctx(s: str) -> str:
    """의장 프롬프트에 싣는 라운드 텍스트의 라운드당 상한(DELIB_DECISION_CTX, 0=무제한) —
    3개 라운드 합산이 좁은 컨텍스트(dev 16K)에서 의장 호출을 밀어내는 꼬리위험 방지."""
    if _DECISION_CTX > 0 and len(s) > _DECISION_CTX:
        return s[:_DECISION_CTX].rstrip() + "\n…(이하 생략)"
    return s


def _clip_sent(text, n: int) -> str:
    """문장 경계에서만 끊어 최대 n자 근처까지 — 중간 절단으로 문장이 깨지지 않게(회의 버블용).
    가로 공백만 정규화하고 개행은 보존한다 — 발언이 한 덩어리로 뭉개져 보이던 원인 수정."""
    t = re.sub(r"[ \t]+", " ", str(text or ""))
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) <= n:
        return t
    sents = [s for s in re.split(r"(?<=[.!?])\s+", t) if s]
    out = sents[0] if sents else t[:n]
    for s in sents[1:]:
        if len(out) + 1 + len(s) > n:
            break
        out += " " + s
    if len(out) > n:  # 문장부호 없는 run-on 출력 방어 — 상한은 반드시 보장
        out = out[:n].rstrip() + "…"
    return out


def _norm_stance(s) -> str:
    """스탠스를 canonical 라벨로 — 부정 표현('동의하지 않습니다' 등)이 동의로 집계되지 않게
    부정 패턴을 먼저 매칭하고, 판별 불가면 조건부로(거짓 만장일치 방지)."""
    s = str(s or "")
    if re.search(r"반대|않|부동의|disagre|oppos|반론", s, re.IGNORECASE):
        return "반대"
    if re.search(r"조건|condition|partial|단서", s, re.IGNORECASE):
        return "조건부 동의"
    if re.search(r"동의|찬성|agree|수용|지지", s, re.IGNORECASE):
        return "동의"
    return "조건부 동의"


def _say_of(rnd: int, d: dict, full: bool = False) -> str:
    """라운드별 구조화 발언 → 대화체 합성(회의 chat 렌더와 동일한 연결어).
    배열 필드는 전 항목을 잇는다 — 종전 first() 는 수용/반박의 첫 항목만 남기고 나머지를 버렸다.
    full=False(회의 버블): DELIB_CLIP_SCALE 배율 절단(_c). full=True(RA 회의록 등 기록):
    무절단 합성 — 기록은 온전해야 하고, 저장 상한은 호출부(_TRANSCRIPT_CLIP)가 여유값으로 건다."""
    BIG = 10 ** 9   # _clip_sent 의 공백 정규화는 유지하되 사실상 무절단

    def clip(v, n):
        return _clip_sent(v, BIG if full else _c(n))

    def joined(v):
        if isinstance(v, list):
            return "; ".join(str(x) for x in v if x)
        return str(v or "")
    # 부분 발언(관점/권장, 수용/반박/심화)은 빈 줄로 구분 — 버블·회의록에서 문단으로 보인다.
    if rnd == 1:
        say = clip(d.get("lens"), 260)
        rec = clip(d.get("recommendation"), 300)
        if rec:
            say = (say + f"\n\n저는 이렇게 봅니다 — {rec}").strip()
    elif rnd == 2:
        parts = []
        con = clip(joined(d.get("concede")), 200)
        reb = clip(joined(d.get("rebut")), 240)
        dp = clip(d.get("deepen"), 320)
        if con:
            parts.append(f"그 지적은 받아들입니다. {con}")
        if reb:
            parts.append(f"다만 반박하자면, {reb}")
        if dp:
            parts.append(f"제 핵심은 이겁니다. {dp}")
        say = "\n\n".join(parts)
    else:
        say = clip(d.get("final_position"), 340)
        vote = clip(d.get("vote"), 160)
        if vote:
            say = (say + f"\n\n최종 권장 — {vote}").strip()
    return say or clip(d.get("say"), 400) or "(발언 파싱 실패)"


def _delib(kind: str, **kw) -> bytes:
    """심의 전용 구조화 이벤트 — 프론트 DelibView(라이브 회의·스테퍼·수렴)가 소비."""
    return _sse("delib", {"kind": kind, **kw})


async def _round_live(llm, personas: list, prompt_fn, rnd: int, required: tuple = ()):
    """라운드 발언을 완료되는 순서대로 산출(async generator) — 라이브 회의 스트림의 핵심.
    gather(전원 대기)와 달리 as_completed 라 먼저 끝난 전문가부터 화면에 등장한다.
    required 는 라운드별 요구 키 — 파싱 재시도·say 보존 판정(_persona_round)에 쓰인다."""
    tasks = [asyncio.ensure_future(_persona_round(llm, p, prompt_fn(p), required)) for p in personas]
    try:
        for fut in asyncio.as_completed(tasks):
            try:
                d = await fut
            except Exception as exc:  # noqa: BLE001 — 한 명의 실패가 라운드를 죽이지 않게(불참 처리)
                print(f"[deliberation] persona r{rnd} failed: {exc!r}")
                continue
            yield d
    finally:  # 클라이언트 중단 시 잔여 LLM 호출 정리
        for t in tasks:
            if not t.done():
                t.cancel()


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
    # 심의 전용 LLM(DELIB_TEMPERATURE 등 env 오버라이드, app.py lifespan) — 미설정이면 본 LLM 그대로.
    llm = getattr(app.state, "delib_llm", None) or app.state.llm
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
        yield _delib("stage", stage="recall")
        yield _sse("status", {"step": "최근 불량 이슈 환기 — SignalForge 조회", "tool": "signalforge"})
        try:
            sf_display, sf_inject, sf_used = await _defect_briefing(tools, llm, question)
        except Exception:  # noqa: BLE001 — 환기 실패가 심의를 죽이지 않게
            sf_display, sf_inject, sf_used = "", "", []
        if sf_used:  # 활동 패널용 — 환기에서 실제 호출된 SF 도구들
            yield _sse("status", {"step": "불량 환기 완료", "tool": None, "tools_used": sf_used})
        if sf_display:
            stream_head = sf_display + "\n\n"
            yield _delib("evidence", source="SignalForge VOC", text=sf_display, included=bool(sf_inject))
            yield _sse("token", {"delta": stream_head})

    # 1) 발굴 — recommend_agents
    # 스테퍼 순서(환기→발굴)와 정확히 일치하도록, 발굴 stage 는 실제 발굴 작업 직전에 방출한다.
    yield _delib("stage", stage="discover")
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
        # role 은 모델 입력(각 페르소나 자신의 시스템 메시지에만 실림 — 인원수에 곱해지지 않는다)
        # 이라 무절단이 기본. 좁은 컨텍스트 환경만 DELIB_ROLE_CLIP>0 으로 방어.
        role = sd.get("description") or sd.get("system_prompt") or ""
        if _ROLE_CLIP > 0:
            role = role[:_ROLE_CLIP]
        personas.append({"key": key, "role": role})
    if len(personas) < 2:
        yield _sse("error", {"code": "no_personas",
                             "message": "관련 전문 페르소나를 충분히 찾지 못했습니다(AIDataHub 에이전트 등록 확인)."})
        yield _sse("done", {}); return
    yield _sse("status", {"step": "참여 전문가: " + ", ".join(p["key"] for p in personas), "tool": "get_agent_session",
                          "personas": [p["key"] for p in personas]})
    yield _delib("personas", personas=[{"key": p["key"], "role": (p.get("role") or "")[:80]} for p in personas])

    base = f"[심의 주제]\n{question}\n" + (f"\n{sf_inject}" if sf_inject else "")

    # 3) 다중 라운드 심의 — 발언이 완료되는 순서대로 delib turn 으로 라이브 방출
    yield _delib("stage", stage="r1", n=len(personas))
    yield _sse("status", {"step": "1라운드 — 도메인별 초기 입장", "tool": None})
    r1 = []
    async for o in _round_live(llm, personas, lambda p: base +
            "\n당신의 관점(lens — 2~4문장, 구체적으로), 위 주제·근거에 실제로 주어진 정보와 당신 도메인의 "
            "확립된 표준·경험칙에 대한 해석(reads — 배열, 접근할 수 없는 데이터·수치를 지어내지 말고 "
            "경험칙에는 (경험칙) 표기), 권장안(recommendation — 2~4문장), "
            "이 주제에서 당신 도메인이 놓칠 리스크(concerns — 최소 2개), 현재 입장 한 줄 요약(position_short)을 "
            "JSON {lens,reads:[],recommendation,concerns:[],position_short} 로. 한 줄 요약은 position_short 에만 — "
            "나머지 필드를 한 줄로 줄이지 마세요.", 1, required=("lens", "recommendation")):
        r1.append(o)
        yield _delib("turn", round=1, persona=o["persona"], say=_say_of(1, o),
                     position=_clip_sent(o.get("position_short"), 90))
    r1t = "\n".join(f"• {o['persona']}: {_ser(o, ('lens', 'reads', 'recommendation', 'concerns'), primary='lens')}" for o in r1)

    yield _delib("stage", stage="r2", n=len(personas))
    yield _sse("status", {"step": "2라운드 — 상호 반박·수치 심화", "tool": None})
    r2 = []
    async for o in _round_live(llm, personas, lambda p: base +
            f"\n[1라운드 전원]\n{r1t}\n\n다른 전문가 입장에 수용(concede)·반박(rebut — 최소 1개, "
            "근거: 수치·표준·실패모드)하고 당신 핵심 주장을 한 단계 더 깊게(deepen — 3문장 이상, 두루뭉술 금지). "
            "JSON {concede:[],rebut:[],deepen} 로.", 2, required=("deepen", "rebut", "concede")):
        r2.append(o)
        yield _delib("turn", round=2, persona=o["persona"], say=_say_of(2, o))
    r2t = "\n".join(f"• {o['persona']}: {_ser(o, ('concede', 'rebut', 'deepen'), primary='deepen')}" for o in r2)

    yield _delib("stage", stage="r3", n=len(personas))
    yield _sse("status", {"step": "3라운드 — 수렴·최종 입장", "tool": None})
    r3 = []
    async for o in _round_live(llm, personas, lambda p: base +
            f"\n[2라운드 전원]\n{r2t}\n\n2R를 반영해 최종 입장(final_position — 2~4문장)·절대 양보 못 하는 "
            "제약(non_negotiable)·최종 권장(vote)으로 수렴하고, "
            "형성된 다수 의견에 대한 당신의 스탠스(동의/조건부 동의/반대)와 최종 입장 한 줄 요약을 밝혀라. "
            "JSON {final_position,non_negotiable,vote,stance,position_short} 로.", 3,
            required=("final_position", "vote")):
        r3.append(o)
        yield _delib("turn", round=3, persona=o["persona"], say=_say_of(3, o),
                     position=_clip_sent(o.get("position_short"), 90),
                     stance=_norm_stance(o.get("stance")))
    r3t = "\n".join(f"• {o['persona']}: {_ser(o, ('final_position', 'non_negotiable', 'vote', 'stance'), primary='final_position')}" for o in r3)

    # 4) 의사결정문 합성
    yield _delib("stage", stage="decide")
    yield _sse("status", {"step": "의사결정문 합성 중", "tool": None})
    decision = await _llm_text(
        llm,
        "당신은 심의체 의장입니다. 한국어 엔지니어링 톤으로 명확하게.",
        base + f"\n[1R 초기입장]\n{_cap_ctx(r1t)}\n\n[2R 심화]\n{_cap_ctx(r2t)}\n\n[3R 최종]\n{_cap_ctx(r3t)}\n\n"
        "## 의사결정문 — (1) 결정사항(번호매김·실행가능), (2) 합의 근거(라운드로 어떻게 수렴했는지), "
        "(3) 소수의견과 처리 — 페르소나가 명시한 non_negotiable(양보 불가 제약)과 stance 를 반영하되, "
        "명시하지 않은 페르소나는 '미표명'으로 기록하고 지어내지 마라, "
        "(4) 미해결 쟁점+담당·다음 액션, (5) 신뢰도·전제. 라운드별 심화·수렴을 드러내라.")

    # 5) Report Archive 기록(옵션·best-effort — 템플릿 있으면)
    yield _delib("stage", stage="report")
    yield _sse("status", {"step": "Report Archive 보고서 저장 중", "tool": "create_report_draft",
                          "detail": f"심의 — {question[:50]}"})
    report_note = ""
    rid = None
    try:
        # 회의록(대화체) — Claude MCP 경로든 챗 경로든 RA 웹에서 회의가 그대로 읽히게 발언을 싣는다.
        transcript = []
        for rnd, arr, label in ((1, r1, "1라운드 — 도메인별 초기 입장"),
                                (2, r2, "2라운드 — 상호 반박·심화"),
                                (3, r3, "3라운드 — 수렴·최종 입장")):
            transcript.append(f"— {label} —")
            # 기록 층위 — 버블용 절단문이 아니라 온전한 발언(full=True)을 남긴다.
            # _TRANSCRIPT_CLIP 은 저장 API 보호용 여유 상한(기본 2000자)일 뿐.
            transcript += [f"[{o['persona']}] {_say_of(rnd, o, full=True)[:_TRANSCRIPT_CLIP]}" for o in arr]
        blocks = {
            "background": [f"심의 주제: {question}"]
                          + ([f"최근 고객 불만 신호(SignalForge VOC) 환기:\n{sf_inject[:1200]}"] if sf_inject else []),
            "results": [r2t[:1500]],
            "recommendation": [p.strip() for p in decision.split("\n\n") if p.strip()][:12],
            "minutes": [f"참여: {', '.join(p['key'] for p in personas)}",
                        "3라운드 심의(R1 초기→R2 심화→R3 수렴)."] + transcript[:40],
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

    # 수렴 집계 — turn 이벤트와 동일한 canonical 정규화로 만장일치/다수결 판정(소수의견 배지의 근거)
    _KEY = {"동의": "agree", "조건부 동의": "conditional", "반대": "oppose"}
    tally = {"agree": 0, "conditional": 0, "oppose": 0, "total": len(r3)}
    for o in r3:
        tally[_KEY[_norm_stance(o.get("stance"))]] += 1
    yield _delib("decision", text=decision + report_note)
    yield _delib("outcome", report_id=rid, title=f"심의 — {question[:50]}",
                 tally=tally, unanimous=(tally["agree"] == tally["total"] and tally["total"] > 0))

    # 프론트 SSE 계약(token{delta} → result{type,content})에 맞춰 방출 — 기존 token{content}+text 는
    # chat.api.ts 가 읽지 못한다(delta undefined). result 전문에는 앞서 흘린 환기(stream_head)도 포함.
    yield _sse("token", {"delta": decision + report_note})
    yield _sse("result", {"type": "text", "content": stream_head + decision + report_note})
    yield _sse("done", {})


async def run_report_save(app, note: str, history: list, groups: list):
    """대화 이력 → Report Archive 보고서(결정적). LLM 을 거치지 않고 코드가 blocks 를 만든다.

    GLM 이 create_report_draft 를 텍스트로 에코해버리는(도구 미호출) 불안정성을 피하려는 설계 —
    '/심의' 파이프라인이 보고서를 코드로 저장하는 것과 같은 원칙. history 는 포털 계약
    [{"role":"user"|"assistant","content":str}, …] (오래된 것→최신, 이번 /보고서 턴 미포함).
    note 는 사용자가 직접 끌어낸 결론(있으면 권고안 맨 앞).
    """
    users = [m.get("content", "") for m in history if m.get("role") == "user"]
    bots = [m.get("content", "") for m in history if m.get("role") == "assistant"]
    if not users and not note:
        yield _sse("result", {"type": "text", "content": "저장할 대화가 없습니다 — 심의/대화 후 다시 시도하세요."})
        yield _sse("done", {})
        return
    question = (users[0] if users else note).split("\n")[0][:120]
    title = f"심의 — {question[:50]}"
    yield _sse("status", {"step": "Report Archive 보고서 저장 중", "tool": "create_report_draft",
                          "detail": title})
    # 회의록 — 대화 전개(누가 무엇을 말했는지) 순서대로. 발언당 400자 캡(RA 웹 가독성).
    minutes = []
    for m in history:
        who = "사용자" if m.get("role") == "user" else "어시스턴트"
        c = str(m.get("content", "")).strip()
        if c:
            minutes.append(f"[{who}] {c[:400]}")
    blocks = {
        "background": [f"심의 주제: {question}"] + ([f"질문 전문:\n{users[0][:1200]}"] if users else []),
        "results": [b[:1500] for b in bots[:-1]][:6] if len(bots) > 1 else [b[:1500] for b in bots],
        "recommendation": ([f"사용자 결론: {note}"] if note else [])
                          + ([p.strip() for p in bots[-1].split("\n\n") if p.strip()][:10] if bots else []),
        "minutes": minutes[:40],
    }
    rid = None
    try:
        tools = await _tools_by_name(app, groups)
        made = _parse_json(await _call(tools, "create_report_draft", {
            "template_id": "deliberation", "template_version": 1,
            "title": title, "blocks": blocks,
            "tags": ["심의", "conversation-report"]}))
        rid = ((made or {}).get("report") or {}).get("id")
    except Exception as exc:  # noqa: BLE001 — RA 미가용(cae00 등)은 비치명적 폴백
        print(f"[report-save] create_report_draft failed: {exc!r}")
    text = (f"📄 Report Archive 보고서 #{rid} 로 저장했습니다 — 「{title}」"
            if rid else "Report Archive 저장이 불가합니다(RA 미가용 또는 도구 없음). 대화는 서버에 남아 있으니 나중에 다시 시도하세요.")
    yield _sse("token", {"delta": text})
    yield _sse("result", {"type": "text", "content": text})
    yield _sse("done", {})

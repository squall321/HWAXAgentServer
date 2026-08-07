# 포털 챗 "심의 모드" — 다중 라운드 전문가 심의를 코드로 오케스트레이션하고 vLLM(프로덕션=상암 GLM)이
# 스텝별 추론을 담당한다. 코어 로직은 재사용 워크플로 hwax-deliberate.js 와 동형 — 오케스트레이션은
# 코드가, 각 페르소나 발언·라운드·의사결정은 LLM 이. 정본은 역량 있는 Claude(개인 Claude via MCP)이고,
# 이 모듈은 GLM 연결 시 포털 챗으로도 되게 하는 진입점이다.
import json
import os
import re
import asyncio
from types import SimpleNamespace
from langchain_mcp_adapters.client import MultiServerMCPClient

DELIBERATE_TRIGGERS = ("/심의", "/deliberate", "/토의")
# 시뮬레이션 심의 — 메커니즘을 좁힌 뒤 CAE 가 해석을 설계하는 2단 심의. 일반 심의보다 먼저
# 검사해야 한다("/시뮬심의"가 "/심의"로 시작하지 않으므로 순서 의존은 없지만 의도를 명시).
SIM_DELIBERATE_TRIGGERS = ("/시뮬심의", "/시뮬레이션심의", "/simdeliberate")
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
_TRANSCRIPT_CLIP = _env_int("DELIB_TRANSCRIPT_CLIP", 12000)  # RA 회의록 발언당 상한(API 보호용)
_PARSE_RETRIES = _env_int("DELIB_PARSE_RETRIES", 1)  # JSON 파싱 실패 시 재호출 횟수
_CLIP_SCALE = max(0.5, _env_float("DELIB_CLIP_SCALE", 1.0))  # 회의 버블 절단 상한 배율
# 라운드 직렬화(r1t 등)는 모델 입력이지만 다인원 합산이라 무제한이면 좁은 컨텍스트(dev 16K)를
# 밀어낸다 — 값당 여유 상한만 걸고(0=무절단), 의장 프롬프트는 라운드당 별도 상한을 둔다.
_SER_CLIP = _env_int("DELIB_SER_CLIP", 700)          # 직렬화 값당 상한(자), 0=무절단
_DECISION_CTX = _env_int("DELIB_DECISION_CTX", 6000)  # 의장 프롬프트 라운드당 상한(자), 0=무제한

# 깊이 회복 손잡이(GLM 리뷰 §5 검증 통과분) — 전부 기본 0(종전 동작). GLM급은 다중 제약
# 동시 적용 시 지시 추종이 분산돼 효과가 상쇄되므로(§5 실행 순서) 한 번에 하나씩 A/B 할 것.
_EVIDENCE_PREPASS = _env_int("DELIB_EVIDENCE_PREPASS", 0)  # T1 정량 근거 선주입(도구 조회→발췌)
_REBUT_QUOTE = _env_int("DELIB_REBUT_QUOTE", 0)      # T2 반박 인용 계약 — quote 실재를 코드 검증
_PROSE_FIRST = _env_int("DELIB_PROSE_FIRST", 0)      # T3 산문 논증 후 JSON(형식 강제 완화)
_CROSS_EXAM = _env_int("DELIB_CROSS_EXAM", 0)        # 2R 교차심문 — 지목 표적의 원본 전체에 반박
_ANCHOR = _env_int("DELIB_ANCHOR", 0)                # 3R 입장 앵커 재주입(동조 붕괴 방어)
_CHAIR_BESTOF = _env_int("DELIB_CHAIR_BESTOF", 1)    # 의장 후보 n개→심판 선택(1=끔, temp>0 필요)
_CHAIR_CITE = _env_int("DELIB_CHAIR_CITE", 0)        # 의장 결정문에 [라운드·페르소나] 출처 태깅

# 좌석 구성 손잡이 — 위 '깊이 회복 손잡이'와 달리 프롬프트 제약을 늘리지 않고 참가자 집합만
# 바꾸므로, GLM 의 지시 추종 예산과 무관하다(단일 변수 A/B 원칙의 적용 대상이 아님).
# 근거: docs/deliberation-quality/plan.md §0-2.
# 의장 산출 항목 — MCP 워크플로(hwax-deliberate.js)의 chairTemplate 과 같은 계약.
# 두 경로가 갈리면 웹 결정문과 MCP 결정문의 형식이 어긋난다.
_CHAIR_ITEMS = {
    "default":
        "(1) 결정사항(번호매김·실행가능), (2) 합의 근거(라운드로 어떻게 수렴했는지), "
        "(3) 소수의견과 처리 — 페르소나가 명시한 non_negotiable(양보 불가 제약)과 stance 를 반영하되, "
        "명시하지 않은 페르소나는 '미표명'으로 기록하고 지어내지 마라, "
        "(4) 미해결 쟁점+담당·다음 액션, (5) 신뢰도·전제.",
    "mechanism":
        "(1) 메커니즘 결론 — 무엇이 무엇을 어떤 경로로 바꾸는가를 인과 사슬로, "
        "(2) 상태변수와 공간·시간 스케일 — 무엇을 추적해야 현상이 기술되는가, "
        "(3) 지배방정식 후보 — 형태 수준으로(확산·반응·이동·구조·열 등 어느 계열인지와 결합 관계), "
        "(4) 미지 파라미터 목록 — 값을 모르는 물리량과 단위·예상 범위, "
        "(5) 반증 관측 — 이 메커니즘이 틀렸다면 무엇이 관측되는가, "
        "(6) 합의 근거와 소수의견 처리, (7) 신뢰도·전제. "
        "이 결정문은 후속 해석 설계 심의의 입력이므로 (2)(3)(4)를 특히 구체적으로 쓰라.",
    "sim-plan":
        "해석 계획서 10개 항목 — (1) 해석 목적: 이 계산이 답할 질문 하나를 문장으로, "
        "(2) 지배방정식과 물리 모델: 위 메커니즘 결론에서 승계해 수식 수준으로 확정, "
        "(3) 해석 종류·차원·기하 축약: 3D full / 2D 평면·축대칭 / 1D 중 무엇이며 그 축약이 정당한 이유, "
        "(4) 솔버·도구 선택과 근거: 사내 보유 도구를 우선 검토하고 없을 때만 외부 도구, "
        "(5) 이산화: 메시 전략·시간 적분·수치 기법과 안정성 조건, (6) 경계·초기조건, "
        "(7) 물성·파라미터 확보 경로와 식별성 판정 — 각 파라미터를 문헌/독립 측정/피팅으로 분류하고, "
        "피팅 대상이 둘 이상이면 서로 곱으로 붙어 분리되지 않는지(퇴화) 판정하라. "
        "퇴화가 있으면 그것을 푸는 독립 관측을 지정하라, "
        "(8) 검증 계획: 해석해·벤치마크·시험 대조, (9) 계산 규모와 일정, "
        "(10) 이 해석이 답할 수 없는 것. (7)과 (10)은 비워두지 마라 — 비면 계획서가 아니다.",
}

# 시뮬레이션 심의 2단 좌석 — 고정 CAE 좌석은 발굴에 맡기지 않는다. 현상 어휘에 끌려
# 방법론·검증 좌석이 빠지는 일이 생긴다.
_SIM_FIXED_CAE = ("xd-cae-modeling", "xd-cae-post")
_SIM_CARRY = _env_int("DELIB_SIM_CARRY", 2)           # 2단에 남길 물리 유임 좌석 수

_COUNTER_SEATS = _env_int("DELIB_COUNTER_SEATS", 2)   # 반대 도메인 좌석 수(0=끔, 종전 동작)
_RESCREEN = _env_int("DELIB_RESCREEN", 1)             # 이어하기 좌석 재심사(0=끔, 종전 동작)
_RESCREEN_SEATS = _env_int("DELIB_RESCREEN_SEATS", 2)  # 재심사로 더할 신규 좌석 상한


def _resolve_opts(req_opts):
    """요청 단위 오버라이드 — 웹 토글이 심의마다 손잡이를 바꿀 수 있게(env 는 기본값).
    미지정 키는 env 기본값 유지(하위호환). 값은 화이트리스트 키만 읽고 정수/실수로 강제·클램프
    하므로 신뢰 안 되는 입력이 와도 안전. timeout_s=None 이면 기동 시 delib_llm 타임아웃 사용."""
    o = SimpleNamespace(
        evidence_prepass=_EVIDENCE_PREPASS, rebut_quote=_REBUT_QUOTE, prose_first=_PROSE_FIRST,
        cross_exam=_CROSS_EXAM, anchor=_ANCHOR, chair_bestof=_CHAIR_BESTOF, chair_cite=_CHAIR_CITE,
        parse_retries=_PARSE_RETRIES, rounds=3, timeout_s=None,
        # 이어하기(사람 개입 스티어링) — 사람 의견 주입 + 이전 심의 요약 + 전문가 재사용(발굴 생략)
        human_note="", continue_summary="", continue_personas=[],
        # 이전 심의의 양보 불가 조항 — 요약에 넣지 않으면 소실되므로 별도 필드로 승계한다(F11).
        continue_non_negotiables=[],
        # 1이면 초기 라운드까지만 돌고 멈춘다(F7 인간 체크포인트). 사람이 빠진 관점을 보태
        # 이어하기를 부르면 좌석 재심사가 그 방향에 맞는 도메인을 불러온다.
        stop_after_round=0,
        # 의장 산출 항목 템플릿 — default | mechanism | sim-plan. 시뮬레이션 심의가 1단/2단에서 지정한다.
        chair_template="default",
        # 사용자 지정 도구 — 심의 시작 전 실제 호출해 정량 근거로 주입(자동 파이프라인 도구에 추가).
        delib_tools=[],
        # 자유 조회 — 라운드 발언 전에 각 전문가가 읽기 전용 도구를 직접 호출하는 단계.
        # 없으면 심의는 시작 시점 근거 스냅샷에 갇힌다(사전 조회의 상상력이 검증 범위의 상한).
        free_tools=_env_int("DELIB_FREE_TOOLS", 1),
        tool_budget=_env_int("DELIB_TOOL_BUDGET", 3),
    )
    if isinstance(req_opts, dict):
        for k in ("evidence_prepass", "rebut_quote", "prose_first", "cross_exam", "anchor",
                  "chair_bestof", "chair_cite", "parse_retries", "rounds",
                  "free_tools", "tool_budget", "stop_after_round"):
            v = req_opts.get(k)
            if v is not None:
                try:
                    setattr(o, k, int(v))
                except (ValueError, TypeError):
                    pass
        ts = req_opts.get("timeout_s")
        if ts is not None:
            try:
                o.timeout_s = float(ts)
            except (ValueError, TypeError):
                pass
        hn = req_opts.get("human_note")
        if isinstance(hn, str):
            o.human_note = hn[:2000]
        ct = req_opts.get("chair_template")
        if isinstance(ct, str) and ct in _CHAIR_ITEMS:
            o.chair_template = ct
        cs = req_opts.get("continue_summary")
        if isinstance(cs, str):
            o.continue_summary = cs[:8000]
        # 이전 심의의 양보 불가 조항(F11) — 요약 문자열에 섞으면 소실되므로 별도 필드로 받는다.
        nn = req_opts.get("non_negotiables") or req_opts.get("continue_non_negotiables")
        if isinstance(nn, list):
            o.continue_non_negotiables = [str(x)[:1200] for x in nn if str(x).strip()][:12]
        cp = req_opts.get("personas")
        if isinstance(cp, list):
            o.continue_personas = [{"key": str(p.get("key"))[:120], "role": str(p.get("role") or "")[:2000]}
                                   for p in cp[:12] if isinstance(p, dict) and p.get("key")]
        tl = req_opts.get("tools")
        if isinstance(tl, list):
            o.delib_tools = [str(n).strip()[:80] for n in tl[:6] if isinstance(n, str) and str(n).strip()]
    # 안전 보정 — 인용 계약 켜면 재시도 하한 2(신규 스키마 준수율), best-of 1~5, 타임아웃 10~1800s
    if o.rebut_quote and o.parse_retries < 2:
        o.parse_retries = 2
    o.parse_retries = max(0, min(10, o.parse_retries))   # 방어심층 — 직접 호출 시 재시도 폭주 상한
    o.chair_bestof = max(1, min(5, o.chair_bestof))
    o.rounds = max(2, min(8, o.rounds))                  # 라운드 수 2~8(기본 3=초기+심화1+수렴)
    o.tool_budget = max(1, min(6, o.tool_budget))        # 자유 조회 1인당 호출 상한
    if o.timeout_s is not None:
        o.timeout_s = max(10.0, min(1800.0, o.timeout_s))
    return o


_DEFAULT_OPTS = _resolve_opts(None)   # env 기본값 스냅샷 — 요청 미지정 시 사용


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


def is_sim_deliberation(message: str) -> bool:
    m = (message or "").strip()
    return any(m.startswith(t) for t in SIM_DELIBERATE_TRIGGERS)


def strip_sim_trigger(message: str) -> str:
    m = (message or "").strip()
    for t in SIM_DELIBERATE_TRIGGERS:
        if m.startswith(t):
            return m[len(t):].strip()
    return m


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


async def _tools_by_name(app, groups: list, result_max=None, desc_max=None) -> dict:
    """result_max: 도구 결과 절단 한도. None 이면 LLM 프롬프트 보호용 기본(TOOL_RESULT_MAX).
    결과를 코드가 JSON 으로 파싱하는 결정적 경로는 CATALOG_RESULT_MAX 를 넘겨 절단을 사실상 끈다."""
    conns = app.state.connections
    if not conns:
        return {}
    scoped = _with_groups(conns, sorted(groups))
    tools = await MultiServerMCPClient(scoped).get_tools()
    # 챗 경로와 같은 래퍼를 반드시 통과시킨다. 우회하면 이미지 도구의 base64 원문이 그대로
    # '정량 근거'로 주입돼 그래프는 사라지고 근거 패널에 'iVBORw0KGgo…' 덩어리가 남는다
    # (감사 확인). 결과 절단·아티팩트 저장·인자 힌트도 전부 이 래퍼에 있다.
    # 늦은 import — app 이 이 모듈을 import 하므로 모듈 로드 시점에 하면 순환이 된다.
    try:
        from app import _prep_tool  # noqa: PLC0415
        tools = [_prep_tool(t, result_max, desc_max) for t in tools]
    except Exception as exc:  # noqa: BLE001 — 래핑 실패해도 심의는 진행
        print(f"[deliberation] _prep_tool 적용 실패: {exc!r}")
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
    """LLM 출력에서 JSON 객체를 관대하게 추출. 최상위 균형 객체들을 앞에서부터 스캔해
    마지막 것을 취한다 — 산문 선행 출력(DELIB_PROSE_FIRST)의 '마지막에 JSON' 계약과 맞고,
    산문 속 '{x}' 수식·중간 예시(첫 '{'~마지막 '}' 방식이 오추출하던 엣지)에 안 속는다.
    JSON-only 출력(객체 하나)에서는 종전과 동일 결과."""
    if isinstance(text, (dict, list)):
        return text
    s = str(text).strip()
    try:
        return json.loads(s)  # 배열/객체 전체가 유효 JSON 이면 그대로
    except Exception:
        pass
    # 병리 입력(미종결 문자열 반복 등)에서 스캔이 O(n²) — 유효 JSON 전체는 위 快경로가 이미
    # 처리했으므로 폴백 스캔에만 상한을 건다(96KB→168ms, 300KB→1.6s 실측).
    if len(s) > 100_000:
        s = s[:100_000]
    dec = json.JSONDecoder()
    found = None
    i = s.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(s, i)
            if isinstance(obj, dict):
                found = obj
            i = s.find("{", max(end, i + 1))
        except Exception:
            i = s.find("{", i + 1)
    return found


async def _llm_text(llm, system: str, human: str) -> str:
    r = await llm.ainvoke([("system", system), ("human", human)])
    return r.content if hasattr(r, "content") else str(r)


async def _persona_round(llm, persona: dict, prompt: str, required: tuple = (),
                         validator=None, opts=_DEFAULT_OPTS) -> dict:
    """페르소나 1명의 한 라운드 발언(JSON). 파싱 실패·요구 키 결손·검증기 지적 시 에러 피드백으로
    재호출(opts.parse_retries, 재시도마다 문구를 바꿔 temperature 0 에서도 동일 실패 반복 방지),
    최종 실패에도 원문을 say 로 보존 — 다음 라운드에 무음 유실이 없다(_ser 참조).
    validator: dict → 지적 문구(str) 또는 None(통과). 형식 검증을 내용 수준으로 올리는 훅
    (인용 반박 실재 검증 등) — 재시도가 소진되면 지적이 남아도 발언은 그대로 쓴다(soft).
    opts: 요청 단위 손잡이(prose_first/parse_retries 사용) — 기본은 env 스냅샷."""
    fmt = ("당신의 논증을 먼저 산문으로 자유롭게 전개한 뒤(6~12문장), 마지막에 유효한 JSON 객체 "
           "하나로 마무리하세요. JSON 뒤에는 아무것도 쓰지 마세요. JSON 필드에는 결론의 전문을 "
           "담으세요 — 산문을 참조('위에서 말했듯')하지 마세요."
           if opts.prose_first else "반드시 유효한 JSON 하나만 출력하세요.")
    sysmsg = (f"당신은 '{persona['key']}' 전문가입니다. 전문 영역: {persona.get('role','')}. "
              f"오직 당신의 도메인 관점에서만, 구체적 수치·표준·실패모드로 발언하세요. 영역 밖은 아는 척 금지. "
              f"{fmt}")

    def problem_of(x):
        if not isinstance(x, dict):
            return "직전 출력이 유효한 JSON 객체가 아니었습니다."
        if required and not any(x.get(k) not in (None, "", []) for k in required):
            return f"직전 JSON 에 요구 키({', '.join(required)})의 내용이 비어 있었습니다."
        return validator(x) if validator else None

    txt = await _llm_text(llm, sysmsg, prompt)
    d = _parse_json(txt)
    for attempt in range(max(0, opts.parse_retries)):
        hint = problem_of(d)
        if not hint:
            break
        txt = await _llm_text(llm, sysmsg, prompt +
                              f"\n\n(재시도 {attempt + 1}/{opts.parse_retries} — {hint} "
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
    """직렬화 값 정규화 — 배열은 이어 붙이고, DELIB_SER_CLIP 여유 상한만 건다(0=무절단).
    dict 항목(인용 반박 계약 등 구조화 출력)은 Python repr 로 새지 않게 JSON 으로 직렬화."""
    if isinstance(v, dict):
        v = json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        v = "; ".join(json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x)
                      for x in v if x)
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


def _norm_ws(s) -> str:
    """공백·개행 정규화 — 인용 실재 검증은 표시 개행 차이에 흔들리면 안 된다."""
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _quote_validator(ctx: str, where: str = "위 라운드 텍스트"):
    """반박 인용 계약(DELIB_REBUT_QUOTE) 검증기 — quote 가 모델이 실제로 본 라운드 직렬화
    문자열(절단 포함)에 실재해야 반박으로 인정. 항목 하나라도 유효하면 통과(재시도 폭주 방지).
    허수아비 반박('동의하지만 추가 고려 필요')을 구조적으로 차단하는, 코드로 검증 가능한
    유일한 깊이 레버(GLM 리뷰 §5). where 는 재시도 힌트의 복사 출처 문구(교차심문은 표적 명시).
    ctx 는 json.dumps 직렬화라 값 안의 개행이 리터럴 \\n, 따옴표가 \\" 로 실린다 — 모델이
    화면 그대로 복사한 quote 는 JSON 디코드 후 실제 개행·따옴표가 되므로, 이스케이프를 해제한
    변형 컨텍스트도 병행 매칭한다(완벽한 verbatim 인용이 다행 값에서 실패하던 비대칭 제거)."""
    nctx = _norm_ws(ctx)
    nctx_unesc = _norm_ws(str(ctx).replace("\\n", " ").replace('\\"', '"').replace("\\\\", "\\"))

    def check(d: dict):
        rebs = d.get("rebut")
        if not isinstance(rebs, list) or not rebs:
            return ("rebut 이 비어 있습니다 — 최소 1개, "
                    "{target,quote,counter,basis} 객체 배열로 작성하세요.")
        any_dict = False
        for r in rebs:
            if not isinstance(r, dict):
                continue
            any_dict = True
            q = _norm_ws(r.get("quote"))
            if len(q) >= 15 and (q in nctx or q in nctx_unesc):
                return None
        if not any_dict:
            return ("rebut 항목이 문자열입니다 — {target,quote,counter,basis} 객체 배열로 다시. "
                    f"quote 는 {where}에서 20자 이상 그대로 복사하세요.")
        return (f"rebut 의 quote 가 상대 발언 원문에 실재하지 않습니다 — {where}에서 "
                "문구를 20자 이상 그대로(변형 없이) 복사해 quote 에 넣으세요.")

    return check


def _item_text(x) -> str:
    """배열 항목 → 대화체 문구. 인용 반박 계약의 dict({target,quote,counter,basis})는
    '누구의 어떤 발언에 대한 반박인지'가 읽히게 합성하고, 그 외 dict 는 key: value 나열."""
    if not isinstance(x, dict):
        return str(x or "")
    if x.get("counter") or x.get("quote"):
        tgt = str(x.get("target") or "").strip()
        q = _norm_ws(x.get("quote"))
        c = str(x.get("counter") or "").strip()
        b = str(x.get("basis") or "").strip()
        head = (f"{tgt}의 " if tgt else "") + (f"'{q[:80]}' 에 대해 —" if q else "")
        parts = [p for p in (head.strip(), c, f"(근거: {b})" if b else "") if p]
        return " ".join(parts)
    return "; ".join(f"{k}: {v}" for k, v in x.items() if v)


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
            return "; ".join(_item_text(x) for x in v if x)
        return _item_text(v) if v else ""
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


async def _round_live(llm, personas: list, prompt_fn, rnd: int, required: tuple = (),
                      validator_fn=None, opts=_DEFAULT_OPTS):
    """라운드 발언을 완료되는 순서대로 산출(async generator) — 라이브 회의 스트림의 핵심.
    gather(전원 대기)와 달리 as_completed 라 먼저 끝난 전문가부터 화면에 등장한다.
    required 는 라운드별 요구 키 — 파싱 재시도·say 보존 판정(_persona_round)에 쓰인다.
    validator_fn: 페르소나 → 내용 검증기(교차심문은 표적이 달라 검증 컨텍스트가 1인 1개).
    opts: 요청 단위 손잡이 — _persona_round 로 전달(prose_first/parse_retries)."""
    tasks = [asyncio.ensure_future(_persona_round(
        llm, p, prompt_fn(p), required,
        validator_fn(p) if validator_fn else None, opts=opts)) for p in personas]
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


# 유령 ID 게이트(app._cap_tool)가 '실행하지 않았다'는 뜻으로 돌려주는 표지. 챗과 심의 양쪽에서
# 판정에 쓰이므로 하위 모듈인 여기에 두고 app 이 import 한다(app→deliberation 단방향 유지).
_PHANTOM_ID_MARK = "는 이번 대화 어디에도 없는 값이다"


def _tool_text_ok(s) -> bool:
    """도구 반환이 실제 내용인지 — 에러 문구(SQL 덤프 등)가 환기/프롬프트에 유입되지 않게 거른다."""
    if not isinstance(s, str) or not s.strip():
        return False
    head = s.lstrip()[:160]
    bad = ("(tool ", "Error executing tool", "Traceback", "ProgrammingError",
           "does not exist", "Connection refused", "Internal Server Error")
    return not any(b in head for b in bad)


def _delib_tool_result_ok(s: str) -> bool:
    """지정 도구 응답이 '근거로 쓸 수 있는' 결과인지 — 텍스트 에러 패턴에 더해, 구조화 에러
    JSON({ok:false}/{errors:[…]}/{error:"…"})과 'error: …' 평문을 걸러 재시도/스킵을 유도한다.

    ⚠ 여기서 놓치면 에러 문구가 '[사용자 지정 도구 정량 결과 (실호출 — 발언에 인용할 것)]'
    으로 심의 라운드에 주입되고 보고서까지 간다. 실측 사고 2건 —
      · get_material(material_id=6061) → {"error": "재료를 찾을 수 없습니다."}
      · get_mat_card(units="MPa")      → error: unknown unit system: 'MPa' (choices: …)
    둘 다 ok:false/errors 가 아니라 통과했고 '근거 확보 3건'으로 집계됐다."""
    if not _tool_text_ok(s):
        return False
    head = s.lstrip()[:200]
    # 유령 ID 게이트의 교정문 — 도구가 실행되지 않았다는 뜻이므로 근거가 아니다. 재시도로 보내면
    # 그 교정문이 _err_note 로 LLM 에 피드백돼 스스로 ID 를 다시 찾는다(self-repair).
    if _PHANTOM_ID_MARK in head:
        return False
    if head.lower().startswith("error:") or head.startswith("오류:"):
        return False
    try:
        v = json.loads(s)
        if isinstance(v, dict) and (v.get("ok") is False or v.get("errors") or v.get("error")):
            return False
    except (ValueError, TypeError):
        pass  # JSON 아님 — 텍스트 결과는 위 검사 통과로 충분
    return True


def _tool_schema_brief(t) -> str:
    """도구 args 스키마를 LLM 인자 구성 프롬프트용 요약으로 — MCP 어댑터의 JSON 스키마 dict 만
    (pydantic 모델이면 빈 문자열 — 설명만으로 구성 시도). 필드 12개·설명 100자 캡."""
    schema = getattr(t, "args_schema", None)
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties", {})
    req = set(schema.get("required", []) or [])
    lines = []
    for k, v in list(props.items())[:12]:
        if isinstance(v, dict):
            lines.append(f"- {k}{'(필수)' if k in req else ''}: {v.get('type', 'any')}"
                         f" — {str(v.get('description', ''))[:100]}")
    return "\n".join(lines)



# ── 자유 조회(free tools) — 발언 전에 전문가가 스스로 데이터를 조회하는 단계 ─────────────
# 읽기 전용만 허용한다. 심의가 데이터를 등록·수정하는 부작용을 내면 안 된다 — 접두사
# 화이트리스트로만 열고, 목록에 없는 이름은 전부 닫는다(deny-by-default).
_FREE_ALLOW = ("list_", "get_", "search_", "find_", "query_", "compute_", "analyze_",
               "evaluate_", "predict_", "assess_", "compare_", "estimate_", "solve_",
               "recover_", "hybrid_", "semantic_", "fts_", "material_", "property_",
               "database_", "coverage_", "plot_", "check_", "describe_", "ashby_",
               "stress_", "top_", "catalog_", "agent_search", "daily_", "alert_")
_FREE_DENY = ("get_agent_session",)   # 페르소나 시스템프롬프트 원문은 조회 근거가 아니다


def _free_tool_ok(name: str) -> bool:
    n = (name or "").lower()
    return n not in _FREE_DENY and n.startswith(_FREE_ALLOW)


def _wrap_cached(tool, cache: dict):
    """같은 심의 안에서 같은 도구·같은 인자 재호출을 1회로 접는다 — 전문가 여럿이 같은 재료를
    조회하는 것이 정상 패턴이라 캐시가 곧 예산 절약이다(도구 객체는 이 심의 전용 로드라 안전)."""
    orig = tool.coroutine
    if orig is None:
        return tool

    async def cached(*a, **kw):
        try:
            key = (tool.name, json.dumps(kw or (a[0] if a and isinstance(a[0], dict) else {}),
                                         sort_keys=True, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001 — 키 직렬화 불가면 캐시 없이 그냥 호출
            return await orig(*a, **kw)
        if key in cache:
            return cache[key]
        out = await orig(*a, **kw)
        cache[key] = out
        return out

    tool.coroutine = cached
    return tool


async def _free_gather_one(g_agent, persona: dict, question: str, ctx: str, budget: int):
    """전문가 1명의 자유 조회(ReAct 1턴) — (키, 호출목록, 발언 주입 블록) 반환. 실패 비치명.
    발언(JSON 계약)과 분리된 이유: 도구 호출 모델은 왕복 후 스키마 계약을 곧잘 어긴다(실측) —
    조회는 여기서, 발언은 종전대로 도구 없는 텍스트 턴에서."""
    sysmsg = (f"당신은 '{persona['key']}' 전문가({str(persona.get('role', ''))[:280]}). "
              f"지금은 심의 발언 전의 데이터 조회 단계다. 필요한 조회를 도구로 직접 수행하라. "
              f"규칙: 최대 {budget}회. 대상을 이름으로 찾을 땐 목록·검색 도구 먼저 — 식별자"
              f"(id·test_id)를 추측해 넣지 마라(차단된다). 끝나면 '조회 요약:' 뒤에 핵심 수치만 "
              f"3줄 이내로 요약하라. 조회할 것이 없으면 '조회 불필요' 한 줄만 출력하라.")
    human = (f"[심의 주제]\n{question}\n\n[지금까지의 논의·근거(발췌)]\n{ctx}\n\n"
             f"당신 발언에 필요한 조회를 지금 수행하라.")
    calls, summary = [], ""
    try:
        res = await g_agent.ainvoke({"messages": [("system", sysmsg), ("user", human)]},
                                    config={"recursion_limit": budget * 2 + 5})
        args_by_id = {}
        for m in (res or {}).get("messages") or []:
            for tc in (getattr(m, "tool_calls", None) or []):
                args_by_id[tc.get("id")] = tc.get("args")
            mtype = getattr(m, "type", "")
            body = getattr(m, "content", "")
            if isinstance(body, list):
                body = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in body)
            if mtype == "tool":
                calls.append((getattr(m, "name", "?") or "?",
                              json.dumps(args_by_id.get(getattr(m, "tool_call_id", None)) or {},
                                         ensure_ascii=False, default=str)[:140],
                              str(body)))
            elif mtype == "ai" and isinstance(body, str) and body.strip():
                summary = body.strip()
    except Exception as exc:  # noqa: BLE001 — 조회 실패가 발언을 막지 않는다
        print(f"[deliberation] free-gather 실패({persona.get('key')}): {exc!r}")
    calls = calls[:budget]
    # 빈 결과([]·{}·null)는 에러는 아니지만 근거도 아니다 — 주입하면 "조회했으나 없음"이
    # 수치 근거처럼 보인다. 이력(SSE)에는 남기되 발언 주입 블록에서는 뺀다.
    def _has_content(b: str) -> bool:
        return _delib_tool_result_ok(b) and b.strip() not in ("[]", "{}", "null", "")
    good = [(n, ap, b) for n, ap, b in calls if _has_content(b)]
    if not good:
        return persona["key"], calls, ""
    block = "\n".join(f"- {n}({ap}): {b[:900]}" for n, ap, b in good)[:3500]
    if summary and not summary.startswith("조회 불필요"):
        block += f"\n(전문가 자체 요약) {summary[:400]}"
    return persona["key"], calls, block

def _dom_of(key: str) -> str:
    """페르소나 키의 도메인 접두사 — disp-burnin → disp. 커버리지 판정의 단위다."""
    k = str(key or "")
    return k.split("-", 1)[0] if "-" in k else k


async def _restore_role(tools: dict, key: str, fallback: str = "") -> str:
    """페르소나 역할 원본을 get_agent_session 으로 복원한다(실패 시 fallback)."""
    try:
        sess = _first_dict(_parse_json(await _call(tools, "get_agent_session", {"agent_type": key})))
        sd = _first_dict(sess.get("data", sess))
        full = sd.get("description") or sd.get("system_prompt") or ""
        if full:
            return full[:_ROLE_CLIP] if _ROLE_CLIP > 0 else full
    except Exception:  # noqa: BLE001 — 실패해도 제공된 role/key 로 참여
        pass
    return fallback


async def _discover(tools: dict, q: str, limit: int, exclude: set = frozenset(),
                    origin: str = "primary") -> list:
    """recommend_agents 로 좌석을 발굴해 [{key, role, origin}] 로 돌려준다.

    반대 도메인 좌석(F1)·이어하기 재심사(F10)가 같은 코드를 쓴다 — 질의문만 다르다.
    도구 실패·빈 결과는 비치명적으로 빈 목록을 돌려준다. 좌석이 없어도 심의는 진행하고,
    그 사실은 origin 표시가 없는 것으로 결정문에 남는다."""
    try:
        recd = _parse_json(await _call(tools, "recommend_agents", {"q": q}))
    except Exception:  # noqa: BLE001
        return []
    if isinstance(recd, list):
        items = recd
    elif isinstance(recd, dict):
        items = recd.get("recommendations") or recd.get("agents") or recd.get("data") or []
    else:
        items = []
    out = []
    for it in (items if isinstance(items, list) else []):
        if len(out) >= limit:
            break
        it = _first_dict(it)
        key = it.get("agent_type") or it.get("id")
        if not key or key in exclude or any(p["key"] == key for p in out):
            continue
        out.append({"key": key, "role": await _restore_role(tools, key), "origin": origin})
    return out


async def _counter_seats(tools: dict, llm, question: str, seated: list, limit: int) -> list:
    """착석 좌석이 보지 못하는 원인 축을 명명하게 하고, 그 축의 짧은 질의로 좌석을 발굴한다.

    원 질문에 "이 분야들 밖"을 덧붙이는 역질의는 작동하지 않는다 — 질의의 대부분이 원 질문이라
    임베딩 이웃이 그대로 돌아온다(실측 2026-08-07: S26U 화두 역질의 상위 5 중 4가 기존 좌석과
    동일, 신규 1명도 같은 도메인). 부정은 검색이 아니라 추론으로 처리해야 한다.

    같은 실측에서 짧은 도메인 질의는 정확히 다른 좌석을 돌려줬다 — '봉지 수분 산소 침투 신뢰성'
    → rel-chemical-corrosion, '접착제 OCA 경화 잔류물' → disp-module-bonding. 풀에는 있는데
    질의가 못 닿고 있었을 뿐이다.

    도메인 신규성을 강제한다 — 이미 착석한 도메인의 후보는 버린다. 그러지 않으면 축만 바꾼
    같은 도메인 좌석이 들어와 커버리지가 그대로다."""
    seated_keys = {p["key"] for p in seated}
    seated_domains = {_dom_of(k) for k in seated_keys}
    try:
        raw = await _llm_text(
            llm,
            "당신은 심의체 좌석 구성을 검토하는 조정자입니다. 짧고 건조하게 답하세요.",
            f"[질문]\n{question[:1500]}\n\n[이미 착석한 전문가]\n{', '.join(sorted(seated_keys))}\n\n"
            "이 전문가들의 담당 범위로는 보이지 않는 원인 축을 3개 제시하라. "
            "각 축을 전문가 검색에 쓸 짧은 명사구 한 줄로만 쓰고(8단어 이내), 설명·번호·기호 없이 "
            "줄바꿈으로 구분하라. 이미 착석한 분야를 다시 쓰지 마라.")
    except Exception:  # noqa: BLE001 — 축 명명 실패는 비치명적. 반대 도메인 좌석 없이 진행한다.
        return []
    axes = [ln.strip(" -•·\t") for ln in (raw or "").splitlines() if ln.strip()][:3]
    out = []
    for axis in axes:
        if len(out) >= limit:
            break
        for c in await _discover(tools, axis, limit=3, exclude=seated_keys, origin="counter"):
            d = _dom_of(c["key"])
            if d in seated_domains:
                continue          # 축만 바꾼 같은 도메인 — 커버리지가 늘지 않는다
            c["axis"] = axis
            out.append(c)
            seated_domains.add(d)
            seated_keys.add(c["key"])
            break                 # 축당 1명 — 축 다양성이 인원수보다 중요하다
    return out[:limit]


def _cont_block(summary: str, non_negotiables: list, human_note: str) -> str:
    """이어하기 컨텍스트 블록 — 요약·양보 불가 조항·사람 의견.

    조항을 요약 문자열과 분리해 싣는 이유는 승계 보장이다. 요약은 호출자가 만드는 자유
    텍스트라 조항이 빠져도 아무도 모르는데, 조항이 사라지면 이전 결정이 소리 없이 되돌아간다."""
    out = ""
    if summary:
        out += f"\n[이전 심의 요약 — 이어서 논의]\n{summary}\n"
    if non_negotiables:
        body = "\n".join(f"- {x}" for x in non_negotiables)
        out += (f"\n[이전 심의의 양보 불가 조항 — 이번 라운드에서도 구속력을 가진다]\n{body}\n"
                "이 조항을 뒤집으려면 어떤 새 근거 때문인지 반드시 명시하라. 근거 없는 폐기는 불인정.\n")
    if human_note:
        out += (f"\n[인간 검토자 의견 — 이번 심의에서 반드시 반영하고, 이 방향으로 논의를 진전시켜라]\n"
                f"{human_note}\n")
    return out


def _evidence_note(ev: dict) -> str:
    """결정문 헤더에 실을 근거 프로파일 — 형식의 권위와 근거의 강도를 일치시킨다."""
    note = ("근거 프로파일 — 도구 조회 {tool}건 · 지식카드 인용 {knowledge}건 · "
            "VOC {voc}건 · 사전 검색 {prepass}건").format(**ev)
    if not ev.get("tool"):
        note += " · 실측 데이터 0건(가설 단계)"
    return note


def _seat_note(personas: list) -> str:
    """좌석 구성을 의장 프롬프트용 한 줄로 만든다 — 결정문이 커버리지 한계를 스스로 밝히게 한다."""
    by = {}
    for p in personas:
        by.setdefault(p.get("origin", "primary"), []).append(p["key"])
    label = {"primary": "주 도메인", "counter": "반대 도메인", "carry": "유임", "new": "이어하기 신규"}
    parts = [f"{label.get(k, k)} {len(v)}명({', '.join(v)})" for k, v in by.items()]
    domains = sorted({(k.split("-", 1)[0] if "-" in k else k) for k in (p["key"] for p in personas)})
    return f"참여 좌석 — {' / '.join(parts)}. 착석 도메인 {len(domains)}종: {', '.join(domains)}."


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


# T1 근거 선주입 후보 — (도구명, 인자 빌더). 게이트웨이 스키마 확인 완료(2026-07-21, 전부 q/query 필수).
# LLM 에게 도구 선택을 맡기지 않는다('/보고서'와 같은 LLM 재량 금지 원칙) — 실패는 _tool_text_ok 로 걸러짐.
_EVIDENCE_TOOLS = (
    ("hybrid_search", lambda q: {"q": q, "top_k": 3}),
    ("search_knowledge", lambda q: {"q": q, "limit": 3}),
    ("search_reports", lambda q: {"q": q, "limit": 3}),
    ("query_rules", lambda q: {"query": q, "k": 3}),
)


async def _evidence_prepass(tools: dict, llm, question: str):
    """T1 정량 근거 선주입(DELIB_EVIDENCE_PREPASS) — 수치 인용을 '기억 인출'에서 '컨텍스트
    발췌'로 바꾸는 최대 깊이 레버(GLM 리뷰 §5). 지식·보고서 검색을 결정적으로 돌리고 LLM 1콜로
    주제 관련 정량 근거만 증류한다. 반환 (표시문, 주입 블록 또는 "", 호출 도구 리스트) —
    best-effort: 도구 실패·관련 근거 없음이면 빈 블록, 심의는 질문 기반으로 계속."""
    chunks, used = [], []
    for name, argf in _EVIDENCE_TOOLS:
        if name not in tools:
            continue
        out = await _call(tools, name, argf(question))
        if not isinstance(out, str):
            out = json.dumps(out, ensure_ascii=False) if out else ""
        if out and _tool_text_ok(out):
            used.append(name)
            chunks.append(f"### {name}\n{out[:2500]}")
        if len(chunks) >= 3:
            break
    if not chunks:
        return "", "", used
    distilled = str(await _llm_text(
        llm,
        "당신은 심의 준비 보조자입니다. 주어진 검색 결과에서만 발췌하고, 결과에 없는 수치를 만들지 마세요.",
        f"[심의 주제]\n{question}\n\n[도구 검색 결과]\n" + "\n\n".join(chunks)[:8000] + "\n\n"
        "주제와 직접 관련된 정량 수치·표준·사례만 불릿으로 추리세요. 각 불릿 끝에 (출처: 도구명) 표기. "
        "직접 관련된 정보가 없으면 '관련 근거 없음' 한 줄만 출력하세요.")).strip()
    if not distilled or "관련 근거 없음" in distilled[:40]:
        return "", "", used
    inject = (f"[정량 근거 (도구 조회 — 발언에 인용할 것. 여기 없는 수치는 지어내지 말고 "
              f"(경험칙) 표기)]\n{distilled[:3000]}\n")
    return distilled, inject, used


async def run_sim_deliberation(app, question: str, groups: list, req_opts=None):
    """시뮬레이션 심의 — 메커니즘을 좁힌 뒤 CAE 가 해석을 설계하는 2단 심의.

    라운드 루프를 건드리지 않고 기존 스트림을 두 번 돌린다. 리팩터링하면 회귀 위험이 크고,
    래퍼는 기존 계약(SSE 이벤트·저장 경로)을 그대로 쓴다.

    좌석 설계가 핵심이다 — CAE 전문가만 모으면 틀린 물리를 아름답게 계산한다. 그래서 2단은
    고정 CAE 좌석 + 1단 결론으로 발굴한 CAE 좌석 + 물리 유임 좌석으로 구성한다. 유임자는
    해석이 물리에서 떠나는 것을 막는 감시자다."""
    opts = _resolve_opts(req_opts)
    decision_a, personas_a, nn_a = "", [], []

    def _capture(chunk: bytes):
        """1단 스트림에서 결정문·좌석·양보 불가 조항을 가로챈다(2단 입력)."""
        nonlocal decision_a
        try:
            if not chunk.startswith(b"data:"):
                return
            ev = json.loads(chunk[5:].decode("utf-8").strip())
        except Exception:  # noqa: BLE001 — 파싱 실패는 무시(캡처 실패가 심의를 죽이지 않게)
            return
        kind = ev.get("kind")
        if kind == "decision":
            decision_a = ev.get("text") or decision_a
        elif kind == "personas":
            for pp in ev.get("personas") or []:
                if pp.get("key") and not any(x["key"] == pp["key"] for x in personas_a):
                    personas_a.append({"key": pp["key"], "role": pp.get("role") or ""})
        elif kind == "turn" and ev.get("non_negotiable"):
            nn = str(ev["non_negotiable"]).strip()
            if nn and nn not in nn_a:
                nn_a.append(nn)

    try:
        # ── 1단 — 메커니즘 심의 ──────────────────────────────────────────────
        yield _sse("status", {"step": "1단 — 메커니즘 심의", "tool": None})
        opts_a = _resolve_opts(req_opts)
        opts_a.chair_template = "mechanism"
        async for chunk in _deliberation_stream(app, question, groups, opts_a):
            _capture(chunk)
            yield chunk
        if not decision_a:
            yield _sse("error", {"code": "sim_no_mechanism",
                                 "message": "1단 메커니즘 심의가 결정문을 내지 못해 해석 설계로 넘어갈 수 없습니다."})
            yield _sse("done", {}); return

        # ── 좌석 전환 ────────────────────────────────────────────────────────
        yield _sse("status", {"step": "좌석 전환 — CAE 전문가 발굴", "tool": "recommend_agents"})
        tools = await _tools_by_name(app, groups)
        sim_seats, seen = [], set()

        async def _add(key, origin):
            if not key or key in seen:
                return
            seen.add(key)
            sim_seats.append({"key": key, "role": await _restore_role(tools, key), "origin": origin})

        for k in _SIM_FIXED_CAE:
            await _add(k, "new")
        # 1단 결론의 물리 축으로 발굴 — 현상 어휘가 아니라 계산의 성격으로 찾아야 CAE 가 잡힌다.
        for cand in await _discover(tools, f"{question}\n\n{decision_a[:1500]}\n\n수치해석 시뮬레이션 모델링",
                                    3, exclude=seen, origin="new"):
            await _add(cand["key"], "new")
        # 물리 유임 — 없으면 해석이 물리에서 떠난다. 1단 좌석 앞에서 강제로 채운다.
        for pa in personas_a[:max(1, _SIM_CARRY)]:
            await _add(pa["key"], "carry")
        n_carry = sum(1 for x in sim_seats if x["origin"] == "carry")
        yield _sse("status", {"step": f"2단 좌석 {len(sim_seats)}인 (CAE {len(sim_seats) - n_carry} · 물리 유임 {n_carry})",
                              "tool": None, "personas": [x["key"] for x in sim_seats]})

        # ── 2단 — 해석 설계 심의 ─────────────────────────────────────────────
        yield _sse("status", {"step": "2단 — 해석 설계 심의", "tool": None})
        opts_b = _resolve_opts(req_opts)
        opts_b.chair_template = "sim-plan"
        opts_b.continue_summary = decision_a[:8000]
        opts_b.continue_non_negotiables = nn_a[:12]
        opts_b.continue_personas = sim_seats
        opts_b.human_note = ("사내 보유 도구를 우선 검토하라. 파라미터 식별성 판정과 "
                             "이 해석이 답할 수 없는 것을 비워두지 마라.")
        sim_q = f"위 메커니즘을 계산으로 확인하고 설계 인자로 돌리기 위한 해석 설계 — 무엇을 어떤 도구로 계산할 것인가. 원 현상: {question}"
        async for chunk in _deliberation_stream(app, sim_q, groups, opts_b):
            yield chunk
    except Exception as exc:  # noqa: BLE001
        print(f"[sim-deliberation] fatal: {exc!r}")
        yield _sse("error", {"code": "sim_deliberation_error",
                             "message": f"시뮬레이션 심의 처리 중 오류: {str(exc)[:200]}"})
        yield _sse("done", {})


async def run_deliberation(app, question: str, groups: list, req_opts=None):
    """심의 SSE 진입점 — 내부 스트림이 어떤 예외로 죽어도 반드시 error+done 을 방출한다.
    (done 없이 끊기면 프론트가 '응답 생성 중'에 갇히고, error 계약이 어긋나면 '(응답이 없습니다)'로 보인다.)
    req_opts: 요청 단위 손잡이 오버라이드(웹 토글) — None 이면 env 기본값."""
    opts = _resolve_opts(req_opts)
    try:
        async for chunk in _deliberation_stream(app, question, groups, opts):
            yield chunk
    except Exception as exc:  # noqa: BLE001
        print(f"[deliberation] fatal: {exc!r}")
        yield _sse("error", {"code": "deliberation_error", "message": f"심의 처리 중 오류: {str(exc)[:200]}"})
        yield _sse("done", {})


async def _deliberation_stream(app, question: str, groups: list, opts=_DEFAULT_OPTS):
    """포털 챗 심의 모드의 SSE 제너레이터. 파이프라인(환기→근거→발굴→N라운드→의사결정→쉬운설명→기록)을\n    코드로 돌리고 진행을 스트리밍한다. '쉬운 설명'은 부가물이 아니라 정식 단계 — 결정문이 전문용어로\n    촘촘해 비전문가가 못 읽는 문제를 절차로 해소한다."""
    # 심의 전용 LLM(DELIB_TEMPERATURE 등 env 오버라이드, app.py lifespan) — 미설정이면 본 LLM 그대로.
    # 근거 계수(F2) — 결정문 헤더에 실을 프로파일. 형식의 권위가 근거의 강도를 넘지 않게,
    # 조회 0건 심의가 확정 결론과 같은 모습으로 유통되는 것을 막는다.
    ev_count = {"tool": 0, "knowledge": 0, "voc": 0, "prepass": 0}
    llm = getattr(app.state, "delib_llm", None) or app.state.llm
    # 요청 단위 타임아웃 오버라이드(웹 토글) — 기동값과 다르면 같은 파라미터로 새 인스턴스(구성만, 저렴).
    if opts.timeout_s is not None:
        cur_to = getattr(app.state, "delib_timeout_s", 0.0) or 0.0
        mk = getattr(app.state, "mk_delib_llm", None)
        if mk and opts.timeout_s != cur_to:
            try:
                llm = mk(opts.timeout_s)
            except Exception as exc:  # noqa: BLE001 — 팩토리 실패 시 기본 delib_llm 유지
                print(f"[deliberation] timeout override failed: {exc!r}")
    yield _sse("status", {"step": "심의 시작 — 전문 페르소나 발굴 중", "tool": "recommend_agents"})

    # 심의는 도구를 LLM 에 바인딩하지 않는다 — 전부 _call 로 부르고 결과를 **코드가 JSON 으로
    # 파싱**한다(페르소나 발굴·역할 로드·지정 도구 근거). 그러므로 프롬프트 보호용 절단을 걸면
    # 안 된다. 실측 사고: TOOL_RESULT_MAX=6000 인 박스에서 recommend_agents 응답(≈6.5KB)이 잘려
    # 파싱이 실패했고, 심의가 매번 no_personas 로 죽었다. 라운드에 들어가는 양은 주입 시점의
    # 별도 캡(_chunks 2000자 / tool_inject 5000자 / _ROLE_CLIP)이 이미 통제한다.
    from app import CATALOG_DESC_MAX, CATALOG_RESULT_MAX  # noqa: PLC0415 — 순환 방지용 늦은 import
    tools = await _tools_by_name(app, groups, CATALOG_RESULT_MAX, CATALOG_DESC_MAX)
    if not tools:
        yield _sse("error", {"code": "gateway_unavailable",
                             "message": "게이트웨이 MCP 도구를 불러오지 못했습니다(게이트웨이 확인)."})
        yield _sse("done", {}); return
    # 유령 ID 게이트를 심의에도 켠다 — /chat 라우트가 _agent_stream 을 거치지 않고 여기로 바로
    # 분기하므로 종전엔 fail-open 이었다. 실측: get_material(material_id=6061)·get_mat_card
    # (test_id=1 → SUS201) 처럼 지어낸 ID 가 그대로 나갔다. 출처는 질문 + 도구 결과에서 늘어난다.
    from app import _int_tokens, _turn_ids  # noqa: PLC0415
    _turn_ids.set(_int_tokens(question) | _int_tokens(opts.human_note or ""))

    # 자유 조회 에이전트 — 라운드 중 전문가가 직접 데이터를 조회한다(읽기 전용·예산 제한).
    # 결과가 LLM 프롬프트로 들어가므로 여기서는 **기본 캡**(TOOL_RESULT_MAX)으로 다시 로드한다 —
    # 위 무절단(CATALOG) 로드를 재사용하면 조회 한 번이 컨텍스트를 삼킨다. 유령 ID 게이트는
    # _turn_ids 가 이미 시드돼 있어 이 로드의 _prep_tool 래퍼에도 그대로 걸린다.
    g_agent = None
    if opts.free_tools:
        try:
            from langgraph.prebuilt import create_react_agent  # noqa: PLC0415
            _fcache: dict = {}
            _g = {n: _wrap_cached(t, _fcache)
                  for n, t in (await _tools_by_name(app, groups)).items() if _free_tool_ok(n)}
            if _g:
                g_agent = create_react_agent(llm, list(_g.values()))
                yield _sse("status", {"step": f"전문가 자유 조회 활성 — 읽기 전용 도구 {len(_g)}종, "
                                              f"1인당 최대 {opts.tool_budget}회", "tool": None})
        except Exception as exc:  # noqa: BLE001 — 자유 조회 불가여도 심의는 종전대로 진행
            print(f"[deliberation] free-tool 준비 실패: {exc!r}")

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
            ev_count["voc"] += 1
            yield _delib("evidence", source="SignalForge VOC", text=sf_display, included=bool(sf_inject))
            yield _sse("token", {"delta": stream_head})

    # 0.5) T1 정량 근거 선주입(DELIB_EVIDENCE_PREPASS) — 발언이 인용할 수치를 심의 전에 조달
    ev_inject = ""
    if opts.evidence_prepass:
        yield _sse("status", {"step": "정량 근거 수집 — 지식·보고서 검색", "tool": "hybrid_search"})
        try:
            ev_display, ev_inject, ev_used = await _evidence_prepass(tools, llm, question)
        except Exception:  # noqa: BLE001 — 근거 수집 실패가 심의를 죽이지 않게
            ev_display, ev_inject, ev_used = "", "", []
        if ev_used:
            yield _sse("status", {"step": "정량 근거 수집 완료", "tool": None, "tools_used": ev_used})
        if ev_display:
            ev_count["prepass"] += 1
            yield _delib("evidence", source="지식·보고서 검색", text=ev_display[:1500],
                         included=bool(ev_inject))

    # 0.7) 사용자 지정 도구 실호출(delib_opts.tools) — 자동 파이프라인 도구만 믿지 않고, 사용자가
    #      선정 패널에서 직접 고른 도구를 주제 기반 인자로 실제 호출해 정량 근거로 주입한다.
    #      인자는 LLM 이 도구 스키마를 보고 구성(불가하면 skip) — 도구별 실패는 비치명.
    tool_inject = ""
    if opts.delib_tools:
        _chunks, _used = [], []
        # 목록·검색 도구를 먼저 돌린다. 상세 도구(get_material·get_mat_card 등)는 식별자가 필요한데
        # 그 값은 목록 조회 결과에만 있다 — 순서가 반대면 상세 도구가 ID 를 지어낼 수밖에 없다.
        # 사용자가 패널에서 고른 순서는 의미가 없으므로(체크박스 순) 재정렬해도 잃는 게 없다.
        _ordered = sorted(opts.delib_tools,
                          key=lambda n: 0 if n.startswith(("list_", "search_", "find_")) else 1)
        for _tn in _ordered:
            _t = tools.get(_tn)
            if _t is None:
                yield _sse("status", {"step": f"지정 도구 없음: {_tn} — 건너뜀", "tool": _tn})
                continue
            _brief = _tool_schema_brief(_t)
            # 인자 구성 → 호출. 도구가 스키마 위반 등 에러 응답(ok:false/errors)을 주면 그 에러를
            # 피드백해 1회 재시도(self-repair) — 에러 JSON 이 '정량 근거'로 주입되는 것을 막는다.
            _good, _err_note = "", ""
            for _attempt in (1, 2):
                yield _sse("status", {"step": f"지정 도구 인자 구성: {_tn}"
                                              + (" (재시도)" if _attempt == 2 else ""), "tool": _tn})
                try:
                    _argd = _parse_json(await _llm_text(
                        llm, "당신은 도구 호출 계획자입니다. 반드시 유효한 JSON 하나만 출력하세요.",
                        f"[심의 주제]\n{question}\n\n[도구]\n{_tn}: {(getattr(_t, 'description', '') or '')[:300]}\n"
                        + (f"[인자 스키마]\n{_brief}\n" if _brief else "")
                        # 앞서 성공한 도구 결과를 같이 준다 — id·test_id 를 지어내지 않고 여기서
                        # 가져다 쓰라는 뜻이다. 이게 없으면 각 도구가 서로를 모른 채 호출돼
                        # 상세 도구가 식별자를 추측한다(실측: list_materials 가 id=19 를 줬는데
                        # 바로 다음 get_mat_card 가 test_id=1 을 찍었다).
                        + (f"\n[앞서 조회한 결과 — 여기 있는 id·test_id 를 그대로 쓰고 새로 지어내지 마라]\n"
                           + "\n".join(_chunks)[:2000] + "\n" if _chunks else "")
                        + (f"\n[직전 시도 오류 — 반드시 교정해 다시 구성하라]\n{_err_note}\n" if _err_note else "")
                        + "\n주제의 정량 분석에 맞게 이 도구를 1회 호출할 인자 JSON 을 출력하라. "
                          "스키마의 타입을 정확히 지켜라(숫자는 숫자로). 스키마에 없는 키 금지. "
                          "식별자(id·test_id 등)는 위 조회 결과에 있는 값만 쓰고, 없으면 추측하지 마라. "
                          "값을 알 수 없는 필수 인자가 있으면 {\"skip\": true} 만 출력."))
                except Exception:  # noqa: BLE001
                    _argd = None
                if not isinstance(_argd, dict) or _argd.get("skip"):
                    break
                yield _sse("status", {"step": f"지정 도구 호출: {_tn}", "tool": _tn,
                                      "detail": json.dumps(_argd, ensure_ascii=False)[:200]})
                _out = await _call(tools, _tn, _argd)
                if not isinstance(_out, str):
                    _out = json.dumps(_out, ensure_ascii=False, default=str) if _out is not None else ""
                if _out and _delib_tool_result_ok(_out):
                    _good = _out
                    break
                _err_note = (_out or "(빈 응답)")[:500]
            if _good:
                _chunks.append(f"### {_tn} ← {json.dumps(_argd, ensure_ascii=False)[:160]}\n{_good[:2000]}")
                _used.append(_tn)
                ev_count["tool"] += 1
                yield _delib("evidence", source=f"지정 도구 {_tn}", text=_good[:1500], included=True)
            else:
                yield _sse("status", {"step": f"지정 도구 실패/건너뜀: {_tn}", "tool": _tn})
        if _chunks:
            tool_inject = ("[사용자 지정 도구 정량 결과 (실호출 — 발언에 인용할 것. 여기 없는 수치는 "
                           "지어내지 말 것)]\n" + "\n\n".join(_chunks)[:5000] + "\n")
            yield _sse("status", {"step": f"지정 도구 근거 확보 — {len(_used)}건", "tool": None,
                                  "tools_used": _used})

    # 1) 발굴 — recommend_agents. 이어하기(continue_personas)는 이전 전문가를 유임시키되,
    #    사람 의견이 주제를 옮겼을 수 있으므로 실효 질문으로 재심사해 신규 좌석을 더한다(F10).
    yield _delib("stage", stage="discover")
    if opts.continue_personas:
        personas = [dict(p) for p in opts.continue_personas]
        for p in personas:
            p.setdefault("origin", "carry")
        # 지정/이어하기 전문가의 역할을 get_agent_session 으로 원본 복원한다 — 수동 추가는 role 이
        # 비어 오고(풀은 compact), 이어하기·추천은 소개용 축약본이라, 원본 역할로 채워 발언 품질을
        # auto 경로와 동일하게(_ROLE_CLIP 동일 적용) 유지한다. 실패 시 제공된 role 을 폴백.
        for p in personas:
            p["role"] = await _restore_role(tools, p["key"], p.get("role") or "")
        yield _sse("status", {"step": "지정 전문가 소집", "tool": "get_agent_session"})
        # 이어하기 좌석 재심사(F10) — 이어하기의 실효 질문은 원 질문이 아니라
        # '원 질문 + 이전 결론 + 사람 의견'이다. 좌석을 그 위에서 다시 뽑아 새 도메인을 연다.
        # 유임은 전원 유지하고 신규만 더한다(정원 확대) — 좌석을 빼면 그 도메인의 이전 발언에
        # 대한 책임 주체가 사라지기 때문. DELIB_RESCREEN=0 으로 종전 동작(재심사 없음) 복귀.
        if _RESCREEN and (opts.continue_summary or opts.human_note):
            eff_q = (f"{question}\n\n[이전 결론]\n{(opts.continue_summary or '')[:2000]}"
                     f"\n\n[사람 의견]\n{(opts.human_note or '')[:1000]}")
            _seated_dom = {_dom_of(p["key"]) for p in personas}
            added = [c for c in await _discover(tools, eff_q, _RESCREEN_SEATS * 3,
                                                exclude={p["key"] for p in personas}, origin="new")
                     if _dom_of(c["key"]) not in _seated_dom][:_RESCREEN_SEATS]
            if added:
                personas.extend(added)
                yield _sse("status", {"step": "이어하기 재심사 — 신규 좌석 " +
                                              ", ".join(p["key"] for p in added),
                                      "tool": "recommend_agents"})
    else:
        personas = await _discover(tools, question, N_PERSONAS, origin="primary")
        # 반대 도메인 좌석(F1) — 질문의 어휘가 도메인을 고정하면 그 밖의 가설은 발생 경로가 없다.
        # 착석 좌석이 못 보는 원인 축을 명명하게 하고 그 축으로 발굴한다(_counter_seats 주석에
        # 실측 근거). DELIB_COUNTER_SEATS=0 으로 종전 동작 복귀.
        if _COUNTER_SEATS and personas:
            counter = await _counter_seats(tools, llm, question, personas, _COUNTER_SEATS)
            if counter:
                personas.extend(counter)
                yield _sse("status", {"step": "반대 도메인 좌석 — " + ", ".join(
                    f"{p['key']}({p.get('axis', '')})" for p in counter),
                    "tool": "recommend_agents"})
    if len(personas) < 2:
        yield _sse("error", {"code": "no_personas",
                             "message": "관련 전문 페르소나를 충분히 찾지 못했습니다(AIDataHub 에이전트 등록 확인)."})
        yield _sse("done", {}); return
    yield _sse("status", {"step": "참여 전문가: " + ", ".join(p["key"] for p in personas), "tool": "get_agent_session",
                          "personas": [p["key"] for p in personas]})
    # 소개 카드용 역할 — 짧은 요약이 아니라 '이 전문가가 뭔지'가 보이게 넉넉히(프론트가 접어 표시).
    # totalRounds — 프론트 스테퍼/회의록이 라운드 수를 동적으로(r1..rN) 그리는 근거.
    # origin — primary/counter/carry/new. 프론트가 좌석 성격 라벨을 그리고, 결정문이 커버리지를 기록한다.
    yield _delib("personas", totalRounds=opts.rounds,
                 personas=[{"key": p["key"], "role": (p.get("role") or "")[:280],
                            "origin": p.get("origin", "primary")} for p in personas])
    seat_note = _seat_note(personas)

    # 페르소나별 주제 지식 주입(결정적 RAG) — 지식카드를 많이 가진 전문가일수록 "지금 주제와
    # 관련된 것"만 골라 들고 와야 한다(사용자 결정 2026-08-05). 자유 조회(모델 재량)와 달리
    # 항상 수행되는 기본기다. agent_search 가 전문가별 retrieval_config(top_k·score 임계값·
    # 태그 가중치)를 자동 적용하므로 검색 폭은 전문가 설정을 따르고, 주입량은 문자 예산으로 자른다
    # (카드 전문을 통째로 넣으면 인원수 × 라운드로 곱해져 컨텍스트가 폭발한다).
    knowledge_by_key: dict = {}
    if _env_int("DELIB_PERSONA_KNOWLEDGE", 1) and "agent_search" in tools:
        _kb_budget = _env_int("DELIB_KNOWLEDGE_BUDGET", 3500)

        def _hit_line(h):
            # agent_search hit 실측 형태(2026-08-05): {record_id, section_id, title,
            # section_title, snippet, score, tags, …} — 본문은 snippet 에 있다.
            if not isinstance(h, dict):
                return str(h)[:300]
            t = h.get("title") or ""
            sec = h.get("section_title") or ""
            x = h.get("snippet") or h.get("text") or h.get("excerpt") or h.get("summary") or ""
            head = f"{t}" + (f" › {sec}" if sec else "")
            if not (head or x):
                return json.dumps(h, ensure_ascii=False, default=str)[:300]
            return f"• [{head}] {str(x).strip()}"[:700]

        async def _kn_one(p):
            try:
                raw = await _call(tools, "agent_search",
                                  {"agent_type": p["key"], "q": question, "mode": "hybrid"})
                d = _parse_json(raw if isinstance(raw, str) else json.dumps(raw, default=str))
                hits = (d or {}).get("hits") if isinstance(d, dict) else None
                if not hits or (isinstance(d, dict) and d.get("refused")):
                    return p["key"], ""
                lines, total, seen = [], 0, set()
                for h in hits:
                    ln = _hit_line(h)
                    if ln in seen:  # 같은 레코드의 유사 섹션 반복 방지
                        continue
                    if total + len(ln) > _kb_budget:
                        break
                    seen.add(ln); lines.append(ln); total += len(ln)
                return p["key"], "\n".join(lines)
            except Exception as exc:  # noqa: BLE001 — 지식 검색 실패는 비치명(그 전문가만 미주입)
                print(f"[deliberation] 지식카드 검색 실패({p.get('key')}): {exc!r}")
                return p["key"], ""

        yield _sse("status", {"step": "페르소나별 지식카드 검색(주제 연관 발췌)", "tool": "agent_search"})
        for _k, _blk in await asyncio.gather(*[_kn_one(p) for p in personas]):
            if _blk:
                knowledge_by_key[_k] = _blk
                ev_count["knowledge"] += 1
                yield _delib("evidence", source=f"{_k} · 지식카드", text=_blk[:400], included=True)
        yield _sse("status", {"step": f"지식카드 주입 — {len(knowledge_by_key)}/{len(personas)}명 "
                                      f"관련 지식 확보", "tool": None})

    # 이어하기 컨텍스트 — 이전 심의 요약 + 사람 의견(스티어링). 사람 의견은 base 에 실려 매 라운드
    # 프롬프트에 자동 주입되므로 전 라운드에 걸쳐 방향을 잡는다. 사람 의견은 근거 카드로도 노출.
    # 이어하기 블록(F11) — 조항 승계를 요약 문자열에 의존하지 않는다.
    cont = _cont_block(opts.continue_summary, opts.continue_non_negotiables, opts.human_note)
    if opts.continue_non_negotiables:
        yield _delib("evidence", source="이전 심의 양보 불가 조항",
                     text="\n".join(f"- {x}" for x in opts.continue_non_negotiables)[:1500], included=True)
    if opts.human_note:
        yield _delib("evidence", source="인간 검토자 의견", text=opts.human_note[:1500], included=True)
    _tail = ((f"\n{sf_inject}" if sf_inject else "") + (f"\n{ev_inject}" if ev_inject else "")
             + (f"\n{tool_inject}" if tool_inject else ""))
    base = f"[심의 주제]\n{question}\n" + cont + _tail
    # 신규 좌석 앵커링 차단(F12) — 재심사로 새로 합류한 좌석에게 이전 결론을 먼저 읽히면
    # 동조 압력을 받아 '새 관점을 얻으려고 불렀다'는 목적이 사라진다. 1라운드에 한해 이전 요약을
    # 감추고 독립 판단을 받는다. 그 판단이 기존 결론과 충돌하면 그게 이어하기의 최대 소득이다.
    _cont_blind = ""
    if opts.human_note:
        _cont_blind = (f"\n[인간 검토자 의견 — 이번 심의에서 반드시 반영하라]\n{opts.human_note}\n")
    base_blind = (f"[심의 주제]\n{question}\n" + _cont_blind + _tail +
                  "\n[안내] 당신은 이번 회차에 새로 합류했다. 이전 논의 결과는 의도적으로 제공하지 "
                  "않는다 — 먼저 당신 도메인의 독립적 판단을 내라. 다음 라운드에서 이전 결론을 받는다.\n")
    _has_blind = any(p.get("origin") == "new" for p in personas) and bool(opts.continue_summary)

    # 3) 다중 라운드 심의 — N 라운드(1 초기 + N-2 심화 + 1 수렴). N=3 이면 종전 R1/R2/R3 와 동일.
    #    발언은 완료되는 순서대로 delib turn 으로 라이브 방출한다.
    N = opts.rounds

    def _kind(r):  # 라운드 성격 — 프롬프트·직렬화·렌더 분기의 단일 기준
        return "initial" if r == 1 else "converge" if r == N else "deepen"

    def _ser_kind(o, kind):  # 라운드 성격별 직렬화 키(다음 라운드 컨텍스트·회의록용)
        if kind == "initial":
            return _ser(o, ("lens", "reads", "recommendation", "concerns"), primary="lens")
        if kind == "deepen":
            return _ser(o, ("concede", "rebut", "deepen"), primary="deepen")
        return _ser(o, ("final_position", "non_negotiable", "vote", "stance"), primary="final_position")

    rebut_spec = ("반박(rebut)은 객체 배열 — 각 항목 {target: 상대 키, quote: 상대 발언에서 "
                  "20자 이상 그대로 복사한 문구, counter: 반박 논지, basis: 수치·표준·실패모드}. "
                  "인용 없는 반박은 불인정. JSON {concede:[],rebut:[{target,quote,counter,basis}],deepen} 로."
                  if opts.rebut_quote else
                  "JSON {concede:[],rebut:[],deepen} 로.")

    rounds_data = []          # [(turns_list, transcript_str), ...] 라운드별
    r1_by_key = {}            # 1라운드 데이터(앵커용) — 1R 완료 후 채움

    for rnd in range(1, N + 1):
        kind = _kind(rnd)
        prev_list, prev_t = rounds_data[-1] if rounds_data else ([], "")
        prev_no, prev_kind = rnd - 1, (_kind(rnd - 1) if rnd > 1 else "")
        rlabel = ("도메인별 초기 입장" if kind == "initial"
                  else "수렴·최종 입장" if kind == "converge" else "상호 반박·수치 심화")
        yield _delib("stage", stage=f"r{rnd}", n=len(personas))
        yield _sse("status", {"step": f"{rnd}라운드 — {rlabel}", "tool": None})

        if kind == "initial":
            prompt_fn = lambda p: ((base_blind if (_has_blind and p.get("origin") == "new") else base) +
                "\n당신의 관점(lens — 2~4문장, 구체적으로), 위 주제·근거에 실제로 주어진 정보와 당신 도메인의 "
                "확립된 표준·경험칙에 대한 해석(reads — 배열, 접근할 수 없는 데이터·수치를 지어내지 말고 "
                "경험칙에는 (경험칙) 표기), 권장안(recommendation — 2~4문장), "
                "이 주제에서 당신 도메인이 놓칠 리스크(concerns — 최소 2개), 현재 입장 한 줄 요약(position_short)을 "
                "JSON {lens,reads:[],recommendation,concerns:[],position_short} 로. 한 줄 요약은 position_short 에만 — "
                "나머지 필드를 한 줄로 줄이지 마세요.")
            required, validator_fn, render = ("lens", "recommendation"), None, 1

        elif kind == "converge":
            def prompt_fn(p, _pt=prev_t, _pno=prev_no):
                # 입장 앵커 재주입(DELIB_ANCHOR) — 약한 모델의 수렴 라운드 동조 붕괴(전원이 평균 입장으로
                # 뭉개짐) 방어. 자기 1R 핵심을 되돌려주고, 입장 변경엔 새 근거 명시를 요구(GLM 리뷰 §5).
                anchor = ""
                if opts.anchor and r1_by_key.get(p["key"]):
                    aser = _ser(r1_by_key[p["key"]], ("lens", "recommendation"), primary="lens")
                    anchor = (f"\n[당신의 1라운드 입장(앵커)]\n{aser}\n다수 의견에 동조해 당신 도메인의 "
                              "제약을 희석하지 마세요 — 입장을 바꾼다면 어떤 새 근거 때문인지 "
                              "final_position 에 명시하세요.\n")
                return (base + f"\n[{_pno}라운드 전원]\n{_pt}\n" + anchor +
                        "\n직전 라운드를 반영해 최종 입장(final_position — 2~4문장)·절대 양보 못 하는 "
                        "제약(non_negotiable)·최종 권장(vote)으로 수렴하고, "
                        "형성된 다수 의견에 대한 당신의 스탠스(동의/조건부 동의/반대)와 최종 입장 한 줄 요약을 밝혀라. "
                        "JSON {final_position,non_negotiable,vote,stance,position_short} 로.")
            required, validator_fn, render = ("final_position", "vote"), None, 3

        else:  # deepen — 직전 라운드에 반박·심화. 교차심문(cross_exam)·인용계약(rebut_quote)은 직전 라운드 대상.
            prev_keys = [o["persona"] for o in prev_list]
            prev_by_key = {o["persona"]: o for o in prev_list}

            def _ctx(p, _pl=prev_list, _pk=prev_keys, _pbk=prev_by_key, _pt=prev_t,
                     _pno=prev_no, _pkind=prev_kind):
                # (컨텍스트, 인용 실재 검증 대상 문자열)
                if not (opts.cross_exam and len(_pl) >= 2):
                    return f"[{_pno}라운드 전원]\n{_pt}", _pt
                tkey = (_pk[(_pk.index(p["key"]) + 1) % len(_pk)] if p["key"] in _pk else _pk[0])
                tser = _ser_kind(_pbk[tkey], _pkind)
                others = "\n".join(
                    f"• {o['persona']}: {_clip_sent(o.get('position_short') or o.get('deepen') or o.get('lens'), 160)}"
                    for o in _pl if o["persona"] != tkey)
                ctx = (f"[당신의 지정 반박 표적: {tkey} — {_pno}라운드 발언 전체]\n{tser}\n\n"
                       f"[다른 전문가 한 줄 입장]\n{others}\n\n"
                       f"표적({tkey})의 논증에서 특정 주장을 골라 반박하세요. 다른 전문가 언급은 자유.")
                return ctx, tser

            def prompt_fn(p, _ctx=_ctx):
                ctx, _ = _ctx(p)
                return (base + f"\n{ctx}\n\n다른 전문가 입장에 수용(concede)·반박(rebut — 최소 1개, "
                        f"근거: 수치·표준·실패모드)하고 당신 핵심 주장을 한 단계 더 깊게(deepen — 3문장 이상, "
                        f"두루뭉술 금지). {rebut_spec}")

            _where = (f"당신의 지정 반박 표적의 {prev_no}라운드 발언 전체"
                      if opts.cross_exam and len(prev_list) >= 2 else f"위 [{prev_no}라운드 전원] 텍스트")
            validator_fn = ((lambda p, _c=_ctx, _w=_where: _quote_validator(_c(p)[1], _w))
                            if opts.rebut_quote else None)
            required, render = ("deepen", "rebut", "concede"), 2

        # 자유 조회 — 발언 전에 각 전문가가 직접 데이터를 조회한다. 수렴 라운드는 새 조회 없이
        # 기존 논의를 정리하는 단계라 건너뛴다. 병렬 실행하되 완료 순서대로 조회 이력을 흘린다 —
        # "수치는 기억이 아니라 조회 기록" 계약이 근거 패널에 그대로 남는다.
        _gathered = {}
        if g_agent is not None and kind != "converge":
            _gctx = (prev_t[-1800:] if (rnd > 1 and prev_t) else base[:1800])
            _gt = [asyncio.ensure_future(
                _free_gather_one(g_agent, p, question, _gctx, opts.tool_budget)) for p in personas]
            for _fut in asyncio.as_completed(_gt):
                try:
                    _k, _calls, _blk = await _fut
                except Exception:  # noqa: BLE001
                    continue
                for _tn, _ap, _out in _calls:
                    yield _sse("status", {"step": f"{_k} 조회: {_tn}", "tool": _tn, "detail": _ap})
                    if _delib_tool_result_ok(_out) and _out.strip() not in ("[]", "{}", "null", ""):
                        ev_count["tool"] += 1
                        yield _delib("evidence", source=f"{_k} · {_tn}", text=_out[:500],
                                     included=True)
                _gathered[_k] = _blk
        # 페르소나별 주입 — 지식카드 발췌(결정적 RAG, 매 라운드 기본기)와 자유 조회 결과(모델
        # 재량)를 함께 얹는다. 수렴 라운드는 새 재료 없이 정리만 하므로 지식카드도 생략.
        _kn = knowledge_by_key if kind != "converge" else {}
        if any(_kn.values()) or any(_gathered.values()):
            _base_fn = prompt_fn

            def prompt_fn(p, _f=_base_fn, _kn=_kn, _g=_gathered):
                out = _f(p)
                kb = _kn.get(p["key"]) or ""
                if kb:
                    out += ("\n\n[당신의 지식카드에서 — 이 주제 관련 발췌. 발언의 1차 근거로 "
                            "인용하세요]\n" + kb)
                blk = _g.get(p["key"]) or ""
                if blk:
                    out += ("\n\n[당신이 직접 조회한 결과 — 발언에 인용하세요. 여기·공용 근거에 "
                            "없는 수치는 (경험칙) 표기]\n" + blk)
                return out

        cur = []
        async for o in _round_live(llm, personas, prompt_fn, rnd, required=required,
                                   validator_fn=validator_fn, opts=opts):
            cur.append(o)
            extra = {}
            if render == 1:
                extra = {"position": _clip_sent(o.get("position_short"), 90)}
            elif render == 3:
                # non_negotiable 을 턴에 실어 보내는 이유 — 프론트가 이어하기 호출에 조항을 승계해야
                # 이전 결정이 소리 없이 되돌아가는 것을 막는다(F11). say 는 표시용으로 절단되므로
                # 승계용 원문을 따로 준다.
                extra = {"position": _clip_sent(o.get("position_short"), 90),
                         "stance": _norm_stance(o.get("stance")),
                         "non_negotiable": str(o.get("non_negotiable") or "")[:1200]}
            yield _delib("turn", round=rnd, persona=o["persona"], say=_say_of(render, o), **extra)
        if rnd == 1:
            r1_by_key = {o["persona"]: o for o in cur}
        ct = "\n".join(f"• {o['persona']}: {_ser_kind(o, kind)}" for o in cur)
        rounds_data.append((cur, ct))
        # 인간 체크포인트(F7) — 초기 라운드에서 멈추고 사람에게 넘긴다. 결정문을 만들지 않고,
        # 대신 전원 초기 입장을 이어하기의 출발점으로 내려보낸다(프론트의 이어하기 폼이 그대로 쓴다).
        if opts.stop_after_round == 1 and rnd == 1:
            _cp = (f"[체크포인트 — {rnd}라운드(초기입장)에서 멈춤]\n{seat_note}\n\n"
                   f"{ct}\n\n빠진 관점이나 추가 관측이 있으면 의견으로 넣어 이어가라. "
                   "좌석 재심사가 그 방향에 맞는 도메인을 불러온다.")
            yield _delib("decision", text=_cp)
            yield _sse("status", {"step": "체크포인트 — 사람 검토 대기(의견을 넣어 이어가기)", "tool": None})
            yield _sse("result", {"type": "text", "content": _cp})
            yield _sse("done", {})
            return

    # 마지막 라운드(수렴) 데이터 — 집계·tally 용
    last_list, last_t = rounds_data[-1]

    # 4) 의사결정문 합성
    yield _delib("stage", stage="decide")
    yield _sse("status", {"step": "의사결정문 합성 중", "tool": None})
    # 출처 태깅(DELIB_CHAIR_CITE) — 절충형 뭉개기(전 의견 나열 병합)를 가시화·감사 가능하게.
    cite_note = ("각 결정사항 항목 끝에 근거가 된 라운드 발언 출처를 [R2·페르소나키] 형식으로 표기하고, "
                 "어느 라운드에도 근거가 없는 항목은 [무근거] 로 표기하라. "
                 if opts.chair_cite else "")
    ev_note = _evidence_note(ev_count)
    chair_items = _CHAIR_ITEMS.get(opts.chair_template, _CHAIR_ITEMS["default"])
    doc_title = "해석 계획서" if opts.chair_template == "sim-plan" else "의사결정문"
    chair_sys = "당신은 심의체 의장입니다. 한국어 엔지니어링 톤으로 명확하게."
    _rtag = lambda r: "초기입장" if r == 1 else "최종" if r == N else "심화"
    rounds_block = "\n\n".join(
        f"[{i + 1}R {_rtag(i + 1)}]\n{_cap_ctx(t)}" for i, (lst, t) in enumerate(rounds_data))
    chair_human = (
        base + f"\n{rounds_block}\n\n"
        f"[{seat_note}]\n[{ev_note}]\n"
        "## 의사결정문 — 맨 위에 위 [근거 프로파일] 줄을 그대로 한 줄로 옮겨 적고, "
        + ("제목 앞에 [가설 단계] 를 붙이고 첫 문단에 '본 결정은 측정이 아니라 관측 패턴 추론이다'를 "
           "명시하라. " if ev_count["tool"] == 0 else "") +
        "(0) 참여 도메인과 커버리지 한계 — 위 좌석 구성을 한 문단으로 기록하고, "
        "이 문제에 관련되나 착석하지 않은 인접 도메인이 있으면 명시하라(없으면 없다고 쓰라), "
        "(1) 결정사항(번호매김·실행가능), (2) 합의 근거(라운드로 어떻게 수렴했는지), "
        "(3) 소수의견과 처리 — 페르소나가 명시한 non_negotiable(양보 불가 제약)과 stance 를 반영하되, "
        "명시하지 않은 페르소나는 '미표명'으로 기록하고 지어내지 마라, "
        "(4) 미해결 쟁점+담당·다음 액션, (5) 신뢰도·전제. " + cite_note +
        "라운드별 심화·수렴을 드러내라.")
    # best-of-n(DELIB_CHAIR_BESTOF≥2) — temp>0 분산의 상위 꼬리를 심판이 회수. 의장 1곳 한정이
    # 체감 대비 최저 비용(GLM 리뷰 §5). temp 0 에선 후보가 동일해 무의미 — env kit 주석 참조.
    n_cand = max(1, opts.chair_bestof)
    if n_cand == 1:
        decision = await _llm_text(llm, chair_sys, chair_human)
    else:
        raw_cands = await asyncio.gather(
            *[_llm_text(llm, chair_sys, chair_human) for _ in range(n_cand)],
            return_exceptions=True)
        cands = [c for c in raw_cands if isinstance(c, str) and c.strip()]
        if not cands:
            raise RuntimeError("의장 의사결정문 합성 실패(후보 전멸)")
        if len(cands) == 1:
            decision = cands[0]
        else:
            pick = _parse_json(await _llm_text(
                llm, "당신은 심의 기록 심사자입니다. 반드시 유효한 JSON 하나만 출력하세요.",
                "\n\n".join(f"[후보 {i + 1}]\n{c[:4000]}" for i, c in enumerate(cands)) +
                "\n\n위 의사결정문 후보 중 (a) 판정 수치가 구체적이고 (b) 라운드 발언에 접지되며 "
                "(c) 소수의견이 보존되고 (d) 실행 가능한 것 하나를 고르세요. "
                'JSON {"best": 후보번호} 로만.')) or {}
            try:
                b = int(pick.get("best", 1))
                # 범위 밖(0·음수 — 음수 인덱싱으로 폴백을 조용히 우회 — ·후보수 초과)은 첫 후보로
                decision = cands[b - 1] if 1 <= b <= len(cands) else cands[0]
            except (ValueError, TypeError):
                decision = cands[0]

    # 4b) 핵심 요약(TL;DR) — 의사결정문을 3~5줄로 증류해 맨 앞에 붙인다. 권고 섹션·챗 답변·이어하기
    #     요약이 모두 결론 요지로 시작하게(보고서를 열자마자 결론이 보이도록).
    yield _sse("status", {"step": "핵심 요약 생성 중", "tool": None})
    try:
        _summary = (await _llm_text(
            llm, "당신은 심의체 의장입니다. 군더더기 없이 핵심만.",
            "다음 의사결정문을 3~5줄 핵심 요약으로 압축하라 — 각 줄 '- '로 시작하는 한 문장 불릿. "
            "① 최종 결론 한 줄 ② 핵심 근거 1~2개(가능하면 수치) ③ 소수의견/합의 여부. "
            f"머리말·제목 없이 불릿만.\n\n{decision[:6000]}")).strip()
    except Exception as exc:  # noqa: BLE001 — 요약 실패해도 의사결정문은 그대로 저장
        print(f"[deliberation] summary failed: {exc!r}")
        _summary = ""
    if _summary:
        decision = f"■ 핵심 요약\n{_summary}\n\n{decision}"

    # 4c) 쉬운 설명 — 의사결정문은 전문 용어·수치로 촘촘해 비전문가·경영층이 '그래서 뭘 하라는
    #     건지' 못 읽는다. 맨 뒤에 한마디 결론 + 왜 그런지 + 당장/다음/금지 를 평이한 말로 붙인다.
    # 정식 절차로 승격 — 스테퍼에 단계로 표시되고, 구조화 이벤트로도 방출된다.
    yield _delib("stage", stage="explain")
    yield _sse("status", {"step": "쉬운 설명 — 비전문가용 정리", "tool": None})
    try:
        _plain = (await _llm_text(
            llm, "당신은 어려운 기술 결정을 비전문가에게 설명하는 사람입니다. 쉬운 말로, 과장 없이.",
            "다음 의사결정문을 처음 보는 사람도 이해하게 정리하라. 형식:\n"
            "### 한마디로\n(무엇을 하라는 것인지 한 문장)\n"
            "### 왜 그런가\n(핵심 근거 2~3개 — 수치가 있으면 쉬운 말로 풀어서)\n"
            "### 당장 할 일 / 다음에 할 일 / 하지 말 것\n(각 2~4개 불릿, 전문용어는 괄호로 풀어쓰기)\n"
            "새로운 내용을 지어내지 말고 원문에 있는 것만 쉽게 바꿔라.\n\n"
            f"{decision[:7000]}")).strip()
    except Exception as exc:  # noqa: BLE001 — 실패해도 의사결정문은 그대로
        print(f"[deliberation] plain summary failed: {exc!r}")
        _plain = ""
    if _plain:
        decision = f"{decision}\n\n---\n\n■ 쉬운 설명\n{_plain}"
        # 별도 이벤트로도 내보내 프론트가 결정문과 분리된 카드로 렌더할 수 있게 한다.
        yield _delib("plain", text=_plain)

    # 5) Report Archive 기록(옵션·best-effort — 템플릿 있으면)
    yield _delib("stage", stage="report")
    yield _sse("status", {"step": "Report Archive 보고서 저장 중", "tool": "create_report_draft",
                          "detail": f"심의 — {question[:50]}"})
    report_note = ""
    rid = None
    try:
        # 회의록(대화체) — Claude MCP 경로든 챗 경로든 RA 웹에서 회의가 그대로 읽히게 발언을 싣는다.
        transcript = []
        for i, (arr, _t) in enumerate(rounds_data):
            r = i + 1
            lbl = ("도메인별 초기 입장" if r == 1 else "수렴·최종 입장" if r == N else "상호 반박·심화")
            transcript.append(f"— {r}라운드 — {lbl} —")
            render = 1 if r == 1 else 3 if r == N else 2
            # 기록 층위 — 버블용 절단문이 아니라 온전한 발언(full=True)을 남긴다.
            # _TRANSCRIPT_CLIP 은 저장 API 보호용 여유 상한(기본 2000자)일 뿐.
            transcript += [f"[{o['persona']}] {_say_of(render, o, full=True)[:_TRANSCRIPT_CLIP]}" for o in arr]
        results_t = rounds_data[N - 2][1] if N >= 3 else rounds_data[0][1]   # 마지막 심화 라운드, 없으면 1R
        blocks = {
            "background": [f"심의 주제: {question}"]
                          + ([f"최근 고객 불만 신호(SignalForge VOC) 환기:\n{sf_inject[:1200]}"] if sf_inject else [])
                          + ([f"정량 근거(도구 조회 선주입):\n{ev_inject[:1200]}"] if ev_inject else []),
            "results": [results_t[:1500]],
            # 맨 앞 '핵심 요약' 문단 + 의사결정문. 요약이 한 슬롯 먹어도 본문이 안 잘리게 +1.
            "recommendation": [p.strip() for p in decision.split("\n\n") if p.strip()][:20],
            "minutes": [f"참여: {', '.join(p['key'] for p in personas)}",
                        f"{N}라운드 심의(1R 초기→…→{N}R 수렴)."] + transcript[:40],
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
    tally = {"agree": 0, "conditional": 0, "oppose": 0, "total": len(last_list)}
    for o in last_list:
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

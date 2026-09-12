"""HWAX Agent Server — LangGraph ReAct + group-scoped MCP tool fan-out.

The portal is a thin proxy; the real model call + tool use live here (계획서 §3).
Flow: ChatDock → portal /agent/chat → THIS → (vLLM for the LLM, MCP gateway for tools).

A LangGraph ReAct agent runs the loop: the LLM (Qwen2.5-7B on vLLM, tool-calling
enabled) decides when to call MCP tools; tools come from the HWAX MCP Gateway via
langchain-mcp-adapters. We stream the run as the portal's §5 SSE contract:
  status (incl. tool calls) → token×N → result → done (or error).

Authorization (계획서 §4): the portal hands off the caller's `groups` (JWT claim, from
SAML memberOf). The *gateway* owns group-based tool filtering (it knows each tool's
backend); this server simply forwards the caller's groups to the gateway on every request
via the `X-HWAX-Groups` header, then builds the ReAct agent from whatever tools the
gateway returns for those groups. The tool set is therefore per-caller, so we load tools
per request and cache the compiled agent by the caller's group-set.

If MCP/LLM wiring is unavailable the server still answers — it just won't have tools.

Env:
  VLLM_BASE_URL   OpenAI-compatible base (default http://127.0.0.1:8000/v1)
  VLLM_MODEL      served model name (default qwen2.5-7b-dev)
  MCP_CONFIG      path to a JSON file (gitignored — holds the gateway token) of per-server
                  config, e.g. {"gateway":{"url":"http://127.0.0.1:9110/mcp",
                  "transport":"streamable_http","headers":{"Authorization":"Bearer hwaxgw_…"}}}.
                  Takes precedence over MCP_SERVERS.
  MCP_SERVERS     fallback: comma-separated name=url pairs (no per-server auth headers).
"""

import hashlib
import itertools
import asyncio
import contextvars
import json
import logging   # _detach_stream 의 오류 로깅이 쓰는데 임포트가 없었다 — 심의가 터지면 오류 처리 자체가 NameError 로 죽었다
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from evidence import (fit_document, sig_numbers as _sig_numbers,
                      unsourced_numbers as _unsourced_numbers)
from deliberation import (
    N_PERSONAS,
    _PHANTOM_ID_MARK,
    _call,
    _agent_search_hits,
    _tool_schema_brief,
    _env_float,
    _env_int,
    _first_dict,
    _parse_json,
    _parse_json_multi,
    _tool_text_ok,
    _has_defect_topic,
    _call,
    _llm_text,
    _tools_by_name,
    is_deliberation,
    is_operator,
    is_sim_deliberation,
    is_test_plan,
    is_report_save,
    run_deliberation,
    run_sim_deliberation,
    run_test_plan,
    run_report_save,
    strip_report_trigger,
    strip_trigger,
    strip_sim_trigger,
    strip_test_plan_trigger,
)
from thinking import is_thinking, run_thinking, strip_thinking_trigger
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "qwen2.5-7b-dev")
# 인증 있는 OpenAI 호환 서버(상암 B300 등)용 — 미설정이면 "EMPTY"(로컬 vLLM 무인증과 동일).
VLLM_API_KEY = os.environ.get("VLLM_API_KEY") or "EMPTY"
MCP_SERVERS = os.environ.get("MCP_SERVERS", "")
# 에이전트 캐시 상한. 사용자 × 30분 창 × 질의선택 조합이라 상한이 없으면 계속 는다.
AGENT_CACHE_MAX = int(os.environ.get("AGENT_CACHE_MAX", "64"))
MCP_CONFIG = os.environ.get("MCP_CONFIG", "")
from urllib.parse import quote  # 그룹 헤더 안전 인코딩

GROUPS_HEADER = "X-HWAX-Groups"  # gateway reads this to filter tools by the caller's groups
USER_HEADER = "X-HWAX-User"      # 호출자 이메일 — 게이트웨이가 사용자별 백엔드 자격증명으로 갈아탄다
SYSTEM_PROMPT = (
    "당신은 HWAX 포털의 어시스턴트입니다. 반드시 한국어로만 답하세요 — "
    "중국어·영어 등 다른 언어로 절대 전환하지 마세요. 간결·정확하게. "
    # 트리거 낱말이 '조회·분석' 뿐이면 '만들어줘·그려줘·비교해줘' 발화에 이 규칙이 걸리지 않는다.
    # 실측: 날조 3건의 발화 동사가 정확히 그 셋이었고, 유일하게 정상이던 1건만 '뽑아줘'(조회형)였다.
    "데이터·수치를 달라는 요청은 반드시 제공된 도구를 사용하세요 — '조회·분석'뿐 아니라 "
    "'만들어줘·그려줘·표로 정리해줘·비교해줘'도 전부 데이터 요청입니다"
    "(예: 보고서/템플릿, VOC·고객의 소리, 백서·기술문서, "
    "데이터셋·데이터 허브, 시뮬레이션 클러스터·Slurm, 재료·복합재·열충격 등 공학해석 앱). "
    "당신의 능력은 '지금 당신에게 주어진 도구 목록(각 도구의 이름·설명)'이 전부이며, 이 목록은 HEAX Hub 등에서 "
    "도구가 계속 추가되어 늘어납니다 — 그러니 특정 기능의 유무를 물으면(예: '열충격/재료/warpage 같은 게 있냐'), "
    "고정된 지식이나 아래 예시 목록이 아니라 실제 당신 도구의 이름·설명을 근거로 판단하세요. 맞는 도구가 있으면 "
    "그 도구로 답하고, 없으면 솔직히 없다고 하세요(도구에 없는 기능을 지어내지 마세요). "
    "도구 결과에 근거해 답하고, 추측하지 마세요. "
    # 아래 넷은 2026-09-12 실측으로 넣었다. MCP(클로드) 쪽 서버 지침과 같은 규율을 GLM 에도 준다 —
    # 웹은 코드가 만드는 근거 블록이 답을 대조하지만, 그건 사후 표시이지 행동 지시가 아니다.
    # ① refused 오독: pwr-swelling 에 '배터리가 부풀어 올랐어요' 는 refused=true 였는데 같은
    #    사람에게 '배터리 스웰링' 은 히트가 났다. 자료가 없는 게 아니라 말이 안 맞은 것이다.
    "도구가 refused(근거 부족)로 답하면 '자료가 없다'고 단정하지 마세요 — 일상어를 현장 용어로 "
    "바꿔 다시 묻거나(예: '부풀어 올랐다'→'스웰링 가스발생') 다른 전문가를 찾아보세요. "
    # ② 이 코퍼스의 임베딩은 무관 문장끼리도 0.87~0.90 이다(docs/gotchas.md e5).
    "유사도 점수(score)를 신뢰도로 읽지 마세요 — 무관한 내용도 높게 나옵니다. 대신 응답의 "
    "desc_match·matched_sections·low_confidence 를 보고 판단하세요. "
    # ③ 근거 블록이 수치를 대조하려면 답에 출처가 있어야 사람이 따라갈 수 있다.
    "도구 결과에서 가져온 수치·사실에는 출처(record_id·문서 제목 등)를 함께 적으세요. "
    # ④ 웹 챗에서도 등록·발행·삭제·잡 제출 도구가 그대로 바인딩된다.
    "등록·발행·삭제·잡 제출처럼 되돌리기 어려운 도구는 무엇을 어디에 하는지 사용자에게 먼저 "
    "확인하고 호출하세요. "
    "조회 도구는 항상 좁게 호출하세요 — limit(기본 10 이하)·필터·기간을 지정하고, "
    "대량 데이터가 필요하면 요약/집계 도구를 우선 사용하세요. "
    # RA 는 70개짜리 저작 워크플로 API 라 입구에서 헤맨다(비슷한 이름 다수 + 21인자 조회).
    # 자기 사용법 도구(get_guide)가 있으니 지도를 먼저 펴게 한다 — 조회 입구도 고정해 준다.
    "Report Archive(보고서) 도구는 종류가 많고 저작 워크플로용입니다 — 익숙하지 않은 작업은 "
    "get_guide 로 사용법을 먼저 확인하고, 보고서 찾기·요약은 search_reports 또는 "
    "get_reports_digest 부터 시작하세요(list_reports 의 수십 개 인자를 추측으로 채우지 마세요). "
    # 도구는 질의 관련도로 일부만 바인딩된다(TOOL_MAX) — 랭킹이 빗나가면 모델이 '기능이
    # 없다'고 단정하는 게 최악의 실패다. 안내대(search_tools)가 그 폴백이다.
    "필요한 기능의 도구가 목록에 안 보이면 '없다'고 단정하기 전에 search_tools 로 찾아보세요 "
    "— 이 대화에 실리지 않은 도구까지 전부 검색됩니다. 찾은 도구가 목록에 없으면 "
    "invoke_tool(name, arguments) 로 그 자리에서 바로 호출하세요 — search_tools 결과의 "
    "args(required 포함)를 그대로 채우고, 인자를 모르면 추측하지 말고 다시 검색하세요. "
    # 종전엔 '차트를 요청받으면 HTML 로 출력하라'가 조건→문법→보상(챗이 렌더링합니다)까지 갖춘
    # 구체적 절차였고, 도구 우선은 추상적 원칙이었다. LLM 은 구체적 지시를 따르므로 자작이 이겼다
    # (실측: 도구 0회로 물성표 + Plotly CDN 차트 생성 — 그 문장 자신의 '외부 리소스 없이'까지 위반).
    # 그래서 순서를 뒤집는다 — 작도 도구가 먼저고, 자작 HTML 은 도구가 없을 때의 폴백이다.
    "그래프·차트를 요청받으면 먼저 이미지를 돌려주는 작도 도구(이름에 plot 이 들어가는 것 등)를 호출하세요. "
    "도구 결과에 이미지 URL(captured.images[].url 의 /agent/artifacts/… 또는 attachment 의 /ai-data-hub/attachments/… 형태)이 있으면 "
    "반드시 마크다운 이미지 문법 ![설명](url) 로 본문에 포함하고, HTML 차트는 만들지 마세요. "
    "작도 도구가 없을 때에만, 도구로 조회한 실제 수치를 써서 외부 리소스 없이 자체 완결된"
    "(인라인 SVG/스크립트, 외부 CDN 금지) HTML 을 ```html 코드블록으로 출력하세요 — 챗이 미리보기로 렌더링합니다. "
    "지식 구조도·관계도·흐름도·계통도(데이터 차트가 아닌 개념도)를 그릴 때는 ASCII 나 코드가 아니라 반드시 ```mermaid 코드블록으로 "
    "출력하세요(graph TD / flowchart LR / mindmap 등) — 챗이 실제 다이어그램으로 렌더링합니다.\n\n"
    "포털 사용법·시작 방법을 물으면 다음을 안내하세요(도구 호출 불필요). "
    "권장 사용법은 이 웹 챗이 아니라 개인 Claude(Desktop/Claude Code)에 이 포털을 MCP로 연결해 쓰는 것입니다 — "
    "웹 챗은 가벼운 확인·데모용이며 본격 업무 사용은 권장되지 않습니다. 연결 방법: "
    "① 포털 상단 'API 토큰' 메뉴(/tokens)에서 토큰을 발급합니다(한 번만 표시되니 즉시 복사). "
    "② 같은 화면에 나오는 등록 명령을 실행합니다 — Claude Code는 `claude mcp add --transport http hwax "
    "<포털주소>/mcp-gw/mcp --header \"Authorization: Bearer <토큰>\"`, Claude Desktop은 표시된 JSON 설정을 붙여넣기. "
    "③ 이후 자신의 Claude에서 이 포털에 연결된 모든 서비스 도구(보고서·VOC·백서 등)를 바로 쓸 수 있습니다. "
    "토큰은 /tokens 화면에서 언제든 폐기할 수 있습니다.\n\n"
    # 가장 위험한 실패는 '도구를 안 부른 것'이 아니라 '지어낸 ID 로 불러서 남의 진짜 데이터를
    # 받아온 것'이었다. 실측: Al6061-T6 요청에 test_id=1 을 찍어 SUS201_annealed 카드를 받고,
    # 곡선 21점을 한 점도 바꾸지 않은 채 'Al6061-T6' 라벨로 재출력했다. 겉보기로는 구분이 안 된다.
    # 문장이 겹쳐 보여도 압축하지 않는다 — A/B 로 통과시킨 텍스트 그대로다.
    "[ID 규칙] 재료를 이름으로 물으면 — '물성'을 묻더라도 — 첫 호출은 언제나 list_materials 입니다. "
    "get_material·get_mat_card·plot_curve 를 먼저 부르지 마세요. "
    "test_id · material_id 처럼 식별자를 받는 인자에 임의의 숫자(1, 12345 같은 값)를 넣는 것은 금지입니다. "
    "사용자가 대상을 이름으로 부르면(예: 'SCM440_alloy_steel', 'Al6061-T6') 첫 호출은 반드시 목록·검색 도구입니다 — "
    "list_materials(query=\"SCM440\") 처럼 이름 일부를 넣어 부르고, 그 응답의 id·test_id 로만 "
    "get_material / get_mat_card / plot_curve / get_curve 를 호출하세요. "
    # 실측: 게이트가 test_id 추측을 막자 모델이 list_materials(category="aluminum") 를 걸었고,
    # 실제 category 는 "metal" 이라 [] 가 돌아왔다. 그러자 "DB에 없다"로 끝났다(id=19 로 실재).
    # 지어낸 필터가 빈 결과를 만들고, 빈 결과가 '없음' 오판으로 이어지는 경로를 끊는다.
    "목록·검색 도구에는 이름 조각만 넣으세요 — category 같은 추가 필터를 스스로 지어내 걸지 마세요"
    "(값을 틀리면 결과가 비어 '없다'고 오판하게 됩니다). "
    "결과가 비면 필터를 빼고 더 짧은 이름 조각으로 다시 조회하세요. "
    "다른 도메인도 같습니다 — 식별자를 받는 도구 앞에는 반드시 목록·검색 도구가 옵니다. "
    "도구 결과의 이름이 물어본 대상과 다르면 그 결과를 쓰지 말고 다시 찾으세요. "
    "호출 한 번이 실패했다고 \"없다\"고 단정하지 말고, 목록·검색 도구로 확인한 뒤에만 없다고 답하세요.\n"
    # stress_strain_plot 은 DB 를 조회하지 않고 '호출자가 준 숫자'를 그리는 순수 렌더러다.
    # 지어낸 물성으로도 진짜 PNG 가 나오므로, '도구를 불렀다'는 사실이 날조를 내부 데이터처럼
    # 보이게 한다(실측: Al6061-T6 항복 276MPa 인데 35000MPa 로 그려 놓고 그 값을 답으로 인용).
    "[수치 출처] 답변에 쓰는 모든 수치·좌표·재료카드 본문은 이번 턴 도구 결과에 실제로 있던 값이어야 합니다 — "
    "기억으로 채우지 마세요. 호출자가 준 숫자를 그대로 그리는 도구(stress_strain_plot 등)에도 "
    "당신이 아는 값이 아니라 조회한 값만 넣으세요. "
    "표·그래프를 요청받아도 첫 동작은 도구 호출입니다 — 도구를 부르기 전에 물성표나 코드블록을 먼저 출력하지 마세요."
)


def _parse_servers(spec: str) -> dict:
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, url = part.split("=", 1)
        out[name.strip()] = {"url": url.strip(), "transport": "streamable_http"}
    return out


def _load_mcp_config() -> dict:
    if MCP_CONFIG and os.path.exists(MCP_CONFIG):
        with open(MCP_CONFIG) as f:
            return json.load(f)
    return _parse_servers(MCP_SERVERS)


def _with_groups(connections: dict, groups: list[str], user: str = "",
                 user_pat: str = "") -> dict:
    """Clone the MCP connection config, injecting the caller's groups header so the gateway
    can filter the tool list (and guard tool calls). Does not mutate the input.

    user 는 '누구인가'다 — groups 가 부류라면 이쪽은 개인이고, 백엔드가 사용자별로
    데이터를 스코프할 때(DynaForge 세션 등) 게이트웨이가 그 사용자 자격증명으로 갈아탄다.
    비면 종전대로 서비스 계정 시야다."""
    # HTTP 헤더는 latin-1 만 담을 수 있다. '연구소' 같은 한글 그룹명을 그대로 실으면
    # UnicodeEncodeError 로 도구 로딩 전체가 실패하고, 그 사용자는 조용히 '도구 0개'가 된다
    # (실측 로그: tool load FAILED groups=['연구소'] UnicodeEncodeError). 퍼센트 인코딩해
    # 보내고 게이트웨이가 디코드한다 — ASCII 그룹명은 인코딩해도 그대로라 하위호환된다.
    hdr = quote(",".join(groups), safe=",")
    extra = {USER_HEADER: quote(user.strip().lower(), safe="@.")} if (user or "").strip() else {}
    # ⚠ 그룹이 없으면 헤더를 **아예 안 보낸다.** 게이트웨이는 '빈 헤더' 를 권한 0인 **사람**으로
    # 읽고(fail-closed), '헤더 없음' 만 내부 서비스로 본다. 신원이 없는 호출(MCP 심의처럼 게이트웨이가
    # 신원 헤더를 안 실어 준 경우)에 빈 문자열을 보내면 도구가 465개 → 8개로 줄어 좌석 발굴이
    # 통째로 실패한다(실측: MCP 심의가 '관련 전문 페르소나를 충분히 찾지 못했습니다' 로 즉사).
    # 빈 값은 '권한이 없다' 가 아니라 '누구인지 모른다' 라는 뜻이므로 헤더를 비워 보내면 안 된다.
    _g = {GROUPS_HEADER: hdr} if groups else {}
    out = {}
    for name, cfg in connections.items():
        cfg = dict(cfg)
        cfg["headers"] = {**cfg.get("headers", {}), **_g, **extra}
        # 포털이 이 챗용으로 발급한 사용자 PAT 가 있으면 서비스 계정(GW_TOKEN) 대신 그것으로
        # 붙는다. 헤더로 "누구"라고 알리는 것과 실제로 그 사람의 자격증명으로 붙는 것은 다르다
        # — 게이트웨이가 포털에 되물어야 하는 도구(대화 검색·저장)는 후자가 없으면 401 이다.
        # 없으면 종전대로 서비스 계정으로 돈다(발급 실패가 챗을 막지 않는다).
        if user_pat:
            cfg["headers"]["Authorization"] = f"Bearer {user_pat}"
        out[name] = cfg
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the LLM once. Tools are loaded per request (they depend on the caller's groups),
    # so we keep the raw connection config and compile/cache a ReAct agent per group-set.
    # LLM_DISABLE_STREAMING=1: 토큰 스트리밍 대신 완결 응답(invoke) — vLLM 0.23.0의 GLM-5.2
    # 스트리밍 경로가 tool_calls 를 유실(빈 배열 + finish_reason:stop)하는 서빙 결함 우회.
    # 텍스트가 모델 턴 단위로 도착하는 대신 도구 호출이 정상 동작한다. dev(qwen)는 기본 0(스트리밍).
    disable_stream = os.environ.get("LLM_DISABLE_STREAMING", "0") == "1"

    # LLM 튜닝은 전부 env — 환경(dev qwen 16K vs 상암 GLM)마다 다른 값을 배포 없이 적용한다.
    # 미설정 시 기존 동작과 동일(temperature=0, max_tokens 미전송=무상한, effort·timeout 미전달).
    # 파싱은 안전 파서(_env_*) — 오타 값이 lifespan 에서 서버 기동을 죽이지 않고 경고 후 기본값.
    #   LLM_TEMPERATURE      : 기본 0 (ReAct 도구호출 결정성)
    #   LLM_MAX_TOKENS       : 0/미설정=미전송. 설정 시 8192급 여유값 권장 — 2048~4096은
    #                          thinking 토큰과 겹치면 미완 JSON 절단을 유발하므로 금지.
    #   LLM_REASONING_EFFORT : 빈 값=미전달(RA 규약). GLM 계열은 반드시 extra_body 경유
    #                          chat_template_kwargs 로 전달 — 톱레벨 reasoning_effort 필드는
    #                          OpenAI 표준 파라미터로 나가므로 쓰지 않는다.
    #   LLM_TIMEOUT_S        : 챗(ReAct) 경로 포함 전역 타임아웃. 0/미설정=라이브러리 기본(600s).
    #   DELIB_*              : 심의 라운드 전용 오버라이드(temperature/max_tokens/effort/timeout).
    #                          챗 경로는 위 LLM_* 만 따르므로 심의 튜닝이 챗에 새지 않는다.
    def _mk_llm(temperature: float, max_tokens: int, effort: str, timeout_s: float = 0.0) -> ChatOpenAI:
        kw: dict = dict(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY, model=VLLM_MODEL,
                        temperature=temperature, disable_streaming=disable_stream)
        if max_tokens > 0:
            kw["max_tokens"] = max_tokens
        if effort:
            kw["extra_body"] = {"chat_template_kwargs": {"reasoning_effort": effort}}
        if timeout_s > 0:
            kw["timeout"] = timeout_s
        return ChatOpenAI(**kw)

    base_t = _env_float("LLM_TEMPERATURE", 0.0)
    base_mt = _env_int("LLM_MAX_TOKENS", 0)
    base_eff = os.environ.get("LLM_REASONING_EFFORT", "")
    base_to = _env_float("LLM_TIMEOUT_S", 0.0)
    app.state.llm = _mk_llm(base_t, base_mt, base_eff, base_to)

    # 심의(라운드 토론) 전용 오버라이드 — DELIB_* 가 하나라도 설정되면 별도 인스턴스.
    if any(os.environ.get(k, "") for k in
           ("DELIB_TEMPERATURE", "DELIB_MAX_TOKENS", "DELIB_REASONING_EFFORT", "DELIB_TIMEOUT_S")):
        d_t = _env_float("DELIB_TEMPERATURE", base_t)
        d_mt = _env_int("DELIB_MAX_TOKENS", base_mt)
        d_eff = os.environ.get("DELIB_REASONING_EFFORT", "") or base_eff
        d_to = _env_float("DELIB_TIMEOUT_S", base_to)
        app.state.delib_llm = _mk_llm(d_t, d_mt, d_eff, d_to)
        print(f"[agent] 심의 전용 LLM 오버라이드 — temperature={d_t}, "
              f"max_tokens={d_mt or '미전송'}, effort={d_eff or '미전달'}, "
              f"timeout={d_to or '기본'}")
    else:
        d_t, d_mt, d_eff, d_to = base_t, base_mt, base_eff, base_to
        app.state.delib_llm = app.state.llm
    # 요청 단위 타임아웃 오버라이드(웹 토글)용 — 같은 temperature/max_tokens/effort 로 timeout 만
    # 바꿔 재구성하는 팩토리(구성만·연결 없음). 기본 타임아웃도 노출해 오버라이드 필요 판정에 쓴다.
    app.state.delib_timeout_s = d_to
    app.state.mk_delib_llm = (lambda ts, _t=d_t, _m=d_mt, _e=d_eff: _mk_llm(_t, _m, _e, ts))
    app.state.llm_nostream = disable_stream
    if disable_stream:
        print("[agent] LLM_DISABLE_STREAMING=1 — 토큰 스트리밍 비활성(도구호출 우선 모드)")
    app.state.connections = _load_mcp_config()
    # 삽입 순서를 LRU 로 쓴다(dict 는 순서를 보존한다). 상한은 _agent_for 가 강제한다.
    app.state.agent_cache = {}  # (groups, pins, query, sources, user, cred) -> ReAct agent
    # '도구 0개'(tool_load_error)와 '강등'(tool_degraded)은 다른 사실이라 칸을 나눈다.
    app.state.tool_degraded = {}  # (groups, user) -> 사유
    # 도구 로딩 오류는 그룹셋별로 보관한다. 전역 스칼라로 두면 한 그룹의 일시적 실패가
    # 다른 사용자 턴까지 '도구 없음'으로 오염시키고, 캐시 적중 경로에선 해제조차 안 돼
    # 영구 고착된다(실측: 실패한 적 없는 그룹이 계속 "도구 연결 복구되면 다시" 응답).
    app.state.tool_load_error = {}  # frozenset(groups) -> 마지막 실패 사유
    # 마지막 성공 도구 스냅샷 — 게이트웨이가 죽어 로드가 실패해도 직전 도구로 응답한다
    # (강건성 최상단). 키에 사용자 신원을 넣어 남의 자격증명 도구가 새지 않게 한다.
    # 값: (raw_tools, ts). 게이트웨이 도구는 자주 안 바뀌므로 짧은 불통은 이걸로 흡수된다.
    app.state.tool_snapshot = {}  # (frozenset(groups), user_lower) -> (raw_tools, ts)
    print(f"[agent] ready — model={VLLM_MODEL}, mcp={list(app.state.connections)}")
    # 심의 MCP(/mcp) — streamable_http_app 은 자체 lifespan(task group)이 있어야 동작하는데
    # FastAPI 의 mount() 는 하위 앱 lifespan 을 전파하지 않는다. 여기서 명시적으로 연다.
    # 실패해도 서버는 뜬다 — 심의 MCP 는 부가 진입점이고, 웹(/chat) 경로는 이것과 무관하다.
    if _DELIB_MCP is not None:
        _delib_mcp_module.bind(app)
        try:
            async with _DELIB_MCP.router.lifespan_context(_DELIB_MCP):
                print("[agent] deliberation MCP mounted at /mcp")
                yield
            return
        except Exception:  # noqa: BLE001
            logging.getLogger("agent").exception("[agent] 심의 MCP lifespan 실패 — /mcp 없이 계속")
    yield


# 심의를 MCP 도구로 노출한다(게이트웨이 백엔드). import 실패는 치명적이지 않다 —
# 웹 심의(/chat 슬래시 트리거)는 이 모듈과 무관하게 돈다.
try:
    import mcp_server as _delib_mcp_module
    _DELIB_MCP = _delib_mcp_module.app
except Exception as _exc:  # noqa: BLE001
    logging.getLogger("agent").warning("[agent] 심의 MCP 비활성 — %r", _exc)
    _delib_mcp_module = None
    _DELIB_MCP = None


app = FastAPI(title="HWAX Agent Server", version="0.3.0", lifespan=lifespan)
if _DELIB_MCP is not None:
    app.mount("/mcp", _DELIB_MCP, name="delib-mcp")
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatRequest(BaseModel):
    message: str
    system_id: str | None = None  # sub-page context → tool scope (portal Phase 2; accepted, not yet used)
    groups: list[str] = []
    # 사용자 지정 우선 도구 — AI 의 도구 선택을 100% 신뢰할 수 없어, 사용자가 도구 카탈로그에서
    # 직접 고른 도구를 우선 사용하게 강제한다(바인딩 보장 + 시스템 프롬프트 지시).
    pinned_tools: list[str] = []
    # 사용자 지정 전문가(agent_type) — 이 전문가의 역할/시스템프롬프트를 페르소나로 주입해
    # '전문가와 대화' 모드가 된다(챗 시작 전 선택 패널에서 지정).
    pinned_agent: str | None = None
    # 여러 전문가를 한 목소리로 — 첫 명이 **주 전문가**(목소리·지식카드·판단 기준)이고 나머지는
    # 보조다. 보조가 HE팀 운영자면 그 앱 도구를 얹고, 도메인 전문가면 판단 기준(역할)만 빌려준다.
    # 같은 층 전문가를 여럿 겹치면 답이 평균으로 무너지고 의견 충돌이 한 목소리에 묻히므로,
    # 상한은 5명이되 실효 제약은 앱 3개·도구 캡이다(docs/multi-persona).
    pinned_agents: list[str] | None = None
    # 웹 리서치 소스 토글 — None 이면 종전 동작(전부), 리스트면 그 소스만 바인딩한다.
    # 빈 리스트는 '전부 끔'이라 나가는 도구가 하나도 실리지 않는다.
    search_sources: list[str] | None = None
    # 앱(소유 MCP 앱) 단위 우선 지정 — 개별 도구 이름을 고르는 대신 앱을 고른다.
    # 도구 210개를 평평하게 훑어 12개를 체크하는 UX 는 "무엇을 골라야 하는지 모르겠다"는
    # 문제를 낳았다. 앱은 10개뿐이고 사용자가 아는 단위라 고를 수 있다. 서버가 앱→도구로
    # 펼치므로 pinned_tools 의 12개 캡에 걸리지 않는다(앱 하나가 평균 23개·최대 32개다).
    pinned_apps: list[str] | None = None
    # 호출자 이메일 — 포털 백엔드가 세션/PAT 주체에서 채운다(클라이언트가 보내는 값이 아니다).
    # 게이트웨이가 이 값으로 사용자별 백엔드 자격증명을 쓴다: 없으면 DynaForge 같은 사용자
    # 스코프 앱은 서비스 계정 시야로 답하고, 그건 '내 모델이 하나도 없다'로 보인다.
    user_email: str = ""
    # 포털이 이 챗용으로 발급한 단명 사용자 PAT. 게이트웨이에 '이 사람으로' 붙기 위한 것이다
    # — 헤더로 누구인지 알리는 것만으로는 포털에 되묻는 도구(대화 검색·저장)가 401 이 된다.
    # 없으면 종전대로 서비스 계정(GW_TOKEN)으로 돈다.
    user_pat: str = ""
    # 멀티턴: 이전 대화 [{"role":"user"|"assistant","content":str}, …]. 검증/절단은 _history_messages 가 담당.
    history: list[dict] = []
    # 심의 손잡이 요청 오버라이드(웹 토글) — deliberation._resolve_opts 가 화이트리스트 키만 읽고
    # 클램프하므로 raw dict 로 받아도 안전. 미지정 키는 env 기본값. 심의(/심의) 경로에서만 쓰인다.
    delib_opts: dict | None = None
    # 띵킹 모드 — 질문을 전문가 풀에 돌려 답할 수 있는 좌석만 각자 답하게 한다(회의 없음).
    # delib_opts 가 아니라 top-level 인 이유: 이건 매 턴 켜져 있어야 하는 모드인데, 프론트의
    # 슬래시 접두사는 첫 발화에만 붙어(ChatContext.applyPrefix) 둘째 턴부터 조용히 꺼진다.
    thinking: bool = False
    # 붙인 문서의 추출문 [{name, kind, text}, …]. 원본 파일은 오지 않는다 — 포털이 브라우저에서
    # 읽어 글만 싣는다(DRM 전제). 검증·절단은 _doc_block 이 한다.
    documents: list[dict] = []
    # 요청 시점 권한(feat:·plat:) — 포털이 원장으로 계산해 준다(HWAXPortal docs/access-control).
    # 심의·Thinking 트리거 판정은 여기 있으므로 포털이 아니라 이쪽이 막는다. None 은 권한을 모르는
    # 옛 호출이라 막지 않는다(포털은 늘 보낸다) — '안 보냄'과 '권한 없음'을 구분해야 해서다.
    entitlements: list[str] | None = None


# 기능 권한 이름(사람용) — 포털 access.yaml 의 label 과 같다.
_FEATURE_LABEL = {"feat:deliberation": "전문가 심의", "feat:thinking": "Thinking",
                  "feat:expert-chat": "전문가와 대화"}


def _denied_feature(req: "ChatRequest") -> str | None:
    """이 요청이 쓰려는 기능 중 권한이 없는 것. 트리거 순서는 chat() 과 같다."""
    if req.entitlements is None:
        return None
    have = set(req.entitlements)
    m = req.message
    if is_sim_deliberation(m) or is_test_plan(m) or is_deliberation(m):
        need = "feat:deliberation"
    elif is_agent_search(m):
        need = "feat:expert-chat"
    elif is_thinking(m) or req.thinking:
        need = "feat:thinking"
    else:
        need = None
    if need and need not in have:
        return need
    if req.pinned_agent and "feat:expert-chat" not in have:
        return "feat:expert-chat"
    return None


async def _deny_stream(key: str) -> AsyncIterator[bytes]:
    """권한이 없을 때의 답 — 오류 배너가 아니라 대화에 남는 한 줄로 알린다(무엇이 없고 어디서 청하나)."""
    msg = (f"'{_FEATURE_LABEL.get(key, key)}' 을(를) 쓸 권한이 없습니다. "
           "상단 메뉴의 내 권한에서 요청하면 관리자가 승인합니다.")
    yield _sse("token", {"delta": msg})
    yield _sse("result", {"type": "text", "content": msg})
    yield _sse("done", {})


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


# ── SSE 절단 내성(감사 원장 1-C) ────────────────────────────────────────────────
# 심의류 스트림은 저장(RA·대화·결정문)이 전부 제너레이터 꼬리에 있다 — 종전 구조에선
# 탭 닫힘·사내 프록시 idle 절단·nginx read timeout 하나가 수 시간 심의를 무저장으로
# 죽였다(취소 스택만 남고 GPU 시간 전소). 심의 본체를 백그라운드 태스크로 옮기고
# SSE 는 큐 구독만 한다 — 구독이 끊겨도 태스크는 계속 돌아 저장까지 완주한다.
_DETACHED_TASKS: set = set()   # 태스크 GC 방지 — 참조가 사라지면 asyncio 가 조용히 버린다


def _detach_stream(gen, label: str):
    q: asyncio.Queue = asyncio.Queue()   # 무제한 — 소비자가 없어도 생산이 막히지 않는다

    async def _drive():
        try:
            async for chunk in gen:
                q.put_nowait(chunk)
        except Exception as exc:  # noqa: BLE001 — 태스크 안 예외는 아무도 await 안 하면 무음이다
            logging.getLogger("agent").exception("[detach:%s] 심의 태스크 오류", label)
            q.put_nowait(_sse("error", {"message": f"심의 오류: {exc}"}))
        finally:
            q.put_nowait(None)

    task = asyncio.create_task(_drive(), name=f"delib-{label}")
    _DETACHED_TASKS.add(task)
    task.add_done_callback(_DETACHED_TASKS.discard)

    async def _subscribe():
        try:
            while True:
                chunk = await q.get()
                if chunk is None:
                    break
                yield chunk
        except asyncio.CancelledError:
            logging.getLogger("agent").warning(
                "[detach:%s] SSE 구독 절단 — 심의는 백그라운드에서 계속 완주한다(저장 포함)", label)
            raise

    return _subscribe()


# 도구 결과 절단(문자) — 컨텍스트 보호용 안전밸브. 무제한이면 대량 조회(VOC 수천 건)가
# 프롬프트를 넘겨 'maximum context length' 로 챗 전체가 죽는다(dev 16K 실측).
# 다만 6000 은 dev(16K) 기준값이라 대형 컨텍스트 모델(prod GLM)에 그대로 쓰면 목록이
# 불필요하게 잘린다 → 기본을 32000 으로 올리고, 소형 모델 박스만 .env 로 낮춘다.
# 0 이면 무제한(권장 안 함 — 안전밸브 해제).
# 32000 도 부족했다: recommend_agents(top_k=40) 한 번이 53KB 다(실측). prod 는 GLM(대형 컨텍스트)
# 이므로 기본을 120000(≈35K 토큰)까지 올린다 — 소형 모델 박스만 .env 로 낮춘다.
TOOL_RESULT_MAX = int(os.environ.get("TOOL_RESULT_MAX", "200000"))
# 결정적 카탈로그 조회(recommend_agents·list_agents·get_agent_session·list_records)는 LLM 프롬프트가
# 아니라 **코드가 JSON 으로 파싱**한다. 여기에 프롬프트 보호용 절단을 걸면 JSON 이 중간에서 끊겨
# 파싱이 조용히 실패하고 "추천 0명"·"풀 9명" 같은 빈 결과가 나온다(실측 원인). 사실상 무제한으로 둔다.
CATALOG_RESULT_MAX = int(os.environ.get("CATALOG_RESULT_MAX", "2000000"))
# 도구를 LLM 에 바인딩하지 않는 경로(심의)의 설명 한도. 바인딩 프롬프트가 아니므로 절단이 무의미
# 하고, 오히려 인자 유효값이 잘려 계획자가 값을 지어낸다(실측: units 선택지가 사라져 "MPa" 날조).
CATALOG_DESC_MAX = int(os.environ.get("CATALOG_DESC_MAX", "4000"))
# 아래 기본값은 모두 대형 컨텍스트(prod B300 8기·GLM) 기준으로 넉넉하게 잡는다.
# 소형 모델 박스(dev qwen 16K)만 .env 로 낮춘다 — 반대로 잡으면 prod 가 dev 사이즈에 묶인다.
TOOL_DESC_MAX = int(os.environ.get("TOOL_DESC_MAX", "2400"))  # 도구 description 절단(문자)
HIST_ITEM_MAX = int(os.environ.get("HIST_ITEM_MAX", "24000"))  # history 항목별 절단(문자)
HIST_MAX_ITEMS = int(os.environ.get("HIST_MAX_ITEMS", "160"))  # history 최대 항목 수
# 이력 예산은 **컨텍스트에서 유도**한다. 종전 고정 200,000자는 한국어로 약 190,000토큰이라
# GLM 128K 를 이력만으로 넘긴다 — 문서까지 붙이면 그대로 400 이다. env 로 못박으면 그 값이 이긴다.
# 문서를 붙인 턴에는 이력이 양보한다(문서가 그 턴의 본론이다). 안 붙였으면 창을 놀릴 이유가
# 없다 — 종전엔 문서 유무와 무관하게 40% 만 써서 1M 창에서 절반 넘게 남겼다.
HIST_CTX_SHARE = float(os.environ.get("HIST_CTX_SHARE", "0.40"))       # 문서가 있을 때
HIST_CTX_SHARE_SOLO = float(os.environ.get("HIST_CTX_SHARE_SOLO", "0.85"))  # 문서가 없을 때
# 예산을 넘긴 오래된 턴을 **버리지 않고** 압축해 넣는다. 이 몫이 그 압축본에 쓰인다.
HIST_COMPACT_SHARE = float(os.environ.get("HIST_COMPACT_SHARE", "0.20"))


def _cap(text, limit=None):
    limit = limit or TOOL_RESULT_MAX
    s = text if isinstance(text, str) else str(text)
    if len(s) <= limit:
        return s
    # 잘린 사실과 대처를 모델이 오해 없이 알도록 명시 — '이게 전부'로 착각해 잘못 답하는 것 방지.
    return s[:limit] + (
        f"\n…[경고: 도구 출력이 {len(s)}자였고 {limit}자에서 잘렸습니다. 이건 전체가 아닙니다. "
        f"전체 목록을 나열하지 말고, limit/필터/기간으로 좁혀 다시 조회하거나 집계 도구를 쓰세요.]")


# ── 도구 산출 이미지 아티팩트 — base64 캡처를 파일로 저장하고 URL 로 치환 ─────────────
# AIDH 러너의 captured.images[].data(base64)가 그대로 LLM 에 가면 절단 캡에 깨지고 컨텍스트를
# 태운다. 파일로 강등해 /artifacts 로 서빙하고, 결과에는 짧은 url 만 남긴다(챗 이미지 렌더 근원).
ARTIFACT_DIR = os.environ.get(
    "ARTIFACT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "artifacts"))
os.makedirs(ARTIFACT_DIR, exist_ok=True)
_ARTIFACT_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
                 "image/webp": "webp", "image/svg+xml": "svg"}


def _stash_artifacts(obj, path_map: list | None = None) -> bool:
    import base64
    import hashlib
    changed = False

    def walk(v):
        nonlocal changed
        if isinstance(v, dict):
            imgs = v.get("images")
            if isinstance(imgs, list):
                for im in imgs:
                    if isinstance(im, dict) and isinstance(im.get("data"), str) and len(im["data"]) > 256:
                        try:
                            raw = base64.b64decode(im["data"])
                        except Exception:  # noqa: BLE001 — 손상 base64 는 그대로 둔다
                            continue
                        ext = _ARTIFACT_EXT.get(str(im.get("mime")), "png")
                        name = hashlib.sha256(raw).hexdigest()[:20] + "." + ext
                        with open(os.path.join(ARTIFACT_DIR, name), "wb") as f:
                            f.write(raw)
                        im.pop("data", None)
                        im["url"] = f"/agent/artifacts/{name}"
                        if path_map is not None and im.get("path"):
                            path_map.append((f"/work/{im['path']}", im["url"]))
                        changed = True
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(obj)
    return changed


# AIDH 가 결과에 싣는 자기 로컬 원점(첨부 URL 등) → 포털 공개 경로 재작성.
# 예: http://127.0.0.1:8001/attachments/… → /ai-data-hub/attachments/… (nginx 라우트 경유,
# 브라우저에서 로드 가능). env 로 원점/공개경로 오버라이드 가능.
_AIDH_LOCAL_ORIGINS = tuple(o.strip() for o in os.environ.get(
    "AIDH_LOCAL_ORIGINS", "http://127.0.0.1:8001,http://localhost:8001").split(",") if o.strip())
_AIDH_PUBLIC_BASE = os.environ.get("AIDH_PUBLIC_BASE", "/ai-data-hub")


def _rewrite_local_urls(s: str) -> str:
    for o in _AIDH_LOCAL_ORIGINS:
        if o in s:
            s = s.replace(o, _AIDH_PUBLIC_BASE)
    return s


# 이번 턴에 생성된 이미지 URL — 약한 모델이 ![](url) 인용을 빠뜨려도 결정적으로 첨부한다.
_turn_images: contextvars.ContextVar = contextvars.ContextVar("turn_images", default=None)

# 이번 턴에 '출처가 있는' 정수 — 사용자 발화·이전 대화·앞선 도구 결과에서 본 숫자들.
# 유령 ID 게이트가 쓴다. 프롬프트만으로는 확률적이라, 최악 케이스(작은 정수를 찍어 남의 진짜
# 데이터를 받아오는 것)는 코드로 결정적으로 막는다 — 실측: test_id=1 → SUS201_annealed 카드를
# 받아 'Al6061-T6' 라벨로 재출력. 빈 결과보다 나쁜 '확신에 찬 오답'이다.
_turn_ids: contextvars.ContextVar = contextvars.ContextVar("turn_ids", default=None)
# 식별자 인자 이름 — id, test_id, material_id, job_id …
_ID_ARG_RE = re.compile(r"^(id|.*_id)$")
# 독립된 정수 토큰만 — \d+ 로 잡으면 "Al6061-T6" 의 부분수열 1 이 test_id=1 을 통과시킨다.
_INT_TOK_RE = re.compile(r"(?<!\d)\d+(?!\d)")
_TURN_IDS_MAX = 2000  # 도구 결과에서 걷는 정수 상한(무한 증식 방지)
# 게이트 표지 _PHANTOM_ID_MARK 는 deliberation 에서 import 한다 — 심의도 이 문구로 '실행되지
# 않은 호출'을 판정하므로, 두 군데에 따로 쓰면 한쪽만 고쳐질 때 조용히 어긋난다.


def _int_tokens(text) -> set:
    """문자열에서 독립 정수 토큰을 뽑아 정수 집합으로. 비문자열은 str() 후 처리."""
    s = text if isinstance(text, str) else str(text)
    return {int(m) for m in _INT_TOK_RE.findall(s)}


def _learn_ids(text) -> None:
    """도구 결과에 실린 정수를 이번 턴의 '출처 있는 값'으로 등록한다 — list_materials 가 준
    id 로 다음 호출이 통과되게 하는 경로다. ⚠ 부모(on_tool_end)가 아니라 자식(도구 코루틴)
    쪽에서 넣는다. 부모에 두면 병렬 호출 시 순서 경합으로 정상 ID 가 차단될 수 있다."""
    src = _turn_ids.get()
    if src is None or len(src) >= _TURN_IDS_MAX:
        return
    src |= _int_tokens(text)


# ── 재촉 → 이어하기 ─────────────────────────────────────────────────────────
# 응답이 끊긴 뒤 사용자가 "야! 하라니까!" 처럼 재촉하면, 그 발화 자체는 아무 내용이 없다.
# 모델에 그대로 넘기면 재촉을 새 질문으로 읽고 "무엇을 도와드릴까요" 로 답하거나, 운이 좋아야
# 이어간다 — 확률적이다. 직전 턴이 실제로 끊겼는지 코드가 판정하고, 끊겼으면 원래 질문을
# 복원해 "이어서 완료하라" 로 바꿔 넣는다.
_NUDGE_RE = re.compile(
    r"^\s*(?:야+[!.,~\s]*)?"
    r"(?:하라니까|해라|해줘|하라고|계속|이어서|왜\s*안\s*해|다시|진행|ㄱㄱ+|고고|"
    r"응답\s*없|answer|continue|go on|keep going)"
    r"[\s!.,~?ㅋㅎ]*$", re.I)
# 직전 답변이 '끊긴' 표지 — 내부 오류 중단 문구, 예고만 하고 끝난 문장, 빈 응답.
_INTERRUPTED_RE = re.compile(
    r"처리 중 내부 오류로 응답이 여기서 중단|응답이 여기서 중단되었습니다|"
    r"도구를 조회하지 않고 생성|조회하겠습니다\s*$|확인해 보겠습니다\s*$")
_NUDGE_MAX_LEN = 24   # 재촉은 짧다. 길면 진짜 새 질문일 가능성이 높다.


def _is_nudge(msg: str) -> bool:
    """내용 없는 재촉인가. 길면 새 질문으로 본다(오판이 재촉 무시보다 나쁘다)."""
    s = (msg or "").strip()
    return bool(s) and len(s) <= _NUDGE_MAX_LEN and bool(_NUDGE_RE.match(s))


def _resume_target(history: list) -> tuple:
    """(원래 질문, 직전 부분응답) — 이어할 것이 있으면 돌려준다. 없으면 (None, None).

    직전 assistant 턴이 비었거나 중단 표지를 달고 있을 때만 이어하기로 본다. 정상 답변 뒤의
    "계속"은 '더 말해달라'는 새 요구이므로 건드리지 않는다.
    """
    if not isinstance(history, list):
        return (None, None)
    last_a, last_q = None, None
    for h in reversed(history):
        if not isinstance(h, dict):
            continue
        role, content = h.get("role"), str(h.get("content") or "")
        if last_a is None and role == "assistant":
            last_a = content
            continue
        if last_a is not None and role == "user":
            last_q = content
            break
    if last_q is None:
        return (None, None)
    if last_a is not None and last_a.strip() and not _INTERRUPTED_RE.search(last_a):
        return (None, None)          # 정상적으로 끝난 답변 — 이어하기 아님
    return (last_q, (last_a or "").strip())


# ── 근거 표기 ───────────────────────────────────────────────────────────────
# 프롬프트로 "근거를 대라"고 시키는 것은 확률적이라 모델이 바뀌면 무너진다. 심의는 이미
# 코드 검증(quote 가 원문에 실재하는가)으로 이 문제를 풀었는데(deliberation._quote_validator),
# 챗에는 그 장치가 없어 도구를 부르고도 답변이 그 범위를 넘어서면 아무도 못 잡았다.
# 여기서는 두 가지를 코드로 한다 — 근거 블록을 실행 기록에서 만들고(지어낼 수 없다),
# 답변의 수치를 도구 출력 원문과 대조해 출처 없는 값을 표시한다.

# 답변에서 뽑을 수치 토큰 — 천단위 콤마와 소수점을 포함해 원문 표기 그대로 잡는다.






# 근거 블록의 고정 표지 — 생성부와 '다음 턴 출처 판정'이 이 문자열로 맞물린다.
# 두 군데에 따로 쓰면 한쪽만 고쳐질 때 조용히 어긋난다(유령 ID 게이트가 세탁을 허용하게 된다).
_EVIDENCE_MARK = "**근거** — 이번 답변이 실제로 조회한 것"


def _evidence_block(calls: list, unsourced: list) -> str:
    """이번 턴의 근거 블록. 모델이 아니라 코드가 실행 기록에서 만든다 — 그래서 지어낼 수 없다.

    도구를 한 번도 안 쓴 턴에는 붙이지 않는다. 그 경우는 기존 '도구 미조회' 경고가 담당한다.
    """
    if not calls:
        return ""
    lines = ["\n\n---", _EVIDENCE_MARK]
    for name, args in calls[:8]:
        lines.append(f"- `{name}`" + (f" · {args}" if args else ""))
    if len(calls) > 8:
        lines.append(f"- … 외 {len(calls) - 8}건")
    if unsourced:
        lines.append("")
        lines.append("> ⚠ 다음 수치는 위 조회 결과에서 확인되지 않았습니다 — "
                     "추론이거나 계산된 값일 수 있으니 그대로 인용하지 마십시오: "
                     + ", ".join(f"`{v}`" for v in unsourced))
    return "\n".join(lines)


def _phantom_id_arg(args: dict, src: set):
    """식별자 인자 중 '이번 턴 어디에도 출처가 없는 정수'를 찾아 (이름, 값) 으로 돌려준다.
    bool 은 int 의 서브클래스라 명시적으로 제외한다(flag=True 가 id 로 오인되면 안 된다)."""
    for k, v in (args or {}).items():
        if not isinstance(k, str) or not _ID_ARG_RE.match(k):
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            continue
        if v not in src:
            return k, v
    return None


ARTIFACT_KEEP = int(os.environ.get("ARTIFACT_KEEP", "500"))


def _prune_artifacts() -> None:
    """아티팩트 무한 증가 방지 — 최신 ARTIFACT_KEEP 개만 남긴다(챗 이미지는 단명 자원)."""
    try:
        fs = [os.path.join(ARTIFACT_DIR, f) for f in os.listdir(ARTIFACT_DIR)]
        fs = [f for f in fs if os.path.isfile(f)]
        if len(fs) <= ARTIFACT_KEEP:
            return
        for f in sorted(fs, key=os.path.getmtime)[:len(fs) - ARTIFACT_KEEP]:
            os.remove(f)
    except Exception:  # noqa: BLE001 — 정리 실패가 응답을 막으면 안 된다
        pass


def _stash_image_items(items) -> list:
    """MCP content 의 이미지 블록(base64)을 파일로 저장하고 서빙 URL 목록을 돌려준다."""
    import base64
    import hashlib
    urls = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # 키 이름은 계층마다 다르다 — MCP SDK(data/mimeType) vs 게이트웨이 정규화(base64/mime_type).
        data = it.get("data") or it.get("base64")
        mime = it.get("mimeType") or it.get("mime_type") or it.get("mime") or ""
        if not (isinstance(data, str) and len(data) > 256 and str(mime).startswith("image/")):
            continue
        try:
            raw = base64.b64decode(data)
        except Exception:  # noqa: BLE001 — 손상 base64 는 건너뜀
            continue
        name = hashlib.sha256(raw).hexdigest()[:20] + "." + _ARTIFACT_EXT.get(mime, "png")
        with open(os.path.join(ARTIFACT_DIR, name), "wb") as f:
            f.write(raw)
        _prune_artifacts()
        urls.append(f"/agent/artifacts/{name}")
    bag = _turn_images.get()
    if bag is not None:
        for u in urls:
            if u not in bag:
                bag.append(u)
    return urls


def _extract_artifacts_text(s):
    """도구 결과 문자열에서 captured.images base64 → 파일+url 치환 + 로컬 URL 공개경로 재작성."""
    if isinstance(s, str):
        s = _rewrite_local_urls(s)
    if not isinstance(s, str) or '"images"' not in s or '"data"' not in s:
        return s
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return s
    pm: list = []
    if _stash_artifacts(obj, pm):
        out = json.dumps(obj, ensure_ascii=False)
        # 결과 텍스트(stdout 의 out_path 등)에 남은 샌드박스 경로를 서빙 URL 로 치환 —
        # LLM 이 /work/out.png 를 그대로 인용해 깨진 이미지가 되는 것을 막는다(실측).
        for old_p, url in pm:
            out = out.replace(old_p, url).replace(old_p.replace("/", "\\/"), url)
        return out
    return s


def _cap_tool(tool, result_max=None):
    """도구 결과를 절단해 LLM 컨텍스트를 보호한다 — 대량 조회(예: VOC 수천 건)가 그대로
    프롬프트에 들어가면 'maximum context length' 400 으로 채팅이 죽는다(실측 16385/16384).
    result_max 를 주면 그 값으로 절단한다(결정적 JSON 조회는 CATALOG_RESULT_MAX 를 넘긴다)."""
    orig = tool.coroutine
    if orig is None:
        return tool

    def _norm(v):
        # MCP content-item 리스트 처리. str(list) 파이썬 repr 로 문자열화되면 URL 재작성·
        # 아티팩트 추출이 전부 우회된다(실측: 튜플 content 도 리스트).
        # 이미지 블록(type=image, base64 data)은 AIDH 가 JSON 밖 ImageContent 로 보내므로
        # 여기서 파일로 저장하고 URL 을 텍스트에 덧붙여야 LLM 이 ![](url) 로 인용할 수 있다.
        if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            texts = [str(i.get("text", "")) for i in v if "text" in i]
            urls = _stash_image_items(v)
            v = "".join(texts)
            if urls:
                v += "\n[생성된 이미지 URL — 답변에 ![설명](url) 로 포함하세요]\n" + "\n".join(urls)
        return _extract_artifacts_text(v)

    def _arg_hint() -> str:
        """도구 인자 스키마 요약 — 실패 시 되돌려 LLM 이 인자명·타입·단위를 교정하게 한다."""
        sc = getattr(tool, "args_schema", None)
        if not isinstance(sc, dict):
            return ""
        req = set(sc.get("required") or [])
        rows = []
        for k, v in list((sc.get("properties") or {}).items())[:14]:
            if isinstance(v, dict):
                rows.append(f"{k}{'*' if k in req else ''}:{v.get('type','any')}"
                            + (f" — {str(v.get('description',''))[:60]}" if v.get("description") else ""))
        return " | ".join(rows)

    def _maybe_repair_hint(s):
        # 인자 검증 실패(누락·타입·단위)는 스키마만 다시 보여주면 다음 시도에서 대개 교정된다.
        if not isinstance(s, str):
            return s
        head = s[:600].lower()
        # 실측 문구: "3 validation errors for …Arguments / Field required" (pydantic),
        # "missing required arg" (매니페스트 검증), "입력 스키마 위반" (heax 앱 규약).
        if any(k in head for k in ("missing required arg", "입력 스키마 위반", "validation error",
                                   "field required", "unexpected keyword", "invalid arguments",
                                   "error executing tool")):
            hint = _arg_hint()
            if hint:
                return s + f"\n[인자 스키마 — 이 형식으로 교정해 다시 호출하세요]\n{hint}"
        return s

    async def capped(*a, **kw):
        # 유령 ID 게이트 — 이번 턴 어디에도 없던 정수를 식별자 인자에 넣은 호출은 실행하지 않는다.
        # src 가 None 이면 챗 밖 경로(심의·카탈로그·테스트)이므로 fail-open 으로 통과시킨다.
        src = _turn_ids.get()
        if src is not None:
            # 어댑터 버전에 따라 인자가 kw 로 오기도, a 안의 dict 로 오기도 한다(실측은 kw).
            for _args in (kw, *(x for x in a if isinstance(x, dict))):
                bad = _phantom_id_arg(_args, src)
                if bad:
                    k, v = bad
                    # 운영 가시성 — 게이트가 무엇을 막았는지 남긴다. 조용히 막으면 "왜 도구가
                    # 안 도냐"는 문의에 근거가 없다.
                    print(f"[gate] {getattr(tool, 'name', '?')}({k}={v}) 차단 — 출처 정수 {len(src)}개")
                    msg = (f"{k}={v} {_PHANTOM_ID_MARK}. 추측한 ID 로 부르면 "
                           f"다른 대상의 진짜 데이터가 돌아와 더 위험하다. 같은 앱의 "
                           f"list_*/search_* 도구로 이름을 조회해 ID 를 확인한 뒤 다시 호출하라. "
                           f"조회할 때는 이름 조각만 넣고 필터는 지어내지 마라 — 결과가 비면 "
                           f"필터를 빼고 더 짧은 조각으로 다시 찾아라.")
                    return ((msg, None)
                            if getattr(tool, "response_format", "") == "content_and_artifact" else msg)
        try:
            out = await orig(*a, **kw)
        except Exception as exc:  # noqa: BLE001 — 인자 검증 실패는 코루틴 안에서 raise 된다(실측).
            # 예외를 그대로 올리면 LangChain 이 원문만 보여줘 LLM 이 같은 실수를 반복한다.
            # 스키마를 실어 돌려주면 다음 시도에서 인자명·타입·단위가 교정된다.
            # ⚠ 모든 예외에 '인자를 고쳐 다시 호출' 힌트를 붙이면 안 된다. 타임아웃·백엔드
            # 다운·연결거부까지 인자 오류로 둔갑해, 모델이 멀쩡한 인자를 바꿔가며 재호출을
            # 반복하고 대기 시간만 배로 늘린다(감사 확인). 인자 문제로 보일 때만 힌트를 준다.
            _e = f"{type(exc).__name__}: {exc}"[:600].lower()
            _is_transport = any(k in _e for k in (
                "timeout", "timed out", "connect", "connection", "unavailable", "refused",
                "backend", "502", "503", "504", "cancelled", "closed"))
            _is_argerr = (not _is_transport) and any(k in _e for k in (
                "validation error", "field required", "missing required arg", "unexpected keyword",
                "invalid arguments", "입력 스키마 위반", "type_error", "value_error"))
            hint = _arg_hint() if _is_argerr else ""
            msg = f"도구 {getattr(tool, 'name', '?')} 호출 실패: {str(exc)[:500]}"
            if hint:
                msg += f"\n[인자 스키마 — 이 형식으로 교정해 다시 호출]\n{hint}"
            elif _is_transport:
                # 인자를 바꿔봐야 소용없다는 것을 모델에게 명시해 무의미한 재시도를 끊는다.
                msg += ("\n[안내] 인자 문제가 아니라 도구 백엔드 연결/시간초과다. "
                        "같은 도구를 인자만 바꿔 다시 호출하지 말고, 다른 방법으로 답하거나 "
                        "사용자에게 해당 백엔드가 일시적으로 불가하다고 알려라.")
            # response_format 계약 준수 — content_and_artifact 도구는 2-튜플을 요구한다(실측).
            return (msg, None) if getattr(tool, "response_format", "") == "content_and_artifact" else msg
        if isinstance(out, tuple) and len(out) == 2:  # (content, artifact) 형식 보존
            body = _maybe_repair_hint(_cap(_norm(out[0]), result_max))
            _learn_ids(body)
            return (body, out[1])
        body = _maybe_repair_hint(_cap(_norm(out), result_max))
        _learn_ids(body)
        return body

    tool.coroutine = capped
    return tool


def _cap_desc(s, limit=None):
    limit = limit or TOOL_DESC_MAX
    if isinstance(s, str) and len(s) > limit:
        return s[:limit] + "…"
    return s


def _slim_tool(tool, desc_max=None):
    """도구 스키마를 슬림하게 — 99개 도구의 긴 description 이 통째로 프롬프트에 들어가면
    16K 모델에서 첫 호출부터 'maximum context length' 400 이 난다(실측 16385/16384).
    tool.description 을 TOOL_DESC_MAX 로 절단하고, args_schema 가 JSON 스키마 dict 면
    각 필드 description 도 같은 캡을 적용한다(pydantic 모델이면 건드리지 않음).

    desc_max 를 주면 그 값으로 절단한다. 이 캡은 **바인딩 프롬프트 보호용**이라, 도구를 LLM 에
    바인딩하지 않는 경로(심의의 인자 구성)에서는 오히려 해가 된다 — 실측: TOOL_DESC_MAX=120 인
    박스에서 get_mat_card 설명이 'units: ton_…' 에서 잘려 유효값(ton_mm_s·kg_m_s·g_mm_ms·
    kg_mm_ms)과 model 선택지가 사라졌고, 인자 계획자가 units="MPa" 를 지어냈다."""
    try:
        tool.description = _cap_desc(tool.description, desc_max)
        schema = getattr(tool, "args_schema", None)
        if isinstance(schema, dict):  # MCP 어댑터 도구는 JSON 스키마 dict — 필드 description 도 절단
            for prop in schema.get("properties", {}).values():
                if isinstance(prop, dict) and isinstance(prop.get("description"), str):
                    prop["description"] = _cap_desc(prop["description"], desc_max)
    except Exception as exc:  # 슬림 실패해도 도구 자체는 살린다
        print(f"[agent] tool slim skipped for {getattr(tool, 'name', '?')}: {exc!r}")
    return tool


def _tag_tool_app(tool):
    """도구 설명 앞에 소유 앱 라벨을 붙인다 — get_*/list_* 처럼 이름이 겹치는 도구가 여러 앱에
    걸쳐 있어(실측: get_* 7개 앱) LLM 이 도메인을 혼동한다. 이름을 안 바꾸므로 호출 계약은 불변."""
    try:
        _, label = _group_of(getattr(tool, "name", ""))
        if label and label != "기타" and not str(tool.description or "").startswith("["):
            tool.description = f"[{label}] {tool.description or ''}"
    except Exception:  # noqa: BLE001 — 태깅 실패해도 도구는 살린다
        pass
    return tool


def _attach_validation_hint(tool):
    """인자 검증 실패는 코루틴 진입 전(StructuredTool.ainvoke 의 pydantic 검증)에 발생해
    결과 래퍼로는 못 잡는다(실측). LangChain 의 handle_validation_error 훅에 스키마 힌트를
    실어, 인자명·타입·단위를 틀려도 다음 시도에서 교정되게 한다."""
    try:
        sc = getattr(tool, "args_schema", None)
        hint = ""
        if isinstance(sc, dict):
            req = set(sc.get("required") or [])
            rows = []
            for k, v in list((sc.get("properties") or {}).items())[:14]:
                if isinstance(v, dict):
                    rows.append(f"{k}{'*' if k in req else ''}:{v.get('type','any')}"
                                + (f" — {str(v.get('description',''))[:60]}" if v.get("description") else ""))
            hint = " | ".join(rows)
        if hint:
            tool.handle_validation_error = (
                lambda e, _h=hint: f"인자 오류: {e}\n[인자 스키마 — 이 형식으로 교정해 다시 호출]\n{_h}")
    except Exception:  # noqa: BLE001 — 훅 부착 실패해도 도구는 살린다
        pass
    return tool


def _prep_tool(tool, result_max=None, desc_max=None):
    """도구 로드 직후 한 번에 적용하는 체인: 앱 태깅 + 검증힌트 + 스키마 슬림 + 결과 절단.
    result_max·desc_max 는 각각 결과·설명 절단 한도 — None 이면 LLM 경로 기본값."""
    return _cap_tool(_slim_tool(_attach_validation_hint(_tag_tool_app(tool)), desc_max), result_max)


# ── 컨텍스트 예산 (운영 타깃 = GLM on B300, 128K 창 기준) ────────────────────────────
# 기본값은 **대형 컨텍스트 기준으로 넉넉하게** 잡는다. 작은 박스(dev qwen 16K)만 .env 로 낮춘다 —
# 반대로 잡으면 운영이 dev 크기에 묶인다. 128K 를 대략 이렇게 나눈 값이다.
#   도구 스키마 40K · 대화 이력 200K자(≈57K토큰, 최신 우선으로 잘림) · 도구 결과 1건 200K자 상한
#   · 역할 8K자 · 지식카드 12K자 · 나머지는 추론·출력
# ⚠ 각 상한은 **따로** 적용된다 — 합이 창을 넘을 수 있다. 넘으면 상류가 400 을 주고 그 턴은
#   중단 문구로 끝난다(무음 아님). 로그의 `[agent] tool budget:` 줄이 잘린 개수를 알려 준다.
# 소형 컨텍스트(dev 16K) 보호 — 도구 스키마 총량이 프롬프트를 넘치면 LLM 400으로 챗 전체가 죽는다.
# TOOL_MAX(0=무제한, prod 기본)로 바인딩 개수를 캡하고, 자주 쓰는 핵심 도구를 우선 남긴다.
# 기본 80 — 0(무제한)이면 랭킹이 아예 안 돌아 도구가 수백 개가 될 때 평평하게 쏟아진다.
# 명시적으로 TOOL_MAX=0 을 주면 종전처럼 전체 바인딩(탈출구 유지).
TOOL_MAX = int(os.environ.get("TOOL_MAX", "200"))
# 그래프 재귀 한도 — 미설정 시 LangGraph 기본(25)에 걸려 턴 전체가 폐기된다. 넉넉히 두되
# 무한은 아니게. 도구 왕복 1회가 노드 2~3개를 쓴다.
AGENT_RECURSION_LIMIT = int(os.environ.get("AGENT_RECURSION_LIMIT", "80"))
# 같은 도구·같은 인자 반복 호출 경고 임계 — 작은 모델의 루프를 사용자에게 드러낸다.
TOOL_REPEAT_WARN = int(os.environ.get("TOOL_REPEAT_WARN", "3"))
# 바인딩 도구 스키마의 추정 토큰 상한(0=무제한). 개수 캡만으로는 컨텍스트 초과를 못 막는다.
TOOL_SCHEMA_BUDGET = int(os.environ.get("TOOL_SCHEMA_BUDGET", "40000"))
_TOOL_PRIORITY = (
    # 안내대 — 관련도 랭킹이 빗나갔을 때의 폴백 입구. 반드시 상시 바인딩한다(2026-09-03,
    # 도구 진입 구조). search_tools 가 이름·인자를 찾아 주고, invoke_tool 이 바인딩에 없는
    # 도구를 **그 턴에 즉시** 호출한다(파괴·제어성 도구는 게이트웨이가 차단).
    "search_tools", "invoke_tool", "list_tool_apps",
    "recommend_agents", "get_agent_session", "agent_search", "semantic_search", "list_records",
    "data_aggregate", "alert_check", "daily_briefing", "query_voc", "search_voc", "get_top_issues",
    "create_report_draft", "update_report_draft", "search_reports", "list_templates",
    "analyze_laminate", "evaluate_laminate", "solve_load_response", "list_materials", "plot_ashby",
    "search_documents", "search_knowledge", "get_material", "compare_products",
    # 실측 보강(감사 8,916건 상위권인데 종전 core 에 없던 것들 — 상위 40개가 호출의 79%).
    "get_record_sections", "list_agents", "get_material_properties", "search_catalog_property",
    "get_curve", "get_context_bundle", "get_fits", "get_mat_card", "get_reports_digest",
    "get_guide",
)
# HE팀 운영자의 상시 예약 — 안내대만 남긴다. 운영자는 자기 앱 도구가 핀으로 이미 앞자리를 차지하는데,
# 위 핵심 38개(VOC·적층·보고서…)까지 예산 밖에서 덧붙이면 그 앱과 무관한 도구가 절반이 되고 작은
# 컨텍스트에서는 초과 400 으로 턴이 죽는다(실측 dev 16K: StepForge 운영자 77개 → 16,385토큰).
# 다른 앱이 필요하면 recommend_agents·list_tool_apps 로 안내하고, 바인딩 밖 도구는 invoke_tool 로 부른다.
_GUIDE_TOOLS = ("search_tools", "invoke_tool", "list_tool_apps", "recommend_agents")


# ── 도구 시맨틱 검색 — AIDH 다국어 e5 임베더 재사용(모델 중복 로딩 없음) ─────────────
# 어휘 매칭은 정확한 도구명에 강하고 임베딩은 표현이 다른 질의("김서림 방지" ↔ fogging)에
# 강하다 → 둘을 RRF 로 합친다. e5 는 절대 유사도가 전반적으로 높아(무관 쌍도 0.8+) 임계값이
# 아니라 상대 순위로만 쓴다.
AIDH_HTTP = os.environ.get("AIDH_HTTP_BASE", "http://127.0.0.1:8001")
_TOOL_VEC: dict = {"sig": "", "names": [], "vecs": []}


# 임베딩 배치 크기 — 한 요청에 전량(도구 196개)을 보내면 GPU 여유에 따라 터진다. 실측은
# 엇갈렸다: GPU 가 붐빌 때 128개부터 HTTP 500 "CUDA out of memory"(agent-server.log 에
# '[tools] embed failed: <HTTPError 500>' 반복), 한산할 때는 196개도 통과. 즉 상시 고장이
# 아니라 부하 의존 간헐 고장이다 — 더 나쁘다. 실패해도 예외를 삼키고 넘어가므로(_ensure_tool_vecs),
# _semantic_order 가 빈 리스트를 돌려 RRF 융합이 조용히 죽고 도구 랭킹이 '어휘+상시핵심+
# 알파벳순'으로 퇴화한 채 아무 신호도 남지 않는다. 64개는 양쪽 조건 모두에서 통과했다.
EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "64"))


def _embed(texts: list, kind: str = "passage") -> list:
    import urllib.request
    if not texts:
        return []
    out: list = []
    for i in range(0, len(texts), EMBED_BATCH):
        chunk = texts[i:i + EMBED_BATCH]
        body = json.dumps({"texts": chunk, "kind": kind}).encode()
        req = urllib.request.Request(f"{AIDH_HTTP}/api/embed", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            out.extend((json.loads(r.read()) or {}).get("vectors") or [])
    return out


def _ensure_tool_vecs(tools: list) -> None:
    """도구 벡터 캐시 — 도구 구성이 바뀔 때만 재계산(수백 개도 수 초)."""
    import hashlib
    names = sorted(getattr(t, "name", "") for t in tools)
    # 개수+첫/끝만 보면 중간 교체를 놓친다 — 전체 이름 해시로 정확히 무효화.
    sig = hashlib.sha256("\n".join(names).encode()).hexdigest()[:16]
    if _TOOL_VEC["sig"] == sig and _TOOL_VEC["vecs"]:
        return
    by = {getattr(t, "name", ""): t for t in tools}
    texts = [f"{n}: {(getattr(by[n], 'description', '') or '')[:400]}" for n in names]
    try:
        vecs = _embed(texts, "passage")
    except Exception as exc:  # noqa: BLE001 — 임베딩 미가용이면 어휘 매칭만으로 동작(회귀 0)
        print(f"[tools] embed failed: {exc!r}")
        return
    if len(vecs) == len(names):
        _TOOL_VEC.update({"sig": sig, "names": names, "vecs": vecs})
        print(f"[tools] semantic index: {len(names)}개")


def _semantic_order(query: str, tools: list) -> list:
    """질의 임베딩 코사인 내림차순 도구 이름 목록(미가용이면 빈 목록)."""
    if not query.strip():
        return []
    _ensure_tool_vecs(tools)
    if not _TOOL_VEC["vecs"]:
        return []
    try:
        qv = _embed([query], "query")
    except Exception:  # noqa: BLE001
        return []
    if not qv:
        return []
    import math
    q = qv[0]
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    scored = []
    for n, v in zip(_TOOL_VEC["names"], _TOOL_VEC["vecs"]):
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        scored.append((sum(a * b for a, b in zip(q, v)) / (qn * vn), n))
    scored.sort(reverse=True)
    return [n for _, n in scored]


def _rrf(*orders, k: int = 60) -> dict:
    """Reciprocal Rank Fusion — 어휘·시맨틱 순위를 점수 스케일 차이 없이 결합."""
    out: dict = {}
    for order in orders:
        for i, name in enumerate(order):
            out[name] = out.get(name, 0.0) + 1.0 / (k + i + 1)
    return out


# 웹 리서치 소스별 도구 — 토글이 꺼지면 **바인딩하지 않는다**. 모델에게 "쓰지 마라"고
# 지시하는 것과 도구가 존재하지 않는 것은 다르다. 사내 질의가 외부로 나가는 기능에서
# 그 차이는 사고와 안전의 차이다. 나가지 않는 도구(search_internal 등)는 대상이 아니다.
_SOURCE_TOOLS = {"scholar": {"search_scholar"}, "web": {"search_web"}}
_ALL_SOURCE_TOOLS = {n for v in _SOURCE_TOOLS.values() for n in v}


def gate_sources(tools: list, sources: list[str] | None) -> list:
    """켜지 않은 소스의 도구를 목록에서 뺀다. sources 가 None 이면 종전대로 전부 통과."""
    if sources is None:
        return tools
    allow = {n for k in sources if k in _SOURCE_TOOLS for n in _SOURCE_TOOLS[k]}
    return [t for t in tools
            if getattr(t, "name", "") not in _ALL_SOURCE_TOOLS
            or getattr(t, "name", "") in allow]


def _select_tools(tools: list, query: str = "", pinned: list[str] | None = None,
                  first: list[str] | None = None, core_names: tuple | None = None) -> list:
    """질의 관련도 기반 도구 선택 — 도구가 수백 개로 늘어도 '알파벳 순 절단'이 아니라
    '이 질문에 필요한 것부터' 남긴다. 우선순위: ① 콕 집은 도구(first) ② 사용자 지정(핀)
    ③ 질의 어휘 관련도 ④ 상시 핵심(라우팅·검색) ⑤ 나머지. 캡 미설정(0)이면 전부 바인딩(회귀 0).

    first 는 핀 중에서도 먼저 사는 것이다 — 앱 하나를 통째로 핀하면 도구가 캡(dev 40)을 넘는데
    (StepForge 81·ReportArchive 70), 핀끼리는 관련도로만 갈려 사용자가 콕 집은 도구나 HE팀
    운영자의 입구 도구(get_guide·list_projects 등)가 캡 밖으로 밀려났다.

    core_names 는 상시 핵심 예약을 바꾼다(기본 _TOOL_PRIORITY). HE팀 운영자는 _GUIDE_TOOLS 만 쓴다."""
    if TOOL_MAX <= 0 or len(tools) <= TOOL_MAX:
        return tools
    pin = set(pinned or []) | set(first or [])
    top = set(first or [])
    core_list = _TOOL_PRIORITY if core_names is None else core_names
    core = {n: i for i, n in enumerate(core_list)}
    # 관련도 — 이름+설명 어휘 겹침(_rank_tools 와 동일 원리, 여기선 전 도구 대상 점수만).
    qtok = _tok_query(query)
    def rel(t) -> float:
        if not qtok:
            return 0.0
        prof = f"{getattr(t,'name','')} {getattr(t,'description','') or ''}".lower().replace("_", " ")
        hit = sum(1 for k in qtok if k in prof)
        return hit / len(qtok)
    # ⚠ 어휘 무매치(rel=0) 도구는 lex_order 에 넣지 않는다. 넣으면 '알파벳 순 전체 순열'이
    # 정상 랭킹인 척 RRF 에 들어가, 이름이 앞선 무관 도구가 진짜 관련 도구를 캡 밖으로 밀어낸다
    # (감사 실측: query_voc/search_voc 가 탈락해 모델이 product_code 를 지어내고 다른 제품
    # 통계를 답으로 내놓음 — 빈 결과보다 나쁜 '확신에 찬 오답').
    lex_order = [n for _, n in sorted(((-rel(t), getattr(t, "name", "")) for t in tools if rel(t) > 0))]
    sem_order = _semantic_order(query, tools)
    fused = _rrf(lex_order, sem_order) if sem_order else {}
    scored = [(t, rel(t)) for t in tools]
    ordered = sorted(scored, key=lambda x: (
        0 if getattr(x[0], "name", "") in top else 1 if getattr(x[0], "name", "") in pin else 2,  # 콕 집은 것 → 핀
        -round(fused.get(getattr(x[0], "name", ""), 0.0), 6),    # 어휘+시맨틱 융합 순위
        -round(x[1], 3),                                          # 어휘 관련도(융합 미가용 시)
        core.get(getattr(x[0], "name", ""), len(core)),           # 상시 핵심
        getattr(x[0], "name", ""),
    ))
    kept = [t for t, _ in ordered[:TOOL_MAX]]
    # 토큰 예산 — TOOL_MAX 는 '개수' 캡이라 실제 프롬프트 비용을 전혀 보장하지 못한다.
    # 스키마가 큰 도구가 몰리면 캡을 지켜도 컨텍스트 초과 400 으로 챗이 죽고, 사용자는
    # '에이전트 처리 중 오류'만 본다(감사: dev 로그에 동일 400 3건). 개수와 별개로 추정
    # 토큰 합계에도 상한을 걸어, 넘치면 관련도 낮은 것부터 떨어뜨린다.
    if TOOL_SCHEMA_BUDGET > 0:
        def _cost(t) -> int:
            try:
                sch = json.dumps(getattr(t, "args_schema", None) or {}, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                sch = ""
            # 한글·JSON 혼합에서 대략 3자 ≈ 1토큰(보수적으로 과대평가해 안전측).
            return (len(getattr(t, "name", "")) + len(getattr(t, "description", "") or "") + len(sch)) // 3 + 8
        total, budgeted = 0, []
        for t in kept:
            c = _cost(t)
            if budgeted and total + c > TOOL_SCHEMA_BUDGET:
                continue
            budgeted.append(t); total += c
        if len(budgeted) < len(kept):
            print(f"[agent] tool budget: {len(kept)}개 → {len(budgeted)}개 (추정 {total}토큰 / 상한 {TOOL_SCHEMA_BUDGET})")
        kept = budgeted
    # 핵심 도구 예약 슬롯 — 정렬키의 core 타이브레이크는 fused 값이 사실상 유일해 발동하지
    # 않는다(감사: fused 고유값 167/168 → docstring 의 '③ 상시 핵심'은 죽은 코드였다).
    # 그래서 순위와 별개로 존재하는 core 전량을 확보한다. 상위 N개만 예약하면 임베딩이
    # 죽은 경로에서 오히려 회귀하므로(감사 검증) 개수를 자르지 않는다.
    if TOOL_MAX > 0:
        kept_names = {getattr(t, "name", "") for t in kept}
        by_name = {getattr(t, "name", ""): t for t in tools}
        missing_core = [by_name[n] for n in core_list
                        if n in by_name and n not in kept_names]
        if missing_core:
            # 뒤에서부터(=관련도 낮은 것부터) 비핀 도구를 밀어내고 core 를 넣는다.
            keep_head = [t for t in kept if getattr(t, "name", "") in pin]
            rest = [t for t in kept if getattr(t, "name", "") not in pin]
            room = max(0, TOOL_MAX - len(keep_head) - len(missing_core))
            kept = keep_head + missing_core + rest[:room]
    n_rel = sum(1 for t, sc in ordered[:TOOL_MAX] if sc > 0)
    print(f"[agent] tool select: {len(tools)}개 → {len(kept)}개 (질의관련 {n_rel}, 핀 {len(pin)})")
    return kept


# ── 도구 검색/카탈로그 — 사용자가 직접 도구를 확인·선택하게 하는 진입점 ─────────────
# 질의 토큰(조사·불용어 제거)과 도구 name+description 의 어휘 겹침으로 관련도 랭킹.
# recommend_svc 의 어휘 매칭과 같은 원리 — CJK 단일자(휨 등) 유지, 범용어 제거.
_TOOL_STOP = {
    "도구", "툴", "tool", "tools", "검색", "추천", "찾아", "찾아줘", "알려", "알려줘", "목록",
    "리스트", "보여", "보여줘", "가능", "사용", "쓸", "뭐", "뭐가", "무엇", "무슨", "어떤",
    "있어", "있냐", "있나", "관련", "대해", "대한", "해줘", "좀", "the", "a", "for", "what",
    "which", "list", "show", "available", "use",
}
_JOSA_T = ("으로", "에서", "에게", "까지", "부터", "이나", "을", "를", "이", "가", "은", "는",
           "의", "에", "과", "와", "도", "로", "만")


def _tok_query(text: str) -> set[str]:
    out = set()
    for t in re.split(r"[\s,./·|()\[\]{}:;\"'?!]+", (text or "").lower()):
        if not t:
            continue
        for j in sorted(_JOSA_T, key=len, reverse=True):   # 조사 제거(어간 2자+ 남을 때만)
            if t.endswith(j) and len(t) - len(j) >= 2:
                t = t[: -len(j)]
                break
        if t in _TOOL_STOP:
            continue
        if len(t) >= 2 or any("가" <= ch <= "힣" for ch in t):
            out.add(t)
    return out


# 도구 → 소유 MCP 앱(백엔드) 매핑 — 166개가 평평하게 쏟아지면 고르기 어려워, 게이트웨이의
# /tools-map 으로 '어느 앱의 기능인지'를 붙여 계층 선택이 되게 한다(짧은 TTL 캐시).
_GW_HTTP = os.environ.get("GATEWAY_HTTP_BASE", "http://127.0.0.1:9110")
_TOOLS_MAP_CACHE: dict = {"at": 0.0, "map": {}, "areas": {}, "area_meta": {}}
_GROUP_LABEL = {
    "ai-data-hub": "AI 데이터 허브", "mx-white-paper": "MX 백서", "reportarchive": "리포트 아카이브",
    "signalforge": "SignalForge VOC", "smart-twin-cluster": "시뮬레이션 클러스터",
    "heax-thermal_shock_mcp": "열충격 해석", "heax-materialtwin_web": "재료 물성(MaterialTwin)",
    "heax-laminate_analyzer_mcp": "적층 복합재 해석", "heax-web_design_agents": "웹 디자인 에이전트",
    "_gateway": "게이트웨이 공통",
}


_APPS_CACHE: dict = {"apps": {}, "at": 0.0}


def _gw_apps() -> dict:
    """게이트웨이 /tools-map 의 apps[] — {앱키: {label, description, tool_count, reachable}}.

    라벨의 정본은 게이트웨이다(heax 앱은 registry name 을 그대로 싣는다). 여기 없을 때만
    _GROUP_LABEL 표로 폴백한다 — 하드코딩 표는 신규 앱이 붙을 때마다 틀린 이름을 노출한다."""
    import time
    import urllib.request
    now = time.time()
    if _APPS_CACHE["apps"] and now - _APPS_CACHE["at"] < 300:
        return _APPS_CACHE["apps"]
    try:
        with urllib.request.urlopen(f"{_GW_HTTP}/tools-map", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        apps = {a["app"]: a for a in (data.get("apps") or []) if a.get("app")}
        if apps:
            _APPS_CACHE.update({"apps": apps, "at": now})
    except Exception:  # noqa: BLE001 — 게이트웨이 불통은 비치명적. 폴백 표로 계속 간다.
        pass
    return _APPS_CACHE["apps"]


def _app_label(key: str) -> str:
    a = _gw_apps().get(key) or {}
    return (a.get("label") or "").strip() or _GROUP_LABEL.get(key) or _pretty_group(key)


def _app_desc(key: str) -> str:
    return ((_gw_apps().get(key) or {}).get("description") or "").strip()


def _tools_map() -> dict:
    import time
    import urllib.request
    now = time.time()
    if _TOOLS_MAP_CACHE["map"] and now - _TOOLS_MAP_CACHE["at"] < 300:
        return _TOOLS_MAP_CACHE["map"]
    try:
        with urllib.request.urlopen(f"{_GW_HTTP}/tools-map", timeout=5) as r:
            data = json.loads(r.read()) or {}
        m = data.get("map") or {}
        if m:
            # 영역(tool_areas.json)도 같은 응답에 온다 — 따로 받지 않는다. 구 게이트웨이면 빈 채로
            # 두어 도구가 '미분류' 로 보인다(분류를 지어내지 않는다).
            _TOOLS_MAP_CACHE.update({"at": now, "map": m, "areas": data.get("areas") or {},
                                     "area_meta": {a["area"]: a for a in (data.get("area_meta") or [])
                                                   if isinstance(a, dict) and a.get("area")}})
    except Exception as exc:  # noqa: BLE001 — 매핑 실패 시 그룹 없이 동작(회귀 0)
        print(f"[tools] map fetch failed: {exc!r}")
    return _TOOLS_MAP_CACHE["map"]


def _pretty_group(key: str) -> str:
    """미등록 앱의 표시 이름 자동 생성 — 새 MCP 앱이 붙어도 코드 수정 없이 읽을 만하게 뜬다.
    예: heax-voice_recorder → Voice Recorder, my-new-app → My New App."""
    if not key:
        return "기타"
    k = key[5:] if key.startswith("heax-") else key
    k = k.removesuffix("_mcp").removesuffix("-mcp")
    words = [w for w in re.split(r"[-_\s]+", k) if w]
    return " ".join(w if w.isupper() else w.capitalize() for w in words) or key


def _group_of(name: str) -> tuple:
    key = _tools_map().get(name, "")
    return key, (_app_label(key) if key else "")


def _area_of(name: str) -> tuple:
    """도구 → (영역 키, 영역 라벨) — 게이트웨이 tool_areas.json 판정 그대로. 미분류는 ('', '')."""
    _tools_map()  # 캐시 갱신(영역은 같은 응답에 실려 온다)
    key = _TOOLS_MAP_CACHE["areas"].get(name, "")
    return key, ((_TOOLS_MAP_CACHE["area_meta"].get(key) or {}).get("label") or "" if key else "")


def _rank_tools(tools: dict, query: str, top_k: int = 12) -> list[dict]:
    """게이트웨이 도구를 질의 관련도순으로 — [{name, desc, score}] (score>0 만)."""
    qtok = _tok_query(query)
    if not qtok:
        return []
    scored = []
    for name, t in tools.items():
        profile = f"{name} {getattr(t, 'description', '') or ''}".lower().replace("_", " ")
        hits = sum(1 for tk in qtok if tk in profile)
        if hits:
            scored.append((hits / len(qtok), name, (getattr(t, "description", "") or "")[:160]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    # 시맨틱 순위와 융합 — 어휘로 못 잡는 표현 차이(김서림↔fogging)를 임베딩이 보완.
    sem = _semantic_order(query, list(tools.values()))
    if sem:
        lex = [n for _, n, _ in scored]
        fused = _rrf(lex, sem)
        pool = {n: (s_, d_) for s_, n, d_ in scored}
        for n in sem[:top_k * 2]:
            if n not in pool and n in tools:
                pool[n] = (0.0, (getattr(tools[n], "description", "") or "")[:160])
        scored = sorted(((v[0], n, v[1]) for n, v in pool.items()),
                        key=lambda x: -fused.get(x[1], 0.0))
    out = []
    for sc, n, d in scored[:top_k]:
        gk, gl = _group_of(n)
        ak, al = _area_of(n)
        out.append({"name": n, "desc": d, "score": round(sc, 3), "group": gk, "group_label": gl,
                    "area": ak, "area_label": al})
    return out


def _tool_catalog(tools: dict) -> list[dict]:
    out = []
    for n, t in sorted(tools.items()):
        gk, gl = _group_of(n)
        ak, al = _area_of(n)
        out.append({"name": n, "desc": (getattr(t, "description", "") or "")[:160],
                    "group": gk, "group_label": gl, "area": ak, "area_label": al})
    return out


def _app_catalog(tools: dict) -> list[dict]:
    """카탈로그에 실린 도구가 속한 앱 목록 — 앱 단위 선택(pinned_apps) UI 의 입력.

    개수는 게이트웨이의 tool_count 가 아니라 지금 이 사용자에게 보이는 도구로 센다.
    그룹 권한으로 걸러진 뒤라 두 값이 다를 수 있고, 화면 개수와 실제 실릴 개수가
    어긋나면 사용자가 고른 것과 다른 것이 실린다."""
    n_by: dict[str, int] = {}
    for n in tools:
        gk, _ = _group_of(n)
        if gk:
            n_by[gk] = n_by.get(gk, 0) + 1
    return [{"app": k, "label": _app_label(k), "desc": _app_desc(k)[:200], "tool_count": v}
            for k, v in sorted(n_by.items(), key=lambda kv: -kv[1])]


def _area_catalog(tools: dict) -> list[dict]:
    """도구 영역 목록 — 도구 선택 UI 의 1단(하는 일로 고르기). _app_catalog 와 같은 규칙으로
    **이 사용자에게 보이는 도구**만 센다. 순서는 분류표(tool_areas.json)에 적힌 순서다 — 사람이
    읽는 순서(CAD → 메시 → 실행 → 계산 → 결과 …)라 인원순으로 섞지 않는다.
    미분류가 있으면 끝에 area='' 로 붙인다 — 숨기면 새 앱의 도구가 영역 보기에서 사라진다."""
    _tools_map()
    meta = _TOOLS_MAP_CACHE["area_meta"]
    n_by: dict[str, int] = {}
    for n in tools:
        ak, _ = _area_of(n)
        n_by[ak] = n_by.get(ak, 0) + 1
    out = [{"area": k, "label": m.get("label") or k, "desc": (m.get("description") or "")[:200],
            "tool_count": n_by[k]} for k, m in meta.items() if n_by.get(k)]
    if n_by.get(""):
        out.append({"area": "", "label": "미분류", "desc": "영역 분류표에 없는 도구", "tool_count": n_by[""]})
    return out


# 에이전트(전문가) 검색 — 도구와 같은 구조적 문제. LLM 에 659명을 나열시키면 결과 절단 캡에
# 걸려 중간에 잘린다. 코드가 결정적으로 추천+분야 요약을 만들어 답한다(LLM 미경유).
_AGENT_SEARCH_TRIGGERS = ("/전문가", "/에이전트", "/agents", "/agent")
_AGENT_SEARCH_RE = re.compile(
    r"(전문가|에이전트|agent)\w*\s*(검색|추천|찾|알려|목록|리스트|보여|뭐|무엇|어떤|있)", re.IGNORECASE)


def is_agent_search(message: str) -> bool:
    m = (message or "").strip()
    if any(m.startswith(t) for t in _AGENT_SEARCH_TRIGGERS):
        return True
    return len(m) <= 80 and bool(_AGENT_SEARCH_RE.search(m))


def strip_agent_trigger(message: str) -> str:
    m = (message or "").strip()
    for t in _AGENT_SEARCH_TRIGGERS:
        if m.startswith(t):
            return m[len(t):].strip()
    return m


async def run_agent_search(app: FastAPI, query: str, groups: list[str]):
    """전문가 카탈로그 SSE — 추천(관련도순) + 분야별 인원 요약을 결정적으로 만든다."""
    yield _sse("status", {"step": "전문가 검색 중", "tool": "recommend_agents"})
    try:
        tools = await _tools_by_name(app, groups, CATALOG_RESULT_MAX)  # 결과를 코드가 파싱 — 절단 금지
    except Exception as exc:  # noqa: BLE001
        print(f"[agents] tools load failed: {exc!r}")
        tools = {}
    if not tools:
        msg = "게이트웨이 도구를 불러오지 못했습니다 — 게이트웨이 상태를 확인하세요."
        yield _sse("token", {"delta": msg}); yield _sse("result", {"type": "text", "content": msg})
        yield _sse("done", {}); return

    rec = []
    if query.strip():
        try:
            recd = _parse_json(await _call(tools, "recommend_agents", {"q": query, "top_k": 10}))
            items = recd if isinstance(recd, list) else (
                (recd or {}).get("recommendations") or (recd or {}).get("agents") or (recd or {}).get("data") or [])
            for it in (items or [])[:10]:
                it = _first_dict(it)
                k = it.get("agent_type") or it.get("id")
                if k:
                    rec.append({"key": k, "name": it.get("name") or k,
                                "desc": (it.get("description") or "")[:160]})
        except Exception as exc:  # noqa: BLE001
            print(f"[agents] recommend failed: {exc!r}")

    pool = []
    try:
        for a in _parse_json_multi(await _call(tools, "list_agents", {"compact": True})):
            a = _first_dict(a)
            k = a.get("agent_type") or a.get("id")
            if k:
                pool.append({"key": k, "name": a.get("name") or k})
    except Exception as exc:  # noqa: BLE001
        print(f"[agents] list_agents failed: {exc!r}")

    doms: dict = {}
    for a in pool:
        d = a["key"].split("-")[0] or "기타"
        doms[d] = doms.get(d, 0) + 1
    yield _sse("agents", {"query": query, "recommended": rec, "pool": pool,
                          "domains": sorted(doms.items(), key=lambda x: -x[1])})

    lines = [f"전문가 {len(pool)}명이 등록돼 있습니다."]
    if rec:
        lines.append(f"\n'{query}' 관련 추천 {len(rec)}명:")
        lines += [f"- {r['name']} (`{r['key']}`)" + (f" — {r['desc'][:90]}" if r["desc"] else "") for r in rec]
    if doms:
        top = ", ".join(f"{d} {n}명" for d, n in sorted(doms.items(), key=lambda x: -x[1])[:10])
        lines.append(f"\n분야별: {top}")
    lines.append("\n특정 전문가와 대화하려면 챗 시작 화면의 '전문가·도구 고르고 시작'에서 고르세요.")
    text = "\n".join(lines)
    yield _sse("token", {"delta": text})
    yield _sse("result", {"type": "text", "content": text})
    yield _sse("done", {})


_TOOL_SEARCH_TRIGGERS = ("/도구", "/tools", "/tool")
_TOOL_SEARCH_RE = re.compile(r"(도구|툴|tool)\w*\s*(검색|추천|찾|알려|목록|리스트|보여|뭐|무엇|어떤|있)", re.IGNORECASE)


def is_tool_search(message: str) -> bool:
    m = (message or "").strip()
    if any(m.startswith(t) for t in _TOOL_SEARCH_TRIGGERS):
        return True
    # 짧은 발화의 '도구 뭐 있어/추천해줘' 류만 — 긴 작업 지시문('~도구를 사용해 분석하라')은 오탐 방지
    return len(m) <= 80 and bool(_TOOL_SEARCH_RE.search(m))


def strip_tool_trigger(message: str) -> str:
    m = (message or "").strip()
    for t in _TOOL_SEARCH_TRIGGERS:
        if m.startswith(t):
            return m[len(t):].strip()
    return m


async def run_tool_search(app: FastAPI, query: str, groups: list[str]):
    """도구 카탈로그 SSE — 코드가 결정적으로 처리(LLM 미경유). 프론트가 tools 이벤트를 받아
    선택 UI 를 그리고, 사용자가 고른 도구는 다음 발화부터 pinned_tools 로 실린다."""
    yield _sse("status", {"step": "사용 가능 도구 검색 중", "tool": None})
    try:
        tools = await _tools_by_name(app, groups)
    except Exception as exc:  # noqa: BLE001
        print(f"[tools] load failed: {exc!r}")
        tools = {}
    if not tools:
        yield _sse("result", {"type": "text", "content": "게이트웨이 도구를 불러오지 못했습니다 — 게이트웨이 상태를 확인하세요."})
        yield _sse("done", {})
        return
    recommended = _rank_tools(tools, query)
    catalog = _tool_catalog(tools)
    yield _sse("tools", {"query": query, "recommended": recommended, "all": catalog,
                         "apps": _app_catalog(tools), "areas": _area_catalog(tools)})
    if recommended:
        head = ", ".join(r["name"] for r in recommended[:5])
        text = (f"질의와 관련된 도구 {len(recommended)}개를 추천합니다(상위: {head}). "
                f"전체 {len(catalog)}개 중 아래 카드에서 직접 선택·추가하면 이후 대화에서 그 도구를 우선 사용합니다.")
    else:
        text = (f"사용 가능한 도구 {len(catalog)}개입니다. 아래 카드에서 검색해 직접 선택하면 "
                f"이후 대화에서 그 도구를 우선 사용합니다.")
    yield _sse("token", {"delta": text})
    yield _sse("result", {"type": "text", "content": text})
    yield _sse("done", {})



async def _get_tools_retry(scoped: dict, *, tries: int = 3, base_delay: float = 0.5):
    """게이트웨이에서 도구 목록을 받되, **일시적** 실패는 지수 백오프로 재시도한다(간헐 도구
    0개 해소). 게이트웨이 재기동·재연결은 보통 1~2초 내에 끝나므로, 그 창에 걸린 요청이
    도구 없이 응답하던 것을 흡수한다(cae00 '될 때도 안 될 때도' 실증). 401/403 인증 실패는
    재시도해도 같아서 즉시 던진다 — 지연만 늘 뿐이다."""
    import asyncio as _aio
    last = None
    for i in range(max(1, tries)):
        try:
            got = await MultiServerMCPClient(scoped).get_tools()
            # 빈 목록도 실패로 본다 — 게이트웨이가 부팅 직후 tools=0 인 과도 상태를 완결로
            # 받으면 도구 없는 에이전트가 만들어진다. 마지막 시도가 아니면 재시도.
            if not got and i < tries - 1:
                raise RuntimeError("empty tool list")
            return got
        except Exception as e:  # noqa: BLE001
            last = e
            s = repr(e) + " " + str(e)
            if re.search(r"\b(401|403)\b", s):
                raise
            if i < tries - 1:
                d = base_delay * (2 ** i)   # 0.5 → 1.0 → 2.0
                print(f"[agent] get_tools 일시 실패({i + 1}/{tries}) — {d:.1f}s 후 재시도: {type(e).__name__}")
                await _aio.sleep(d)
    raise last


async def _agent_for(app: FastAPI, groups: list[str], pinned: list[str] | None = None,
                     query: str = "", sources: list[str] | None = None, user: str = "",
                     user_pat: str = "", first: list[str] | None = None,
                     core_names: tuple | None = None):
    """ReAct agent whose tools are the gateway's group-filtered set for this caller.
    Cached by group-set; the tools carry the groups header so tool *calls* are scoped too.
    pinned·first·core_names 는 TOOL_MAX 캡 환경에서만 바인딩 구성을 바꾸므로 그때만 캐시 키에
    포함한다(무제한 환경은 바인딩 동일 → 키 분화 없이 시스템 프롬프트 지시로만 우선순위 반영)."""
    pin_key = ((tuple(sorted(pinned or [])), tuple(sorted(first or [])), core_names)
               if ((pinned or first or core_names is not None) and TOOL_MAX > 0) else ())
    # 캡이 걸린 환경에서는 바인딩 도구가 질의에 따라 달라진다 — 질의 토큰을 캐시 키에 넣어
    # 같은 주제는 재사용하고 다른 주제는 새로 구성한다(무제한 환경은 종전대로 그룹 단위 캐시).
    q_key = tuple(sorted(_tok_query(query))[:8]) if TOOL_MAX > 0 else ()
    # 소스 토글은 바인딩 구성을 바꾸므로 캐시 키에 들어가야 한다 — 안 넣으면 앞 대화의
    # 캐시된 에이전트가 재사용돼 토글이 무시된다(조용히 켜진 채로 남는다).
    # ⚠ 호출자 신원은 반드시 키에 있어야 한다. 도구는 생성 시점의 헤더를 물고 있어서,
    # 신원을 빼면 A 가 만든 에이전트를 B 가 재사용하며 B 의 도구 호출이 A 의 자격증명으로
    # 나간다 — 그룹이 같으면 조용히 남의 데이터가 보인다.
    # ⚠ 자격증명도 키에 있어야 한다. 도구는 만들 때의 Authorization 헤더를 물고 있어서,
    # PAT 가 갱신돼도 캐시된 에이전트는 옛 토큰으로 계속 돌다가 만료되는 순간 도구가 조용히
    # 죽는다. 포털이 30분 창에 맞춰 같은 토큰을 주므로 캐시 적중은 그대로다.
    key = (frozenset(groups), pin_key, q_key,
           tuple(sorted(sources)) if sources is not None else None,
           (user or "").strip().lower(),
           hashlib.sha256((user_pat or "").encode()).hexdigest()[:16])
    cache = app.state.agent_cache
    if key not in cache:
        tools = []
        load_failed = False
        degraded = ""   # 사용자 PAT 로 못 붙어 서비스 계정으로 내려앉았다면 그 이유
        connections = app.state.connections
        if connections:
            try:
                # 어느 자격증명으로 도는지는 운영에서 반드시 보여야 한다 — 사용자 PAT 가
                # 안 실려 오면 대화 검색·저장이 조용히 401 이 되고, 로그가 없으면 그 이유를
                # 밖에서 알 방법이 없다.
                print(f"[agent] tool load: user={user or '-'} "
                      f"creds={'user-pat' if user_pat else 'service-account'}")
                scoped = _with_groups(connections, sorted(groups), user, user_pat)
                import asyncio as _aio
                try:
                    _got = await _get_tools_retry(scoped)
                except Exception as _pe:
                    # ⚠ 사용자 PAT 로 붙는 데 실패하면 서비스 계정으로 한 번 되돌아간다.
                    # 챗의 도구 연결은 게이트웨이 하나뿐이라(mcp_servers.json), 이 한 번의
                    # 거절이 대화검색만이 아니라 도구 전량을 0개로 만든다. 포털 키 회전·
                    # 게이트웨이 재기동·PAT 검증 설정 누락 어느 하나로도 그렇게 된다.
                    # 자격증명을 바꾼 대가로 가용성을 잃지 않게, 이전 동작으로 내려앉는다.
                    # (대화 도구는 서비스 계정에선 CONV_UNAVAILABLE 로 명시적으로 실패한다 —
                    #  조용한 오답이 아니라 눈에 보이는 실패라 이 강등은 안전하다.)
                    if not user_pat:
                        raise
                    # 원인을 반드시 남긴다. 예외를 이름도 없이 삼키고 고정 문구만 찍으면,
                    # 재시도가 성공했을 때 401(PAT 무효)인지 타임아웃인지 알 길이 없다 —
                    # '되돌아갈 길 없음' 을 '원인 알 길 없음' 으로 바꾸는 셈이다.
                    degraded = repr(_pe)[:200]
                    print(f"[agent] tool load: 사용자 PAT 실패 — 서비스 계정으로 재시도 ({degraded})")
                    scoped = _with_groups(connections, sorted(groups), user, "")
                    _got = await _get_tools_retry(scoped)
                _raw = [_prep_tool(t) for t in _got]
                # 성공분을 스냅샷에 보관(source 게이트·select 전 순수 목록) — 다음 불통 때
                # 폴백 재료다. 사용자 신원 키라 남에게 재사용되지 않는다.
                if _raw:
                    app.state.tool_snapshot[(frozenset(groups), (user or "").strip().lower())] = (
                        _raw, time.time())
                _raw = gate_sources(_raw, sources)
                # 임베딩 호출은 동기 HTTP — 이벤트 루프를 막지 않게 스레드로 뺀다(동시 챗 보호).
                tools = await _aio.to_thread(_select_tools, _raw, query, pinned, first, core_names)
            except Exception as exc:  # gateway down → degrade to a no-tool agent, don't crash
                load_failed = True
                # 상태코드를 뽑아 둔다 — prod 에서 '툴콜이 되었다 안 되었다' 할 때 401(토큰)
                # 인지 타임아웃인지 구분이 안 되면 원인 추적이 불가능하다.
                # MCP 클라이언트는 실패를 ExceptionGroup 으로 감싸 던진다 — 그대로 두면
                # "unhandled errors in a TaskGroup" 만 남아 401 인지 타임아웃인지 알 수 없다.
                # 하위 예외/원인 사슬을 훑어 HTTP 상태나 실제 메시지를 뽑는다.
                def _root_cause(e, depth=0):
                    if depth > 6:
                        return e
                    subs = getattr(e, "exceptions", None)  # ExceptionGroup
                    if subs:
                        return _root_cause(subs[0], depth + 1)
                    nxt = e.__cause__ or e.__context__
                    return _root_cause(nxt, depth + 1) if nxt is not None else e
                _root = _root_cause(exc)
                _st = getattr(getattr(_root, "response", None), "status_code", None)
                if _st is None:
                    _m = re.search(r"\b(4\d\d|5\d\d)\b", str(_root))
                    _st = int(_m.group(1)) if _m else None
                if _st == 401:
                    detail = "HTTP 401 — 게이트웨이 인증 실패(GW_TOKEN 불일치/만료)"
                elif _st:
                    detail = f"HTTP {_st}"
                else:
                    detail = f"{type(_root).__name__}: {str(_root)[:120]}"
                app.state.tool_load_error[frozenset(groups)] = detail
                print(f"[agent] tool load FAILED groups={sorted(groups)} {detail} ({exc!r})")
                # ── 스냅샷 폴백 — 게이트웨이가 죽어도 직전 성공 도구로 응답한다(강건성 최상단).
                #    401(인증) 실패면 스냅샷 도구도 같은 토큰을 물어 무의미하니 폴백하지 않는다.
                snap = app.state.tool_snapshot.get((frozenset(groups), (user or "").strip().lower()))
                if snap and _st != 401:
                    _snap_raw, _snap_ts = snap
                    _age = int(time.time() - _snap_ts)
                    _sr = gate_sources(list(_snap_raw), sources)
                    tools = await _aio.to_thread(_select_tools, _sr, query, pinned, first, core_names)
                    load_failed = False   # 도구가 살아 있으니 실패 아님 — 단 신선하지 않다
                    degraded = f"stale-snapshot({_age}s): {detail}"
                    app.state.tool_load_error.pop(frozenset(groups), None)
                    print(f"[agent] 스냅샷 폴백 — 도구 {len(tools)}개(나이 {_age}s) 로 응답. 게이트웨이 복구 시 자동 갱신")
        if not load_failed:
            # 성공했으면 이 그룹셋의 과거 오류를 지운다(다른 그룹셋 상태는 건드리지 않는다).
            app.state.tool_load_error.pop(frozenset(groups), None)
        agent = create_react_agent(app.state.llm, tools)
        if load_failed:
            # 실패 결과는 캐시하지 않는다 — 캐시하면 게이트웨이가 복구돼도 이 그룹은
            # 재시작 전까지 영구 no-tool 이 된다(조용한 최악의 실패 모드). 이번 요청만
            # 도구 없이 응답하고, 다음 요청에서 재시도한다.
            return agent
        # ⚠ 상한 없는 dict 였다. 키에 그룹/사용자만 있을 땐 항목 수가 사람 수로 수렴했지만,
        # 자격증명 해시가 키에 들어오면서 사용자마다 30분에 하나씩 새 항목이 생기고 옛 항목은
        # 영영 남는다(도구 수백 개를 품은 객체라 가볍지도 않다). 오래된 것부터 버린다.
        # ⚠ 강등된 에이전트를 사용자 PAT 키에 넣으면 안 된다. 넣으면 일시적 거절 한 번이
        # PAT 창(30분) 내내 조용한 강등으로 고착되고, 게이트웨이가 곧 복구돼도 그 사용자는
        # 계속 서비스 계정으로 돈다 — 위 불변식("자격증명도 키에 있어야 한다")과 어긋난다.
        # 서비스 계정 키에 넣어 두면 재사용은 되면서 다음 요청이 사용자 PAT 를 다시 시도한다.
        store_key = key
        _who = (frozenset(groups), (user or "").strip().lower())
        if degraded:
            store_key = key[:-1] + (hashlib.sha256(b"").hexdigest()[:16],)
            # ⚠ tool_load_error 에 쓰면 안 된다. 그 칸의 뜻은 "도구가 0개" 하나뿐이고,
            # 소비자가 그렇게 읽어 모델에게 "도구를 호출하지 마라"를 주입한다 — 폴백이
            # 살려 낸 도구 전량을 폴백 자신이 봉인하는 셈이 된다(실제로 그랬다).
            # 강등은 "도구는 있는데 서비스 계정 시야"라는 다른 사실이므로 칸을 나눈다.
            # 키에 사용자를 넣는 이유 — 강등은 그 사람의 자격증명 사건이지 그룹 사건이 아니다.
            app.state.tool_degraded[_who] = degraded
        else:
            app.state.tool_degraded.pop(_who, None)
        # 상한이 0 이하면 축출 루프가 빈 dict 에서 next() 를 불러 StopIteration 이 된다.
        while AGENT_CACHE_MAX > 0 and len(cache) >= AGENT_CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[store_key] = agent
        return agent
    else:
        # 재사용된 항목은 최근 것으로 옮긴다 — 그래야 위 축출이 '가장 안 쓴 것'을 버린다.
        cache[key] = cache.pop(key)
    return cache[key]


def _hist_budget_chars(sample: str = "", has_docs: bool = False) -> int:
    """이력에 줄 글자 예산.

    env HIST_BUDGET 은 **낮추기만** 한다. 올리는 쪽으로 두면 낡은 .env 한 줄이 컨텍스트를
    조용히 넘겨 400 을 만든다(실측: dev .env 의 HIST_BUDGET=16000 이 16,384 창을 넘겼다).
    탐지가 틀려 창을 크게 잡아야 하면 그건 LLM_CONTEXT_TOKENS 로 고칠 일이다.
    """
    share = HIST_CTX_SHARE if has_docs else HIST_CTX_SHARE_SOLO
    tokens = int((_model_context_tokens() - DOC_RESERVE_TOKENS) * share)
    derived = max(1500, _chars_for_tokens(sample or "가나다라", max(1200, tokens)))
    forced = os.environ.get("HIST_BUDGET")
    return min(derived, max(1500, int(forced))) if forced else derived


def _compact_turns(dropped: list, budget: int) -> str:
    """예산 밖으로 밀려난 오래된 턴을 **버리지 않고** 한 덩어리로 압축한다.

    종전에는 그냥 버렸다. 그러면 모델은 그런 대화가 있었다는 것조차 모르고, 사용자는
    "아까 말했잖아" 가 왜 안 통하는지 모른다 — 화면에도 아무 흔적이 없다.

    LLM 요약을 쓰지 않는 이유는 지연과 비용이다. 이력은 **매 턴** 다시 들어오므로 요약도
    매 턴 다시 해야 한다. 대신 발췌로 압축한다 — 첫 턴(대개 과제를 규정한다)에 몫을 더
    주고 나머지는 머리를 남긴다. 무엇이 압축됐는지 모델이 알면 사용자에게 되물을 수 있다.
    """
    if not dropped or budget < 200:
        return ""
    lines, n = [], len(dropped)
    # 몫을 균등하게 주지 않는다. **사용자 턴이 요구·결정을 담고** 어시스턴트 턴은 그것을 풀어
    # 쓴 것이라, 같은 글자를 쓸 거면 사용자 쪽을 남기는 편이 복원력이 높다. 첫 턴은 대개
    # 과제 자체를 규정하므로 한 몫 더 준다.
    weights = [(3 if i == 0 else 0) + (2 if role == "user" else 1)
               for i, (role, _) in enumerate(dropped)]
    unit = budget / max(1, sum(weights))
    for i, (role, content) in enumerate(dropped):
        cap = max(60, int(unit * weights[i]))
        who = "사용자" if role == "user" else "어시스턴트"
        body = " ".join(content.split())
        if len(body) > cap:
            body = body[:cap] + "…"
        lines.append(f"- {who}: {body}")
    return (f"[이전 대화 {n}턴 압축 — 길이 때문에 원문 대신 발췌만 있다. 여기 근거가 모자라면 "
            "지어내지 말고 사용자에게 그 대목을 다시 말해 달라고 하라]\n" + "\n".join(lines))


def _history_messages(history: list[dict], stats: dict | None = None,
                      has_docs: bool = False) -> list[tuple[str, str]]:
    """멀티턴 history 를 검증·절단해 LangChain 메시지 tuple 로 만든다.

    방어: role 이 user/assistant 외면 무시, 항목별 HIST_ITEM_MAX 절단, 항목 수 최대
    HIST_MAX_ITEMS. 전체는 최신 것 우선으로 예산 안에 담고, **밀려난 오래된 턴은 버리지 않고
    압축본 한 덩어리로** 맨 앞에 넣는다(_compact_turns).

    stats 를 주면 {"kept", "compacted", "budget"} 을 채워 준다 — 호출부가 사용자에게 알린다.
    """
    items: list[tuple[str, str]] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        role, content = entry.get("role"), entry.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content:
            continue
        if len(content) > HIST_ITEM_MAX:
            content = content[:HIST_ITEM_MAX] + "…"
        items.append((role, content))
    items = items[-HIST_MAX_ITEMS:]

    budget = _hist_budget_chars("".join(c for _, c in items[-6:]), has_docs)
    verbatim_budget = int(budget * (1.0 - HIST_COMPACT_SHARE))
    kept: list[tuple[str, str]] = []
    used = 0
    cut = len(items)                        # 이 앞의 것들이 압축 대상
    for idx in range(len(items) - 1, -1, -1):   # 최신부터 예산을 채운다
        role, content = items[idx]
        if used + len(content) > verbatim_budget and kept:
            break
        kept.append((role, content))
        used += len(content)
        cut = idx
    kept.reverse()

    digest = _compact_turns(items[:cut], int(budget * HIST_COMPACT_SHARE))
    if digest:
        kept.insert(0, ("user", digest))
    if stats is not None:
        stats.update(kept=len(items) - cut, compacted=cut, budget=budget)
    return kept


# 핸드오프(챗→심의)용 도구 결과 길이. 화면 미리보기(220)와 **일부러 분리한다** —
# 활동 패널은 짧아야 읽히고, 심의는 날것이 많이 필요하다(220자×12건 ≈ 2.6KB 만 도착해
# 예산이 비어 있었다). 표시용을 늘리면 패널도 브라우저 localStorage 도 같이 무거워지므로
# 필드를 나눈다. 심의 예산이 60KB 로 올라갔으므로(deliberation._EVID_BUDGET) 여기도 같이
# 올린다 — 4000자×40건이면 예산을 채운다. 안 올리면 예산만 크고 실제로 실리는 건 그대로다.
HANDOFF_RESULT_CHARS = int(os.environ.get("HANDOFF_RESULT_CHARS", "4000"))

# 붙인 문서(추출문) 주입 예산. 원본 파일은 오지 않는다 — DRM 문서는 그 PC 에서만 복호화되므로
# 추출은 사용자 PC 의 Office COM 에서 끝난다(HWAXPortal docs/doc-deliberate).
# ⚠ 진짜 천장은 이 값이 아니라 **모델 컨텍스트**다. 넉넉히 잡되, 넘치면 잘리는 게 아니라
# 낱장 경계에서 가운데를 덜어낸다(_fit_doc) — 뒤를 버리면 발표자료는 결론이 날아간다.
# 진짜 천장은 고정 숫자가 아니라 **모델 컨텍스트**다. 고정값을 크게 잡으면 긴 발표자료에서
# 400(context length exceeded)이 나고, 사용자에게는 '응답 생성 실패' 로만 보인다(실측:
# 326,875자 180슬라이드 → dev 16,384 토큰 모델에서 그대로 터졌다). 그래서 서버에 물어본다.
DOC_MAX_FILES = int(os.environ.get("DOC_MAX_FILES", "10"))           # 건수 상한
DOC_HEAD_RATIO = float(os.environ.get("DOC_HEAD_RATIO", "0.6"))      # 넘칠 때 앞쪽에 줄 몫
# 컨텍스트에서 문서 몫을 **비율로** 떼면 큰 창을 버린다. 고정 오버헤드(시스템 프롬프트·도구
# 스키마·출력)는 창 크기에 비례하지 않기 때문이다 — 1M 창에서 45%만 쓰면 50만 토큰을 놀린다.
# 그래서 '컨텍스트 − 예비분 − 이력' 으로 잡는다. 운영 타깃은 GLM(128K)·Claude Opus(200K~1M)다.
DOC_RESERVE_TOKENS = int(os.environ.get("DOC_RESERVE_TOKENS", "12000"))  # 시스템+도구+출력
DOC_SAFETY = float(os.environ.get("DOC_SAFETY", "0.97"))                 # 변환 오차 안전 계수
_CTX_FALLBACK = int(os.environ.get("LLM_CONTEXT_TOKENS", "128000"))  # 물어보기 실패 시
_ctx_cache: dict = {}

# 토큰 환산 — 한글은 토큰당 1자 남짓, 라틴 문자는 3~4자다. 한 값으로 뭉뚱그리면 한국어 문서에서
# 400 이 나거나(과대평가) 영문 문서에서 창의 3분의 1만 쓴다(과소평가). 글자 구성으로 가른다.
_CJK_RANGES = ((0xAC00, 0xD7AF), (0x4E00, 0x9FFF), (0x3040, 0x30FF), (0x3130, 0x318F))


def _tokens_per_char(text: str) -> float:
    """이 글의 글자당 토큰 밀도. 앞부분만 표본으로 본다(2백만 자를 다 세면 느리다)."""
    sample = text[:20000] or "가"
    cjk = sum(1 for ch in sample
              if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES))
    return (cjk / 1.05 + (len(sample) - cjk) / 3.6) / len(sample)


def _est_tokens(text: str) -> int:
    """대략의 토큰 수."""
    return max(1, int(len(text) * _tokens_per_char(text))) if text else 0


def _chars_for_tokens(text: str, tokens: int) -> int:
    """이 글의 구성 그대로 `tokens` 토큰에 해당하는 글자 수.

    ⚠ 토큰 수를 정수로 거쳐 나눗셈하면 안 된다. 표본이 짧을 때 절사 오차가 그대로 증폭된다 —
    "가나다라"(4자·3.81토큰→3)로 재면 예산이 **27% 부풀어** 1M 창에서 1,066,412토큰짜리
    이력 예산이 나왔다(실측). 밀도(실수)로 바로 나눈다.
    """
    return max(500, int(tokens / max(1e-6, _tokens_per_char(text))))


def _model_context_tokens() -> int:
    """이 모델이 받는 최대 토큰. OpenAI 호환 /v1/models 의 max_model_len 을 한 번만 읽는다.

    박스마다 모델이 다르다(dev 16K · 운영 GLM). env 로 박아 두면 한쪽에서 반드시 틀리고,
    틀린 결과는 400 이라 사용자에게 '응답 생성 실패' 로만 보인다."""
    if "n" in _ctx_cache:
        return _ctx_cache["n"]
    n = _CTX_FALLBACK
    try:
        import httpx

        r = httpx.get(f"{VLLM_BASE_URL.rstrip('/')}/models",
                      headers={"Authorization": f"Bearer {VLLM_API_KEY}"}, timeout=5.0)
        data = [m for m in ((r.json() or {}).get("data") or []) if isinstance(m, dict)]
        # 이름이 맞는 것 우선, 서빙 모델이 하나뿐이면 그것. 여러 개인데 이름이 안 맞으면
        # 아무거나 집지 않는다 — 틀린 컨텍스트로 예산을 잡느니 보수적 기본값이 낫다.
        pick = next((m for m in data if m.get("id") == VLLM_MODEL), data[0] if len(data) == 1 else None)
        v = (pick or {}).get("max_model_len") or (pick or {}).get("context_length")
        if isinstance(v, int) and v > 0:
            n = v
    except Exception as e:      # 못 물어보면 기본값으로 간다 — 기능을 막지는 않는다
        print(f"[agent] 모델 컨텍스트 조회 실패({e}) — {n:,} 토큰으로 가정한다")
    _ctx_cache["n"] = n
    print(f"[agent] 문서 예산 기준 컨텍스트 = {n:,} 토큰")
    return n


def _doc_budget_tokens(history_tokens: int = 0) -> int:
    """붙인 문서 전부에 줄 **토큰** 예산 — 컨텍스트에서 예비분과 이력을 뺀 나머지.

    이력도 같은 창을 다툰다(_hist_budget_chars 로 컨텍스트에서 유도한다). 고정값으로 빼면
    긴 대화 중에 문서를 붙이는 순간 400 이 난다 — 그래서 그 턴의 실제 이력을 뺀다.
    """
    ctx = _model_context_tokens()
    # 바닥을 '컨텍스트의 N%' 로 두면 안 된다 — 이력이 창을 이미 먹었을 때 **없는 자리를
    # 있다고** 계산해 그대로 400 이 난다. 남은 만큼만 주고, 최소치는 상징적으로만 둔다.
    # 안전 계수는 토큰↔글자 왕복 변환의 오차 몫이다(실측 128,000 창에서 142토큰 초과).
    # 넘치면 400 이고 400 은 '응답 생성 실패' 로만 보이므로, 조금 손해 보는 쪽으로 둔다.
    return max(1500, int((ctx - DOC_RESERVE_TOKENS - max(0, history_tokens)) * DOC_SAFETY))


def _doc_total_chars(documents=None, history_tokens: int = 0) -> int:
    """문서 예산을 글자 수로. env DOC_TOTAL_CHARS 로 못박으면 그 값이 이긴다.

    글자↔토큰 환산은 **그 문서의 글자 구성**으로 한다 — 한국어 발표자료와 영문 논문은
    같은 글자 수라도 토큰이 3배 넘게 차이 난다.
    """
    tokens = _doc_budget_tokens(history_tokens)
    joined = "".join(str((d or {}).get("text") or "")[:20000] for d in (documents or [])) or "가나다"
    derived = max(1500, _chars_for_tokens(joined, tokens))
    forced = os.environ.get("DOC_TOTAL_CHARS")   # 낮추기만 한다(위 HIST_BUDGET 과 같은 이유)
    return min(derived, max(1500, int(forced))) if forced else derived



def _doc_block(documents, history_tokens: int = 0, budget_tokens: int = 0) -> str:
    """붙인 문서를 시스템 프롬프트에 실을 블록으로.

    세 갈래(첫 호출·재시도·강제 도구호출)가 모두 sys_prompt 를 공유하므로 여기 실으면
    한 군데만 고쳐도 전부 덮인다 — user 메시지에 붙이면 재시도 경로에서 조용히 빠진다.

    예산은 **건수로 균등 분배**한다. 앞 문서가 다 먹고 뒤 문서가 통째로 사라지면, 사용자는
    두 건을 붙였는데 답이 한 건만 본 채로 나온다. 자른 건 잘랐다고 문서마다 적는다."""
    if not documents:
        return ""
    docs = [d for d in documents if isinstance(d, dict) and str(d.get("text") or "").strip()]
    if not docs:
        return ""
    docs = docs[:DOC_MAX_FILES]
    total = (_chars_for_tokens("".join(str((d or {}).get("text") or "")[:20000] for d in docs)
                               or "가나다", budget_tokens)
             if budget_tokens > 0 else _doc_total_chars(docs, max(0, history_tokens)))
    share = max(2000, total // len(docs))
    parts = []
    for i, d in enumerate(docs, start=1):
        name = str(d.get("name") or f"문서{i}")[:260]
        text = str(d.get("text") or "")
        body, note = fit_document(text, share, DOC_HEAD_RATIO)
        head = f"[문서 {i}/{len(docs)} · {name} · {len(text):,}자"
        head += f" — {note}]" if note else "]"
        parts.append(head + "\n" + body)
    return ("\n\n[사용자가 붙인 문서 — 사용자 PC 의 Office 로 추출한 원문이다. 아래 규율을 지켜라]\n"
            "· 이 문서에 대해 답할 때는 **여기 적힌 것만** 근거로 삼아라. 기억으로 채우지 마라.\n"
            "· 수치·날짜·이름을 인용할 때 어디서 왔는지 함께 적어라(예: [s.7], [p.12], 문서 이름).\n"
            "· '앞 N자만 실림' 이라고 적힌 문서는 뒷부분을 못 봤다. 뒷부분이 필요하면 "
            "그렇다고 말하고 사용자에게 해당 구간을 따로 붙여 달라고 하라 — 지어내지 마라.\n"
            "· 문서 내용과 네 사전 지식이 어긋나면 어긋난다고 말하라. 조용히 덮어쓰지 마라.\n\n"
            + "\n\n".join(parts))


def _tool_preview(v, n: int = 220) -> str:
    """활동 패널 드릴다운용 도구 입출력 요약 — 안전 문자열화 + 공백 압축 + 절단."""
    try:
        if v is None:
            return ""
        content = getattr(v, "content", None)   # ToolMessage 등 랩퍼 언랩
        if content is not None:
            v = content
        s = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else str(v)
        s = re.sub(r"\s+", " ", s).strip()
        return s[:n]
    except Exception:  # noqa: BLE001 — 미리보기 실패가 스트림을 죽이면 안 됨
        return ""


# 페르소나 역할 원문 상한(문자). HE팀 정본 동기화(HWAXPortal infra/scripts/sync-he-personas.py
# PROMPT_MAX)가 같은 값으로 막는다 — 여기서 잘리면 역할 뒤쪽('답하는 법')이 조용히 빠진다.
PERSONA_ROLE_MAX = 8000
# 페르소나 캐시 수명(초). 예전엔 재시작 전까지 영구였다 — 정본을 고쳐 동기화해도 에이전트서버를
# 다시 띄우기 전까지 옛 역할·옛 앱으로 답했다.
PERSONA_TTL_S = _env_int("PERSONA_TTL_S", 300)


# 한 대화에 세울 수 있는 전문가 수. 상한을 5로 두되 값이 나는 조합은 '주 전문가 1 + 보조'이고,
# 같은 층 전문가를 여럿 겹치면 답이 평균으로 무너진다(docs/multi-persona/PLAN.md).
PERSONA_MAX = _env_int("PERSONA_MAX", 5)
# 보조 전문가의 역할 문구 길이. 주 전문가(PERSONA_ROLE_MAX)보다 짧게 싣는다 — 보조는 판단 기준만
# 빌려주는 자리라 전문을 실으면 도구 스키마 예산을 그만큼 밀어낸다.
PERSONA_HELPER_ROLE_MAX = _env_int("PERSONA_HELPER_ROLE_MAX", 3000)


def _persona_keys(many: list[str] | None, one: str | None) -> list[str]:
    """지정 전문가 목록 정규화 — 중복·공백을 걷고 상한을 씌운다. 첫 명이 주 전문가다.
    구 계약(pinned_agent 하나)도 같은 함수로 받는다(목록이 비면 그 한 명을 쓴다)."""
    src = list(many or []) or ([one] if one else [])
    out: list[str] = []
    for k in src:
        if not isinstance(k, str):
            continue
        k = k.strip()[:120]
        if k and k not in out:
            out.append(k)
    return out[:PERSONA_MAX]


async def _persona_meta(app: FastAPI, groups: list[str], agent_type: str) -> dict:
    """선택 전문가의 역할 원문과 운영 설정(get_agent_session) — {role, operator, apps, key_tools}.

    operator 는 HE팀 MCP 운영자(response_config.persona_kind == 'mcp_operator')다. 지식카드가 아니라
    자기 앱 도구로 답하므로, 호출부가 도구 선별 **전에** 이 값을 읽어 그 앱 도구를 묶는다."""
    cache = getattr(app.state, "persona_cache", None)
    if cache is None:
        cache = app.state.persona_cache = {}
    hit = cache.get(agent_type)
    if hit and time.time() - hit[0] < PERSONA_TTL_S:
        return hit[1]
    meta: dict = {"role": "", "operator": False, "apps": [], "key_tools": []}
    try:
        tools = await _tools_by_name(app, groups, CATALOG_RESULT_MAX)  # 역할 원문을 JSON 으로 읽는다
        sess = _first_dict(_parse_json(await _call(tools, "get_agent_session", {"agent_type": agent_type})))
        sd = _first_dict(sess.get("data", sess))
        meta["role"] = str(sd.get("system_prompt") or sd.get("description") or "")[:PERSONA_ROLE_MAX]
        rc = sd.get("response_config") if isinstance(sd.get("response_config"), dict) else {}
        if rc.get("persona_kind") == "mcp_operator":
            meta["operator"] = True
            meta["apps"] = [a.strip()[:80] for a in (rc.get("mcp_apps") or []) if isinstance(a, str) and a.strip()][:3]
            meta["key_tools"] = [n.strip()[:80] for n in (rc.get("key_tools") or []) if isinstance(n, str) and n.strip()][:12]
    except Exception as exc:  # noqa: BLE001 — 실패 시 페르소나 없이 일반 챗
        print(f"[agent] persona load failed for {agent_type}: {exc!r}")
    if meta["role"]:
        cache[agent_type] = (time.time(), meta)
    return meta


# 지정 전문가의 지식카드 주입 예산(문자). 심의 경로(DELIB_KNOWLEDGE_BUDGET)와 같은 기본값이다 —
# 챗은 인원 1명·라운드 1회라 심의처럼 곱해지지 않으므로 더 조일 이유가 없다.
CHAT_KNOWLEDGE_BUDGET = _env_int("CHAT_KNOWLEDGE_BUDGET", 12000)

# 지식 조회용 도구 핸들 캐시(그룹셋별, 300초) — _tools_by_name 은 매번 백엔드 5곳의 도구 목록을
# 다시 받아 온다. 페르소나 대화는 매 발화마다 이 경로를 타므로 캐시가 없으면 발화마다 그 왕복이
# 통째로 붙는다. agent_cache 가 이미 도구를 품은 에이전트를 무기한 캐시하므로 300초는 그보다 짧다.
_KTOOLS_CACHE: dict = {}


async def _knowledge_tools(app: FastAPI, groups: list[str]) -> dict:
    import time
    key = frozenset(groups)
    hit = _KTOOLS_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    tools = await _tools_by_name(app, groups, CATALOG_RESULT_MAX)  # 히트를 코드가 파싱 — 절단 금지
    if tools:
        _KTOOLS_CACHE[key] = (time.time(), tools)
    return tools


def _knowledge_line(h) -> str:
    """agent_search hit 한 건 → 한 줄. 실측 형태(2026-08-05)는
    {record_id, section_id, title, section_title, snippet, score, tags, …} 로 본문은 snippet 에 있다."""
    if not isinstance(h, dict):
        return str(h)[:300]
    title = h.get("title") or ""
    sec = h.get("section_title") or ""
    body = h.get("snippet") or h.get("text") or h.get("excerpt") or h.get("summary") or ""
    head = title + (f" › {sec}" if sec else "")
    if not (head or body):
        return json.dumps(h, ensure_ascii=False, default=str)[:300]
    return f"• [{head}] {str(body).strip()}"[:700]


async def _persona_knowledge(app: FastAPI, groups: list[str], agent_type: str, query: str) -> str:
    """지정 전문가의 주제 연관 지식카드를 코드가 미리 조회해 발췌한다(결정적 RAG).

    모델에게 'agent_search 를 써라'고 지시만 하면 안 부르면 그만이고, 사용자에게는 그 전문가가
    아는 것이 없는 것처럼 보인다. 심의 경로가 이미 같은 이유로 결정적 조회를 하고 있어
    (deliberation.DELIB_PERSONA_KNOWLEDGE) 그 형태를 따른다.

    캐시하지 않는다 — 질의가 발화마다 달라 적중률이 낮고, 낡은 발췌를 주면 방금 물은 것과
    무관한 지식이 붙는다. 실패는 비치명이다(지식 없이 페르소나만으로 답한다)."""
    try:
        tools = await _knowledge_tools(app, groups)
        if "agent_search" not in tools:
            return ""
        # 타임아웃·강등 판정은 공용 헬퍼에 맡긴다 — _call 은 예외를 문자열로 삼켜서
        # '지식이 없다' 와 '못 물어봤다' 가 같은 빈 결과로 보인다(그 둘은 다른 사실이다).
        hits, note = await _agent_search_hits(tools, agent_type, query)
        if note:
            print(f"[agent] 지식카드 강등({agent_type}): {note}")
            _persona_knowledge.last_note = note   # 호출부가 사용자에게 사유를 보이게
        else:
            _persona_knowledge.last_note = ""
        if not hits:
            return ""
        lines, total, seen = [], 0, set()
        for h in hits:
            ln = _knowledge_line(h)
            if ln in seen:  # 같은 레코드의 유사 섹션 반복 방지
                continue
            if total + len(ln) > CHAT_KNOWLEDGE_BUDGET:
                break
            seen.add(ln)
            lines.append(ln)
            total += len(ln)
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001 — 지식 검색 실패는 비치명(페르소나만으로 계속)
        print(f"[agent] 지식카드 검색 실패({agent_type}): {exc!r}")
        _persona_knowledge.last_note = f"조회 실패({type(exc).__name__})"
        return ""


# 직전 호출의 강등 사유. 챗 스트림이 읽어 사용자에게 보인다 — 무음 강등을 만들지 않는다.
_persona_knowledge.last_note = ""


# 모델마다 호출 JSON 모양이 다르다. qwen/hermes 는 {"name":…,"arguments":…}, Anthropic 계열은
# {"type":"tool_use","name":…,"input":…} 를 쓴다(실측: Haiku 가 input 형식으로 출력).
# 한 형식만 알면 다른 모델에서 안전망이 통째로 무력해지므로 셋 다 인정한다.
_LEAK_RE = re.compile(
    r'</?tool_call>|<\|tool_call\|>|"type"\s*:\s*"tool_use"|'
    r'"name"\s*:\s*"[A-Za-z_][A-Za-z0-9_]*"[^{}]{0,80}?"(?:arguments|input|parameters)"\s*:')


def _needs_final_rescue(turn_calls: list, text: str, post_tool_chars: int) -> bool:
    """도구를 부른 턴인데 마무리 답이 없으면 True — 구제(도구 결과로 답 만들기) 대상이다.

    두 모양이 있다. ① 글자가 아예 없는 턴. ② **예고만 하고 끊긴 턴** — "먼저 가이드와
    템플릿을 확인하겠습니다" 하고 도구를 부른 뒤, 결과를 받고도 한 글자도 더 내지 않는다.
    ②는 text 가 비어 있지 않아 '빈 응답' 조건을 그대로 통과했고, 사용자에겐 중간에 끊긴
    응답으로 보였다(실측 신고: 보고서 작성이 늘 그 문장에서 멈췄다).
    post_tool_chars = 마지막 도구 결과 뒤에 모델이 낸 글자 수."""
    if not turn_calls:
        return False
    return (not text.strip()) or post_tool_chars <= 0


def _looks_like_leaked_tool_call(text: str, no_tool_ran: bool = False) -> bool:
    """모델이 도구 호출을 실행하지 못하고 호출문을 본문에 그대로 출력했는지 판정.

    기본은 엄격하게 — 코드블록 안(```json …)은 설명용 예시일 수 있어 제외한다.
    다만 **이번 턴에 도구가 한 번도 실행되지 않았다면** 얘기가 다르다. 작은 모델은
    "확인하겠습니다" 하고 인자를 코드펜스에 찍은 뒤 실제 호출은 하지 않는 실패를 자주 낸다
    (실측: dev 7B 가 ```{"product_code": "GS25U"}``` 를 출력하고 종료). 그 경우엔 펜스도
    후보로 본다 — 어차피 도구가 안 돌았으므로 '설명용 예시'일 가능성이 낮다."""
    if not text:
        return False
    stripped = text if no_tool_ran else re.sub(r"```.*?```", "", text, flags=re.S)
    return bool(_LEAK_RE.search(stripped))


def _extract_leaked_calls(text: str, no_tool_ran: bool = False) -> list:
    """본문에 텍스트로 새어 나온 도구 호출을 구조로 복원한다.

    작은 모델(하이쿠급)이나 파서가 안 맞는 서빙에서는 구조화 tool_call 을 못 내고
    <tool_call>{"name":…,"arguments":…}</tool_call> 를 그냥 출력해 버린다. 재시도에만
    기대면 같은 모델이 또 같은 실수를 하므로, 여기서 뽑아 **직접 실행**한다.
    ```json 예시 블록은 설명 목적일 수 있어 제외한다.
    """
    if not text:
        return []
    body = text if no_tool_ran else re.sub(r"```.*?```", "", text, flags=re.S)
    out, seen = [], set()
    # 이름과 인자 블록을 각각 찾는다 — 사이에 "type":"tool_use" 같은 다른 키가 끼어도,
    # 인자 키가 arguments/input/parameters 중 무엇이어도 복원된다.
    for m in re.finditer(
        r'"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"'
        r'(?:\s*,\s*"[^"]+"\s*:\s*(?:"[^"]*"|null|true|false|-?[0-9.]+))*'
        r'\s*,\s*"(?:arguments|input|parameters)"\s*:\s*(\{(?:[^{}]|\{[^{}]*\})*\})',
            body, re.S):
        name = m.group(1)
        try:
            args = json.loads(m.group(2))
        except Exception:  # noqa: BLE001 — 인자 파싱 실패면 빈 인자로 시도
            args = {}
        if not isinstance(args, dict):
            args = {}
        key = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "arguments": args})
        if len(out) >= 3:  # 한 턴에 과도 실행 방지
            break
    return out


_ANNOUNCE_RE = re.compile(
    r"(확인하겠습니다|조회하겠습니다|호출하겠습니다|가져오겠습니다|검색하겠습니다|알아보겠습니다|"
    r"조회해\s?보겠습니다|바로\s?(조회|호출|확인)|let me (check|call|search|look|get|fetch)|"
    r"i(?:'| wi)ll (call|check|search|use|fetch)|i(?:'m| am) going to (call|check|use)|"
    # 중국어 — 모델이 언어를 흘리면(실측: dev 7B 가 중국어로 답하며 "我们将调用 analyze_laminate")
    # 한국어 패턴이 통째로 빗나가 예고-미호출을 못 잡는다. 운영 타깃 GLM 도 같은 계열이다.
    r"(?:我们)?将(?:使用|调用|查询|获取)|让我(?:们)?(?:来)?(?:调用|使用|查询|检查)|接下来[，,]?\s*(?:我们)?(?:将|会)(?:调用|使用)|"
    # 산문 속 파이썬식 호출문 + 호출 의사 — "list_materials(query=\"Al6061-T6\")를 호출하여 …"
    # 로 끝나고 실제 호출은 없던 실측 턴을 잡는다. 도구명(…_…)+괄호+인자 형태에 '호출/사용/
    # 실행' 이 붙은 경우만 본다 — 도구 이름만 언급하는 설명문은 걸리지 않는다.
    r"[a-z][a-z0-9]*_[a-z0-9_]*\([^)]{0,120}\)\s*(를|을|으로|로)?\s*(호출|사용|실행))",
    re.IGNORECASE)  # "Let me check…" 처럼 문장 첫 글자가 대문자인 경우가 대부분이다


def _announced_without_calling(text: str) -> bool:
    """도구를 쓰겠다고 '말만' 하고 실제로는 한 번도 호출하지 않은 턴인지.

    작은 모델의 대표적 실패다 — "확인하겠습니다" 뒤에 인자만 코드펜스로 찍고 끝낸다
    (실측: dev 7B 가 ```{"product_code": "GS25U"}``` 를 출력하고 종료). 이때는 도구 이름이
    없어 호출문 복원도 불가능하므로, 예고 자체를 신호로 삼아 한 번 더 강제한다."""
    return bool(text) and bool(_ANNOUNCE_RE.search(text))


# 도구를 부르지 않고 결과를 지어낸 흔적 — 표면이 완벽한 답변이라 사용자가 진짜 데이터로 믿는다.
# 누출·예고와 달리 실패의 흔적이 없어 가장 위험하다(Haiku 실측: 도구 0회인데 "건수: 342,
# 최근 언급: 2026-07-25" 같은 구체값을 확정 서술). 오탐이 더 나쁘므로 **고정밀 표지만** 본다.
_FABRICATED_RE = re.compile(
    r"^\s*(?:Tool|도구)\s*[:：]\s*[A-Za-z_][A-Za-z0-9_]*\s*\(\)|"      # "Tool: get_x()"
    r"^\s*(?:Status|상태)\s*[:：]\s*(?:Success|성공|OK)\b|"              # "Status: Success"
    r"^\s*\[?(?:도구|tool)\s*(?:결과|output|result)\]?\s*[:：]",         # "[도구 결과]:"
    re.IGNORECASE | re.MULTILINE)


def _looks_fabricated_tool_result(text: str) -> bool:
    """도구를 실행하지 않았는데 '도구 결과'처럼 서술한 응답인지(고정밀 표지 기반)."""
    return bool(text) and bool(_FABRICATED_RE.search(text))


# 도구 0회인데 표·차트를 직접 그린 턴 — _FABRICATED_RE 는 "Tool: x()" 같은 자백형 표지만 보므로
# 이 유형(실측: 물성표 + Plotly CDN HTML, 자백 표지 없음)을 통째로 놓쳤다. 사용자 화면에는
# 오류도 경고도 없이 그럴듯한 표와 차트만 남았다.
_CHART_SURFACE_RE = re.compile(
    r"```html|<script\s+src=|^\s*\|\s*-{3,}", re.M)
# 특정 대상을 지목한 질문인지 — 영문+숫자 2자리 이상이 붙은 토큰(Al6061-T6, SUS304, SCM440).
# 이 조건이 '사용자가 준 숫자로 그리는 정당한 자작'(예: "우리 팀 인원 비율을 도표로")을 살린다.
_ENTITY_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*\d{2,}[A-Za-z0-9_\-]*\b")


def _drew_own_chart(message: str, text: str) -> bool:
    """DB 에 있을 대상을 지목해 물었는데, 조회 없이 표/차트 표면을 직접 만들어낸 응답인지."""
    return bool(text) and bool(_CHART_SURFACE_RE.search(text)) and bool(_ENTITY_RE.search(message or ""))


def _mentioned_tools(text: str, names) -> list:
    """발언 속에서 실제 도구 이름을 찾는다 — '호출하겠습니다'만 남기고 끝난 턴을 코드가
    이어받을 때 무엇을 부르려 했는지 복원하는 용도. 정확 일치 우선, 오타(밑줄 포함 토큰)는
    근접 후보가 유일할 때만 교정한다(모호하면 버림 — 엉뚱한 도구 호출이 미호출보다 나쁘다)."""
    import difflib  # noqa: PLC0415
    ns = set(names or [])
    out: list = []
    for t in re.findall(r"[a-z][a-z0-9_]{2,}", (text or "").lower()):
        if t in ns:
            if t not in out:
                out.append(t)
        elif "_" in t:
            close = difflib.get_close_matches(t, list(ns), n=2, cutoff=0.85)
            if len(close) == 1 and close[0] not in out:
                out.append(close[0])
    return out


async def _agent_stream(app: FastAPI, req: ChatRequest) -> AsyncIterator[bytes]:
    full: list[str] = []
    turn_imgs: list = []
    _turn_images.set(turn_imgs)
    # 근거 블록·수치 대조용 — 이번 턴에 실제로 실행된 도구와 그 출력 원문.
    # contextvar 가 아니라 지역 리스트인 이유는 turn_imgs 와 같다: LangGraph 실행 컨텍스트를
    # 넘지 못해 스트림 쪽에서 직접 회수해야 한다(실측 빈 값).
    turn_calls: list = []          # [(도구명, 인자요약)]
    turn_out: list = []            # 도구 출력 원문 조각(수치 대조용)
    # 마지막 도구 결과 **뒤에** 모델이 뱉은 글자 수. 0 이면 "확인하겠습니다" 하고 도구만 부른 뒤
    # 말없이 끝난 턴이다 — 화면에는 예고 한 줄만 남아 '중간에 끊긴' 것으로 보인다(실측 신고).
    # 빈 응답 구제는 text 가 통째로 빌 때만 돌아서 이 모양을 그대로 통과시켰다.
    _post_tool_chars = 0
    # 유령 ID 게이트의 출처집합 — 발화와 **전체 history** 의 정수로 시작한다(최근 몇 턴만 보면
    # "아까 그 재료" 처럼 오래된 ID 를 다시 쓰는 정상 호출이 막힌다). 도구 결과의 정수는
    # _learn_ids 가 호출 성공 시마다 더한다.
    _seed_ids = _int_tokens(req.message)
    for _h in (req.history or []):
        if not isinstance(_h, dict):
            continue
        _hc = _h.get("content") or ""
        # 사용자 발화는 그대로 출처로 인정한다. assistant 발화는 다르다 — 모델이 지어낸 정수가
        # 다음 턴에 '이전 대화에 있던 값'이 되어 게이트를 통과하는 세탁 경로가 된다
        # (1턴에 찍은 test_id=1 → 2턴에 통과 → 남의 재료 카드를 받아 온다).
        # 도구가 실제로 돌았던 턴(근거 블록이 붙은 답변)만 출처로 본다. 도구 0회 턴의 수치는
        # 근거가 없으므로 다음 턴에서도 근거가 아니다.
        if _h.get("role") == "assistant" and _EVIDENCE_MARK not in _hc:
            continue
        _seed_ids |= _int_tokens(_hc)
    _turn_ids.set(_seed_ids)
    # 재촉 → 이어하기. "야! 하라니까!" 는 그 자체로 내용이 없어, 그대로 넘기면 모델이 새 질문으로
    # 읽고 "무엇을 도와드릴까요" 로 답한다. 직전 턴이 실제로 끊겼을 때만 원래 질문을 복원한다
    # (정상 답변 뒤의 '계속' 은 더 말해달라는 새 요구이므로 건드리지 않는다).
    if _is_nudge(req.message):
        _orig, _part = _resume_target(req.history or [])
        if _orig:
            yield _sse("status", {"step": "직전 요청 이어서 진행", "tool": None})
            req = req.model_copy(update={"message": (
                f"{_orig}\n\n[이어하기] 직전 답변이 중간에 끊겼습니다. 위 요청을 처음부터 다시 "
                f"수행해 끝까지 답하세요. 필요한 도구는 다시 호출하십시오."
                + (f"\n[끊긴 지점까지 나온 내용]\n{_part[:2000]}" if _part else ""))})
            print(f"[agent] nudge→resume: {_orig[:60]!r}")
    yield _sse("status", {"step": "분석 중", "tool": None})
    try:
        # 지정 전문가 — 역할·운영 설정을 도구 선별보다 먼저 읽는다. HE팀 운영자는 자기 앱 도구를
        # 묶어야 해서 _agent_for 전에 알아야 한다(예전엔 역할 문구만 붙고 그 앱 도구는 따라오지 않아,
        # 운영자를 골라도 도구는 질의 관련도로만 뽑혔다).
        # 여러 명을 골랐으면 첫 명이 주 전문가다 — 목소리·지식카드·판단 기준은 이 사람 것이고,
        # 나머지는 도구(운영자)와 판단 기준(도메인 전문가)만 보탠다. 한 명만 고른 종전 경로는
        # _keys 가 한 원소가 되어 그대로다.
        _keys = _persona_keys(req.pinned_agents, req.pinned_agent)
        agent_key = _keys[0] if _keys else ""
        persona = await _persona_meta(app, req.groups, agent_key) if agent_key else {}
        operator = bool(persona.get("operator"))
        op_apps = list(persona.get("apps") or []) if operator else []
        # 보조 전문가 — 운영자는 앱·입구도구를, 도메인 전문가는 역할(판단 기준)을 빌려준다.
        helpers: list[tuple[str, dict]] = []
        helper_apps: list[str] = []
        for _k in _keys[1:]:
            _m = await _persona_meta(app, req.groups, _k)
            helpers.append((_k, _m))
            if _m.get("operator"):
                helper_apps.extend(_m.get("apps") or [])
        # ⚠ 보조 운영자의 앱은 **통째로 핀하지 않는다**. 앱 하나가 도구 38~81개인데 주 전문가의
        # 역할·지식카드까지 같이 실리면 스키마만으로 컨텍스트를 먹는다(실측: PCB 전문가 + 라미네이트
        # 운영자 조합이 dev 16K 에서 16,385 토큰 초과). 보조는 **입구 도구(key_tools)** 만 빌려주고
        # 나머지는 search_tools·invoke_tool 로 닿는다 — 운영자 상시 예약을 안내대로 줄인 것과 같은 이유다
        # (이 시스템 실측: 상위 40개가 호출의 79%, 도구를 늘릴수록 선택 정확도가 떨어진다).
        helper_apps = [a for a in dict.fromkeys(helper_apps) if a not in op_apps]
        # 사용자 지정 우선 도구 — 도구 카탈로그에서 직접 고른 것. 바인딩 보장(+캡 환경 우선순위)
        # 과 시스템 프롬프트 지시 둘 다로 강제한다(모델의 자율 선택은 유지 — 금지가 아니라 우선).
        pinned = [str(n)[:80] for n in (req.pinned_tools or []) if isinstance(n, str) and n.strip()][:12]
        # 캡에서 먼저 사는 도구 — 콕 집은 것과 운영자의 입구 도구(key_tools). 앱을 통째로 핀하면
        # 캡을 넘고, 핀끼리는 관련도로만 갈려 이 둘이 밀려난다.
        first = list(dict.fromkeys([*pinned, *(persona.get("key_tools") or [] if operator else []),
                                    *[t for _k, _m in helpers if _m.get("operator")
                                      for t in (_m.get("key_tools") or [])]]))
        # 앱 지정 → 그 앱의 도구로 펼침. 개별 지정과 합집합이며, 개별 지정이 앞에 온다
        # (사용자가 콕 집은 것이 앱 전체보다 우선). 12개 캡은 개별 지정에만 적용된다 —
        # 앱은 애초에 20~30개를 의도한 선택이라 같은 캡을 씌우면 조용히 잘린다.
        # 운영자가 모는 앱이 사용자 지정 앱보다 앞선다(합쳐 3개).
        user_apps = [str(a)[:80] for a in (req.pinned_apps or []) if isinstance(a, str) and a.strip()][:3]
        apps = list(dict.fromkeys([*op_apps, *user_apps]))[:3]
        _op_missing: list[str] = []
        if apps:
            _tm = _tools_map()
            _from_apps = [n for n, gk in _tm.items() if gk in apps and n not in pinned]
            pinned = list(dict.fromkeys([*pinned, *first, *sorted(_from_apps)]))
            yield _sse("status", {"step": f"지정 앱 {len(apps)}개 → 도구 {len(_from_apps)}개 우선",
                                  "tool": None, "tools_used": apps})
            # 운영자의 앱이 이 게이트웨이에 없다 — cae00 전용 앱(arp·odb-hub)을 다른 박스에서 고른 경우.
            # 말없이 두면 모델은 역할에 적힌 도구를 부르겠다고 약속만 하고 아무것도 못 부른다.
            # 매핑 자체를 못 받았으면(_tm 빈 값) 없다고 단정하지 않는다.
            if _tm:
                _have = set(_tm.values())
                _op_missing = [a for a in op_apps if a not in _have]
        # 도구 선별 질의에는 최근 히스토리를 함께 넣는다. 현재 메시지만 보면 '다시 제출해줘',
        # '그럼 그래프로 그려줘' 같은 후속 발화는 토큰이 거의 없어 관련도가 0이 되고, 직전 턴에
        # 쓰던 도구가 캡 밖으로 사라진다(감사 실측: slurm 14개 → 0개, 모델이 이미 받은 잡 ID를
        # 되물음). 사용자에겐 아무 오류도 안 보여 '갑자기 도구를 못 쓴다'로만 보인다.
        _recent = " ".join(
            str(h.get("content", ""))[:300]
            for h in (req.history or [])[-2:]
            if isinstance(h, dict)
        )
        _sel_q = f"{_recent} {req.message}".strip() if _recent else req.message
        agent = await _agent_for(app, req.groups, pinned, _sel_q, req.search_sources,
                                 req.user_email, req.user_pat, first,
                                 # 앱 하나를 통째로 핀한 턴이면 상시 예약을 안내대 4개로 줄인다.
                                 # ⚠ 조건이 '주 전문가가 운영자일 때'면 안 된다 — 이 기능의 핵심
                                 # 조합인 '도메인 전문가(주) + 도구 운영자(보조)'가 그 밖으로
                                 # 빠져 핵심 38개가 예산 밖에서 덧붙고, dev 16K 에서 16,385
                                 # 토큰으로 400 이 났다(실측: PCB 전문가 + 라미네이트 운영자).
                                 _GUIDE_TOOLS if (operator or op_apps) else None)
        # 게이트웨이에서 도구를 못 받아 오면 도구 0개 에이전트가 되고, 모델은 도구가 있다고
        # 착각한 채 "지금 바로 호출하겠습니다"만 하고 아무것도 호출하지 않는다(조용한 실패).
        # 사용자에게 상태를 알리고, 모델에게도 도구가 없음을 명시해 헛약속을 막는다.
        _tool_err = (getattr(app.state, "tool_load_error", None) or {}).get(frozenset(req.groups))
        # 강등은 '도구 없음' 이 아니다 — 서비스 계정 시야로 돌 뿐 도구는 다 있다. 그래서
        # 도구 호출을 막지 않고, 사용자에게만 그 사실을 알린다(사용자별 칸이라 남에게 안 번진다).
        _tool_deg = (getattr(app.state, "tool_degraded", None) or {}).get(
            (frozenset(req.groups), (req.user_email or "").strip().lower()))
        sys_prompt = SYSTEM_PROMPT
        if _tool_deg and not _tool_err:
            yield _sse("status", {"step": "내 자격증명으로 도구에 붙지 못해 공용 권한으로 답합니다 "
                                          "— 내 대화 검색·저장은 이번 턴에 안 됩니다",
                                  "tool": None})
        if _tool_err:
            yield _sse("status", {"step": f"도구 목록을 불러오지 못했습니다({_tool_err}) — 도구 없이 답변합니다",
                                  "tool": None})
            sys_prompt += ("\n\n[중요] 이번 턴에는 사용 가능한 도구가 하나도 없다(게이트웨이 연결 실패). "
                           "도구를 호출하겠다고 말하지 말고, 도구 없이 답할 수 있는 범위만 답한 뒤 "
                           "도구가 필요하다면 '도구 연결이 복구되면 다시 시도해 달라'고 알려라.")
        # 앱 지정이면 도구 이름 20~30개를 나열하지 않는다 — 프롬프트가 이름 목록으로 뒤덮이면
        # 모델이 무엇을 왜 쓰는지보다 목록 훑기에 예산을 쓴다. 앱 이름과 개수만 말하고,
        # 실제 강제는 바인딩 우선순위(pin)가 담당한다.
        if apps:
            _labels = ", ".join(_app_label(a) for a in apps)
            _hdr, _why = (("[운영 앱 — 선택한 HE팀 운영자]", "이 운영자가 모는 앱이다")
                          if set(apps) <= set(op_apps) else
                          ("[사용자 지정 우선 앱]", "사용자가 이 앱(들)의 기능을 쓰라고 직접 골랐다"))
            sys_prompt += (f"\n\n{_hdr}\n{_labels}\n"
                           f"{_why}. 이 질문 처리에 적합한 도구가 "
                           f"그 앱 안에 있으면 반드시 우선 호출하고 결과를 답변에 인용하라"
                           f"(다른 도구 사용 금지는 아니다). 어떤 도구를 쓸지는 네가 고른다.")
            if req.pinned_tools:
                _direct = [n for n in pinned if n in set(req.pinned_tools or [])]
                if _direct:
                    sys_prompt += ("\n그중 사용자가 콕 집은 도구: " + ", ".join(_direct[:12]) + " — 가장 우선.")
        elif pinned:
            sys_prompt += ("\n\n[사용자 지정 우선 도구]\n" + ", ".join(pinned)
                           + "\n사용자가 직접 선택한 도구다. 이 질문 처리에 적합하면 반드시 이 도구들을 "
                             "우선 호출하고, 결과를 답변에 인용하라(다른 도구 사용 금지는 아님).")
        if _op_missing:
            _ml = ", ".join(_app_label(a) for a in _op_missing)
            yield _sse("warning", {"code": "operator_app_missing",
                                   "message": f"{agent_key} 가 모는 앱({_ml})이 이 게이트웨이에 연결돼 있지 않습니다 — "
                                              "그 앱 도구 없이 답합니다."})
            sys_prompt += (f"\n\n[중요] 이 운영자가 모는 앱({_ml})의 도구가 지금 연결돼 있지 않다. 그 도구를 "
                           "호출하겠다고 말하지 말고, 연결되지 않았다는 사실을 먼저 밝힌 뒤 도구 없이 답할 수 "
                           "있는 범위만 답하라.")
        # 지정 전문가 페르소나 — 선택 패널에서 고른 전문가의 역할로 답하는 '전문가와 대화' 모드.
        if agent_key and operator:
            # HE팀 MCP 운영자 — 역할(사전 지식·작업 순서·함정)만 싣고 지식카드 선조회는 하지 않는다.
            # 이 역할의 근거는 앱 도구 결과다. 지식카드 0건이라고 "사내 지식카드에 없다"를 먼저
            # 밝히게 하면, 도구로 답할 질문에 엉뚱한 단서가 붙는다.
            _opl = ", ".join(_app_label(a) for a in op_apps) or "지정 앱"
            sys_prompt += (f"\n\n[HE팀 MCP 운영자 — 사용자가 선택]\n너는 '{agent_key}' 운영자다. 아래 사전 지식과 "
                           f"작업 순서를 따라 {_opl} 의 도구를 직접 호출해 답하라.\n{persona.get('role') or ''}")
            yield _sse("status", {"step": f"{agent_key} — {_opl} 도구로 답합니다", "tool": None})
        elif agent_key:
            role = persona.get("role") or ""
            if role:
                sys_prompt += (f"\n\n[전문가 페르소나 — 사용자가 선택]\n너는 '{agent_key}' 전문가다. "
                               f"아래 역할과 범위를 지켜 그 전문가로서 답하라.\n{role}\n"
                               f"이 전문가 도메인의 데이터 조회는 agent_search(\"{agent_key}\", 질문) 를 우선 사용하라.")
            # 그 전문가의 지식카드를 코드가 미리 조회해 붙인다. 위 지시만으로는 모델이 안 부르면
            # 그만이고, 사용자에겐 '이 전문가는 아는 게 없다'로만 보인다. 질의는 도구 선별과 같은
            # _sel_q(최근 히스토리 + 현재 발화) 를 쓴다 — 현재 발화만 넣으면 '그럼 더 자세히' 같은
            # 후속 질의에서 관련도가 무너지는 문제가 도구 선별에서 이미 실측됐다.
            yield _sse("status", {"step": f"{agent_key} 지식카드 검색", "tool": "agent_search"})
            know = await _persona_knowledge(app, req.groups, agent_key, _sel_q)
            if know:
                sys_prompt += (f"\n\n[{agent_key} 지식카드 — 이번 질문 연관 발췌]\n{know}\n"
                               f"이 발췌는 그 전문가가 실제로 보유한 사내 지식이다. 답변에 해당하는 내용이 "
                               f"있으면 반드시 근거로 인용하되, 발췌에 없는 것을 있는 것처럼 말하지 마라. "
                               f"더 필요하면 agent_search(\"{agent_key}\", 구체적 질의) 로 추가 조회하라.")
                yield _sse("status", {"step": f"지식카드 {len(know):,}자 주입", "tool": None})
            else:
                # '없다' 와 '못 물어봤다' 를 구분해 말한다. 검색이 늦거나 죽어서 못 받은 것을
                # '보유 지식 없음' 으로 말하면, 그 전문가가 아는 게 없다는 거짓이 사용자에게 간다.
                _note = getattr(_persona_knowledge, "last_note", "")
                if _note:
                    sys_prompt += (f"\n\n[{agent_key} 지식카드]\n지식 조회가 시간 안에 끝나지 않아 "
                                   f"이번 답변에는 사내 발췌가 없다. 일반 지식으로 답하되 "
                                   f"'사내 지식카드를 조회하지 못했다(없다는 뜻이 아니다)'고 먼저 밝혀라.")
                    yield _sse("warning", {"code": "knowledge_degraded",
                                           "message": f"{agent_key} 지식카드를 조회하지 못했습니다 — {_note}. "
                                                      "보유 지식이 없다는 뜻이 아닙니다."})
                    yield _sse("status", {"step": f"지식카드 조회 실패 — {_note}", "tool": None})
                else:
                    # 0히트를 침묵하면 모델이 일반 지식으로 메우고 사용자는 그것을 사내 지식으로 읽는다.
                    sys_prompt += (f"\n\n[{agent_key} 지식카드]\n이번 질문과 연관된 보유 지식을 찾지 못했다. "
                                   f"일반 지식으로 답하되 '사내 지식카드에는 관련 내용이 없다'고 먼저 밝혀라.")
                    yield _sse("status", {"step": "연관 지식카드 없음 — 일반 지식으로 답변", "tool": None})
        # 보조 전문가 — 목소리는 주 전문가 하나다. 보조는 판단 기준(역할)과 도구를 빌려줄 뿐이고,
        # 각자 따로 답하지 않는다(그건 Thinking 이고, 갈린 의견을 세우는 자리는 심의다).
        # ⚠ 갈리는 것을 한 목소리가 숨기면 '거짓 합의'가 된다 — 갈리면 한 줄로 밝히게 한다.
        if helpers:
            _blocks = []
            for _k, _m in helpers:
                _r = (_m.get("role") or "")[:PERSONA_HELPER_ROLE_MAX]
                _tag = "MCP 운영자" if _m.get("operator") else "도메인 전문가"
                _apps_note = ""
                if _m.get("operator") and _m.get("apps"):
                    # 입구 도구만 묶여 있다 — 나머지는 안내대(search_tools·invoke_tool)로 닿는다고
                    # 명시한다. 안 그러면 '도구가 없다'고 판단해 일반 지식으로 답한다.
                    _apps_note = (f" (도구: {', '.join(_app_label(a) for a in _m['apps'])}"
                                  " — 입구 도구만 묶여 있다. 더 필요하면 search_tools 로 찾아 "
                                  "invoke_tool 로 호출하라)")
                _blocks.append(f"- {_k} · {_tag}{_apps_note}\n{_r}" if _r else f"- {_k} · {_tag}{_apps_note}")
            sys_prompt += (
                "\n\n[함께 선택된 보조 전문가 — 렌즈와 도구만 빌려준다]\n"
                + "\n".join(_blocks)
                + f"\n\n답은 '{agent_key}' 한 사람의 목소리로 쓴다. 보조 전문가의 이름으로 따로 말하지 말고, "
                  "그들의 판단 기준과 도구를 네 판단에 녹여라. 보조 전문가의 사내 지식이 필요하면 "
                  "agent_search(\"<그 전문가 키>\", 질의) 로 직접 조회하라.\n"
                  "⚠ 관점이 실제로 갈리면 숨기지 마라 — 답 끝에 '이견: <무엇이 갈리는지>' 한 줄을 붙이고, "
                  "판단이 갈려 결론을 못 내면 '/심의 <질문>' 으로 정식 심의를 권하라.")
            yield _sse("status", {"step": f"주 전문가 {agent_key} + 보조 {len(helpers)}명으로 답합니다",
                                  "tool": None})
        # 붙인 문서는 **sys_prompt 에** 싣는다 — 아래 세 갈래(첫 호출·오류 재시도·강제 도구호출)가
        # 모두 sys_prompt 를 공유하므로, user 메시지에 붙이면 재시도 경로에서 조용히 빠진다.
        # ⚠ 순서가 중요하다. 문서 예산은 '컨텍스트 − 예비분 − 이력' 인데, 여기서 빼야 할 이력은
        # 원본이 아니라 **압축 뒤 실제로 실리는 것**이다. 원본을 빼면 긴 대화에서 문서가 자리를
        # 통째로 잃는다(실측 128K 창에서 문서 몫이 1,502자까지 떨어졌다).
        _hstat: dict = {}
        _hist_msgs = _history_messages(req.history, _hstat, has_docs=bool(req.documents))
        if _hstat.get("compacted"):
            yield _sse("status", {"step": f"이전 대화 {_hstat['compacted']}턴은 길이 때문에 "
                                          f"발췌로 압축해 넣었습니다(최근 {_hstat['kept']}턴은 원문)",
                                  "tool": None})
        _hist_tokens = _est_tokens("".join(c for _, c in _hist_msgs))
        _docs = _doc_block(req.documents, _hist_tokens)
        if _docs:
            sys_prompt += _docs
            _n = min(len(req.documents), DOC_MAX_FILES)
            _step = f"붙인 문서 {_n}건을 읽습니다"
            if "⋯ 가운데" in _docs:
                # 대화가 길어 자리가 없는 것인지, 문서 자체가 긴 것인지 구분해 준다 —
                # 앞의 경우엔 '새 대화에서 붙이면 더 읽는다' 가 답이라 조치가 다르다.
                _step += (" — 대화가 길어 문서에 줄 자리가 적습니다(새 대화에서 붙이면 더 읽습니다)"
                          if _hist_tokens > _model_context_tokens() * 0.3
                          else " — 길어서 가운데 일부는 건너뜁니다")
            yield _sse("status", {"step": _step, "tool": None})
        messages = [("system", sys_prompt), *_hist_msgs, ("user", req.message)]
        inputs = {"messages": messages}
        # 호출 예산 — 작은 모델은 같은 도구를 같은 인자로 반복 호출하다 그래프 재귀 한도에
        # 부딪히고, 그러면 그때까지 스트리밍된 내용까지 버려진 채 '응답 생성 실패'만 남는다
        # (감사 확인). 모델의 자제력에 기대지 말고 코드가 상한을 건다.
        # blocked: 유령 ID 게이트가 실행을 막은 호출 수 — 실효 호출 수에서 뺀다.
        _budget = {"calls": 0, "blocked": 0, "seen": {}}
        _cfg = {"recursion_limit": AGENT_RECURSION_LIMIT}
        async for event in agent.astream_events(inputs, version="v2", config=_cfg):
            kind = event["event"]
            if kind == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:  # empty on tool-call delta chunks — guard
                    full.append(token)
                    _post_tool_chars += len(token if isinstance(token, str) else str(token))
                    yield _sse("token", {"delta": token})
            elif kind == "on_chat_model_end" and getattr(app.state, "llm_nostream", False):
                # 비스트리밍 모드 — stream 이벤트가 없으므로 완결 응답에서 텍스트를 회수해
                # 모델 턴 단위로 방출한다(도구 호출만 한 턴은 content 가 비어 자연히 skip).
                out_msg = event.get("data", {}).get("output")
                content = getattr(out_msg, "content", "") or ""
                if isinstance(content, list):  # 멀티파트 content 방어
                    content = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
                if content:
                    full.append(content)
                    _post_tool_chars += len(content)
                    yield _sse("token", {"delta": content})
            elif kind == "on_tool_start":
                _budget["calls"] += 1
                _fp = f"{event.get('name')}|{json.dumps(event.get('data', {}).get('input'), sort_keys=True, ensure_ascii=False, default=str)[:400]}"
                _budget["seen"][_fp] = _budget["seen"].get(_fp, 0) + 1
                if _budget["seen"][_fp] == TOOL_REPEAT_WARN:
                    # 같은 도구·같은 인자 반복 — 더 해도 결과가 달라지지 않는다.
                    yield _sse("status", {"step": f"같은 호출 반복 감지({event.get('name')}) — 결과가 바뀌지 않습니다",
                                          "tool": event.get("name")})
                args = _tool_preview(event.get("data", {}).get("input"))
                turn_calls.append((str(event.get("name") or "?"), args or ""))
                yield _sse("status", {"step": f"도구 호출: {event['name']}", "tool": event["name"],
                                      **({"detail": args} if args else {})})
            elif kind == "on_tool_end":
                # 도구 출력에 실린 아티팩트 URL 수집 — contextvar 는 LangGraph 실행 컨텍스트를
                # 넘지 못해(실측 빈 값) 스트림 쪽에서 직접 회수한다.
                _raw = event.get("data", {}).get("output")
                _txt = getattr(_raw, "content", _raw)
                if isinstance(_txt, str):
                    # 수치 대조용 원문 적재 — 총량을 묶어 두지 않으면 대용량 조회 한 번에
                    # 메모리가 튄다. 앞부분만 남겨도 수치는 대개 초반에 실린다.
                    if sum(len(x) for x in turn_out) < 400_000:
                        turn_out.append(_txt[:120_000])
                    for _u in re.findall(r"/agent/artifacts/[A-Za-z0-9][A-Za-z0-9_.-]*", _txt):
                        if _u not in turn_imgs:
                            turn_imgs.append(_u)
                    # 게이트에 막힌 호출은 '도구를 썼다'로 세면 안 된다. 세면 그 턴이 no-tool 로
                    # 잡히지 않아 강제 재시도가 발동하지 않고, 모델이 "list_materials 를 부르겠다"
                    # 고 말만 하고 끝난다(실측 Q3: 유일한 호출이 차단된 plot_curve 였다).
                    if _PHANTOM_ID_MARK in _txt:
                        _budget["blocked"] += 1
                out = _tool_preview(_raw)
                # 심의 핸드오프용 날것 — 표시용보다 길 때만 싣는다(짧은 결과에 중복 페이로드 금지).
                # ⚠ 변수명 주의 — `full` 은 1701행의 **토큰 누적 리스트**다. 처음에 `full =` 로
                #   덮어썼다가 다음 토큰의 full.append 가 AttributeError 로 챗을 죽였다(감사 C35).
                _hand = _tool_preview(_raw, HANDOFF_RESULT_CHARS)
                _post_tool_chars = 0   # 이 결과 뒤에 모델이 말을 했는지만 본다
                yield _sse("status", {"step": f"도구 완료: {event['name']}", "tool": event["name"],
                                      **({"result_preview": out} if out else {}),
                                      **({"result_full": _hand} if len(_hand) > len(out) else {})})
    except Exception as exc:
        # 상세는 서버 로그에만(내부 유출 방지). 단 AGENT_DEBUG_ERRORS=1 이면 예외 타입·메시지를
        # 브라우저 응답에도 실어 운영자가 바로 원인을 본다(기본 꺼짐 — 켜면 재시작 필요).
        print(f"[agent] chat error: {exc!r}")
        import traceback as _tb
        _tb.print_exc()
        detail = ""
        if os.environ.get("AGENT_DEBUG_ERRORS") == "1":
            detail = f" — {type(exc).__name__}: {str(exc)[:400]}"
        # 부분 응답이 있으면 버리지 않는다 — 여기까지 스트리밍된 내용 + 중단 사실을 명시해
        # 대화 기록에 남긴다. 아무 설명 없이 초록불만 꺼지는 상태를 만들지 않는다.
        _partial = "".join(full).strip()
        if _partial:
            _note = ("\n\n⚠ 처리 중 내부 오류로 응답이 여기서 중단되었습니다"
                     f"{detail}. 같은 질문을 다시 보내면 재시도합니다.")
            yield _sse("token", {"delta": _note})
            yield _sse("result", {"type": "text", "content": _partial + _note})
        else:
            # 부분 응답조차 없으면 사용자는 오류 한 줄만 받는다. 일시적 실패(스트림 끊김·
            # 상류 5xx·파서 오류)가 대부분이라 한 번은 자동으로 다시 해 본다 — 사용자가
            # "야" 하고 재촉해야 다시 도는 것은 시스템이 할 일을 사람에게 미루는 것이다.
            # 스트리밍이 아니라 단발 호출로 간다(스트림 경로가 방금 실패한 그 경로다).
            _rescued = ""
            # 즉시 1회로는 순간 장애를 못 넘긴다. 상류가 잠깐 끊긴 경우가 대부분이라
            # 짧게 쉬었다 다시 한다. 간격 없이 붙이면 같은 실패를 그대로 다시 받는다.
            _tries = max(0, int(os.environ.get("CHAT_AUTO_RETRY", "2") or 0))
            for _attempt in range(1, _tries + 1):
                yield _sse("status", {"step": f"오류 발생 — 자동 재시도 {_attempt}/{_tries}",
                                      "tool": None})
                if _attempt > 1:
                    await asyncio.sleep(min(4.0, 1.5 * (_attempt - 1)))
                try:
                    _msgs = [("system", sys_prompt), *_history_messages(req.history),
                             ("user", req.message)]
                    # _cfg 는 try 안에서 정의되므로 그 전에 터진 예외에서는 없다.
                    _cfg4 = locals().get("_cfg") or {"recursion_limit": AGENT_RECURSION_LIMIT}
                    _r4 = await agent.ainvoke({"messages": _msgs}, config=_cfg4)
                    for m in reversed((_r4 or {}).get("messages") or []):
                        if getattr(m, "type", "") != "ai":
                            continue
                        _c4 = getattr(m, "content", None)
                        if isinstance(_c4, list):
                            _c4 = "".join(p.get("text", "") if isinstance(p, dict) else str(p)
                                          for p in _c4)
                        if isinstance(_c4, str) and _c4.strip():
                            _rescued = _c4.strip()
                            break
                    if _rescued:
                        break
                except Exception as exc2:  # noqa: BLE001
                    print(f"[agent] auto-retry {_attempt}/{_tries} failed: {exc2!r}")
                    exc = exc2          # 마지막 실패 원인으로 메시지를 만든다
            if _rescued:
                print(f"[agent] auto-retry 성공 — 오류 턴 복구(시도 {_attempt}회)")
                yield _sse("token", {"delta": _rescued})
                yield _sse("result", {"type": "text", "content": _rescued})
            else:
                # 원인을 뭉뚱그리지 않는다. LLM 미도달은 '에이전트 오류' 가 아니라 인프라
                # 문제이고, 사용자가 질문을 바꿔도 해결되지 않는다 — 그렇게 말해 줘야 한다.
                _kind = type(exc).__name__
                if "APIConnection" in _kind or "APITimeout" in _kind or "Connection" in _kind:
                    _msg = ("LLM 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요 — "
                            f"질문을 바꿔도 해결되지 않습니다.{detail}")
                else:
                    _msg = f"에이전트 처리 중 오류{detail} (자동 재시도 {_tries}회 모두 실패)"
                yield _sse("error", {"code": "agent_error", "message": _msg})
        yield _sse("done", {})
        return
    text = "".join(full)
    # 도구 호출이 '텍스트로 샌' 경우 구조 — vLLM 스트리밍 tool-call 파서가 호출을 못 뽑으면
    # 모델이 낸 <tool_call>{"name":…,"arguments":…}</tool_call> 가 그대로 본문에 실려 오고,
    # 도구는 하나도 실행되지 않는다. 사용자에겐 "조회했더니 아무것도 안 나온다"로 보인다
    # (실측: 스트리밍 ON → 호출문 누출·미실행 / OFF → get_training_data 정상 실행).
    # 캡을 씌우거나 무시하지 말고, 그 턴을 비스트리밍으로 한 번 다시 돌려 실제로 호출시킨다.
    # 실효 호출 0회 = 진짜로 아무 데이터도 못 가져온 턴(차단된 호출은 데이터가 없다).
    _no_tool = (_budget["calls"] - _budget["blocked"]) <= 0
    # 예고만 하고 호출하지 않은 턴 — 도구 이름이 없어 복원이 불가하므로 '금지+강제' 지시를
    # 붙여 한 번만 다시 돌린다. 모델의 자발성에 기대지 않고 코드가 한 번 더 기회를 만든다.
    _fabricated = _no_tool and _looks_fabricated_tool_result(text)
    # 자작 표/차트도 같은 취급 — _no_tool 이 앞에 있으므로 도구를 부른 뒤의 자작은 영향 없다.
    _own_chart = _no_tool and _drew_own_chart(req.message, text)
    if _no_tool and (_announced_without_calling(text) or _fabricated or _own_chart):
        _why = "날조 표지" if _fabricated else ("자작 표·차트" if _own_chart else "예고만")
        print(f"[agent] 도구 미호출({_why}) — 강제 지시로 1회 재시도")
        yield _sse("status", {"step": "도구를 실제로 호출하도록 다시 시도합니다", "tool": None})
        try:
            _forced = [("system", sys_prompt + "\n\n[중요] 도구를 쓰겠다고 예고하지 마라. "
                        "설명이나 인자 예시를 출력하지 말고, 필요한 도구를 **지금 즉시 호출**하라. "
                        "인자를 모르면 기본값이나 빈 인자로 호출한 뒤 결과를 보고 판단하라. "
                        "차트·표를 직접 작성하지 말고 데이터 조회 도구를 먼저 호출하라."),
                       *_history_messages(req.history), ("user", req.message)]
            _r2 = await agent.ainvoke({"messages": _forced}, config=_cfg)
            for m in reversed((_r2 or {}).get("messages") or []):
                if getattr(m, "type", "") != "ai":
                    continue
                _c2 = getattr(m, "content", None)
                if isinstance(_c2, list):
                    _c2 = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in _c2)
                if isinstance(_c2, str) and _c2.strip() and not _announced_without_calling(_c2):
                    text = _c2
                    yield _sse("token", {"delta": "\n\n" + _c2})
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] forced-call retry failed: {exc!r}")
        # 여기까지 왔는데 여전히 도구 0회 + 날조 표지면, 근거 없는 수치를 '조회 결과'처럼
        # 내보내는 셈이다. 조용히 통과시키지 말고 출처가 없다는 사실을 명시한다.
        if (_budget["calls"] - _budget["blocked"]) <= 0 and (_looks_fabricated_tool_result(text)
                                      or _drew_own_chart(req.message, text)):
            _warn = ("\n\n> ⚠ 이 답변은 **도구를 조회하지 않고 생성**되었습니다. "
                     "수치·날짜는 실제 데이터가 아닐 수 있으니 그대로 사용하지 마세요.")
            text += _warn
            yield _sse("token", {"delta": _warn})
    # ── 확정 종결 — "조회하겠습니다"만 남기고 끝나거나 빈 응답으로 끝나는 애매한 턴을 없앤다.
    # 사용자 요구(2026-08-05): 스트림이 애매하게 꺼지지 말 것 — 도구가 있는지 없는지 판별될
    # 때까지 진행해서, 있으면 코드가 직접 1회 호출해 결과로 답하고, 없으면 '없다'고 명시하고
    # 끝낸다. 어느 쪽이든 사용자는 결론을 받는다(누출 케이스는 아래 기존 경로가 처리).
    if ((_budget["calls"] - _budget["blocked"]) <= 0
            and (_announced_without_calling(text) or not text.strip())
            and not _looks_like_leaked_tool_call(text, _no_tool)):
        yield _sse("status", {"step": "도구 유무 확인 — 확정 종결 절차", "tool": None})
        try:
            _tmap = await _tools_by_name(app, req.groups, user=req.user_email, user_pat=req.user_pat)
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] finalizer tool load failed: {exc!r}")
            _tmap = {}
        _cand = _mentioned_tools(text, _tmap.keys())
        if not _cand and _tmap:
            # 예고문에 도구 이름이 없으면 질의 관련도 상위 1개로 보수적 폴백 — 절반 이상
            # 어휘가 겹칠 때만(엉뚱한 도구를 부르는 것이 미호출보다 나쁘다).
            _rk = _rank_tools(_tmap, req.message, top_k=3)
            if _rk and _rk[0].get("score", 0) >= 0.5:
                _cand = [_rk[0]["name"]]
        if _cand and _cand[0] in _tmap:
            _tn = _cand[0]
            _brief = _tool_schema_brief(_tmap[_tn])
            try:
                _argd = _parse_json(await _llm_text(
                    app.state.llm, "당신은 도구 호출 계획자입니다. 반드시 유효한 JSON 하나만 출력하세요.",
                    f"[사용자 질문]\n{req.message}\n\n[도구]\n{_tn}: "
                    f"{(getattr(_tmap[_tn], 'description', '') or '')[:300]}\n"
                    + (f"[인자 스키마]\n{_brief}\n" if _brief else "")
                    + "\n이 질문에 맞게 이 도구를 1회 호출할 인자 JSON 을 출력하라. "
                      "스키마 타입을 지켜라. 값을 알 수 없는 필수 인자가 있으면 {\"skip\": true} 만 출력."))
            except Exception:  # noqa: BLE001
                _argd = None
            if not isinstance(_argd, dict) or _argd.get("skip"):
                _close = (f"\n\n확인 결과 — 도구 `{_tn}` 는 있습니다. 다만 질문만으로 필수 인자를 "
                          f"확정할 수 없어 호출하지 못했습니다. 필요한 값을 알려주시면 바로 실행합니다."
                          + (f"\n(인자: {_brief[:200]})" if _brief else ""))
                text = (text + _close).strip()
                yield _sse("token", {"delta": _close})
            else:
                yield _sse("status", {"step": f"도구 직접 실행: {_tn}", "tool": _tn,
                                      "detail": json.dumps(_argd, ensure_ascii=False)[:200]})
                try:
                    _out = await _call(_tmap, _tn, _argd)
                except Exception as exc:  # noqa: BLE001
                    _out = f"(tool {_tn} error: {exc})"
                _out = _cap(str(_out))
                yield _sse("status", {"step": f"도구 완료: {_tn}", "tool": _tn})
                try:
                    _final = (await _llm_text(
                        app.state.llm,
                        "너는 도구 결과를 사용자에게 한국어로 정리해 주는 조수다. 도구를 다시 "
                        "호출하겠다고 말하지 말고, 아래 결과만으로 답하라. 결과가 오류라면 오류라고 "
                        "명확히 알리고 다음 행동을 제안하라.",
                        f"[사용자 질문]\n{req.message}\n\n[{_tn} 결과]\n{_out}")).strip()
                except Exception:  # noqa: BLE001
                    _final = f"[{_tn} 결과]\n{_out[:1500]}"
                text = (text + "\n\n" + _final).strip()
                yield _sse("token", {"delta": "\n\n" + _final})
        else:
            _near = ""
            if _tmap:
                _rk = _rank_tools(_tmap, req.message, top_k=3)
                _near = ", ".join(f"`{r['name']}`" for r in _rk) if _rk else ""
            _close = ("\n\n확인 결과 — 이 요청을 수행할 도구가 현재 연결된 도구 목록"
                      + (f"({len(_tmap)}개)" if _tmap else "(게이트웨이 미연결 상태)") + "에 없습니다."
                      + (f" 이름이 가까운 후보: {_near}." if _near else "")
                      + " 해당 기능이 추가되면 이 채팅에서 바로 사용할 수 있습니다.")
            text = (text + _close).strip()
            yield _sse("token", {"delta": _close})

    if _looks_like_leaked_tool_call(text, _no_tool):
        print(f"[agent] tool-call leaked into text (도구 실행 {_budget['calls']}회) — 비스트리밍 재시도")
        yield _sse("status", {"step": "도구 호출을 다시 시도합니다", "tool": None})
        try:
            retry = await agent.ainvoke(inputs)
            _msgs = (retry or {}).get("messages") or []
            _last = ""
            for m in reversed(_msgs):
                # ⚠ 반드시 AI 메시지만. 아무 메시지나 집으면 도구 원문(ToolMessage), 사용자
                # 질문(HumanMessage), 최악의 경우 **시스템 프롬프트 전문**(SystemMessage)이
                # 그대로 답변으로 나간다(감사에서 실제 노출 확인). 폴백이 유출 경로가 되면 안 된다.
                if getattr(m, "type", "") != "ai":
                    continue
                _c = getattr(m, "content", None)
                if isinstance(_c, list):  # 멀티파트 방어
                    _c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in _c)
                if isinstance(_c, str) and _c.strip() and not _looks_like_leaked_tool_call(_c):
                    _last = _c
                    break
            if _last:
                text = _last
                yield _sse("token", {"delta": "\n\n" + _last})
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] non-streaming retry failed: {exc!r}")
            yield _sse("status", {"step": f"도구 호출 재시도 실패({type(exc).__name__})", "tool": None})

        # 재시도해도 여전히 누출이면 모델의 호출 능력에 기대지 않는다 — 새어 나온 호출을
        # 코드가 직접 실행하고 그 결과로 답을 만든다. 작은 모델에서도 도구가 '잡히게' 하는
        # 마지막 방어선이다(모델이 구조화 호출을 영영 못 내도 사용자는 결과를 받는다).
        if _looks_like_leaked_tool_call(text, _no_tool):
            calls = _extract_leaked_calls(text, _no_tool)
            if calls:
                try:
                    _tmap = await _tools_by_name(app, req.groups, user=req.user_email, user_pat=req.user_pat)
                except Exception as exc:  # noqa: BLE001
                    print(f"[agent] direct-exec tool load failed: {exc!r}")
                    _tmap = {}
                results = []
                for c in calls:
                    if c["name"] not in _tmap:
                        # 작은 모델은 도구 이름을 자주 흘린다(get_dataset_summry, queryVoc 등).
                        # 사람이라면 바로 알아볼 오타 때문에 답을 못 주는 건 낭비다 — 충분히
                        # 가까운 이름 하나로만 좁혀질 때에 한해 교정한다(모호하면 포기).
                        import difflib as _dl
                        _cand = _dl.get_close_matches(c["name"], list(_tmap), n=2, cutoff=0.82)
                        if len(_cand) == 1:
                            print(f"[agent] 도구 이름 교정: {c['name']} → {_cand[0]}")
                            c["name"] = _cand[0]
                        else:
                            continue
                    yield _sse("status", {"step": f"도구 직접 실행: {c['name']}", "tool": c["name"]})
                    try:
                        r = await _call(_tmap, c["name"], c["arguments"])
                    except Exception as exc:  # noqa: BLE001
                        r = f"(tool {c['name']} error: {exc})"
                    results.append((c["name"], _cap(str(r))))
                if results:
                    joined = "\n\n".join(f"[{n} 결과]\n{v}" for n, v in results)
                    try:
                        final = (await _llm_text(
                            app.state.llm,
                            "너는 도구 결과를 사용자에게 한국어로 정리해 주는 조수다. 도구를 다시 "
                            "호출하겠다고 말하지 말고, 아래 결과만으로 답하라.",
                            f"[사용자 질문]\n{req.message}\n\n{joined}")).strip()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[agent] direct-exec summarize failed: {exc!r}")
                        final = joined
                    text = final
                    yield _sse("token", {"delta": "\n\n" + final})
    # 도구가 만든 그래프를 모델이 인용하지 않았으면 코드가 붙인다 — 소형 모델의 지시 누락으로
    # 생성된 이미지가 화면에서 사라지는 것을 막는 결정적 보강(중복 첨부는 방지).
    # 마크다운 이미지 형태(](url))가 없으면 첨부 — 모델이 URL 을 본문에 평문으로만 적은 경우도 보강.
    missing = [u for u in turn_imgs if f"]({u})" not in text]
    if missing:
        add = "\n\n" + "\n".join(f"![생성된 그래프]({u})" for u in missing)
        text += add
        yield _sse("token", {"delta": add})
    # 빈 응답 구제 — 도구는 돌았는데 모델이 마무리 텍스트를 내지 않은 턴. 기존 보강기는
    # '도구 0회' 조건이 붙어 있어 이 경우를 통과시켰고, 화면에는 '(응답이 없습니다)' 만 남았다.
    # 사용자에겐 초록불이 꺼지고 아무것도 안 나온 것으로 보인다 — 조회는 이미 다 해 놓고서다.
    # 도구 결과가 손에 있으므로 그것으로 최소 응답을 만든다. 없는 말을 지어내지 않고,
    # 조회한 사실과 결과 발췌만 싣는다.
    # ⚠ 여기서 '빈 응답'은 두 모양이다. ① 아무 글자도 없는 턴 ② **예고만 하고 끊긴 턴** —
    # "먼저 가이드와 템플릿을 확인하겠습니다" 하고 도구를 부른 뒤 결과를 받고도 아무 말 없이
    # 끝난다(실측 신고: 보고서 작성이 늘 그 문장에서 멈췄다). ②는 text 가 비어 있지 않아
    # 예전 조건을 그대로 통과했고, 사용자에겐 '중간에 끊긴 응답'으로 보였다.
    if _needs_final_rescue(turn_calls, text, _post_tool_chars):
        _names = ", ".join(dict.fromkeys(n for n, _ in turn_calls))
        _excerpt = "\n".join(turn_out)[:12000].strip()
        _before = text   # 구제로 뭐라도 보탰는지 판정용(예고만 남은 턴은 text 가 비지 않는다)
        print(f"[agent] {'stalled' if text.strip() else 'empty'} final after tools({_names}) — 자동 재시도")
        yield _sse("status", {"step": "도구 결과 뒤 응답이 없어 자동 재시도", "tool": None})
        # 재시도는 '요약만' 시킨다 — 도구를 다시 부르면 같은 조회를 반복하고 같은 자리에서
        # 또 빌 수 있다. 이미 받은 결과를 컨텍스트로 주고 답만 쓰게 하는 것이 확실하다.
        try:
            # 도구를 다시 부르지 않게 한다 — 같은 조회를 반복하면 같은 자리에서 또 빈다.
            # 이미 받은 결과만 주고 요약을 시킨다.
            _msgs = [("system", "조회 결과를 사용자에게 설명하는 역할이다. 아래 결과에 있는 "
                                "내용만 쓰고 없는 값을 지어내지 마라. 도구를 새로 호출하지 마라. "
                                "결과가 비었으면 비었다고 말하라."),
                     ("user", f"[사용자 질문]\n{req.message}\n\n[조회한 도구]\n{_names}\n\n"
                              f"[조회 결과]\n{_excerpt}\n\n위 결과로 질문에 답하라.")]
            _r3 = await agent.ainvoke({"messages": _msgs}, config=_cfg)
            _c3 = None
            for m in reversed((_r3 or {}).get("messages") or []):
                if getattr(m, "type", "") == "ai":
                    _c3 = getattr(m, "content", None)
                    break
            if isinstance(_c3, list):
                _c3 = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in _c3)
            if isinstance(_c3, str) and _c3.strip():
                # 예고 문장은 지우지 않고 이어 붙인다 — 이미 사용자 화면에 흘러간 글자다.
                # 지우면 스트리밍으로 본 것과 저장본이 달라진다.
                _add = _c3.strip()
                if text.strip():
                    _add = "\n\n" + _add
                text = (text + _add) if text.strip() else _add
                yield _sse("token", {"delta": _add})
                print("[agent] empty-final retry 성공")
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] empty-final retry failed: {exc!r}")
        # 재시도까지 실패하면 조회 결과라도 내보낸다. 조회는 이미 끝났으므로 이것을 버리면
        # 사용자는 아무것도 못 받는다(초록불만 꺼지는 그 상태다).
        if text == _before:
            _short = _excerpt[:1200]
            _fallback = (f"조회는 완료했으나 요약 생성에 실패했습니다. 조회한 도구는 `{_names}` 입니다.\n\n"
                         + (f"조회 결과 발췌\n\n```\n{_short}\n```\n" if _short else ""))
            if text.strip():
                _fallback = "\n\n" + _fallback
            text = (text + _fallback) if text.strip() else _fallback
            yield _sse("token", {"delta": _fallback})
            print("[agent] empty-final retry 실패 — 원문 발췌로 대체")
    # 근거 블록 — 코드가 실행 기록에서 만든다. 모델이 쓰는 게 아니라 지어낼 수 없고,
    # 답변의 수치를 도구 출력 원문과 대조해 출처 없는 값을 함께 표시한다.
    # 사용자 발화도 출처로 인정한다 — 사용자가 준 치수를 되풀이한 것을 날조로 보면 안 된다.
    if turn_calls and os.environ.get("CHAT_EVIDENCE_BLOCK", "1") != "0":
        try:
            _src = "\n".join(turn_out) + "\n" + (req.message or "")
            _bad = _unsourced_numbers(text, _src)
            _ev = _evidence_block(turn_calls, _bad)
            if _ev:
                text += _ev
                yield _sse("token", {"delta": _ev})
        except Exception as exc:  # noqa: BLE001 — 근거 표기 실패가 답변을 못 죽인다
            print(f"[agent] evidence block failed: {exc!r}")
    yield _sse("result", {"type": "text", "content": text})
    yield _sse("done", {})


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    # 심의 모드: "/심의 <질문>" → 다중 라운드 전문가 심의 파이프라인(코드가 오케스트레이션, vLLM=GLM 이 추론).
    # 정본은 역량 있는 Claude(개인 Claude via MCP); 이건 GLM 연결 시 포털 챗으로도 되게 하는 진입점.
    denied = _denied_feature(req)
    if denied:
        # 메뉴를 숨겨도 슬래시 명령은 손으로 칠 수 있다 — 트리거를 아는 이쪽이 마지막으로 막는다.
        return StreamingResponse(_deny_stream(denied), media_type="text/event-stream", headers=SSE_HEADERS)
    if is_sim_deliberation(req.message):
        # 시뮬레이션 심의: "/시뮬심의 <현상>" → 메커니즘 심의 → CAE 해석 설계 심의 2단.
        # 일반 심의보다 먼저 검사한다 — 트리거가 겹치지는 않지만 의도를 코드 순서로 남긴다.
        stream = _detach_stream(run_sim_deliberation(app, strip_sim_trigger(req.message), req.groups,
                                                     req.delib_opts, req.user_email, req.user_pat,
                                                     req.history), "sim")
    elif is_test_plan(req.message):
        # 시험 계획 심의: "/시험계획 <목적>" → 물성 근거 공백을 조회한 뒤 우선순위·조건축까지.
        # 해석은 물성이 없으면 시작할 수 없어, 실무에서 가장 먼저 막히는 지점이다.
        stream = _detach_stream(run_test_plan(app, strip_test_plan_trigger(req.message), req.groups,
                                              req.delib_opts, req.user_email, req.user_pat,
                                              req.history), "test-plan")
    elif is_deliberation(req.message):
        stream = _detach_stream(run_deliberation(app, strip_trigger(req.message), req.groups,
                                                 req.delib_opts, req.user_email, req.user_pat,
                                                 req.history), "delib")
    elif is_report_save(req.message):
        # "/보고서 <선택: 결론>" → 대화 이력을 코드가 blocks 로 만들어 RA 저장(결정적 — LLM 미경유).
        stream = run_report_save(app, strip_report_trigger(req.message), req.history, req.groups,
                                 req.user_email, req.user_pat)
    elif is_agent_search(req.message):
        # "전문가 뭐 있어" 류 → 추천+분야 요약을 코드가 결정적으로 생성(LLM 나열 절단 방지).
        stream = run_agent_search(app, strip_agent_trigger(req.message), req.groups)
    elif is_tool_search(req.message):
        # "/도구 <질의>" 또는 '도구 뭐 있어' 류 → 도구 카탈로그+추천을 SSE tools 이벤트로(결정적).
        # 프론트가 선택 UI 를 그리고, 고른 도구는 다음 발화부터 pinned_tools 로 우선 사용된다.
        stream = run_tool_search(app, strip_tool_trigger(req.message), req.groups)
    elif is_thinking(req.message) or req.thinking:
        # 띵킹 모드 — 답할 수 있는 전문가만 각자 답하고, 못 하는 좌석은 넘길 곳을 지목한다.
        # 명시 슬래시 트리거(/심의·/도구 등)보다 **뒤**에 둔다: 모드가 켜져 있어도 그 턴에
        # 사용자가 대놓고 /심의 를 치면 그 의도가 이긴다.
        stream = run_thinking(app, strip_thinking_trigger(req.message), req.groups,
                              req.user_email, req.user_pat, req.history, req.documents)
    else:
        stream = _agent_stream(app, req)
    return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)


class ExpertsRequest(BaseModel):
    message: str
    groups: list[str] = []
    # 대화 전체 — 좌석 추천을 화두 한 줄이 아니라 오간 맥락 위에서 하기 위한 것.
    # ⚠ 통째로 임베딩 질의에 넣지 않는다(아래 _seat_axes 주석 참조). 축을 뽑는 데만 쓴다.
    history: list[dict] = []
    # 조직도 트리만 그리면 되는 호출 — 명부(키·이름)만 즉시 주고 추천·도구 카탈로그·태그는
    # 건너뛴다. 태그는 응답의 69%(796명에 301KB)인데 쓰이는 곳은 검색뿐이라, 트리를 그 무게
    # 뒤에 세울 이유가 없다. 화면은 이걸로 먼저 그리고 전체를 뒤에서 받아 얹는다.
    light: bool = False


# 대화에서 뽑을 도메인 축 개수. 축마다 recommend_agents 를 한 번씩 더 부르므로 지연과 맞바꾼다.
EXPERT_AXES = int(os.environ.get("EXPERT_AXES", "4"))
# 축 추출에 넣을 대화 분량(문자). 축만 뽑으면 되므로 크게 필요 없다.
_AXIS_CTX_BUDGET = 5000


async def _domain_roster(tools: dict) -> list[dict]:
    """전문가 풀의 **도메인 분류 정본**(list_agent_domains). 실패하면 빈 목록."""
    try:
        rows = _parse_json_multi(await _call(tools, "list_agent_domains", {}))
    except Exception as exc:  # noqa: BLE001 — 분류 조회 실패는 비치명적
        print(f"[experts] list_agent_domains failed: {exc!r}")
        return []
    out = []
    for r in rows:
        r = _first_dict(r)
        d = str(r.get("domain") or "").strip()
        if d:
            out.append({"domain": d, "agents": int(r.get("agent_count") or 0)})
    return sorted(out, key=lambda x: -x["agents"])


async def _seat_axes(llm, tools: dict, message: str, history: list) -> list[dict]:
    """대화에서 **좌석 검색 축**을 뽑는다 — 자유 생성이 아니라 도메인 분류에서의 **선택**이다.

    두 가지를 동시에 푼다.

    ① 긴 질의 문제 — 대화를 통째로 임베딩 질의에 던지면 추천이 **나빠진다.** 긴 텍스트는
       한 벡터로 평균이 되어 변별이 죽는다. 실측(2026-08-07) — 원 질문에 말을 덧붙인
       역질의는 상위 5 중 4가 기존 좌석과 동일했고, 반대로 '봉지 수분 산소 침투 신뢰성'
       같은 **짧은 도메인 질의는 정확히 다른 좌석**(rel-chemical-corrosion)을 돌려줬다.
       그래서 축마다 짧은 질의로 나눠 던진다(deliberation._counter_seats 와 같은 방식).

    ② 체계성 문제 — 축을 LLM 이 자유 연상으로 만들면 커버리지 보장이 없다. 무엇을 빠뜨렸는지
       셀 수조차 없다. 그래서 풀의 **도메인 분류(list_agent_domains, 실시간)** 를 프레임으로
       주고 '해당하는 도메인을 고르라'는 **선택** 문제로 바꾼다. 프레임이 풀 전체를 덮으므로
       빠진 도메인을 셀 수 있고, 없는 도메인을 지어내면 아래 검증에서 버려진다.

    반환: [{domain, phrase}] — phrase 가 실제 검색 질의다. 실패하면 빈 목록(호출부가 종전대로).
    """
    if not history:
        return []
    roster = await _domain_roster(tools)
    if not roster:
        return []
    lines, budget = [], _AXIS_CTX_BUDGET
    for m in history[:60]:
        if not isinstance(m, dict):
            continue
        t = str(m.get("content") or "").strip()
        if not t:
            continue
        who = "사람" if m.get("role") == "user" else "AI"
        line = f"[{who}] {t[:800]}"
        if budget - len(line) < 0:
            break
        lines.append(line)
        budget -= len(line)
    if not lines:
        return []
    roster_txt = ", ".join(f"{r['domain']}({r['agents']}명)" for r in roster)
    try:
        raw = await _llm_text(
            llm,
            "당신은 심의 좌석 편성자입니다. 지시한 형식의 줄만 출력하세요.",
            f"[전문가 풀의 도메인 분류 — 여기 있는 것만 쓸 수 있다]\n{roster_txt}\n\n"
            f"[화두]\n{message[:1000]}\n\n[오간 대화]\n" + "\n".join(lines) +
            f"\n\n위 대화 전체를 읽고, 이 문제를 판단하려면 어느 도메인이 필요한지 위 목록에서 "
            f"고르라. 화두에 직접 쓰인 말만이 아니라 대화에서 드러난 조건·제약·의심까지 반영하라. "
            f"중요한 순서로 최대 {EXPERT_AXES}줄, 각 줄은 다음 형식뿐이다.\n"
            f"도메인코드 | 그 도메인에서 무엇을 볼지 짧은 명사구(8단어 이내)\n"
            f"목록에 없는 코드는 쓰지 마라. 설명·번호·머리말 없이 줄만 출력하라.")
    except Exception as exc:  # noqa: BLE001 — 축 추출 실패는 비치명적. 종전 단일 질의로 간다.
        print(f"[experts] axis extraction failed: {exc!r}")
        return []
    known = {r["domain"] for r in roster}
    axes, seen = [], set()
    for ln in str(raw or "").splitlines():
        ln = re.sub(r"^[\s\-\*\d\.\)·]+", "", ln).strip()
        if "|" not in ln:
            continue
        dom, _, phrase = ln.partition("|")
        dom, phrase = dom.strip().lower(), phrase.strip()[:60]
        # 분류에 없는 도메인은 버린다 — 지어낸 축으로 검색하면 체계성이 도로 무너진다.
        if dom not in known or len(phrase) < 2 or dom in seen:
            continue
        seen.add(dom)
        axes.append({"domain": dom, "phrase": phrase})
        if len(axes) >= EXPERT_AXES:
            break
    return axes


@app.post("/deliberate/experts")
async def deliberate_experts(req: ExpertsRequest) -> dict:
    """심의 전 전문가 선정 미리보기 — 자동 추천(recommend_agents) + 전체 풀(list_agents compact).
    프론트가 추천을 미리 보여주고, 사용자가 확인·수동추가한 personas 로 심의를 실행한다."""
    tools = await _tools_by_name(app, req.groups, CATALOG_RESULT_MAX)  # 결과를 코드가 파싱 — 절단 금지
    if not tools:
        return {"recommended": [], "pool": [], "error": "gateway_unavailable"}

    def _norm(d: dict) -> dict:
        d = _first_dict(d)
        key = d.get("agent_type") or d.get("id") or ""
        return {"key": key, "name": d.get("name") or key,
                "role": (d.get("description") or "")[:280],
                "tags": list(d.get("common_tags") or [])}

    if req.light:
        # 조직도 1단계 — 명부만. recommend_agents(축 질의 포함 최대 5회)·도구 카탈로그·태그를
        # 전부 건너뛴다. 화면이 트리를 그리는 데 필요한 건 키와 이름뿐이다.
        pool_light: list[dict] = []
        try:
            for a in _parse_json_multi(await _call(tools, "list_agents", {"compact": True})):
                n = _norm(a)
                if n["key"]:
                    pool_light.append({"key": n["key"], "name": n["name"], "tags": []})
        except Exception as exc:  # noqa: BLE001
            print(f"[experts] list_agents failed (light): {exc!r}")
        return {"recommended": [], "candidates": [], "pool": pool_light, "light": True,
                "tools": {"recommended": [], "expert_tools": [], "pipeline": [],
                          "all": [], "apps": [], "areas": []},
                "low_confidence": False, "axes": []}

    # 질문 연관 순위 — recommend_agents 를 넉넉히(top_k=CAND) 받아 관련도순 후보로 쓴다.
    # recommended = 상위 N(기본 선택), candidates = 관련 전문가 목록(수동 추가 기본 노출·검색 우선).
    cand_k = int(os.environ.get("EXPERT_CANDIDATE_TOP_K", "40"))

    # 원 응답 보관 — relevant_tools 는 좌석 정규화(_norm)에서 버려지는 필드라 여기 담아 둔다.
    # ⚠ 예전엔 아래에서 `recd` 를 그냥 참조했는데, 그건 _rank 의 **지역변수**라 바깥에서는
    # 이름이 없다. 매 호출 NameError 가 나고 try/except 가 삼켜 expert_tools 가 **항상 비었다**
    # (실측 재현 2026-09-09 — 전문가 선정 화면의 '전문가가 쓰는 도구' 칸이 늘 0개였다).
    raw_by_q: dict[str, dict] = {}

    async def _rank(q: str, top_k: int) -> list[dict]:
        """한 질의의 추천 결과를 정규화해 돌려준다. 실패는 빈 목록(호출부가 계속 진행)."""
        try:
            recd = _parse_json(await _call(tools, "recommend_agents", {"q": q, "top_k": top_k}))
        except Exception as exc:  # noqa: BLE001 — 추천 실패해도 풀로 수동 선택 가능
            print(f"[experts] recommend failed (q={q[:40]!r}): {exc!r}")
            return []
        if isinstance(recd, dict):
            raw_by_q[q] = recd
        items = recd if isinstance(recd, list) else (
            (recd or {}).get("recommendations") or (recd or {}).get("agents") or (recd or {}).get("data") or [])
        out = []
        for it in (items or [])[:top_k]:
            it = _first_dict(it)
            n = _norm(it)
            # 도구 운영자(HE팀)는 추천·후보에 안 올린다 — 풀(조직도)에는 남아 사람이 직접 고를 수 있다.
            if not n["key"] or is_operator(n["key"]):
                continue
            n["score"] = it.get("score")
            n["why"] = it.get("why") or ""
            n["low_confidence"] = bool(it.get("low_confidence"))
            n["desc_match"] = it.get("desc_match")
            out.append(n)
        return out

    # 축 분해 — 대화가 있으면 축마다 짧은 질의를 따로 던진다(긴 질의는 이웃이 그대로 돌아온다).
    # 축 추출이 실패하거나 대화가 없으면 axes 는 빈 목록이고, 아래는 종전 단일 질의와 같아진다.
    axes = await _seat_axes(app.state.llm, tools, req.message, req.history)
    base_task = _rank(req.message, cand_k)
    axis_tasks = [_rank(a["phrase"], 8) for a in axes]
    base_out, *axis_out = await asyncio.gather(base_task, *axis_tasks)

    # 병합 — 화두 질의 결과를 바탕에 깔고, 축별 상위를 **축을 한 명씩 돌아가며** 얹는다.
    # 라운드로빈이라 한 축이 앞자리를 독식하지 않는다(축 다양성이 인원수보다 중요하다).
    candidates: list[dict] = []
    by_key: dict[str, dict] = {}
    for n in base_out:
        if n["key"] not in by_key:
            by_key[n["key"]] = n
            candidates.append(n)
    # 축당 신규 좌석 상한. 라운드로빈만으로는 안 된다 — 화두 질의가 이미 40명을 물어오므로
    # 대부분의 축 추천은 거기 이미 있고(라벨만 붙는다), '정말 못 닿던' 좌석을 내는 축 하나가
    # 깊이를 계속 파며 앞자리를 독식한다(실측: pwr 축이 상위 5 중 3을 먹었다).
    # 축 다양성이 인원수보다 중요하다.
    _AXIS_QUOTA = 2
    picked: list[dict] = []
    quota: dict[str, int] = {}
    for depth in range(8):
        for axis, lst in zip(axes, axis_out):
            if depth >= len(lst):
                continue
            n = lst[depth]
            label = f"{axis['domain']} · {axis['phrase']}"
            prev = by_key.get(n["key"])
            if prev is not None:
                prev.setdefault("axes", []).append(label)  # 이미 있으면 축 출처만 보탠다
                continue
            if quota.get(axis["domain"], 0) >= _AXIS_QUOTA:
                continue
            quota[axis["domain"]] = quota.get(axis["domain"], 0) + 1
            n["axes"] = [label]
            by_key[n["key"]] = n
            picked.append(n)
    # 커버리지 회계 — 고른 도메인 중 실제로 좌석을 얻은 것과 못 얻은 것. 자유 생성 축이었으면
    # 셀 수조차 없던 값이다. 의장 (0) 커버리지 항목이 짐작 대신 이 숫자를 쓸 수 있다.
    for axis, lst in zip(axes, axis_out):
        axis["seats"] = [n["key"] for n in lst[:3]]
    # 축 좌석과 화두 좌석을 **번갈아** 세운다. 축을 앞에 몰아 세웠더니 화두 자체의 좌석이
    # 통째로 밀려났다 — '염수 부식 후 낙하 크랙' 에서 rel-drop-impact 가 상위 5에서 빠졌다
    # (실측: 축 적용 전후 상위 5가 하나도 겹치지 않았다). 축은 화두가 못 닿는 도메인을 보태는
    # 것이지 화두를 대체하는 게 아니다. 축부터 시작해 교차시킨다 — 축이 이번 수정의 값이므로
    # 첫 자리는 주되, 화두 좌석이 함께 남는다.
    merged: list[dict] = []
    seen_m: set[str] = set()
    for a, b in itertools.zip_longest(picked, candidates):
        for n in (a, b):
            if n is not None and n["key"] not in seen_m:
                seen_m.add(n["key"])
                merged.append(n)
    candidates = merged
    recommended = candidates[:N_PERSONAS]

    # 풀에 맞는 전문가가 없을 때 화면이 그렇게 말할 수 있게 올려 보낸다. AIDataHub 가
    # 역할/설명 어휘 일치로 판정한다 — score 로는 못 한다(e5 코사인은 무관해도 0.87~0.90).
    # 실제 사고: 'OCA 의 산소 확산 계수' 질의에 백플레인 TFT·안테나 OTA 가 추천됐는데,
    # 랭킹 버그가 아니라 풀에 그 전문성이 아예 없었다.
    low_conf = bool(recommended and all(r.get("low_confidence") for r in recommended))

    # 전체 풀(compact — 관련도 밖 전문가까지 키워드로 찾을 때). 647+ 규모라 role 은 싣지 않는다.
    pool: list[dict] = []
    try:
        for a in _parse_json_multi(await _call(tools, "list_agents", {"compact": True})):
            n = _norm(a)
            if n["key"]:
                pool.append({"key": n["key"], "name": n["name"], "tags": n["tags"]})
    except Exception as exc:  # noqa: BLE001
        print(f"[experts] list_agents failed: {exc!r}")

    # 도구 — 파이프라인이 자동 쓰는 도구(정보 표시) + 주제 관련 추천 + 전체 카탈로그(검색 추가용).
    # 사용자가 고른 도구(delib_opts.tools)는 심의에서 실제 호출돼 정량 근거로 주입된다.
    _PIPELINE_TOOLS = ("recommend_agents", "get_agent_session", "alert_check", "daily_briefing",
                       "query_voc", "hybrid_search", "search_knowledge", "search_reports",
                       "create_report_draft")
    # 전문가가 쓰는 도구 — AIDH recommend_agents 가 주는 relevant_tools(compatible_agents).
    expert_tools = []
    try:
        # 화두 질의의 응답을 쓴다 — 축 질의는 좁은 명사구라 도구 연관도가 화두보다 낮다.
        _base_recd = raw_by_q.get(req.message) or {}
        for rt in (_base_recd.get("relevant_tools") or []):
            rt = _first_dict(rt)
            nm = rt.get("name")
            if nm:
                gk, gl = _group_of(nm)
                ak, al = _area_of(nm)
                expert_tools.append({
                    "name": nm, "desc": (rt.get("description") or "")[:160],
                    "score": rt.get("score"), "group": gk, "group_label": gl,
                    "area": ak, "area_label": al,
                    "agents": list(rt.get("compatible_agents") or [])[:8],
                })
    except Exception as exc:  # noqa: BLE001 — 연결 정보 없으면 생략
        print(f"[experts] relevant_tools failed: {exc!r}")
    tools_info = {
        "recommended": _rank_tools(tools, req.message),
        "expert_tools": expert_tools,
        "pipeline": [n for n in _PIPELINE_TOOLS if n in tools],
        "all": _tool_catalog(tools),
        "apps": _app_catalog(tools),
        "areas": _area_catalog(tools),
    }
    # axes — 대화에서 뽑은 도메인 축. 화면이 "왜 이 좌석인지"를 보여주는 근거다.
    # 빈 배열이면 화두 한 줄로만 추천했다는 뜻이고, 화면도 그렇게 말해야 한다.
    return {"recommended": recommended, "candidates": candidates, "pool": pool,
            "tools": tools_info, "low_confidence": low_conf, "axes": axes}


# ── 심의 전 'VOC 먼저 보기' ────────────────────────────────────────────────────────
# 심의 엔진의 불량 환기(_defect_briefing)는 (1) 질문에 불량 단어가 있을 때만 켜지고 (2) 화두로
# 검색하지 않고 경보 제품의 최신 부정 VOC 를 가져오며 (3) 넣을지를 LLM 이 통째로 정한다 — 사람이
# 보고 고를 틈이 없다. 여기서는 화두로 **검색**해 목록을 주고 고르는 것은 사람이 한다. 고른 것은
# delib_opts.evidence(원천 근거 — 결론 아님)로, 보강 문장은 human_note 로 간다(포털 프론트).
class VocPreviewRequest(BaseModel):
    message: str
    groups: list[str] = []
    # 사람이 고친 검색어 — 주면 추출을 건너뛴다. 보강의 첫 단계는 검색어를 바로잡는 일이다.
    keywords: list[str] = []


_VOC_PREVIEW_MAX = int(os.environ.get("VOC_PREVIEW_MAX", "24"))


async def _voc_keywords(llm, message: str) -> list[str]:
    """search_voc 는 FTS 이고 '영어 권장'이다 — VOC 본문이 대부분 영어(번역본 포함)라 한국어
    화두를 그대로 넣으면 안 걸린다(실측 — '솔더 크랙' 은 빈 응답). 화두에서 소비자가 쓸 법한
    영어 증상·부품 낱말을 뽑는다. LLM 이 실패하면 화두 속 영문 토큰으로 대신한다."""
    try:
        raw = await _llm_text(
            llm, "당신은 검색어 추출기입니다. 반드시 유효한 JSON 하나만 출력하세요.",
            f"[화두]\n{message[:1500]}\n\n위 화두와 관련된 **고객 불만**을 글로벌 사용자 커뮤니티(VOC)에서 찾을 "
            "영어 검색어를 2~4개 뽑아라. 엔지니어 용어가 아니라 **소비자가 쓰는 말**로, 부품·증상 위주로 "
            "짧게(1~3 단어). 예: hinge crack, battery swelling, overheating, screen flicker. "
            'JSON {"keywords": ["...", "..."]} 로만.')
        kws = (_parse_json(raw) or {}).get("keywords") or []
        out = [str(k).strip()[:40] for k in kws if isinstance(k, str) and str(k).strip()]
        if out:
            return out[:4]
    except Exception as exc:  # noqa: BLE001 — 추출 실패는 폴백으로
        print(f"[voc] keyword extraction failed: {exc!r}")
    return [w for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", message)][:4]


def _voc_row(v: dict, kw: str) -> dict:
    """SignalForge VOC 한 건 → 화면·근거용 정규형(필드명은 SF 실측 스키마)."""
    text = str(v.get("content_translated") or v.get("content_original") or "").strip()
    return {
        "id": v.get("id"), "keyword": kw,
        "product": str(v.get("product_name") or v.get("product_code") or "")[:60],
        "platform": str(v.get("platform_name") or "")[:40],
        "country": str(v.get("country_code") or "")[:8],
        "date": str(v.get("published_at") or "")[:10],
        "sentiment": str(v.get("sentiment_label") or "")[:16],
        "score": v.get("sentiment_score"),
        "text": text[:600],
        "url": str(v.get("source_url") or "")[:300],
    }


@app.post("/deliberate/voc-preview")
async def deliberate_voc_preview(req: VocPreviewRequest) -> dict:
    """심의 전 VOC 미리보기 — 화두로 SignalForge 를 검색해 사람이 고를 목록을 준다.

    ⚠ 세 가지를 뭉개지 않는다(무음 결함의 공통 패턴 — '빈 결과'와 '못 물어봄'이 같아 보인다).
      · unavailable — 이 사용자에게 search_voc 가 없다(권한·미연결)
      · degraded    — 물어봤는데 도구가 실패했다
      · items=[]    — 물어봤고 정말 없다"""
    tools = await _tools_by_name(app, req.groups, CATALOG_RESULT_MAX)
    if "search_voc" not in tools:
        return {"items": [], "keywords": [], "unavailable": True,
                "note": "SignalForge VOC 도구를 쓸 수 없습니다(권한 또는 연결)."}
    kws = [k.strip()[:40] for k in req.keywords if k and k.strip()][:6] or \
        await _voc_keywords(app.state.llm, req.message)
    if not kws:
        return {"items": [], "keywords": [], "note": "검색어를 뽑지 못했습니다 — 영어 검색어를 직접 넣어 주세요."}
    per_kw: list[list[dict]] = []
    failed = 0
    for kw in kws:
        raw = await _call(tools, "search_voc", {"keyword": kw, "limit": 10})
        if isinstance(raw, str) and raw.strip() and not _tool_text_ok(raw):
            failed += 1
            per_kw.append([])
            continue
        # 목록 도구 — 원소별 블록이 이어져 온다(_parse_json 은 마지막 하나만 읽는다).
        per_kw.append([_voc_row(v, kw) for v in _parse_json_multi(raw) if isinstance(v, dict)])
    # 검색어마다 번갈아 뽑는다 — 한 검색어가 목록을 독점하면 나머지 증상이 안 보인다.
    items, seen = [], set()
    for i in range(max((len(x) for x in per_kw), default=0)):
        for rows in per_kw:
            if i < len(rows) and rows[i]["id"] not in seen and rows[i]["text"]:
                seen.add(rows[i]["id"])
                items.append(rows[i])
    total = len(items)
    items = items[:_VOC_PREVIEW_MAX]
    return {"keywords": kws, "items": items, "total": total,
            "degraded": failed == len(kws),
            "partial": 0 < failed < len(kws),
            "tools_used": ["search_voc"],
            **({"note": f"{total}건 중 {len(items)}건만 보입니다 — 검색어를 좁혀 보세요."}
               if total > len(items) else {})}


# ── 심의 전 되묻기 — 메커니즘 분석에 필요한 최소 정보가 비었으면 몇 가지를 묻는다 ──────────
# 화두 한 줄로 심의를 돌리면 좌석이 조건·범위를 **지어내며** 논의한다(낙하 높이도 온도 범위도
# 모르는 채 파손 원인을 좁힌다). 빈 칸을 한 번에 모아 묻고(AIDataHub _scan_missing 선례 —
# 여러 번 왕복하지 않는다), 답은 포털이 화두에 '[보강 정보]' 로 붙인다. 그러면 좌석 추천도
# 그 정보를 보고 다시 뽑는다. 칸의 키는 여기서 고정한다 — LLM 이 칸을 지어내지 못하게.
# (키, 이름, 설명 — 빈 칸이면 이것이 질문이 된다, 흔한 답 후보 — LLM 이 후보를 안 줬을 때)
_CLARIFY_SLOTS: dict[str, list[tuple[str, str, str, list[str]]]] = {
    "mechanism": [
        ("target", "대상", "무엇에서 일어났나요 — 제품·부품·계면", []),
        ("symptom", "현상", "어떻게 됐나요 — 관측된 그대로", ["크랙·파단", "변형·휨", "박리", "특성 저하"]),
        ("condition", "조건", "어떤 하중·환경에서 났나요", ["낙하·충격", "온도 사이클", "고온고습", "반복 굽힘"]),
        ("scope", "범위", "얼마나·언제부터·어디서만 났나요 — 발생률, 특정 로트·모델만인지",
         ["특정 로트만", "특정 모델만", "전 모델", "장기 사용 후"]),
        ("known", "이미 확인한 것", "해 본 시험·분석이나 의심하는 원인이 있나요",
         ["파단면·단면 분석", "재현 시험", "해석", "아직 없음"]),
    ],
    "option-select": [
        ("decision", "결정할 것", "무엇을 고르는 결정인가요", []),
        ("options", "후보안", "비교할 안이 무엇인가요(2개 이상)", []),
        ("criteria", "판단 기준", "무엇으로 우열을 가리나요", ["비용", "신뢰성", "일정", "양산성"]),
        ("constraints", "제약", "넘으면 안 되는 선이 있나요 — 규격·일정·양산성", []),
    ],
    "credibility": [
        ("subject", "판정 대상", "믿을지 따질 해석·모델·데이터가 무엇인가요", []),
        ("use", "결정 용도", "그 결과로 무엇을 결정하려 하나요", ["설계 확정", "시험 대체", "원인 판정"]),
        ("reference", "비교할 실측", "대조할 시험·현장 데이터가 있나요", ["시험 데이터 있음", "현장 데이터 있음", "없음"]),
    ],
    "test-plan": [
        ("target", "대상 재료·부품", "무엇의 물성·성능을 확보하나요", []),
        ("purpose", "용도", "그 값을 어떤 해석·판단에 쓰나요", ["낙하 해석", "열 해석", "피로 수명", "휨 예측"]),
        ("range", "조건 범위", "온도·변형률 속도·습도 등 필요한 범위가 있나요", []),
        ("have", "이미 있는 데이터", "보유 시험값·문헌값이 있나요", ["사내 시험값", "문헌값", "없음"]),
    ],
}
_JOB_SLOTSET = {"diagnosis": "mechanism", "sim-plan": "mechanism", "build-plan": "mechanism",
                "default": "mechanism", "option-select": "option-select",
                "credibility": "credibility", "test-plan": "test-plan"}
_CLARIFY_MAX_ASK = 4
# 자유 심의는 사람이 방법을 고르지 않았으니, 물리 현상·불량 화두일 때만 칸을 묻는다. 이 판정을 LLM 에
# 맡겼더니 dev 7B 가 '사내 교육 제도 개선' 에도 applicable=true 를 내고 낙하·온도 칸을 물었다(실측).
_PHENOMENON_RE = re.compile(
    r"휨|변형|발열|과열|누설|박리|부식|열화|피로|균열|마모|진동|소음|깨짐|들뜸|수명|강도"
    r"|warpage|delamination|corrosion|fatigue|overheat|leak|vibration", re.IGNORECASE)


def _clarify_applicable(job: str, text: str) -> bool:
    if job != "default":                  # 사람이 방법을 골랐다 — 그 방법의 최소 정보는 늘 필요하다
        return True
    return bool(_has_defect_topic(text) or _PHENOMENON_RE.search(text))


def _bigrams(t: str) -> set:
    t = re.sub(r"[^0-9A-Za-z가-힣%°.+-]", "", str(t or "").lower())
    return {t[i:i + 2] for i in range(len(t) - 1)} or ({t} if t else set())


def _grounded(value: str, source: str, need: float = 0.6) -> bool:
    """칸 값이 화두·대화에 **실제로 적혀 있나** — 글자 쌍 겹침으로 본다(조사·띄어쓰기에 안 흔들림).

    ⚠ 왜 필요한가. 'present' 를 LLM 이 판정하게 두면 빈약한 화두에서도 전부 '있음'이라 하고 값을
    **칸 설명에서 베껴 온다.** 실측(2026-09-11, dev qwen2.5-7b) — '폴드 힌지 파손 원인' 에
    '조건: 낙하 높이 · 범위: 특정 로트 · 이미 확인한 것: 해 본 시험' 을 채웠다. 셋 다 화두에 없는
    말이고 전부 설명문 예시였다. 되묻기가 가장 필요한 화두에서 아무것도 안 묻게 되는 결함이다."""
    vb = _bigrams(value)
    if not vb:
        return False
    sb = _bigrams(source)
    return len(vb & sb) / len(vb) >= need


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _ko_text(t) -> str:
    """한국어 화면에 쓸 문구만 — 한자가 섞이면 버린다. dev qwen2.5-7b 가 질문·후보를 중국어로
    쓰는 일이 실측됐다('应力', '这种故障是普遍发生的…'). 그러면 칸 설명·기본 후보로 대신한다."""
    t = str(t or "").strip()
    return "" if _HAN_RE.search(t) else t


def _slot_objects(raw: str, keys: list[str]) -> dict:
    """칸 키별로 객체를 따로 뽑는다 — 출력 전체가 JSON 으로 안 읽혀도 읽히는 칸은 살린다.

    실측(2026-09-11, dev qwen2.5-7b): JSON 을 쓰다가 한 칸의 문자열 **안에서** 날것 줄바꿈과
    중국어 반복 잡문으로 무너졌다(2,078자). 바깥 객체가 안 읽히니 _parse_json 은 마지막으로
    완결된 안쪽 칸 하나를 돌려주고, 되묻기가 통째로 'unparsed' 가 됐다. 칸 키는 우리가 정한
    것이라 키 위치에서 따로 읽을 수 있다. strict=False 는 문자열 안 날것 제어문자를 허용한다."""
    s = str(raw or "")
    dec = json.JSONDecoder(strict=False)
    out: dict = {}
    for k in keys:
        m = re.search(r'"%s"\s*:\s*\{' % re.escape(k), s)
        if not m:
            continue
        try:
            obj, _ = dec.raw_decode(s, m.end() - 1)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out[k] = obj
    return out


class ClarifyRequest(BaseModel):
    message: str
    job: str = "default"
    history: list[dict] = []


@app.post("/deliberate/clarify")
async def deliberate_clarify(req: ClarifyRequest) -> dict:
    """빈 칸 스캔 — {applicable, slots:[{key,label,present,value,question,options}], ask:[key]}.

    ⚠ 되묻기는 **절대 심의를 막지 않는다.** LLM 이 실패하거나 응답이 이상하면 ask=[] 로 돌려
    묻지 않고 진행한다(묻지 못한 것을 '다 있다'로 꾸미지는 않는다 — error 로 알린다).
    칸이 응답에서 빠지면 그 칸은 묻지 않는다(괴롭히지 않는 쪽으로 틀린다)."""
    slots = _CLARIFY_SLOTS[_JOB_SLOTSET.get(req.job, "mechanism")]
    hist: list[str] = []
    budget = 3000
    for m in req.history[-20:]:
        t = str((m or {}).get("content") or "").strip()
        if t and budget > 0:
            hist.append(f"{m.get('role', 'user')}: {t[:budget]}")
            budget -= len(t)
    if not _clarify_applicable(req.job, req.message + "\n" + "\n".join(hist)):
        return {"applicable": False, "slots": [], "ask": []}   # LLM 을 부르지도 않는다
    spec = "\n".join(f"- {k}: {label} — {hint}" for k, label, hint, _o in slots)
    try:
        raw = await _llm_text(
            app.state.llm, "당신은 심의 준비 보조자입니다. 반드시 유효한 JSON 하나만 출력하세요.",
            f"[화두]\n{req.message[:3000]}\n"
            + (f"\n[앞선 대화]\n" + "\n".join(hist) + "\n" if hist else "")
            + f"\n아래는 이 심의에 필요한 최소 정보 칸이다.\n{spec}\n\n"
            "화두·대화에 **이미 적힌 것만** present=true 로 하고 value 에 짧게 옮겨라. 추측으로 채우지 마라. "
            "비어 있으면 present=false, question 에 질문자에게 물을 한 문장(존댓말), 흔한 답 후보가 있으면 "
            "options 에 짧은 낱말 2~4개(없으면 []). "
            'JSON {"slots": {"<칸키>": {"present": bool, "value": "", "question": "", "options": []}}} 로만.')
        v = _parse_json(raw) or {}
    except Exception as exc:  # noqa: BLE001 — 되묻기 실패가 심의를 막으면 안 된다
        print(f"[clarify] failed: {exc!r}")
        return {"applicable": False, "slots": [], "ask": [], "error": "clarify_failed"}
    got = v.get("slots") if isinstance(v.get("slots"), dict) else {}
    if not got:
        got = _slot_objects(raw, [k for k, *_ in slots])
    if not got:                            # 알아듣지 못했다 — 묻지 않되 '다 있다'로 꾸미지 않는다
        _r = str(raw)
        print(f"[clarify] unparsed LLM output (len={len(_r)}): {_r[:160]!r} … {_r[-240:]!r}")
        return {"applicable": False, "slots": [], "ask": [], "error": "clarify_unparsed"}
    source = req.message + "\n" + "\n".join(hist)
    out, ask = [], []
    for key, label, hint, dflt in slots:
        g = got.get(key)
        if not isinstance(g, dict):
            # 응답에서 빠진 칸 — 모델이 무너졌거나 빼먹었다. '있음'으로 칠 근거가 없으니 묻는다.
            # (종전엔 묻지 않았는데, 그러면 모델이 무너진 칸이 바로 가장 필요한 질문이었다 — 실측.)
            g = {}
        # '있음' 은 값이 원문에 실재할 때만 — LLM 이 설명문을 베껴 채운 칸을 걸러낸다(_grounded).
        present = bool(g.get("present")) and _grounded(str(g.get("value") or ""), source)
        opts = [_ko_text(o)[:30] for o in (g.get("options") or []) if isinstance(o, str) and _ko_text(o)][:4] \
            or list(dflt)
        row = {"key": key, "label": label, "hint": hint, "present": present,
               "value": str(g.get("value") or "").strip()[:200] if present else "",
               # 근거 검사로 되돌린 칸은 LLM 이 질문을 안 달았다(있다고 여겼으니) — 칸 설명을 질문으로.
               "question": (_ko_text(g.get("question"))[:200] if not g.get("present") else "") or hint,
               "options": opts}
        out.append(row)
        if not present and len(ask) < _CLARIFY_MAX_ASK:
            ask.append(key)
    return {"applicable": True, "slots": out, "ask": ask}


class AgentDetailRequest(BaseModel):
    key: str
    groups: list[str] = []


# AIDataHub 가 역할 원문 뒤에 붙이는 공용 도구 안내(build_system_prompt 의 '## How to access this hub'
# 블록) — 모든 전문가에게 같은 영문 안내라 사람이 읽을 역할이 아니다. 역할 원문이 없는 에이전트는
# 'You are an assistant for "…"' 자동 틀을 받는데, 그것도 역할 문서가 아니다.
_HUB_GUIDE_SEP = "\n\n---\n\n## How to access this hub"


def _role_doc(system_prompt: str) -> str:
    sp = system_prompt or ""
    if sp.startswith('You are an assistant for "'):
        return ""
    i = sp.find(_HUB_GUIDE_SEP)
    return (sp[:i] if i >= 0 else sp).strip()


@app.post("/catalog/agent")
async def catalog_agent(req: AgentDetailRequest) -> dict:
    """전문가 상세 + 보유 지식(레코드 목록) — 브라우즈 UI 용(결정적·LLM 미경유).
    LLM 텍스트 나열은 결과 절단 캡에 걸려 잘리므로, 탐색은 이 데이터로 UI 가 그린다.
    records 는 미리보기 20건이고 records_total 이 전체 수다 — 전체는 /catalog/agent/records 로 넘긴다."""
    tools = await _tools_by_name(app, req.groups, CATALOG_RESULT_MAX)  # 상세·레코드를 JSON 으로 읽는다
    if not tools:
        return {"error": "gateway_unavailable"}
    key = req.key.strip()[:120]
    out: dict = {"key": key, "name": key, "role": "", "prompt": "", "tags": [], "samples": [], "records": [],
                 "records_total": None, "operator": False, "apps": []}
    try:
        sess = _first_dict(_parse_json(await _call(tools, "get_agent_session", {"agent_type": key})))
        sd = _first_dict(sess.get("data", sess))
        out["name"] = sd.get("name") or key
        out["role"] = str(sd.get("description") or "")[:2000]
        # 역할 문서 전문 — 심층 보기가 '이 전문가가 어떻게 생각하고 답하는가'를 그대로 보여 준다.
        out["prompt"] = _role_doc(str(sd.get("system_prompt") or ""))[:PERSONA_ROLE_MAX * 2]
        # get_agent_session 은 태그를 scope.common_tags 에 둔다(AIDataHub mcp_runtime). 최상위만 보면
        # 늘 빈 목록이었고, 화면은 빈 태그 줄을 숨기므로 아무도 몰랐다.
        out["tags"] = list(sd.get("common_tags") or (sd.get("scope") or {}).get("common_tags") or [])[:30]
        out["samples"] = [str(s)[:200] for s in (sd.get("sample_queries") or [])][:5]
        # HE팀 운영자 — 보유 지식이 0건인 게 정상이라, 무엇으로 답하는지(앱·도구 수)를 함께 준다.
        rc = sd.get("response_config") if isinstance(sd.get("response_config"), dict) else {}
        if rc.get("persona_kind") == "mcp_operator":
            _tm = _tools_map()
            out["operator"] = True
            out["apps"] = [{"key": a, "label": _app_label(a), "tool_count": sum(1 for g in _tm.values() if g == a),
                            "connected": (a in set(_tm.values())) if _tm else None}
                           for a in (rc.get("mcp_apps") or []) if isinstance(a, str) and a.strip()][:3]
    except Exception as exc:  # noqa: BLE001 — 상세 실패해도 지식 목록은 시도
        print(f"[catalog] agent session failed: {exc!r}")
    try:
        raw = _parse_json(await _call(tools, "list_records", {"agents": [key], "limit": 20}))
        recs = raw if isinstance(raw, list) else (
            (raw or {}).get("records") or (raw or {}).get("items") or (raw or {}).get("data") or [])
        for r in recs[:20]:
            r = _first_dict(r)
            rid = r.get("id") or r.get("record_id") or ""
            title = r.get("title") or ""
            if rid or title:
                out["records"].append({"id": str(rid)[:80], "title": str(title)[:160],
                                       "data_type": str(r.get("data_type") or "")[:20]})
        # 상세칸의 '보유 지식 20건'은 목록 상한이었다 — 실제로는 2,729건인 사람도 20건으로 보였다.
        if isinstance(raw, dict) and isinstance(raw.get("total"), int):
            out["records_total"] = raw["total"]
    except Exception as exc:  # noqa: BLE001
        print(f"[catalog] records failed: {exc!r}")
    return out


class AgentRecordsRequest(BaseModel):
    key: str
    q: str = ""
    offset: int = 0
    limit: int = 50
    groups: list[str] = []


# 심층 보기 지식카드 한 쪽 상한 — 한 번에 수천 건(material-twin-analyst 2,729)을 받으면 화면도
# 무겁고 게이트웨이 응답도 크다. 쪽으로 넘긴다.
CATALOG_PAGE_MAX = 100


def _record_row(r) -> dict:
    r = _first_dict(r)
    return {"id": str(r.get("id") or r.get("record_id") or "")[:80],
            "title": str(r.get("title") or "")[:300],
            "data_type": str(r.get("data_type") or "")[:20],
            "doc_type": str(r.get("doc_type") or "")[:60],
            "year": r.get("year") if isinstance(r.get("year"), int) else None,
            "tags": [str(t)[:60] for t in (r.get("tags") or [])][:12],
            "summary": str(r.get("summary") or "")[:400]}


@app.post("/catalog/agent/records")
async def catalog_agent_records(req: AgentRecordsRequest) -> dict:
    """전문가 한 명의 지식카드 목록 — 제목·요약 검색(q)·쪽(offset·limit)·총수. 심층 보기 용(LLM 미경유)."""
    tools = await _tools_by_name(app, req.groups, CATALOG_RESULT_MAX)
    offset = max(0, int(req.offset or 0))
    if not tools or "list_records" not in tools:
        return {"error": "gateway_unavailable", "total": 0, "offset": offset, "items": []}
    limit = max(1, min(int(req.limit or 50), CATALOG_PAGE_MAX))
    args: dict = {"agents": [req.key.strip()[:120]], "limit": limit, "offset": offset}
    if req.q.strip():
        args["q"] = req.q.strip()[:200]
    raw = _parse_json(await _call(tools, "list_records", args))
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        # '0건'과 '못 물어봤다'를 뭉개지 않는다 — 실패를 빈 목록으로 주면 '지식이 없는 전문가'로 읽힌다.
        return {"error": "records_failed", "total": 0, "offset": offset, "items": []}
    items = [row for row in (_record_row(r) for r in raw["items"]) if row["id"] or row["title"]]
    total = raw.get("total") if isinstance(raw.get("total"), int) else offset + len(items)
    return {"total": total, "offset": offset, "limit": limit, "items": items}


class RecordRequest(BaseModel):
    id: str
    groups: list[str] = []


# 카드 한 장 본문 상한(문자)·표 행 상한. 표 레코드는 행이 수천 개일 수 있어 앞쪽만 싣고
# truncated 로 잘렸다고 알린다 — 잘린 걸 전부인 척 보이면 없는 결론을 읽게 된다.
RECORD_TEXT_MAX = 60000
RECORD_ROWS_MAX = 200


def _cell(v):
    """표 칸 — 숫자·불리언·빈 값은 그대로, 나머지는 200자 문자열로."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))[:200]


def _record_view(rec: dict) -> dict:
    """get_record 원문 → 읽기 화면. 본문 모양이 셋이다 — DOC(sections·sources)·DATA(headers·rows)·
    그 밖(SIM 의 tool_call 등). 모르는 모양은 버리지 않고 JSON 원문으로 보인다(없다고 하면 거짓이다)."""
    content = rec.get("content") if isinstance(rec.get("content"), dict) else {}
    out = {**_record_row(rec), "agents": [str(a)[:120] for a in (rec.get("agents") or [])][:20],
           "sections": [], "table": None, "sources": [], "truncated": False}
    out["summary"] = str(rec.get("summary") or "")[:2000]
    budget = RECORD_TEXT_MAX
    for s in content.get("sections") or []:
        s = _first_dict(s)
        text = str(s.get("content_text") or s.get("text") or "")
        if budget <= 0:
            out["truncated"] = True
            break
        if len(text) > budget:
            text, out["truncated"] = text[:budget], True
        budget -= len(text)
        out["sections"].append({"id": str(s.get("section_id") or "")[:40], "title": str(s.get("title") or "")[:200],
                                "level": s.get("level") if isinstance(s.get("level"), int) else 1, "text": text})
    headers, rows = content.get("headers"), content.get("rows")
    if isinstance(headers, list) and isinstance(rows, list):
        out["table"] = {"caption": str(content.get("caption") or "")[:200],
                        "headers": [str(h)[:80] for h in headers][:40],
                        "rows": [[_cell(c) for c in row[:40]] for row in rows[:RECORD_ROWS_MAX] if isinstance(row, list)],
                        "total_rows": len(rows),
                        "notes": str(content.get("notes") or "")[:2000]}
        if len(rows) > RECORD_ROWS_MAX:
            out["truncated"] = True
    for src in content.get("sources") or []:
        src = _first_dict(src)
        if src.get("title") or src.get("doi") or src.get("url"):
            out["sources"].append({k: str(src.get(k) or "")[:300] for k in ("title", "doi", "url", "kind", "authors")}
                                  | {"year": src.get("year") if isinstance(src.get("year"), int) else None})
    if not out["sections"] and out["table"] is None:
        rest = {k: v for k, v in content.items() if k != "sources"}
        if rest:
            text = json.dumps(rest, ensure_ascii=False, indent=1, default=str)
            out["truncated"] = out["truncated"] or len(text) > RECORD_TEXT_MAX
            out["sections"].append({"id": "", "title": "원문(JSON)", "level": 1, "text": text[:RECORD_TEXT_MAX]})
    return out


@app.post("/catalog/record")
async def catalog_record(req: RecordRequest) -> dict:
    """지식카드 한 장 — 제목·요약·태그·본문 섹션·표·출처. 심층 보기의 읽기 칸 용(LLM 미경유)."""
    tools = await _tools_by_name(app, req.groups, CATALOG_RESULT_MAX)
    rid = req.id.strip()[:120]
    if not tools or "get_record" not in tools:
        return {"error": "gateway_unavailable", "id": rid}
    rec = _first_dict(_parse_json(await _call(tools, "get_record", {"record_id": rid})))
    if not rec.get("id"):
        return {"error": "record_failed", "id": rid}
    return _record_view(rec)


@app.get("/artifacts/{name}")
def get_artifact(name: str) -> FileResponse:
    """도구 산출 이미지 서빙 — _stash_artifacts 가 저장한 파일만(이름은 해시라 열거 불가)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name) or ".." in name:
        raise HTTPException(status_code=404)
    p = os.path.join(ARTIFACT_DIR, name)
    if not os.path.isfile(p):
        raise HTTPException(status_code=404)
    return FileResponse(p)


def _search_capability() -> dict:
    """웹 리서치로 지금 무엇이 가능한가. 프론트가 토글을 비활성으로 그릴 근거다.

    전역이 꺼져 있는데 UI 에서 켤 수 있으면 사용자는 켰다고 믿고 결과를 기다린다 —
    아무것도 안 나가는데 '검색이 잘 안 되는 도구'로만 보인다. 그게 이 기능의 가장
    현실적인 실패 모드다."""
    tm = _tools_map()
    have = {"scholar": "search_scholar" in tm, "web": "search_web" in tm}
    return {"sources": {k: {"available": v} for k, v in have.items()},
            "note": "available=false 면 서버가 그 소스를 제공하지 않습니다(도구 미등록 또는 전역 차단)."}


@app.get("/search-capability")
def search_capability() -> dict:
    return _search_capability()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": VLLM_MODEL,
        "vllm": VLLM_BASE_URL,
        "mcp": list(getattr(app.state, "connections", {})),
        "tool_scoping": "gateway (X-HWAX-Groups)",
        "tool_desc_max": TOOL_DESC_MAX,
        "tool_max": TOOL_MAX,   # 0=무제한. >0 이고 게이트웨이 도구수보다 작으면 챗에 일부 도구 미바인딩
        # 예산은 이제 모델 컨텍스트에서 유도한다 — 원격 진단에서 실제 값이 보여야 한다.
        "context_tokens": _model_context_tokens(),
        "hist_budget": _hist_budget_chars(),                  # 문서 없이 대화만 할 때
        "hist_budget_with_docs": _hist_budget_chars(has_docs=True),
        "doc_budget": _doc_total_chars(),
        # 최근 도구 로드 실패 사유(그룹셋별) — '도구가 없다'는 증상의 원인을 원격에서 본다.
        # 비어 있으면 최근 로드가 전부 성공. 값이 있으면 401/타임아웃 등 실사유가 찍힌다.
        "tool_load_errors": {" ".join(sorted(g)) or "(none)": v
                             for g, v in (getattr(app.state, "tool_load_error", None) or {}).items()},
    }

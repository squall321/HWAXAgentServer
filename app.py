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
import copy
import json
import logging   # _detach_stream 의 오류 로깅이 쓰는데 임포트가 없었다 — 심의가 터지면 오류 처리 자체가 NameError 로 죽었다
import os
import re
import secrets
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI

from evidence import (fit_document, sig_numbers as _sig_numbers,
                      unsourced_numbers as _unsourced_numbers)
from deliberation import (
    N_PERSONAS,
    _EVID_ITEM_MAX,
    _cut_mark,
    _CUT_MARK_ROOM,
    _fit_recent,
    _narrow_cap,
    _wide,
    _PHANTOM_ID_MARK,
    _AUTH_REJECT_MARK,
    envelope_failed,
    _call,
    _agent_search_hits,
    _evid_budget,
    _tool_schema_brief,
    _seat_tool_prefer,
    knowledge_line,
    _env_float,
    _env_int,
    _first_dict,
    _parse_json,
    _parse_json_multi,
    _tool_text_ok,
    _has_defect_topic,
    _call,
    _llm_text,
    _llm_limit,
    _quiet_calls,
    _resolve_opts,
    _started_by,
    _tools_by_name,
    _wants_external,
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
import call_trace as _call_trace
import external_llm as _external_llm
import vision as _vision
import sc_gate as _sc_gate   # 자기검증 관문 — 바꿀 곳을 이번 턴 도구 결과 원문과 다시 대조한다(HWAXPortal docs/request-1030 HA-27)
import deliberation as _delib_engine   # DELIB_IMAGES 는 모듈로 읽는다 — 이름으로 가져오면 엔진을 다시 읽은 뒤(시험의 reload) 다른 문맥 변수를 쥔다
import review_gate as _review_gate
import status_wait as _status_wait_mod   # 챗 턴의 잡 상태 조회를 코드가 기다린다(HWAXPortal docs/request-1030 HA-28)
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "qwen2.5-7b-dev")
# 인증 있는 OpenAI 호환 서버(상암 B300 등)용 — 미설정이면 "EMPTY"(로컬 vLLM 무인증과 동일).
VLLM_API_KEY = os.environ.get("VLLM_API_KEY") or "EMPTY"
MCP_SERVERS = os.environ.get("MCP_SERVERS", "")
# 에이전트 캐시 상한. 사용자 × 30분 창 × 질의선택 조합이라 상한이 없으면 계속 는다.
AGENT_CACHE_MAX = int(os.environ.get("AGENT_CACHE_MAX", "64"))
# 에이전트 캐시의 수명(초, 0 = 기한 없음). 묶은 도구 목록은 지은 때의 게이트웨이 카탈로그다 — 사용자 PAT 키는 포털이 30분 창마다
# 새 토큰을 줘 저절로 갈리지만, 서비스 계정 키(PAT 없음 · 강등 자리)는 기한 없이 옛 목록을 썼다. 새 도구는 그동안도 search_tools ·
# invoke_tool 로 닿는다 — 수명은 그 창과 같은 30분이다(HWAXPortal docs/request-1027 HA-21).
AGENT_CACHE_TTL_S = int(os.environ.get("AGENT_CACHE_TTL_S", "1800"))
MCP_CONFIG = os.environ.get("MCP_CONFIG", "")
# 도구 호출 1건을 엔진이 기다리는 기한(초), 0=없음. 종전엔 없었다 — 게이트웨이가 ping 만 보내며 멈추면
# (전송 한도 30초/300초는 바이트 사이 침묵만 재므로 ping 이 되감는다) 좌석 자유 조회·RA 저장·VOC·역할 복원이
# 끝없이 기다렸고, 그 대기가 심의 잡 자리를 붙들었다. MCP 세션의 요청 기한으로 건다(_with_groups).
# ⚠ 게이트웨이의 GATEWAY_CALL_TIMEOUT(600초 — 전송 read·단발 세션 바깥 기한 660초)보다 **커야 한다.**
#   안쪽이 먼저 걸려야 어느 백엔드가 느린지 구체적인 문구가 나오고, 이 값은 게이트웨이 자체가 멈췄을 때의
#   마지막 그물이다. 게이트웨이 한도를 올리면 이 값도 같은 폭으로 올린다.
MCP_CALL_TIMEOUT_S = _env_float("MCP_CALL_TIMEOUT_S", 900.0)
from urllib.parse import quote  # 그룹 헤더 안전 인코딩

GROUPS_HEADER = "X-HWAX-Groups"  # gateway reads this to filter tools by the caller's groups
USER_HEADER = "X-HWAX-User"      # 호출자 이메일 — 게이트웨이가 사용자별 백엔드 자격증명으로 갈아탄다
# 확인 관문(HWAXPortal docs/request-1030 HA-29) — 사람이 이번 말로 시키지 않은 잡 · 쓰기는 코드가 확인 카드로 멈춘다(아래 _confirm_gate). 기동 때
# 한 번 읽는다(CHAT_STABLE_PREFIX 와 같은 꼴). 0 이면 관문이 아무것도 막지 않고 시스템 프롬프트 넷째 규칙도 종전 글이다 — 박스에서 관문이
# 오작동하면 코드 되돌림 없이 끄는 길이다.
CHAT_CONFIRM_GATE = os.environ.get("CHAT_CONFIRM_GATE", "1").strip() != "0"
# 넷째 규칙 둘 — 관문이 켜져 있으면 확인을 글로 따로 묻지 않게 한다(모델이 글로 묻고 사람이 '응' 한 뒤 다시 카드가 뜨는 이중 확인을 막는다).
_SP_CONFIRM_OLD = "등록·발행·삭제·잡 제출처럼 되돌리기 어려운 도구는 무엇을 어디에 하는지 사용자에게 먼저 확인하고 호출하세요. "
_SP_CONFIRM_GATE = ("잡 시작·등록·발행·삭제처럼 상태를 바꾸는 도구는 사용자가 이번 말로 그 일을 시킨 것이 아니면 서버가 보내지 않고 사용자에게 "
                    "확인 카드를 띄웁니다 — 확인을 글로 따로 묻지 말고, 무엇을 왜 하려는지 한 줄 말한 뒤 호출하세요. 확인 번호를 지어내거나 "
                    "카드를 흉내 내지 마세요. ")
_SP_HEAD = (
    # 질문의 언어로 답한다 — 영어 질문에도 한국어로만 답했다(온톨로지 시험 OT-07 — HWAXPortal docs/request-1027 HA-15). 중국어로 새는 것
    # (Qwen · GLM 의 버릇)은 계속 막는다.
    "당신은 HWAX 포털의 어시스턴트입니다. 사용자 질문의 언어로 답하세요 — 기본은 한국어이고, "
    "사용자가 중국어로 묻지 않는 한 중국어로 절대 전환하지 마세요. 간결·정확하게. "
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
    # ④ 웹 챗에서도 등록·발행·삭제·잡 제출 도구가 그대로 바인딩된다. 관문(HA-29)이 켜져 있으면 코드가 확인 카드로 멈춘다.
    + (_SP_CONFIRM_GATE if CHAT_CONFIRM_GATE else _SP_CONFIRM_OLD) +
    "조회 도구는 항상 좁게 호출하세요 — limit(기본 10 이하)·필터·기간을 지정하고, "
    "대량 데이터가 필요하면 요약/집계 도구를 우선 사용하세요. "
    # RA 는 70개짜리 저작 워크플로 API 라 입구에서 헤맨다(비슷한 이름 다수 + 21인자 조회).
    # 자기 사용법 도구(get_guide)가 있으니 지도를 먼저 펴게 한다 — 조회 입구도 고정해 준다.
    "Report Archive(보고서) 도구는 종류가 많고 저작 워크플로용입니다 — 익숙하지 않은 작업은 "
    "get_guide 로 사용법을 먼저 확인하고, 보고서 찾기·요약은 search_reports 또는 "
    "get_reports_digest 부터 시작하세요(list_reports 의 수십 개 인자를 추측으로 채우지 마세요). "
    # 10-22 cae00 — PPT 를 AI 데이터 허브로 보낸 뒤 '방금 변환한 것 검토해 줘' 에 앞서 올린 그림을 검토했다. 보내기 결과는 이제 이력에
    # 업로드 근거로 남는다(HWAXPortal docs/chat-ppt PP-D3 · PP-D5).
    "사용자가 '방금 올린 것 · 변환한 것 · 보낸 것' 을 말하면 이력의 업로드 근거('사람이 챗 화면에서 파일을 올려 보낸 결과')를 먼저 "
    "보세요 — 없으면 앞 대화의 그림으로 대신 답하지 말고 어느 파일인지(이름) 물으세요. "
    # 도구는 질의 관련도로 일부만 바인딩된다(TOOL_MAX) — 랭킹이 빗나가면 모델이 '기능이
    # 없다'고 단정하는 게 최악의 실패다. 안내대(search_tools)가 그 폴백이다.
    "필요한 기능의 도구가 목록에 안 보이면 '없다'고 단정하기 전에 search_tools 로 찾아보세요 "
    "— 이 대화에 실리지 않은 도구까지 전부 검색됩니다. 찾은 도구가 목록에 없으면 "
    "invoke_tool(name, arguments) 로 그 자리에서 바로 호출하세요 — search_tools 결과의 "
    "args(required 포함)를 그대로 채우고, 인자를 모르면 추측하지 말고 다시 검색하세요. "
)
# 코어 데이터 규칙의 기본 글 — 코어 어휘표(아래 _core_vocab)가 없을 때 그대로 쓴다. 있으면 _core_rules_text 가 만든 글로 바꾼다.
_CORE_RULES_DEFAULT = (
    # 코어 데이터(부품 · 모델 · 과제 · 불량) — 이 서버는 MCP 서버 instructions 를 읽지 않아 게이트웨이 안내문 · 허브 platform_profile 이 웹 챗에
    # 닿지 않았고, 코어 질문이 코어 타입이 없는 앱으로 갔다(온톨로지 시험 OT-02 · 03 · 04 · 12 · 13 — HWAXPortal docs/request-1027 HA-03).
    # 한 줄 한 실패 꼴이다. ⑥ 의 '(시험) 앱' 문구는 넣지 않는다(사용자 결정 D-1 — 라벨을 바로잡는다).
    "[코어 데이터 규칙] 부품 · 모델 · 과제 · 불량 같은 코어 객체를 물으면 이렇게 하세요. "
    "① 정본은 자주검증 Datahub(rootdesign)입니다 — 이름 · 코드는 먼저 object_resolve 로 풀고, 결과가 candidates(여럿)면 "
    "고르지 말고 사용자에게 어느 것인지 물으세요. "
    "② PLM 과제(plm_task)의 식별자는 과제명입니다 — 과제코드는 별칭 · task_code 로 대조하세요. "
    "③ 개수는 목록을 세지 말고 응답의 total 이나 objects_summary 로 답하세요. "
    "④ 무엇이 무엇과 이어져 있는지는 graph_neighbors 로 보세요. "
    "⑤ 다른 허브(FailureTrace 등)로 건널 때는 앞 허브의 id 를 그대로 쓰지 말고 key 로 그 허브에서 다시 resolve 하세요. "
    "⑥ not_configured · 404 · 빈 출력은 '없다' 가 아니라 '조회하지 못했다' 로 말하세요. "
    "⑦ Report Archive · MatNexus · ComponentCore 는 코어 객체를 갖지 않습니다 — 코어 질문을 그쪽으로 보내지 마세요. "
)
_SP_TAIL = (
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
SYSTEM_PROMPT = _SP_HEAD + _CORE_RULES_DEFAULT + _SP_TAIL

# ── 코어 어휘표(HWAXPortal docs/request-1029 B-1 · C-11) ──────────────────────────────────────────────────────
# 정본은 포털 리포의 backend/config/core_vocab.json 이다. 에이전트 서버 · 포털은 늘 한 박스에 형제 리포로 있어(dev ~/claude · cae00 ~/Projects)
# 그 파일을 직접 읽는다. 바뀐 시각이 다르면 다시 읽고 코어 규칙 글을 새로 만든다. 없거나 꼴이 틀리면 위 기본 글(SYSTEM_PROMPT 그대로)로
# 돌고, 그 사실을 /health 의 core_vocab 에 싣는다.
CORE_VOCAB_PATH = Path(__file__).resolve().parent.parent / "HWAXPortal" / "backend" / "config" / "core_vocab.json"
# 박스 덧씌우기(HWAXPortal docs/request-1029 E-7 ② — gitignore) — 발동 낱말 · 정본 · 앱 역할을 cae00 에서 커밋 없이 고친다. 게이트웨이와 같은
# JSON merge patch(RFC 7386)이고 지문 셈법도 같다 — 두 서비스의 /health core_vocab.fingerprint 가 같아야 한 표를 본다(update-all §6 이 견준다).
CORE_VOCAB_LOCAL_PATH = CORE_VOCAB_PATH.with_name("core_vocab.local.json")
_CORE_VOCAB: dict = {"mtime": None, "data": None, "error": "아직 읽지 않음", "overlay": None}


def _merge_patch(target, patch):
    """JSON merge patch(RFC 7386) — 객체는 재귀로 합치고, 그 밖은 바꾸며, null 은 그 칸을 지운다(목록은 통째로 바뀐다). 게이트웨이와 같다."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _merge_patch(out.get(k), v)
    return out


def _json_fp(obj) -> str:
    """값의 지문 — 키 차례 JSON 의 sha256 앞 12자(설명 칸 _doc 은 뺀다). 게이트웨이 gateway._json_fp 와 같은 셈이다."""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k != "_doc"}
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


def _patch_paths(patch: dict) -> list[str]:
    out = []
    for k, v in patch.items():
        if k == "_doc":
            continue
        out += [f"{k}.{kk}" for kk in v] if isinstance(v, dict) and v else [k]
    return out


def _core_rules_text(d: dict) -> str:
    """코어 규칙 글 — 발동 낱말(R-1) · 첫 걸음은 정본 resolve 하나(R-7) · 타입 이름 대응(10-30 HA-32) · 관계 대응(R-8) · 코어가 아닌 앱(R-2)을
    어휘표에서 만든다. 나머지 줄은 기본 글과 같다. 10-29 QA 에서 발동 낱말이 '부품 · 모델 · 과제 · 불량' 뿐이라 고장모드 · 시험항목 · 가상검증 ·
    관리 기준 · 물성 질문 다섯이 코어를 건너뛰었고(OT-06 ~ 10), FMEA · ARP · ODB 허브가 코어 질문을 가로챘다. 10-30 QA 에서는 없는 타입 이름을
    지어내 불렀다(DW-22 — object_resolve(type_slug='management_standard'), 실제는 criterion). 시스템 프롬프트는 호출마다 실리므로 짧게 둔다."""
    apps = list((d.get("apps") or {}).values())
    sib = " · ".join(a["label"] for a in apps if a.get("role") == "sibling") or "FailureTrace 등"
    non = " · ".join(f"{a['label']}({a['note']})" if a.get("note") else a["label"] for a in apps if a.get("role") == "noncore")
    rel = " · ".join(f"{r['phrase']} = {r['relation']}" for r in d.get("relations") or [])
    # 타입 이름은 허브 slug 꼴이다(StandardPlatform ontology SLUG_RE). 꼴이 아니면 그 판을 버린다(다른 칸처럼) — 종전엔 박스 덧씌우기의 오타가
    # 객체 · 숫자면 repr 꼴로 프롬프트에 실리고 빈 목록이면 줄이 조용히 빠졌는데, 게이트웨이는 types 를 읽지 않아 지문도 같았다(10-30 반대 검토).
    types = d.get("types")
    if types is not None and not isinstance(types, dict):
        raise TypeError(f"types 는 낱말 → 타입 이름의 객체여야 한다({type(types).__name__})")
    for w, s in (types or {}).items():
        if not (w.strip() and isinstance(s, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", s)):
            raise TypeError(f"types 의 {w!r} = {s!r} — 타입 이름은 허브 slug 꼴(소문자로 시작하는 영문 · 숫자 · 밑줄 32자 이내)이어야 한다")
    typ = " · ".join(f"{w} = {s}" for w, s in (types or {}).items())
    return (
        f"[코어 데이터 규칙] {' · '.join(d['rule_words'])} 같은 코어 객체를 물으면 이렇게 하세요. "
        "이 낱말이 나오면 첫 걸음은 정본의 object_resolve 하나입니다 — 여러 앱을 한꺼번에 훑지 말고, 사례 · 실측 · 규격 원문은 그 뒤에 "
        "다른 앱에서 덧붙이세요. "
        f"① 정본은 {d['canonical']['label']}입니다 — 이름 · 코드는 먼저 object_resolve 로 풀고, 결과가 candidates(여럿)면 "
        "고르지 말고 사용자에게 어느 것인지 물으세요. "
        # 대응에 없는 발동 낱말(부품 · 원인 · 시험군 …)도 허브 object_resolve 는 type_slug 가 필수다 — 허브 안내의 '타입을 모르면 search' 는 이 서버가
        # MCP 서버 안내를 읽지 않아 웹 챗에 닿지 않는다(위 _CORE_RULES_DEFAULT 주석). search 의 types[] 가 타입별 type_slug · 건수다.
        + (f"타입 이름(type_slug)은 지어내지 말고 이 대응을 쓰세요 — {typ}. "
           "대응에 없는 낱말은 짐작하지 말고 정본의 search 결과 types[] 에서 타입 이름을 확인하세요. " if typ else "")
        + "② PLM 과제(plm_task)의 식별자는 과제명입니다 — 과제코드는 별칭 · task_code 로 대조하세요. "
        "③ 개수는 목록을 세지 말고 응답의 total 이나 objects_summary 로 답하세요. "
        "④ 무엇이 무엇과 이어져 있는지는 graph_neighbors 로 보세요" + (f" — 자주 묻는 관계는 {rel} 입니다. " if rel else ". ")
        + f"⑤ 다른 허브({sib})로 건널 때는 앞 허브의 id 를 그대로 쓰지 말고 key 로 그 허브에서 다시 resolve 하세요. "
        "⑥ not_configured · 404 · 빈 출력은 '없다' 가 아니라 '조회하지 못했다' 로 말하세요. "
        + (f"⑦ {non} 는 코어 객체를 갖지 않습니다 — 표준 키 · 개수 · 관계는 코어에서 답하고, 코어 질문을 그쪽으로 보내지 마세요. "
           if non else "⑦ Report Archive · MatNexus · ComponentCore 는 코어 객체를 갖지 않습니다 — 코어 질문을 그쪽으로 보내지 마세요. "))


def _core_vocab() -> dict | None:
    """코어 어휘표 — 바뀐 시각이 다르면 다시 읽고 코어 규칙 글을 만든다. 없거나 못 읽으면 None 이고 까닭은 _CORE_VOCAB['error'] 다."""
    try:
        mt = CORE_VOCAB_PATH.stat().st_mtime_ns
    except OSError:
        if _CORE_VOCAB["error"] != "없음":
            print(f"[agent] ⚠ 코어 어휘표가 없다({CORE_VOCAB_PATH}) — 코어 규칙은 기본 글로 돈다")
        _CORE_VOCAB.update(mtime=None, data=None, error="없음")
        return None
    try:
        lmt = CORE_VOCAB_LOCAL_PATH.stat().st_mtime_ns
    except OSError:
        lmt = None

    def build(d: dict) -> dict:
        return {"version": d.get("version"), "rule_words": len(d["rule_words"]), "canonical": d["canonical"]["app"],
                "rules": _core_rules_text(d),
                # 허브 키 → 라벨 — 정본과 role sibling(다른 허브 id 막이 _hub_apps, HWAXPortal docs/request-1030 HA-30). /health 에는 싣지 않는다.
                "hubs": {d["canonical"]["app"]: d["canonical"].get("label") or d["canonical"]["app"],
                         **{k: (a or {}).get("label") or k for k, a in (d.get("apps") or {}).items()
                            if (a or {}).get("role") == "sibling"}}}
    if (mt, lmt) != _CORE_VOCAB["mtime"]:
        try:
            raw = json.loads(CORE_VOCAB_PATH.read_text(encoding="utf-8"))
            data = build(raw)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            print(f"[agent] ⚠ 코어 어휘표를 못 읽었다({CORE_VOCAB_PATH}) — 코어 규칙은 기본 글로 돈다: {exc!r}"[:400])
            _CORE_VOCAB.update(mtime=(mt, lmt), data=None, error=f"못 읽음({type(exc).__name__})", overlay=None)
        else:
            eff, overlay = raw, {"present": lmt is not None}
            if lmt is not None:
                try:
                    patch = json.loads(CORE_VOCAB_LOCAL_PATH.read_text(encoding="utf-8"))
                    if not isinstance(patch, dict):
                        raise TypeError("덧씌우기는 객체여야 한다")
                    merged = _merge_patch(raw, patch)
                    data, eff = build(merged), merged
                    overlay.update(fingerprint=_json_fp(patch), changed=_patch_paths(patch)[:40])
                except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                    overlay["error"] = f"덧씌우기를 버렸다({type(exc).__name__}: {str(exc)[:120]})"
                    print(f"[agent] ⚠ 코어 어휘표 덧씌우기({CORE_VOCAB_LOCAL_PATH})를 버리고 기본 표로 돈다: {exc!r}"[:400])
            data = {**data, "fingerprint": _json_fp(eff), "base_fingerprint": _json_fp(raw)}
            _CORE_VOCAB.update(mtime=(mt, lmt), data=data, error=None, overlay=overlay)
    return _CORE_VOCAB["data"]


def _system_prompt() -> str:
    """챗 시스템 프롬프트 — 코어 규칙만 어휘표에서 만든 글로 바꾼다. 어휘표가 없으면 SYSTEM_PROMPT 와 글자 하나 다르지 않다."""
    d = _core_vocab()
    return SYSTEM_PROMPT if d is None else _SP_HEAD + d["rules"] + _SP_TAIL


# 프롬프트 앞부분 고정(HWAXPortal docs/request-1029 W-3) — Qwen 의 채팅 틀은 도구 설명을 **첫 시스템 메시지 끝**에 붙인다. 턴마다 바뀌는
# 안내(쓸 수 있는 앱 · 핀 · 페르소나 · 지식카드 발췌 · 붙인 문서)가 첫 시스템 메시지에 붙어 있으면 그것이 바뀔 때마다 '시스템 + 도구' 앞부분이
# 캐시에서 깨진다. 그래서 바뀌는 안내는 이번 사람 발화 바로 앞의 시스템 칸에 둔다. 안내의 자리가 바뀌어 지시를 따르는 정도가 달라질 수 있어
# (C-7) 끄는 손잡이를 둔다 — CHAT_STABLE_PREFIX=0 이면 종전처럼 한 시스템 메시지에 잇는다.
# ⚠ 안내를 **둘째 시스템 메시지**로 두면 Qwen 의 채팅 틀이 'System message must be at the beginning' 으로 거절한다(cae00 실측 — LLM 400,
# 전문가 · 앱 핀 · 붙인 문서 챗, 10-30 QA HA-26). 시스템 메시지는 맨 앞 하나뿐이어야 한다 — 안내는 이번 사람 발화의 앞머리에 싣는다.
# 그래도 그 400 이 오면 오류 턴의 자동 재시도는 안내를 첫 시스템 메시지에 합쳐 보낸다(_turn_msgs(merged=True) — 박스 우회
# CHAT_STABLE_PREFIX=0 과 같은 꼴로, 우회 뒤 운영자 14명이 0/14 실패였다). 재시도는 안내를 빼지 않는다 — 자리만 옮긴다.
_STABLE_PREFIX = os.environ.get("CHAT_STABLE_PREFIX", "1").strip() != "0"
_TURN_NOTES_HEAD = "[이번 턴의 안내 — 시스템이 붙인 것이다. 사람이 쓴 글이 아니다]\n"
_TURN_NOTES_TAIL = "\n\n[사람의 말]\n"
_SYS_NOT_FIRST = "System message must be at the beginning"     # 그 거절 글 — vLLM 이 400 본문에 그대로 싣는다(자동 재시도가 이 글로 가른다)


def _with_turn_notes(user_msg, notes: str):
    """이번 발화 앞머리에 이 턴의 안내를 싣는다 — 글이면 글 앞에, 그림이 든 HumanMessage 면 첫 글 조각으로."""
    head = _TURN_NOTES_HEAD + notes
    if isinstance(user_msg, tuple):
        role, content = user_msg
        if isinstance(content, str):
            return (role, head + _TURN_NOTES_TAIL + content)
        return (role, [{"type": "text", "text": head + _TURN_NOTES_TAIL}, *list(content or [])])
    content = getattr(user_msg, "content", None)
    if isinstance(content, str):
        return user_msg.model_copy(update={"content": head + _TURN_NOTES_TAIL + content})
    if isinstance(content, list):
        return user_msg.model_copy(update={"content": [{"type": "text", "text": head + _TURN_NOTES_TAIL}, *content]})
    return user_msg


def _turn_msgs(sys_prompt: str, turn_notes: str, hist: list, recap, user_msg, *, merged: bool = False) -> list:
    """챗 한 턴의 메시지 — [첫 시스템(고정) · 이력 · (그림 다시 싣기) · 이 턴의 안내를 앞머리에 단 이번 발화]. 안내가 없거나 손잡이를 끄면 종전 꼴.

    시스템 메시지는 늘 맨 앞 하나다(Qwen 채팅 틀 — HA-26). 고정 앞부분(시스템 + 도구 + 이력)은 그대로라 캐시 몫은 W-3 과 같다.
    merged 면 손잡이와 상관없이 종전 꼴(안내를 첫 시스템 메시지에 잇는다) — 그래도 'System message' 400 을 받은 턴의 자동 재시도가 쓴다."""
    if not turn_notes or not _STABLE_PREFIX or merged:
        return [("system", sys_prompt + turn_notes), *hist, *([recap] if recap else []), user_msg]
    return [("system", sys_prompt), *hist, *([recap] if recap else []), _with_turn_notes(user_msg, turn_notes.lstrip("\n"))]


def _core_vocab_health() -> dict:
    """/health 의 core_vocab — 읽었나 · 버전 · 발동 낱말 수 · 정본 앱. 경로는 싣지 않는다(박스 배치가 드러난다)."""
    d = _core_vocab()
    if d is None:
        return {"loaded": False, "error": _CORE_VOCAB["error"]}
    return {"loaded": True, "version": d["version"], "rule_words": d["rule_words"], "canonical": d["canonical"],
            # 덧씌운 표 · 기본 표의 지문과 박스 덧씌우기 — 게이트웨이 /health core_vocab 과 같아야 한다(E-7 ③)
            "fingerprint": d["fingerprint"], "base": d["base_fingerprint"],
            "overlay": dict(_CORE_VOCAB.get("overlay") or {"present": False})}


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
        # 도구 호출 1건의 기한 — MCP ClientSession 의 요청 기한으로 건다(어댑터가 session_kwargs 를 그대로
        # 세션에 넘긴다). 박스 설정에 session_kwargs 가 있으면 그 위에 얹는다.
        if MCP_CALL_TIMEOUT_S > 0:
            cfg["session_kwargs"] = {**(cfg.get("session_kwargs") or {}),
                                     "read_timeout_seconds": timedelta(seconds=MCP_CALL_TIMEOUT_S)}
        out[name] = cfg
    return out


def _mcp_call_timed_out(exc: BaseException) -> bool:
    """그 예외가 엔진측 도구 기한(MCP_CALL_TIMEOUT_S)에 걸린 것인가 — MCP 세션이 408 로 올린다."""
    return (type(exc).__name__ == "McpError"
            and (getattr(getattr(exc, "error", None), "code", None) == 408
                 or "Timed out while waiting for response" in str(exc)))


def _stream_chunk_limit_note(exc: BaseException) -> str:
    """그 예외가 챗 스트리밍의 청크 침묵 한도(LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S)에 걸린 것이면 화면에 낼
    사유 한 구절, 아니면 "".

    langchain-openai 가 StreamChunkTimeoutError 로 올린다. openai SDK 예외가 아니라 챗 오류 문구의 'APITimeout'·
    'Connection' 가름 어디에도 안 걸렸고, 화면에는 '처리 중 내부 오류'·'에이전트 처리 중 오류' 로 나갔다(라이브러리
    문구는 설정 이름을 말하지만 서버 로그에만 남는다). 스트리밍 박스에서는 이 한도(start.sh 기본 300초)가
    LLM_TIMEOUT_S(900초)보다 **먼저** 걸린다 — 심의가 공유 LLM 을 차지한 동안 챗이 이렇게 끝난다.
    걸린 값은 설정을 다시 읽지 않고 예외에서 읽는다(그 호출에 실제로 걸린 값이다)."""
    if "StreamChunkTimeout" not in type(exc).__name__:
        return ""
    _s = getattr(exc, "timeout_s", None)
    return ("LLM 이 " + (f"{_s:,.0f}초 동안 " if isinstance(_s, (int, float)) and _s > 0 else "제한 시간 동안 ")
            + "토큰을 보내지 않았습니다(LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S) — LLM 이 밀려 있거나 멈췄습니다")


# ── 챗 전용 LLM(선택) — HWAXPortal docs/chat-llm-stcx ─────────────────────────────────────────────────────────────
# CHAT_LLM_BASE_URL 이 비면 챗도 VLLM_* 클라이언트(app.state.llm)를 그대로 쓴다 — 아무것도 바뀌지 않는다. 채우면 챗 ReAct · 챗 안 도구
# 계획만 그 서버로 간다. 띵킹 · 심의 준비(전문가 고르기 축 · VOC · 되묻기 · 주제 뽑기) · 심의 · 카드 대조는 VLLM_* 그대로다(CL-D2).
# 넘김은 LLM 호출 단위다 — 턴 전체를 다시 돌리면 이미 실행한 도구가 두 번 돈다(CL-D3). 넘기는 것은 연결 실패 · 시간 초과 · 5xx ·
# 401/403(그 서버의 키 — VLLM_* 는 자격이 따로다) · 첫 조각 전 스트림 침묵 · '창이 모자라다' 400(ChatCtxOverflow — 챗 턴의 창은 GLM 창
# 안이다, CL-D9)이다(_chat_fallback_errors). 다른 400 은 VLLM_* 에서도 같은 까닭으로 실패할 수 있고 404(모델 이름)는 설정 오류라 숨기지
# 않는다. 토큰이 흐르기 시작한 뒤의 실패는 넘기지 못한다(with_fallbacks 의 스트림 규칙).
#   CHAT_LLM_BASE_URL · CHAT_LLM_MODEL       둘 다 있어야 켜진다. CHAT_LLM_API_KEY(기본 EMPTY)
#   CHAT_LLM_ENABLE_THINKING   기본 1 — 생각을 켠다(사용자 결정 10-18 — 10-17 의 '끔' 을 뒤집었다, HWAXPortal docs/chat-depth-context).
#                              0 이면 끈다(chat_template_kwargs.enable_thinking=false).
#   CHAT_LLM_REASONING_EFFORT  생각 강도 — 기본 xhigh(모델 최고). medium · low. 생각을 켰을 때만 보낸다.
#                              LLM_REASONING_EFFORT 는 VLLM_* 서버 것이라 챗 서버에 보내지 않는다.
#   CHAT_LLM_MAX_TOKENS        챗 서버에 보내는 출력 상한(생각 + 답) — 생각을 켜면 기본 0(보내지 않는다: stcx 서버가 131,072 로 막는다 —
#                              요청서 v1.1 0장, CD-D6), 끄면 LLM_MAX_TOKENS 를 따른다.
#   CHAT_LLM_OUTPUT_RESERVE    챗 턴의 창에서 비워 둘 출력 몫 — 생각을 켜면 기본 65536(실측 xhigh 출력의 두 배), 끄면 0. 보내는 상한이 더
#                              크면 그 값이다(vLLM 은 입력 + max_tokens > 창이면 400). 창의 절반까지만 뗀다(_agent_stream — CD-D4).
#   CHAT_LLM_DISABLE_STREAMING 비우면 LLM_DISABLE_STREAMING 을 따른다 — GLM 의 스트리밍 tool_calls 유실 우회가 켜진 박스(cae00)는 챗
#                              서버도 비스트리밍으로 시작한다. 그 서버의 스트리밍 도구 호출을 확인한 뒤 0 으로 연다. 챗 서버와 넘겨받는
#                              VLLM_* 의 스트리밍이 달라도 글은 빠지지 않는다(_agent_stream_turn 이 호출마다 본다)
#   CHAT_LLM_MAX_RETRIES       기본 1 — 챗 서버에 다시 보내는 횟수. 그다음은 넘긴다
#   CHAT_LLM_FALLBACK          기본 1 — 0 이면 넘기지 않는다(실패가 그대로 챗에 보인다)
#   CHAT_LLM_CONTEXT_TOKENS    챗 서버의 /v1/models 에서 창을 못 읽을 때 쓸 값(기본 LLM_CONTEXT_TOKENS, 없으면 128000)
import openai  # noqa: E402 — 챗 서버의 '창이 모자라다' 400 을 넘김 대상으로 바꾸는 데만 쓴다(langchain_openai 가 이미 들인 의존성이다)

# vLLM 이 '이 요청은 내 창보다 길다' 로 거절하는 문구 — OpenAI 호환 서버의 검사(renderers/params.py)와 엔진의 검사(input_processor.py,
# 'longer than the maximum model length'), 그리고 OpenAI 의 오류 코드. vLLM 0.23.0 원문으로 확인했다(10-18).
_CTX_OVERFLOW_MARKS = ("maximum context length", "maximum model length", "context_length_exceeded")


class ChatCtxOverflow(openai.BadRequestError):
    """챗 서버가 '이 요청은 내 창보다 길다' 로 거절한 400(CL-D9). 다른 400 과 달리 VLLM_* 는 같은 까닭으로 실패하지 않는다 — 챗 턴의 창은
    두 서버 중 작은 값이라(CL-D5) 그 요청은 GLM 창 안에 든다. 이런 거절은 챗 서버 복제본들의 창이 섞일 때 난다 — stcx 를 1M(YaRN)으로
    올리는 시범 · rolling 동안 에이전트가 새 복제본에서 1M 을 읽고 만든 긴 요청을 부하분산기가 옛 262,144 복제본에 보낸다."""


@contextmanager
def _ctx_overflow_falls_back():
    """그 안에서 난 '창이 모자라다' 400 만 ChatCtxOverflow 로 바꿔 올린다 — 나머지 예외는 그대로다."""
    try:
        yield
    except openai.BadRequestError as e:
        if not isinstance(e, ChatCtxOverflow) and any(m in str(e).lower() for m in _CTX_OVERFLOW_MARKS):
            raise ChatCtxOverflow(e.message, response=e.response, body=e.body) from e
        raise


class _ChatServerLLM(ChatOpenAI):
    """챗 전용 LLM 의 앞(챗 서버) 클라이언트 — ChatOpenAI 그대로이되 '창이 모자라다' 400 을 ChatCtxOverflow 로 올려 with_fallbacks 가
    넘기게 한다. 비스트리밍(_generate · _agenerate) · 스트리밍(_stream · _astream) 네 길을 다 덮는다 — 400 은 첫 조각 전에 난다."""

    def _generate(self, *a, **k):
        with _ctx_overflow_falls_back():
            return super()._generate(*a, **k)

    async def _agenerate(self, *a, **k):
        with _ctx_overflow_falls_back():
            return await super()._agenerate(*a, **k)

    def _stream(self, *a, **k):
        with _ctx_overflow_falls_back():
            yield from super()._stream(*a, **k)

    async def _astream(self, *a, **k):
        with _ctx_overflow_falls_back():
            async for chunk in super()._astream(*a, **k):
                yield chunk

    # 생각 글 — langchain-openai 는 OpenAI 규격 밖의 칸(vLLM 의 reasoning · 옛 이름 reasoning_content)을 버린다(그 모듈의 'API scope'
    # 주의). 챗 서버(Qwen)는 생각을 켜면 그 칸에 생각을 흘리므로, 조각 · 완결 응답에서 꺼내 additional_kwargs.reasoning_content 에 둔다 —
    # 챗 루프가 'reasoning' 사건으로 화면의 접힌 '생각 중' 칸에 낸다(사용자 결정 10-18). 다음 턴 이력에는 넣지 않는다.
    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        gen = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        rc = _reasoning_in(chunk, "delta")
        if gen is not None and rc:
            gen.message.additional_kwargs["reasoning_content"] = rc
        return gen

    def _create_chat_result(self, response, generation_info=None):
        res = super()._create_chat_result(response, generation_info)
        rc = _reasoning_in(response, "message")
        if rc and res.generations:
            res.generations[0].message.additional_kwargs["reasoning_content"] = rc
        return res


def _reasoning_in(obj, part: str) -> str:
    """OpenAI 호환 응답(조각이면 part='delta', 완결이면 'message')에서 생각 글 — vLLM 0.23 은 reasoning, 그 전 판 · 파생판은 reasoning_content."""
    try:
        d = obj if isinstance(obj, dict) else obj.model_dump()
        m = ((d.get("choices") or [{}])[0] or {}).get(part) or {}
        return str(m.get("reasoning") or m.get("reasoning_content") or "")
    except Exception:  # noqa: BLE001 — 생각 글을 못 꺼내도 답은 그대로다
        return ""


class _VisionGuardLLM(ChatOpenAI):
    """챗의 넘김 받는 VLLM_*(GLM) 클라이언트 — 그 서버가 그림을 못 받으면(LLM_VISION 판정 — 모르면 못 받는 것으로) 그림 조각을 빼고
    '[그림 N장 — 이 모델은 그림을 읽지 못해 뺐다]' 한 줄로 바꿔 보낸다. 빼지 않으면 그림이 실린 챗이 넘어가는 순간 400 으로 죽는다.
    판정(탐침)은 그림이 실린 호출이 실제로 올 때 한 번 — 그림 없는 넘김은 묻지 않는다."""

    @staticmethod
    def _keep(messages):
        if not _vision.has_images(messages):
            return messages
        return messages if _base_vision() else _vision.strip_images(messages)

    def _generate(self, messages, *a, **k):
        return super()._generate(self._keep(messages), *a, **k)

    async def _agenerate(self, messages, *a, **k):
        return await super()._agenerate(await asyncio.to_thread(self._keep, messages), *a, **k)

    def _stream(self, messages, *a, **k):
        yield from super()._stream(self._keep(messages), *a, **k)

    async def _astream(self, messages, *a, **k):
        async for chunk in super()._astream(await asyncio.to_thread(self._keep, messages), *a, **k):
            yield chunk


def _chat_vision(app_) -> bool:
    """챗이 쓰는 모델이 그림을 받는가 — 챗 전용 LLM 이 켜져 있으면 그 서버(CHAT_LLM_VISION), 아니면 VLLM_*(LLM_VISION). 막히는 호출이다
    (첫 판정에 탐침 한 번) — 이벤트 루프에서는 to_thread 로 부른다."""
    cfg = getattr(app_.state, "chat_llm_cfg", None)
    if cfg:
        return _vision.supports(os.environ.get("CHAT_LLM_VISION", "auto"), cfg["url"], cfg["key"], cfg["model"])
    return _vision.supports(os.environ.get("LLM_VISION", "auto"), VLLM_BASE_URL, VLLM_API_KEY, VLLM_MODEL)


def _chat_vision_known(app_) -> bool | None:
    """_chat_vision 의 막히지 않는 판 — 지금 아는 값(아직 안 물었으면 None). 그림이 안 걸린 턴이 탐침을 기다리지 않게. None 이면 도구가
    그림을 실제로 낸 그 호출에서 묻는다(_loop_fit_prompt). 기동 때 미리 묻지 않는다 — 서버를 띄우기만 하는 도구(m0b 탐침 --dry-run)가
    LLM 을 부르지 않는다는 약속이 있다."""
    cfg = getattr(app_.state, "chat_llm_cfg", None)
    if cfg:
        return _vision.cached(os.environ.get("CHAT_LLM_VISION", "auto"), cfg["url"], cfg["model"])
    return _vision.cached(os.environ.get("LLM_VISION", "auto"), VLLM_BASE_URL, VLLM_MODEL)


def _base_vision() -> bool:
    """VLLM_*(띵킹 · 심의 — GLM)가 그림을 받는가(LLM_VISION). 막히는 호출 — to_thread 로."""
    return _vision.supports(os.environ.get("LLM_VISION", "auto"), VLLM_BASE_URL, VLLM_API_KEY, VLLM_MODEL)


def _transcriber(app_):
    """그림 판독에 쓸 LLM — 그림을 받는 챗 서버(넘김 없이 그 서버만), 아니면 그림을 받는 VLLM_*, 둘 다 아니면 None. 막히는 호출 — to_thread 로."""
    cfg = getattr(app_.state, "chat_llm_cfg", None)
    if cfg and _vision.supports(os.environ.get("CHAT_LLM_VISION", "auto"), cfg["url"], cfg["key"], cfg["model"]):
        chat = getattr(app_.state, "chat_llm", None)
        return getattr(chat, "runnable", None) or chat
    if _base_vision():
        return app_.state.llm
    return None


def _stash_uploads(items) -> tuple[list, int]:
    """챗에 올린 그림 → (아티팩트 주소들, 건너뛴 수). 해시 이름이라 같은 그림은 한 파일이다. 그림 꼴이 아니거나 깨졌거나 크면
    (VISION_MAX_BYTES) · 장수 상한(VISION_MAX_IMAGES) 밖이면 건너뛰고 센다 — 호출부가 화면에 알린다(조용히 빼지 않는다)."""
    import base64
    import hashlib
    items = [it for it in (items or []) if isinstance(it, dict)]
    urls: list = []
    skipped = max(0, len(items) - _vision.MAX_IMAGES)
    for it in items[:_vision.MAX_IMAGES]:
        data, mime = str(it.get("data") or ""), str(it.get("mime") or "")
        if data.startswith("data:"):
            head, _, data = data.partition(",")
            mime = mime or head[5:].split(";", 1)[0]
        if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            skipped += 1
            continue
        try:
            raw = base64.b64decode(data, validate=False)
        except Exception:  # noqa: BLE001 — 깨진 base64 는 건너뛴다(센다)
            raw = b""
        real = _vision.sniff(raw) if raw else None      # 확장자는 내용으로 — 선언된 mime 과 달라도 내용이 그림이면 받는다
        if not real or len(raw) > _vision.MAX_BYTES:
            skipped += 1
            continue
        name = hashlib.sha256(raw).hexdigest()[:20] + "." + _ARTIFACT_EXT[real]
        with open(os.path.join(ARTIFACT_DIR, name), "wb") as f:
            f.write(raw)
        u = f"/agent/artifacts/{name}"
        if u not in urls:
            urls.append(u)
    if urls:
        _prune_artifacts()
    return urls, skipped


def _with_tool_images(msgs: list, limit: int) -> list:
    """도구 결과에 나온 그림을 그 결과 묶음(한 번의 도구 호출들) 바로 뒤에 사람 쪽 '시스템 첨부' 로 — 모델이 그림을 직접 본다. 최근
    그림부터 limit 장. 도구 결과 사이에는 끼우지 않는다(한 호출의 도구 답들은 붙어 있어야 한다)."""
    from langchain_core.messages import HumanMessage, ToolMessage  # noqa: PLC0415

    groups: list = []                 # [(마지막 도구 결과 위치, 그림 주소들)]
    i = 0
    while i < len(msgs):
        if isinstance(msgs[i], ToolMessage):
            j, urls = i, []
            while j < len(msgs) and isinstance(msgs[j], ToolMessage):
                c = msgs[j].content
                for u in _vision.image_urls(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)):
                    if u not in urls:
                        urls.append(u)
                j += 1
            if urls:
                groups.append((j - 1, urls))
            i = j
        else:
            i += 1
    if not groups:
        return msgs
    plan, left = {}, limit
    for end, urls in reversed(groups):
        if left <= 0:
            break
        take = urls[-left:]
        plan[end], left = take, left - len(take)
    out = []
    for k, m in enumerate(msgs):
        out.append(m)
        if k in plan:
            ps, miss = _vision.parts(plan[k], ARTIFACT_DIR, AIDH_HTTP, limit=len(plan[k]))
            if ps:
                out.append(HumanMessage(content=[{"type": "text", "text": (
                    f"[시스템 첨부 — 방금 도구가 낸 그림 {len(ps)}장" + (f"(그 밖의 {miss}장은 서버에 없거나 커서 못 실었다)" if miss else "")
                    + ". 그림을 직접 보고 수치 · 분포를 판단하라]")}, *ps]))
    return out


def _chat_fallback_errors() -> tuple:
    """넘기는 예외 — 연결 실패 · 시간 초과 · 5xx(openai SDK 는 500 이상을 InternalServerError 로 낸다) · 401/403 · 첫 조각 전 스트림 침묵 ·
    '창이 모자라다' 400(ChatCtxOverflow — _ChatServerLLM 이 다른 400 과 갈라 올린다, CL-D9).

    401 · 403 은 챗 서버의 키 문제다 — VLLM_* 는 자기 자격이 따로 있어 같은 까닭으로 실패하지 않는다. 넘기지 않으면 키 없이 켜거나 키를
    바꾸는 동안(stcx rolling 약 20분 — 옛 키 · 새 키가 섞인다) 챗이 그대로 실패했다(10-17 cae00 리뷰 A1). 설정 오류가 숨지 않게 /health 의
    chat_llm.last_error · 넘김 횟수 · update-all 3.5c 의 '키를 거절했다' 가 말한다.
    스트림이 첫 조각을 내기 전에 멈추면(LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S, start.sh 기본 300초) langchain-openai 가
    StreamChunkTimeoutError 를 올린다 — openai SDK 예외가 아니라 넘기지 못했고, stcx 복제본 하나가 받고 멈추면 그 사용자가 300초 뒤 오류를
    봤다(A2). 그 예외는 TimeoutError 계열이다(3.10 에서는 asyncio.TimeoutError 와 TimeoutError 둘 다). 조각이 흐른 뒤의 실패는 여전히
    넘기지 못한다(with_fallbacks 의 스트림 규칙)."""
    import asyncio  # noqa: PLC0415

    return (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError,
            openai.AuthenticationError, openai.PermissionDeniedError, asyncio.TimeoutError, TimeoutError, ChatCtxOverflow)


def _now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402 — 챗 전용 LLM 의 넘김 계수기만 쓴다


class _ChatLLMWatch(BaseCallbackHandler):
    """챗 전용 LLM 의 넘김을 센다 — 앞(챗 서버)의 마지막 실패 사유와 뒤(VLLM_*)가 넘겨받은 호출 수 · 시각. /health 의 chat_llm 이 읽는다.
    뒤 인스턴스는 챗의 넘김에만 쓰는 따로 만든 것이라(app.state.llm 과 다르다) 그 시작이 곧 넘김 한 번이다."""

    # 이벤트 루프 안에서 돈다(동기 처리기는 기본이면 스레드 풀로 가 계수가 겹칠 수 있다) — 하는 일은 수 세기와 한 줄뿐이다.
    run_inline = True

    def __init__(self, info: dict, role: str) -> None:
        self.info, self.role, self._said = info, role, 0.0

    def on_llm_error(self, error, **kwargs) -> None:
        if self.role == "primary":
            self.info["last_error"] = f"{type(error).__name__}: {str(error)[:160]}"
            self.info["last_error_at"] = _now_iso()

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        if self.role != "fallback":
            return
        fb = self.info["fallback"]
        fb["count"] += 1
        fb["last_at"] = _now_iso()
        now = time.monotonic()
        if now - self._said >= 60:          # 챗 서버가 오래 죽어 있어도 로그를 호출마다 찍지 않는다
            self._said = now
            print(f"[agent] 챗 LLM 넘김 — {self.info.get('last_error') or '사유 모름'} → {VLLM_MODEL}(VLLM_*) · "
                  f"지금까지 {fb['count']}회")


# 챗 LLM 호출의 대화 머리(HWAXPortal docs/request-1029 W-1) — 박스 부하분산기(~/stcx-llm/nginx.conf, O-2)가 이 값으로 같은 대화의 호출을
# 같은 GPU 복제본에 보내 접두 캐시를 다시 쓰게 한다(10-29 — 이어진 두 호출이 같은 복제본으로 간 것은 117쌍 중 1쌍 · 접두 캐시 적중 11.9%).
# 값은 서버 대화 id 의 해시, 없으면 '사람 + 이 턴의 시작' 의 해시다(한 질문의 3 ~ 16회 호출이 한 복제본). 자기검증 호출도 같은 값이다.
# 머리를 보지 않는 부하분산기 · 서버에는 아무 일도 없다.
_AFFINITY_HEADER = "X-Session-Affinity"
_chat_affinity: contextvars.ContextVar = contextvars.ContextVar("chat_affinity", default="")


async def _affinity_hook(request) -> None:
    v = _chat_affinity.get()
    if v:
        request.headers[_AFFINITY_HEADER] = v


def _affinity_key(req) -> str:
    base = ((getattr(req, "conversation_id", "") or "").strip()
            or f"{(getattr(req, 'user_email', '') or '').strip().lower()}|{time.time_ns()}")
    return hashlib.sha256(base.encode()).hexdigest()[:16]


def _build_chat_llm(base_llm, mk, *, temperature: float, max_tokens: int, timeout_s: float, base_effort: str,
                    base_retries: int, base_nostream: bool):
    """(챗이 쓸 LLM, /health 에 실을 상태, 내부 설정). 꺼져 있으면 (base_llm, {"enabled": False}, None). mk 는 lifespan 의 _mk_llm."""
    url = (os.environ.get("CHAT_LLM_BASE_URL") or "").strip().rstrip("/")
    model = (os.environ.get("CHAT_LLM_MODEL") or "").strip()
    if not url:
        return base_llm, {"enabled": False}, None
    if not model:
        info = {"enabled": False, "error": "CHAT_LLM_BASE_URL 은 있는데 CHAT_LLM_MODEL 이 비었다 — 챗도 VLLM_* 를 쓴다"}
        print(f"[agent] ⚠ {info['error']}")
        return base_llm, info, None
    key = os.environ.get("CHAT_LLM_API_KEY") or "EMPTY"
    thinking = (os.environ.get("CHAT_LLM_ENABLE_THINKING") or "1").strip() == "1"
    effort = (os.environ.get("CHAT_LLM_REASONING_EFFORT") or "xhigh").strip() if thinking else ""
    # 생각을 켜면 출력 상한은 보내지 않는다 — 끝없이 길어지는 회차는 stcx 서버가 131,072 로 끊는다(요청서 v1.1 0장). 공용
    # LLM_MAX_TOKENS 는 GLM 길에도 걸리는 값이라 챗 서버에 그대로 쓰지 않는다. 끄면 종전대로 공용 값이다.
    chat_mt = _env_int("CHAT_LLM_MAX_TOKENS", 0 if thinking else max_tokens)
    # 출력 몫 — 보내지 않아도 입력이 창을 다 채우면 생각할 자리가 없어 답이 서버 상한 전에 창에서 잘린다(CD-D6).
    out_reserve = max(_env_int("CHAT_LLM_OUTPUT_RESERVE", 65536 if thinking else 0), chat_mt)
    _ns = (os.environ.get("CHAT_LLM_DISABLE_STREAMING") or "").strip()
    nostream = base_nostream if not _ns else _ns == "1"
    fallback_on = (os.environ.get("CHAT_LLM_FALLBACK") or "1").strip() != "0"
    info: dict = {"enabled": True, "server": _call_trace.server_label(url), "model": model, "thinking": thinking,
                  "effort": effort or None, "max_tokens": chat_mt or None, "output_reserve": out_reserve or None,
                  "streaming": not nostream, "last_error": None, "last_error_at": None,
                  "fallback": {"enabled": fallback_on, "to": VLLM_MODEL, "count": 0, "last_at": None}}
    primary = mk(temperature, chat_mt, effort, timeout_s, _env_int("CHAT_LLM_MAX_RETRIES", 1), base_url=url, model=model,
                 api_key=key, nostream=nostream, cls=_ChatServerLLM,
                 chat_template_kwargs={"enable_thinking": thinking},
                 callbacks=[_ChatLLMWatch(info, "primary")], affinity=True)
    chat = primary
    if fallback_on:
        fb = mk(temperature, max_tokens, base_effort, timeout_s, base_retries, callbacks=[_ChatLLMWatch(info, "fallback")],
                cls=_VisionGuardLLM)
        chat = primary.with_fallbacks([fb], exceptions_to_handle=_chat_fallback_errors())
    cfg = {"url": url, "model": model, "key": key, "max_tokens": chat_mt, "output_reserve": out_reserve,
           "ctx_fallback": _env_int("CHAT_LLM_CONTEXT_TOKENS", 0) or _CTX_FALLBACK}
    print(f"[agent] 챗 전용 LLM — {info['server']} · {model} · 생각 {('켬(' + effort + ')') if thinking else '끔'} · "
          f"출력 상한 {f'{chat_mt:,} 토큰' if chat_mt else '서버 기본(보내지 않음)'} · 출력 몫 {out_reserve:,} 토큰 · "
          f"{'스트리밍' if not nostream else '비스트리밍'} · 넘김 {('→ ' + VLLM_MODEL) if fallback_on else '끔'}"
          " (띵킹 · 심의 준비 · 심의 · 카드 대조는 VLLM_*)")
    return chat, info, cfg


def _chat_llm_of(app_) -> object:
    """챗 흐름이 쓸 LLM — 챗 전용이 켜져 있으면 그것(넘김 포함), 아니면 app.state.llm(시험이 llm 만 꾸린 상태도 그대로 돈다)."""
    return getattr(app_.state, "chat_llm", None) or app_.state.llm


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the LLM once. Tools are loaded per request (they depend on the caller's groups),
    # so we keep the raw connection config and compile/cache a ReAct agent per group-set.
    # LLM_DISABLE_STREAMING=1: 토큰 스트리밍 대신 완결 응답(invoke) — vLLM 0.23.0의 GLM-5.2
    # 스트리밍 경로가 tool_calls 를 유실(빈 배열 + finish_reason:stop)하는 서빙 결함 우회.
    # 텍스트가 모델 턴 단위로 도착하는 대신 도구 호출이 정상 동작한다. dev(qwen)는 기본 0(스트리밍).
    disable_stream = os.environ.get("LLM_DISABLE_STREAMING", "0") == "1"

    # LLM 튜닝은 전부 env — 환경(dev qwen 16K vs 상암 GLM)마다 다른 값을 배포 없이 적용한다.
    # 미설정 시 temperature=0, max_tokens 미전송=무상한, effort 미전달. **시간 한도는 미설정이어도 걸린다.**
    # 파싱은 안전 파서(_env_*) — 오타 값이 lifespan 에서 서버 기동을 죽이지 않고 경고 후 기본값.
    #   LLM_TEMPERATURE      : 기본 0 (ReAct 도구호출 결정성)
    #   LLM_MAX_TOKENS       : 0/미설정=미전송. 설정 시 8192급 여유값 권장 — 2048~4096은
    #                          thinking 토큰과 겹치면 미완 JSON 절단을 유발하므로 금지.
    #   LLM_REASONING_EFFORT : 빈 값=미전달(RA 규약). GLM 계열은 반드시 extra_body 경유
    #                          chat_template_kwargs 로 전달 — 톱레벨 reasoning_effort 필드는
    #                          OpenAI 표준 파라미터로 나가므로 쓰지 않는다.
    #   LLM_TIMEOUT_S        : 챗(ReAct)·띵킹·심의 전 도우미가 쓰는 기본 LLM 의 호출 시도 1회 한도(초).
    #                          기본 900, 0=무제한. 종전 주석은 '미설정=라이브러리 기본 600s' 라고 적었지만
    #                          걸려 있지 않았다 — langchain-openai 가 timeout=None 을 명시로 넘겨 openai 의
    #                          기본값이 안 걸린다(실측: 요청에 실린 타임아웃 넷이 전부 None). 멈춘 호출 하나를
    #                          끝없이 기다렸다. 심의가 공유 LLM 을 차지한 동안에도 버티게 600 보다 넉넉히 둔다.
    #   LLM_CONNECT_TIMEOUT_S: LLM 서버에 **연결**하는 한도(초). 기본 10. 위 한도와 따로 준다 — float 하나를
    #                          주면 connect 도 그 값이라, read 를 30분~4시간으로 올리는 순간 죽은 서버에 붙으려는
    #                          시도도 그만큼 기다린다. 연결은 LLM 큐 대기와 무관하다(느린 일이 아니라 죽은
    #                          상대를 재는 값이라 짧게 둔다).
    #   LLM_MAX_RETRIES      : openai SDK 의 자동 재시도(타임아웃·연결 오류·429·5xx) 횟수. 기본 2(종전
    #                          라이브러리 기본값 그대로 — 이름만 붙였다).
    #   DELIB_*              : 심의 라운드 전용(temperature/max_tokens/effort/timeout/재시도).
    #                          챗 경로는 위 LLM_* 만 따르므로 심의 튜닝이 챗에 새지 않는다.
    #   DELIB_TIMEOUT_S      : 심의 LLM 호출 **시도 1회**의 한도(초). 기본 1800, 0=무제한. 좌석 발언·자유 조회
    #                          스텝·의장·요약이 전부 이 값을 쓴다. 심의 전체 시간이 아니다 — 심의에는 벽시계가
    #                          없다. 20석 넘는 패널이 공유 LLM 에 동시 요청을 20~44건 올리면 한 호출의 시계에
    #                          큐 대기가 들어가 600초는 짧았고, 미설정(종전 무제한)은 멈춘 호출 하나가 잡 자리를
    #                          영영 붙들었다. 한 심의에서만 더 필요하면 요청 timeout_s 로 올린다
    #                          (상한 DELIB_TIMEOUT_MAX_S). ⚠ LLM_TIMEOUT_S 를 물려받지 않는다 — 챗 한도를
    #                          줄인 박스에서 심의가 같이 줄었다.
    #   DELIB_LLM_MAX_RETRIES: 심의의 SDK 재시도 횟수. 기본 1. 타임아웃 재시도는 생성을 처음부터 다시 한다 —
    #                          느리지만 진행 중인 호출에는 긴 한 번이 짧은 세 번보다 낫고, 막힌 LLM 에 같은 긴
    #                          프롬프트를 세 번 넣으면 부하만 는다. 한 번은 순간 끊김·5xx 용으로 남긴다.
    #                          **바깥 한도 계산의 기준이다** — LLM 논리 호출 1회의 최악 = (1+재시도)×한도 + 대기
    #                          (기본 2×1800+8 = 3608초). 리스크 앱 패널 벽시계·포털 릴레이·nginx 침묵 한도가
    #                          이 값보다 커야 한다.
    import httpx  # noqa: PLC0415 — openai 가 이미 끌어오는 의존성이다. 여기서만 쓴다

    conn_to = _env_float("LLM_CONNECT_TIMEOUT_S", 10.0)

    def _mk_llm(temperature: float, max_tokens: int, effort: str, timeout_s: float = 0.0,
                retries: int = 2, *, base_url: str = "", model: str = "", api_key: str = "",
                nostream: bool | None = None, chat_template_kwargs: dict | None = None,
                callbacks: list | None = None, cls: type = ChatOpenAI, affinity: bool = False) -> ChatOpenAI:
        # 주소 · 모델 · 키를 주지 않으면 VLLM_* 다(챗 · 심의 클라이언트). 챗 전용 LLM(_build_chat_llm)만 다른 서버와 cls(_ChatServerLLM)를 준다.
        url = base_url or VLLM_BASE_URL
        kw: dict = dict(base_url=url, api_key=api_key or VLLM_API_KEY, model=model or VLLM_MODEL,
                        temperature=temperature, disable_streaming=disable_stream if nostream is None else nostream,
                        # read·write·pool 은 timeout_s(0 이하면 무제한), connect 는 따로 준다.
                        timeout=httpx.Timeout(timeout_s if timeout_s > 0 else None,
                                              connect=conn_to if conn_to > 0 else None),
                        max_retries=max(0, retries))
        if max_tokens > 0:
            kw["max_tokens"] = max_tokens
        tmpl = {**({"reasoning_effort": effort} if effort else {}), **(chat_template_kwargs or {})}
        if tmpl:
            kw["extra_body"] = {"chat_template_kwargs": tmpl}
        if callbacks:
            kw["callbacks"] = list(callbacks)
        if affinity:
            # 챗 전용 LLM 만 — 요청마다 대화 머리를 싣는다(_affinity_hook · W-1). SDK 의 기본 클라이언트 설정(제한 · 따라가기)은 그대로다.
            import openai  # noqa: PLC0415
            kw["http_async_client"] = openai.DefaultAsyncHttpxClient(event_hooks={"request": [_affinity_hook]})
        # 호출 추적(CALL_TRACE_URL)이 켜져 있을 때만 기록기를 붙인다 — 꺼져 있으면 그대로 돌려준다(call_trace.py).
        return _call_trace.instrument_llm(cls(**kw), _call_trace.server_label(url))

    base_t = _env_float("LLM_TEMPERATURE", 0.0)
    base_mt = _env_int("LLM_MAX_TOKENS", 0)
    base_eff = os.environ.get("LLM_REASONING_EFFORT", "")
    base_to = _env_float("LLM_TIMEOUT_S", 900.0)
    base_rt = _env_int("LLM_MAX_RETRIES", 2)
    app.state.llm = _mk_llm(base_t, base_mt, base_eff, base_to, base_rt)

    # 심의(라운드 토론) 전용 — **늘** 별도 인스턴스다. 종전엔 DELIB_* 가 하나도 없으면 챗 LLM 을 그대로 써서
    # '미설정 = 무제한' 이 심의에 걸렸다(dev 실측). 한도와 재시도 횟수가 챗과 다르므로 따로 만든다.
    d_t = _env_float("DELIB_TEMPERATURE", base_t)
    d_mt = _env_int("DELIB_MAX_TOKENS", base_mt)
    d_eff = os.environ.get("DELIB_REASONING_EFFORT", "") or base_eff
    d_to = _env_float("DELIB_TIMEOUT_S", 1800.0)
    d_rt = _env_int("DELIB_LLM_MAX_RETRIES", 1)
    app.state.delib_llm = _mk_llm(d_t, d_mt, d_eff, d_to, d_rt)
    # /health 의 sampling — 심의가 실제로 쓰는 생성 파라미터(리스크 앱이 패널의 모델 출처에 도장으로 남긴다 — 없으면 null 로 진행).
    # None 은 '보내지 않는다'(top_p 는 서버 기본값, max_tokens 는 상한 없음). seed 는 보내지 않아 싣지 않는다.
    app.state.delib_sampling = {"temperature": d_t, "top_p": None, "max_tokens": d_mt or None}
    print(f"[agent] LLM 한도 — 챗: 호출 1회 {base_to or '무제한'}초·재시도 {base_rt}회(LLM_TIMEOUT_S·"
          f"LLM_MAX_RETRIES) / 심의: 호출 1회 {d_to or '무제한'}초·재시도 {d_rt}회(DELIB_TIMEOUT_S·"
          f"DELIB_LLM_MAX_RETRIES) / 연결 {conn_to or '무제한'}초(LLM_CONNECT_TIMEOUT_S)")
    print(f"[agent] 심의 전용 LLM — temperature={d_t}, "
          f"max_tokens={d_mt or '미전송'}, effort={d_eff or '미전달'}")
    for _name, _v in (("LLM_TIMEOUT_S", base_to), ("DELIB_TIMEOUT_S", d_to)):
        if _v <= 0:
            print(f"[agent] ⚠ {_name}=0 — LLM 호출에 시간 한도가 없다. 멈춘 호출 하나를 끝없이 기다린다"
                  "(심의면 잡 자리를 영영 붙든다)")
    # 요청 단위 타임아웃 오버라이드(웹 토글)용 — 같은 temperature/max_tokens/effort/재시도로 timeout 만
    # 바꿔 재구성하는 팩토리(구성만·연결 없음). 기본 타임아웃도 노출해 오버라이드 필요 판정에 쓴다.
    app.state.delib_timeout_s = d_to
    app.state.mk_delib_llm = (lambda ts, _t=d_t, _m=d_mt, _e=d_eff, _r=d_rt: _mk_llm(_t, _m, _e, ts, _r))
    # 챗 전용 LLM(CHAT_LLM_*) — 비면 챗도 위 llm 을 쓴다(HWAXPortal docs/chat-llm-stcx). llm_nostream 은 챗 경로만 읽으므로
    # (_timed_events · _agent_stream) 챗 클라이언트의 값이다.
    app.state.chat_llm, app.state.chat_llm_info, app.state.chat_llm_cfg = _build_chat_llm(
        app.state.llm, _mk_llm, temperature=base_t, max_tokens=base_mt, timeout_s=base_to,
        base_effort=base_eff, base_retries=base_rt, base_nostream=disable_stream)
    _chat_ctx_cache.clear()
    app.state.llm_nostream = ((not app.state.chat_llm_info["streaming"]) if app.state.chat_llm_info.get("enabled")
                              else disable_stream)
    if _card_review is not None:
        _card_review.gate_of(app)    # 카드 대조 관문 — 평소 CARD_REVIEW_CONCURRENCY, 심의가 돌면 _WITH_DELIB(수만 센다)
    # 외부 응답자(EXTERNAL_LLM_ENABLED=1) — 과제 줄을 기동 때 만들고(설정을 한 번 읽는다) 시간 한도 사슬을 적는다. 꺼져 있으면 없다.
    _ext_board = _external_llm.board_of(app)
    if _ext_board is not None:
        _x_read, _x_tries = _llm_limit(app.state.delib_llm)
        print(_external_llm.startup_note(_ext_board.settings, _x_read, _x_tries))
    if disable_stream:
        print("[agent] LLM_DISABLE_STREAMING=1 — 토큰 스트리밍 비활성(도구호출 우선 모드)")
    if _call_trace.startup_note():
        print(_call_trace.startup_note())
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
    if DELIB_HEARTBEAT_S <= 0:
        # 종전 문구는 '2×DELIB_TIMEOUT_S 보다 커야' 였다 — LLM 호출 한 번의 최악이다. 좌석 발언 하나는 호출을
        # (1 + 파싱 재시도)번 잇고 그 사이에 스트림이 조용하다(_quiet_calls). 그 글대로 nginx 를 2시간에 맞춘
        # 박스는 마지막 좌석이 재시도 중일 때 살아 있는 구독이 끊긴다. 이 박스에 걸린 값으로 잰 수를 적는다
        # (진행 조회의 quiet_ok_s 와 같은 식). 호출 한도까지 꺼져 있으면 수를 지어내지 않는다.
        _read, _tries = _llm_limit(app.state.delib_llm)
        _calls = _quiet_calls(_resolve_opts(None))
        print("[agent] ⚠ DELIB_HEARTBEAT_S=0 — 심의 SSE heartbeat(ping)가 꺼졌다. 좌석 발언 하나가 끝날 때까지 "
              "스트림이 조용하고, 그 발언은 LLM 호출을 (1 + 파싱 재시도)번 잇는다(DELIB_PARSE_RETRIES — 지금 "
              f"설정으로 {_calls}번). 바깥 침묵 한도 셋(포털 AGENT_STREAM_IDLE_TIMEOUT_S · nginx "
              "NGINX_AGENT_READ_TIMEOUT · 리스크 앱 HWAXRISK_ENGINE_READ_TIMEOUT_S)"
              + (f"이 {_calls}×({_tries}×DELIB_TIMEOUT_S+8) = {_calls * (_read * _tries + 8):,.0f}초보다 커야 "
                 "살아 있는 심의의 구독이 끊기지 않는다(자유 조회 단계와, timeout_s·parse_retries 를 올려 청한 "
                 "심의는 그보다 길다)" if _read > 0 and _tries > 0 else
                 " 어느 것이든 살아 있는 심의의 구독을 끊을 수 있다 — 호출 한도도 꺼져 있어(DELIB_TIMEOUT_S=0) "
                 "그 침묵에 끝이 없다"))
    # 심의 MCP(/mcp) — streamable_http_app 은 자체 lifespan(task group)이 있어야 동작하는데
    # FastAPI 의 mount() 는 하위 앱 lifespan 을 전파하지 않는다. 여기서 명시적으로 연다.
    # 실패해도 서버는 뜬다 — 심의 MCP 는 부가 진입점이고, 웹(/chat) 경로는 이것과 무관하다.
    if _DELIB_MCP is not None:
        _delib_mcp_module.bind(app)
        try:
            async with _DELIB_MCP.router.lifespan_context(_DELIB_MCP):
                print("[agent] deliberation MCP mounted at /mcp")
                try:
                    yield
                finally:
                    # 내려가는 중 — 곧 도는 심의가 전부 취소되며 자리가 난다. 그 자리에 줄 선 심의를
                    # 띄우지 않게 먼저 닫는다(띄우면 뜨자마자 죽고 결정문 없는 done 으로 남는다).
                    _delib_mcp_module.unbind()
                    await _stop_routines()
            return
        except Exception:  # noqa: BLE001
            logging.getLogger("agent").exception("[agent] 심의 MCP lifespan 실패 — /mcp 없이 계속")
    try:
        yield
    finally:
        await _stop_routines()


async def _stop_routines() -> None:
    """내려가는 중 — 살아 있는 분석 루틴 작업을 멈춘다(원장은 running 으로 남는다 — 사람 PAT 가 사라지므로 사람이 '이어서', R3-D8)."""
    try:
        import routine  # noqa: PLC0415 — routine 이 app 을 늦게 읽는다(순환 import 회피)
        await routine.shutdown()
    except Exception:  # noqa: BLE001 — 내려가는 길을 막지 않는다
        logging.getLogger("agent").exception("[agent] 분석 루틴 작업 정리 실패")


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
# 카드 대조(리스크 심사 재설계 M2 — /card-review/{limits,pack,plan,run}). import 가 실패해도 기동은 계속하고 /health 의
# capabilities 에서 빠지며 사유를 싣는다(X2-15) — 챗·심의는 이 모듈과 무관하다. 리스크 앱은 capabilities 를 보고 판을 가른다.
try:
    import card_review as _card_review
    app.include_router(_card_review.router)
    _CARD_REVIEW_ERR = ""
except Exception as _exc:  # noqa: BLE001
    logging.getLogger("agent").warning("[agent] 카드 대조 비활성 — %r", _exc)
    _card_review = None
    _CARD_REVIEW_ERR = f"{type(_exc).__name__}: {_exc}"[:300]
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatRequest(BaseModel):
    message: str
    # 서버 대화 id(포털이 싣는다 — 없으면 빈 값). 챗 LLM 호출의 대화 머리(W-1)에만 쓴다.
    conversation_id: str = ""
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
    # 호출자 소속 id — 포털이 원장에서 채운다(권한표에 없는 id 는 빈 값). 위키에 남기는 심의(delib_opts.wiki)의 범위(aff:<소속>)다 —
    # 비면 후보로 보낸다(HWAXPortal docs/mxwp-wiki P8). 요청 손잡이가 아니라 신원이라 delib_opts 에 두지 않는다.
    affiliation: str = ""
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
    # 이 발화에 올린 그림 — [{name, mime, data(base64 또는 data URL)}]. 아티팩트로 저장해 'user_images' 사건으로 주소를 돌려주고, 챗 서버가
    # 그림을 받으면 이 발화의 그림 조각으로 싣는다(HWAXPortal docs/chat-depth-context T5). 이력 항목은 주소(images)로 다시 온다.
    images: list[dict] = []
    # 요청 시점 권한(feat:·plat:) — 포털이 원장으로 계산해 준다(HWAXPortal docs/access-control).
    # 심의·Thinking 트리거 판정은 여기 있으므로 포털이 아니라 이쪽이 막는다. None 은 권한을 모르는
    # 옛 호출이라 막지 않는다(포털은 늘 보낸다) — '안 보냄'과 '권한 없음'을 구분해야 해서다.
    entitlements: list[str] | None = None
    # 분석 루틴(HWAXPortal docs/analysis-routines R3) — {id, version?, inputs}(새 실행) 또는 {run_id}(이어서 · 다시 붙기). delib_opts 처럼 원 사전으로
    # 받고 실행기(routine.py)가 검사한다. 포털 ChatRequest 에도 같은 칸이 있어야 한다 — 어느 한쪽이라도 빠지면 조용히 버려져 일반 챗이 돈다.
    routine: dict | None = None


# 기능 권한 이름(사람용) — 포털 access.yaml 의 label 과 같다.
_FEATURE_LABEL = {"feat:deliberation": "전문가 심의", "feat:thinking": "Thinking",
                  "feat:expert-chat": "전문가와 대화", "feat:routines": "분석 루틴"}


def _denied_feature(req: "ChatRequest") -> str | None:
    """이 요청이 쓰려는 기능 중 권한이 없는 것. 트리거 순서는 chat() 과 같다."""
    if req.entitlements is None:
        return None
    have = set(req.entitlements)
    m = req.message
    if req.routine is not None:
        need = "feat:routines"          # 루틴 가지가 맨 앞이다(chat()) — 그 턴은 슬래시 · 띵킹을 보지 않는다
    elif is_sim_deliberation(m) or is_test_plan(m) or is_deliberation(m):
        need = "feat:deliberation"
    elif is_report_save(m):
        need = None                     # chat() 은 /보고서 를 전문가 검색보다 먼저 저장으로 보낸다 — 결론 글의 '전문가 추천' 은 검색이 아니다
    elif is_agent_search(m, pinned=bool(_persona_keys(req.pinned_agents, req.pinned_agent))):   # chat() 과 같은 판정이어야 한다(HA-31)
        need = "feat:expert-chat"
    elif (is_thinking(m) or req.thinking) and not _confirm_reply(req):   # 확인 카드의 답은 chat() 처럼 일반 챗이다(HA-29 · C-10)
        need = "feat:thinking"
    else:
        need = None
    if need and need not in have:
        return need
    if req.pinned_agent and "feat:expert-chat" not in have:
        return "feat:expert-chat"
    return None


def _delib_pause_info() -> dict | None:
    """새 심의 멈춤(R1)이 켜져 있으면 그 상태(retry_after_s 포함), 아니면 None. 잡 모듈을 못 붙이면 None 이다(멈춤은 운영 손잡이다 — 못 읽어
    막지 않는다. 깃발 판정 규칙은 review_gate.DrainState)."""
    try:
        import delib_jobs  # noqa: PLC0415 — /health 와 같다(심의 MCP 가 비활성이어도 답한다)
        return delib_jobs.pause_info()
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] 새 심의 멈춤을 읽지 못했다({type(exc).__name__}) — 꺼짐으로 읽는다")
        return None


async def _paused_stream(info: dict) -> AsyncIterator[bytes]:
    """새 심의 멈춤의 답 — 엔진의 다른 거절과 같은 꼴(error 다음 done). 포털 화면은 error 의 글을 그대로 보인다."""
    import delib_jobs  # noqa: PLC0415
    yield _sse("error", {"code": "delib_paused", "message": delib_jobs.pause_message(info),
                         "retry_after_s": info["retry_after_s"], "infra": True, "retryable": True})
    yield _sse("done", {})


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
# 분리 태스크마다 누가 띄웠나(human | app — delib_opts.started_by, M3.md #26). 카드 대조 관문의 절반 판정만 읽는다.
_DETACHED_BY: dict = {}
# 심의 SSE heartbeat 간격(초), 0=끔. 이벤트가 없는 동안에도 이 간격으로 `event: ping` 을 흘린다.
# 심의는 LLM 호출 한 번(의장 결정문·좌석 발언·요약)이 도는 동안 스트림에 바이트가 0 이다 — 그 침묵이
# 바깥의 침묵 한도(포털 릴레이·nginx /agent/·리스크 앱 읽기 한도·사내 프록시)를 넘기면 **살아 있는**
# 심의의 구독이 끊긴다. LLM 한도(DELIB_TIMEOUT_S)를 넉넉히 올릴 수 있는 것은 이 ping 이 있어서다.
# 이벤트 이름은 ping 으로 고정한다 — 이 스트림을 읽는 셋(포털 릴레이 파서 · 포털 프론트 dispatch ·
# 리스크 앱 collect_stream)이 모르는 이름을 버린다(status 로 보내면 리스크 앱 events[] 400칸을 채운다).
# 끄면(0) 바깥 침묵 한도 셋이 좌석 발언 하나의 침묵보다 커야 한다 — LLM 논리 호출 1회(재시도 포함 약
# 2×DELIB_TIMEOUT_S)가 아니라 그것의 (1 + 파싱 재시도)배다(기본 3×3,608초. 기동 경고가 그 박스의 수를 적는다).
DELIB_HEARTBEAT_S = _env_float("DELIB_HEARTBEAT_S", 15.0)


def _detach_stream(gen, label: str, started_by: str = "human"):
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
    _DETACHED_BY[task] = started_by
    task.add_done_callback(_DETACHED_TASKS.discard)
    task.add_done_callback(lambda t: _DETACHED_BY.pop(t, None))

    async def _subscribe():
        # ⚠ q.get() 을 wait_for 로 감싸지 않는다. wait_for 는 만료 때마다 안쪽 get 을 **취소**하고, 취소와
        #   도착이 겹친 프레임이 살아남는지는 asyncio 구현에 달렸다(이 venv 는 Python 3.10 이다). 사본에서
        #   박자를 맞춰 흘려서는 잃는 것을 재현하지 못했지만, 그 프레임이 결정문일 수 있어 구현에 기대지
        #   않는다 — get 태스크를 **쥔 채** asyncio.wait(timeout) 으로 기다리고(wait 는 취소하지 않는다),
        #   만료되면 ping 만 내고 같은 태스크를 다시 기다린다.
        #   생성기(_drive)는 감싸지 않는다 — 거기에 시간 한도를 걸면 진행 중인 LLM 호출이 취소된다.
        get, last = None, time.monotonic()
        try:
            while True:
                if get is None:
                    get = asyncio.ensure_future(q.get())
                if DELIB_HEARTBEAT_S > 0:
                    done, _pending = await asyncio.wait({get}, timeout=DELIB_HEARTBEAT_S)
                    if not done:
                        # 내용 없는 프레임 — idle_s 는 마지막 **진짜** 프레임 뒤 경과(화면이 '마지막 진행
                        # N초 전' 을 그린다), ts 는 이 파일의 다른 이벤트와 같은 epoch 밀리초다.
                        yield _sse("ping", {"idle_s": int(time.monotonic() - last),
                                            "ts": int(time.time() * 1000)})
                        continue
                chunk, get = await get, None
                last = time.monotonic()
                if chunk is None:
                    break
                yield chunk
        except asyncio.CancelledError:
            logging.getLogger("agent").warning(
                "[detach:%s] SSE 구독 절단 — 심의는 백그라운드에서 계속 완주한다(저장 포함)", label)
            raise
        finally:
            if get is not None and not get.done():
                get.cancel()     # 구독이 끊겼다 — 기다리던 get 만 접는다(큐의 프레임은 그대로 남는다)

    return _subscribe()


def _pinged_stream(gen):
    """챗 · 띵킹 · 도우미 스트림에도 DELIB_HEARTBEAT_S 마다 ping(HWAXPortal docs/request-1029 P-1 ④) — 생각 · 비스트리밍 LLM 호출 · 긴 도구가
    조용한 동안 바깥 침묵 한도(포털 챗 중계 AGENT_CHAT_IDLE_TIMEOUT_S, 900초)에 살아 있는 답이 끊기지 않게. 프레임 꼴은 심의 ping 과 같다.
    턴은 한 태스크가 끝까지 돌린다 — 걸음마다 다른 태스크로 돌리면 턴 안에서 건 ContextVar(_external_llm.ctx_override 등)가 다음 걸음에
    사라진다. 심의(_detach_stream)와 달리 구독이 끊기면 그 태스크도 취소하고 정리를 기다린다 — 챗 중지는 중지다(C-5). 심의 수(/health
    delib_active)에 세지 않는다. 턴의 예외는 끝에서 그대로 올린다(종전처럼 스트림이 끊긴다)."""
    if DELIB_HEARTBEAT_S <= 0:
        return gen

    async def _sub():
        q: asyncio.Queue = asyncio.Queue()

        async def _drive():
            try:
                async for chunk in gen:
                    q.put_nowait(chunk)
            finally:
                q.put_nowait(None)

        task = asyncio.create_task(_drive(), name="chat-turn")
        get, last = None, time.monotonic()
        try:
            while True:
                if get is None:
                    get = asyncio.ensure_future(q.get())
                done, _pending = await asyncio.wait({get}, timeout=DELIB_HEARTBEAT_S)
                if not done:
                    yield _sse("ping", {"idle_s": int(time.monotonic() - last), "ts": int(time.time() * 1000)})
                    continue
                chunk, get = await get, None
                if chunk is None:
                    break
                last = time.monotonic()
                yield chunk
            await task
        finally:
            if get is not None and not get.done():
                get.cancel()
            if not task.done():
                task.cancel()            # 구독이 끊겼다(중지 · 탭 닫기) — 턴도 멈춘다
            await asyncio.gather(task, return_exceptions=True)

    return _sub()


# 도구 결과 절단(문자) — 컨텍스트 보호용 안전밸브. 무제한이면 대량 조회(VOC 수천 건)가
# 프롬프트를 넘겨 'maximum context length' 로 챗 전체가 죽는다(dev 16K 실측).
# 다만 6000 은 dev(16K) 기준값이라 대형 컨텍스트 모델(prod GLM)에 그대로 쓰면 목록이
# 불필요하게 잘린다 → 기본을 32000 으로 올리고, 소형 모델 박스만 .env 로 낮춘다.
# 0 이면 무제한(권장 안 함 — 안전밸브 해제).
# 32000 도 부족했다: recommend_agents(top_k=40) 한 번이 53KB 다(실측). prod 는 GLM(대형 컨텍스트)
# 이므로 기본을 200000(≈190K 토큰, 1백만 토큰 창의 약 1/5)으로 둔다 — 소형 모델 박스만 .env 로 낮춘다.
TOOL_RESULT_MAX = int(os.environ.get("TOOL_RESULT_MAX", "200000"))
# 도구 결과를 _cap 이 잘랐다는 표지 — 모델에게 가는 글이고, 챗 스트림은 이 표지를 보고 상태줄로도 알린다.
_CAP_MARK = "…[경고: 도구 출력이"

# 도구 호출이 **실패했다**는 기계 표지. 예외는 _cap_tool 이 문자열로 바꿔 돌려주므로
# (그래야 LLM 이 스키마를 보고 교정한다) 스트림에서는 성공과 구분이 안 됐다. 이 표지가
# 그 판단을 실어 나른다 — 절차 원장의 `ok` 가 여기서 나온다(PLAN §9-9 ③).
_TOOL_FAIL_MARK = "\u2716"   # ✖ — 사람 눈에도 보이고 기계도 찾는다
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
# 앞 턴에서 도구가 조회한 결과(포털 ChatHistoryMessage.evidence)에 주는 이력 몫의 위쪽 — 근거가 없거나 적으면 글이 그 자리를 쓴다.
# 종전엔 이력이 글만이라 다음 턴이 '아까 조회한 그 값' 을 모르고 다시 조회하거나 지어냈다(HWAXPortal docs/chat-depth-context CD-D5).
HIST_EVIDENCE_SHARE = float(os.environ.get("HIST_EVIDENCE_SHARE", "0.5"))


def _cap(text, limit=None):
    limit = TOOL_RESULT_MAX if limit is None else limit
    s = text if isinstance(text, str) else str(text)
    # 0 이면 무제한이다(위 주석의 약속). 종전엔 `limit or TOOL_RESULT_MAX` 라 0 이 0자 절단 + 경고가 됐다.
    if limit <= 0 or len(s) <= limit:
        return s
    # 잘린 사실과 대처를 모델이 오해 없이 알도록 명시 — '이게 전부'로 착각해 잘못 답하는 것 방지.
    return s[:limit] + (
        f"\n{_CAP_MARK} {len(s):,}자였고 {limit:,}자에서 잘렸습니다. 이건 전체가 아닙니다. "
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
# 이번 턴에 도구마다 부른 수(노출 이름 → 횟수) — 같은 도구를 네 번째 부르면 결과에 '앞 결과를 써라' 를 붙인다(HWAXPortal docs/request-1029 W-5).
# 10-29 QA 에서 objects_summary 를 한 턴에 7번(셋은 조건 주소 오류) · failuretrace_objects_summary 를 10번(각 약 10만 자) 불렀다.
_turn_tool_calls: contextvars.ContextVar = contextvars.ContextVar("turn_tool_calls", default=None)
_TOOL_REPEAT_NOTE_AT = 4
# 이번 턴의 상태 기다림(HWAXPortal docs/request-1030 HA-28) — status_wait.new_turn() 의 {born, seen, spent_s}. None 이면(챗 턴 밖 · 기능 꺼짐)
# 기다리지도 잡 id 를 배우지도 않는다. 도구 코루틴은 복사된 문맥에서 같은 사전을 고친다(_turn_ids 와 같은 길).
_turn_status_wait: contextvars.ContextVar = contextvars.ContextVar("turn_status_wait", default=None)
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


def _turn_ids_max() -> int:
    """출처 정수 집합의 상한 — 좁은 창은 종전 2,000개, 넓은 창은 창의 토큰 수(한 턴이 볼 수 있는 정수가 그보다 많을
    수 없다). 종전엔 1백만 토큰 창에서도 2,000개라, 긴 이력의 씨앗만으로 차서 그 턴에 도구가 돌려준 ID 를 못 배웠다 —
    다음 호출이 그 ID 를 쓰면 게이트가 '출처 없다' 로 막았다."""
    return max(_TURN_IDS_MAX, _model_context_tokens()) if _wide() else _TURN_IDS_MAX


def _learn_ids(text) -> None:
    """도구 결과에 실린 정수를 이번 턴의 '출처 있는 값'으로 등록한다 — list_materials 가 준
    id 로 다음 호출이 통과되게 하는 경로다. ⚠ 부모(on_tool_end)가 아니라 자식(도구 코루틴)
    쪽에서 넣는다. 부모에 두면 병렬 호출 시 순서 경합으로 정상 ID 가 차단될 수 있다."""
    src = _turn_ids.get()
    if src is None or len(src) >= _turn_ids_max():
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


def _finish_reason(msg) -> str:
    """모델 응답의 finish_reason — response_metadata·additional_kwargs 어느 쪽에 와도 읽는다(심의 _llm_text 와 같다)."""
    try:
        meta = getattr(msg, "response_metadata", None) or {}
        return str(meta.get("finish_reason")
                   or (getattr(msg, "additional_kwargs", None) or {}).get("finish_reason") or "")
    except Exception:  # noqa: BLE001 — 검출 실패가 챗을 죽이면 안 된다
        return ""


def _resume_part(part: str) -> str:
    """이어하기에 싣는 '끊긴 지점까지 나온 내용' — 좁은 창은 종전 앞 2,000자(원문 길이 표식), 넓은 창은 전부."""
    return _cut_mark(part, _narrow_cap(2000))


def _rescue_excerpt(turn_out: list) -> str:
    """빈 응답 구제에 주는 도구 결과 발췌 — 좁은 창은 종전 앞 12,000자, 넓은 창은 챗 이력 예산(창에서 유도)까지.
    종전엔 1백만 토큰 창에서도 12,000자라, 큰 조회 뒤의 구제 답이 결과의 앞 한 토막만 보고 쓰였다. 넓은 창의 길이는
    실제로 싣는 발췌 전체로 다시 잰다(_fit_tokens) — 앞 20,000자(JSON 머리)의 밀도로만 환산해 뒤가 한국어인 결과에서
    창의 세 배를 실을 수 있었다."""
    joined = "\n".join(turn_out)
    if not _wide():
        return joined[:12000].strip()
    cap = max(12000, _hist_budget_chars(joined[:20000]))
    return _fit_tokens(lambda c: joined[:c], cap, _hist_budget_tokens(), 12000)[0].strip()


# 보조 호출(화두 뽑기·되묻기·좌석 축)이 읽는 대화 몫 — 넓은 창에서 창의 이 비율(1백만 토큰 창 약 55,000자). 이 호출들은
# 대화 뒷부분의 쟁점만 보면 되는데 종전엔 챗 이력 예산 전부(약 88만 자)를 받아, 긴 대화에서 '심의로 넘기기' 한 번에 세
# 호출이 각각 수십만 토큰을 프리필했다 — 심의가 같은 LLM 을 쓰는 동안이면 포털의 600초 한도에 걸려 화두가 기본값,
# 되묻기가 '묻지 않음', 축이 [] 로 내려앉는다. 좁은 창은 종전 고정값(floor)이다.
_AUX_CTX_SHARE = 0.05


def _aux_budget(floor: int, sample: str = "") -> int:
    """대화를 읽는 보조 LLM 호출(화두 뽑기·되묻기·좌석 축)의 대화 몫(자) — 좁은 창은 종전 고정값(floor), 넓은 창은
    창의 _AUX_CTX_SHARE(챗 이력 예산을 넘지 않고, 바닥 floor). 뺀 것은 호출부가 프롬프트에 적는다(_aux_omit_note)."""
    if not _wide():
        return floor
    share = _chars_for_tokens(sample or "가", int(_model_context_tokens() * _AUX_CTX_SHARE))
    return max(floor, min(_hist_budget_chars(sample), share))


def _recent_lines(history: list, budget: int, line_cap: int, fmt, stats: dict | None = None) -> list[str]:
    """대화를 **최근부터** 예산 안에 담아 시간 순으로 돌려준다. 종전 셋(화두·되묻기·좌석 축)은 오래된 쪽부터 예산을
    깎아, 긴 답 하나가 앞에 있으면 그 뒤(진짜 쟁점이 있는 자리)가 통째로 빠졌다. line_cap 은 줄 하나의 상한(0=무제한,
    끊으면 원문 길이 표식)이고 예산은 실은 길이로 깎는다. fmt(m, 글) 이 한 줄을 만든다. 남은 예산보다 긴 줄은 원문
    길이를 적고 남은 예산까지 줄여 싣는다(deliberation._fit_recent) — 종전엔 첫 줄(가장 최근)이면 예산과 상관없이 통째로
    실렸고 아니면 버려졌다. stats 를 주면 {"omitted": 싣지 않은 오래된 발화 수} 를 채운다."""
    msgs = [m for m in (history or []) if isinstance(m, dict) and str(m.get("content") or "").strip()]
    lines, _left, omitted = _fit_recent([str(m.get("content")).strip() for m in msgs], budget, line_cap,
                                        lambda i, t: fmt(msgs[i - 1], t))
    if stats is not None:
        stats["omitted"] = omitted
    return lines


def _aux_omit_note(tag: str, skipped: int, stats: dict, budget: int) -> str:
    """보조 호출 프롬프트에 붙이는 '뺀 대화' 한 줄 — 건수 창(최근 N건) 밖 skipped 와 예산 밖 stats["omitted"] 의 합.
    서버 로그에도 남긴다. 뺀 것이 없으면 빈 문자열."""
    n = max(0, skipped) + int(stats.get("omitted") or 0)
    if not n:
        return ""
    print(f"[{tag}] 앞선 대화 {n}건을 싣지 않았다(보조 호출 몫 {budget:,}자)")
    return f"(앞선 대화 {n}건은 싣지 않았다 — 최근 대화만 읽는다, 몫 {budget:,}자)"


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
        _lab = _app_label_cached(name)          # 어느 앱의 결과인지(R-10 — 접두 없는 이름을 다른 앱 것으로 읽었다)
        lines.append(f"- `{name}`" + (f"({_lab})" if _lab else "") + (f" · {args}" if args else ""))
    if len(calls) > 8:
        lines.append(f"- … 외 {len(calls) - 8}건")
    if unsourced:
        # 보이는 것은 6건까지다(경고가 답변을 덮지 않게). 대신 **총 건수**를 적는다 — 심의 결정문과 같은
        # 표기다. 종전엔 세는 쪽이 6건에서 멈춰, 몇 건이 출처 없는 값인지 블록만 봐서는 알 수 없었다.
        _cnt = f"(표시 6건 · 총 {len(unsourced)}건)" if len(unsourced) > 6 else ""
        lines.append("")
        lines.append("> ⚠ 다음 수치는 위 조회 결과에서 확인되지 않았습니다 — "
                     f"추론이거나 계산된 값일 수 있으니 그대로 인용하지 마십시오{_cnt}: "
                     + ", ".join(f"`{v}`" for v in unsourced[:6]))
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


# ── 다른 허브의 id(HWAXPortal docs/request-1030 HA-30 · design-ha30) ────────────────────────────────────────────────────────
# 허브(자주검증 Datahub · FailureTrace · CAE 그룹 Datahub — 같은 틀의 설치 셋)의 객체 id 는 설치마다 따로 만든 UUID 라 같은 객체도 허브마다 id 가
# 다르고 key 만 같다. 10-30 OT-07 에서 failuretrace_graph_neighbors 에 자주검증 허브의 id 를 넘겨 APP-GRAPH-0001 이었다(10-27 N-13 도 같은 꼴) —
# 코어 규칙 ⑤('key 로 그 허브에서 다시 resolve')는 프롬프트 글뿐이라 두 번 다 지켜지지 않았고, 유령 ID 막이는 정수만 봐 UUID 글은 처음부터 지났다.
# 그래서 이 턴에 허브 도구가 성공으로 돌려준 결과에서 UUID → 허브를 배우고(_learn_hub_ids), 허브 도구 인자 어디에든(칸 이름 · 깊이와 상관없다 —
# conditions 안 · '|' 로 이은 값도) 다른 허브에서만 본 UUID 가 있으면 보내지 않고 대상 허브의 object_resolve(type_slug, key) 로 다시 풀라고
# 돌려준다(_cross_hub_id_msg). 코드가 대신 다시 풀지 않는다 — key · type 을 모르는 id, 다른 type slug, candidates(규칙 ①), 쓰기 도구의 말없는
# 대상 바꾸기, 근거 · 활동 · 감사에 안 보이는 숨은 호출이 걸린다(설계 5절).
# 끄는 손잡이 CHAT_XHUB_ID_GATE=0 — 턴 상태를 None 으로 둬 종전과 글자 하나 다르지 않다. 기동 때 읽는다(바꾸면 재기동, CHAT_STABLE_PREFIX 와 같다).
_XHUB_GATE = os.environ.get("CHAT_XHUB_ID_GATE", "1").strip() != "0"
# 이번 턴에 허브 결과에서 본 UUID — {uuid(소문자): {"apps": {허브 키}, "hub": 처음 본 허브, "tool": 처음 낸 도구, "key", "type", "label"}}.
# None 이면(심의 · 카탈로그 · 루틴 · 띵킹 · 턴 밖의 시험) 배우지도 막지도 않는다(유령 ID 막이와 같은 fail-open). 변하는 dict 하나라 병렬 도구 호출의
# 자식 문맥에서 더한 것이 같은 턴의 다음 호출에 보인다(_learn_ids 와 같은 길).
_turn_hub_ids: contextvars.ContextVar = contextvars.ContextVar("turn_hub_ids", default=None)
# 코어 어휘표가 없거나 깨졌을 때의 허브 표(키 → 라벨) — 어휘표의 canonical 과 role sibling 이다. role staging(시험 사본)은 허브가 아니다(도구 · id 꼴이 다르다).
_HUB_APPS_DEFAULT = {"rootdesigndatahub": "자주검증 Datahub(rootdesign)", "failuretrace": "FailureTrace",
                     "caegroupdatahub": "CAE 그룹 Datahub"}
_UUID_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
_HUB_IDS_MAX = 20_000        # 한 턴에 적는 id 상한 — 넘으면 새 id 는 모르는 id 가 되어 지나간다(이미 있는 id 에 허브를 더하는 것은 늘 한다)
_HUB_TAG_DEPTH = 12          # 이름표(key · type · label)를 찾는 JSON 깊이
_HUB_TAG_CHARS = 80          # 글에 싣는 이름표 한 칸의 길이
_XHUB_SHOW = 3               # 막은 글에 한 줄씩 보이는 id 수 — 나머지는 수만 적는다
# 막은 글의 표지 — 챗 스트림이 이것으로 막은 호출을 blocked 로 센다(design-integration C-6). 유령 ID 막이의 _PHANTOM_ID_MARK 는 싣지 않는다 —
# 그것은 심의가 '실행되지 않은 호출' 로 읽는 다른 계약이다.
_XHUB_ID_MARK = "는 다른 허브의 id 다"
# /health 의 xhub_gate — 기동 뒤 막은 호출 수(design-integration C-8). /health 는 스레드 풀에서, 막이는 이벤트 루프에서 돈다(_HUB_FOLD_LOCK 과 같다).
_XHUB_LOCK = threading.Lock()
_XHUB_STAT: dict = {"blocked": 0}


def _uuids(text) -> list[str]:
    """글의 UUID — 소문자로 맞추고 처음 나온 차례로 겹침을 뺀다."""
    s = text if isinstance(text, str) else str(text)
    return list(dict.fromkeys(m.lower() for m in _UUID_RE.findall(s)))


def _hub_apps() -> dict:
    """허브 키 → 라벨 — 코어 어휘표의 hubs(canonical · role sibling — 박스 덧씌우기로 sibling 을 더하면 커밋 없이 는다). 어휘표가 없거나 깨졌으면 기본표."""
    return (_core_vocab() or {}).get("hubs") or _HUB_APPS_DEFAULT


def _hub_of(name) -> str:
    """도구 노출 이름 → 허브 키(허브가 아니면 빈 값). 게이트웨이 /tools-map 캐시만 본다 — 도구 호출마다 부르므로 묻지 않는다(_app_label_cached 와
    같다). 맵에 있으면 그 백엔드가 허브일 때만 그 키, 없으면(별칭 · 캐시가 빈 때) 이름이 '<허브 키에서 - 를 뺀 것>_' 로 시작하는지로 가른다 — 세
    허브는 도구 이름이 같아 게이트웨이가 그 접두를 단다(_aggregate)."""
    n = str(name or "").strip()          # 게이트웨이도 invoke_tool 의 name 을 strip() 해서 찾는다 — 앞 공백 하나로 막이를 비켜 갔다(반대 검토 2차)
    hubs = _hub_apps()
    key = (_TOOLS_MAP_CACHE.get("map") or {}).get(n)
    if key:
        return key if key in hubs else ""
    low = n.lower()
    return next((h for h in hubs if low.startswith(h.replace("-", "") + "_")), "")


def _hub_tool(hub: str, base: str) -> str:
    """대상 허브의 그 도구 노출 이름(돌려주는 글에 싣는다) — 접두 이름이 맵에 있으면 그것, 맵의 맨 이름이 그 허브 것이면 맨 이름(허브 하나 박스),
    둘 다 아니면 접두 이름(게이트웨이 별칭이라 invoke_tool 로 늘 닿는다)."""
    m = _TOOLS_MAP_CACHE.get("map") or {}
    pre = f"{hub.replace('-', '')}_{base}"
    if pre in m:
        return pre
    return base if m.get(base) == hub else pre


def _hub_id_meta(text: str) -> dict:
    """허브 결과 글의 이름표 — {uuid: {"key", "type", "label"}}. JSON 으로 읽힐 때만 얻는다(접기 머리 줄은 떼고 읽는다). 두 꼴만 본다 — {id,
    key|label, type_slug}(resolve · 목록 · 검색 · 그래프 노드)와 {object_id, object_key|object_label, object_type_slug}(object_get 의 관련 객체).
    처음 본 것을 남긴다. 못 읽으면(상한에서 잘린 글 · JSON 이 아닌 글) 빈 사전이고 id 는 이름표 없이 적힌다. 글에 실을 값이라 공백을 한 칸으로
    줄이고 '"' 를 "'" 로 바꾸고 _HUB_TAG_CHARS 자로 자른다."""
    s = text.split("\n", 1)[1] if text.startswith("[포털 챗이 허브 응답을 접었다") and "\n" in text else text
    try:
        data = json.loads(s)
    except ValueError:
        return {}
    out: dict = {}

    def clip(v) -> str:
        return " ".join(v.split()).replace('"', "'")[:_HUB_TAG_CHARS] if isinstance(v, str) else ""

    def walk(v, depth: int) -> None:
        if depth > _HUB_TAG_DEPTH:
            return
        if isinstance(v, dict):
            for pre in ("", "object_"):
                u = v.get("id" if not pre else "object_id")
                if isinstance(u, str) and _UUID_RE.fullmatch(u.strip()) and u.strip().lower() not in out:
                    key, label = clip(v.get(pre + "key")), clip(v.get(pre + "label"))
                    if key or label:
                        out[u.strip().lower()] = {"key": key, "type": clip(v.get(pre + "type_slug")), "label": label}
            for x in v.values():
                walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v:
                walk(x, depth + 1)
    walk(data, 0)
    return out


def _learn_hub_ids(name, text, carried=()) -> None:
    """이번 턴에 허브 도구가 성공으로 돌려준 결과의 UUID 를 '어느 허브에서 본 id 인가' 로 적는다. name 은 경유면 안쪽 이름, text 는 모델이 보는 글
    (접기 · 자르기 · 인자 힌트 뒤 — 모델이 보지 않은 id 는 배우지 않는다). 실패 결과(✖ · 오류 봉투 · 막은 글)에서는 배우지 않는다 — 오류 글은 받은
    id 를 되풀이해, 배우면 그 허브 것으로 잘못 적는다. carried 는 이 호출이 인자로 실어 보낸 UUID 다 — 성공 꼴의 '없다' 답도 받은 id 를
    되풀이한다(StandardPlatform object_resolve none 의 hint · search 의 q · objects_resolve_many 의 name · objects_list 0건 진단의 label). 그래서 그
    id 는 허브가 객체 행(id · object_id 에 key 나 label)으로 돌려줄 때만 배운다 — 메아리를 배우면 사람이 친 id · 앞 턴의 id 를 먼저 다른 허브에
    물은 뒤 진짜 주인 허브의 호출이 막히고, 반대로 공유 id 가 되어 OT-07 꼴이 지났다(반대 검토 2차). 놓치는 쪽(fail-open)이 대가다 — 참조 id 조건으로
    걸러 받은 목록은 그 참조 id 를 가르치지 않는다. ⚠ _learn_ids 처럼 자식(도구 코루틴) 쪽에서 부른다."""
    st = _turn_hub_ids.get()
    if st is None or not isinstance(text, str):
        return
    hub = _hub_of(name)
    if not hub or not _result_ok(text):
        return
    ids = _uuids(text)
    if not ids:
        return
    meta = _hub_id_meta(text)
    for u in ids:
        if u in carried and u not in meta:
            continue                 # 이 호출이 실어 보낸 id 의 메아리 — 그 허브 것으로 배우지 않는다
        e = st.get(u)
        if e is not None:
            e["apps"].add(hub)
            if not (e["key"] or e["label"]) and u in meta:     # 앞에서 이름표 없이 본 id(간선 끝 등) — 이번 결과가 이름표를 준다
                e.update(meta[u])
        elif len(st) < _HUB_IDS_MAX:
            m = meta.get(u) or {}
            st[u] = {"apps": {hub}, "hub": hub, "tool": str(name)[:80],
                     "key": m.get("key", ""), "type": m.get("type", ""), "label": m.get("label", "")}


def _cross_hub_id_msg(target, args) -> str | None:
    """허브 도구 호출의 인자에 이 턴에 다른 허브의 결과에서만 본 UUID 가 있으면 돌려줄 글(보내지 않는다), 없으면 None. 상태에 없는 id(사람이 친 것 ·
    앞 턴의 것 · 처음 보는 것)와 대상 허브의 결과에도 나온 id(제 것 · 공유 id)는 지난다. 칸 이름 · 깊이와 상관없이 인자 글 전체에서 UUID 를 찾는다 —
    숨은 자리(objects_list 의 conditions[].value · resolve 의 name · search 의 q)의 다른 허브 id 는 오류가 아니라 0건으로 돌아와 '없다' 로 읽힌다.
    args 는 인자 사전이거나 invoke_tool 의 arguments(JSON 글일 수 있다)다. 막으면 로그 한 줄을 남기고 /health xhub_gate.blocked 를 센다."""
    st = _turn_hub_ids.get()
    if not st:
        return None                  # 이 턴에 허브 결과가 아직 없다 — 허브와 상관없는 턴의 비용은 여기까지다
    dst = _hub_of(target)
    if not dst:
        return None
    bad = [u for u in _uuids(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str))
           if u in st and dst not in st[u]["apps"]]
    if not bad:
        return None
    top = args
    if isinstance(top, str):
        try:
            top = json.loads(top)
        except ValueError:
            top = None

    def field(u: str) -> str:
        """그 UUID 가 든 맨 위 인자 이름 — 못 가르면 arguments."""
        for k, v in (top.items() if isinstance(top, dict) else ()):
            if u in (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)).lower():
                return str(k)[:40]
        return "arguments"
    hubs = _hub_apps()
    tgt, dl = str(target)[:80], hubs.get(dst) or dst
    parts = []
    for u in bad[:_XHUB_SHOW]:
        e = st[u]
        who = (f"key 「{e['key']}」" if e["key"] else f"key 없음 · 이름 「{e['label']}」" if e["label"] else "key 를 결과에서 읽지 못했다")
        if (e["key"] or e["label"]) and e["type"]:
            who += f" · type_slug {e['type']}"
        parts.append(f"{field(u)}={u} {_XHUB_ID_MARK} — 이 턴에 {hubs.get(e['hub']) or e['hub']} 의 {e['tool']} 결과에서 나왔다({who}).")
    if len(bad) > _XHUB_SHOW:
        parts.append(f"그 밖에 {len(bad) - _XHUB_SHOW}개도 다른 허브의 id 다.")
    parts.append(f"허브마다 같은 객체도 id 가 달라 {dl} 에서는 이 id 로 찾지 못한다.")
    res, many, srch = (_hub_tool(dst, b) for b in ("object_resolve", "objects_resolve_many", "search"))
    e0 = st[bad[0]]
    nm = e0["key"] or e0["label"]
    if len(bad) == 1 and nm and e0["type"]:
        parts.append(f'{res}(type_slug="{e0["type"]}", name="{nm}") 로 {dl} 의 id 를 받아 그 id 로 다시 불러라'
                     f'(그 타입이 없다고 하면 {srch}(q="{nm}")).')
    elif len(bad) == 1 and nm:
        parts.append(f'{srch}(q="{nm}") 로 {dl} 에서 그 객체를 찾아 그 id 로 다시 불러라.')
    else:
        parts.append(f"각 객체의 key 로 {res}(type_slug, name=key) — 여럿이면 {many}(type_slug, names=[…]) — 를 불러 {dl} 의 id 를 받아 "
                     "다시 불러라. key 를 모르는 id 는 앞 허브의 결과에서 그 객체의 key 를 찾는다.")
    parts.append(f"candidates 면 고르지 말고 사용자에게 묻고, none 이면 '{dl} 에서 찾지 못했다' 고 말하라. 인자 문제가 아니다 — 같은 id 로 다시 "
                 "부르지 마라.")
    # 운영 가시성 — 재시험(OT-07)은 이 줄로 막은 수를 본다. 바로 흘린다(손 기동 · 감시 크론으로 뜬 서버는 stdout 이 블록 버퍼링된다 — HA-34 와 같다).
    print(f"[gate] {tgt}({field(bad[0])}={bad[0][:8]}…) 차단 — {e0['hub']} 의 id 를 {dst} 에 실었다(다른 허브 id {len(bad)}개)", flush=True)
    with _XHUB_LOCK:
        _XHUB_STAT["blocked"] += 1
    return f"{_TOOL_FAIL_MARK} 도구 {tgt} 호출 실패(보내지 않았다): " + " ".join(parts)


def _xhub_gate_health() -> dict:
    """/health 의 xhub_gate — 손잡이(CHAT_XHUB_ID_GATE)와 기동 뒤 막은 호출 수. 손잡이를 끈 박스를 밖에서 가린다(design-integration C-8)."""
    with _XHUB_LOCK:
        return {"on": _XHUB_GATE, "blocked": _XHUB_STAT["blocked"]}


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


# 챗 경로에서만 거는 도구 인자 기본값 — 모델이 그 인자를 주지 않았을 때만 채운다(준 값은 그대로). 이름은 노출 이름 끝으로 고른다
# (게이트웨이 별칭 · 충돌 접두 <앱키>_<도구>, invoke_tool 의 안쪽 이름도). 값 옆의 글은 그 도구 설명 끝에 붙여 모델이 언제 바꿀지 안다.
#   plm_defect_search.enrich=0 — 기본 5 는 PLM 원본을 다시 불러 PLM 한도(하베스터 · 챗 · 웹이 같이 쓴다)를 먹는다(10-20 이후 웹 챗
#   72건). 사용자가 실시간 상태를 요구할 때만 올린다(사용자 결정 10-27 D-3 — HWAXPortal docs/request-1027 HA-24). 심의 · 게이트웨이의
#   노출 기본값은 그대로다(PLM 보류).
_CHAT_ARG_DEFAULTS = {
    "plm_defect_search": ({"enrich": 0}, "이 대화에서는 enrich 를 주지 않으면 0 이다(PLM 원본을 다시 부르지 않는다) — "
                                         "사용자가 PLM 의 실시간 상태를 요구할 때만 enrich 를 준다"),
}


def _arg_defaults_for(name, table) -> tuple[dict, str] | None:
    """그 도구 이름의 (기본값, 설명 끝에 붙일 글) — 이름 그대로이거나 별칭 · 충돌 접두를 단 꼴. 없으면 None."""
    n = str(name or "").strip().lower()
    for t, entry in (table or {}).items():
        if n == t or n.endswith("_" + t):
            return entry
    return None


# 허브(자주검증 Datahub · FailureTrace) 응답 접기 — 한 응답이 13만 자를 넘겨 챗 창(262,144 토큰)을 넘기고, GLM 으로 넘겨도 30만 토큰을
# 넘겨 턴이 오류로 끝났다(/health chat_llm.last_error ChatCtxOverflow — HWAXPortal docs/request-1027 HA-04). 원천이 줄이기 전까지
# (Datahub-platform DP-01) 여기서 접는다 — 고르는 데 쓰지 않는 메타(objects_summary 의 group_options · objects_list 의 ref_labels ·
# properties_schema)는 걷고, object_get 의 관계(related)는 관계별 수와 앞 _HUB_RELATED_HEAD 개로. 칸 모양은 요청서의 설명으로 잡았다(실물
# 응답은 이 박스에서 못 봤다) — JSON 이 아니거나 그 칸이 없으면 글자 하나 바꾸지 않는다. 접었으면 맨 앞에 무엇을 얼마나 접었는지 한 줄.
_HUB_RELATED_HEAD = 5
_HUB_FOLD = (("objects_summary", ("group_options",), False), ("objects_list", ("ref_labels", "properties_schema"), False),
             ("object_get", (), True))
# 허브의 나머지 큰 응답(graph_neighbors · object_fields · object_references — HWAXPortal docs/request-1029 W-5, OT-11 object_fields 217,122자)은
# 칸 모양을 본 적이 없다(견본 O-12 전). 그래서 칸 이름이 아니라 일반 규칙이다 — 응답이 _HUB_BIG_CHARS 를 넘을 때만, 길이가 _HUB_LIST_HEAD 를
# 넘는 배열을 앞 _HUB_LIST_HEAD 개와 '전체 N개 중' 한 줄로 줄인다. 배열은 배열로 둔다(개수는 남는다).
_HUB_FOLD_LISTS = ("graph_neighbors", "object_fields", "object_references")
_HUB_BIG_CHARS = 60_000
_HUB_LIST_HEAD = 50
# 접기의 흔적(HWAXPortal docs/request-1030 HA-34) — 접은 표지가 LLM 에 가는 결과 글 머리에만 있어, 10-29 · 10-30 재시험이 접기가 실제로
# 걸렸는지 가리지 못했다. 글이 줄어든 접기마다 로그 한 줄(도구 이름 · 원문 자 수 · 접은 뒤 자 수, invoke_tool 경유면 'invoke_tool(안쪽 이름)')과
# /health 의 hub_fold(기동 뒤 횟수 · 원문 자 수 합 · 접은 뒤 자 수 합). 메타 칸이 비었거나 작은 응답은 걷어도 머리 줄이 더 길어 글이 는다 —
# 그것까지 세면 횟수가 objects_summary · objects_list 호출 수가 되고 접은 뒤 합이 원문 합을 넘는다(반대 검토). 로그 줄은 바로 흘린다 — 부팅
# 유닛 · 감시 크론 · 손 기동(start.sh -d)으로 뜬 서버는 PYTHONUNBUFFERED 가 없어(update-sites 만 내보낸다) 파일로 가는 stdout 이 블록
# 버퍼링된다. /health 는 스레드 풀에서, 접기는 이벤트 루프에서 돈다 — 자물쇠로 묶는다(_SC_LOCK 과 같다).
_HUB_FOLD_LOCK = threading.Lock()
_HUB_FOLD_STAT: dict = {"count": 0, "chars_in": 0, "chars_out": 0}


def _hub_fold_note(name, before: str, after: str, via: bool = False) -> str:
    """접은 글을 그대로 돌려준다 — 글이 줄었을 때만 세고 로그 한 줄을 남긴다(via — invoke_tool 경유로 안쪽 이름을 접었다)."""
    if len(after) >= len(before):
        return after
    with _HUB_FOLD_LOCK:
        _HUB_FOLD_STAT["count"] += 1
        _HUB_FOLD_STAT["chars_in"] += len(before)
        _HUB_FOLD_STAT["chars_out"] += len(after)
    shown = f"invoke_tool({str(name)[:80]})" if via else str(name)[:80]
    print(f"[agent] 허브 응답 접음 — {shown}: 원문 {len(before):,}자 → {len(after):,}자", flush=True)
    return after


def _hub_fold_health() -> dict:
    with _HUB_FOLD_LOCK:
        return dict(_HUB_FOLD_STAT)


def _fold_hub_lists(text: str) -> str:
    if len(text) <= _HUB_BIG_CHARS:
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    cut = {"lists": 0, "items": 0}

    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            if len(v) > _HUB_LIST_HEAD:
                cut["lists"] += 1
                cut["items"] += len(v) - _HUB_LIST_HEAD
                return [walk(x) for x in v[:_HUB_LIST_HEAD]] + [
                    f"…[전체 {len(v):,}개 중 앞 {_HUB_LIST_HEAD}개만 실었다 — 나머지는 조건을 좁혀 다시 조회]"]
            return [walk(x) for x in v]
        return v

    folded = walk(data)
    if not cut["lists"]:
        return text
    return (f"[포털 챗이 허브 응답을 접었다 — 긴 목록 {cut['lists']:,}곳에서 항목 {cut['items']:,}건 뺐다 · 원문 {len(text):,}자]\n"
            + json.dumps(folded, ensure_ascii=False))


def _fold_hub_result(name, text, via=False):
    n = str(name or "").strip().lower()
    if (isinstance(text, str) and text.lstrip().startswith(("{", "["))
            and any(n == t or n.endswith("_" + t) for t in _HUB_FOLD_LISTS)):
        return _hub_fold_note(name, text, _fold_hub_lists(text), via)
    rule = next((r for r in _HUB_FOLD if n == r[0] or n.endswith("_" + r[0])), None)
    if rule is None or not isinstance(text, str) or not text.lstrip().startswith(("{", "[")):
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    _, drop, fold_related = rule
    cut: Counter = Counter()

    def walk(v):
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if k in drop:
                    cut[k] += 1
                elif fold_related and k == "related":
                    out[k] = fold(x)
                else:
                    out[k] = walk(x)
            return out
        return [walk(x) for x in v] if isinstance(v, list) else v

    def group(items: list) -> dict:
        res = {}
        for r, its in items:
            res[r] = {"count": len(its), "head": [walk(i) for i in its[:_HUB_RELATED_HEAD]]}
            cut["related"] += max(0, len(its) - _HUB_RELATED_HEAD)
        return res

    def fold(rel):
        if isinstance(rel, dict) and any(isinstance(x, list) and len(x) > _HUB_RELATED_HEAD for x in rel.values()):
            return {**{r: walk(x) for r, x in rel.items() if not isinstance(x, list)},
                    **group([(r, x) for r, x in rel.items() if isinstance(x, list)])}
        if isinstance(rel, list) and len(rel) > _HUB_RELATED_HEAD:
            by: dict = {}
            for it in rel:
                k = it.get("relation") or it.get("predicate") or it.get("type") if isinstance(it, dict) else None
                by.setdefault(str(k or "관계 미상"), []).append(it)
            return group(list(by.items()))
        return walk(rel)

    folded = walk(data)
    if not cut:
        return text
    what = " · ".join(f"관계 항목 {v:,}건" if k == "related" else f"{k} {v:,}곳" for k, v in cut.items())
    return _hub_fold_note(name, text, f"[포털 챗이 허브 응답을 접었다 — {what} 뺐다 · 원문 {len(text):,}자]\n"
                          + json.dumps(folded, ensure_ascii=False), via)


def _app_label_cached(name: str) -> str:
    """도구 노출 이름 → 앱 라벨 — 캐시만 본다(도구 결과마다 부르므로 게이트웨이에 묻지 않는다. 캐시는 이 턴의 도구를 묶을 때 채웠다)."""
    key = (_TOOLS_MAP_CACHE.get("map") or {}).get(name, "")
    if not key:
        return ""
    a = (_APPS_CACHE.get("apps") or {}).get(key) or {}
    return (a.get("label") or "").strip() or _GROUP_LABEL.get(key) or _pretty_group(key)


def _chat_result_note(name: str, polled: bool = False) -> tuple[str, str]:
    """챗 도구 결과의 (머리, 꼬리). 머리는 '이 결과는 ○○ 앱' 이다(HWAXPortal docs/request-1029 R-10) — 도구 설명 앞 라벨만으로는 접두 없는
    이름(DynaForge 의 list_tasks · ontology_sync_state …)을 다른 앱 것으로 읽었다(EX-05). 꼬리는 같은 도구를 이 턴에 네 번째부터 부를 때
    '앞 결과를 써라' 다(W-5). 허브의 개수 · 목록 도구에는 조건 주소를 object_fields 로 보라고 덧붙인다(OT-11 APP-OBJECTS-0073 셋).
    polled 면(상태 도구이고 결과에서 상태 낱말을 읽었다 — HA-28) 꼬리를 달지 않는다 — 상태는 다시 부르면 바뀐다. 호출 수는 그대로 센다."""
    label = _app_label_cached(name)
    head = f"[이 결과는 {label} 앱의 {name} 이다]\n" if label else ""
    cnt = _turn_tool_calls.get()
    if cnt is None:
        return head, ""
    cnt[name] += 1
    if cnt[name] < _TOOL_REPEAT_NOTE_AT or polled:
        return head, ""
    tail = (f"\n[이 턴에 {name} 를 {cnt[name]}번째 불렀다 — 앞 결과로 답할 수 있는지 먼저 보라. 같은 조건으로 다시 부르면 결과가 같다")
    n = name.lower()
    if any(n == t or n.endswith("_" + t) for t in ("objects_summary", "objects_list")):
        tail += " · 조건 주소(in.applies_to_part 꼴)를 모르면 object_fields 로 확인하고 부른다"
    return head, tail + "]"


def _refuse(tool, msg: str):
    """막은 · 실패한 호출의 반환 — content_and_artifact 도구는 2-튜플을 요구한다(실측). `capped` 의 관문 걸음과 호출 실패가 함께 쓴다."""
    return (msg, None) if getattr(tool, "response_format", "") == "content_and_artifact" else msg


# `capped` 의 실행 설정 인자 기본값 — LangChain 은 코루틴에 RunnableConfig 형 인자가 있으면 그 이름으로 실행 설정을 넣는다(상태 기다림의 진행 줄이
# 그것으로 사건을 낸다). ⚠ 기본값을 None 으로 두지 마라 — 파이썬 3.10 의 get_type_hints 가 Optional[RunnableConfig] 로 읽어 넣지 않는다.
_NO_RC: RunnableConfig = {}


def _status_tool(name: str) -> bool:
    """상태 도구인가(HA-28) — 앱 키 · routine_read 는 캐시만 본다(망 호출 없음). 기능이 꺼져 있으면 늘 거짓."""
    m = _TOOLS_MAP_CACHE
    return _status_wait_mod.is_status_tool(name, (m.get("map") or {}).get(name) or None, m.get("routine_read"))


async def _status_wait(name: str, kw: dict, out, again, rc, chat: bool) -> tuple:
    """`capped` 의 R2(HWAXPortal docs/request-1030 HA-28 · design-integration C-3 · C-4) — (결과, 기다림 안내, polled). 상태 도구이고 chat 이면
    잡이 끝나기를 기다리고(status_wait.handle), 아니면 잡 시작 결과에서 id 를 배운다 — 턴 상태만 있으면 래퍼 종류와 상관없이(확정 종결 ·
    새어 나온 호출 · 승인 실행이 건 잡도). 턴 상태가 없으면(심의 · 카탈로그 · 시험의 직접 호출) 그대로 돌려준다. 경유(invoke_tool)면 안쪽
    이름 · 인자로 본다. 진행 줄은 실행 설정(rc)의 콜백으로 낸다 — 콜백이 없으면 내지 않고, 부모 실행이 없어 내지 못하면 한 줄 남기고 기다림은
    그대로 간다(status_wait._emit)."""
    turn = _turn_status_wait.get()
    if turn is None:
        return out, "", False
    inner, args = _inner_tool(name, kw)
    m = _TOOLS_MAP_CACHE

    async def emit(data):
        if (rc or {}).get("callbacks") is not None:
            await adispatch_custom_event(_status_wait_mod.EVENT, data, config=rc)

    # 멈춤 술어(C-4) — 이 턴에 확인 관문(HA-29)의 G5 가 호출을 막았으면 참이다. 그 턴은 이 단계가 끝난 뒤 다음 모델 호출 앞에서 끊기고 카드로
    # 끝나므로 기다림의 결과를 모델이 끝내 보지 못한다 — 잠 · 다시 조회 앞에서 접어 카드가 최대 600초 늦게 서지 않게 한다. 맥락은 턴과 나눠 쥔
    # 객체라 나란한 도구가 막은 것도 보인다. 관문이 꺼졌거나 챗 밖이면 맥락이 None 이라 늘 거짓이다. pending(카드에 실을 호출)을 보지 않는다 —
    # 승인 실행이 도구 목록을 못 받아 다시 카드로 돌린 호출도 거기 들지만 그 턴은 끊기지 않고 끝까지 돈다(반대 검토 2차 — 그 턴의 되풀이 상태
    # 조회가 0초에 접히고 '다른 까닭으로 먼저 멈춘다' 는 틀린 안내를 받았다).
    return await _status_wait_mod.handle(
        inner, (m.get("map") or {}).get(inner) or None, m.get("routine_read"), kw if args is None else args, out, again, turn,
        chat=chat, emit=emit,
        stop=lambda: bool(getattr(_turn_confirm.get(), "held", 0)))


def _status_wait_retry() -> None:
    """그래프를 처음부터 다시 도는 길(agent.ainvoke — 자동 재시도 · 강제 재시도 · 새어 나온 호출의 재시도 · 빈 답 구제) 앞 — 상태 기다림의 턴 상태를
    그 길에 맞춘다(status_wait.retry — '같은 호출의 되풀이' 셈을 비운다). 이 길은 진행 줄을 낼 스트림이 없다 — ping 을 끈 박스(DELIB_HEARTBEAT_S
    ≤ 0)에서는 그 길의 기다림 합을 POLL_CALL_MAX_S 로 묶는다(design-ha28 4.12 의 '침묵은 150초를 넘지 않는다' 가 이 길에서도 서게 — 반대 검토 2차)."""
    _status_wait_mod.retry(_turn_status_wait.get(), _status_wait_mod.POLL_CALL_MAX_S if DELIB_HEARTBEAT_S <= 0 else None)


def _cap_tool(tool, result_max=None, arg_defaults=None, chat=False):
    """도구 결과를 절단해 LLM 컨텍스트를 보호한다 — 대량 조회(예: VOC 수천 건)가 그대로
    프롬프트에 들어가면 'maximum context length' 400 으로 채팅이 죽는다(실측 16385/16384).
    result_max 를 주면 그 값으로 절단한다(결정적 JSON 조회는 CATALOG_RESULT_MAX 를 넘긴다).
    arg_defaults 를 주면(챗 경로 — _CHAT_ARG_DEFAULTS) 모델이 주지 않은 인자를 그 값으로 채운다.
    chat 이면(챗에 묶는 도구만 — 결과를 코드가 JSON 으로 읽는 길에는 주지 않는다) 결과에 앱 머리 · 되풀이 꼬리를 단다(_chat_result_note)."""
    orig = tool.coroutine
    if orig is None:
        return tool
    _own = _arg_defaults_for(getattr(tool, "name", ""), arg_defaults)
    if _own:
        tool.description = f"{tool.description or ''} [{_own[1]}]"

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

    async def capped(*a, _hwax_rc: RunnableConfig = _NO_RC, **kw):
        # 걸음 차례(HWAXPortal docs/request-1030/design-integration.md 2절) — G 는 부르기 전에 막으면 글을 돌려주고,
        # R 은 성공한 호출에만 돈다. G1 invoke_tool 안쪽 거부 → G2 외부 응답자 · 반출 동의 거부 → G3 유령 ID →
        # G4 다른 허브 id → G5 확인 카드 → G6 챗 인자 기본값 → C 호출 → R1 ~ R8.
        # ── G1 외부 응답자 과제 도구는 invoke_tool 로도 부르지 않는다(_AGENT_DENY_TOOLS — 목록에서 뺀 것을 이름으로 우회하지 못하게).
        # 게이트웨이는 별칭(<앱키>_<도구>)으로도 부르게 하므로 그 꼴도 막는다. 게이트웨이에 닿기 전에 여기서 끝낸다.
        if getattr(tool, "name", "") == "invoke_tool":
            for _args in (kw, *(x for x in a if isinstance(x, dict))):
                _inner = _args.get("name") if isinstance(_args, dict) else None
                if isinstance(_inner, str) and _agent_denied(_inner):
                    if _chat_panel_tool(_inner):
                        print(f"[gate] invoke_tool({_inner[:80]}) 차단 — 패널 전용 도구(이 대화는 패널을 그리지 못한다)")
                        msg = (f"{_TOOL_FAIL_MARK} 도구 {_inner[:80]} 호출 실패: 패널을 그리는 클라이언트 전용 도구라 이 대화에서는 "
                               "쓰지 않는다(패널이 그려지지 않는다). 같은 일을 하는 일반 도구(예: 실행 준비 prepare_run · 업로드 도구)로 한다. "
                               "인자 문제가 아니다 — 다시 부르지 마라")
                    else:
                        print(f"[gate] invoke_tool({_inner[:80]}) 차단 — 외부 응답자 과제 도구는 이 서버의 에이전트가 부르지 않는다")
                        msg = (f"{_TOOL_FAIL_MARK} 도구 {_inner[:80]} 호출 실패: 외부 응답자 과제 도구는 사용자의 Claude Code "
                               "워크플로만 쓴다 — 이 대화의 에이전트는 부를 수 없다. 인자 문제가 아니다 — 다시 부르지 마라")
                    return _refuse(tool, msg)
        # ── G2 외부 응답자 심의는 사람의 클라이언트에서만 연다(X-D6) — 심의 시작·이어하기에 외부 선택(advanced.llm=external)이나 반출 동의
        # 칸(consent_external)을 실은 호출은 보내지 않는다. 직접 묶인 그 도구로도, invoke_tool 의 안쪽 이름으로도 같다.
        _via_inv = getattr(tool, "name", "") == "invoke_tool"
        for _args in (kw, *(x for x in a if isinstance(x, dict))):
            _target, _targs = ((_args.get("name"), _args.get("arguments")) if _via_inv
                               else (getattr(tool, "name", ""), _args))
            if _agent_external_call(_target, _targs):
                _target = str(_target)[:80]
                print(f"[gate] {_target} 차단 — 이 서버의 에이전트는 외부 응답자 심의·반출 동의 칸을 싣지 못한다")
                msg = (f"{_TOOL_FAIL_MARK} 도구 {_target} 호출 실패: 외부 응답자 심의(advanced.llm=external)와 반출 동의 표지"
                       "(consent_external)는 사람이 자기 클라이언트에서 고른다 — 이 대화의 에이전트는 그 칸을 실을 수 없어 이 호출은 "
                       "보내지 않았다. 사용자가 외부 응답자 심의를 원하면 리스크 앱이나 자기 Claude Code 에서 직접 열라고 알려라. 인자 "
                       "문제가 아니다 — 그 칸을 실어 다시 부르지 마라")
                return _refuse(tool, msg)
        # ── G3 유령 ID 게이트 — 이번 턴 어디에도 없던 정수를 식별자 인자에 넣은 호출은 실행하지 않는다.
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
                    return _refuse(tool, msg)
        # ── G4 다른 허브 id(HWAXPortal docs/request-1030 HA-30) — 이 턴에 다른 허브의 결과에서만 본 UUID 를 허브 도구에 실은 호출은 보내지 않고
        # key 로 다시 풀라고 돌려준다(_cross_hub_id_msg). 직접 묶인 도구로도, invoke_tool 의 안쪽 이름으로도 같다. 턴 상태가 없으면 지난다.
        # G5 확인 카드보다 앞이다 — 틀린 id 의 호출로 사람에게 확인을 받으면 확인 뒤에 여기서 막혀 사람의 확인이 헛것이 된다.
        for _args in (kw, *(x for x in a if isinstance(x, dict))):
            _target, _targs = ((_args.get("name"), _args.get("arguments")) if _via_inv
                               else (getattr(tool, "name", ""), _args))
            _xmsg = _cross_hub_id_msg(_target, _targs)
            if _xmsg:
                return _refuse(tool, _xmsg)
        # ── G5 확인 카드(HA-29) — 사람이 이번 말로 시키지 않은 잡 · 쓰기, 되돌리기 어려운 호출은 보내지 않는다(턴이 끝에 카드를 띄운다).
        # 인자가 틀린 호출(G3 · G4)에 카드를 띄우면 사람이 틀린 대상을 승인하므로 이 걸음이 마지막이다. 열쇠는 기본값(G6) 앞 인자로 잰다.
        _cparse = _confirm_parse(tool, a, kw)          # G6 이 kw 를 바꾸기 전의 열쇠 — 자격이 거절되면 C 의 실패 갈래가 적는다
        _held = _confirm_gate(tool, a, kw)
        if _held:
            return _refuse(tool, _held)
        # ── G6 챗 인자 기본값
        if _own:
            for _k, _v in _own[0].items():
                kw.setdefault(_k, _v)
        elif arg_defaults and getattr(tool, "name", "") == "invoke_tool":
            _inner = _arg_defaults_for(kw.get("name"), arg_defaults) if isinstance(kw.get("name"), str) else None
            _inner_def = _inner[0] if _inner else None
            _ia = kw.get("arguments")
            if _inner_def and isinstance(_ia, str):
                try:
                    _parsed = json.loads(_ia or "{}")
                except ValueError:
                    _parsed = None
                if isinstance(_parsed, dict):
                    kw["arguments"] = json.dumps({**_inner_def, **_parsed}, ensure_ascii=False)
            elif _inner_def and (_ia is None or isinstance(_ia, dict)):
                kw["arguments"] = {**_inner_def, **(_ia or {})}
        # ── C 호출 — 예외면 ✖ 글로 끝난다(아래 R 걸음을 타지 않는다)
        try:
            out = await orig(*a, **kw)
        except Exception as exc:  # noqa: BLE001 — 인자 검증 실패는 코루틴 안에서 raise 된다(실측).
            # MCP 전송이 올리는 예외는 anyio TaskGroup 에 싸여 온다 — 문구가 'unhandled errors in a
            # TaskGroup (1 sub-exception)' 뿐이라 진짜 원인(게이트웨이의 401·연결 거절)이 안 보였다.
            # 안쪽 예외를 꺼내 그것으로 말하고 판정한다.
            while getattr(exc, "exceptions", None):
                exc = exc.exceptions[0]
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
            # ⚠ 이 자리가 **성패를 삼키던 곳**이다. 예외를 잡아 문자열로 돌려주므로
            # LangGraph 는 정상 반환으로 보고, 스트림은 실패한 호출을 성공한 호출과
            # 구분할 수 없었다 — 이 리포가 반복해서 만나는 모양이다. 판단은 이미 위에서
            # 내리고 있으니(_is_transport·_is_argerr) **버리지만 않으면** 된다.
            msg = f"{_TOOL_FAIL_MARK} 도구 {getattr(tool, 'name', '?')} 호출 실패: {str(exc)[:500]}"
            if getattr(getattr(exc, "response", None), "status_code", None) == 401:
                # 게이트웨이가 자격을 거절했다 — 시작할 때 받은 사용자 토큰이 도는 중에 만료·폐기된 것이다.
                # 표지를 싣는다: 심의는 이것을 보고 서비스 계정으로 한 번 더 부른다(deliberation._svc_fallback).
                # 확인 관문(HA-29)의 잡 · 쓰기면 그 재시도를 G5 가 자격 거절 글로 막는다(_confirm_auth_rejected).
                _confirm_auth_rejected(_cparse)
                msg = (f"{_TOOL_FAIL_MARK} 도구 {getattr(tool, 'name', '?')} 호출 실패: {_AUTH_REJECT_MARK} — "
                       "사용자 토큰이 만료됐거나 폐기됐다. 인자 문제가 아니다 — 같은 도구를 인자만 바꿔 다시 "
                       "부르지 마라")
            if _mcp_call_timed_out(exc):
                # 엔진측 기한이 걸렸다. SDK 문구('Timed out while waiting for response to ClientRequest')만
                # 실으면 무엇이 얼마를 기다렸는지, 어느 값을 봐야 하는지 없다 — 기한과 손잡이를 말한다.
                msg = (f"{_TOOL_FAIL_MARK} 도구 {getattr(tool, 'name', '?')} 호출 실패: "
                       f"{MCP_CALL_TIMEOUT_S:,.0f}초 안에 답하지 않았다(MCP_CALL_TIMEOUT_S) — 게이트웨이 무응답이다. "
                       "게이트웨이가 살아 있으면 제 한도(GATEWAY_CALL_TIMEOUT)에서 먼저 답하므로, 여기까지 "
                       "왔으면 게이트웨이가 멈췄거나 이 값이 게이트웨이 한도보다 작다(커야 한다)")
            if hint:
                msg += f"\n[인자 스키마 — 이 형식으로 교정해 다시 호출]\n{hint}"
            elif _is_transport:
                # 인자를 바꿔봐야 소용없다는 것을 모델에게 명시해 무의미한 재시도를 끊는다.
                msg += ("\n[안내] 인자 문제가 아니라 도구 백엔드 연결/시간초과다. "
                        "같은 도구를 인자만 바꿔 다시 호출하지 말고, 다른 방법으로 답하거나 "
                        "사용자에게 해당 백엔드가 일시적으로 불가하다고 알려라.")
            return _refuse(tool, msg)
        # ── R1 접기 · 앱 머리 · 되풀이 꼬리는 경유(invoke_tool)면 안쪽 도구 이름으로 본다
        _eff = getattr(tool, "name", "")
        if _eff == "invoke_tool" and isinstance(kw.get("name"), str):
            _eff = kw["name"]
        # ── R2 상태 기다림(HA-28) — 상태 도구가 '도는 중' 을 주면 이 턴에 건 잡이거나 같은 호출의 되풀이일 때 코드가 기다린다(chat 일 때만).
        # 다시 조회는 orig 를 같은 인자로 직접 부른다(관문을 다시 지나지 않고 _hwax_rc 도 새지 않는다). 아래 걸음은 마지막 결과에 한 번만 돈다.
        out, _wait_note, _polled = await _status_wait(getattr(tool, "name", ""), kw, out, lambda: orig(*a, **kw), _hwax_rc, chat)
        # ── R3 앱 머리 · 되풀이 꼬리(상태를 읽은 상태 도구는 꼬리를 달지 않는다)
        _head, _tail = _chat_result_note(_eff, polled=_polled) if chat else ("", "")
        # ── R4 (content, artifact) 꼴이면 글만 다루고 artifact 는 그대로 돌려준다 — 한 갈래
        _pair = isinstance(out, tuple) and len(out) == 2
        raw = _norm(out[0] if _pair else out)
        # ── R5 원문 붙잡기(HA-27) — 접기 · 자르기 전 원문을 이번 챗 턴 그릇에(턴 밖 · 관문 끔이면 아무것도 안 한다). 자기검증 관문이 검토자의
        # 바꿀 곳을 이 원문과 다시 대조한다 — 검토자 사본은 아래 접기 · 상한을 지나 원문의 수가 빠져 있었다(OT-04 34 → 13).
        _sc_keep_raw(raw)
        # ── R6 접기 → 자르기 → 인자 힌트
        body = _maybe_repair_hint(_cap(_fold_hub_result(_eff, raw, _via_inv), result_max))
        # ── R7 id 배우기 — 정수(유령 ID 막이) · UUID → 허브(HA-30, 모델이 보는 글에서 · 경유면 안쪽 이름으로)
        _learn_ids(body)
        _learn_hub_ids(_eff, body, set(_uuids(json.dumps(kw, ensure_ascii=False, default=str))) if _turn_hub_ids.get() is not None else ())
        # ── R8 머리 + 본문 + 기다림 안내 + 꼬리
        return (_head + body + _wait_note + _tail, out[1]) if _pair else _head + body + _wait_note + _tail

    tool.coroutine = capped
    return tool


def _cap_desc(s, limit=None):
    # 0 은 무제한이다(_cap 과 같은 약속). 종전엔 `limit or TOOL_DESC_MAX` 라 TOOL_DESC_MAX=0 이면 설명이 전부 '…' 가
    # 됐고, 무절단을 뜻한 desc_max=0(CATALOG_DESC_MAX=0)은 바인딩 보호값 2,400 으로 떨어졌다.
    limit = TOOL_DESC_MAX if limit is None else limit
    if limit > 0 and isinstance(s, str) and len(s) > limit:
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


def _prep_tool(tool, result_max=None, desc_max=None, arg_defaults=None, chat=False):
    """도구 로드 직후 한 번에 적용하는 체인: 앱 태깅 + 검증힌트 + 스키마 슬림 + 결과 절단.
    result_max·desc_max 는 각각 결과·설명 절단 한도 — None 이면 LLM 경로 기본값. arg_defaults · chat 은 챗 경로만 준다(_CHAT_ARG_DEFAULTS)."""
    return _cap_tool(_slim_tool(_attach_validation_hint(_tag_tool_app(tool)), desc_max), result_max, arg_defaults, chat)


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
# 무한은 아니게. 도구 왕복 1회가 노드 둘(agent·tools)을 쓴다 — 80 이면 연속 왕복 39회가 끝까지 돌고, 40번째 호출은
# langgraph 가 'need more steps' 로 막아 턴이 정상으로 끝난다. ⚠ 그래프에 노드를 더 끼우지 마라(pre_model_hook 등) —
# 왕복마다 한 단계를 더 써 25회로 줄고, agent 노드의 잔여 단계 검사를 비켜 GraphRecursionError 로 턴이 죽는다(m1 3차).
AGENT_RECURSION_LIMIT = int(os.environ.get("AGENT_RECURSION_LIMIT", "80"))
# 같은 도구·같은 인자 반복 호출 경고 임계 — 작은 모델의 루프를 사용자에게 드러낸다.
TOOL_REPEAT_WARN = int(os.environ.get("TOOL_REPEAT_WARN", "3"))
# 바인딩 도구 스키마의 추정 토큰 상한(0=무제한). 개수 캡만으로는 컨텍스트 초과를 못 막는다.
TOOL_SCHEMA_BUDGET = int(os.environ.get("TOOL_SCHEMA_BUDGET", "40000"))
# 캡 없는 길(TOOL_MAX=0 — 운영 cae00)의 도구 설명 예산(추정 토큰). 비거나 0 이면 종전대로 창의 45% 다(HWAXPortal docs/request-1029 W-2 ·
# 결정 D-3 — 손잡이만 짓고 기본값은 그대로, 박스에서 30,000 · 40,000 을 같은 16문항으로 견준 뒤 정한다). 10-29 에는 호출마다 약 8.8만 토큰이
# 실렸다(262,144 − 출력 몫 65,536 의 45%). 줄이면 search_tools → invoke_tool 왕복이 늘 수 있다.
CHAT_TOOL_SCHEMA_BUDGET = int(os.environ.get("CHAT_TOOL_SCHEMA_BUDGET", "0") or 0)
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
# 지목한 전문가의 분야 도구를 몇 개까지 먼저 앉힐지. 캡(dev 40)의 3할 남짓이라 질문 어휘로
# 고르는 자리를 다 먹지 않는다 — 전문가 짐작이 빗나가도 관련 도구가 들어올 여지를 남긴다.
_PERSONA_PREFER_N = _env_int("CHAT_PERSONA_PREFER_N", 12)


# ── 도구 시맨틱 검색 — AIDH 다국어 e5 임베더 재사용(모델 중복 로딩 없음) ─────────────
# 어휘 매칭은 정확한 도구명에 강하고 임베딩은 표현이 다른 질의("김서림 방지" ↔ fogging)에
# 강하다 → 둘을 RRF 로 합친다. e5 는 절대 유사도가 전반적으로 높아(무관 쌍도 0.8+) 임계값이
# 아니라 상대 순위로만 쓴다.
AIDH_HTTP = os.environ.get("AIDH_HTTP_BASE", "http://127.0.0.1:8001")
_TOOL_VEC: dict = {"sig": "", "names": [], "vecs": [], "space": None}
# AIDataHub /api/embed 가 마지막으로 낸 임베딩 공간 — (모델 이름, 차원). 임베더가 바뀌면(예 e5-base 768 → BGE-M3 1024) 옛 공간의 도구
# 벡터와 견주지 않게 캐시가 이것을 함께 본다(포털 docs/embedder-bge-m3).
_EMBED_SPACE: dict = {"space": None}


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
            data = json.loads(r.read()) or {}
        vecs = data.get("vectors") or []
        out.extend(vecs)
        if vecs:
            _EMBED_SPACE["space"] = (str(data.get("model") or ""), len(vecs[0]))
    return out


# 서명(도구 이름 전부)별로 쥐는 색인 수 — 색인이 하나뿐이라 작은 목록(HE팀 운영자 · 카탈로그 10개)이 큰 목록(일반 챗 1,613개)을 밀어내고,
# 다음 챗 턴이 1,613개를 다시 임베딩했다(로그 1613 → 10 → 1613 → 10 — HWAXPortal docs/request-1027 HA-16). 공간(임베더)도 열쇠에 든다.
_TOOL_VEC_KEEP = 4
_TOOL_VEC_LRU: "OrderedDict[tuple, dict]" = OrderedDict()


def _ensure_tool_vecs(tools: list) -> dict | None:
    """도구 벡터 캐시 — 그 도구 구성의 색인 {sig, names, vecs, space}(못 지으면 None). 구성이 바뀔 때만 재계산(수백 개도 수 초).
    _TOOL_VEC 은 마지막으로 쓴 색인이다(보기용) — 부르는 쪽은 돌려받은 것을 쓴다(스레드 사이에 다른 구성으로 바뀌어도 이름 · 벡터가 짝을 잃지 않게)."""
    import hashlib
    names = sorted(getattr(t, "name", "") for t in tools)
    # 개수+첫/끝만 보면 중간 교체를 놓친다 — 전체 이름 해시로 정확히 무효화.
    sig = hashlib.sha256("\n".join(names).encode()).hexdigest()[:16]
    cur = dict(_TOOL_VEC)
    if cur["sig"] == sig and cur["vecs"] and cur["space"] == _EMBED_SPACE["space"]:
        return cur
    hit = _TOOL_VEC_LRU.get((sig, _EMBED_SPACE["space"]))
    if hit is not None:
        _TOOL_VEC_LRU.move_to_end((sig, _EMBED_SPACE["space"]))
        _TOOL_VEC.update(hit)
        return hit
    by = {getattr(t, "name", ""): t for t in tools}
    texts = [f"{n}: {(getattr(by[n], 'description', '') or '')[:400]}" for n in names]
    try:
        vecs = _embed(texts, "passage")
    except Exception as exc:  # noqa: BLE001 — 임베딩 미가용이면 어휘 매칭만으로 동작(회귀 0)
        print(f"[tools] embed failed: {exc!r}")
        return None
    if len(vecs) != len(names):
        return None
    ent = {"sig": sig, "names": names, "vecs": vecs, "space": _EMBED_SPACE["space"]}
    _TOOL_VEC.update(ent)
    _TOOL_VEC_LRU[(sig, ent["space"])] = ent
    _TOOL_VEC_LRU.move_to_end((sig, ent["space"]))
    while len(_TOOL_VEC_LRU) > _TOOL_VEC_KEEP:
        _TOOL_VEC_LRU.popitem(last=False)
    print(f"[tools] semantic index: {len(names)}개")
    return ent


def _semantic_order(query: str, tools: list) -> list:
    """질의 임베딩 코사인 내림차순 도구 이름 목록(미가용이면 빈 목록)."""
    if not query.strip():
        return []
    ent = _ensure_tool_vecs(tools)
    if not ent or not ent["vecs"]:
        return []
    try:
        qv = _embed([query], "query")
    except Exception:  # noqa: BLE001
        return []
    if not qv:
        return []
    if ent["space"] != _EMBED_SPACE["space"]:
        # 질의가 다른 공간에서 왔다(AIDataHub 임베더가 바뀌었다) — 옛 도구 벡터와 견주면 zip 이 차원을 말없이 잘라 엉뚱한 순서가
        # 된다. 다시 짓고, 그래도 공간이 다르면 의미 순서를 내지 않는다(어휘 순서만 쓴다).
        ent = _ensure_tool_vecs(tools)
        if not ent or ent["space"] != _EMBED_SPACE["space"] or not ent["vecs"]:
            return []
    import math
    q = qv[0]
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    scored = []
    for n, v in zip(ent["names"], ent["vecs"]):
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


def _tool_schema_cost(t) -> int:
    """도구 하나의 추정 스키마 토큰 — 한글·JSON 혼합에서 대략 3자 ≈ 1토큰(보수적 과대평가)."""
    try:
        sch = json.dumps(getattr(t, "args_schema", None) or {}, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        sch = ""
    return (len(getattr(t, "name", "")) + len(getattr(t, "description", "") or "") + len(sch)) // 3 + 8


# 안내대 — 관련도가 빗나갔을 때의 폴백 입구(_TOOL_PRIORITY 첫 셋). 스키마 예산과 상관없이 맨 앞에 남긴다 — 예산 밖으로 잘린 도구는 이
# 셋으로 닿는다(search_tools 가 찾고 invoke_tool 이 바인딩에 없는 것을 그 턴에 부른다). 종전엔 무캡 경로가 게이트웨이 순서 뒤에서부터
# 잘라 이 셋이 맨 먼저 빠졌다 — 게이트웨이 자체 도구가 목록 맨 끝이다(HWAXPortal docs/chat-depth-context F1).
_GUIDE_FRONT = _TOOL_PRIORITY[:3]
# 온톨로지 코어 — 정본 허브(자주검증 Datahub · FailureTrace)의 해석 · 개수 · 관계 · 읽기 도구와 PLM 과제 도구. 무캡 경로가 예산 때문에
# 자를 때 관련도와 상관없이 남기고(_fit_schema keep), 캡 경로는 상시 핵심과 함께 예약한다. 종전엔 절단 25회 모두에서 허브가 빠져(평균 103개
# 중 89개 — 10-27 /health tool_cut 1,613 → 183) 웹 챗 도구 호출 722건 중 허브 호출이 1건이었다(HWAXPortal docs/request-1027 HA-02).
# 사용자에게 보이는 것만 남는다(게이트웨이가 그 사람의 도구만 준다) — 약 15개 · 4~6천 토큰. 이름은 두 허브가 함께 붙은 박스의 노출 이름이다
# (겹치는 이름은 둘 다 접두가 붙는다). HE팀 운영자(core_names 를 준다)는 제 앱 도구가 앞자리라 넣지 않는다.
_ONTO_CORE = (
    "rootdesigndatahub_object_resolve", "rootdesigndatahub_objects_resolve_many", "rootdesigndatahub_objects_list",
    "rootdesigndatahub_objects_summary", "rootdesigndatahub_object_get", "rootdesigndatahub_object_fields",
    "rootdesigndatahub_graph_neighbors", "rootdesigndatahub_search",
    "failuretrace_object_resolve", "failuretrace_objects_list", "failuretrace_objects_summary", "failuretrace_graph_neighbors",
    "list_tasks", "task_revisions", "compare_task_revisions", "find_plm_task",
)
# 마지막 절단 — /health 가 싣는다(앞뒤 개수 · 상한 · 추정 토큰 · 뺀 앱별 수). update-all 헬스게이트가 '챗 도구 절단 N개' 로 읽는다.
_TOOL_CUT: dict = {}


def _fit_schema(tools: list, budget: int, keep: tuple = ()) -> list:
    """추정 토큰 합계가 budget 을 넘지 않도록 **뒤에서부터** 떨어뜨린다(앞이 더 관련).

    캡 경로와 무캡 경로가 **같은 계산**을 쓰게 하려고 떼어 냈다. 둘이 갈리면 한쪽만
    예산을 안 보게 되고, 실제로 무캡 경로가 검사를 통째로 건너뛰고 있었다.
    떨어뜨린 것은 로그로 남긴다 — 조용히 자르면 '도구가 있는데 안 부른다' 로 보인다. 어느 앱이 몇 개 빠졌는지까지
    적는다(종전엔 개수만이라 'MaterialTwin 34종이 통째로 없다' 를 로그로 못 읽었다). keep 의 이름은 예산과 상관없이 남긴다.
    """
    total, kept = 0, []
    for t in tools:
        c = _tool_schema_cost(t)
        if kept and total + c > budget and getattr(t, "name", "") not in keep:
            continue
        kept.append(t)
        total += c
    if len(kept) < len(tools):
        ids = {id(t) for t in kept}
        dropped = Counter((_group_of(getattr(t, "name", ""))[1] or "기타") for t in tools if id(t) not in ids)
        apps = " · ".join(f"{a} {n}" for a, n in dropped.most_common())
        print(f"[agent] tool budget: {len(tools)}개 → {len(kept)}개 (추정 {total}토큰 / 상한 {budget}) — 뺀 것: {apps}")
        _TOOL_CUT.clear()
        _TOOL_CUT.update(at=_now_iso(), before=len(tools), after=len(kept), budget=budget, est_tokens=total,
                         dropped_apps=dict(dropped.most_common(30)))
    return kept


def _relevance_order(tools: list, query: str, pinned, first, core_list: tuple, prefer) -> list[tuple]:
    """[(도구, 어휘 관련도)] — 콕 집은 것 → 핀 → 지목한 전문가의 분야 도구 → 어휘 + 시맨틱 융합 순위 → 어휘 관련도 → 상시 핵심 → 이름.
    캡 경로와 무캡 경로가 같은 순서를 쓴다(종전엔 무캡 경로가 이 정렬 없이 게이트웨이 순서 그대로 뒤에서부터 잘랐다 — F1)."""
    pin = set(pinned or []) | set(first or [])
    top = set(first or [])
    pref = {n: i for i, n in enumerate(prefer or []) if n not in pin}
    core = {n: i for i, n in enumerate(core_list)}
    # 관련도 — 이름+설명 어휘 겹침(_rank_tools 와 같은 _lex_scores — IDF 무게 · 앱 라벨은 지목할 때만, HA-07).
    qtok = _tok_query(query)
    _lex = _lex_scores(tools, qtok)

    def rel(t) -> float:
        return _lex.get(getattr(t, "name", ""), 0.0)
    # ⚠ 어휘 무매치(rel=0) 도구는 lex_order 에 넣지 않는다. 넣으면 '알파벳 순 전체 순열'이
    # 정상 랭킹인 척 RRF 에 들어가, 이름이 앞선 무관 도구가 진짜 관련 도구를 캡 밖으로 밀어낸다
    # (감사 실측: query_voc/search_voc 가 탈락해 모델이 product_code 를 지어내고 다른 제품
    # 통계를 답으로 내놓음 — 빈 결과보다 나쁜 '확신에 찬 오답').
    lex_order = [n for _, n in sorted(((-rel(t), getattr(t, "name", "")) for t in tools if rel(t) > 0))]
    sem_order = _semantic_order(query, tools)
    fused = _rrf(lex_order, sem_order) if sem_order else {}
    scored = [(t, rel(t)) for t in tools]
    return sorted(scored, key=lambda x: (
        # 콕 집은 것 → 핀 → 전문가 분야 도구 → 나머지
        0 if getattr(x[0], "name", "") in top else 1 if getattr(x[0], "name", "") in pin
        else 2 if getattr(x[0], "name", "") in pref else 3,
        pref.get(getattr(x[0], "name", ""), 0),                   # 분야 도구끼리는 전문가 점수 순
        -round(fused.get(getattr(x[0], "name", ""), 0.0), 6),    # 어휘+시맨틱 융합 순위
        -round(x[1], 3),                                          # 어휘 관련도(융합 미가용 시)
        core.get(getattr(x[0], "name", ""), len(core)),           # 상시 핵심
        getattr(x[0], "name", ""),
    ))


def _select_tools(tools: list, query: str = "", pinned: list[str] | None = None,
                  first: list[str] | None = None, core_names: tuple | None = None,
                  prefer: list[str] | None = None) -> list:
    """질의 관련도 기반 도구 선택 — 도구가 수백 개로 늘어도 '알파벳 순 절단'이 아니라
    '이 질문에 필요한 것부터' 남긴다. 우선순위: ① 콕 집은 도구(first) ② 사용자 지정(핀)
    ③ **지목한 전문가의 분야 도구(prefer)** ④ 질의 어휘 관련도 ⑤ 상시 핵심 ⑥ 나머지.
    캡 미설정(0)이면 전부 바인딩(회귀 0).

    prefer 가 핀보다 **뒤**인 것이 핵심이다 — 사람이 일부러 고른 것은 언제나 먼저고,
    전문가의 분야 도구는 '아무도 안 골랐을 때 그 사람이 늘 쓰는 것'을 채우는 자리다.

    first 는 핀 중에서도 먼저 사는 것이다 — 앱 하나를 통째로 핀하면 도구가 캡(dev 40)을 넘는데
    (StepForge 81·ReportArchive 70), 핀끼리는 관련도로만 갈려 사용자가 콕 집은 도구나 HE팀
    운영자의 입구 도구(get_guide·list_projects 등)가 캡 밖으로 밀려났다.

    core_names 는 상시 핵심 예약을 바꾼다(기본 _TOOL_PRIORITY). HE팀 운영자는 _GUIDE_TOOLS 만 쓴다."""
    core_list = (_TOOL_PRIORITY + _ONTO_CORE) if core_names is None else core_names
    if TOOL_MAX <= 0 or len(tools) <= TOOL_MAX:
        # 캡이 없거나 안 걸리면 **아무것도 떨어뜨리지 않는다** — 스키마 총량이 예산 안이면. 다만 순서는 바꾼다 —
        # 전문가 도구를 465종 한가운데 묻어 두면 모델이 못 찾고, 상태줄이 "우선" 이라고
        # 말한 것이 거짓이 된다. 운영(cae00)은 원격 GLM 이라 update-all 이 TOOL_MAX=0 을
        # 박으므로, 여기를 안 고치면 이 기능은 **운영에서 100% 무효**다(실측).
        # 캡이 없어도 **스키마 총량**은 본다. 종전엔 이 경로가 예산 검사를 통째로 건너뛰어
        # TOOL_MAX=0(운영 cae00)에서 465종이 무제한으로 붙었다 — 1M 창이라 지금은 넉넉하지만
        # 창 작은 모델로 바꾸면 조용히 400 이 된다. 상한은 env(TOOL_SCHEMA_BUDGET)가 아니라
        # **컨텍스트에서** 유도한다 — env 40,000 을 쓰면 cae00 의 의도된 전량 바인딩이 깨진다.
        budget = CHAT_TOOL_SCHEMA_BUDGET if CHAT_TOOL_SCHEMA_BUDGET > 0 else max(4000, int(_model_context_tokens() * 0.45))
        if sum(_tool_schema_cost(t) for t in tools) > budget:
            # 다 못 싣는다 — 캡 경로와 같은 순서로 세워 관련도 낮은 것부터 뺀다. 종전엔 게이트웨이 순서 그대로 뒤에서부터 잘라, 챗이
            # stcx(창 262,144 — 상한 117,964)로 바뀐 뒤 대화마다 678개 중 226개가 관련도와 상관없이 빠졌다(MaterialTwin 34종 전부 ·
            # 안내대까지 — 운영 10-18 실측, HWAXPortal docs/chat-depth-context F1). 예산 안이면 이 정렬(임베딩 조회)을 하지 않는다.
            tools = [t for t, _ in _relevance_order(tools, query, pinned, first, core_list, prefer)]
        elif prefer:
            _r = {n: i for i, n in enumerate(prefer)}
            tools = sorted(tools, key=lambda t: _r.get(getattr(t, "name", ""), len(_r)))
        keep = _GUIDE_FRONT + (_ONTO_CORE if core_names is None else ())
        # 고정 몫(안내대 · 코어)은 정해진 차례(keep — 안내대 먼저)로 맨 앞에 — 관련도 순서로 다시 놓이면 턴마다 도구 몫의 앞부분이 바뀌어
        # 접두 캐시가 깨진다(HWAXPortal docs/request-1029 W-3). 요청서는 이름순을 권했지만 정해진 차례도 턴마다 같고 안내대가 앞에 남는다.
        _ki = {n: i for i, n in enumerate(keep)}
        front = sorted((t for t in tools if getattr(t, "name", "") in _ki), key=lambda t: _ki[getattr(t, "name", "")])
        tools = front + [t for t in tools if getattr(t, "name", "") not in keep]
        return _fit_schema(tools, budget, keep=keep)
    pin = set(pinned or []) | set(first or [])
    ordered = _relevance_order(tools, query, pinned, first, core_list, prefer)
    kept = [t for t, _ in ordered[:TOOL_MAX]]
    # 토큰 예산 — TOOL_MAX 는 '개수' 캡이라 실제 프롬프트 비용을 전혀 보장하지 못한다.
    # 스키마가 큰 도구가 몰리면 캡을 지켜도 컨텍스트 초과 400 으로 챗이 죽고, 사용자는
    # '에이전트 처리 중 오류'만 본다(감사: dev 로그에 동일 400 3건). 개수와 별개로 추정
    # 토큰 합계에도 상한을 걸어, 넘치면 관련도 낮은 것부터 떨어뜨린다.
    if TOOL_SCHEMA_BUDGET > 0:
        kept = _fit_schema(kept, TOOL_SCHEMA_BUDGET)
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
    # 채움말 — 긴 설명의 도구에 우연히 적중했다(HWAXPortal docs/request-1027 HA-07)
    "있으면", "같이", "뭐야", "그리고", "그럼", "혹시", "그냥", "어떻게", "얼마나", "정도", "주세요", "싶어",
}
_JOSA_T = ("으로", "에서", "에게", "까지", "부터", "이나", "을", "를", "이", "가", "은", "는",
           "의", "에", "과", "와", "도", "로", "만")


def _tok_query(text: str) -> set[str]:
    """질의 → 낱말 집합. 한 글자 낱말과 띄어 쓴 조사 단독은 버린다 — 도구 설명의 아무 낱말 안에나 들어가 우연 적중했다(15/16 질문에서 허브
    핵심 도구의 적중이 잡음뿐이었다 — HA-07). 종전엔 한 글자 한글(휨 등)을 남겼다 — 그런 질의는 의미 순위(임베딩)가 잡는다."""
    out = set()
    for t in re.split(r"[\s,./·|()\[\]{}:;\"'?!]+", (text or "").lower()):
        if not t:
            continue
        for j in sorted(_JOSA_T, key=len, reverse=True):   # 조사 제거(어간 2자+ 남을 때만)
            if t.endswith(j) and len(t) - len(j) >= 2:
                t = t[: -len(j)]
                break
        if t in _TOOL_STOP or t in _JOSA_T:
            continue
        if len(t) >= 2:
            out.add(t)
    return out


# 어휘 관련도 — 낱말마다 IDF 무게(여러 도구에 흔한 낱말은 덜 센다)로 '적중한 낱말의 무게 / 전체 무게'(0~1). 종전엔 적중 낱말 수 / 질의 낱말
# 수라 흔한 낱말 하나가 드문 낱말 하나와 같은 무게였다. 앱 라벨([라벨] — _tag_tool_app 이 설명 앞에 붙인다)은 질의가 그 앱을 지목할 때만 센다 —
# 라벨의 '(시험)' 때문에 '시험' 이 든 질문마다 230개가 적중했다(C-4). 지목 판정은 괄호 꼬리를 뺀 라벨 이름으로 한다(HA-07).
_LABEL_HEAD = re.compile(r"^\[([^\]]*)\]\s*")


def _lex_profile(t, qtok: set) -> str:
    name, desc = getattr(t, "name", ""), getattr(t, "description", "") or ""
    m = _LABEL_HEAD.match(desc)
    if m:
        desc = desc[m.end():]
        core = re.sub(r"\([^)]*\)", " ", m.group(1)).lower()
        if any(k in core for k in qtok):
            desc = f"{m.group(1)} {desc}"
    return f"{name} {desc}".lower().replace("_", " ")


def _lex_scores(tools, qtok: set) -> dict:
    """도구 이름 → 어휘 관련도(위 규칙). 질의 낱말이 없으면 빈 사전."""
    import math
    if not qtok:
        return {}
    profs = [(getattr(t, "name", ""), _lex_profile(t, qtok)) for t in tools]
    n = len(profs)
    w = {k: math.log((n + 1) / (sum(1 for _, pr in profs if k in pr) + 1)) + 1.0 for k in qtok}
    tot = sum(w.values())
    return {name: sum(w[k] for k in qtok if k in pr) / tot for name, pr in profs}


# 도구 → 소유 MCP 앱(백엔드) 매핑 — 166개가 평평하게 쏟아지면 고르기 어려워, 게이트웨이의
# /tools-map 으로 '어느 앱의 기능인지'를 붙여 계층 선택이 되게 한다(짧은 TTL 캐시).
_GW_HTTP = os.environ.get("GATEWAY_HTTP_BASE", "http://127.0.0.1:9110")
# routine_read — 게이트웨이가 읽기로 확인한 노출 이름(frozenset). 옛 게이트웨이는 그 칸이 없어 None('모름')이다. 상태 기다림(HA-28)의 울타리로만
# 쓰고, 읽는 쪽은 .get 으로 읽는다(시험이 그 칸 없는 사전으로 통째로 바꿔 끼운다).
_TOOLS_MAP_CACHE: dict = {"at": 0.0, "map": {}, "areas": {}, "area_meta": {}, "routine_read": None}
_GROUP_LABEL = {
    "ai-data-hub": "AI 데이터 허브", "mx-white-paper": "MX 백서", "reportarchive": "리포트 아카이브",
    "signalforge": "SignalForge VOC", "smart-twin-cluster": "시뮬레이션 클러스터",
    "smart-twin-mcp": "시뮬레이션 실행(SmartTwin)",          # 연결 안 된 박스에서도 게이트웨이 라벨과 같은 이름으로
    "heax-thermal_shock_mcp": "열충격 해석", "heax-materialtwin_web": "재료 물성(MaterialTwin)",
    "heax-laminate_analyzer_mcp": "적층 복합재 해석", "heax-web_design_agents": "웹 디자인 에이전트",
    "_gateway": "게이트웨이 공통",
}


_APPS_CACHE: dict = {"apps": {}, "at": 0.0}
# /tools-map 조회가 실패한 뒤 다시 묻기까지(초) — 그동안은 가진 것(없으면 빈 것)으로 간다. 5초 한도는 짧아야
# 맞다(죽은 게이트웨이를 재는 값이다). 문제는 실패를 캐시하지 않던 것이다 — 이 조회는 동기 urlopen 이라 이벤트
# 루프를 통째로 막는데, 게이트웨이가 매달리면 도구 수 × 좌석 수만큼 5초씩 다시 물었다. 그 동안은 심의의
# heartbeat(ping)까지 멈춰 바깥에서는 서버가 죽은 것처럼 보인다(컨텍스트 조회 _CTX_RETRY_S 와 같은 방식).
_GW_RETRY_S = 60.0


def _gw_apps() -> dict:
    """게이트웨이 /tools-map 의 apps[] — {앱키: {label, description, tool_count, reachable}}.

    라벨의 정본은 게이트웨이다(heax 앱은 registry name 을 그대로 싣는다). 여기 없을 때만
    _GROUP_LABEL 표로 폴백한다 — 하드코딩 표는 신규 앱이 붙을 때마다 틀린 이름을 노출한다."""
    import time
    import urllib.request
    now = time.time()
    if _APPS_CACHE["apps"] and now - _APPS_CACHE["at"] < 300:
        return _APPS_CACHE["apps"]
    if time.monotonic() < _APPS_CACHE.get("retry_at", 0.0):
        return _APPS_CACHE["apps"]      # 방금 못 물어봤다 — 죽은 게이트웨이를 호출마다 5초씩 기다리지 않는다
    try:
        with urllib.request.urlopen(f"{_GW_HTTP}/tools-map", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        apps = {a["app"]: a for a in (data.get("apps") or []) if a.get("app")}
        if apps:
            _APPS_CACHE.update({"apps": apps, "at": now})
    except Exception as exc:  # noqa: BLE001 — 게이트웨이 불통은 비치명적. 폴백 표로 계속 간다.
        print(f"[tools] apps fetch failed — {_GW_RETRY_S:.0f}초 뒤 다시 묻는다: {exc!r}")
        _APPS_CACHE["retry_at"] = time.monotonic() + _GW_RETRY_S
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
    if time.monotonic() < _TOOLS_MAP_CACHE.get("retry_at", 0.0):
        return _TOOLS_MAP_CACHE["map"]  # 방금 못 물어봤다 — _GW_RETRY_S 뒤에 다시 묻는다(_GW_RETRY_S 주석)
    try:
        with urllib.request.urlopen(f"{_GW_HTTP}/tools-map", timeout=5) as r:
            data = json.loads(r.read()) or {}
        m = data.get("map") or {}
        if m:
            # 영역(tool_areas.json)도 같은 응답에 온다 — 따로 받지 않는다. 구 게이트웨이면 빈 채로
            # 두어 도구가 '미분류' 로 보인다(분류를 지어내지 않는다).
            _rr = data.get("routine_read")
            _TOOLS_MAP_CACHE.update({"at": now, "map": m, "areas": data.get("areas") or {},
                                     "area_meta": {a["area"]: a for a in (data.get("area_meta") or [])
                                                   if isinstance(a, dict) and a.get("area")},
                                     "routine_read": (frozenset(x for x in _rr if isinstance(x, str))
                                                      if isinstance(_rr, list) else None)})
    except Exception as exc:  # noqa: BLE001 — 매핑 실패 시 그룹 없이 동작(회귀 0)
        print(f"[tools] map fetch failed — {_GW_RETRY_S:.0f}초 뒤 다시 묻는다: {exc!r}")
        _TOOLS_MAP_CACHE["retry_at"] = time.monotonic() + _GW_RETRY_S
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
    lex = _lex_scores(list(tools.values()), qtok)
    for name, t in tools.items():
        sc = lex.get(getattr(t, "name", name), 0.0)
        if sc > 0:
            scored.append((sc, name, (getattr(t, "description", "") or "")[:160]))
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
# 자연어는 '누가 있나' 만 묻는 질문일 때만 가로챈다 — 목록 답은 LLM 을 거치지 않아 다른 물음을 말없이 버린다(HWAXPortal docs/request-1030
# HA-31 · DW-13 — '…전문가 에이전트를 추천하고, 추천 근거가 된 기록 수를 알려줘.' 가 목록만 받았다). 수 · 근거 · 비교 · 기준 · 조건,
# 둘째 요청(이음 낱말 · 문장이 둘 · 찾는 말 앞에 맺은 요청 · 뒤에 이은 요청)이 보이면 챗 에이전트로 보낸다. 잘못 보내도 챗이
# recommend_agents 로 답하지만 잘못 가로채면 물음이 사라진다 — 의심스러우면 챗이다.
_AGENT_SEARCH_OTHER_ASK_RE = re.compile(
    r"몇|개수|갯수|건수|횟수|인원수|명수|점수|얼마|(?:카드|전문가|에이전트|사람|문서|표본|샘플)수|\d+\s*(?:명|개|건|위)"  # 수(붙여 쓴 꼴 · 'N명만')
    r"|(?<![가-힣])수(?!\s*[가는도만]?\s*[있없])(?:[를는가도만와과로의에]|까지|이?랑|하고|별|만큼|보다)?(?![가-힣])"  # ('할 수 있' 은 아니다)
    r"|근거(?!리)|기록|출처|이유|왜(?!곡)"                                               # 근거('근거리' · '왜곡' 은 분야 낱말)
    r"|비교|차이|누가\s*더|어느\s*(?:쪽|것|게|편)|\bvs\b"                                 # 비교
    r"|제일|가장|최고|순위|랭킹|순으로|순서|상위|신뢰도"                                   # 기준 — 목록은 관련도순으로만 줄을 세운다
    r"|조건|경우|만약|만일"                                                              # 조건
    r"|(?<![있없])(?:으|하|되|나|크|오|가|보|쓰|리|르|기|우|이|라|다)면(?![가-힣])"         # '-면' 은 활용 꼴만(표면 · 계면 · 도면은 분야 낱말,
    r"|(?:어|아|워|려|라|커|꺼|깨|해|서)지면(?![가-힣])"                                    # 접지면 · 지면도 그렇고 '주시면' 은 부탁이다)
    r"|[있없](?:으|다)면(?![가-힣])(?!\s*(?:좀\s*)?(?:알려|추천|보여|소개|찾|말해))"          # '있으면 알려줘' 는 조건이 아니라 '있나' 다
    r"|(?:검색|추천|찾|알려|보여)[가-힣]*?(?:\s*주)?고(?![가-힣])(?!\s*[싶있])"             # 둘째 요청('찾고 싶어' 는 아니다)
    r"|그리고|그러고|그다음|그런데|근데|이어서|아울러|또한"
    r"|[.?!;…](?![0-9A-Za-z])\s*[^\s.?!;~…)]|\n\s*\S",                                # 문장이 둘('2.5D' · 'v1.2' 의 점은 아니다)
    re.IGNORECASE)
# 찾는 말(_AGENT_SEARCH_RE) 앞에 맺은 요청('…알려줘 전문가도' · '…설명하고 전문가'), 뒤에 이은 요청(쉼표 · 이음 어미 -고 · -서 · -며 뒤의 글,
# '카드도' 같은 덧붙임, '다음 · 뒤 · 후 · 또'). 앞은 분야 낱말 자리라 맺은 꼴만 본다 — 이음 어미를 다 보면 '센서 · 사고' 가 걸린다.
_AGENT_SEARCH_PREV_ASK_RE = re.compile(r"줘|주세요|줄래|주실래|[하되](?:고|며|면서)(?![가-힣])(?!\s*(?:싶|있|계))|[해돼]서(?![가-힣])")
_AGENT_SEARCH_NEXT_ASK_RE = re.compile(
    r",\s*[^\s,.?!~]|(?:고(?!\s*(?:싶|있|계))|서|며)(?![가-힣])\s*[^\s.?!~]|[가-힣]도(?![가-힣])"
    r"|(?:(?<![가-힣])|(?<=[한은는인]))(?:다음|뒤|후|또)(?:에|엔|로)?(?![가-힣])")


def is_agent_search(message: str, pinned: bool = False) -> bool:
    """전문가 목록으로 답할 턴인가. 빗금 트리거는 사람이 대놓고 적은 의도라 늘 그렇다(전문가를 고른 대화에서도 — 목록 카드에서 바꿔 고른다).
    자연어는 80자 이하 · 다른 물음 없음 · 고른 전문가 없음(pinned — 운영자 포함, _persona_keys)일 때만이다. 고른 대화의 질문은 그
    전문가가 답한다(DW-13 은 AI 데이터 허브 운영자를 고른 대화에서도 가로챘다)."""
    m = (message or "").strip()
    if any(m.startswith(t) for t in _AGENT_SEARCH_TRIGGERS):
        return True
    if pinned or len(m) > 80:
        return False
    mt = _AGENT_SEARCH_RE.search(m)
    return bool(mt) and not (_AGENT_SEARCH_OTHER_ASK_RE.search(m) or _AGENT_SEARCH_PREV_ASK_RE.search(m[:mt.start()])
                             or _AGENT_SEARCH_NEXT_ASK_RE.search(m[mt.end():]))


# 추천 질의는 분야 낱말이다 — '전문가 · 추천해 · 줘' 는 요청이라 그대로 넘기면 설명에 그 낱말이 든 사람마다 겹침 점수가 붙어 무관한 전문가가
# 오른다(HA-31 · AIDataHub AD-19). 찾는 말 앞에서는 '어떤' 과 끝에 붙은 전문가 · 에이전트만, 뒤에서는 찾는 말의 남은 활용과 부탁 낱말만
# 뺀다 — 분야 낱말이 찾는 말로 시작하거나 그것을 품어도('검색 엔진' · '체크리스트' · 'he-agent-builder') 남는다. 다 빠져 비면 추천 없이
# 명부 · 분야만 낸다(종전엔 요청 낱말로 추천했다).
_AGENT_QUERY_HEAD_RE = re.compile(r"(?<!\S)(?:어떤|무슨|어느|좀)(?!\S)|(?:\s*(?:전문가|에이전트)[가-힣]*)+\s*$")
_AGENT_QUERY_TAIL_RE = re.compile(
    r"(?<!\S)(?:(?:검색|추천|찾|알려|목록|리스트|보여|뭐|무엇|어떤|있|부탁|감사|고맙|싶|좋겠|가능|주[세시실]|줄래)[가-힣]*|좀|해|줘|줘요|수)"
    r"[?!.~,]*(?!\S)")


def _agent_search_query(text: str) -> str:
    m = (text or "").strip()
    mt = _AGENT_SEARCH_RE.search(m)
    if not mt:
        return m
    head = _AGENT_QUERY_HEAD_RE.sub(" ", m[:mt.start()])
    tail = _AGENT_QUERY_TAIL_RE.sub(" ", re.sub(r"^\w*[?!.~,]*", "", m[mt.end():]))   # 찾는 말의 남은 활용(추천'해' · 있'는지')부터 뺀다
    return " ".join(f"{head} {tail}".split())


def strip_agent_trigger(message: str) -> str:
    """전문가 검색의 추천 질의. 빗금 트리거면 그 뒤에 사람이 적은 글 그대로다(정리하지 않는다 — 'he-agent-builder' 같은 키도 온다),
    자연어면 찾는 말을 뺀 분야 낱말이다(_agent_search_query)."""
    m = (message or "").strip()
    for t in _AGENT_SEARCH_TRIGGERS:
        if m.startswith(t):
            return m[len(t):].strip()
    return _agent_search_query(m)


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



# 이 서버의 챗·심의 에이전트가 묶지 않는 게이트웨이 도구 — 외부 응답자 과제 도구(external_llm.TASK_TOOLS). 게이트웨이는 이 서버의
# /mcp 를 사용자 신원으로 중계하므로, 묶으면 주입된 지시를 받은 GLM 턴이 그 사람의 기다리는 과제를 집거나 답을 낼 수 있다.
# 도구 목록을 받는 곳(_get_tools_retry — 챗·심의·카탈로그가 다 지난다)에서 빼고, 상시 묶이는 invoke_tool 로 부르는 것도 막는다(_cap_tool).
_AGENT_DENY_TOOLS = frozenset(_external_llm.TASK_TOOLS)
# 패널(MCP Apps)을 그리는 클라이언트 전용 도구 — 포털 챗 · 심의는 패널을 그리지 못해, 부르면 사용자는 패널 없이 글 몇 줄만 본다
# (10-14 실측 — open_resin_panel 설명은 '묻지 않아도 먼저 호출' 이다). 같은 거부 길(목록에서 빼고 invoke_tool 로도 막는다)을 쓰되 집합은 따로
# 둔다(_AGENT_DENY_TOOLS 는 외부 응답자 과제 도구와 같아야 한다). **정확한 이름만** — `*panel*` 무늬는 risk_list_panels 같은 조회 도구까지
# 막는다. 포털이 패널을 그리게 되면(MCP 업로드 M-2) 이 집합을 푼다(HWAXPortal docs/request-1027 HA-05). ARP 의 arp_upload_open 은 무엇을
# 돌려주는지 실측이 없어 넣지 않았다(같은 꼴로 확인되면 더한다).
_CHAT_PANEL_TOOLS = frozenset({"open_workflow_panel", "open_resin_panel", "open_camfpcb_panel", "open_frc_panel", "open_gi_panel",
                               "open_magnet_panel", "panel_upload_begin", "panel_upload_append", "panel_upload_commit"})


def _chat_panel_tool(name: str) -> bool:
    """패널 전용 도구인가 — 이름 그대로이거나 게이트웨이 별칭·충돌 접두(<앱키>_<도구>)를 단 꼴."""
    n = str(name or "").strip().lower()
    return any(n == t or n.endswith("_" + t) for t in _CHAT_PANEL_TOOLS)


# 분석 루틴 도구(게이트웨이 로컬 — HWAXPortal docs/analysis-routines R2) — 일반 챗 ReAct 는 묶지 않는다. 루틴은 사람이 골라 켜는 것이고(챗의
# 루틴 모드는 코드가 단계를 돈다 — R3), 자유 턴의 모델이 실행을 시작하거나 원장에 쓰면 사람이 고르지 않은 연쇄가 돈다. 같은 거부 길(목록에서
# 빼고 invoke_tool 로도 막는다)을 쓰되 집합은 따로 둔다(_AGENT_DENY_TOOLS 는 외부 응답자 과제 도구와 같아야 한다).
_ROUTINE_TOOLS = frozenset({"routine_list", "routine_get", "routine_run_start", "routine_call", "routine_step_done",
                            "routine_report_submit"})


def _agent_denied(name: str) -> bool:
    """이 서버의 에이전트가 묶지도 부르지도 않는 도구인가 — 이름 그대로이거나 게이트웨이 별칭·충돌 접두(<앱키>_<도구>)를 단 꼴."""
    n = str(name or "").strip().lower()
    return (any(n == t or n.endswith("_" + t) for t in _AGENT_DENY_TOOLS) or _chat_panel_tool(n)
            or n in _ROUTINE_TOOLS)


# 이 서버의 에이전트가 외부 응답자 칸을 실어 부르지 못하는 심의 도구 — 이름 규칙은 _agent_denied 와 같다(게이트웨이 별칭 포함).
# 게이트웨이는 이 호출을 사용자 신원으로 중계하므로, 지식카드에 숨은 지시를 받은 GLM 턴이 advanced.llm=external 과
# consent_external: true 를 스스로 달면 외부 심의가 서고 원장에 거짓 동의가 남는다(X-D6). 사람의 동의는 사람의 클라이언트에서만 온다.
_AGENT_NO_EXTERNAL = ("deliberate_start", "deliberate_continue")
# 리스크 앱의 잡 시작 도구(X-D7 ④) — 같은 까닭이다. 판단 LLM 을 Claude 로 고르거나(앱의 이름 claude · 엔진의 이름 external) 반출 동의
# 칸(consent_external — 값과 상관없이)을 실은 호출은 보내지 않는다. 그 도구의 인자는 평평하다(바깥 칸 — advanced 가 없다).
_AGENT_NO_EXTERNAL_JOB = ("risk_start_job",)
# 게이트웨이 REST 다리 — 본문에 반출 동의 칸이 있으면(어느 사이트 · 경로든) 보내지 않고, 리스크 앱의 잡 경로(…/targets/<key>/jobs ·
# …/jobs/<id>/<action>)에 판단 LLM claude · external 을 실은 본문도 보내지 않는다(llm 은 흔한 칸 이름이라 그 경로에서만 본다).
_AGENT_REST_BRIDGE = ("rest_call",)
_EXTERNAL_LLM_NAMES = ("claude", "external")
_RISK_JOB_PATH = re.compile(r"(?:^|/)(?:targets/[^/]+/jobs|jobs/[^/]+/[^/]+)/*$")


def _named(name, tools) -> bool:
    """도구 이름이 그 가운데 하나인가 — 이름 그대로이거나 게이트웨이 별칭·충돌 접두(<앱키>_<도구>)를 단 꼴(_agent_denied 와 같은 규칙)."""
    n = str(name or "").strip().lower()
    return any(n == t or n.endswith("_" + t) for t in tools)


def _picks_external(fields: dict) -> bool:
    """칸 이름이 llm 인 값이 claude · external 이거나(앞뒤 공백 · 대소문자는 맞춘다 — 엔진의 읽기와 같다) 반출 동의 칸이 있는가(값과
    상관없이)."""
    keys = {str(k).strip().lower(): v for k, v in fields.items()}
    return (str(keys.get("llm") or "").strip().lower() in _EXTERNAL_LLM_NAMES
            or _external_llm.CONSENT_FIELD in keys)


def _arg_dict(v) -> dict | None:
    """도구 인자의 객체 칸 — dict 이거나, FastMCP 가 도구에 넘기기 전에 풀어 주는 JSON 글(func_metadata.pre_parse_json)이면 푼 dict.
    그 밖은 None 이다."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


def _agent_external_call(name, args) -> bool:
    """이 서버의 에이전트가 부르는 심의 시작·이어하기가 외부 응답자를 고르거나(advanced.llm — 엔진과 같은 읽기, _wants_external)
    반출 동의 칸(consent_external — 값과 상관없이, 바깥 칸이든 advanced 안이든)을 싣는가. 리스크 앱의 잡 시작(risk_start_job)이 llm
    claude · external 이나 동의 칸을 싣는가(X-D7 ④). REST 다리(rest_call)의 본문이 동의 칸을 싣거나, 리스크 앱 잡 경로의 본문이 llm
    claude · external 을 싣는가. 그 밖의 도구 · 그 칸이 없는 호출은 거짓이다(종전 그대로 간다)."""
    if _named(name, _AGENT_NO_EXTERNAL):
        args = _arg_dict(args) or {}
        adv = _arg_dict(args.get("advanced")) or {}
        keys = {str(k).strip().lower() for k in (*args, *adv)}
        return _wants_external(adv) or _external_llm.CONSENT_FIELD in keys
    if _named(name, _AGENT_NO_EXTERNAL_JOB):
        return _picks_external(_arg_dict(args) or {})
    if _named(name, _AGENT_REST_BRIDGE):
        args = _arg_dict(args) or {}
        body = _arg_dict(args.get("body")) or {}
        if _external_llm.CONSENT_FIELD in {str(k).strip().lower() for k in body}:
            return True
        path = str(args.get("path") or "").strip().split("?", 1)[0].split("#", 1)[0]
        return bool(_RISK_JOB_PATH.search(path)) and _picks_external(body)
    return False


# ── 확인 관문(HWAXPortal docs/request-1030 HA-29 · design-ha29.md) ─────────────────────────────────────────────────────────────
# DW-03(10-30 QA) — StepForge run_job 을 사람에게 묻지 않고 두 번(graph · 판 전체 drop_check) 걸었다. drop_check 는 10% 에서 1,812초를 돌다 사용자가
# 취소했다(단일 러너를 잡고 있었다). 프롬프트의 '먼저 확인' 은 지켜지지 않았다 — 그래서 코드가 막는다. 막는 도구는 셋으로 가른다.
#   job     — 잡을 시작하는 도구(아래 바닥 목록). 엄하게 — 주문 동사에 더해 그 일을 글자로 불러야(도구 이름 · 무엇 칸 값 · 파일 이름 · 콕 집은
#             도구) 통과한다. 잡은 단일 러너 · 클러스터 · GLM 을 몇 분 ~ 몇 시간 잡는다.
#   write   — 운영자 정의(포털 infra/personas/he-team.json)의 writes. 너그럽게 — 질문 · 부정이 없는 주문 동사면 통과한다(저작 흐름이 쓰기마다
#             끊기면 박스에서 관문을 끄게 되고, 그러면 DW-03 도 돌아온다).
#   confirm — 운영자 정의의 confirm(지우기 · 권한 · 되돌리기 어려운 것). 늘 카드다.
# 게이트웨이의 쓰기 표지(/tools-map write)는 쓰지 않는다 — 이름 낱말 규칙이라 docforge_convert · 낙하 제출을 놓치고 읽기를 쓰기로 적는다(dev 실측).
# 막으면 부르지 않고 글을 돌려준다(던지면 턴의 except 로 가 자동 재시도가 그래프를 다시 돈다). 그 단계가 끝나면 다음 모델 호출 앞에서 턴을 끊고
# 턴 끝에 결정적인 확인 카드(SSE confirm + 본문 끝 글, 번호 cf-…)를 낸다. 승인은 다음 사람 말로 받고, 서버가 저장한 호출을 처음 꼴 그대로 한 번
# 보낸다. 심의 좌석 · 띵킹 · 분석 루틴 · 카탈로그는 이 턴 맥락이 없어 관문이 서지 않는다.
# 목록의 정본은 포털 리포다 — 에이전트 서버 · 포털은 늘 한 박스에 형제 리포로 있어(dev ~/claude · cae00 ~/Projects) 그 파일을 직접 읽는다(코어
# 어휘표와 같은 선례). CONFIRM_LISTS_PATH 로 다른 파일을 가리킬 수 있고, 상대 경로는 이 리포 루트가 앵커다.
_CONFIRM_LISTS_ENV = os.environ.get("CONFIRM_LISTS_PATH", "").strip()
CONFIRM_LISTS_PATH = (Path(__file__).resolve().parent / Path(_CONFIRM_LISTS_ENV).expanduser() if _CONFIRM_LISTS_ENV
                      else Path(__file__).resolve().parent.parent / "HWAXPortal" / "infra" / "personas" / "he-team.json")
_CONFIRM_LISTS: dict = {"key": None, "writes": frozenset(), "confirm": frozenset(), "error": "아직 읽지 않음"}
# 잡 시작 바닥 목록 — 운영자 정의 없이도 막는다(요청서가 이름 댄 잡 · 클러스터 제출 · StepForge 의 다른 잡 넣기 — 같은 단일 러너 · 심의 · 리스크 잡).
# 이름 규칙은 거절 관문과 같다(_named — 그대로이거나 게이트웨이 별칭 · 충돌 접두를 단 꼴).
_CONFIRM_JOB_TOOLS = (
    "run_job", "run_checklist", "run_analysis", "run_compare", "run_curvature", "docforge_convert",
    "submit_job", "slurm_submit_job", "smarttwin_submit", "submit_lsdyna_job", "fullangle_drop_simulation",
    "single_drop_simulation", "job_rerun", "catalog_run", "recipe_run", "run_recipe", "run_operation",
    "remesh_parts", "export_step", "build_cavity", "intake", "deliberate_start", "deliberate_continue", "risk_start_job",
    # StepForge 의 다섯째 잡 넣기 — 덱(.k)에서 STEP 을 짓는 deck_step 잡을 단일 러너에 넣고 run(기본 detect)까지 이어 건다(StepForge b2c70f1
    # app/mcp_server.py — jobs.enqueue 를 부르는 다섯 곳). 운영자 정의에도 게이트웨이 write 표지에도 없어 어떤 말에서도 카드 없이 나갔다(반대 검토 2차).
    "extract_surface_to_cad")
# '이번 말이 시켰다' 판정 — 주문 동사 · 질문 · 부정. 질문 · 부정은 주문을 지운다(어긋나면 카드 쪽으로 간다 — 엄한 쪽이 틀려도 카드가 하나 더 뜰 뿐이다).
_ORDER_RE = re.compile(
    r"(?:실행|재실행|제출|시작|변환|등록|생성|저장|업로드|추가|수정|변경|삭제|취소|적용|반입|게시)\s*(?:을|를)?\s*"
    r"(?:해\s*(?:줘|주세요|주십시오|줄래|봐)|하자|합시다|하세요|해라|하라|해(?=\s*[.!~]*\s*$))"
    r"|(?:돌려|걸어|만들어|올려|지워|넣어|바꿔|고쳐|붙여)\s*(?:줘|주세요|주십시오|줄래|봐|라)"
    r"|(?:돌려|걸어|만들어|올려|지워|넣어|바꿔|고쳐|붙여|돌리자|걸자)(?=\s*[.!~]*\s*$)"
    r"|(?:^|[\s,.;!])(?:please\s+)?(?:run|rerun|submit|start|launch|execute|convert|create|register|upload|save|delete|remove|cancel|apply)\b",
    re.I | re.M)
_ASK_RE = re.compile(r"\?|(?:까요?|나요|니|냐|는지|을지|ㄹ지|어때|되나|돼|될까|가능해|가능할까)\s*[.!~]*\s*$", re.M)
_NEG_RE = re.compile(r"(?:지\s*마|지\s*말|하지\s*않|말고|말아|안\s*(?:돼|된|해|함)|금지|멈춰|중지|그만|아니(?:요|야|오)?(?=[\s.,!~]|$)|아뇨"
                     r"|\bno\b|\bnope\b|don'?t|do\s+not|never|\bstop\b)", re.I)
# 카드의 답 — 번호가 없는 짧은 답(말 전체가 이 꼴일 때만). 승인 낱말(yes 무리 · 실행 무리)이 하나는 있어야 한다(_affirmed) — 모든 무리가 선택이라
# '.' · '~' · '!' 같은 문장부호만, '어...' 같은 망설임만인 말도 맞아 서버가 저장한 잡을 곧바로 보냈다(반대 검토 2차). '확인' 은 뺐다 — '확인해 볼게'
# · '확인했어' 처럼 승인이 아닌 말과 갈리지 않는다(카드 글은 '실행' 이라고 답하라고 한다).
_AFFIRM_RE = re.compile(
    r"^(?:(?P<yes>응|어|네|넵|넹|예|그래|좋아|좋아요|ㅇㅇ|ㅇㅋ|오케이|ok|okay|yes|y|go)[\s,.!~]*)?"
    r"(?:(?P<verb>실행|진행|승인|돌려|걸어|시작)\s*(?:해|해요|하자|합니다|할게|하세요|해라|해\s*줘|해\s*주세요|줘|주세요)?)?[\s.!~]*$", re.I)
_DECLINE_RE = re.compile(r"^(?:아니(?:요|야|오)?|아뇨|no|nope|취소|그만|됐어|괜찮아|하지\s*마|안\s*해)[\s.!~]*$", re.I)
_CF_ID_RE = re.compile(r"\bcf-[0-9a-f]{12}\b")
# 잡 부류의 주문 — 잡을 거는 동사(실행 · 재실행 · 돌려 · 제출 · 시작 · 걸어 · 변환 · 수행 · run 해줘)가 그 일의 낱말 바로 뒤에 와야 한다(사이에는 조사 ·
# '잡' · '한 번' 같은 짧은 말만). 영어는 문장 머리의 명령꼴(run · submit · start …) 뒤에 그 낱말이 있어야 한다. 종전엔 _ORDER_RE(만들어줘 · 취소해줘 ·
# 삭제해줘 · 'run' 이 명사로 쓰인 곳까지)가 말 어디에서든 맞고 대상 낱말도 말 어디에든 있으면 됐다 — 'drop_check 결과로 표 만들어줘' · 'drop_check 잡
# 취소해줘' 에도 판 전체 drop_check 이 카드 없이 돌았다(반대 검토 2차 — DW-03 재발). 내일 · 나중에 같은 미루는 말이 있으면 지금 시킨 것이 아니다.
_JOB_VERB = (r"(?:(?:재?실행|제출|시작|변환|수행)\s*(?:을|를)?\s*(?:해\s*(?:줘|주세요|주십시오|줄래|봐)|하자|합시다|하세요|해라|하라|해(?=\s*[.!~]*\s*$)"
             r"|시켜\s*(?:줘|주세요|주십시오|봐))"
             r"|(?:돌려|걸어)\s*(?:줘|주세요|주십시오|줄래|봐|라)|(?:돌려|걸어|돌리자|걸자)(?=\s*[.!~]*\s*$)"
             r"|(?:re)?run\s*(?:해\s*(?:줘|주세요|주십시오|줄래|봐)|하자|해(?=\s*[.!~]*\s*$)))")
_JOB_VERB_RE = re.compile(_JOB_VERB, re.I | re.M)
_JOB_AFTER = re.compile(r"(?:\s*(?:[`'\"’”」』)\]]|을|를|이|가|은|는|도|만|으로|로|좀|잡을|잡으로|잡도|잡|작업을|작업|한\s*번|다시|바로|지금|먼저|얼른"
                        r"|빨리|당장|부터))*\s*" + _JOB_VERB, re.I | re.M)
_JOB_EN = re.compile(r"(?:^|[.!?\n;]\s*)(?:please\s+)?(?:run|rerun|submit|start|launch|execute|convert)\b([^.!?\n;]*)", re.I)
_LATER_RE = re.compile(r"내일|모레|나중에|이따|다음\s*(?:주|달|번)|tomorrow|later", re.I)
# '그 일을 글자로 불렀나' 에 쓰는 무엇 칸 — 식별자 칸(project_id · job_id …)은 쓰지 않는다('P1 돌려줘' 가 그 과제의 아무 잡이나 통과시키면 DW-03 이
# 그대로 남는다). 모델이 지어 넣는 자유 글 칸(name — 새로 만들 과제 · 산출물 이름)도 쓰지 않는다 — 모델은 그 이름을 사람 말의 낱말로 짓기 쉬워
# 사람이 묻기만 한 말이 그 잡을 '부른' 셈이 됐다(반대 검토 2차 — StepForge run_job(kind=deck_step, params.name='보드 낙하 검토')).
_CONFIRM_WHAT_KEYS = ("kind", "operation", "tool", "job", "recipe", "action", "scenario", "template")
_CONFIRM_FILE_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_CONFIRM_MARK = "[확인 대기]"            # 막은 호출의 결과 글 표지 — _TOOL_FAIL_MARK 로 시작해 _result_ok 가 거짓이다(활동 · 절차 원장이 성공으로 접지 않는다)
_CONFIRM_TTL_S = 1800                    # 카드 수명 — 포털의 PAT 창 · AGENT_CACHE_TTL_S 와 같은 30분(승인된 호출은 승인한 턴의 새 자격으로 나간다)
_CONFIRM_MAX = 256                       # 카드 저장 상한 — 넘으면 오래된 것부터
_CONFIRM_WORKLOAD_S = 15.0               # 예상 작업량 묻기(목록 받기 + estimate_cost) 전체 — 게이트웨이 600초 · 엔진 900초보다 안쪽이다
_CONFIRM_LOCK = threading.Lock()         # /health 는 스레드 풀에서, 카드 · 셈은 이벤트 루프에서 돈다(_HUB_FOLD_LOCK 과 같다)
_CONFIRM_PENDING: "OrderedDict[str, dict]" = OrderedDict()
_CONFIRM_STAT: Counter = Counter()
_CONFIRM_COUNT_KEYS = ("cards", "held_calls", "approved", "executed", "declined", "unknown_reply", "ordered")
# 이번 챗 턴의 확인 맥락 — None 이면 챗 밖(심의 좌석 · 띵킹 · 루틴 · 카탈로그 · 시험)이라 관문이 서지 않는다(_turn_ids 와 같은 꼴).
_turn_confirm: contextvars.ContextVar = contextvars.ContextVar("turn_confirm", default=None)


def _confirm_count(key: str, n: int = 1) -> None:
    with _CONFIRM_LOCK:
        _CONFIRM_STAT[key] += n


def _confirm_lists() -> dict:
    """운영자 정의의 writes · confirm 두 목록 — he-team.json 의 바뀐 시각이 다르면 다시 읽는다(재기동 없이 HP-53 이 채운 목록이 들어온다).
    선택 앱(optional[])의 목록도 넣고, 접두 별칭(<앱 키에서 하이픈을 뺀 것>_<이름> — sync-he-personas.py 가 받는 꼴)은 접두를 뗀 꼴도 넣는다(모델이
    맨 이름으로 불러도 맞게). 없으면 두 목록이 비고(바닥 목록만 돈다) 까닭은 error 다 — 로그는 처음 한 번. 꼴이 틀린 항목(글이 아닌 이름 ·
    app 이 빠진 선택 앱 …)은 그것만 건너뛰고 error 에 수를 적으며, 파일을 통째로 못 읽으면(JSON 이 깨짐 · personas 가 없음) 마지막으로 읽은 목록을
    쥔 채 error 만 적는다 — 종전엔 항목 하나의 실수가 앞서 읽어 둔 좋은 목록까지 버려 박스 전체의 쓰기 · 확인 부류가 카드 없이 나갔다(반대 검토 2차).
    error 가 있으면 /health lists.loaded 가 거짓이다(update-all §7 의 ⚠)."""
    try:
        st = CONFIRM_LISTS_PATH.stat()
    except OSError:
        if _CONFIRM_LISTS["error"] != "없음":
            print(f"[confirm] ⚠ 운영자 정의가 없다({CONFIRM_LISTS_PATH}) — 확인 관문은 잡 시작 바닥 목록만으로 돈다", flush=True)
        _CONFIRM_LISTS.update(key=None, writes=frozenset(), confirm=frozenset(), error="없음")
        return _CONFIRM_LISTS
    key = (str(CONFIRM_LISTS_PATH), st.st_mtime_ns, st.st_size)
    if key != _CONFIRM_LISTS["key"]:
        try:
            personas = json.loads(CONFIRM_LISTS_PATH.read_text(encoding="utf-8"))["personas"]
            if not isinstance(personas, list):
                raise TypeError("personas 가 목록이 아니다")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            print(f"[confirm] ⚠ 운영자 정의를 못 읽었다({CONFIRM_LISTS_PATH}) — 마지막으로 읽은 목록"
                  f"(쓰기 {len(_CONFIRM_LISTS['writes'])} · 확인 {len(_CONFIRM_LISTS['confirm'])})으로 돈다: {exc!r}"[:400], flush=True)
            _CONFIRM_LISTS.update(key=key, error=f"못 읽음({type(exc).__name__})")
            return _CONFIRM_LISTS
        got: dict = {"writes": set(), "confirm": set()}
        bad = 0

        def lst(v) -> list:
            nonlocal bad
            if v is None:
                return []
            if not isinstance(v, list):
                bad += 1
                return []
            return v
        for p in personas:
            if not isinstance(p, dict):
                bad += 1
                continue
            opts = []
            for o in lst(p.get("optional")):
                if not isinstance(o, dict):
                    bad += 1
                    continue
                if not isinstance(o.get("app"), str) or not o["app"]:
                    bad += 1            # app 이 빠진 선택 앱 — 그 목록은 넣되 접두를 뗀 꼴은 짓지 못한다
                opts.append(o)
            pre = [a.replace("-", "").lower() + "_" for a in [*lst(p.get("apps")), *(o.get("app") for o in opts)] if isinstance(a, str) and a]
            for fld in ("writes", "confirm"):
                for n in [*lst(p.get(fld)), *(x for o in opts for x in lst(o.get(fld)))]:
                    if not isinstance(n, str) or not n.strip():
                        bad += 1
                        continue
                    n = n.strip().lower()
                    got[fld].add(n)
                    got[fld].update(n[len(x):] for x in pre if n.startswith(x) and len(n) > len(x))
        if bad:
            print(f"[confirm] ⚠ 운영자 정의({CONFIRM_LISTS_PATH})에서 꼴이 틀린 항목 {bad}개를 건너뛰었다 — 나머지 목록으로 돈다", flush=True)
        _CONFIRM_LISTS.update(key=key, writes=frozenset(got["writes"]), confirm=frozenset(got["confirm"]),
                              error=f"항목 {bad}개를 건너뜀(꼴이 틀렸다)" if bad else None)
    return _CONFIRM_LISTS


def _confirm_class(name) -> tuple:
    """(부류, 맞은 목록 이름) — 부류는 confirm > job > write(겹치면 더 엄한 쪽), 목록 밖이면 (None, ""). 맞은 이름은 그 부류에서 가장 긴 것이고
    승인 · 중복 판정 열쇠의 앞 칸이다(노출 이름 · 별칭 · 경유로 불러도 같은 호출이면 같은 열쇠)."""
    n = str(name or "").strip().lower()
    if not n:
        return None, ""
    lists = _confirm_lists()
    for kind, names in (("confirm", lists["confirm"]), ("job", _CONFIRM_JOB_TOOLS), ("write", lists["writes"])):
        hit = [t for t in names if n == t or n.endswith("_" + t)]
        if hit:
            return kind, max(hit, key=len)
    return None, ""


def _confirm_canon(args) -> str:
    """승인 · 중복 판정 열쇠의 인자 칸 — dict(JSON 글이면 풀어서)에서 값이 None · "" · [] · {} 인 칸을 재귀로 빼고 키 차례 JSON 으로 적는다.
    못 읽는 글은 그대로 쓴다(같은 글일 때만 같다)."""
    v = args
    if isinstance(v, str):
        try:
            v = json.loads(v) if v.strip() else {}
        except ValueError:
            return v

    def prune(x):
        if isinstance(x, dict):
            out = {}
            for k, y in x.items():
                y = prune(y)
                if y is not None and y != "" and y != [] and y != {}:
                    out[str(k)] = y
            return out
        return [prune(y) for y in x] if isinstance(x, (list, tuple)) else x
    return json.dumps(prune({} if v is None else v), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _confirm_targets(args) -> list:
    """'그 일을 글자로 불렀나' 에 견줄 인자 낱말 — 무엇 칸의 글 값(3 ~ 64자)과 파일 이름('/' 가 있고 확장자로 끝나는 글 값의 끝 조각). 맨 위 칸과
    한 단계 아래 dict(params.tool 꼴 — JSON 글이면 풀어서)까지 본다."""
    top = _arg_dict(args) or {}
    out = []
    for d in (top, *(x for x in (_arg_dict(v) for v in top.values()) if x)):
        for k, v in d.items():
            if not isinstance(v, str):
                continue
            s = v.strip()
            if str(k).strip().lower() in _CONFIRM_WHAT_KEYS and 3 <= len(s) <= 64:
                out.append(s)
            if "/" in s and _CONFIRM_FILE_RE.search(s):
                out.append(s.rstrip("/").rsplit("/", 1)[-1])
    return out


def _confirm_job_named(low: str, word) -> bool:
    """잡을 그 낱말로 시켰나 — 그 낱말 바로 뒤(조사 · '잡' · '한 번' 같은 짧은 말만 사이에)에 잡을 거는 동사가 오거나(_JOB_AFTER), 문장 머리의 영어
    명령(run · submit …) 뒤에 그 낱말이 있다(_JOB_EN). 낱말은 영숫자 · 밑줄 경계로 찾는다."""
    w = str(word or "").strip().lower()
    if not w:
        return False
    pat = re.compile(rf"(?<![a-z0-9_]){re.escape(w)}(?![a-z0-9_])")
    if any(_JOB_AFTER.match(low, m.end()) for m in pat.finditer(low)):
        return True
    return any(pat.search(m.group(1)) for m in _JOB_EN.finditer(low))


def _confirm_pin_is(pin: str, name) -> bool:
    """콕 집은 도구가 바로 이 도구인가 — 그대로이거나 한쪽이 다른 쪽의 접두 별칭(<앱키>_<이름>)이다. 맞은 목록 이름(run_job)으로 견주지 않는다 —
    한 앱의 run_job 핀이 다른 앱의 같은 꼬리 잡(appb_run_job)까지 열었다(반대 검토 2차)."""
    n = str(name or "").strip().lower()
    return bool(n) and (n == pin or n.endswith("_" + pin) or pin.endswith("_" + n))


def _confirm_ordered(ctx, kind: str, name, base: str, args) -> bool:
    """이번 사람 말(원문)이 이 호출을 분명히 시켰나. write 는 질문 · 부정 없는 주문 동사(_ORDER_RE)면 된다. job 은 엄하다 — 질문 · 부정 · 미루는 말이
    없고, 그 일의 낱말(도구 이름 노출 · 맨 이름 · 무엇 칸 값 · 파일 이름) 바로 뒤에 잡을 거는 동사가 와야 한다(_confirm_job_named). 사람이 그
    도구를 콕 집었으면(pinned_tools — 앱 핀은 치지 않는다, 앱 하나에 잡이 여럿이다) 잡을 거는 동사만 말에 있으면 된다 — 포털은 핀을 대화에 둬
    다음 말에도 싣는다(반대 검토 2차 — '보고서 만들어줘' · '취소해줘' 가 핀 하나로 run_job 을 열었다). confirm 은 늘 거짓이다."""
    u = ctx.utterance
    if kind not in ("job", "write") or not u or _ASK_RE.search(u) or _NEG_RE.search(u):
        return False
    if kind == "write":
        return bool(_ORDER_RE.search(u))
    if _LATER_RE.search(u):
        return False
    low = u.lower()
    if any(_confirm_pin_is(p, name) for p in ctx.pinned):
        return bool(_JOB_VERB_RE.search(low) or _JOB_EN.search(low))
    return any(_confirm_job_named(low, w) for w in (name, base, *_confirm_targets(args)))


class _ConfirmCtx:
    """한 챗 턴의 확인 맥락(_turn_confirm) — 값은 변하는 객체 하나라 도구 작업(복사된 문맥)과 턴이 같은 객체를 본다(_turn_ids 와 같은 길).
    utterance — 주문 판정 글(이번 사람 말 원문, 카드의 답이면 빈 글 — 확인 답은 새 주문이 아니다) · pinned — 사람이 콕 집은 도구 ·
    approved — 이번에 승인된 호출 {(부른 이름, 맞은 이름, 정규 인자): 호출} · used — 이 턴에 이미 나간 호출의 열쇠(맞은 이름 · 승인 열쇠) · pending — 막은 호출(턴 끝 카드에 실린다) ·
    note — 이 턴의 안내에 붙일 글 · status — 턴 머리의 상태줄 · card — 본문 끝에 붙인 카드 글 · card_id — 승인된 카드 번호 · sent — 서버가 보낸 수 ·
    held — 이 턴에 G5 가 막은 호출 수 · rejected — 자격이 거절된(401) 호출의 열쇠 · no_pat — 카드 턴엔 있던 사용자 자격이 승인 턴에 없다 · done —
    이 턴에 보낸 잡 · 쓰기 [(이름, 결과 머리)](카드 글이 '이 턴에 보낸 호출' 로 밝힌다)."""
    __slots__ = ("utterance", "pinned", "approved", "used", "pending", "note", "status", "card", "card_id", "sent", "held", "rejected",
                 "no_pat", "done")

    def __init__(self, utterance: str = "", pinned=()):
        self.utterance = utterance
        self.pinned = frozenset(str(p).strip().lower() for p in pinned if str(p).strip())
        self.approved, self.used, self.pending = {}, set(), []
        self.note = self.status = self.card = self.card_id = ""
        self.sent = 0
        self.held = 0           # 상태 기다림의 멈춤 술어(C-4)가 본다(pending 은 다시 카드로 돌린 호출도 든다)
        self.rejected: set = set()
        self.no_pat = False
        self.done: list = []


def _affirmed(msg: str) -> bool:
    """짧은 승인 말인가 — 말 전체가 _AFFIRM_RE 꼴이고(30자 이하) 승인 낱말이 하나는 있다('어' 만으로는 아니다 — 망설임이다)."""
    m = _AFFIRM_RE.match(msg) if msg and len(msg) <= 30 else None
    return bool(m) and bool(m.group("verb") or (m.group("yes") and m.group("yes").lower() != "어"))


def _confirm_reply(req) -> tuple | None:
    """이번 말이 이력 끝 카드의 답인가 → ('approve' | 'decline', 카드 번호), 아니면 None. 확인은 바로 다음 말로만 한다 — 이력의 마지막 항목이 카드를
    실은 어시스턴트 글일 때만 본다. 번호가 든 말(단추 글)은 그 카드의 번호여야 하고(지난 카드의 번호는 무시), 번호를 뺀 나머지가 부정이거나 '취소' 로
    시작하면 거절, 짧은 승인 꼴(_affirmed)이면 승인, 그 밖(물음 · 다른 지시)은 답이 아니다. 번호가 없으면 말 전체가 짧은 승인 · 거절 꼴일 때만이다.
    저장분은 보지 않는다(순수) — chat() 과 _denied_feature 가 같은 판정으로 가른다
    (design-integration C-10)."""
    if not CHAT_CONFIRM_GATE:
        return None
    hist = req.history or []
    last = hist[-1] if hist else None
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return None
    ids = _CF_ID_RE.findall(str(last.get("content") or ""))
    if not ids:
        return None
    msg = (req.message or "").strip()
    if _CF_ID_RE.search(msg):
        said = [i for i in _CF_ID_RE.findall(msg) if i in ids]
        if not said:
            return None
        # 번호를 뺀 나머지가 승인 · 거절의 말이어야 한다 — 단추 글('실행 — 확인 번호 cf-…')이면 '실행' 이 남는다. 번호만 있으면 무엇이든 승인이던
        # 때는 'cf-… 이게 무슨 잡이야?' · 'cf-… 대신 graph 만 돌려줘' 가 저장한 잡을 보냈다(반대 검토 2차).
        rest = re.sub(r"^[\s—–\-:·]+|[\s—–\-:·]+$", "", " ".join(_CF_ID_RE.sub(" ", msg).replace("확인 번호", " ").split()))
        if _NEG_RE.search(rest) or rest.startswith("취소") or _DECLINE_RE.match(rest):
            return "decline", said[0]
        return ("approve", said[0]) if _affirmed(rest) else None
    if _DECLINE_RE.match(msg):
        return "decline", ids[-1]
    return ("approve", ids[-1]) if _affirmed(msg) and not _NEG_RE.search(msg) else None


def _confirm_take(cid: str, req) -> dict | None:
    """저장분을 꺼낸다(한 번만) — 번호 · 사용자 · 대화가 맞고 기한 안일 때. 다른 사람 · 다른 대화의 답은 꺼내지 않는다(그 카드는 주인에게 남는다)."""
    user, conv = (req.user_email or "").strip().lower(), (req.conversation_id or "").strip()
    with _CONFIRM_LOCK:
        card = _CONFIRM_PENDING.get(cid)
        if card is None or card["user"] != user or card["conv"] != conv:
            return None
        del _CONFIRM_PENDING[cid]
    return card if card["exp"] > time.time() else None


def _confirm_drop(req) -> None:
    """이 사용자 · 대화의 카드를 지운다 — 카드는 바로 다음 말로만 답한다(카드 글 '다른 말을 하면 보내지 않습니다'). 이력 끝만 보는 판정으로는 모자라다
    — 모델이 다음 답에 아직 저장된 옛 번호를 다시 적으면 그다음 사람의 '응' 이 그 카드를 승인한다."""
    user, conv = (req.user_email or "").strip().lower(), (req.conversation_id or "").strip()
    with _CONFIRM_LOCK:
        gone = [k for k, v in _CONFIRM_PENDING.items() if (v["user"], v["conv"]) == (user, conv)]
        for k in gone:
            del _CONFIRM_PENDING[k]
    if gone:
        print(f"[confirm] 카드 {', '.join(gone)} 지움 — 다음 말이 그 카드의 답이 아니다", flush=True)


def _confirm_ctx(req) -> _ConfirmCtx:
    """이 턴의 확인 맥락. 앞 카드의 답이면 저장분을 꺼내(나란한 두 요청이 같은 카드를 승인해도 한쪽만 — 꺼내기에 await 가 없다) 승인 · 거절을 정한다.
    승인이면 그 카드의 호출이 approved 에 든다(메시지를 짓기 전에 서버가 보낸다 — _confirm_send). 거절이면 이 턴의 안내에 그 사실을 싣는다. 답이
    아니면 이 대화의 카드를 지운다(_confirm_drop)."""
    ctx = _ConfirmCtx(req.message or "", req.pinned_tools or [])
    reply = _confirm_reply(req)
    if reply is None:
        _confirm_drop(req)
        return ctx
    verdict, cid = reply
    ctx.utterance = ""
    card = _confirm_take(cid, req)
    if card is None:
        _confirm_count("unknown_reply")
        ctx.status = (f"확인 번호를 찾지 못했다({_CONFIRM_TTL_S // 60}분이 지났거나 서버가 다시 떴다) — 필요하면 다시 확인 카드가 뜬다")
        print(f"[confirm] 답한 카드 {cid} 를 찾지 못했다(기한 · 재기동 · 다른 사람 · 다른 대화)", flush=True)
        return ctx
    if verdict == "decline":
        _confirm_count("declined")
        ctx.status = f"확인 카드 {cid} 거절 — 그 호출은 보내지 않습니다"
        ctx.note = f"[확인 카드 {cid} 거절 — 사용자가 그 호출을 원하지 않는다. 같은 호출을 다시 하지 마라]"
        print(f"[confirm] 거절 {cid}", flush=True)
        return ctx
    _confirm_count("approved")
    ctx.card_id = cid
    ctx.approved = {(c["name"].strip().lower(), c["base"], c["canon"]): c for c in card["calls"]}
    # 카드 턴은 사람 자격(PAT)으로 돌았는데 승인 턴에 그 자격이 없으면(포털 발급 실패 — 빈 글) 보내지 않는다 — 그대로 보내면 승인된 잡 · 쓰기가
    # 서비스 계정 연결로 말없이 나간다(C-11 · 반대 검토 2차, 포털 루틴 길의 routine_no_pat 과 같다). 카드 턴도 자격 없이 돌았으면(서비스 계정 박스) 같다.
    ctx.no_pat = bool(card.get("pat")) and not (req.user_pat or "").strip()
    ctx.status = f"확인 카드 {cid} 승인 — 승인된 호출 {len(card['calls'])}건을 보냅니다"
    return ctx


def _confirm_schema_limits(tool) -> dict:
    """도구 스키마의 time_limit(Slurm 시간 상한) · wait_sec(결과까지 기다리는 상한) 기본값 — 카드의 예상 작업량에 싣는다."""
    sc = getattr(tool, "args_schema", None)
    props = sc.get("properties") if isinstance(sc, dict) else None
    if not isinstance(props, dict):
        return {}
    return {k: props[k]["default"] for k in ("time_limit", "wait_sec")
            if isinstance(props.get(k), dict) and props[k].get("default") not in (None, "")}


def _confirm_refusal(name, kind: str, again: bool) -> str:
    why = ("되돌리기 어려운 작업이라 사람의 확인 전에는 보내지 않는다" if kind == "confirm" else
           "이 턴에 같은 호출을 이미 보냈다 — 다시 보내면 하나 더 선다" if again else
           "사용자가 이번 말로 시킨 작업이 아니라 보내지 않았다")
    return (f"{_TOOL_FAIL_MARK} 도구 {str(name)[:80]} 호출 보류: {_CONFIRM_MARK} — {why}. 서버가 사용자에게 확인 카드를 띄운다.\n"
            "이번 턴에는 이 도구를 다시 부르지 말고(인자를 바꿔도 같다) 무엇을 왜 하려 했는지 한 줄로 말하고 끝내라. 인자 문제가 아니다.")


def _confirm_held_text(txt) -> bool:
    """도구 결과가 확인 관문이 막은 글인가 — 머리만 본다(_result_ok 와 같은 까닭 — 뒤쪽의 같은 글귀는 남의 것이다)."""
    head = txt.lstrip()[:200] if isinstance(txt, str) else ""
    return head.startswith(_TOOL_FAIL_MARK) and f"호출 보류: {_CONFIRM_MARK}" in head


def _confirm_parse(tool, a, kw):
    """G5 가 보는 호출 → (부른 이름, 안쪽 인자, 경유인가, 부류, 맞은 목록 이름, 열쇠(맞은 이름, 정규 인자)) — 턴 맥락이 없거나 관문이 꺼졌거나
    목록 밖이면 None."""
    if _turn_confirm.get() is None or not CHAT_CONFIRM_GATE:
        return None
    via = getattr(tool, "name", "") == "invoke_tool"
    args = kw if kw else next((x for x in a if isinstance(x, dict)), kw)
    name, targs = (args.get("name"), args.get("arguments")) if via else (getattr(tool, "name", ""), args)
    kind, base = _confirm_class(name)
    return None if kind is None else (name, targs, via, kind, base, (base, _confirm_canon(targs)))


def _confirm_auth_rejected(parsed) -> None:
    """사람 자격이 거절된(401 — 보내지 않았다) 확인 부류 호출의 열쇠를 적는다(capped 의 호출 실패 갈래 — parsed 는 G6 이 기본값을 더하기 전의
    _confirm_parse). 같은 턴에 그 호출이 다시 오면(_svc_fallback 의 서비스 계정 재시도 · 모델의 되풀이) G5 가 '이미 보냈다' 카드가 아니라 자격 거절
    글로 막는다 — 보내지 않은 잡을 '이 턴에 이미 보냈다' 로 말하고 승인하면 다시 401 이던 꼴(반대 검토 2차). 사람이 시킨 잡 · 쓰기는 서비스 계정으로
    보내지 않는다(C-11 의 뜻)."""
    ctx = _turn_confirm.get()
    if parsed is not None and ctx is not None:
        ctx.rejected.add(parsed[5])


def _confirm_gate(tool, a, kw) -> str | None:
    """G5 — 이번 사람 말이 시키지 않은 잡 · 쓰기와 되돌리기 어려운 호출은 보내지 않고 모델에게 돌려줄 글을 낸다(통과면 None). 판정은 노출 이름이고
    invoke_tool 이면 안쪽 이름 · 안쪽 인자다. 승인된 호출(서버가 보낸다)과 주문으로 통과한 호출은 이 턴에 한 번만 간다 — 같은 열쇠가 다시 오면 카드다
    (예외 뒤 자동 재시도가 그래프를 다시 돌려도 이미 보낸 잡을 또 걸지 않는다). 판정 · 표시는 await 없이 한 번에 한다(나란한 호출도 한 이벤트 루프라
    경합이 없다). 열쇠는 모델이 준 인자(챗 인자 기본값 앞)로 잰다."""
    got = _confirm_parse(tool, a, kw)
    if got is None:
        return None
    ctx = _turn_confirm.get()
    name, targs, via, kind, base, key = got
    if key in ctx.rejected:
        return (f"{_TOOL_FAIL_MARK} 도구 {str(name)[:80]} 호출 실패: {_AUTH_REJECT_MARK} — 사용자 토큰이 만료됐거나 폐기됐다. 사람이 시킨 잡 · 쓰기는 "
                "서비스 계정으로 보내지 않는다. 인자 문제가 아니다 — 같은 도구를 다시 부르지 말고 사용자에게 다시 로그인해 달라고 알려라")
    # 승인 열쇠는 부른 이름까지 — 맞은 목록 이름만 보면 다른 앱의 같은 꼬리 도구(appA_run_job · appB_run_job)가 한 열쇠라, 두 호출을 승인해도
    # 서버가 하나만 보낸다. 되풀이(again)는 맞은 이름 열쇠로 본다(경유 · 별칭으로 다시 불러도 같은 잡이다).
    akey = (str(name).strip().lower(), *key)
    if akey in ctx.approved and akey not in ctx.used:
        ctx.used.update((akey, key))
        return None
    if key not in ctx.used and _confirm_ordered(ctx, kind, name, base, targs):
        ctx.used.add(key)
        _confirm_count("ordered")
        print(f"[confirm] 주문 통과 {str(name)[:80]}", flush=True)
        return None
    again = key in ctx.used
    if not any((c["name"].strip().lower(), c["base"], c["canon"]) == akey for c in ctx.pending):     # 똑같은 호출(나란히 두 번)만 한 줄로
        try:
            kept = copy.deepcopy(targs)
        except Exception:  # noqa: BLE001 — 복사 못 하는 값은 그대로 쥔다
            kept = targs
        ctx.pending.append({"name": str(name), "via": via, "base": base, "kind": kind, "args": kept, "canon": key[1],
                            "again": again, "limits": {} if via else _confirm_schema_limits(tool)})
    ctx.held += 1
    return _confirm_refusal(name, kind, again)


def _confirm_store(req, calls: list) -> tuple:
    """막은 호출을 카드로 둔다 → (번호, 기한) — 번호는 추측할 수 없다(secrets). 같은 사용자 · 대화의 옛 카드와 기한이 지난 것은 지우고, 상한을 넘으면
    오래된 것부터 버린다. 프로세스 하나(uvicorn 단일 — 루틴 레지스트리와 같은 전제)라 메모리로 충분하다 — 재기동하면 사라지고 그때 답은 '찾지
    못했다' 다(닫힌 쪽)."""
    user, conv = (req.user_email or "").strip().lower(), (req.conversation_id or "").strip()
    now = time.time()
    cid = "cf-" + secrets.token_hex(6)
    with _CONFIRM_LOCK:
        for k in [k for k, v in _CONFIRM_PENDING.items() if (v["user"], v["conv"]) == (user, conv) or v["exp"] <= now]:
            del _CONFIRM_PENDING[k]
        _CONFIRM_PENDING[cid] = {"user": user, "conv": conv, "exp": now + _CONFIRM_TTL_S, "at": now, "calls": calls,
                                 "pat": bool((req.user_pat or "").strip())}       # 카드 턴의 자격 — 승인 턴에 없으면 보내지 않는다
        while len(_CONFIRM_PENDING) > _CONFIRM_MAX:
            _CONFIRM_PENDING.popitem(last=False)
    return cid, now + _CONFIRM_TTL_S


_CARD_ARGS_MAX = 600            # 카드 글에 펼치는 인자 합(자) — 넘으면 '… 외 N칸'(인자 전체는 카드 사건의 args)
_CARD_VALUE_MAX = 80            # 값 하나(자) — 넘으면 앞부분과 '…(N자)'
_CARD_LIST_SHOW = 5             # 수 · 글 목록에서 보이는 원소 수
_CARD_SSE_ARGS_MAX = 20_000     # 카드 사건 args(정규 JSON) 상한 — 넘으면 앞부분과 args_cut


def _confirm_args_text(args) -> str:
    """카드에 보이는 인자 — 정규 인자를 잎까지 펼친 k.sub.leaf=v(사람이 승인하면 서버는 저장분 전부를 보낸다 — 카드가 줄여 보이면 판 전체와 부품
    하나짜리 잡이 '{1칸}' · '{2칸}' 으로만 갈렸다, 반대 검토 2차). 수 · 글 목록은 k=[a, b, … N개], dict 가 든 목록은 k[0].x=v 로 펼친다. 값은
    _CARD_VALUE_MAX 자에서 자르고 '…(N자)' 를 달며 줄바꿈은 ⏎ 로 적는다. 합이 _CARD_ARGS_MAX 자를 넘으면 '… 외 N칸' 으로 끝낸다(인자 전체는
    카드 사건의 args — 정규 JSON)."""
    canon = _confirm_canon(args)
    try:
        d = json.loads(canon)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        return _confirm_val(canon)

    leaves: list = []

    def walk(k: str, v) -> None:
        if isinstance(v, dict):
            for sk, sv in v.items():
                walk(f"{k}.{sk}" if k else str(sk), sv)
        elif isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v):
            for i, x in enumerate(v):
                walk(f"{k}[{i}]", x)
        elif isinstance(v, list):
            shown = ", ".join(_confirm_val(x) for x in v[:_CARD_LIST_SHOW])
            leaves.append(f"{k}=[{shown}{f', … {len(v)}개' if len(v) > _CARD_LIST_SHOW else ''}]")
        else:
            leaves.append(f"{k}={_confirm_val(v)}")
    walk("", d)
    out, used = [], 0
    for i, leaf in enumerate(leaves):
        if out and used + len(leaf) + 3 > _CARD_ARGS_MAX:
            out.append(f"… 외 {len(leaves) - i}칸")
            break
        out.append(leaf)
        used += len(leaf) + 3
    return " · ".join(out)


def _confirm_val(v) -> str:
    """카드에 적는 값 하나 — 불은 true · false, 줄바꿈은 ⏎, _CARD_VALUE_MAX 자를 넘으면 앞부분과 '…(N자)'."""
    if isinstance(v, bool):
        return "true" if v else "false"
    t = str(v).replace("\r\n", "\n").replace("\n", "⏎")
    return t if len(t) <= _CARD_VALUE_MAX else t[:_CARD_VALUE_MAX] + f"…({len(t):,}자)"


def _confirm_limit_hint(args, limits) -> str:
    """인자 · 스키마 기본값의 time_limit · wait_sec 한 줄(없으면 빈 글)."""
    d = _arg_dict(args) or {}
    for k, fmt in (("time_limit", "Slurm 시간 상한 {}"), ("wait_sec", "결과까지 최대 {}초 기다린다")):
        v, tag = d.get(k), ""
        if v in (None, ""):
            v, tag = (limits or {}).get(k), "(기본값)"
        if v not in (None, ""):
            return fmt.format(v) + tag
    return ""


def _confirm_sf_job(c: dict) -> bool:
    """StepForge 의 run_job 인가 — 접두 별칭이거나 /tools-map 이 그 이름을 StepForge 앱으로 적었다."""
    n = str(c["name"]).strip().lower()
    return c["base"] == "run_job" and (n.startswith("heaxstep_forge_")
                                       or (_TOOLS_MAP_CACHE.get("map") or {}).get(c["name"]) == "heax-step_forge")


def _confirm_estimate_text(d) -> str:
    """StepForge estimate_cost 의 답 → 카드 한 줄. `work_outlook`(StepForge ST-12 `b2c70f1`)이 있으면 그것을 싣는다 — `model_s` 는 '모형 값' 이지
    예상 시간이 아니고(하한도 상한도 아니다 — StepForge 실측 실제/모형 0.1 ~ 1.2), 가장 나은 근거는 같은 인자로 끝난 잡의 `seen_s` 다(StepForge 답
    10-30). 옛 판(work_outlook 없음)은 expected_s 를 근거(cost_basis)와 '하한일 수 있다' 와 함께 적는다(60초 예상이 1,812초였다).
    잡의 판정(gateway_verdict)은 async_job 이라 넘침 판정(over · may_exceed)만 싣는다."""
    if not isinstance(d, dict) or not d:
        return "예상 작업량을 읽지 못했다"

    def num(x) -> str:
        return f"{x:,.0f}" if abs(x) >= 10 else f"{x:g}"

    def isnum(x) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool)
    exp, lo, hi = d.get("expected_s"), d.get("low_s"), d.get("high_s")
    wo = d.get("work_outlook")
    if isinstance(wo, dict) and wo:
        parts = []
        if isnum(wo.get("shapes")):
            parts.append(f"합칠 형상 {num(wo['shapes'])}개")
        if isnum(wo.get("model_s")):
            parts.append(f"모형 값 {num(wo['model_s'])}초(예상 시간이 아니다 — 하한도 상한도 아니다)")
        elif isinstance(wo.get("model_why"), str) and wo["model_why"].strip():
            parts.append("모형 값 없음 — " + _cut_mark(wo["model_why"].strip(), 120))
        seen = wo.get("seen_s")
        if isinstance(seen, dict) and isnum(seen.get("median_s")):
            top = seen.get("max_s") if isnum(seen.get("max_s")) else seen["median_s"]
            parts.append(f"같은 인자로 끝난 잡 {num(seen['n']) if isnum(seen.get('n')) else '?'}번 — 가운데 {num(seen['median_s'])}초 · "
                         f"가장 길게 {num(top)}초")
        if wo.get("holds_runner"):
            parts.append("러너를 오래 잡을 수 있다" + (f"(동시 {num(wo['runner_slots'])}칸)" if isnum(wo.get("runner_slots")) else ""))
        s = " · ".join(parts) or "작업 전망을 읽지 못했다"
    elif isnum(exp):
        rng = f"{num(lo)} ~ {num(hi)}초 · " if isnum(lo) and isnum(hi) else ""
        s = f"약 {num(exp)}초({rng}근거 {d.get('cost_basis') or '모름'}) — 추정이며 하한일 수 있다"
    else:
        s = "예상 시간 없음 — " + _cut_mark(str(d.get("basis_note") or "근거가 없다").strip(), 160)
    if d.get("gateway_verdict") in ("over", "may_exceed"):
        s += f" · 판정 {d['gateway_verdict']}"
    if isinstance(d.get("advice"), str) and d["advice"].strip():
        s += " · " + _cut_mark(d["advice"].strip(), 160)
    return s


async def _confirm_sf_estimate(tmap: dict, c: dict) -> str:
    """StepForge run_job 의 시작 전 작업량 — 읽기 도구 estimate_cost(project_id, job, args)를 접두 별칭으로 invoke_tool 로 묻는다(다른 앱에 같은
    이름이 붙은 박스에서도 StepForge 것에 닿는다 — 게이트웨이는 별칭을 늘 받는다)."""
    a = _arg_dict(c["args"]) or {}
    pid, kind = a.get("project_id"), a.get("kind")
    if not pid or not kind:
        return "예상 작업량을 묻지 못했다(과제 · 잡 종류가 인자에 없다)"
    out = await _call(tmap, "invoke_tool", {"name": "heaxstep_forge_estimate_cost",
                                             "arguments": {"project_id": pid, "job": kind, "args": _arg_dict(a.get("params")) or {}}})
    txt = "" if out is None else str(out)
    if not txt or not _result_ok(txt):
        return "예상 작업량을 받지 못했다" + (f" — {_tool_preview(txt, 120)}" if txt else "(invoke_tool 이 없다)")
    try:
        return _confirm_estimate_text(_first_dict(_parse_json(txt)))
    except Exception:  # noqa: BLE001 — 읽지 못한 답이 카드를 막지 않는다
        return "예상 작업량을 읽지 못했다"


def _confirm_workload_local(calls: list) -> list:
    """카드에 싣는 예상 작업량(잡 부류) — 잡을 거는 호출의 응답은 시작 **뒤**에 오므로, 시작 전에 알 수 있는 둘만 싣는다. (가) StepForge run_job 은
    estimate_cost 를 묻고(_confirm_workload), (나) 인자 · 스키마 기본값의 time_limit · wait_sec 은 여기서 싣는다. 나머지는 '미리 알려 주지 않는다'
    다. 묻지 않고 알 수 있는 것은 채우고, 도구 목록이 필요한 호출(StepForge 견적 · invoke_tool 의 안쪽 도구 스키마)을 돌려준다."""
    need = []
    for c in calls:
        hint = "" if _confirm_sf_job(c) else _confirm_limit_hint(c["args"], c.get("limits"))
        if hint:
            c["workload"] = hint
        elif _confirm_sf_job(c) or c["via"]:
            need.append(c)
        else:
            c["workload"] = "이 도구는 작업량을 미리 알려 주지 않는다"
    return need


async def _confirm_workload(app_, req, calls: list) -> None:
    """도구 목록이 필요한 예상 작업량을 묻는다 — StepForge estimate_cost(StepForge 답 10-30 — 칸 expected_s · cost_basis · gateway_verdict · basis_note
    · advice)와 invoke_tool 로 부른 도구의 스키마 기본값. 목록 받기 + 묻기 전체를 _CONFIRM_WORKLOAD_S 초로 묶는다 — 넘거나 실패하면 그렇게 적고
    카드는 그대로 선다. 읽기라 자격은 기본값이다(design-integration C-11)."""
    async def probe():
        tmap = await _tools_by_name(app_, req.groups, user=req.user_email, user_pat=req.user_pat)
        for c in calls:
            c["workload"] = (await _confirm_sf_estimate(tmap, c) if _confirm_sf_job(c)
                             else _confirm_limit_hint(c["args"], _confirm_schema_limits(tmap.get(c["name"])))
                             or "이 도구는 작업량을 미리 알려 주지 않는다")
    miss = ""
    try:
        await asyncio.wait_for(probe(), _CONFIRM_WORKLOAD_S)
    except asyncio.TimeoutError:
        miss = f"예상 작업량을 {_CONFIRM_WORKLOAD_S:g}초 안에 받지 못했다"
    except Exception as exc:  # noqa: BLE001 — 묻기가 실패해도 카드는 선다
        miss = f"예상 작업량을 받지 못했다({type(exc).__name__})"
    if miss:
        print(f"[confirm] {miss}", flush=True)
        for c in calls:
            if not c.get("workload"):
                c["workload"] = miss


def _confirm_card_text(cid: str, calls: list, sent=()) -> str:
    """본문 끝의 결정적 카드 글 — 다른 기기 · 서버 사본 · 단추 없는 화면에서도 글로 확인할 수 있고, 그 안의 번호가 다음 턴 이력으로 돌아와 승인을
    잇는다. 끝이 확인 번호라 끊긴 턴 표지(_INTERRUPTED_RE)에 걸리지 않는다. sent 는 이 턴에 보낸 잡 · 쓰기 [(이름, 결과 머리)] — 한 단계에 시킨
    잡(통과)과 시키지 않은 잡(카드)이 함께 오면 흐름을 끊어 시작된 잡을 답이 말하지 않았다. 사람이 '안 돌았다' 로 읽고 다시 시키면 같은 잡이 하나
    더 선다(반대 검토 2차)."""
    kinds = {c["kind"] for c in calls}
    head = ("되돌리기 어려운 작업이라 보내기 전에 확인합니다." if kinds == {"confirm"} else
            "이번 말로 시키지 않은 작업이라 보내지 않았습니다." if "confirm" not in kinds else
            "이번 말로 시키지 않았거나 되돌리기 어려운 작업이라 보내지 않았습니다.")
    lines = ["\n\n---", f"**확인이 필요합니다** — {head}"]
    for i, c in enumerate(calls, start=1):
        s = f"{i}. " + (f"{c['app']} " if c.get("app") else "") + f"`{c['name']}`" + (f" · {c['args_text']}" if c.get("args_text") else "")
        if c.get("workload"):
            s += f" — 예상 작업량: {c['workload']}"
        if c.get("again"):
            s += " (이 턴에 같은 호출을 이미 보냈다 — 다시 보내면 하나 더 선다)"
        lines.append(s)
    for n, (name, head) in enumerate(sent):
        if n == 0:
            lines.append("이 턴에 보낸 호출(위 카드와 따로 이미 나갔다):")
        lines.append(f"- `{name}` → {head}" if head else f"- `{name}`")
    lines.append(f"실행하려면 **실행** 을 누르거나 '실행' 이라고 답하세요. 다른 말을 하면 보내지 않습니다. "
                 f"(확인 번호 `{cid}` · {_CONFIRM_TTL_S // 60}분)")
    return "\n".join(lines)


async def _confirm_card(app_, req, ctx):
    """턴 끝의 확인 카드 — 막은 호출의 예상 작업량을 묻고(잡 부류만) 서버 메모리에 둔 뒤 SSE confirm 사건을 낸다. 본문 끝에 붙일 글은 ctx.card 에
    둔다(답 본문은 다음 턴 이력으로 돌아온다 — meta 는 이력에 실리지 않는다). 화면 단추는 사건의 approve_text · decline_text 를 그대로 보낸다(꼴이
    두 리포에 두 벌로 생기지 않는다)."""
    calls = ctx.pending
    for c in calls:
        c["app"] = _app_label_cached(c["name"])
        # 승인 때 같은 앱인지 견줄 열쇠 — 승인 실행은 노출 이름을 그때 다시 풀어, 이름이 겹치던 앱 하나가 30분 안에 빠지거나 붙으면 사람이 승인하지 않은
        # 앱으로 갔다(반대 검토 2차 — submit_job 의 ste(24대) · stcx(356대)). 맵이 모르면 비운다(견주지 않는다).
        c["app_key"] = (_TOOLS_MAP_CACHE.get("map") or {}).get(c["name"], "")
        c["args_text"] = _confirm_args_text(c["args"])
    need = _confirm_workload_local([c for c in calls if c["kind"] == "job" and not c.get("workload")])
    if need:
        yield _sse("status", {"step": "확인 카드 — 예상 작업량을 묻는 중", "tool": None})
        await _confirm_workload(app_, req, need)
    cid, exp = _confirm_store(req, calls)
    ctx.card = _confirm_card_text(cid, calls, ctx.done)
    _confirm_count("cards")
    _confirm_count("held_calls", len(calls))
    print(f"[confirm] 카드 {cid} — " + ", ".join(f"{c['name']}({c['kind']})" for c in calls)[:300], flush=True)
    yield _sse("confirm", {"id": cid, "expires_at": int(exp * 1000),
                           "calls": [{"tool": c["name"], **({"app": c["app"]} if c.get("app") else {}), "kind": c["kind"],
                                      "args_text": c["args_text"], **_confirm_sse_args(c["canon"]),
                                      **({"workload": c["workload"]} if c.get("workload") else {}),
                                      "again": bool(c.get("again"))} for c in calls],
                           "approve_text": f"실행 — 확인 번호 {cid}", "decline_text": f"취소 — 확인 번호 {cid} 는 실행하지 않음"})


def _confirm_sse_args(canon: str) -> dict:
    """카드 사건의 인자 전부 — 승인하면 서버가 보낼 인자(정규 JSON, 기본값 앞)를 화면이 펼쳐 보일 수 있게 싣는다. _CARD_SSE_ARGS_MAX 자를 넘으면
    앞부분과 args_cut."""
    c = str(canon or "")
    return {"args": c} if len(c) <= _CARD_SSE_ARGS_MAX else {"args": c[:_CARD_SSE_ARGS_MAX], "args_cut": True}


def _confirm_with_defaults(name, targs):
    """승인 실행이 보낼 인자 — 챗 인자 기본값(_CHAT_ARG_DEFAULTS)을 처음 호출 때처럼 더한다(준 값이 이긴다, design-integration C-12). 승인 실행은
    챗 래퍼가 아닌 도구로 나가 G6 을 타지 않는다 — 표가 잡 도구로 늘어도 처음 호출과 같은 꼴이 나가게."""
    own = _arg_defaults_for(name, _CHAT_ARG_DEFAULTS)
    if not own:
        return targs
    if isinstance(targs, str):
        d = _arg_dict(targs or "{}")
        return json.dumps({**own[0], **d}, ensure_ascii=False) if d is not None else targs
    return {**own[0], **(targs or {})} if targs is None or isinstance(targs, dict) else targs


def _confirm_resend(ctx, calls: list, why: str) -> None:
    """승인했지만 보내지 못한 호출을 다시 카드로 돌린다(턴 끝에 새 카드가 선다) — 그 승인 열쇠는 거둔다(모델이 같은 호출을 불러도 G5 가 통과시키지 않게)."""
    gone = {id(c) for c in calls}
    ctx.approved = {k: v for k, v in ctx.approved.items() if id(v) not in gone}
    ctx.pending.extend({**c, "again": False, "workload": why} for c in calls)


async def _confirm_send(app_, req, ctx, turn_calls: list, turn_out: list):
    """승인된 카드의 호출을 서버가 보낸다 — 카드에 적힌 차례대로 처음 부른 꼴 그대로 한 번(invoke_tool 이면 invoke_tool 로 — 게이트웨이의 파괴 ·
    제어 이름 거절은 그대로 걸린다). 이 호출도 래퍼를 지나고(G1 ~ G4 를 다시 지난다 — 승인은 '사람이 시켰다' 이지 'id 가 맞다' 가 아니다) G5 는
    승인 열쇠로 통과한다. 사용자 자격만 쓴다 — svc_fallback=False(사람이 승인한 잡 · 쓰기가 말없이 서비스 계정으로 나가지 않는다, C-11). 결과는
    이 턴의 안내(ctx.note)와 근거(turn_calls · turn_out)에 싣는다. 보내지 않고 다시 카드로 돌리는 때가 셋이다 — 카드 턴엔 있던 사용자 자격이 이번
    말에 없다(no_pat — 그대로 보내면 서비스 계정 연결로 나간다) · 도구 목록을 못 받았다 · 노출 이름이 가리키는 앱이 카드 때와 다르다(반대 검토 2차)."""
    cid, calls = ctx.card_id, list(ctx.approved.values())
    if ctx.no_pat:
        _confirm_resend(ctx, calls, "승인했지만 이번 말에 사용자 자격이 실리지 않아 보내지 못했다(서비스 계정으로는 보내지 않는다)")
        ctx.note = (f"[확인 카드 {cid} 승인 — 그러나 이번 말에 사용자 자격(PAT)이 실리지 않아 승인된 호출을 보내지 않았다(서비스 계정으로는 보내지 "
                    "않는다). 그 호출을 다시 부르지 말고 사용자에게 잠시 뒤 다시 승인해 달라고 알려라 — 이 턴 끝에 확인 카드가 다시 뜬다]")
        print(f"[confirm] 승인 {cid} — 이번 말에 사용자 자격이 없어 보내지 않았다(카드 턴엔 있었다)", flush=True)
        yield _sse("status", {"step": f"확인 카드 {cid} — 사용자 자격이 없어 승인된 호출을 보내지 않았습니다", "tool": None})
        return
    try:
        tmap = await _tools_by_name(app_, req.groups, user=req.user_email, user_pat=req.user_pat, svc_fallback=False)
    except Exception as exc:  # noqa: BLE001 — 사용자 자격 거절(넘김 없음이라 로드 실패가 그대로 온다) · 게이트웨이 불통
        print(f"[confirm] 승인 {cid} — 도구 목록을 받지 못했다: {type(exc).__name__}: {exc}"[:300], flush=True)
        tmap = {}
    if not tmap:
        _confirm_resend(ctx, calls, "승인했지만 도구 목록을 받지 못해 보내지 못했다")
        ctx.note = (f"[확인 카드 {cid} 승인 — 그러나 서버가 도구 목록을 받지 못해(사용자 자격 거절이거나 게이트웨이 불통) 승인된 호출을 보내지 "
                    "못했다. 그 호출을 다시 부르지 말고 사용자에게 보내지 못했다고 알려라 — 이 턴 끝에 확인 카드가 다시 뜬다]")
        yield _sse("status", {"step": f"확인 카드 {cid} — 도구 목록을 받지 못해 승인된 호출을 보내지 못했습니다", "tool": None})
        return
    moved = []
    if any(c.get("app_key") for c in calls):
        cur = await asyncio.to_thread(_tools_map)          # 낡았으면 다시 묻는다(동기 urlopen — 스레드로)
        for c in calls:
            now = (cur or {}).get(c["name"], "")
            if c.get("app_key") and now != c["app_key"]:
                moved.append((c, now))
    if moved:
        for c, now in moved:
            _confirm_resend(ctx, [c], f"승인했지만 이 이름이 가리키는 앱이 카드 때({c['app_key']})와 달라({now or '없음'}) 보내지 않았다 — 다시 확인해 달라")
            print(f"[confirm] 승인 {cid} — {c['name'][:80]} 의 앱이 카드 때 {c['app_key']} → 지금 {now or '없음'} — 보내지 않았다", flush=True)
        yield _sse("status", {"step": f"확인 카드 {cid} — 도구가 가리키는 앱이 카드 때와 달라 {len(moved)}건은 보내지 않았습니다", "tool": None})
    gone = {id(c) for c, _now in moved}
    lines = []
    for i, c in enumerate(calls, start=1):
        if id(c) in gone:
            continue
        name = c["name"]
        targs = _confirm_with_defaults(name, c["args"])
        via = c["via"] or name not in tmap        # 목록에서 없어진 직접 도구는 invoke_tool 로
        ctx.approved[(name.strip().lower(), c["base"], _confirm_canon(targs))] = c     # 기본값을 더한 꼴도 승인 열쇠로 — 관문은 보내는 인자로 잰다
        call_id = f"{cid}-{i}"
        shown = _arg_dict(targs) if isinstance(targs, str) else targs
        detail = _tool_preview(targs if shown is None else shown)
        yield _sse("status", {"step": f"도구 호출: {name}", "tool": name, **({"via": "invoke_tool"} if via else {}),
                              "call": call_id, **({"detail": detail} if detail else {})})
        t0 = time.monotonic()
        out = await (_call(tmap, "invoke_tool", {"name": name, "arguments": targs}) if via else _call(tmap, name, targs))
        # 이 호출의 열쇠를 모두 '나갔다' 로 — 기본값 앞의 승인 열쇠(부른 이름, 맞은 이름, 처음 인자)까지. 기본값을 더한 꼴만 막으면 모델이 처음 인자로
        # 다시 부를 때 G5 가 승인으로 통과시켜 한 번의 승인에 잡이 두 번 나갔다(반대 검토 2차 — C-12 가 대비한 경우).
        ctx.used.update({(name.strip().lower(), c["base"], c["canon"]), (c["base"], c["canon"])})
        if out is None:
            txt, ok = f"{_TOOL_FAIL_MARK} 도구 {name} 호출 실패: 이 서버의 도구 목록에 그 도구도 invoke_tool 도 없어 보내지 못했다", False
        else:
            txt = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
            ok = _result_ok(txt)
            ctx.sent += 1
            _confirm_count("executed")
            turn_calls.append((name, detail or ""))
            _turn_out_add(turn_out, txt)
            ctx.done.append((name, _tool_preview(txt, 160)))
        prev, hand = _tool_preview(txt), _handoff_text(txt)
        yield _sse("status", {"step": f"도구 완료: {name}", "tool": name, **({"via": "invoke_tool"} if via else {}), "ok": ok,
                              "ts": int(time.time() * 1000), "ms": round((time.monotonic() - t0) * 1000), "call": call_id,
                              **({"result_preview": prev} if prev else {}), **({"result_full": hand} if len(hand) > len(prev) else {})})
        lines.append(f"- {name}({c.get('args_text') or _confirm_args_text(c['args'])}) → {_cut_mark(txt, 6000)}")
    print(f"[confirm] 승인 {cid} — {ctx.sent}건 보냄", flush=True)
    # 상태 기다림(HA-28)이 켜져 있으면 결과를 물은 사람에게는 상태 조회를 한 번 부르라고 시킨다 — '볼 수 있다' 처럼 허용만 하면 모델이 queued 로 답하고
    # 끝내 이 턴에 배운 잡 id(born)로 기다릴 기회가 사라진다(반대 검토 2차 — DW-02 의 '돌려서 결과 알려줘'). 꺼져 있으면 기다려 준다고 말하지 않는다.
    follow = ("결과(잡 id · 상태)를 사용자에게 알려라. 잡이고 사람이 결과를 물었으면(예 '돌려서 결과 알려줘') 이 잡 id 로 상태 조회 도구를 한 번 "
              "부르라 — 코드가 끝날 때까지 기다린다(그 사이 LLM 을 부르지 않는다)." if _status_wait_mod.on() else
              "결과(잡 id · 상태)를 사용자에게 알리고, 잡이면 상태 조회 도구로 진행을 볼 수 있다.")
    moved_note = (f"\n도구가 가리키는 앱이 카드 때와 달라 {len(moved)}건은 보내지 않았다 — 그 호출을 다시 부르지 마라(이 턴 끝에 확인 카드가 다시 뜬다)."
                  if moved else "")
    if lines:
        ctx.note = (f"[확인 카드 {cid} 승인 — 서버가 사람의 승인으로 이 호출을 보냈다]\n" + "\n".join(lines)
                    + "\n이 호출을 다시 하지 마라(같은 잡이 하나 더 선다). " + follow + moved_note)
    else:
        ctx.note = f"[확인 카드 {cid} 승인 — 그러나 승인된 호출을 보내지 않았다]" + moved_note


def _confirm_strip(text: str, card) -> str:
    """수 대조(근거 블록의 '출처 없는 수')에 쓸 답 — 카드 글을 뗀다. 카드의 예상 작업량('약 1,812초')은 조회 결과가 아니다(design-integration C-14)."""
    return text.replace(card, "") if card else text


async def _confirm_events(events):
    """챗 흐름을 그대로 흘린다 — 막은 호출(확인 대기)이 생긴 뒤 흐름이 오류로 끝나면(재귀 한도 등이 끊기와 겹쳤다) 삼키고 끝낸다. 턴의 except 로
    가면 자동 재시도가 그래프를 처음부터 다시 돌려 카드 대신 재시도 · 오류 문구로 끝난다."""
    try:
        async for ev in events:
            yield ev
    except Exception as exc:  # noqa: BLE001
        ctx = _turn_confirm.get()
        if ctx is None or not ctx.pending:
            raise
        print(f"[confirm] 막은 호출이 있는 턴의 흐름 오류 — 재시도 대신 카드로 끝낸다: {type(exc).__name__}: {exc}"[:300], flush=True)
    finally:
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()


async def _confirm_close(events) -> None:
    """끊은 흐름을 닫는다 — 막 시작한 모델 호출은 langgraph 의 취소로 접힌다. 닫다가 난 예외는 로그 한 줄로 삼킨다(턴의 except 로 가면 자동 재시도가
    그래프를 다시 돈다)."""
    try:
        await events.aclose()
    except Exception as exc:  # noqa: BLE001
        print(f"[confirm] 끊은 흐름을 닫다가 오류 — 무시하고 카드로 끝낸다: {type(exc).__name__}: {exc}"[:300], flush=True)


def _confirm_health() -> dict:
    """/health 의 confirm_gate — 켜짐 · 목록(읽었나 · 까닭 · 부류별 수 — 접두를 뗀 꼴 포함) · 살아 있는 카드 수 · 기동 뒤 셈. 경로는 싣지 않는다(박스
    배치가 드러난다 — core_vocab 과 같은 까닭). lists.loaded 가 거짓이면 바닥 목록만 돈다(형제 리포 경로부터 본다)."""
    lists = _confirm_lists()
    now = time.time()
    with _CONFIRM_LOCK:
        pending = sum(1 for v in _CONFIRM_PENDING.values() if v["exp"] > now)
        counts = {k: _CONFIRM_STAT.get(k, 0) for k in _CONFIRM_COUNT_KEYS}
    return {"on": CHAT_CONFIRM_GATE,
            "lists": {"loaded": lists["error"] is None, "error": lists["error"], "jobs": len(_CONFIRM_JOB_TOOLS),
                      "writes": len(lists["writes"]), "confirm": len(lists["confirm"])},
            "pending": pending, "counts": counts}


async def _get_tools_retry(scoped: dict, *, tries: int = 3, base_delay: float = 0.5):
    """게이트웨이에서 도구 목록을 받되, **일시적** 실패는 지수 백오프로 재시도한다(간헐 도구
    0개 해소). 게이트웨이 재기동·재연결은 보통 1~2초 내에 끝나므로, 그 창에 걸린 요청이
    도구 없이 응답하던 것을 흡수한다(cae00 '될 때도 안 될 때도' 실증). 401/403 인증 실패는
    재시도해도 같아서 즉시 던진다 — 지연만 늘 뿐이다. 이 서버의 에이전트가 묶지 않는 도구(_AGENT_DENY_TOOLS)는 여기서 뺀다."""
    import asyncio as _aio
    last = None
    for i in range(max(1, tries)):
        try:
            got = [t for t in await MultiServerMCPClient(scoped).get_tools() if not _agent_denied(getattr(t, "name", ""))]
            # 빈 목록도 실패로 본다 — 게이트웨이가 부팅 직후 tools=0 인 과도 상태를 완결로
            # 받으면 도구 없는 에이전트가 만들어진다. 마지막 시도가 아니면 재시도.
            if not got and i < tries - 1:
                raise RuntimeError("empty tool list")
            return _call_trace.wrap_tools(got)     # 호출 추적이 켜져 있을 때만 도구마다 시간·성패를 잰다(꺼져 있으면 그 목록 그대로)
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
                     core_names: tuple | None = None, prefer: list[str] | None = None):
    """ReAct agent whose tools are the gateway's group-filtered set for this caller.
    Cached by group-set; the tools carry the groups header so tool *calls* are scoped too.
    pinned·first·core_names·prefer 는 TOOL_MAX 캡 환경에서만 바인딩 구성을 바꾸므로 그때만
    캐시 키에 포함한다(무제한 환경은 바인딩 동일 → 키 분화 없이 시스템 프롬프트 지시로만 반영).
    무제한 환경이라도 스키마 예산 때문에 잘라 만든 에이전트는 턴마다 원본 목록에서 다시 고른다(HA-01 — 아래 hit).
    ⚠ prefer 를 키에서 빼면 전문가를 바꿔도 앞 전문가로 만든 에이전트가 재사용돼 **조용히**
    무시된다 — 소스 토글에서 이미 같은 사고가 났다(아래 주석)."""
    # ⚠ prefer 는 TOOL_MAX 와 **무관하게** 키에 들어가야 한다. 캡이 없어도 바인딩 **순서**를
    #   바꾸기 때문이다(운영 cae00 이 정확히 TOOL_MAX=0 이다). 조건에 TOOL_MAX>0 을 걸면
    #   전문가를 바꿔도 앞 전문가 순서로 만든 에이전트가 재사용된다.
    pin_key = ((tuple(sorted(pinned or [])), tuple(sorted(first or [])), core_names)
               if ((pinned or first or core_names is not None) and TOOL_MAX > 0) else ())
    pin_key = (pin_key, tuple(prefer or [])) if prefer else pin_key
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
    hit = cache.get(key)
    if hit is not None and AGENT_CACHE_TTL_S > 0 and time.time() - getattr(hit, "_hwax_at", time.time()) > AGENT_CACHE_TTL_S:
        cache.pop(key, None)         # 카탈로그가 바뀌었을 수 있다 — 게이트웨이에서 다시 받는다(HA-21)
        hit = None
    if hit is not None and TOOL_MAX <= 0 and len(getattr(hit, "_hwax_raw", ()) or ()) > len(getattr(hit, "_hwax_tools", ()) or ()):
        # 무캡 경로가 스키마 예산 때문에 잘라 만든 에이전트다 — 그 묶음은 첫 질의 · 핀으로 고른 것이라, 키(질의 · 핀이 없다)로 재사용하면
        # 30분 창의 다음 질문에도 앞 묶음을 썼고 창 중간에 켠 앱 핀은 안내만 바뀌었다(HWAXPortal docs/request-1027 HA-01 — 10-25 ~ 10-27
        # 절단 25회 중 17회가 앞 묶음). 원본 목록(같은 자격 — 같은 키)에서 이번 턴으로 다시 고르고, 묶음이 바뀌었을 때만 새로 짓는다.
        # 게이트웨이에서 다시 받지 않는다 — 도구 벡터는 이미 캐시라 질의 임베딩 한 번만 든다.
        import asyncio as _aio
        tools = await _aio.to_thread(_select_tools, list(hit._hwax_raw), query, pinned, first, core_names, prefer)
        names = tuple(getattr(t, "name", "") for t in tools)
        if names != hit._hwax_tools:
            agent = create_react_agent(_chat_llm_of(app), tools, prompt=_loop_fit_prompt)
            agent._hwax_tools, agent._hwax_raw = names, hit._hwax_raw
            agent._hwax_at = getattr(hit, "_hwax_at", time.time())     # 원본 목록을 받은 때 — 수명은 그때부터 센다
            cache.pop(key)
            cache[key] = agent
            return agent
    if key not in cache:
        tools = []
        raw_pool = None     # 무캡 경로에서 다시 고를 원본 목록(소스 게이트 뒤) — 잘린 에이전트만 쓴다
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
                _raw = [_prep_tool(t, arg_defaults=_CHAT_ARG_DEFAULTS, chat=True) for t in _got]
                # 성공분을 스냅샷에 보관(source 게이트·select 전 순수 목록) — 다음 불통 때
                # 폴백 재료다. 사용자 신원 키라 남에게 재사용되지 않는다.
                if _raw:
                    app.state.tool_snapshot[(frozenset(groups), (user or "").strip().lower())] = (
                        _raw, time.time())
                _raw = gate_sources(_raw, sources)
                raw_pool = tuple(_raw)
                # 임베딩 호출은 동기 HTTP — 이벤트 루프를 막지 않게 스레드로 뺀다(동시 챗 보호).
                tools = await _aio.to_thread(_select_tools, _raw, query, pinned, first, core_names, prefer)
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
                    raw_pool = tuple(_sr)
                    tools = await _aio.to_thread(_select_tools, _sr, query, pinned, first, core_names, prefer)
                    load_failed = False   # 도구가 살아 있으니 실패 아님 — 단 신선하지 않다
                    degraded = f"stale-snapshot({_age}s): {detail}"
                    app.state.tool_load_error.pop(frozenset(groups), None)
                    print(f"[agent] 스냅샷 폴백 — 도구 {len(tools)}개(나이 {_age}s) 로 응답. 게이트웨이 복구 시 자동 갱신")
        if not load_failed:
            # 성공했으면 이 그룹셋의 과거 오류를 지운다(다른 그룹셋 상태는 건드리지 않는다).
            app.state.tool_load_error.pop(frozenset(groups), None)
        # 모델 호출마다 쌓인 메시지를 재어 창을 넘으면 오래된 도구 결과부터 줄인다(_loop_fit_prompt — 넓은 창). agent 노드
        # 안에서 줄인다 — pre_model_hook 은 별도 노드라 왕복마다 그래프 단계를 하나 더 쓴다(AGENT_RECURSION_LIMIT 주석).
        agent = create_react_agent(_chat_llm_of(app), tools, prompt=_loop_fit_prompt)   # 챗 전용 LLM(있으면)
        # 실제로 묶인 도구 이름을 달아 둔다. 호출부가 "N종 우선 바인딩" 같은 말을 하려면
        # **묶인 것**을 알아야 하는데, prefer 는 그룹 필터 없는 /tools-map(465종)에서 뽑은
        # 이름이라 그 사용자에게 없는 도구가 섞일 수 있다(CAEG 헤더로는 8종만 온다).
        # 캐시 값 모양을 바꾸지 않으려고 객체에 붙인다 — 캐시를 타도 함께 따라온다.
        agent._hwax_tools = tuple(getattr(t, "name", "") for t in tools)
        agent._hwax_raw = raw_pool if TOOL_MAX <= 0 else None     # 캡 경로는 질의 · 핀이 이미 키에 있다
        agent._hwax_at = time.time()
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


def _loop_room_tokens() -> int:
    """문서 없는 챗 턴에서 이력 밖에 남겨 둘 도구 결과 몫(토큰) — 넓은 창에서만 뗀다. 결과 하나의 상한(TOOL_RESULT_MAX)을
    최악 환산(한글 1.05자/토큰)으로 바꾼 값이고, (창 − 예비분)의 절반을 넘지 않는다. 종전엔 1백만 토큰 창에서 이력이
    (창 − 예비분)의 85% 를 다 쓰면 남는 몫(약 149K 토큰)이 도구 결과 하나(약 190K 토큰)보다 작아, 큰 조회 한 번에 ReAct
    루프가 창을 넘었다. 좁은 창은 0(종전 — 작은 박스는 .env 로 TOOL_RESULT_MAX 를 낮춘다), 무제한(0)도 뗄 값을 몰라 0."""
    if not _wide() or TOOL_RESULT_MAX <= 0:
        return 0
    return min(int(TOOL_RESULT_MAX / 1.05) + 1, max(0, _model_context_tokens() - DOC_RESERVE_TOKENS) // 2)


# 챗 ReAct 루프의 모델 입력 맞추기 — 한 턴에 큰 도구 결과를 여럿 받아도 창을 넘기지 않는다. 위 몫(_loop_room_tokens)은 결과 **한 건**
# 분이라, 이력이 찬 긴 대화에서 큰 조회를 두 번 하거나(TOOL_RESULT_MAX 200,000자 둘) 12만 자급을 세 번 하면 루프 안에서 창을 넘어
# 400 으로 턴이 통째로 실패했고, 자동 재시도 둘도 같은 이력으로 같은 400 이었다(m1 2차 AF1-loop-room-single). 운영 창은
# 1,048,576 토큰이고 지침은 '자르지 않는다' 라 평소엔 아무것도 건드리지 않는다 — 쌓인 메시지의 추정 토큰이 창 − 예비분
# (DOC_RESERVE_TOKENS — 도구 스키마·출력 몫)을 넘을 때만 **오래된 도구 결과부터** 원문 길이 표식을 달고 넘는 만큼 줄인다.
# 대화 상태는 그대로 두고 모델에 가는 글(agent 노드의 prompt 가 돌려주는 메시지)만 바꾸므로, 호출마다 원문에서 다시 잰다. 줄였으면 사건
# (hwax_loop_fit)을 내고 챗 스트림이 경고(loop_results_cut)로 바꾼다. 좁은 창은 종전대로 손대지 않는다 — 작은 박스는
# .env 로 TOOL_RESULT_MAX 를 낮추고(_loop_room_tokens 와 같은 약속), 예비분이 창보다 큰 박스(dev 16K)는 이 문턱이 음수다.
_LOOP_FIT_EVENT = "hwax_loop_fit"
_LOOP_FIT_FLOOR = 1000      # 줄여도 남기는 도구 결과의 앞부분(자) — 무엇을 조회했는지는 보이게


def _msg_tokens(m) -> int:
    """메시지 하나의 추정 토큰 — 본문(글 전체로 잰다)과 도구 호출 인자."""
    c = getattr(m, "content", "")
    text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)
    calls = getattr(m, "tool_calls", None)
    return _est_tokens_all(text) + (_est_tokens_all(json.dumps(calls, ensure_ascii=False, default=str)) if calls else 0)


def _fit_loop_messages(msgs: list) -> tuple[list, dict | None]:
    """루프가 모델에 보낼 메시지 — 창 − 예비분을 넘으면 오래된 도구 결과부터 넘는 만큼 줄인 사본과 {n(줄인 건수), chars(그
    결과들의 원문 합), before·after(추정 토큰), limit}, 넘지 않으면 (받은 그대로, None)."""
    if not _wide():
        return msgs, None
    limit = _model_context_tokens() - DOC_RESERVE_TOKENS
    toks = [_msg_tokens(m) for m in msgs]
    before = sum(toks)
    excess = before - limit
    if limit <= 0 or excess <= 0:
        return msgs, None
    out, n, chars = list(msgs), 0, 0
    for i, m in enumerate(msgs):
        if excess <= 0:
            break
        if not isinstance(m, ToolMessage) or not isinstance(m.content, str) or len(m.content) <= _LOOP_FIT_FLOOR:
            continue
        text, target = m.content, max(1, toks[i] - excess)

        def build(c, text=text):
            if c >= len(text):
                return text
            return (text[:c].rstrip() + f"\n…[창이 차서 이 도구 결과 {len(text):,}자 중 앞 {c:,}자만 실었다 — 뒷부분이 "
                    "필요하면 조건을 좁혀 다시 조회하라]")

        body = _fit_tokens(build, max(_LOOP_FIT_FLOOR, _chars_for_tokens(text, target)), target, _LOOP_FIT_FLOOR)[0]
        t = _est_tokens_all(body)
        if t >= toks[i]:
            continue
        out[i] = m.model_copy(update={"content": body})
        excess -= toks[i] - t
        n, chars = n + 1, chars + len(text)
    if not n:
        return msgs, None
    return out, {"n": n, "chars": chars, "before": before, "after": limit + excess, "limit": limit}


async def _loop_fit_prompt(state, config):
    """create_react_agent 의 prompt — agent 노드 안에서 모델 직전에 돌아 _fit_loop_messages 를 모델 입력에만 건다(돌려준
    메시지는 모델에만 가고 대화 상태에는 응답만 더해진다). 줄였으면 사건을 낸다(챗이 경고로 바꾼다).
    ⚠ pre_model_hook 으로 달지 마라 — 별도 노드라 왕복마다 그래프 단계를 하나 더 써 연속 왕복이 39회 → 25회로 준다.
    config 를 넘긴다 — 파이썬 3.10 은 비동기 사건에 실행 맥락이 저절로 따라가지 않는다."""
    fitted, info = _fit_loop_messages(state["messages"])
    if info:
        print(f"[agent] 루프 메시지 {info['before']:,}토큰이 창 몫 {info['limit']:,}토큰을 넘어 오래된 도구 결과 "
              f"{info['n']}건을 줄였다(→ {info['after']:,}토큰)")
        await adispatch_custom_event(_LOOP_FIT_EVENT, info, config=config)
    # 도구 그림 — 챗 모델이 그림을 받으면(턴이 run config 로 알린다 — 문맥 변수는 LangGraph 실행 맥락을 넘지 못한다) 그 결과 뒤에 그림을 싣는다.
    # 턴이 모르면(None — 아직 안 물었다) 도구가 그림을 실제로 낸 지금 묻는다(한 번 — 판정은 서버마다 남는다).
    hv = ((config or {}).get("configurable") or {}).get("hwax_vision")
    if hv is None and _vision.tool_image_urls(fitted):
        hv = await asyncio.to_thread(_chat_vision, app)
    if hv:
        fitted = await asyncio.to_thread(_with_tool_images, fitted, _vision.MAX_IMAGES)
    return fitted


def _hist_budget_tokens(has_docs: bool = False) -> int:
    """이력에 줄 **토큰** 예산 — (창 − 예비분 [− 도구 결과 몫]) × 몫. 글자 예산(_hist_budget_chars)은 이것을 표본의 밀도로
    바꾼 값이라, 표본과 실제로 싣는 글의 구성이 다르면 이 값으로 다시 잰다(_fit_tokens·_history_messages)."""
    share = HIST_CTX_SHARE if has_docs else HIST_CTX_SHARE_SOLO
    reserve = DOC_RESERVE_TOKENS + (0 if has_docs else _loop_room_tokens())
    return max(1200, int((_model_context_tokens() - reserve) * share))


def _hist_budget_chars(sample: str = "", has_docs: bool = False) -> int:
    """이력에 줄 글자 예산.

    env HIST_BUDGET 은 **낮추기만** 한다. 올리는 쪽으로 두면 낡은 .env 한 줄이 컨텍스트를
    조용히 넘겨 400 을 만든다(실측: dev .env 의 HIST_BUDGET=16000 이 16,384 창을 넘겼다).
    탐지가 틀려 창을 크게 잡아야 하면 그건 LLM_CONTEXT_TOKENS 로 고칠 일이다.
    문서 없는 턴은 도구 결과 몫(_loop_room_tokens)을 먼저 떼고 나눈다. 문서가 있는 턴은 문서가 나머지를 쓴다
    (_doc_budget_tokens — 그 길의 도구 결과 몫은 따로 없다).
    """
    derived = max(1500, _chars_for_tokens(sample or "가나다라", _hist_budget_tokens(has_docs)))
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


# 앞 턴의 조회 근거를 그 턴의 물음 뒤에 붙일 때의 머리 — 사람 쪽 메시지에 '시스템 첨부' 로 단다. 답 쪽에 넣으면 모델이 이 꼴을 제
# 답으로 흉내 낸다. 그때의 값이라는 것도 적는다 — 바뀌었을 만한 값을 옛 근거로 단정하지 않게.
_HIST_EVID_HEAD = ("[시스템 첨부 — 어시스턴트가 이 물음에 답하려고 도구로 조회한 결과다(앞 턴). 같은 값을 다시 물으면 다시 조회하지 "
                   "말고 이것을 인용하라. 그때의 값이라 그사이 바뀌었을 만한 것은 다시 조회하라]")
_HIST_EVID_FLOOR = 1000     # 남은 근거 몫이 이보다 작으면 줄여 싣지 않고 도구 이름만 남긴다(토막은 근거가 못 된다)
# 업로드 근거(kind=upload — 사람이 챗 화면에서 파일을 올려 보낸 결과, 포털이 사람 대신 도구를 불렀다)의 머리. 위 머리('이 물음에 답하려고
# 조회한')를 달면 앞 물음과 상관없는 일이 그 답의 근거로 읽힌다 — 10-22 '방금 변환한 것' 이 대화에 없어 앞 그림을 검토했다(HWAXPortal
# docs/chat-ppt PP-D3). 알림은 대개 앞 답 뒤에 오므로 그 알림 바로 앞에 사람 쪽 메시지로 따로 선다(바로 앞이 사람 메시지면 거기 잇되 이 머리로 갈린다).
_HIST_UPLOAD_HEAD = ("[시스템 첨부 — 사람이 챗 화면에서 파일을 올려 보낸 결과다(포털이 사람 대신 도구를 불렀다 — 어시스턴트가 조회한 것이 "
                     "아니다). 사람이 '방금 올린 것 · 변환한 것 · 보낸 것' 이라고 하면 이것을 가리킨다. 같은 결과를 다시 조회하지 말고 "
                     "이것을 쓰라]")


def _hist_evidence(entry: dict, item_cap: int) -> tuple[list[str], list[str], list[str], list[str]]:
    """이력 항목의 evidence(포털 ChatHistoryMessage.evidence — 그 턴에 도구가 낸 [{tool, args, result, kind?}]) →
    (조회 줄들, 조회 도구 이름들, 업로드 줄들, 업로드 도구 이름들). kind=upload 는 사람이 올려 보낸 결과라 따로 모은다(PP-D3).
    모양이 틀린 것은 건너뛴다. 결과 하나는 좁은 창에서만 항목 상한(item_cap)으로 줄인다 — 넓은 창은 이 턴의 근거 몫이 지킨다."""
    rows: list[str] = []
    names: list[str] = []
    up_rows: list[str] = []
    up_names: list[str] = []
    ev = entry.get("evidence")
    for e in (ev if isinstance(ev, list) else []):
        if not isinstance(e, dict):
            continue
        tool = str(e.get("tool") or "").strip()[:200]
        res = str(e.get("result") or "").strip()
        if not tool or not res:
            continue
        args = " ".join(str(e.get("args") or "").split())[:2000]
        line = f"· {tool}{f'({args})' if args else ''} → {_cut_mark(res, item_cap)}"
        rs, ns = (up_rows, up_names) if e.get("kind") == "upload" else (rows, names)
        rs.append(line)
        if tool not in ns:
            ns.append(tool)
    return rows, names, up_rows, up_names


def _history_messages(history: list[dict], stats: dict | None = None,
                      has_docs: bool = False) -> list[tuple[str, str]]:
    """멀티턴 history 를 검증·절단해 LangChain 메시지 tuple 로 만든다.

    방어: role 이 user/assistant 외면 무시, 항목별 HIST_ITEM_MAX 절단, 항목 수 최대
    HIST_MAX_ITEMS. 전체는 최신 것 우선으로 예산 안에 담고, **밀려난 오래된 턴은 버리지 않고
    압축본 한 덩어리로** 맨 앞에 넣는다(_compact_turns).

    어시스턴트 항목에 evidence(그 턴에 도구가 조회한 결과)가 있으면 그 턴의 물음 뒤에 '시스템 첨부' 로 붙인다 — 종전엔 글만 실어
    다음 턴이 '아까 조회한 그 값' 을 모르고 다시 조회하거나 지어냈다(HWAXPortal docs/chat-depth-context CD-D5). 근거 몫은 이력
    예산의 HIST_EVIDENCE_SHARE 까지다. 1차로 원문에 남는 턴을 정하고, 그 턴들의 근거만큼 원문 몫을 줄여 다시 채운다(2차) — 근거가
    없는 이력은 1차 그대로라 종전과 같다. 근거는 최신 턴부터 싣고, 몫이 모자라면 오래된 턴의 근거부터 줄이고 그다음엔 도구 이름만
    남긴다(결론 글보다 원자료를 먼저 버린다). 압축본에는 그 턴이 조회한 도구 이름을 남긴다. 근거 중 kind=upload(사람이 챗 화면에서
    파일을 올려 보낸 결과 — 포털이 대화에 남긴 알림의 근거)는 따로 된 머리로 싣는다 — 앞 물음의 근거가 아니다(HWAXPortal docs/chat-ppt PP-D3).

    stats 를 주면 {"kept", "compacted", "budget"} 을 채워 준다 — 호출부가 사용자에게 알린다. 가장 최근 항목 하나를
    줄였으면 "cut"(그 항목의 원문 길이)과 "cut_role"(user·assistant — 경고 문구를 가른다)도 채운다. 근거를 실었으면
    "evidence"(온전히 실은 턴 수) · "evidence_cut"(앞부분만 실은 턴 수) · "evidence_names"(도구 이름만 남긴 턴 수)도 채운다.
    """
    items: list[tuple[str, str]] = []
    raws: list[str] = []                    # 항목 상한 전의 원문 — 최근 항목을 다시 줄일 때 원문 길이를 적는다
    evs: list[list[str]] = []               # 항목마다 그 턴의 조회 근거 줄(어시스턴트 항목만)
    names_of: list[list[str]] = []          # 항목마다 그 턴이 조회한 도구 이름
    ups: list[list[str]] = []               # 항목마다 업로드 근거 줄(kind=upload — 사람이 올려 보낸 결과, PP-D3)
    up_names_of: list[list[str]] = []       # 항목마다 업로드 근거의 도구 이름
    # 항목 하나의 상한 — 좁은 창은 HIST_ITEM_MAX(종전 24,000자), 넓은 창은 그 턴의 이력 예산이 지킨다(항목 상한 없음).
    # 포털은 항목을 창 하나까지 받는데(HWAXPortal ee24499) 종전엔 1백만 토큰 창에서도 24,000자에서 '…' 로 끊어, 이력의
    # 결정문 뒤쪽이 빠졌다. 끊으면 원문 길이를 적는다.
    _item_cap = _narrow_cap(HIST_ITEM_MAX)
    for entry in history:
        if not isinstance(entry, dict):
            continue
        role, content = entry.get("role"), entry.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content:
            continue
        items.append((role, _cut_mark(content, _item_cap)))
        raws.append(content)
        rows, names, up_rows, up_names = _hist_evidence(entry, _item_cap) if role == "assistant" else ([], [], [], [])
        evs.append(rows)
        names_of.append(names)
        ups.append(up_rows)
        up_names_of.append(up_names)
    items, raws = items[-HIST_MAX_ITEMS:], raws[-HIST_MAX_ITEMS:]
    evs, names_of = evs[-HIST_MAX_ITEMS:], names_of[-HIST_MAX_ITEMS:]
    ups, up_names_of = ups[-HIST_MAX_ITEMS:], up_names_of[-HIST_MAX_ITEMS:]

    budget = _hist_budget_chars("".join(c for _, c in items[-6:]), has_docs)
    verbatim_budget = int(budget * (1.0 - HIST_COMPACT_SHARE))
    # 글자 예산은 최근 6항목의 앞 20,000자 밀도로 환산한 값이다 — 여섯째 항목이 영문 로그면 한국어 이력이 창의 몇 배로
    # 실렸다. 항목마다 글 전체로 잰 토큰(_est_tokens_all)으로도 지킨다(둘 중 먼저 차는 쪽에서 멈춘다).
    tok_budget = _hist_budget_tokens(has_docs)
    verbatim_tok = int(tok_budget * (1.0 - HIST_COMPACT_SHARE))

    def _fill(vb: int, vt: int):
        """최신부터 원문 몫(vb 자 · vt 토큰)을 채운다 — ([(항목 번호, 역할, 글)] 시간순, 압축 경계, 줄인 원문 길이, 그 역할)."""
        kept: list[tuple[int, str, str]] = []
        used = used_tok = 0
        cut = len(items)                        # 이 앞의 것들이 압축 대상
        item_cut, item_role = 0, ""
        for idx in range(len(items) - 1, -1, -1):   # 최신부터 예산을 채운다
            role, content = items[idx]
            tok = _est_tokens_all(content)
            if used + len(content) > vb or used_tok + tok > vt:
                if kept:
                    break
                # 가장 최근 항목 하나가 원문 몫을 넘는다 — 종전엔 예산과 상관없이 통째로 넣었다. 포털은 항목을 창 하나
                # (4,194,304자)까지 받으므로 창을 넘는 붙여넣기 한 번이 그 턴을 400 으로 죽이고, 실패한 턴의 메시지가 다음
                # 턴의 최신 이력이 되어 이후 턴과 자동 재시도가 전부 같은 400 이었다. 원문 길이를 적고 원문 몫까지 줄인다.
                # 좁은 창은 항목 상한(HIST_ITEM_MAX)이 이미 묶으므로 그 항목이 창을 실제로 넘길 때만(_hist_item_room_tokens)
                # 그 문턱까지 줄인다 — 원문 몫에 맞추면 예비분이 창보다 큰 dev 16K 에서 몫이 바닥(960토큰)이라, 보통 답 하나가
                # 매 턴 약 1,000자로 잘렸다(m1 2차 AF1-dev-hist-cut).
                room = vt if _wide() else _hist_item_room_tokens(vt)
                if _wide() or tok > room:
                    raw = raws[idx]
                    chars = vb if _wide() else _chars_for_tokens(raw, room)
                    content = _fit_tokens(lambda c: _cut_mark(raw, c), max(1, chars - _CUT_MARK_ROOM), room, 1)[0]
                    item_cut, item_role = len(raw), role
                    tok = _est_tokens_all(content)
            kept.append((idx, role, content))
            used += len(content)
            used_tok += tok
            cut = idx
        kept.reverse()
        return kept, cut, item_cut, item_role

    def _blocks(idx: int) -> list[tuple[str, str]]:
        """그 항목의 근거 덩이 [(종류, 글)] — 업로드(사람이 올려 보낸 결과, PP-D3)가 앞, 그 턴의 조회가 뒤."""
        out_: list[tuple[str, str]] = []
        if ups[idx]:
            out_.append(("upload", "\n".join([_HIST_UPLOAD_HEAD, *ups[idx]])))
        if evs[idx]:
            out_.append(("tool", "\n".join([_HIST_EVID_HEAD, *evs[idx]])))
        return out_

    ev_cap_c, ev_cap_t = int(budget * HIST_EVIDENCE_SHARE), int(tok_budget * HIST_EVIDENCE_SHARE)

    def _need(kept_) -> tuple[int, int]:
        """원문에 남는 턴들의 근거가 쓸 몫(자 · 토큰) — 근거 몫(HIST_EVIDENCE_SHARE)까지."""
        c = t = 0
        for idx, _r, _c in kept_:
            for _kind, b in _blocks(idx):
                c, t = c + len(b) + 2, t + _est_tokens_all(b)
        return min(c, ev_cap_c), min(t, ev_cap_t)

    kept, cut, item_cut, item_role = _fill(verbatim_budget, verbatim_tok)
    need_c, need_t = _need(kept)
    if need_c:
        kept, cut, item_cut, item_role = _fill(max(1, verbatim_budget - need_c), max(1, verbatim_tok - need_t))
        need_c, need_t = _need(kept)        # 2차로 남은 턴의 근거 — 1차보다 적거나 같다

    out: list[tuple[str, str]] = [(r, c) for _i, r, c in kept]
    n_full = n_cut = n_names = 0
    left_c, left_t = need_c, need_t
    for k in range(len(kept) - 1, -1, -1):  # 최신 턴의 근거부터 — 몫이 모자라면 오래된 턴의 것이 먼저 줄고 빠진다
        idx = kept[k][0]
        a_pos = k                           # 이 답의 자리 — 앞에 따로 넣으면 한 칸 밀린다
        for kind, blk in _blocks(idx):
            t = _est_tokens_all(blk)
            if len(blk) + 2 <= left_c and t <= left_t:
                text = blk
                n_full += 1
            elif left_c - _CUT_MARK_ROOM - 2 >= _HIST_EVID_FLOOR and left_t > 0:
                text = _fit_tokens(lambda c, b=blk: _cut_mark(b, c), left_c - _CUT_MARK_ROOM - 2, left_t, 200)[0]
                n_cut += 1
            elif kind == "upload":
                text = (f"[시스템 첨부 — 사람이 챗 화면에서 파일을 올려 보냈다({', '.join(up_names_of[idx])}). 결과는 이력 예산 밖이라 "
                        "싣지 않았다 — 알림에 적힌 파일 이름 · 보고서 번호로 다시 조회하라]")
                n_names += 1
            else:
                text = (f"[시스템 첨부 — 어시스턴트가 이 물음에 답하려고 {', '.join(names_of[idx])} 을(를) 조회했다. 결과는 이력 "
                        "예산 밖이라 싣지 않았다 — 그 값이 필요하면 다시 조회하라]")
                n_names += 1
            left_c -= len(text) + 2
            left_t -= _est_tokens_all(text)
            # 그 턴의 물음(바로 앞의 사람 발화) 뒤에 붙인다 — 물음이 압축본으로 밀려났거나 바로 앞이 답이면(업로드 알림은 대개 앞
            # 답 뒤에 온다) 그 답 바로 앞에 사람 쪽 메시지로 따로 넣는다. 같은 역할이 둘 잇달아 가지 않게 하는 것은 둘 다 같다 —
            # 업로드는 머리가 따로라('사람이 올려 보낸 결과') 앞 물음의 근거로 읽히지 않는다.
            if a_pos > 0 and out[a_pos - 1][0] == "user":
                out[a_pos - 1] = ("user", f"{out[a_pos - 1][1]}\n\n{text}")
            else:
                out.insert(a_pos, ("user", text))
                a_pos += 1

    # 압축본도 토큰으로 지킨다 — 글자 몫이 영문 표본으로 부풀었으면 한국어 압축본이 창을 넘는다. 근거가 있던 턴은 조회한 도구
    # 이름을 앞에 남긴다(그 턴에 무엇을 봤는지는 남긴다 — 다시 조회할지 모델이 고른다).
    def _tag(i: int) -> str:
        return " · ".join([*([f"올림: {', '.join(up_names_of[i])}"] if up_names_of[i] else []),
                           *([f"조회: {', '.join(names_of[i])}"] if names_of[i] else [])])
    dropped = [(r, (f"({_tag(i)}) {c}" if _tag(i) else c)) for i, (r, c) in enumerate(items[:cut])]
    digest = (_fit_tokens(lambda c: _compact_turns(dropped, c), int(budget * HIST_COMPACT_SHARE),
                          int(tok_budget * HIST_COMPACT_SHARE), 200)[0] if cut else "")
    if digest:
        out.insert(0, ("user", digest))
    if stats is not None:
        stats.update(kept=len(items) - cut, compacted=cut, budget=budget)
        if item_cut:
            stats["cut"], stats["cut_role"] = item_cut, item_role
        if n_full or n_cut or n_names:
            stats.update(evidence=n_full, evidence_cut=n_cut, evidence_names=n_names)
    return out


def _hist_item_room_tokens(verbatim_tok: int) -> int:
    """좁은 창에서 가장 최근 이력 항목 하나를 줄이는 문턱(토큰) — 그 항목 하나로 창을 넘길 때다. 창 − 예비분이고, 예비분은
    창의 절반까지만 본다. 예비분(DOC_RESERVE_TOKENS)은 도구 스키마 예산에서 유도한 위쪽 어림이라 좁은 박스에서 창보다 클 수
    있다(dev 16K 는 TOOL_SCHEMA_BUDGET=12000 이라 28,000) — 그대로 빼면 문턱이 음수가 되어 모든 항목을 줄인다. 원문 몫보다 작아지지
    않는다(넓은 창은 이 문턱을 쓰지 않는다)."""
    ctx = _model_context_tokens()
    return max(verbatim_tok, ctx - min(DOC_RESERVE_TOKENS, ctx // 2))


# 핸드오프(챗→심의)용 도구 결과 길이. 화면 미리보기(220)와 **일부러 분리한다** —
# 활동 패널은 짧아야 읽히고, 심의는 날것이 많이 필요하다(220자×12건 ≈ 2.6KB 만 도착해
# 예산이 비어 있었다). 표시용을 늘리면 패널도 브라우저 localStorage 도 같이 무거워지므로
# 필드를 나눈다. 심의 예산이 60KB 로 올라갔으므로(deliberation._EVID_BUDGET) 여기도 같이
# 올린다 — 4000자×40건이면 예산을 채운다. 안 올리면 예산만 크고 실제로 실리는 건 그대로다.
# 이 값은 좁은 창의 길이다 — 넓은 창(1백만 토큰)은 심의가 근거 항목 하나로 받는 길이(_EVID_ITEM_MAX 150,000자)까지
# 싣는다(_handoff_chars). 종전엔 1백만 토큰 창에서도 4,000자라 큰 표·목록의 앞 몇 줄만 심의에 갔다(사용자 지침
# 2026-10-10). 명시하면(HANDOFF_RESULT_CHARS) 넓은 창에서는 낮추기만 한다. 포털은 받은 것을 서버 대화에 그대로 남긴다.
HANDOFF_RESULT_CHARS = int(os.environ.get("HANDOFF_RESULT_CHARS", "4000"))
_HANDOFF_SET = bool(os.environ.get("HANDOFF_RESULT_CHARS"))


def _handoff_chars() -> int:
    """챗→심의 핸드오프(result_full·detail_full) 길이(자) — 좁은 창 HANDOFF_RESULT_CHARS, 넓은 창 _EVID_ITEM_MAX."""
    if not _wide():
        return HANDOFF_RESULT_CHARS
    got = max(HANDOFF_RESULT_CHARS, _EVID_ITEM_MAX)
    return min(HANDOFF_RESULT_CHARS, got) if _HANDOFF_SET else got


def _handoff_text(v) -> str:
    """도구 입출력의 핸드오프용 날것 — 미리보기(_tool_preview)와 같은 규약(공백 압축·지문 표식)에 길이만 다르다."""
    return _tool_preview(v, _handoff_chars())


def _turn_out_add(turn_out: list, txt: str) -> None:
    """결정문·답변 수치 대조의 원문(turn_out)에 도구 결과를 싣는다. 좁은 창은 종전대로 결과마다 앞 120,000자·합
    400,000자, 넓은 창은 결과를 자르지 않고(모델이 본 그대로 — _cap 이 이미 TOOL_RESULT_MAX 로 묶었다) 합은 창이
    담는 글자 수까지다. 종전엔 모델이 200,000자까지 본 결과의 뒤쪽 수치가 '출처 없음' 으로 찍혔다.
    좁은 창에서 자른 결과에는 원문 길이 표지(_cut_mark)를 단다 — 자기검증 검토자가 그 결과 머리에 '(잘림 — 근거 몫' 을 본다(HA-27)."""
    if _wide():
        item, total = 0, max(400_000, int(_model_context_tokens() * 1.05))
    else:
        item, total = 120_000, 400_000
    if sum(len(x) for x in turn_out) < total:
        turn_out.append(_cut_mark(txt, item) if item else txt)

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
# 시스템 프롬프트 + **도구 스키마** + 출력 몫. 도구 스키마가 압도적으로 크다 —
# TOOL_SCHEMA_BUDGET 기본이 40,000 토큰인데 여기를 12,000 으로 박아 뒀었다(실측 3배 초과).
# 고정값으로 두면 도구 예산을 올릴 때마다 조용히 어긋나므로 거기서 유도한다.
DOC_RESERVE_TOKENS = int(os.environ.get("DOC_RESERVE_TOKENS", str(TOOL_SCHEMA_BUDGET + 16000)))
DOC_SAFETY = float(os.environ.get("DOC_SAFETY", "0.97"))                 # 변환 오차 안전 계수
_CTX_FALLBACK = int(os.environ.get("LLM_CONTEXT_TOKENS", "128000"))  # 물어보기 실패 시
# 위 값을 사람이 적었나 — 적었으면 그 값은 이 박스의 창이고(출처 env), 안 적었으면 코드 기본값이다(출처 fallback).
# 둘 다 같은 int 라 _model_context_tokens 만 보면 가를 수 없다(카드 대조의 ctx_source 가 이걸로 셋을 가른다).
_CTX_ENV_SET = bool((os.environ.get("LLM_CONTEXT_TOKENS") or "").strip())
_CTX_RETRY_S = 60.0     # 조회가 실패한 뒤 다시 묻기까지(초) — 그동안은 기본값으로 간다
# 받은 창을 다시 묻기까지(초) — 0 이면 처음 답을 굳힌다(종전). 다시 묻는 동안 호출자는 기다리지 않는다(뒤에서 묻고 지금 값을 준다).
# 다시 물은 답은 **줄일 때만** 받는다 — 넓히는 것은 재기동 때다(_refresh_context_bg). HWAXPortal docs/delib-ops R5.
_CTX_TTL_S = max(0.0, float(os.environ.get("LLM_CONTEXT_TTL_S", "600") or 0))
# 한 번 물을 때 /models 를 몇 번 묻나 — LLM 서버 여럿을 한 주소로 묶은 풀(nginx least_conn)은 묻는 때마다 다른 서버가 답한다. 가장 작은
# 창을 쓴다 — 큰 창으로 짠 예산이 작은 서버로 가면 400(maximum context length)이다(10-20 free-gather 2건). 바쁜 서버는 least_conn 이 비켜
# 가 몇 번을 물어도 작은 서버를 못 만날 수 있다 — 섞인 풀은 작은 서버의 창을 LLM_CONTEXT_TOKENS 에 적는다(상한으로도 쓴다 — _decide_context).
_CTX_SAMPLES = max(1, int(os.environ.get("LLM_CONTEXT_SAMPLES", "3") or 1))
_CTX_LOCK = threading.Lock()        # 뒤에서 다시 묻기는 한 번에 하나
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


_CJK_RUN = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _CJK_RANGES) + "]+")


def _est_tokens_all(text: str) -> int:
    """글 **전체**의 글자 구성으로 잰 토큰 수(환산은 _tokens_per_char 와 같다). _est_tokens 는 앞 20,000자만 표본으로 봐서,
    머리가 영문(로그·코드·JSON)이고 뒤가 한국어인 글을 세 배 넘게 적게 센다. 정규식으로 세어 수백만 자도 수십 ms 다."""
    if not text:
        return 0
    cjk = len(text) - len(_CJK_RUN.sub("", text))
    return max(1, int(cjk / 1.05 + (len(text) - cjk) / 3.6))


def _fit_tokens(build, cap: int, tokens: int, floor: int) -> tuple[str, int]:
    """build(cap) 이 만든 글이 글 전체로 잰 토큰 수(_est_tokens_all) tokens 안에 들 때까지 cap(자)을 줄인다(바닥 floor) —
    (글, cap). 글자 예산을 앞 20,000자 표본의 밀도로 환산한 자리(정리본·빈 응답 구제)가 실제로 싣는 글로 다시 잰다."""
    body = build(cap)
    for _ in range(8):
        t = _est_tokens_all(body)
        if t <= tokens or cap <= floor:
            break
        cap = max(floor, int(cap * tokens / t * 0.995))
        body = build(cap)
    return body, cap


def _chars_for_tokens(text: str, tokens: int) -> int:
    """이 글의 구성 그대로 `tokens` 토큰에 해당하는 글자 수.

    ⚠ 토큰 수를 정수로 거쳐 나눗셈하면 안 된다. 표본이 짧을 때 절사 오차가 그대로 증폭된다 —
    "가나다라"(4자·3.81토큰→3)로 재면 예산이 **27% 부풀어** 1M 창에서 1,066,412토큰짜리
    이력 예산이 나왔다(실측). 밀도(실수)로 바로 나눈다.
    """
    return max(500, int(tokens / max(1e-6, _tokens_per_char(text))))


def _ask_context() -> tuple[int | None, list]:
    """LLM 서버의 /models 를 _CTX_SAMPLES 번 물어 (가장 작은 창, 받은 창들). 답은 왔는데 창 칸이 없으면 (None, []). 첫 물음이 실패하면
    예외를 그대로 올린다(못 물었다). 뒤 물음이 실패하면 받은 것만 쓴다."""
    import httpx

    got: list = []
    for i in range(_CTX_SAMPLES):
        try:
            r = httpx.get(f"{VLLM_BASE_URL.rstrip('/')}/models",
                          headers={"Authorization": f"Bearer {VLLM_API_KEY}"}, timeout=5.0)
            data = [m for m in ((r.json() or {}).get("data") or []) if isinstance(m, dict)]
        except Exception:
            if i == 0:
                raise
            break
        # 이름이 맞는 것 우선, 서빙 모델이 하나뿐이면 그것. 여러 개인데 이름이 안 맞으면
        # 아무거나 집지 않는다 — 틀린 컨텍스트로 예산을 잡느니 보수적 기본값이 낫다.
        pick = next((m for m in data if m.get("id") == VLLM_MODEL), data[0] if len(data) == 1 else None)
        v = (pick or {}).get("max_model_len") or (pick or {}).get("context_length")
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            got.append(v)
    return (min(got) if got else None), got


def _decide_context(server_n: int | None) -> tuple[int, str]:
    """(창, 출처) — 서버 답과 LLM_CONTEXT_TOKENS(사람이 적었으면 **상한**)의 작은 쪽. 출처는 server · env(상한이 걸렸거나 서버가 창 칸
    없이 답했는데 사람이 적었다) · fallback(코드 기본값)."""
    if server_n is None:
        return _CTX_FALLBACK, ("env" if _CTX_ENV_SET else "fallback")
    if _CTX_ENV_SET and _CTX_FALLBACK < server_n:
        # 섞인 풀(창 300K · 1M)이면 서버 답이 큰 쪽일 수 있다 — 사람이 적은 값이 이긴다(HWAXPortal docs/delib-ops R5). 종전엔 이 값이 '물어보기
        # 실패 때' 값일 뿐이라 서버 답을 누를 수 없었고, 리스크 20만 A/B 절차(1M 서버에 200000)도 1M 으로 돌았을 것이다.
        return _CTX_FALLBACK, "env"
    return server_n, "server"


def _refresh_context_bg() -> None:
    """뒤에서 창을 다시 묻는다(_CTX_TTL_S — 부른 쪽이 _CTX_LOCK 을 쥐고 넘긴다). **줄일 때만** 받는다 — 넓히는 것은 재기동 때다.

    섞인 풀에서 바쁜 작은 서버를 least_conn 이 비켜 가면 다시 물은 답이 큰 쪽뿐일 수 있다 — 그 답으로 넓히면 작은 서버로 간 큰 호출이
    400 이다. 그리고 창이 오르내리면 카드 대조의 판(limits_rev)이 그때마다 바뀌어 리스크 앱의 며칠짜리 실행이 보류된다(P-06). 서버가 더 큰
    창을 답하면 넓히지 않고 /health 의 context.wider 에 적는다. 못 물으면 지금 값을 그대로 두고 _CTX_RETRY_S 뒤에 다시 묻는다."""
    try:
        old = _ctx_cache.get("n")
        try:
            server_n, got = _ask_context()
        except Exception as e:  # noqa: BLE001 — 지금 값으로 계속 간다
            print(f"[agent] 모델 컨텍스트 다시 묻기 실패({type(e).__name__}) — {old:,} 토큰 그대로({_CTX_RETRY_S:.0f}초 뒤 다시 묻는다)")
            _ctx_cache["at"] = time.monotonic() - max(0.0, _CTX_TTL_S - _CTX_RETRY_S)
            return
        _ctx_cache.update(server=server_n, samples=list(got), at=time.monotonic())
        if server_n is None:
            return          # 창 칸 없이 답했다 — 지금 값을 둔다
        n, src = _decide_context(server_n)
        if n < old:
            _ctx_cache.update(n=n, src=src)
            _ctx_cache.pop("wider", None)
            print(f"[agent] 문서 예산 기준 컨텍스트를 줄였다 — {old:,} → {n:,} 토큰(서버 답 {got} · 출처 {src})")
        elif n > old:
            if _ctx_cache.get("wider") != n:
                print(f"[agent] LLM 서버가 지금 창({old:,})보다 큰 창 {n:,} 을 답했다 — 넓히는 것은 재기동 때다(풀에 작은 서버가 섞였으면 "
                      "그 창을 LLM_CONTEXT_TOKENS 에 적는다)")
            _ctx_cache["wider"] = n
        else:
            _ctx_cache["src"] = src
            _ctx_cache.pop("wider", None)
    finally:
        _CTX_LOCK.release()


def _model_context_tokens() -> int:
    """이 모델이 받는 최대 토큰. OpenAI 호환 /v1/models 의 max_model_len 을 읽는다.

    박스마다 모델이 다르다(dev 16K · 운영 GLM). env 로 박아 두면 한쪽에서 반드시 틀리고,
    틀린 결과는 400 이라 사용자에게 '응답 생성 실패' 로만 보인다.
    처음 물을 때는 답을 기다린다. 받은 뒤로는 _CTX_TTL_S 마다 **뒤에서** 다시 묻고 그동안 지금 값을 준다 — 다시 물은 답은 줄일 때만
    받는다(_refresh_context_bg). 못 물어봤으면 굳히지 않고 _CTX_RETRY_S 뒤에 다시 묻는다. LLM_CONTEXT_TOKENS 를 사람이 적었으면 그 값이
    서버 답의 **상한**이다(_decide_context). HWAXPortal docs/delib-ops R5.

    외부 응답자 심의를 도는 태스크(문맥) 안에서는 그 응답자의 창(EXTERNAL_LLM_CTX_TOKENS)이다 — 심의 입구가 세운다
    (deliberation._external_entry). 그 밖(챗·카드 대조·다른 심의·/health)은 종전 그대로 이 서버 모델의 창이다."""
    _ov = _external_llm.ctx_override.get()
    if _ov:
        return _ov
    if "n" in _ctx_cache:
        n = _ctx_cache["n"]         # 다시 묻기를 띄운 호출도 지금 값을 준다
        # 물은 때(at)가 있는 값만 다시 묻는다 — 없으면 시험이 창을 박아 둔 것이다(그 값이 이 박스의 창이다).
        if (_CTX_TTL_S > 0 and "at" in _ctx_cache and time.monotonic() - _ctx_cache["at"] >= _CTX_TTL_S
                and _CTX_LOCK.acquire(blocking=False)):
            try:
                threading.Thread(target=_refresh_context_bg, name="ctx-refresh", daemon=True).start()
            except Exception:  # noqa: BLE001 — 스레드를 못 띄우면(자원 고갈) 다음 호출이 다시 해 본다
                _CTX_LOCK.release()
        return n
    n = _CTX_FALLBACK
    if time.monotonic() < _ctx_cache.get("retry_at", 0.0):
        return n        # 방금 못 물어봤다 — 죽은 엔드포인트를 호출마다 5초씩 기다리지 않는다
    try:
        server_n, got = _ask_context()
    except Exception as e:      # 못 물어보면 기본값으로 간다 — 기능을 막지는 않는다
        # ⚠ 이 값은 굳히지 않는다. 첫 호출자는 /health 이고 재기동 직후에는 LLM 이 아직 안 떠 있다 —
        #   그 한 번의 실패로 기본값이 프로세스 수명 내내 남아, 창이 더 작은 박스에서 예산이 전부 실제
        #   창 밖으로 잡혔다(dev 16K 를 128K 로 — 의장 프롬프트가 넘쳐 다 돈 심의의 결정문이 400 으로 죽는다).
        #   답은 왔는데 창 크기가 없는 것은 실패가 아니다(다시 물어도 같다) — 그건 굳힌다.
        print(f"[agent] 모델 컨텍스트 조회 실패({e}) — {n:,} 토큰으로 가정한다"
              f"({_CTX_RETRY_S:.0f}초 뒤 다시 묻는다)")
        _ctx_cache["retry_at"] = time.monotonic() + _CTX_RETRY_S
        return n
    n, src = _decide_context(server_n)
    _ctx_cache.update(n=n, src=src, server=server_n, samples=list(got), at=time.monotonic())
    print(f"[agent] 문서 예산 기준 컨텍스트 = {n:,} 토큰(서버 답 {got or '없음'} · 출처 {src}"
          + (f" · {_CTX_TTL_S:.0f}초마다 다시 묻고 줄일 때만 받는다" if _CTX_TTL_S > 0 else "") + ")")
    return n


def _context_health() -> dict:
    """/health 의 context — 창을 정한 근거(HWAXPortal docs/delib-ops R5). tokens 는 context_tokens 와 같은 값이다. server 는 마지막으로 물은
    답(받은 창들 중 가장 작은 것) · samples 는 그 답들 · env_cap 은 사람이 적은 상한 · wider 는 서버가 더 큰 창을 답했지만 넓히지 않은 값
    (재기동 때 넓힌다) · asked_s_ago 는 마지막으로 물은 뒤 지난 초. 아직 못 물었으면(LLM 서버가 안 닿는다) 가정한 창과 그 출처(env · fallback)를
    싣고 retrying 참 · retry_in_s(다음 물음까지) — 비워 두면 /health 를 보는 사람이 '창을 모른다' 로 읽는다(실제로는 가정한 값으로 돈다)."""
    c = _ctx_cache
    at = c.get("at")
    known = "n" in c
    out = {"tokens": c.get("n") if known else _CTX_FALLBACK,
           "source": c.get("src", "server") if known else ("env" if _CTX_ENV_SET else "fallback"),
           "retrying": not known, "server": c.get("server"), "samples": list(c.get("samples") or []),
           "env_cap": _CTX_FALLBACK if _CTX_ENV_SET else None, "wider": c.get("wider"),
           "reask_s": _CTX_TTL_S, "samples_per_ask": _CTX_SAMPLES,
           "asked_s_ago": round(time.monotonic() - at, 1) if at is not None else None}
    if not known and c.get("retry_at"):
        out["retry_in_s"] = round(max(0.0, c["retry_at"] - time.monotonic()), 1)
    return out


_chat_ctx_cache: dict = {}
_CHAT_CTX_TTL_S = 600.0     # 받은 답(창 · 모델 · 키 상태)을 다시 묻기까지(초) — 처음 답을 영원히 굳히면 키를 바꿔도 /health 가 옛 답이었다(A3)


def _chat_context_tokens() -> int | None:
    """챗 전용 LLM(CHAT_LLM_*)의 창 — 꺼져 있으면 None. 그 서버 /v1/models 의 max_model_len 을 **200 으로 답했을 때만** 받아
    _CHAT_CTX_TTL_S 동안 쓴다(_model_context_tokens 와 같은 규율 — 못 물어봤으면 _CTX_RETRY_S 뒤에 다시 묻고, 그동안은 앞서 받은 값 또는
    CHAT_LLM_CONTEXT_TOKENS · _CTX_FALLBACK). 그 서버가 설정한 모델을 내는지(served)와 마지막 조회 실패(probe_error)도 적어 /health 가 보인다.

    ⚠ 예전엔 상태 코드를 안 보고 401 본문까지 r.json() 으로 읽어 '모델 없음' 으로 영원히 굳혔다 — 키가 틀린 것이 모델 이름 문제로 보였고,
    키를 고쳐도 재기동 전까지 그대로였다(10-17 cae00 리뷰 A3)."""
    cfg = getattr(app.state, "chat_llm_cfg", None)
    if not cfg:
        return None
    now = time.monotonic()
    if "n" in _chat_ctx_cache and now < _chat_ctx_cache.get("fresh_until", 0.0):
        return _chat_ctx_cache["n"]
    fallback = _chat_ctx_cache.get("n", cfg["ctx_fallback"])
    if now < _chat_ctx_cache.get("retry_at", 0.0):
        return fallback
    try:
        import httpx

        r = httpx.get(f"{cfg['url']}/models", headers={"Authorization": f"Bearer {cfg['key']}"}, timeout=5.0)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = [m for m in ((r.json() or {}).get("data") or []) if isinstance(m, dict)]
    except Exception as e:      # 터널이 아직 안 열렸을 수 있다 · 키가 틀렸을 수 있다 — 굳히지 않고 앞 값(없으면 기본값)으로 간다
        print(f"[agent] 챗 LLM 컨텍스트 조회 실패({type(e).__name__}: {str(e)[:60]}) — {fallback:,} 토큰으로 간다"
              f"({_CTX_RETRY_S:.0f}초 뒤 다시 묻는다)")
        _chat_ctx_cache.update(retry_at=now + _CTX_RETRY_S, probe_error=f"{type(e).__name__}: {str(e)[:80]}")
        return fallback
    pick = next((m for m in data if m.get("id") == cfg["model"]), data[0] if len(data) == 1 else None)
    v = (pick or {}).get("max_model_len") or (pick or {}).get("context_length")
    n = v if isinstance(v, int) and v > 0 else cfg["ctx_fallback"]
    _chat_ctx_cache.update(n=n, src="server" if n == v else "fallback",
                           served=any(m.get("id") == cfg["model"] for m in data),
                           fresh_until=now + _CHAT_CTX_TTL_S, probe_error=None)
    print(f"[agent] 챗 LLM 창 = {n:,} 토큰({_chat_ctx_cache['src']})"
          + ("" if _chat_ctx_cache["served"] else f" — ⚠ 그 서버가 {cfg['model']} 를 내지 않는다(CHAT_LLM_MODEL)"))
    return n


def _model_context_state() -> dict:
    """창 크기와 **그 값이 어디서 왔나** — {"tokens", "source", "retrying"}. 값은 _model_context_tokens 그대로다.

    source 는 셋이다 — server(LLM 서버가 창을 알려 줬다) · env(서버가 창 칸 없이 답했거나 닿지 않았는데
    LLM_CONTEXT_TOKENS 를 사람이 적었다 · 서버 답이 그 값보다 커서 상한이 걸렸다 — R5) · fallback(코드 기본값). retrying 은 방금 묻지 못해 _CTX_RETRY_S 뒤에
    다시 묻는 중일 때 참이다. 창으로 예산을 짜는 쪽(카드 대조)이 '가정한 창' 위에서 몇 시간짜리 계획을 굳히지
    않게 가른다 — int 하나만으로는 '서버가 128,000 이라 했다' 와 '못 물어서 128,000 으로 친다' 가 같다."""
    n = _model_context_tokens()
    if "n" in _ctx_cache:
        # 굳힌 값과 출처는 함께 적는다. 출처 없이 굳은 값은 시험이 창을 박아 둔 것뿐이다(그 값이 이 박스의 창이다).
        return {"tokens": n, "source": _ctx_cache.get("src", "server"), "retrying": False}
    return {"tokens": n, "source": "env" if _CTX_ENV_SET else "fallback", "retrying": True}


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


def _doc_status(docs_block: str, n: int, hist_tokens: int) -> str:
    """붙인 문서를 읽는다는 상태줄 — 줄여 실었으면 그렇다고 말한다. 종전엔 가운데를 덜어낸 경우(⋯ 가운데)만 말하고,
    낱장 표지가 없어 **앞부분만** 실은 경우(fit_document 의 '앞 N자만 실림')는 화면에 아무 말이 없었다."""
    step = f"붙인 문서 {n}건을 읽습니다"
    # 문서 머리줄의 표식으로 가른다 — 규율 문구에도 '앞 N자만 실림' 이 글자 그대로 있어 낱말만 찾으면 늘 참이다.
    mid, head = "⋯ 가운데" in docs_block, bool(re.search(r"자 — 앞 [\d,]+자만 실림", docs_block))
    if mid or head:
        # 대화가 길어 자리가 없는 것인지, 문서 자체가 긴 것인지 구분해 준다 —
        # 앞의 경우엔 '새 대화에서 붙이면 더 읽는다' 가 답이라 조치가 다르다.
        step += (" — 대화가 길어 문서에 줄 자리가 적습니다(새 대화에서 붙이면 더 읽습니다)"
                 if hist_tokens > _model_context_tokens() * 0.3
                 else " — 길어서 가운데 일부는 건너뜁니다" if mid
                 else " — 길어서 앞부분만 실었습니다(낱장 표지가 없는 글)")
    return step


# 호출별 시작 시각 — 소요(ms)를 재려면 시작을 기억해야 한다. 짝 키(run_id)로 담는다.
# 완료에서 꺼내 쓰고 지운다.
#
# ⚠ **모듈 전역이라 동시 스트림이 공유한다.** 키가 LangGraph run_id(UUID)라 섞이지는
# 않지만, **짝이 안 오는 시작**(클라이언트 중단·예외·게이트 차단)은 영영 남는다.
# 서버가 몇 주 떠 있으면 그만큼 샌다 — 상한을 두고 오래된 것부터 버린다.
# (버려진 항목은 소요만 못 재고 기록 자체는 남는다 — `ms` 가 빠질 뿐이다.)
_T0_MAX = 512
_t0_by_call: "OrderedDict[str, float]" = OrderedDict()


_TEXT_FAIL_HEAD = re.compile(r"^(error|오류)\s*[:：]", re.I)


def _result_ok(txt) -> bool:
    """도구 결과가 성공인가. **머리만 본다.**

    ⚠ 예전엔 결과 **전체**(최대 20만 자)에서 ✖ 를 찾았다. 표식은 실패를 만들 때 맨 앞에
    찍히므로(867행) 뒤쪽에서 찾는 ✖ 는 전부 남의 것이다 — 합격/불합격 표를 내는
    `check_design_rules`·`qa_run`, ✔/✖ 를 쓰는 한국어 문서, VOC 행. 그것들이 걸리면
    **성공한 호출이 실패로** 기록됐다. 화면은 빨갛고, 절차 원장은 그 라벨을 오류 문구로
    남기고, 쓰기 도구면 "실제로 만들어졌을 수 있으니 확인하라" 까지 띄웠다 — 이미 잘 끝난
    쓰기를 확인하러 보냈다. `_tool_text_ok` 는 진작 `[:160]` 만 보고 있었다(둘이 어긋났다).

    반대 방향도 여기서 막는다. `deliberation._call` 은 예외를 `"(tool X error: …)"`
    **문자열로 삼켜** 돌려주므로 `except` 로는 실패를 못 잡는다 — 직접 실행 경로가
    실패를 `ok: True` 로 내보내고 있었다. 성패를 모르는 것이 아니라 **틀리게 아는** 것이라
    더 나쁘다.
    """
    if not isinstance(txt, str):
        return True          # 성패를 말해 주는 문자열이 아니다 — 실패로 몰지 않는다
    head = txt.lstrip()[:400]
    if (head.startswith(_TOOL_FAIL_MARK) or head.startswith("(tool ")
            or _PHANTOM_ID_MARK in head):
        return False
    # ⚠ **평문으로 실패를 알리는 도구**가 있다 — KooSlurm(smarttwin_*·slurm_*)은 실패를
    #   `"error: 제출 실패(status=500)"` 평문으로 주고 isError=false 다. 아래 봉투 검사는 JSON 만
    #   보므로 이 경우를 **성공으로** 적었다(2026-09-16). 포털 절차 판정기(judge._TEXT_FAIL)와 같은
    #   규칙 — **첫 줄만** 본다(본문 중간의 "error:" 는 로그 인용일 수 있다).
    first = next((ln.strip() for ln in head.splitlines() if ln.strip()), "")
    if _TEXT_FAIL_HEAD.match(first):
        return False
    # ⚠ **본문이 실패라고 말하는 것도 본다.** 표식만 보면 `isError=false` 로 오는 실패를
    # 전부 성공으로 적는다 — 실호출로 확인했다(`{"status":"error",…}`·`{"error":"…"}`).
    # 같은 리포의 심의 판정기는 그걸 잡고 있었다. 판정기가 갈라져 있던 것이지
    # 좁게 두기로 한 것이 아니다(2026-09-15 5차 감사).
    return not envelope_failed(txt)


def _t0_remember(cid: str) -> None:
    _t0_by_call[cid] = time.monotonic()
    while len(_t0_by_call) > _T0_MAX:
        _t0_by_call.popitem(last=False)   # 가장 오래된 것부터


def _inner_tool(name: str, inp) -> tuple[str, dict | str | None]:
    """`invoke_tool(name=…, arguments=…)` 는 **경유**다 — 기록에는 안쪽 도구가 남아야 한다.

    이 허브의 안내가 "목록에 없는 도구는 invoke_tool 로 부르라" 이므로 실제 챗 호출의
    상당수가 이 경유를 탄다. 그런데 기록에는 `invoke_tool` 만 남아서 —
      · 절차 도출이 `invoke_tool` 단계만 늘어놓고
      · 어느 앱인지 못 찾아 **전부 결손**이 되고
      · 같은 도구를 몇 번 불렀는지도 안 보인다.
    경유 사실은 `via` 로 남기고 `tool` 은 안쪽 이름으로 바꾼다.
    """
    if name != "invoke_tool" or not isinstance(inp, dict):
        return name, None
    inner = inp.get("name")
    if not isinstance(inner, str) or not inner:
        return name, None
    args = inp.get("arguments")
    if isinstance(args, dict):
        return inner, args
    # **인자가 없다** — 이 허브가 권하는 정상 모양이다(`invoke_tool(name="list_materials")`).
    # 여기서 바깥 dict 를 돌려주면 `name` 이 **그 도구의 인자인 양** 기록돼, 저장 검증이
    # "스키마에 없는 인자 — name" 으로 사람이 넣은 적 없는 것을 거절한다.
    if args is None or (isinstance(args, str) and not args.strip()):
        return inner, {}
    # 모델은 `arguments` 를 JSON **문자열**로 곧잘 보낸다. 풀리면 그것이 진짜 인자다.
    if isinstance(args, str):
        try:
            got = json.loads(args)
        except ValueError:
            got = None
        if isinstance(got, dict):
            return inner, got
    # 인자가 **있긴 한데 우리가 못 읽는** 모양이다(깨진 JSON·배열·스칼라).
    # `{}` 로 뭉개면 '인자 없음' 과 같아지고, 바깥 dict 를 돌려주면 `name` 을 지어낸다.
    # 원문을 그대로 돌려준다 — 기록에는 남고, 초안은 이것을 `args_not_structured`
    # 결손으로 잡는다(`derive.draft`). 모르는 것을 아는 척하지 않는다.
    return inner, args if isinstance(args, str) else json.dumps(args, ensure_ascii=False,
                                                                default=str)


def _tool_preview(v, n: int = 220) -> str:
    """도구 입출력 요약 — 안전 문자열화 + 공백 압축 + **표식 붙인** 절단.

    ⚠ 표식 없이 자르면 **다른 호출이 같은 서명이 된다.** n 자 뒤에서만 갈리는 두 호출은
    잘린 문자열이 똑같아져, 활동 목록에서 뒤엣것이 조용히 사라진다. 심의 쪽은 이미
    `…#sha1[:6]` 로 이 문제를 풀어 뒀다(deliberation.py:2356) — 여기도 같게 맞춘다.
    """
    try:
        if v is None:
            return ""
        content = getattr(v, "content", None)   # ToolMessage 등 랩퍼 언랩
        if content is not None:
            v = content
        s = json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else str(v)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) <= n:
            return s
        return s[:n] + "…#" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:6]
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
_PERSONA_HELPER_SET = bool(os.environ.get("PERSONA_HELPER_ROLE_MAX"))


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
    # note 는 '역할이 비었다' 의 **사유**다. 비어 있으면 '역할 문서가 원래 없다', 차 있으면
    # '못 물어봤다' — 그 둘은 다른 사실이고, 호출부가 사용자에게 갈라서 말해야 한다
    # (지식카드 경로가 이미 _persona_knowledge.last_note 로 같은 일을 한다).
    # key·name 도 담는다. 도구 점수(_seat_tool_rank)가 역할뿐 아니라 **키와 이름**의 낱말을
    # 쓰는데, 챗은 이 둘을 안 넘겨 심의와 다른 순위가 나오고 있었다 — 같은 기준을 쓴다는
    # 주장이 실제로는 절반만 참이었다(실측: pcb_warpage_surrogate 가 챗에서만 빠졌다).
    meta: dict = {"key": agent_type, "name": "", "role": "",
                  "operator": False, "apps": [], "key_tools": [], "note": ""}
    try:
        tools = await _tools_by_name(app, groups, CATALOG_RESULT_MAX)  # 역할 원문을 JSON 으로 읽는다
        # ⚠ 예외만으로는 '못 물어봤다' 를 못 잡는다. 게이트웨이 미연결이면 _tools_by_name 이
        #   예외가 아니라 **빈 dict** 를 주고, 도구 실패는 _call 이 "(tool … error: …)" 문자열로
        #   삼킨다. 그래서 세 갈래 중 둘이 '역할 문서가 비어 있다' 로 나가고 있었다 — 갈랐다고
        #   선언해 놓고 실제로는 합쳐진 채였다.
        if "get_agent_session" not in tools:
            meta["note"] = "게이트웨이에 연결하지 못했습니다"
            return meta
        raw = await _call(tools, "get_agent_session", {"agent_type": agent_type})
        if isinstance(raw, str) and raw.lstrip().startswith("(tool "):
            meta["note"] = raw.strip()[:160]
            print(f"[agent] persona load failed for {agent_type}: {meta['note']}")
            return meta
        sess = _first_dict(_parse_json(raw))
        sd = _first_dict(sess.get("data", sess))
        # ⚠ _role_doc 으로 허브 사용 안내를 떼고 싣는다. 원문을 그대로 주면 실측 69%가
        #   다른 클라이언트용 안내문이고, 그 안내가 포털 챗에서는 틀린 지시다.
        meta["name"] = str(sd.get("name") or "")[:120]
        # 좁은 창만 PERSONA_ROLE_MAX(정본 동기화 상한과 같은 값)에서 끊고 원문 길이를 적는다 — 넓은 창은 역할 전문.
        meta["role"] = _cut_mark(_role_doc(str(sd.get("system_prompt") or ""))
                                 or str(sd.get("description") or ""), _narrow_cap(PERSONA_ROLE_MAX))
        rc = sd.get("response_config") if isinstance(sd.get("response_config"), dict) else {}
        if rc.get("persona_kind") == "mcp_operator":
            meta["operator"] = True
            meta["apps"] = [a.strip()[:80] for a in (rc.get("mcp_apps") or []) if isinstance(a, str) and a.strip()][:3]
            meta["key_tools"] = [n.strip()[:80] for n in (rc.get("key_tools") or []) if isinstance(n, str) and n.strip()][:12]
    except Exception as exc:  # noqa: BLE001 — 실패 시 페르소나 없이 일반 챗(단, 조용히는 아니다)
        meta["note"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        print(f"[agent] persona load failed for {agent_type}: {exc!r}")
    # 조회가 **성공했으면** 캐시한다(역할이 비어 있어도). 종전 조건은 role 이라, 역할 문서가
    # 원래 빈 페르소나는 발화마다 게이트웨이를 다시 쳤다 — _tools_by_name 은 MCP get_tools +
    # 465종 _prep_tool 이라 싼 호출이 아니다. 실패(note)는 여전히 캐시하지 않는다(다음 턴 재시도).
    if not meta["note"]:
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


# 지식카드 한 줄 포맷은 deliberation 의 것을 그대로 쓴다 — 챗·심의가 따로 만들면
# 한쪽만 필드를 빠뜨린다(실제로 record_id·causal_status 를 둘 다 빠뜨리고 있었다).
_knowledge_line = knowledge_line


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


# 챗 첫 조각 지연(M3 E3-9 · D-34) — 한 턴의 첫 on_chat_model_start 에서 첫 on_chat_model_stream 조각까지(스트리밍을 끈 박스는
# on_chat_model_end 까지)를 잰다. LLM 줄 대기와 미리 채우기(prefill)가 드러나는 값이고 도구 왕복은 들지 않는다. 턴을 시작할 때의
# 카드 대조 수(review_active)로 idle·review 고리에 넣는다. 계측은 챗을 건드리지 않는다 — 실패는 삼킨다.
_CHAT_LOAD = _review_gate.ChatLoad()


def _review_active_now() -> int:
    if _card_review is None:
        return 0
    return int(_card_review.gate_of(app).active)


async def _timed_events(events):
    """astream_events 를 그대로 흘리며 첫 조각 지연을 한 번 잰다(_CHAT_LOAD). 지금 도는 챗 턴 수(active)도 센다."""
    _CHAT_LOAD.active += 1
    t0, review, done = None, 0, False
    try:
        async for ev in events:
            if not done:
                try:
                    k = ev.get("event") if isinstance(ev, dict) else None
                    if k == "on_chat_model_start" and t0 is None:
                        t0, review = time.monotonic(), _review_active_now()
                    elif t0 is not None and (k == "on_chat_model_stream" or (
                            k == "on_chat_model_end" and getattr(app.state, "llm_nostream", False))):
                        _CHAT_LOAD.record((time.monotonic() - t0) * 1000, review)
                        done = True
                except Exception as exc:  # noqa: BLE001 — 계측 실패가 챗을 죽이지 않는다
                    done = True
                    print(f"[load] 챗 첫 조각 계측 실패(무시) — {type(exc).__name__}: {exc}"[:200])
            yield ev
    finally:
        _CHAT_LOAD.active = max(0, _CHAT_LOAD.active - 1)
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()


# ── 챗 자기검증(HWAXPortal docs/chat-self-check) ─────────────────────────────────────────────────────────────
# 초안을 근거와 대조해 한 번 고친 뒤 본문으로 낸다. 사용자 결정(10-23, SC-D1) — 검증한 답만 본문에(초안은 '생각 중' 칸) · 같은 챗 모델 ·
# 항상(근거도 수치도 없는 짧은 답만 건너뜀). 끄면(CHAT_SELF_CHECK=0) 종전 그대로다.
_SELF_CHECK_SHORT = 400          # 이보다 짧고 근거(조회 · 붙인 문서 · 그림)도 수치도 없는 답은 건너뛴다(인사 · 되묻기)
_SELF_CHECK_MIN_RATIO = 0.4      # 고친 답이 초안의 이 비율보다 짧으면 의심한다 — 검토자가 답을 잘라 먹었다
_SELF_CHECK_NUMS = 30            # 검토자에게 넘기는 '출처 없는 수치' 의 위쪽
# 검토자 출력 상한(토큰, HWAXPortal docs/request-1029 W-9 · W-4). 검토자는 고친 답 전체가 아니라 바꿀 곳만 쓴다(W-4) — 상한이 없던 때는 검토 글이
# 서버 상한(131,072 토큰)까지 늘었다(EX-03 — 한 호출 399초). 걸리면 초안을 낸다(_self_check_judge 의 cut). 본답에는 상한을 두지 않는다(결정 D-6).
_SC_OUT_CAP = 4096
_DRAFT_HEAD = "\n\n— 초안(검증 전) —\n"
_CHECK_HEAD = "\n\n— 검증 —\n"
_SELF_CHECK_SYS = ("당신은 답변 검토자입니다. 어시스턴트가 쓴 답 초안을 근거와 대조해 틀린 곳만 고칩니다. 근거는 아래 [사용자 질문] · "
                   "[이번 턴 조회 결과] · [붙인 문서] · [앞 대화] 뿐입니다 — 근거에 없는 사실을 새로 지어 넣지 않습니다. 한국어로 씁니다.")
# ② 줄 — 도구 · 문서가 있는 턴에만 실린다(아래 _SELF_CHECK_ASK_NO_EVIDENCE 가 통째로 바꿔 끼운다 — 그래서 ASK 는 이 줄을 그대로 잇는다).
# 10-30 재시험에서 검토자가 사본에서 잘린 수를 '근거 없음' 으로 읽고 맞는 초안 값을 지웠고(OT-09 · DW-05), 보이는 항목을 세어 total 을
# 고쳤다(OT-04) — 조회 결과의 값은 지우지 말고, 잘린 결과에서 온 값은 짚기만 하라고 밝힌다(HWAXPortal docs/request-1030 HA-27 ③).
# _SC_CUT_WORD 는 잘린 결과의 머리(_sc_result_head)와 이 줄이 함께 쓰는 낱말이다 — 한쪽만 바꾸면 지시가 가리키는 머리가 없어진다.
_SC_CUT_WORD = "(잘림"
_SC_ASK_EVIDENCE_LINE = (
    "② 수치 · 사실이 근거에 있나 — 근거에 없는 수치는 지우거나 '(근거 없음 — 추정)' 으로 표시하라. [코드 대조] 목록을 먼저 보라. 조회 결과에 "
    f"나오는 수치 · 값은 지우지도 '근거 없음' 으로 바꾸지도 마라. 머리에 '{_SC_CUT_WORD}' 이 붙은 결과는 뒷부분을 보지 못한 것이다 — 그 결과에서 온 "
    "수치 · 개수 · 항목의 값은 틀려 보여도 [바꿀 곳] 에 쓰지 말고 [짚은 점] 에만 적어라. 개수 · 합계는 total · count 같은 칸으로 확인하고 "
    "보이는 항목을 세어 고치지 마라. [새 글] 에 새로 쓰는 수치는 조회 결과에 있는 값이나 그 값들의 합 · 차 · 비율만 쓴다.\n")
_SELF_CHECK_ASK = (
    "다음을 검토하라.\n"
    "① 질문의 모든 부분에 답했나 — 빠진 부분을 근거로 답할 수 있으면 채우고, 없으면 '근거가 없어 답하지 못했다' 고 밝혀라.\n"
    + _SC_ASK_EVIDENCE_LINE +
    "③ 근거와 어긋나는 말 — 근거대로 고쳐라. 어느 항목의 값(부서 · 담당 · 상태 · 이름)을 바꿀 때는 같은 결과의 그 항목 행에서 확인한 것만 "
    "고치고, 아니면 [짚은 점] 에 적어라.\n"
    "④ 계산 · 단위 — 다시 계산해 틀리면 고쳐라.\n"
    "⑤ 조회해 놓고 빠뜨린 중요한 결과 — 답에 더하라.\n"
    "고칠 곳만 고치고 나머지는 글자 그대로 둔다. 말투 · 형식(표 · 목록 · 그림 링크 ![…](…) · 출처 표기)을 바꾸지 마라. 근거가 잘려 있으면"
    "(…[N자 중 앞 M자]) 그 뒤에 있을 수 있는 내용은 지우지 말고 그대로 둔다.\n\n"
    "아래 꼴로만 답하라. 고친 답 전체를 다시 쓰지 마라 — 바꿀 곳만 적으면 초안에 넣어 준다.\n"
    "[판정] 고칠 것 없음  또는  [판정] 고침\n"
    "[고친 점]\n- [바꿀 곳] 의 쌍마다 한 줄, 같은 차례로(무엇을 왜 — 근거). 고칠 것이 없으면 '- 없음'\n"
    "[짚은 점](고치지 않고 사람이 확인할 곳이 있을 때만 쓴다. 고칠 것 없이 짚기만 하면 [판정] 고칠 것 없음 과 이 칸만 쓴다)\n"
    "- 한 줄에 하나(무엇이 왜 의심스러운가 — 근거)\n"
    "[바꿀 곳](고칠 것이 없으면 이 칸을 쓰지 않는다. 바꿀 곳마다 [원문] · [새 글] 한 쌍을 쓴다)\n"
    "[원문]\n초안의 글을 글자 그대로(초안에 한 번만 나오도록 앞뒤 낱말을 조금 넣는다)\n"
    "[새 글]\n그 글을 고친 것(지우려면 비워 둔다. 더하려면 원문을 그대로 쓰고 그 뒤에 이어 쓴다)")
# 조회 결과도 붙인 문서도 없는 턴 — ② 를 '일반 지식 수치는 표시하지 않는다' 로 바꾼다. 위 ② 를 그대로 주면 '도구 없는 턴' 머리(① ③ ④ 만
# 본다)와 어긋나, 검토자가 일반 지식 수치마다 '(근거 없음 — 추정)' 을 달고 '고침' 으로 판정했다(HWAXPortal docs/request-1027 HA-12).
_SELF_CHECK_ASK_NO_EVIDENCE = _SELF_CHECK_ASK.replace(
    _SC_ASK_EVIDENCE_LINE,
    "② 이번 턴에는 대조할 조회 결과 · 붙인 문서가 없다 — 일반 지식으로 쓴 수치 · 사실에 표시를 달지 말고 지우지도 마라"
    "(앞 대화와 어긋나는 것만 ③ 으로 고친다).\n")
# 관문이 켜져 있고 도구를 부른 턴에만 맨 끝에 — 꺼져 있으면 사실이 아니라 싣지 않는다(HA-27). num 은 원문 값 줄을 끈 꼴이라 그 말을 뺀다.
_SC_ASK_GATE_LINE = ("서버가 [바꿀 곳] 의 쌍을 조회 결과 원문(자르기 전)과 다시 대조한다 — 원문에 있는 수치를 지우거나 '근거 없음' 으로 바꾸는 "
                     "쌍은 버리고, 원문에서 확인되지 않는 새 수치나 원문 값을 바꾸는 쌍은 넣지 않고 짚은 곳으로 돌린다.")
_SC_ASK_GATE_LINE_NUM = _SC_ASK_GATE_LINE.replace("새 수치나 원문 값을 바꾸는 쌍은", "새 수치를 쓰는 쌍은")
_SC_VERDICT = re.compile(r"\[판정\]\s*([^\n]*)")
# 절 머리 앞의 꾸밈 — 검토자가 '**[짚은 점]**' · '## [짚은 점]' 처럼 굵게 · 머리글로 쓰면 종전 꼴로는 마지막 [새 글] 이 그 머리에서 끊기지 않아
# 짚은 점 칸 전체가 답 본문에 들어갔다(반대 검토 2차). 줄머리의 공백 · * · _ · # · > 를 건너뛴다.
_SC_HEAD_PAD = r"\n[ \t*_#>]*"
_SC_FIXES = re.compile(r"\[고친 점\][^\n]*\n(.*?)(?=" + _SC_HEAD_PAD + r"\[(?:바꿀 곳|짚은\s*점)\]|\Z)", re.S)
# [짚은 점] — 고치지 않고 사람이 볼 곳(HA-27). [바꿀 곳] 앞이든 뒤든 읽고, 쌍의 [새 글] 은 이 머리 앞에서 끊는다(새 글에 섞이지 않게).
_SC_FLAGS = re.compile(r"\[짚은\s*점\][^\n]*\n(.*?)(?=" + _SC_HEAD_PAD + r"\[(?:바꿀 곳|고친 점|판정|원문)\]|\Z)", re.S)
_SC_NEW_END = re.compile(_SC_HEAD_PAD + r"\[(?:짚은\s*점|고친 점|판정)\]")
# [짚은 점] 의 '없음' 줄 — 낱말만 바꾼 꼴('해당 없음' · '없습니다.' · '짚을 곳 없음' · '(없음)' · '없음 — 근거와 모두 일치')도 짚은 곳이 아니다. 종전엔
# '없음' · '없음.' 둘만 버려 맞는 답이 flagged 가 되고 /health 의 flagged · reviewer_flags 가 부풀었다(반대 검토 2차). '없음 — 다만 …' 처럼 뒤에
# 다른 말이 붙으면 짚은 곳이다.
_SC_NONE_LINE = re.compile(
    r"[(\[]?\s*(?:해당\s*(?:사항\s*)?|짚을\s*(?:곳|점)(?:이|은)?\s*|특이\s*사항\s*|문제(?:점)?\s*|의심(?:스러운)?\s*(?:곳|점)?(?:은|이)?\s*)?"
    r"(?:없음|없습니다|없다|없어요|none|n/?a)\s*[)\]]?\s*[.。!]*"
    r"(?:\s*[—–\-:]\s*(?:근거와\s*)?(?:모두|전부|다)\s*(?:일치|맞(?:음|다|습니다)|확인(?:됨|했다|했습니다))\s*(?:함|한다|합니다)?\s*[.。!]*)?", re.I)
_SC_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _self_check_on() -> bool:
    return os.environ.get("CHAT_SELF_CHECK", "1") != "0"


# ── 자기검증 관문(HWAXPortal docs/request-1030 HA-27) — 검토자의 바꿀 곳을 넣기 전에 이번 턴 도구 결과 원문(접기 · 자르기 전)과 다시 대조해
# 넣음 · 짚음 · 버림을 정한다(sc_gate). 원문은 _cap_tool.capped 의 R5 가 턴 그릇에 붙잡는다. 손잡이 둘은 CHAT_SELF_CHECK 처럼 턴마다 프로세스 환경을
# 읽지만 .env 는 start.sh 가 기동 때 한 번 싣는다 — 박스에서 바꾸면 재기동해야 먹는다 — CHAT_SELF_CHECK_GATE 1(수 + 원문 값) · num(수만 — OT-08 줄 끔) · 0(끔 — 원문을 붙잡지 않고 바꿀 곳을 종전대로 다 넣는다),
# CHAT_SELF_CHECK_RAW_CHARS 턴마다 붙잡는 원문 합(자, 0 이면 붙잡지 않고 모델 사본으로만 대조).
_SC_RAW_ITEM = 2_000_000          # 결과 하나에서 붙잡는 원문(자) — CATALOG_RESULT_MAX 와 같은 크기
_SC_EXTRA_CHARS = 1_000_000       # 덧붙인 근거(사람 발화 · 붙인 문서 · 이력)의 합(자)
_SC_NOTE_MAX, _SC_NOTE_CHARS = 20, 300      # meta 의 flags · dropped 줄 수 · 줄 길이
# 이번 챗 턴의 원문 그릇 {texts, chars, partial} — _turn_ids 와 같은 꼴(턴이 그릇을 걸고 도구 코루틴이 그 그릇에 넣는다). None 이면 붙잡지 않는다
# (자기검증 끔 · 관문 끔 · 심의 · 띵킹 · 루틴 · 카탈로그 — 이 함수를 지나지 않는 길).
_turn_sc_raw: contextvars.ContextVar = contextvars.ContextVar("turn_sc_raw", default=None)


def _sc_gate_mode() -> str:
    v = (os.environ.get("CHAT_SELF_CHECK_GATE", "1") or "1").strip().lower()
    return "0" if v == "0" else "num" if v == "num" else "1"


def _sc_raw_cap() -> int:
    try:
        return max(0, int(os.environ.get("CHAT_SELF_CHECK_RAW_CHARS", "6000000") or 6_000_000))
    except ValueError:
        return 6_000_000


def _sc_raw_box() -> dict | None:
    return {"texts": [], "chars": 0, "partial": False} if _sc_gate_mode() != "0" and _sc_raw_cap() > 0 else None


async def _sc_raw_off(coro):
    """카탈로그 조회(지정 전문가의 역할 원문 get_agent_session · 지식카드 검색 agent_search)는 원문 그릇에 넣지 않고 부른다 — 설계 4.2 의 '카탈로그
    조회는 그릇이 없다'. 그 길의 도구도 capped(R5)를 지나는데 R5 는 문맥 변수만 보아, 챗 턴 안에서는 결과 하나 최대 200만 자의 검색 원문이 그릇
    600만 자 몫을 먹고(뒤 결과가 partial) 강한 수를 늘렸다(반대 검토 2차). 모델이 실제로 본 발췌는 턴이 따로 붙잡는다(_sc_keep_raw)."""
    tok = _turn_sc_raw.set(None)
    try:
        return await coro
    finally:
        _turn_sc_raw.reset(tok)


def _sc_keep_raw(raw) -> None:
    """도구 결과 원문을 이번 턴 그릇에 — 턴 합 _sc_raw_cap() · 결과 하나 _SC_RAW_ITEM 자까지(넘으면 앞부분 + partial). 병렬 도구 코루틴은 같은
    이벤트 루프에서 await 없이 더하므로 자물쇠가 필요 없다. 자르지 않는 한 이미 메모리에 있던 글의 참조다."""
    box = _turn_sc_raw.get()
    if box is None:
        return
    s = raw if isinstance(raw, str) else str(raw)          # _cap 과 같은 문자열화
    room = _sc_raw_cap() - box["chars"]
    if room <= 0:
        box["partial"] = True
        return
    take = min(room, _SC_RAW_ITEM)
    if len(s) > take:
        s, box["partial"] = s[:take], True
    box["texts"].append(s)
    box["chars"] += len(s)


# 모델 사본에 코드가 붙인 절단 표지 — 그 수(자 수)는 근거가 아니다. 색인 전에 뗀다.
_SC_CAP_LINE = re.compile(r"\n" + re.escape(_CAP_MARK) + r"[^\n]*")
_SC_CUT_TAIL = re.compile(r" …\[[0-9,]+자 중 앞 [0-9,]+자\]$")


# 이력 근거에서 실패한 호출의 머리 — 포털이 붙인다(HWAXPortal frontend/src/components/chat/history.ts turnEvidence 의 '[호출 실패] ').
_HIST_FAIL_HEAD = "[호출 실패]"


def _sc_result_ok(t) -> bool:
    """원문 색인에 넣을 결과인가 — 성공한 결과(_result_ok)이거나, 잡 상태 조회가 '그 잡은 failed' 를 돌려준 결과(status_wait.job_record — 잡 id 와
    상태 낱말이 한 사전에, ok:false 아님)다. _result_ok 는 status failed · error 칸을 실패 봉투로 읽어, 잡이 실패했다는 성공한 조회(StepForge
    job_status · DocForge docforge_status 의 끝 상태)를 통째로 뺐다 — 초안의 실패 설명 수치(진행 45 · RSS 29.7GB)를 지우는 쌍이 넣음이 됐다
    (반대 검토 2차). 거르는 것은 호출 실패다(✖ · 유령 ID · {ok:false} · 첫 줄 error: · 막은 글)."""
    if _result_ok(t):
        return True
    s = t.lstrip() if isinstance(t, str) else ""
    return bool(s) and not s.startswith(_TOOL_FAIL_MARK) and _status_wait_mod.job_record(s)


def _sc_index(req: ChatRequest, outputs: list, raw: dict | None):
    """원문 색인(sc_gate.RawIndex) — 모델 사본(그릇이 빈 턴 · 상한을 넘긴 결과 · 사본에만 있는 코드 글) + 그릇의 원문 + 덧붙인 근거(사람 발화 ·
    붙인 문서 · 이력의 사람 발화 · 이력의 도구 결과, 합 _SC_EXTRA_CHARS). 사본을 원문보다 앞에 둔다 — 색인이 상한에 차면 뒤 글을 건너뛰는데,
    모델이 본 글이 먼저 들어야 한다(반대 검토 2차). 실패한 결과(_sc_result_ok 가 거짓 — ✖ 거절 · 실패 봉투 · 유령 ID)는 근거가 아니라 뺀다
    (design-integration C-13). 어시스턴트 발화는 근거 블록이 있어도 넣지 않는다 — 그 글에는 지어낸 수가 들 수 있고 근거 블록의 ⚠ 줄이 출처 없는
    수를 다시 적어, 다음 턴에 그 수가 '원문에 있다' 가 되는 세탁 길이었다(반대 검토 2차 — 설계 6.2 를 뒤집는다). 앞 턴의 도구 결과는 이력의
    evidence[].result 로 따로 온다 — 실패한 호출('[호출 실패]' 머리)은 뺀다."""
    texts: list[str] = []
    for o in outputs:
        o = o if isinstance(o, str) else str(o)
        body = _sc_gate.strip_heads(o)            # 앱 머리 뒤의 실패 봉투도 본다(머리가 있으면 _result_ok 가 첫 줄로 머리를 읽는다)
        if not (_sc_result_ok(body) and (body is o or _sc_result_ok(o))):
            continue
        if _CAP_MARK in o:
            o = _SC_CAP_LINE.sub("", o)
        texts.append(_SC_CUT_TAIL.sub("", o) if o.endswith("자]") else o)
    texts += [t for t in (raw or {}).get("texts") or [] if _sc_result_ok(t)]
    extra: list[str] = []
    room = _SC_EXTRA_CHARS

    def add(t) -> None:
        nonlocal room
        t = str(t or "")
        if t and room > 0:
            extra.append(t[:room])
            room -= min(len(t), room)
    add(req.message)
    for d in req.documents or []:
        if isinstance(d, dict):
            add(d.get("text"))
    for h in reversed(req.history or []):               # 최근 것부터 — 몫이 모자라면 오래된 것이 빠진다
        if not isinstance(h, dict):
            continue
        if h.get("role") == "user":
            add(h.get("content"))
        for e in (h.get("evidence") if isinstance(h.get("evidence"), list) else []):
            if isinstance(e, dict) and not str(e.get("result") or "").startswith(_HIST_FAIL_HEAD):
                add(e.get("result"))
    return _sc_gate.RawIndex.build(texts, extra, partial=bool((raw or {}).get("partial")))


def _sc_prepare(req: ChatRequest, draft: str, calls: list, outputs: list, raw: dict | None, mode: str):
    """검토자 입력과 원문 색인 — 스레드 한 번(관문 계산이 이벤트 루프를 막지 않게). 관문은 켜져 있고 도구를 부른 턴에만 선다(Q11)."""
    idx = _sc_index(req, outputs, raw) if calls and mode != "0" else None
    return _self_check_messages(req, draft, calls, outputs, idx), idx


# 자기검증 런타임 — /health 의 self_check(HWAXPortal docs/request-1027 HA-13). 운영자가 검토자의 실패 · 지연을 알 길이 없었다.
# 상태별 수(ok · fixed · suspect · unreadable · failed · skipped) · 검토에 걸린 ms(건너뛴 것 빼고 최근 200건의 p50 · p95) · 마지막 실패.
# /health 는 스레드 풀에서, 기록은 이벤트 루프에서 돈다 — 자물쇠로 묶는다. 사유는 고정 문구다(사용자 글을 싣지 않는다).
_SC_LOCK = threading.Lock()
_SC_STAT: dict = {"counts": Counter(), "ms": [], "last_error": None}
_SC_MS_KEEP = 200


# 관문 계수(HA-27) — 관문이 돈 턴 · 그 가운데 검토자가 '고침' 이라 한 턴(관문 앞의 버릇 — 지시 ③ 의 효과) · 원문이 상한에 걸린 턴 · 쌍 단위의
# 코드별 수. 턴 상태(counts)는 관문 뒤의 결과로 센다 — 그래야 fixed_ratio 가 '답이 실제로 바뀐 턴' 이다.
_SC_GATE_KEYS = ("applied", *_sc_gate.CODES, "reviewer_flags")


def _sc_record(check: dict) -> None:
    st = str(check.get("status") or "?")
    with _SC_LOCK:
        _SC_STAT["counts"][st] += 1
        if st != "skipped" and isinstance(check.get("ms"), int):
            _SC_STAT["ms"] = (_SC_STAT["ms"] + [check["ms"]])[-_SC_MS_KEEP:]
        if st in ("failed", "unreadable", "suspect"):
            _SC_STAT["last_error"] = {"status": st, "reason": str(check.get("reason") or "")[:200], "at": int(time.time())}
        g = check.get("gate")
        if isinstance(g, dict):
            # 시험이 _SC_STAT 를 gate 칸 없이 바꿔 끼운다(HA-13) — 없으면 만든다
            agg = _SC_STAT.setdefault("gate", {"turns": 0, "reviewer_fixed": 0, "raw_partial": 0, "counts": Counter()})
            agg["turns"] += 1
            agg["reviewer_fixed"] += int(bool(g.get("reviewer_fixed")))
            agg["raw_partial"] += int(bool(g.get("raw_partial")))
            for k in _SC_GATE_KEYS:
                agg["counts"][k] += int(g.get(k) or 0)


def _sc_health() -> dict:
    with _SC_LOCK:
        counts, ms, last = dict(_SC_STAT["counts"]), sorted(_SC_STAT["ms"]), _SC_STAT["last_error"]
        g = _SC_STAT.get("gate") or {}
        gate = {"mode": _sc_gate_mode(), "turns": int(g.get("turns", 0)), "reviewer_fixed": int(g.get("reviewer_fixed", 0)),
                "raw_partial": int(g.get("raw_partial", 0)),
                "counts": {k: int((g.get("counts") or {}).get(k, 0)) for k in _SC_GATE_KEYS}}

    def pct(q: float):
        return ms[min(len(ms) - 1, int(round(q * (len(ms) - 1))))] if ms else None
    # fixed 비율 — 검증한 턴(건너뛴 것 빼고) 가운데 답이 실제로 바뀐 턴. 셀 턴이 없으면 None(HA-27 — 재시험이 이 값이 내려가는지 본다)
    den = sum(counts.values()) - counts.get("skipped", 0)
    return {"on": _self_check_on(), "counts": counts, "ms": {"n": len(ms), "p50": pct(0.5), "p95": pct(0.95)}, "last_error": last,
            "fixed_ratio": round(counts.get("fixed", 0) / den, 3) if den > 0 else None, "gate": gate}


def _sse_parse(frame) -> tuple[str, dict]:
    """우리가 만든 SSE 프레임 하나(_sse) → (사건, 자료). 꼴이 다르면 ('', {}) — 감싸개는 그런 프레임을 그대로 흘린다."""
    try:
        txt = frame.decode("utf-8") if isinstance(frame, (bytes, bytearray)) else str(frame)
        head, _, rest = txt.partition("\n")
        if not head.startswith("event: ") or not rest.startswith("data: ") or not txt.endswith("\n\n") or txt.count("\nevent: "):
            return "", {}
        data = json.loads(rest[6:].strip())
        return (head[7:].strip(), data) if isinstance(data, dict) else ("", {})
    except (UnicodeDecodeError, ValueError):
        return "", {}


# 답에 섞인 다른 문자(한자 · 가나) — Qwen · GLM 이 한국어 답에 중국어 · 일본어 낱말을 하나씩 흘렸다(OT-10 · EX-01 · EX-03 — HWAXPortal
# docs/request-1029 R-9). 기계로 갈아 끼우지 않는다(뜻이 틀어질 수 있다) — 자기검증에 짚어 넘기고 검토자가 고친다.
_FOREIGN_SCRIPT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]+")
_FOREIGN_MAX = 20


def _foreign_words(draft: str, question: str, *sources: str) -> list[str]:
    """답의 한자 · 가나 낱말 가운데 질문 · 근거에 없는 것. 질문이 그 문자로 쓰였으면 짚지 않는다(그 언어로 물었다)."""
    if not draft or _FOREIGN_SCRIPT.search(question or ""):
        return []
    hay = "\n".join(x for x in sources if x)
    return [w for w in dict.fromkeys(_FOREIGN_SCRIPT.findall(draft)) if w not in hay][:_FOREIGN_MAX]


def _self_check_skip(req: ChatRequest, draft: str, calls: list) -> bool:
    """검증을 건너뛰나 — 이번 턴에 근거(조회 · 붙인 문서 · 올린 그림)도 수치도 없는 짧은 답(인사 · 되묻기)만. 다른 문자 낱말이 섞였으면
    짧아도 돈다(R-9)."""
    if calls or req.documents or req.images:
        return False
    if _foreign_words(draft, req.message or ""):
        return False
    return len(draft.strip()) < _SELF_CHECK_SHORT and not _sig_numbers(draft)


def _sc_fit(text: str, tokens: int) -> str:
    """tokens 안에 들게 앞부분만(잘랐으면 _cut_mark 표지 — 검토자가 잘린 근거를 '없다' 로 읽지 않게 지시에 적었다)."""
    if not text or _est_tokens_all(text) <= tokens:
        return text
    return _fit_tokens(lambda c: _cut_mark(text, c), _chars_for_tokens(text, tokens), max(1, tokens), 200)[0]


def _sc_recent(hist: list, tokens: int) -> str:
    """앞 대화를 최근 것부터 tokens 안에 — 넘치는 항목은 앞부분만(표지), 그보다 오래된 것은 싣지 않는다."""
    lines: list[str] = []
    for r, c in reversed(hist):
        line = f"{'사람' if r == 'user' else '어시스턴트'}: {c}"
        t = _est_tokens_all(line)
        if t > tokens:
            if tokens > 200:
                lines.append(_sc_fit(line, tokens))
            break
        lines.append(line)
        tokens -= t
    return "\n".join(reversed(lines))


_SC_SHARES = {"res": 0.55, "doc": 0.25, "hist": 0.20}     # 넘칠 때 조회 결과 · 붙인 문서 · 앞 대화의 몫


def _sc_shares(need: dict, room: int) -> dict:
    """몫 나누기 — 제 몫 안에 드는 쪽은 다 주고, 남는 몫을 넘치는 쪽에 비율대로 다시 나눈다(드는 쪽이 없어질 때까지). 문서 · 앞 대화가
    없는 턴에 조회 결과가 55% 에서 잘리지 않는다."""
    got: dict = {}
    while True:
        left = room - sum(got.values())
        over = [k for k in need if k not in got]
        tot = sum(_SC_SHARES[k] for k in over)
        fit = [k for k in over if need[k] <= left * _SC_SHARES[k] / tot] if tot else []
        if not fit:
            part = {k: max(0, int(left * _SC_SHARES[k] / tot)) for k in over}
            if part:
                part[over[0]] += max(0, left - sum(part.values()))      # 나눗셈 끝수는 첫 쪽에(합이 남는 몫과 같게)
            got.update(part)
            return got
        got.update({k: need[k] for k in fit})


_SC_RES_HEAD = 16      # 결과 머리 줄 몫(토큰) — 잘림 머리는 더 길지만 room 의 여유(2,000)가 받는다


def _sc_fit_results(outputs: list, tokens: int) -> list[tuple[str, bool]]:
    """조회 결과를 결과마다 몫을 나눠 담는다(물 채우기) → [(실을 글, 이 함수가 줄였나)], 들어온 차례대로. 합이 몫 안이면 그대로, 넘치면 필요량
    (_est_tokens_all + 머리 몫)이 작은 결과부터 min(필요량, 남은 몫 ÷ 남은 결과 수) 를 주고, 모자란 결과만 앞부분으로 줄인다(_cut_mark 표지).
    종전엔 이어 붙인 글 전체를 앞에서 잘라 앞 결과가 몫을 다 먹으면 뒤 결과가 통째로 빠지고 표지는 끝에 하나뿐이었다(HA-27)."""
    need = [_est_tokens_all(o) + _SC_RES_HEAD for o in outputs]
    if sum(need) <= tokens:
        return [(o, False) for o in outputs]
    got = [0] * len(outputs)
    left = max(0, tokens)
    order = sorted(range(len(outputs)), key=lambda i: need[i])
    for n, i in enumerate(order):
        got[i] = min(need[i], left // (len(order) - n))
        left -= got[i]
    return [(o, False) if got[i] >= need[i] else (_sc_fit(o, max(1, got[i] - _SC_RES_HEAD)), True) for i, o in enumerate(outputs)]


def _sc_result_head(i: int, out: str, cut: bool) -> str:
    """결과 머리 — 잘렸으면 까닭을 밝힌다(줄이기 전 사본에서 가린다). _SC_CUT_WORD 는 지시 ②(_SC_ASK_EVIDENCE_LINE)가 가리키는 낱말이다."""
    why = []
    if _CAP_MARK in out:
        why.append("도구 결과 상한")
    if "[포털 챗이 허브 응답을 접었다" in out[:400]:
        why.append("허브 응답 접기")
    if _SC_CUT_TAIL.search(out[-64:]):
        why.append("근거 몫")
    if cut:
        why.append("검토 몫")
    return f"─ 결과 {i} {_SC_CUT_WORD} — {' · '.join(why)}: 뒷부분은 보이지 않는다) ─" if why else f"─ 결과 {i} ─"


def _self_check_messages(req: ChatRequest, draft: str, calls: list, outputs: list, idx=None) -> list:
    """검토자 입력 — 이 턴의 창(_model_context_tokens — 챗 턴은 두 서버 중 작은 창) 안에서. 고친 답을 쓸 출력 몫을 먼저 떼고, 남는 몫에
    다 들면 그대로, 넘치면 조회 결과 55% · 붙인 문서 25% · 앞 대화 20% 로 나눈다(쓰지 않는 몫은 넘치는 쪽으로 — _sc_shares). 조회 결과는
    결과마다 몫을 나누고 잘린 결과의 머리에 까닭을 밝힌다. idx(원문 색인 — HA-27)가 있으면 [코드 대조] 에서 원문에 있거나 원문 값의 셈으로
    나오는 수를 뺀다(원문에는 있는데 사본에서 잘린 수가 '출처 없는 수' 로 가서 검토자를 '지우기' 쪽으로 밀었다)."""
    win = _model_context_tokens()
    out_room = _SC_OUT_CAP                   # 검토자 호출에 보내는 출력 상한 그대로 — 입력 + 상한이 창에 들어야 한다
    room = max(3000, win - out_room - _est_tokens_all(_SELF_CHECK_SYS + _SELF_CHECK_ASK) - _est_tokens_all(draft) - 2000)
    q = _sc_fit(req.message or "", max(500, room // 10))
    room -= _est_tokens_all(q)
    res_need = sum(_est_tokens_all(o) + _SC_RES_HEAD for o in outputs) if calls else 0
    docs = [d for d in (req.documents or []) if isinstance(d, dict) and str(d.get("text") or "").strip()]
    dtext = "\n\n".join(f"── {str(d.get('name') or '문서')[:200]} ──\n{d.get('text')}" for d in docs)
    hist = _history_messages(req.history)[-8:]
    cap = _sc_shares({"res": res_need, "doc": _est_tokens_all(dtext),
                      "hist": sum(_est_tokens_all(f"어시스턴트: {c}") for _r, c in hist)}, room)
    blocks = [f"[사용자 질문]\n{q}"]
    if calls:
        lines = [f"{i}. {n}{f'({a})' if a else ''}" for i, (n, a) in enumerate(calls, start=1)]
        res = "\n\n".join(f"{_sc_result_head(i, o, cut)}\n{f}"
                          for i, (o, (f, cut)) in enumerate(zip(outputs, _sc_fit_results(outputs, cap["res"])), start=1))
        blocks.append("[이번 턴 조회 결과]\n" + "\n".join(lines) + ("\n\n" + res if res else ""))
    else:
        blocks.append("[이번 턴 조회 결과]\n(이번 턴에는 도구를 부르지 않았다 — 근거 대조는 붙인 문서 · 앞 대화로만 한다. 일반 지식으로 쓴 설명은 "
                      "지우지 말고, ① 빠진 물음 ③ 앞뒤가 어긋나는 말 ④ 계산 · 단위만 본다)")
    if docs:
        blocks.append("[붙인 문서]\n" + _sc_fit(dtext, cap["doc"]))
    htext = _sc_recent(hist, cap["hist"])
    if htext:
        blocks.append("[앞 대화 — 최근]\n" + htext)
    if req.images or _vision.history_images(req.history):
        blocks.append("[그림]\n이 턴에는 그림이 실렸다 — 검토자는 그림을 보지 못한다. 그림에서 읽은 내용(수치 포함)은 지우지 말고 그대로 둔다.")
    bad = _unsourced_numbers(draft, "\n".join(outputs) + "\n" + (req.message or ""), limit=0)
    if bad and idx is not None:
        _ok = _sc_gate.sourced_in(draft, idx, message=req.message or "")
        bad = [v for v in bad if v.replace(",", "") not in _ok]
    if calls and bad:
        blocks.append("[코드 대조 — 답의 수치 중 조회 결과 · 사용자 발화에서 찾지 못한 것]\n"
                      + ", ".join(f"`{v}`" for v in bad[:_SELF_CHECK_NUMS]) + (f" 외 {len(bad) - _SELF_CHECK_NUMS}건" if len(bad) > _SELF_CHECK_NUMS else ""))
    alien = _foreign_words(draft, req.message or "", "\n".join(outputs), dtext)
    if alien:
        blocks.append("[코드 대조 — 답에 섞인 다른 문자(한자 · 가나) 낱말, 질문 · 근거에 없는 것]\n"
                      + ", ".join(f"`{w}`" for w in alien)
                      + "\n— 질문의 언어로 바꿔라(뜻을 그대로). 고칠 것이 이것뿐이어도 [판정] 고침 이다.")
    blocks.append(f"[답 초안]\n{draft}")
    ask = _SELF_CHECK_ASK if (calls or docs) else _SELF_CHECK_ASK_NO_EVIDENCE
    mode = _sc_gate_mode()
    if calls and mode != "0":
        ask += "\n" + (_SC_ASK_GATE_LINE if mode == "1" else _SC_ASK_GATE_LINE_NUM)
    blocks.append(ask)
    from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
    return [SystemMessage(content=_SELF_CHECK_SYS), HumanMessage(content="\n\n".join(blocks))]


def _self_check_parse(out: str) -> tuple[str | None, list, list | None]:
    """검토자 답 → (판정 'ok' · 'fixed' · 못 읽으면 None, 고친 점들, 바꿀 곳 [(원문, 새 글)] — 쌍의 꼴이 깨졌으면 None)."""
    m = _SC_VERDICT.search(out or "")
    if not m:
        return None, [], ""
    v = re.sub(r"[*_`\s]", "", m.group(1))
    verdict = "fixed" if v.startswith("고침") and "없" not in v else "ok"
    fx = _SC_FIXES.search(out)
    fixes = []
    for line in (fx.group(1).splitlines() if fx else []):
        t = line.strip()
        if t.startswith("-"):
            t = t[1:].strip()
            if t and t not in ("없음", "없음."):
                fixes.append(t[:400])
    patches: list | None = []
    for part in (out.split("[바꿀 곳]", 1)[1].split("[원문]")[1:] if "[바꿀 곳]" in out else []):
        if "[새 글]" not in part:
            patches = None
            break
        old, new = part.split("[새 글]", 1)
        new = _SC_NEW_END.split(new, 1)[0]        # [바꿀 곳] 뒤에 온 [짚은 점] · [고친 점] 이 마지막 새 글에 섞이지 않게(HA-27)
        patches.append((old.strip(), new.strip()))
    return verdict, fixes[:20], patches


def _self_check_flags(out: str) -> list[str]:
    """검토자의 [짚은 점] — 고치지 않고 사람이 확인할 곳(HA-27). '없음' 꼴(_SC_NONE_LINE)은 버리고 20줄 × 400자까지. _self_check_parse 의 돌려주는
    꼴은 그대로다."""
    m = _SC_FLAGS.search(out or "")
    flags = []
    for line in (m.group(1).splitlines() if m else []):
        t = line.strip()
        if t.startswith("-"):
            t = t[1:].strip()
            if t and not _SC_NONE_LINE.fullmatch(t):
                flags.append(t[:400])
    return flags[:20]


def _sc_apply(draft: str, patches: list) -> tuple[str, str]:
    """바꿀 곳을 초안에 넣는다 → (고친 답, '') 또는 ('', 까닭). 원문은 초안에서 한 곳만 찾아야 한다 — 띄어쓰기 · 줄바꿈만 다르게 옮겼으면
    그것까지는 찾는다. 하나라도 못 넣으면(없음 · 여러 곳 · 겹침) 아무것도 넣지 않는다 — 일부만 고친 답은 앞뒤가 어긋난다(W-4)."""
    spans = []
    for old, new in patches:
        if not old:
            return "", "바꿀 곳의 원문이 비었다"
        hits = [m.span() for m in re.finditer(re.escape(old), draft)]
        if not hits:
            hits = [m.span() for m in re.finditer(r"\s+".join(map(re.escape, old.split())), draft)]
        if len(hits) != 1:
            return "", "바꿀 곳을 초안에서 찾지 못했다" if not hits else f"바꿀 곳이 초안에 {len(hits)}번 나온다"
        spans.append((*hits[0], new))
    spans.sort(key=lambda x: x[0])
    if any(b[0] < a[1] for a, b in zip(spans, spans[1:])):
        return "", "바꿀 곳끼리 겹친다"
    out = draft
    for start, end, new in reversed(spans):
        out = out[:start] + new + out[end:]
    return out, ""


def _sc_pair_desc(old: str, new: str) -> str:
    """[고친 점] 줄과 쌍의 수가 다를 때 코드가 만드는 쌍의 설명."""
    return f"'{' '.join(old.split())[:60]}' → '{' '.join(new.split())[:60] or '(지움)'}'"


def _self_check_judge(draft: str, verdict: str | None, fixes: list, patches: list | None, cut: bool, *,
                      idx=None, flags=(), values: bool = True, message: str = "") -> tuple[str, dict]:
    """검토자 판정 → (낼 답, meta.self_check). 바꿀 곳을 못 넣거나 고친 답이 수상하면 초안을 그대로 내고 까닭을 적는다(고칠 점은 보인다).
    idx(이번 턴 원문 색인 — HA-27)가 있으면 쌍마다 원문과 다시 대조해 넣음 쌍만 넣는다(sc_gate.gate_patches). flags 는 검토자의 [짚은 점].
    idx 도 flags 도 없으면 종전과 글자까지 같은 (답, check) 다(관문 끔 · 도구 없는 턴)."""
    flags = [str(f) for f in (flags or ()) if str(f).strip()]
    if idx is not None or flags:
        return _sc_judge_gated(draft, verdict, fixes, patches, cut, idx, flags, values, message)
    if verdict is None:
        return draft, {"status": "unreadable", "reason": "검토자 답을 읽지 못했다 — 초안을 그대로 냈다"}
    if verdict == "ok":
        return draft, {"status": "ok"}
    why, ans = "", ""
    if cut:
        why = "검토자 답이 출력 상한에서 끊겼다"
    elif patches is None:
        why = "바꿀 곳의 꼴을 읽지 못했다([원문] 뒤에 [새 글] 이 없다)"
    elif not patches:
        why = "고친다고 했지만 바꿀 곳이 없다"
    else:
        ans, why = _sc_put(draft, patches)
    if why:
        return draft, {"status": "suspect", "reason": why + " — 초안을 그대로 냈다", "fixes": fixes}
    if ans.strip() == draft.strip():
        return draft, {"status": "ok"}         # 고쳤다고 했지만 바뀐 글자가 없다
    return ans, {"status": "fixed", "fixes": fixes}


def _sc_put(draft: str, patches: list) -> tuple[str, str]:
    """바꿀 곳을 넣고 고친 답이 수상한지 본다 → (고친 답, '') 또는 (_, 까닭) — 못 넣음(_sc_apply) · 지나치게 짧음 · 그림 잃음."""
    ans, why = _sc_apply(draft, patches)
    if not why and len(ans) < len(draft) * _SELF_CHECK_MIN_RATIO:
        why = f"고친 답이 초안보다 지나치게 짧다({len(draft):,}자 → {len(ans):,}자)"
    if not why:
        lost = [u for u in dict.fromkeys(_SC_IMG.findall(draft)) if u not in ans]
        if lost:
            why = f"고친 답이 초안의 그림 {len(lost)}개를 잃었다"
    return ans, why


# flagged 의 까닭 — 고정 글이다(_sc_record 가 사유를 원장에 싣는다 — 사용자 글을 싣지 않는다).
_SC_WHY_GATED = "검토자의 고침을 조회 결과 원문과 대조해 답에 넣지 않았다 — 짚은 곳만 보인다"
_SC_WHY_FLAGS = "검토자가 고치지 않고 짚기만 했다"
_SC_WHY_DROPPED = "검토자의 고침이 조회 결과 원문에 있는 값을 지우거나 바꿔 버렸다 — 초안을 그대로 냈다"


def _sc_judge_gated(draft: str, verdict: str | None, fixes: list, patches: list | None, cut: bool, idx, flags: list,
                    values: bool, message: str) -> tuple[str, dict]:
    """관문 길(HWAXPortal docs/request-1030/design-ha27.md 6.6) — 원문 색인이 있거나 검토자가 [짚은 점] 을 썼을 때. 상태는 관문 뒤의 결과다 —
    넣음 쌍만 넣고(다 넣거나 하나도 넣지 않는다 — C-24 그대로), 넣음이 없으면 짚음 · [짚은 점] 이 있을 때 flagged(답 = 초안), 버림만이면 ok.
    meta 에 flags(검토자 [짚은 점] + 짚음이 된 쌍) · dropped(버린 쌍 — 화면에는 보이지 않는다) · gate(관문이 돈 턴만)를 싣는다. 관문 뒤에
    검토자에게 다시 묻지 않는다(턴 시간 · 모델 호출 수가 늘지 않는다)."""
    gate = ({"applied": 0, "reviewer_flags": len(flags), "reviewer_fixed": verdict == "fixed", "raw_partial": bool(idx.partial)}
            if idx is not None else None)

    def done(ans: str, check: dict) -> tuple[str, dict]:
        if gate is not None:
            check["gate"] = gate
        return ans, check

    def cap(lines: list) -> list:
        return [str(x)[:_SC_NOTE_CHARS] for x in lines][:_SC_NOTE_MAX]

    def flagged(lines: list, why: str, dropped: list = ()) -> dict:
        # fixes 에도 같은 줄 — 옛 화면은 flagged 를 '그 밖의 상태' 로 보고 fixes 를 '검토자가 짚은 곳(반영하지 않음)' 으로 보인다
        return {"status": "flagged", "fixes": lines, "flags": lines, **({"dropped": dropped} if dropped else {}), "reason": why}

    if verdict is None:
        return draft, {"status": "unreadable", "reason": "검토자 답을 읽지 못했다 — 초안을 그대로 냈다"}
    if verdict == "ok":
        return done(draft, flagged(cap(flags), _SC_WHY_FLAGS) if flags else {"status": "ok"})
    why = ("검토자 답이 출력 상한에서 끊겼다" if cut else
           "바꿀 곳의 꼴을 읽지 못했다([원문] 뒤에 [새 글] 이 없다)" if patches is None else
           "고친다고 했지만 바꿀 곳이 없다" if not patches and not flags else "")
    if why:
        return done(draft, {"status": "suspect", "reason": why + " — 초안을 그대로 냈다", "fixes": fixes,
                            **({"flags": cap(flags)} if flags else {})})
    if not patches:                                   # 고친다고 했지만 바꿀 곳 없이 짚기만 했다
        return done(draft, flagged(cap(flags), _SC_WHY_FLAGS))
    decisions = (_sc_gate.gate_patches(patches, idx, draft=draft, message=message, values=values) if idx is not None
                 else [_sc_gate.KEEP] * len(patches))
    paired = len(fixes) == len(patches)           # [고친 점] 줄이 쌍과 같은 차례로 짝을 이룬다(지시가 약속한다)
    desc = [fixes[i] if paired else _sc_pair_desc(o, w) for i, (o, w) in enumerate(patches)]
    keep = [p for p, d in zip(patches, decisions) if d.action == "keep"]
    demoted = [f"{desc[i]} — 반영 안 함: {_sc_gate.reason(d)}" for i, d in enumerate(decisions) if d.action == "demote"]
    dropped = cap([f"{desc[i]} — 버림: {_sc_gate.reason(d)}" for i, d in enumerate(decisions) if d.action == "drop"])
    if gate is not None:
        for d in decisions:
            if d.action != "keep":
                gate[d.code] = gate.get(d.code, 0) + 1
    shown = cap(flags + demoted)
    extra = {**({"flags": shown} if shown else {}), **({"dropped": dropped} if dropped else {})}
    if not keep:
        if shown:
            return done(draft, flagged(shown, _SC_WHY_GATED, dropped))
        return done(draft, {"status": "ok", "dropped": dropped, "reason": _SC_WHY_DROPPED})
    ans, why = _sc_put(draft, keep)
    if why:
        return done(draft, {"status": "suspect", "reason": why + " — 초안을 그대로 냈다",
                            "fixes": [fixes[i] for i, d in enumerate(decisions) if d.action != "drop"] if paired else fixes, **extra})
    if ans.strip() == draft.strip():                  # 넣었지만 바뀐 글자가 없다
        return done(draft, flagged(shown, _SC_WHY_GATED if len(keep) < len(patches) else _SC_WHY_FLAGS, dropped) if shown
                    else {"status": "ok", **extra})
    if gate is not None:
        gate["applied"] = len(keep)
    kept = fixes if len(keep) == len(patches) else [desc[i] for i, d in enumerate(decisions) if d.action == "keep"]
    return done(ans, {"status": "fixed", "fixes": kept, **extra})


async def _self_check_stream(app: FastAPI, req: ChatRequest, draft: str, calls: list, outputs: list, res: dict,
                             raw: dict | None = None):
    """검토자를 부른다 — 글은 '생각 중' 칸으로 흘리고, 끝나면 res 에 final · check 를 둔다. 실패해도 초안이 나간다. raw 는 이번 턴의 원문
    그릇(HA-27) — 관문이 켜져 있고 도구를 부른 턴이면 검토자 입력과 같은 스레드에서 원문 색인을 짓고, 검토자의 바꿀 곳을 넣기 전에 그 색인과
    다시 대조한다(관문 계산은 스레드 두 번 — 이벤트 루프 · 15초 ping 을 막지 않는다)."""
    llm = _chat_llm_of(app)
    acc, cut = "", False
    model = getattr(llm, "model_name", None) or getattr(llm, "model", None)
    kw = {"max_tokens": _SC_OUT_CAP}
    if os.environ.get("CHAT_SELF_CHECK_THINK", "0") != "1":
        kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    mode = _sc_gate_mode()
    try:
        msgs, idx = await asyncio.to_thread(_sc_prepare, req, draft, calls, outputs, raw, mode)
    except Exception as exc:  # noqa: BLE001 — 검토 준비 실패가 답을 못 죽인다(관문 없이 넣지 않는다 — 초안을 낸다)
        print(f"[self-check] 검토 준비 실패 — {type(exc).__name__}: {exc}"[:300])
        res.update(final=draft, check={"status": "failed", "reason": f"검토자 입력 · 원문 대조를 준비하지 못했다({type(exc).__name__}) — "
                                                                     "초안을 그대로 냈다"}, model=None)
        return
    try:
        yield _sse("reasoning", {"delta": _CHECK_HEAD})
        async for ch in llm.astream(msgs, **kw):
            rc = (getattr(ch, "additional_kwargs", None) or {}).get("reasoning_content")
            if rc:
                yield _sse("reasoning", {"delta": rc})
            c = getattr(ch, "content", "") or ""
            if isinstance(c, list):
                c = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
            if c:
                # 판정 · 고친 점 · 바꿀 곳을 그대로 '생각' 칸에 흘린다 — 바꿀 곳은 짧고(W-4), 무엇을 고쳤는지 그 칸에서 보인다.
                acc += c
                yield _sse("reasoning", {"delta": c})
            md = getattr(ch, "response_metadata", None) or {}
            if md.get("finish_reason") == "length":
                cut = True
            if md.get("model_name"):
                model = md["model_name"]
    except Exception as exc:  # noqa: BLE001 — 검증 실패가 답을 못 죽인다
        print(f"[self-check] 검토자 호출 실패 — {type(exc).__name__}: {exc}"[:300])
        res.update(final=draft, check={"status": "failed", "reason": f"검토자 호출 실패({type(exc).__name__}) — 초안을 그대로 냈다"},
                   model=None)
        return
    verdict, fixes, patches = _self_check_parse(acc)
    try:
        final, check = await asyncio.to_thread(_self_check_judge, draft, verdict, fixes, patches, cut, idx=idx,
                                               flags=_self_check_flags(acc), values=mode == "1", message=req.message or "")
    except Exception as exc:  # noqa: BLE001 — 관문 실패가 답을 못 죽인다(관문 없이 넣지 않는다 — 초안을 낸다)
        print(f"[self-check] 원문 대조 실패 — {type(exc).__name__}: {exc}"[:300])
        final, check = draft, {"status": "failed", "reason": f"검토자의 고침을 원문과 대조하지 못했다({type(exc).__name__}) — 초안을 그대로 냈다"}
    if isinstance(check.get("gate"), dict):
        check["gate"]["raw_chars"] = int((raw or {}).get("chars") or 0)     # 0 이면 이 턴에 원문을 붙잡지 못했다(사본으로만 대조)
    res.update(final=final, check=check, model=str(model) if model else None)


async def _self_checked_turn(app: FastAPI, req: ChatRequest) -> AsyncIterator[bytes]:
    """자기검증 감싸개(SC-D2) — 안쪽 턴의 초안 글(token)은 '생각 중' 칸(reasoning)으로 돌리고 결과 · 끝 · meta 는 붙잡는다. 초안이
    끝나면 검증해 고친 답을 본문(token)으로 내고, 근거 블록은 고친 답으로 다시 센다. 오류 · 조기 종료 · 다른 결과 꼴은 초안을 본문으로
    그대로 흘린다(종전과 같은 끝)."""
    box: dict = {}
    inner = _agent_stream_turn(app, req, box)
    draft_parts: list[str] = []
    held: dict = {}
    in_draft = failed = False
    try:
        async for frame in inner:
            ev, data = _sse_parse(frame)
            if ev == "token" and not failed:
                d = str(data.get("delta") or "")
                if d:
                    draft_parts.append(d)
                    if not in_draft:        # 첫 초안 글 · 도구를 부르고 다시 쓰기 시작한 글 — 생각 글과 갈라 보이게 머리를 단다
                        in_draft = True
                        yield _sse("reasoning", {"delta": _DRAFT_HEAD})
                    yield _sse("reasoning", {"delta": d})
                continue
            if ev == "reasoning" and in_draft:
                in_draft = False
                yield _sse("reasoning", {"delta": "\n\n"})
            if ev in ("result", "meta", "done"):
                held[ev] = data
                continue
            if ev == "error" and not failed:
                failed = True       # 오류 길 — 초안을 본문으로 먼저 흘리고 오류를 낸다(종전과 같은 화면)
                if draft_parts:
                    yield _sse("token", {"delta": "".join(draft_parts)})
            yield frame
    finally:
        await inner.aclose()
    res = held.get("result")
    draft = box.get("draft")
    if failed or draft is None or not isinstance(res, dict) or res.get("type") != "text":
        if not failed and draft_parts:
            yield _sse("token", {"delta": "".join(draft_parts)})
        for ev in ("meta", "result", "done"):
            if ev in held:
                yield _sse(ev, held[ev])
        return
    calls, outputs = box.get("calls") or [], box.get("outputs") or []
    t0 = time.monotonic()
    if box.get("confirm"):
        # 확인 카드로 멈춘 턴(HA-29) — 검토자를 부르지 않는다(카드 글 · 번호를 고치지 못하게). 앞서 조회한 도구가 있어도 같다(_self_check_skip
        # 만으로는 검토가 돈다).
        final, check, vmodel = draft, {"status": "skipped", "reason": "확인 카드로 멈춘 턴 — 검증할 답이 없다"}, None
    elif _self_check_skip(req, draft, calls):
        final, check, vmodel = draft, {"status": "skipped", "reason": "근거(조회 · 붙인 문서 · 그림)도 수치도 없는 짧은 답"}, None
    else:
        yield _sse("status", {"step": "답을 검증하는 중 — 질문 · 근거 · 수치를 대조합니다", "tool": None})
        out: dict = {}
        sc = _self_check_stream(app, req, draft, calls, outputs, out, box.get("raw"))
        try:
            async for fr in sc:
                yield fr
        finally:
            await sc.aclose()       # 연결이 끊기면 검토자 호출도 지금 닫는다
        final, check, vmodel = out.get("final", draft), out.get("check") or {"status": "failed"}, out.get("model")
    check["ms"] = int((time.monotonic() - t0) * 1000)
    _sc_record(check)
    for i in range(0, len(final), 400):
        yield _sse("token", {"delta": final[i:i + 400]})
    text = final
    if calls and os.environ.get("CHAT_EVIDENCE_BLOCK", "1") != "0":
        try:
            _bad = _unsourced_numbers(_confirm_strip(final, box.get("confirm")), "\n".join(outputs) + "\n" + (req.message or ""),
                                      limit=0)   # 카드 글(예상 작업량)은 수 대조에서 뺀다(C-14)
            _ev = _evidence_block(calls, _bad)
            if _ev:
                text += _ev
                yield _sse("token", {"delta": _ev})
        except Exception as exc:  # noqa: BLE001 — 근거 표기 실패가 답변을 못 죽인다
            print(f"[agent] evidence block failed: {exc!r}")
    meta = dict(held.get("meta") or {})
    meta["models"] = dict(meta.get("models") or {})
    if vmodel:
        meta["models"][vmodel] = meta["models"].get(vmodel, 0) + 1    # 검토자 호출도 이 턴의 모델 호출이다(C3)
    meta["self_check"] = check
    yield _sse("meta", meta)
    yield _sse("result", {"type": "text", "content": text})
    if "done" in held:
        yield _sse("done", held["done"])


async def _agent_stream(app: FastAPI, req: ChatRequest) -> AsyncIterator[bytes]:
    """챗 한 턴. 챗 전용 LLM(CHAT_LLM_*)이 켜져 있으면 이 턴의 문맥 예산을 그 서버 창과 VLLM_* 창 중 **작은 값**으로 잡는다 —
    챗 경로의 예산(이력 · 문서 · 도구 결과 · 도구 설명)은 _model_context_tokens() 하나를 보는데 그 값은 VLLM_*(GLM 1M) 창이라,
    그대로면 창이 작은 챗 서버(Qwen 262,144)에 넘치는 요청을 보낸다(400). 작은 값이면 VLLM_* 로 넘겨도 맞는다.
    출력 몫(CHAT_LLM_OUTPUT_RESERVE — 생각을 켜면 65,536, 보내는 상한이 더 크면 그 값)만큼 뗀다. 상한을 보내면 vLLM 은 입력 + max_tokens 가
    창을 넘을 때 400 이고 그 400 은 VLLM_* 로 넘어가 Qwen 대신 GLM 이 조용히 답한다. 보내지 않아도 입력이 창을 채우면 생각할 자리가 없어 답이
    잘린다(HWAXPortal docs/chat-depth-context CD-D4 · CD-D6). 창의 절반보다 크게는 떼지 않는다.
    외부 응답자와 같은 문맥 변수(_external_llm.ctx_override)를 이 턴의 태스크에만 건다(HWAXPortal docs/chat-llm-stcx)."""
    tok = None
    # 챗 전용 LLM 이 없는 박스(dev 등)는 스레드를 오가지 않는다 — 종전 턴과 똑같다.
    chat_cfg = getattr(app.state, "chat_llm_cfg", None)
    chat_ctx = await asyncio.to_thread(_chat_context_tokens) if chat_cfg else None
    if chat_ctx:
        base_ctx = await asyncio.to_thread(_model_context_tokens)
        win = min(chat_ctx, base_ctx)
        out = max(0, int(chat_cfg.get("output_reserve") or chat_cfg.get("max_tokens") or 0))
        tok = _external_llm.ctx_override.set(win - min(out, win // 2))
    # 자기검증(HWAXPortal docs/chat-self-check) — 켜져 있으면 초안을 근거와 대조해 한 번 고친 뒤 본문으로 낸다
    turn = _self_checked_turn(app, req) if _self_check_on() else _agent_stream_turn(app, req)
    atok = _chat_affinity.set(_affinity_key(req))      # 이 턴의 챗 LLM 호출 대화 머리(W-1) — 턴의 태스크에만
    try:
        async for chunk in turn:
            yield chunk
    finally:
        await turn.aclose()      # 연결이 끊겨 이 생성기가 닫혀도 턴의 finally 가 지금 돈다(종전과 같은 때)
        _chat_affinity.reset(atok)
        if tok is not None:
            try:
                _external_llm.ctx_override.reset(tok)
            except ValueError:   # 다른 문맥에서 닫혔다 — 그 문맥은 곧 버려진다
                pass


# 앞 대화 그림 다시 싣기 머리의 단서(PP-D5) — 다시 실린 그림은 질문 바로 앞에 놓여, 사람이 '방금 변환한 것' 이라고 하면 모델이 그 그림을
# 가리키는 말로 읽었다(10-22 cae00 대화 — PPT 를 AI 데이터 허브로 보낸 뒤 앞서 올린 그림을 검토했다). 다시 싣기는 그대로 둔다(그림과 새
# 문서를 견주는 질문 · 이력 압축) — 단서만 단다.
_RECAP_NOT_UPLOAD = ("이번 질문이 이 그림이 아니라 올리거나 변환한 파일 · 보고서를 가리키면 그림으로 대신 답하지 말고, 이력의 업로드 근거"
                     "('사람이 챗 화면에서 파일을 올려 보낸 결과')를 보거나 없으면 어느 파일인지 물어라")


async def _turn_image_messages(app_, req, vision_ok: bool):
    """(이 발화의 사람 메시지, 앞 대화의 그림 메시지 또는 None). 화면 사건은 _TURN_IMAGE_EVENTS 에 쌓는다(호출부가 흘린다).
    챗 모델이 그림을 받으면 그림 조각, 못 받으면 그림을 받는 다른 모델이 옮긴 판독문, 그것도 없으면 싣지 못했다고 적는다 — 어느 갈래든
    화면에 한 줄로 알린다(조용히 빼지 않는다)."""
    from langchain_core.messages import HumanMessage  # noqa: PLC0415

    events = _TURN_IMAGE_EVENTS.get()

    def say(step: str) -> None:
        if events is not None:
            events.append(_sse("status", {"step": step, "tool": None}))
    up, skipped = await asyncio.to_thread(_stash_uploads, req.images)
    if up and events is not None:
        events.append(_sse("user_images", {"urls": up}))
    if skipped:
        say(f"올린 그림 {skipped}장은 싣지 못했다 — 그림(png · jpg · gif · webp)이 아니거나 깨졌거나 "
            f"{_vision.MAX_BYTES // (1024 * 1024)}MB 를 넘거나 한 번에 {_vision.MAX_IMAGES}장 상한 밖이다")
    hist = _vision.history_images(req.history)
    pick = hist[-_vision.HISTORY_IMAGES:] if _vision.HISTORY_IMAGES > 0 else []
    tr = await asyncio.to_thread(_transcriber, app_) if not vision_ok and (up or pick) else None
    hint = req.message[:300]
    user_msg, recap = ("user", req.message), None
    if up and vision_ok:
        ps, miss = await asyncio.to_thread(_vision.parts, up, ARTIFACT_DIR, AIDH_HTTP, _vision.MAX_IMAGES)
        text = req.message + (f"\n\n[올린 그림 {len(up)}장 중 {miss}장은 커서 싣지 못했다]" if miss else "")
        user_msg = HumanMessage(content=[{"type": "text", "text": text}, *ps])
    elif up and tr is not None:
        got = await _vision.transcribe_all(tr, [(u, f"올린 그림 {i}") for i, u in enumerate(up, start=1)],
                                           ARTIFACT_DIR, AIDH_HTTP, hint=hint)
        user_msg = ("user", req.message + "\n\n[시스템 첨부 — 올린 그림을 이 챗 모델은 읽지 못해 그림을 읽는 모델이 판독문으로 옮겼다. "
                    "판독문에 없는 수치를 지어내지 마라]\n"
                    + "\n".join(f"· [그림 {i}] {t or '판독하지 못했다'}" for i, (_u, _w, t) in enumerate(got, start=1)))
        say("이 챗 모델은 그림을 읽지 못한다 — 올린 그림을 판독문으로 실었다" + _vision.unknown_note())
    elif up:
        user_msg = ("user", f"{req.message}\n\n[시스템 첨부 — 그림 {len(up)}장을 올렸지만 이 서버의 모델은 그림을 읽지 못하고, "
                            "판독할 모델도 없다]")
        say("이 챗 모델은 그림을 읽지 못한다 — 올린 그림을 모델에 싣지 못했다(판독할 모델도 없다)" + _vision.unknown_note())
    if pick and vision_ok:
        ps, labels = [], []
        for u, why in pick:     # 한 장씩 — 지워진 그림이 끼어도 설명 번호가 실린 그림 순서와 맞게
            one, _m = await asyncio.to_thread(_vision.parts, [u], ARTIFACT_DIR, AIDH_HTTP, 1)
            if one:
                ps += one
                labels.append(f"{len(ps)}) {why}")
        miss = len(pick) - len(ps)
        if ps:
            recap = HumanMessage(content=[{"type": "text", "text": (
                f"[시스템 첨부 — 앞 대화에 나온 그림 {len(ps)}장(전체 {len(hist)}장 중 최근부터): " + "; ".join(labels)
                + (f" · {miss}장은 서버에서 지워졌거나 커서 못 실었다" if miss else "")
                + ". 질문에 이 그림이 걸리면 직접 보고 판단하라. " + _RECAP_NOT_UPLOAD + "]")}, *ps])
        say(f"앞 대화의 그림 {len(ps)}장을 다시 실었습니다" + (f" · {miss}장은 서버에서 지워졌거나 커서 못 실었다" if miss else "")
            + (f" · 오래된 {len(hist) - len(pick)}장은 상한(VISION_HISTORY_IMAGES) 밖" if len(hist) > len(pick) else ""))
    elif pick and tr is not None:
        got = await _vision.transcribe_all(tr, pick, ARTIFACT_DIR, AIDH_HTTP, hint=hint)
        lines = [f"· {k}) ({why}) {t}" for k, (_u, why, t) in enumerate(got, start=1) if t]
        if lines:
            recap = HumanMessage(content="[시스템 첨부 — 앞 대화에 나온 그림의 판독문이다(그림을 읽는 모델이 옮긴 글 — 그림 자체가 아니다). "
                                         "판독문에 없는 수치를 지어내지 마라. " + _RECAP_NOT_UPLOAD + "]\n" + "\n".join(lines))
        say(f"앞 대화의 그림 판독문 {len(lines)}장을 실었습니다(챗 모델이 그림을 읽지 못한다)" + _vision.unknown_note()
            + (f" · {len(got) - len(lines)}장은 판독하지 못했다" if len(got) > len(lines) else ""))
    elif pick:
        say(f"앞 대화의 그림 {len(hist)}장 — 챗 모델도 판독할 모델도 그림을 읽지 못해 싣지 못했다" + _vision.unknown_note())
    return user_msg, recap


async def _drain_image_events(events: list):
    for ev in list(events):
        yield ev
    events.clear()


_TURN_IMAGE_EVENTS: contextvars.ContextVar = contextvars.ContextVar("turn_image_events", default=None)


async def _agent_stream_turn(app: FastAPI, req: ChatRequest, box: dict | None = None) -> AsyncIterator[bytes]:
    """챗 한 턴. box 를 주면(자기검증 감싸개 — HWAXPortal docs/chat-self-check SC-D2) 끝에서 초안(text) · 이번 턴 조회 기록을 거기
    넘기고 근거 블록은 붙이지 않는다 — 감싸개가 고친 답으로 다시 센다."""
    full: list[str] = []
    _turn_models: dict = {}     # 이 턴의 모델 호출 수(모델 이름 → 횟수) — 턴 끝 'meta' 사건(C3)
    turn_imgs: list = []
    _turn_images.set(turn_imgs)
    _turn_image_events: list = []   # 그림 손질이 낸 화면 사건(올린 그림 주소 · 상태 줄) — 메시지를 짓기 전에 흘린다
    _TURN_IMAGE_EVENTS.set(_turn_image_events)
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
    _turn_tool_calls.set(Counter())
    # ── 10-30 턴 상태 자리 — 재촉 → 이어하기가 req.message 를 바꾸기 전에 건다(HA-30 · HA-28 · HA-29 · HA-27, design-integration 3절)
    _turn_hub_ids.set({} if _XHUB_GATE else None)          # HA-30 — 턴마다 새로(앞 턴에 배운 것은 남지 않는다)
    _turn_status_wait.set(_status_wait_mod.new_turn() if _status_wait_mod.on() else None)       # HA-28
    _turn_confirm.set(_confirm_ctx(req) if CHAT_CONFIRM_GATE else None)   # HA-29 — 주문 판정은 원문을 본다 · 앞 카드의 승인 · 거절도 여기서 가린다
    _turn_sc_raw.set(_sc_raw_box() if box is not None else None)       # HA-27 — 자기검증 감싸개가 준 턴만(원문 그릇)
    _cctx = _turn_confirm.get() or _ConfirmCtx()     # 관문이 꺼져 있으면 빈 맥락(막지도 보내지도 않는다)
    for _c in _cctx.approved.values():
        _seed_ids |= _int_tokens(_c["args"])          # 승인된 인자의 정수는 사람이 카드에서 본 값이다 — 유령 ID 관문의 출처로
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
                + (f"\n[끊긴 지점까지 나온 내용]\n{_resume_part(_part)}" if _part else ""))})
            print(f"[agent] nudge→resume: {_orig[:60]!r}")
    yield _sse("status", {"step": "분석 중", "tool": None})
    if _cctx.status:
        yield _sse("status", {"step": _cctx.status, "tool": None})     # 확인 카드의 승인 · 거절 · 찾지 못함(HA-29)
    try:
        # 지정 전문가 — 역할·운영 설정을 도구 선별보다 먼저 읽는다. HE팀 운영자는 자기 앱 도구를
        # 묶어야 해서 _agent_for 전에 알아야 한다(예전엔 역할 문구만 붙고 그 앱 도구는 따라오지 않아,
        # 운영자를 골라도 도구는 질의 관련도로만 뽑혔다).
        # 여러 명을 골랐으면 첫 명이 주 전문가다 — 목소리·지식카드·판단 기준은 이 사람 것이고,
        # 나머지는 도구(운영자)와 판단 기준(도메인 전문가)만 보탠다. 한 명만 고른 종전 경로는
        # _keys 가 한 원소가 되어 그대로다.
        _keys = _persona_keys(req.pinned_agents, req.pinned_agent)
        agent_key = _keys[0] if _keys else ""
        # 카탈로그 조회(역할 원문 · 지식카드)는 원문 그릇에 넣지 않는다(_sc_raw_off — HA-27 설계 4.2)
        persona = await _sc_raw_off(_persona_meta(app, req.groups, agent_key)) if agent_key else {}
        operator = bool(persona.get("operator"))
        op_apps = list(persona.get("apps") or []) if operator else []
        # 보조 전문가 — 운영자는 앱·입구도구를, 도메인 전문가는 역할(판단 기준)을 빌려준다.
        helpers: list[tuple[str, dict]] = []
        helper_apps: list[str] = []
        for _k in _keys[1:]:
            _m = await _sc_raw_off(_persona_meta(app, req.groups, _k))
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
            # 그 앱 도구가 정말 없을 때만 '0개 우선' 대신 없다고 말한다. 매핑을 못 받았으면(_tm 빈 값) 없다고 단정하지
            # 않는다. _from_apps 는 콕 집은 도구를 빼고 세므로 판정은 매핑으로 한다.
            _no_tools = bool(_tm) and not any(gk in apps for gk in _tm.values())
            yield _sse("status", {"step": (f"지정 앱 {len(apps)}개 — 이 서버에는 그 앱 도구가 없습니다" if _no_tools
                                           else f"지정 앱 {len(apps)}개 → 도구 {len(_from_apps)}개 우선"),
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
        # 전문가를 앉혔으면 **그 사람이 쓸 줄 아는 도구**가 먼저 붙어야 한다. 종전에는 운영자
        # 페르소나만 도구가 바뀌고(op_apps·key_tools) 도메인 전문가는 질문 어휘로만 골라서,
        # 물성 전문가를 골라도 물성 DB 가 캡 밖으로 밀리는 일이 있었다 — 전문가를 고른 효과가
        # 말투·판단 기준까지만 갔다. 심의 좌석과 같은 기준(_seat_tool_rank)을 쓴다.
        # 사용자가 콕 집은 것(pinned)·앱 핀보다는 **뒤**다(_select_tools 의 3순위).
        prefer: list[str] = []
        if persona and not (req.pinned_tools or req.pinned_apps or apps):
            # ⚠ 캐시 미스면 동기 urlopen(5초)이라 이벤트 루프를 막는다. 종전엔 앱 핀 턴만
            #   탔는데 이제 전문가를 앉힌 **모든 턴**이 타므로 스레드로 뺀다(같은 함수의
            #   임베딩 호출이 이미 같은 이유로 to_thread 다).
            _names = list(await asyncio.to_thread(_tools_map))
            if _names:
                prefer = _seat_tool_prefer(_names, persona, _sel_q, _PERSONA_PREFER_N)
        agent = await _agent_for(app, req.groups, pinned, _sel_q, req.search_sources,
                                 req.user_email, req.user_pat, first,
                                 # 앱 하나를 통째로 핀한 턴이면 상시 예약을 안내대 4개로 줄인다.
                                 # ⚠ 조건이 '주 전문가가 운영자일 때'면 안 된다 — 이 기능의 핵심
                                 # 조합인 '도메인 전문가(주) + 도구 운영자(보조)'가 그 밖으로
                                 # 빠져 핵심 38개가 예산 밖에서 덧붙고, dev 16K 에서 16,385
                                 # 토큰으로 400 이 났다(실측: PCB 전문가 + 라미네이트 운영자).
                                 _GUIDE_TOOLS if (operator or op_apps) else None,
                                 prefer)
        # ⚠ 상태줄은 **바인딩된 뒤에** 낸다. 앞에 내면 (a) 조기 반환·예산 절단으로 실제로는
        #   안 붙었을 수 있고 (b) prefer 는 그룹 필터 전 목록이라 그 사용자에게 없는 도구
        #   이름을 "우선 바인딩" 칩으로 그려 준다. 실제로 묶인 것만 말한다.
        if prefer:
            _bound = set(getattr(agent, "_hwax_tools", ()) or ())
            _hit = [n for n in prefer if n in _bound] if _bound else prefer
            if _hit:
                yield _sse("status", {"step": f"{persona.get('name') or agent_key} 분야 도구 "
                                              f"{len(_hit)}종 우선 바인딩",
                                      "tool": None, "tools_used": _hit[:8]})
            else:
                # 하나도 안 붙었으면 그렇게 말한다 — 조용히 넘기면 '전문가 도구로 답했다'로 읽힌다.
                yield _sse("status", {"step": f"{persona.get('name') or agent_key} 분야 도구는 "
                                              f"이번 턴에 안 붙었습니다(권한·예산)", "tool": None})
        # 게이트웨이에서 도구를 못 받아 오면 도구 0개 에이전트가 되고, 모델은 도구가 있다고
        # 착각한 채 "지금 바로 호출하겠습니다"만 하고 아무것도 호출하지 않는다(조용한 실패).
        # 사용자에게 상태를 알리고, 모델에게도 도구가 없음을 명시해 헛약속을 막는다.
        _tool_err = (getattr(app.state, "tool_load_error", None) or {}).get(frozenset(req.groups))
        # 강등은 '도구 없음' 이 아니다 — 서비스 계정 시야로 돌 뿐 도구는 다 있다. 그래서
        # 도구 호출을 막지 않고, 사용자에게만 그 사실을 알린다(사용자별 칸이라 남에게 안 번진다).
        _tool_deg = (getattr(app.state, "tool_degraded", None) or {}).get(
            (frozenset(req.groups), (req.user_email or "").strip().lower()))
        sys_prompt = _system_prompt()
        turn_notes = ""      # 이 턴만의 안내 — 시스템 메시지 밖에 둔다(_turn_msgs · W-3)
        if _tool_deg and not _tool_err:
            yield _sse("status", {"step": "내 자격증명으로 도구에 붙지 못해 공용 권한으로 답합니다 "
                                          "— 내 대화 검색·저장은 이번 턴에 안 됩니다",
                                  "tool": None})
        if _tool_err:
            yield _sse("status", {"step": f"도구 목록을 불러오지 못했습니다({_tool_err}) — 도구 없이 답변합니다",
                                  "tool": None})
            turn_notes += ("\n\n[중요] 이번 턴에는 사용 가능한 도구가 하나도 없다(게이트웨이 연결 실패). "
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
            turn_notes += (f"\n\n{_hdr}\n{_labels}\n"
                           f"{_why}. 이 질문 처리에 적합한 도구가 "
                           f"그 앱 안에 있으면 반드시 우선 호출하고 결과를 답변에 인용하라"
                           f"(다른 도구 사용 금지는 아니다). 어떤 도구를 쓸지는 네가 고른다.")
            if req.pinned_tools:
                _direct = [n for n in pinned if n in set(req.pinned_tools or [])]
                if _direct:
                    turn_notes += ("\n그중 사용자가 콕 집은 도구: " + ", ".join(_direct[:12]) + " — 가장 우선.")
        elif pinned:
            turn_notes += ("\n\n[사용자 지정 우선 도구]\n" + ", ".join(pinned)
                           + "\n사용자가 직접 선택한 도구다. 이 질문 처리에 적합하면 반드시 이 도구들을 "
                             "우선 호출하고, 결과를 답변에 인용하라(다른 도구 사용 금지는 아님).")
        # 붙어 있는 앱만 말한다 — 일부만 빠졌는데 '그 앱 도구 없이' 라고 하면 붙은 앱 도구까지 쓰지 말라는 뜻으로 읽힌다.
        _op_ok = [a for a in op_apps if a not in _op_missing]
        if _op_missing:
            _ml = ", ".join(_app_label(a) for a in _op_missing)
            _okl = ", ".join(_app_label(a) for a in _op_ok)
            yield _sse("warning", {"code": "operator_app_missing",
                                   "message": f"{agent_key} 가 모는 앱({_ml})이 이 게이트웨이에 연결돼 있지 않습니다 — "
                                              + (f"{_okl} 도구로 답합니다." if _okl else "그 앱 도구 없이 답합니다.")})
            turn_notes += (f"\n\n[중요] 이 운영자가 모는 앱({_ml})의 도구가 지금 연결돼 있지 않다. 그 도구를 "
                           "호출하겠다고 말하지 말고, 연결되지 않았다는 사실을 먼저 밝힌 뒤 "
                           + (f"연결된 {_okl} 의 도구로 할 수 있는 범위에서 답하라." if _okl
                              else "도구 없이 답할 수 있는 범위만 답하라."))
        # 지정 전문가 페르소나 — 선택 패널에서 고른 전문가의 역할로 답하는 '전문가와 대화' 모드.
        if agent_key and operator:
            # HE팀 MCP 운영자 — 역할(사전 지식·작업 순서·함정)만 싣고 지식카드 선조회는 하지 않는다.
            # 이 역할의 근거는 앱 도구 결과다. 지식카드 0건이라고 "사내 지식카드에 없다"를 먼저
            # 밝히게 하면, 도구로 답할 질문에 엉뚱한 단서가 붙는다.
            # 위 [중요] 와 반대로 말하지 않는다 — 모는 앱이 하나도 안 붙었으면 '직접 호출해 답하라' 를 싣지 않는다.
            if op_apps and not _op_ok:
                turn_notes += (f"\n\n[HE팀 MCP 운영자 — 사용자가 선택]\n너는 '{agent_key}' 운영자다. 모는 앱이 이 서버에 "
                               f"연결돼 있지 않으니 아래 사전 지식으로 답할 수 있는 범위만 답하라.\n{persona.get('role') or ''}")
                yield _sse("status", {"step": f"{agent_key} — 모는 앱이 연결돼 있지 않아 도구 없이 답합니다", "tool": None})
            else:
                _opl = ", ".join(_app_label(a) for a in _op_ok) or "지정 앱"
                turn_notes += (f"\n\n[HE팀 MCP 운영자 — 사용자가 선택]\n너는 '{agent_key}' 운영자다. 아래 사전 지식과 "
                               f"작업 순서를 따라 {_opl} 의 도구를 직접 호출해 답하라.\n{persona.get('role') or ''}")
                yield _sse("status", {"step": f"{agent_key} — {_opl} 도구로 답합니다", "tool": None})
        elif agent_key:
            role = persona.get("role") or ""
            if role:
                turn_notes += (f"\n\n[전문가 페르소나 — 사용자가 선택]\n너는 '{agent_key}' 전문가다. "
                               f"아래 역할과 범위를 지켜 그 전문가로서 답하라.\n{role}\n"
                               f"이 전문가 도메인의 데이터 조회는 agent_search(\"{agent_key}\", 질문) 를 우선 사용하라.")
            else:
                # 역할이 비면 종전에는 **조용히** 건너뛰었다. 사용자는 전문가를 골라 놓고
                # 일반 답을 받으면서 그 사실을 모른다 — 바로 아래 지식카드 경로가 이미
                # '없다' 와 '못 물어봤다' 를 갈라 말하는데 정작 역할은 안 그러고 있었다.
                _pnote = persona.get("note") or ""
                if _pnote:
                    turn_notes += (f"\n\n[전문가 페르소나]\n'{agent_key}' 의 역할 문서를 불러오지 "
                                   f"못했다. 일반 지식으로 답하되 '이 전문가의 역할 문서를 "
                                   f"불러오지 못했다'고 먼저 밝혀라.")
                    yield _sse("warning", {"code": "persona_load_failed",
                                           "message": f"{agent_key} 역할 문서를 불러오지 못했습니다 — "
                                                      f"{_pnote}. 이번 답변은 그 전문가로서가 아닙니다."})
                    yield _sse("status", {"step": f"역할 문서 조회 실패 — {_pnote}", "tool": None})
                else:
                    yield _sse("warning", {"code": "persona_role_empty",
                                           "message": f"{agent_key} 에 등록된 역할 문서가 비어 있습니다 "
                                                      f"— 전문가 말투·범위가 적용되지 않습니다."})
                    yield _sse("status", {"step": f"{agent_key} 역할 문서 없음", "tool": None})
            # 그 전문가의 지식카드를 코드가 미리 조회해 붙인다. 위 지시만으로는 모델이 안 부르면
            # 그만이고, 사용자에겐 '이 전문가는 아는 게 없다'로만 보인다. 질의는 도구 선별과 같은
            # _sel_q(최근 히스토리 + 현재 발화) 를 쓴다 — 현재 발화만 넣으면 '그럼 더 자세히' 같은
            # 후속 질의에서 관련도가 무너지는 문제가 도구 선별에서 이미 실측됐다.
            yield _sse("status", {"step": f"{agent_key} 지식카드 검색", "tool": "agent_search"})
            know = await _sc_raw_off(_persona_knowledge(app, req.groups, agent_key, _sel_q))
            if know:
                _sc_keep_raw(know)       # 모델이 본 발췌는 근거다 — 자기검증 관문이 그 수를 지우는 고침을 버린다(검색 원문은 넣지 않는다)
                turn_notes += (f"\n\n[{agent_key} 지식카드 — 이번 질문 연관 발췌]\n{know}\n"
                               f"이 발췌는 그 전문가가 실제로 보유한 사내 지식이다. 답변에 해당하는 내용이 "
                               f"있으면 반드시 근거로 인용하되, 발췌에 없는 것을 있는 것처럼 말하지 마라. "
                               f"더 필요하면 agent_search(\"{agent_key}\", 구체적 질의) 로 추가 조회하라.")
                yield _sse("status", {"step": f"지식카드 {len(know):,}자 주입", "tool": None})
            else:
                # '없다' 와 '못 물어봤다' 를 구분해 말한다. 검색이 늦거나 죽어서 못 받은 것을
                # '보유 지식 없음' 으로 말하면, 그 전문가가 아는 게 없다는 거짓이 사용자에게 간다.
                _note = getattr(_persona_knowledge, "last_note", "")
                if _note:
                    turn_notes += (f"\n\n[{agent_key} 지식카드]\n지식 조회가 시간 안에 끝나지 않아 "
                                   f"이번 답변에는 사내 발췌가 없다. 일반 지식으로 답하되 "
                                   f"'사내 지식카드를 조회하지 못했다(없다는 뜻이 아니다)'고 먼저 밝혀라.")
                    yield _sse("warning", {"code": "knowledge_degraded",
                                           "message": f"{agent_key} 지식카드를 조회하지 못했습니다 — {_note}. "
                                                      "보유 지식이 없다는 뜻이 아닙니다."})
                    yield _sse("status", {"step": f"지식카드 조회 실패 — {_note}", "tool": None})
                else:
                    # 0히트를 침묵하면 모델이 일반 지식으로 메우고 사용자는 그것을 사내 지식으로 읽는다.
                    turn_notes += (f"\n\n[{agent_key} 지식카드]\n이번 질문과 연관된 보유 지식을 찾지 못했다. "
                                   f"일반 지식으로 답하되 '사내 지식카드에는 관련 내용이 없다'고 먼저 밝혀라.")
                    yield _sse("status", {"step": "연관 지식카드 없음 — 일반 지식으로 답변", "tool": None})
        # 보조 전문가 — 목소리는 주 전문가 하나다. 보조는 판단 기준(역할)과 도구를 빌려줄 뿐이고,
        # 각자 따로 답하지 않는다(그건 Thinking 이고, 갈린 의견을 세우는 자리는 심의다).
        # ⚠ 갈리는 것을 한 목소리가 숨기면 '거짓 합의'가 된다 — 갈리면 한 줄로 밝히게 한다.
        if helpers:
            _blocks = []
            for _k, _m in helpers:
                # 보조 역할 — 좁은 창은 종전 상한(스키마 예산을 밀어내지 않게), 넓은 창은 명시값이 없으면 전문.
                _r = _cut_mark(_m.get("role") or "",
                               PERSONA_HELPER_ROLE_MAX if (not _wide() or _PERSONA_HELPER_SET)
                               else 0)
                _tag = "MCP 운영자" if _m.get("operator") else "도메인 전문가"
                _apps_note = ""
                if _m.get("operator") and _m.get("apps"):
                    # 입구 도구만 묶여 있다 — 나머지는 안내대(search_tools·invoke_tool)로 닿는다고
                    # 명시한다. 안 그러면 '도구가 없다'고 판단해 일반 지식으로 답한다.
                    _apps_note = (f" (도구: {', '.join(_app_label(a) for a in _m['apps'])}"
                                  " — 입구 도구만 묶여 있다. 더 필요하면 search_tools 로 찾아 "
                                  "invoke_tool 로 호출하라)")
                _blocks.append(f"- {_k} · {_tag}{_apps_note}\n{_r}" if _r else f"- {_k} · {_tag}{_apps_note}")
            turn_notes += (
                "\n\n[함께 선택된 보조 전문가 — 렌즈와 도구만 빌려준다]\n"
                + "\n".join(_blocks)
                + f"\n\n답은 '{agent_key}' 한 사람의 목소리로 쓴다. 보조 전문가의 이름으로 따로 말하지 말고, "
                  "그들의 판단 기준과 도구를 네 판단에 녹여라. 보조 전문가의 사내 지식이 필요하면 "
                  "agent_search(\"<그 전문가 키>\", 질의) 로 직접 조회하라.\n"
                  "⚠ 관점이 실제로 갈리면 숨기지 마라 — 답 끝에 '이견: <무엇이 갈리는지>' 한 줄을 붙이고, "
                  "판단이 갈려 결론을 못 내면 '/심의 <질문>' 으로 정식 심의를 권하라.")
            yield _sse("status", {"step": f"주 전문가 {agent_key} + 보조 {len(helpers)}명으로 답합니다",
                                  "tool": None})
        # 붙인 문서는 **turn_notes 에** 싣는다 — 아래 세 갈래(첫 호출·오류 재시도·강제 도구호출)가
        # 모두 _turn_msgs 로 메시지를 지으므로, user 메시지에 붙이면 재시도 경로에서 조용히 빠진다.
        # ⚠ 순서가 중요하다. 문서 예산은 '컨텍스트 − 예비분 − 이력' 인데, 여기서 빼야 할 이력은
        # 원본이 아니라 **압축 뒤 실제로 실리는 것**이다. 원본을 빼면 긴 대화에서 문서가 자리를
        # 통째로 잃는다(실측 128K 창에서 문서 몫이 1,502자까지 떨어졌다).
        _hstat: dict = {}
        _hist_msgs = _history_messages(req.history, _hstat, has_docs=bool(req.documents))
        if _hstat.get("compacted"):
            yield _sse("status", {"step": f"이전 대화 {_hstat['compacted']}턴은 길이 때문에 "
                                          f"발췌로 압축해 넣었습니다(최근 {_hstat['kept']}턴은 원문)",
                                  "tool": None})
        if _hstat.get("evidence") or _hstat.get("evidence_cut") or _hstat.get("evidence_names"):
            # 앞 턴의 조회 근거를 얼마나 실었나 — 줄이거나 이름만 남긴 턴이 있으면 그 수를 같이 말한다(조용히 빼지 않는다).
            _ev_parts = [f"{_hstat['evidence']}턴 원문"] if _hstat.get("evidence") else []
            if _hstat.get("evidence_cut"):
                _ev_parts.append(f"{_hstat['evidence_cut']}턴 앞부분만")
            if _hstat.get("evidence_names"):
                _ev_parts.append(f"{_hstat['evidence_names']}턴은 이력 예산 밖이라 도구 이름만")
            yield _sse("status", {"step": "앞 턴에서 조회한 근거를 이력에 실었습니다 — " + " · ".join(_ev_parts),
                                  "tool": None})
        if _hstat.get("cut"):
            # 줄인 것이 모델의 답이면 '나눠서 다시 보내 주세요' 는 틀린 말이다 — 사용자가 보낸 글이 아니다.
            yield _sse("warning", {"code": "history_item_cut",
                                   "message": (f"직전 답이 {_hstat['cut']:,}자라 모델 창에 맞추느라 직전 답의 앞부분만 "
                                               "이력에 실었습니다 — 그 답의 뒷부분은 이번 턴의 모델이 보지 못합니다."
                                               if _hstat.get("cut_role") == "assistant" else
                                               f"가장 최근 대화 항목이 {_hstat['cut']:,}자라 이력 예산"
                                               f"({_hstat['budget']:,}자)을 넘어 앞부분만 실었습니다 — 뒷부분이 "
                                               "필요하면 나눠서 다시 보내 주세요."),
                                   "knob": "HIST_CTX_SHARE_SOLO·HIST_CTX_SHARE(이력 몫)"})
        # 글 전체로 잰다 — 앞 20,000자 표본(_est_tokens)은 압축본·영문 로그가 머리인 한국어 이력을 3분의 1 로 센다.
        _hist_tokens = _est_tokens_all("".join(c for _, c in _hist_msgs))
        _docs = _doc_block(req.documents, _hist_tokens)
        if _docs:
            turn_notes += _docs
            yield _sse("status", {"step": _doc_status(_docs, min(len(req.documents), DOC_MAX_FILES),
                                                      _hist_tokens), "tool": None})
        # 그림 — 올린 그림은 아티팩트로 저장해 주소를 화면에 돌려준다(이력에 다시 실린다). 챗 모델이 그림을 받으면 이 발화의 그림 조각으로,
        # 앞 대화의 그림(사람이 올린 것 · 도구가 낸 것)은 최근 VISION_HISTORY_IMAGES 장을 '앞 대화의 그림' 으로 다시 싣는다. 못 받으면
        # 판독문(그림을 받는 다른 모델)으로, 그것도 없으면 못 실었다고 말한다(HWAXPortal docs/chat-depth-context T5).
        _turn_vision = (await asyncio.to_thread(_chat_vision, app)
                        if (req.images or _vision.history_images(req.history)) else _chat_vision_known(app))
        _user_msg, _recap = await _turn_image_messages(app, req, _turn_vision)
        async for _ev in _drain_image_events(_turn_image_events):
            yield _ev
        # 확인 카드(HA-29) — 앞 카드를 승인했으면 서버가 그 호출을 처음 꼴 그대로 한 번 보낸다(모델 재현에 기대지 않는다 — 칸 하나 차이로 카드가
        # 되풀이되거나 아예 안 불러 단추가 헛돈다). 승인 · 거절의 결과는 이 턴의 안내로 싣는다.
        if _cctx.approved:
            async for _ev in _confirm_send(app, req, _cctx, turn_calls, turn_out):
                yield _ev
        if _cctx.note:
            turn_notes += "\n\n" + _cctx.note
        messages = _turn_msgs(sys_prompt, turn_notes, _hist_msgs, _recap, _user_msg)
        inputs = {"messages": messages}
        # 호출 예산 — 작은 모델은 같은 도구를 같은 인자로 반복 호출하다 그래프 재귀 한도에
        # 부딪히고, 그러면 그때까지 스트리밍된 내용까지 버려진 채 '응답 생성 실패'만 남는다
        # (감사 확인). 모델의 자제력에 기대지 말고 코드가 상한을 건다.
        # blocked: 유령 ID · 다른 허브 id 게이트 · 확인 관문이 실행을 막은 호출 수 — 실효 호출 수에서 뺀다. calls 는 서버가 승인으로 보낸 수부터 센다 — 안 세면
        # 모델이 결과만 알리고 끝난 턴이 '도구 0회' 로 읽혀 강제 재시도 · 확정 종결이 돈다.
        _budget = {"calls": _cctx.sent, "blocked": 0, "seen": {}}
        _loop_fit_told = 0      # 루프 입력 맞추기가 줄였다고 알린 도구 결과 수 — 늘 때만 다시 알린다(호출마다 원문에서 다시 잰다)
        _cfg = {"recursion_limit": AGENT_RECURSION_LIMIT,
                # 도구 그림을 그 결과 뒤에 실을지 — 프롬프트 갈고리(_loop_fit_prompt)가 읽는다(문맥 변수는 LangGraph 실행 맥락을 넘지 못한다).
                # 값은 셋 — True · False · None(아직 모름 — 도구가 그림을 낸 호출에서 묻는다).
                "configurable": {"hwax_vision": locals().get("_turn_vision")}}
        # 조각을 흘린 모델 호출 — 그 호출의 끝 이벤트에서는 글을 다시 내지 않는다. 조각 없이 끝난 호출은 끝 이벤트의 글을 낸다:
        # 챗 전용 LLM 은 스트리밍인데 넘겨받은 VLLM_* 가 비스트리밍(GLM 우회)이면, 종전처럼 서버 전체 값(llm_nostream) 하나만
        # 보면 넘겨받은 답의 글이 화면에 안 나갔다(docs/chat-llm-stcx).
        _streamed_runs: set = set()
        # 확인 관문(HA-29)이 이 턴의 호출을 막았나 — 막았으면 그 단계(나란한 도구)가 끝난 뒤 다음 모델 호출 앞에서 흐름을 끊는다. 모델이 '걸었다' 고
        # 지어내거나 다른 잡을 이어 거는 길을 닫는다. 막은 호출은 조회한 것으로 남기지 않는다(호출 → turn_calls 의 그 항목).
        _hold = False
        _held_at: dict = {}
        _evs = _confirm_events(_timed_events(agent.astream_events(inputs, version="v2", config=_cfg)))
        async for event in _evs:
            kind = event["event"]
            if kind == "on_chat_model_start":
                if _hold:
                    break
                # 이 턴의 호출마다 답한 모델 — 넘김(챗 서버 → VLLM_*)이 일어나면 같은 질문에 답의 깊이가 달라지므로 메시지마다 보인다(C3).
                _mn = str((event.get("metadata") or {}).get("ls_model_name") or event.get("name") or "?")
                _turn_models[_mn] = _turn_models.get(_mn, 0) + 1
            elif kind == "on_chat_model_stream":
                _streamed_runs.add(event.get("run_id"))
                _rc = (getattr(event["data"]["chunk"], "additional_kwargs", None) or {}).get("reasoning_content")
                if _rc:
                    yield _sse("reasoning", {"delta": _rc})
                token = event["data"]["chunk"].content
                if token:  # empty on tool-call delta chunks — guard
                    full.append(token)
                    _post_tool_chars += len(token if isinstance(token, str) else str(token))
                    yield _sse("token", {"delta": token})
            elif kind == "on_chat_model_end":
                out_msg = event.get("data", {}).get("output")
                # 출력 상한(max_tokens)에서 끊긴 응답 — 챗 루프는 finish_reason 을 한 번도 읽지 않아, 답이 문장 중간에서
                # 끊겨도 정상 답과 똑같이 끝났다(심의 _llm_text 에는 이 검출이 있다). 운영은 max_tokens 를 안 보내
                # 엔드포인트 기본 출력 상한이 걸린다. 끊겼으면 화면과 대화 기록에 남긴다.
                if _finish_reason(out_msg) == "length":
                    yield _sse("warning", {"code": "output_cut",
                                           "message": "답이 모델 출력 상한에서 끊겼습니다 — 뒷부분이 빠졌을 수 "
                                                      "있습니다. 범위를 나눠 다시 물어 주세요.",
                                           "knob": "LLM_MAX_TOKENS(미설정이면 엔드포인트 기본 출력 상한)"})
                if getattr(app.state, "llm_nostream", False) or event.get("run_id") not in _streamed_runs:
                    # 비스트리밍 모드(또는 이 호출만 조각 없이 끝났다) — stream 이벤트가 없으므로 완결 응답에서 텍스트를 회수해
                    # 모델 턴 단위로 방출한다(도구 호출만 한 턴은 content 가 비어 자연히 skip). 생각 글도 그 호출이 끝난 뒤 한 번에.
                    _rc = (getattr(out_msg, "additional_kwargs", None) or {}).get("reasoning_content")
                    if _rc:
                        yield _sse("reasoning", {"delta": _rc})
                    content = getattr(out_msg, "content", "") or ""
                    if isinstance(content, list):  # 멀티파트 content 방어
                        content = "".join(
                            p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
                    if content:
                        full.append(content)
                        _post_tool_chars += len(content)
                        yield _sse("token", {"delta": content})
            elif kind == "on_custom_event" and event.get("name") == _LOOP_FIT_EVENT:
                # 루프 입력 맞추기(_loop_fit_prompt)가 창을 넘는 도구 결과를 줄였다 — 모델만 알고 화면·대화 기록엔 없던 일이다.
                _fit = event.get("data") or {}
                if _fit.get("n", 0) > _loop_fit_told:
                    _loop_fit_told = _fit["n"]
                    yield _sse("warning", {"code": "loop_results_cut",
                                           "message": f"이번 턴에 받은 도구 결과가 모델 창을 넘어, 결과 {_fit['n']}건"
                                                      f"(오래된 것부터 · 원문 {_fit.get('chars', 0):,}자)을 앞부분만 남기고 줄여 모델에 "
                                                      "실었습니다 — 그 결과의 뒷부분은 모델이 보지 못했습니다. 조건을 좁혀 "
                                                      "나눠 조회하면 다 볼 수 있습니다.",
                                           "knob": "TOOL_RESULT_MAX"})
            elif kind == "on_custom_event" and event.get("name") == _status_wait_mod.EVENT:
                # 상태 도구의 코드 기다림(HA-28) — 도구가 도는 동안 진행 줄을 흘린다. tool 은 비운다(서버 사본 활동 · 다음 턴 근거에 섞이지 않게).
                yield _sse("status", _status_wait_mod.frame(event.get("data") or {}))
            elif kind == "on_tool_start":
                _budget["calls"] += 1
                _fp = f"{event.get('name')}|{json.dumps(event.get('data', {}).get('input'), sort_keys=True, ensure_ascii=False, default=str)[:400]}"
                _budget["seen"][_fp] = _budget["seen"].get(_fp, 0) + 1
                if (_budget["seen"][_fp] == TOOL_REPEAT_WARN
                        and not _status_tool(_inner_tool(str(event.get("name") or ""), event.get("data", {}).get("input"))[0])):
                    # 같은 도구·같은 인자 반복 — 더 해도 결과가 달라지지 않는다. 상태 도구는 결과가 바뀐다 — 경고하지 않는다(HA-28).
                    yield _sse("status", {"step": f"같은 호출 반복 감지({event.get('name')}) — 결과가 바뀌지 않습니다",
                                          "tool": event.get("name")})
                _in = event.get("data", {}).get("input")
                # 소요는 **시작을 기억해야** 잴 수 있다. 짝 키로 기억한다 — 같은 도구를
                # N번 부르면 이름으로는 못 잰다(§5-5 가 그 기계다).
                if event.get("run_id"):
                    _t0_remember(str(event["run_id"]))
                # 경유 껍데기를 벗긴다 — 안 벗기면 기록이 전부 `invoke_tool` 이 된다
                _tname, _inner_args = _inner_tool(str(event.get("name") or ""), _in)
                if _inner_args is not None:
                    _in = _inner_args
                args = _tool_preview(_in)
                # 절차 도출용 날것 — 220자 미리보기로 만든 절차는 **인자가 손상돼 있다**
                # (적층 정의·시나리오 같은 큰 인자는 통째로 잘린다). 결과에 result_full 을
                # 두는 것과 같은 이유이고 같은 상한을 쓴다. 표시용보다 길 때만 싣는다.
                _args_full = _handoff_text(_in)
                turn_calls.append((_tname or "?", args or ""))
                if event.get("run_id"):
                    _held_at[str(event["run_id"])] = turn_calls[-1]
                # `run_id` 는 **같은 호출의 시작·완료에 같은 값**으로 온다. 이게 없어서
                # 인자와 결과를 짝지을 수 없었고, 화면은 도구 **이름**으로 묶어 같은 도구
                # N번 호출이 한 줄로 합쳐졌다(§5-5 predict_sed 다섯 번).
                yield _sse("status", {"step": f"도구 호출: {_tname}", "tool": _tname,
                                      **({"via": "invoke_tool"} if _inner_args is not None else {}),
                                      **({"call": str(event["run_id"])} if event.get("run_id") else {}),
                                      **({"detail": args} if args else {}),
                                      **({"detail_full": _args_full}
                                         if len(_args_full) > len(args) else {})})
            elif kind == "on_tool_end":
                # 도구 출력에 실린 아티팩트 URL 수집 — contextvar 는 LangGraph 실행 컨텍스트를
                # 넘지 못해(실측 빈 값) 스트림 쪽에서 직접 회수한다.
                _raw = event.get("data", {}).get("output")
                _txt = getattr(_raw, "content", _raw)
                if _confirm_held_text(_txt):
                    # 확인 관문(HA-29)이 막은 호출 — 보내지 않았다. '도구를 썼다' 로 세지 않고(blocked), 근거 블록 · 자기검증 · 다음 턴 출처에
                    # '조회한 것' 으로 남기지 않는다(turn_calls 에서 빼고 turn_out 에 넣지 않는다). '도구 완료' 대신 '확인 대기' 로 알린다 —
                    # 화면에 '도는 중' 으로 남지 않게. 카드는 턴 끝에 선다.
                    _budget["blocked"] += 1
                    _hold = True
                    _cidh = str(event.get("run_id") or "")
                    _t0_by_call.pop(_cidh, None)
                    _hc = _held_at.pop(_cidh, None)
                    if _hc in turn_calls:
                        turn_calls.remove(_hc)
                    _tnh, _ivh = _inner_tool(str(event.get("name") or ""), (event.get("data") or {}).get("input"))
                    yield _sse("status", {"step": f"확인 대기: {_tnh} — 사람의 확인 전에는 보내지 않습니다", "tool": _tnh,
                                          **({"via": "invoke_tool"} if _ivh is not None else {}), "ok": False,
                                          "ts": int(time.time() * 1000), **({"call": _cidh} if _cidh else {}),
                                          "result_preview": _tool_preview(_raw)})
                    continue
                if isinstance(_txt, str):
                    # 수치 대조용 원문 적재 — 좁은 창은 앞부분·총량을 묶고, 넓은 창은 모델이 본 그대로 싣는다(_turn_out_add).
                    _turn_out_add(turn_out, _txt)
                    # 도구 결과를 상한(TOOL_RESULT_MAX)에서 잘랐으면 화면·대화 기록에도 남긴다 — 종전엔 모델만 알았다.
                    if _CAP_MARK in _txt:
                        _cm = re.search(r"출력이 ([\d,]+)자였고 ([\d,]+)자에서", _txt)
                        yield _sse("status", {"step": f"도구 결과 상한 — {event.get('name')} 결과 "
                                                      + (f"{_cm.group(1)}자 중 앞 {_cm.group(2)}자만"
                                                         if _cm else "일부만")
                                                      + " 모델에 실었다(조건을 좁혀 다시 조회하게 했다)",
                                              "tool": event.get("name"), "knob": "TOOL_RESULT_MAX"})
                    for _u in re.findall(r"/agent/artifacts/[A-Za-z0-9][A-Za-z0-9_.-]*", _txt):
                        if _u not in turn_imgs:
                            turn_imgs.append(_u)
                    # 게이트에 막힌 호출은 '도구를 썼다'로 세면 안 된다. 세면 그 턴이 no-tool 로
                    # 잡히지 않아 강제 재시도가 발동하지 않고, 모델이 "list_materials 를 부르겠다"
                    # 고 말만 하고 끝난다(실측 Q3: 유일한 호출이 차단된 plot_curve 였다).
                    # 다른 허브 id 막이(HA-30)도 같다 — 표지가 달라 글 머리 400자에서 따로 찾는다(design-integration C-6).
                    if _PHANTOM_ID_MARK in _txt or _XHUB_ID_MARK in _txt[:400]:
                        _budget["blocked"] += 1
                out = _tool_preview(_raw)
                # 심의 핸드오프용 날것 — 표시용보다 길 때만 싣는다(짧은 결과에 중복 페이로드 금지).
                # ⚠ 변수명 주의 — `full` 은 1701행의 **토큰 누적 리스트**다. 처음에 `full =` 로
                #   덮어썼다가 다음 토큰의 full.append 가 AttributeError 로 챗을 죽였다(감사 C35).
                _hand = _handoff_text(_raw)
                _post_tool_chars = 0   # 이 결과 뒤에 모델이 말을 했는지만 본다
                _ok = _result_ok(_txt)
                # 짝이 맞으려면 완료도 **같은 이름**이어야 한다 — 한쪽만 안쪽 이름이면
                # 화면과 원장이 두 줄로 갈라진다.
                _tname2, _iv = _inner_tool(str(event.get("name") or ""),
                                           (event.get("data") or {}).get("input"))
                if CHAT_CONFIRM_GATE and _confirm_class(_tname2)[0]:
                    _cctx.done.append((_tname2, out[:160]))      # 이 턴에 나간 잡 · 쓰기 — 카드가 서면 카드 글이 밝힌다(HA-29)
                _cid2 = str(event.get("run_id") or "")
                _t0 = _t0_by_call.pop(_cid2, None)
                yield _sse("status", {"step": f"도구 완료: {_tname2}", "tool": _tname2,
                                      **({"via": "invoke_tool"} if _iv is not None else {}),
                                      "ok": _ok, "ts": int(time.time() * 1000),
                                      **({"ms": round((time.monotonic() - _t0) * 1000)}
                                         if _t0 is not None else {}),
                                      **({"call": str(event["run_id"])} if event.get("run_id") else {}),
                                      **({"result_preview": out} if out else {}),
                                      **({"result_full": _hand} if len(_hand) > len(out) else {})})
            elif kind == "on_tool_error":
                # ⚠ LangChain 은 도구가 **터지면** on_tool_end 대신 이것을 낸다. 아무도
                # 안 받는 동안 그 호출은 완료 이벤트가 아예 안 나갔다 — 화면에는 인자만
                # 있고 결과도 성패도 없어 **아직 도는 중과 똑같이** 보였고, 절차 원장은
                # 결과 없음·ok 없음을 성공으로 접어 넣었다(from_chat 의 `ok is not False`).
                # 시작 시각도 안 꺼내져 그것만 상한에 밀려 버려졌다.
                _err = (event.get("data") or {}).get("error")
                _tname3, _iv3 = _inner_tool(str(event.get("name") or ""),
                                            (event.get("data") or {}).get("input"))
                _cid3 = str(event.get("run_id") or "")
                _t03 = _t0_by_call.pop(_cid3, None)
                _emsg = f"{_TOOL_FAIL_MARK} 도구 {_tname3} 호출 실패: {str(_err)[:500]}"
                _post_tool_chars = 0
                yield _sse("status", {"step": f"도구 실패: {_tname3}", "tool": _tname3,
                                      **({"via": "invoke_tool"} if _iv3 is not None else {}),
                                      "ok": False, "ts": int(time.time() * 1000),
                                      **({"ms": round((time.monotonic() - _t03) * 1000)}
                                         if _t03 is not None else {}),
                                      **({"call": _cid3} if _cid3 else {}),
                                      "result_preview": _tool_preview(_emsg)})
        if _hold:
            await _confirm_close(_evs)      # 끊었다 — 막 시작한 모델 호출은 접힌다(같은 단계의 나란한 도구 결과는 이미 받았다)
    except Exception as exc:
        if _cctx.pending:
            # 막은 호출이 있는 턴(HA-29) — 자동 재시도 · 오류 문구 대신 카드로 끝낸다. 재시도는 그래프를 처음부터 다시 돌린다(흐름의 오류는
            # _confirm_events 가 이미 삼킨다 — 여기는 그 밖에서 난 오류다). 근거 블록은 감싸개가 있으면 감싸개가 붙인다.
            print(f"[confirm] 막은 호출이 있는 턴의 오류 — 재시도 대신 카드로 끝낸다: {exc!r}"[:300], flush=True)
            text = "".join(full)
            async for _fr in _confirm_card(app, req, _cctx):
                yield _fr
            text += _cctx.card
            if box is not None:
                box.update(draft=text, calls=list(turn_calls), outputs=list(turn_out), confirm=_cctx.card)
            else:
                yield _sse("token", {"delta": _cctx.card})
            yield _sse("result", {"type": "text", "content": text})
            yield _sse("done", {})
            return
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
        # 스트리밍 청크 한도에 걸린 것이면 '내부 오류' 가 아니라 그 한도를 말한다 — 곧바로 다시 보내면 밀린 LLM 에
        # 한 건을 더 얹을 뿐이다. '응답이 여기서 중단되었습니다' 는 그대로 둔다(_INTERRUPTED_RE 가 끊긴 턴을 이 글로 안다).
        _chunk_why = _stream_chunk_limit_note(exc)
        # 그래프 재귀 한도 — 일시 장애가 아니다. 같은 한도로 다시 부르면 같은 도구를 처음부터 다시 실행하고(부작용 있는 도구도)
        # 같은 자리에서 또 걸린다. 그래서 자동 재시도하지 않고, 끊긴 턴 표지(_INTERRUPTED_RE)도 달지 않는다 — '계속' 재촉이
        # 원래 질문을 처음부터 다시 돌려 같은 도구를 또 실행한다(m1 3차).
        _loop_cap = (f"한 턴의 도구 왕복 상한(AGENT_RECURSION_LIMIT={AGENT_RECURSION_LIMIT} — 연속 왕복 약 "
                     f"{max(0, (AGENT_RECURSION_LIMIT - 1) // 2)}회)에 걸려 답을 끝내지 못했습니다{detail}. 같은 질문을 그대로 "
                     "다시 보내면 같은 도구를 처음부터 다시 실행합니다 — 범위를 나눠 물어 주세요."
                     if isinstance(exc, GraphRecursionError) else "")
        if _partial:
            _note = (f"\n\n⚠ {_loop_cap}" if _loop_cap else
                     (f"\n\n⚠ {_chunk_why}. 응답이 여기서 중단되었습니다{detail}. "
                      "잠시 후 같은 질문을 다시 보내 주세요 — 질문을 바꿔도 해결되지 않습니다.") if _chunk_why else
                     ("\n\n⚠ 처리 중 내부 오류로 응답이 여기서 중단되었습니다"
                      f"{detail}. 같은 질문을 다시 보내면 재시도합니다."))
            yield _sse("token", {"delta": _note})
            yield _sse("result", {"type": "text", "content": _partial + _note})
        elif _loop_cap:
            yield _sse("error", {"code": "tool_loop_limit", "message": _loop_cap})
        else:
            # 부분 응답조차 없으면 사용자는 오류 한 줄만 받는다. 일시적 실패(스트림 끊김·
            # 상류 5xx·파서 오류)가 대부분이라 한 번은 자동으로 다시 해 본다 — 사용자가
            # "야" 하고 재촉해야 다시 도는 것은 시스템이 할 일을 사람에게 미루는 것이다.
            # 스트리밍이 아니라 단발 호출로 간다(스트림 경로가 방금 실패한 그 경로다).
            _rescued = ""
            # 즉시 1회로는 순간 장애를 못 넘긴다. 상류가 잠깐 끊긴 경우가 대부분이라
            # 짧게 쉬었다 다시 한다. 간격 없이 붙이면 같은 실패를 그대로 다시 받는다.
            _tries = max(0, int(os.environ.get("CHAT_AUTO_RETRY", "2") or 0))
            # 안내를 발화 머리에 실었는데도 시스템 자리 400 이면(HA-26) 그 뒤의 재시도는 이 턴 끝까지 안내를 첫 시스템 메시지에 합친다.
            # exc 는 늘 마지막 실패(첫 시도 · 앞 재시도)라 시도마다 본다. 한 번 합치면 다른 오류가 끼어도 되돌리지 않는다.
            _merged = False
            for _attempt in range(1, _tries + 1):
                if not _merged and _SYS_NOT_FIRST in str(exc):
                    _merged = True
                    print(f"[agent] '{_SYS_NOT_FIRST}' 400 — 남은 재시도는 이 턴의 안내를 첫 시스템 메시지에 합쳐 보낸다(HA-26)")
                # 첫 재시도는 왜 다시 받는지 말한다 — 길게는 LLM_TIMEOUT_S × 시도 횟수를 기다리는 동안 화면에 뜨는
                # 것이 이 줄뿐이다.
                yield _sse("status", {"step": (f"{_chunk_why} — 스트리밍 없이 다시 받습니다 · "
                                               if _chunk_why and _attempt == 1 else "오류 발생 — ")
                                              + f"자동 재시도 {_attempt}/{_tries}",
                                      "tool": None})
                if _attempt > 1:
                    await asyncio.sleep(min(4.0, 1.5 * (_attempt - 1)))
                try:
                    _msgs = _turn_msgs(sys_prompt, locals().get("turn_notes") or "",
                                       _history_messages(req.history, has_docs=bool(req.documents)),
                                       locals().get("_recap"), locals().get("_user_msg") or ("user", req.message),
                                       merged=_merged)
                    # _cfg 는 try 안에서 정의되므로 그 전에 터진 예외에서는 없다.
                    _cfg4 = locals().get("_cfg") or {"recursion_limit": AGENT_RECURSION_LIMIT}
                    _status_wait_retry()
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
            if _cctx.pending:
                # 재시도(agent.ainvoke) 안에서 확인 관문(HA-29)이 막았다 — 건진 글(있으면)과 카드로 끝낸다. 종전엔 재시도가 건진 글을 그대로 내고
                # 끝나 카드 사건 · 본문 카드 글 · 저장분이 모두 없었다(모델은 '확인 카드를 띄웠다' 고 답하는데 승인할 길이 없었다 — 반대 검토 2차).
                print(f"[confirm] 자동 재시도 안에서 막은 호출 — 재시도 끝에 카드를 낸다(건진 글 {'있음' if _rescued else '없음'})", flush=True)
                if _rescued:
                    yield _sse("token", {"delta": _rescued})
                async for _fr in _confirm_card(app, req, _cctx):
                    yield _fr
                text = (_rescued or "") + _cctx.card
                if box is not None:
                    box.update(draft=text, calls=list(turn_calls), outputs=list(turn_out), confirm=_cctx.card)
                else:
                    yield _sse("token", {"delta": _cctx.card})
                yield _sse("result", {"type": "text", "content": text})
            elif _rescued:
                print(f"[agent] auto-retry 성공 — 오류 턴 복구(시도 {_attempt}회)")
                yield _sse("token", {"delta": _rescued})
                yield _sse("result", {"type": "text", "content": _rescued})
            else:
                # 원인을 뭉뚱그리지 않는다. LLM 미도달은 '에이전트 오류' 가 아니라 인프라
                # 문제이고, 사용자가 질문을 바꿔도 해결되지 않는다 — 그렇게 말해 줘야 한다.
                _kind = type(exc).__name__
                # 시간 초과는 '연결하지 못했다' 가 아니다 — 붙었는데 답이 한도 안에 안 온 것이다. 심의가 공유
                # LLM 을 차지한 동안 챗이 이렇게 끝나는데, 종전 문구로는 LLM 서버가 내려간 줄 알았다.
                # 연결 시간 초과도 SDK 가 APITimeoutError 로 올려서 원인 사슬의 httpx 예외로 가른다.
                _chain, _e = [], exc
                while _e is not None and len(_chain) < 6:
                    _chain.append(type(_e).__name__)
                    _e = _e.__cause__ or _e.__context__
                # 재시도까지 돈 뒤면 exc 는 마지막 재시도의 실패다 — 여기서 청크 한도가 잡히는 것은 재시도를 끈
                # 박스(CHAT_AUTO_RETRY=0)뿐이다.
                _chunk_last = _stream_chunk_limit_note(exc)
                if _chunk_last:
                    _msg = (f"{_chunk_last}. 잠시 후 다시 시도해 주세요 — "
                            f"질문을 바꿔도 해결되지 않습니다.{detail}")
                elif "APITimeout" in _kind and not any("Connect" in _n for _n in _chain):
                    _read, _n_try = _llm_limit(getattr(app.state, "llm", None))
                    _msg = ("LLM 응답이 " + (f"{_read:,.0f}초 안에 " if _read else "제한 시간 안에 ")
                            + "오지 않았습니다(LLM_TIMEOUT_S" + (f" · {_n_try}회 시도" if _n_try else "")
                            + ") — LLM 이 밀려 있거나 멈췄습니다. 잠시 후 다시 시도해 주세요 — "
                            f"질문을 바꿔도 해결되지 않습니다.{detail}")
                elif "APIConnection" in _kind or "APITimeout" in _kind or "Connection" in _kind:
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
    # 아래 구제 길 넷(강제 재시도 · 확정 종결 · 새어 나온 호출 · 빈 답 구제)은 확인 관문이 막은 호출이 있으면 건너뛴다(HA-29) — 그 턴은 카드로
    # 끝난다. 구제 길이 다시 부르면 막은 잡을 또 고르거나, 막은 것을 모델이 '걸었다' 로 요약한다.
    if _no_tool and not _cctx.pending and (_announced_without_calling(text) or _fabricated or _own_chart):
        _why = "날조 표지" if _fabricated else ("자작 표·차트" if _own_chart else "예고만")
        print(f"[agent] 도구 미호출({_why}) — 강제 지시로 1회 재시도")
        yield _sse("status", {"step": "도구를 실제로 호출하도록 다시 시도합니다", "tool": None})
        try:
            _forced = _turn_msgs(sys_prompt, (locals().get("turn_notes") or "") + "\n\n[중요] 도구를 쓰겠다고 예고하지 마라. "
                                 "설명이나 인자 예시를 출력하지 말고, 필요한 도구를 **지금 즉시 호출**하라. "
                                 "인자를 모르면 기본값이나 빈 인자로 호출한 뒤 결과를 보고 판단하라. "
                                 "차트·표를 직접 작성하지 말고 데이터 조회 도구를 먼저 호출하라.",
                                 _history_messages(req.history, has_docs=bool(req.documents)),
                                 locals().get("_recap"), locals().get("_user_msg") or ("user", req.message))
            _status_wait_retry()
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
    if ((_budget["calls"] - _budget["blocked"]) <= 0 and not _cctx.pending
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
                    _chat_llm_of(app), "당신은 도구 호출 계획자입니다. 반드시 유효한 JSON 하나만 출력하세요.",
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
                # 직접 실행 경로에도 같은 계약을 준다 — 짝 지을 키(call)·성패(ok)·표식 절단.
                # 여기만 빠지면 "핀 도구로 돌린 것" 은 절차로 못 편다.
                _cid = f"direct-{_tn}-{len(text)}"
                yield _sse("status", {"step": f"도구 직접 실행: {_tn}", "tool": _tn,
                                      "call": _cid,
                                      "detail": _tool_preview(_argd, 200)})
                try:
                    _out = await _call(_tmap, _tn, _argd)
                except Exception as exc:  # noqa: BLE001
                    _out = f"{_TOOL_FAIL_MARK} (tool {_tn} error: {exc})"
                _out = _cap(str(_out))
                # ⚠ `except` 로는 못 잡는다 — `_call` 이 예외를 문자열로 삼켜 돌려준다.
                # 문자열을 봐야 실패를 안다(그래서 위 try 에서 _ok 를 정하지 않는다).
                _ok = _result_ok(_out)
                _prev3, _hand3 = _tool_preview(_out), _handoff_text(_out)
                yield _sse("status", {"step": f"{'확인 대기' if _confirm_held_text(_out) else '도구 완료'}: {_tn}", "tool": _tn,
                                      "call": _cid, "ok": _ok,
                                      "result_preview": _prev3,
                                      # 핸드오프용 날것 — 종전엔 이 경로만 미리보기 220자뿐이었다
                                      **({"result_full": _hand3} if len(_hand3) > len(_prev3) else {})})
                # 확인 관문(HA-29)이 막은 결과는 LLM 으로 요약하지 않는다(막은 글을 '걸었다' 로 읽는다) — 카드가 턴 끝에 선다
                _held3 = _confirm_held_text(_out)
                if not _held3:
                    try:
                        _final = (await _llm_text(
                            _chat_llm_of(app),
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

    if not _cctx.pending and _looks_like_leaked_tool_call(text, _no_tool):
        print(f"[agent] tool-call leaked into text (도구 실행 {_budget['calls']}회) — 비스트리밍 재시도")
        yield _sse("status", {"step": "도구 호출을 다시 시도합니다", "tool": None})
        try:
            _status_wait_retry()
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
        # 마지막 방어선이다(모델이 구조화 호출을 영영 못 내도 사용자는 결과를 받는다). 재시도 안에서 확인 관문이 막았으면 그 턴은 카드로 끝난다.
        if not _cctx.pending and _looks_like_leaked_tool_call(text, _no_tool):
            calls = _extract_leaked_calls(text, _no_tool)
            if calls:
                try:
                    _tmap = await _tools_by_name(app, req.groups, user=req.user_email, user_pat=req.user_pat)
                except Exception as exc:  # noqa: BLE001
                    print(f"[agent] direct-exec tool load failed: {exc!r}")
                    _tmap = {}
                results, held_names = [], []
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
                    if _confirm_held_text(str(r)):
                        # 확인 관문(HA-29)이 막았다 — 요약에 싣지 않는다(카드가 턴 끝에 선다)
                        yield _sse("status", {"step": f"확인 대기: {c['name']} — 사람의 확인 전에는 보내지 않습니다", "tool": c["name"],
                                              "ok": False})
                        held_names.append(c["name"])
                        continue
                    results.append((c["name"], _cap(str(r))))
                if results:
                    joined = "\n\n".join(f"[{n} 결과]\n{v}" for n, v in results)
                    try:
                        final = (await _llm_text(
                            _chat_llm_of(app),
                            "너는 도구 결과를 사용자에게 한국어로 정리해 주는 조수다. 도구를 다시 "
                            "호출하겠다고 말하지 말고, 아래 결과만으로 답하라.",
                            f"[사용자 질문]\n{req.message}\n\n{joined}")).strip()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[agent] direct-exec summarize failed: {exc!r}")
                        final = joined
                    text = final
                    yield _sse("token", {"delta": "\n\n" + final})
                elif held_names:
                    # 막힌 호출만 있었다 — 모델이 흘린 호출문(<tool_call>{…})을 답 본문 · 서버 사본 · 다음 턴 이력에 남기지 않는다(카드가 뒤에 붙는다 —
                    # 반대 검토 2차). 종전에는 요약 글로 갈아 끼우는 길만 있어 막히면 호출문이 그대로 남았다.
                    text = (", ".join(f"`{n}`" for n in dict.fromkeys(held_names)) + " 를 부르려 했습니다 — 사람의 확인이 필요해 보내지 "
                            "않았습니다.")
                    yield _sse("token", {"delta": "\n\n" + text})
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
    if not _cctx.pending and _needs_final_rescue(turn_calls, text, _post_tool_chars):
        _names = ", ".join(dict.fromkeys(n for n, _ in turn_calls))
        _excerpt = _rescue_excerpt(turn_out)
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
            _status_wait_retry()
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
    if _cctx.pending:
        # 확인 카드(HA-29) — 근거 블록 앞. 감싸개가 있는 길은 카드 글을 초안에만 넣는다(초안은 '생각' 칸으로 가 두 번 보이지 않는다 — 감싸개는
        # 카드 턴을 검토하지 않고 초안을 본문으로 낸다).
        async for _fr in _confirm_card(app, req, _cctx):
            yield _fr
        text += _cctx.card
        if box is None:
            yield _sse("token", {"delta": _cctx.card})
    if box is not None:
        box.update(draft=text, calls=list(turn_calls), outputs=list(turn_out), raw=_turn_sc_raw.get(), confirm=_cctx.card or None)
    # 근거 블록 — 코드가 실행 기록에서 만든다. 모델이 쓰는 게 아니라 지어낼 수 없고,
    # 답변의 수치를 도구 출력 원문과 대조해 출처 없는 값을 함께 표시한다.
    # 사용자 발화도 출처로 인정한다 — 사용자가 준 치수를 되풀이한 것을 날조로 보면 안 된다.
    if turn_calls and os.environ.get("CHAT_EVIDENCE_BLOCK", "1") != "0" and box is None:
        try:
            _src = "\n".join(turn_out) + "\n" + (req.message or "")
            _bad = _unsourced_numbers(_confirm_strip(text, _cctx.card), _src, limit=0)   # 전부 센다 — 블록이 6건만 보이고 총 건수를 적는다 · 카드 글은 빼고(C-14)
            _ev = _evidence_block(turn_calls, _bad)
            if _ev:
                text += _ev
                yield _sse("token", {"delta": _ev})
        except Exception as exc:  # noqa: BLE001 — 근거 표기 실패가 답변을 못 죽인다
            print(f"[agent] evidence block failed: {exc!r}")
    if _turn_models:
        # 답한 모델 · 생각 설정 — 화면이 메시지 아래에 보인다(C3). 챗 서버가 아닌 모델이 섞였으면 넘김이 일어났다.
        _ci = getattr(app.state, "chat_llm_info", None) or {}
        yield _sse("meta", {"models": dict(_turn_models), "chat_model": _ci.get("model"), "thinking": _ci.get("thinking"),
                            "effort": _ci.get("effort"), "fallback_to": (_ci.get("fallback") or {}).get("to")})
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
    # 호출 추적의 자리 id — 이 턴의 LLM·도구 호출이 한 묶음이 된다(심의·띵킹은 엔진이 kind 를 고쳐 단다). 꺼져 있으면 아무것도 안 한다.
    _call_trace.tag(kind="chat", turn=_call_trace.new_id())
    if req.routine is not None:
        # 분석 루틴(HWAXPortal docs/analysis-routines R3-design §4) — 코드가 단계를 도는 떼어 낸 작업이다. 심의 멈춤 · 슬래시 · 띵킹보다 먼저
        # 가른다(슬래시가 이기면 루틴 요청이 일반 턴이 된다). _pinged_stream 으로 감싸지 않는다 — 그 감싸개는 구독이 끊기면 턴을 취소한다(챗 중지).
        # 루틴은 브라우저가 떠나도 돈다 — 구독 · ping · 다시 붙기 · 심의 멈춤 · 동시 상한은 routine 이 맡는다.
        import routine as _routine  # noqa: PLC0415 — routine 이 app 을 늦게 읽는다(순환 import 회피)
        return StreamingResponse(_routine.run_routine(app, req), media_type="text/event-stream", headers=SSE_HEADERS)
    # 누가 띄운 심의인가 — 리스크 앱 러너의 패널은 app(카드 대조 관문의 절반 판정에 세지 않는다 — M3.md #26).
    started_by = _started_by(req.delib_opts.get("started_by") if isinstance(req.delib_opts, dict) else None)
    # 새 심의 멈춤(HWAXPortal docs/delib-ops R1) — 웹 세 트리거도 MCP 두 도구와 같이 막는다. 앱이 띄운 리스크 패널은 막지 않는다 — 엔진이
    # 거절하면 패널이 오류로 닫힌다. 앱은 비우기 깃발(/health.drain)로 스스로 새 패널을 멈춘다(update-all 은 둘 다 세운다).
    if started_by != "app" and (is_sim_deliberation(req.message) or is_test_plan(req.message) or is_deliberation(req.message)):
        _paused = _delib_pause_info()
        if _paused:
            return StreamingResponse(_paused_stream(_paused), media_type="text/event-stream", headers=SSE_HEADERS)
    if is_sim_deliberation(req.message):
        # 시뮬레이션 심의: "/시뮬심의 <현상>" → 메커니즘 심의 → CAE 해석 설계 심의 2단.
        # 일반 심의보다 먼저 검사한다 — 트리거가 겹치지는 않지만 의도를 코드 순서로 남긴다.
        _q = strip_sim_trigger(req.message)
        stream = _detach_stream(_with_delib_images(req, _q, lambda o: run_sim_deliberation(
            app, _q, req.groups, o, req.user_email, req.user_pat, req.history, **_wiki_kw(req))), "sim", started_by)
    elif is_test_plan(req.message):
        # 시험 계획 심의: "/시험계획 <목적>" → 물성 근거 공백을 조회한 뒤 우선순위·조건축까지.
        # 해석은 물성이 없으면 시작할 수 없어, 실무에서 가장 먼저 막히는 지점이다.
        _q = strip_test_plan_trigger(req.message)
        stream = _detach_stream(_with_delib_images(req, _q, lambda o: run_test_plan(
            app, _q, req.groups, o, req.user_email, req.user_pat, req.history, **_wiki_kw(req))), "test-plan", started_by)
    elif is_deliberation(req.message):
        # `/심의 <질문>` 은 사람이 **대놓고 적은 의도**라 그대로 화두가 된다. 다만 `/심의` 만
        # 치거나 "이거 어떻게 할까" 처럼 짧게 던지면 화두가 비어 좌석이 논의할 게 없다
        # (종전엔 빈 문자열로 그대로 심의가 돌았다 — 가드가 없었다).
        # 그때만 대화에서 뽑고, **무엇을 화두로 삼았는지 화면에 밝힌다.**
        stream = _detach_stream(_delib_with_topic(req), "delib", started_by)
    elif is_report_save(req.message):
        # "/보고서 <선택: 결론>" → 대화 이력을 코드가 blocks 로 만들어 RA 저장(결정적 — LLM 미경유).
        stream = run_report_save(app, strip_report_trigger(req.message), req.history, req.groups,
                                 req.user_email, req.user_pat)
    elif is_agent_search(req.message, pinned=bool(_persona_keys(req.pinned_agents, req.pinned_agent))):
        # "전문가 뭐 있어" 류 → 추천+분야 요약을 코드가 결정적으로 생성(LLM 나열 절단 방지). 전문가를 고른 대화의 자연어는 그 전문가에게 간다(HA-31).
        stream = run_agent_search(app, strip_agent_trigger(req.message), req.groups)
    elif is_tool_search(req.message):
        # "/도구 <질의>" 또는 '도구 뭐 있어' 류 → 도구 카탈로그+추천을 SSE tools 이벤트로(결정적).
        # 프론트가 선택 UI 를 그리고, 고른 도구는 다음 발화부터 pinned_tools 로 우선 사용된다.
        stream = run_tool_search(app, strip_tool_trigger(req.message), req.groups)
    elif (is_thinking(req.message) or req.thinking) and not _confirm_reply(req):
        # 띵킹 모드 — 답할 수 있는 전문가만 각자 답하고, 못 하는 좌석은 넘길 곳을 지목한다.
        # 명시 슬래시 트리거(/심의·/도구 등)보다 **뒤**에 둔다: 모드가 켜져 있어도 그 턴에
        # 사용자가 대놓고 /심의 를 치면 그 의도가 이긴다. 확인 카드의 답(단추 · '실행')은 일반 챗으로 간다 — 승인된 호출은 챗 턴이 보낸다
        # (HA-29, 권한 판정 _denied_feature 도 같은 판정이다 — C-10).
        stream = run_thinking(app, strip_thinking_trigger(req.message), req.groups,
                              req.user_email, req.user_pat, req.history, req.documents)
    else:
        stream = _agent_stream(app, req)
    if not (is_sim_deliberation(req.message) or is_test_plan(req.message) or is_deliberation(req.message)):
        stream = _pinged_stream(stream)      # 심의(_detach_stream)는 이미 ping 을 낸다
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
    # 최근 60건을 최근부터 담는다(_recent_lines). 종전엔 **앞에서부터** 60건·5,000자라 긴 대화의 뒤쪽(쟁점이 옮겨 간
    # 자리)이 축 추출에 안 갔다. 좁은 창은 종전 예산 5,000자·줄 800자, 넓은 창은 보조 호출 몫(_aux_budget — 창의 5%)·줄
    # 무절단(몫을 넘는 줄은 원문 길이를 적고 줄인다). 뺀 대화 수는 프롬프트에 적는다.
    _st: dict = {}
    _ab = _aux_budget(_AXIS_CTX_BUDGET)
    lines = _recent_lines(history[-60:], _ab, _narrow_cap(800),
                          lambda m, t: f"[{'사람' if m.get('role') == 'user' else 'AI'}] {t}", _st)
    if not lines:
        return []
    _omit = _aux_omit_note("experts", len(history) - len(history[-60:]), _st, _ab)
    if _omit:
        lines.insert(0, _omit)
    roster_txt = ", ".join(f"{r['domain']}({r['agents']}명)" for r in roster)
    try:
        raw = await _llm_text(
            llm,
            "당신은 심의 좌석 편성자입니다. 지시한 형식의 줄만 출력하세요.",
            f"[전문가 풀의 도메인 분류 — 여기 있는 것만 쓸 수 있다]\n{roster_txt}\n\n"
            f"[화두]\n{_cut_mark(message, _narrow_cap(1000))}\n\n[오간 대화]\n" + "\n".join(lines) +
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
# SignalForge 의 주의 줄 머리(SG-04 `ec40075`) — 해석한 제품 코드가 PLM 프로젝트 여럿에 걸리거나 질의가 다른 프로젝트를 짚으면 해석
# notes 끝에 '주의 — <plm_caution.note>' 한 줄을 달고(shared/ontology.py interpret), search_voc 봉투의 notes 가 그 해석 notes 다
# (mcp-server/server.py search_voc). 봉투에는 plm_caution 칸이 없고 이 줄뿐이다.
_VOC_CAUTION = "주의 — "


async def _voc_keywords(llm, message: str) -> list[str]:
    """search_voc 는 FTS 이고 '영어 권장'이다 — VOC 본문이 대부분 영어(번역본 포함)라 한국어
    화두를 그대로 넣으면 안 걸린다(실측 — '솔더 크랙' 은 빈 응답). 화두에서 소비자가 쓸 법한
    영어 증상·부품 낱말을 뽑는다. LLM 이 실패하면 화두 속 영문 토큰으로 대신한다."""
    try:
        raw = await _llm_text(
            llm, "당신은 검색어 추출기입니다. 반드시 유효한 JSON 하나만 출력하세요.",
            f"[화두]\n{_cut_mark(message, _narrow_cap(1500))}\n\n위 화두와 관련된 **고객 불만**을 글로벌 사용자 커뮤니티(VOC)에서 찾을 "
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
        "text": _cut_mark(text, _narrow_cap(600)),   # 좁은 창만 앞 600자(표식) — 이 글이 심의 근거로도 간다
        "url": str(v.get("source_url") or "")[:300],
    }


@app.post("/deliberate/voc-preview")
async def deliberate_voc_preview(req: VocPreviewRequest) -> dict:
    """심의 전 VOC 미리보기 — 화두로 SignalForge 를 검색해 사람이 고를 목록을 준다.

    ⚠ 세 가지를 뭉개지 않는다(무음 결함의 공통 패턴 — '빈 결과'와 '못 물어봄'이 같아 보인다).
      · unavailable — 이 사용자에게 search_voc 가 없다(권한·미연결)
      · degraded    — 물어봤는데 도구가 실패했다(결과를 읽지 못한 것도 — HA-33)
      · items=[]    — 물어봤고 보일 것이 없다(SignalForge 가 준 까닭 · 본문 없이 받은 수는 note 에)"""
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
    empty_notes: list[tuple[str, list[str]]] = []      # 0건 검색어와 그 notes — 글은 note 를 지을 때 짓는다(주의 줄로 실린 글을 뺀다)
    cautions: dict[str, list[str]] = {}      # 행이 있는 검색어가 받은 주의 줄 → 그 검색어들(처음 받은 차례)
    caution_ids: dict[str, set] = {}         # 그 주의 줄 → 그 검색어들이 돌려준 행 id(화면의 검색어 표지를 맞춘다)
    for kw in kws:
        raw = await _call(tools, "search_voc", {"keyword": kw, "limit": 10})
        if isinstance(raw, str) and raw.strip() and not _tool_text_ok(raw):
            failed += 1
            per_kw.append([])
            continue
        # 목록 도구 — 원소별 블록이 이어져 온다(_parse_json 은 마지막 하나만 읽는다). 지금 SignalForge search_voc 는 늘 객체 하나
        # {total, results, strategy, match_mode, notes} 라 그 results 를 펼친다 — 안 펼치면 객체가 본문 없는 한 건으로 읽혀 아래에서
        # 걸러지고, 실패 표시 없이 0건이었다(HWAXPortal docs/request_261030.md HA-33). 옛 꼴(원소별 블록 · 배열)은 그대로 읽는다.
        found = [v for v in _parse_json_multi(raw) if isinstance(v, dict)]
        env = next((v for v in found if isinstance(v.get("results"), list)), None)
        rows = [_voc_row(v, kw) for v in (env["results"] if env else found) if isinstance(v, dict)]
        if env is not None and not any(r["text"] for r in rows):
            # 펼쳐도 보일 글이 0건 — SF 는 답했으니 실패가 아니다. 까닭(시도한 경로 · 범위 · 기간)을 미리보기에 싣는다. SF 는 0건을
            # 'VOC 가 없다' 로 단정하지 말라고 한다. 받은 행에 본문이 없으면 그 수를 앞에 둔다 — SF 키워드 경로(mcp-server/tools/query.py
            # _search_rows)는 번역문만 골라 번역 전 글은 본문 없이 오고, 수를 안 실으면 '정말 없음' 과 같아 보인다.
            notes = env.get("notes")
            notes = [str(n) for n in (notes if isinstance(notes, list) else [notes]) if n]
            if env["results"]:
                notes.insert(0, f"받은 {len(env['results'])}건에 보일 본문이 없습니다")
            if notes:
                empty_notes.append((kw, notes))
        elif env is not None:
            # 행이 있어도 SignalForge 의 주의 줄(_VOC_CAUTION)은 싣는다 — notes 를 0건일 때만 실으면 다른 프로젝트 제품의 VOC 가 섞였을 수
            # 있다는 말이 행이 보이는 미리보기, 그 말이 가장 필요한 때에 닿지 않는다(HWAXPortal docs/request-1030 HA-33 덧붙임). 다른 notes 는
            # 종전처럼 싣지 않는다. 0건 갈래는 위에서 notes 를 통째로 실으므로 그대로 둔다. 같은 줄은 검색어끼리 한 번만 싣는다.
            cs = env.get("notes")
            for n in (cs if isinstance(cs, list) else [cs]):
                if isinstance(n, str) and n.startswith(_VOC_CAUTION):
                    ks = cautions.setdefault(n, [])
                    if kw not in ks:
                        ks.append(kw)
                    caution_ids.setdefault(n, set()).update(r["id"] for r in rows if r["text"])
        elif not any(r["text"] for r in rows) and isinstance(raw, str) and raw.strip() not in ("", "[]"):
            # 펼칠 것이 없고 읽을 글도 없는데 결과 글은 왔다(모르는 꼴 · 실패 문구 · 옛 꼴의 본문 없는 행) — 0건으로 두면 '정말 없음' 과
            # 같아 보이니 실패로 센다. '[]' 는 옛 꼴의 0건이다(내용 블록 0개를 도구 래퍼 _cap 이 그 글로 만든다).
            print(f"[voc] search_voc({kw!r}) 결과를 읽지 못해 실패로 센다 — 앞 160자 {raw.strip()[:160]!r}")
            failed += 1
            rows = []
        per_kw.append(rows)
    # 검색어마다 번갈아 뽑는다 — 한 검색어가 목록을 독점하면 나머지 증상이 안 보인다.
    items, seen = [], set()
    for i in range(max((len(x) for x in per_kw), default=0)):
        for rows in per_kw:
            if i < len(rows) and rows[i]["id"] not in seen and rows[i]["text"]:
                seen.add(rows[i]["id"])
                items.append(rows[i])
    total = len(items)
    items = items[:_VOC_PREVIEW_MAX]

    def _caution_head(c: str, ks: list[str]) -> str:
        """주의 줄의 머리 — 그 주의를 받은 검색어. 화면은 행마다 그 행을 먼저 돌려준 검색어를 표지로 단다(위 번갈아 뽑기가 행 id 로 겹침을
        뺀다). 그래서 그 검색어들이 돌려준 보이는 행에 다른 표지가 있으면 그 표지를 덧붙이고, 상한에 잘려 하나도 안 보이면 그렇다고 적는다 —
        검색어만 적으면 사람은 보이는 행에는 주의가 걸리지 않는다고 읽었다(반대 검토 10-31)."""
        own = " · ".join(f"'{k}'" for k in ks)
        tags = list(dict.fromkeys(r["keyword"] for r in items if r["id"] in caution_ids.get(c, ())))
        if not tags:
            return f"{own}(보이는 행 없음)"
        return own if set(tags) <= set(ks) else own + "(보이는 행 표지 " + " · ".join(f"'{t}'" for t in tags) + ")"
    # 주의 줄이 맨 앞이다 — 보이는 행이 어느 제품의 것인지에 대한 말이다. 0건 줄은 종전 글 그대로이되 주의 줄로 이미 실린 글은 뺀다 — 같은
    # 주의가 안내 한 줄에 두 번 나왔다(반대 검토 10-31). 0건 검색어를 주의 줄의 검색어 표지에 달지는 않는다(보이는 행이 없다).
    zero = []
    for zkw, ns in empty_notes:
        rest = [n for n in ns if n not in cautions]
        zero.append(f"'{zkw}' 0건" + (" — " + " · ".join(rest) if rest else ""))
    note = ([f"{_caution_head(c, ks)} {c}" for c, ks in cautions.items()]
            + ([f"{total}건 중 {len(items)}건만 보입니다 — 검색어를 좁혀 보세요."] if total > len(items) else []) + zero)
    return {"keywords": kws, "items": items, "total": total,
            "degraded": failed == len(kws),
            "partial": 0 < failed < len(kws),
            "tools_used": ["search_voc"],
            **({"note": " / ".join(note)} if note else {})}


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


class TopicRequest(BaseModel):
    """챗 대화에서 심의 화두를 뽑는다 — 첫 발화가 아니라 **오간 내용 전체**에서."""

    history: list[dict] = []
    fallback: str = ""          # 실패하면 돌려줄 것(대개 첫 발화) — 브리프가 못 열리면 안 된다
    job: str = "default"


# 이보다 짧으면 화두로 못 쓴다고 본다. "왜?"·"이거" 같은 것은 좌석에 줄 게 없다.
DELIB_TOPIC_MIN = int(os.environ.get("DELIB_TOPIC_MIN", "12"))


def _wiki_kw(req) -> dict:
    """위키에 남기는 심의(delib_opts.wiki)만 소속을 입구에 넘긴다(범위 aff:<소속> — HWAXPortal docs/mxwp-wiki P8). 아니면 입구
    호출은 종전 그대로다."""
    return {"affiliation": req.affiliation} if (req.delib_opts or {}).get("wiki") else {}


async def _delib_with_topic(req):
    """`/심의` 경로 — 화두가 비었거나 너무 짧으면 대화에서 뽑아 쓴다."""
    q = strip_trigger(req.message)
    if len(q.strip()) < DELIB_TOPIC_MIN and req.history:
        r = await _derive_topic(req.history, q)
        got = (r.get("topic") or "").strip()
        if got and got != q.strip():
            yield _sse("status", {"step": f"대화에서 화두를 뽑았습니다 — “{got[:120]}”",
                                  "tool": None})
            q = got
        elif not q.strip():
            # 뽑지도 못했고 적힌 것도 없다 — 빈 심의를 돌리느니 되묻는다.
            msg = ("무엇을 판단해 드릴지 한 줄로 적어 주세요. 예: `/심의 구리 두께를 12um 로 "
                   "낮출 것인가` — 대화가 이어지는 중이면 '심의로 넘기기' 버튼이 화두를 "
                   "대신 뽑아 줍니다.")
            yield _sse("token", {"delta": msg})
            yield _sse("result", {"type": "text", "content": msg})
            yield _sse("done", {})
            return
    async for chunk in _with_delib_images(req, q, lambda o: run_deliberation(
            app, q, req.groups, o, req.user_email, req.user_pat, req.history, **_wiki_kw(req))):
        yield chunk


def _delib_image_list(req) -> list:
    """심의에 걸리는 그림 — [(주소, 출처 설명)] 시간순. 앞 대화의 그림(사람이 올린 것 · 도구가 낸 것) 뒤에 심의로 넘긴 근거에 나온 그림."""
    imgs = _vision.history_images(req.history)
    seen = {u for u, _ in imgs}
    for e in ((req.delib_opts or {}).get("evidence") or []):
        if isinstance(e, dict):
            for u in _vision.image_urls(e.get("result")):
                if u not in seen:
                    seen.add(u)
                    imgs.append((u, f"심의로 넘긴 근거({str(e.get('tool') or e.get('source') or '')[:60]})에 나온 그림"))
    return imgs


async def _with_delib_images(req, q: str, start):
    """심의 엔진 앞 — 그림을 자료로 싣는다(사용자 지시 10-18 '자료를 기반으로 판단', 결정 '탐침 뒤 자동', HWAXPortal docs/chat-depth-context T5).
      (1) 판독문 — 그림을 읽는 모델(Qwen)이 옮긴 글을 원천 근거(delib_opts.evidence)에 덧붙인다. 좌석 · 의장이 [e:N] 으로 짚고, 결정문의
          근거 목록에 남는다. 심의 LLM 이 그림을 받아도 싣는다 — 그림은 짚을 번호가 없다.
      (2) 그림 조각 — 심의 LLM(VLLM_*)이 그림을 받는다고 판정되면 최근 VISION_HISTORY_IMAGES 장을 좌석 · 의장 호출마다(DELIB_IMAGES).
    start(opts) 가 엔진 생성기를 만든다 — 근거를 덧붙인 opts 를 받아야 해서 미리 만들어 두지 않는다."""
    imgs = _delib_image_list(req)
    tok = None
    if imgs:
        async for chunk in _delib_image_evidence(req, q, imgs):
            yield chunk
        if _delib_engine._wants_external(req.delib_opts):
            # 외부 응답자 심의 — 과제는 글뿐이라(external_llm 이 그림 조각을 싣지 못한다) 그림 조각은 싣지 않는다. 판독문은 위에서 근거로
            # 실렸다(동의한 반출의 글이다). 종전엔 심의 LLM 의 판정대로 '그림 N장을 싣는다' 고 말하고 조각은 과제에서 말없이 빠졌다.
            yield _sse("status", {"step": f"외부 응답자 심의다 — 과제가 글뿐이라 그림 {len(imgs)}장 자체는 싣지 않는다(판독문은 근거로 "
                                          "간다)", "tool": None})
        elif await asyncio.to_thread(_base_vision):
            pick = imgs[-_vision.HISTORY_IMAGES:] if _vision.HISTORY_IMAGES > 0 else []
            ps, miss = await asyncio.to_thread(_vision.parts, [u for u, _ in pick], ARTIFACT_DIR, AIDH_HTTP, len(pick))
            if ps:
                tok = (_delib_engine.DELIB_IMAGES, _delib_engine.DELIB_IMAGES.set(ps))
            yield _sse("status", {"step": f"심의 LLM 이 그림을 받는다 — 그림 {len(ps)}장을 좌석 · 의장 호출마다 싣는다"
                                          + (f" · {miss}장은 서버에서 지워졌거나 커서 못 실었다" if miss else "")
                                          + (f" · 오래된 {len(imgs) - len(pick)}장은 상한(VISION_HISTORY_IMAGES) 밖"
                                             if len(imgs) > len(pick) else ""), "tool": None})
    try:
        async for chunk in start(req.delib_opts):
            yield chunk
    finally:
        if tok is not None:
            try:
                tok[0].reset(tok[1])
            except ValueError:   # 다른 문맥에서 닫혔다 — 그 문맥(심의 태스크)은 곧 끝난다
                pass


async def _delib_image_evidence(req, q: str, imgs: list):
    """그림을 판독해 req.delib_opts.evidence 에 덧붙인다(그림 해시로 디스크에 남아 같은 그림은 다시 읽지 않는다). 상태 줄을 낸다."""
    tr = await asyncio.to_thread(_transcriber, app)
    if tr is None:
        yield _sse("status", {"step": f"그림 {len(imgs)}장 — 판독할 모델이 없어(그림을 받는 모델 없음) 심의 근거에 싣지 못했다"
                                      + _vision.unknown_note(),
                              "tool": None})
        return
    pick = imgs[-_vision.TRANSCRIBE_MAX:] if _vision.TRANSCRIBE_MAX > 0 else []
    yield _sse("status", {"step": f"그림 {len(pick)}장을 판독해 심의 근거로 싣는다", "tool": None})
    got = await _vision.transcribe_all(tr, pick, ARTIFACT_DIR, AIDH_HTTP, hint=q[:300])
    items = [{"source": f"그림 판독 · {why}", "tool": "vision_transcribe", "args": u, "result": t} for u, why, t in got if t]
    if items:
        opts = dict(req.delib_opts or {})
        opts["evidence"] = list(opts.get("evidence") or []) + items
        req.delib_opts = opts
    yield _sse("status", {"step": f"그림 판독문 {len(items)}장을 심의 근거로 실었다"
                                  + (f" · {len(got) - len(items)}장은 판독하지 못했다" if len(got) > len(items) else "")
                                  + (f" · 오래된 {len(imgs) - len(pick)}장은 상한(VISION_TRANSCRIBE_MAX) 밖" if len(imgs) > len(pick) else ""),
                          "tool": None})


def _seat_vision_max() -> int:
    """좌석 조회 그림 판독의 상한 — DELIB_SEAT_VISION_MAX(조회 한 번에 판독하는 그림 수, 0 = 좌석 판독만 끈다). 없으면 VISION_TRANSCRIBE_MAX 와
    같은 값이다(종전 그대로). 종전엔 그 손잡이 하나라 좌석 판독만 끄려면 챗 · 띵킹 판독까지 꺼졌다(HWAXPortal docs/request-1027 HA-17).
    부를 때 읽는다 — /health 가 지금 값을 보인다. 모양이 틀린 값은 종전 값이다."""
    v = os.environ.get("DELIB_SEAT_VISION_MAX", "").strip()
    try:
        return max(0, int(v)) if v else _vision.TRANSCRIBE_MAX
    except ValueError:
        return _vision.TRANSCRIBE_MAX


class _SeatImageReader:
    """심의 도중 좌석이 자유 조회로 받은 그림의 판독기 — 엔진(deliberation._seat_image_transcripts)이 부른다(HWAXPortal
    docs/chat-depth-context T5-b · CD-D14, 사용자 요청 10-21). 판독 모델은 위 심의 판독과 같다(_transcriber — 그림을 받는 챗 서버 → 그림을
    받는 VLLM_*). 같은 그림은 해시로 디스크에서 꺼낸다(vision.transcribe). 판독 묶음은 서버 전체에서 한 번에 하나다(묶음 안은 넷씩) — 20석이
    한꺼번에 판독하면 챗 서버를 혼자 차지한다. 잠금은 이벤트 루프마다 새로 짓는다(시험은 asyncio.run 을 여러 번 돈다)."""

    def __init__(self, app_):
        self._app = app_
        self._gate: tuple | None = None        # (루프, asyncio.Lock)

    @property
    def limit(self) -> int:
        """조회 한 번(좌석 하나의 한 라운드)에서 판독하는 그림 수 — 좌석 전용 손잡이(_seat_vision_max)."""
        return _seat_vision_max()

    @staticmethod
    def image_urls(text) -> list:
        return _vision.image_urls(text)

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._gate is None or self._gate[0] is not loop:
            self._gate = (loop, asyncio.Lock())
        return self._gate[1]

    async def read(self, urls: list, hint: str = "") -> dict | None:
        """그림 주소들 → {주소: 판독문}(받아 오지 못했거나 판독에 실패한 것은 빈 글). 판독할 모델이 없으면 None."""
        tr = await asyncio.to_thread(_transcriber, self._app)
        if tr is None:
            return None
        async with self._lock():
            got = await _vision.transcribe_all(tr, [(u, "") for u in urls], ARTIFACT_DIR, AIDH_HTTP, hint=(hint or "")[:300])
        return {u: t for u, _why, t in got}


# 엔진에 기동 때 단다 — MCP 심의 잡(delib_jobs)도 같은 엔진 모듈을 부른다(_with_delib_images 를 지나지 않는다). 엔진은 모듈로 읽는다 —
# 시험이 엔진을 다시 읽으면(reload) 이 값은 비고, 그때는 종전대로 그림 주소만 간다.
_delib_engine.SEAT_IMAGE_READER = _SeatImageReader(app)


async def _derive_topic(history: list, fallback: str = "") -> dict:
    """대화에서 심의 화두 한 문장을 뽑는다. 엔드포인트와 `/심의` 경로가 **같은 것**을 쓴다.

    두 곳이 다른 프롬프트를 쓰면 같은 대화에서 다른 화두가 나오고, 사용자는 어느 쪽이
    진짜인지 알 수 없다."""
    fb = (fallback or "").strip()
    # 최근 40건을 **최근부터** 예산 안에 담는다(_recent_lines). 좁은 창은 종전 예산 12,000자·줄 2,000자, 넓은 창은 보조 호출
    # 몫(_aux_budget — 창의 5%)·줄 무절단. 종전엔 오래된 쪽부터 원문 길이로 예산을 깎아, 앞의 긴 답 하나가 뒤의 물음을 밀어냈다.
    _h = (history or [])[-40:]
    _st: dict = {}
    _ab = _aux_budget(12000)
    hist = _recent_lines(_h, _ab, _narrow_cap(2000),
                         lambda m, t: f"{'사용자' if m.get('role') == 'user' else '어시스턴트'}: {t}", _st)
    if not hist:
        return {"topic": fb, "why": "", "options": [], "error": "no_history"}
    _omit = _aux_omit_note("topic", len(history or []) - len(_h), _st, _ab)
    if _omit:
        hist.insert(0, _omit)

    try:
        raw = await _llm_text(
            app.state.llm,
            "당신은 심의 준비 보조자입니다. 반드시 유효한 JSON 하나만 출력하세요.",
            "아래는 사람과 어시스턴트가 나눈 대화다. 이 대화가 **결국 무엇을 판단해 달라는 것인지**"
            " 한 문장으로 뽑아라.\n\n"
            + "\n".join(hist)
            + "\n\n규칙\n"
            "· **답이 아니라 물음**을 써라. '…을 12um 로 낮춘다' 가 아니라 '…을 12um 로 낮출 것인가' 다. "
            "결론을 화두에 넣으면 심의가 그것을 전제로 깔고 시작한다.\n"
            "· 대화 **뒷부분의 쟁점**을 우선해라. 첫 발화는 대개 배경이고, 진짜 물음은 오가면서 옮겨 간다.\n"
            "· 대화에 나온 수치·조건·제약을 물음 안에 넣어라(예: 굽힘 반경 3.0mm, 목표 20만회).\n"
            "· 대화에 없는 것을 지어내지 마라. 애매하면 좁히지 말고 그대로 둬라.\n"
            "· why 에는 왜 이 물음인지 한 줄(어느 대목에서 왔는지).\n"
            "· options 에는 다르게 잡을 수 있는 화두 1~2개(없으면 []).\n\n"
            'JSON {"topic": "한 문장 물음", "why": "한 줄", "options": ["대안 화두", ...]} 로만.')
        v = _parse_json(raw) or {}
    except Exception as exc:  # noqa: BLE001 — 화두 제안 실패가 심의를 막으면 안 된다
        print(f"[topic] failed: {exc!r}")
        return {"topic": fb, "why": "", "options": [], "error": "llm_failed"}

    topic = str(v.get("topic") or "").strip()[:600]
    if not topic:
        return {"topic": fb, "why": "", "options": [], "error": "empty"}
    opts = [str(x).strip()[:600] for x in (v.get("options") or []) if str(x).strip()][:2]
    return {"topic": topic, "why": str(v.get("why") or "").strip()[:300], "options": opts}


@app.post("/deliberate/topic")
async def deliberate_topic(req: TopicRequest) -> dict:
    """대화를 읽고 **심의에 부칠 한 문장**을 쓴다.

    왜 첫 발화를 그냥 쓰면 안 되나 — 대화는 움직인다. 처음엔 "굽힘 수명이 왜 부족하냐" 로
    시작해도, 오가는 사이 쟁점은 "구리 두께를 12um 로 낮출 것인가" 로 옮겨 간다. 첫 발화를
    화두로 넣으면 심의가 **이미 지나온 자리**를 다시 판다.

    ⚠ 되묻기(/deliberate/clarify)와 같은 규율이다 — **실패가 브리프를 막지 않는다.** LLM 이
    죽거나 이상하면 fallback 을 그대로 돌려주고 error 로 알린다. 사람이 고쳐 쓰는 칸이므로
    틀린 제안보다 위험한 것은 화면이 안 열리는 것이다.

    P1(브리프에 결론 금지)도 지킨다 — 답이 아니라 **판단을 요구하는 물음**을 쓰게 한다.
    """
    return await _derive_topic(req.history, req.fallback)


@app.post("/deliberate/clarify")
async def deliberate_clarify(req: ClarifyRequest) -> dict:
    """빈 칸 스캔 — {applicable, slots:[{key,label,present,value,question,options}], ask:[key]}.

    ⚠ 되묻기는 **절대 심의를 막지 않는다.** LLM 이 실패하거나 응답이 이상하면 ask=[] 로 돌려
    묻지 않고 진행한다(묻지 못한 것을 '다 있다'로 꾸미지는 않는다 — error 로 알린다).
    칸이 응답에서 빠지면 그 칸은 묻지 않는다(괴롭히지 않는 쪽으로 틀린다)."""
    slots = _CLARIFY_SLOTS[_JOB_SLOTSET.get(req.job, "mechanism")]
    # 최근 20건을 최근부터 담는다(_recent_lines — 화두 뽑기와 같은 결함이 있었다). 좁은 창 종전 예산 3,000자.
    _st: dict = {}
    _ab = _aux_budget(3000)
    hist: list[str] = _recent_lines(req.history[-20:], _ab, _narrow_cap(3000),
                                    lambda m, t: f"{m.get('role', 'user')}: {t}", _st)
    if not _clarify_applicable(req.job, req.message + "\n" + "\n".join(hist)):
        return {"applicable": False, "slots": [], "ask": []}   # LLM 을 부르지도 않는다
    spec = "\n".join(f"- {k}: {label} — {hint}" for k, label, hint, _o in slots)
    # 뺀 대화 수는 프롬프트에만 싣는다 — hist 는 '있음' 판정의 원문(source)이기도 하다.
    _omit = _aux_omit_note("clarify", len(req.history) - len(req.history[-20:]), _st, _ab) if hist else ""
    try:
        raw = await _llm_text(
            app.state.llm, "당신은 심의 준비 보조자입니다. 반드시 유효한 JSON 하나만 출력하세요.",
            f"[화두]\n{_cut_mark(req.message, _narrow_cap(3000))}\n"
            + (f"\n[앞선 대화]\n" + "\n".join(([_omit] if _omit else []) + hist) + "\n" if hist else "")
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
# 허브 사용 안내의 시작. **문구 하나에 기대지 않는다** — 상류가 "How to access" 를
# "How to use" 로만 바꿔도 조용히 안 잘리고 안내문 2,840자가 통째로 들어간다(감사 실측).
# 머리말에 "this hub" 가 들어간 마크다운 헤딩이면 잡는다. 한국어 역할 문서에 영문
# 헤딩으로 그 표현이 정당하게 들어갈 일은 없다.
_HUB_GUIDE_RE = re.compile(r"\n#{1,6}[^\n]*\bthis hub\b", re.I)


def _role_doc(system_prompt: str) -> str:
    """역할 원문에서 **이 클라이언트와 무관한 허브 사용 안내**를 떼어 낸다.

    AIDataHub 가 붙이는 '## How to access this hub' 이하는 MCP 클라이언트용이다.
    실측(sim-pcb-warpage, 2026-09-13): 원문 4,100자 중 2,840자(**69%**)가 그 안내였고
    내용도 포털 챗에서는 틀리거나 해롭다 — "do NOT use WebFetch"(여기엔 없는 도구),
    "curl -s http://127.0.0.1:8001/…"(내부 주소를 프롬프트에 흘린다),
    "get_agent_session 을 FIRST 로 불러라"(서버가 이미 불렀다. 한 번 더 시킨다).

    구분자는 정규식으로 찾는다 — 정확한 문자열 하나에 기대면 허브가 머리말을 조금만 바꿔도
    **조용히** 안 잘리고 원문이 통째로 들어간다.
    """
    sp = system_prompt or ""
    if sp.startswith('You are an assistant for "'):
        return ""          # 자동 생성 플레이스홀더 — 역할이 아니다
    m = _HUB_GUIDE_RE.search(sp)
    if not m:
        return sp.strip()
    # 구분선 찌꺼기는 **잘랐을 때만** 떼어 낸다. 무조건 돌리면 역할 본문이 정당하게
    # "…판정 기준은 A---" 로 끝나는 경우까지 깎는다(실측).
    return re.sub(r"\n*-{3,}\s*$", "", sp[:m.start()].rstrip()).strip()


@app.post("/catalog/agent")
async def catalog_agent(req: AgentDetailRequest) -> dict:
    """전문가 상세 + 보유 지식(레코드 목록) — 브라우즈 UI 용(결정적·LLM 미경유).
    LLM 텍스트 나열은 결과 절단 캡에 걸려 잘리므로, 탐색은 이 데이터로 UI 가 그린다.
    records 는 미리보기 20건이고 records_total 이 전체 수다 — 전체는 /catalog/agent/records 로 넘긴다.
    상세를 못 받으면 error="session_failed", 목록을 못 받으면 records_error="records_failed"(records_total 은 None) —
    실패를 빈 전문가·0건으로 주지 않는다."""
    tools = await _tools_by_name(app, req.groups, CATALOG_RESULT_MAX)  # 상세·레코드를 JSON 으로 읽는다
    if not tools:
        return {"error": "gateway_unavailable"}
    key = req.key.strip()[:120]
    out: dict = {"key": key, "name": key, "role": "", "prompt": "", "tags": [], "samples": [], "records": [],
                 "records_total": None, "operator": False, "apps": []}
    try:
        raw_sess = await _call(tools, "get_agent_session", {"agent_type": key})
        sess = _first_dict(_parse_json(raw_sess)) if _result_ok(raw_sess) else {}
        sd = _first_dict(sess.get("data", sess))
        if not sd.get("agent_type"):
            # 도구 없음(None)·✖ 호출 실패·'agent not found' 평문·실패 봉투가 다 여기로 온다. 빈 전문가로 주면 '역할도 지식도
            # 없는 사람'으로 읽힌다. 원문을 받았으면 agent_type 이 늘 있다(적재 때 생긴 자리 행도) — 그때 역할이 비면 실패가
            # 아니라 '아직 등록되지 않은 전문가'다(화면·심의 _restore_role 과 같은 기준).
            raise ValueError(f"no agent session: {str(raw_sess)[:160]!r}")
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
        out["error"] = "session_failed"
        print(f"[catalog] agent session failed: {exc!r}")
    try:
        raw_rec = await _call(tools, "list_records", {"agents": [key], "limit": 20})
        raw = _parse_json(raw_rec) if _result_ok(raw_rec) else None
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            # /catalog/agent/records 와 같은 판정 — '0건'과 '못 물어봤다'를 뭉개지 않는다. AIDataHub list_records 는
            # 늘 {total, count, offset, items} 를 준다(종전의 list·records·data 갈래는 운영에서 쓰이지 않았다).
            raise ValueError(f"no record list: {str(raw_rec)[:160]!r}")
        recs = raw["items"]
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
        out["records_error"] = "records_failed"
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
    """전문가 한 명의 지식카드 목록 — 낱말 검색(q — 공백으로 나눈 낱말을 모두 담은 카드. 제목·요약, AIDataHub 에서 본문 검색을
    켜면 본문까지)·쪽(offset·limit)·총수. 심층 보기 용(LLM 미경유). q 는 그대로 넘긴다 — 나누고 거는 것은 AIDataHub 다."""
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
    # 다음 부분 보기 — 앞 응답의 next 를 그대로 돌려받는다(절 번호 · 그 절 안 글자 위치).
    section_offset: int = 0
    char_offset: int = 0


# 카드 한 장 본문 상한(문자)·표 행 상한. 표 레코드는 행이 수천 개일 수 있어 앞쪽만 싣고
# truncated 로 잘렸다고 알린다 — 잘린 걸 전부인 척 보이면 없는 결론을 읽게 된다.
RECORD_TEXT_MAX = 60000
RECORD_ROWS_MAX = 200


def _cell(v):
    """표 칸 — 숫자·불리언·빈 값은 그대로, 나머지는 200자 문자열로."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))[:200]


def _md_cell(c) -> str:
    # 포털 md.ts 의 splitCells 는 ASCII '|' 로만 칸을 가른다 — 칸 속 '|' 는 전각으로.
    return ("" if c is None else str(c)).replace("\n", " ").replace("|", "｜")[:200]


def _block_md(b: dict, refs: dict) -> str:
    """DocumentJSON 블록 한 개 → 마크다운. 표는 MXWhitePaper 가 블록 안에(headers·rows), 허브 변환기(kooremapper-iga)는
    content.tables 에 두고 ref 로 가리킨다(실측). 칸 이름도 다르다 — 코드는 code·language(MX)와 text(변환기),
    수식은 expression, 콜아웃은 title·text. MXWhitePaper 의 _render_blocks 는 이 이름들을 안 읽어 그대로 못 쓴다."""
    t = str(b.get("type") or "")
    if t in ("table", "figure") and isinstance(b.get("ref"), str) and b["ref"] in refs:
        b = {**refs[b["ref"]], "type": t}
    if t == "table":
        hdr, rows = b.get("headers") or [], [r for r in b.get("rows") or [] if isinstance(r, list)]
        if not hdr and rows:
            hdr, rows = rows[0], rows[1:]
        if not hdr:
            return ""
        md = ["| " + " | ".join(map(_md_cell, hdr)) + " |", "|" + " --- |" * len(hdr)]
        md += ["| " + " | ".join(map(_md_cell, r)) + " |" for r in rows[:RECORD_ROWS_MAX]]
        if len(rows) > RECORD_ROWS_MAX:  # 잘린 표를 전부인 척 보이지 않는다(위 RECORD_ROWS_MAX 주석과 같은 까닭)
            md.append(f"\n*표 앞 {RECORD_ROWS_MAX}행만 싣는다 — 전체 {len(rows)}행.*")
        return (f"**{b['caption']}**\n\n" if b.get("caption") else "") + "\n".join(md)
    if t == "code":
        return f"```{b.get('language') or ''}\n{b.get('code') or b.get('text') or ''}\n```"
    if t == "math":
        return f"$$\n{b.get('expression') or b.get('tex') or ''}\n$$"
    if t == "figure":
        return f"*[그림] {b['caption']}*" if b.get("caption") else ""
    text = str(b.get("text") or "")
    if t == "list_item":
        return f"{b.get('marker') or '-'} {text}"
    if t == "callout" and b.get("title"):
        return f"**{b['title']}** {text}"
    return f"> {text}" if t == "quote" else text


def _flat_sections(secs, refs: dict):
    """절 트리(MX 의 subsections · 변환기의 children) → (절, 본문) 깊이 우선.
    content_text·text 가 비면 blocks 를 푼다 — 종전엔 최상위 절의 content_text 만 읽어 제목만 남았다."""
    for s in secs or []:
        s = _first_dict(s)
        text = str(s.get("content_text") or s.get("text") or "")
        if not text and isinstance(s.get("blocks"), list):
            text = "\n\n".join(t for t in (_block_md(_first_dict(b), refs) for b in s["blocks"]) if t)
        yield s, text
        yield from _flat_sections(s.get("subsections") or s.get("children"), refs)


def _safe_cut(text: str, limit: int) -> int:
    """text 를 limit 자 안에서 끊을 자리 — 마지막 빈 줄(코드 펜스 ``` · $$ 수식 밖) 바로 뒤. 글자 위치 그대로 자르면 표 · 펜스 · 수식
    한가운데서 끊겨 다음 쪽이 깨진 마크다운으로 시작했다(HWAXPortal docs/request-1027 HA-18). 표는 줄 사이에 빈 줄이 없어 빈 줄에서
    끊으면 갈리지 않는다. 그런 자리가 쪽의 1/4 보다 앞이면(아주 긴 펜스 · 문단) 종전대로 limit 에서 끊는다 — 쪽이 작아지지 않게."""
    if len(text) <= limit:
        return len(text)
    head = text[:limit]
    pos = head.rfind("\n\n")
    while pos >= limit // 4 and pos > 0:
        before = head[:pos]
        if before.count("```") % 2 == 0 and before.count("$$") % 2 == 0:
            return pos + 2
        pos = head.rfind("\n\n", 0, pos)
    return limit


def _record_view(rec: dict, section_offset: int = 0, char_offset: int = 0) -> dict:
    """get_record 원문 → 읽기 화면. 본문 모양이 셋이다 — DOC(sections·sources)·DATA(headers·rows)·
    그 밖(SIM 의 tool_call 등). 모르는 모양은 버리지 않고 JSON 원문으로 보인다(없다고 하면 거짓이다).
    DOC 절은 블록 구조(blocks·subsections·children)도 마크다운으로 풀어 싣는다(_flat_sections).
    한 쪽은 RECORD_TEXT_MAX 자다. total_sections·total_chars 는 카드 전체(푼 절 기준) 값이고, next 가 있으면
    그 위치({section, char})를 section_offset·char_offset 으로 돌려받아 다음 쪽을 준다. 위치를 안 주면 첫 쪽이다."""
    content = rec.get("content") if isinstance(rec.get("content"), dict) else {}
    out = {**_record_row(rec), "agents": [str(a)[:120] for a in (rec.get("agents") or [])][:20],
           "sections": [], "table": None, "sources": [], "truncated": False}
    out["summary"] = str(rec.get("summary") or "")[:2000]
    refs = {x["id"]: x for k in ("tables", "figures")
            for x in (content.get(k) if isinstance(content.get(k), list) else [])
            if isinstance(x, dict) and isinstance(x.get("id"), str) and x["id"]}
    flat = list(_flat_sections(content.get("sections"), refs))
    secs, texts = [s for s, _ in flat], [t for _, t in flat]      # 이어 읽기는 이 목록 위에서 돈다
    # 얼마나 잘렸는지 말하려면 총량이 있어야 한다. next 가 None 이면 절은 끝까지 실었다.
    out.update(total_sections=len(secs), total_chars=sum(map(len, texts)), next=None)
    budget = RECORD_TEXT_MAX
    for i in range(section_offset, len(secs)):
        if budget <= 0:
            out["truncated"], out["next"] = True, {"section": i, "char": 0}
            break
        start = char_offset if i == section_offset else 0
        s, rest = secs[i], texts[i][start:]
        text = rest[:_safe_cut(rest, budget)]
        budget -= len(text)
        out["sections"].append({"id": str(s.get("section_id") or "")[:40], "title": str(s.get("title") or "")[:200],
                                "level": s.get("level") if isinstance(s.get("level"), int) else 1, "text": text})
        if start + len(text) < len(texts[i]):
            # 상한은 대개 절 중간에서 걸린다 — 절 번호만 넘기면 그 절 앞을 되풀이하거나 뒤를 건너뛴다.
            out["truncated"], out["next"] = True, {"section": i, "char": start + len(text)}
            break
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
    if not secs and out["table"] is None:  # 끝을 넘긴 section_offset 이 원문(JSON) 보기로 바뀌지 않게
        rest = {k: v for k, v in content.items() if k != "sources"}
        if rest:
            text = json.dumps(rest, ensure_ascii=False, indent=1, default=str)
            out.update(total_sections=1, total_chars=len(text))  # 비워 두면 SIM 카드가 '빈 카드'로 판정된다(catalog_record)
            out["truncated"] = out["truncated"] or len(text) > RECORD_TEXT_MAX
            # 코드 펜스로 감싼다 — 평문이면 포털이 비례폭 문단 하나로 그려 중첩을 못 따라간다(SIM 카드 86~119줄).
            # 자른 뒤 감싸서 잘려도 펜스는 닫힌다. indent=1 출력은 첫·끝 줄 말고 공백으로 시작해 안에서 펜스가 안 닫힌다.
            out["sections"].append({"id": "", "title": "원문(JSON)", "level": 1,
                                    "text": "```json\n" + text[:RECORD_TEXT_MAX] + "\n```"})
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
    # 다음 부분을 받을 때마다 get_record 로 카드 전체를 다시 받는다 — 첫 열기와 같은 비용이다. 음수는 목록과 같이 막는다.
    so, co = max(0, req.section_offset), max(0, req.char_offset)
    view = _record_view(rec, so, co)
    # 빈 카드 판정은 이 쪽이 아니라 카드 전체로 한다 — 이어 읽은 쪽에 빈 절만 남은 멀쩡한 카드를 빈 카드로 보지 않게.
    if view["table"] is None and view["total_chars"] == 0:
        # content 엔 빈 '본문' 자리표시뿐이고 글은 RAG 절에만 있는 카드가 있다(MX 자리표시 154장 중 149장, 실측).
        raw = await _call(tools, "get_record_sections", {"record_id": rid})
        # 조회 실패를 '빈 카드'로 보이지 않는다. 게이트웨이 도구의 실패는 _cap_tool 의 ✖ 로 오고(_result_ok 가 본다),
        # _call 의 '(tool … error' 와 평문 'Error executing tool …' 은 _tool_text_ok 가 본다.
        if isinstance(raw, str) and raw.strip() and not (_result_ok(raw) and _tool_text_ok(raw)):
            return {"error": "record_failed", "id": rid}
        # 목록 도구라 원소마다 content 하나로 와서 _call 이 이어 붙인다 — _parse_json 은 마지막 하나만 잡는다.
        rows = [r for r in _parse_json_multi(raw) if isinstance(r, dict) and r.get("content_text")]
        # 어디에도 글이 없으면 절을 비운다 — 제목만 늘어놓지 않고 읽기 칸이 '본문이 비어 있는 카드입니다.' 를 띄운다.
        # 빈 목록을 _record_view 에 넘기면 원문(JSON) 폴백이 {"sections": []} 를 본문으로 띄우므로 넘기지 않는다.
        # RAG 절로 다시 그릴 때도 같은 위치를 넘겨 총량·next 까지 RAG 절 기준으로 옮긴다.
        alt = (_record_view({**rec, "content": {"sections": rows}}, so, co) if rows
               else {"sections": [], "truncated": False, "total_sections": 0, "total_chars": 0, "next": None})
        view.update({k: alt[k] for k in ("sections", "truncated", "total_sections", "total_chars", "next")})
    return view


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


def _routine_health() -> dict:
    """/health 의 분석 루틴 칸 — 도는 실행 수(쉬는 실행은 빼고) · 쉬는 실행 수 · 동시 상한(HWAXPortal docs/analysis-routines R3-D7 · R3-design §8).
    쉬는 실행(parked — 새 심의 멈춤이 풀리길 · 줄 선 심의가 시작되길 기다린다)은 routine_active 에 세지 않는다. 모듈을 못 붙이면 0 · None(/health 는
    늘 답한다)."""
    try:
        import routine  # noqa: PLC0415 — routine 이 app 을 늦게 읽는다(순환 import 회피)
        return {"routine_active": routine.active_count(), "routine_parked": routine.parked_count(),
                "routine_max": routine.ROUTINE_MAX_RUNNING}
    except Exception:  # noqa: BLE001
        return {"routine_active": 0, "routine_parked": 0, "routine_max": None}


def _delib_load() -> tuple[int, int]:
    """(진행 중, 대기) 심의 수 — 웹 경로(분리 태스크)와 MCP 잡 원장을 합친다. 도는 분석 루틴도 진행 중에 더한다(R3-D7 — update-all · start.sh ·
    deploy-all 의 기다림이 고침 없이 루틴을 기다린다). 쉬는 루틴(parked — 새 심의 멈춤 · 줄 선 심의를 기다린다)은 더하지 않는다 — update-all 이
    건 멈춤이 그 루틴의 심의를 붙잡고 있어 세면 기다림이 끝나지 않는다(R3-design §8). 카드 대조의 절반 판정(_delib_human)에는 세지 않는다 —
    루틴은 챗 LLM 이다."""
    active = len(_DETACHED_TASKS) + _routine_health()["routine_active"]
    try:
        import delib_jobs  # noqa: PLC0415 — 심의 MCP 가 비활성이어도 /health 는 답해야 한다
        return active + delib_jobs.running_count(), len(delib_jobs._PENDING)
    except Exception:  # noqa: BLE001
        return active, 0


def _delib_human() -> int:
    """사람이 띄운 도는 심의 수 — 분리 태스크 가운데 started_by 가 app 이 아닌 것 + MCP 잡 가운데 같은 것(M3.md #26).
    카드 대조 관문의 절반 판정만 이 수를 본다. delib_active(_delib_load)는 그대로 전부 센다(재기동 미룸 판정)."""
    n = sum(1 for t in list(_DETACHED_TASKS) if _DETACHED_BY.get(t, "human") != "app")
    try:
        import delib_jobs  # noqa: PLC0415 — 심의 MCP 가 비활성이어도 /health 는 답해야 한다
        return n + delib_jobs.running_human_count()
    except Exception:  # noqa: BLE001
        return n


_DRAIN_OFF = {"on": False, "scope": None, "until": None, "until_idle": None, "hold": False, "by": None, "reason": None,
              "set_at": None}


def _card_review_health() -> dict:
    """/health 의 카드 대조 칸 — review_active · draining · drain{…} · capabilities · card_review{…}. 모듈을 못 붙였거나 셈이
    실패하면 capabilities 를 비우고 사유를 싣는다(/health 는 늘 답한다). draining 은 비우기 scope all(카드 대조 새 호출을 받지
    않는다)이고 셸이 grep 으로 읽게 최상위에 둔다. drain.on 은 scope 와 상관없이 '앱이 새 패널을 집지 않는다' 다(M3.md 4.7)."""
    if _card_review is None:
        return {"review_active": 0, "draining": False, "drain": dict(_DRAIN_OFF), "capabilities": [],
                "card_review": {"error": _CARD_REVIEW_ERR}}
    try:
        fields = _card_review.health_fields(app)
        drain = _card_review.gate_of(app).drain.snapshot()
    except Exception as exc:  # noqa: BLE001
        return {"review_active": 0, "draining": False, "drain": dict(_DRAIN_OFF), "capabilities": [],
                "card_review": {"error": f"{type(exc).__name__}: {exc}"[:300]}}
    return {"review_active": fields["active"], "draining": bool(drain["on"] and drain["scope"] == "all"),
            "drain": drain, "capabilities": ["card_review", "drain"], "card_review": fields}


def _chat_llm_health() -> dict:
    """/health 의 chat_llm — 꺼져 있으면 {"enabled": false}. 켜져 있으면 서버(호스트) · 모델 · 생각 · 스트리밍 · 넘김(켬 · 대상 · 횟수 ·
    마지막 시각) · 챗 서버의 마지막 실패 · 창(ctx_tokens · 출처 · 그 서버가 모델을 내나). 키는 싣지 않는다 — /health 는 인증이 없다.
    창은 이미 읽은 값만 보인다(None 이면 아직 첫 챗 턴 전) — 헬스 검사가 원격 서버를 두드리지 않게."""
    info = getattr(app.state, "chat_llm_info", None) or {"enabled": False}
    if not info.get("enabled"):
        return dict(info)
    return {**info, "fallback": dict(info.get("fallback") or {}), "ctx_tokens": _chat_ctx_cache.get("n"),
            "ctx_source": _chat_ctx_cache.get("src"), "model_served": _chat_ctx_cache.get("served"),
            "ctx_probe_error": _chat_ctx_cache.get("probe_error"), "tool_cut": dict(_TOOL_CUT) or None}


def _external_llm_health() -> dict:
    """/health 의 외부 응답자 칸 — 꺼져 있으면 {}(종전 응답 그대로). 켜져 있으면 external_llm{ctx_tokens, max_tokens, wait_s,
    answer_s, call_worst_s, queued, claimed, evid_budget}. 수와 설정만 싣는다(과제 글·주인은 싣지 않는다 — /health 는 인증이
    없다). evid_budget 은 외부 응답자 창으로 셈한 심의 근거 몫(자)이다 — 리스크 앱이 외부 심의의 브리프를 이 값에 맞춘다."""
    board = _external_llm.board_of(app)
    if board is None:
        return {}
    token = _external_llm.ctx_override.set(board.settings.ctx_tokens)
    try:
        evid = _evid_budget()
    finally:
        _external_llm.ctx_override.reset(token)
    return {"external_llm": {**_external_llm.health_fields(board), "evid_budget": evid}}


# ── 비우기(update-all 동안 — M3.md 4.7) ─────────────────────────────────────────
# POST /drain {on, scope: all|panels, ttl_s | until_idle{grace_s, max_s}, hold?, by, reason, force?} · GET /drain. 깃발은
# review_gate.DrainState(파일 하나 — 재기동을 넘긴다)이고 규칙도 거기 있다. 인증은 없다 — 서버는 루프백에만 묶이고 /chat 도
# 같은 신뢰 수준이다. 포털은 /drain 을 중계하지 않는다(포털 시험이 라우터에서 뽑아 본다).
# scope: "delib" 는 **새 심의 멈춤**(HWAXPortal docs/delib-ops R1)이다 — 같은 규칙의 다른 파일(delib_jobs.pause_state)이라 카드 대조 깃발과
# 서로 건드리지 않는다. 끄기도 scope: "delib" 를 적어야 그 깃발이다(종전 끄기 요청은 scope 를 안 적었다 — 그대로 카드 대조 깃발이다).
# GET /drain 은 카드 대조 깃발 그대로다(리스크 앱 · 포털과 나눠 쓰는 계약 예제) — 멈춤은 /health 의 delib_pause 와 scope delib 응답이 보인다.
# until_idle 은 받지 않는다(멈춤은 '심의가 비면 풀린다' 가 아니라 잡아 두고 재기동하는 것이다). 끄면 그 자리에서 줄을 띄운다.
_DRAIN_FIELDS = ("on", "scope", "ttl_s", "until_idle", "hold", "by", "reason", "force")
_ECHO_MAX = 80                          # 거절이 되비추는 호출자 값 하나의 글자 상한


def _echo(v) -> str:
    """호출자가 보낸 값을 되비출 때 — 80자에서 끊고 원래 길이를 단다(거절 본문·로그가 입력 크기만큼 커지지 않게)."""
    s = str(v)
    return s if len(s) <= _ECHO_MAX else f"{s[:_ECHO_MAX]}…({len(s)}자)"


def _drain_view(gate) -> dict:
    a, q = _delib_load()
    return {**gate.drain.snapshot(), "review_active": gate.active + _external_review_active(), "delib_active": a,
            "delib_queued": q}


def _delib_pause_view() -> dict:
    """새 심의 멈춤 깃발(R1) — DrainState.snapshot 꼴(꺼져 있으면 on=false). 켜져 있으면 retry_after_s 도. 못 읽으면 꺼짐 꼴에 error."""
    try:
        import delib_jobs  # noqa: PLC0415
        return delib_jobs.pause_info() or delib_jobs.pause_state().snapshot()
    except Exception as exc:  # noqa: BLE001
        return {"on": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _external_review_active() -> int:
    """도는 외부 응답자 카드 대조 호출 수 — GLM 관문 자리를 잡지 않으므로 관문의 active 에 없다. review_active 에 더한다(update-all
    비우기가 그 수가 0 이 되기를 기다린다 — 재기동하면 과제 줄을 잃는다). 꺼진 박스는 0 이다."""
    board = _external_llm.board_of(app)
    return board.calls_active() if board is not None else 0


@app.get("/drain")
def get_drain():
    if _card_review is None:
        return JSONResponse({"code": "drain_unavailable", "message": f"카드 대조 모듈이 없다 — {_CARD_REVIEW_ERR}"},
                            status_code=503)
    return _drain_view(_card_review.gate_of(app))


@app.post("/drain")
async def post_drain(request: Request):
    from review_gate import DrainError  # noqa: PLC0415

    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = None
    # 새 심의 멈춤(scope delib)은 카드 대조 모듈 없이도 받는다. 그 밖은 종전 그대로 — 모듈이 없으면 무엇을 보내든 503 이다.
    if _card_review is None and not (isinstance(body, dict) and body.get("scope") == "delib"):
        return JSONResponse({"code": "drain_unavailable", "message": f"카드 대조 모듈이 없다 — {_CARD_REVIEW_ERR}"},
                            status_code=503)
    if body is None:
        return JSONResponse({"code": "bad_request", "message": "본문이 JSON 이 아니다"}, status_code=422)
    if not isinstance(body, dict):
        return JSONResponse({"code": "bad_request", "message": "본문은 JSON 객체여야 한다"}, status_code=422)
    extra = sorted(set(body) - set(_DRAIN_FIELDS))
    if extra:
        # 호출자가 보낸 칸 이름을 되비춘다 — 이름마다 80자, 20개까지(나머지는 수로만).
        shown = [_echo(x) for x in extra[:20]]
        return JSONResponse({"code": "unknown_fields", "message": f"선언하지 않은 칸 {len(extra)}개 {shown} — 조용히 버리지 "
                             "않고 거절한다", "fields": shown, "fields_n": len(extra)}, status_code=422)
    if not isinstance(body.get("on"), bool):
        return JSONResponse({"code": "bad_request", "message": "on 은 true|false 다"}, status_code=422)
    for k in ("hold", "force"):
        if not isinstance(body.get(k, False), bool):
            return JSONResponse({"code": "bad_request", "message": f"{k} 는 true|false 다"}, status_code=422)
    if body.get("until_idle") is not None and not isinstance(body["until_idle"], dict):
        return JSONResponse({"code": "bad_request", "message": "until_idle 은 {grace_s, max_s} 다"}, status_code=422)
    if body.get("scope") == "delib":
        return _post_delib_pause(body)
    gate = _card_review.gate_of(app)
    try:
        if body["on"]:
            snap = gate.drain.set_on(scope=body.get("scope"), by=body.get("by"), reason=body.get("reason") or "",
                                     ttl_s=body.get("ttl_s"), until_idle=body.get("until_idle"),
                                     hold=bool(body.get("hold")))
            print(f"[drain] 깃발 on scope={snap['scope']} by={snap['by']} hold={snap['hold']} until={snap['until']} "
                  f"until_idle={snap['until_idle']}")
        else:
            gate.drain.set_off(by=body.get("by") or "", force=bool(body.get("force")))
    except DrainError as exc:
        print(f"[drain] 거절 {exc.status} {exc.code} — {exc.message}"[:300])
        return JSONResponse(exc.body(), status_code=exc.status)
    except OSError as exc:                  # 깃발 파일을 못 쓰거나 못 지운다(권한·디스크) — 500·거짓 200 이 아니라 사유를 싣는다
        print(f"[drain] 깃발 파일을 쓰거나 지우지 못했다 — {type(exc).__name__}: {exc}"[:300])
        return JSONResponse({"code": "drain_unavailable", "message": f"비우기 깃발 파일을 쓰거나 지우지 못했다 — "
                             f"{type(exc).__name__}(DRAIN_STATE_PATH 의 권한·디스크를 본다)"}, status_code=503)
    return _drain_view(gate)


def _post_delib_pause(body: dict):
    """POST /drain {scope: "delib", ...} — 새 심의 멈춤을 세우거나 내린다. 규칙(수명 · 주인 · 고정 · 409 · 503)은 비우기 깃발과 같다."""
    import delib_jobs  # noqa: PLC0415
    from review_gate import DrainError  # noqa: PLC0415

    if body.get("until_idle") is not None:
        return JSONResponse({"code": "bad_request", "message": "새 심의 멈춤은 until_idle 을 받지 않는다 — ttl_s 나 hold 다(멈춤은 "
                             "심의가 비어도 풀리지 않는다 — 잡아 두고 재기동한다)"}, status_code=422)
    pause = delib_jobs.pause_state()
    try:
        if body["on"]:
            snap = pause.set_on(scope="delib", by=body.get("by"), reason=body.get("reason") or "", ttl_s=body.get("ttl_s"),
                                hold=bool(body.get("hold")))
            print(f"[drain] 새 심의 멈춤 on by={snap['by']} hold={snap['hold']} until={snap['until']}")
        else:
            pause.set_off(by=body.get("by") or "", force=bool(body.get("force")))
            print(f"[drain] 새 심의 멈춤 off by={str(body.get('by'))[:80]}")
            delib_jobs._pump()                      # 풀렸다 — 붙잡아 둔 줄을 그 자리에서 띄운다
    except DrainError as exc:
        print(f"[drain] 새 심의 멈춤 거절 {exc.status} {exc.code} — {exc.message}"[:300])
        return JSONResponse(exc.body(), status_code=exc.status)
    except OSError as exc:
        print(f"[drain] 새 심의 멈춤 파일을 쓰거나 지우지 못했다 — {type(exc).__name__}: {exc}"[:300])
        return JSONResponse({"code": "drain_unavailable", "message": f"새 심의 멈춤 파일을 쓰거나 지우지 못했다 — "
                             f"{type(exc).__name__}(DELIB_JOB_DIR 의 권한·디스크를 본다)"}, status_code=503)
    a, q = _delib_load()
    return {**_delib_pause_view(), "delib_active": a, "delib_queued": q}


def _delib_ops_health() -> dict:
    """/health 의 새 심의 멈춤 · 줄 넘기기(HWAXPortal docs/delib-ops R1 · R2). 모듈을 못 붙이면 멈춤 꺼짐 · 줄 넘기지 않음으로 낸다(재기동 판정이
    줄 선 수를 막는 수로 센다 — 종전 쪽)."""
    try:
        import delib_jobs  # noqa: PLC0415
        pause = _delib_pause_view()
        d = delib_jobs.queue_durability()
        return {"delib_paused": bool(pause.get("on")), "delib_pause": pause, "delib_queue_durable": d["durable"],
                "delib_requeued": d["requeued"], "delib_seats_running": delib_jobs.seats_running()}
    except Exception as exc:  # noqa: BLE001
        return {"delib_paused": False, "delib_pause": {"on": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
                "delib_queue_durable": False, "delib_requeued": 0, "delib_seats_running": None}


# ── 동시 심의 한도(HWAXPortal docs/delib-ops R4) ─────────────────────────────────
# GET /limits · POST /limits {max_running?, max_running_per_user?, queue_max?, max_seats_running?(정수 · null 은 그 칸의 덮개를 지운다), by, reason?,
# reset?} — max_seats_running 은 도는 GLM 잡의 좌석 합 상한(0 = 끔 — R4' · DO-D16).
# 박스 덮개(<잡 원장>/state/limits.json)가 env 위에 덮이고 판정 때마다 읽혀 재기동이 없다. 올리면 그 자리에서 줄 선 잡을 띄운다. 내리면 도는
# 잡은 그대로 두고 새 시작만 줄인다. 인증은 없다 — /drain 과 같다(서버는 루프백에만 묶인다). 포털은 이 주소를 중계하지 않는다.
_LIMITS_FIELDS = ("max_running", "max_running_per_user", "queue_max", "max_seats_running", "by", "reason", "reset")


@app.get("/limits")
def get_limits():
    import delib_jobs  # noqa: PLC0415 — /health 와 같다(심의 MCP 가 비활성이어도 답한다)
    return delib_jobs.limits()


@app.post("/limits")
async def post_limits(request: Request):
    import delib_jobs  # noqa: PLC0415

    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return JSONResponse({"code": "bad_request", "message": "본문이 JSON 이 아니다"}, status_code=422)
    if not isinstance(body, dict):
        return JSONResponse({"code": "bad_request", "message": "본문은 JSON 객체여야 한다"}, status_code=422)
    extra = sorted(set(body) - set(_LIMITS_FIELDS))
    if extra:
        shown = [_echo(x) for x in extra[:20]]
        return JSONResponse({"code": "unknown_fields", "message": f"선언하지 않은 칸 {len(extra)}개 {shown} — 조용히 버리지 "
                             "않고 거절한다", "fields": shown, "fields_n": len(extra)}, status_code=422)
    if not isinstance(body.get("reset", False), bool):
        return JSONResponse({"code": "bad_request", "message": "reset 은 true|false 다"}, status_code=422)
    vals = {k: body[k] for k in ("max_running", "max_running_per_user", "queue_max", "max_seats_running") if k in body}
    if not vals and not body.get("reset"):
        return JSONResponse({"code": "bad_request", "message": "바꿀 칸(max_running · max_running_per_user · queue_max · "
                             "max_seats_running)이나 reset 이 없다"}, status_code=422)
    try:
        out = delib_jobs.set_limits(vals, by=str(body.get("by") or ""), reason=str(body.get("reason") or ""),
                                    reset=bool(body.get("reset")))
    except ValueError as exc:
        return JSONResponse({"code": "bad_request", "message": str(exc)[:300]}, status_code=422)
    except OSError as exc:                  # 덮개 파일을 못 쓰거나 못 지운다(권한·디스크) — 거짓 200 이 아니라 사유를 싣는다
        return JSONResponse({"code": "limits_unavailable", "message": f"동시 한도 덮개 파일을 쓰거나 지우지 못했다 — "
                             f"{type(exc).__name__}(DELIB_JOB_DIR 의 권한·디스크를 본다)"}, status_code=503)
    print(f"[limits] 전역 {out['max_running']} · 사용자별 {out['max_running_per_user']} · 줄 {out['queue_max']} · 좌석 합 "
          f"{out['max_seats_running'] or '끔'} (by {str(body.get('by'))[:80]}{' · 덮개 지움' if body.get('reset') else ''})")
    return out


def _limits_health() -> dict:
    """/health 의 limits — 지금 걸린 동시 한도 셋과 출처(file · env). 모듈을 못 붙였으면 빈 사전(/health 는 늘 답한다)."""
    try:
        import delib_jobs  # noqa: PLC0415
        return delib_jobs.limits()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


@app.get("/health")
def health() -> dict:
    _active, _queued = _delib_load()
    _review = _card_review_health()
    _external = _external_llm_health()
    if _external:
        # 외부 응답자가 켜진 박스만 capabilities 에 external_llm 이 붙는다 — 리스크 앱은 이것을 보고 llm=claude 잡을 받는다(X-2).
        _review["capabilities"] = [*_review["capabilities"], "external_llm"]
        # 도는 외부 응답자 카드 대조 호출도 review_active 에 든다(GLM 관문 밖이다 — update-all 비우기가 이 수를 기다린다).
        _review["review_active"] = _review["review_active"] + _external["external_llm"]["inflight"]
    return {
        "status": "ok",
        # 지금 도는 심의·줄 선 심의 수. 재기동은 이것들을 전부 끊는다 — start.sh 와 update-all 이 이 수를 보고
        # 재기동을 건너뛴다(강행은 AGENT_RESTART_FORCE=1). 이름을 바꾸면 그쪽이 '모름' 으로 읽고 그대로 내린다.
        "delib_active": _active,
        "delib_queued": _queued,
        # 도는 분석 루틴 수 · 쉬는 루틴 수 · 동시 상한(routine_active · routine_parked · routine_max — R3-D7 · R3-design §8). routine_active 는 위
        # delib_active 에도 들어 있고, 쉬는 루틴(새 심의 멈춤 · 줄 선 심의를 기다린다)은 어디에도 더하지 않는다(update-all 의 기다림이 수렴하게).
        **_routine_health(),
        # 동시 심의 한도 셋과 출처 — 박스 덮개(POST /limits)가 env 위에 덮인다(HWAXPortal docs/delib-ops R4).
        "limits": _limits_health(),
        # 심의 기준일(delib_opts.as_of — HWAXPortal docs/request-1027 HA-11)을 받는 판이다. 포털은 기준일 심의를 넘기기 전에 이 표지를 본다 —
        # 옛 판은 그 칸을 말없이 버려 기준일을 준 소급 검증이 오늘의 카드로 돌았다(포털을 먼저 올리고 이 서버의 재기동이 미뤄진 창).
        "delib_as_of": True,
        # 새 심의 멈춤(delib_paused · delib_pause — R1) · 줄이 재기동을 넘기나(delib_queue_durable — 참이면 재기동 판정이 줄 선 수를 막는 수로
        # 세지 않는다, start.sh · 포털 lib/delib-busy.sh) · 이번 기동에 다시 세운 줄(delib_requeued — R2, update-all 이 재기동 뒤 찍는다) ·
        # 도는 GLM 잡의 좌석 합(delib_seats_running — 좌석 합 상한 limits.max_seats_running 이 본다, 앉기 전은 어림).
        **_delib_ops_health(),
        # 카드 대조는 위 두 수와 **다른 칸**이다(C-25) — 재기동을 막지 않는다(끊기면 앱이 차감 없이 다시 낸다).
        **_review,
        # 외부 응답자(켜진 박스만) — 과제 줄의 수. 재기동하면 줄을 잃는다(도는 호출은 앱이 인프라로 다시 낸다).
        **_external,
        # 챗 첫 조각 지연 — idle(카드 대조 0)·review(1 이상) 고리와 slow(review.p50 > 2 × idle.p50, 두 고리 10턴 이상).
        "load": {"chat": _CHAT_LOAD.snapshot()},
        "model": VLLM_MODEL,
        "vllm": VLLM_BASE_URL,
        # 심의 LLM 의 생성 파라미터 {temperature, top_p, max_tokens} — None 은 보내지 않는 값(HWAXPortal docs/design-risk-review §8.4.1).
        "sampling": getattr(app.state, "delib_sampling", None),
        # 챗 전용 LLM(CHAT_LLM_*) — 꺼져 있으면 {"enabled": false}(챗도 위 model · vllm). 켜져 있으면 넘김 횟수까지(docs/chat-llm-stcx).
        "chat_llm": _chat_llm_health(),
        # 챗 자기검증 — 켜짐 · 상태별 수 · 걸린 ms · 마지막 실패(HA-13 — 포털 update-all §7 스모크가 읽는다)
        "self_check": _sc_health(),
        # 챗 상태 기다림 — 켜짐 · 간격 · 턴 합 상한 · 기동 뒤 시작한 기다림 · 끝 · 상한 · 실패(꼴 바뀜 포함) 수(HWAXPortal docs/request-1030 HA-28)
        "status_wait": _status_wait_mod.health(),
        # 확인 관문(HA-29) — 켜짐 · 운영자 목록을 읽었나(loaded 가 거짓이면 잡 시작 바닥 목록만 돈다 — 형제 리포 경로부터) · 살아 있는 카드 수 · 셈
        "confirm_gate": _confirm_health(),
        # 다른 허브 id 막이 — 손잡이(CHAT_XHUB_ID_GATE)와 기동 뒤 막은 호출 수(HWAXPortal docs/request-1030 HA-30 · design-integration C-8). 재시험
        # OT-07 은 이 수와 로그의 '[gate] … 다른 허브 id' 줄을 함께 본다.
        "xhub_gate": _xhub_gate_health(),
        # 코어 어휘표를 읽었나 — 못 읽으면 챗 코어 규칙이 기본 글로 돈다(HWAXPortal docs/request-1029 C-11)
        "core_vocab": _core_vocab_health(),
        # 마지막 챗 도구 절단 — 스키마 예산(창의 45%)을 넘어 뺀 수 · 앱별 수 · 상한. 없으면 null(아직 안 잘렸다 — 기동 뒤 첫 챗 턴 전이거나
        # 다 들었다). update-all 헬스게이트가 '챗 도구 절단 N개' 로 읽는다(HWAXPortal docs/chat-depth-context F1 · F3).
        "tool_cut": dict(_TOOL_CUT) or None,
        # 허브 응답 접기 — 기동 뒤 글이 줄어든 접기의 횟수 · 원문 자 수 합 · 접은 뒤 자 수 합(HWAXPortal docs/request-1030 HA-34). 챗 · 심의의
        # 도구 결과를 함께 센다(같은 래퍼다). count 가 0 이면 아직 한 번도 접지 않았다. 큰 응답이 접혔는지는 합이 아니라 로그 줄의 원문 자 수로 가른다.
        "hub_fold": _hub_fold_health(),
        # 그림을 받는가 — 서버(호스트 · 모델)마다 탐침 결과(None 은 아직 모름 — 첫 그림 턴에 묻는다). docs/chat-depth-context T5.
        "vision": _vision.state(),
        # 심의 좌석이 조회로 받은 그림의 판독 — 지금 상한 · 그 값을 준 손잡이 · 판독기가 엔진에 달렸나(HA-17)
        "seat_vision": {"max": _seat_vision_max(), "on": _seat_vision_max() > 0,
                        "knob": "DELIB_SEAT_VISION_MAX" if os.environ.get("DELIB_SEAT_VISION_MAX", "").strip() else "VISION_TRANSCRIBE_MAX",
                        "reader": getattr(_delib_engine, "SEAT_IMAGE_READER", None) is not None},
        "mcp": list(getattr(app.state, "connections", {})),
        "tool_scoping": "gateway (X-HWAX-Groups)",
        "tool_desc_max": TOOL_DESC_MAX,
        "tool_max": TOOL_MAX,   # 0=무제한. >0 이고 게이트웨이 도구수보다 작으면 챗에 일부 도구 미바인딩
        # 예산은 이제 모델 컨텍스트에서 유도한다 — 원격 진단에서 실제 값이 보여야 한다.
        "context_tokens": _model_context_tokens(),
        # 그 창을 정한 근거 — 서버 답 · 사람이 적은 상한 · 넓히지 않은 큰 답(HWAXPortal docs/delib-ops R5). context_tokens 뒤에 둔다(첫 조회가 먼저다).
        "context": _context_health(),
        # 좌석 프롬프트에 실을 수 있는 원천 근거의 글자 수(창에서 유도한다 — 요청 때 잰다). 리스크 앱이 이 값으로
        # 브리프 예산을 잡는다 — 이름을 바꾸면 그쪽이 옛 한도로 떨어진다(tests/test_risk_app_contract_anchors).
        "evid_budget": _evid_budget(),
        "hist_budget": _hist_budget_chars(),                  # 문서 없이 대화만 할 때
        "hist_budget_with_docs": _hist_budget_chars(has_docs=True),
        "doc_budget": _doc_total_chars(),
        # 최근 도구 로드 실패 사유(그룹셋별) — '도구가 없다'는 증상의 원인을 원격에서 본다.
        # 비어 있으면 최근 로드가 전부 성공. 값이 있으면 401/타임아웃 등 실사유가 찍힌다.
        "tool_load_errors": {" ".join(sorted(g)) or "(none)": v
                             for g, v in (getattr(app.state, "tool_load_error", None) or {}).items()},
    }

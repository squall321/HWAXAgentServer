# HWAX 전문가 심의를 MCP 도구로 노출한다 — 포털 웹이 GLM 으로 하는 것과 같은 것을 MCP 클라이언트에서.
"""심의 MCP 서버 — `app.py` 가 `/mcp` 로 mount 하는 streamable-http 앱.

**해결하는 문제.** 게이트웨이 도구 목록에 심의 진입점이 하나도 없었다. 그래서 MCP 클라이언트가
"심의" 를 찾으면 이름에 '심의' 가 들어간 유일한 도구군(발표자료 생성기)으로 수렴했고,
사용자가 시뮬레이션 심의를 시키면 슬라이드를 만들었다. 진입점이 없으면 랭킹이 빗나간 게 아니라
정답이 목록에 없는 것이다.

**파리티 기준은 포털 웹이다.** 웹(GLM)에서 되던 것이 MCP 에서도 똑같이 돼야 한다.
Job 7종은 `frontend/src/components/chat/delibTaxonomy.ts`, 손잡이는 `deliberation.py:_resolve_opts`
화이트리스트가 정본이고, 이 파일은 그 둘을 도구 인자로 옮긴 것뿐이다. 엔진은 같다.

**계약.** 심의는 길다(수 분~수 시간). MCP 도구 호출은 동기라 한 번에 못 끝낸다.
`deliberate_start` 로 열고 `deliberate_status` 로 지켜보고 `deliberate_result` 로 회수한다.
"""
from __future__ import annotations

import logging
from urllib.parse import unquote

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import delib_jobs

log = logging.getLogger("agent.mcp")

_JOB_LINES = "\n".join(
    f"  · {k} ({v['group']}·{v['label']}) — {v['what']}" for k, v in delib_jobs.JOBS.items())

_INSTRUCTIONS = f"""HWAX 전문가 심의 엔진.

여러 도메인 전문가 좌석이 라운드를 돌며 도구 근거 위에서 의견을 수렴시켜 결정 문서를 만든다.
포털 웹의 심의 메뉴와 **같은 엔진·같은 좌석 규율**이고, 여기서는 MCP 로 연다.
사용자가 '심의', '원인 규명', '안 선택', '신뢰 판정', '해석 설계', '시험 계획', '구축 계획' 을
요청하면 여기서 시작한다.

심의 종류(job):
{_JOB_LINES}

심의는 수 분에서 수 시간 걸린다. 그래서 3단이다 —
deliberate_start 로 열고, deliberate_status 로 진행을 보고, deliberate_result 로 결정문을 받는다.
start 는 즉시 돌아온다. 응답을 기다리며 붙잡고 있지 마라.

⚠ 이 서버의 도구가 HWAX 공학 심의의 정본 진입점이다. 이름이 비슷한 meeting_* 도구는
발표자료(슬라이드) 제작용 디자인 회의체이지 공학 심의가 아니다."""

mcp = FastMCP(
    name="hwax-deliberation",
    instructions=_INSTRUCTIONS,
    stateless_http=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_APP = None


def bind(app) -> None:
    global _APP  # noqa: PLW0603 — 프로세스당 하나뿐인 FastAPI 앱을 늦게 주입한다
    _APP = app
    try:
        delib_jobs.reap_orphans()   # 재기동으로 죽은 running 잡을 interrupted 로 정리
    except Exception:  # noqa: BLE001
        log.exception("잡 원장 정리 실패 — 계속")


def _need_app():
    if _APP is None:
        raise RuntimeError("에이전트 서버가 아직 준비되지 않았다 — 잠시 뒤 다시")
    return _APP


def _caller(ctx: Context | None) -> tuple[str, list[str]]:
    """게이트웨이가 검증된 PAT claims 로 실어 주는 신원 헤더를 꺼낸다.

    이게 없으면 모든 MCP 심의가 서비스 계정·빈 그룹 시야로 돌아, 사용자 스코프 앱(DynaForge 등)이
    '내 것이 하나도 없다' 로 답한다. 헤더 접근 경로는 전송 구현에 따라 다르므로 전부 실패해도
    심의는 계속 간다(종전 동작 = 서비스 계정)."""
    if ctx is None:
        return "", []
    try:
        req = getattr(ctx.request_context, "request", None)
        hdr = getattr(req, "headers", None)
        if hdr is None:
            return "", []
        user = (hdr.get("x-hwax-user") or "").strip()
        raw = (hdr.get("x-hwax-groups") or "").strip()
        # 퍼센트 인코딩을 되돌린다 — 게이트웨이는 한글 그룹명 때문에 인코딩해 보내고, 권한 키의
        # `:` 까지 인코딩되면(feat%3Achat) 그대로 쓰는 순간 전 키가 무효가 된다(실측: 심의 좌석 0명).
        raw = unquote(raw)
        groups = [g.strip() for g in raw.replace(";", ",").split(",") if g.strip()]
        return user, groups
    except Exception:  # noqa: BLE001 — 신원 확보 실패가 심의를 막으면 안 된다
        log.debug("호출자 신원 헤더를 못 읽었다 — 서비스 계정으로 진행", exc_info=True)
        return "", []


def _build_opts(*, rounds: int = 0, modifiers=None, evidence=None, personas=None,
                tools=None, apps=None, human_note: str = "", search_sources=None,
                continue_summary: str = "", non_negotiables=None, options=None,
                stop_after_round: int = 0, rounds_so_far: int = 0,
                save_report: bool = True, append_to_report_id: int = 0,
                advanced=None) -> dict:
    """도구 인자 → deliberation._resolve_opts 화이트리스트 dict.

    값 검증·클램프는 엔진이 한다(신뢰 안 되는 입력을 전제로 짜여 있다). 여기서는 모양만 맞춘다."""
    o: dict = {}
    if rounds:
        o["rounds"] = int(rounds)
    if modifiers:
        o["modifiers"] = [str(m).strip() for m in modifiers if str(m).strip()]
    if evidence:
        o["evidence"] = [e for e in evidence if isinstance(e, dict)]
    if personas:
        o["personas"] = [p for p in personas if isinstance(p, dict) and p.get("key")]
    if tools:
        o["tools"] = [str(t).strip() for t in tools if str(t).strip()]
    if apps:
        o["apps"] = [str(a).strip() for a in apps if str(a).strip()]
    if human_note:
        o["human_note"] = str(human_note)
    if search_sources is not None:
        o["search_sources"] = [str(s).strip() for s in search_sources if str(s).strip()]
    if continue_summary:
        o["continue_summary"] = str(continue_summary)
    if non_negotiables:
        o["non_negotiables"] = [str(x) for x in non_negotiables if str(x).strip()]
    if stop_after_round:
        o["stop_after_round"] = int(stop_after_round)
    if options:
        o["options"] = options if isinstance(options, str) else [str(x) for x in options if str(x).strip()]
    if rounds_so_far:
        o["rounds_so_far"] = int(rounds_so_far)
    if not save_report:
        o["save_report"] = 0
    if append_to_report_id:
        o["append_to_report_id"] = int(append_to_report_id)
    if isinstance(advanced, dict):
        o.update({k: v for k, v in advanced.items() if v is not None})
    return o


_START_DESC = (
    "HWAX 전문가 심의를 시작한다. 사용자가 '심의해줘'·'원인 규명'·'불량 원인'·'안 선택'·"
    "'트레이드오프'·'신뢰 판정'·'리스크 심사'·'위험 도출'·'해석 설계'·'시뮬레이션 심의'·"
    "'시험 계획'·'시험 설계'·'구축 계획'·'메커니즘 규명' 을 요청하면 이 도구를 쓴다. "
    "포털 웹 심의와 같은 엔진이다. 여러 전문가 좌석이 라운드를 돌며 도구 근거 위에서 수렴해 "
    "결정 문서를 만든다. **즉시 job_id 를 돌려주고 심의는 뒤에서 계속 돈다** — "
    "결과는 deliberate_status / deliberate_result 로 받는다. 응답을 붙잡고 기다리지 마라. "
    "어떤 job 을 골라야 할지 모르면 deliberate_jobs 를 먼저 부른다."
)


@mcp.tool(title="심의 시작 (원인규명·안선택·신뢰판정·해석설계·시험설계·구축계획·자유)",
          description=_START_DESC)
async def deliberate_start(
    question: str,
    job: str = "default",
    rounds: int = 0,
    modifiers: list[str] | None = None,
    evidence: list[dict] | None = None,
    personas: list[dict] | None = None,
    tools: list[str] | None = None,
    apps: list[str] | None = None,
    human_note: str = "",
    search_sources: list[str] | None = None,
    options: list[str] | None = None,
    stop_after_round: int = 0,
    save_report: bool = True,
    advanced: dict | None = None,
    ctx: Context | None = None,
) -> dict:
    """심의를 연다.

    Args:
        question: 심의할 화두. 구체적일수록 좌석 발굴이 정확하다.
        job: 심의 종류 — diagnosis(원인 규명) · option-select(안 선택) · credibility(신뢰 판정) ·
             risk-review(리스크 심사) · mechanism(메커니즘 규명) · sim-plan(해석 설계 2단) ·
             test-plan(시험 설계) · build-plan(구축 계획 3단) · default(자유 심의).
             deliberate_jobs 로 목록을 본다.
        rounds: 라운드 수. 0 이면 기본값 3. 2~8 로 클램프된다.
        modifiers: 얹을 층 — voi(교착 정산) · premortem(사전부검) · toulmin(논증 엄밀) ·
                   eliminative(완결 기준) · anon1r(익명 1R). 최대 5개.
        evidence: 원천 근거 주입(최대 40, 항목당 12,000자). [{source, tool, args, result}] — 이미 도구로 뽑아 둔
                  결과를 좌석에 '검증 대상'으로 깐다. 결론이 아니라 원천만 넣어라.
        personas: 좌석 지정(최대 20 — deliberation.MAX_REQ_SEATS). [{key, role}] — 비우면 서버가
                  recommend_agents 로 발굴한다.
        tools: 심의 시작 전 실제로 호출해 정량 근거로 깔 도구 이름(최대 6).
        apps: 좌석 자유 조회 범위를 이 앱들로 좁힌다(최대 3).
        human_note: 사람 의견 주입 — 매 라운드 좌석에 전달된다(최대 2000자).
        search_sources: 웹 리서치 소스 토글. 지정하면 인용 계약이 강제된다.
        options: 후보안 목록(안 선택용, 최대 8). 2개 이상이면 최종 라운드가 이 중에서 고르는 표결을
                 요구한다. 없으면 표결을 강제하지 않는다 — 후보 없이 표를 받으면 좌석이 방금 자기가
                 함께 만든 결론에 찬성표를 던져 정보량이 0 이 된다.
        stop_after_round: 1 이면 초기 라운드까지만 돌고 사람 검토를 기다린다(체크포인트).
        save_report: False 면 Report Archive 저장을 건너뛴다. 탐색적 심의로 아카이브를 어지럽히지
                     않으려 할 때. 기본 True.
        advanced: 품질 손잡이 그대로 전달 — free_tools · tool_budget · chair_bestof · chair_cite ·
                  rebut_quote · cross_exam · anchor · evidence_prepass · prose_first ·
                  parse_retries · timeout_s. 보통 비운다.
    """
    opts = _build_opts(rounds=rounds, modifiers=modifiers, evidence=evidence, personas=personas,
                       tools=tools, apps=apps, human_note=human_note, options=options,
                       search_sources=search_sources, stop_after_round=stop_after_round,
                       save_report=save_report, advanced=advanced)
    user, groups = _caller(ctx)
    rec = delib_jobs.start(_need_app(), job, question, delib_opts=opts or None,
                           groups=groups, user_email=user)
    out = delib_jobs.summary(rec)
    out["caller"] = user or "(서비스 계정 — 게이트웨이가 신원 헤더를 안 보냈다)"
    out["note"] = ("심의를 시작했다. 진행은 deliberate_status(job_id), 결정문은 "
                   "deliberate_result(job_id). 보통 수 분~수십 분 걸리므로 즉시 다시 묻지 말고 "
                   "사용자에게 job_id 를 알려라.")
    return out


@mcp.tool(
    title="심의 이어하기",
    description=("끝난 HWAX 심의에 사람 의견을 넣어 이어서 돌린다. 이전 좌석과 양보 불가 조항을 "
                 "승계하므로 처음부터 다시 돌리는 것보다 싸고 결론이 되돌아가지 않는다."),
)
async def deliberate_continue(
    previous_job_id: str,
    human_note: str,
    question: str = "",
    job: str = "",
    rounds: int = 0,
    non_negotiables: list[str] | None = None,
    keep_seats: bool = True,
    append_report: bool = True,
    modifiers: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """이전 심의를 이어 돌린다.

    Args:
        previous_job_id: 이어갈 심의의 job_id.
        human_note: 넣을 사람 의견. 이것이 이어하기의 핵심이다 — 패널이 갖지 못한 관측을 넣는다.
        question: 화두를 바꾸려면 지정. 비우면 이전 화두를 그대로 쓴다.
        job: 심의 종류를 바꾸려면 지정. 비우면 이전과 같다.
        rounds: 라운드 수. 0 이면 기본값.
        non_negotiables: 이전 결정의 양보 불가 조항. 요약에 섞으면 소실되므로 따로 넘긴다.
        keep_seats: True 면 이전 좌석을 그대로 앉힌다(발굴 생략). False 면 다시 발굴한다.
        append_report: True 면 이전 회차의 Report Archive 보고서에 페이지로 이어붙인다 — 한 사안이
                       보고서 여러 건으로 흩어지지 않는다. 이전 보고서가 없으면 새로 만든다.
        modifiers: 이번 회차에 얹을 층.

    라운드 번호는 이전 회차에 이어서 센다 — 3회차 회의록이 매번 '1R' 로 돌아가면
    어느 회차의 발언인지 구분이 안 된다.
    """
    prev = delib_jobs.get(previous_job_id)
    if not prev:
        raise ValueError(f"그런 심의 잡이 없다: {previous_job_id} — deliberate_list 로 확인하라")
    summary_text = (prev.get("decision") or prev.get("result_text") or "").strip()
    if not summary_text:
        raise ValueError(f"이전 심의에 결정문이 없다(status={prev.get('status')}) — 끝난 뒤 이어하라")
    opts = _build_opts(
        rounds=rounds, modifiers=modifiers, human_note=human_note,
        continue_summary=summary_text[:8000],
        non_negotiables=non_negotiables,
        personas=(prev.get("seats") or []) if keep_seats else None,
        rounds_so_far=delib_jobs.rounds_end(prev),
        append_to_report_id=(int(prev.get("report_id") or 0) if append_report else 0),
    )
    user, groups = _caller(ctx)
    rec = delib_jobs.start(_need_app(), job or prev.get("job") or "default",
                           question or prev["question"], delib_opts=opts,
                           groups=groups, user_email=user)
    out = delib_jobs.summary(rec)
    out["continued_from"] = previous_job_id
    out["rounds_start_at"] = delib_jobs.rounds_end(prev) + 1
    out["appending_to_report"] = (prev.get("report_id") if append_report else None)
    return out


@mcp.tool(title="심의 진행 상황",
          description="진행 중인 HWAX 심의의 단계·라운드·좌석을 본다. 결정문은 아직 없을 수 있다.")
async def deliberate_status(job_id: str) -> dict:
    """심의 진행 상황. status 가 done 이면 deliberate_result 로 결정문을 받는다."""
    job = delib_jobs.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다: {job_id} — deliberate_list 로 확인하라")
    return delib_jobs.summary(job)


@mcp.tool(title="심의 결과(결정 문서) 회수",
          description="끝난 HWAX 심의의 결정 문서 전문을 받는다. 아직 진행 중이면 현재 단계만 돌려준다.")
async def deliberate_result(job_id: str) -> dict:
    """결정 문서 전문·쉬운 설명·좌석 구성·저장된 보고서 id."""
    job = delib_jobs.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다: {job_id} — deliberate_list 로 확인하라")
    out = delib_jobs.summary(job, full=True)
    if job.get("status") == "running":
        out["note"] = "아직 진행 중이다. 잠시 뒤 다시 부르거나 deliberate_status 로 지켜봐라."
    return out


@mcp.tool(title="심의 목록",
          description="최근 HWAX 심의 잡 목록. 진행 중인 것과 끝난 것을 최신순으로 본다.")
async def deliberate_list(limit: int = 20) -> dict:
    """최근 심의를 최신순으로. job_id 를 잊었을 때 여기서 찾는다."""
    rows = delib_jobs.list_jobs(max(1, min(100, int(limit or 20))))
    return {"jobs": rows, "running": sum(1 for r in rows if r["status"] == "running"),
            "running_max": delib_jobs.MAX_RUNNING}


@mcp.tool(title="심의 취소",
          description="진행 중인 HWAX 심의를 접는다. 동시 실행 상한에 걸렸을 때 자리를 비운다.")
async def deliberate_cancel(job_id: str) -> dict:
    """진행 중인 심의를 취소한다. 저장(대화·보고서)도 함께 중단된다."""
    return delib_jobs.cancel(job_id)


@mcp.tool(
    title="심의 전사 — 좌석별 라운드 발언",
    description=("HWAX 심의에서 어느 좌석이 몇 라운드에 무엇을 말했는지 원문을 본다. "
                 "결정문만으로 부족할 때, 그리고 리스크 원장에 패널 결과를 제출할 때 쓴다."),
)
async def deliberate_transcript(job_id: str, round: int = 0, seat: str = "",
                                offset: int = 0, limit: int = 40) -> dict:
    """좌석 발언 전사를 페이지로. 전량은 컨텍스트를 터뜨리므로 기본 40턴씩 준다.

    Args:
        job_id: 심의 잡 id.
        round: 특정 라운드만. 0 이면 전체.
        seat: 특정 좌석 키가 포함된 발언만.
        offset: 건너뛸 턴 수 · limit: 가져올 턴 수(≤200).
    """
    return delib_jobs.transcript(job_id, rnd=(round or None), seat=seat,
                                 offset=offset, limit=limit)


@mcp.tool(title="심의 메뉴 — 어떤 심의를 고를까",
          description="심의 종류 7가지와 각각 언제 쓰는지, 얹을 수 있는 층 5가지, 옵션 목록.")
async def deliberate_jobs() -> dict:
    """포털 웹 심의 메뉴와 같은 택소노미. job 값을 고르는 데 쓴다."""
    return {
        "jobs": [{"job": k, "group": v["group"], "label": v["label"], "when": v["what"],
                  "input": v["input"],
                  "example": f'deliberate_start(job="{k}", question="<{v["input"]}>")'}
                 for k, v in delib_jobs.JOBS.items()],
        "modifiers": {
            "voi": "교착 정산 — 패널이 값으로 못 가르고 막힐 때",
            "premortem": "사전부검 — 결정 굳기 전 실패를 미리 막고 싶을 때",
            "toulmin": "논증 엄밀 — 주장이 근거 없이 세질 때",
            "eliminative": "완결 기준 — 언제 끝인지 모호할 때",
            "anon1r": "익명 1R — 초반 쏠림·거수기 우려",
        },
        "options": {
            "evidence": "이미 뽑아 둔 도구 결과·문서 추출문을 원천 근거로 주입(≤40) — 결론 말고 원천만",
            "personas": "좌석 직접 지정(≤20). 비우면 서버가 발굴한다",
            "tools": "심의 전 실제 호출할 도구(≤6) · apps: 좌석 자유 조회 범위(≤3)",
            "human_note": "사람 의견 주입 — 매 라운드 좌석에 전달",
            "options": "후보안 목록(≤8). 2개 이상이면 최종 라운드가 그 중에서 고르는 표결을 요구한다",
            "stop_after_round": "1 이면 초기 라운드에서 멈추고 사람 검토를 기다린다",
            "save_report": "False 면 RA 저장을 건너뛴다(탐색적 심의)",
            "advanced": "free_tools·tool_budget·chair_bestof·chair_cite·rebut_quote·cross_exam·"
                        "anchor·evidence_prepass·prose_first·parse_retries·timeout_s",
        },
        "running_max": delib_jobs.MAX_RUNNING,
        "note": "meeting_* 도구는 발표자료 제작용 디자인 회의체다 — 공학 심의가 아니다.",
    }


app = mcp.streamable_http_app()

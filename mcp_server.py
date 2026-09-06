# HWAX 전문가 심의를 MCP 도구로 노출한다 — 게이트웨이에 붙은 어떤 클라이언트에서도 심의를 열 수 있게.
"""심의 MCP 서버 — `app.py` 가 `/mcp` 로 mount 하는 streamable-http 앱.

**해결하는 문제.** 게이트웨이 도구 목록에 심의 진입점이 하나도 없었다. 그래서 MCP 클라이언트가
"심의" 를 찾으면 이름에 '심의' 가 들어간 유일한 도구군(발표자료 생성기)으로 수렴했고,
사용자가 시뮬레이션 심의를 시키면 슬라이드를 만들었다. 진입점이 없으면 랭킹이 빗나가는 게 아니라
정답이 목록에 없는 것이다.

**계약.** 심의는 길다(수 분~수 시간). MCP 도구 호출은 동기라 한 번에 못 끝낸다.
`deliberate_start` 로 열고 `deliberate_status` 로 지켜보고 `deliberate_result` 로 회수한다.
실행 본체와 저장(대화·보고서)은 종전 경로 그대로다 — 이 파일은 진입만 연다.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import delib_jobs

log = logging.getLogger("agent.mcp")

_KIND_LINES = "\n".join(f"  · {k} — {v['label']}: {v['what']}" for k, v in delib_jobs.KINDS.items())

_INSTRUCTIONS = f"""HWAX 전문가 심의 엔진.

여러 도메인 전문가 좌석이 라운드를 돌며 도구 근거 위에서 의견을 수렴시켜 의사결정문을 만든다.
사용자가 '심의', '전문가 심의', '시뮬레이션 심의', '시험 계획' 을 요청하면 여기서 시작한다.

심의 종류:
{_KIND_LINES}

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

# app.py 가 mount 시점에 채운다 — FastAPI 앱 인스턴스(도구 연결·LLM 이 여기 붙어 있다).
_APP = None


def bind(app) -> None:
    global _APP  # noqa: PLW0603 — 프로세스당 하나뿐인 FastAPI 앱을 늦게 주입한다
    _APP = app


def _need_app():
    if _APP is None:
        raise RuntimeError("에이전트 서버가 아직 준비되지 않았다 — 잠시 뒤 다시")
    return _APP


@mcp.tool(
    title="심의 시작 (전문가 심의·시뮬레이션 심의·시험 계획)",
    description=(
        "HWAX 전문가 심의를 시작한다. 사용자가 '심의해줘'·'전문가 심의'·'시뮬레이션 심의'·"
        "'해석 설계'·'시험 계획' 을 요청하면 이 도구를 쓴다. 여러 전문가 좌석이 라운드를 돌며 "
        "도구 근거 위에서 수렴해 의사결정문을 만든다. **즉시 job_id 를 돌려주고 심의는 뒤에서 계속 돈다** — "
        "결과는 deliberate_status / deliberate_result 로 받는다. 응답을 붙잡고 기다리지 마라."
    ),
)
async def deliberate_start(
    question: str,
    kind: str = "general",
    rounds: int = 0,
    modifiers: list[str] | None = None,
) -> dict:
    """심의를 연다.

    Args:
        question: 심의할 화두. 한 문장으로 구체적일수록 좌석 발굴이 정확하다.
        kind: general(범용 전문가 심의) · sim(시뮬레이션 2단 — 메커니즘→해석 설계) ·
              test-plan(시험 계획 — 무엇을 먼저 측정할 것인가). 기본 general.
        rounds: 라운드 수. 0 이면 서버 기본값(보통 3). 2~8 로 클램프된다.
        modifiers: 얹을 심의 층. voi(정보가치) · premortem(사전부검) · toulmin(논증구조) ·
                   eliminative(소거법) · anon1r(1라운드 익명). 없으면 생략.
    """
    opts: dict = {}
    if rounds:
        opts["rounds"] = int(rounds)
    if modifiers:
        opts["modifiers"] = [str(m) for m in modifiers if str(m).strip()]
    job = delib_jobs.start(_need_app(), kind, question, delib_opts=opts or None)
    s = delib_jobs.summary(job)
    s["note"] = ("심의를 시작했다. 진행은 deliberate_status(job_id) 로 보고, "
                 "끝나면 deliberate_result(job_id) 로 결정문을 받는다. "
                 "보통 수 분~수십 분 걸리므로 즉시 다시 묻지 말고 사용자에게 job_id 를 알려라.")
    return s


@mcp.tool(
    title="심의 진행 상황",
    description="진행 중인 HWAX 심의의 단계·라운드·좌석을 본다. 결정문은 아직 없을 수 있다.",
)
async def deliberate_status(job_id: str) -> dict:
    """심의 진행 상황을 돌려준다. status 가 done 이면 deliberate_result 로 결정문을 받는다."""
    job = delib_jobs.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다: {job_id} — deliberate_list 로 확인하라")
    return delib_jobs.summary(job)


@mcp.tool(
    title="심의 결과(의사결정문) 회수",
    description="끝난 HWAX 심의의 의사결정문 전문을 받는다. 아직 진행 중이면 현재 단계만 돌려준다.",
)
async def deliberate_result(job_id: str) -> dict:
    """의사결정문 전문과 쉬운 설명, 저장된 보고서 id 를 돌려준다."""
    job = delib_jobs.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다: {job_id} — deliberate_list 로 확인하라")
    out = delib_jobs.summary(job, full=True)
    if job.get("status") == "running":
        out["note"] = "아직 진행 중이다. 잠시 뒤 다시 부르거나 deliberate_status 로 지켜봐라."
    return out


@mcp.tool(
    title="심의 목록",
    description="최근 HWAX 심의 잡 목록. 진행 중인 것과 끝난 것을 최신순으로 본다.",
)
async def deliberate_list(limit: int = 20) -> dict:
    """최근 심의를 최신순으로. job_id 를 잊었을 때 여기서 찾는다."""
    return {"jobs": delib_jobs.list_jobs(max(1, min(100, int(limit or 20))))}


@mcp.tool(
    title="심의 종류 안내",
    description="어떤 심의를 골라야 하는지 — 종류별 쓰임과 시작 예시를 돌려준다.",
)
async def deliberate_kinds() -> dict:
    """심의 종류와 언제 무엇을 쓰는지."""
    return {
        "kinds": [{"kind": k, "label": v["label"], "when": v["what"],
                   "example": f'deliberate_start(kind="{k}", question="...")'}
                  for k, v in delib_jobs.KINDS.items()],
        "modifiers": {
            "voi": "정보가치 — 무엇을 더 알면 결정이 바뀌는가",
            "premortem": "사전부검 — 이 결정이 실패했다고 가정하고 원인을 역추적",
            "toulmin": "논증구조 — 주장·근거·보증·반박을 분리",
            "eliminative": "소거법 — 후보를 지워 나가며 좁힌다",
            "anon1r": "1라운드 익명 — 앵커링 차단",
        },
        "running_max": delib_jobs.MAX_RUNNING,
        "note": "meeting_* 도구는 발표자료 제작용 디자인 회의체다 — 공학 심의가 아니다.",
    }


app = mcp.streamable_http_app()

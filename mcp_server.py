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
import deliberation as _engine   # 근거 상한·본문 키를 **읽어서** 설명을 만든다(_EVID_DESC)

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
동시 실행 상한이 차 있으면 start 는 status=queued 로 돌아온다 — 줄을 선 것이고 자리가 나면 스스로
시작한다. 다시 시작하지 말고 deliberate_status 로 순번(queue)을 본다.

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


# 근거 인자 안내 — **숫자를 손으로 적지 않는다.** 종전 독스트링은 "최대 40, 항목당 12,000자" 를
# 적어 두었는데 엔진 상수(_EVID_ITEM_MAX)는 150,000 이었다. 실사용 팀이 그 글을 믿고 제 근거를
# 미리 잘라 92% 를 버렸다(S26U 피드백 1-4, 2026-10-07). 상수에서 읽어 만들면 낡을 수 없다.
# 그리고 그 독스트링은 클라이언트에 **간 적이 없다** — description 을 따로 넘기면 FastMCP 는
# 독스트링을 안 쓴다. 호출자가 받는 글(_START_DESC)에 실어야 읽힌다.
# ⚠ 합계 예산(_evid_budget)은 여기서 재지 않는다. 모델 컨텍스트에서 유도하는 값인데, 이 모듈은
#   app.py 가 반쯤 import 된 시점에 로드돼 그 조회가 실패하고, 엔진은 폴백 값을 프로세스 내내
#   캐시한다(사본으로 실측: 16K 창인데 예산이 128K 기준 17,967자로 굳는다 — 맞는 값은 2,000자.
#   좌석 프롬프트가 창을 넘긴다). 부를 때 재서 준다(_evid_limits).
_EVID_DESC = (
    f"evidence(원천 근거) — [{{source, tool, args, result, key}}], 최대 {_engine._EVID_ITEMS}건. "
    "**본문은 `result` 에 넣는다**"
    f"({'·'.join(_engine._EVID_BODY_KEYS[1:])} 도 차례로 찾지만 정본은 result 다). "
    f"**미리 자르지 마라** — 항목당 천장은 {_engine._EVID_ITEM_MAX:,}자이고, 넘으면 엔진이 덜어낸 뒤 "
    "무엇을 뺐는지 본문에 밝힌다(낱장 표지 `## [s.N]`·`## [p.N]` 가 있으면 그 경계에서 가운데를, "
    "없으면 뒤쪽을 덜어낸다). 먼저 걸리는 것은 **합계 예산**이다 — 모델 컨텍스트에서 유도되고"
    f"(천장 {_engine._EVID_BUDGET:,}자, 작은 창에서는 훨씬 작다) 항목 하나도 이 값을 넘지 못한다. "
    "앞 항목부터 채우다 넘치면 뒤 항목은 통째로 빠지므로, 여러 건이면 합이 deliberate_jobs 의 "
    "limits(지금 걸리는 값) 안에 들게 하고 중요한 것을 앞에 둬라. 빠진 항목(예산·건수 초과, 본문 "
    "없음)은 deliberate_status 의 evidence_omitted 에 뜬다. 호출자 표식(예: E1-CH-015)은 `source` 에 "
    "넣으면 [e:N] 옆에 그대로 찍힌다. 제 번호를 `key`(선택 — 영문·숫자·`_.-`, "
    f"{_engine._EVID_KEY_MAX}자 이내)에 넣으면 표지가 [e:N|KEY] 로 찍혀, 엔진 번호 N 이 빠진 항목 "
    "때문에 밀려도 결정문의 인용을 제 원장과 맞춰 볼 수 있다. 결론이 아니라 원천만 넣어라."
)

# advanced 로 넘기는 손잡이 — 도구 설명과 deliberate_jobs 가 같은 글을 쓴다. voc·chair_template 은
# 엔진이 처음부터 받았는데 어디에도 안 적혀 있어, 소급 검증에 최근 VOC 가 섞여 들어갔다.
# persona_knowledge 는 종전에 환경변수뿐이라 한 심의만 끌 방법이 없었다(S26U 피드백 1-5).
# 합성 좌석을 여는 설정(DELIB_KNOWLEDGE_SYNTHETIC_SEATS)은 서버 쪽 손잡이지만 여기에 적는다 — 반대석을
# 등록하고도 지식이 안 실리는 까닭을 호출자가 알 자리가 이 글뿐이다. 적힌 키는 엔진에서 읽는다.
# sealed 가 닫는 것·닫지 않는 것은 엔진 표에서 읽어 적는다 — 손으로 적으면 엔진에 유입 경로가 늘 때
# 이 글만 낡는다(근거 상한 '12,000자' 가 그렇게 낡았다).
_ADV_DESC = (
    "advanced(품질 손잡이 dict, 보통 비운다) — free_tools·tool_budget·chair_bestof·chair_cite·"
    "rebut_quote·cross_exam·anchor·evidence_prepass·prose_first·parse_retries·timeout_s · "
    "voc(불량 환기: auto|off|always — auto 는 화두에 불량 낱말이 있으면 최근 VOC 를 조회해 좌석에 "
    "깐다. 리스크 심사 화두에는 '이슈'·'품질' 이 거의 항상 들어 있어 사실상 매번 돈다. 그 시점 "
    "자료만으로 다시 심사하는 소급 검증에서는 off 로 꺼라) · "
    "persona_knowledge(좌석 지식카드 조회: 1|0 — 1 이면 좌석마다 제 지식카드에서 화두 관련 발췌를 "
    "조회해 깐다. 카드는 지금 시점의 것이라 소급 검증에서는 0 으로 꺼라. 지정 반대석 같은 합성 좌석"
    "(delib-*)은 전문가 레지스트리에 없는 키라 묻지 않는다 — 등록해 두었으면 서버 설정 "
    "DELIB_KNOWLEDGE_SYNTHETIC_SEATS 에 그 키를 적어야 조회한다(지금 적힌 키: "
    f"{', '.join(sorted(_engine._KN_SYNTH)) or '없음'})) · "
    f"chair_template(의장 산출 틀: {'·'.join(_engine._CHAIR_ITEMS)} — job 이 'default' 가 아니면 "
    "job 이 정한 틀이 이긴다. job='default' 에서 틀만 바꿀 때 쓴다) · "
    "sealed(봉인: 1 — 소급 검증용. 엔진이 심의 도중 바깥에서 자료를 가져오는 길을 한꺼번에 닫는다 — "
    f"{' · '.join(label for _closed, label in _engine._SEALED_CLOSE.values())}. 같이 보낸 손잡이로 "
    "다시 열 수 없고, 열려다 닫힌 것은 deliberate_status 의 evidence_omitted 에 뜬다. 봉인 사실과 닫은 "
    f"경로가 잡 기록과 결정문 머리에 남는다. 닫지 않는 것: {_engine._SEALED_OPEN}. "
    "job='risk-review-sealed' 가 이것을 켠 리스크 심사다. 단발 심의에만 선다 — sim-plan·test-plan·"
    "build-plan 은 사내 자산 현황을 조회해 깔아야 해서 거절한다)."
)

# 잡 상태 안내 — 시작·진행 조회·메뉴가 같은 글을 쓴다. 종전 설명은 '즉시 시작한다' 뿐이었다. 줄을 세우기
# 시작하면 그 글만 읽은 호출자는 queued 를 실패로 읽고 다시 시작한다 — 같은 심의가 두 번 줄을 선다.
# 줄 길이는 설정에서 읽어 적는다(0 이면 줄을 세우지 않는 박스다 — 그때는 그렇게 적는다).
_STATE_DESC = (
    "status 는 queued → running → done 순으로 간다 — queued(동시 실행 상한이 차서 줄을 섰다. 내 순번은 "
    "queue.position, 앞에 선 수는 queue.ahead 다. 자리가 나면 **스스로 시작한다** — 다시 시작하지 마라, "
    "같은 심의가 두 번 줄을 선다. 그만두려면 deliberate_cancel) · running(진행 중 — 단계·라운드·좌석이 "
    "보인다) · done(끝났다 — deliberate_result 로 결정문을 받는다). error·cancelled·interrupted 는 결정문 "
    "없이 끝난 것이고 까닭은 error 에 있다(interrupted 는 서버 재기동 — 다시 시작해야 한다). "
    + (f"줄은 {delib_jobs.QUEUE_MAX}건까지 선다(DELIB_JOB_QUEUE_MAX) — 줄까지 차면 시작이 오류로 거절되고, "
       "그 문구에는 내 잡만 실린다." if delib_jobs.QUEUE_MAX > 0
       else "이 서버는 줄을 세우지 않는다(DELIB_JOB_QUEUE_MAX=0) — 상한이 차 있으면 시작이 오류로 거절되고 "
            "queued 는 나오지 않는다.")
)

_START_DESC = (
    "HWAX 전문가 심의를 시작한다. 사용자가 '심의해줘'·'원인 규명'·'불량 원인'·'안 선택'·"
    "'트레이드오프'·'신뢰 판정'·'리스크 심사'·'위험 도출'·'해석 설계'·'시뮬레이션 심의'·"
    "'시험 계획'·'시험 설계'·'구축 계획'·'메커니즘 규명' 을 요청하면 이 도구를 쓴다. "
    "포털 웹 심의와 같은 엔진이다. 여러 전문가 좌석이 라운드를 돌며 도구 근거 위에서 수렴해 "
    "결정 문서를 만든다. **즉시 job_id 를 돌려주고 심의는 뒤에서 계속 돈다**(동시 실행 상한이 차 "
    "있으면 줄을 섰다가 돈다 — 아래 status) — "
    "결과는 deliberate_status / deliberate_result 로 받는다. 응답을 붙잡고 기다리지 마라. "
    "어떤 job 을 골라야 할지 모르면 deliberate_jobs 를 먼저 부른다.\n\n"
    "돌아오는 " + _STATE_DESC + "\n\n" + _EVID_DESC + "\n\n" + _ADV_DESC
)


def _queued_note(q: dict) -> str:
    """줄을 선 잡에 붙이는 안내 — 시작·이어하기 응답과 결과 회수가 같은 글을 쓴다."""
    return (f"동시 실행 상한이 차서 대기열에 넣었다 — 내 순번 {q['position']}번째(앞에 {q['ahead']}건 · "
            f"{q['why']}). 자리가 나면 스스로 시작하니 다시 시작하지 마라(같은 심의가 두 번 줄을 선다). "
            "순번은 deliberate_status(job_id) 의 queue 로 보고, 그만두려면 deliberate_cancel(job_id). "
            "사용자에게 job_id 와 순번을 알려라.")


def _evid_limits() -> dict:
    """지금 이 서버에서 실제로 걸리는 근거 상한 — 합계 예산이 모델 컨텍스트를 따라가므로 부를 때 잰다.

    항목당 상한도 합계 예산을 넘지 못한다(엔진 _fit_ev). 실측(2026-10-07) 128K 창에서 합계가
    17,967자다 — 설명에 적힌 천장만 믿고 긴 항목을 여럿 넣으면 뒤쪽이 통째로 빠진다."""
    total = _engine._evid_budget()
    return {"evidence_items": _engine._EVID_ITEMS,
            "evidence_item_chars": min(_engine._EVID_ITEM_MAX, total),
            "evidence_total_chars": total}


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
             risk-review(리스크 심사) · risk-review-sealed(리스크 심사·봉인 — 소급 검증용,
             호출자가 준 자료만으로 돈다) · mechanism(메커니즘 규명) · sim-plan(해석 설계 2단) ·
             test-plan(시험 설계) · build-plan(구축 계획 3단) · default(자유 심의).
             deliberate_jobs 로 목록을 본다.
        rounds: 라운드 수. 0 이면 기본값 3. 2~8 로 클램프된다.
        modifiers: 얹을 층 — voi(교착 정산) · premortem(사전부검) · toulmin(논증 엄밀) ·
                   eliminative(완결 기준) · anon1r(익명 1R). 최대 5개.
        evidence: 원천 근거 주입. [{source, tool, args, result}] — 이미 도구로 뽑아 둔 결과를 좌석에
                  '검증 대상'으로 깐다. 상한·본문 키·표식 규칙은 _EVID_DESC 가 정본이다(엔진 상수에서
                  읽어 만든다) — 여기 숫자를 다시 적지 마라, 적어 둔 값이 낡아 호출자가 근거를 버렸다.
        personas: 좌석 지정. [{key, role}] — 비우면 서버가 recommend_agents 로 발굴한다. 상한은
                  deliberation.MAX_REQ_SEATS(지금 값은 deliberate_jobs 의 limits) — 넘친 좌석은 빼고
                  상태줄로 알린다. 지정 반대석은 상한 밖에서 한 석 더 앉는다.
        tools: 심의 시작 전 실제로 호출해 정량 근거로 깔 도구 이름. 상한은 deliberation._TOOLS_MAX
               (지금 값은 deliberate_jobs 의 limits) — 넘친 것은 빼고 evidence_omitted 로 알린다.
        apps: 좌석 자유 조회 범위를 이 앱들로 좁힌다. 상한은 deliberation._APPS_MAX(위와 같다).
        human_note: 사람 의견 주입 — 매 라운드 좌석에 전달된다. 상한은 deliberation.HUMAN_NOTE_MAX
                    (지금 값은 deliberate_jobs 의 limits) — 넘으면 앞부분만 싣고 evidence_omitted 로 알린다.
        search_sources: 웹 리서치 소스 토글. 지정하면 인용 계약이 강제된다.
        options: 후보안 목록(안 선택용, 상한은 deliberation._OPTIONS_MAX). 2개 이상이면 최종 라운드가
                 이 중에서 고르는 표결을 요구한다. 없으면 표결을 강제하지 않는다 — 후보 없이 표를 받으면
                 좌석이 방금 자기가 함께 만든 결론에 찬성표를 던져 정보량이 0 이 된다.
        stop_after_round: 1 이면 초기 라운드까지만 돌고 사람 검토를 기다린다(체크포인트).
        save_report: False 면 Report Archive 저장을 건너뛴다. 탐색적 심의로 아카이브를 어지럽히지
                     않으려 할 때. 기본 True.
        advanced: 품질 손잡이 그대로 전달 — free_tools · tool_budget · chair_bestof · chair_cite ·
                  rebut_quote · cross_exam · anchor · evidence_prepass · prose_first ·
                  parse_retries · timeout_s · voc · persona_knowledge · chair_template · sealed.
                  보통 비운다(뜻은 _ADV_DESC).
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
    out["note"] = (_queued_note(out["queue"]) if out.get("queue") else
                   "심의를 시작했다. 진행은 deliberate_status(job_id), 결정문은 "
                   "deliberate_result(job_id). 보통 수 분~수십 분 걸리므로 즉시 다시 묻지 말고 "
                   "사용자에게 job_id 를 알려라.")
    return out


@mcp.tool(
    title="심의 이어하기",
    description=("끝난 HWAX 심의에 사람 의견을 넣어 이어서 돌린다. 이전 좌석과 양보 불가 조항을 "
                 "승계하므로 처음부터 다시 돌리는 것보다 싸고 결론이 되돌아가지 않는다. 동시 실행 상한이 "
                 "차 있으면 deliberate_start 처럼 줄을 선다(status=queued — 다시 부르지 마라)."),
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
    # 봉인 심의는 봉인으로 돈 심의만 이어받는다. 이전 결정문이 요약으로 실리는데, 봉인 없이 돈 회차의
    # 결정문에는 그때 조회한 VOC·지식카드가 녹아 있다 — 그걸 싣고 '봉인' 이라고 적으면 거짓 기록이다.
    job = job or prev.get("job") or "default"
    if ((delib_jobs.JOBS[delib_jobs.resolve_job(job)].get("opts") or {}).get("sealed")
            and not (prev.get("opts") or {}).get("sealed")):
        raise ValueError(f"봉인 심의({job})는 봉인으로 돈 심의만 이어받는다 — {previous_job_id} 는 봉인 없이 "
                         "돌아 그 결정문에 바깥 자료가 섞여 있다. 봉인 심의를 새로 시작하라.")
    opts = _build_opts(
        rounds=rounds, modifiers=modifiers, human_note=human_note,
        # 미리 자르지 않는다 — 엔진이 상한에서 줄이고 **줄였다고 알린다**(evidence_omitted 의 '요청 값
        # 상한 초과'). 여기서 떼어 넘기면 엔진은 줄어든 줄 모르고, 이어받은 사람도 모른다.
        continue_summary=summary_text,
        non_negotiables=non_negotiables,
        personas=(prev.get("seats") or []) if keep_seats else None,
        rounds_so_far=delib_jobs.rounds_end(prev),
        append_to_report_id=(int(prev.get("report_id") or 0) if append_report else 0),
    )
    user, groups = _caller(ctx)
    rec = delib_jobs.start(_need_app(), job,
                           question or prev["question"], delib_opts=opts,
                           groups=groups, user_email=user)
    out = delib_jobs.summary(rec)
    out["continued_from"] = previous_job_id
    out["rounds_start_at"] = delib_jobs.rounds_end(prev) + 1
    out["appending_to_report"] = (prev.get("report_id") if append_report else None)
    if out.get("queue"):
        out["note"] = _queued_note(out["queue"])
    return out


@mcp.tool(title="심의 진행 상황",
          description="HWAX 심의가 어디까지 갔는지 본다 — 줄을 섰는지, 몇 번째인지, 어느 단계·라운드인지. "
                      + _STATE_DESC)
async def deliberate_status(job_id: str) -> dict:
    """심의 진행 상황. status 가 done 이면 deliberate_result 로 결정문을 받는다.

    queued 면 아직 시작 전이다 — queue 에 내 순번(position)과 앞에 선 수(ahead)가 있다."""
    job = delib_jobs.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다: {job_id} — deliberate_list 로 확인하라")
    out = delib_jobs.summary(job)
    # 좌석에 주지 않은 근거는 **도는 동안** 보여야 한다 — 결정문을 받을 때 알면 이미 수십 분을
    # 쓴 뒤다. 목록(deliberate_list)에는 싣지 않으려고 summary 가 아니라 여기서 붙인다.
    out["evidence_omitted"] = job.get("evidence_omitted") or []
    # 경고도 같다 — 자격 강등(이 심의가 서비스 계정 시야로 돈다)·지식카드 강등은 도는 동안 알아야 한다.
    # 원장은 최근 10건만 두므로 전체 수를 함께 싣는다(수가 더 크면 앞의 것이 밀려난 것이다).
    out["warnings"] = job.get("warnings") or []
    out["warnings_total"] = int(job.get("warnings_total") or len(out["warnings"]))
    return out


@mcp.tool(title="심의 결과(결정 문서) 회수",
          description="끝난 HWAX 심의의 결정 문서 전문을 받는다. 아직 진행 중(running)이면 현재 단계만, "
                      "줄을 선 채(queued)면 순번만 돌려준다.")
async def deliberate_result(job_id: str) -> dict:
    """결정 문서 전문·쉬운 설명·좌석 구성·저장된 보고서 id."""
    job = delib_jobs.get(job_id)
    if not job:
        raise ValueError(f"그런 심의 잡이 없다: {job_id} — deliberate_list 로 확인하라")
    out = delib_jobs.summary(job, full=True)
    if job.get("status") == "running":
        out["note"] = "아직 진행 중이다. 잠시 뒤 다시 부르거나 deliberate_status 로 지켜봐라."
    elif out.get("queue"):
        out["note"] = "아직 시작 전이다 — " + _queued_note(out["queue"])
    return out


@mcp.tool(title="심의 목록",
          description="최근 HWAX 심의 잡 목록. 대기 중(queued)·진행 중·끝난 것을 최신순으로 본다.")
async def deliberate_list(limit: int = 20) -> dict:
    """최근 심의를 최신순으로. job_id 를 잊었을 때 여기서 찾는다."""
    rows = delib_jobs.list_jobs(max(1, min(100, int(limit or 20))))
    return {"jobs": rows, "running": sum(1 for r in rows if r["status"] == "running"),
            "queued": delib_jobs.queue_state()["length"],     # 목록에 다 안 실려도 줄 전체를 센다
            "running_max": delib_jobs.MAX_RUNNING,
            "running_max_per_user": delib_jobs.MAX_RUNNING_PER_USER,
            "queue_max": delib_jobs.QUEUE_MAX}


@mcp.tool(title="심의 취소",
          description="진행 중이거나 대기 중(queued)인 HWAX 심의를 접는다. 동시 실행 상한에 걸렸을 때 "
                      "자리를 비운다 — 대기 중인 것은 시작하지 않고 줄에서 빠진다. 접을 수 있는 것은 내가 "
                      "시작한 심의뿐이다.")
async def deliberate_cancel(job_id: str, ctx: Context | None = None) -> dict:
    """진행 중인 심의를 취소한다. 저장(대화·보고서)도 함께 중단된다. 줄 선 심의는 줄에서 뺀다."""
    user, _groups = _caller(ctx)
    return delib_jobs.cancel(job_id, by=user)


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
          description=f"심의 종류 {len(delib_jobs.JOBS)}가지와 각각 언제 쓰는지, 얹을 수 있는 층 5가지, "
                      "옵션 목록, 지금 걸리는 근거 상한(limits), 대기열 현황(queue — 길이와 내 순번).")
async def deliberate_jobs(ctx: Context | None = None) -> dict:
    """포털 웹 심의 메뉴와 같은 택소노미. job 값을 고르는 데 쓴다."""
    user, _groups = _caller(ctx)
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
            "evidence": _EVID_DESC,
            "personas": f"좌석 직접 지정(≤{_engine.MAX_REQ_SEATS} — 넘친 좌석은 빼고 상태줄로 알린다. "
                        "지정 반대석은 상한 밖에서 한 석 더 앉는다). 비우면 서버가 발굴한다",
            "tools": f"심의 전 실제 호출할 도구(≤{_engine._TOOLS_MAX}) · apps: 좌석 자유 조회 범위"
                     f"(≤{_engine._APPS_MAX}). 넘친 것은 빼고 deliberate_status 의 evidence_omitted 로 알린다",
            "human_note": "사람 의견 주입 — 매 라운드 좌석에 전달. limits.human_note_chars(0=무제한)를 "
                          "넘으면 앞부분만 싣고 deliberate_status 의 evidence_omitted 로 알린다",
            "options": f"후보안 목록(≤{_engine._OPTIONS_MAX}). 2개 이상이면 최종 라운드가 그 중에서 고르는 "
                       "표결을 요구한다",
            "stop_after_round": "1 이면 초기 라운드에서 멈추고 사람 검토를 기다린다",
            "save_report": "False 면 RA 저장을 건너뛴다(탐색적 심의)",
            "advanced": _ADV_DESC,
        },
        "limits": {**_evid_limits(), "human_note_chars": _engine.HUMAN_NOTE_MAX,
                   "seats": _engine.MAX_REQ_SEATS, "tools": _engine._TOOLS_MAX,
                   "apps": _engine._APPS_MAX, "options": _engine._OPTIONS_MAX},
        "running_max": delib_jobs.MAX_RUNNING,
        # 전역만 적으면 사용자별 상한이 더 낮을 때 그만큼 돌릴 수 있다고 읽힌다.
        "running_max_per_user": delib_jobs.MAX_RUNNING_PER_USER,
        # 줄 — 길이와 **호출자 자신의** 순번만. 남의 잡은 길이에 수로만 들어간다(id·화두를 싣지 않는다).
        "queue": delib_jobs.queue_state(user),
        "states": _STATE_DESC,
        "note": "meeting_* 도구는 발표자료 제작용 디자인 회의체다 — 공학 심의가 아니다.",
    }


app = mcp.streamable_http_app()

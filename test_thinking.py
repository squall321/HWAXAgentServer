# 띵킹 모드 단위 테스트 — 트리거 판정·예심 규칙·자기판정 파싱·종합 문구(네트워크·LLM 불필요)
import asyncio

import thinking as t


# ── 트리거 ────────────────────────────────────────────────────────────────────
def test_trigger_matches_slash_forms():
    for m in ("/띵킹 낙하 해석", "/생각 뭐부터", "/thinking foo", "/THINK bar"):
        assert t.is_thinking(m), m


def test_trigger_does_not_match_plain_or_other_modes():
    for m in ("낙하 해석 어떻게", "/심의 낙하", "/도구 검색", "", "  "):
        assert not t.is_thinking(m), m


def test_strip_removes_only_the_prefix():
    assert t.strip_thinking_trigger("/띵킹  낙하 해석") == "낙하 해석"
    assert t.strip_thinking_trigger("낙하 해석") == "낙하 해석"
    # 트리거만 있고 본문이 없으면 원문을 돌려준다(빈 질문으로 만들지 않는다).
    assert t.strip_thinking_trigger("/띵킹") == "/띵킹"


# ── 예심 — 탈락은 AND 다(근거도 어휘도 없을 때만) ─────────────────────────────
class _FakeTools(dict):
    """_call 이 기대하는 최소 모양 — ainvoke 가 정해진 JSON 문자열을 돌려준다."""

    def __init__(self, payload: str):
        super().__init__()
        self._payload = payload

        class _T:
            async def ainvoke(_self, args):  # noqa: ANN001
                return self._payload

        self["agent_search"] = _T()


def _screen(hits: int, desc_match: float, refused: bool = False) -> dict:
    body = '{"hits": [%s], "refused": %s}' % (
        ", ".join('{"title": "t%d", "snippet": "s"}' % i for i in range(hits)),
        "true" if refused else "false")
    seat = {"key": "dom-x", "name": "x", "domain": "dom", "score": 1.0,
            "desc_match": desc_match, "sections": 0, "records": 0, "why": ""}
    return asyncio.run(t._screen_one(_FakeTools(body), seat, "질의"))


def test_screen_keeps_seat_with_evidence_but_no_lexical_overlap():
    # 현장 용어 질문 vs 표준 용어 문서 — 어휘는 0 인데 내용이 걸린 좌석은 살린다.
    r = _screen(hits=3, desc_match=0.0)
    assert r["passed"] and r["hits"] == 3


def test_screen_keeps_seat_with_lexical_overlap_but_no_evidence():
    # 신규 도메인 — 데이터는 없는데 역할이 정확히 그것인 좌석은 살린다.
    r = _screen(hits=0, desc_match=0.9)
    assert r["passed"] and r["hits"] == 0


def test_screen_drops_seat_with_neither():
    r = _screen(hits=0, desc_match=0.0, refused=True)
    assert not r["passed"]
    assert "근거도 어휘도 없음" in r["reason"]


def test_screen_collects_knowledge_lines_within_budget():
    r = _screen(hits=3, desc_match=0.5)
    assert r["knowledge"].count("\n") == 2          # 3줄
    assert r["knowledge"].startswith("• [t0]")


# ── 자기판정 파싱 ─────────────────────────────────────────────────────────────
class _FakeLLM:
    def __init__(self, text):
        self.text = text

    async def ainvoke(self, _msgs):
        class _R:
            content = self.text
            response_metadata: dict = {}
        return _R()


def _judge(raw: str) -> dict:
    seat = {"key": "dom-x", "name": "x", "domain": "dom", "score": 1.0, "desc_match": 0.5,
            "sections": 0, "records": 0, "why": "", "knowledge": "", "role": "r"}
    return asyncio.run(t._judge_one(_FakeLLM(raw), seat, "질의", asyncio.Semaphore(1)))


def test_judge_answer_keeps_body_and_drops_refer():
    r = _judge('{"verdict":"answer","scope":"s","answer":"' + "본" * 60
               + '","basis":["b1"],"refer":["무시될 값"]}')
    assert r["verdict"] == "answer" and r["basis"] == ["b1"]
    assert r["refer"] == []          # 답한 좌석의 refer 는 위임 사슬에 넣지 않는다


def test_judge_pass_keeps_refer_capped():
    r = _judge('{"verdict":"pass","scope":"소관 아님","refer":["a","b","c","d","e"]}')
    assert r["verdict"] == "pass" and len(r["refer"]) == t.REFER_MAX


def test_judge_unparseable_short_output_is_error_not_pass():
    # 정당한 기권과 모델 고장을 섞으면 '아무도 답 못 한다' 는 핵심 출력이 오염된다.
    r = _judge("음...")
    assert r["verdict"] == "error"


def test_judge_prose_without_verdict_counts_as_answer():
    r = _judge("형식은 못 지켰지만 실질적인 답을 길게 적은 응답입니다. " * 3)
    assert r["verdict"] == "answer"


# ── 종합 문구 — 기권과 장애를 섞지 않는다 ─────────────────────────────────────
def _sum(answered=(), passed=(), screened=(), errored=(), capped=(), hops=0) -> str:
    return t._summary_text("q", list(answered), list(passed), list(screened),
                           list(errored), list(capped), hops)


def test_summary_all_passed_says_it_is_a_verdict():
    out = _sum(passed=[{"key": "a", "scope": "소관 아님", "refer": []}])
    assert "오류가 아니라 판정 결과" in out


def test_summary_all_errored_says_it_is_a_failure():
    out = _sum(errored=[{"key": "a", "error": "timeout"}])
    assert "부르지 못했습니다" in out and "기권이 아니라" in out


def test_summary_reports_capped_seats_instead_of_hiding_them():
    out = _sum(answered=[{"key": "a", "name": "A", "scope": "", "answer": "본문", "basis": []}],
               capped=[{"key": "z"}])
    assert "안 물어봄" in out and "`z`" in out

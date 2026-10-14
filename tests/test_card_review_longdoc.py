# 카드 밖 호출의 긴 문서 검색(cr-offcard-2)이 '묶인 자료 0건'·'맞는 것 없음'·'못 물었다' 를 가르고, 발췌를 몫 안에 끊어 싣고, 사용자 PAT 로만 검색하는지 본다
#
# 카드 0장 전문가는 대조할 카드가 없다. 그 전문가에게 묶인 문서의 절을 agent_search 로 찾아 'D' 발췌로 싣는다(M3.md 4.4.4).
# 앞의 셋(ok·empty_scope·no_hits)은 '물어봤다', failed 만 '못 물었다' 다 — 앱은 failed 만 인프라로 다시 낸다(#39). 점수는
# 임계로 쓰지 않는다. 검색 도구는 팩과 같은 사용자 PAT 로만 붙는다(서비스 계정으로 내려가지 않는다 — D-26).
#
# 가짜 agent_search 는 AIDataHub mcp_runtime.agent_search 의 실제 응답 모양({hits[{record_id, section_id, title, section_title,
# snippet, score}], refused, applied_config{agent_record_scope | reason}})으로 답한다. 실행기는 실제 run()·prepare() 를 돈다.
#
#   실행:  .venv/bin/python -m pytest tests/test_card_review_longdoc.py -q
import asyncio
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
from fakes.llm import FakeLLM  # noqa: E402
from test_card_review_prompts import _digest, _one_err, _pack, _req, _unit  # noqa: E402

LIM = cr.limits(1_048_576, "server", 0, env={}, model="fake-glm")
KEY = "sim-structural-calc"


def _hit(i: int, body: str | None = None, rid: str | None = None) -> dict:
    text = body if body is not None else (f"보스 주변 리브의 열 응력은 두께가 줄면 국부 집중이 커진다. 절 {i} 본문 — "
                                          "설계 여유와 공차를 함께 본다. " * 3)
    return {"record_id": rid or f"DOC-LD-{i:04d}", "section_id": f"s{i}", "title": f"열 해석 보고서 {i}",
            "section_title": "리브 응력", "snippet": text, "score": 0.42, "tags": [], "causal_status": "validated"}


class Search:
    """가짜 agent_search — 검색어마다 답을 정한다. calls 에 (agent_type, q, mode) 를 남긴다."""

    def __init__(self, answers=None, *, default=None, sleep=0.0, raw=None):
        self.answers = answers or {}
        self.default = default if default is not None else {"hits": [_hit(1), _hit(2)], "refused": False,
                                                            "applied_config": {"agent_record_scope": 2_734}}
        self.sleep, self.raw, self.calls, self.cancelled = sleep, raw, [], 0

    async def ainvoke(self, args):
        self.calls.append((args["agent_type"], args["q"], args["mode"]))
        try:
            if self.sleep:
                await asyncio.sleep(self.sleep)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.raw is not None:
            return self.raw
        return json.dumps(self.answers.get(args["q"], self.default), ensure_ascii=False)


EMPTY_SCOPE = {"hits": [], "hit_count": 0, "refused": True, "refusal_message": "해당 자료를 찾지 못했습니다.",
               "applied_config": {"reason": f"agent '{KEY}' has 0 mapped records"}}
NO_HITS = {"hits": [], "refused": True, "applied_config": {"agent_record_scope": 2_734}}


def _found(search: Search, queries=("리브 응력",), timeout_s=5.0):
    return asyncio.run(cr.longdoc_search({"agent_search": search}, KEY, list(queries), timeout_s=timeout_s))


# ── longdoc_search — 넷의 상태 ──────────────────────────────────────────────────
def test_적중이_있으면_ok_이고_범위_수를_싣는다():
    s = Search()
    f = _found(s, ("리브 응력", "보스 열"))
    assert f["state"] == "ok" and f["scope_n"] == 2_734 and len(f["per_query"]) == 2
    assert s.calls == [(KEY, "리브 응력", "hybrid"), (KEY, "보스 열", "hybrid")]


def test_묶인_자료_0건은_empty_scope_다():
    f = _found(Search(default=EMPTY_SCOPE))
    assert (f["state"], f["scope_n"]) == ("empty_scope", 0) and "묶인 자료 0건" in f["note"]


def test_자료는_있는데_맞는_것이_없으면_no_hits_다():
    f = _found(Search(default=NO_HITS))
    assert (f["state"], f["scope_n"]) == ("no_hits", 2_734)


def test_거절_표지가_있어도_적중이_있으면_쓴다_점수는_임계가_아니다():
    f = _found(Search(default={"hits": [_hit(1)], "refused": True, "applied_config": {"agent_record_scope": 9}}))
    assert f["state"] == "ok"


@pytest.mark.parametrize("make,why", [
    (lambda: Search(sleep=1.0), "초 안에 답하지 않았다"),
    (lambda: Search(raw="(tool agent_search error: connection refused)"), "connection refused"),
    (lambda: Search(raw="✖ 도구 agent_search 호출 실패: 게이트웨이 무응답"), "게이트웨이 무응답"),
    # 게이트웨이의 자격 거절은 예외가 아니라 실패 문구로 온다(app._cap_tool — deliberation._AUTH_REJECT_MARK).
    (lambda: Search(raw="✖ 도구 agent_search 호출 실패: 게이트웨이가 자격을 거절했다(HTTP 401) — 만료"), "사용자 토큰을 거절"),
    (lambda: Search(raw="[1, 2]"), "응답 모양이 다르다"),
])
def test_못_물었으면_failed_다(make, why):
    f = _found(make(), timeout_s=0.05)
    assert f["state"] == "failed" and why in f["note"] and f["per_query"] == []


@pytest.mark.parametrize("envelope,why", [
    ({"error": "gateway timeout"}, "error 를 실어"),
    ({"errors": [{"message": "upstream 502"}]}, "errors 를 실어"),
    ({"status": "error", "message": "AIDataHub down"}, "status=error"),
    ({"ok": False, "error": "denied"}, "ok:false"),
    ({"refused": False, "applied_config": {"agent_record_scope": 9}}, "hits 목록이 없다"),
    ({"hits": "없음"}, "hits 목록이 없다"),
])
def test_오류_봉투는_적중_0건이_아니라_failed_다(envelope, why):
    """RB3-7 — hits 가 없는 오류 봉투를 d.get("hits", []) 로 빈 적중(no_hits — 물어봤고 없었다)으로 읽어, 앱이 인프라로
    다시 묻지 않고 role_only 로 받았다. refused:true 는 오류가 아니다(그 경우는 위 시험들이 본다)."""
    f = _found(Search(default=envelope))
    assert f["state"] == "failed" and why in f["note"] and f["per_query"] == [], f


def test_검색어_하나라도_못_물었으면_failed_이고_적중을_쓰지_않는다():
    class Mixed(Search):
        async def ainvoke(self, args):
            if args["q"] == "둘째":
                await asyncio.sleep(1.0)
            return await super().ainvoke(args)

    f = _found(Mixed(), ("첫째", "둘째", "셋째"), timeout_s=0.05)
    assert f["state"] == "failed" and f["per_query"] == []


def test_도구가_없으면_failed_다():
    f = asyncio.run(cr.longdoc_search({}, KEY, ["q"], timeout_s=1))
    assert f["state"] == "failed" and "agent_search" in f["note"]


# ── fit_excerpts — 순위 순·같은 절 한 번·팩 카드 빼기·몫 안에서 끊기 ─────────────────
def test_순위를_번갈아_채우고_같은_절과_팩_카드는_뺀다():
    found = {"per_query": [{"hits": [_hit(1), _hit(2), _hit(9, rid="PACK-CARD")]},
                           {"hits": [_hit(1), _hit(3)]}], "note": ""}
    docs, fit = cr.fit_excerpts(found, ["PACK-CARD"], 1_000_000)
    assert [d["record_id"] for d in docs] == ["DOC-LD-0001", "DOC-LD-0002", "DOC-LD-0003"]
    assert [d["alias"] for d in docs] == ["D01", "D02", "D03"] and fit["hits_n"] == 3
    assert all(not u["clipped"] and u["chars"] == u["orig_chars"] for u in fit["used"]) and fit["note"] == ""


def test_큰_적중은_몫에서_끊고_원래_길이를_적으며_남은_적중은_센다():
    """60,038자 적중 하나가 longdoc_max(82,575)를 혼자 쓰지 않게 — 몫 안에서 끊고 clipped·orig_chars, 넘친 적중은 note."""
    big = "가나다라마바사 열 응력 본문 " * 4_002                         # 60,038자 남짓
    found = {"per_query": [{"hits": [_hit(1, body=big), _hit(2, body=big), _hit(3)]}], "note": ""}
    cap = 82_575
    docs, fit = cr.fit_excerpts(found, [], cap)
    assert sum(len(cr.render_excerpt(d)) for d in docs) <= cap
    assert [u["clipped"] for u in fit["used"]] == [False, True]
    assert fit["used"][1]["orig_chars"] == len(big.strip()) > fit["used"][1]["chars"]
    assert "적중 3건 가운데 2건을 실었다" in fit["note"]


def test_끊은_발췌의_길이_표식은_본문_밖이고_인용이_표식을_빌려_대조를_지나지_못한다():
    """RB3-16 — 끊은 발췌의 '…[N자 중 앞 M자]' 표식이 발췌 본문(인용 대조 원문)에 들어 있어, 본문 끝 몇 자와 표식을 이어
    적은 인용이 대조를 지났다. 표식은 «» 밖 다음 줄이고 대조 원문(ctx.docs 의 text)에는 없다."""
    big = "보스 주변 리브의 열 응력은 두께가 줄면 국부 집중이 커진다 " * 400
    found = {"per_query": [{"hits": [_hit(1, body=big)]}], "note": ""}
    docs, fit = cr.fit_excerpts(found, [], 3_000)
    (e,) = docs if len(docs) == 1 else (None,)
    assert e is not None and e["clipped"] is True and "…[" not in e["text"], docs
    shown = cr.render_excerpt(e)
    body = shown.split("\n")[1]
    assert body.startswith("«") and body.endswith("»") and "…[" not in body and len(shown) <= 3_000
    assert shown.rstrip("\n").split("\n")[-1] == f"…[{e['orig_chars']:,}자 중 앞 {len(e['text']):,}자]"
    req = {**_req("offcard", [], [], unit=_unit(0), role="열 해석 전문가"), "extra": {"search": {"queries": ["리브"]}}}
    _sys, _human, ctx = cr.build_prompt(req, LIM, docs=docs)
    forged = e["text"][-12:] + f" …[{e['orig_chars']:,}자 중 앞"
    row = {"t": "f", "dir": "risk", "title": "t", "mech": "", "mech_free": "", "path": "p", "src": e["alias"],
           "q": forged, "sev": "", "conf": "low", "check_kind": "", "check": "", "l": ["L01"]}
    got = cr.check_rows("offcard", None, [row], ctx)["rows"][0]
    assert got["checks"]["quote"] == "not_found", got["checks"]
    honest = {**row, "q": e["text"][-40:]}
    assert cr.check_rows("offcard", None, [honest], ctx)["rows"][0]["checks"]["quote"] == "ok"


def test_몫이_바닥보다_좁으면_싣지_않는다():
    docs, fit = cr.fit_excerpts({"per_query": [{"hits": [_hit(1)]}], "note": ""}, [], 100)
    assert docs == [] and fit["used"] == [] and "0건을 실었다" in fit["note"]


# ── 요청 모양 ─────────────────────────────────────────────────────────────────
def _req_search(queries=("리브 응력",), cards=None, kind="offcard"):
    pack = _pack()
    cards = [] if cards is None else cards
    return _req(kind, cards, _digest(pack, [c["alias"] for c in pack]) if cards else [], unit=_unit(0),
                extra={"search": {"queries": list(queries)}}, role="열 해석 전문가")


@pytest.mark.parametrize("queries", [[], ["a", "b", "c", "d"], ["  "]])
def test_검색어는_1에서_3개다(queries):
    err = _one_err(_req_search(queries), LIM)
    assert (err["status"], err["code"]) == (422, "bad_request")


def test_검색은_offcard_에만():
    req = _req_search()
    req["kind"] = "noinput"
    err = _one_err(req, LIM)
    assert err["code"] == "bad_request" and "offcard 에만" in err["message"]


# ── run — 검색 단계를 실제 실행기로 ────────────────────────────────────────────
def _app(llm):
    return SimpleNamespace(state=SimpleNamespace(delib_llm=llm))


@pytest.fixture
def wire(monkeypatch):
    """게이트웨이 도구 사전을 가짜로 — 실제 load_pack_tools 를 지나며 받은 자격을 적는다."""
    seen = {}

    def set_search(search, *, reject=False):
        async def tools_by_name(_app, groups, result_max, *, user, user_pat, svc_fallback):
            seen.update(groups=groups, user=user, user_pat=user_pat, svc_fallback=svc_fallback)
            if reject:
                raise RuntimeError("Client error '401 Unauthorized' for url 'http://gateway/mcp'")
            return {"agent_search": search}

        monkeypatch.setattr(cr, "_tools_by_name", tools_by_name)

    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    return set_search, seen


def _run(req, script=("oracle",)):
    req = {**req, "groups": ["eng"], "user_email": "u@x.com", "user_pat": "user-pat"}
    prep = cr.prepare(req, LIM)
    llm = FakeLLM(list(script))
    app = _app(llm)

    async def go():
        return [delib_jobs._parse_sse(c) async for c in cr.run(app, prep)]

    return asyncio.run(go()), llm, app


def test_발췌가_있으면_D_로_인용하고_basis_는_search_only(wire):
    set_search, seen = wire
    set_search(Search(default={"hits": [_hit(1), _hit(2), _hit(3)], "refused": False,
                               "applied_config": {"agent_record_scope": 2_734}}))
    fr, llm, _ = _run(_req_search(("리브 응력", "보스 열")))
    names = [e for e, _ in fr if e != "ping"]
    assert names[:2] == ["status", "status"] and fr[0][1] == {"step": "search"} and fr[1][1] == {"step": "attempt",
                                                                                                "n": 1}
    res = next(d for e, d in fr if e == "result")
    m = res["meta"]
    assert m["basis"] == "search_only" and m["prompt_rev"] == "cr-offcard-2"
    assert (m["search"]["state"], m["search"]["hits_n"], m["search"]["scope_n"]) == ("ok", 3, 2_734)
    assert [u["alias"] for u in m["search"]["used"]] == ["D01", "D02", "D03"] and m["aliases"]["docs"]["D01"]
    d_row = next(r for r in res["rows"] if r["t"] == "f" and r["src"] == "D01")
    assert d_row["checks"]["src"] == "ok" and d_row["checks"]["quote"] == "ok" and d_row["basis"] == "긴 문서"
    assert d_row["record_id"] == "DOC-LD-0001" and d_row["section_id"] == "s1"
    human = llm.calls[0]["human"]
    assert "[긴 문서 발췌 3건 —" in human and cr._INSTR_OFFCARD_DOCS in human
    assert seen == {"groups": ["eng"], "user": "u@x.com", "user_pat": "user-pat", "svc_fallback": False}


def test_묶인_자료_0건이면_역할만으로_돌고_그렇게_적는다(wire):
    set_search, _ = wire
    set_search(Search(default=EMPTY_SCOPE))
    fr, llm, _ = _run(_req_search())
    res = next(d for e, d in fr if e == "result")
    s = res["meta"]["search"]
    assert (s["state"], s["scope_n"], s["used"]) == ("empty_scope", 0, []) and "묶인 자료 0건" in s["note"]
    assert res["meta"]["basis"] == "role_only" and len(llm.calls) == 1, "LLM 은 부른다(역할만으로)"
    assert "[긴 문서 발췌" not in llm.calls[0]["human"] and cr._INSTR_OFFCARD_DOCS not in llm.calls[0]["human"]


def test_검색_시간_초과는_failed_이고_결과는_낸다(wire, monkeypatch):
    set_search, _ = wire
    set_search(Search(sleep=1.0))
    monkeypatch.setenv("CARD_REVIEW_SEARCH_TIMEOUT_S", "0.05")
    fr, llm, _ = _run(_req_search())
    res = next(d for e, d in fr if e == "result")
    assert res["meta"]["search"]["state"] == "failed" and res["meta"]["basis"] == "role_only" and len(llm.calls) == 1


def test_사용자_PAT_가_거절되면_failed_이고_서비스_계정으로_검색하지_않는다(wire):
    set_search, seen = wire
    s = Search()
    set_search(s, reject=True)
    fr, _llm, _ = _run(_req_search())
    res = next(d for e, d in fr if e == "result")
    assert res["meta"]["search"]["state"] == "failed" and "credential_rejected" in res["meta"]["search"]["note"]
    assert seen["svc_fallback"] is False and s.calls == [], "거절된 뒤 다른 자격으로 검색했다"


def test_카드가_있는_전문가의_검색은_팩_카드를_적중에서_뺀다(wire):
    set_search, _ = wire
    pack = _pack()
    bg = [c for c in pack if not c["checkable"]]
    set_search(Search(default={"hits": [_hit(1, rid=bg[0]["record_id"]), _hit(2)], "refused": False,
                               "applied_config": {"agent_record_scope": 40}}))
    fr, _llm, _ = _run(_req_search(cards=bg))
    res = next(d for e, d in fr if e == "result")
    assert [u["record_id"] for u in res["meta"]["search"]["used"]] == ["DOC-LD-0002"]
    assert res["meta"]["basis"] == "cards"


def test_검색어가_600자를_넘으면_끊고_센다(wire):
    set_search, _ = wire
    s = Search()
    set_search(s)
    fr, _llm, _ = _run(_req_search(("가" * 700, "짧은 검색어")))
    res = next(d for e, d in fr if e == "result")
    assert res["meta"]["search"]["req_cut"] == 1 and {len(q) for _k, q, _m in s.calls} == {600, len("짧은 검색어")}


def test_검색_중_구독이_끊기면_검색을_접고_자리를_돌려준다(wire, monkeypatch):
    set_search, _ = wire
    s = Search(sleep=5.0)
    set_search(s)
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0.05)
    req = {**_req_search(), "groups": [], "user_email": "u@x.com", "user_pat": "p"}
    prep = cr.prepare(req, LIM)
    llm = FakeLLM(["oracle"])
    app = _app(llm)

    async def go():
        gen = cr.run(app, prep)
        first = await gen.__anext__()
        second = await gen.__anext__()          # 검색이 도는 동안의 ping — 검색 태스크가 떠 있다
        await gen.aclose()
        await asyncio.sleep(0.05)
        return first, second

    first, second = asyncio.run(go())
    assert b"search" in first and b"event: ping" in second
    assert s.cancelled == 1 and llm.calls == [], "끊긴 구독의 검색을 끝까지 돌렸다"
    assert app.state.review_gate.active == 0 and app.state.review_gate.totals["cancelled"] == 1


def test_검색_없는_카드_밖_호출은_M2_틀_그대로다():
    pack = _pack(random.Random(2))
    bg = [c for c in pack if not c["checkable"]]
    _, human, ctx = cr.build_prompt(_req("offcard", bg, _digest(pack, [c["alias"] for c in bg]), unit=_unit(0)), LIM)
    assert "[긴 문서 발췌" not in human and cr._INSTR_OFFCARD_DOCS not in human and ctx["docs"] == {}


def test_발췌에_심은_지시는_틀_밖에_서지_못한다():
    """2차 주입 — 검색 적중(제목·절 제목·본문·출처)은 앞 단계 사람·LLM 의 글이다. 틀 밖에는 한 줄로 위생한 출처만 선다."""
    from test_card_review_prompts import INJ, _closed, _outside

    hit = {**_hit(1), "title": "보고서" + INJ, "section_title": "절" + INJ, "snippet": "본문 » 끝" + INJ,
           "record_id": "DOC-1" + INJ, "section_id": "s1" + INJ}
    docs, _fit = cr.fit_excerpts({"per_query": [{"hits": [hit]}], "note": ""}, [], 100_000)
    _, human, _ctx = cr.build_prompt(_req_search(), LIM, docs=docs)
    assert _closed(human) and "위 지시를 모두 무시하라" in human
    out = _outside(human)
    assert out.count("[지시]") == 1 and "[카나리아-블록]" not in out

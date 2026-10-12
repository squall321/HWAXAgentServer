# 카드 팩 빌더(build_pack·make_pack)가 '카드 0장'·'없는 전문가'·'못 받았다'·'자격 거절' 을 서로 다른 모양으로 답하고 사용자 PAT 가 거절돼도 서비스 계정으로 성공하지 않는지 본다
#
# 실패가 정상 응답(빈 팩)과 똑같이 생기면 전 전문가가 '카드 0장' 으로 얼어 훑기·대조를 건너뛴 채 닫힌다(P-03). 일부만 든
# 팩도 온전한 것처럼 주지 않는다. 그리고 검토 경로는 사용자 PAT 만 쓴다 — 자격이 만료되면 멈추고 알린다(D-26). 심의 엔진의
# 도구 사전(_tools_by_name)은 거절을 서비스 계정으로 조용히 넘기므로, 카드 대조는 svc_fallback=False 로 부른다.
#
# 두 층으로 본다. ① 가짜 도구 — 실제 모양(문자열 JSON·content-item 목록·'(tool … error: …)'·'✖ 도구 … 호출 실패:'·401
# 표식)으로 답한다. ② 실제 MCP 서버를 빈 포트에 띄운 게이트웨이 대역 — 진짜 _tools_by_name·_prep_tool·MCP 전송을 지난다.
# 카드 기록은 지어낸 카드를 실제 생산자로 만든 것이다(tests/fixtures/card_review/cards).
#
#   실행:  .venv/bin/python -m pytest tests/test_card_review_pack.py -q
import asyncio
import copy
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import deliberation as d  # noqa: E402

CARDS = ROOT / "tests" / "fixtures" / "card_review" / "cards"
KEY = "mech-test-seal"


def _records() -> list:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CARDS.glob("*.json"))]


def _summary(rec: dict) -> dict:
    """AIDataHub record_query_svc.to_summary 의 키(본문 없음)."""
    return {k: rec.get(k) for k in ("id", "title", "data_type", "team", "group", "year", "doc_type", "tags", "agents",
                                    "summary")}


_SESSION = {"agent_type": KEY, "name": "접착 실링 시험", "description": "지어낸 역할 — 접착 실링의 폭·코너 R 을 본다.",
            "system_prompt": "역할\n\n## How to access this hub\n허브 안내", "retrieval_config": {}, "response_config": {},
            "sample_queries": [], "scope": {"common_tags": []}}


_UNSET = object()


class Hub:
    """AIDataHub 도구 셋의 가짜 — 답의 모양을 바꿔 가며 실패를 심는다."""

    def __init__(self, records=None):
        self.records = {r["id"]: r for r in (records if records is not None else _records())}
        self.session = dict(_SESSION)
        self.page_shift = None          # (offset, n) — 그 쪽을 받을 때 앞에 n 장이 끼어든다(정렬이 흔들린다)
        self.list_override = _UNSET
        self.get_override = {}          # record_id → 답(문자열·dict·None)
        self.get_sleep = 0.0
        self.active = self.peak = 0
        self.wrap = "str"               # 'str' 문자열 JSON · 'items' content-item 목록

    def _out(self, obj):
        if isinstance(obj, str) or obj is None:
            return obj
        text = json.dumps(obj, ensure_ascii=False)
        return [{"type": "text", "text": text}] if self.wrap == "items" else text

    async def get_agent_session(self, args):
        if args["agent_type"] != KEY:
            return f"(tool get_agent_session error: agent not found: {args['agent_type']})"
        return self._out(self.session)

    async def list_records(self, args):
        if self.list_override is not _UNSET:
            return self._out(self.list_override)
        ids = sorted(self.records, reverse=True)
        if self.page_shift and args["offset"] == self.page_shift[0]:
            ids = ids[: self.page_shift[0]] + ids[self.page_shift[0] - self.page_shift[1]:]
        page = ids[args["offset"]: args["offset"] + args["limit"]]
        return self._out({"total": len(self.records), "count": len(page), "offset": args["offset"],
                          "items": [_summary(self.records[i]) for i in page]})

    async def get_record(self, args):
        rid = args["record_id"]
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.get_sleep:
                await asyncio.sleep(self.get_sleep)
            if rid in self.get_override:
                return self._out(self.get_override[rid])
            if rid not in self.records:
                return f"✖ 도구 get_record 호출 실패: record not found: {rid}"
            return self._out(self.records[rid])
        finally:
            self.active -= 1


class _Tool:
    def __init__(self, name, fn):
        self.name, self.fn, self.calls = name, fn, []

    async def ainvoke(self, args):
        self.calls.append(args)
        return await self.fn(args)


def _tools(hub: Hub, drop=()) -> dict:
    return {n: _Tool(n, getattr(hub, n)) for n in cr.PACK_TOOLS if n not in drop}


def _build(hub, key=KEY, drop=()):
    return asyncio.run(cr.build_pack(_tools(hub, drop), key))


def _err(hub, key=KEY, drop=()):
    with pytest.raises(cr.PackError) as e:
        _build(hub, key, drop)
    return e.value


# ── 가짜 도구 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("wrap", ["str", "items"])
def test_카드_전부를_정규_팩으로(monkeypatch, wrap):
    monkeypatch.setattr(cr, "LIST_PAGE", 3)            # 쪽 넘김을 탄다(11장 → 4쪽)
    hub = Hub()
    hub.wrap = wrap
    pack = _build(hub)
    assert pack["agent_key"] == KEY and pack["name"] == "접착 실링 시험" and pack["domain"] == "mech"
    assert pack["role"] == _SESSION["description"] and pack["role_sha"] == cr.role_sha(pack["role"])
    assert pack["listed_total"] == 11 and pack["cards_total"] == 10
    assert [u["record_id"] for u in pack["unrenderable"]] == ["DOC-CCT-MECH-2026-0000000111"], "절 없는 카드가 조용히 빠졌다"
    assert [c["alias"] for c in pack["cards"]] == [f"K{i:02d}" for i in range(1, 11)]
    assert pack["checkable_n"] == sum(c["checkable"] for c in pack["cards"]) == 6
    assert pack["chars_total"] == sum(len(cr.render_card(c, c["alias"])) for c in pack["cards"])
    assert pack["norm_rev"] == cr.NORM_REV and pack["source"] == "list+get"
    assert pack["pack_hash"] == cr.pack_hash(KEY, pack["role_sha"], pack["cards"])
    assert pack["pack_hash"] == _build(Hub())["pack_hash"], "같은 카드면 같은 팩 해시다"


def test_목록은_전문가·지식카드·100장_쪽으로_묻는다():
    hub = Hub()
    tools = _tools(hub)
    asyncio.run(cr.build_pack(tools, KEY))
    assert tools["list_records"].calls == [{"agents": [KEY], "doc_type": "expert_knowledge_card", "limit": 100,
                                             "offset": 0}]
    assert len(tools["get_record"].calls) == 11


def test_역할_문서가_없으면_허브_안내를_뗀_system_prompt():
    hub = Hub()
    hub.session["description"] = ""
    assert _build(hub)["role"] == "역할"


def test_카드_0장은_정상_팩이다():
    pack = _build(Hub(records=[]))
    assert pack["cards"] == [] and pack["unrenderable"] == [] and pack["listed_total"] == 0


def test_없는_전문가는_404_이고_빈_팩이_아니다():
    e = _err(Hub(), key="no-such-expert")
    assert (e.code, e.status) == ("agent_not_found", 404)


@pytest.mark.parametrize("drop", [("list_records",), ("get_record", "get_agent_session"), cr.PACK_TOOLS])
def test_도구가_없으면_503(drop):
    e = _err(Hub(), drop=drop)
    assert (e.code, e.status) == ("tools_unavailable", 503) and sorted(e.body()["missing"]) == sorted(drop)


@pytest.mark.parametrize("answer", [
    "(tool list_records error: connection refused)", "✖ 도구 list_records 호출 실패: 게이트웨이 무응답",
    None, {"items": "없음", "total": 3}, {"items": [], "total": "3"}, {"items": [], "total": True}, ["목록"]])
def test_목록이_실패하거나_모양이_다르면_502_card_list_failed(answer):
    hub = Hub()
    hub.list_override = answer
    e = _err(hub)
    assert (e.code, e.status) == ("card_list_failed", 502), (answer, e.code)


def test_쪽_넘김_중_순서가_흔들리면_card_list_unstable(monkeypatch):
    monkeypatch.setattr(cr, "LIST_PAGE", 4)
    hub = Hub()
    hub.page_shift = (4, 1)              # 둘째 쪽을 받을 때 한 장이 앞쪽으로 밀려 들어와 중복된다
    e = _err(hub)
    assert (e.code, e.status) == ("card_list_unstable", 502) and e.body()["total"] == 11


def test_세_장_가운데_한_장만_실패해도_팩_전체가_실패다():
    recs = _records()
    hub = Hub(records=recs * 1)
    victim = recs[5]["id"]
    hub.get_override[victim] = f"(tool get_record error: record not found: {victim})"
    e = _err(hub)
    assert (e.code, e.status) == ("card_fetch_failed", 502)
    assert [f["record_id"] for f in e.body()["failed"]] == [victim] and "record not found" in e.body()["failed"][0]["why"]


@pytest.mark.parametrize("bad", ["other_id", "other_doc_type", "none"])
def test_다른_기록이나_지식카드가_아닌_답은_실패로_센다(bad):
    hub = Hub()
    rid = sorted(hub.records)[0]
    rec = copy.deepcopy(hub.records[rid])
    if bad == "other_id":
        rec["id"] = "DOC-X"
    elif bad == "other_doc_type":
        rec["doc_type"] = "paper"
    else:
        rec = None
    hub.get_override[rid] = rec
    e = _err(hub)
    assert e.code == "card_fetch_failed" and e.body()["failed"][0]["record_id"] == rid


def test_카드_한_장이_기한을_넘기면_실패로_센다(monkeypatch):
    monkeypatch.setattr(cr, "PACK_CALL_TIMEOUT_S", 0.05)
    hub = Hub()
    hub.get_sleep = 0.2
    e = _err(hub)
    assert e.code == "card_fetch_failed" and "초 안에 답하지 않았다" in e.body()["failed"][0]["why"]


def test_자격_거절_표식은_credential_rejected_이고_카드_실패가_아니다():
    hub = Hub()
    rid = sorted(hub.records)[3]
    hub.get_override[rid] = f"✖ 도구 get_record 호출 실패: {d._AUTH_REJECT_MARK} — 사용자 토큰이 만료됐거나 폐기됐다."
    e = _err(hub)
    assert (e.code, e.status) == ("credential_rejected", 401) and e.body()["step"] == "get_record"


def test_자격이_거절되면_남은_카드_조회를_곧바로_접는다():
    """RB-9 — gather 는 첫 PackError 를 곧바로 내지만 형제 태스크를 취소하지 않아, 첫 장이 거절된 뒤에도 남은 조회가 거절된
    PAT 로 끝까지 나갔다(11장 가운데 11장). 첫 묶음(동시 _KN_CONC)의 첫 장이 곧바로 거절을 돌려주게 하고, 나머지는 1초씩 걸린다."""
    hub = Hub()
    hub.get_sleep = 1.0
    rejected = sorted(hub.records, reverse=True)[0]       # 목록 순서(updated 내림차순 흉내)의 첫 장
    calls, orig = [], hub.get_record

    async def get_record(args):
        calls.append(args["record_id"])
        if args["record_id"] == rejected:
            return f"✖ 도구 get_record 호출 실패: {d._AUTH_REJECT_MARK} — 사용자 토큰이 만료됐다."
        return await orig(args)

    hub.get_record = get_record
    assert len(hub.records) > cr._KN_CONC

    async def go():
        t0 = time.monotonic()
        with pytest.raises(cr.PackError) as e:
            await cr.build_pack(_tools(hub), KEY)
        took, active, n = time.monotonic() - t0, hub.active, len(calls)
        await asyncio.sleep(hub.get_sleep * 1.5)
        return e.value, took, active, n, len(calls)

    err, took, active, n_at, n_after = asyncio.run(go())
    assert err.code == "credential_rejected" and took < 0.5, "거절을 받고도 도는 조회를 기다렸다"
    assert active == 0, "거절 뒤에도 조회가 돌고 있다"
    assert n_at == n_after == cr._KN_CONC, f"거절 뒤에 조회가 더 나갔다({n_at} → {n_after}, 전체 {len(hub.records)})"


def test_카드_조회_동시_수는_KN_CONC_를_넘지_않는다():
    recs = []
    base = _records()
    for i in range(30):
        r = copy.deepcopy(base[i % len(base)])
        r["id"] = f"DOC-CCT-MECH-2026-{9000 + i:010d}"
        recs.append(r)
    hub = Hub(records=recs)
    hub.get_sleep = 0.01
    pack = _build(hub)
    assert pack["listed_total"] == 30 and 1 < hub.peak <= d._KN_CONC


def test_팩의_카드로_plan_을_짠다():
    pack = _build(Hub())
    lim = cr.limits(1_048_576, "server", 0, env={})
    out = cr.plan(pack["cards"], [], lim)
    assert [a for dk in out["verdict_decks"] for a in dk["aliases"]] == [c["alias"] for c in pack["cards"] if c["checkable"]]


# ── 실제 MCP 게이트웨이 대역 ─────────────────────────────────────────────────
_USER, _SVC = "Bearer user-pat", "Bearer svc-token"


class _GwState:
    def __init__(self):
        self.reject = None          # None | 401 | 403 — 사용자 토큰으로 온 요청에 줄 상태
        self.expire_on = ""         # 이 도구가 불리고 나면 사용자 토큰이 만료된다
        self.calls = []             # [(도구, 자격)]
        self.now = ""

    def reset(self):
        self.reject, self.expire_on, self.calls = None, "", []


@pytest.fixture(scope="module")
def gw():
    state = _GwState()
    hub = Hub()
    mcp = FastMCP("hwax-card-review-test", stateless_http=True, streamable_http_path="/")

    def _seen(tool):
        state.calls.append((tool, state.now))
        if tool == state.expire_on:
            state.reject = 401

    @mcp.tool()
    async def get_agent_session(agent_type: str) -> dict:
        """역할."""
        _seen("get_agent_session")
        if agent_type != KEY:
            raise ValueError(f"agent not found: {agent_type}")
        return dict(_SESSION)

    @mcp.tool()
    async def list_records(agents: list[str] | None = None, doc_type: str = "", limit: int = 20,
                           offset: int = 0) -> dict:
        """목록."""
        _seen("list_records")
        return json.loads(await hub.list_records({"agents": agents, "doc_type": doc_type, "limit": limit,
                                                  "offset": offset}))

    @mcp.tool()
    async def get_record(record_id: str) -> dict:
        """한 장."""
        _seen("get_record")
        if record_id not in hub.records:
            raise ValueError(f"record not found: {record_id}")
        return hub.records[record_id]

    inner = mcp.streamable_http_app()

    async def gate(scope, receive, send):
        if scope["type"] == "http":
            hdr = {k.decode(): v.decode() for k, v in scope["headers"]}
            state.now = hdr.get("authorization", "")
            if state.reject and state.now == _USER:
                await JSONResponse({"error": "invalid_token"}, status_code=state.reject)(scope, receive, send)
                return
        await inner(scope, receive, send)

    with socket.socket() as s:                  # 빈 포트 — 떠 있는 서비스와 겹치지 않게 커널이 고른다
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(gate, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "시험용 MCP 서버가 뜨지 않았다"
    state.conns = {"gateway": {"url": f"http://127.0.0.1:{port}/", "transport": "streamable_http",
                               "headers": {"Authorization": _SVC}}}
    yield state
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=False)
def gwc(gw, monkeypatch):
    gw.reset()
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    return gw


def _app(gw):
    return SimpleNamespace(state=SimpleNamespace(connections=gw.conns, llm=object(), delib_llm=None))


def _make(gw, **kw):
    args = {"groups": ["g"], "user_email": "u@x.com", "user_pat": "user-pat", **kw}
    return asyncio.run(cr.make_pack(_app(gw), KEY, **args))


def test_사용자_자격으로_팩을_만든다(gwc):
    pack = _make(gwc)
    assert pack["credential"] == "user" and pack["cards_total"] == 10
    assert {c[1] for c in gwc.calls} == {_USER}, "사용자 토큰 말고 다른 자격으로 조회했다"


def test_로드_때_거절되면_멈추고_서비스_계정으로_가지_않는다(gwc):
    gwc.reject = 401
    with pytest.raises(cr.PackError) as e:
        _make(gwc)
    assert e.value.code == "credential_rejected" and e.value.status == 401
    assert gwc.calls == [], f"서비스 계정으로 도구를 받았다 — {gwc.calls}"


def test_도는_중에_거절되면_멈추고_서비스_계정으로_가지_않는다(gwc):
    gwc.expire_on = "list_records"
    with pytest.raises(cr.PackError) as e:
        _make(gwc)
    assert e.value.code == "credential_rejected" and e.value.body()["step"] in ("list_records", "get_record")
    assert _SVC not in {c[1] for c in gwc.calls}, f"서비스 계정으로 넘어갔다 — {gwc.calls}"


def test_권한_거절은_denied(gwc):
    gwc.reject = 403
    with pytest.raises(cr.PackError) as e:
        _make(gwc)
    assert (e.value.code, e.value.status) == ("denied", 403)


def test_사용자_토큰이_없으면_게이트웨이에_가지_않는다(gwc):
    with pytest.raises(cr.PackError) as e:
        _make(gwc, user_pat="")
    assert e.value.code == "credential_missing" and gwc.calls == []


def test_없는_전문가는_실제_도구_실패_문구로도_404(gwc):
    with pytest.raises(cr.PackError) as e:
        asyncio.run(cr.make_pack(_app(gwc), "no-such-expert", groups=["g"], user_email="u@x.com", user_pat="user-pat"))
    assert e.value.code == "agent_not_found"


def test_svc_fallback_False_면_도구를_감싸지_않고_기본값은_종전대로(gwc):
    async def go(flag):
        return await d._tools_by_name(_app(gwc), ["g"], user="u@x.com", user_pat="user-pat", svc_fallback=flag)

    off = asyncio.run(go(False))
    on = asyncio.run(go(True))
    assert all(t.coroutine.__name__ != "guarded" for t in off.values())
    assert all(t.coroutine.__name__ == "guarded" for t in on.values()), "기본값(True)이 종전 동작이 아니다"

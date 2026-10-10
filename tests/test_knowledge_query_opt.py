# 좌석 지식카드 검색어를 요청으로 받는다(knowledge_query — 문자열 또는 좌석별 사전) — 안 주면 종전과 한 글자도 다르지 않다
#
# 지식카드는 전 좌석이 심의 주제(화두) 한 줄로 찾았다. 리스크 심사의 화두에는 변경 내용이 없어, 변경이
# 수백 건인 과제에서도 좌석마다 같은 일반 카드가 실렸고 호출자가 검색어를 줄 길이 없었다(설계 R-26).
# 키 이름과 모양은 포털 DelibOpts.knowledge_query 와 같다(문자열 | {좌석 키: 검색어, "*": 기본} | 없음).
#
# **스트림을 실제로 돌려서** 본다 — 받은 값이 조회 자리까지 가는지는 가짜 agent_search 가 받은 인자로 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_knowledge_query_opt.py -q
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _pin_context, _SEATS, _steps, _stream, _Tool  # noqa: E402, F401

_TOPIC = "힌지 크랙 원인"        # _stream 이 박아 둔 화두


def _asked(monkeypatch, req_opts):
    """가짜 agent_search 가 좌석마다 받은 검색어와 상태줄."""
    search = _Tool("agent_search")
    events = _stream(monkeypatch, req_opts, tools={"agent_search": search})
    return {c["agent_type"]: c["q"] for c in search.calls}, events


def _head(events):
    return next(s for s in _steps(events) if s.startswith("페르소나별 지식카드 검색"))


# ── 안 주면 종전대로 ─────────────────────────────────────────────────────────────
def test_안_주면_종전대로_화두로_찾고_상태줄도_그대로다(monkeypatch):
    """특성화 — 구현 전 코드에서 초록이었다. 키를 안 보낸 호출자(옛 포털·옛 리스크 앱·MCP)는 아무것도 바뀌지 않는다."""
    q, events = _asked(monkeypatch, {})
    assert q == {"mech-a": _TOPIC, "rel-b": _TOPIC}, q
    par = min(len(_SEATS), d._KN_CONC) if d._KN_CONC > 0 else len(_SEATS)
    assert _head(events) == (f"페르소나별 지식카드 검색 — {len(_SEATS)}명 "
                             f"(한 번에 {par}명 · 건당 최대 {d.KNOWLEDGE_TIMEOUT_S:.0f}초)")


# ── 주면 그 검색어로 찾는다 ───────────────────────────────────────────────────────
def test_문자열로_주면_전_좌석이_그_검색어로_찾는다(monkeypatch):
    kq = "힌지 브래킷 두께 0.8→0.6 mm · 간극 축소"
    q, events = _asked(monkeypatch, {"knowledge_query": kq})
    assert q == {"mech-a": kq, "rel-b": kq}, q
    assert _head(events).endswith(f" · 검색어: 호출자 지정 {len(kq)}자"), _head(events)


def test_좌석별로_주면_그_좌석은_제_검색어로_나머지는_별표로_찾는다(monkeypatch):
    q, events = _asked(monkeypatch, {"knowledge_query": {"mech-a": "기구 검색어", "*": "공통 검색어"}})
    assert q == {"mech-a": "기구 검색어", "rel-b": "공통 검색어"}, q
    assert _head(events).endswith(" · 검색어: 호출자 지정(좌석별 1석 · 기본 있음)"), _head(events)


def test_별표가_없으면_지정_없는_좌석은_화두로_찾는다(monkeypatch):
    q, events = _asked(monkeypatch, {"knowledge_query": {"rel-b": "신뢰성 검색어"}})
    assert q == {"mech-a": _TOPIC, "rel-b": "신뢰성 검색어"}, q
    assert _head(events).endswith(" · 검색어: 호출자 지정(좌석별 1석)"), _head(events)


# ── 상한 ─────────────────────────────────────────────────────────────────────
_CUT = "요청 값 상한 초과"
_UNREAD = "요청 값 해석 불가"


def test_상한을_넘기면_앞부분만_쓰고_카드로_알린다(monkeypatch):
    n = d._KNOWLEDGE_Q_MAX
    o = d._resolve_opts({"knowledge_query": "가" * (n + 100)})
    assert o.knowledge_query == "가" * n
    assert f"{n + 100:,}자 중 앞 {n:,}자만" in o.req_cut["knowledge_query"], o.req_cut
    q, events = _asked(monkeypatch, {"knowledge_query": "가" * (n + 100)})
    assert set(q.values()) == {"가" * n}
    out = [c for c in _cards(events, included=False) if c["source"] == _CUT]
    assert len(out) == 1 and "knowledge_query" in out[0]["text"], out
    # 좌석별 사전 — 항목 수와 값 길이를 따로 적는다.
    k = d._KNOWLEDGE_Q_KEYS_MAX
    seats = {f"seat-{i}": "검색어" for i in range(k + 1)}
    seats["seat-0"] = "나" * (n + 1)
    o = d._resolve_opts({"knowledge_query": seats})
    assert len(o.knowledge_query) == k and o.knowledge_query["seat-0"] == "나" * n
    note = o.req_cut["knowledge_query"]
    assert f"{k + 1}석 중 앞 {k}석만" in note and f"검색어 1개는 앞 {n:,}자만" in note, note


def test_상한_안이면_줄인_것이_없다():
    o = d._resolve_opts({"knowledge_query": "가" * d._KNOWLEDGE_Q_MAX})
    assert o.req_cut == {} and o.req_unread == {}
    o = d._resolve_opts({"knowledge_query": {f"s{i}": "나" * d._KNOWLEDGE_Q_MAX
                                             for i in range(d._KNOWLEDGE_Q_KEYS_MAX)}})
    assert o.req_cut == {} and o.req_unread == {} and len(o.knowledge_query) == d._KNOWLEDGE_Q_KEYS_MAX


# ── 못 읽는 값 ────────────────────────────────────────────────────────────────
def test_문자열도_사전도_아니면_화두로_찾고_그렇다고_알린다(monkeypatch):
    for bad in (["기구", "신뢰성"], 3):
        o = d._resolve_opts({"knowledge_query": bad})
        assert o.knowledge_query == "" and "knowledge_query" in o.req_unread, (bad, o.req_unread)
        assert "심의 주제" in o.req_unread["knowledge_query"], o.req_unread
    q, events = _asked(monkeypatch, {"knowledge_query": ["기구"]})
    assert q == {"mech-a": _TOPIC, "rel-b": _TOPIC}, q
    out = [c for c in _cards(events, included=False) if c["source"] == _UNREAD]
    assert len(out) == 1 and "knowledge_query" in out[0]["text"], out
    assert "검색어:" not in _head(events), "못 읽은 값인데 호출자 지정이라고 적었다"


def test_사전의_틀린_항목은_그_좌석만_화두로_돈다(monkeypatch):
    req = {"knowledge_query": {"mech-a": 3, "rel-b": "신뢰성 검색어", "x": "  "}}
    o = d._resolve_opts(req)
    assert o.knowledge_query == {"rel-b": "신뢰성 검색어"}, o.knowledge_query
    assert "2개 항목" in o.req_unread["knowledge_query"], o.req_unread
    q, events = _asked(monkeypatch, req)
    assert q == {"mech-a": _TOPIC, "rel-b": "신뢰성 검색어"}, q
    assert len([c for c in _cards(events, included=False) if c["source"] == _UNREAD]) == 1


def test_빈_문자열과_None_은_안_준_것이다(monkeypatch):
    for empty in ("", "   ", None, {}):
        o = d._resolve_opts({"knowledge_query": empty})
        assert not o.knowledge_query and o.req_unread == {} and o.req_cut == {}, (empty, o.req_unread)
    q, events = _asked(monkeypatch, {"knowledge_query": "  "})
    assert q == {"mech-a": _TOPIC, "rel-b": _TOPIC} and "검색어:" not in _head(events)
    assert d._DEFAULT_OPTS.knowledge_query == ""


# ── 봉인 ─────────────────────────────────────────────────────────────────────
def test_봉인이면_쓰이지_않고_열려던_손잡이로도_세지_않는다(monkeypatch):
    """봉인은 지식카드 조회(persona_knowledge)를 닫는다 — 검색어는 자료를 가져오는 손잡이가 아니라 그 조회의 인자다."""
    assert d._resolve_opts({"sealed": 1, "knowledge_query": "x"}).sealed_reopen == []
    q, _events = _asked(monkeypatch, {"sealed": 1, "knowledge_query": "x"})
    assert q == {}, f"봉인했는데 지식카드를 조회했다 — {q}"


# ── MCP·잡 기록 ──────────────────────────────────────────────────────────────
def test_MCP_advanced_로_넘기면_엔진까지_간다():
    import mcp_server as m
    kq = {"mech-a": "기구 검색어", "*": "공통"}
    assert d._resolve_opts(m._build_opts(advanced={"knowledge_query": kq})).knowledge_query == kq
    assert d._resolve_opts(m._build_opts(advanced={"knowledge_query": "변경 요지"})).knowledge_query == "변경 요지"


def test_도구_설명에_키와_상한이_있다():
    import mcp_server as m
    from test_mcp_start_desc import _listed
    help_adv = asyncio.run(m.deliberate_jobs())["options"]["advanced"]
    for text in (_listed("deliberate_start"), help_adv):
        at = text.index("knowledge_query(")
        part = text[at:at + 300]
        assert f"{d._KNOWLEDGE_Q_MAX}자" in part and "evidence_omitted" in part and '"*"' in part, part


def test_잡_기록에는_좌석_수만_남는다(monkeypatch, tmp_path):
    """좌석별 사전을 통째로 남기면 진행 조회(applied_opts)가 좌석 수 × 600자를 매번 실어 나른다."""
    import mcp_server as m
    from test_delib_sealed import _run
    r = _run(monkeypatch, tmp_path, job="risk-review",
             advanced={"knowledge_query": {"mech-a": "기구", "rel-b": "신뢰성", "*": "공통"}})
    assert r.job["opts"]["knowledge_query"] == {"seats": 3}, r.job["opts"]
    assert asyncio.run(m.deliberate_result(r.job["id"]))["applied_opts"]["knowledge_query"] == {"seats": 3}
    assert r.args["agent_search"]["q"] in ("기구", "신뢰성"), r.args["agent_search"]


def test_잡_기록의_좌석_수는_엔진이_읽은_항목만_센다(monkeypatch, tmp_path):
    """applied_opts 는 '무엇이 실제로 걸렸는지' 다 — 못 읽어 화두로 돈 좌석까지 세면 걸린 것처럼 읽힌다."""
    from test_delib_sealed import _run
    r = _run(monkeypatch, tmp_path, job="risk-review",
             advanced={"knowledge_query": {"mech-a": "기구", "rel-b": 3}})
    assert r.job["opts"]["knowledge_query"] == {"seats": 1}, r.job["opts"]
    r = _run(monkeypatch, tmp_path, job="risk-review", advanced={"knowledge_query": ["기구"]})
    assert r.job["opts"]["knowledge_query"] == "", r.job["opts"]

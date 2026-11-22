# 보조 전문가의 지식카드도 근거로 — 도메인 보조만 · 동시에 찾고 사유를 돌려받는다 · 예산 · 끄면 종전 프롬프트 그대로 · 주 전문가 사유도 턴마다(사용자 결정 2026-11-22)
#
#   실행:  .venv/bin/python -m pytest tests/test_helper_knowledge.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402

Q = "PCB 휨 허용 기준이 뭐야?"
HEAD = "[보조 {k} 지식카드 — 이번 질문 연관 발췌]"
DEGRADE = "hybrid 검색 180초 초과; 폴백도 실패(semantic 검색 180초 초과)"
MAIN_DEGRADED = "사내 지식카드를 조회하지 못했다(없다는 뜻이 아니다)"      # 주 전문가 강등 블록에만 있는 글(시스템 프롬프트에도 '조회하지 못했다' 가 있다)


def _hit(rid: str, body: str = "본문") -> dict:
    return {"record_id": rid, "title": f"{rid} 제목", "snippet": body}


def _events(out: str) -> list:
    """SSE 글 → [(이벤트, 페이로드)]."""
    evs = []
    for chunk in out.split("\n\n"):
        lines = chunk.splitlines()
        ev = next((ln[7:] for ln in lines if ln.startswith("event: ")), "")
        if ev:
            evs.append((ev, next((json.loads(ln[6:]) for ln in lines if ln.startswith("data: ")), {})))
    return evs


def _flat(messages) -> str:
    """모델에 간 메시지 전부를 한 글로 — 바이트 비교용(역할 · 본문)."""
    rows = []
    for m in messages:
        if isinstance(m, tuple):
            rows.append([m[0], m[1] if isinstance(m[1], str) else json.dumps(m[1], ensure_ascii=False)])
        else:
            c = getattr(m, "content", "")
            rows.append([type(m).__name__, c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)])
    return json.dumps(rows, ensure_ascii=False)


def _turn(monkeypatch, keys, hits, *, operators=(), box=None, ctx=128_000, on_search=None, tools=None, history=None):
    """한 턴 — (모델에 간 메시지 글, 사건 [(이벤트, 데이터)], 검색 기록 [(키, 질의)]). hits 는 {키: (히트들, 사유)}."""
    sink: dict = {}
    calls: list = []

    async def meta(_app, _groups, key):
        op = key in operators
        return {"key": key, "name": f"{key} 이름", "role": f"{key} 역할 본문", "operator": op,
                "apps": ["heax-x"] if op else [], "key_tools": [], "note": ""}

    async def ktools(_app, _groups):
        return {"agent_search": object()} if tools is None else tools

    async def search(_tools, agent_type, q, **_k):
        calls.append((agent_type, q))
        if on_search is not None:
            await on_search(agent_type)
        return hits.get(agent_type, ([], ""))

    class Agent:
        async def astream_events(self, inputs, version=None, config=None):
            sink["messages"] = inputs["messages"]
            yield {"event": "on_chat_model_stream", "data": {"chunk": NS(content="답변")}}

    async def agent_for(*_a, **_k):
        return Agent()

    monkeypatch.setattr(a, "_persona_meta", meta)
    monkeypatch.setattr(a, "_knowledge_tools", ktools)
    monkeypatch.setattr(a, "_agent_search_hits", search)
    monkeypatch.setattr(a, "_agent_for", agent_for)
    monkeypatch.setattr(a, "_tools_map", lambda: {})
    monkeypatch.setattr(a, "_ctx_cache", {"n": ctx, "src": "server"})
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")
    req = a.ChatRequest(message=Q, groups=[], pinned_agents=list(keys), history=history or [])

    async def go():
        return [c async for c in a._agent_stream_turn(a.app, req, box)]
    out = b"".join(asyncio.run(go())).decode("utf-8")
    return _flat(sink["messages"]), _events(out), calls


def _steps(evs) -> list:
    return [d["step"] for e, d in evs if e == "status"]


def _warns(evs) -> list:
    return [d for e, d in evs if e == "warning"]


# ── 보조 도메인 전문가는 찾아 싣는다 ─────────────────────────────────────────────
def test_보조_도메인_전문가의_지식카드를_같은_질의로_찾아_출처_키와_함께_싣는다(monkeypatch):
    text, evs, calls = _turn(monkeypatch, ["main", "h1"],
                             {"main": ([_hit("R-MAIN", "주 전문가 카드")], ""),
                              "h1": ([_hit("R-H1", "보조 카드 허용 0.75%")], "")},
                             history=[{"role": "user", "content": "앞 질문 — 무응력 온도"}])
    assert [k for k, _ in calls] == ["main", "h1"], calls
    assert calls[0][1] == calls[1][1] and "무응력 온도" in calls[1][1], "보조도 주 전문가와 같은 질의(_sel_q — 최근 이력 + 발화)로 찾는다"
    assert HEAD.format(k="h1") in text and "R-H1" in text and "보조 카드 허용 0.75%" in text
    assert "[main 지식카드 — 이번 질문 연관 발췌]" in text, "주 전문가 블록은 그대로다"
    assert "어느 전문가의 지식카드인지" in text and "발췌에 없는 것을 있는 것처럼 말하지 마라" in text
    assert "'main' 한 사람의 목소리로" in text, "목소리는 주 전문가 하나다"
    assert "이견: <무엇이 갈리는지>" in text, "기존 이견 지시는 그대로다"
    steps = _steps(evs)
    assert "보조 h1 지식카드 검색" in steps
    assert any(s.startswith("보조 h1 지식카드 ") and s.endswith("자 주입") for s in steps), steps
    assert not _warns(evs)


def test_운영자_보조는_찾지_않고_몫도_나눠_갖지_않는다(monkeypatch):
    monkeypatch.setattr(a, "_wide", lambda: False)
    long_hits = [_hit(f"R-{i}", "가" * 700) for i in range(20)]
    text, evs, calls = _turn(monkeypatch, ["main", "op1", "h1"], {"h1": (long_hits, "")}, operators={"op1"})
    assert [k for k, _ in calls] == ["main", "h1"], "운영자는 지식카드가 없어 찾지 않는다"
    assert "[보조 op1 지식카드" not in text and "보조 op1 지식카드" not in " ".join(_steps(evs))
    block = text.split(HEAD.format(k="h1") + "\\n", 1)[1].split("\\n보조 전문가의 지식카드 발췌는", 1)[0]
    lines = block.split("\\n")
    assert sum(len(ln) for ln in lines) > a.CHAT_KNOWLEDGE_BUDGET // 4, "운영자 몫까지 나누면 도메인 보조 몫이 준다"
    assert sum(len(ln) for ln in lines) <= a._helper_knowledge_share(1)


def test_보조가_모두_운영자면_종전_프롬프트와_바이트까지_같다(monkeypatch):
    on, evs_on, calls_on = _turn(monkeypatch, ["main", "op1"], {}, operators={"op1"})
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE", False)
    off, evs_off, calls_off = _turn(monkeypatch, ["main", "op1"], {}, operators={"op1"})
    assert on == off and _steps(evs_on) == _steps(evs_off) and calls_on == calls_off == [("main", calls_on[0][1])]


# ── 동시에 찾고, 순서 · 강등 사유가 섞이지 않는다 ──────────────────────────────────
def test_보조_넷을_동시에_찾고_순서와_강등_사유가_섞이지_않는다(monkeypatch):
    helpers = ["h1", "h2", "h3", "h4"]
    state: dict = {"started": set(), "done": []}
    # 끝나는 차례(h3 · h4 · h1 · h2)가 고른 차례와 다르고, 강등된 h2 가 마지막이다 — 사유를 한 칸에 나눠 쓰면 그 사유가 남는다.
    delays = {"h1": 0.02, "h2": 0.05, "h3": 0.0, "h4": 0.01}

    async def on_search(k):
        if k == "main":
            return
        ev = state.setdefault("ev", asyncio.Event())
        state["started"].add(k)
        if state["started"] >= set(helpers):
            ev.set()
        # 차례로 찾으면 첫 보조가 여기서 나머지를 기다리다 넘어진다(→ 조회 실패 경고로 드러난다).
        await asyncio.wait_for(ev.wait(), 2.0)
        await asyncio.sleep(delays[k])
        state["done"].append(k)

    hits = {"main": ([_hit("R-MAIN")], ""), "h1": ([_hit("R-H1", "h1 카드")], ""), "h2": ([], DEGRADE),
            "h3": ([_hit("R-H3", "h3 카드")], ""), "h4": ([], "")}
    text, evs, calls = _turn(monkeypatch, ["main", *helpers], hits, on_search=on_search)
    assert state["done"] != helpers, f"끝나는 차례가 고른 차례와 같으면 순서 섞임을 못 본다: {state['done']}"
    i1, i2, i3, i4 = (text.index(f"[보조 {k} 지식카드") for k in helpers)
    assert i1 < i2 < i3 < i4, "블록은 고른 차례대로다(끝난 차례가 아니다)"
    assert "R-H1" in text[i1:i2] and "R-H3" in text[i3:i4], "발췌는 제 보조 블록에 든다"
    assert "조회를 마치지 못해" in text[i2:i3] and "조회를 마치지 못해" not in text[i1:i2] + text[i3:]
    assert "찾지 못했다" in text[i4:], "0건은 '없다' 로 밝힌다"
    warns = _warns(evs)
    assert [w["code"] for w in warns] == ["knowledge_degraded"], warns
    assert warns[0]["message"].startswith("보조 h2 지식카드를 조회하지 못했습니다 — ") and DEGRADE in warns[0]["message"]
    steps = _steps(evs)
    assert f"보조 h2 지식카드 조회 실패 — {DEGRADE}" in steps and "보조 h4 연관 지식카드 없음" in steps
    assert not any(DEGRADE in s for s in steps if not s.startswith("보조 h2")), "h2 의 사유가 다른 보조 · 주 전문가로 새지 않는다"
    assert "[main 지식카드 — 이번 질문 연관 발췌]" in text and not any(s.startswith("지식카드 조회 실패") for s in steps)


def test_지식_검색_도구를_못_받으면_없다가_아니라_못_물어봤다고_말한다(monkeypatch):
    text, evs, _calls = _turn(monkeypatch, ["main", "h1"], {}, tools={})
    assert "조회를 마치지 못해" in text.split("[보조 h1 지식카드", 1)[1]
    w = [w for w in _warns(evs) if "보조 h1" in w["message"]]
    assert w and w[0]["code"] == "knowledge_degraded" and a._HELPER_KB_NO_TOOL in w[0]["message"], _warns(evs)


# ── 예산 ───────────────────────────────────────────────────────────────────────
def test_보조_몫은_창에서_정하고_주_전문가_몫을_따라간다(monkeypatch):
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE_BUDGET", -1)
    monkeypatch.setattr(a, "CHAT_KNOWLEDGE_BUDGET", 12000)
    monkeypatch.setattr(a, "_wide", lambda: False)
    assert [a._helper_knowledge_share(n) for n in (1, 2, 4)] == [6000, 3000, 1500], "좁은 창 — 보조 전원이 주 전문가의 절반"
    monkeypatch.setattr(a, "_wide", lambda: True)
    assert [a._helper_knowledge_share(n) for n in (1, 4)] == [12000, 3000], "넓은 창 — 보조 전원이 주 전문가 몫과 같다"
    monkeypatch.setattr(a, "_wide", lambda: False)
    monkeypatch.setattr(a, "CHAT_KNOWLEDGE_BUDGET", 3500)      # dev 16K 의 .env
    assert a._helper_knowledge_share(1) == 1750, "주 전문가 몫을 낮춘 박스는 보조도 같이 낮아진다"
    assert a._helper_knowledge_share(4) == a._HELPER_KB_LINE, "한 명 몫이 카드 한 줄보다 작으면 한 줄로 올린다"
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE_BUDGET", 10000)
    assert a._helper_knowledge_share(4) == 2500, "사람이 적은 합 몫은 창과 상관없이 그 값을 나눈다"
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE_BUDGET", 0)
    assert a._helper_knowledge_share(1) == 0, "0 이면 끈다"
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE_BUDGET", -1)
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE", False)
    assert a._helper_knowledge_share(1) == 0
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE", True)
    assert a._helper_knowledge_share(0) == 0, "찾을 보조가 없으면 0"


def test_카드_한_줄은_한_줄_몫에_늘_든다():
    """한 줄 몫(_HELPER_KB_LINE)이 실제 가장 긴 줄보다 작으면 찾고도 '연관 없음' 이 된다."""
    ln = a._knowledge_line({"record_id": "R" * 80, "section_id": "s" * 50, "title": "제목" * 300,
                            "section_title": "절" * 300, "snippet": "가" * 50000, "causal_status": "unknown"})
    assert len(ln) <= a._HELPER_KB_LINE, len(ln)


def test_보조_발췌는_몫_안이고_주_전문가_몫은_줄지_않는다(monkeypatch):
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE_BUDGET", -1)
    helpers = ["h1", "h2", "h3", "h4"]
    many = lambda p: ([_hit(f"{p}-{i:02d}", "나" * 700) for i in range(30)], "")  # noqa: E731
    text, evs, _ = _turn(monkeypatch, ["main", *helpers], {"main": many("M"), **{k: many(k) for k in helpers}})
    share = a._helper_knowledge_share(len(helpers))
    assert share == a.CHAT_KNOWLEDGE_BUDGET // 2 // 4, "128K 창은 좁은 창이다"
    total = 0
    for k in helpers:
        block = text.split(HEAD.format(k=k) + "\\n", 1)[1].split("\\n\\n", 1)[0].split("\\n보조 전문가의 지식카드 발췌는", 1)[0]
        n = sum(len(ln) for ln in block.split("\\n"))
        assert 0 < n <= share, (k, n, share)
        total += n
    assert total <= a._helper_knowledge_share(1), "보조 전원의 합이 합 몫을 넘지 않는다"
    main = text.split("[main 지식카드 — 이번 질문 연관 발췌]\\n", 1)[1].split("\\n이 발췌는", 1)[0]
    assert sum(len(ln) for ln in main.split("\\n")) > a.CHAT_KNOWLEDGE_BUDGET - a._HELPER_KB_LINE, "주 전문가 몫은 그대로다"


# ── 끄면 · 보조가 없으면 종전과 바이트까지 같다 ─────────────────────────────────────
def test_끄면_보조를_찾지_않고_프롬프트는_삽입분만_빠진_종전_꼴이다(monkeypatch):
    hits = {"main": ([_hit("R-MAIN")], ""), "h1": ([_hit("R-H1", "보조 카드")], ""), "h2": ([], "")}
    on, evs_on, calls_on = _turn(monkeypatch, ["main", "h1", "h2"], hits)
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE", False)
    off, evs_off, calls_off = _turn(monkeypatch, ["main", "h1", "h2"], hits)
    assert [k for k, _ in calls_off] == ["main"], "끄면 보조는 찾지 않는다"
    assert "[보조 " not in off and "보조 전문가의 지식카드 발췌는" not in off
    i = on.index("\\n\\n[보조 h1 지식카드")
    end = "근거로 든 것처럼 쓰지 마라."
    j = on.index(end, i) + len(end)
    assert on[:i] + on[j:] == off, "켠 것과 끈 것의 차이는 보조 지식카드 삽입분 하나뿐이다"
    assert [s for s in _steps(evs_on) if not s.startswith("보조 ")] == _steps(evs_off)
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE", True)
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE_BUDGET", 0)
    off0, _evs, calls0 = _turn(monkeypatch, ["main", "h1", "h2"], hits)
    assert off0 == off and [k for k, _ in calls0] == ["main"], "합 몫 0 도 끈다"


def test_보조가_없으면_켜든_끄든_종전과_바이트까지_같다(monkeypatch):
    hits = {"main": ([_hit("R-MAIN")], "")}
    on, evs_on, calls_on = _turn(monkeypatch, ["main"], hits)
    monkeypatch.setattr(a, "PERSONA_HELPER_KNOWLEDGE", False)
    off, evs_off, calls_off = _turn(monkeypatch, ["main"], hits)
    assert on == off and _steps(evs_on) == _steps(evs_off) and calls_on == calls_off
    assert "[보조 " not in on


# ── 주 전문가의 강등 사유도 턴마다 돌려받는다 — 앞 턴 · 다른 사람 턴의 사유가 새지 않는다 ───────────────────────
def _ke(monkeypatch, *, tools=None, hits=([], ""), raises=None, budget=5000):
    async def ktools(_app, _groups):
        if raises:
            raise raises
        return {"agent_search": object()} if tools is None else tools

    async def search(_tools, _agent_type, _q, **_k):
        return hits
    monkeypatch.setattr(a, "_knowledge_tools", ktools)
    monkeypatch.setattr(a, "_agent_search_hits", search)
    return asyncio.run(a._knowledge_excerpt(NS(state=NS()), [], "main", "q", budget))


def test_안쪽_함수는_발췌와_사유를_돌려준다(monkeypatch):
    assert _ke(monkeypatch, hits=([], DEGRADE)) == ("", DEGRADE)
    text, note = _ke(monkeypatch, hits=([_hit("R-1")], ""))
    assert "R-1" in text and note == ""
    fb = "hybrid 검색 180초 초과 → semantic 로 되물음"
    text, note = _ke(monkeypatch, hits=([_hit("R-1")], fb))
    assert "R-1" in text and note == fb, "결과가 있어도 사유는 돌려준다(쓰는 쪽이 고른다)"
    assert _ke(monkeypatch, raises=RuntimeError("x")) == ("", "조회 실패(RuntimeError)")
    assert _ke(monkeypatch, tools={}) == ("", None), "지식 검색 도구를 못 받았으면 None — '없다' 도 '강등' 도 아니다"
    lines = _ke(monkeypatch, hits=([_hit(f"R-{i}", "다" * 400) for i in range(10)], ""), budget=1000)[0].split("\n")
    assert sum(len(ln) for ln in lines) <= 1000 and len(lines) == 2, lines
    assert not hasattr(a, "_persona_knowledge"), "프로세스 전체가 나눠 쓰던 칸(_persona_knowledge.last_note)은 없다"


def test_주_전문가_발췌는_CHAT_KNOWLEDGE_BUDGET_으로_잘린다(monkeypatch):
    monkeypatch.setattr(a, "CHAT_KNOWLEDGE_BUDGET", 1000)
    text, _evs, _ = _turn(monkeypatch, ["main"], {"main": ([_hit(f"R-{i}", "다" * 400) for i in range(10)], "")})
    block = text.split("[main 지식카드 — 이번 질문 연관 발췌]\\n", 1)[1].split("\\n이 발췌는", 1)[0]
    lines = block.split("\\n")
    assert sum(len(ln) for ln in lines) <= 1000 and len(lines) == 2, lines


def test_앞_턴의_강등_사유가_다음_턴에_안_보인다(monkeypatch):
    _text, evs, _ = _turn(monkeypatch, ["main"], {"main": ([], DEGRADE)})
    w = _warns(evs)
    assert [x["code"] for x in w] == ["knowledge_degraded"] and DEGRADE in w[0]["message"], w
    assert f"지식카드 조회 실패 — {DEGRADE}" in _steps(evs)
    # 다음 턴은 지식 검색 도구를 못 받았다(권한 · 게이트웨이 목록 빔) — 종전엔 앞 턴의 사유가 이 턴의 경고로 나갔다.
    text, evs, _ = _turn(monkeypatch, ["main"], {}, tools={})
    assert not _warns(evs), _warns(evs)
    assert "연관 지식카드 없음 — 일반 지식으로 답변" in _steps(evs) and not any(DEGRADE in s for s in _steps(evs))
    assert "이번 질문과 연관된 보유 지식을 찾지 못했다" in text and MAIN_DEGRADED not in text, \
        "도구를 못 받은 주 전문가는 종전처럼 '연관 없음' 갈래다"


def test_두_턴을_동시에_돌려도_사유가_섞이지_않는다(monkeypatch):
    """A 턴(강등)과 B 턴(지식 검색 도구 없음)이 한 프로세스에서 겹친다 — B 는 A 가 강등된 **뒤에** 묻는다. 한 칸을 나눠 쓰면 B 가 A 의 사유를
    자기 경고로 낸다(다른 사람의 사유가 새는 길)."""
    ev: dict = {}
    sinks: dict = {}

    async def meta(_app, _groups, key):
        return {"key": key, "name": key, "role": f"{key} 역할", "operator": False, "apps": [], "key_tools": [], "note": ""}

    async def ktools(_app, groups):
        if "gb" in groups:
            await asyncio.wait_for(ev["a"].wait(), 2.0)
            return {}
        return {"agent_search": object()}

    async def search(_tools, agent_type, _q, **_k):
        if agent_type == "pa":
            ev["a"].set()
            return [], DEGRADE
        return [], ""

    class Agent:
        def __init__(self, g):
            self.g = g

        async def astream_events(self, inputs, version=None, config=None):
            sinks[self.g] = inputs["messages"]
            yield {"event": "on_chat_model_stream", "data": {"chunk": NS(content="답변")}}

    async def agent_for(_app, groups, *_a, **_k):
        return Agent(groups[0])

    monkeypatch.setattr(a, "_persona_meta", meta)
    monkeypatch.setattr(a, "_knowledge_tools", ktools)
    monkeypatch.setattr(a, "_agent_search_hits", search)
    monkeypatch.setattr(a, "_agent_for", agent_for)
    monkeypatch.setattr(a, "_tools_map", lambda: {})
    monkeypatch.setattr(a, "_ctx_cache", {"n": 128_000, "src": "server"})
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")

    async def run(g, key):
        req = a.ChatRequest(message=Q, groups=[g], pinned_agents=[key])
        return b"".join([c async for c in a._agent_stream_turn(a.app, req, None)]).decode("utf-8")

    async def both():
        ev["a"] = asyncio.Event()
        return await asyncio.gather(run("ga", "pa"), run("gb", "pb"))
    out_a, out_b = asyncio.run(both())
    wa, wb = _warns(_events(out_a)), _warns(_events(out_b))
    assert len(wa) == 1 and wa[0]["message"].startswith("pa 지식카드를 조회하지 못했습니다") and DEGRADE in wa[0]["message"], wa
    assert not wb, f"B 턴에 A 턴의 사유가 샜다: {wb}"
    assert "연관 지식카드 없음 — 일반 지식으로 답변" in _steps(_events(out_b))
    assert MAIN_DEGRADED in _flat(sinks["ga"]) and MAIN_DEGRADED not in _flat(sinks["gb"])


# ── 자기검증 — 보조 발췌도 근거로 남는다 ──────────────────────────────────────────
def test_보조_발췌도_원문_그릇에_근거로_남는다(monkeypatch):
    monkeypatch.setenv("CHAT_SELF_CHECK_GATE", "1")
    box: dict = {}
    hits = {"main": ([_hit("R-MAIN", "허용 응력 41.5 MPa")], ""), "op1": ([_hit("R-OP")], ""),
            "h1": ([_hit("R-H1", "휨 허용 0.75%")], "")}
    async def raw_capture(k):
        a._sc_keep_raw(f"검색 원문 {k} 999")      # 카탈로그 도구 래퍼(R5)가 하는 일 — 원문 그릇이 꺼져 있어야 아무것도 안 든다

    _turn(monkeypatch, ["main", "op1", "h1"], hits, operators={"op1"}, box=box, on_search=raw_capture)
    texts = box["raw"]["texts"]
    assert len(texts) == 2 and "41.5 MPa" in texts[0] and "0.75%" in texts[1], texts
    assert not any("검색 원문" in t for t in texts), "보조 검색은 원문 그릇 밖에서 돈다(gather 의 태스크도)"

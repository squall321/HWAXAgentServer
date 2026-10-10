# 심의 뒤 절반(라운드·의장·후처리·보고서 저장·다단 승계)의 고정 상한을 모델 창에서 유도한다 — 좁은 창은 종전 그대로
#
# 사용자 지침(2026-10-10) — 운영 모델은 GLM-5.2, 창 1,048,576 토큰이다. 16K·128K 시절에 박은 글자 상한(기록용 발언
# 12,000자, 핵심 요약이 읽는 결정문 앞 6,000자, 쉬운 설명 7,000자, 다단 승계 8,000/20,000자, RA 보고서 '결과' 칸
# 1,500자 …)이 1백만 토큰 창에서도 걸려 글자가 잘렸다. 좁은 창(dev 16K)은 종전 동작을 지킨다(바닥값).
#
# 앞의 **특성화** 시험은 구현 전 코드에서 초록인 것을 먼저 본 것이다. 스트림을 실제로 돌리고 LLM 호출만 가로챈다.
#
#   실행:  .venv/bin/python -m pytest tests/test_window_budget_b.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _steps, _Tool  # noqa: E402, F401

_SMALL = (16384, 128000, 200000)
_BIG = 1048576          # GLM-5.2
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]


def _win(tokens):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()
    d._free_tok_cache.clear()


def _long(tag, n):
    """n 자 남짓의 글 — 꼬리에 tag 를 둔다(뒤가 잘렸는지 꼬리표로 본다)."""
    body = "".join(f"<{i:04d}>" + "가" * 34 for i in range(n // 40))
    return body + f" [{tag}]"


class _Llm:
    """좌석·의장·그 밖(핵심 요약·쉬운 설명 등)을 가른다. 그 밖의 호출은 (system, human) 으로 적어 둔다."""

    request_timeout = None
    max_retries = 0
    max_tokens = None

    def __init__(self, seat, chair):
        self.seat, self.chair, self.others, self.prompts = seat, chair, [], []

    async def text(self, _obj, system, human):
        self.prompts.append((system, human))
        m_ = d.re.search(r"당신은 '([^']+)' 전문가", system)
        if m_:
            return json.dumps(self.seat(m_.group(1)), ensure_ascii=False)
        if "엔지니어링 톤" in system:
            return self.chair
        self.others.append((system, human))
        return "- 요약 한 줄"


def _seat_plain(key):
    return {"lens": f"{key} 관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
            "position_short": "요약", "concede": [], "rebut": [], "deepen": f"{key} 심화",
            "final_position": f"{key} 의 최종 입장", "non_negotiable": "", "vote": "진행", "stance": "동의"}


def _run(monkeypatch, tokens, *, seat=_seat_plain, chair="결정: 진행한다", tools=None, entry=None, **req):
    """심의를 끝까지 돌려 (이벤트, LLM 대역)을 받는다."""
    _win(tokens)
    llm = _Llm(seat, chair)
    tools = {"agent_search": _Tool("agent_search")} if tools is None else tools

    async def _fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", llm.text)
    stub = SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=None))
    opts = {"personas": _SEATS, "rounds": 2, "free_tools": 0, "voc": "off", "rescreen": 0,
            "persona_knowledge": 0, "save_report": 0, **req}

    async def go():
        return [delib_jobs._parse_sse(c)
                async for c in (entry or d.run_deliberation)(stub, "힌지 크랙 원인", [], opts)]

    return asyncio.run(go()), llm


def _turns(events):
    return [data for ev, data in events if ev == "delib" and data.get("kind") == "turn"]


# ── 기록용 발언(say_full)·RA 회의록 — 좁은 창은 종전 12,000자, 넓은 창은 끊지 않는다 ─────────────────
def _seat_long(key):
    return {**_seat_plain(key), "lens": _long(f"{key}-끝", 20000)}


@pytest.mark.parametrize("tokens", _SMALL)
def test_좁은_창의_기록용_발언은_종전대로_12000자에서_끊긴다(monkeypatch, tokens):
    events, _llm = _run(monkeypatch, tokens, seat=_seat_long)
    t = next(x for x in _turns(events) if x["round"] == 1 and x["persona"] == "mech-a")
    assert len(t["say_full"]) == 12000 and t["say_full_chars"] > 12000, (len(t["say_full"]), t["say_full_chars"])


def test_넓은_창의_기록용_발언은_끊지_않는다(monkeypatch):
    events, _llm = _run(monkeypatch, _BIG, seat=_seat_long)
    t = next(x for x in _turns(events) if x["round"] == 1 and x["persona"] == "mech-a")
    assert "[mech-a-끝]" in t["say_full"], f"1백만 토큰 창에서도 기록용 발언을 {len(t['say_full']):,}자에서 끊었다"
    assert len(t["say_full"]) == t["say_full_chars"]


def _saved_blocks(monkeypatch, tokens, **kw):
    save = _Tool("create_report_draft", '{"report_id": 7}')
    events, llm = _run(monkeypatch, tokens, save_report=1,
                       tools={"agent_search": _Tool("agent_search"), "create_report_draft": save}, **kw)
    assert len(save.calls) == 1, "보고서를 저장하지 않았다 — 시험 전제가 깨졌다"
    return save.calls[0]["blocks"], events


def test_넓은_창의_RA_회의록은_발언을_끊지_않는다(monkeypatch):
    blocks, _ev = _saved_blocks(monkeypatch, _BIG, seat=_seat_long)
    assert "[mech-a-끝]" in "\n".join(blocks["minutes"]), "RA 회의록이 발언을 12,000자에서 끊었다"


@pytest.mark.parametrize("tokens", (16384, _BIG))
def test_RA_결과_칸은_마지막_심화_라운드의_좌석을_전부_싣는다(monkeypatch, tokens):
    """'결과' 칸이 그 라운드 전사의 앞 1,500자였다 — 첫 좌석의 발언 일부만 남았다. 항목 상한(2,000자)은 나눠서 지킨다."""
    seats = [{"key": f"dom{i:02d}-seat", "role": "역할"} for i in range(1, 7)]
    blocks, _ev = _saved_blocks(monkeypatch, tokens, personas=seats, rounds=3,
                                seat=lambda k: {**_seat_plain(k), "deepen": _long(f"{k}-심화끝", 600)})
    got = "\n".join(blocks["results"])
    for s in seats:
        assert f"[{s['key']}-심화끝]" in got, f"RA '결과' 칸에 {s['key']} 의 심화가 없다"
    assert all(len(x) <= d._RA_ITEM_MAX for x in blocks["results"])


def test_RA_블록_건수_상한에_걸리면_원장에_알린다(monkeypatch):
    """문단·회의록 건수 상한(RA 보호값)은 두되, 걸렸으면 서버 로그만이 아니라 카드로 남긴다."""
    monkeypatch.setattr(d, "_RA_MINUTES_MAX", 3)
    blocks, events = _saved_blocks(monkeypatch, 128000)
    cards = [c for c in _cards(events, included=False) if c["source"] == "보고서 블록 상한 초과"]
    assert cards and "회의록" in cards[0]["text"], [c["source"] for c in _cards(events, included=False)]


def test_넓은_창의_MCP_전사_full_은_발언_전부를_표식_없이_준다(monkeypatch, tmp_path):
    """Claude 사용자가 받는 길 — 잡 원장에 쌓고 deliberate_transcript(full=true) 로 회수한다."""
    _win(_BIG)
    llm = _Llm(_seat_long, "결정: 진행한다")

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", llm.text)
    stub = SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=None))
    opts = {"personas": _SEATS, "rounds": 2, "free_tools": 0, "voc": "off", "rescreen": 0,
            "persona_knowledge": 0, "save_report": 0}
    j = {"id": "t-wide", "status": "running", "question": "q", "turns": [], "checkpoint": None,
         "started_at": delib_jobs._now()}
    monkeypatch.setitem(delib_jobs._JOBS, j["id"], j)
    asyncio.run(delib_jobs._drive(j, d.run_deliberation(stub, "힌지 크랙 원인", [], opts)))
    res = asyncio.run(m.deliberate_transcript(job_id="t-wide", round=1, seat="mech-a", full=True))
    t = res["turns"][0]
    assert "[mech-a-끝]" in t["say"] and "say_clipped" not in t, (len(t["say"]), t.get("say_clipped"))
    assert not res["note"], res["note"]


def test_전사_도구_설명이_full_의_해석_우려를_없다고_하지_않는다():
    """기록용 발언은 해석 목록·우려·양보 불가 조항까지 싣는다(5a56dd3) — 설명이 아직 '여기 없다' 고 적었다."""
    desc = next(t.description for t in asyncio.run(m.mcp.list_tools()) if t.name == "deliberate_transcript")
    assert "여기 없다" not in desc, desc
    assert "해석" in desc and "우려" in desc, desc


# ── 잡 원장 — 전사 상한은 조용하지 않게, 걸린 요청 값은 원장에 전문으로 ──────────────────────────────
def test_전사_상한에_걸리면_경고로_알린다(monkeypatch):
    """종전엔 401번째 턴 자리에 note 한 줄뿐이라 deliberate_status 의 warnings 에 안 떴다."""
    monkeypatch.setattr(delib_jobs, "TURN_MAX", 3)
    job = {"id": "t-cap", "turns": []}
    for i in range(5):
        delib_jobs._apply(job, "delib", {"kind": "turn", "round": 1, "persona": f"s{i}", "say": "말"})
    assert len(job["turns"]) == 4 and "note" in job["turns"][-1], job["turns"]
    assert any("전사 상한" in w and "DELIB_JOB_TURN_MAX" in w for w in job.get("warnings") or []), job.get("warnings")
    assert job.get("warnings_total") == 1, "한 번만 알린다"


def test_원장은_요청_값을_전문으로_남기고_응답은_표식을_달아_줄인다():
    """applied_opts 는 무엇이 걸렸는지의 기록이다 — 사람 의견이 앞 117자 + '…' 로만 남아 나중에 재현할 수 없었다."""
    note = "사람 의견 " + "가" * 3000 + " [끝]"
    echo = delib_jobs._opts_echo({"human_note": note, "evidence": [{"result": "x"}] * 3, "rounds": 3})
    assert echo["human_note"] == note and echo["evidence"] == 3 and echo["rounds"] == 3, echo
    job = {"id": "t-o", "status": "done", "question": "q", "opts": echo}
    shown = delib_jobs.summary(job, full=True)["applied_opts"]["human_note"]
    assert len(shown) < 300 and f"{len(note):,}자" in shown, shown


# ── 후처리 — 핵심 요약·쉬운 설명은 결정문 전부를 읽는다(좁은 창은 종전 앞 6,000·7,000자) ─────────────────
_DECISION = _long("결정문-끝", 30000)


def _other(llm, needle):
    return next(h for s, h in llm.others if needle in s)


def test_16K_창의_핵심_요약과_쉬운_설명은_종전대로_앞부분만_읽는다(monkeypatch):
    _events, llm = _run(monkeypatch, 16384, chair=_DECISION)
    summ = _other(llm, "군더더기 없이")
    assert summ.endswith(_DECISION[:6000]) and _DECISION[:6001] not in summ
    plain = _other(llm, "비전문가에게")
    assert "[결정문-끝]" not in plain and len(plain) < 7600, len(plain)


@pytest.mark.parametrize("tokens", (128000, _BIG))
def test_핵심_요약과_쉬운_설명은_결정문_전부를_읽는다(monkeypatch, tokens):
    """결정문 뒤쪽(소수의견·판정·risk_spec)을 못 본 요약이 보고서 맨 앞에 붙고 이어하기 요약의 머리가 됐다."""
    _events, llm = _run(monkeypatch, tokens, chair=_DECISION)
    assert "[결정문-끝]" in _other(llm, "군더더기 없이"), "핵심 요약이 결정문 앞 6,000자만 읽었다"
    assert "[결정문-끝]" in _other(llm, "비전문가에게"), "쉬운 설명이 결정문 앞 7,000자만 읽었다"


def test_16K_창에서_요약이_결정문을_줄여_읽으면_알린다(monkeypatch):
    events, _llm = _run(monkeypatch, 16384, chair=_DECISION)
    cards = [c for c in _cards(events, included=False) if c["source"] == "후처리 입력 상한 초과"]
    assert cards and "핵심 요약" in cards[0]["text"] and f"{len(_DECISION):,}자" in cards[0]["text"], cards


def test_넓은_창에서는_후처리_상한_카드가_없다(monkeypatch):
    events, _llm = _run(monkeypatch, _BIG, chair=_DECISION)
    assert not [c for c in _cards(events) if c["source"] == "후처리 입력 상한 초과"]


def test_best_of_심판은_넓은_창에서_후보_전문을_본다(monkeypatch):
    cands = iter([_long("후보1-끝", 9000), _long("후보2-끝", 9000)])

    class _Bo(_Llm):
        async def text(self, _obj, system, human):
            if "엔지니어링 톤" in system:
                return next(cands, "결정")
            return await super().text(_obj, system, human)

    _win(_BIG)
    llm = _Bo(_seat_plain, "")
    monkeypatch.setattr(d, "_llm_text", llm.text)

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    stub = SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=None))
    opts = {"personas": _SEATS, "rounds": 2, "free_tools": 0, "voc": "off", "rescreen": 0,
            "persona_knowledge": 0, "save_report": 0, "chair_bestof": 2}

    async def go():
        return [c async for c in d.run_deliberation(stub, "q", [], opts)]

    asyncio.run(go())
    judge = _other(llm, "심의 기록 심사자")
    assert "[후보1-끝]" in judge and "[후보2-끝]" in judge, "심판이 후보 앞 4,000자만 봤다"


def test_의장_결정문이_출력_상한에서_끊기면_경고한다(monkeypatch):
    """결정문 꼬리의 표식 한 줄뿐이라 deliberate_status 의 warnings 에 안 떴다."""
    events, _llm = _run(monkeypatch, _BIG, chair="결정: 진행" + "\n" + d._OUT_CUT_MARK)
    w = [data for ev, data in events if ev == "warning" and data.get("code") == "chair_output_cut"]
    assert w and "DELIB_MAX_TOKENS" in (w[0].get("knob") or ""), w


def test_날조_가능_웹_인용은_8건을_넘으면_총_건수를_적는다(monkeypatch):
    bad = [f"[W:d_{i:012x}#1]" for i in range(11)]

    async def _verify(*_a, **_k):
        return 0, len(bad), bad, []

    monkeypatch.setattr(d, "_verify_web_citations", _verify)
    events, _llm = _run(monkeypatch, _BIG, chair="결정: " + " ".join(bad), search_sources=["web"])
    final = next(data["text"] for ev, data in events if ev == "delib" and data.get("kind") == "decision")
    assert "외 3건" in final, final[-400:]


# ── 다단 승계 — 1단 결정문을 2단에, 2단 결정문을 3단에 넘길 때 ─────────────────────────────────────
_CONT = "[이전 심의 요약 — 이어서 논의]"


def _carried(llm):
    """다음 단 좌석·의장이 받은 '이전 심의 요약' 블록들."""
    return [h.split(_CONT, 1)[1] for _s, h in llm.prompts if _CONT in h]


def test_16K_창의_1단_결정문_승계는_종전대로_8000자다(monkeypatch):
    _events, llm = _run(monkeypatch, 16384, chair=_long("1단-끝", 20000), entry=d.run_sim_deliberation)
    got = _carried(llm)
    assert got and all("[1단-끝]" not in x for x in got), "시험 전제 — 16K 는 앞 8,000자만 넘긴다"


def test_넓은_창의_1단_결정문은_잘리지_않고_2단에_간다(monkeypatch):
    events, llm = _run(monkeypatch, _BIG, chair=_long("1단-끝", 20000), entry=d.run_sim_deliberation)
    got = _carried(llm)
    assert got and all("[1단-끝]" in x for x in got), "1백만 토큰 창에서도 1단 결정문을 앞 8,000자만 넘겼다"
    assert not [c for c in _cards(events) if c["source"] == "다단 승계 상한 초과"]


def test_다단_승계에서_줄이면_카드로_알린다(monkeypatch):
    events, _llm = _run(monkeypatch, 16384, chair=_long("1단-끝", 20000), entry=d.run_sim_deliberation)
    cards = [c for c in _cards(events, included=False) if c["source"] == "다단 승계 상한 초과"]
    assert cards and "1단 결정문" in cards[0]["text"] and "8,000자" in cards[0]["text"], cards


def test_넓은_창의_2단_결정문은_3단에_20000자보다_길게_간다(monkeypatch):
    _events, llm = _run(monkeypatch, _BIG, chair=_long("단-끝", 25000), entry=d.run_sim_deliberation,
                        build_plan=1)
    got = _carried(llm)
    assert len(got) >= 2 and all("[단-끝]" in x for x in got), "2단 결정문을 3단에 앞 20,000자만 넘겼다"


def test_넓은_창의_양보_불가_조항은_1200자에서_끊기지_않는다(monkeypatch):
    nn = _long("조항-끝", 3000)
    events, _llm = _run(monkeypatch, _BIG, rounds=2, seat=lambda k: {**_seat_plain(k), "non_negotiable": nn})
    t = [x for x in _turns(events) if x["round"] == 2]
    assert t and all(x["non_negotiable"] == nn for x in t), [len(x.get("non_negotiable", "")) for x in t]


def test_좁은_창의_양보_불가_조항은_종전대로_1200자이고_원래_길이를_남긴다(monkeypatch):
    nn = _long("조항-끝", 3000)
    events, _llm = _run(monkeypatch, 128000, rounds=2, seat=lambda k: {**_seat_plain(k), "non_negotiable": nn})
    t = [x for x in _turns(events) if x["round"] == 2]
    assert t and all(x["non_negotiable"] == nn[:1200] and x["non_negotiable_chars"] == len(nn) for x in t), t[0]


def test_양보_불가_조항이_목록이면_파이썬_표기가_아니라_항목으로_잇는다(monkeypatch):
    events, _llm = _run(monkeypatch, _BIG, rounds=2,
                        seat=lambda k: {**_seat_plain(k), "non_negotiable": ["R 은 0.3 이상", "두께 유지"]})
    t = [x for x in _turns(events) if x["round"] == 2][0]
    assert "['" not in t["non_negotiable"] and "R 은 0.3 이상" in t["non_negotiable"], t["non_negotiable"]


# ── /보고서 — 원문 보존형은 원문을 남긴다, 정리본은 창이 받는 만큼 이력을 읽는다 ─────────────────────
def _report(monkeypatch, history, note="", tokens=_BIG, agent=None):
    _win(tokens)
    save = _Tool("create_report_draft", '{"report_id": 58}')

    async def _fake_tools(*_a, **_k):
        return {"create_report_draft": save}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    if agent is not None:
        async def _agent_for(*_a, **_k):
            return agent
        monkeypatch.setattr(app, "_agent_for", _agent_for)

    async def go():
        return [delib_jobs._parse_sse(c) async for c in d.run_report_save(None, note, history, [], user="u@x.com")]

    events = asyncio.run(go())
    return (save.calls[0]["blocks"] if save.calls else None), events


def _hist(n_turns=8, size=3000):
    h = []
    for i in range(n_turns):
        h.append({"role": "user", "content": _long(f"질문{i}-끝", size)})
        h.append({"role": "assistant", "content": "\n\n".join(_long(f"답{i}-{k}", 200) for k in range(12))
                  + "\n\n" + _long(f"답{i}-끝", size)})
    return h


def test_원문_보존형_보고서는_발언을_400자_40건에서_끊지_않는다(monkeypatch):
    h = _hist(n_turns=25)
    blocks, _ev = _report(monkeypatch, h)
    minutes = "\n".join(blocks["minutes"])
    assert "[질문24-끝]" in minutes and "[답24-끝]" in minutes, "회의록이 발언당 400자·40건에서 끊겼다"
    assert "[질문0-끝]" in "\n".join(blocks["background"]), "질문 전문이 1,200자에서 끊겼다"
    results = "\n".join(blocks["results"])
    assert "[답0-끝]" in results and "[답23-끝]" in results, "결과 칸이 답 6건 × 1,500자였다"
    rec = "\n".join(blocks["recommendation"])
    assert "[답24-11]" in rec and "[답24-끝]" in rec, "권고 칸이 마지막 답의 앞 10문단이었다"
    assert all(len(x) <= d._RA_ITEM_MAX for v in blocks.values() for x in v), "RA 항목 상한은 나눠 지킨다"


def test_원문_보존형_보고서가_건수_상한에_걸리면_결과_문구에_적는다(monkeypatch):
    monkeypatch.setattr(d, "_RA_MINUTES_MAX", 5)
    _blocks, events = _report(monkeypatch, _hist(n_turns=4, size=100))
    text = next(data["content"] for ev, data in events if ev == "result")
    assert "#58" in text and "회의록" in text and "8건 중 앞 5건" in text, text


class _Agent:
    def __init__(self):
        self.seen = []

    async def ainvoke(self, inp):
        self.seen.append(inp["messages"][0][1])
        return {"messages": [SimpleNamespace(content="# 제목\n## 요약\n요약\n## 결론과 근거\n결론")]}


def test_16K_창의_정리본은_종전대로_이력_60000자를_읽는다(monkeypatch):
    ag = _Agent()
    _report(monkeypatch, _hist(n_turns=12, size=4000), note="정리", tokens=16384, agent=ag)
    body = ag.seen[0].split("대화 이력:\n", 1)[1]
    assert "중간 생략" in body and len(body) < 60200, len(body)


def test_넓은_창의_정리본은_이력을_가운데서_덜지_않는다(monkeypatch):
    ag = _Agent()
    _blocks, events = _report(monkeypatch, _hist(n_turns=12, size=4000), note="정리", agent=ag)
    body = ag.seen[0].split("대화 이력:\n", 1)[1]
    assert "중간 생략" not in body and "[답5-끝]" in body, len(body)


def test_16K_창의_정리본이_이력을_덜었으면_결과_문구에_적는다(monkeypatch):
    _blocks, events = _report(monkeypatch, _hist(n_turns=12, size=4000), note="정리", tokens=16384, agent=_Agent())
    text = next(data["content"] for ev, data in events if ev == "result")
    assert "#58" in text and "정리본은 대화 이력" in text, text

# 좌석 추천의 온톨로지 신호 스위치(SEAT_ONTO) 시험 — 기본 끔 · 도구가 인자를 알 때만 · 리스크 심사 제외 · 그림자 기록
import asyncio
import inspect
import json

import pytest

import deliberation
import persona_links
import thinking


class _Tool:
    """recommend_agents 흉내 — 받은 인자를 적고 정해 둔 답을 준다. args 는 진짜 도구처럼 인자 이름 → 스키마다."""

    def __init__(self, reply, knows_onto=True):
        self.calls, self.reply = [], reply
        self.args = {"q": {"type": "string"}, "top_k": {"type": "integer"}}
        if knows_onto:
            self.args["onto"] = {"type": "string"}

    async def ainvoke(self, args):
        self.calls.append(dict(args))
        return json.dumps(self.reply, ensure_ascii=False)


_REPLY = {"agents": [{"agent_type": "rel-a", "name": "A", "score": 2.0},
                     {"agent_type": "rel-b", "name": "B", "score": 1.5, "onto": {"objects": 1, "strong": 3, "weak": 0, "bonus": 1.0}},
                     {"agent_type": "pcb-c", "name": "C", "score": 1.0}]}


def test_switch_is_off_by_default_and_needs_a_tool_that_knows_the_argument(monkeypatch):
    tools = {"recommend_agents": _Tool(_REPLY)}
    monkeypatch.delenv("SEAT_ONTO", raising=False)
    assert persona_links.seat_onto_mode() == "off" and persona_links.reco_onto(tools) == ""
    for raw, want in (("rank", "rank"), (" Shadow ", "shadow"), ("on", "off"), ("1", "off"), ("", "off")):
        monkeypatch.setenv("SEAT_ONTO", raw)
        assert persona_links.seat_onto_mode() == want
    monkeypatch.setenv("SEAT_ONTO", "rank")
    assert persona_links.reco_onto(tools) == "rank"
    assert persona_links.reco_onto(tools, risk=True) == ""                               # 리스크 심사에는 넘기지 않는다(D-18)
    assert persona_links.reco_onto({"recommend_agents": _Tool(_REPLY, knows_onto=False)}) == ""   # 옛 AIDataHub
    assert persona_links.reco_onto({}) == "" and persona_links.reco_onto(None) == ""


def test_tool_schema_as_json_schema_dict_is_read_too():
    class _SchemaOnly:
        args_schema = {"properties": {"q": {}, "onto": {}}}

    class _Raises:
        @property
        def args(self):
            raise RuntimeError("x")
        args_schema = {"properties": {"q": {}}}

    assert persona_links._tool_knows(_SchemaOnly(), "onto") is True
    assert persona_links._tool_knows(_Raises(), "onto") is False


def test_arguments_are_unchanged_when_nothing_is_asked():
    assert persona_links.reco_args({}, "질문") == {"q": "질문"}
    assert persona_links.reco_args({}, "질문", 40) == {"q": "질문", "top_k": 40}
    assert persona_links.reco_args({}, "질문", 5, onto="rank") == {"q": "질문", "top_k": 5, "onto": "rank"}


def test_rank_keys_and_discover_pass_the_value_only_when_given(monkeypatch):
    tool = _Tool(_REPLY)
    tools = {"recommend_agents": tool}
    assert asyncio.run(deliberation._rank_keys(tools, "솔더 크랙")) == ["rel-a", "rel-b", "pcb-c"]
    assert tool.calls[-1] == {"q": "솔더 크랙"}                                           # 기본은 글자 그대로
    asyncio.run(deliberation._rank_keys(tools, "솔더 크랙", 40, onto="rank"))
    assert tool.calls[-1] == {"q": "솔더 크랙", "top_k": 40, "onto": "rank"}

    async def _role(_tools, key, why=None):
        return f"역할 {key}"

    monkeypatch.setattr(deliberation, "_restore_role", _role)
    seats = asyncio.run(deliberation._discover(tools, "솔더 크랙", 2, onto="shadow"))
    assert [s["key"] for s in seats] == ["rel-a", "rel-b"] and tool.calls[-1]["onto"] == "shadow"
    asyncio.run(deliberation._discover(tools, "솔더 크랙", 2))
    assert "onto" not in tool.calls[-1]


def test_summon_passes_the_value_only_when_given():
    tool = _Tool(_REPLY)
    tools = {"recommend_agents": tool}
    got = asyncio.run(thinking._summon(tools, "솔더 크랙", 5, set()))
    assert [s["key"] for s in got] == ["rel-a", "rel-b", "pcb-c"] and tool.calls[-1] == {"q": "솔더 크랙", "top_k": 5}
    asyncio.run(thinking._summon(tools, "솔더 크랙", 5, set(), onto="rank"))
    assert tool.calls[-1] == {"q": "솔더 크랙", "top_k": 5, "onto": "rank"}


def test_shadow_record_says_who_would_enter_and_writes_only_when_logging_is_on(monkeypatch, tmp_path):
    monkeypatch.setenv("SEAT_EVAL_DIR", str(tmp_path))
    monkeypatch.delenv("PERSONA_SHADOW_LOG", raising=False)
    rec = persona_links.shadow_onto("delib", _REPLY["agents"], 1)
    assert rec == {"u": "onto", "site": "delib", "want": 1, "rows": 3, "signal": 1, "enter": ["rel-b"], "leave": ["rel-a"]}
    assert not list(tmp_path.rglob("s-*.jsonl"))                                          # 꺼져 있으면 적지 않는다
    # 덧점이 없으면 아무도 들고 나지 않는다 · 깨진 줄은 건너뛴다
    plain = persona_links.shadow_onto("think", [{"agent_type": "a", "score": 1}, "깨진 줄", {"score": 3}, {"agent_type": "b", "score": "x"}], 2)
    assert plain["rows"] == 2 and plain["signal"] == 0 and plain["enter"] == [] and plain["leave"] == []
    monkeypatch.setenv("PERSONA_SHADOW_LOG", "1")
    persona_links.shadow_onto("delib", _REPLY["agents"], 1)
    files = list(tmp_path.rglob("s-*.jsonl"))
    if files:                                                                             # 박스 자리가 import 때 굳는 구성이면 파일은 그쪽에 있다
        line = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
        assert line["u"] == "onto" and "q" not in line and "question" not in line


def test_shadow_mode_does_not_change_the_order_the_tool_gave(monkeypatch):
    seen = []
    monkeypatch.setattr(persona_links, "shadow_onto", lambda site, items, want: seen.append((site, len(items), want)))
    tools = {"recommend_agents": _Tool(_REPLY)}
    assert asyncio.run(deliberation._rank_keys(tools, "q", onto="shadow")) == ["rel-a", "rel-b", "pcb-c"]
    asyncio.run(thinking._summon(tools, "q", 7, set(), onto="shadow"))
    assert seen == [("delib", 3, deliberation.N_PERSONAS), ("think", 3, 7)]
    seen.clear()
    asyncio.run(deliberation._rank_keys(tools, "q", onto="rank"))
    asyncio.run(deliberation._rank_keys(tools, "q"))
    assert seen == []                                                                     # shadow 가 아니면 적지 않는다


def test_call_sites_ask_through_the_switch_and_risk_review_is_excluded():
    """첫 좌석 · 띵킹 첫 소집 · 심의 미리보기 셋만 스위치를 본다 — 엔진의 첫 좌석은 리스크 심사 여부를 함께 넘긴다."""
    import app

    src = inspect.getsource(deliberation)
    assert 'onto=persona_links.reco_onto(tools, risk=opts.chair_template == "risk-review")' in src
    assert src.count("persona_links.reco_onto(") == 1
    assert inspect.getsource(thinking).count("persona_links.reco_onto(tools)") == 1
    assert inspect.getsource(app.deliberate_experts).count("_pl.reco_onto(tools)") == 1

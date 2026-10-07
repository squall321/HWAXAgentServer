# 지정 도구의 인자를 정하는 계획자가 앞선 도구 결과를 전부 보는지 — 계획자가 받은 프롬프트로 본다
#
# 지정 도구(delib_opts.tools)는 목록·검색 도구를 먼저 돌리고, 상세 도구의 인자(id 등)는 LLM 계획자가 **앞서
# 조회한 결과**에서 가져다 쓴다. 그런데 그 결과를 이어 붙여 앞 2,000자에서 잘라 줬다. 도구 하나의 결과가
# 2,000자까지라 첫 도구의 결과만 보였고(그마저 토막), 둘째 목록 도구가 준 id 는 계획자에게 없었다 — 상세 도구가
# 식별자를 지어내거나 건너뛰었는데, 표식도 알림도 없었다.
#
# **스트림을 실제로 돌려서** 계획자가 받은 프롬프트를 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_tool_plan_context.py -q
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402

from test_delib_silent_drops import _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_PREV = "[앞서 조회한 결과"


def _run(monkeypatch, sizes):
    """크기가 sizes 인 결과를 내는 목록 도구들 뒤에 상세 도구 하나를 돌린다.
    반환 (이벤트, {도구 이름: 그 도구의 계획자가 받은 '앞서 조회한 결과' 블록 또는 ""})."""
    lists = [f"list_probe_{i}" for i in range(1, len(sizes) + 1)]
    tools = {nm: _Tool(nm, answer=json.dumps({"id": 4000 + i, "mark": f"ID-{i}-HEAD", "body": "x" * sz}))
             for i, (nm, sz) in enumerate(zip(lists, sizes), start=1)}
    tools["get_detail"] = _Tool("get_detail", answer='{"value": 1.5}')
    tools["agent_search"] = _Tool("agent_search")
    plans = {}

    async def _llm(_obj, system, human):
        if "도구 호출 계획자" in system:
            name = re.search(r"\[도구\]\n(\w+):", human).group(1)
            plans[name] = human[human.index(_PREV):].split("\n주제의 정량 분석에 맞게")[0] if _PREV in human else ""
            return '{"q": "힌지"}'
        return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["a", "b"],
                           "position_short": "입장"}, ensure_ascii=False)

    monkeypatch.setattr(d, "_llm_text", _llm)
    events = _stream(monkeypatch, {"tools": ["get_detail", *lists], "persona_knowledge": 0}, tools=tools)
    assert list(plans) == [*lists, "get_detail"], f"시험 전제 — 목록 도구가 먼저, 상세 도구가 마지막에 돈다: {list(plans)}"
    return events, plans


def test_둘째_목록_도구의_결과가_상세_도구의_계획자에게_보인다(monkeypatch):
    """종전엔 첫 도구의 결과 1,900자가 2,000자 창을 다 써서 둘째 도구의 id 가 창 밖이었다."""
    _events, plans = _run(monkeypatch, [1900, 300])
    seen = plans["get_detail"]
    assert "ID-1-HEAD" in seen and "ID-2-HEAD" in seen and "4002" in seen, f"둘째 도구의 결과가 안 보인다 — {seen[-200:]}"
    assert seen.count("### list_probe_") == 2
    assert " …[" not in seen and "앞부분만 실었다" not in seen, "안 줄였는데 줄였다고 적었다"
    assert plans["list_probe_1"] == "", "앞선 결과가 없는 첫 도구에 빈 블록을 실었다"
    assert "ID-1-HEAD" in plans["list_probe_2"]


def test_길어서_줄일_때는_도구마다_같은_몫이고_줄였다고_글과_상태줄이_말한다(monkeypatch):
    events, plans = _run(monkeypatch, [2500, 2500, 2500, 2500])
    seen = plans["get_detail"]
    assert all(f"ID-{i}-HEAD" in seen for i in range(1, 5)), "뒤쪽 도구가 계획자에게 안 갔다"
    assert seen.count(" …[") == 4, "줄인 결과마다 표식이 있어야 한다 — 없으면 계획자는 그것이 전부인 줄 안다"
    assert "앞부분만 실었다 — 잘린 뒤쪽에 있을 값을 짐작하지 마라" in seen.split("]\n")[0], seen[:200]
    assert len(seen) <= d._TOOL_INJECT_MAX + 200, f"상한을 넘겨 실었다 — {len(seen):,}자"
    line = next(s_ for s_ in _steps(events) if s_.startswith("지정 도구 인자 구성: get_detail"))
    share = re.search(r"앞선 결과 4건은 도구마다 앞 ([\d,]+)자까지만 본다", line)
    assert share, line
    assert f"자 중 {share.group(1)}자]" in seen, "상태줄이 말한 몫과 글의 표식이 다르다"
    # 앞선 결과가 상한 안인 도구의 줄에는 그 말이 없다.
    assert next(s_ for s_ in _steps(events) if s_.startswith("지정 도구 인자 구성: list_probe_2")) == (
        "지정 도구 인자 구성: list_probe_2")


def test_좌석에_싣는_블록과_같은_규칙이다():
    """한 함수가 둘을 만든다 — 한쪽만 고치면 좌석이 본 것과 계획자가 본 것이 달라진다."""
    got = [(f"### t{i} ← {{}}\n", f"<{i}>" + "가" * 3000) for i in range(1, 5)]
    text, share = d._fit_tool_results(got, d._TOOL_INJECT_MAX)
    assert share and all(f"<{i}>" in text for i in range(1, 5)) and len(text) <= d._TOOL_INJECT_MAX
    assert text.count(f"자 중 {share:,}자]") == 4
    short, none = d._fit_tool_results(got[:1], d._TOOL_INJECT_MAX)
    assert none == 0 and short.endswith(f" …[3,003자 중 {d._TOOL_CHUNK_MAX:,}자]"), short[-40:]

# 좌석 조회 결과의 '원문 N자' 표식이 조회 턴에서 먼저 잘린 길이가 아니라 도구가 낸 원문 길이를 말한다 — engine-budget A23-6
#
# 조회 턴의 도구 래퍼(app._cap_tool → app._cap)가 결과를 _free_result_chars(1백만 토큰 창 200,000자)에서 자르고 원문 길이는
# 꼬리의 경고('…[경고: 도구 출력이 N자였고 …')에만 남긴다. 발언 턴은 같은 결과를 몫에 맞춰 앞을 남기며 다시 줄이므로 그 경고가
# 잘려 나가고, 표식·카드·공용 풀의 '원문' 이 잘린 뒤 길이(200,1xx자)로 적혔다 — 좌석은 원문이 실제보다 세 배 짧다고 읽었다.
#
#   실행:  .venv/bin/python -m pytest tests/test_lookup_orig_len.py -q
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import deliberation as d  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _stream, _Tool  # noqa: E402, F401
from test_window_budget import _BIG, _Msg, _Recorder, _win  # noqa: E402

_BODY = "<r0>" + "값" * 600000 + "</r0>"          # 도구가 낸 원문 — 600,009자


def _seat(_key):
    return {"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A", "B"], "position_short": "요약",
            "concede": [], "rebut": ["반박"], "deepen": "심화", "final_position": "최종", "non_negotiable": "",
            "vote": "진행", "stance": "동의"}


def _run(monkeypatch, tokens):
    import langgraph.prebuilt

    prompts = []

    async def _llm(_obj, system, human):
        if "전문가입니다" in system:
            prompts.append(human)
            return json.dumps(_seat(""), ensure_ascii=False)
        return "결정문"

    _win(tokens)
    # 생산자 실제 함수 — 조회 턴 래퍼가 좌석에게 돌려주는 글이다(app._cap_tool 이 _cap 으로 자른다).
    capped = app._cap(_BODY, d._free_result_chars(d._resolve_opts({}).tool_budget))
    assert len(capped) < len(_BODY), "시험 전제 — 조회 턴에서 잘려야 한다"
    msgs = [_Msg("ai", "", tool_calls=[{"id": "c0", "args": {"q": "힌지"}}]),
            _Msg("tool", capped, tool_call_id="c0", name="get_material"),
            _Msg("ai", "조회 요약: 끝")]
    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: _Recorder(msgs))
    events = _stream(monkeypatch, {"personas": [{"key": "mech-a", "role": "역할"}, {"key": "rel-b", "role": "역할"}],
                                   "rounds": 3, "save_report": 0, "rebut_quote": 0, "free_tools": 1,
                                   "persona_knowledge": 0},
                     tools={"agent_search": _Tool("agent_search"), "get_material": _Tool("get_material")},
                     until=lambda ev, _data: ev == "done")
    return events, prompts


def _block(human, head):
    i = human.find(head)
    return "" if i < 0 else human[i:].split("\n\n[", 1)[0]


def test_발언_턴의_자기_조회_표식은_도구_원문_길이를_적는다(monkeypatch):
    _events, prompts = _run(monkeypatch, _BIG)
    blk = _block(prompts[0], "[당신이 직접 조회한 결과")
    assert blk, prompts[0][-500:]
    marks = re.findall(r"…\[([\d,]+)자 중 앞 ([\d,]+)자\]", blk)
    assert marks and marks[0][0] == f"{len(_BODY):,}", marks
    assert int(marks[0][1].replace(",", "")) <= d._free_result_chars(3), marks


def test_자기_조회_상한_카드의_원문_합은_도구_원문이다(monkeypatch):
    events, _prompts = _run(monkeypatch, _BIG)
    cards = [c for c in _cards(events, included=False) if c["source"] == "자기 조회 결과 상한 초과"]
    assert cards and f"{len(_BODY):,}자" in cards[0]["text"], cards


def test_앞_라운드_자기_조회_블록의_원문도_도구_원문이다(monkeypatch):
    """공용 풀(gather_lens)에 적는 원문 길이 — 다음 라운드의 '앞 라운드에 조회한 결과' 블록이 그 값을 적는다."""
    _events, prompts = _run(monkeypatch, _BIG)
    later = [_block(h, "[당신이 앞 라운드에 조회한 결과") for h in prompts[2:]]
    later = [b for b in later if b]
    assert later and all(f"원문 {len(_BODY):,}자" in b for b in later), [b[-120:] for b in later]


def test_좁은_창에서도_원문_길이를_적는다(monkeypatch):
    _events, prompts = _run(monkeypatch, 128000)
    blk = _block(prompts[0], "[당신이 직접 조회한 결과")
    assert f"{len(_BODY):,}자 중 앞" in blk, blk[-200:]


# ── 꼬리 경고 읽기 — app._cap 의 실제 출력으로 맞춰 본다(글자를 바꾸면 여기서 잡힌다) ───────────────────────────
def test_조회_턴_래퍼가_자른_글에서_원문_길이와_자른_자리를_읽는다():
    for n, lim in ((600009, 200000), (1234567, 23359), (5001, 5000)):
        assert d._capped_at(app._cap("가" * n, lim)) == (n, lim)
        assert d._orig_len(app._cap("가" * n, lim)) == n
    assert d._capped_at(app._cap("가" * 100, 200)) is None and d._orig_len("가" * 100) == 100


def test_본문_속_같은_글자열에는_속지_않는다():
    """경고는 자른 자리 바로 뒤에서 시작해야 래퍼의 것이다 — 도구가 그 문구를 인용한 글이면 원문 길이가 아니다."""
    quoted = "로그: \n…[경고: 도구 출력이 9,999,999자였고 10자에서 잘렸습니다. 끝"
    assert d._capped_at(quoted) is None and d._orig_len(quoted) == len(quoted)

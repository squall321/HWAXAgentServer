# 상태줄로만 나가던 유실 알림이 잡 원장에 남는지 — 큰 패널을 끝까지 돌려 진행 조회·결과 회수로 본다
#
# 잡 원장은 상태줄을 최근 30줄만 둔다. 20석 3라운드 심의는 상태줄이 160줄을 넘는다 — 좌석마다 자유 조회가
# 한 줄씩 찍힌다. 그런데 아래 알림은 상태줄 **한 줄뿐**이었다. 결과를 회수할 때는 이미 창 밖이라, MCP 호출자는
# 좌석 하나가 한 번도 발언하지 못했다는 것도, 지정한 도구가 돌지 않았다는 것도 알 길이 없었다(잡 기록의
# applied_opts 에는 지정한 도구·앱이 걸린 것처럼 적혀 있다).
#   · 좌석 유실(오류·시간초과로 그 라운드에 발언 못 함)
#   · 지정 도구 없음 / 실패·건너뜀
#   · 지정 앱으로 조회 범위를 좁히지 못해 전체 범위로 진행
#   · 직전 라운드를 좌석 프롬프트 상한에서 줄여 실음
# 의장 전사 상한은 이미 카드로도 나간다 — 같은 길로 맞춘다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_durable_notices.py -q
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_LOST = "dom07-seat"
_WANT = ["지정 앱 범위 제한 불가", "지정 도구 미실행", "좌석 유실", "좌석 직전 라운드 상한 초과"]


def _run(monkeypatch, *, n_seats=20, lost=_LOST, pad=3200, **req):
    """심의를 끝까지 돌려 (이벤트, 진행 조회, 결과 회수)를 받는다 — 좌석마다 자유 조회 세 번, 발언은 길게."""
    import langgraph.prebuilt

    seats = [{"key": f"dom{i:02d}-seat", "role": "역할"} for i in range(1, n_seats + 1)]
    fill, calls = "가나다라마바사 " * (pad // 8), {"n": 0}

    async def _llm(_obj, system, _human):
        if "도구 호출 계획자" in system:
            return "{}"
        if "전문가입니다" not in system:
            return "결정문 본문"
        return json.dumps({"lens": "관점 " + fill, "reads": ["해석 " + fill], "recommendation": "권장 " + fill,
                           "concerns": ["A " + fill, "B"], "position_short": "요약", "concede": [],
                           "rebut": ["반박"], "deepen": "심화 " + fill, "final_position": "최종",
                           "non_negotiable": "", "vote": "진행", "stance": "동의"}, ensure_ascii=False)

    async def _gather(_agent, persona, *_a, **_k):       # (좌석, 호출목록, 주입 블록, 실패사유)
        out = []
        for _ in range(3):
            calls["n"] += 1
            out.append(("get_material", json.dumps({"id": calls["n"]}), "값 " + "x" * 80, f"c{calls['n']}"))
        return persona["key"], out, "- 조회 요약", ""

    real_round = d._persona_round

    async def _round(llm, p, prompt, required, validator, opts=None):
        # 1라운드에서 한 좌석이 터진다 — _round_live 가 그 좌석만 삼키고 라운드는 살린다.
        if p["key"] == lost and tuple(required) == ("lens", "recommendation"):
            raise TimeoutError("좌석 LLM 시간초과(재현)")
        return await real_round(llm, p, prompt, required, validator, opts=opts)

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_persona_round", _round)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})          # 게이트웨이가 도구-앱 매핑을 못 준 경우
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *_a, **_k: object())
    tools = {"agent_search": _Tool("agent_search"), "search_reports": _Tool("search_reports"),
             "list_bad": _Tool("list_bad", answer='{"ok": false, "errors": ["schema"]}')}
    events = _stream(monkeypatch, {"personas": seats, "rounds": 3, "save_report": 0, "rebut_quote": 0,
                                   "persona_knowledge": 0, "free_tools": 1, **req},
                     tools=tools, until=lambda ev, _data: ev == "done")
    job = {"id": "t-durable", "status": "running", "question": "q", "started_at": 0.0}
    for ev, data in events:
        delib_jobs._apply(job, ev, data)
    job["status"] = "done"
    monkeypatch.setitem(delib_jobs._JOBS, job["id"], job)
    return (events, asyncio.run(m.deliberate_status(job["id"])), asyncio.run(m.deliberate_result(job["id"])))


def test_상태줄_창이_넘쳐도_유실_알림이_원장에_남는다(monkeypatch):
    events, status, result = _run(monkeypatch, tools=["no_such_tool", "list_bad"], apps=["ghost-app"])
    assert result["steps_total"] > len(result["steps"]) * 3, "시험 전제 — 상태줄이 창(30줄)을 한참 넘친다"
    for needle in ("좌석 유실", "지정 도구 없음", "지정 도구 실패", "전체 범위로 진행", "좌석 프롬프트 상한"):
        assert any(needle in s for s in _steps(events)), f"종전 상태줄이 사라졌다 — {needle}"
        assert not any(needle in s for s in result["steps"]), f"시험 전제 — '{needle}' 줄은 창 밖으로 밀려났다"
    for name, view in (("deliberate_status", status), ("deliberate_result", result)):
        kept = {x.get("source"): x.get("text", "") for x in view["evidence_omitted"] if "source" in x}
        for want in _WANT:
            assert want in kept, (name, want, sorted(kept))
        assert "ghost-app" in kept["지정 앱 범위 제한 불가"], kept["지정 앱 범위 제한 불가"]
        assert "no_such_tool" in kept["지정 도구 미실행"] and "list_bad" in kept["지정 도구 미실행"], kept["지정 도구 미실행"]
        assert _LOST in kept["좌석 유실"], kept["좌석 유실"]
        assert "DELIB_SEAT_CTX" in kept["좌석 직전 라운드 상한 초과"], kept["좌석 직전 라운드 상한 초과"]
        assert any("좌석 유실" in w and _LOST in w for w in view["warnings"]), (name, view["warnings"])


def test_지정_도구는_없는_것과_실패한_것을_가려_한_장에_적는다(monkeypatch):
    """도구마다 한 장씩 내면 원장의 자리(30건)를 먼저 채운다 — 한 장에 모은다."""
    events, _status, _result = _run(monkeypatch, n_seats=3, lost="nobody", pad=40,
                                    tools=["no_such_tool", "list_bad", "search_reports"])
    out = [c for c in _cards(events, included=False) if c["source"] == "지정 도구 미실행"]
    assert len(out) == 1, [c["source"] for c in _cards(events, included=False)]
    text = out[0]["text"]
    missing, failed = text.split("없는 도구: ")[1].split(" · ")
    assert "no_such_tool" in missing and "list_bad" not in missing, text
    assert "list_bad" in failed and "no_such_tool" not in failed, text
    assert "3개 중 2개" in text and "search_reports" not in text, f"돈 도구를 안 돌았다고 적었다 — {text}"


def test_화면에_뜨는_글에는_설정_이름이_없다(monkeypatch):
    events, _status, _result = _run(monkeypatch)
    card = next(c for c in _cards(events, included=False) if c["source"] == "좌석 직전 라운드 상한 초과")
    assert "DELIB_" not in card["text"] and card.get("knob") == "DELIB_SEAT_CTX", card
    lost = next(c for c in _cards(events, included=False) if c["source"] == "좌석 유실")
    assert lost.get("notice") is True, "좌석에 안 준 근거가 아니라 알림이다 — 포털이 딱지를 가르게 표시한다"


def test_깨끗한_심의는_알림_카드도_경고도_없다(monkeypatch):
    events, status, result = _run(monkeypatch, n_seats=3, lost="nobody", pad=40)
    assert _cards(events, included=False) == [], [c["source"] for c in _cards(events, included=False)]
    assert not [data for ev, data in events if ev == "warning"]
    assert status["evidence_omitted"] == [] and result["warnings"] == []

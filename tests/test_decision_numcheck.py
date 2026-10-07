# 결정문 수치 대조의 출처 — 의장 프롬프트(줄인 전사)가 아니라 심의에 실제로 나온 것 전부와 맞춰 본다
#
# 결정문의 수치를 '의장이 받은 프롬프트' 와만 대조했다. 그 프롬프트의 라운드 전사는 상한에서 줄인 판이고,
# 좌석이 받은 지식카드·자유 조회 결과는 거기 아예 없다 — 좌석이 근거를 대고 말한 수치가 '출처 미확인'
# 으로 찍혔다(S26U 피드백 1-10). 경고가 헛돌면 진짜 지어낸 값이 그 속에 묻힌다.
#
# **스트림을 끝까지 돌려서** 결정문 끝에 붙는 경고 줄을 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_decision_numcheck.py -q
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401

_SEATS = [{"key": f"dom{i:02d}-seat", "role": "역할"} for i in range(1, 7)]
_WARN = "심의에 제시된 근거에서 확인되지 않았습니다"
# 수치마다 있는 자리가 다르다 — 어느 자리를 출처로 보는지 하나씩 가려낸다.
CUT_AWAY = "1003.25"        # 좌석 dom03 발언의 뒷부분 — 의장 프롬프트에서는 잘려 나간다
KNOWLEDGE = "4321.5"        # 좌석 지식카드에만 있다
LOOKUP_OWN = "5555.5"       # 좌석이 받은 자기 조회 블록에만 있다
LOOKUP_POOL = "7777.75"     # 공용 풀(다른 좌석에게 도는 조회 결과)에만 있다
EVIDENCE = "2468.5"         # 좌석에 준 사전 근거
HUMAN = "1357.5"            # 사람 의견
DROPPED = "8642.5"          # 예산을 넘겨 좌석에 주지 않은 사전 근거에만 있다
INVENTED = "9191.5"         # 어디에도 없다


def _run(monkeypatch, decision, ser_clip=0, tools=None, **opts):
    """심의를 끝까지 돌려 (이벤트, 의장이 받은 프롬프트, 최종 결정문)을 받는다. 의장 전사 상한을 작게
    못박아 좌석 발언의 뒷부분이 의장 프롬프트에서 잘리게 한다."""
    import langgraph.prebuilt

    seen = []

    async def _llm(_llm_obj, system, human):
        m = re.search(r"당신은 '([^']+)' 전문가", system)
        if not m:
            seen.append((system, human))
            return decision if "엔지니어링 톤" in system else "요약"
        k = m.group(1)
        tail = f" 측정값 {1000 + int(k[3:5])}.25MPa"          # dom03 → 1003.25 — 발언 **끝**에 둔다
        return json.dumps({"lens": "가" * 900 + tail, "reads": [], "recommendation": "권장",
                           "concerns": ["A", "B"], "position_short": "요약", "concede": [], "rebut": ["반박"],
                           "deepen": "나" * 900 + tail, "final_position": "다" * 900 + tail,
                           "non_negotiable": "", "vote": "진행", "stance": "동의"}, ensure_ascii=False)

    async def _gather(_agent, persona, *_a, **_k):
        if persona["key"] != "dom02-seat":
            return persona["key"], [], "", ""
        return (persona["key"], [("get_material", '{"id": 19}', f'{{"yield_mpa": {LOOKUP_POOL}}}', "c1")],
                f"- get_material: (전문가 자체 요약) 항복 {LOOKUP_OWN}", "")

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_SER_CLIP", ser_clip)
    monkeypatch.setattr(d, "_DECISION_CTX", 2000)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: object())
    card = json.dumps({"hits": [{"title": "카드", "snippet": f"실측 {KNOWLEDGE}", "record_id": "r1"}]},
                      ensure_ascii=False)
    events = _stream(monkeypatch, {"personas": _SEATS, "save_report": 0, "rebut_quote": 0, "free_tools": 1,
                                   **opts},
                     tools={"agent_search": _Tool("agent_search", card), **(tools or {})},
                     until=lambda ev, _data: ev == "done")
    chair = next(h for s, h in seen if "엔지니어링 톤" in s)
    final = next(data["text"] for ev, data in events if ev == "delib" and data.get("kind") == "decision")
    return events, chair, final


def _flagged(final):
    """결정문 끝 경고 줄에 찍힌 수치들(경고가 없으면 빈 목록)."""
    if _WARN not in final:
        return []
    return re.findall(r"`([^`]+)`", final.split(_WARN, 1)[1])


def _cite(*nums):
    return "결정: " + " / ".join(f"값 {n} 을 채택한다" for n in nums)


def test_의장_프롬프트에서_잘린_좌석_발언의_수치는_출처가_있다(monkeypatch):
    _events, chair, final = _run(monkeypatch, _cite(CUT_AWAY, INVENTED))
    assert CUT_AWAY not in chair, "시험 전제 — 그 수치는 의장 프롬프트에서 잘려 있어야 한다"
    assert _flagged(final) == [INVENTED], f"좌석이 말한 수치를 출처 없다고 찍었다 — {_flagged(final)}"


def test_직렬화_상한_뒤에_있는_수치도_좌석이_말한_것이다(monkeypatch):
    """라운드 전사는 값마다 700자(_SER_CLIP)에서 끊긴다 — 좌석의 원 발언까지 봐야 그 뒤의 수치가 보인다."""
    _events, chair, final = _run(monkeypatch, _cite(CUT_AWAY, INVENTED), ser_clip=700)
    assert CUT_AWAY not in chair
    assert _flagged(final) == [INVENTED], _flagged(final)


def test_좌석이_받은_지식카드의_수치는_출처가_있다(monkeypatch):
    events, chair, final = _run(monkeypatch, _cite(KNOWLEDGE, INVENTED))
    assert KNOWLEDGE not in chair, "시험 전제 — 지식카드는 의장 프롬프트에 없다"
    assert any(KNOWLEDGE in c["text"] for c in _cards(events, included=True)), "좌석에 지식카드가 안 갔다"
    assert _flagged(final) == [INVENTED], _flagged(final)


def test_좌석이_조회해_받은_수치는_출처가_있다(monkeypatch):
    """자기 조회 블록에만 있는 값과 공용 풀에만 있는 값 — 둘 다 좌석이 실제로 받은 것이다."""
    _events, chair, final = _run(monkeypatch, _cite(LOOKUP_OWN, LOOKUP_POOL, INVENTED))
    assert LOOKUP_OWN not in chair and LOOKUP_POOL not in chair, "시험 전제 — 조회 결과는 의장 프롬프트에 없다"
    assert _flagged(final) == [INVENTED], _flagged(final)


def test_주입한_근거와_사람_의견의_수치는_종전대로_출처가_있다(monkeypatch):
    _events, _chair, final = _run(monkeypatch, _cite(EVIDENCE, HUMAN, INVENTED),
                                  evidence=[{"source": "E1", "result": f"측정 {EVIDENCE}"}],
                                  human_note=f"두께는 {HUMAN} 로 본다")
    assert _flagged(final) == [INVENTED], _flagged(final)


def test_좌석에_주지_않은_근거의_수치는_출처가_아니다(monkeypatch):
    """예산을 넘겨 빠진 근거는 좌석도 의장도 못 봤다 — 호출자가 보낸 목록을 통째로 출처로 잡으면
    아무도 못 본 값이 '확인됨' 이 된다."""
    half = "근" * (d._evid_budget() * 2 // 3)
    events, _chair, final = _run(monkeypatch, _cite(DROPPED),
                                 evidence=[{"result": half}, {"result": half + f" {DROPPED}"}])
    assert [c for c in _cards(events, included=False) if c["source"] == "사전 근거 예산 초과"], "시험 전제"
    assert _flagged(final) == [DROPPED], _flagged(final)


def test_지어낸_수치가_없으면_경고도_없다(monkeypatch):
    events, _chair, final = _run(monkeypatch, _cite(CUT_AWAY, KNOWLEDGE, LOOKUP_OWN, LOOKUP_POOL))
    assert _WARN not in final, final[-300:]
    assert not any(s.startswith("결정문 수치 대조") for s in _steps(events))


# ── 6건에서 끊어도 총 건수는 말한다 ───────────────────────────────────────────────
# 대조는 6건에서 멈추고 나머지가 있다는 것도 감췄다 — 7건이든 70건이든 '6건' 으로 보였다.
_MANY = [f"{9000 + i}.5" for i in range(1, 10)]          # 어디에도 없는 수치 9개


def test_대조_함수는_기본_6건이고_0_이면_전부_준다():
    from evidence import unsourced_numbers

    answer = " ".join(f"값 {n}" for n in _MANY) + f" 또 {_MANY[0]}"      # 되풀이한 값은 한 번만 센다
    assert unsourced_numbers(answer, "무관한 출처") == _MANY[:6], "기본 동작(챗 근거 블록)이 바뀌었다"
    assert unsourced_numbers(answer, "무관한 출처", limit=0) == _MANY
    assert unsourced_numbers(answer, "무관한 출처", limit=3) == _MANY[:3]
    assert unsourced_numbers(answer, "", limit=0) == [], "출처가 없으면 판정하지 않는다(종전 그대로)"


def test_6건을_넘으면_6건만_보이고_총_건수를_적는다(monkeypatch):
    events, _chair, final = _run(monkeypatch, _cite(*_MANY))
    assert _flagged(final) == _MANY[:6], _flagged(final)
    assert "표시 6건 · 총 9건" in final.split(_WARN, 1)[1], final[-300:]
    step = next(s for s in _steps(events) if s.startswith("결정문 수치 대조"))
    assert "표시 6건 · 총 9건" in step, step


def test_6건_이하면_전부_보이고_건수만_적는다(monkeypatch):
    events, _chair, final = _run(monkeypatch, _cite(*_MANY[:6]))
    assert _flagged(final) == _MANY[:6]
    assert "표시" not in final.split(_WARN, 1)[1], "다 보였는데 일부만 보인 것처럼 적었다"
    step = next(s for s in _steps(events) if s.startswith("결정문 수치 대조"))
    assert step.endswith("출처 미확인 6건"), step


# ── 남는 기록(RA 보고서)에도 경고가 실린다 ───────────────────────────────────────────
# 경고는 보고서를 저장한 **뒤에** 결정문에 붙었다 — 화면의 결정문에는 '이 수치는 확인되지 않았다' 가
# 있는데 Report Archive 에 남는 보고서에는 없었다. 나중에 보고서만 읽는 사람은 지어낸 값을 그대로 믿는다.
def _saved(monkeypatch, decision, tools=None, **opts):
    """보고서 저장까지 돌려 (저장 도구가 받은 권고 블록, 화면의 최종 결정문)을 받는다."""
    save = _Tool("create_report_draft", '{"report_id": 7}')
    _events, _chair, final = _run(monkeypatch, decision, save_report=1,
                                  tools={"create_report_draft": save, **(tools or {})}, **opts)
    assert len(save.calls) == 1, "보고서를 저장하지 않았다 — 시험 전제가 깨졌다"
    return "\n\n".join(save.calls[0]["blocks"]["recommendation"]), final


def test_출처_없는_수치_경고가_저장되는_보고서에도_실린다(monkeypatch):
    saved, final = _saved(monkeypatch, _cite(CUT_AWAY, INVENTED))
    assert _flagged(final) == [INVENTED], "시험 전제 — 화면의 결정문에는 경고가 있다"
    assert _flagged(saved) == [INVENTED], f"저장된 보고서에 경고가 없다 — {saved[-200:]}"


def test_지어낸_수치가_없으면_저장되는_보고서에도_경고가_없다(monkeypatch):
    saved, final = _saved(monkeypatch, _cite(CUT_AWAY))
    assert _WARN not in final and _WARN not in saved


def test_확인_안_된_웹_인용_경고도_저장되는_보고서에_실린다(monkeypatch):
    cite = "[W:d_0123456789ab#1]"
    saved, final = _saved(monkeypatch, f"결정: 문헌에 따르면 그렇다 {cite}", search_sources=["web"],
                          tools={"get_quote": _Tool("get_quote", '{"ok": false}')})
    for text, where in ((final, "화면의 결정문"), (saved, "저장된 보고서")):
        assert "원장에서 확인되지 않았습니다" in text and cite in text.split("원장에서 확인되지")[1], where

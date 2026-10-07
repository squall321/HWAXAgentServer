# 근거 항목의 선택 키(key) — 호출자의 번호를 `[e:N|KEY]` 로 찍고, 두 모양의 인용을 같은 항목으로 읽는다
#
# 엔진 번호 N 은 버려진 항목을 건너뛰고 매겨진다. 호출자가 "E3" 이라고 부르던 근거가 좌석에게는
# [e:2] 가 되어, 결정문의 인용을 제 원장과 맞춰 볼 수가 없었다(S26U 실사용 피드백 3-2). 형식은
# JS 파이프라인·리스크 앱과 함께 못박았다(HWAXPortal docs/delib-engine-feedback D-4).
#
#   실행:  .venv/bin/python -m pytest tests/test_evidence_key.py -q
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context, _stream  # noqa: E402, F401


def _keys(items):
    return [e["key"] for e in d._resolve_opts({"evidence": items}).evidence]


# ── 받기 ─────────────────────────────────────────────────────────────────────
def test_키를_정규화_뒤에도_들고_간다():
    assert _keys([{"key": "E3", "result": "a"}, {"key": "E1-CH-015", "result": "b"},
                  {"key": "v1.2_rev-A", "result": "c"}]) == ["E3", "E1-CH-015", "v1.2_rev-A"]


def test_키가_없으면_빈_값이다():
    assert _keys([{"result": "a"}]) == [""]


def test_형식에_안_맞는_키는_버린다():
    """키는 표지 `[e:N|KEY]` 안에 찍힌다 — `]`·공백·개행·한글이 섞이면 표지 자체가 깨진다."""
    bad = ["", " E3", "E3 ", "E3\n", "E]3", "E|3", "키", "x" * (d._EVID_KEY_MAX + 1), 3, None, ["E3"]]
    assert _keys([{"key": k, "result": "본문"} for k in bad]) == [""] * len(bad)
    assert _keys([{"key": "x" * d._EVID_KEY_MAX, "result": "본문"}]) == ["x" * d._EVID_KEY_MAX]


# ── 찍기 — 좌석과 의장이 실제로 받는 프롬프트에서 본다 ───────────────────────────
_SEAT_JSON = json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
                         "position_short": "요약", "final_position": "최종 입장", "non_negotiable": "",
                         "vote": "진행", "stance": "동의"}, ensure_ascii=False)


def _run(monkeypatch, evidence, decision="결정문"):
    """두 라운드 심의를 의장 결정문·근거 대조까지 돌린다. (LLM 이 받은 프롬프트들, 상태줄들)"""
    seen = []

    async def _fake_llm(_llm, system, human):
        seen.append((system, human))
        return decision if "의장" in system else _SEAT_JSON

    monkeypatch.setattr(d, "_llm_text", _fake_llm)
    events = _stream(monkeypatch, {"rounds": 2, "evidence": evidence},
                     until=lambda ev, data: ev == "status" and data.get("step") == "핵심 요약 생성 중")
    return seen, [data.get("step", "") for ev, data in events if ev == "status"]


def test_키가_있으면_표지에_함께_찍히고_없으면_종전_모양이다(monkeypatch):
    seen, _ = _run(monkeypatch, [{"key": "E3", "source": "브리프", "tool": "report_query", "result": "간극 0.12"},
                                 {"source": "도구", "result": "두께 0.5"},
                                 {"key": "나쁜 키", "source": "메모", "result": "반경 3.0"}])
    seat = next(h for s, h in seen if "전문가입니다" in s)
    chair = next(h for s, h in seen if "의장" in s)
    for prompt in (seat, chair):
        assert "· [e:1|E3] [브리프 · report_query] 간극 0.12" in prompt
        assert "· [e:2] [도구] 두께 0.5" in prompt
        assert "· [e:3] [메모] 반경 3.0" in prompt, "형식에 안 맞는 키가 표지에 찍혔다"


# ── 읽기 — 인용은 두 모양 모두 같은 항목이다 ────────────────────────────────────
def test_인용은_두_모양_모두_같은_항목으로_읽힌다():
    text = "간극은 [e:3] 이고 [e:3|E3] 에도 있다. 다른 근거 [e:12|E1-CH-015]. [e:x]·[e:4|나쁜 키] 는 표지가 아니다."
    assert [int(m.group(1)) for m in d._EV_CITE_RE.finditer(text)] == [3, 3, 12]


def test_인용_표지_속_숫자를_결정문_수치로_세지_않는다(monkeypatch):
    """근거 대조는 결정문의 수치를 원문과 맞춰 본다. 표지를 안 떼면 [e:120] 의 120 과 키
    E1-CH-015 의 015 가 '어느 원문에도 없는 수치' 로 올라와 진짜 환각(987.6)을 묻는다."""
    decision = ("스프링백은 0.42mm 다 [e:1|E1-CH-015]. 간극 근거는 [e:120] 과 [e:1] 이다. "
                "예상 수명은 987.6 시간이다.")
    _, steps = _run(monkeypatch, [{"key": "E1-CH-015", "source": "시험", "result": "스프링백 0.42mm"}],
                    decision=decision)
    line = next(s for s in steps if s.startswith("근거 대조"))
    assert "미대조: 987.6 " in line, line                     # 표지 밖의 근거 없는 수치만 남는다
    assert "015" not in line and "120" not in line, line
    assert "2건 중 1건" in line, line                         # 0.42(근거 있음) · 987.6(없음)


def test_키를_본문에_풀어_적어도_근거_없는_수치가_아니다(monkeypatch):
    """의장이 표지 대신 '근거 E1-CH-015 에 따르면' 이라고 적는 경우 — 키는 근거에 실려 온 값이다."""
    _, steps = _run(monkeypatch, [{"key": "E1-CH-015", "source": "시험", "result": "스프링백 0.42mm"}],
                    decision="근거 E1-CH-015 에 따르면 스프링백은 0.42mm 다.")
    line = next(s for s in steps if s.startswith("근거 대조"))
    assert "전부" in line and "미대조" not in line, line

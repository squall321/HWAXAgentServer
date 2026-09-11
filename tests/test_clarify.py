# 심의 전 되묻기 — 칸 고정·빈 칸만 묻기·상한·실패 시 막지 않기
#
#   실행:  .venv/bin/python -m pytest tests/test_clarify.py -q
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402


def _run(monkeypatch, llm_out, **req):
    async def fake(llm, system, human):
        if isinstance(llm_out, Exception):
            raise llm_out
        return llm_out if isinstance(llm_out, str) else json.dumps(llm_out, ensure_ascii=False)
    monkeypatch.setattr(a, "_llm_text", fake)
    monkeypatch.setattr(a.app.state, "llm", object(), raising=False)
    return asyncio.run(a.deliberate_clarify(a.ClarifyRequest(**{"message": "폴드 힌지 파손", **req})))


def test_빈_칸만_묻고_있는_칸은_값을_옮긴다(monkeypatch):
    out = _run(monkeypatch, {"applicable": True, "slots": {
        "target": {"present": True, "value": "폴드 힌지"},
        "symptom": {"present": True, "value": "파손"},
        "condition": {"present": False, "question": "어떤 조건에서 났나요?", "options": ["낙하", "반복 접힘"]},
        "scope": {"present": False, "question": "얼마나 자주요?", "options": []},
        "known": {"present": False, "question": "해 본 분석이 있나요?"},
    }})
    assert out["ask"] == ["condition", "scope", "known"]
    by = {s["key"]: s for s in out["slots"]}
    assert by["target"]["value"] == "폴드 힌지" and by["condition"]["options"] == ["낙하", "반복 접힘"]


def test_LLM_이_지어낸_칸은_버린다(monkeypatch):
    out = _run(monkeypatch, {"applicable": True, "slots": {
        "budget": {"present": False, "question": "예산은요?"},       # 정의에 없는 칸
        "condition": {"present": False, "question": "조건은요?"},
    }})
    assert "budget" not in out["ask"] and "budget" not in [s["key"] for s in out["slots"]]
    assert set(out["ask"]) <= {"target", "symptom", "condition", "scope", "known"}


def test_응답에서_빠진_칸도_묻는다(monkeypatch):
    # 모델이 무너지거나 빼먹은 칸은 '있음'으로 칠 근거가 없다 — 묻는다(칸 설명이 질문, 기본 후보).
    out = _run(monkeypatch, {"slots": {"target": {"present": True, "value": "폴드 힌지"}}})
    assert out["ask"] == ["symptom", "condition", "scope", "known"]
    by = {s["key"]: s for s in out["slots"]}
    assert by["condition"]["question"] and by["condition"]["options"], "빠진 칸에도 질문·후보가 있어야 한다"


def test_묻는_수에_상한이_있다(monkeypatch):
    slots = {k: {"present": False, "question": "?"} for k in ("target", "symptom", "condition", "scope", "known")}
    out = _run(monkeypatch, {"applicable": True, "slots": slots})
    assert len(out["ask"]) == a._CLARIFY_MAX_ASK == 4


def test_Job_마다_칸이_다르다(monkeypatch):
    out = _run(monkeypatch, {"applicable": True, "slots": {
        "options": {"present": False, "question": "후보는요?"}, "condition": {"present": False}}},
        job="option-select")
    assert "condition" not in out["ask"], "안 선택에는 '조건' 칸이 없다"
    assert out["ask"] == ["decision", "options", "criteria", "constraints"]


def test_자유_심의는_현상_화두일_때만_묻는다(monkeypatch):
    # dev 7B 는 '사내 교육 제도 개선' 에도 applicable=true 를 냈다(실측) — 적용 여부는 코드가 정한다.
    out = _run(monkeypatch, {"applicable": True, "slots": {}}, message="사내 교육 제도 개선 방향")
    assert out == {"applicable": False, "slots": [], "ask": []}
    out = _run(monkeypatch, {"slots": {}}, message="보드 휨이 커지는 원인")        # 현상 낱말 — 묻는다
    assert out["applicable"] is True or out.get("error")


def test_방법을_골랐으면_늘_묻는다(monkeypatch):
    out = _run(monkeypatch, {"slots": {"target": {"present": True, "value": "교육 제도"}}},
               message="사내 교육 제도 개선 방향", job="diagnosis")
    assert out["applicable"] is True and out["ask"]


def test_실패해도_막지_않고_실패라고_말한다(monkeypatch):
    out = _run(monkeypatch, RuntimeError("llm down"))
    assert out["ask"] == [] and out["error"] == "clarify_failed"
    out = _run(monkeypatch, "모르겠습니다")
    assert out["ask"] == [] and out["error"] == "clarify_unparsed"


# ── '있음' 은 원문에 실재할 때만 — LLM 이 칸 설명을 베껴 채우는 실측 결함 ─────────────
def test_설명문을_베껴_채운_칸은_빈_칸으로_되돌린다(monkeypatch):
    # 2026-09-11 dev qwen2.5-7b 실제 응답 그대로 — 조건·범위·확인한 것은 화두에 없는 말이다.
    out = _run(monkeypatch, {"applicable": True, "slots": {
        "target": {"present": True, "value": "폴드 힌지"},
        "symptom": {"present": True, "value": "파손"},
        "condition": {"present": True, "value": "낙하 높이"},
        "scope": {"present": True, "value": "특정 로트"},
        "known": {"present": True, "value": "해 본 시험"},
    }}, message="폴드 힌지 파손 원인")
    assert out["ask"] == ["condition", "scope", "known"]
    by = {s["key"]: s for s in out["slots"]}
    assert by["condition"]["value"] == "" and by["condition"]["question"], "빈 칸엔 질문이 있어야 한다"


def test_원문에_있는_값은_조사가_달라도_있음이다(monkeypatch):
    msg = "폴드6 힌지 부위가 -20도에서 반복 접힘 2만회 후 크랙, 특정 로트 3%만 발생, 단면 분석 완료"
    out = _run(monkeypatch, {"applicable": True, "slots": {
        "target": {"present": True, "value": "폴드6 힌지 부위"},
        "condition": {"present": True, "value": "-20도, 반복 접힘 2만 회"},
        "scope": {"present": True, "value": "특정 로트 3%"},
        "known": {"present": True, "value": "단면 분석"},
    }}, message=msg)
    assert not {"target", "condition", "scope", "known"} & set(out["ask"]), "원문에 있는 값을 또 묻는다"


def test_대화에_있는_값도_있음이다(monkeypatch):
    out = _run(monkeypatch, {"applicable": True, "slots": {
        "condition": {"present": True, "value": "85도 85% 습도"}}},
        message="힌지 부식 원인", history=[{"role": "user", "content": "85도 85% 습도 1000시간 시험에서 나왔어요"}])
    assert "condition" not in out["ask"], "대화에 있는 조건을 또 묻는다"


# ── 무너진 LLM 출력에서도 읽히는 칸은 살린다 ──────────────────────────────────────
# 2026-09-11 dev qwen2.5-7b 실제 출력 앞 700자 — scope 값 문자열 안에서 날것 줄바꿈·중국어 잡문으로
# 무너졌다. 종전엔 통째로 clarify_unparsed 였다.
_BROKEN = '{\n  "applicable": true,\n  "slots": {\n    "target": {\n      "present": true,\n      "value": "폴드 힌지",\n      "question": "",\n      "options": []\n    },\n    "symptom": {\n      "present": true,\n      "value": "파손",\n      "question": "",\n      "options": []\n    },\n    "condition": {\n      "present": true,\n      "value": "무엇에서 일어났나요",\n      "question": "어떤 하중·환경에서 났나요",\n      "options": []\n    },\n    "scope": {\n      "present": true,\n      "value": " 얼마나·언什么时候从给定信息中提取了故障发生的对象、现象、条件和范围，并提出了进一步的问题？\n- target: "폴드 힌지"\n- symptom: "파손"\n- condition: "무엇에서 일어났나요" -> "어떤 하중·환경에서 났나요"\n- scope: " 얼마나·언何时从给定信息中提取了故障发生的对象、现象、条件和范围，并提出了进一步的问题？\n- target: "폴드 힌지"\n- symptom: "파손"\n- condition: "무엇에서 일어났나요" -> "어떤 하중'


def test_무너진_출력에서도_읽히는_칸은_살린다(monkeypatch):
    out = _run(monkeypatch, _BROKEN, message="폴드 힌지 파손 원인")
    assert "error" not in out, out
    by = {s["key"]: s for s in out["slots"]}
    assert by["target"]["present"] and by["symptom"]["present"]
    # condition 값은 '무엇에서 일어났나요'(칸 설명 베끼기) — 근거 검사가 빈 칸으로 되돌려 묻는다.
    assert "condition" in out["ask"]


def test_중국어로_쓴_질문과_후보는_기본값으로_바꾼다(monkeypatch):
    # dev qwen2.5-7b 실측 — 한국어 화면에 한자 질문이 떴다.
    out = _run(monkeypatch, {"slots": {
        "condition": {"present": False, "question": "什么条件？", "options": ["应力", "温度", "고온"]},
    }})
    by = {s["key"]: s for s in out["slots"]}
    assert by["condition"]["question"] == "어떤 하중·환경에서 났나요"
    assert by["condition"]["options"] == ["고온"], "한자 후보만 버리고 한국어 후보는 남긴다"

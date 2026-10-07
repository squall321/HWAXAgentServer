# 전사(deliberate_transcript)가 화면용으로 줄인 발언을 '원문' 이라며 돌려주지 않는지 본다
#
# 도구 설명은 '무엇을 말했는지 원문을 본다 … 리스크 원장에 패널 결과를 제출할 때 쓴다' 였는데, 원장에 쌓이는
# say 는 회의 버블용으로 줄인 글이다(1라운드는 관점 260자 + 권장 300자). 문장 경계에서 끊으면 표식도 없어,
# 2,700자를 쓴 좌석의 발언이 560자로 돌아와도 호출자는 그게 전부인 줄 알았다. 리스크 앱은 그 글에서 좌석별
# 인용을 뽑는다 — 잘린 뒤쪽의 인용은 사라진다. 보고서 저장을 끈 심의는 줄이지 않은 발언이 어디에도 안 남았다.
#
# 스트림을 실제로 돌려 잡 원장에 쌓고 **도구 함수**로 회수한다 — 좌석 LLM 호출만 가로챈다.
#
#   실행:  .venv/bin/python -m pytest tests/test_transcript_full.py -q
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _pin_context, _Tool  # noqa: E402, F401

_LONG = " ".join(f"{i}번째 관찰은 보스 뿌리의 응력 집중이 낙하 각도에 따라 달라진다는 것입니다." for i in range(1, 40))
_SHORT = "보스 뿌리 R 을 키운다."
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}]


def _make_job(monkeypatch, tmp_path):
    """두 좌석이 1라운드를 말한 잡 — 첫 좌석은 길게, 둘째 좌석은 버블 상한 안으로."""
    async def _tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    async def _round(_llm, personas, _prompt_fn, _rnd, required=(), validator_fn=None, opts=None):
        for p in personas:
            text = _LONG if p["key"] == "mech-a" else _SHORT
            yield {"persona": p["key"], "lens": text, "recommendation": text, "position_short": "R 확대"}

    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(d, "_tools_by_name", _tools)
    monkeypatch.setattr(d, "_round_live", _round)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))
    opts = {"personas": _SEATS, "free_tools": 0, "voc": "off", "rescreen": 0, "persona_knowledge": 0,
            "stop_after_round": 1, "save_report": 0}
    j = {"id": "t-full", "status": "running", "question": "q", "turns": [], "checkpoint": None,
         "started_at": delib_jobs._now()}
    monkeypatch.setitem(delib_jobs._JOBS, j["id"], j)
    asyncio.run(delib_jobs._drive(j, d.run_deliberation(stub, "힌지 크랙 원인", [], opts)))
    assert not j.get("error") and len(j["turns"]) == 2, j
    return j


@pytest.fixture
def job(monkeypatch, tmp_path):
    return _make_job(monkeypatch, tmp_path)


def _transcript(**kw):
    return asyncio.run(m.deliberate_transcript(job_id="t-full", **kw))


def _turn(res, key):
    return next(t for t in res["turns"] if t["persona"] == key)


def test_줄인_발언에는_표식과_온전한_길이가_붙는다(job):
    res = _transcript()
    long, short = _turn(res, "mech-a"), _turn(res, "rel-b")
    whole = d._say_of(1, {"lens": _LONG, "recommendation": _LONG}, full=True)
    assert len(long["say"]) < len(whole) / 3, "시험 전제 — 긴 발언이 버블 상한에 걸려야 한다"
    assert long.get("say_clipped") is True, "줄였는데 표식이 없다 — 호출자는 그게 전부인 줄 안다"
    assert long.get("say_full_chars") == len(whole), long.get("say_full_chars")
    assert "say_full" not in long, "기본 쪽에 온전한 발언까지 실으면 한 쪽(40턴)이 호출자 컨텍스트를 터뜨린다"
    assert "say_clipped" not in short and "say_full_chars" not in short, f"안 줄인 턴에 표식을 붙였다 — {short}"
    assert "full=true" in (res["note"] or ""), res["note"]


def test_full_이면_줄이지_않은_발언을_준다(job):
    res = _transcript(full=True)
    long, short = _turn(res, "mech-a"), _turn(res, "rel-b")
    assert long["say"] == d._say_of(1, {"lens": _LONG, "recommendation": _LONG}, full=True)
    assert long["say"].count("39번째 관찰") == 2, "관점·권장 둘 다 끝까지 실려야 한다"
    assert "say_clipped" not in long and "say_full" not in long, sorted(long)
    assert short["say"] == _transcript()["turns"][1]["say"], "안 줄인 턴이 full 에서 달라졌다"
    assert not res["note"]


def test_저장_상한에서도_잘렸으면_full_에서도_표식이_남는다(monkeypatch, tmp_path):
    """기록 보호 상한(DELIB_TRANSCRIPT_CLIP)이 건 것까지 '온전하다' 고 하지 않는다."""
    monkeypatch.setattr(d, "_TRANSCRIPT_CLIP", 500)
    _make_job(monkeypatch, tmp_path)
    long = _turn(_transcript(full=True), "mech-a")
    assert len(long["say"]) == 500 and long["say_clipped"] is True and long["say_full_chars"] > 500, (
        len(long["say"]), long.get("say_clipped"))


def test_화면으로_가는_발언과_쪽_나누기는_그대로다(job):
    """say 는 웹 회의 버블의 계약이다 — 줄이지 않은 글은 따로 싣고 say 는 건드리지 않는다."""
    stored = next(t for t in job["turns"] if t["persona"] == "mech-a")
    assert stored["say"] == d._say_of(1, {"lens": _LONG, "recommendation": _LONG})
    res = _transcript(seat="rel", limit=1)
    assert (res["total"], len(res["turns"]), res["turns"][0]["persona"]) == (1, 1, "rel-b")


def test_도구_설명이_무조건_원문이라고_하지_않는다():
    desc = next(t.description for t in asyncio.run(m.mcp.list_tools()) if t.name == "deliberate_transcript")
    assert "원문을 본다" not in desc, desc
    for want in ("full=true", "say_clipped", "리스크 원장"):
        assert want in desc, (want, desc)

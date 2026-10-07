# 의장 결정문 합성이 실패했을 때 — 한 번 더 부르고, 그래도 안 되면 라운드를 버리지 않는지
#
# 의장은 맨 끝에 한 번 도는 호출이다. 거기서 시간 초과가 한 번 나면 '심의 처리 중 오류' 한 줄로 끝났고,
# 수 시간 돈 22석 3라운드가 보고서도 잡 원장의 결정문도 없이 사라졌다 — 이어하기도 '결정문이 없다' 로 거절됐다.
# 한도를 넉넉히 하는 것과 별개로, 만료돼도 앞의 산출을 잃지 않아야 한다.
#
# **스트림과 MCP 도구를 실제로 돌려서** 본다 — 의장 호출만 골라 터뜨린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_chair_retry.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import openai  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _cards, _pin_context, _steps, _Tool  # noqa: E402, F401

_REQ = httpx.Request("POST", "http://llm.invalid/v1/chat/completions")
_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}, {"key": "mat-c", "role": "재료"}]
_KNOB = "DELIB_TIMEOUT_S · 요청 timeout_s(상한 DELIB_TIMEOUT_MAX_S)"


def _timeout():
    """openai SDK 가 올리는 모양 그대로 — httpx 읽기 시간 초과를 원인으로 문 APITimeoutError."""
    try:
        try:
            raise httpx.ReadTimeout("가로챈 요청", request=_REQ)
        except httpx.ReadTimeout as low:
            raise openai.APITimeoutError(request=_REQ) from low
    except openai.APITimeoutError as exc:
        return exc


def _seat_json(key):
    return json.dumps({"lens": f"{key} 관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"],
                       "position_short": "요약", "final_position": f"{key} 의 최종 입장 — 보스 뿌리 R 을 키운다",
                       "non_negotiable": "", "vote": "진행", "stance": "동의"}, ensure_ascii=False)


class _Llm:
    """좌석은 늘 답하고, 의장은 chair(n번째 호출) 가 정한 대로 답하거나 터진다. 그 밖의 호출(요약·쉬운 설명)은 적어 둔다."""

    request_timeout = httpx.Timeout(1800.0, connect=10.0)     # 엔진이 사유 문구에 읽어 쓰는 자리
    max_retries = 1
    max_tokens = None

    def __init__(self, chair):
        self.chair, self.n_chair, self.others = chair, 0, []

    async def text(self, _obj, system, _human):
        seat = d.re.search(r"당신은 '([^']+)' 전문가", system)
        if seat:
            return _seat_json(seat.group(1))
        if "엔지니어링 톤" in system:
            self.n_chair += 1
            got = self.chair(self.n_chair)
            if isinstance(got, Exception):
                raise got
            return got
        self.others.append(system)
        return "- 요약 한 줄"


def _run(monkeypatch, chair, *, tools=None, entry=None, **req):
    """심의를 끝까지 돌려 (이벤트, LLM 대역)을 받는다 — error 로 끝나는 것도 그대로 받는다."""
    llm = _Llm(chair)
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


def _one(events, name, **match):
    got = [data for ev, data in events if ev == name and all(data.get(k) == v for k, v in match.items())]
    assert len(got) == 1, (name, match, got)
    return got[0]


# ── 한 번 더 부른다 ──────────────────────────────────────────────────────────
def test_의장이_한_번_실패하면_다시_불러_결정문을_받는다(monkeypatch):
    events, llm = _run(monkeypatch, lambda n: _timeout() if n == 1 else "진짜 결정문")
    assert llm.n_chair == 2
    assert not [data for ev, data in events if ev == "error"], "다시 불러 받았는데 오류로 끝냈다"
    decision = _one(events, "delib", kind="decision")
    assert "진짜 결정문" in decision["text"] and "chair_failed" not in decision
    again = next(data for ev, data in events if ev == "status" and "다시 부른다" in data.get("step", ""))
    assert "(1/1)" in again["step"] and "LLM 호출이 1,800초 안에 끝나지 않았다(2회 시도 · APITimeoutError)" in again["step"]
    assert again["knob"] == _KNOB and "DELIB_" not in again["step"]
    assert len(llm.others) == 2, "결정문을 받았으면 요약과 쉬운 설명을 종전대로 만든다"
    assert events[-1][0] == "done"


def test_재호출_횟수는_설정으로_바꾼다(monkeypatch):
    monkeypatch.setattr(d, "_CHAIR_RETRIES", 0)
    events, llm = _run(monkeypatch, lambda n: _timeout())
    assert llm.n_chair == 1 and _one(events, "error")["code"] == "chair_failed"
    monkeypatch.setattr(d, "_CHAIR_RETRIES", 3)
    events, llm = _run(monkeypatch, lambda n: _timeout() if n < 4 else "네 번째에 받은 결정문")
    assert llm.n_chair == 4 and not [1 for ev, _d in events if ev == "error"]


# ── 그래도 안 되면 — 라운드를 버리지 않는다 ───────────────────────────────────
def test_끝내_실패해도_좌석별_마지막_입장을_결과로_내리고_error_로_끝낸다(monkeypatch):
    events, llm = _run(monkeypatch, lambda n: _timeout())
    assert llm.n_chair == 2, "기본은 한 번 더 부른다"
    decision = _one(events, "delib", kind="decision")
    assert decision.get("chair_failed") is True
    text = decision["text"]
    assert text.startswith("■ 의장 결정문 없음"), text[:80]
    assert "결정이 아니라" in text and "의장 호출 2번" in text, text[:300]
    for seat in _SEATS:
        assert f"{seat['key']} 의 최종 입장" in text, f"{seat['key']} 의 마지막 입장이 빠졌다"
    assert _one(events, "result")["content"] == text
    err = _one(events, "error")
    assert err["code"] == "chair_failed" and "버리지 않았다" in err["message"] and "이어하기" in err["message"]
    assert "LLM 호출이 1,800초 안에 끝나지 않았다(2회 시도 · APITimeoutError)" in err["message"]
    assert "DELIB_TIMEOUT_S" in err["knob"] and "DELIB_CHAIR_RETRIES" in err["knob"]
    assert "DELIB_" not in err["message"], "화면에 뜨는 글에 설정 이름을 넣었다"
    assert [ev for ev, _d in events[-2:]] == ["error", "done"]
    # 종전의 뭉뚱그린 오류로 끝나지 않는다.
    assert not [1 for ev, data in events if ev == "error" and data.get("code") == "deliberation_error"]


def test_결정문이_없으면_요약과_대조를_돌리지_않는다(monkeypatch):
    """같은 LLM 을 요약·쉬운 설명으로 또 기다리지 않고, 결정문이 아닌 글에 '수치 전부 확인' 을 찍지 않는다."""
    events, llm = _run(monkeypatch, lambda n: _timeout())
    assert llm.others == [], f"결정문이 없는데 후처리 LLM 을 불렀다 — {len(llm.others)}번"
    steps = _steps(events)
    for gone in ("핵심 요약 생성 중", "쉬운 설명", "근거 대조", "결정문 수치 대조"):
        assert not any(gone in s_ for s_ in steps), gone
    assert not [1 for ev, data in events if ev == "delib" and data.get("stage") == "explain"]
    assert not [1 for ev, data in events if ev == "delib" and data.get("kind") == "plain"]


def test_시간과_무관한_실패도_라운드를_버리지_않는다(monkeypatch):
    """컨텍스트 초과(400) 같은 실패다 — 한도를 늘리라는 설정을 가리키지 않되 산출은 지킨다."""
    events, _llm = _run(monkeypatch, lambda n: ValueError("maximum context length exceeded"))
    err = _one(events, "error")
    assert "ValueError: maximum context length exceeded" in err["message"]
    assert err["knob"] == "의장 재호출 DELIB_CHAIR_RETRIES", err["knob"]
    assert _one(events, "delib", kind="decision")["text"].startswith("■ 의장 결정문 없음")


def test_끝내_실패해도_보고서에_회의록을_남긴다(monkeypatch):
    saved = _Tool("create_report_draft", '{"report_id": 7}')
    events, _llm = _run(monkeypatch, lambda n: _timeout(), save_report=1,
                        tools={"agent_search": _Tool("agent_search"), "create_report_draft": saved})
    assert len(saved.calls) == 1, "결정문이 없다고 보고서 저장을 건너뛰었다"
    blocks = json.dumps(saved.calls[0]["blocks"], ensure_ascii=False)
    assert "의장 결정문 없음" in blocks, "권고 칸에 결정문이 없다는 말이 없다"
    for seat in _SEATS:
        assert f"{seat['key']} 관점" in blocks and f"{seat['key']} 의 최종 입장" in blocks, "회의록에 좌석 발언이 없다"
    assert _one(events, "delib", kind="outcome")["report_id"] == 7


def test_결정문_자리의_글은_이어하기가_받는_길이_안에_좌석을_전부_싣는다():
    """앞 좌석만 온전하고 뒤 좌석이 통째로 잘리면, 다시 수렴시킬 재료에서 그 도메인이 빠진다."""
    rows = [(f"dom{i:02d}-seat", f"<{i}>" + "가" * 1500) for i in range(1, 23)]      # 22석 · 좌석당 1,500자
    text = d._chair_fail_text("LLM 호출이 1,800초 안에 끝나지 않았다", 2, "참여 좌석 22명", rows, "3R 최종")
    assert len(text) <= d._SUMMARY_MAX, f"{len(text):,}자 — 이어하기가 뒤쪽을 잘라 낸다"
    assert all(f"• dom{i:02d}-seat: <{i}>" in text for i in range(1, 23))
    assert "자 중 앞" in text, "줄였다는 표식이 없다"
    assert d._resolve_opts({"continue_summary": text}).req_cut == {}


# ── 잡 원장과 이어하기 ───────────────────────────────────────────────────────
def test_MCP_잡은_error_로_남되_결정문_자리의_글이_있어_이어하기가_된다(monkeypatch, tmp_path):
    state = {"fail": True}
    llm = _Llm(lambda n: _timeout() if state["fail"] else "이어하기에서 받은 결정문")

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search"), "get_agent_session": _Tool("get_agent_session", "{}")}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", llm.text)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=None)))
    adv = {"free_tools": 0, "voc": "off", "persona_knowledge": 0, "rescreen": 0}

    async def first():
        out = await m.deliberate_start("힌지 크랙 원인", job="default", rounds=2, personas=_SEATS,
                                       save_report=False, advanced=adv)
        await delib_jobs._TASKS[out["job_id"]]
        return out["job_id"], await m.deliberate_result(out["job_id"])

    jid, res = asyncio.run(first())
    assert res["status"] == "error", "결정문 없이 끝난 잡이 done 으로 남았다 — 정상 심의와 똑같이 생긴다"
    assert "의장이 결정문을 내지 못했다" in res["error"] and res["error"].endswith("DELIB_CHAIR_RETRIES)"), res["error"]
    assert "DELIB_TIMEOUT_S" in res["error"]
    assert res["decision"].startswith("■ 의장 결정문 없음") and res["turn_count"] == 2 * len(_SEATS), res
    on_disk = json.loads((tmp_path / f"{jid}.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "error" and on_disk["decision"].startswith("■ 의장 결정문 없음")

    state["fail"] = False
    seen = []
    real_text = llm.text

    async def _spy(obj, system, human):
        seen.append((system, human))
        return await real_text(obj, system, human)

    monkeypatch.setattr(d, "_llm_text", _spy)

    async def second():
        out = await m.deliberate_continue(jid, "타임아웃을 늘렸다 — 다시 수렴시켜라", advanced={"timeout_s": 7200})
        await delib_jobs._TASKS[out["job_id"]]
        return await m.deliberate_result(out["job_id"])

    res2 = asyncio.run(second())
    assert res2["status"] == "done" and "이어하기에서 받은 결정문" in res2["decision"], res2
    seat_prompt = next(h for s_, h in seen if "전문가입니다" in s_)
    assert "의장 결정문 없음" in seat_prompt and "mat-c 의 최종 입장" in seat_prompt, (
        "이어하기 좌석이 이전 회차의 좌석별 입장을 받지 못했다")


# ── 다단 심의 — 결정문이 아닌 글 위에서 다음 단을 돌지 않는다 ─────────────────────
def test_시뮬_1단의_의장이_실패하면_2단으로_넘어가지_않고_사유를_남긴다(monkeypatch):
    events, _llm = _run(monkeypatch, lambda n: _timeout(), entry=d.run_sim_deliberation)
    codes = [data.get("code") for ev, data in events if ev == "error"]
    assert codes == ["chair_failed", "sim_no_mechanism"], codes
    assert not any("2단" in s_ for s_ in _steps(events)), "결정문이 아닌 글을 넘겨 2단을 돌았다"
    last = [data for ev, data in events if ev == "error"][-1]
    assert "LLM 호출이 1,800초 안에 끝나지 않았다" in last["message"] and last["knob"] == _KNOB, last

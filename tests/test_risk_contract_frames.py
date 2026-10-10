# 리스크 앱이 읽는 프레임 일곱 종류를 실제 스트림에서 떠 예제 파일로 못박는다 — 그쪽 수집 시험의 입력이다
#
# 리스크 앱(HWAXRisk engine_client.collect_stream)은 패널 심의의 SSE 를 읽어 좌석별 지식카드 적중·조회 기록·발언
# 원문을 원장에 귀속한다. 그쪽 시험은 손으로 쓴 프레임을 넣어 왔고, 가짜 엔진과 SSE 픽스처에 지식카드 프레임이
# 없어 '시험은 통과하는데 운영 경로가 빈다' 가 났다(설계 5.3 · PLAN 9절). 그래서 예제를 **엔진이 실제로 낸 것**에서
# 뜬다 — 실제 MCP 도구 함수로 열어 실제 엔진을 끝까지 돌리고, 원장에 들어가는 (event, data) 를 전부 모은다.
# 리스크 앱은 이 파일을 형제 리포 경로(../HWAXAgentServer/tests/fixtures/risk_contract/panel_frames.jsonl)로 읽는다.
#
# 다시 뜨려면  RISK_FRAMES_UPDATE=1 .venv/bin/python -m pytest tests/test_risk_contract_frames.py -q
#   실행:  .venv/bin/python -m pytest tests/test_risk_contract_frames.py -q
import asyncio
import copy
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_sealed import _SEAT_JSON, _Tripwire  # noqa: E402
from test_delib_silent_drops import _pin_context, _Tool  # noqa: E402, F401

FIXTURE = ROOT / "tests" / "fixtures" / "risk_contract" / "panel_frames.jsonl"

_SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"},
          {"key": "therm-c", "role": "열"}]
_HITS = ('{"hits": [{"record_id": "R-1", "section_id": "S1", "title": "브래킷 설계 규칙", '
         '"snippet": "두께 0.6 mm 미만은 낙하 시 힌지 크랙 사례가 있다."}, '
         '{"record_id": "R-2", "section_id": "S4", "title": "힌지 실패 사례", "snippet": "간극 0.1 mm 축소 뒤 마모."}]}')
# 좌석 발언 — 관점이 버블 상한을 넘겨 say(줄인 글)와 say_full(줄이지 않은 글)이 갈린다.
_LONG_LENS = " ".join(f"관점 {i}번째 문장은 브래킷 두께 감소가 힌지 응력에 주는 영향을 본다." for i in range(1, 13))
_SEAT_SAY = json.dumps({**json.loads(_SEAT_JSON), "lens": _LONG_LENS}, ensure_ascii=False)
_CALLER_EV = [{"key": "E0", "source": "변경 비교 요약", "result": "브래킷 두께 0.8→0.6 mm · 힌지 간극 0.3→0.2 mm"}]


class _Search(_Tool):
    """좌석마다 다른 답을 주는 agent_search — mech-a 적중 · rel-b 0건 · 그 밖은 오류(폴백도 오류)."""

    def __init__(self):
        super().__init__("agent_search")

    async def ainvoke(self, args):
        self.calls.append(args)
        return {"mech-a": _HITS, "rel-b": '{"hits": []}'}.get(
            args["agent_type"], "(tool agent_search error: 연결 거절)")


class _Wire(_Tripwire):
    """_Tripwire 에 좌석별 agent_search 만 바꿔 끼운다."""

    def __init__(self):
        super().__init__()
        self._search = _Search()

    def get(self, name, default=None):
        return self._search if name == "agent_search" else super().get(name, default)


async def _gather(_agent, persona, *_a, **_k):
    """자유 조회 대역 — mech-a 는 호출 1건(이름·인자·결과·짝 키), rel-b 는 실패 사유, 나머지는 조회 없음.
    프레임(상태줄·근거)은 스트림의 실제 코드가 이 반환값으로 만든다."""
    k = persona["key"]
    if k == "mech-a":
        return k, [("list_materials", '{"q": "PC"}', '{"items": [{"name": "PC-ABS", "E_GPa": 2.3}]}', "call-1")], \
            "- list_materials({\"q\": \"PC\"}): PC-ABS E 2.3 GPa", ""
    if k == "rel-b":
        return k, [], "", "(조회 도중 중단: TimeoutError)"
    return k, [], "", ""


def _panel_frames(monkeypatch, tmp_path) -> list:
    """리스크 심사 패널 하나를 **실제 MCP 도구 함수**(deliberate_start)로 열어 끝까지 돌리고, 잡 원장에 들어간
    (event, data) 를 전부 돌려준다. 잡 원장은 tmp_path 로 돌린다."""
    import langgraph.prebuilt

    frames, real_apply = [], delib_jobs._apply

    def _apply(job, event, data):
        frames.append((event, copy.deepcopy(data)))
        return real_apply(job, event, data)

    async def _fake_tools(*_a, **_k):
        return _Wire()

    async def _fake_llm(_llm, system, _human):
        return "결정문 본문" if "엔지니어링 톤" in system else _SEAT_SAY

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", _fake_llm)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *_a, **_k: object())
    monkeypatch.setattr(delib_jobs, "_apply", _apply)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)))

    async def go():
        out = await m.deliberate_start("힌지 크랙 원인", job="risk-review", rounds=2, personas=_SEATS,
                                       evidence=_CALLER_EV, advanced={"voc": "off"})
        await delib_jobs._TASKS[out["job_id"]]
        return delib_jobs._JOBS[out["job_id"]]

    job = asyncio.run(go())
    assert job["status"] == "done", f"심의가 끝까지 못 갔다 — {job.get('error')}"
    return frames


# ── 리스크 앱이 읽는 종류 ───────────────────────────────────────────────────────
def _kind(event: str, data: dict) -> str:
    """리스크 앱이 읽는 일곱 종류 가운데 무엇인지, 아니면 빈 문자열."""
    if event == "status":
        if isinstance(data.get("knowledge"), dict):
            return "knowledge_status"
        if " 조회: " in str(data.get("step", "")) and "detail" in data:
            return "lookup_status"
        return ""
    if event != "delib":
        return ""
    if data.get("kind") == "turn":
        return "turn_say_full" if "say_full" in data else ""
    if data.get("kind") != "evidence":
        return ""
    src = str(data.get("source", ""))
    if src.endswith(" · 지식카드"):
        return "knowledge_evidence"
    if src.endswith(" · 자유 조회 실패"):
        return "lookup_failed_evidence"
    if src.startswith("챗 정리 · "):
        return "caller_evidence"
    if data.get("tool") and "args" in data and src.endswith(f" · {data['tool']}"):
        return "lookup_evidence"
    return ""


_KINDS = {"knowledge_status", "knowledge_evidence", "lookup_status", "lookup_evidence",
          "lookup_failed_evidence", "caller_evidence", "turn_say_full"}
_DONE_MASK = re.compile(r"^지식카드 \d+/")


def _lines(frames) -> list:
    """예제 줄 — 리스크 앱이 읽는 종류만, 정렬해서. 좌석이 끝나는 순서(as_completed)는 정해지지 않으므로
    `지식카드 n/N` 의 n 을 가린다."""
    out = []
    for event, data in frames:
        if not _kind(event, data):
            continue
        if event == "status" and "knowledge" in data:
            data = {**data, "step": _DONE_MASK.sub("지식카드 ?/", data["step"])}
        out.append(json.dumps({"event": event, "data": data}, ensure_ascii=False, sort_keys=True))
    return sorted(out)


def test_예제는_실제_스트림이_낸_것과_같다(monkeypatch, tmp_path):
    got = _lines(_panel_frames(monkeypatch, tmp_path))
    if os.environ.get("RISK_FRAMES_UPDATE") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text("".join(x + "\n" for x in got), encoding="utf-8")
    want = FIXTURE.read_text(encoding="utf-8").splitlines()
    assert got == want, ("엔진이 내는 프레임이 예제와 다르다 — 엔진을 고쳤으면 리스크 앱 수집 시험과 같이 보고 "
                         "RISK_FRAMES_UPDATE=1 로 다시 뜬다")


def test_예제에_일곱_종류가_다_있다():
    """대역이 바뀌어 예제가 조용히 얇아지는 것을 막는다 — 하나라도 빠지면 그쪽 시험이 그 종류를 못 본다."""
    rows = [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines()]
    kinds = {_kind(r["event"], r["data"]) for r in rows}
    assert kinds == _KINDS, f"빠진 종류: {sorted(_KINDS - kinds)} · 모르는 줄: {'' in kinds}"
    # 종류마다 리스크 앱이 집는 필드가 있다.
    for r in rows:
        k, data = _kind(r["event"], r["data"]), r["data"]
        if k == "knowledge_status":
            assert set(data["knowledge"]) == {"persona", "hits", "degraded", "query"}, data
        elif k == "lookup_status":
            assert {"detail", "call", "ok"} <= set(data), data
        elif k == "lookup_evidence":
            assert {"tool", "args", "call"} <= set(data) and data["included"] is True, data
        elif k == "lookup_failed_evidence":
            assert data["included"] is False, data
        elif k == "turn_say_full":
            assert len(data["say_full"]) > len(data["say"]), data

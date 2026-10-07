# 사람 의견(human_note) 상한 — 잘랐으면 화면과 잡 원장에 남긴다
#
# 엔진은 사람 의견을 2,000자에서 **말없이** 잘랐다. 포털은 이 칸에 8,000자까지 받으므로, 길게 쓴
# 의견은 뒤 3/4 이 좌석에 안 갔고 쓴 사람은 전부 반영된 줄 알았다(S26U 피드백 1-11 — 근거 본문이
# 조용히 사라지던 것과 같은 모양이다). 상한을 설정으로 빼고, 잘랐으면 그렇다고 알린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_human_note_cap.py -q
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _stream, _Tool  # noqa: E402, F401

_CUT = "사람 의견 상한 초과"


def _note(n):
    """앞·뒤를 가려 볼 수 있는 n 자 의견 — 상한까지는 A, 그 뒤는 Z 로 찬다."""
    return "A" * d.HUMAN_NOTE_MAX + "Z" * (n - d.HUMAN_NOTE_MAX)


# ── 받기 ─────────────────────────────────────────────────────────────────────
def test_상한_안이면_한_글자도_안_바뀐다():
    o = d._resolve_opts({"human_note": "가" * d.HUMAN_NOTE_MAX})
    assert o.human_note == "가" * d.HUMAN_NOTE_MAX and not o.human_note_cut
    assert not d._resolve_opts({}).human_note_cut and not d._DEFAULT_OPTS.human_note_cut


def test_넘으면_앞에서부터_상한까지만_싣고_원문_길이를_들고_간다():
    o = d._resolve_opts({"human_note": _note(d.HUMAN_NOTE_MAX + 3000)})
    assert o.human_note == "A" * d.HUMAN_NOTE_MAX
    assert o.human_note_cut == (d.HUMAN_NOTE_MAX + 3000, o.human_note)


def test_상한은_환경변수로_바꾸고_0_이면_자르지_않는다():
    code = ("import deliberation as d\n"
            "o = d._resolve_opts({'human_note': '가' * 9000})\n"
            "print(d.HUMAN_NOTE_MAX, len(o.human_note), bool(o.human_note_cut))\n")

    def run(value):
        env = {k: v for k, v in os.environ.items() if k != "DELIB_HUMAN_NOTE_MAX"}
        if value is not None:
            env["DELIB_HUMAN_NOTE_MAX"] = value
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                           timeout=120, env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
        assert r.returncode == 0, r.stderr[-600:]
        return r.stdout.split()

    assert run(None) == ["2000", "2000", "True"]          # 기본값 — 종전 그대로 2,000자
    assert run("6000") == ["6000", "6000", "True"]
    assert run("0") == ["0", "9000", "False"]


# ── 알리기 — 화면과 잡 원장 ──────────────────────────────────────────────────────
def test_잘랐으면_카드에_몇_자_중_몇_자인지가_남고_설정_이름은_화면_글에_없다(monkeypatch):
    """카드 글은 웹 사용자가 읽는다 — 포털의 이어하기 칸은 길이 제한이 없어 길게 쓴 사람이 바로 이 카드를
    본다. 설정·필드 이름은 따로 실어(knob) 잡 원장에만 붙인다(아래 시험)."""
    n = d.HUMAN_NOTE_MAX + 3000
    events = _stream(monkeypatch, {"human_note": _note(n)})
    out = [c for c in _cards(events, included=False) if c["source"] == _CUT]
    assert len(out) == 1, [c["source"] for c in _cards(events, included=False)]
    for want in (f"{n:,}자 중 앞 {d.HUMAN_NOTE_MAX:,}자만", "3,000자", f"상한 {d.HUMAN_NOTE_MAX:,}자"):
        assert want in out[0]["text"], (want, out[0]["text"])
    for internal in ("DELIB_HUMAN_NOTE_MAX", "human_note"):
        assert internal not in out[0]["text"], f"화면에 나가는 글에 내부 이름 {internal} 이 있다 — {out[0]['text']}"
    assert "DELIB_HUMAN_NOTE_MAX" in out[0]["knob"], out[0]
    shown = next(c for c in _cards(events, included=True) if c["source"] == "인간 검토자 의견")
    assert "Z" not in shown["text"], "잘린 뒷부분이 좌석에 준 카드에 보인다"


def test_잘린_것이_MCP_호출자에게도_보인다(monkeypatch):
    events = _stream(monkeypatch, {"human_note": _note(d.HUMAN_NOTE_MAX + 1)})
    for name, view in _mcp_view(monkeypatch, events).items():
        notes = [x for x in view["evidence_omitted"] if x.get("source") == _CUT]
        assert len(notes) == 1 and "DELIB_HUMAN_NOTE_MAX" in notes[0]["text"], (name, view)


def test_안_잘랐으면_카드도_없다(monkeypatch):
    events = _stream(monkeypatch, {"human_note": "가" * d.HUMAN_NOTE_MAX})
    assert [c for c in _cards(events) if c["source"] == _CUT] == []
    assert [c["source"] for c in _cards(events, included=True)] == ["인간 검토자 의견"]


# ── 엔진이 쓴 지시문은 이 상한을 받지 않는다 ─────────────────────────────────────
def _test_plan_events(monkeypatch, human_note):
    """시험 설계 진입점을 1라운드 문턱까지 실제로 돌린다 — 이 경로는 사람 의견 칸을 엔진 지시문과
    물성 현황 조회 결과로 **갈아 끼운다**(수천~수만 자)."""
    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    async def _fake_snapshot(*_a, **_k):
        return "[물성 근거 현황] " + "현" * (d.HUMAN_NOTE_MAX * 3)

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_material_evidence_snapshot", _fake_snapshot)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))
    opts = {"free_tools": 0, "voc": "off", "rescreen": 0, "human_note": human_note}

    async def go():
        out, gen = [], d.run_test_plan(stub, "접착층 박리 강도", [], opts)
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                out.append((ev, data))
                if ev == "delib" and data.get("kind") == "stage" and data.get("stage") == "r1":
                    break
        finally:
            await gen.aclose()
        return out

    events = asyncio.run(go())
    assert events and events[-1][1].get("stage") == "r1", [e for e in events if e[0] == "error"]
    return events


def test_엔진이_깐_지시문과_현황은_자르지_않는다(monkeypatch):
    seen = {}
    real = d._cont_block

    def _spy(summary, nn, human_note):
        seen["note"] = human_note
        return real(summary, nn, human_note)

    monkeypatch.setattr(d, "_cont_block", _spy)
    events = _test_plan_events(monkeypatch, "")
    assert len(seen["note"]) > d.HUMAN_NOTE_MAX * 3, "엔진이 깐 물성 현황이 사람 의견 상한에서 잘렸다"
    assert [c for c in _cards(events) if c["source"] == _CUT] == []


def test_시험_설계는_엔진_지시문_뒤에_호출자_의견을_싣고_잘랐으면_그_길이로_알린다(monkeypatch):
    """종전엔 이 경로가 호출자의 의견을 통째로 버렸다(tests/test_test_plan_caller_inputs). 이제 지시문 뒤에
    싣는다 — 그러니 상한에서 자른 것도 알려야 하고, 길이는 **호출자 의견의** 길이여야 한다(칸 전체에는 엔진
    지시문과 수천 자짜리 물성 현황이 같이 들어 있다)."""
    seen = {}
    real = d._cont_block

    def _spy(summary, nn, human_note):
        seen["note"] = human_note
        return real(summary, nn, human_note)

    monkeypatch.setattr(d, "_cont_block", _spy)
    n = d.HUMAN_NOTE_MAX + 3000
    events = _test_plan_events(monkeypatch, _note(n))
    assert seen["note"].startswith("이미 실측이 있는 항목"), "엔진 지시문이 머리에 없다"
    assert "A" * d.HUMAN_NOTE_MAX in seen["note"] and "Z" not in seen["note"], "호출자 의견(상한까지)이 좌석에 안 간다"
    out = [c for c in _cards(events, included=False) if c["source"] == _CUT]
    assert len(out) == 1, [c["source"] for c in _cards(events, included=False)]
    assert f"{n:,}자 중 앞 {d.HUMAN_NOTE_MAX:,}자만" in out[0]["text"] and "3,000자" in out[0]["text"], out[0]["text"]
    shown = next(c for c in _cards(events, included=True) if c["source"] == "인간 검토자 의견")
    assert "AAAA" in shown["text"], "화면의 사람 의견 카드에 호출자 의견이 안 보인다"


def test_엔진이_의견_칸을_통째로_갈아_끼운_단에서는_실었다고_말하지_않는다(monkeypatch):
    """해석 설계 2·3단은 이 칸을 엔진 지시문으로 **갈아 끼운다**(호출자 의견은 1단에 실렸고 거기서 알렸다).
    그 단에서 '앞 N자만 실었다' 고 또 적으면 거짓이다 — 아예 안 실린다."""
    from test_delib_silent_drops import _at_round1

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    opts = d._resolve_opts({"human_note": _note(d.HUMAN_NOTE_MAX + 3000), "free_tools": 0, "voc": "off",
                            "rescreen": 0, "personas": [{"key": "mech-a", "role": "기구"},
                                                        {"key": "rel-b", "role": "신뢰성"}]})
    assert opts.human_note_cut, "시험 전제 — 1단에서 이미 잘렸다"
    opts.human_note = "사내 보유 도구를 우선 검토하라."          # run_sim_deliberation 이 2단에서 하는 일
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))

    async def go():
        out, gen = [], d._deliberation_stream(stub, "해석 설계", [], opts)
        try:
            async for chunk in gen:
                out.append(delib_jobs._parse_sse(chunk))
                if _at_round1(*out[-1]):
                    break
        finally:
            await gen.aclose()
        return out

    events = asyncio.run(go())
    assert _at_round1(*events[-1]), [e for e in events if e[0] == "error"]
    assert [c for c in _cards(events) if c["source"] == _CUT] == []


# ── 안내 ─────────────────────────────────────────────────────────────────────
def test_메뉴가_지금_상한을_상수에서_읽어_알려_준다(monkeypatch):
    monkeypatch.setattr(d, "HUMAN_NOTE_MAX", 4321)
    menu = asyncio.run(m.deliberate_jobs())
    assert menu["limits"]["human_note_chars"] == 4321
    assert "2000" not in (m.deliberate_start.__doc__ or ""), "독스트링에 손으로 적은 상한이 남아 있다"

# 요청이 청하는 LLM 호출당 한도(timeout_s)의 상한을 설정(DELIB_TIMEOUT_MAX_S)으로 두고, 죄었으면 알린다
#
# 엔진은 요청의 timeout_s 를 10~1800 으로 **말없이** 죄었다. 큰 패널이라 2시간을 청한 호출자는 30분으로 돈 줄
# 몰랐고, 잡 기록에는 보낸 값이 걸린 것처럼 남았으며, 이어하기는 그 값을 아예 이어받지 않았다. 같은 1800 이
# 포털 스키마·프론트 클램프에도 리터럴로 따로 있어, 한쪽 값만 아는 호출자가 포털에서 요청 전체를 422 로 잃었다.
# 20석 넘는 패널은 공유 LLM 에 줄을 서는 시간까지 이 시계에 들어가므로 상한의 기본값을 14,400초로 올린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_timeout_cap.py -q
import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

# 같은 하네스를 쓴다 — 스트림과 MCP 도구를 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_sealed import _continue, _run  # noqa: E402
from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _stream, _Tool  # noqa: E402, F401

_CARD = "요청 값 상한 초과"


# ── 범위 ─────────────────────────────────────────────────────────────────────
def test_종전_상한_1800_을_넘는_값을_그대로_받는다():
    for ts in (1801, 3600, 7200, 14400):
        o = d._resolve_opts({"timeout_s": ts})
        assert (o.timeout_s, o.timeout_clamped) == (float(ts), None), (ts, o.timeout_s, o.timeout_clamped)


def test_상한을_넘으면_상한으로_하한_밑이면_하한으로_죄고_보낸_값을_들고_간다():
    o = d._resolve_opts({"timeout_s": 20000})
    assert (o.timeout_s, o.timeout_clamped) == (14400.0, (20000.0, 14400.0))
    o = d._resolve_opts({"timeout_s": 3})
    assert (o.timeout_s, o.timeout_clamped) == (10.0, (3.0, 10.0))
    assert d._resolve_opts({"timeout_s": "inf"}).timeout_s == 14400.0
    assert d._resolve_opts({}).timeout_clamped is None and d._DEFAULT_OPTS.timeout_clamped is None


def test_기본값_14400_은_소스에_적힌_수다():
    """포털 스키마(le)와 프론트 클램프가 이 수와 같아야 한다 — 포털의 계약 시험이 **이 줄을 읽어** 대조한다
    (HWAXPortal backend/tests/test_delib_timeout_cap_contract). 줄 모양을 바꾸면 그 시험이 못 찾는다."""
    src = (ROOT / "deliberation.py").read_text(encoding="utf-8")
    assert re.search(r'^DELIB_TIMEOUT_MAX_S = _env_float\("DELIB_TIMEOUT_MAX_S", 14400\.0\)$', src, re.M)
    # 포털 시험의 정규식 그대로 — 파일에서 **처음** 걸리는 것이 기본값이어야 한다(주석의 다른 수를 집지 않게).
    first = re.search(r"""["']DELIB_TIMEOUT_MAX_S["']\s*,\s*["']?(\d+(?:\.\d+)?)""", src)
    assert first and float(first.group(1)) == 14400.0, first and first.group(0)


def _loaded(value):
    env = {k: v for k, v in os.environ.items() if k != "DELIB_TIMEOUT_MAX_S"}
    if value is not None:
        env["DELIB_TIMEOUT_MAX_S"] = value
    code = ("import deliberation as d; "
            "print(d.DELIB_TIMEOUT_MAX_S, d._resolve_opts({'timeout_s': 20000}).timeout_s)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stderr[-600:]
    cap, got = r.stdout.split()[-2:]
    return float(cap), float(got), r.stdout


def test_상한은_설정으로_바꾼다():
    assert _loaded(None)[:2] == (14400.0, 14400.0)
    assert _loaded("28800")[:2] == (28800.0, 20000.0), "상한을 올린 박스에서도 14,400 으로 죈다"
    assert _loaded("600")[:2] == (600.0, 600.0)


def test_0_과_음수는_상한_없음이_아니다():
    """하한(10초)과 뒤집히면 timeout_s 를 준 요청이 전부 10초로 돈다 — 좌석 전원이 시간 초과로 빠진다.
    이 파일의 다른 손잡이는 0 이 무제한이라 그렇게 넣기 쉽다. 기본값으로 읽고 그렇게 말한다."""
    for bad in ("0", "-1", "5"):
        cap, got, out = _loaded(bad)
        assert (cap, got) == (14400.0, 14400.0), (bad, cap, got)
        assert "DELIB_TIMEOUT_MAX_S" in out and "기본값 14400" in out, out


# ── 알림 ─────────────────────────────────────────────────────────────────────
def test_죄었으면_카드로_알리고_잡_원장에는_손잡이_이름이_붙는다(monkeypatch):
    events = _stream(monkeypatch, {"timeout_s": 20000})
    (card,) = [c for c in _cards(events, included=False) if c["source"] == _CARD]
    assert "20,000초" in card["text"] and "14,400초" in card["text"], card["text"]
    assert "LLM 호출 1회" in card["text"] and "심의 전체 시간이 아니다" in card["text"], card["text"]
    assert card.get("notice") is True, "좌석에 안 준 근거가 아니라 알림이다"
    # 화면에 뜨는 글에는 설정·필드 이름이 없다 — 이름은 knob 으로 가고 잡 원장이 붙인다.
    assert "DELIB_" not in card["text"] and "timeout_s" not in card["text"], card["text"]
    for name, view in _mcp_view(monkeypatch, events).items():
        row = next(x for x in view["evidence_omitted"] if x.get("source") == _CARD)
        assert "DELIB_TIMEOUT_MAX_S" in row["text"] and "timeout_s" in row["text"], (name, row)


def test_범위_안이면_카드가_없다(monkeypatch):
    for req in ({"timeout_s": 7200}, {"timeout_s": 14400}, {}):
        events = _stream(monkeypatch, req)
        assert not [c for c in _cards(events, included=False) if c["source"] == _CARD], req


def test_죈_값으로_LLM_을_다시_만든다(monkeypatch):
    """알림만 내고 정작 호출은 서버 기본값으로 가면 안 된다 — 요청 한도는 그 값으로 LLM 을 새로 구성해 건다."""
    made = []

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    state = SimpleNamespace(llm=object(), delib_llm=object(), delib_timeout_s=1800.0,
                            mk_delib_llm=lambda ts: made.append(ts) or object())

    async def go(req):
        gen = d.run_deliberation(SimpleNamespace(state=state), "힌지 크랙 원인", [],
                                 {"personas": [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}],
                                  "free_tools": 0, "voc": "off", "rescreen": 0, "persona_knowledge": 0, **req})
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                assert ev != "error", data
                if ev == "delib" and data.get("stage") == "r1":
                    break
        finally:
            await gen.aclose()

    asyncio.run(go({"timeout_s": 20000}))
    assert made == [14400.0], made
    made.clear()
    asyncio.run(go({"timeout_s": 7200}))
    assert made == [7200.0], f"종전엔 1800 으로 죄어 서버 기본값과 같다고 보고 다시 만들지 않았다 — {made}"
    made.clear()
    asyncio.run(go({}))
    assert made == [], "안 준 요청은 서버 기본값 LLM 을 그대로 쓴다"


# ── 잡 기록과 이어하기 ───────────────────────────────────────────────────────
def test_잡_기록에는_걸린_값이_남고_이어하기가_그_값을_이어받는다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, job="diagnosis", advanced={"timeout_s": 20000})
    assert asyncio.run(m.deliberate_result(r.job["id"]))["applied_opts"]["timeout_s"] == 14400.0, r.job["opts"]
    assert any(x.get("source") == _CARD for x in r.job["evidence_omitted"]), r.job["evidence_omitted"]
    c = _continue(r)
    assert c.job["opts"]["timeout_s"] == 14400.0, f"이어하기가 호출당 한도를 서버 기본값으로 되돌렸다 — {c.job['opts']}"
    assert not any(x.get("source") == _CARD for x in c.job["evidence_omitted"]), (
        "이어받은 값은 이미 죈 값이다 — 또 죄었다고 적었다")
    c2 = _continue(r, advanced={"timeout_s": 600})
    assert c2.job["opts"]["timeout_s"] == 600.0, "이번 회차에 다시 건 값이 이기지 않았다"


def test_안_건_심의의_이어하기는_서버_기본값으로_돈다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, job="diagnosis")
    assert "timeout_s" not in r.job["opts"]
    assert "timeout_s" not in _continue(r).job["opts"]


# ── 찾을 수 있다 ─────────────────────────────────────────────────────────────
def test_안내가_호출_1회_기준임과_받는_범위를_엔진_값으로_말한다(monkeypatch):
    from test_mcp_start_desc import _listed
    listed = {t.name: t.description for t in asyncio.run(m.mcp.list_tools())}
    menu = asyncio.run(m.deliberate_jobs())
    for text in (_listed("deliberate_start"), menu["options"]["advanced"]):     # 시작 도구는 손잡이 칸 설명(advanced.timeout_s)에 있다
        at = text.index("timeout_s(")
        seg = text[at:at + 420]
        for want in ("LLM 호출 1회", "심의 전체 시간이 아니다", "DELIB_TIMEOUT_S", "DELIB_TIMEOUT_MAX_S",
                     f"{d.DELIB_TIMEOUT_MAX_S:.0f}", "evidence_omitted"):
            assert want in seg, (want, seg)
    assert "timeout_s" in listed["deliberate_continue"], "이어받는 손잡이에 timeout_s 가 없다"
    assert menu["limits"]["timeout_s_max"] == d.DELIB_TIMEOUT_MAX_S
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(delib_timeout_s=1800.0)))
    assert asyncio.run(m.deliberate_jobs())["limits"]["timeout_s_default"] == 1800.0


# ── T4 생각 강도를 요청마다(HWAXPortal docs/deliberation-quality T4 · docs/leftovers-1121 L21-8 #14) ─────────────────────
def _effort_run(monkeypatch, req, default_to=1800.0):
    """요청 한 번으로 엔진을 1라운드까지 돌려, 심의 LLM 을 다시 만든 호출의 인자(위치 · 이름)를 적는다."""
    made = []

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    def mk(*a, **kw):
        made.append((a, kw))
        return object()

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    state = SimpleNamespace(llm=object(), delib_llm=object(), delib_timeout_s=default_to, mk_delib_llm=mk)

    async def go():
        gen = d.run_deliberation(SimpleNamespace(state=state), "힌지 크랙 원인", [],
                                 {"personas": [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}],
                                  "free_tools": 0, "voc": "off", "rescreen": 0, "persona_knowledge": 0, **req})
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                assert ev != "error", data
                if ev == "delib" and data.get("stage") == "r1":
                    break
        finally:
            await gen.aclose()
    asyncio.run(go())
    return made


def test_생각_강도를_주면_그_심의만_그_강도의_LLM_으로_돈다(monkeypatch):
    assert _effort_run(monkeypatch, {"reasoning_effort": "high"}) == [((1800.0, "high"), {})], "시간 한도는 서버 값 그대로 · 강도만 바꾼다"
    assert _effort_run(monkeypatch, {"reasoning_effort": " LOW ", "timeout_s": 7200}) == [((7200.0, "low"), {})]


def test_생각_강도를_안_주면_종전_그대로다(monkeypatch):
    assert _effort_run(monkeypatch, {}) == [], "아무것도 안 주면 서버의 심의 LLM 을 그대로 쓴다"
    assert _effort_run(monkeypatch, {"timeout_s": 7200}) == [((7200.0,), {})], "시간 한도만 주면 종전 호출 꼴(인자 하나)이다"
    assert d._resolve_opts({}).reasoning_effort is None


@pytest.mark.parametrize("bad", ["extreme", 3, True, ["high"], "hi gh"])
def test_모르는_생각_강도는_버리고_그렇게_말한다(monkeypatch, bad):
    o = d._resolve_opts({"reasoning_effort": bad})
    assert o.reasoning_effort is None and "reasoning_effort" in o.req_unread and "low|medium|high|xhigh" in o.req_unread["reasoning_effort"]
    assert _effort_run(monkeypatch, {"reasoning_effort": bad}) == []


def test_빈_값은_안_준_것이다():
    for empty in (None, ""):
        o = d._resolve_opts({"reasoning_effort": empty})
        assert o.reasoning_effort is None and "reasoning_effort" not in o.req_unread


def test_러너가_생각_강도를_낮음_대_높음으로_잰다():
    from tools import delib_ab

    assert "reasoning_effort" in delib_ab.KNOB_ORDER and delib_ab.levels_of("reasoning_effort") == ("low", "high")

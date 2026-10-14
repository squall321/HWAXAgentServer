# 쟁점 패널(리스크 앱 M3 S3-7)이 심의 엔진에 기대는 것을 못박는다 — 큰 카드 근거 항목 · 근거 key 형식 · 좌석별 검색어 · 관문 셈(#26) · 보고서 저장 끄기 · 가짜 LLM 의 심의 대본
#
# 쟁점 패널은 새 엔진 코드 없이 종전 패널 길(포털 /agent/chat 의 /심의 → _detach_stream)로 돈다(m3-engine-ops 계약 10). 그래서 이
# 시험은 엔진이 이미 하는 일을 쟁점 패널의 모양으로 다시 본다 — 바뀌면 앱이 조용히 어긋나는 자리들이다(M3.md #26 · #48).
#   ① 1백만 토큰 창에서 카드 절을 담은 큰 I6 항목(card:<record_id>#<sid> 표지와 «») 하나가 줄지도 빠지지도 않고 좌석에 든다.
#   ② 근거 key 는 [A-Za-z0-9_.-]{1,24} 다 — card:… 를 key 로 보내면 표지에서 빠지고 req_unread["evidence.key"] 에 사유가 남는다
#      (형식 가드 — 앱은 카드 절을 I6 한 항목 안에 싣는다, #48).
#   ③ 좌석별 knowledge_query 가 그 좌석의 지식카드 검색어가 된다.
#   ④ 사람이 띄운 심의는 delib_active 에 들고 관문을 4 로, 리스크 앱 패널(started_by=app)도 delib_active 에 들지만 관문은 8 이다.
#   ⑤ save_report=0 이면 보고서 저장 도구를 부르지 않는다.
#   ⑥ 가짜 LLM 의 심의 대본(tests/fakes/llm.py delib_respond)으로 risk-review 패널이 끝까지 돌고, 결정문 뒤 risk_spec 이 판정 FAIL
#      로 읽히며 리스크 앱의 risk_spec.v1 스키마를 지난다(형제 리포가 곁에 있으면 — 곁에 있는데 스키마를 못 읽으면 실패, P-22).
# 끝으로 포털의 scripts/check_chair_parity.py 로 세 리포 좌석 계약이 그대로인지 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_risk_issue_panel_contract.py -q
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
from fakes.llm import DELIB_SAY, delib_respond, risk_spec_of  # noqa: E402

PORTAL = ROOT.parent / "HWAXPortal"
RISK = ROOT.parent / "HWAXRisk"
SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}, {"key": "pcb-c", "role": "보드"}]
TK = "diff:" + "ab" * 16


class _Tool:
    coroutine = None

    def __init__(self, name, answer='{"hits": []}'):
        self.name, self._answer, self.calls = name, answer, []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self._answer


@pytest.fixture
def ctx_1m():
    saved = dict(app._ctx_cache)
    app._ctx_cache["n"] = 1_048_576
    d._evid_cache.clear()
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


def _panel(monkeypatch, opts, *, tools=None, until=None):
    """risk-review 패널 하나를 run_deliberation 으로 **실제로** 돌린다 — LLM 자리는 가짜 LLM 의 심의 대본. (사건, LLM 이 받은
    (시스템, 사람) 글, 도구 사전). 좌석은 지정(발굴 생략), 자유 조회·VOC·재심사는 끈다(망을 타는 단계)."""
    seen = []

    async def fake_llm(_llm, system, human):
        seen.append((system, human))
        return delib_respond(system, human)

    tools = tools if tools is not None else {"agent_search": _Tool("agent_search"),
                                             "create_report_draft": _Tool("create_report_draft", '{"id": 7}')}

    async def fake_tools(*_a, **_k):
        return tools

    monkeypatch.setattr(d, "_llm_text", fake_llm)
    monkeypatch.setattr(d, "_tools_by_name", fake_tools)
    stub = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))
    base = {"personas": SEATS, "free_tools": 0, "voc": "off", "rescreen": 0, "chair_template": "risk-review",
            "save_report": 0, "started_by": "app", "rounds": 3}

    async def go():
        out, gen = [], d.run_deliberation(stub, f"쟁점 패널 — {TK} 보스 간극", [], {**base, **opts})
        try:
            async for chunk in gen:
                ev, data = delib_jobs._parse_sse(chunk)
                out.append((ev, data))
                if until and until(ev, data):
                    break
        finally:
            await gen.aclose()
        return out

    events = asyncio.run(go())
    errs = [data for ev, data in events if ev == "error"]
    assert not errs, f"심의가 오류로 끝났다 — {errs}"
    return events, seen, tools


def _evidence_cards(events) -> list:
    return [data for ev, data in events if ev == "delib" and data.get("kind") == "evidence"]


# ① 큰 I6 한 항목 ───────────────────────────────────────────────────────────────
def test_카드_절을_담은_큰_I6_한_항목이_1M_창에서_빠지거나_줄지_않고_좌석에_든다(monkeypatch, ctx_1m):
    i6 = "\n".join(f"card:DOC-{i:04d}#s{i} «" + "보스 둘레 간극과 낙하 응력에 대한 카드 절 본문. " * 160 + "»"
                   for i in range(28))
    assert 100_000 < len(i6) < d._EVID_ITEM_MAX, "시험 전제 — 항목 하나의 천장 안에서 크게"
    evidence = [{"key": "I1", "source": "쟁점 닻", "result": f"{TK} mechanical.drop_stress ck:0123456789ab"}] + \
               [{"key": f"I{n}", "source": f"쟁점 근거 {n}", "result": f"근거 {n} 본문 " * 50} for n in (4, 5)] + \
               [{"key": "I6", "source": "카드 절", "result": i6}, {"key": "M", "source": "메모", "result": "사람 메모"}]
    events, seen, _ = _panel(monkeypatch, {"evidence": evidence},
                             until=lambda ev, data: ev == "delib" and data.get("kind") == "stage" and
                             str(data.get("stage")) == "r2")
    sources = [c.get("source") for c in _evidence_cards(events)]
    assert "사전 근거 예산 초과" not in sources and "사전 근거 항목 상한 초과" not in sources, sources
    seat_prompts = [h for s, h in seen if "전문가입니다" in s]
    assert seat_prompts and all(i6 in h for h in seat_prompts), "I6 전문이 좌석 프롬프트에 그대로 들지 않았다"
    assert all("[e:4|I6]" in h for h in seat_prompts), "근거 key 가 표지에 실리지 않았다"


# ② key 형식 가드 ─────────────────────────────────────────────────────────────────
def test_card_로_시작하는_key_는_표지에서_빠지고_사유가_남는다():
    o = d._resolve_opts({"evidence": [{"key": "card:DOC-1#s1", "result": "카드 절"}, {"key": "I6", "result": "x"}]})
    assert [e["key"] for e in o.evidence] == ["", "I6"]
    assert "card:DOC-1#s1" in o.req_unread["evidence.key"] and "형식" in o.req_unread["evidence.key"]
    assert d._EVID_KEY_RE.pattern.startswith("[A-Za-z0-9_.-]{1,")


# ③ 좌석별 검색어 ────────────────────────────────────────────────────────────────
def test_좌석별_knowledge_query_가_그_좌석의_지식카드_검색어가_된다(monkeypatch, ctx_1m):
    search = _Tool("agent_search")
    _panel(monkeypatch, {"knowledge_query": {"mech-a": "보스 간극 리브", "*": "낙하 응력"}},
           tools={"agent_search": search},
           until=lambda ev, data: ev == "delib" and data.get("kind") == "stage" and str(data.get("stage")) == "r1")
    by = {c["agent_type"]: c["q"] for c in search.calls}
    assert by["mech-a"] == "보스 간극 리브" and by["rel-b"] == by["pcb-c"] == "낙하 응력", search.calls


# ④ 관문 셈(#26) ──────────────────────────────────────────────────────────────────
def test_사람_심의는_관문을_4로_쟁점_패널은_8로_두고_둘_다_delib_active_에_든다(monkeypatch):
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(app.app.state, "review_gate", None, raising=False)

    async def go():
        release = asyncio.Event()

        async def gen():
            await release.wait()
            yield b"x"

        app._detach_stream(gen(), "issue-panel", "app")
        await asyncio.sleep(0)
        panel_only = (app._delib_load()[0], cr.gate_of(app.app).snapshot()["limit"])
        app._detach_stream(gen(), "delib")
        await asyncio.sleep(0)
        snap = cr.gate_of(app.app).snapshot()
        both = (app._delib_load()[0], snap["limit"], snap["limit_reason"], snap["delib_human"])
        release.set()
        await asyncio.sleep(0.05)
        return panel_only, both

    panel_only, both = asyncio.run(go())
    assert panel_only == (1, 8), "쟁점 패널만 돌 때 관문이 절반으로 내려갔다(#26)"
    assert both == (2, 4, "delib_human", 1)


# ⑤·⑥ 끝까지 — 보고서 저장 끄기와 가짜 LLM 의 심의 대본 ─────────────────────────────────
def _schema_errors(spec: dict) -> list | None:
    path = RISK / "backend" / "app" / "schemas" / "risk_spec.v1.json"
    if not RISK.is_dir():
        return None
    assert path.is_file(), f"형제 리포는 있는데 risk_spec 스키마가 없다 — {path}"
    import jsonschema

    v = jsonschema.Draft7Validator(json.loads(path.read_text(encoding="utf-8")))
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in v.iter_errors(spec)]


def test_가짜_LLM_의_심의_대본으로_쟁점_패널이_끝까지_돌고_결정문이_FAIL_risk_spec_을_싣는다(monkeypatch, ctx_1m):
    evidence = [{"key": "I1", "source": "쟁점 닻", "result": f"{TK} · mechanical.drop_stress · ck:0123456789ab"}]
    events, seen, tools = _panel(monkeypatch, {"evidence": evidence, "rebut_quote": 1})
    decision = next(data["text"] for ev, data in events if ev == "delib" and data.get("kind") == "decision")
    fences = re.findall(r"```json\s*(\{.*?\})\s*```", decision, re.S)
    assert fences, "결정문 뒤에 risk_spec 펜스가 없다"
    spec = json.loads(fences[-1])
    f = spec["findings"][0]
    assert (spec["schema"], f["judgement"], f["severity"], spec["verdict"]) == ("risk_spec", "FAIL", "치명", "no-go")
    assert spec["scope"]["target_key"] == TK and f["mechanism_detail"] == "mechanical.drop_stress"
    assert f["subject"]["ckeys"] == ["ck:0123456789ab"] and set(f["raised_by"]) == {"mech-a", "rel-b", "pcb-c"}
    errs = _schema_errors(spec)
    assert errs in (None, []), errs
    seat_calls = [h for s, h in seen if "전문가입니다" in s]
    seated = {re.search(r"'([^']+)' 전문가입니다", s).group(1) for s, h in seen if "전문가입니다" in s}
    assert seated == {"mech-a", "rel-b", "pcb-c", "delib-baseline-defender"}, seated   # risk-review 는 기준선 옹호석 자동
    assert len(seat_calls) == len(seated) * 3, len(seat_calls)
    assert not any("(재시도" in h for h in seat_calls), "대본 좌석이 요구 키·인용 계약을 못 지켜 다시 물었다"
    assert tools["create_report_draft"].calls == [], "save_report=0 인데 보고서를 저장했다"
    steps = [data.get("step", "") for ev, data in events if ev == "status"]
    assert "보고서 저장 건너뜀(save_report=0)" in steps
    assert DELIB_SAY in decision


def test_대본_risk_spec_은_자리값으로도_스키마를_지난다():
    errs = _schema_errors(risk_spec_of("단서가 없는 의장 프롬프트"))
    assert errs in (None, []), errs


def test_세_리포_좌석_계약은_그대로다():
    script = PORTAL / "scripts" / "check_chair_parity.py"
    if not PORTAL.is_dir():
        pytest.skip("형제 리포 HWAXPortal 이 곁에 없다")
    assert script.is_file(), f"형제 리포는 있는데 파리티 스크립트가 없다 — {script}"
    r = subprocess.run([sys.executable, str(script)], cwd=str(PORTAL), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_가짜_LLM_서버의_대본은_카드_대조가_아닌_프롬프트를_심의_대본으로_답한다():
    """리허설은 가짜 LLM 서버 하나로 카드 대조와 쟁점 패널을 함께 돈다 — 거동(flag_fail 등)과 상관없이 심의 호출은 대본이다."""
    from fakes.llm import act, respond

    text, finish = respond(act("flag_fail"), "당신은 심의체 의장입니다. 한국어 엔지니어링 톤으로 명확하게.",
                           f"{TK} risk_spec 을 함께 내라")
    assert finish == "stop" and '"judgement": "FAIL"' in text and "```json" in text
    seat, _ = respond(act("oracle"), "당신은 'mech-a' 전문가입니다. 전문 영역: 기구.", "[1라운드]")
    assert json.loads(seat.split("\n", 1)[1])["lens"].startswith(DELIB_SAY)

# 자기검증 관문(HA-27) 블랙박스 시험 — 설계서만 보고 쓴 수 뽑기 · 원문 색인 · 쌍 판정 · 원문 붙잡기 · 검토자 입력 · 판정 · /health
"""HWAXPortal docs/request_261030.md HA-27 — 자기검증이 맞는 초안을 틀리게 고쳤다(OT-04 34 → 13 · OT-08 다른 항목의 부서 · OT-09 '20' 을
'근거 없음' 으로 · DW-05 직전 기간 값을 지움). 설계(docs/request-1030/design-ha27.md)는 검토자의 바꿀 곳을 넣기 전에 이번 턴 도구 결과의
원문(접기 · 자르기 전)과 다시 대조해 넣음 · 짚음 · 버림을 정한다.

이 파일은 구현을 보지 않고 설계서 12절의 시험 목록(G · I · D · W · M · P · J · E · H · L)과 5 · 6 · 8 · 10절의 약속, design-integration.md
C-13 만으로 썼다. 이름 · 손잡이 · 사건 꼴 · 문구는 설계서 그대로이고, 설계가 열어 둔 꼴은 맨 위 도우미 칸 한 곳에 가정으로 모았다. 가짜는
기존 시험의 것을 다시 쓴다 — tests/test_chat_self_check.py 의 _Reviewer(가짜 검토자) · _inner(가짜 안쪽 턴) · _run, tests/test_request_1029.py
의 _tool_of(_prep_tool 로 감싸 진짜 capped 를 지난다) · _cache. 루트 conftest 가 CHAT_SELF_CHECK=0 이라 턴 시험은 제 손으로 켠다.

  실행:  .venv/bin/python -m pytest tests/test_bb_ha27.py -q
"""
import asyncio
import json
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402
from test_chat_self_check import _Reviewer, _events, _inner, _run  # noqa: E402
from test_request_1029 import _cache, _tool_of  # noqa: E402


# ── 설계가 열어 둔 꼴 — 가정은 이 칸에만 둔다(구현의 꼴이 다르면 여기만 고친다) ──────────────────────────────────────────────
def _g():
    """새 순수 모듈 sc_gate(설계 3 · 11절 — 에이전트 루트, evidence.py 옆). 없으면 시험마다 그 까닭으로 실패한다 — 맨 위에서 import 하면
    파일 전체가 수집 오류 하나로 가려 어느 약속이 빠졌는지 보이지 않는다."""
    try:
        import sc_gate  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        pytest.fail(f"sc_gate 모듈이 없다 — HA-27 관문이 아직 없다({exc})", pytrace=False)
    return sc_gate


def _num(s: str):
    """설계 I3 의 num = lambda s: num_tokens(s)[0]."""
    return _g().num_tokens(s)[0]


def _idx(*raws, extra=(), partial=False):
    """원문 색인 RawIndex.build(texts, extra=(), *, partial=False)(설계 6.2) — 사전 · 목록은 JSON 글로 바꿔 넣는다."""
    texts = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in raws]
    return _g().RawIndex.build(texts, extra=list(extra), partial=partial)


def _has(idx, s: str, strong: bool = False) -> bool:
    """가정 — idx.has(n, strong=…) 의 n 은 수 뽑기가 낸 Num 이다(6.3 은 n 의 반올림 폭 · pct 로 구간을 잡는다). 설계 시험 글의 'has(34)' 는
    has(num_tokens("34")[0]) 로 읽는다."""
    return bool(idx.has(_num(s), strong=strong))


def _values_removed(idx, old: str, new: str) -> list:
    """가정 — 원문 값을 뺀 것(I4 · 7절)은 RawIndex 의 메서드 idx.values_removed(old, new) 다(11절의 모듈 함수 목록에 없다). 메서드가 없으면
    모듈 함수 sc_gate.values_removed(idx, old, new) 로 본다."""
    f = getattr(idx, "values_removed", None)
    return list(f(old, new)) if f is not None else list(_g().values_removed(idx, old, new))


def _gate(pairs, raw, **kw) -> list:
    """gate_patches(patches, idx, *, draft="", message="", values=True)(6.4) → [(action, code, detail)]. 설계 D 시험이 쌍마다 따로 적은 기대는
    따로 부른다 — 한 번에 부르면 묶음(6.5)이 서로를 짚는다(D8 의 두 쌍은 같은 47 을 더한다)."""
    return [(d.action, d.code, d.detail) for d in _g().gate_patches(list(pairs), _idx(raw), **kw)]


# ── 공용 꼴 ────────────────────────────────────────────────────────────────────────────────────────────────────────
_OK = "[판정] 고칠 것 없음\n[고친 점]\n- 없음\n"
_GATE_LINE = "서버가 [바꿀 곳] 의 쌍을 조회 결과 원문"          # 관문이 켜진 도구 턴에만 싣는 지시 끝 줄(5.5 _SC_ASK_GATE_LINE)
# OT-04 꼴 — 항목 40개 뒤 끝에 total 34(1,000자 넘는 JSON). 모델 사본은 상한에서 잘려 34 를 못 본다(E1 · W1 · W6).
_OT04_RAW = json.dumps({"items": [{"model": f"DM-{i:03d}", "stage": "개발"} for i in range(40)], "total": 34}, ensure_ascii=False)
_OT04_FIX = "[판정] 고침\n[고친 점]\n- 34 → 13\n[바꿀 곳]\n[원문]\n개발모델은 34개\n[새 글]\n개발모델은 13개"
# OT-08 꼴 — 같은 결과의 두 항목, 부서가 다르다(I4 · D7 · 턴 시험)
_OT08_RAW = '[{"item": "X-01", "dept": "품질기술팀"}, {"item": "X-02", "dept": "신뢰성그룹"}]'


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """셸이 내보낸 관문 손잡이는 지운다(기본값 '1' · 6,000,000 을 본다). /health 계수 · 접기 계수는 시험마다 0 에서 센다 — _SC_STAT 는 gate
    칸 없이 둔다(기존 HA-13 시험이 그렇게 바꿔 끼우므로 기록이 칸을 스스로 세워야 한다, 8.3). 창은 박아 둔다(LLM 서버에 묻지 않게)."""
    for k in ("CHAT_SELF_CHECK_GATE", "CHAT_SELF_CHECK_RAW_CHARS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(a, "_SC_STAT", {"counts": a.Counter(), "ms": [], "last_error": None})
    monkeypatch.setattr(a, "_HUB_FOLD_STAT", {"count": 0, "chars_in": 0, "chars_out": 0})
    monkeypatch.setattr(a, "_ctx_cache", {"n": 131_072})


def _box() -> dict:
    """턴 그릇의 처음 꼴(4.2)."""
    return {"texts": [], "chars": 0, "partial": False}


@contextmanager
def _raw_box(box: dict):
    """시험 동안만 이번 턴 그릇을 건다 — 문맥 변수는 풀지 않으면 뒤 시험에 남는다."""
    tok = a._turn_sc_raw.set(box)
    try:
        yield box
    finally:
        a._turn_sc_raw.reset(tok)


def _sc(evs) -> dict:
    return [d for e, d in evs if e == "meta"][-1]["self_check"]


def _result(evs) -> str:
    return [d for e, d in evs if e == "result"][-1]["content"]


def _answer(evs) -> str:
    """마지막 result 의 답 — 근거 블록(\\n\\n---) 앞까지."""
    return _result(evs).split("\n\n---", 1)[0]


def _head(body: str, i: int) -> str:
    """검토자 입력에서 i 번째 결과의 머리 줄('─ 결과 i …')."""
    return next(ln for ln in body.splitlines() if ln.startswith(f"─ 결과 {i} "))


def _run_req(monkeypatch, inner, reviewer, req):
    """_run(tests/test_chat_self_check.py)과 같다 — 이력이 든 요청을 그대로 넘긴다(_run 은 이력을 비운다)."""
    monkeypatch.setattr(a, "_agent_stream_turn", inner)
    monkeypatch.setattr(a.app.state, "chat_llm", reviewer, raising=False)

    async def go():
        return [c async for c in a._self_checked_turn(a.app, req)]
    return _events(asyncio.run(go()))


def _mcp_tool(name: str, text: str):
    """운영 꼴 — MCP 어댑터 도구는 (내용 블록 목록, 아티팩트) 둘을 돌려준다(content_and_artifact, tests/test_hub_fold_stats.py 와 같다)."""
    async def call(**_kw):
        return [{"type": "text", "text": text}], None
    return a._prep_tool(StructuredTool(name=name, description="d", coroutine=call, response_format="content_and_artifact",
                                       args_schema={"type": "object", "properties": {}}),
                        arg_defaults=a._CHAT_ARG_DEFAULTS, chat=True)


class _CallingAgent:
    """가짜 에이전트(tests/test_chat_self_check.py 의 _ToolAgent 꼴) — on_tool_start 다음에 진짜 래퍼 도구(_prep_tool)를 불러 그 글로
    on_tool_end 를 내고 답을 흘린다. 도구 코루틴이 진짜 capped 를 지나므로 원문 붙잡기 · 접기 · 자르기가 운영 꼴 그대로 돈다(설계 E1)."""

    def __init__(self, tool, answer: str):
        self.tool, self.answer = tool, answer

    async def astream_events(self, inputs, version=None, config=None):
        yield {"event": "on_tool_start", "name": self.tool.name, "run_id": "c1", "data": {"input": {}}}
        out = await self.tool.ainvoke({})
        yield {"event": "on_tool_end", "name": self.tool.name, "run_id": "c1", "data": {"output": out, "input": {}}}
        yield {"event": "on_chat_model_stream", "data": {"chunk": types.SimpleNamespace(content=self.answer)}}

    async def ainvoke(self, payload, config=None):
        raise AssertionError("답이 있는 턴은 구제 호출을 하지 않는다")


def _use_agent(monkeypatch, **env):
    """OT-04 꼴 턴의 꾸밈 — 관문 손잡이를 지운 뒤 env 를 걸고, 모델 사본 상한 200자 · 앱 캐시 비움 · 가짜 에이전트가 list_models 를 부른다."""
    for k in ("CHAT_SELF_CHECK_GATE", "CHAT_SELF_CHECK_RAW_CHARS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(a, "TOOL_RESULT_MAX", 200)
    monkeypatch.setattr(a.app.state, "chat_llm_cfg", None, raising=False)
    _cache(monkeypatch, {}, {})
    agent = _CallingAgent(_tool_of("list_models", _OT04_RAW), "개발모델은 34개입니다.")

    async def built(*_a, **_k):
        return agent
    monkeypatch.setattr(a, "_agent_for", built)


def _req(message: str = "개발모델은 몇 개야?", history=()) -> "a.ChatRequest":
    return a.ChatRequest(message=message, groups=["portal-admin"], history=list(history))


# ── G — 수 뽑기(sc_gate.num_tokens, 설계 6.1) ────────────────────────────────────────────────────────────────────────
def test_HA27_G1_수_뽑기는_쉼표_퍼센트_만_어림을_열쇠와_반올림_폭으로_정규화한다():
    toks = _g().num_tokens("개발모델 34개 · 13건 · 1,812초 · 12.5% · 1.2만 건 · 약 1,800초")
    assert [n.key for n in toks] == ["34", "13", "1812", "12.5%", "12000", "1800"]
    assert [n.tol for n in toks] == pytest.approx([0.5, 0.5, 0.5, 0.05, 500, 50]), "어림 표지(약)가 있으면 정수부 끝의 0 만큼 폭이 넓다"
    assert [bool(n.pct) for n in toks] == [False, False, False, True, False, False]


def test_HA27_G2_모델_코드_버전_IP_그림_주소_인용_목록_번호는_수가_아니다():
    assert list(_g().num_tokens("SM-S928B · Al6061-T6 · v1.2.3 · 10.0.0.1 · ![g](/agent/artifacts/a1b2.png) · (조회 결과 2)\n1. 첫째")) == []


def test_HA27_G3_날짜와_식별자_안의_수는_맥락을_단다():
    toks = _g().num_tokens("2026-10-30 · APP-GRAPH-0001 · 34개")
    assert [(n.value, n.ctx) for n in toks] == [(2026, "date"), (10, "date"), (30, "date"), (1, "id"), (34, "num")]


def test_HA27_G4_천_단위_쉼표는_셋씩일_때만이고_붙은_한글과_문장_끝_점은_수를_끊지_않는다():
    def keys(s):
        return [n.key for n in _g().num_tokens(s)]
    assert keys("1,2,3") == ["1", "2", "3"]
    assert keys("12,345,678") == ["12345678"]
    assert keys("모델34개") == ["34"], "앞이 한글이면 수다(evidence._NUM_TOK_RE 가 놓치던 꼴)"
    assert keys("값은 12.5.") == ["12.5"]


def test_HA27_G5_범위의_퍼센트는_앞_수에도_걸린다():
    assert [(n.key, bool(n.pct)) for n in _g().num_tokens("5~10%")] == [("5%", True), ("10%", True)]


def test_HA27_C13_UUID_와_확인_카드_번호의_조각은_수가_아니다():
    """design-integration C-13 — 숫자로 시작하는 UUID 의 첫 묶음(0 ~ 99)이 작은 정수로 새지 않게 가림에 UUID 꼴과 cf-[0-9a-f]{12} 를 더한다."""
    def keys(s):
        return [n.key for n in _g().num_tokens(s)]
    assert keys("3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b") == []
    assert keys("cf-0123456789ab") == []
    assert keys("id 3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b 는 다른 허브 것 · 불량 3건") == ["3"], "가림이 이웃 수까지 먹지 않는다"


# ── I — 원문 색인 · 출처 판정(RawIndex · sourced · derivable, 설계 6.2 · 6.3) ───────────────────────────────────────────────
def test_HA27_I1_JSON_수_값과_배열_길이는_강한_수_글_속_수는_약한_수다():
    idx = _idx('{"date": "2026-10-13", "total": 34, "rows": [1, 2, 3]}')
    assert _has(idx, "34", strong=True)
    assert not _has(idx, "13", strong=True), "날짜 글 속의 13 은 약한 수다"
    assert _has(idx, "13")
    assert _has(idx, "3", strong=True), "배열 길이 · 값"
    assert _has(idx, "2026")


def test_HA27_I2_반올림_폭과_퍼센트():
    idx = _idx('{"t": 1812.4, "r": 0.125, "s": 33.6}')
    assert _has(idx, "1,812초") and _has(idx, "12.5%"), "12.5% 는 원문 0.125 와도 견준다"
    assert _has(idx, "34"), "33.6 이 34 의 폭(0.5) 안"
    assert not _has(idx, "33")
    assert not _has(idx, "12.50"), "퍼센트가 아니면 0.125 와 견주지 않는다"


def test_HA27_I3_기준_값끼리의_합_차_비율_곱만_셈으로_본다():
    g, empty, anchors = _g(), _idx(), [34.0, 13.0]
    for s, want in (("47", True), ("21", True), ("2.6", True), ("38.2%", True), ("442", True), ("99", False)):
        assert bool(g.sourced(_num(s), empty, anchors)) is want, s
    assert bool(g.derivable(_num("47"), [])) is False
    assert not any(bool(g.sourced(_num(s), empty, [])) for s in ("47", "21", "2.6", "38.2%", "442")), "기준 값이 없으면 셈이 없다"


def test_HA27_I4_원문_값을_뺀_것은_띄어쓰기와_상관없다():
    idx = _idx(_OT08_RAW)
    assert _values_removed(idx, "X-01 담당은 품질기술팀", "X-01 담당은 신뢰성그룹") == ["품질기술팀"]
    assert _values_removed(idx, "품질기술팀 담당", "담당 품질 기술팀") == [], "공백을 걷고 센다"


def test_HA27_I5_접은_배열의_길이는_세지_않는다():
    idx = _idx('[1, 2, "…[전체 300개 중 앞 2개만 실었다 — …]"]')
    assert _has(idx, "2", strong=True), "전제 — 수 값은 강한 수다"
    assert not _has(idx, "3", strong=True), "끝 원소가 접기 표지인 배열의 길이(3)는 가짜 개수다"


def test_HA27_I6_덧붙인_근거는_강한_수이고_색인은_partial_을_싣는다():
    assert _has(_idx(extra=["사람 발화 — 기준 4321 로 계산"]), "4321", strong=True)
    assert bool(_idx('{"a": 1}').partial) is False
    assert bool(_idx('{"a": 1}', partial=True).partial) is True, "그릇이 상한에 걸렸으면 색인도 partial(meta · /health 에 남긴다)"


# ── D — 쌍마다 결정(sc_gate.gate_patches, 설계 6.4 · 6.5) ────────────────────────────────────────────────────────────
def test_HA27_D1_OT04_원문_수를_다른_수로_바꾸는_쌍은_버린다():
    raw = {"total": 34, "page_size": 13, "items": [{"model": f"DM-{i:02d}"} for i in range(13)]}
    [(act, code, det)] = _gate([("개발모델은 34개", "개발모델은 13개")], raw)
    assert (act, code) == ("drop", "drop_sourced_number") and "34" in det, "바꾼 쪽(13)이 원문에 있어도 지운 쪽(34)이 원문에 있으면 버린다"


def test_HA27_D2_OT09_원문_수를_근거_없음으로_바꾸는_쌍은_버린다():
    [(act, code, _det)] = _gate([("기준은 20 이다", "기준은 (근거 없음 — 추정) 이다")], {"limit": 20})
    assert (act, code) == ("drop", "drop_sourced_number")


def test_HA27_D3_원문_수에_근거_없음_표시만_다는_쌍은_버리고_원문에_없으면_넣는다():
    pair = [("기준은 20 이다", "기준은 20(근거 없음 — 추정) 이다")]
    assert [x[:2] for x in _gate(pair, {"limit": 20})] == [("drop", "drop_marks_sourced")]
    assert [x[:2] for x in _gate(pair, {"limit": 25})] == [("keep", "keep")]


def test_HA27_D4_DW05_직전_기간_값을_지우는_쌍은_버린다():
    [(act, code, det)] = _gate([("직전 120 → 이번 150 (+30)", "이번 150")], {"prev": 120, "cur": 150})
    assert (act, code) == ("drop", "drop_sourced_number") and "120" in det


def test_HA27_D5_원문_값으로_고치는_쌍은_넣고_근거_없는_새_수는_짚는다():
    raw, draft = {"max_stress": 312.5, "sf": 1.8}, "최대 응력은 321.5 MPa 이고 안전율은 1.8 입니다."
    assert [x[:2] for x in _gate([("321.5 MPa", "312.5 MPa")], raw, draft=draft)] == [("keep", "keep")]
    [(act, code, det)] = _gate([("321.5 MPa", "318.0 MPa")], raw, draft=draft)
    assert (act, code) == ("demote", "demote_unsourced_number") and "318.0" in det


def test_HA27_D6_글_속에만_있는_작은_정수를_지우는_쌍은_버리지_않고_짚는다():
    [(act, code, det)] = _gate([("불량 3건", "불량 5건")], {"date": "2026-10-03"})
    assert (act, code) == ("demote", "demote_weak_source") and "3" in det


def test_HA27_D7_OT08_원문_값을_빼는_쌍은_짚고_값_관문을_끄면_넣는다():
    pair = [("X-01 담당은 품질기술팀", "X-01 담당은 신뢰성그룹")]
    [(act, code, det)] = _gate(pair, _OT08_RAW)
    assert (act, code) == ("demote", "demote_removes_value") and "품질기술팀" in det
    assert [x[:2] for x in _gate(pair, _OT08_RAW, values=False)] == [("keep", "keep")]


def test_HA27_D8_작은_정수의_셈은_그_줄의_기준_값만_쓴다():
    raw, draft = {"a": 34, "b": 13, "c": 20}, "모델 34개, 시험 13개.\n다른 줄 20개."
    assert [x[:2] for x in _gate([("시험 13개.", "시험 13개. 합계 47개.")], raw, draft=draft)] == [("keep", "keep")], "47 = 34 + 13(그 줄)"
    [(act, code, det)] = _gate([("다른 줄 20개.", "다른 줄 20개, 합계 47개.")], raw, draft=draft)
    assert (act, code) == ("demote", "demote_unsourced_number") and "47" in det, "다른 줄의 34 · 13 으로는 셈하지 않는다"


def test_HA27_D9_버린_쌍과_같은_수를_더하는_쌍도_짚는다():
    got = _gate([("모델 34개", "모델 13개"), ("입니다.", "입니다. 그중 13개가 시험 중입니다.")], {"total": 34, "page": 13})
    assert [x[:2] for x in got] == [("drop", "drop_sourced_number"), ("demote", "demote_coupled")], "반쪽만 넣으면 '34개 … 그중 13개' 로 어긋난다"


def test_HA27_D10_수도_값도_없는_고침은_넣는다():
    assert [x[:2] for x in _gate([("这个 기준", "이 기준")], {"x": 1})] == [("keep", "keep")]


def test_HA27_D11_원문에_없는_수는_지워도_된다():
    assert [x[:2] for x in _gate([("약 1,500건(추정)이다.", "")], {"x": 1})] == [("keep", "keep")]


# ── W — 원문 붙잡기(capped 의 R5 · 턴 그릇, 설계 4절) ─────────────────────────────────────────────────────────────────
def test_HA27_W1_상한에_잘린_결과도_원문은_그대로_붙잡는다(monkeypatch):
    _cache(monkeypatch, {}, {})
    monkeypatch.setattr(a, "TOOL_RESULT_MAX", 100)
    assert len(_OT04_RAW) > 1000 and _OT04_RAW.endswith('"total": 34}')
    for make in (_tool_of, _mcp_tool):        # 글을 돌려주는 도구 · 운영 꼴(내용 블록 목록 + 아티팩트)
        with _raw_box(_box()) as box:
            out = asyncio.run(make("list_models", _OT04_RAW).ainvoke({}))
        assert a._CAP_MARK in out and '"total": 34' not in out, make.__name__
        assert box["texts"] == [_OT04_RAW], make.__name__


def test_HA27_W2_그릇이_없으면_오류_없이_돌고_아무것도_남기지_않는다(monkeypatch):
    _cache(monkeypatch, {}, {})
    monkeypatch.setattr(a, "TOOL_RESULT_MAX", 100)
    assert a._turn_sc_raw.get() is None, "턴 밖(심의 · 카탈로그)은 그릇이 없다"
    out = asyncio.run(_tool_of("list_models", _OT04_RAW).ainvoke({}))
    assert a._CAP_MARK in out
    a._sc_keep_raw("그릇 없이 부른 원문")
    assert a._turn_sc_raw.get() is None, "그릇을 새로 세우지 않는다"


def test_HA27_W3_접기_전_원문을_붙잡는다(monkeypatch):
    _cache(monkeypatch, {}, {})
    big = json.dumps({"type": "failure_mode", "fields": [{"path": f"in.f{i}", "label": "x" * 400} for i in range(300)]})
    assert len(big) > a._HUB_BIG_CHARS
    with _raw_box(_box()) as box:
        out = asyncio.run(_tool_of("object_fields", big).ainvoke({}))
    assert out.startswith("[포털 챗이 허브 응답을 접었다")
    assert box["texts"] == [big]


def test_HA27_W4_실패한_호출과_막은_호출은_붙잡지_않는다(monkeypatch):
    _cache(monkeypatch, {}, {})

    async def boom(**_kw):
        raise RuntimeError("backend refused")
    bad = a._prep_tool(StructuredTool(name="list_models", description="d", coroutine=boom,
                                      args_schema={"type": "object", "properties": {}}),
                       arg_defaults=a._CHAT_ARG_DEFAULTS, chat=True)
    with _raw_box(_box()) as box:
        out = asyncio.run(bad.ainvoke({}))
    assert out.startswith(a._TOOL_FAIL_MARK) and box == _box(), "실패 글은 근거가 아니다"
    tok = a._turn_ids.set({1})
    try:
        with _raw_box(_box()) as box2:
            out2 = asyncio.run(_tool_of("get_model", '{"id": 999, "name": "남의 모델"}').ainvoke({"id": 999}))
    finally:
        a._turn_ids.reset(tok)
    assert a._PHANTOM_ID_MARK in out2 and box2 == _box(), "유령 ID 관문이 막은 호출은 부르지 않았다"


def test_HA27_W5_턴_합_상한을_넘으면_앞부분만_싣고_partial_이다(monkeypatch):
    _cache(monkeypatch, {}, {})
    monkeypatch.setenv("CHAT_SELF_CHECK_RAW_CHARS", "500")
    tool = _tool_of("list_models", "가" * 400)
    with _raw_box(_box()) as box:
        for _ in range(3):
            asyncio.run(tool.ainvoke({}))
    assert [len(t) for t in box["texts"]] == [400, 100], "셋째는 더하지 않는다"
    assert box["texts"][1] == "가" * 100 and box["partial"] is True and box["chars"] == 500


def test_HA27_W5b_기본_상한은_결과_하나_200만_자_턴_합_600만_자(monkeypatch):
    _cache(monkeypatch, {}, {})
    tool = _tool_of("list_models", "x" * 2_500_000)
    with _raw_box(_box()) as box:
        for _ in range(4):
            asyncio.run(tool.ainvoke({}))
    assert [len(t) for t in box["texts"]] == [2_000_000] * 3, "결과 하나는 앞 200만 자(_SC_RAW_ITEM) · 넷째는 턴 합 600만 자를 넘는다"
    assert box["partial"] is True and box["chars"] == 6_000_000


def _turn_box(monkeypatch, **env) -> dict:
    """진짜 _agent_stream_turn 한 턴에 자기검증 감싸개처럼 box 를 주고, 턴 끝에 받은 box 를 돌려준다."""
    _use_agent(monkeypatch, **env)
    box: dict = {}

    async def go():
        return [c async for c in a._agent_stream_turn(a.app, _req(), box)]
    asyncio.run(go())
    return box


def test_HA27_W6_턴_끝_box_에_원문이_가고_관문을_끄거나_상한이_0_이면_없다(monkeypatch):
    on = _turn_box(monkeypatch)
    assert on["raw"]["texts"] == [_OT04_RAW] and on["raw"]["partial"] is False
    assert a._CAP_MARK in on["outputs"][0] and '"total": 34' not in on["outputs"][0], "모델 사본은 잘린 그대로다"
    assert _turn_box(monkeypatch, CHAT_SELF_CHECK_GATE="0")["raw"] is None
    assert _turn_box(monkeypatch, CHAT_SELF_CHECK_RAW_CHARS="0")["raw"] is None


def test_HA27_B3_좁은_창의_근거_사본을_자르면_표지를_단다(monkeypatch):
    """설계 5.3 — _turn_out_add 의 조용한 자르기(txt[:item])를 _cut_mark 로. 넓은 창(item 0)은 그대로다."""
    txt = "가" * 130_000
    monkeypatch.setattr(a, "_wide", lambda: False)
    got: list = []
    a._turn_out_add(got, txt)
    assert got == [a._cut_mark(txt, 120_000)] and got[0].endswith("…[130,000자 중 앞 120,000자]")
    monkeypatch.setattr(a, "_wide", lambda: True)
    wide: list = []
    a._turn_out_add(wide, txt)
    assert wide == [txt]


# ── M — 검토자 입력(결과마다 몫 · 잘림 머리 · 지시 · [코드 대조] 거르기, 설계 5절) ───────────────────────────────────────────────
def test_HA27_M1_결과마다_몫을_나눠_작은_결과가_큰_결과_때문에_빠지지_않는다():
    got = [tuple(x) for x in a._sc_fit_results(["가" * 200_000, "끝 결과 34"], 3_000)]
    assert len(got) == 2
    (first, cut), second = got
    assert bool(cut) and "자 중 앞" in first and a._est_tokens_all(first) <= 3_000
    assert second == ("끝 결과 34", False)
    assert [tuple(x) for x in a._sc_fit_results(["작은 결과 1", "작은 결과 2"], 3_000)] == [("작은 결과 1", False), ("작은 결과 2", False)], \
        "합이 몫 안이면 그대로"
    two = [tuple(x) for x in a._sc_fit_results(["가" * 100_000, "나" * 100_000, "작은 결과"], 3_000)]
    assert two[2] == ("작은 결과", False) and two[0][1] and two[1][1]
    sizes = [a._est_tokens_all(t) for t, _c in two[:2]]
    assert all(1_000 <= s <= 1_500 for s in sizes), ("넘치는 결과끼리는 남은 몫을 나눠 받는다(물 채우기)", sizes)


def test_HA27_M2_도구_결과_상한에_잘린_결과는_머리에_잘림을_밝히고_뒤_결과도_실린다(monkeypatch):
    monkeypatch.setattr(a, "_model_context_tokens", lambda: 16_000)
    big = a._cap("측정값 행 " * 50_000, 100_000)
    assert a._CAP_MARK in big
    body = a._self_check_messages(_req("측정값은?"), "작은 결과는 34 입니다.", [("t1", ""), ("t2", "")], [big, "작은 결과 34"])[1].content
    assert "─ 결과 1 (잘림 — 도구 결과 상한" in body
    assert "─ 결과 2 ─\n작은 결과 34" in body, "종전엔 앞 결과가 몫을 다 먹으면 뒤 결과가 통째로 빠졌다"


def test_HA27_M3_허브_접기와_근거_몫_잘림도_머리에_밝힌다():
    folded = ("[이 결과는 X 앱의 object_fields 이다]\n[포털 챗이 허브 응답을 접었다 — 긴 목록 1곳에서 항목 250건 뺐다 · 원문 120,000자]\n"
              + json.dumps({"fields": [{"path": "in.f1"}]}))
    clipped = a._cut_mark("근거 행 " * 300, 500)            # 좁은 창 근거 사본 꼴(5.3) — 끝이 ' …[N자 중 앞 M자]'
    body = a._self_check_messages(_req("필드는?"), "필드는 34 개입니다.", [("object_fields", ""), ("q", ""), ("r", "")],
                                  [folded, clipped, "작은 결과 34"])[1].content
    h1, h2, h3 = (_head(body, i) for i in (1, 2, 3))
    assert "(잘림" in h1 and "허브 응답 접기" in h1, h1
    assert "(잘림" in h2 and "근거 몫" in h2, h2
    assert h3 == "─ 결과 3 ─", h3


def test_HA27_M4_지시에_짚은_점_잘림_서버_대조_줄이_실리고_관문을_끄면_대조_줄만_빠진다(monkeypatch):
    """감싸개를 지나 검토자에게 실제로 간 글로 본다 — 관문 줄을 싣는 조건(손잡이 · 도구 턴)이 운영 길 그대로 돈다."""
    draft = "최대 응력은 321.5 MPa 입니다."

    def ask(**inner_kw) -> str:
        rv = _Reviewer(_OK)
        _run(monkeypatch, _inner(draft, **inner_kw), rv)
        return rv.calls[0][0][1].content
    tool_turn = {"calls": [("query_stress", "part=A")], "outputs": ["max 312.5 MPa"]}
    body = ask(**tool_turn)
    for must in ("[짚은 점]", "'(잘림' 이 붙은 결과", _GATE_LINE, "[짚은 점] 에만 적어라", "보이는 항목을 세어 고치지 마라",
                 "같은 결과의 그 항목 행에서 확인한 것만", "고친 답 전체를 다시 쓰지 마라", "'(근거 없음 — 추정)' 으로 표시하라"):
        assert must in body, must
    monkeypatch.setenv("CHAT_SELF_CHECK_GATE", "0")
    off = ask(**tool_turn)
    assert _GATE_LINE not in off, "관문을 끄면 사실이 아니라 싣지 않는다"
    assert "'(잘림' 이 붙은 결과" in off and "[짚은 점]" in off, "잘림 지시 · [짚은 점] 은 관문을 꺼도 그대로다(10절)"
    monkeypatch.delenv("CHAT_SELF_CHECK_GATE")
    bare = ask()                                     # 도구 없는 턴 — ② 를 통째로 바꿔 끼운다
    assert "'(잘림' 이 붙은 결과" not in bare and _GATE_LINE not in bare
    assert "고친 답 전체를 다시 쓰지 마라" in bare and "[짚은 점]" in bare


def test_HA27_M5_코드_대조는_원문에_있는_수를_빼고_idx_가_없으면_종전이다():
    req, calls, outs = _req("응력은?"), [("query_stress", "part=A")], ["응력 결과는 상한에서 잘렸다"]
    assert "`321.5`" in a._self_check_messages(req, "응력은 321.5 MPa", calls, outs)[1].content
    assert "`321.5`" in a._self_check_messages(req, "응력은 321.5 MPa", calls, outs, idx=None)[1].content
    with_idx = a._self_check_messages(req, "응력은 321.5 MPa", calls, outs, idx=_idx({"v": 321.5}))[1].content
    assert "`321.5`" not in with_idx, "원문에는 있는데 사본에서 잘린 수를 '출처 없는 수' 로 넘기지 않는다"


def test_HA27_M5b_코드_대조는_원문_값의_합도_뺀다():
    req, calls, outs = _req("응력은?"), [("query_stress", "")], ["A 312.5 MPa, B 410.25 MPa"]
    draft = "A 는 312.5 MPa, B 는 410.25 MPa, 둘의 합은 722.75 MPa 입니다."
    assert "`722.75`" in a._self_check_messages(req, draft, calls, outs)[1].content
    assert "`722.75`" not in a._self_check_messages(req, draft, calls, outs, idx=_idx(*outs))[1].content


# ── P — 검토자 답 읽기([짚은 점], 설계 5.6) ────────────────────────────────────────────────────────────────────────
def test_HA27_P1_짚은_점은_고친_점에_섞이지_않는다():
    out = "[판정] 고침\n[고친 점]\n- a\n[짚은 점]\n- b\n- 없음\n[바꿀 곳]\n[원문]\nx\n[새 글]\ny"
    assert a._self_check_parse(out) == ("fixed", ["a"], [("x", "y")])
    assert a._self_check_flags(out) == ["b"]


def test_HA27_P2_바꿀_곳_뒤에_온_짚은_점은_새_글에_섞이지_않는다():
    out = "[판정] 고침\n[고친 점]\n- a\n[바꿀 곳]\n[원문]\nx\n[새 글]\ny\n[짚은 점]\n- b"
    assert a._self_check_parse(out) == ("fixed", ["a"], [("x", "y")])
    assert a._self_check_flags(out) == ["b"]


def test_HA27_P3_짚은_점은_20개_400자까지():
    many = "[판정] 고칠 것 없음\n[짚은 점]\n" + "\n".join(f"- 의심 {i}" for i in range(25))
    assert a._self_check_flags(many) == [f"의심 {i}" for i in range(20)]
    long = a._self_check_flags("[판정] 고칠 것 없음\n[짚은 점]\n- " + "가" * 1000)
    assert len(long) == 1 and 0 < len(long[0]) <= 400


# ── J — 판정 · 상태(_self_check_judge, 설계 6.6 · 6.7 · 8.1) ─────────────────────────────────────────────────────────────
def test_HA27_J1_관문_없이_부르면_종전과_글자까지_같다():
    """기존 test_가드는_… 의 기대를 새 이름 칸(idx=None · flags=())을 주고 그대로 — 빈 칸을 더하지 않는다."""
    def judge(*x):
        return a._self_check_judge(*x, idx=None, flags=())
    draft = "결론은 이렇다. " * 30 + "끝 문장은 A 다.\n![응력](/artifacts/a.png)"
    good = [("끝 문장은 A 다.", "끝 문장은 B 다.")]
    assert judge(draft, None, [], [], False) == (draft, {"status": "unreadable", "reason": "검토자 답을 읽지 못했다 — 초안을 그대로 냈다"})
    assert judge(draft, "ok", [], good, False) == (draft, {"status": "ok"})
    assert judge(draft, "fixed", ["x"], good, False) == (draft.replace("A 다.", "B 다."), {"status": "fixed", "fixes": ["x"]})
    for patches, cut, why in ((good, True, "끊겼다"), (None, False, "꼴을 읽지 못했다"), ([], False, "바꿀 곳이 없다"),
                              ([("없는 글", "x")], False, "찾지 못했다"), ([("결론은 이렇다.", "x")], False, "30번 나온다"),
                              ([("", "x")], False, "원문이 비었다"), ([("끝 문장은 A", "x"), ("A 다.", "y")], False, "겹친다"),
                              ([("결론은 이렇다. " * 30, "")], False, "지나치게 짧다"),
                              ([("![응력](/artifacts/a.png)", "")], False, "그림 1개를 잃었다")):
        final, check = judge(draft, "fixed", ["x"], patches, cut)
        assert final == draft and check["status"] == "suspect" and why in check["reason"] and check["fixes"] == ["x"], (why, check)
        assert set(check) == {"status", "reason", "fixes"}, (why, check)
    assert judge(draft, "fixed", ["x"], [("끝 문장은 A 다.", "끝 문장은 A 다.")], False) == (draft, {"status": "ok"})


def test_HA27_J2_버림만_남으면_ok_이고_초안에_dropped_와_gate_를_싣는다():
    draft = "개발모델은 34개입니다. 끝."
    final, check = a._self_check_judge(draft, "fixed", ["34→13"], [("개발모델은 34개", "개발모델은 13개")], False,
                                       idx=_idx({"total": 34}))
    assert final == draft and check["status"] == "ok", check
    assert len(check["dropped"]) == 1 and "34" in check["dropped"][0] and "버림" in check["dropped"][0], check
    assert check["gate"]["drop_sourced_number"] == 1
    assert "flags" not in check, "빈 칸은 싣지 않는다"


def test_HA27_J3_짚음만_남으면_flagged_이고_초안을_낸다():
    draft = "최대 321.5 MPa 이다."
    final, check = a._self_check_judge(draft, "fixed", ["321.5 → 318.0"], [("321.5 MPa", "318.0 MPa")], False,
                                       idx=_idx({"max": 312.5}))
    assert final == draft and check["status"] == "flagged", check
    assert check["fixes"] == check["flags"] and "318.0" in check["flags"][0], "flagged 의 fixes 는 flags 와 같다(옛 화면 호환)"
    assert str(check.get("reason") or "").strip()


def test_HA27_J4_넣음과_짚음이_섞이면_넣은_것만_고치고_짚은_것은_flags_에():
    draft = "최대 321.5 MPa 이다. 결론은 위험하다."
    final, check = a._self_check_judge(draft, "fixed", ["a", "b"],
                                       [("321.5 MPa", "312.5 MPa"), ("위험하다.", "위험하다. 안전율은 2.4 이다.")], False,
                                       idx=_idx({"max": 312.5}))
    assert final == "최대 312.5 MPa 이다. 결론은 위험하다."
    assert check["status"] == "fixed" and check["fixes"] == ["a"], check
    assert check["flags"][0].startswith("b — 반영 안 함"), check["flags"]


def test_HA27_J4b_고친_점_줄_수가_쌍_수와_다르면_쌍의_설명은_코드가_만든다():
    """6.7 — '{옛 글 60자}' → '{새 글 60자 또는 (지움)}'. 검토자의 [고친 점] 한 줄을 어느 쌍에 붙일지 모르면 쓰지 않는다."""
    draft = "최대 321.5 MPa 이다. 결론은 위험하다."
    final, check = a._self_check_judge(draft, "fixed", ["둘을 한 줄로 적은 고친 점"],
                                       [("321.5 MPa", "312.5 MPa"), ("위험하다.", "위험하다. 안전율은 2.4 이다.")], False,
                                       idx=_idx({"max": 312.5}))
    assert final == "최대 312.5 MPa 이다. 결론은 위험하다." and check["status"] == "fixed", check
    assert len(check["fixes"]) == 1 and "321.5 MPa" in check["fixes"][0] and "312.5 MPa" in check["fixes"][0], check["fixes"]
    assert "안전율은 2.4" in check["flags"][0] and "— 반영 안 함" in check["flags"][0], check["flags"]
    # 지우는 쌍 — 수가 없는 문장이라야 값 규칙(4)까지 간다(X-01 처럼 원문 글 속 수가 든 문장을 지우면 규칙 1 이 먼저 버린다)
    draft2 = "담당 부서는 품질기술팀 입니다. 끝 문장."
    final2, check2 = a._self_check_judge(draft2, "fixed", [], [("담당 부서는 품질기술팀 입니다.", "")], False, idx=_idx(_OT08_RAW))
    assert final2 == draft2 and check2["status"] == "flagged" and "(지움)" in check2["flags"][0], check2


def test_HA27_J5_판정_ok_와_짚은_점이면_flagged():
    draft = "최대 응력은 312.5 MPa 이다."
    final, check = a._self_check_judge(draft, "ok", [], [], False, flags=["의심"])
    assert final == draft and check["status"] == "flagged" and check["fixes"] == ["의심"] and check["flags"] == ["의심"], check


def test_HA27_J6_넣을_쌍을_초안에서_못_찾으면_suspect_이고_관문의_flags_도_싣는다():
    draft = "최대 321.5 MPa 이다. 결론은 위험하다."
    final, check = a._self_check_judge(draft, "fixed", ["a", "b"],
                                       [("없는 글 321.5 MPa", "없는 글 312.5 MPa"), ("위험하다.", "위험하다. 안전율은 2.4 이다.")],
                                       False, idx=_idx({"max": 312.5}))
    assert final == draft and check["status"] == "suspect" and "찾지 못했다" in check["reason"], check
    assert check.get("flags") and "2.4" in check["flags"][0], check


def test_HA27_J6b_suspect_의_고친_점에서_버린_쌍의_줄은_뺀다():
    draft = "개발모델은 34개입니다. 끝."
    final, check = a._self_check_judge(draft, "fixed", ["a", "b"], [("개발모델은 34개", "개발모델은 13개"), ("없는 글", "x")], False,
                                       idx=_idx({"total": 34}))
    assert final == draft and check["status"] == "suspect" and "찾지 못했다" in check["reason"], check
    assert check["fixes"] == ["b"], "8.1 — suspect 는 지금처럼 [고친 점](버린 쌍의 줄은 뺀다)"
    assert len(check["dropped"]) == 1 and "34" in check["dropped"][0], check


def test_HA27_J7_dropped_와_flags_는_20줄_줄마다_300자까지():
    drops = [(f"값은 {100 + i} 이다", f"값은 {300 + i} 이다") for i in range(25)]
    final, check = a._self_check_judge("초안", "fixed", [], drops, False, idx=_idx({"vals": list(range(100, 125))}))
    assert final == "초안" and check["status"] == "ok" and len(check["dropped"]) == 20, check
    assert check["gate"]["drop_sourced_number"] == 25, "줄은 20개까지지만 계수는 전부 센다"
    demotes = [(f"항목{i} 의 값", f"항목{i} 의 값 {i}.75") for i in range(25)]
    long_fixes = [f"고친 점 {i} — " + "설명 " * 120 for i in range(25)]          # 설명 줄 하나가 300자를 넘는다
    final, check = a._self_check_judge("초안", "fixed", long_fixes, demotes, False, idx=_idx({"x": 1}))
    assert final == "초안" and check["status"] == "flagged" and len(check["flags"]) == 20, check
    assert all(len(x) <= 300 for x in check["flags"] + check["fixes"]), max(len(x) for x in check["flags"])


# ── E — 턴 끝까지(설계 E1 · E2, 10절 손잡이, 6.2 덧붙인 근거, design-integration C-13) ─────────────────────────────────────
def _ot04_turn(monkeypatch, **env):
    """설계 E1 — 진짜 래퍼를 지난 list_models(사본 상한 200자) → 초안 '개발모델은 34개입니다.' → 검토자 34 → 13."""
    _use_agent(monkeypatch, **env)
    rv = _Reviewer(_OT04_FIX)
    monkeypatch.setattr(a.app.state, "chat_llm", rv, raising=False)

    async def go():
        return [c async for c in a._agent_stream(a.app, _req())]
    return _events(asyncio.run(go())), rv


def test_HA27_E1_OT04_턴_끝까지_초안의_맞는_값이_나간다(monkeypatch):
    evs, rv = _ot04_turn(monkeypatch)
    assert _result(evs).startswith("개발모델은 34개입니다."), _result(evs)[:200]
    sc = _sc(evs)
    assert sc["status"] == "ok" and len(sc["dropped"]) == 1 and "34" in sc["dropped"][0], sc
    assert "flags" not in sc, sc
    h = a._sc_health()
    assert h["gate"]["counts"]["drop_sourced_number"] == 1
    assert h["counts"] == {"ok": 1} and h["fixed_ratio"] == 0.0, "턴 상태는 관문 뒤의 결과로 센다(8.4)"
    assert (h["gate"]["turns"], h["gate"]["reviewer_fixed"]) == (1, 1), "관문 앞의 검토자 버릇은 따로 센다"
    assert "(잘림 — 도구 결과 상한" in rv.calls[0][0][1].content, "검토자는 잘린 사본을 받았고 그 사실을 머리로 안다"


def test_HA27_E2_관문을_끄면_종전대로_고치고_잘림_머리는_남는다(monkeypatch):
    evs, rv = _ot04_turn(monkeypatch, CHAT_SELF_CHECK_GATE="0")
    assert _result(evs).startswith("개발모델은 13개입니다."), _result(evs)[:200]
    sc = _sc(evs)
    assert sc["status"] == "fixed" and "gate" not in sc, sc
    assert a._sc_health()["gate"]["turns"] == 0, "관문이 돌지 않은 턴은 gate 에 세지 않는다"
    ask = rv.calls[0][0][1].content
    assert "(잘림 — 도구 결과 상한" in ask, "결과 머리의 잘림 표시는 관문을 꺼도 그대로다(10절)"
    assert _GATE_LINE not in ask


_OT08_FIX = "[판정] 고침\n[고친 점]\n- X-01 의 부서를 바로잡음\n[바꿀 곳]\n[원문]\nX-01 담당은 품질기술팀\n[새 글]\nX-01 담당은 신뢰성그룹"


def test_HA27_E_OT08_다른_항목의_값으로_바꾸면_짚고_num_이나_0_이면_넣는다(monkeypatch):
    draft = "X-01 담당은 품질기술팀 입니다. X-02 도 같은 날 확인했습니다."

    def turn():
        return _inner(draft, calls=[("list_items", "")], outputs=[_OT08_RAW])
    evs = _run(monkeypatch, turn(), _Reviewer(_OT08_FIX))
    sc = _sc(evs)
    assert _answer(evs) == draft and sc["status"] == "flagged" and "품질기술팀" in sc["flags"][0], sc
    for mode in ("num", "0"):                          # num — 수 관문만(OT-08 줄 끔) · 0 — 관문 끔
        monkeypatch.setenv("CHAT_SELF_CHECK_GATE", mode)
        evs = _run(monkeypatch, turn(), _Reviewer(_OT08_FIX))
        assert _answer(evs) == draft.replace("품질기술팀", "신뢰성그룹") and _sc(evs)["status"] == "fixed", (mode, _sc(evs))


def test_HA27_E_짚기만_한_검토는_flagged_이고_관문을_꺼도_같다(monkeypatch):
    draft = "최대 응력은 312.5 MPa 입니다."
    note = "시험 조건(온도)이 조회 결과에 없다 — 사람이 확인"
    for mode in (None, "0"):
        if mode is not None:
            monkeypatch.setenv("CHAT_SELF_CHECK_GATE", mode)
        evs = _run(monkeypatch, _inner(draft, calls=[("query_stress", "part=A")], outputs=["max 312.5 MPa"]),
                   _Reviewer(f"[판정] 고칠 것 없음\n[고친 점]\n- 없음\n[짚은 점]\n- {note}"))
        sc = _sc(evs)
        assert _answer(evs) == draft and sc["status"] == "flagged" and sc["flags"] == [note] and sc["fixes"] == [note], (mode, sc)


# 6.2 덧붙인 근거 — 사람 발화 · 이력의 사람 발화 · 근거 블록이 있는 어시스턴트 발화 · 이력 evidence[*].result 는 근거이고, 근거 블록 없는
# 어시스턴트 발화는 근거가 아니다(지어낸 수가 다음 턴의 근거가 되는 세탁 길). 검토자가 4321 에 '근거 없음' 을 달면, 4321 이 근거에 있으면 버리고
# (drop_marks_sourced → ok, 초안 그대로) 없으면 넣는다(fixed).
_X_DRAFT = "불량 4321건의 주원인은 납땜 불량입니다."
_X_MARKED = "불량 4321건(근거 없음 — 추정)의 주원인은 납땜 불량입니다."
_X_FIX = "[판정] 고침\n[고친 점]\n- 4321 은 조회 결과에 없다\n[바꿀 곳]\n[원문]\n불량 4321건의\n[새 글]\n불량 4321건(근거 없음 — 추정)의"
_X_ASK = {"role": "user", "content": "불량 몇 건이야?"}


def _x_check(monkeypatch, req, want: str, *, calls=(("defect_causes", ""),), outputs=("원인 분포: 납땜 61%, 부품 22%",)) -> None:
    """want 'ok' — 근거에 있는 4321 에 '근거 없음' 을 단 쌍을 버렸다(초안 그대로). 'fixed' — 근거에 없어 넣었다. 어느 쪽이든 도구를 부른
    턴이라 관문이 돌았다(8.1 — gate 칸은 관문이 돈 턴만) — 관문 없이 다 넣던 종전 길로는 'fixed' 도 통과하지 않는다."""
    evs = _run_req(monkeypatch, _inner(_X_DRAFT, calls=list(calls), outputs=list(outputs)), _Reviewer(_X_FIX), req)
    ans, sc = _answer(evs), _sc(evs)
    assert "gate" in sc, sc
    if want == "ok":
        assert ans == _X_DRAFT and sc["status"] == "ok" and len(sc.get("dropped") or []) == 1, sc
    else:
        assert ans == _X_MARKED and sc["status"] == "fixed", sc


def test_HA27_X1_사람_발화의_수는_근거다(monkeypatch):
    _x_check(monkeypatch, _req("불량 4321건 기준으로 주원인을 알려 줘"), "ok")
    _x_check(monkeypatch, _req("불량의 주원인을 알려 줘"), "fixed")


@pytest.mark.parametrize("history, want", [
    ([_X_ASK, {"role": "assistant", "content": "불량은 4321건입니다."}], "fixed"),                       # 근거 블록 없는 답 — 세탁 길
    ([_X_ASK, {"role": "assistant", "content": f"불량은 4321건입니다.\n\n---\n{a._EVIDENCE_MARK}\n- `count_defects`"}], "ok"),
    ([_X_ASK, {"role": "assistant", "content": "불량은 4321건입니다.",
               "evidence": [{"tool": "count_defects", "args": "", "result": '{"count": 4321}'}]}], "ok"),   # 앞 턴의 도구 결과
    ([{"role": "user", "content": "불량 4321건 기준으로 봐 줘"}, {"role": "assistant", "content": "네."}], "ok"),   # 이력의 사람 발화
], ids=["근거_블록_없는_답", "근거_블록_있는_답", "이력_조회_결과", "이력_사람_발화"])
def test_HA27_X2_이력은_사람_발화_근거_블록_조회_결과만_근거다(monkeypatch, history, want):
    _x_check(monkeypatch, _req("그 불량의 주원인은?", history), want)


def test_HA27_X3_실패하거나_막힌_호출의_사본은_근거가_아니다(monkeypatch):
    """design-integration C-13 — _result_ok 가 거짓인 사본(✖ 실패 · 유령 ID 막음)은 색인에서 뺀다. 성공한 사본의 같은 수는 근거다(대조)."""
    calls = (("count_defects", ""), ("defect_causes", ""))
    _x_check(monkeypatch, _req("불량의 주원인을 알려 줘"), "ok", calls=calls, outputs=('{"count": 4321}', "원인 분포: 납땜 61%"))
    for bad in (f"{a._TOOL_FAIL_MARK} 도구 count_defects 호출 실패: limit 4321 을 넘는 요청이다",
                f"id=4321 {a._PHANTOM_ID_MARK}. 추측한 ID 로 부르면 다른 대상의 진짜 데이터가 돌아와 더 위험하다."):
        _x_check(monkeypatch, _req("불량의 주원인을 알려 줘"), "fixed", calls=calls, outputs=(bad, "원인 분포: 납땜 61%"))


# ── H — /health self_check(설계 8.3 · 8.4) ─────────────────────────────────────────────────────────────────────────
def test_HA27_H1_health_에_flagged_와_fixed_ratio_와_관문_합이_실린다():
    """_SC_STAT 는 gate 칸 없이 시작한다(fixture) — 기록이 칸을 스스로 세운다. flagged 는 실패가 아니라 last_error 에 싣지 않는다."""
    g_fixed = {"applied": 1, "drop_sourced_number": 1, "demote_unsourced_number": 0, "reviewer_flags": 0,
               "reviewer_fixed": True, "raw_chars": 1000, "raw_partial": False}
    g_ok = {"applied": 0, "drop_sourced_number": 2, "drop_marks_sourced": 1, "reviewer_flags": 0,
            "reviewer_fixed": True, "raw_chars": 2000, "raw_partial": True}
    g_flag = {"applied": 0, "demote_unsourced_number": 1, "demote_removes_value": 1, "reviewer_flags": 2,
              "reviewer_fixed": False, "raw_chars": 10, "raw_partial": False}
    a._sc_record({"status": "fixed", "fixes": ["x"], "ms": 100, "gate": g_fixed})
    a._sc_record({"status": "ok", "dropped": ["y — 버림: z"], "ms": 200, "gate": g_ok})
    a._sc_record({"status": "flagged", "flags": ["z"], "fixes": ["z"], "reason": "r", "ms": 300, "gate": g_flag})
    a._sc_record({"status": "skipped", "ms": 0})
    h = a._sc_health()
    assert h["counts"] == {"fixed": 1, "ok": 1, "flagged": 1, "skipped": 1}, h
    assert h["fixed_ratio"] == 0.333, "fixed ÷ (counts 합 − skipped), 셋째 자리 반올림"
    assert h["last_error"] is None
    g = h["gate"]
    assert (g["turns"], g["reviewer_fixed"], g["raw_partial"]) == (3, 2, 1), g
    want = {"applied": 1, "drop_sourced_number": 3, "demote_weak_source": 0, "drop_marks_sourced": 1, "demote_removes_value": 1,
            "demote_unsourced_number": 1, "demote_coupled": 0, "reviewer_flags": 2}
    assert {k: g["counts"].get(k, 0) for k in want} == want, g["counts"]


def test_HA27_H2_셀_턴이_없으면_fixed_ratio_는_None():
    assert a._sc_health()["fixed_ratio"] is None
    a._sc_record({"status": "skipped", "ms": 0})
    assert a._sc_health()["fixed_ratio"] is None, "건너뛴 것만 있으면 셀 턴이 없다"


def test_HA27_H3_gate_mode_는_손잡이의_지금_값이다(monkeypatch):
    assert a._sc_health()["gate"]["mode"] == "1", "기본은 수 + 값 관문"
    for v in ("num", "0", "1"):
        monkeypatch.setenv("CHAT_SELF_CHECK_GATE", v)
        assert a._sc_health()["gate"]["mode"] == v, "턴마다 읽는다 — 재기동 없이 바뀐다"


# ── L — 크기(설계 12절 L1, 선택 · 느슨하게) ──────────────────────────────────────────────────────────────────────────
def test_HA27_L1_600만_자_원문의_색인과_쌍_20개_판정이_10초_안에_끝난다():
    g = _g()
    rows = [{"id": 100_000 + i, "v": (100_000 + i) * 1.5, "name": f"항목{100_000 + i}"} for i in range(120_000)]
    raw = json.dumps({"rows": rows}, ensure_ascii=False)
    assert len(raw) > 5_500_000, len(raw)
    draft = "\n".join(f"항목{100_000 + i} 의 값은 {(100_000 + i) * 1.5 + 0.25} 이다." for i in range(20))
    pairs = [(f"값은 {(100_000 + i) * 1.5 + 0.25} 이다", f"값은 {(100_000 + i) * 1.5} 이다") for i in range(20)]
    t0 = time.monotonic()
    idx = g.RawIndex.build([raw])
    got = g.gate_patches(pairs, idx, draft=draft, message="값을 확인해 줘")
    took = time.monotonic() - t0
    assert len(got) == 20 and took < 10.0, f"{took:.1f}초"

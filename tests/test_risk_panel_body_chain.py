# 리스크 앱 러너가 실제로 만든 패널 본문을 포털 중계와 이 엔진이 끝까지 읽는지 1백만 토큰 창으로 돌려 본다(세 리포 사슬 — EX1)
#
# dev 박스의 모델 창(16K)으로는 좌석 쪽 수리를 실주행으로 볼 수 없다 — 지식카드 검색어·근거 항목·변경 원장 도구 고정·조회
# 지침이 좌석에 닿는지는 운영 창(GLM-5.2, 1,048,576 토큰)에서 본다. 그래서 LLM 없이 세 리포의 실제 코드를 잇는다.
#   1. 리스크 앱 — 형제 리포의 공장(tests/factories.portal_panel_body)이 합성 다건 변경 타깃에서 러너 한 바퀴를 실모듈로
#      돌려 포털에 보내는 본문을 낸다. 엔진 /health 가 알려 주는 근거 예산은 이 리포가 1백만 토큰 창에서 유도한 값이다.
#   2. 포털 — 그 본문을 포털의 실제 요청 모델(ChatRequest)과 중계 함수(_relay_stream)에 지나게 해 이 서버가 받는 요청을 얻는다.
#   3. 엔진 — 그 요청을 이 서버의 실제 /chat 처리기에 넣는다. LLM·게이트웨이 도구·조회 에이전트만 대역이다(자유 조회 한 턴
#      _free_gather_one 과 좌석 도구 추림 _tools_for_seat 은 진짜다).
# 앞 두 단계는 별도 프로세스다(두 리포 다 최상위 패키지 이름이 app 이다 — 각 리포의 venv 로 돈다).
#
# 형제 리포가 없거나 M1 이전 판(리스크 앱에 app/devseed.py 가 없다)이면 건너뛴다. 있는데 공장·포털 모델을 못 부르면
# 실패한다 — 건너뜀은 통과가 아니다. 다른 체크아웃을 보려면 HWAX_RISK_REPO · HWAX_PORTAL_REPO(리포 루트)를 주고, 그
# 체크아웃에 venv 가 없으면(git worktree) HWAX_RISK_PYTHON · HWAX_PORTAL_PYTHON 을 준다.
#
#   실행:  .venv/bin/python -m pytest tests/test_risk_panel_body_chain.py -q
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402

from test_delib_sealed import _SEAT_JSON  # noqa: E402
from test_free_gather import _Msg  # noqa: E402

_WINDOW = 1_048_576
_RISK = Path(os.environ.get("HWAX_RISK_REPO") or ROOT.parent / "HWAXRisk") / "backend"
_PORTAL = Path(os.environ.get("HWAX_PORTAL_REPO") or ROOT.parent / "HWAXPortal") / "backend"
# 1백만 토큰 창이면 하나도 나오면 안 되는 카드 — 러너 본문이 통째로 좌석에 가고 요청 값이 전부 읽혔다는 뜻이다.
_LOSS_CARDS = {"사전 근거 예산 초과", "사전 근거 항목 상한 초과", "사전 근거 본문 없음", "요청 값 상한 초과",
               "요청 값 해석 불가", "지정 좌석 키 없음"}

# 리스크 앱 리포에서 돈다 — 임시 저장소 위에서 공장이 만든 본문과 그 타깃의 diff id 를 찍는다.
_RISK_BODY = """
import json, sys
from pathlib import Path
from app.risk_store import RiskStore
from tests import factories
data = Path(sys.argv[1])
store = RiskStore(data / "risk_review.db")
store.open()
store.migrate()
made = factories.portal_panel_body(store, data, int(sys.argv[2]), engine_budget=int(sys.argv[3]))
print("RISK_BODY " + json.dumps({"body": made.body, "diff_id": made.target.diff_id,
                                 "events": len(made.target.diff["semantic"]["events"])}, ensure_ascii=False))
"""

# 포털 리포에서 돈다 — 본문을 실제 요청 모델로 읽고 실제 중계 함수가 에이전트 서버에 보내는 요청을 붙잡는다.
# 선언 안 된 키는 포털이 에러 없이 버리므로(model_dump(exclude_none=True)) 무엇이 떨어졌는지도 돌려받는다.
_PORTAL_RELAY = """
import asyncio, json, sys
from types import SimpleNamespace
import httpx
from app.agent.routes import ChatRequest, _relay_stream
body = json.load(sys.stdin)
request = ChatRequest.model_validate(body)
seen = {}

def answer(req):
    seen["payload"] = json.loads(req.content.decode())
    # 중계는 본문을 흘려 읽는다(aiter_raw) — 미리 읽힌 content 로 주면 StreamConsumed 다.
    return httpx.Response(200, stream=httpx.ByteStream(b"event: done\\ndata: {}\\n\\n"),
                          headers={"content-type": "text/event-stream"})

async def relay():
    principal = SimpleNamespace(subject="sub-synth", groups=["feat:deliberation"], email="syn@example.com")
    audit = SimpleNamespace(record=lambda **_k: None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        async for _chunk in _relay_stream(request, principal, audit, SimpleNamespace(agent_server_url="http://agent.test"),
                                          client):
            pass

asyncio.run(relay())
kept = request.delib_opts.model_dump(exclude_none=True)
print("PORTAL_PAYLOAD " + json.dumps({"payload": seen["payload"], "dropped": sorted(set(body["delib_opts"]) - set(kept))},
                                     ensure_ascii=False))
"""


def _python(env: str, backend: Path) -> str:
    return os.environ.get(env) or str(backend / ".venv" / "bin" / "python")


def _need_siblings() -> None:
    if not (_RISK / "app").is_dir() or not (_PORTAL / "app" / "agent" / "routes.py").is_file():
        pytest.skip(f"형제 리포 없음: {_RISK.parent} · {_PORTAL.parent} (HWAX_RISK_REPO · HWAX_PORTAL_REPO)")
    if not (_RISK / "app" / "devseed.py").is_file():
        pytest.skip(f"리스크 앱 체크아웃이 M1 이전 판이다(app/devseed.py 없음): {_RISK.parent} — M1 가지를 보려면 "
                    "HWAX_RISK_REPO 로 그 체크아웃을 가리킨다")


def _run_marked(cmd: list, *, cwd: Path, marker: str, env: dict, stdin: str = "") -> dict:
    r = subprocess.run(cmd, input=stdin, cwd=cwd, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env},
                       capture_output=True, text=True, timeout=600)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith(marker + " ")), None)
    # 형제 리포가 있는데 못 부르면 건너뛰지 않는다 — 공장 이름이 바뀌었거나 venv 가 깨졌다.
    assert r.returncode == 0 and line, (f"{cwd} 에서 {marker} 를 얻지 못했다(rc={r.returncode}) — "
                                        f"{(r.stderr or r.stdout).strip()[-1500:]}")
    return json.loads(line[len(marker) + 1:])


def _budget_at(window: int) -> int:
    """이 리포의 /health 가 그 창에서 알려 줄 근거 예산 — 리스크 앱이 브리프 크기를 여기서 정한다."""
    saved = dict(app._ctx_cache)
    try:
        app._ctx_cache["n"] = window
        d._evid_cache.clear()
        d._free_tok_cache.clear()
        return app.health()["evid_budget"]
    finally:
        app._ctx_cache.clear()
        app._ctx_cache.update(saved)
        d._evid_cache.clear()
        d._free_tok_cache.clear()


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """리스크 앱 본문 → 포털 중계 요청. 별도 프로세스 둘이라 이 파일에서 한 번만 만든다."""
    _need_siblings()
    budget = _budget_at(_WINDOW)
    data = tmp_path_factory.mktemp("risk-data")
    risk = _run_marked([_python("HWAX_RISK_PYTHON", _RISK), "-c", _RISK_BODY, str(data), "40", str(budget)],
                       cwd=_RISK, marker="RISK_BODY", env={"HWAXRISK_DATA_DIR": str(data), "HEAX_DATA_DIR": ""})
    portal = _run_marked([_python("HWAX_PORTAL_PYTHON", _PORTAL), "-c", _PORTAL_RELAY], cwd=_PORTAL,
                         marker="PORTAL_PAYLOAD", env={}, stdin=json.dumps(risk["body"], ensure_ascii=False))
    return SimpleNamespace(budget=budget, body=risk["body"], diff_id=risk["diff_id"], events=risk["events"],
                           payload=portal["payload"], dropped=portal["dropped"])


# ── 대역 ────────────────────────────────────────────────────────────────────
class _GwTool:
    """게이트웨이 도구 대역 — 스키마가 크다(도구당 약 2,000토큰). 후보 합이 좌석 도구 예산(1백만 토큰 창에서도 천장
    40,000토큰)을 넘어야 고정(_RISK_PIN_TOOLS)이 '변경 원장 도구가 좌석에 묶이는가' 를 가른다 — 다 들어가면 고정이 없어도
    묶여 이 시험이 고정을 보지 못한다."""

    coroutine = None      # 자유 조회 준비(_wrap_cached)가 보는 자리 — 없으면 그대로 둔다
    _ANSWERS = {"create_report_draft": '{"report_id": 7}'}

    def __init__(self, name, log):
        self.name, self._log = name, log
        self.description = f"{name} — 읽기 전용 조회." + " 인자 규칙과 함정을 적은 설명." * 40
        self.args_schema = {"type": "object", "properties": {
            f"arg_{i}": {"type": "string", "description": "이 인자에 넣는 값의 뜻과 단위." * 12} for i in range(20)}}

    async def ainvoke(self, args):
        self._log.append((self.name, args))
        return self._ANSWERS.get(self.name, '{"hits": []}')


class _Wire(dict):
    """좌석 자유 조회 후보가 되는 도구들(지식카드 · 물성 · 리스크 심사 유지 도구 · 러너가 지정한 도구)을 담고, 그 밖의
    이름도 '있다' 고 답한다(좌석 역할 복원 · 결정문 저장 같은 엔진 쪽 호출)."""

    def __init__(self, extra=()):
        self.calls = []
        names = ("agent_search", *d._MATERIAL_TOOLS, *d._RISK_KEEP_TOOLS, *extra)
        super().__init__({n: _GwTool(n, self.calls) for n in dict.fromkeys(names)})

    def __bool__(self):
        return True

    def __contains__(self, _name):
        return True

    def get(self, name, default=None):
        return super().get(name) or _GwTool(name, self.calls)


class _Lookup:
    """조회 에이전트 대역 — 받은 도구 이름과 프롬프트를 적고 '조회 불필요' 로 답한다."""

    def __init__(self, tools, log):
        self.tools, self._log = [t.name for t in tools], log

    async def astream(self, inp, config=None, stream_mode=None):
        msgs = inp["messages"]
        self._log.append({"tools": self.tools, "system": msgs[0][1], "human": msgs[1][1]})
        yield {"messages": [_Msg("ai", "조회 불필요")]}


def _engine(monkeypatch, payload, window):
    """포털이 보낸 요청을 이 서버의 /chat 처리기에 넣어 끝까지 돌린다. 반환 (이벤트, 도구 호출, LLM 프롬프트, 조회 턴)."""
    import langgraph.prebuilt

    wire, prompts, lookups = _Wire(payload["delib_opts"].get("tools") or ()), [], []

    async def _fake_tools(*_a, **_k):
        return wire

    async def _fake_llm(_llm, system, human):
        prompts.append((system, human))
        return "결정문 본문" if "엔지니어링 톤" in system else _SEAT_JSON

    monkeypatch.setitem(app._ctx_cache, "n", window)
    d._evid_cache.clear()
    d._free_tok_cache.clear()
    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", _fake_llm)
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})            # 게이트웨이 /tools-map 을 타지 않게
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, tools, **_k: _Lookup(tools, lookups))
    monkeypatch.setattr(app.app.state, "llm", object(), raising=False)
    monkeypatch.setattr(app.app.state, "delib_llm", None, raising=False)

    async def go():
        resp = await app.chat(app.ChatRequest(**payload))
        out = []
        async for chunk in resp.body_iterator:
            out.append(delib_jobs._parse_sse(chunk if isinstance(chunk, bytes) else chunk.encode()))
        return out

    try:
        events = asyncio.run(go())
    finally:
        d._evid_cache.clear()
        d._free_tok_cache.clear()
    errors = [data for ev, data in events if ev == "error"]
    assert not errors, f"심의가 오류로 끝났다 — {errors}"
    assert any(ev == "done" for ev, _ in events), "스트림이 done 없이 끝났다 — 하네스가 낡았다"
    return events, wire.calls, prompts, lookups


def _cards(events) -> list:
    return [data for ev, data in events if ev == "delib" and data.get("kind") == "evidence"]


# ── 시험 ────────────────────────────────────────────────────────────────────
def test_러너_본문이_포털을_지나_엔진이_전부_읽는다(chain):
    """포털이 키를 버리지 않았고, 엔진 _resolve_opts 가 그 요청을 읽지 못하거나 줄인 것이 없다."""
    assert chain.dropped == [], f"러너가 실은 손잡이를 포털이 말없이 버렸다 — {chain.dropped}"
    sent = chain.body["delib_opts"]
    assert chain.payload["delib_opts"] == sent, "포털 중계가 러너가 실은 값을 바꿨다"
    assert chain.payload["message"] == chain.body["message"]
    o = d._resolve_opts(chain.payload["delib_opts"])
    assert o.chair_template == "risk-review"
    assert o.knowledge_query and o.knowledge_query == sent["knowledge_query"], "러너의 지식카드 검색어가 엔진에 안 닿았다"
    assert (o.req_unread, o.req_cut) == ({}, {}), (o.req_unread, o.req_cut)
    assert (o.evidence_over, o.evidence_dropped_empty, o.seats_no_key) == (0, 0, None)
    assert len(o.evidence) == len(sent["evidence"])
    # 1백만 토큰 창의 근거 예산이면 변경 목록(E2)이 전부 실린다 — 러너가 자른 것이 없다.
    e2 = next(e["result"] for e in sent["evidence"] if e.get("key") == "E2")
    assert f"[전체 {chain.events}건 — 실림 {chain.events} · 생략 0 |" in e2, e2.splitlines()[-1]
    assert f"diff_id={chain.diff_id}" in chain.payload["message"], "질문에 그 타깃의 변경 비교 표식이 없다"


def test_러너_본문이_1백만_토큰_창에서_좌석과_조회_턴에_끝까지_닿는다(chain, monkeypatch):
    events, calls, prompts, lookups = _engine(monkeypatch, chain.payload, _WINDOW)
    assert any(ev == "delib" and data.get("kind") == "decision" for ev, data in events), "결정문까지 가지 못했다"

    # 1) 좌석에 줄 것을 줄이거나 못 읽었다는 카드가 하나도 없다.
    lost = [(c.get("source"), str(c.get("text", ""))[:200]) for c in _cards(events) if c.get("source") in _LOSS_CARDS]
    assert lost == [], f"1백만 토큰 창인데 러너 본문 일부가 좌석에 못 갔다 — {lost}"

    seats = [p["key"] for p in chain.body["delib_opts"]["personas"]]
    kq = chain.body["delib_opts"]["knowledge_query"]

    # 2) 지식카드는 좌석마다 호출자 검색어로 찾는다(화두가 아니다).
    asked = {a["agent_type"]: a["q"] for name, a in calls if name == "agent_search"}
    assert set(asked) == set(seats), f"지식카드를 물은 좌석이 러너 좌석과 다르다 — {sorted(asked)}"
    assert all(q == kq for q in asked.values()), {k: q[:80] for k, q in asked.items() if q != kq}

    # 3) 1라운드 좌석 프롬프트에 러너가 실은 근거 항목 표지가 전부 있다.
    labels = [f"[e:{i}|{e['key']}]" for i, e in enumerate(chain.body["delib_opts"]["evidence"], start=1)]
    first = [s + "\n" + h for s, h in prompts if "[챗 워크스페이스가 정리한 원천 데이터" in s + h]
    for key in seats:
        mine = [t for t in first if f"'{key}'" in t]
        assert mine, f"{key} 의 근거 실린 좌석 프롬프트가 없다"
        missing = [lb for lb in labels if lb not in mine[0]]
        assert missing == [], f"{key} 의 1라운드 프롬프트에 근거 항목이 빠졌다 — {missing}"

    # 4) 조회 턴 — 좌석에 묶인 도구 맨 앞에 변경 원장 도구가 서고, 프롬프트에 그 타깃의 diff_id 와 조회 지침이 있다.
    contract = [k for k in seats if d._dom_of(k) in d._RISK_SEAT_CONTRACT]
    assert contract, "계약표 도메인 좌석이 없다 — 조회 지침 단언이 아무것도 보지 않는다"
    for key in seats:
        mine = [lk for lk in lookups if f"'{key}' 전문가(" in lk["system"]]
        assert mine, f"{key} 의 조회 턴이 없다"
        for lk in mine:
            assert lk["tools"][:1] == ["risk_get_diff"], f"{key} 의 도구 맨 앞이 변경 원장 도구가 아니다 — {lk['tools'][:3]}"
            assert f"diff_id={chain.diff_id}" in lk["system"] + lk["human"], f"{key} 의 조회 턴에 diff_id 값이 없다"
            if key in contract:
                hint = d._risk_lookup_hint(d._dom_of(key), chain.payload["message"])
                assert hint.startswith(d._RISK_LOOKUP_NOTE)
                assert f"[이 심의의 조회 지침] {hint} " in lk["system"], f"{key} 의 조회 턴에 조회 지침·계약 줄이 없다"

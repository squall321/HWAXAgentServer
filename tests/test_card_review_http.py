# 카드 대조 라우트 넷(/card-review/limits·pack·plan·run)이 스트림을 열기 전에 422·409·403 으로 거절하고, 선언하지 않은 칸을 조용히 버리지 않으며, /health 에 카드 대조 칸을 싣는지 실제 앱으로 본다
#
# 선언하지 않은 칸이 조용히 사라지면(포털 ChatRequest 의 칸 유실 사고) 그 칸이 프롬프트에서 빠진 채 '검토함' 이 된다 —
# 그래서 요청 모델은 extra="forbid" 이고 422 unknown_fields 로 이름을 댄다. 섞인 판(expect)은 409 다. 권한은 포털이 먼저 막지만
# 에이전트 서버도 entitlements 로 한 번 더 막는다(None 이면 옛 호출로 통과). 카드 대조는 /health.delib_active 를 올리지 않는다
# (재기동을 막지 않는다 — C-25).
#
# 앱은 app.app 그대로다(라우터를 실제로 붙인 길). lifespan 은 돌리지 않고 delib_llm·창·관문만 시험마다 바꿔 끼운다.
#
#   실행:  .venv/bin/python -m pytest tests/test_card_review_http.py -q
import asyncio
import json
import random
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
from fakes.llm import Act, FakeLLM  # noqa: E402
from test_card_review_pack import Hub, _tools  # noqa: E402
from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: E402

PACK = _pack(random.Random(1), n=4, size=(300, 600))
CHECK = [c for c in PACK if c["checkable"]]


@pytest.fixture
def env(monkeypatch):
    """창 1,048,576(서버가 알려 준 값)·가짜 LLM·새 관문. 끝나면 전부 되돌린다."""
    llm = FakeLLM(["oracle"])
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a.app.state, "delib_llm", llm, raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    return llm


@pytest.fixture
def client(env):
    return TestClient(a.app)


def _verdict(n=3, **kw):
    cards = CHECK[:n]
    return _req("verdict", cards, _digest(PACK, [c["alias"] for c in cards]), unit=_unit(0), variant="fwd", **kw)


def _frames(text: str) -> list:
    return [delib_jobs._parse_sse(c.encode()) for c in text.split("\n\n") if c.strip()]


# ── limits ───────────────────────────────────────────────────────────────────
def test_limits_는_계약의_칸을_싣고_관문을_더한다(client):
    r = client.get("/card-review/limits")
    assert r.status_code == 200
    lim = r.json()
    want = {"api_rev", "kinds", "model", "ctx_tokens", "ctx_source", "ctx_retrying", "llm_max_tokens", "out_tokens",
            "p_hard", "unit_body_max", "verdict", "sweep", "offcard", "runnable", "unit_size", "manifest_item",
            "prompt_rev", "norm_rev", "limits_rev", "gate"}
    assert want <= set(lim) and "ctx_too_small" not in lim
    assert (lim["ctx_tokens"], lim["ctx_source"], lim["ctx_retrying"], lim["model"]) == (1_048_576, "server", False,
                                                                                         "fake-glm")
    assert lim["gate"] == {"limit": 8, "limit_reason": "normal", "active": 0}
    assert {"unit_body_max", "card_budget", "k", "k_out"} <= set(lim["verdict"])
    assert {"manifest_page_max", "card_budget", "units_per_page_max", "manifest_item_max"} <= set(lim["sweep"])
    assert {"p", "findings_max", "known_max"} <= set(lim["offcard"])
    # 관문 칸은 판(limits_rev)에 들지 않는다 — 도는 수가 바뀔 때마다 계획이 무르면 안 된다.
    assert lim["limits_rev"] == cr.limits(1_048_576, "server", 0, model="fake-glm")["limits_rev"]


def test_limits_창_출처와_좁은_창(client, monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"retry_at": 1e18})       # 방금 못 물었다 — 기본값으로 가며 다시 묻는 중
    lim = client.get("/card-review/limits").json()
    assert lim["ctx_retrying"] is True and lim["ctx_source"] in ("env", "fallback")
    monkeypatch.setattr(a, "_ctx_cache", {"n": 4_096, "src": "server"})
    lim = client.get("/card-review/limits").json()
    assert lim["ctx_too_small"]["need_ctx_tokens"] > 4_096 and not all(lim["runnable"].values())


def test_limits_는_delib_llm_의_출력_상한을_읽는다(client, env):
    env.max_tokens = 8_192
    lim = client.get("/card-review/limits").json()
    assert lim["llm_max_tokens"] == 8_192 and lim["out_tokens"] == 16_000


# ── run ──────────────────────────────────────────────────────────────────────
def test_run_프레임_순서(client):
    r = client.post("/card-review/run", json=_verdict(3))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    fr = _frames(r.text)
    assert [e for e, _ in fr] == ["status", "result", "done"]
    assert fr[1][1]["quality"]["expected"] == 3 and fr[1][1]["meta"]["ctx_tokens"] == 1_048_576


def test_run_자리가_없으면_error_done(client):
    gate = cr.gate_of(a.app)
    held = [gate.try_acquire("x") for _ in range(8)]
    fr = _frames(client.post("/card-review/run", json=_verdict(1)).text)
    assert [e for e, _ in fr] == ["error", "done"] and fr[0][1]["code"] == "review_busy"
    for t in held:
        gate.release(t, "ok")


@pytest.mark.parametrize("mutate,code,field", [
    (lambda b: b.update(extras={}), "unknown_fields", "extras"),
    (lambda b: b["unit"].update(evidences=[]), "unknown_fields", "unit.evidences"),
    # M3 에서 extra.search 를 선언했다(cr-offcard-2) — 안쪽 칸 이름이 어긋나면(q ≠ queries) 거절한다.
    (lambda b: b.update(extra={"search": {"q": "x"}}), "unknown_fields", "extra.search.q"),
    (lambda b: b["unit"]["lines"][0].update(kind="x"), "unknown_fields", "unit.lines.0.kind"),
    (lambda b: b["expert"].update(persona="x"), "unknown_fields", "expert.persona"),
    # 안쪽 항목(RB-4) — 칸 이름이 어긋나면 블록이 말없이 비던 자리.
    (lambda b: b["digest"][0].update(kind="faq"), "unknown_fields", "digest.0.kind"),
    (lambda b: b["unit"]["missing"][0].update(text="x"), "unknown_fields", "unit.missing.0.text"),
    (lambda b: b.update(extra={"known": [{"alias": "K01", "judgement": "OK", "reason": "x"}]}), "unknown_fields",
     "extra.known.0.reason"),
    (lambda b: b.update(extra={"mechanisms": [{"code": "m.a", "name": "x"}]}), "unknown_fields",
     "extra.mechanisms.0.name"),
    (lambda b: b.update(extra={"domains": [{"key": "mech", "code": "mech"}]}), "unknown_fields",
     "extra.domains.0.code"),
])
def test_선언하지_않은_칸은_이름을_대고_거절한다(client, env, mutate, code, field):
    body = _verdict(1)
    mutate(body)
    r = client.post("/card-review/run", json=body)
    assert r.status_code == 422 and r.json()["code"] == code and field in r.json()["fields"], r.json()
    assert env.calls == []


def test_review_payload_의_곁칸은_받는다(client):
    body = _verdict(1)
    body["unit"].update(unit_hash="u1", summary_sha="s", evidence_sha="e", evidence_status="ok",
                        input_gaps=[{"kind": "ecad"}])
    assert client.post("/card-review/run", json=body).status_code == 200


@pytest.mark.parametrize("mutate,code", [
    (lambda b: b.pop("expert"), "bad_request"),
    (lambda b: b["unit"]["lines"][0].pop("text"), "bad_request"),
    (lambda b: b.update(kind="cross"), "unsupported_kind"),
    (lambda b: b.update(kind="hunt"), "unsupported_kind"),
    (lambda b: b["unit"].update(kind="snap_unit"), "unsupported_unit_kind"),
    (lambda b: b["unit"].update(lines=[], context=[]), "empty_unit"),
    (lambda b: b.update(cards=CHECK[:9]), "deck_too_large"),
    (lambda b: b.update(variant="side"), "bad_request"),
    (lambda b: b["unit"].update(missing=[{"note": "종류가 없는 결측"}]), "bad_request"),
    (lambda b: b.update(extra={"mechanisms": [{"label": "코드가 없는 메커니즘"}]}), "bad_request"),
    (lambda b: b.update(digest=["K09"]), "bad_request"),
])
def test_스트림_전_422(client, env, mutate, code):
    body = _verdict(2)
    mutate(body)
    r = client.post("/card-review/run", json=body)
    assert r.status_code == 422 and r.json()["code"] == code, r.json()
    assert env.calls == []


def test_JSON_이_아니면_422(client):
    r = client.post("/card-review/run", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["code"] == "bad_request"


def test_창을_넘는_요청은_LLM_을_부르지_않고_over_budget(client, env):
    body = _verdict(1)
    lim = cr.limits(1_048_576, "server", 0)
    body["unit"]["lines"].append({"ref": "c:big", "text": "가" * (lim["p_hard"] + 10)})
    r = client.post("/card-review/run", json=body)
    j = r.json()
    assert r.status_code == 422 and j["code"] == "over_budget" and j["need"] > j["have"] == lim["p_hard"]
    assert env.calls == [] and cr.gate_of(a.app).totals["over_budget"] == 1


def test_창이_모자란_박스는_ctx_too_small(client, env, monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 4_096, "src": "server"})
    r = client.post("/card-review/run", json=_verdict(1))
    assert r.status_code == 422 and r.json()["code"] == "ctx_too_small" and r.json()["need_ctx_tokens"] > 4_096


def test_판이_섞이면_409(client, env):
    lim = client.get("/card-review/limits").json()
    ok = _verdict(1, )
    ok["expect"] = {"prompt_rev": lim["prompt_rev"]["verdict"], "limits_rev": lim["limits_rev"]}
    assert client.post("/card-review/run", json=ok).status_code == 200
    ok["expect"]["limits_rev"] = "00000000"
    r = client.post("/card-review/run", json=ok)
    assert r.status_code == 409 and r.json()["code"] == "rev_mismatch" and r.json()["limits_rev"] == lim["limits_rev"]


@pytest.mark.parametrize("path", ["/card-review/run", "/card-review/pack", "/card-review/plan"])
def test_권한이_없으면_403_이고_None_이면_옛_호출로_통과(client, env, path):
    body = {"run": _verdict(1), "pack": {"agent_key": "x"}, "plan": {"cards": [], "units": []}}[path.rsplit("/", 1)[1]]
    r = client.post(path, json={**body, "entitlements": ["feat:thinking"]})
    assert r.status_code == 403 and r.json() == {"code": "denied", "message": r.json()["message"],
                                                 "need": "feat:deliberation"}
    assert env.calls == []
    if path.endswith("run"):
        assert client.post(path, json={**body, "entitlements": None}).status_code == 200
        assert client.post(path, json={**body, "entitlements": ["feat:deliberation"]}).status_code == 200


def test_창을_모르는_동안은_판을_견주지_않고_503_ctx_unknown(client, env, monkeypatch):
    """RB-3 — 서버가 막 떴는데 LLM 이 잠시 안 닿으면 창 조회가 실패해 기본값(가정한 창)으로 가며 다시 묻는다(ctx_retrying).
    그 창으로 판을 셈하면 앱이 서버 창으로 짠 계획과 어긋나 409 rev_mismatch(계약 — 자동 재개 없는 잡 보류)가 됐다.
    실제 app._model_context_state 를 지나고 LLM 주소만 아무도 듣지 않는 포트로 둔다."""
    planned = client.get("/card-review/limits").json()                 # 앱이 계획을 짤 때 받은 판(서버 창 1M)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    monkeypatch.setattr(a, "VLLM_BASE_URL", f"http://127.0.0.1:{dead}/v1")
    monkeypatch.setattr(a, "_ctx_cache", {})
    monkeypatch.setattr(a, "_CTX_ENV_SET", False)
    monkeypatch.setattr(a, "_CTX_FALLBACK", 128_000)
    lim = client.get("/card-review/limits").json()
    assert (lim["ctx_retrying"], lim["ctx_source"]) == (True, "fallback") and lim["limits_rev"] != planned["limits_rev"]
    expect = {"prompt_rev": cr.PROMPT_REV["verdict"], "limits_rev": planned["limits_rev"]}
    cards = [{k: c[k] for k in ("alias", "record_id", "checkable", "chars")} for c in PACK]
    for path, body in (("/card-review/run", {**_verdict(), "expect": expect}), ("/card-review/run", _verdict()),
                       ("/card-review/plan", {"cards": cards, "units": [], "expect": {"limits_rev": expect["limits_rev"]}}),
                       ("/card-review/plan", {"cards": cards, "units": []})):
        r = client.post(path, json=body)
        out = r.json()
        assert r.status_code == 503 and out["code"] == "ctx_unknown", (path, r.status_code, out)
        assert 1 <= out["retry_after_s"] <= a._CTX_RETRY_S and (out["infra"], out["retryable"]) == (True, True)
    assert env.calls == []
    # 창이 돌아오면 같은 요청이 돈다.
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    assert client.post("/card-review/run", json={**_verdict(), "expect": expect}).status_code == 200


def test_출처가_굳은_fallback_은_예산을_셈하고_진짜_판_차이만_409(client, env, monkeypatch):
    """서버가 창 칸 없이 답했다(retrying 거짓) — 그 창이 이 박스의 창이다. 예산을 그대로 셈하고, 다른 창으로 짠 계획은 진짜
    판 차이(409 rev_mismatch)다."""
    planned = client.get("/card-review/limits").json()
    monkeypatch.setattr(a, "_ctx_cache", {"n": 128_000, "src": "fallback"})
    lim = client.get("/card-review/limits").json()
    assert (lim["ctx_retrying"], lim["ctx_source"], lim["ctx_tokens"]) == (False, "fallback", 128_000)
    expect = {"prompt_rev": cr.PROMPT_REV["verdict"], "limits_rev": planned["limits_rev"]}
    r = client.post("/card-review/run", json={**_verdict(), "expect": expect})
    assert r.status_code == 409 and r.json()["code"] == "rev_mismatch"
    assert client.post("/card-review/run", json=_verdict()).status_code == 200


# ── plan ─────────────────────────────────────────────────────────────────────
def test_plan_은_지금_판으로_묶는다(client):
    cards = [{k: c[k] for k in ("alias", "record_id", "checkable", "chars")} for c in PACK]
    units = [{"unit_id": f"u-{i}", "manifest_chars": 300, "body_chars": 2_000} for i in range(5)]
    r = client.post("/card-review/plan", json={"cards": cards, "units": units})
    out = r.json()
    lim = client.get("/card-review/limits").json()
    assert r.status_code == 200 and out["limits_rev"] == lim["limits_rev"]
    assert sum(len(d["aliases"]) for d in out["verdict_decks"]) == len(CHECK) and out["manifest_pages"] == [
        [u["unit_id"] for u in units]]
    r = client.post("/card-review/plan", json={"cards": cards, "units": units, "expect": {"limits_rev": "x"}})
    assert r.status_code == 409
    r = client.post("/card-review/plan", json={"cards": [{**cards[0], "char": 1}], "units": []})
    assert r.status_code == 422 and r.json()["code"] == "unknown_fields"


# ── pack ─────────────────────────────────────────────────────────────────────
def test_pack_은_사용자_자격으로만_만든다(client, monkeypatch):
    hub = Hub()
    seen = {}

    async def fake_load(_app, groups, *, user_email, user_pat):
        seen.update(groups=groups, user_email=user_email, user_pat=user_pat)
        if not user_pat:
            raise cr.PackError("credential_missing", 401, "토큰 없음")
        return _tools(hub)

    monkeypatch.setattr(cr, "load_pack_tools", fake_load)
    body = {"agent_key": "mech-test-seal", "groups": ["g"], "user_email": "u@x.com", "user_pat": "pat"}
    r = client.post("/card-review/pack", json=body)
    pack = r.json()
    assert r.status_code == 200 and pack["credential"] == "user" and pack["cards_total"] == 10
    assert seen == {"groups": ["g"], "user_email": "u@x.com", "user_pat": "pat"}
    r = client.post("/card-review/pack", json={**body, "user_pat": ""})
    assert r.status_code == 401 and r.json()["code"] == "credential_missing"
    r = client.post("/card-review/pack", json={**body, "agent_key": "no-such"})
    assert r.status_code == 404 and r.json()["code"] == "agent_not_found"
    r = client.post("/card-review/pack", json={**body, "agents": ["x"]})
    assert r.status_code == 422 and r.json()["code"] == "unknown_fields"


def test_팩_자리를_기한까지만_기다리고_429_review_busy(client, monkeypatch):
    """RB-8 — 팩 관문(동시 2)의 줄 기다림에 기한이 없어 셋째부터의 기다림이 포털 단발 한도(600초)에 들었다. 자리가 PACK_WAIT_S
    안에 나지 않으면 429 review_busy{retry_after_s}(기다림 부류 — 아무것도 세지 않는다)다."""
    hub = Hub()

    async def fake_load(_app, groups, *, user_email, user_pat):
        return _tools(hub)

    monkeypatch.setattr(cr, "load_pack_tools", fake_load)
    monkeypatch.setattr(cr, "PACK_WAIT_S", 0.2)
    monkeypatch.setattr(a.app.state, "card_review_pack_sem", asyncio.Semaphore(0), raising=False)   # 두 자리가 다 찼다
    body = {"agent_key": "mech-test-seal", "user_pat": "pat"}
    t0 = time.monotonic()
    r = client.post("/card-review/pack", json=body)
    out = r.json()
    assert r.status_code == 429 and out["code"] == "review_busy" and time.monotonic() - t0 < 5
    assert out["retry_after_s"] == cr.gate_of(a.app).retry_after_s and out["limit"] == cr.PACK_CONC
    assert (out["infra"], out["retryable"]) == (True, True) and hub.active == 0
    monkeypatch.setattr(a.app.state, "card_review_pack_sem", asyncio.Semaphore(cr.PACK_CONC), raising=False)
    r = client.post("/card-review/pack", json=body)
    assert r.status_code == 200 and a.app.state.card_review_pack_sem._value == cr.PACK_CONC, "자리를 돌려주지 않았다"


# ── /health ──────────────────────────────────────────────────────────────────
def test_health_에_카드_대조_칸이_있고_기존_칸은_그대로다(client, env):
    env.request_timeout = __import__("httpx").Timeout(1800, connect=10)
    env.max_retries = 1
    h = client.get("/health").json()
    assert {"status", "delib_active", "delib_queued", "model", "context_tokens", "evid_budget"} <= set(h)
    assert h["capabilities"] == ["card_review"] and h["review_active"] == 0
    c = h["card_review"]
    assert {"rev", "kinds", "limit", "limit_reason", "active", "timeout_s", "tries", "attempts_max", "request_worst_s",
            "prompt_rev", "totals", "last_error", "delib_human", "search_timeout_s"} <= set(c)
    assert c["kinds"] == list(cr.KINDS) and c["search_timeout_s"] == 60.0
    # 요청 최악 = (1 + 보충 2) × (2 시도 × 1800 + 8) + 긴 문서 검색 기한 60 = 10,884초(m2-wp3a 걸음 5 · M3 E3-5).
    assert (c["timeout_s"], c["tries"], c["attempts_max"], c["request_worst_s"]) == (1800, 2, 3, 10_884)
    assert set(c["totals"]) >= {"calls", "ok", "error", "busy", "truncated", "repaired", "over_budget",
                                "context_overflow", "tokens_in", "tokens_out", "tokens_unknown_calls"}


def test_health_한도가_꺼진_박스는_요청_최악이_None(client, env):
    assert client.get("/health").json()["card_review"]["request_worst_s"] is None


def test_도는_동안_review_active_는_오르고_delib_active_는_그대로다(env, monkeypatch):
    llm = FakeLLM([Act("slow", delay=0.3)])
    monkeypatch.setattr(a.app.state, "delib_llm", llm, raising=False)
    lim = cr.limits(1_048_576, "server", 0)
    prep = cr.prepare(_verdict(1), lim)
    seen = {}

    async def go():
        async def consume():
            return [c async for c in cr.run(a.app, prep)]

        t = asyncio.ensure_future(consume())
        await asyncio.sleep(0.1)
        seen["mid"] = a.health()
        await t

    before = a.health()
    asyncio.run(go())
    after = a.health()
    assert seen["mid"]["review_active"] == 1 and seen["mid"]["card_review"]["active"] == 1
    assert (seen["mid"]["delib_active"], seen["mid"]["delib_queued"]) == (before["delib_active"], before["delib_queued"])
    assert after["review_active"] == 0 and after["card_review"]["totals"]["ok"] == 1


def test_모듈을_못_붙이면_capabilities_가_비고_사유를_싣는다(monkeypatch):
    monkeypatch.setattr(a, "_card_review", None)
    monkeypatch.setattr(a, "_CARD_REVIEW_ERR", "ImportError: x")
    h = a._card_review_health()
    assert h == {"review_active": 0, "capabilities": [], "card_review": {"error": "ImportError: x"}}


def test_검증기는_app_없이_돈다():
    """리스크 앱 시험이 형제 리포에서 서브프로세스로 부르는 길 — app 을 import 하지 않는다."""
    import subprocess
    code = ("import json, sys; import card_review as cr; "
            "lim = cr.limits(1048576, 'server', 0); body = json.load(sys.stdin); "
            "print(json.dumps([cr.validate_run_request(body), cr.validate_run_request(body, lim), "
            "cr.validate_run_request({**body, 'extras': {}}), 'app' in sys.modules]))")
    r = subprocess.run([sys.executable, "-c", code], input=json.dumps(_verdict(1), ensure_ascii=False), cwd=str(ROOT),
                       env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    shape, full, bad, app_loaded = json.loads(r.stdout.strip().splitlines()[-1])
    assert shape == [] and full == [] and app_loaded is False
    assert bad[0]["status"] == 422 and bad[0]["code"] == "unknown_fields" and bad[0]["fields"] == ["extras"]


def test_포털이_덮어쓰는_신원_칸_넷은_세_POST_가_다_받는다(client, monkeypatch):
    """포털 중계는 run 만이 아니라 pack·plan 본문에도 신원 칸 넷을 덮어쓴다(D-28). 요청 모델이 extra="forbid" 라 선언이 빠지면
    포털을 거친 호출이 전부 422 unknown_fields 다."""
    ident = {"groups": ["g"], "user_email": "u@x.com", "user_pat": "pat", "entitlements": ["feat:deliberation"]}

    async def fake_load(*_a, **_k):
        return _tools(Hub())

    monkeypatch.setattr(cr, "load_pack_tools", fake_load)
    assert client.post("/card-review/run", json={**_verdict(1), **ident}).status_code == 200
    assert client.post("/card-review/pack", json={"agent_key": "mech-test-seal", **ident}).status_code == 200
    assert client.post("/card-review/plan", json={"cards": [], "units": [], **ident}).status_code == 200


def test_비우기_중이면_팩도_429_review_busy_draining_이고_조회하지_않는다(client, monkeypatch, tmp_path):
    """M3.md 4.5 — 비우기(scope all)면 새 run·pack 이 review_busy{reason:"draining", retry_after_s 60}(기다림)이다. 팩은
    게이트웨이에 한 건도 묻지 않는다. scope panels 는 막지 않는다."""
    hub = Hub()
    loads = []

    async def fake_load(_app, groups, *, user_email, user_pat):
        loads.append(1)
        return _tools(hub)

    monkeypatch.setattr(cr, "load_pack_tools", fake_load)
    monkeypatch.setenv("DRAIN_STATE_PATH", str(tmp_path / "drain.json"))
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    cr.gate_of(a.app).drain.set_on(scope="all", by="update-all", ttl_s=600)
    body = {"agent_key": "mech-test-seal", "user_pat": "pat"}
    r = client.post("/card-review/pack", json=body)
    out = r.json()
    assert r.status_code == 429 and (out["code"], out["reason"], out["retry_after_s"]) == ("review_busy", "draining", 60)
    assert loads == [] and (out["infra"], out["retryable"]) == (True, True)
    cr.gate_of(a.app).drain.set_on(scope="panels", by="update-all", ttl_s=600)
    assert client.post("/card-review/pack", json=body).status_code == 200 and loads == [1]

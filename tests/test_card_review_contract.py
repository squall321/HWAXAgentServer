# 카드 대조 계약 예제(limits·pack·plan·run 프레임·결과·오류·거절)를 실제 라우트와 가짜 LLM 대본으로 떠 tests/fixtures/card_review/contract/ 에 못박는다 — 포털 중계와 리스크 앱이 같은 바이트로 시험한다
#
# 예제는 손으로 쓰지 않는다(PLAN 9절 — 가짜가 실물의 산출로 만들어진다). 실제 앱(app.app)의 /card-review 라우트를 TestClient
# 로 부르고, LLM 자리에는 대본 LLM(tests/fakes/llm.py)을 끼운다. 대본 LLM 의 정답은 실제 build_prompt 가 렌더한 프롬프트에서
# 만든다. 카드는 지어낸 카드를 실제 생산자로 만든 기록이고(tests/fixtures/card_review/cards), 팩은 실제 build_pack 이 만든다.
#
# 시각·밀리초는 가린다(meta.llm_ms → 0, 팩 fetched_at → 0). ping 은 끈다(DELIB_HEARTBEAT_S=0 — ping 모양은 {idle_s, ts}).
# index.json 이 장면마다 요청 파일·대본·창·산출 파일을 잇는다.
#
# 리스크 앱은 형제 리포 경로(../HWAXAgentServer/tests/fixtures/card_review/contract/)로 읽는다. 엔진을 고쳐 예제가 달라지면
# 리스크 앱 수집 시험과 같이 보고 다시 뜬다.
#
#   다시 뜨기:  CARD_REVIEW_CONTRACT_UPDATE=1 .venv/bin/python -m pytest tests/test_card_review_contract.py -q
#   실행:       .venv/bin/python -m pytest tests/test_card_review_contract.py -q
import asyncio
import copy
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
from fakes.llm import Act, FakeLLM  # noqa: E402
from test_card_review_pack import KEY, Hub, _tools  # noqa: E402
from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "card_review" / "contract"
_UPDATE = os.environ.get("CARD_REVIEW_CONTRACT_UPDATE") == "1"
# 이 시험이 만드는 파일(머리 이름). 그 밖의 파일(정규화·인용 벡터·틀 잠금)은 다른 시험이 만든다.
MANAGED = ("index.json", "rejects.json", "limits_", "pack_", "plan_", "req_", "run_", "result_", "error_",
           "llm_transcript_")
MODEL = "fake-glm"
W1M, W16K = 1_048_576, 16_384

PACK = _pack(random.Random(1), n=4, size=(300, 600))
CHECK = [c for c in PACK if c["checkable"]]
MECHS = [{"code": "thermal.cte_mismatch", "label": "열팽창 불일치"}, {"code": "interface.seal_loss", "label": "실링 상실"}]
DOMAINS = [{"key": "mech", "label": "기구"}, {"key": "rel", "label": "신뢰성"}, {"key": "therm", "label": "열"}]


def _verdict_req(variant="fwd", n=8, unit=None):
    cards = CHECK[:1] if variant == "solo" else CHECK[:n]
    return _req("verdict", cards, _digest(PACK, [c["alias"] for c in cards]), unit=unit or _unit(0), variant=variant)


def _sweep_req():
    manifest = [{"unit_id": f"u-{i:02d}", "kind": "bundle", "title": f"단위 {i}", "text": "후면 커버 둘레 접착 계면 변경"}
                for i in range(30)]
    return _req("sweep", PACK, [], manifest=manifest, extra={"domains": DOMAINS})


def _offcard_req():
    bg = [c for c in PACK if not c["checkable"]]
    known = [{"alias": "K01", "judgement": "OK", "why": "접착폭 하한을 지킨다"}]
    return _req("offcard", bg, _digest(PACK, [c["alias"] for c in bg]), unit=_unit(0),
                extra={"domains": DOMAINS, "mechanisms": MECHS, "known": known})


def _near_window_req():
    """16K 창에서 조립 길이가 창 한도 바로 밑에 닿는 verdict — 한글 본문이라 추정 토큰이 창의 85% 를 넘는다."""
    lim = cr.limits(W16K, "server", 0, model=MODEL)
    base = cr.prepare(_verdict_req(n=1), lim)
    room = lim["p_hard"] - base["ctx"]["prompt_chars"] - 200
    unit = _unit(0)
    unit["lines"].append({"ref": "c:big", "text": "가" * room})
    return _verdict_req(n=1, unit=unit)


REQUESTS = {
    "verdict_fwd": _verdict_req("fwd"), "verdict_rev": _verdict_req("rev"), "verdict_solo": _verdict_req("solo"),
    "verdict_one": _verdict_req(n=1), "sweep": _sweep_req(), "offcard": _offcard_req(),
    "verdict_near_window_16k": _near_window_req(),
}
# 장면 — (요청, 대본, 창). review_busy 는 대본이 아니라 관문을 미리 채운다.
SCENES = {
    "verdict_ok": ("verdict_fwd", ["oracle"], W1M),
    "verdict_rev": ("verdict_rev", ["oracle"], W1M),
    "verdict_solo": ("verdict_solo", ["oracle"], W1M),
    "verdict_repaired": ("verdict_fwd", ["skip_middle", "oracle"], W1M),
    "verdict_missing": ("verdict_fwd", ["skip_middle"] * 3, W1M),
    "verdict_truncated": ("verdict_fwd", ["truncate", "oracle"], W1M),
    "verdict_repair_error": ("verdict_fwd", ["skip_middle", "timeout"], W1M),
    "verdict_flagged": ("verdict_fwd", ["all_ok_fakequote"] * 3, W1M),
    "sweep_ok": ("sweep", ["oracle"], W1M),
    "sweep_broken_line": ("sweep", [{"behavior": "broken_line", "at": 16}, "oracle"], W1M),
    "offcard_ok": ("offcard", ["oracle"], W1M),
    "review_busy": ("verdict_one", ["oracle"], W1M),
    "llm_timeout": ("verdict_one", ["timeout"], W1M),
    "llm_unreachable": ("verdict_one", ["unreachable"], W1M),
    "llm_error": ("verdict_one", ["http_503"], W1M),
    "llm_misconfigured": ("verdict_one", ["http_401"], W1M),
    "llm_rejected": ("verdict_one", ["http_400"], W1M),
    "context_overflow": ("verdict_near_window_16k", ["http_400"], W16K),
    "truncated_empty": ("verdict_one", ["truncate_empty"], W1M),
    "no_rows": ("verdict_one", ["prose_only"], W1M),
}


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def _mask(event: str, data: dict) -> dict:
    data = copy.deepcopy(data)
    if event == "result":
        data["meta"]["llm_ms"] = [0] * len(data["meta"]["llm_ms"])
    return data


def _setup(monkeypatch, window: int, llm) -> TestClient:
    monkeypatch.setattr(a, "_ctx_cache", {"n": window, "src": "server"})
    monkeypatch.setattr(a.app.state, "delib_llm", llm, raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    return TestClient(a.app)


def _transcript(llm: FakeLLM) -> str:
    """시도마다 대본 LLM 이 낸 원문 — 오류 거동이면 그 예외 이름만 적는다."""
    from fakes.llm import ERROR_BEHAVIORS, act, respond

    out = []
    for i, call in enumerate(llm.calls, 1):
        step = llm.script[min(i - 1, len(llm.script) - 1)]
        if step.text is None and step.behavior in ERROR_BEHAVIORS:
            out.append(f"=== 시도 {i} · {step.behavior} — 응답 없음(예외) ===\n")
            continue
        text, finish = respond(act(step), call["system"], call["human"], llm.oracle)
        out.append(f"=== 시도 {i} · {step.behavior} · finish_reason={finish} ===\n{text}\n")
    return "".join(out)


def produce(monkeypatch) -> dict:
    """예제 전부 — {파일 이름: 글}."""
    monkeypatch.setattr(a, "VLLM_MODEL", MODEL)
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    files: dict = {}

    # limits — 운영 창(1,048,576)과 돌 수 없는 창(4,096).
    c = _setup(monkeypatch, W1M, FakeLLM(["oracle"]))
    files["limits_1m.json"] = _dump(c.get("/card-review/limits").json())
    c = _setup(monkeypatch, 4_096, FakeLLM(["oracle"]))
    files["limits_ctx_too_small.json"] = _dump(c.get("/card-review/limits").json())

    # pack — 실제 load_pack_tools·build_pack 을 지나고 게이트웨이 도구 사전만 지어낸 AIDataHub 로 바꾼다.
    c = _setup(monkeypatch, W1M, FakeLLM(["oracle"]))
    body = {"agent_key": KEY, "groups": ["g"], "user_email": "u@x.com", "user_pat": "user-pat"}

    def pack_with(hub=None, drop=(), reject=False, **override):
        async def tools_by_name(*_a, **_k):
            if reject:
                raise RuntimeError("Client error '401 Unauthorized' for url 'http://gateway/mcp'")
            return _tools(hub or Hub(), drop)

        monkeypatch.setattr(cr, "_tools_by_name", tools_by_name)
        r = c.post("/card-review/pack", json={**body, **override})
        out = r.json()
        if r.status_code == 200:
            out["fetched_at"] = 0
        return r.status_code, out

    st, pack = pack_with()
    assert st == 200
    files["pack_ok.json"] = _dump(pack)
    st, empty = pack_with(Hub(records=[]))
    assert st == 200 and empty["cards"] == []
    files["pack_empty.json"] = _dump(empty)
    bad = Hub()
    bad.get_override[sorted(bad.records)[0]] = "✖ 도구 get_record 호출 실패: 연결이 끊겼다"
    unstable = Hub()
    unstable.list_override = {"total": len(unstable.records) + 1, "count": len(unstable.records), "offset": 0,
                              "items": [{"id": rid} for rid in sorted(unstable.records)]}
    errors = {}
    for code, kw in {"agent_not_found": {"agent_key": "no-such-expert"},
                     "tools_unavailable": {"drop": ("get_record",)},
                     "card_fetch_failed": {"hub": bad},
                     "card_list_unstable": {"hub": unstable},
                     "credential_missing": {"user_pat": ""},
                     "credential_rejected": {"reject": True}}.items():
        st, out = pack_with(**kw)
        assert out["code"] == code, (code, out)
        errors[code] = {"status": st, "body": out}
    # 팩 관문(동시 2)이 PACK_WAIT_S 안에 비지 않으면 429 review_busy(기다림 — 아무것도 세지 않는다). 기다림만 줄여 뜬다.
    monkeypatch.setattr(cr, "PACK_WAIT_S", 0.05)
    monkeypatch.setattr(a.app.state, "card_review_pack_sem", asyncio.Semaphore(0), raising=False)
    st, out = pack_with()
    assert (st, out["code"]) == (429, "review_busy"), out
    errors["review_busy"] = {"status": st, "body": out}
    monkeypatch.setattr(a.app.state, "card_review_pack_sem", None, raising=False)
    files["pack_errors.json"] = _dump(errors)

    # plan — 위 팩의 카드와 단위 다섯.
    plan_req = {"cards": [{k: x[k] for k in ("alias", "record_id", "checkable", "chars")} for x in pack["cards"]],
                "units": [{"unit_id": f"u-{i:02d}", "manifest_chars": 300 + i, "body_chars": 2_000 + i} for i in range(5)]}
    r = c.post("/card-review/plan", json=plan_req)
    assert r.status_code == 200
    files["plan_req.json"] = _dump(plan_req)
    files["plan_ok.json"] = _dump(r.json())
    # plan — 창 131,072 에서 묶음째로는 창을 넘고 카드 한 장씩(solo)은 드는 (묶음, 단위) 쌍 하나(pair_over).
    c = _setup(monkeypatch, 131_072, FakeLLM(["oracle"]))
    l128 = c.get("/card-review/limits").json()
    x = l128["verdict"]["card_budget"] // 2 - 400
    po_req = {"cards": [{"alias": "K01", "record_id": "DOC-T-0001", "checkable": True, "chars": x},
                        {"alias": "K02", "record_id": "DOC-T-0002", "checkable": True, "chars": x}],
              "units": [{"unit_id": "u-big", "manifest_chars": 300,
                         "body_chars": l128["p_hard"] - l128["offcard"]["fixed"] - 50},
                        {"unit_id": "u-small", "manifest_chars": 300, "body_chars": 2_000}]}
    r = c.post("/card-review/plan", json=po_req)
    assert r.status_code == 200 and [p["unit_id"] for p in r.json()["pair_over"]] == ["u-big"], r.json()
    files["plan_pair_over_req.json"] = _dump(po_req)
    files["plan_pair_over.json"] = _dump(r.json())

    # run — 장면마다 요청·프레임 전부·마지막 프레임·대본 원문.
    for name, req in REQUESTS.items():
        files[f"req_{name}.json"] = _dump(req)
    index = {}
    for name, (req_name, script, window) in SCENES.items():
        llm = FakeLLM(script)
        c = _setup(monkeypatch, window, llm)
        held = []
        if name == "review_busy":
            gate = cr.gate_of(a.app)
            held = [gate.try_acquire("held") for _ in range(4)]
        r = c.post("/card-review/run", json=REQUESTS[req_name])
        assert r.status_code == 200, (name, r.text[:300])
        frames = [delib_jobs._parse_sse(x.encode()) for x in r.text.split("\n\n") if x.strip()]
        for t in held:
            cr.gate_of(a.app).release(t, "ok")
        masked = [(e, _mask(e, d)) for e, d in frames]
        files[f"run_{name}.sse"] = b"".join(a._sse(e, d) for e, d in masked).decode()
        event, last = masked[-2]
        final = f"{event}_{name}.json"
        files[final] = _dump(last)
        entry = {"request": f"req_{req_name}.json", "script": list(script),
                 "ctx_tokens": window, "frames": f"run_{name}.sse", "final": final, "llm_calls": len(llm.calls)}
        if llm.calls:
            files[f"llm_transcript_{name}.txt"] = _transcript(llm)
            entry["transcript"] = f"llm_transcript_{name}.txt"
        index[name] = entry
    files["index.json"] = _dump(index)

    # 스트림을 열기 전의 거절 — 상태 코드와 본문.
    c = _setup(monkeypatch, W1M, FakeLLM(["oracle"]))
    lim = c.get("/card-review/limits").json()
    one = REQUESTS["verdict_one"]
    big = copy.deepcopy(one)
    big["unit"]["lines"].append({"ref": "c:big", "text": "가" * (lim["p_hard"] + 10)})
    rejects_in = {
        "unknown_fields": {**one, "extras": {}},
        "bad_request": {k: v for k, v in one.items() if k != "expert"},
        "unsupported_kind": {**one, "kind": "cross"},
        "unsupported_unit_kind": {**one, "unit": {**one["unit"], "kind": "snap_unit"}},
        "empty_unit": {**one, "unit": {**one["unit"], "lines": [], "context": []}},
        "deck_too_large": {**one, "cards": CHECK[:9]},
        "over_budget": big,
        "rev_mismatch": {**one, "expect": {"prompt_rev": lim["prompt_rev"]["verdict"], "limits_rev": "00000000"}},
        "denied": {**one, "entitlements": ["feat:thinking"]},
    }
    rejects = {}
    for code, b in rejects_in.items():
        r = c.post("/card-review/run", json=b)
        assert r.json()["code"] == code, (code, r.json())
        rejects[code] = {"status": r.status_code, "body": r.json()}
    c = _setup(monkeypatch, 4_096, FakeLLM(["oracle"]))
    r = c.post("/card-review/run", json=one)
    rejects["ctx_too_small"] = {"status": r.status_code, "body": r.json()}
    # 창을 모른다 — 창 조회가 실패해 다시 묻는 중(기본값 128,000 으로 가정). 1M 창으로 짠 판을 실어도 409 가 아니라 503 이다.
    c = _setup(monkeypatch, W1M, FakeLLM(["oracle"]))
    monkeypatch.setattr(a, "_ctx_cache", {"retry_at": float("inf")})
    monkeypatch.setattr(a, "_CTX_ENV_SET", False)
    monkeypatch.setattr(a, "_CTX_FALLBACK", 128_000)
    r = c.post("/card-review/run", json={**one, "expect": {"prompt_rev": lim["prompt_rev"]["verdict"],
                                                          "limits_rev": lim["limits_rev"]}})
    rejects["ctx_unknown"] = {"status": r.status_code, "body": r.json()}
    # over_budget 의 본문에는 요청이 없다(크기만) — 요청은 싣지 않고 어떻게 만들었는지만 적는다.
    files["rejects.json"] = _dump({"_how": {"over_budget": "req_verdict_one 의 unit.lines 에 p_hard+10 자 줄 하나를 더했다",
                                            "ctx_too_small": "창 4,096 에서 req_verdict_one",
                                            "ctx_unknown": "창 조회가 실패해 다시 묻는 중(기본값 128,000 가정)에 limits_1m 의 "
                                                           "판을 expect 로 실은 req_verdict_one",
                                            "_else": "req_verdict_one 을 코드 이름대로 한 칸 바꿨다"}, **rejects})
    return files


def _managed(name: str) -> bool:
    return any(name == m or (m.endswith("_") and name.startswith(m)) for m in MANAGED)


def test_예제는_실제_라우트가_낸_것과_같다(monkeypatch):
    got = produce(monkeypatch)
    if _UPDATE:
        OUT.mkdir(parents=True, exist_ok=True)
        for p in OUT.iterdir():
            if _managed(p.name) and p.name not in got:
                p.unlink()
        for name, text in got.items():
            (OUT / name).write_text(text, encoding="utf-8")
    have = sorted(p.name for p in OUT.iterdir() if _managed(p.name))
    assert have == sorted(got), f"예제 목록이 다르다 — 없는 것 {sorted(set(got) - set(have))}, 남은 것 {sorted(set(have) - set(got))}"
    for name, text in got.items():
        assert (OUT / name).read_text(encoding="utf-8") == text, (
            f"{name} 가 실제 라우트의 산출과 다르다 — 엔진을 고쳤으면 리스크 앱·포털 시험과 같이 보고 "
            "CARD_REVIEW_CONTRACT_UPDATE=1 로 다시 뜬다")


# ── 예제가 조용히 얇아지지 않게 ─────────────────────────────────────────────────
def _load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def test_엔진이_내는_오류_코드마다_예제가_있다():
    finals = {v["final"] for v in _load("index.json").values()}
    for code in cr.ERRORS:
        assert f"error_{code}.json" in finals, f"오류 코드 {code} 의 예제가 없다"
        err = _load(f"error_{code}.json")
        assert err["code"] == code and (err["infra"], err["retryable"]) == cr.ERRORS[code]
    rejects = _load("rejects.json")
    assert set(rejects) - {"_how"} == {"unknown_fields", "bad_request", "unsupported_kind", "unsupported_unit_kind",
                                       "empty_unit", "deck_too_large", "over_budget", "rev_mismatch", "denied",
                                       "ctx_too_small", "ctx_unknown"}
    assert {rejects[k]["status"] for k in ("rev_mismatch", "denied", "ctx_unknown")} == {409, 403, 503}
    assert rejects["ctx_unknown"]["body"]["retry_after_s"] >= 1 and rejects["ctx_unknown"]["body"]["infra"] is True


def test_결과_예제는_계약의_칸을_다_싣는다():
    q_keys = {"expected", "ok", "flagged", "missing", "bad_lines", "truncated", "tail_broken", "attempts", "repaired",
              "stamp", "dropped_over_cap", "maybe_more"}
    m_keys = {"model", "ctx_tokens", "prompt_rev", "norm_rev", "limits_rev", "input_sha", "prompt_chars", "usage",
              "finish_reason", "llm_ms", "soft_over", "basis", "bg_dropped", "order_effective", "aliases"}
    index = _load("index.json")
    results = [v["final"] for v in index.values() if v["final"].startswith("result_")]
    assert len(results) == 11
    for name in results:
        res = _load(name)
        assert {"kind", "variant", "rows", "missing", "extras", "quality", "meta"} <= set(res), name
        assert q_keys <= set(res["quality"]) and m_keys <= set(res["meta"]) and "inj" in res["extras"], name
    assert _load("result_verdict_repair_error.json")["quality"]["repair_error"]["code"] == "llm_timeout"
    assert _load("result_verdict_truncated.json")["quality"]["truncated"] is True
    assert _load("result_verdict_missing.json")["missing"]
    # 프레임 파일의 순서 — status(시도마다) → result|error → done.
    for v in index.values():
        frames = [delib_jobs._parse_sse(x.encode()) for x in (OUT / v["frames"]).read_text(encoding="utf-8").split("\n\n")
                  if x.strip()]
        names = [e for e, _ in frames]
        assert names[-1] == "done" and names[-2] in ("result", "error") and set(names[:-2]) <= {"status"}, names


def test_limits_예제의_판이_팩_결과와_같다():
    lim, res = _load("limits_1m.json"), _load("result_verdict_ok.json")
    assert res["meta"]["limits_rev"] == lim["limits_rev"] and res["meta"]["prompt_rev"] == lim["prompt_rev"]["verdict"]
    assert _load("plan_ok.json")["limits_rev"] == lim["limits_rev"] and _load("plan_ok.json")["pair_over"] == []
    po = _load("plan_pair_over.json")
    assert [(p["deck_id"], p["unit_id"]) for p in po["pair_over"]] == [(po["verdict_decks"][0]["deck_id"], "u-big")]
    assert all(p["need"] > p["have"] for p in po["pair_over"]) and po["oversize"]["units"] == []
    assert _load("pack_ok.json")["norm_rev"] == lim["norm_rev"] == res["meta"]["norm_rev"]
    assert "ctx_too_small" in _load("limits_ctx_too_small.json")


@pytest.mark.parametrize("name", ["req_verdict_fwd.json", "req_sweep.json", "req_offcard.json", "req_verdict_solo.json"])
def test_요청_예제는_app_없는_검증기를_지난다(name):
    lim = _load("limits_1m.json")
    assert cr.validate_run_request(_load(name), lim) == []

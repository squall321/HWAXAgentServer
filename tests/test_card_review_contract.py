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
import review_gate  # noqa: E402
from fakes.llm import Act, FakeLLM, default_oracle  # noqa: E402
from test_card_review_chunk import _long_card, _split_oracle  # noqa: E402
from test_card_review_pack import KEY, Hub, _tools  # noqa: E402
from test_card_review_prompts import _digest, _pack, _req, _unit  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "card_review" / "contract"
_UPDATE = os.environ.get("CARD_REVIEW_CONTRACT_UPDATE") == "1"
# 이 시험이 만드는 파일(머리 이름). 그 밖의 파일(정규화·인용 벡터·틀 잠금)은 다른 시험이 만든다.
MANAGED = ("index.json", "rejects.json", "limits_", "pack_", "plan_", "req_", "run_", "result_", "error_",
           "llm_transcript_", "drain_")
MODEL = "fake-glm"
W1M, W16K = 1_048_576, 16_384
# 조각 장면(HWAXPortal docs/risk-200k W-D11)의 창 — 한 장 자리가 1만 자 남짓이라 카드 3만 자로 조각 넷이 난다(예제 파일이 작다).
# 계획 예제는 실제 목표 창(20만)으로 뜬다 — 계획 요청에는 글자 수만 실려 파일이 작다.
W32K, W200K = 32_768, 200_000

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


def _sweep_gaps_req():
    """입력 결손 전문가의 훑기(cr-sweep-2) — req_sweep 에 전문가 단위 결측만 더했다(프롬프트는 [결측] 블록과 한 문장만 다르다)."""
    r = _sweep_req()
    r["extra"] = {**r["extra"], "missing": [{"kind": "ecad", "note": "보드 고정점·지지 스팬·보드 근접 계면을 볼 회로 배치가 없다"}]}
    return r


def _verdict_mech_req():
    return _req("verdict", CHECK[:4], _digest(PACK, [c["alias"] for c in CHECK[:4]]), unit=_unit(0), variant="fwd",
                extra={"mechanisms": MECHS})


def _noinput_req():
    """입력 결손 두 질문 — 카드 전부(점검·배경), 단위의 결측(unit.missing), 메커니즘 목록."""
    return _req("noinput", PACK, _digest(PACK), unit=_unit(0), extra={"mechanisms": MECHS})


def _offcard_search_req():
    """카드 0장 전문가의 카드 밖 호출 + 긴 문서 검색(cr-offcard-2)."""
    r = _req("offcard", [], [], unit=_unit(0), extra={"domains": DOMAINS, "mechanisms": MECHS,
                                                       "search": {"queries": ["보스 둘레 리브 응력", "고정점 간극 낙하"]}})
    r["expert"] = {**r["expert"], "key": "sim-structural-calc", "name": "구조 해석", "domain": "sim"}
    return r


def _summary_req():
    """영역 요약 — 등록부 행 40(리스크 앱 synth.row_line 꼴) · 꼭 다룰 행 6 · 집계 블록 하나."""
    rows = [{"ref": f"reg:{i:012x}", "text": f"{'치명' if i < 3 else '중대'}/{'FAIL' if i < 6 else 'WARNING'} · "
                                            f"mechanical.drop_stress · «REAR_COVER_{i}» · 제기 {i % 7 + 1}명/{i % 3 + 1}영역"}
            for i in range(40)]
    return {"kind": "summary", "variant": None, "target_key": "diff:" + "ab" * 16, "expert": None,
            "summary": {"scope": {"kind": "domain", "key": "mech", "label": "기구(구조)"}, "rows": rows,
                        "must_cover": [r["ref"] for r in rows[:6]],
                        "blocks": [{"name": "numbers", "title": "영역 수치",
                                    "text": "전문가 122 · 셀 검토함 2,301 · 거름 1,204 · 입력 결손 0"}]},
            "expect": {"prompt_rev": "cr-summary-2"}}


def _near_window_req():
    """16K 창에서 조립 길이가 창 한도 바로 밑에 닿는 verdict — 한글 본문이라 추정 토큰이 창의 85% 를 넘는다."""
    lim = cr.limits(W16K, "server", 0, model=MODEL)
    base = cr.prepare(_verdict_req(n=1), lim)
    room = lim["p_hard"] - base["ctx"]["prompt_chars"] - 200
    unit = _unit(0)
    unit["lines"].append({"ref": "c:big", "text": "가" * room})
    return _verdict_req(n=1, unit=unit)


def _chunk_card(*, mark=False) -> dict:
    """한 장 자리(창 32,768 에서 약 1.1만 자)를 넘는 카드 — 절 셋, 가운데 절이 문단 · 문장으로 나뉜다. mark 면 마지막 절 머리에
    위험표지 문장을 넣는다(갈림 대본 _split_oracle 이 그 조각만 FAIL 로 낸다)."""
    card = _long_card("K05", secs=(3_000, 24_000, 2_000))
    if mark:
        last = card["sections"][-1]
        last["text"] = "위험표지 이 절의 하중 조건에서는 압입 간극이 한계를 넘는다. " + last["text"]
        card["text_sha"] = cr._text_sha(card["sections"])
        card["chars"] = len(cr.render_card(card, "K05"))
    return card


def _verdict_chunked_req(mark=False):
    """판정 — 점검형 카드 한 장(K05)이 한 장 자리를 넘는다. 앱은 원래 카드를 그대로 보내고 run 이 조각 낸다."""
    return _req("verdict", [_chunk_card(mark=mark)], _digest(PACK), unit=_unit(0), variant="fwd")


def _sweep_chunked_req():
    manifest = [{"unit_id": f"u-{i:02d}", "kind": "bundle", "title": f"단위 {i}", "text": "후면 커버 둘레 접착 계면 변경"}
                for i in range(3)]
    return _req("sweep", [_chunk_card()], [], manifest=manifest, extra={"domains": DOMAINS})


REQUESTS = {
    "verdict_fwd": _verdict_req("fwd"), "verdict_rev": _verdict_req("rev"), "verdict_solo": _verdict_req("solo"),
    "verdict_one": _verdict_req(n=1), "sweep": _sweep_req(), "offcard": _offcard_req(),
    "verdict_near_window_16k": _near_window_req(),
    # M3 — 결측 훑기 · 판정 틀의 mech · 입력 결손 두 질문 · 긴 문서 검색 · 요약
    "sweep_gaps": _sweep_gaps_req(), "verdict_mech": _verdict_mech_req(), "noinput": _noinput_req(),
    "offcard_search": _offcard_search_req(), "summary_domain": _summary_req(),
    # W1 카드 조각 — 한 장 자리를 넘는 카드 한 장의 판정 · 판정(조각들이 갈린다) · 훑기
    "verdict_chunked": _verdict_chunked_req(), "verdict_chunked_split": _verdict_chunked_req(mark=True),
    "sweep_chunked": _sweep_chunked_req(),
}
# 포털 중계가 덮어쓰는 신원 칸 넷 — 예제 요청 파일에는 싣지 않고 보낼 때만 더한다(긴 문서 검색은 사용자 PAT 로만 돈다).
IDENT = {"groups": ["g"], "user_email": "u@x.com", "user_pat": "user-pat", "entitlements": ["feat:deliberation"]}
SEARCH_HITS = {"hits": [{"record_id": f"DOC-LD-{i:04d}", "section_id": f"s{i}", "title": f"구조 해석 보고서 {i}",
                         "section_title": "리브 응력", "score": 0.4,
                         "snippet": f"보스 둘레 리브의 응력은 두께가 줄면 국부 집중이 커진다 — 절 {i}. 낙하 1.5 m 에서 여유가 "
                                    "줄어든 사례가 있다."} for i in (1, 2, 3)],
               "refused": False, "applied_config": {"agent_record_scope": 2_734}}
SEARCH_EMPTY = {"hits": [], "refused": True, "refusal_message": "해당 자료를 찾지 못했습니다.",
                "applied_config": {"reason": "agent 'sim-structural-calc' has 0 mapped records"}}
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
    # M3
    "sweep_gaps": ("sweep_gaps", ["oracle"], W1M),
    "verdict_mech": ("verdict_mech", ["mech_mix"], W1M),
    "noinput_ok": ("noinput", ["oracle"], W1M),
    "noinput_repaired": ("noinput", ["noinput_flawed", "oracle"], W1M),
    "noinput_capped": ("noinput", ["noinput_many"], W1M),
    "offcard_search_ok": ("offcard_search", ["oracle"], W1M),
    "offcard_search_empty_scope": ("offcard_search", ["oracle"], W1M),
    "offcard_search_failed": ("offcard_search", ["oracle"], W1M),
    "summary_ok": ("summary_domain", ["oracle"], W1M),
    "summary_repaired": ("summary_domain", ["summary_skip_cover", "oracle"], W1M),
    "summary_missing": ("summary_domain", ["summary_skip_cover", "summary_skip_one", "summary_skip_one"], W1M),
    "review_busy_draining": ("verdict_one", ["oracle"], W1M),
    # 적대 검토 4라운드(RB3B) — 앱이 시험할 다툼·뒤집힘의 픽스처. 걸린 줄이 남고 진 줄이 contested_by 로 실린다(dup_conflict),
    # 보충이 판정을 바꿔 갈아 꼈다(flipped).
    "verdict_contested": ("verdict_fwd", ["first_fail_fakequote", "oracle"], W1M),
    "verdict_flipped": ("verdict_fwd", ["all_ok_fakequote", "flag_fail"], W1M),
    "noinput_contested": ("noinput", ["noinput_flawed", "noinput_retract"], W1M),
    # 적대 검토 6라운드(RB3D · D-42) — 별칭 표의 낱말(checks.alias)과, 표 밖 낱말로 고친 판정이 OK 와 부딪쳐 남긴 다툼(raw).
    "verdict_alias": ("verdict_fwd", ["alias_words"], W1M),
    "verdict_coerced_clash": ("verdict_fwd", ["coerced_clash"], W1M),
    # W1 카드 조각(docs/risk-200k W-D11) — 조각마다 부르고 카드 한 장의 행으로 합친다 · 갈리면 인용된 절만 모아 한 번 더 · 훑기.
    "verdict_chunked": ("verdict_chunked", ["oracle"], W32K),
    "verdict_chunked_split": ("verdict_chunked_split", ["oracle"], W32K),
    "sweep_chunked": ("sweep_chunked", ["oracle"], W32K),
}
# 장면마다의 정답 — 없으면 프롬프트에서 만든 기본 정답(default_oracle).
ORACLES = {"verdict_chunked_split": _split_oracle}
# 장면마다 더 하는 준비 — 검색 도구의 답(사용자 PAT 로 붙는 가짜 게이트웨이)·검색 기한·비우기 깃발.
SEARCH_SCENES = {"offcard_search_ok": ("answer", SEARCH_HITS), "offcard_search_empty_scope": ("answer", SEARCH_EMPTY),
                 "offcard_search_failed": ("hang", None)}
T0 = 1_760_400_000.0          # 비우기 깃발 예제의 시계(정해 둔 값 — 예제가 다시 떠도 같다)


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
    # plan — 창 200,000 에서 한 장 자리를 넘는 카드(점검형 K02 · 배경 K03)는 멈추지 않고 혼자 한 묶음(chunked)이다(W-D11).
    c = _setup(monkeypatch, W200K, FakeLLM(["oracle"]))
    ch_req = {"cards": [{"alias": "K01", "record_id": "DOC-T-0001", "checkable": True, "chars": 3_000},
                        {"alias": "K02", "record_id": "DOC-T-0002", "checkable": True, "chars": 300_000},
                        {"alias": "K03", "record_id": "DOC-T-0003", "checkable": False, "chars": 160_000},
                        {"alias": "K04", "record_id": "DOC-T-0004", "checkable": True, "chars": 4_000}],
              "units": [{"unit_id": f"u-{i:02d}", "manifest_chars": 300 + i, "body_chars": 2_000 + i} for i in range(3)]}
    r = c.post("/card-review/plan", json=ch_req)
    assert r.status_code == 200 and r.json()["oversize"]["cards"] == [], r.json()
    files["plan_chunked_req.json"] = _dump(ch_req)
    files["plan_chunked.json"] = _dump(r.json())

    # run — 장면마다 요청·프레임 전부·마지막 프레임·대본 원문.
    for name, req in REQUESTS.items():
        files[f"req_{name}.json"] = _dump(req)
    index = {}
    for name, (req_name, script, window) in SCENES.items():
        llm = FakeLLM(script, oracle=ORACLES.get(name, default_oracle))
        c = _setup(monkeypatch, window, llm)
        held = []
        body = REQUESTS[req_name]
        if name == "review_busy":
            gate = cr.gate_of(a.app)
            held = [gate.try_acquire("held") for _ in range(8)]
        if name == "review_busy_draining":
            cr.gate_of(a.app).drain.set_on(scope="all", by="update-all", ttl_s=600)
        if name in SEARCH_SCENES:
            how, answer = SEARCH_SCENES[name]
            body = {**body, **IDENT}
            monkeypatch.setenv("CARD_REVIEW_SEARCH_TIMEOUT_S", "0.05" if how == "hang" else "60")

            class _Search:
                def __init__(self, how=how, answer=answer):
                    self.how, self.answer = how, answer

                async def ainvoke(self, _args):
                    if self.how == "hang":
                        await asyncio.sleep(5)
                    return json.dumps(self.answer, ensure_ascii=False)

            async def tools_by_name(*_a, _s=_Search(), **_k):
                return {"agent_search": _s}

            monkeypatch.setattr(cr, "_tools_by_name", tools_by_name)
        r = c.post("/card-review/run", json=body)
        assert r.status_code == 200, (name, r.text[:300])
        frames = [delib_jobs._parse_sse(x.encode()) for x in r.text.split("\n\n") if x.strip()]
        for t in held:
            cr.gate_of(a.app).release(t, "ok")
        if name == "review_busy_draining":
            cr.gate_of(a.app).drain.set_off(by="update-all")
        if name in SEARCH_SCENES:
            monkeypatch.delenv("CARD_REVIEW_SEARCH_TIMEOUT_S")
        masked = [(e, _mask(e, d)) for e, d in frames]
        files[f"run_{name}.sse"] = b"".join(a._sse(e, d) for e, d in masked).decode()
        event, last = masked[-2]
        final = f"{event}_{name}.json"
        files[final] = _dump(last)
        entry = {"request": f"req_{req_name}.json", "script": list(script),
                 "ctx_tokens": window, "frames": f"run_{name}.sse", "final": final, "llm_calls": len(llm.calls)}
        if name in SEARCH_SCENES:
            entry["search"] = {"how": SEARCH_SCENES[name][0], "identity": "포털이 덮어쓰는 신원 칸 넷을 더해 보냈다",
                               **({"timeout_s": 0.05} if SEARCH_SCENES[name][0] == "hang" else {})}
        if name == "review_busy_draining":
            entry["drain"] = {"on": True, "scope": "all", "by": "update-all"}
        if name in ORACLES:
            entry["oracle"] = f"{ORACLES[name].__name__} (tests/test_card_review_chunk.py)"
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
        # M3 앱은 요청 종류의 판(limits_rev_by_kind[kind])을 limits_rev_kind 로 보낸다(RB3-6 — M2 앱은 limits_rev).
        "rev_mismatch": {**one, "expect": {"prompt_rev": lim["prompt_rev"]["verdict"], "limits_rev_kind": "00000000"}},
        "denied": {**one, "entitlements": ["feat:thinking"]},
        # M3 — 앱이 limits.summary.cover_max 를 어겼다(꼭 다룰 행이 cover_hard_max 를 넘는다).
        "cover_too_large": _cover_too_large(lim),
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
                                                          "limits_rev_kind": lim["limits_rev_by_kind"]["verdict"]}})
    rejects["ctx_unknown"] = {"status": r.status_code, "body": r.json()}
    # M3 요약의 크기 거절 둘 — LLM 0회(스트림을 열기 전). 영역을 나눠 부르지 않는다(D-22).
    llm0 = FakeLLM(["oracle"])
    c = _setup(monkeypatch, W1M, llm0)
    lim = c.get("/card-review/limits").json()
    r = c.post("/card-review/run", json=_cover_too_large(lim))
    files["error_cover_too_large.json"] = _dump({"status": r.status_code, "body": r.json(), "llm_calls": len(llm0.calls),
                                                  "_how": f"req_summary_domain 의 must_cover 를 cover_hard_max+1"
                                                          f"({lim['summary']['cover_hard_max'] + 1})건으로"})
    big_rows = [{"ref": f"reg:{i:012x}", "text": "가" * 380} for i in range(lim["p_hard"] // 390 + 10)]
    r = c.post("/card-review/run", json={**REQUESTS["summary_domain"],
                                         "summary": {**REQUESTS["summary_domain"]["summary"], "rows": big_rows,
                                                     "must_cover": []}})
    files["error_over_budget_summary.json"] = _dump({"status": r.status_code, "body": r.json(),
                                                     "llm_calls": len(llm0.calls),
                                                     "_how": f"req_summary_domain 의 rows 를 380자 행 {len(big_rows):,}개로"})
    assert len(llm0.calls) == 0

    # M3 비우기 — POST·GET /drain 과 그때의 /health(카드 대조 칸만). 시계는 T0 로 고정한다.
    c = _setup(monkeypatch, W1M, FakeLLM(["oracle"]))
    monkeypatch.setattr(a, "_CHAT_LOAD", review_gate.ChatLoad())
    gate = cr.gate_of(a.app)
    gate.drain.clock = lambda: T0
    gate.drain.set_off(by="-", force=True)

    def post(body):
        r = c.post("/drain", json=body)
        return {"request": body, "status": r.status_code, "body": r.json()}

    files["drain_on_all.json"] = _dump(post({"on": True, "scope": "all", "ttl_s": 1800, "by": "update-all",
                                             "reason": "update-all 1h — §2 앞"}))
    h = c.get("/health").json()
    files["drain_health.json"] = _dump({
        **{k: h[k] for k in ("delib_active", "delib_queued", "review_active", "draining", "drain", "capabilities")},
        "card_review": {k: h["card_review"][k] for k in ("kinds", "limit", "limit_reason", "delib_human", "active",
                                                         "search_timeout_s")},
        "load": h["load"]})
    files["drain_until_idle.json"] = _dump(post({"on": True, "scope": "panels", "by": "update-all",
                                                 "until_idle": {"grace_s": 1800, "max_s": 46800},
                                                 "reason": "update-all 끝 — 재기동 건너뜀 장부"}))
    files["drain_off.json"] = _dump(post({"on": False, "by": "update-all"}))
    # 주인 아닌 by 의 hold 아닌 요청은 합치지 않고 409 drain_owned 다(RB3B-9 — update-all 은 409 를 무엇이든 '남의 것' 으로 읽는다).
    post({"on": True, "scope": "all", "ttl_s": 7200, "by": "human:운영자", "reason": "사람의 점검(고정 아님)"})
    files["drain_owned.json"] = _dump(post({"on": True, "scope": "all", "ttl_s": 1800, "by": "update-all"}))
    post({"on": False, "by": "human:운영자"})
    post({"on": True, "scope": "all", "hold": True, "by": "human:운영자", "reason": "사람이 고정"})
    files["drain_held.json"] = _dump(post({"on": True, "scope": "all", "ttl_s": 1800, "by": "update-all"}))
    files["drain_get.json"] = _dump(c.get("/drain").json())
    gate.drain.set_off(by="human:운영자", force=True)
    # over_budget 의 본문에는 요청이 없다(크기만) — 요청은 싣지 않고 어떻게 만들었는지만 적는다.
    files["rejects.json"] = _dump({"_how": {"over_budget": "req_verdict_one 의 unit.lines 에 p_hard+10 자 줄 하나를 더했다",
                                            "ctx_too_small": "창 4,096 에서 req_verdict_one",
                                            "ctx_unknown": "창 조회가 실패해 다시 묻는 중(기본값 128,000 가정)에 limits_1m 의 "
                                                           "판을 expect 로 실은 req_verdict_one",
                                            "_else": "req_verdict_one 을 코드 이름대로 한 칸 바꿨다"}, **rejects})
    return files


def _cover_too_large(lim: dict) -> dict:
    """요약 요청 — 꼭 다룰 행이 그 박스의 cover_hard_max 를 하나 넘는다(앱이 cover_max 를 어겼다 — 422 cover_too_large)."""
    hard = lim["summary"]["cover_hard_max"]
    req = copy.deepcopy(REQUESTS["summary_domain"])
    rows = [{"ref": f"reg:{i:012x}", "text": f"중대/WARNING · 행 {i}"} for i in range(hard + 5)]
    req["summary"].update(rows=rows, must_cover=[r["ref"] for r in rows[:hard + 1]])
    return req


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
                                       "ctx_too_small", "ctx_unknown", "cover_too_large"}
    assert {rejects[k]["status"] for k in ("rev_mismatch", "denied", "ctx_unknown")} == {409, 403, 503}
    assert rejects["ctx_unknown"]["body"]["retry_after_s"] >= 1 and rejects["ctx_unknown"]["body"]["infra"] is True


def test_결과_예제는_계약의_칸을_다_싣는다():
    q_keys = {"expected", "ok", "flagged", "missing", "bad_lines", "truncated", "tail_broken", "attempts", "repaired",
              "stamp", "dropped_over_cap", "maybe_more", "dup_conflict", "flipped"}
    m_keys = {"model", "ctx_tokens", "prompt_rev", "norm_rev", "limits_rev", "limits_rev_kind", "input_sha",
              "prompt_chars", "usage", "finish_reason", "llm_ms", "soft_over", "basis", "bg_dropped", "order_effective",
              "aliases", "clipped"}
    index = _load("index.json")
    results = [v["final"] for v in index.values() if v["final"].startswith("result_")]
    assert len(results) == 30
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
    assert res["meta"]["limits_rev_kind"] == lim["limits_rev_by_kind"]["verdict"]
    assert set(lim["limits_rev_by_kind"]) == set(lim["kinds"])
    mm = _load("rejects.json")["rev_mismatch"]["body"]
    assert (mm["limits_rev_kind"], mm["expect"]["limits_rev_kind"]) == (lim["limits_rev_by_kind"]["verdict"], "00000000")
    assert _load("plan_ok.json")["limits_rev"] == lim["limits_rev"] and _load("plan_ok.json")["pair_over"] == []
    po = _load("plan_pair_over.json")
    assert [(p["deck_id"], p["unit_id"]) for p in po["pair_over"]] == [(po["verdict_decks"][0]["deck_id"], "u-big")]
    assert all(p["need"] > p["have"] for p in po["pair_over"]) and po["oversize"]["units"] == []
    assert _load("pack_ok.json")["norm_rev"] == lim["norm_rev"] == res["meta"]["norm_rev"]
    assert "ctx_too_small" in _load("limits_ctx_too_small.json")


@pytest.mark.parametrize("name", ["req_verdict_fwd.json", "req_sweep.json", "req_offcard.json", "req_verdict_solo.json",
                                  "req_sweep_gaps.json", "req_verdict_mech.json", "req_noinput.json",
                                  "req_offcard_search.json", "req_summary_domain.json", "req_verdict_chunked.json",
                                  "req_verdict_chunked_split.json", "req_sweep_chunked.json"])
def test_요청_예제는_app_없는_검증기를_지난다(name):
    lim = _load("limits_1m.json")
    assert cr.validate_run_request(_load(name), lim) == []


# ── M3 예제가 계약(M3.md 4.3~4.7)을 싣는다 ──────────────────────────────────────────
def test_M3_limits_예제():
    lim = _load("limits_1m.json")
    assert lim["kinds"] == ["sweep", "verdict", "offcard", "noinput", "summary"]
    assert lim["cell_kinds"] == ["sweep", "verdict", "offcard", "noinput"] and lim["api_rev"] == "cr-1"
    assert lim["runnable"] == dict.fromkeys(lim["kinds"], True) and lim["max_tokens_source"] == "none"
    assert (lim["noinput"]["items_max"], lim["summary"]["cover_max"], lim["summary"]["cover_hard_max"],
            lim["summary"]["narrative_max"], lim["summary"]["line_max"]) == (15, 18, 54, 2_520, 400)
    assert lim["offcard"]["longdoc_max"] > 0 and lim["verdict"]["mech_max"] > 0 and lim["search"] == {"timeout_s": 60.0}
    assert lim["prompt_rev"] == {"sweep": "cr-sweep-2", "verdict": "cr-verdict-2", "offcard": "cr-offcard-2",
                                 "noinput": "cr-noinput-1", "summary": "cr-summary-2"}
    assert lim["gate"] == {"limit": 8, "limit_reason": "normal", "active": 0}


def test_M3_결측_훑기_예제는_결측_블록과_문장만_다르다():
    lim = _load("limits_1m.json")
    _, plain, _ = cr.build_prompt(_load("req_sweep.json"), lim)
    _, gaps, _ = cr.build_prompt(_load("req_sweep_gaps.json"), lim)
    note = _load("req_sweep_gaps.json")["extra"]["missing"][0]["note"]
    block = f"[결측]\n- ecad — «{note}»\n\n"
    assert gaps.replace(block, "", 1).replace(cr._INSTR_SWEEP_MISSING, "", 1) == plain
    assert _load("result_sweep_gaps.json")["meta"]["prompt_rev"] == "cr-sweep-2"


def test_M3_입력_결손_예제():
    ok = _load("result_noinput_ok.json")
    q1 = [r for r in ok["rows"] if r["t"] == "q1"]
    assert [r["impact"] for r in q1] == ["yes", "yes"] and sum(r["t"] == "q2" for r in ok["rows"]) == 2
    assert next(r["more"] for r in ok["rows"] if r["t"] == "more") == "no"
    assert (ok["meta"]["basis"], ok["quality"]["attempts"], ok["quality"]["maybe_more"]) == ("noinput", 1, False)
    rep = _load("result_noinput_repaired.json")
    assert rep["quality"]["attempts"] == 2 and rep["missing"] == []
    hint = (OUT / "llm_transcript_noinput_repaired.txt").read_text(encoding="utf-8")
    assert "=== 시도 2" in hint
    cap = _load("result_noinput_capped.json")
    assert cap["quality"]["maybe_more"] is True and cap["quality"]["dropped_over_cap"] == 2


def test_M3_판정_틀_mech_예제():
    res = _load("result_verdict_mech.json")
    assert res["quality"]["attempts"] == 1 and res["meta"]["prompt_rev"] == "cr-verdict-2"
    got = [(r["judgement"], r["mech"], r["mech_free"], r["checks"]["mech"]) for r in res["rows"][:3]]
    assert got == [("WARNING", "thermal.cte_mismatch", "", "ok"), ("FAIL", "free", "not.in.taxonomy", "free"),
                   ("FAIL", "", "", "missing")]


def test_M3_긴_문서_검색_예제():
    ok, empty, failed = (_load(f"result_offcard_search_{x}.json") for x in ("ok", "empty_scope", "failed"))
    assert (ok["meta"]["search"]["state"], ok["meta"]["basis"]) == ("ok", "search_only")
    assert [u["alias"] for u in ok["meta"]["search"]["used"]] == ["D01", "D02", "D03"]
    assert any(r["t"] == "f" and r["src"] == "D01" and r["checks"]["quote"] == "ok" for r in ok["rows"])
    assert (empty["meta"]["search"]["state"], empty["meta"]["basis"]) == ("empty_scope", "role_only")
    assert "묶인 자료 0건" in empty["meta"]["search"]["note"]
    assert (failed["meta"]["search"]["state"], failed["meta"]["basis"]) == ("failed", "role_only")
    frames = (OUT / "run_offcard_search_ok.sse").read_text(encoding="utf-8")
    assert frames.startswith('event: status\ndata: {"step": "search"}')


def test_M3_요약_예제():
    ok, rep, miss = (_load(f"result_summary_{x}.json") for x in ("ok", "repaired", "missing"))
    assert (ok["missing"], ok["quality"]["attempts"], ok["meta"]["basis"]) == ([], 1, "rows")
    assert (rep["missing"], rep["quality"]["attempts"]) == ([], 2)
    assert (miss["missing"], miss["quality"]["attempts"]) == (["R001"], 3)
    assert ok["meta"]["aliases"]["rows"]["R001"] == _load("req_summary_domain.json")["summary"]["rows"][0]["ref"]
    cover, over = _load("error_cover_too_large.json"), _load("error_over_budget_summary.json")
    assert (cover["status"], cover["body"]["code"], cover["llm_calls"]) == (422, "cover_too_large", 0)
    assert (over["status"], over["body"]["code"], over["llm_calls"]) == (422, "over_budget", 0)


def test_M3_비우기_예제():
    on, idle, off, held, got = (_load(f"drain_{x}.json") for x in ("on_all", "until_idle", "off", "held", "get"))
    assert (on["status"], on["body"]["on"], on["body"]["scope"], on["body"]["until"] - on["body"]["set_at"]) == (
        200, True, "all", 1800)
    assert idle["body"]["scope"] == "panels" and idle["body"]["until_idle"]["max_s"] == 46800
    assert off["body"]["on"] is False
    assert (held["status"], held["body"]["code"], held["body"]["drain"]["hold"]) == (409, "drain_held", True)
    owned = _load("drain_owned.json")
    ob = owned["body"]
    assert (owned["status"], ob.get("code"), (ob.get("drain") or {}).get("by"), (ob.get("drain") or {}).get("hold")) == (
        409, "drain_owned", "human:운영자", False), ob
    assert set(got) == {"on", "scope", "until", "until_idle", "hold", "by", "reason", "set_at", "review_active",
                        "delib_active", "delib_queued"}
    h = _load("drain_health.json")
    assert (h["draining"], h["card_review"]["limit"], h["card_review"]["limit_reason"]) == (True, 0, "draining")
    assert h["capabilities"] == ["card_review", "drain"]
    busy = _load("error_review_busy_draining.json")
    assert (busy["code"], busy["reason"], busy["retry_after_s"], busy["limit"]) == ("review_busy", "draining", 60, 0)


def test_RB3B_다툼과_뒤집힘_예제():
    """앱이 dup_conflict·flipped 가 0 이 아닌 셀을 걸림으로 다룬다(M3.md 4.13) — 그 픽스처가 없었다(검토자 지적). 걸린 줄은 남고
    진 줄이 contested_by{verdict, cq, quote, why, attempt} 로 실리며, 검증된 반대 답이 왔으면 그 인용을 더 묻지 않는다."""
    con = _load("result_verdict_contested.json")
    assert (con["quality"]["dup_conflict"], con["quality"]["flipped"], con["quality"]["attempts"]) == (1, 0, 2)
    k01 = next(r for r in con["rows"] if r["k"] == "K01")
    assert (k01["judgement"], k01["checks"]["quote"], k01["checks"]["dup_conflict"]) == ("FAIL", "not_found", True)
    cb = k01.get("contested_by") or {}
    assert set(cb) == {"verdict", "cq", "quote", "why", "attempt"}, k01
    assert (cb["verdict"], cb["quote"], cb["attempt"]) == (["yes", "OK"], "ok", 2)
    flip = _load("result_verdict_flipped.json")
    assert flip["quality"]["flipped"] == len(flip["rows"]) == 8 and flip["quality"]["dup_conflict"] == 0
    assert all(r["judgement"] == "FAIL" and r["flipped_from"] == {"verdict": ["yes", "OK"], "attempt": 1}
               for r in flip["rows"])
    ni = _load("result_noinput_contested.json")
    hit = [r for r in ni["rows"] if r["t"] == "q1" and (r.get("checks") or {}).get("dup_conflict")]
    assert ni["quality"]["dup_conflict"] == len(hit) == 1 and hit[0]["impact"] == "yes"
    cb = hit[0].get("contested_by") or {}
    assert (cb.get("verdict"), cb.get("quote")) == (["no"], "ok"), hit[0]


def test_RB3D_별칭과_고친_판정의_다툼_예제():
    """D-42 A — 별칭 표의 낱말은 그 뜻의 값(checks.alias 에 낱말)이고 보충하지 않는다. 표 밖 낱말은 고친 값이고 같은 카드의 OK
    와 부딪치면 흔적 없이 지지 않는다 — dup_conflict 와 contested_by.raw."""
    al = _load("result_verdict_alias.json")
    by = {r["k"]: r for r in al["rows"]}
    assert (by["K01"]["judgement"], by["K01"]["checks"].get("alias")) == ("FAIL", ["judgement:FAILED"]), by["K01"]
    assert (by["K02"]["judgement"], by["K02"]["checks"].get("alias")) == ("OK", ["judgement:통과"]), by["K02"]
    assert al["quality"]["attempts"] == 1 and all(not r["checks"]["coerced"] for r in al["rows"])
    cl = _load("result_verdict_coerced_clash.json")
    k01 = next(r for r in cl["rows"] if r["k"] == "K01")
    cb = k01.get("contested_by") or {}
    assert (k01["judgement"], k01["checks"].get("dup_conflict"), cb.get("verdict"), cb.get("raw")) == (
        "OK", True, ["yes", "undetermined"], "보류중"), k01
    assert (cl["quality"]["dup_conflict"], cl["quality"]["attempts"]) == (1, 1)


# ── W1 카드 조각 예제(HWAXPortal docs/risk-200k W-D11) ─────────────────────────────────
def test_W1_조각_계획_예제():
    """20만 창 — 한 장 자리를 넘는 카드는 oversize 가 아니라 혼자 한 묶음(chunked)이다. 점검형은 판정 · 훑기 둘 다, 배경 카드는
    훑기에만. 조각 수 어림(chunks)은 하한에 가깝다(실제는 결과의 meta.chunked.n)."""
    pl = _load("plan_chunked.json")
    assert pl["oversize"] == {"cards": [], "kinds": {}, "units": [], "manifest": []} and pl["pair_over"] == []
    ch_v = [d for d in pl["verdict_decks"] if d.get("chunked")]
    ch_s = [d for d in pl["sweep_decks"] if d.get("chunked")]
    assert [d["aliases"] for d in ch_v] == [["K02"]] and [d["aliases"] for d in ch_s] == [["K02"], ["K03"]]
    assert all(d["soft_over"] and d["chunks"] >= 2 for d in ch_v + ch_s)
    assert all(not d.get("chunked") for d in pl["verdict_decks"] + pl["sweep_decks"] if d not in ch_v + ch_s)


def test_W1_조각_실행_예제():
    """조각마다 status{chunk, of} 를 내며 부르고 카드 한 장의 행으로 합친다 — 앱에는 한 카드 = 한 행(k 는 원래 별칭)."""
    index = _load("index.json")
    ok = _load("result_verdict_chunked.json")
    n = ok["meta"]["chunked"]["n"]
    (row,) = [r for r in ok["rows"] if r["t"] == "v"]
    assert row["k"] == "K05" and [c["a"] for c in row["checks"]["chunks"]] == ok["meta"]["chunked"]["aliases"]
    assert ok["meta"]["chunked"]["aliases"] == [f"K05.{i}" for i in range(1, n + 1)] and n >= 3
    assert ok["meta"]["aliases"]["cards"] == {"K05": _load("req_verdict_chunked.json")["cards"][0]["record_id"]}
    assert (ok["quality"]["attempts"], ok["meta"]["chunked"]["reconcile"], index["verdict_chunked"]["llm_calls"]) == (
        n, None, n)
    frames = [delib_jobs._parse_sse(x.encode()) for x in (OUT / "run_verdict_chunked.sse").read_text(encoding="utf-8")
              .split("\n\n") if x.strip()]
    assert [(d["chunk"], d["of"]) for e, d in frames if e == "status"] == [(i, n) for i in range(1, n + 1)]
    split = _load("result_verdict_chunked_split.json")
    split_frames = [delib_jobs._parse_sse(x.encode()) for x in (OUT / "run_verdict_chunked_split.sse")
                    .read_text(encoding="utf-8").split("\n\n") if x.strip()]
    last = [d for e, d in split_frames if e == "status"][-1]
    assert last == {"step": "reconcile", "n": 1, "of": split["meta"]["chunked"]["n"]}, "재판정은 조각 번호가 아니다"
    (row,) = [r for r in split["rows"] if r["t"] == "v"]
    assert {c["judgement"] for c in row["checks"]["chunks"]} >= {"FAIL", "OK"}, "조각들이 갈렸다"
    assert (row["judgement"], row["checks"]["reconcile"], split["meta"]["chunked"]["reconcile"]) == ("WARNING", "ok", "ok")
    assert index["verdict_chunked_split"]["llm_calls"] == split["meta"]["chunked"]["n"] + 1, "재판정 한 번 더"
    sw = _load("result_sweep_chunked.json")
    us = [r for r in sw["rows"] if r["t"] == "u"]
    assert len(us) == 3 and all(u["k"] == ["K05"] for u in us) and sw["extras"]["unmatched_cards"] == []

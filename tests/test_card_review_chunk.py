# 카드 조각 — 창의 '한 장 자리' 를 넘는 카드를 조각 내 보통 카드처럼 묶는다(HWAXPortal docs/risk-200k W-D4 · W-D11)
"""왜 — 20만 토큰 창이면 한 장 자리가 약 12.6만 자다. dev 표본(전문가 32명 · 카드 5,716장)에서 그보다 긴 카드가 143장이었고 그런
카드를 가진 전문가 19명의 잡이 계획의 oversize 로 통째로 멈췄다(재설계 D-22). 사용자 지시는 '거절이 아니라 여러 번 핑퐁' 이다.

지키는 것 — ① 조각은 한도 안이고 원문을 빠짐없이 덮는다(겹친 머리를 빼고 이으면 원래 절 본문) ② 결정적이다 ③ 절 번호는 원래 것
④ 1M 창에서는 조각이 생기지 않는다(계획이 같다) ⑤ 20만 계획은 멈추지 않고 그 카드를 조각 묶음으로 둔다 ⑥ 앱이 원래 카드를
보내면 run 이 조각마다 부르고 카드 한 장의 행으로 합친다(앱에는 한 카드 = 한 행 그대로) ⑦ 조각들이 갈리면 인용된 절만 모아
한 번 더 묻는다 ⑧ 훑기도 같다.
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
from fakes.llm import FakeLLM, default_oracle, quote_of, read_prompt  # noqa: E402
from test_card_review_http import _frames  # noqa: E402
from test_card_review_prompts import _req, _unit  # noqa: E402

LIM_200K = cr.limits(200_000, "server", 32_000, env={})
LIM_1M = cr.limits(1_048_576, "server", 0, env={})
WORDS = ("간극", "압입", "볼트", "토크", "열팽창", "피로", "균열", "도금", "접합", "응력", "실링", "누설")


def _long_card(alias: str = "K07", *, secs=(30_000, 300_000, 20_000), seed: int = 7) -> dict:
    """절 셋 — 가운데 절 하나가 한 장 자리보다 길어 문단 · 문장으로 나뉜다. 문장 끝은 '다.' 다."""
    rng = random.Random(seed)

    def sentence() -> str:
        return " ".join(rng.choice(WORDS) for _ in range(rng.randint(6, 14))) + " 이 변하면 응력이 달라진다."

    sections = []
    for i, n in enumerate(secs, 1):
        paras, total = [], 0
        while total < n:
            p = " ".join(sentence() for _ in range(rng.randint(4, 9)))
            paras.append(p)
            total += len(p) + 2
        sections.append({"sid": str(i), "title": f"절 {i}", "text": "\n\n".join(paras)[:n].rstrip()})
    card = {"record_id": "DOC-CHUNK-0007", "card_id": "C-7", "title": "긴 설계 규칙 카드", "type": "design-rule",
            "checkable": True, "tier": "", "confidence": "", "causal_status": "", "review_status": "", "standard_refs": [],
            "sources_n": 0, "updated": "", "upstream_checksum": "", "sections": sections, "text_sha": cr._text_sha(sections)}
    return {"alias": alias, **card, "chars": len(cr.render_card(card, alias))}


def _rebuild(chunks: list) -> dict:
    """조각들의 절 본문을 겹친 머리를 빼고 이어 절 번호마다 원문을 되살린다."""
    out: dict = {}
    for c in chunks:
        for s in c["sections"]:
            t, prev = s["text"], out.get(s["sid"], "")
            if prev:
                k = max(i for i in range(0, min(len(prev), len(t)) + 1) if prev.endswith(t[:i]))
                t = t[k:]
            out[s["sid"]] = prev + t
    return out


def test_조각은_한도_안이고_원문을_빠짐없이_덮고_결정적이다():
    card = _long_card()
    limit = int(cr.card_room(LIM_200K) * cr.CHUNK_SHARE)
    assert card["chars"] > cr.card_room(LIM_200K)
    chunks = cr.chunk_card(card, "K07", limit)
    assert len(chunks) >= 3 and chunks == cr.chunk_card(card, "K07", limit), "결정적이다"
    assert all(c["chars"] <= limit and c["chars"] == len(cr.render_card(c, c["alias"])) for c in chunks)
    assert [c["alias"] for c in chunks] == [f"K07.{i}" for i in range(1, len(chunks) + 1)]
    assert all(c["chunk"]["parent"] == "K07" and c["chunk"]["of"] == len(chunks) for c in chunks)
    assert all(c["record_id"] == card["record_id"] and c["checkable"] for c in chunks), "같은 카드다"
    assert _rebuild(chunks) == {s["sid"]: s["text"] for s in card["sections"]}, "겹침을 빼고 이으면 원문 그대로"
    parts = [s for c in chunks for s in c["sections"] if s.get("part")]
    assert parts and all(s["sid"] == "2" for s in parts), "한 장 자리보다 긴 절만 나눈다"
    # 겹침 — 둘째 조각부터 머리가 앞 조각 꼬리와 같고 CHUNK_OVERLAP_MAX 안이다
    texts = [s["text"] for s in parts]
    for prev, cur in zip(texts, texts[1:]):
        k = max(i for i in range(0, min(len(prev), len(cur)) + 1) if prev.endswith(cur[:i]))
        assert 0 < k <= cr.CHUNK_OVERLAP_MAX
    head = cr.render_card(chunks[0], chunks[0]["alias"]).split("\n")
    assert head[2].startswith(f"조각 1/{len(chunks)} — 카드 K07 의 일부") and "§1 절 1 / §2 절 2 / §3 절 3" in head[2]


def test_자리_안이면_조각이_없고_1M_에서는_같은_카드도_통째다():
    card = _long_card()
    assert cr.chunk_card(card, "K07", int(cr.card_room(LIM_1M) * cr.CHUNK_SHARE)) == []
    plan = cr.plan([{"alias": "K07", "record_id": card["record_id"], "checkable": True, "chars": card["chars"]}],
                   [{"unit_id": "u-01", "manifest_chars": 200, "body_chars": 2_000}], LIM_1M)
    assert plan["oversize"]["cards"] == [], "1M 에서는 oversize 가 아니다 — 동작이 같다"
    plan = cr.plan([{"alias": "K07", "record_id": card["record_id"], "checkable": True, "chars": card["chars"]},
                    {"alias": "K08", "record_id": "R-8", "checkable": True, "chars": 3_000}],
                   [{"unit_id": "u-01", "manifest_chars": 200, "body_chars": 2_000}], LIM_200K)
    assert plan["oversize"] == {"cards": [], "kinds": {}, "units": [], "manifest": []}, "20만에서도 멈추지 않는다"
    (deck,) = [d for d in plan["verdict_decks"] if "K07" in d["aliases"]]
    assert deck["aliases"] == ["K07"] and deck["chunked"] and deck["chunks"] >= 3, "혼자 한 묶음 — run 이 조각 낸다"
    assert any(d.get("chunked") for d in plan["sweep_decks"]) and plan["pair_over"] == []
    small = _long_card(secs=(3_000,))
    assert cr.chunk_card(small, "K07", 10_000) == []


@pytest.fixture
def client200k(monkeypatch):
    """창 200,000(서버가 알려 준 값)·가짜 LLM(oracle)."""
    monkeypatch.setattr(a, "_ctx_cache", {"n": 200_000, "src": "server"})
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    return TestClient(a.app)


def test_조각_별칭으로_run_이_돌고_인용이_조각_안에서_선다(client200k):
    card = _long_card()
    limit = int(cr.card_room(cr.limits(200_000, "server", 0, env={})) * cr.CHUNK_SHARE)
    chunks = cr.chunk_card(card, "K07", limit)
    body = _req("verdict", chunks[:1], [], unit=_unit(2_000), variant="fwd")
    r = client200k.post("/card-review/run", json=body)
    assert r.status_code == 200, r.text
    fr = _frames(r.text)
    result = next(d for e, d in fr if e == "result")
    (row,) = result.get("rows") or [None]
    assert row["k"] == "K07.1" and row["record_id"] == card["record_id"], row
    sids = {s["sid"] for s in chunks[0]["sections"]}
    assert row["checks"]["quote"] == "ok" and row["checks"]["quote_sid"] in sids, row["checks"]
    assert result["quality"]["expected"] == 1 and result["quality"]["missing"] in (0, [], None), result["quality"]


def _calls(client) -> int:
    return len(a.app.state.delib_llm.calls)


def test_원래_카드를_보내면_run_이_조각마다_부르고_카드_한_장의_행으로_합친다(client200k):
    card = _long_card()
    n = len(cr.chunk_card(card, "K07", int(cr.card_room(cr.limits(200_000, "server", 0, env={})) * cr.CHUNK_SHARE)))
    r = client200k.post("/card-review/run", json=_req("verdict", [card], [], unit=_unit(2_000), variant="fwd"))
    assert r.status_code == 200, r.text
    fr = _frames(r.text)
    chunks_seen = {d.get("chunk") for e, d in fr if e == "status"}
    assert chunks_seen == set(range(1, n + 1)), "조각마다 한 번씩 부른다"
    result = next(d for e, d in fr if e == "result")
    (row,) = [x for x in result["rows"] if x["t"] == "v"]
    assert row["k"] == "K07" and row["record_id"] == card["record_id"], "앱에는 한 카드 = 한 행"
    assert [c["a"] for c in row["checks"]["chunks"]] == [f"K07.{i}" for i in range(1, n + 1)]
    assert row["checks"]["quote"] == "ok", "인용은 카드 전체 본문에서 다시 대조한다"
    q, meta = result["quality"], result["meta"]
    assert (q["expected"], q["missing"], q["attempts"]) == (1, 0, n)
    assert meta["chunked"]["n"] == n and meta["chunked"]["reconcile"] is None and meta["usage"]["llm_calls"] == n
    assert _calls(client200k) == n and fr[-1][0] == "done"


def _split_oracle(system: str, human: str) -> list:
    """표지 낱말이 든 조각만 FAIL(그 낱말부터 인용) · 나머지는 OK, 재판정 프롬프트에는 WARNING — 조각들이 갈리는 대본."""
    p = read_prompt(human)
    if p["kind"] != "verdict":
        return default_oracle(system, human)
    rows = []
    for k, secs in p["cards"].items():
        hit = next((s for s in secs if "위험표지" in s), None)
        if "재판정 —" in human:
            rows.append({"t": "v", "k": k, "applies": "yes", "judgement": "WARNING", "cq": hit[hit.index("위험표지"):][:40],
                         "l": p["lines"][:1], "why": "두 조각의 근거를 맞대니 조건부로 걸린다", "need": "시험으로 확인"})
        elif hit:
            rows.append({"t": "v", "k": k, "applies": "yes", "judgement": "FAIL", "cq": hit[hit.index("위험표지"):][:40],
                         "l": p["lines"][:1], "why": "이 절의 한계를 넘는다", "need": ""})
        else:
            rows.append({"t": "v", "k": k, "applies": "yes", "judgement": "OK", "cq": quote_of(secs), "l": p["lines"][:1],
                         "why": "한계 안이다", "need": ""})
    return rows


def test_조각들이_갈리면_인용된_절만_모아_한_번_더_묻는다(client200k, monkeypatch):
    card = _long_card()
    last = card["sections"][-1]
    last["text"] = "위험표지 이 절의 하중 조건에서는 압입 간극이 한계를 넘는다. " + last["text"]
    card["text_sha"] = cr._text_sha(card["sections"])
    card["chars"] = len(cr.render_card(card, "K07"))
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"], oracle=_split_oracle), raising=False)
    n = len(cr.chunk_card(card, "K07", int(cr.card_room(cr.limits(200_000, "server", 0, env={})) * cr.CHUNK_SHARE)))
    fr = _frames(client200k.post("/card-review/run", json=_req("verdict", [card], [], unit=_unit(2_000), variant="fwd")).text)
    result = next(d for e, d in fr if e == "result")
    (row,) = [x for x in result["rows"] if x["t"] == "v"]
    judgements = [c["judgement"] for c in row["checks"]["chunks"]]
    assert "FAIL" in judgements and "OK" in judgements, judgements
    assert row["judgement"] == "WARNING" and row["checks"]["reconcile"] == "ok", "재판정의 답이 선다"
    assert row["checks"]["quote"] == "ok" and row["cq"].startswith("위험표지")
    assert _calls(client200k) == n + 1 and result["meta"]["chunked"]["reconcile"] == "ok"


def test_훑기도_조각마다_부르고_단위마다_합친다(client200k):
    card = _long_card()
    manifest = [{"unit_id": f"u-0{i}", "kind": "bundle", "title": f"단위 {i}", "text": "후면 커버 PSA 폭 변경"}
                for i in (1, 2)]
    r = client200k.post("/card-review/run", json=_req("sweep", [card], [], manifest=manifest))
    assert r.status_code == 200, r.text
    result = next(d for e, d in _frames(r.text) if e == "result")
    us = [x for x in result["rows"] if x["t"] == "u"]
    assert len(us) == 2 and all(u["k"] == ["K07"] and u["rel"] == "maybe" for u in us), us
    assert result["meta"]["chunked"]["n"] >= 3 and result["extras"]["unmatched_cards"] == []


def _marked_card() -> dict:
    card = _long_card()
    last = card["sections"][-1]
    last["text"] = "위험표지 이 절의 하중 조건에서는 압입 간극이 한계를 넘는다. " + last["text"]
    card["text_sha"] = cr._text_sha(card["sections"])
    card["chars"] = len(cr.render_card(card, "K07"))
    return card


def _n_chunks(card) -> int:
    return len(cr.chunk_card(card, "K07", int(cr.card_room(cr.limits(200_000, "server", 0, env={})) * cr.CHUNK_SHARE)))


def test_조각_하나가_끝내_실패하면_반쯤_합치지_않고_그_error_로_끝낸다(client200k, monkeypatch):
    """둘째 조각의 첫 시도가 400 — 받은 첫 조각을 '판정함' 으로 내지 않는다. error 에 몇 번째 조각인지 싣고 앱은 그 호출을 종전 규칙으로
    다시 낸다."""
    card = _long_card()
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle", "http_400"]), raising=False)
    fr = _frames(client200k.post("/card-review/run", json=_req("verdict", [card], [], unit=_unit(2_000), variant="fwd")).text)
    names = [e for e, _ in fr]
    assert "result" not in names and names[-2:] == ["error", "done"], names
    err = fr[-2][1]
    assert (err["code"], err["chunk"], err["of"]) == ("llm_rejected", 2, _n_chunks(card)), err


def test_재판정이_실패하면_가장_무거운_판정이_남고_실패를_적는다(client200k, monkeypatch):
    """조각들이 갈렸는데 재판정 호출이 503 — 합친 판정은 조각 가운데 가장 무거운 것(FAIL)이고 checks.reconcile = failed 다
    (가볍게 접지 않는다)."""
    card = _marked_card()
    n = _n_chunks(card)
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"] * n + ["http_503"], oracle=_split_oracle),
                        raising=False)
    fr = _frames(client200k.post("/card-review/run", json=_req("verdict", [card], [], unit=_unit(2_000), variant="fwd")).text)
    result = next(d for e, d in fr if e == "result")
    (row,) = [x for x in result["rows"] if x["t"] == "v"]
    assert (row["judgement"], row["checks"]["reconcile"], result["meta"]["chunked"]["reconcile"]) == ("FAIL", "failed", "failed")
    assert row["cq"].startswith("위험표지") and row["checks"]["quote"] == "ok"
    assert [d for e, d in fr if e == "status"][-1]["step"] == "reconcile"


def test_갈렸는데_두_쪽_근거를_못_실으면_묻지_않고_skipped_로_적는다(client200k, monkeypatch):
    """걸림 쪽 인용이 본문에 없다(지어낸 인용) — 그 쪽 근거 절을 찾지 못해 재판정 카드를 세울 수 없다. 묻지 않고(LLM 호출 없음)
    가장 무거운 판정을 두되 갈렸다는 사실을 checks.reconcile = skipped 로 남긴다(조용히 접지 않는다)."""
    from fakes.llm import FAKE_QUOTE

    def fake_side(system: str, human: str) -> list:
        rows = _split_oracle(system, human)
        return [{**r, "cq": FAKE_QUOTE} if r.get("judgement") == "FAIL" else r for r in rows]

    card = _marked_card()
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"], oracle=fake_side), raising=False)
    fr = _frames(client200k.post("/card-review/run", json=_req("verdict", [card], [], unit=_unit(2_000), variant="fwd")).text)
    result = next(d for e, d in fr if e == "result")
    (row,) = [x for x in result["rows"] if x["t"] == "v"]
    assert {c["judgement"] for c in row["checks"]["chunks"]} >= {"FAIL", "OK"}
    assert (row["judgement"], row["checks"]["reconcile"], result["meta"]["chunked"]["reconcile"]) == (
        "FAIL", "skipped", "skipped")
    assert all(d.get("step") != "reconcile" for e, d in fr if e == "status"), "묻지 않았다"


def test_조각_사이에_자리가_없으면_기다렸다_그_조각부터_잇는다(client200k, monkeypatch):
    """둘째 조각이 관문에서 review_busy(1초 뒤) — 첫 조각을 다시 돌지 않고 기다렸다 둘째 조각부터 잇는다. 앱에는 error 가 가지
    않는다(조각 사이에 자리를 남에게 내준 것은 앱의 일이 아니다)."""
    import time

    import review_gate

    card = _long_card()
    n = _n_chunks(card)
    gate = cr.gate_of(a.app)
    real, seen = gate.try_acquire, {"n": 0}

    def flaky(tag=""):
        seen["n"] += 1
        return review_gate.Busy("normal", 8, 8, 1) if seen["n"] == 2 else real(tag)

    monkeypatch.setattr(gate, "try_acquire", flaky)
    t0 = time.monotonic()
    fr = _frames(client200k.post("/card-review/run", json=_req("verdict", [card], [], unit=_unit(2_000), variant="fwd")).text)
    assert time.monotonic() - t0 >= 1.0, "retry_after_s 를 기다렸다"
    names = [e for e, _ in fr]
    assert "error" not in names and names[-2:] == ["result", "done"], names
    result = fr[-2][1]
    assert result["meta"]["chunked"]["n"] == n and _calls(client200k) == n, "첫 조각을 다시 돌지 않았다"
    assert seen["n"] == n + 1 and gate.active == 0, "자리를 다 돌려줬다"


def test_조각_호출에_원래_별칭으로_답해도_그_조각의_답이다(client200k, monkeypatch):
    """틀 줄이 '카드 K07 의 일부' 라 모델이 K07.2 대신 K07 로 적기 쉽다 — 실린 카드가 그 조각 하나뿐이니 그 조각의 답으로 읽는다
    (버리면 세 번 묻고 no_rows 로 조각째 실패했다). 훑기의 k 목록도 같다."""
    def parent_alias(system: str, human: str) -> list:
        rows = default_oracle(system, human)
        for r in rows:
            if r.get("t") == "v":
                r["k"] = r["k"].split(".")[0]
            elif r.get("t") == "u":
                r["k"] = [x.split(".")[0] for x in r.get("k") or []]
        return rows

    card = _long_card()
    n = _n_chunks(card)
    llm = FakeLLM(["oracle"], oracle=parent_alias)
    monkeypatch.setattr(a.app.state, "delib_llm", llm, raising=False)
    fr = _frames(client200k.post("/card-review/run", json=_req("verdict", [card], [], unit=_unit(2_000), variant="fwd")).text)
    result = fr[-2][1]
    assert fr[-2][0] == "result" and len(llm.calls) == n, (fr[-2], len(llm.calls))
    (row,) = [x for x in result["rows"] if x["t"] == "v"]
    assert row["k"] == "K07" and len(row["checks"]["chunks"]) == n and result["quality"]["dropped_alias"] == 0
    manifest = [{"unit_id": "u-01", "kind": "bundle", "title": "단위 1", "text": "후면 커버 PSA 폭 변경"}]
    fr = _frames(client200k.post("/card-review/run", json=_req("sweep", [card], [], manifest=manifest)).text)
    us = [x for x in fr[-2][1]["rows"] if x["t"] == "u"]
    assert [u["k"] for u in us] == [["K07"]] and all("k:unknown_alias" not in u["checks"]["coerced"] for u in us)


def test_원래_별칭이_따로_실렸으면_접지_않는다():
    """조각과 원래 카드가 한 요청에 같이 있으면(앱이 그렇게 보내지 않지만) K07 은 원래 카드의 답이다 — 접지 않는다."""
    card = _long_card()
    chunks = cr.chunk_card(card, "K07", int(cr.card_room(LIM_200K) * cr.CHUNK_SHARE))
    small = {**_long_card("K08", secs=(800,)), "record_id": "DOC-8"}
    ctx = cr.build_ctx(_req("verdict", [chunks[0], small], [], unit=_unit(2_000), variant="fwd"), LIM_1M)
    assert ctx["fold"] == {"K07": "K07.1"}
    ctx = cr.build_ctx(_req("verdict", [chunks[0], {**card, "alias": "K07"}], [], unit=_unit(2_000), variant="fwd"),
                       LIM_1M)
    assert ctx["fold"] == {}
    ctx = cr.build_ctx(_req("verdict", chunks[:2], [], unit=_unit(2_000), variant="fwd"), LIM_1M)
    assert ctx["fold"] == {}, "조각이 둘이면 어느 조각인지 모른다"


def test_판정_행_없이_끝난_조각이_있으면_그_카드는_빠진_카드다(client200k, monkeypatch):
    """solo(조건 줄 c 를 함께 낸다)에서 둘째 조각만 판정 행을 끝내 안 낸다 — 보통 run 이 보충까지 묻고도 빠진 조각이다. 나머지 조각의
    OK 로 카드를 '판정함' 으로 세우지 않는다(그 조각에 걸림이 있을 수 있다) — 판정 행 없이 missing 에 원래 별칭을 싣는다."""
    def no_verdict_on_2(system: str, human: str) -> list:
        rows = default_oracle(system, human)
        return [r for r in rows if not (r.get("t") == "v" and r.get("k") == "K07.2")]

    card = _long_card()
    n = _n_chunks(card)
    llm = FakeLLM(["oracle"], oracle=no_verdict_on_2)
    monkeypatch.setattr(a.app.state, "delib_llm", llm, raising=False)
    fr = _frames(client200k.post("/card-review/run", json=_req("verdict", [card], [], unit=_unit(2_000), variant="solo")).text)
    assert fr[-2][0] == "result", fr[-2]
    result = fr[-2][1]
    assert [x for x in result["rows"] if x["t"] == "v"] == [] and result["missing"] == ["K07"], result["missing"]
    assert result["meta"]["chunked"]["unjudged"] == ["K07.2"] and result["meta"]["chunked"]["reconcile"] is None
    assert (result["quality"]["expected"], result["quality"]["missing"]) == (1, 1)
    assert len(llm.calls) > n, "빠진 조각은 보통 run 이 보충까지 물었다"

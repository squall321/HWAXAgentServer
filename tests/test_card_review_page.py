# 카드 밖 · 입력 결손의 반드시 전문인 카드(extra.must_full) — 몫이 아니라 창까지 싣고, 넘치면 쪽으로 나눠 부른다(HWAXPortal docs/risk-200k W-D15)
"""왜 — 20만 창에서 카드 밖 · 입력 결손 호출의 '몫 안 카드 자리' 는 2만여 자라 훑기에 걸린 카드 한 장도 제목으로 내려갔다(bg_dropped).
창 자체는 12만~14만 자를 허용한다.

지키는 것 — ① must_full 이 없으면 프롬프트가 종전과 바이트 그대로 ② must_full 카드는 몫을 넘어 창까지 전문(soft_over), 나머지만
제목 ③ 창 한 번에 안 들면 쪽마다 부르고(status{page, of}) 한 result 로 합친다 ④ 쪽 자리보다 긴 카드는 조각으로 쪽에 싣고 근거는
원래 별칭으로 접는다 ⑤ 합치기 — 같은 줄은 하나로, m 은 합, more 는 '더 있다' 가 이기고, none 은 지적이 없을 때만 ⑥ 쪽 하나가
끝내 실패하면 그 error 로 끝낸다 ⑦ 검증 — offcard · noinput 에만, cards 안의 별칭만.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
from fakes.llm import FakeLLM, default_oracle, read_prompt  # noqa: E402
from test_card_review_chunk import _long_card  # noqa: E402
from test_card_review_http import _frames  # noqa: E402
from test_card_review_prompts import _req, _unit  # noqa: E402

LIM = cr.limits(200_000, "server", 0, env={})
MECHS = [{"code": "thermal.cte_mismatch", "label": "열팽창 불일치"}]


def _card(alias: str, chars: int, seed: int) -> dict:
    """chars 자 남짓의 카드 — 절 둘(문단 · 문장 끝 '다.')."""
    c = _long_card(alias, secs=(chars // 2, chars - chars // 2), seed=seed)
    c = {**c, "record_id": f"DOC-PAGE-{seed:04d}", "card_id": f"C-{seed}"}
    c["chars"] = len(cr.render_card(c, alias))
    return c


def _noinput(cards, must, **kw):
    return _req("noinput", cards, [], unit=_unit(0), extra={"mechanisms": MECHS, "must_full": must, **kw})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 200_000, "src": "server"})
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    return TestClient(a.app)


def test_must_full_이_없거나_몫_안이면_프롬프트가_종전과_같다():
    cards = [_card(f"K0{i}", 3_000, i) for i in range(1, 4)]
    base = cr.build_prompt(_req("noinput", cards, [], unit=_unit(0), extra={"mechanisms": MECHS}), LIM)
    same = cr.build_prompt(_noinput(cards, ["K01", "K02"]), LIM)
    assert base[:2] == same[:2] and base[2]["bg_dropped"] == same[2]["bg_dropped"] == 0
    assert cr.prepare(_noinput(cards, ["K01"]), LIM).get("paged") is None


def test_반드시_전문인_카드는_몫을_넘어_창까지_싣고_나머지만_제목이다():
    cards = [_card("K01", 45_000, 1), _card("K02", 45_000, 2), _card("K03", 45_000, 3)]
    room = LIM["noinput"]["body"] - cr.unit_size(_unit(0))
    assert all(c["chars"] > room for c in cards), "카드 한 장도 몫 안 자리에 안 든다 — 종전에는 셋 다 제목이었다"
    _s, plain, ctx0 = cr.build_prompt(_req("noinput", cards, [], unit=_unit(0), extra={"mechanisms": MECHS}), LIM)
    assert ctx0["bg_dropped"] == 3
    _s, human, ctx = cr.build_prompt(_noinput(cards, ["K03", "K01"]), LIM)
    assert ctx["card_order"] == ["K01", "K03"] and ctx["bg_dropped"] == 1 and ctx["soft_over"]
    assert "[K01] 종류 " in human and "[K03] 종류 " in human and "[K02] 종류 " not in human
    assert "K02 " in human.split("[카드 제목")[1].split("\n\n")[0], "반드시 전문 밖의 카드는 제목으로 남는다"


def test_창_한_번에_안_들면_쪽으로_나눠_부르고_한_결과로_합친다(client):
    cards = [_card(f"K0{i}", 50_000, i) for i in range(1, 6)] + [_card("K06", 3_000, 6)]
    must = [c["alias"] for c in cards[:5]]
    prep = cr.prepare(_noinput(cards, must), LIM)
    pages = prep["paged"]["pages"]
    assert len(pages) >= 2 and sorted(a for p in pages for a in p) == must, "반드시 전문인 카드가 정확히 한 쪽씩"
    for sub, page in zip(prep["paged"]["subs"], pages):
        assert sub["ctx"]["card_order"] == page and len(sub["system"]) + len(sub["human"]) <= LIM["p_hard"]
        assert {"K06", *[x for x in must if x not in page]} <= set(sub["ctx"]["digest"]), "다른 쪽 카드는 제목"
    fr = _frames(client.post("/card-review/run", json=_noinput(cards, must)).text)
    assert [(d["page"], d["of"]) for e, d in fr if e == "status"] == [(i, len(pages)) for i in range(1, len(pages) + 1)]
    assert fr[-2][0] == "result", fr[-2]
    res = fr[-2][1]
    q1 = [r for r in res["rows"] if r["t"] == "q1"]
    assert {r["k"] for r in q1} == {p[0] for p in pages} | {p[1] for p in pages if len(p) > 1}, "쪽마다의 근거가 다 남는다"
    assert all(r["checks"]["quote"] == "ok" for r in q1)
    assert [r["t"] for r in res["rows"]].count("more") == 1 and res["missing"] == []
    meta = res["meta"]
    assert meta["paged"] == {"n": len(pages), "pages": pages, "must_full": 5} and meta["bg_dropped"] == 1
    assert set(meta["aliases"]["cards"]) == {c["alias"] for c in cards} and len(a.app.state.delib_llm.calls) == len(pages)


def test_쪽_자리보다_긴_카드는_조각으로_쪽에_싣고_근거를_원래_별칭으로_접는다(client):
    big = _card("K01", 300_000, 11)
    cards = [big, _card("K02", 20_000, 12)]
    prep = cr.prepare(_noinput(cards, ["K01", "K02"]), LIM)
    flat = [x for p in prep["paged"]["pages"] for x in p]
    assert [x for x in flat if x.startswith("K01.")] and "K02" in flat, flat
    fr = _frames(client.post("/card-review/run", json=_noinput(cards, ["K01", "K02"])).text)
    res = fr[-2][1]
    ks = {r["k"] for r in res["rows"] if r["t"] in ("q1", "q2") and r.get("k")}
    assert ks <= {"K01", "K02"} and "K01" in ks, "조각 별칭(K01.n)이 결과에 남지 않는다"
    assert all(r["checks"]["quote"] == "ok" for r in res["rows"] if r["t"] == "q1" and r["k"] == "K01")
    assert res["meta"]["paged"]["pages"] == prep["paged"]["pages"]


def test_카드_밖_쪽_합치기는_m_은_합_more_는_더_있다가_이기고_none_은_지적이_없을_때만(client, monkeypatch):
    """첫 쪽은 지적 없이 none · more=no, 둘째 쪽은 지적 하나 · 본 메커니즘 · more=yes — 합친 답은 지적 하나 · none 없음 · more=yes ·
    maybe_more 참이다."""
    def per_page(system: str, human: str) -> list:
        p = read_prompt(human)
        rows = default_oracle(system, human)
        if p["kind"] != "offcard":
            return rows
        if "K01" in p["cards"]:
            return [{"t": "none", "why": "이 쪽의 카드로는 카드 밖 리스크가 보이지 않는다"}, {"t": "m", "seen": []},
                    {"t": "more", "more": "no"}]
        return [r if r["t"] != "more" else {"t": "more", "more": "yes"} for r in rows if r["t"] != "m"] + [
            {"t": "m", "seen": ["thermal.cte_mismatch"]}]

    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"], oracle=per_page), raising=False)
    cards = [_card(f"K0{i}", 50_000, 20 + i) for i in range(1, 6)]
    body = _req("offcard", cards, [], unit=_unit(0),
                extra={"mechanisms": MECHS, "must_full": [c["alias"] for c in cards]})
    fr = _frames(client.post("/card-review/run", json=body).text)
    res = fr[-2][1]
    ts = [r["t"] for r in res["rows"]]
    assert ts.count("f") == 1 and "none" not in ts and ts.count("m") == 1 and ts.count("more") == 1, ts
    assert next(r for r in res["rows"] if r["t"] == "m")["seen"] == ["thermal.cte_mismatch"]
    assert next(r for r in res["rows"] if r["t"] == "more")["more"] == "yes" and res["quality"]["maybe_more"] is True


def test_쪽_하나가_끝내_실패하면_그_error_로_끝낸다(client, monkeypatch):
    cards = [_card(f"K0{i}", 50_000, 30 + i) for i in range(1, 6)]
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle", "http_400"]), raising=False)
    fr = _frames(client.post("/card-review/run", json=_noinput(cards, [c["alias"] for c in cards])).text)
    names = [e for e, _ in fr]
    assert "result" not in names and names[-2:] == ["error", "done"], names
    assert (fr[-2][1]["code"], fr[-2][1]["page"]) == ("llm_rejected", 2)


def test_must_full_은_카드_밖_입력_결손에만_cards_안의_별칭만():
    cards = [_card("K01", 3_000, 41)]
    with pytest.raises(cr.RequestError) as e:
        cr.build_ctx(_req("verdict", cards, [], unit=_unit(0), variant="fwd", extra={"must_full": ["K01"]}), LIM)
    assert e.value.code == "bad_request"
    with pytest.raises(cr.RequestError) as e:
        cr.build_ctx(_noinput(cards, ["K09"]), LIM)
    assert e.value.code == "bad_request" and "K09" in str(e.value)
    assert cr.validate_run_request(_noinput(cards, ["K01"]), LIM) == []


def test_입력_결손_합치기는_없음_자리_줄을_실제_줄이_있으면_뺀다():
    rows = [{"t": "q1", "impact": "no", "title": "", "k": "", "why": "이 쪽에는 영향이 없다"},
            {"t": "q2", "item": "", "needs": "", "k": "", "why": "이 쪽에는 확인할 것이 없다"},
            {"t": "q1", "impact": "yes", "title": "간극 축소", "k": "K02", "why": "겹친다"},
            {"t": "q2", "item": "부품 배치", "needs": "ECAD", "k": "", "why": "대조한다"},
            {"t": "more", "more": "no"}, {"t": "more", "more": "no"}]
    out = cr._merge_page_rows("noinput", rows)
    assert [(r["t"], r.get("impact"), r.get("item")) for r in out] == [
        ("q1", "yes", None), ("q2", None, "부품 배치"), ("more", None, None)]
    only_no = cr._merge_page_rows("noinput", rows[:2] + rows[4:])
    assert [r["t"] for r in only_no] == ["q1", "q2", "more"], "실제 줄이 없으면 '없음' 자리 줄이 남는다"


def test_더_있다는_쪽의_답을_모은다_합친_줄_수로_다시_세지_않는다(monkeypatch):
    """쪽마다 '더 없다' 인데 쪽들의 q1 을 합치니 상한(items_max)을 넘는다 — 쪽마다는 상한에 안 닿았으니 '더 있다' 가 아니다(합친 수로
    셈하면 앱이 쓸데없이 다시 묻는다)."""
    monkeypatch.setattr(a, "_ctx_cache", {"n": 32_768, "src": "server"})
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    lim = cr.limits(32_768, "server", 0, env={})
    cards = [_card(f"K{i:02d}", 6_000, 70 + i) for i in range(1, 17)]
    body = _noinput(cards, [c["alias"] for c in cards])
    res = _frames(TestClient(a.app).post("/card-review/run", json=body).text)[-2][1]
    q1 = [r for r in res["rows"] if r["t"] == "q1"]
    assert len(q1) >= lim["noinput"]["items_max"] and res["meta"]["paged"]["n"] >= 2, (len(q1), lim["noinput"]["items_max"])
    assert res["quality"]["maybe_more"] is False


def test_쪽의_조각에_원래_별칭으로_답해도_근거가_원래_카드로_선다(client, monkeypatch):
    """쪽에 실린 조각(K01.2)에 모델이 원래 별칭(K01)으로 근거를 대도 그 조각의 근거로 읽는다(build_ctx 의 fold) — 버리면 근거 카드가
    빈 줄이 된다."""
    def parent_alias(system: str, human: str) -> list:
        rows = default_oracle(system, human)
        for r in rows:
            if r.get("t") in ("q1", "q2") and r.get("k"):
                r["k"] = r["k"].split(".")[0]
        return rows

    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"], oracle=parent_alias), raising=False)
    cards = [_card("K01", 300_000, 81), _card("K02", 20_000, 82)]
    res = _frames(client.post("/card-review/run", json=_noinput(cards, ["K01", "K02"])).text)[-2][1]
    q1 = [r for r in res["rows"] if r["t"] == "q1"]
    assert q1 and all(r["k"] in ("K01", "K02") for r in q1) and "K01" in {r["k"] for r in q1}, q1
    assert all("k:K01" not in r["checks"]["coerced"] for r in q1)

# 단위가 홀로 창을 넘을 때 — run 이 단위를 쪽으로 나눠 부르고 카드 × 단위 한 결과로 합친다(HWAXPortal docs/risk-200k W-D22)
"""왜 — 20만 창에서 단위 하나가 14만 자 남짓(단위 홀로 자리 — 창 한도에서 카드 밖 틀 몫을 뺀 것)을 넘으면 계획이 oversize.units 로
내고 앱이 잡을 ctx_too_small 로 세웠다(D-22 의 마지막 남은 멈춤). 카드 조각(W-D16)은 카드를 단위에 맞추지만, 단위가 홀로 넘으면
카드 자리가 없다.

지키는 불변식 —
① 멈춤 0 — 그런 단위도 계획이 oversize 로 내지 않고(unit_paged) run 이 끝까지 결과를 낸다(판정 · 카드 밖 · 입력 결손)
② 카드 누락 0 — 판정은 쪽마다 묶음의 카드 전부를 싣고, 한 쪽이라도 그 카드의 판정 행이 없으면 빠진 카드로 낸다(다른 쪽 OK 로 덮지 않는다)
③ 줄 누락 0 — 단위의 줄(변경 · 문맥 · 근거)은 모두 어느 쪽엔가 실린다(쪽 자리보다 긴 줄은 나눠 실어 이으면 원문이다) · 머리는 쪽마다
④ 전문 노출의 정직 — meta.aliases.cards 는 모든 쪽에 전문으로 실린 카드만(리스크 앱 W3 커버리지 · 불변식 G10 의 입력)
⑤ 실제로 드는 단위는 그대로 — 선언 몫으로 셈한 문턱을 넘어도 통째로 들면 쪽으로 나누지 않는다 · 문턱 손잡이는 판에 들지 않는다
⑥ 쪽 하나가 끝내 실패하면 그 error 로 끝낸다 — 받은 쪽만 합쳐 '판정함' 으로 두지 않는다
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
from fakes.llm import FakeLLM, default_oracle  # noqa: E402
from test_card_review_chunk import _long_card  # noqa: E402
from test_card_review_http import _frames  # noqa: E402
from test_card_review_prompts import _req  # noqa: E402

LIM = cr.limits(200_000, "server", 0, env={})
MECHS = [{"code": "thermal.cte_mismatch", "label": "열팽창 불일치"}]
DOMAINS = [{"key": "mech", "label": "기구"}, {"key": "rel", "label": "신뢰성"}]
SHARE = "CARD_REVIEW_UNIT_PAGE_SHARE"
CONTRACT = ROOT / "tests" / "fixtures" / "card_review" / "contract"


def _big_unit(target: int, *, big_line: int = 0, n_ctx: int = 2, n_ev: int = 2) -> dict:
    """unit_size 가 target 에 닿는 단위 — 참조가 다 다른 변경 줄로 채운다. big_line 이면 그만큼 긴 근거 줄 하나(문장 끝 '다.')를 더한다."""
    u = {"unit_id": "u-big", "kind": "bundle", "title": "후면 커버 둘레 실링(큰 단위)", "summary": "PSA 폭 1.2 → 0.9 mm 외 변경 다수",
         "notes": "penetration_depth 는 하한 추정치다.", "missing": [{"kind": "ecad", "note": "회로 변경 정보가 없다"}],
         "lines": [],
         "context": [{"ref": f"c:ctx{i}", "text": "이웃 단위의 변경 줄", "owner_unit": "u-02"} for i in range(n_ctx)],
         "evidence": [{"ref": f"tool:call-{i}", "text": "list_interfaces → touching 3곳"} for i in range(n_ev)]}
    if big_line:
        text, k = "", 0
        while len(text) < big_line:          # 문장마다 번호 — 조각이 원문의 어느 자리인지 하나로 정해진다
            text += f"{k}번째 조회 — 보스 둘레 리브의 접촉 압력이 국부적으로 오른다. "
            k += 1
        u["evidence"].append({"ref": "tool:dump", "text": text[:big_line]})
    i = 0
    while cr.unit_size(u) < target:
        u["lines"].append({"ref": f"c:{i:012x}", "text": f"변경 {i} — 후면 커버 둘레 접착 계면의 치수가 바뀌었다. " * 3})
        i += 1
    return u


def _card(alias: str, chars: int, seed: int, checkable: bool = True) -> dict:
    c = _long_card(alias, secs=(chars // 2, chars - chars // 2), seed=seed)
    c = {**c, "record_id": f"DOC-UP-{seed:04d}", "card_id": f"C-{seed}", "checkable": checkable}
    c["chars"] = len(cr.render_card(c, alias))
    return c


def _verdict(cards, unit, variant="fwd"):
    return _req("verdict", cards, [], unit=unit, variant=variant)


def _items(unit: dict) -> list:
    return [(part, x["ref"], x["text"]) for part in ("lines", "context", "evidence") for x in unit.get(part) or []]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(a, "_ctx_cache", {"n": 200_000, "src": "server"})
    monkeypatch.setattr(a, "VLLM_MODEL", "fake-glm")
    monkeypatch.setattr(a, "DELIB_HEARTBEAT_S", 0)
    monkeypatch.setattr(a, "_delib_load", lambda: (0, 0))
    monkeypatch.delenv(SHARE, raising=False)
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"]), raising=False)
    monkeypatch.setattr(a.app.state, "review_gate", None, raising=False)
    return TestClient(a.app)


def _result(client, body) -> dict:
    fr = _frames(client.post("/card-review/run", json=body).text)
    assert fr[-2][0] == "result", fr[-2]
    return fr[-2][1]


# ── ① 멈춤 0 — 계획 ─────────────────────────────────────────────────────────────
def test_계획은_홀로_창을_넘는_단위를_멈추지_않고_unit_paged_로_낸다():
    cards = [{"alias": "K01", "record_id": "D-1", "checkable": True, "chars": 3_000},
             {"alias": "K02", "record_id": "D-2", "checkable": False, "chars": 5_000}]
    room = cr.unit_room(LIM)
    units = [{"unit_id": "u-big", "manifest_chars": 300, "body_chars": room + 1},
             {"unit_id": "u-edge", "manifest_chars": 300, "body_chars": room},
             {"unit_id": "u-small", "manifest_chars": 300, "body_chars": 2_000}]
    pl = cr.plan(cards, units, LIM)
    assert pl["oversize"]["units"] == [] and pl["unit_paged"] == ["u-big"], pl
    assert "u-big" not in {p["unit_id"] for p in pl["pair_over"]}, "쪽은 묶음에 맞춰 짓는다 — 짝 넘침으로 solo 를 시키지 않는다"
    assert "unit_paged" not in cr.plan(cards, units[1:], LIM), "빈 칸은 싣지 않는다 — 기존 계획 예제가 바이트 그대로다"
    l1m = cr.limits(1_048_576, "server", 0, env={})
    big = [{"unit_id": "u-big", "manifest_chars": 300, "body_chars": cr.unit_room(l1m) + 1}]
    assert cr.plan(cards, big, l1m)["unit_paged"] == ["u-big"], "같은 코드가 창을 따른다(1M 에서는 89만 자 남짓부터)"


def test_단위_홀로_자리는_창에서_유도한다():
    """단위 홀로 자리 = 창 한도 − 단위를 싣는 세 종류 가운데 가장 큰 틀 몫(판정은 돌릴 바닥만큼 카드 자리를 더). 20만 · 출력 32K 에서
    140,291자(PLAN 의 '14만 자 남짓'), 1M 에서 89만 자 남짓이다."""
    l32 = cr.limits(200_000, "server", 32_000, env={})
    want = l32["p_hard"] - max(l32["offcard"]["fixed"], l32["noinput"]["fixed"], l32["verdict"]["fixed"] + cr.RUN_FLOOR)
    assert cr.unit_room(l32) == want == 140_291
    assert 880_000 < cr.unit_room(cr.limits(1_048_576, "server", 0, env={})) < 910_000


# ── ① · ② 판정 ─────────────────────────────────────────────────────────────────
def test_판정_묶음은_쪽마다_부르고_카드마다_한_행으로_합친다(client):
    cards = [_card(f"K0{i}", 3_000, i) for i in range(1, 4)]
    unit = _big_unit(200_000)
    body = _verdict(cards, unit)
    prep = cr.prepare(body, LIM)
    up = prep["unit_paged"]
    n = len(up["subs"])
    assert n >= 2
    for sub in up["subs"]:
        assert sub["ctx"]["card_order"] == ["K01", "K02", "K03"], "쪽마다 묶음의 카드 전부"
        assert len(sub["system"]) + len(sub["human"]) <= LIM["p_hard"]
    fr = _frames(client.post("/card-review/run", json=body).text)
    marks = {(d.get("unit_page"), d.get("unit_pages")) for e, d in fr if e == "status"}
    assert marks == {(i, n) for i in range(1, n + 1)}, marks
    assert fr[-2][0] == "result", fr[-2]
    res = fr[-2][1]
    parent = cr.build_ctx(body, LIM)
    vs = [r for r in res["rows"] if r["t"] == "v"]
    assert sorted(r["k"] for r in vs) == ["K01", "K02", "K03"] and res["missing"] == []
    for r in vs:
        assert r["l"] and set(r["l"]) <= set(parent["lines"]), "줄 별칭은 원래 단위의 별칭이다"
        assert r["refs"] == [parent["lines"][x]["ref"] for x in r["l"]]
        assert r["checks"]["quote"] == "ok" and len(r["checks"]["pages"]) == n
    meta = res["meta"]
    assert meta["unit_paged"]["n"] == n and set(meta["aliases"]["cards"]) == {"K01", "K02", "K03"}
    assert meta["aliases"]["lines"] == {x: y["ref"] for x, y in parent["lines"].items()}
    assert len(a.app.state.delib_llm.calls) == n


def test_한_쪽이라도_그_카드의_판정이_없으면_빠진_카드로_낸다(client, monkeypatch):
    def drop_k02_on_page2(system: str, human: str) -> list:
        rows = default_oracle(system, human)
        return [r for r in rows if not (r.get("k") == "K02" and "[쪽 2/" in human)]

    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"], oracle=drop_k02_on_page2), raising=False)
    cards = [_card(f"K0{i}", 3_000, 10 + i) for i in range(1, 3)]
    res = _result(client, _verdict(cards, _big_unit(200_000)))
    assert [r["k"] for r in res["rows"] if r["t"] == "v"] == ["K01"], "둘째 쪽에서 판정 못 한 K02 를 첫 쪽의 OK 로 세우지 않는다"
    assert res["missing"] == ["K02"] and res["meta"]["unit_paged"]["unjudged"] == {"K02": [2]}


def test_쪽마다_판정이_다르면_가장_무거운_판정이_서고_그_쪽의_줄을_댄다(client, monkeypatch):
    def fail_on_page2(system: str, human: str) -> list:
        rows = default_oracle(system, human)
        if "[쪽 2/" in human:
            for r in rows:
                if r.get("k") == "K01":
                    r.update(judgement="FAIL", why="이 쪽의 변경 줄이 카드의 한계를 넘는다", sev="중대")
        return rows

    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle"], oracle=fail_on_page2), raising=False)
    cards = [_card(f"K0{i}", 3_000, 20 + i) for i in range(1, 3)]
    body = _verdict(cards, _big_unit(200_000))
    prep = cr.prepare(body, LIM)
    first2 = next(iter(prep["unit_paged"]["subs"][1]["ctx"]["lines"]))
    want = prep["unit_paged"]["maps"][1][first2]
    res = _result(client, body)
    k1 = next(r for r in res["rows"] if r["t"] == "v" and r["k"] == "K01")
    assert (k1["applies"], k1["judgement"]) == ("yes", "FAIL") and k1["l"] == [want], k1
    pages = k1["checks"]["pages"]
    assert [p["judgement"] for p in pages][:2] == ["OK", "FAIL"] and pages[1]["page"] == 2
    k2 = next(r for r in res["rows"] if r["t"] == "v" and r["k"] == "K02")
    assert k2["judgement"] == "OK" and len(k2["l"]) == len(prep["unit_paged"]["subs"]), "OK 끼리는 쪽들의 줄을 다 모은다"


# ── ③ 줄 누락 0 ────────────────────────────────────────────────────────────────
def _covered(text: str, pieces: list) -> bool:
    """조각들이 원문의 부분 문자열이고 이어 붙이면(겹친 머리를 빼면) 원문 전체를 덮는가."""
    spans, at = [], 0
    for p in pieces:
        i = text.find(p, max(0, at - len(p)))
        if i < 0:
            return False
        spans.append((i, i + len(p)))
        at = i + len(p)
    end = 0
    for lo, hi in sorted(spans):
        if lo > end:
            return False
        end = max(end, hi)
    return end == len(text)


def test_단위의_줄은_모두_어느_쪽엔가_실리고_머리는_쪽마다_긴_줄은_나눠_이으면_원문():
    unit = _big_unit(60_000, big_line=250_000)
    body = _verdict([_card("K01", 3_000, 31)], unit)
    prep = cr.prepare(body, LIM)
    subs = prep["unit_paged"]["subs"]
    assert len(subs) >= 3
    got: dict = {}
    for i, sub in enumerate(subs, 1):
        pu = sub["req"]["unit"]
        assert {k: pu[k] for k in ("unit_id", "title", "summary", "notes", "missing")} == \
            {k: unit[k] for k in ("unit_id", "title", "summary", "notes", "missing")}, "머리는 쪽마다 그대로"
        assert (pu["page"]["index"], pu["page"]["of"]) == (i, len(subs)) and f"[쪽 {i}/{len(subs)} " in sub["human"]
        assert cr.unit_size(pu) <= prep["unit_paged"]["room"]
        assert "변경 줄 " in sub["human"] and "(전부다)" in sub["human"], "모든 쪽에 다 실린 변경 줄은 종전 머리 그대로"
        for part, ref, text in _items(pu):
            got.setdefault((part, ref), []).append(text)
    for part, ref, text in _items(unit):
        pieces = got.get((part, ref))
        assert pieces, f"{part} {ref} 가 어느 쪽에도 없다"
        if ref != "tool:dump":
            assert text in pieces
    dump = next(x["text"] for x in unit["evidence"] if x["ref"] == "tool:dump")
    assert len(got[("evidence", "tool:dump")]) >= 2 and _covered(dump, got[("evidence", "tool:dump")])
    cuts = [c for sub in subs for c in sub["req"]["unit"]["page"]["cut"]]
    assert cuts and all("나눔" in sub["human"] for sub in subs if sub["req"]["unit"]["page"]["cut"])
    again = cr.prepare(body, LIM)["unit_paged"]["subs"]
    assert [s["req"]["unit"] for s in again] == [s["req"]["unit"] for s in subs], "같은 단위면 같은 쪽(결정적)"


def test_변경_줄이_쪽_자리의_반을_넘으면_변경_줄도_나눠_싣고_머리가_몫을_말한다():
    unit = _big_unit(200_000)
    prep = cr.prepare(_verdict([_card("K01", 3_000, 32)], unit), LIM)
    subs = prep["unit_paged"]["subs"]
    counts = [len(s["req"]["unit"]["lines"]) for s in subs]
    assert sum(counts) == len(unit["lines"]) and all(counts), "변경 줄이 정확히 한 쪽씩"
    assert all(s["req"]["unit"]["context"] == unit["context"] and s["req"]["unit"]["evidence"] == unit["evidence"]
               for s in subs), "작은 무리(문맥 · 근거)는 모든 쪽에"
    assert "(전부다)" not in subs[0]["human"] and f"단위 전체 {len(unit['lines'])}줄" in subs[0]["human"]


# ── ① · ④ 카드 밖 · 입력 결손 ──────────────────────────────────────────────────
def test_입력_결손도_쪽으로_돌고_반드시_전문인_카드는_모든_쪽에_전문이다(client):
    cards = [_card("K01", 30_000, 41, False), _card("K02", 30_000, 42), _card("K03", 3_000, 43, False)]
    body = _req("noinput", cards, [], unit=_big_unit(200_000), extra={"mechanisms": MECHS, "must_full": ["K01", "K02"]})
    prep = cr.prepare(body, LIM)
    n = len(prep["unit_paged"]["subs"])
    assert n >= 2
    res = _result(client, body)
    meta = res["meta"]
    assert set(meta["aliases"]["cards"]) == {"K01", "K02"} and meta["must_dropped"] == 0 and meta["bg_dropped"] == 1
    parent = cr.build_ctx(body, LIM)
    q1 = [r for r in res["rows"] if r["t"] == "q1"]
    assert q1 and {r["k"] for r in q1} <= {"K01", "K02"} and all(set(r["l"]) <= set(parent["lines"]) for r in q1)
    assert [r["t"] for r in res["rows"]].count("more") == 1 and res["missing"] == []
    assert meta["unit_paged"]["n"] == n


def test_카드_밖도_쪽으로_돌고_합친_결과는_쪽마다_따로_부른_것을_합친_것과_같다(client):
    cards = [_card("K01", 20_000, 51, False), _card("K02", 4_000, 52, False)]
    body = _req("offcard", cards, [], unit=_big_unit(200_000),
                extra={"mechanisms": MECHS, "domains": DOMAINS, "must_full": ["K01"]})
    prep = cr.prepare(body, LIM)
    res = _result(client, body)
    one_by_one = [_result(client, sub["req"]) for sub in prep["unit_paged"]["subs"]]
    merged = cr.merge_unit_paged(prep, one_by_one)
    strip = lambda x: json.loads(json.dumps({**x, "meta": {**x["meta"], "llm_ms": []}}))  # noqa: E731
    assert strip(merged) == strip(res), "run 의 합치기와 쪽마다 따로 부른 답의 합치기가 같다"
    assert set(res["meta"]["aliases"]["cards"]) == {"K01"} and res["meta"]["bg_dropped"] == 1
    ts = [r["t"] for r in res["rows"]]
    assert ts.count("m") == 1 and ts.count("more") == 1 and "f" in ts


def test_전문_노출은_모든_쪽에_전문으로_실린_카드만_센다(client):
    """한 쪽에서라도 제목으로 내려간 카드는 그 단위에 전문으로 맞대어지지 않았다 — 교집합만 전문이다(W3 커버리지가 거짓 '전문' 을
    세면 놓친 카드가 G10 을 지난다)."""
    cards = [_card("K01", 20_000, 61, False), _card("K02", 20_000, 62, False)]
    body = _req("noinput", cards, [], unit=_big_unit(200_000), extra={"mechanisms": MECHS, "must_full": ["K01", "K02"]})
    prep = cr.prepare(body, LIM)
    res = [_result(client, sub["req"]) for sub in prep["unit_paged"]["subs"]]
    last = res[-1]["meta"]
    last["aliases"]["cards"].pop("K02")
    last["must_dropped"] = 1
    merged = cr.merge_unit_paged(prep, res)
    assert set(merged["meta"]["aliases"]["cards"]) == {"K01"}
    assert merged["meta"]["must_dropped"] == 1 and merged["meta"]["bg_dropped"] == 1


# ── 조각과 함께 ────────────────────────────────────────────────────────────────
def test_큰_카드와_큰_단위가_만나면_쪽마다_카드를_조각_내_돈다(client):
    """카드가 쪽 옆에 들면(12만 자) 쪽 자리를 카드에 맞춰 줄이고 조각 내지 않는다. 한 장 자리를 넘는 카드(16만 자)는 어느 쪽 옆에도 못
    든다 — 자리를 반씩 나눠(쪽 수 × 조각 수가 가장 작다) 쪽마다 카드를 조각으로 돈다."""
    beside = _verdict([_card("K01", 120_000, 70)], _big_unit(200_000), variant="solo")
    assert not any(s.get("chunked") for s in cr.prepare(beside, LIM)["unit_paged"]["subs"])
    card = _card("K01", 160_000, 71)
    assert card["chars"] > cr.card_room(LIM)
    body = _verdict([card], _big_unit(200_000), variant="solo")
    prep = cr.prepare(body, LIM)
    subs = prep["unit_paged"]["subs"]
    assert prep["unit_paged"]["room"] == (LIM["p_hard"] - LIM["verdict"]["fixed"]) // 2
    assert len(subs) >= 2 and all(s.get("chunked") for s in subs), "쪽마다 카드를 조각으로"
    res = _result(client, body)
    vs = [r for r in res["rows"] if r["t"] == "v"]
    assert [r["k"] for r in vs] == ["K01"] and res["missing"] == [] and vs[0]["checks"]["quote"] == "ok"
    calls = sum(len(s["chunked"]["subs"]) for s in subs)
    assert len(a.app.state.delib_llm.calls) >= calls


# ── ⑤ 그대로인 것 · 문턱 손잡이 ────────────────────────────────────────────────
def test_선언_몫으로는_넘어도_보충까지_실제로_드는_단위는_쪽으로_나누지_않는다():
    """문턱은 선언한 틀 몫(역할 · 배경 제목 · 메커니즘 몫을 끝까지 쓴다고 본다)으로 셈해 넉넉하다. 역할이 짧고 카드 한 장인 판정은 문턱을
    넘는 단위도 보충 지적 블록까지 창 안에 든다 — 나누지 않는다(호출이 준다). 보충 시도가 넘치는 단위만 나눈다(빠진 카드를 다시 물어야
    한다)."""
    at = cr.unit_page_at(LIM, env={})
    body = _verdict([_card("K01", 3_000, 97)], _big_unit(at + 4_000))
    assert cr.unit_size(body["unit"]) > at
    prep = cr.prepare(body, LIM)
    assert "unit_paged" not in prep and prep["human"] == cr.build_prompt(body, LIM)[1]
    cr.build_prompt(body, LIM, hint="지적 " * LIM["verdict"]["hint_max"])        # 보충도 든다
    tight = _verdict([_card("K01", 3_000, 97)], _big_unit(LIM["p_hard"] - 5_500))    # 실제 틀 몫 5천 자 남짓 · 지적 1,050자
    cr.build_prompt(tight, LIM)                                                   # 첫 시도는 들지만
    with pytest.raises(cr.OverBudget):
        cr.build_prompt(tight, LIM, hint="지적 " * LIM["verdict"]["hint_max"])     # 보충은 넘친다
    assert "unit_paged" in cr.prepare(tight, LIM), "보충이 넘치는 단위는 나눈다"


def test_문턱은_창에서_유도하고_손잡이는_그_몫이며_판에_들지_않는다():
    room = cr.unit_room(LIM)
    assert cr.unit_page_at(LIM, env={}) == room
    assert cr.unit_page_at(LIM, env={SHARE: "0.5"}) == max(LIM["unit_body_max"], int(room * 0.5))
    assert cr.unit_page_at(LIM, env={SHARE: "0.01"}) == LIM["unit_body_max"], "바닥은 단위 몫(unit_body_max)"
    for bad in ("0", "1.5", "x", "-1", ""):
        assert cr.unit_page_at(LIM, env={SHARE: bad}) == room, bad
    assert cr.limits(200_000, "server", 0, env={SHARE: "0.5"}) == LIM, "판 · 본문이 손잡이를 모른다(run 에서만 읽는다)"


def test_손잡이를_낮추면_묶음과_함께_넘치는_중간_크기_단위도_쪽으로_돈다(monkeypatch):
    cards = [_card(f"K0{i}", 20_000, 80 + i) for i in range(1, 5)]
    body = _verdict(cards, _big_unit(100_000))
    assert cr.unit_size(body["unit"]) < cr.unit_room(LIM)
    monkeypatch.delenv(SHARE, raising=False)
    with pytest.raises(cr.OverBudget):
        cr.prepare(body, LIM)            # 종전 그대로 — 계획이 pair_over 로 알리고 앱이 카드를 solo 로 돌린다
    monkeypatch.setenv(SHARE, "0.5")
    prep = cr.prepare(body, LIM)
    assert len(prep["unit_paged"]["subs"]) >= 2
    assert all(len(s["system"]) + len(s["human"]) <= LIM["p_hard"] for s in prep["unit_paged"]["subs"])


def test_머리가_쪽_자리를_넘는_단위는_LLM_전에_over_budget_이다():
    unit = _big_unit(200_000)
    unit["summary"] = "가" * (LIM["p_hard"] + 10)
    with pytest.raises(cr.OverBudget):
        cr.prepare(_verdict([_card("K01", 3_000, 91)], unit), LIM)


# ── ⑥ 실패 ─────────────────────────────────────────────────────────────────────
def test_쪽_하나가_끝내_실패하면_그_error_로_끝낸다(client, monkeypatch):
    monkeypatch.setattr(a.app.state, "delib_llm", FakeLLM(["oracle", "http_400"]), raising=False)
    fr = _frames(client.post("/card-review/run", json=_verdict([_card("K01", 3_000, 95)], _big_unit(200_000))).text)
    names = [e for e, _ in fr]
    assert "result" not in names and names[-2:] == ["error", "done"], names
    assert (fr[-2][1]["code"], fr[-2][1]["unit_page"]) == ("llm_rejected", 2)


def test_쪽_칸은_수만_싣고_요청_모양이_검사한다():
    unit = _big_unit(200_000)
    sub = cr.prepare(_verdict([_card("K01", 3_000, 96)], unit), LIM)["unit_paged"]["subs"][0]["req"]
    assert cr.validate_run_request(sub, LIM) == [], "쪽 요청도 계약 모양을 지난다(앱이 보내지 않는 엔진 안쪽 칸)"
    bad = json.loads(json.dumps(sub))
    bad["unit"]["page"]["note"] = "아무 글"
    assert cr.validate_run_request(bad, LIM)[0]["code"] == "unknown_fields"

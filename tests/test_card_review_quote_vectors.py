# 인용·수치 대조의 파리티 벡터(quote_vectors.json)를 만들고 엔진의 판정이 벡터와 같은지 본다 — 리스크 앱이 같은 파일로 대조한다
#
# 인용 대조는 두 군데서 돈다 — 엔진의 check_rows(행마다 checks)와 리스크 앱의 얼린 팩 재대조. 한쪽 정규화가 한 글자만
# 달라도 맞는 인용이 불일치가 된다(리포 경계의 E7). 그래서 벡터 한 벌을 두고 두 리포의 시험이 같은 파일을 읽는다(QP1).
# 벡터의 원문은 지어낸 카드를 실제 생산자로 만든 기록(tests/fixtures/card_review/cards)을 normalize_card 에 넣은 것이고,
# 인용은 그 원문에서 잘라 공백·기호를 흔든 것이다(손으로 쓴 정답표가 아니다). 이 시험이 만들고 손으로 고치지 않는다.
#
# 벡터를 다시 뜨려면  CARD_REVIEW_CONTRACT_UPDATE=1 .venv/bin/python -m pytest tests/test_card_review_quote_vectors.py -q
#   실행:  .venv/bin/python -m pytest tests/test_card_review_quote_vectors.py -q
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import card_review as cr  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "card_review"
VECTORS = FIX / "contract" / "quote_vectors.json"
_UPDATE = os.environ.get("CARD_REVIEW_CONTRACT_UPDATE") == "1"


def _card(name: str) -> dict:
    return cr.normalize_card(json.loads((FIX / "cards" / f"{name}.json").read_text(encoding="utf-8")))


def _target(card: dict) -> dict:
    """벡터에 싣는 대조 대상 — 정규 글(절별)이다. 앱은 얼린 팩에서 같은 것을 꺼낸다."""
    return {"record_id": card["record_id"], "card_text": cr.card_text(card),
            "sections": [{"sid": s["sid"], "text": s["text"]} for s in card["sections"]]}


def _quote_vectors() -> list:
    dr, fc, data = _card("design_rule"), _card("failure_case"), _card("data")
    s1 = dr["sections"][0]["text"]            # '정적 접착 실링 부위의 **유효 접착폭**은 … 남긴다.\n폭이 줄면 …'
    s2 = dr["sections"][1]["text"]
    piece = s2[:30]
    v = []

    def add(name, card, q, why, rule):
        v.append({"name": name, "rule": rule, "why": why, "card": _target(card), "q": q})

    add("exact", dr, piece, "원문 그대로", "Q1")
    add("ws_shaken", dr, "  " + piece.replace(" ", "\n   ", 3) + " \t", "공백·개행을 흔들었다", "Q1")
    add("one_char_changed", dr, piece[:10] + ("가" if piece[10] != "가" else "나") + piece[11:], "글자 하나를 바꿨다", "Q2")
    add("other_card", dr, fc["sections"][0]["text"][:30], "같은 호출의 다른 카드 문장", "Q3")
    add("empty", dr, "", "인용이 비었다", "Q6")
    add("ws_only", dr, "   \n  ", "공백뿐이다", "Q6")
    add("json_skeleton", dr, '{"t":"v","k":"K01","l":["L', "JSON 뼈대·구두점뿐인 15자 이상", "Q7")
    add("too_short", dr, s2[:14], "14자", "Q7")
    add("markdown_dropped", dr, s1.split("\n")[0][:40].replace("**", ""), "강조 기호(**)를 빼고 옮겼다", "_nq")
    add("curly_quotes", dr, "“" + piece + "”", "굽은 따옴표로 감쌌다", "_nq")
    add("guillemets", dr, "«" + piece + "»", "인용 구간 기호째 옮겼다", "_nq")
    add("escaped_newline", dr, s1.replace("\n", "\\n")[30:75], "개행을 \\n 글자로 옮겼다", "unescape")
    add("cross_section", dr, s1[-12:] + "\n" + s2[:12], "두 절에 걸친 인용(절 번호는 빈다)", "quote_sid")
    add("table_rows", data, "| 23 ℃ | 12.5 N/cm |\n| 85 ℃ | 7.25 N/cm |", "표 두 줄", "Q1")
    add("table_row_short", data, "| 85 ℃ | 7.25 N/cm |", "표 한 줄 — 실질 문자가 10자 미만", "Q7")
    add("title_not_quotable", dr, dr["title"], "카드 제목 — 머리 줄은 인용 원문이 아니다", "head")
    for x in v:
        status, sid = cr.quote_check(x["q"], {"text": x["card"]["card_text"],
                                              "sections": [(s["sid"], s["text"]) for s in x["card"]["sections"]]})
        x["expect"] = {"quote": status, "quote_sid": sid}
    return v


def _num_vectors() -> list:
    cases = [
        ("5", "유효 폭 0.15 mm", "Q4 — 5 는 0.15 에 걸리지 않는다"),
        ("0.15", "유효 폭 0.15 mm", "Q4 — 0.15 는 0.15 mm 에 걸린다"),
        ("20", "압축률 20~30% 범위", "Q4 — 20 은 20~30% 에 걸린다"),
        ("1,200", "목표 1200 시간", "쉼표는 지우고 본다"),
        ("232", "총 1232 개", "부분문자열이 아니라 토큰이다"),
        ("0.35", "폭 0.3 mm, R 5 mm", "원문에 없는 수치"),
    ]
    out = []
    for num, src, why in cases:
        out.append({"num": num, "source": src, "why": why, "in": num.replace(",", "") in cr.num_tokens(src)})
    return out


def _unsourced_vectors() -> list:
    cases = [
        ("폭이 0.9 mm 로 줄어 하한 0.8 mm 에 가깝다", "하한 0.8 mm\n폭 1.2 → 0.9 mm", "둘 다 원문에 있다"),
        ("폭이 0.35 mm 로 줄었다", "폭 0.3 mm", "지어낸 수치"),
        ("차이 0.3 mm", "1.2 → 0.9 mm", "모델이 셈한 차이(계산값일 수 있다)"),
        ("3개 줄이 바뀌었다", "", "작은 정수는 대조하지 않는다(sig_numbers)"),
        ("규격 9999-2 를 넘는다", "[K01] 종류 design-rule(점검) · 규격 ISO 9999-2", "머리 줄의 규격 번호도 원천이다"),
    ]
    return [{"text": t, "sources": s, "why": w, "unsourced": cr.nums_unsourced(t, s)} for t, s, w in cases]


def _vectors() -> dict:
    return {"norm_rev": cr.NORM_REV, "rules": {"quote_min": cr.QUOTE_MIN, "quote_substantive_min": cr.QUOTE_SUBST_MIN},
            "note": "tests/test_card_review_quote_vectors.py 가 만든다. 손으로 고치지 않는다.",
            "quote": _quote_vectors(), "num_tokens": _num_vectors(), "nums_unsourced": _unsourced_vectors()}


def test_벡터의_기대가_불변식과_맞는다():
    """벡터가 담은 기대 자체가 Q1~Q7 을 말하는지 — 정규화가 틀어져 기대가 함께 틀어지면 파리티는 둘 다 틀려도 통과한다."""
    q = {x["name"]: x["expect"] for x in _quote_vectors()}
    for name in ("exact", "ws_shaken", "markdown_dropped", "curly_quotes", "guillemets", "escaped_newline",
                 "table_rows"):
        assert q[name]["quote"] == "ok", name
    assert q["exact"]["quote_sid"] == "2" and q["cross_section"] == {"quote": "ok", "quote_sid": ""}
    assert q["one_char_changed"]["quote"] == "not_found" and q["other_card"]["quote"] == "not_found"
    assert q["empty"]["quote"] == "empty" and q["ws_only"]["quote"] == "empty"
    assert q["json_skeleton"]["quote"] == "short" and q["too_short"]["quote"] == "short"
    assert q["table_row_short"]["quote"] == "short"
    assert q["title_not_quotable"]["quote"] == "not_found", "카드 제목(머리 줄)이 인용 원문으로 통과했다"
    n = {x["num"]: x["in"] for x in _num_vectors()}
    assert n == {"5": False, "0.15": True, "20": True, "1,200": True, "232": False, "0.35": False}
    u = [x["unsourced"] for x in _unsourced_vectors()]
    assert u == [[], ["0.35"], ["0.3"], [], []]


def test_인용_파리티_벡터():
    got = _vectors()
    if _UPDATE:
        VECTORS.parent.mkdir(parents=True, exist_ok=True)
        VECTORS.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    assert VECTORS.is_file(), "벡터 파일이 없다 — CARD_REVIEW_CONTRACT_UPDATE=1 로 떠라"
    assert got == json.loads(VECTORS.read_text(encoding="utf-8")), (
        "인용·수치 대조가 벡터와 다르다 — 리스크 앱이 같은 파일로 대조한다. 뜻을 바꿨으면 CARD_REVIEW_CONTRACT_UPDATE=1")

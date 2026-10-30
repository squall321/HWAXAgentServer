# HWAXPortal docs/request-1030 HA-32 — 코어 어휘표의 타입 이름 대응(types)을 챗 코어 규칙 ① 에 한 줄로 싣는다
"""10-30 디지털 워커 QA — 챗이 없는 코어 타입 이름을 지어내 불렀다(DW-22 — object_resolve(type_slug='management_standard'), 실제는 criterion).
낱말 → 타입 이름 대응은 포털 리포의 코어 어휘표(정본 backend/config/core_vocab.json 의 types)에서 읽고, cae00 은 박스 덧씌우기
(core_vocab.local.json)로 고친다. 대응이 없는 표(옛 포털)면 규칙 글은 종전과 글자 하나 다르지 않다."""
import hashlib
import json
import os

import app as a

_REAL = a.CORE_VOCAB_PATH          # 형제 포털 리포의 정본 — conftest 가 시험마다 '없음' 으로 돌리기 전의 기본 경로
_BODY = {"version": 1, "canonical": {"app": "hub", "label": "허브"}, "rule_words": ["고장모드", "관리 기준"], "relations": [],
         "types": {"고장모드": "failure_mode", "관리 기준": "criterion", "시험항목": "test_item"}, "apps": {}}
_LINE = "타입 이름(type_slug)은 지어내지 말고 이 대응을 쓰세요 — 고장모드 = failure_mode · 관리 기준 = criterion · 시험항목 = test_item. "


def _rules(sp: str) -> str:
    assert sp.startswith(a._SP_HEAD) and sp.endswith(a._SP_TAIL), "코어 규칙 밖의 글은 그대로다"
    return sp[len(a._SP_HEAD):len(sp) - len(a._SP_TAIL)]


def _write(p, body) -> None:
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def test_HA32_대응은_규칙_1_끝의_한_줄이고_없으면_종전_글_그대로다(monkeypatch, tmp_path):
    p = tmp_path / "core_vocab.json"
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    _write(p, _BODY)
    rules = _rules(a._system_prompt())
    assert rules[rules.index("①"):rules.index("②")].endswith(_LINE), "object_resolve 를 말하는 ① 의 끝, 어휘표의 차례대로"
    assert rules.count("type_slug") == 1
    old = {k: v for k, v in _BODY.items() if k != "types"}
    assert a._core_rules_text(old) == a._core_rules_text({**_BODY, "types": {}}) == rules.replace(_LINE, ""), \
        "대응이 없거나 비면 그 줄만 빠진다 — 옛 포털의 표로 도는 박스는 종전 글 그대로다"


def test_HA32_박스_덧씌우기로_낱말_하나씩_고치고_더하고_지운다(monkeypatch, tmp_path):
    p, loc = tmp_path / "core_vocab.json", tmp_path / "core_vocab.local.json"
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    monkeypatch.setattr(a, "CORE_VOCAB_LOCAL_PATH", loc)
    _write(p, _BODY)
    base = a._core_vocab()
    _write(loc, {"types": {"관리 기준": "management_criterion", "부품": "part", "시험항목": None}})
    d = a._core_vocab()
    assert "이 대응을 쓰세요 — 고장모드 = failure_mode · 관리 기준 = management_criterion · 부품 = part. ②" in d["rules"], \
        "객체라 merge patch 가 낱말 하나씩 바꾸고 · 더하고 · null 로 지운다(목록이면 통째로 바꿔 적어야 한다)"
    assert "test_item" not in d["rules"]
    h = a._core_vocab_health()
    assert set(h["overlay"]["changed"]) == {"types.관리 기준", "types.부품", "types.시험항목"}
    merged = {**_BODY, "types": {"고장모드": "failure_mode", "관리 기준": "management_criterion", "부품": "part"}}
    want = hashlib.sha256(json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    assert h["fingerprint"] == want and h["base"] == base["fingerprint"] != want, \
        "대응도 지문에 든다 — 게이트웨이는 types 를 읽지 않지만 같은 표 전체로 셈한다(update-all §7 이 두 지문을 견준다)"
    _write(loc, {"types": [["관리 기준", "criterion"]]})
    st = loc.stat()
    os.utime(loc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert a._core_vocab()["fingerprint"] == base["fingerprint"] and "덧씌우기를 버렸다" in a._core_vocab_health()["overlay"]["error"], \
        "대응이 객체가 아니면 덧씌우기를 버리고 기본 표로 돈다"


def test_HA32_형제_포털의_정본을_읽어_대응이_있으면_그대로_싣는다(monkeypatch):
    """포털에 types 가 들어오기 전 판에서도 돈다 — 정본에 대응이 있으면 낱말 = 타입마다 ① 에 있고, 없으면 그 줄이 없다."""
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", _REAL)
    vocab = json.loads(_REAL.read_text(encoding="utf-8"))
    rules = _rules(a._system_prompt())
    one = rules[rules.index("①"):rules.index("②")]
    types = vocab.get("types") or {}
    assert ("type_slug" in one) == bool(types)
    for w, slug in types.items():
        assert f"{w} = {slug}" in one, (w, slug)
    assert "management_standard" not in rules

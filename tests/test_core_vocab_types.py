# HWAXPortal docs/request-1030 HA-32 — 코어 어휘표의 타입 이름 대응(types)을 챗 코어 규칙 ① 에 한 줄로 싣는다
"""10-30 디지털 워커 QA — 챗이 없는 코어 타입 이름을 지어내 불렀다(DW-22 — object_resolve(type_slug='management_standard'), 실제는 criterion).
낱말 → 타입 이름 대응은 포털 리포의 코어 어휘표(정본 backend/config/core_vocab.json 의 types)에서 읽고, cae00 은 박스 덧씌우기
(core_vocab.local.json)로 고친다. 대응에 없는 낱말은 정본 search 결과의 types[] 로 확인하라고 같은 줄이 말한다 — 허브의 object_resolve 는
type_slug 가 필수다. 대응이 없는 표(옛 포털)면 규칙 글은 종전과 글자 하나 다르지 않다."""
import hashlib
import json
import os

import pytest

import app as a

_REAL = a.CORE_VOCAB_PATH          # 형제 포털 리포의 정본 — conftest 가 시험마다 '없음' 으로 돌리기 전의 기본 경로
_BODY = {"version": 1, "canonical": {"app": "hub", "label": "허브"}, "rule_words": ["고장모드", "관리 기준"], "relations": [],
         "types": {"고장모드": "failure_mode", "관리 기준": "criterion", "시험항목": "test_item"}, "apps": {}}
_MISS = "대응에 없는 낱말은 짐작하지 말고 정본의 search 결과 types[] 에서 타입 이름을 확인하세요. "
_LINE = "타입 이름(type_slug)은 지어내지 말고 이 대응을 쓰세요 — 고장모드 = failure_mode · 관리 기준 = criterion · 시험항목 = test_item. " + _MISS


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
    assert "이 대응을 쓰세요 — 고장모드 = failure_mode · 관리 기준 = management_criterion · 부품 = part. " + _MISS + "②" in d["rules"], \
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


def test_HA32_덧씌우기의_타입_이름이_허브_slug_꼴이_아니면_그_덧씌우기를_버린다(monkeypatch, tmp_path):
    """타입 이름은 허브 slug 꼴이다(StandardPlatform ontology SLUG_RE — 소문자로 시작하는 영문 · 숫자 · 밑줄 32자 이내). 종전엔 꼴이 아닌 값도
    받았다 — 객체 · 숫자 · 참거짓은 repr 꼴로, 한글 · 대문자 · 긴 이름은 그대로 프롬프트에 실렸고 빈 목록은 줄을 조용히 뺐다. 덧씌우기 오류도 두
    서비스의 지문 차이도 없었다(10-30 반대 검토 — 게이트웨이는 types 를 읽지 않는다). 다른 칸처럼 그 덧씌우기를 버리고 까닭을 남긴다 —
    update-all §7 이 ⚠ 로 낸다."""
    p, loc = tmp_path / "core_vocab.json", tmp_path / "core_vocab.local.json"
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    monkeypatch.setattr(a, "CORE_VOCAB_LOCAL_PATH", loc)
    _write(p, _BODY)
    base = a._core_vocab()
    for i, bad in enumerate(({"부품": {"slug": "part"}}, {"부품": 123}, {"관리 기준": True}, {"관리 기준": "관리기준"},
                             {"관리 기준": "Criterion"}, {"부품": "p" * 33}, [])):
        _write(loc, {"types": bad})
        st = loc.stat()
        os.utime(loc, ns=(st.st_atime_ns, st.st_mtime_ns + (i + 1) * 1_000_000_000))
        d, h = a._core_vocab(), a._core_vocab_health()
        assert d["rules"] == base["rules"] and h["fingerprint"] == base["fingerprint"], (bad, d["rules"][d["rules"].index("①"):])
        assert "덧씌우기를 버렸다(TypeError: types" in h["overlay"]["error"], (bad, h["overlay"])


def test_HA32_형제_포털의_정본을_읽어_대응이_있으면_그대로_싣는다(monkeypatch):
    """형제 포털 정본의 대응이 낱말 = 타입마다 ① 에 있다. 정본에 대응이 없는 판(포털 HA-32 를 합치기 전)이면 건너뛴다 — 그때 통과로 세면
    고친 것을 되돌려도 초록이라 아무것도 지키지 않는다(10-30 반대 검토). 대응이 없을 때 그 줄이 없다는 것은 첫 시험이 본다."""
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", _REAL)
    types = json.loads(_REAL.read_text(encoding="utf-8")).get("types")
    if not types:
        pytest.skip("형제 포털 정본에 types 가 아직 없다 — 포털 HA-32 를 합친 뒤에 돈다")
    rules = _rules(a._system_prompt())
    one = rules[rules.index("①"):rules.index("②")]
    for w, slug in types.items():
        assert f"{w} = {slug}" in one, (w, slug)
    assert one.endswith(_MISS) and "management_standard" not in rules

# HWAXPortal docs/request-1029 의 에이전트 서버 몫 — 코어 어휘표로 만드는 챗 코어 규칙(R-1 · R-2 · R-7 · R-8)
"""10-29 cae00 웹 챗 QA — 고장모드 · 시험항목 · 가상검증 · 관리 기준 · 물성 질문 다섯이 코어를 건너뛰었다(OT-06 ~ 10). 발동 낱말이
'부품 · 모델 · 과제 · 불량' 뿐이었고(R-1), FMEA · ARP · ODB 허브가 코어 질문을 가로챘다(R-2). 낱말은 포털 리포의 코어 어휘표(정본)에서 읽는다."""
import json
import os

import app as a

_REAL = a.CORE_VOCAB_PATH          # 형제 포털 리포의 정본 — conftest 가 시험마다 '없음' 으로 돌리기 전의 기본 경로


def _rules(sp: str) -> str:
    assert sp.startswith(a._SP_HEAD) and sp.endswith(a._SP_TAIL), "코어 규칙 밖의 글은 그대로다"
    return sp[len(a._SP_HEAD):len(sp) - len(a._SP_TAIL)]


def test_어휘표가_없으면_시스템_프롬프트는_글자_하나_다르지_않다():
    assert a._system_prompt() == a.SYSTEM_PROMPT == a._SP_HEAD + a._CORE_RULES_DEFAULT + a._SP_TAIL
    assert a._core_vocab_health() == {"loaded": False, "error": "없음"}


def test_R1_R2_R7_R8_코어_규칙을_어휘표에서_만든다(monkeypatch):
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", _REAL)
    vocab = json.loads(_REAL.read_text(encoding="utf-8"))
    sp = a._system_prompt()
    rules = _rules(sp)
    assert rules != a._CORE_RULES_DEFAULT
    head = rules[:rules.index("①")]
    assert all(w in head for w in vocab["rule_words"]), head
    assert "첫 걸음은 정본의 object_resolve 하나" in head and "사례 · 실측 · 규격 원문은 그 뒤에" in head, "R-7 · R-1 의 둘째 문장"
    marks = "①②③④⑤⑥⑦"
    for i, must in enumerate(("object_resolve", "과제명", "objects_summary", "graph_neighbors", "다시 resolve", "조회하지 못했다",
                              "ComponentCore")):
        line = rules[rules.index(marks[i]):rules.index(marks[i + 1])] if i < 6 else rules[rules.index("⑦"):]
        assert must in line, (marks[i], must)
    assert vocab["canonical"]["label"] in rules[rules.index("①"):rules.index("②")]
    four = rules[rules.index("④"):rules.index("⑤")]
    assert all(f"{r['phrase']} = {r['relation']}" in four for r in vocab["relations"]), four
    seven = rules[rules.index("⑦"):]
    for x in ("FMEA(사내 규격 · 사례)", "ARP(해석 실측)", "ODB 허브(보드 규칙)", "Report Archive", "MatNexus",
              "표준 키 · 개수 · 관계는 코어에서 답하고"):
        assert x in seven, x
    assert "FailureTrace · CAE 그룹 Datahub" in rules[rules.index("⑤"):rules.index("⑥")]
    assert "(시험)" not in sp and "시험 사본" not in sp, "시험 사본은 규칙에 싣지 않는다(10-27 D-1 · R-6 은 2차)"
    assert a._core_vocab_health() == {"loaded": True, "version": vocab["version"], "rule_words": len(vocab["rule_words"]),
                                      "canonical": "rootdesigndatahub"}


def test_어휘표는_바뀐_시각에_다시_읽고_깨지면_기본_글로_돈다(monkeypatch, tmp_path, capsys):
    p = tmp_path / "core_vocab.json"
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", p)
    body = {"version": 7, "canonical": {"app": "hub", "label": "허브"}, "rule_words": ["낱말하나"],
            "relations": [], "apps": {"x": {"role": "noncore", "label": "X앱", "note": "사례"}}}
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    rules = _rules(a._system_prompt())
    assert rules.startswith("[코어 데이터 규칙] 낱말하나 같은 코어 객체를") and "① 정본은 허브입니다" in rules
    assert "graph_neighbors 로 보세요. ⑤ 다른 허브(FailureTrace 등)로" in rules, "관계 · 형제가 없으면 그 꼬리를 달지 않는다"
    assert "⑦ X앱(사례) 는 코어 객체를" in rules
    body["rule_words"] = ["낱말둘"]
    body["apps"] = {}
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    rules = _rules(a._system_prompt())
    assert "낱말둘" in rules and "낱말하나" not in rules
    assert rules.endswith(a._CORE_RULES_DEFAULT[a._CORE_RULES_DEFAULT.index("⑦"):]), "코어가 아닌 앱이 없으면 ⑦ 은 기본 글이다"
    p.write_text("{깨진", encoding="utf-8")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert a._system_prompt() == a.SYSTEM_PROMPT
    assert a._core_vocab_health() == {"loaded": False, "error": "못 읽음(JSONDecodeError)"}
    p.unlink()
    assert a._system_prompt() == a.SYSTEM_PROMPT and a._core_vocab_health()["error"] == "없음"
    out = capsys.readouterr().out
    assert out.count("코어 어휘표") == 2, "깨짐 · 없어짐을 한 번씩 알린다"

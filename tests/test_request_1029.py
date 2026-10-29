# HWAXPortal docs/request-1029 의 에이전트 서버 몫 — 코어 어휘표로 만드는 챗 코어 규칙(R-1 · R-2 · R-7 · R-8)과 자기검증 출력 상한(W-9)
"""10-29 cae00 웹 챗 QA — 고장모드 · 시험항목 · 가상검증 · 관리 기준 · 물성 질문 다섯이 코어를 건너뛰었다(OT-06 ~ 10). 발동 낱말이
'부품 · 모델 · 과제 · 불량' 뿐이었고(R-1), FMEA · ARP · ODB 허브가 코어 질문을 가로챘다(R-2). 낱말은 포털 리포의 코어 어휘표(정본)에서 읽는다.
자기검증 검토자는 출력 상한이 없어 한 호출이 399초 · 240,960자까지 늘었다(EX-03 — W-9)."""
import asyncio
import json
import os
import sys
from pathlib import Path

import app as a

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_chat_self_check import _Reviewer, _inner, _run, _script, _stats, _text, _turn, fake_llm  # noqa: E402,F401

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


def test_W9_검토자_출력_상한은_초안에_비례하고_아래_위가_있다():
    assert a._sc_out_cap("짧은 답") == 2048
    mid = "최대 응력은 321.5 MPa 입니다. " * 300
    assert a._sc_out_cap(mid) == 2 * a._est_tokens_all(mid) + 1024 and 2048 < a._sc_out_cap(mid) < 16384
    assert a._sc_out_cap("가" * 200_000) == 16384


def test_W9_검토자_호출에_출력_상한을_보내고_생각을_켜도_보낸다(monkeypatch):
    draft = "토크는 12.5 N·m 입니다. " * 5
    rv = _Reviewer("[판정] 고칠 것 없음\n[고친 점]\n- 없음\n[답]\n")
    _run(monkeypatch, _inner(draft), rv)
    kw = rv.calls[0][1]
    assert kw["max_tokens"] == a._sc_out_cap(draft) and kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    monkeypatch.setenv("CHAT_SELF_CHECK_THINK", "1")
    rv2 = _Reviewer("[판정] 고칠 것 없음\n[고친 점]\n- 없음\n[답]\n")
    _run(monkeypatch, _inner(draft), rv2)
    assert rv2.calls[0][1] == {"max_tokens": a._sc_out_cap(draft)}, "생각 몫도 상한 안이다 — 끊기면 초안을 낸다"


def test_실제_경로_챗은_상한_없이_검토자는_상한으로_가고_시스템_글에_어휘표_규칙이_실린다(monkeypatch, fake_llm):
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", _REAL)
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    draft = "M3 나사의 권장 체결 토크는 12.5 N·m 입니다."
    _script(fake_llm, [draft, "[판정] 고칠 것 없음\n[고친 점]\n- 없음\n[답]\n"])
    evs = _turn(monkeypatch, fake_llm, "M3 나사 체결 토크는?")
    st = _stats(fake_llm)
    assert st["calls"] == 2 and _text(evs, "token") == draft
    assert st["max_tokens"] == [None, a._sc_out_cap(draft)], "본답에는 상한을 두지 않는다(결정 D-6) · 검토자는 W-9 상한"
    assert "FMEA(사내 규격 · 사례)" in st["systems"][0] and "첫 걸음은 정본의 object_resolve 하나" in st["systems"][0]


# ── R-10 · W-5 — 챗 도구 결과의 앱 머리 · 되풀이 꼬리 · 허브의 나머지 큰 응답 접기 ──────────────────────────────────────────
import json as _json  # noqa: E402
from collections import Counter as _Counter  # noqa: E402

from langchain_core.tools import StructuredTool as _ST  # noqa: E402


def _tool_of(name, text, *, chat=True):
    async def call(**_kw):
        return text
    return a._prep_tool(_ST(name=name, description="d", coroutine=call, args_schema={"type": "object", "properties": {}}),
                        arg_defaults=a._CHAT_ARG_DEFAULTS if chat else None, chat=chat)


def _cache(monkeypatch, mapping, labels):
    """도구 → 앱 캐시를 앉힌다 — 묻는 함수도 막는다(낡은 캐시면 도구를 묶을 때 떠 있는 게이트웨이에 실제로 물었다)."""
    apps = {k: {"app": k, "label": v} for k, v in labels.items()}
    monkeypatch.setitem(a._TOOLS_MAP_CACHE, "map", mapping)
    monkeypatch.setitem(a._APPS_CACHE, "apps", apps)
    monkeypatch.setattr(a, "_tools_map", lambda: mapping)
    monkeypatch.setattr(a, "_gw_apps", lambda: apps)


def test_R10_챗_도구_결과_머리에_앱_이름이_붙고_코드가_읽는_길에는_붙지_않는다(monkeypatch):
    _cache(monkeypatch, {"list_tasks": "heax-kooremapper_mcp", "invoke_tool": "_gateway"},
           {"heax-kooremapper_mcp": "DynaForge"})
    out = asyncio.run(_tool_of("list_tasks", '{"items": []}').ainvoke({}))
    assert out.startswith("[이 결과는 DynaForge 앱의 list_tasks 이다]\n") and out.endswith('{"items": []}')
    raw = asyncio.run(_tool_of("list_tasks", '{"items": []}', chat=False).ainvoke({}))
    assert raw == '{"items": []}', "카탈로그 · 심의 길은 결과를 코드가 JSON 으로 읽는다 — 머리를 달면 깨진다"
    via = asyncio.run(_tool_of("invoke_tool", "결과").ainvoke({"name": "list_tasks", "arguments": {}}))
    assert via.startswith("[이 결과는 DynaForge 앱의 list_tasks 이다]"), "경유면 안쪽 도구의 앱이다"
    assert a._evidence_block([("list_tasks", "code=A")], []).count("`list_tasks`(DynaForge) · code=A") == 1


def test_W5_같은_도구를_네_번째부터_부르면_앞_결과를_쓰라고_붙인다(monkeypatch):
    _cache(monkeypatch, {}, {})
    tok = a._turn_tool_calls.set(_Counter())
    try:
        tool = _tool_of("failuretrace_objects_summary", '{"total": 3}')
        outs = [asyncio.run(tool.ainvoke({})) for _ in range(5)]
    finally:
        a._turn_tool_calls.reset(tok)
    assert all("번째 불렀다" not in o for o in outs[:3])
    assert "4번째 불렀다" in outs[3] and "5번째 불렀다" in outs[4] and "object_fields" in outs[3]
    other = _tool_of("search_voc", "voc")
    tok = a._turn_tool_calls.set(_Counter({"search_voc": 3}))
    try:
        o = asyncio.run(other.ainvoke({}))
    finally:
        a._turn_tool_calls.reset(tok)
    assert "4번째 불렀다" in o and "object_fields" not in o, "조건 주소 안내는 허브의 개수 · 목록 도구에만"
    assert "번째 불렀다" not in asyncio.run(tool.ainvoke({})), "턴 밖(카운터 없음)에서는 붙이지 않는다"


def test_W5_허브의_나머지_큰_응답은_긴_목록을_앞부분과_전체_수로_접는다(monkeypatch):
    big = _json.dumps({"type": "failure_mode", "fields": [{"path": f"in.f{i}", "label": "x" * 400} for i in range(300)]})
    assert len(big) > a._HUB_BIG_CHARS
    for name in ("object_fields", "rootdesigndatahub_graph_neighbors", "failuretrace_object_references"):
        out = a._fold_hub_result(name, big)
        head, body = out.split("\n", 1)
        assert head.startswith("[포털 챗이 허브 응답을 접었다 — 긴 목록 1곳에서 항목 250건 뺐다"), head
        data = _json.loads(body)
        assert len(data["fields"]) == a._HUB_LIST_HEAD + 1 and "전체 300개 중" in data["fields"][-1]
        assert data["type"] == "failure_mode"
    small = _json.dumps({"fields": [{"path": f"in.f{i}"} for i in range(300)]})
    assert a._fold_hub_result("object_fields", small) == small, "작은 응답은 글자 하나 바꾸지 않는다"
    assert a._fold_hub_result("object_fields", "표 글 " * 20000) == "표 글 " * 20000, "JSON 이 아니면 그대로"
    via = asyncio.run(_tool_of("invoke_tool", big).ainvoke({"name": "rootdesigndatahub_object_fields", "arguments": {}}))
    assert "[포털 챗이 허브 응답을 접었다" in via, "invoke_tool 의 안쪽 이름으로도 접는다"


# ── R-9 — 답에 섞인 다른 문자(한자 · 가나) 낱말을 자기검증에 짚어 넘긴다 ─────────────────────────────────────────────────
def test_R9_질문_근거에_없는_한자_가나_낱말만_짚는다():
    assert a._foreign_words("기준값은 380 MPa 이며 这个 기준은 엄격하다. 確認 필요", "관리 기준은?") == ["这个", "確認"]
    assert a._foreign_words("这个 기준", "这个 기준이 뭐야?") == [], "그 문자로 물었으면 짚지 않는다"
    assert a._foreign_words("VOC 원문은 '质量差' 이다", "VOC 요약", '{"text": "质量差"}') == [], "근거에 그대로 있는 인용은 짚지 않는다"
    assert a._foreign_words("한국어만 쓴 답", "질문") == []


def test_R9_다른_문자가_섞이면_짧은_답도_검증하고_검토자에게_목록을_준다():
    req = a.ChatRequest(message="안녕", groups=[], history=[])
    assert a._self_check_skip(req, "안녕하세요", []) is True
    assert a._self_check_skip(req, "안녕하세요 这个", []) is False, "짧은 인사여도 다른 문자가 섞이면 돈다"
    body = a._self_check_messages(req, "안녕하세요 这个", [], [])[1].content
    assert "[코드 대조 — 답에 섞인 다른 문자(한자 · 가나) 낱말" in body and "`这个`" in body
    assert body.index("[코드 대조 — 답에 섞인") < body.index("[답 초안]")
    assert "[코드 대조 — 답에 섞인" not in a._self_check_messages(req, "안녕하세요", [], [])[1].content


# ── W-1 — 챗 LLM 호출의 대화 머리(같은 대화 → 같은 GPU 복제본) ────────────────────────────────────────────────────────────
def test_W1_대화_머리는_서버_대화_id_가_있으면_그_해시고_없으면_턴마다_다르다():
    import hashlib
    r = a.ChatRequest(message="x", groups=[], conversation_id="conv-1", user_email="u@x")
    assert a._affinity_key(r) == hashlib.sha256(b"conv-1").hexdigest()[:16] == a._affinity_key(r)
    n = a.ChatRequest(message="x", groups=[], user_email="u@x")
    assert a._affinity_key(n) != a._affinity_key(n), "대화 id 가 없으면 '사람 + 턴의 시작' — 턴마다 새 값"


def test_W1_실제_경로_한_턴의_챗_호출과_검토자_호출이_같은_대화_머리를_싣는다(monkeypatch, fake_llm):
    import hashlib
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    draft = "M3 나사의 권장 체결 토크는 12.5 N·m 입니다."
    _script(fake_llm, [draft, "[판정] 고칠 것 없음\n[고친 점]\n- 없음\n[답]\n"])
    orig = a._agent_stream

    def with_conv(app_, req):                          # 포털이 서버 대화 id 를 실은 요청
        return orig(app_, req.model_copy(update={"conversation_id": "conv-7"}))
    monkeypatch.setattr(a, "_agent_stream", with_conv)
    _turn(monkeypatch, fake_llm, "M3 나사 체결 토크는?")
    st = _stats(fake_llm)
    assert st["calls"] == 2 and st["affinity"] == [hashlib.sha256(b"conv-7").hexdigest()[:16]] * 2, st["affinity"]

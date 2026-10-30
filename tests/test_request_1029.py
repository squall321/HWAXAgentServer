# HWAXPortal docs/request-1029 의 에이전트 서버 몫 — 코어 어휘표로 만드는 챗 코어 규칙(R-1 · R-2 · R-7 · R-8)과 자기검증 출력 상한(W-9)
"""10-29 cae00 웹 챗 QA — 고장모드 · 시험항목 · 가상검증 · 관리 기준 · 물성 질문 다섯이 코어를 건너뛰었다(OT-06 ~ 10). 발동 낱말이
'부품 · 모델 · 과제 · 불량' 뿐이었고(R-1), FMEA · ARP · ODB 허브가 코어 질문을 가로챘다(R-2). 낱말은 포털 리포의 코어 어휘표(정본)에서 읽는다.
자기검증 검토자는 출력 상한이 없어 한 호출이 399초 · 240,960자까지 늘었다(EX-03 — W-9)."""
import asyncio
import json
import os

import pytest
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
    h = a._core_vocab_health()
    assert {k: h[k] for k in ("loaded", "version", "rule_words", "canonical")} == {
        "loaded": True, "version": vocab["version"], "rule_words": len(vocab["rule_words"]), "canonical": "rootdesigndatahub"}
    assert h["fingerprint"] == h["base"] and len(h["fingerprint"]) == 12 and h["overlay"] == {"present": False}, "덧씌우기 없음 — E-7 ③"


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


def test_W9_W4_검토자_호출에_출력_상한_4096_을_보내고_생각을_켜도_보낸다(monkeypatch):
    """W-4 로 검토자가 바꿀 곳만 쓰게 된 뒤 초안 비례 상한(2,048 ~ 16,384)을 4,096 으로 줄였다(요청서 W-9 의 권장값)."""
    for draft in ("토크는 12.5 N·m 입니다. " * 5, "최대 응력은 321.5 MPa 입니다. " * 300):
        rv = _Reviewer("[판정] 고칠 것 없음\n[고친 점]\n- 없음\n")
        _run(monkeypatch, _inner(draft), rv)
        kw = rv.calls[0][1]
        assert kw["max_tokens"] == 4096 and kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}, "초안 길이와 상관없다"
    monkeypatch.setenv("CHAT_SELF_CHECK_THINK", "1")
    rv2 = _Reviewer("[판정] 고칠 것 없음\n[고친 점]\n- 없음\n")
    _run(monkeypatch, _inner("토크는 12.5 N·m 입니다."), rv2)
    assert rv2.calls[0][1] == {"max_tokens": 4096}, "생각 몫도 상한 안이다 — 끊기면 초안을 낸다"


def test_실제_경로_챗은_상한_없이_검토자는_상한으로_가고_시스템_글에_어휘표_규칙이_실린다(monkeypatch, fake_llm):
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", _REAL)
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    draft = "M3 나사의 권장 체결 토크는 12.5 N·m 입니다."
    _script(fake_llm, [draft, "[판정] 고칠 것 없음\n[고친 점]\n- 없음\n"])
    evs = _turn(monkeypatch, fake_llm, "M3 나사 체결 토크는?")
    st = _stats(fake_llm)
    assert st["calls"] == 2 and _text(evs, "token") == draft
    assert st["max_tokens"] == [None, 4096], "본답에는 상한을 두지 않는다(결정 D-6) · 검토자는 W-9 · W-4 상한"
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
    _script(fake_llm, [draft, "[판정] 고칠 것 없음\n[고친 점]\n- 없음\n"])
    orig = a._agent_stream

    def with_conv(app_, req):                          # 포털이 서버 대화 id 를 실은 요청
        return orig(app_, req.model_copy(update={"conversation_id": "conv-7"}))
    monkeypatch.setattr(a, "_agent_stream", with_conv)
    _turn(monkeypatch, fake_llm, "M3 나사 체결 토크는?")
    st = _stats(fake_llm)
    assert st["calls"] == 2 and st["affinity"] == [hashlib.sha256(b"conv-7").hexdigest()[:16]] * 2, st["affinity"]


# ── W-3 — 프롬프트 앞부분 고정(첫 시스템 메시지 · 도구의 고정 몫이 턴을 가로질러 같다) ─────────────────────────────────────────
def test_W3_바뀌는_안내는_사람_발화_앞머리에_싣고_시스템은_맨_앞_하나다_끄면_종전_꼴이다(monkeypatch):
    """둘째 시스템 메시지는 Qwen 채팅 틀이 400 으로 거절한다(HA-26 — 'System message must be at the beginning')."""
    hist, user = [("user", "앞 질문"), ("assistant", "앞 답")], ("user", "이번 질문")
    m = a._turn_msgs("고정 시스템", "\n\n[사용자 지정 우선 도구]\nx", hist, None, user)
    assert m[0] == ("system", "고정 시스템") and m[1:3] == hist and len(m) == 4
    assert [r for r, _ in m].count("system") == 1, "시스템 메시지는 맨 앞 하나"
    role, text = m[-1]
    assert role == "user" and text.startswith("[이번 턴의 안내") and "[사용자 지정 우선 도구]\nx" in text and text.endswith("이번 질문")
    assert a._turn_msgs("고정 시스템", "", hist, None, user) == [("system", "고정 시스템"), *hist, user], "안내가 없으면 한 칸"
    monkeypatch.setattr(a, "_STABLE_PREFIX", False)
    assert a._turn_msgs("고정 시스템", "\n\nX", hist, ("user", "그림"), user) == \
        [("system", "고정 시스템\n\nX"), *hist, ("user", "그림"), user], "CHAT_STABLE_PREFIX=0 — 종전처럼 한 시스템 메시지"


def test_W3_도구의_고정_몫은_정해진_차례로_맨_앞이다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 0)
    monkeypatch.setattr(a, "_model_context_tokens", lambda: 1_000_000)
    names = ["zz_other", "list_tasks", "search_tools", "rootdesigndatahub_objects_list", "aa_other", "invoke_tool",
             "rootdesigndatahub_object_resolve"]
    tools = [_ST(name=n, description="d", coroutine=lambda **_k: None, args_schema={"type": "object", "properties": {}}) for n in names]
    keep = a._GUIDE_FRONT + a._ONTO_CORE
    want = [n for n in keep if n in names]
    got = [t.name for t in a._select_tools(tools, "질문")]
    assert got[:len(want)] == want, got
    assert [t.name for t in a._select_tools(list(reversed(tools)), "다른 질문")][:len(want)] == want, "들어온 차례와 무관하다"


def test_W3_실제_경로_턴을_가로질러_첫_시스템_메시지가_바이트까지_같다(monkeypatch, fake_llm):
    monkeypatch.setenv("CHAT_SELF_CHECK", "0")
    _script(fake_llm, ["첫 답", "둘째 답"])
    orig = a._agent_stream
    docs = {"on": False}

    def with_doc(app_, req):                           # 둘째 턴에는 문서를 붙인다 — 이 턴의 안내가 달라진다
        return orig(app_, req.model_copy(update={"documents": [{"name": "a.txt", "text": "붙인 문서의 본문 글"}]})
                    if docs["on"] else req)
    monkeypatch.setattr(a, "_agent_stream", with_doc)
    _turn(monkeypatch, fake_llm, "첫 질문")
    docs["on"] = True
    _turn(monkeypatch, fake_llm, "둘째 질문")
    st = _stats(fake_llm)
    assert st["calls"] == 2 and st["first_system"][0] == st["first_system"][1], "바뀌는 안내가 첫 시스템 메시지에 섞였다"
    # 안내는 이번 사람 발화의 앞머리에 — 시스템 메시지는 맨 앞 하나뿐이다(HA-26, 가짜 서버가 둘째 시스템을 400 으로 거절한다)
    assert "붙인 문서의 본문 글" in st["humans"][1] and "붙인 문서의 본문 글" not in st["systems"][1]


# ── W-2 — 캡 없는 길의 도구 설명 예산 손잡이(기본값은 그대로) ───────────────────────────────────────────────────────────────
def test_W2_예산_손잡이를_주면_그_값이고_비면_창의_45퍼센트다(monkeypatch):
    monkeypatch.setattr(a, "TOOL_MAX", 0)
    monkeypatch.setattr(a, "_model_context_tokens", lambda: 200_000)
    seen = {}

    def fit(tools, budget, keep=()):
        seen["budget"] = budget
        return tools
    monkeypatch.setattr(a, "_fit_schema", fit)
    tools = [_ST(name=f"t{i}", description="d", coroutine=lambda **_k: None, args_schema={"type": "object", "properties": {}})
             for i in range(3)]
    a._select_tools(tools, "질문")
    assert seen["budget"] == 90_000, "비면 종전대로 창의 45%(결정 D-3)"
    monkeypatch.setattr(a, "CHAT_TOOL_SCHEMA_BUDGET", 30_000)
    a._select_tools(tools, "질문")
    assert seen["budget"] == 30_000


# ── W-4 — 자기검증 고침은 바꿀 곳만 받아 초안에 넣는다(못 넣으면 초안) ─────────────────────────────────────────────────────────
def test_W4_바꿀_곳은_초안의_좌표로_한꺼번에_넣고_띄어쓰기만_다르면_찾는다():
    draft = "| 부품 | 응력 |\n|---|---|\n| A | 321.5 MPa |\n\nA 의 안전율은 1.8 입니다. 결론은 A 가 위험하다는 것입니다."
    got, why = a._sc_apply(draft, [("결론은 A 가 위험하다는 것입니다.", "결론은 A 가 안전하다는 것입니다."),
                                   ("|  A  |  321.5 MPa |", "| A | 312.5 MPa |"),
                                   ("안전율은 1.8 입니다.", "안전율은 1.8 입니다(조회 결과 1). 여유는 0.8 입니다.")])
    assert why == "" and got == ("| 부품 | 응력 |\n|---|---|\n| A | 312.5 MPa |\n\nA 의 안전율은 1.8 입니다(조회 결과 1). 여유는 0.8 입니다. "
                                 "결론은 A 가 안전하다는 것입니다."), "적은 차례와 상관없이 · 띄어쓰기만 다르게 옮긴 원문도 · 더하기는 원문 뒤에 이어"
    assert a._sc_apply(draft, [("안전율은 1.8", "안전율은 2.0"), ("1.8 입니다", "x")]) == ("", "바꿀 곳끼리 겹친다")
    assert a._sc_apply(draft, [("A", "B")])[1] == "바꿀 곳이 초안에 3번 나온다", "여러 곳이면 넣지 않는다 — 어느 것인지 모른다"


def _review_prompt(fake_llm) -> str:
    return _stats(fake_llm)["humans"][1]


def test_W4_실제_경로_검토자는_바꿀_곳만_쓰고_본문은_초안에_넣은_고친_답이다(monkeypatch, fake_llm):
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    draft = "M3 나사의 권장 체결 토크는 12.5 N·m 입니다. 와셔는 쓰지 않습니다."
    _script(fake_llm, [draft, "[판정] 고침\n[고친 점]\n- 12.5 는 근거가 없어 추정으로 표시\n[바꿀 곳]\n[원문]\n12.5 N·m 입니다.\n"
                              "[새 글]\n12.5 N·m (근거 없음 — 추정) 입니다."])
    evs = _turn(monkeypatch, fake_llm, "M3 나사 체결 토크는?")
    assert _text(evs, "token") == "M3 나사의 권장 체결 토크는 12.5 N·m (근거 없음 — 추정) 입니다. 와셔는 쓰지 않습니다."
    sc = [d for e, d in evs if e == "meta"][-1]["self_check"]
    assert sc["status"] == "fixed" and sc["fixes"] == ["12.5 는 근거가 없어 추정으로 표시"]
    ask = _review_prompt(fake_llm)
    assert "고친 답 전체를 다시 쓰지 마라" in ask and "[원문]" in ask and "[새 글]" in ask and "[답]\n" not in ask


def test_W4_실제_경로_바꿀_곳을_초안에서_못_찾으면_초안을_내고_까닭을_적는다(monkeypatch, fake_llm):
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    draft = "M3 나사의 권장 체결 토크는 12.5 N·m 입니다."
    _script(fake_llm, [draft, "[판정] 고침\n[고친 점]\n- x\n[바꿀 곳]\n[원문]\nM4 나사의 토크는 12.5\n[새 글]\nM4 나사의 토크는 15.2"])
    evs = _turn(monkeypatch, fake_llm, "M3 나사 체결 토크는?")
    assert _text(evs, "token") == draft
    sc = [d for e, d in evs if e == "meta"][-1]["self_check"]
    assert sc["status"] == "suspect" and "찾지 못했다" in sc["reason"] and sc["fixes"] == ["x"], sc


# ── E-7 ② ③ — 코어 어휘표 박스 덧씌우기 · 지문(게이트웨이와 같은 셈) ──────────────────────────────────────────────────────
def test_E7_어휘표_덧씌우기는_merge_patch_로_얹고_깨지면_버린다(monkeypatch, tmp_path):
    import hashlib
    monkeypatch.setattr(a, "CORE_VOCAB_PATH", _REAL)
    loc = tmp_path / "core_vocab.local.json"
    monkeypatch.setattr(a, "CORE_VOCAB_LOCAL_PATH", loc)
    base = a._core_vocab()
    assert base["fingerprint"] == base["base_fingerprint"] and a._core_vocab_health()["overlay"] == {"present": False}
    loc.write_text(json.dumps({"rule_words": ["덧씌운낱말"], "apps": {"fmea": None}}, ensure_ascii=False), encoding="utf-8")
    d = a._core_vocab()
    assert "덧씌운낱말" in d["rules"] and "FMEA" not in d["rules"].split("⑦", 1)[1], "목록은 바꾸고 null 은 지운다"
    raw = json.loads(_REAL.read_text(encoding="utf-8"))
    merged = {**raw, "rule_words": ["덧씌운낱말"], "apps": {k: v for k, v in raw["apps"].items() if k != "fmea"}}
    want = hashlib.sha256(json.dumps({k: v for k, v in merged.items() if k != "_doc"}, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()[:12]
    h = a._core_vocab_health()
    assert h["fingerprint"] == want and h["base"] == base["base_fingerprint"], "지문 — 덧씌운 표의 키 차례 JSON(_doc 뺌) sha256 앞 12자"
    assert set(h["overlay"]["changed"]) == {"rule_words", "apps.fmea"}
    loc.write_text("[]", encoding="utf-8")
    st = loc.stat()
    os.utime(loc, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert a._core_vocab()["fingerprint"] == base["fingerprint"] and "덧씌우기를 버렸다" in a._core_vocab_health()["overlay"]["error"]


def test_E7_합치기와_지문은_게이트웨이와_글자까지_같다():
    """두 서비스가 같은 파일에서 같은 지문을 내야 update-all §6 이 '한 표를 본다' 를 견줄 수 있다 — 한쪽만 고치면 여기서 깨진다."""
    import inspect
    gw = Path(a.__file__).resolve().parent.parent / "HWAXMcpGateway" / "gateway.py"
    if not gw.exists():
        pytest.skip("형제 게이트웨이 리포가 없다")
    src = gw.read_text(encoding="utf-8")
    for fn in (a._merge_patch, a._json_fp):
        body = inspect.getsource(fn)
        code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith(('"""', "#")) and "게이트웨이" not in ln)
        name = fn.__name__
        i = src.index(f"def {name}(")
        theirs = src[i:src.index("\n\n\n", i)]
        mine_lines = [ln for ln in code.splitlines() if ln.strip() and not ln.strip().startswith(("JSON", "값의"))]
        their_lines = [ln for ln in theirs.splitlines() if ln.strip() and not ln.strip().startswith(('"""', "JSON", "값의", "#"))]
        assert [ln for ln in mine_lines if '"""' not in ln] == [ln for ln in their_lines if '"""' not in ln], name


def test_HA26_그림이_든_발화도_안내를_첫_글_조각으로_싣고_시스템은_하나다():
    """그림을 올린 턴의 발화는 글 조각 목록이다 — 안내를 첫 글 조각으로 앞에 붙이고 그림 조각은 그대로 둔다."""
    from langchain_core.messages import HumanMessage
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    user = HumanMessage(content=[{"type": "text", "text": "이 그림을 봐"}, img])
    recap = HumanMessage(content=[{"type": "text", "text": "앞 대화의 그림"}, img])
    m = a._turn_msgs("고정 시스템", "\n\n[붙인 문서]\n본문", [("user", "앞")], recap, user)
    assert [getattr(x, "type", None) or x[0] for x in m].count("system") == 1 and m[0] == ("system", "고정 시스템")
    assert m[-2] is recap
    last = m[-1]
    assert isinstance(last, HumanMessage) and last.content[0]["type"] == "text" and last.content[0]["text"].startswith("[이번 턴의 안내")
    assert "[붙인 문서]\n본문" in last.content[0]["text"] and last.content[1:] == user.content, "그림 조각은 그대로"


def test_HA26_실제_Qwen_채팅_틀은_둘째_시스템_메시지를_거절하고_고친_꼴은_받는다():
    """가짜는 진짜 꼴로 — Qwen3 의 chat_template 이 하는 검사(시스템 메시지가 맨 앞이 아니면 예외)를 그대로 흉내 내 바뀌는 안내 셋(전문가 · 앱 핀 · 붙인 문서)을 넣어 본다."""
    def qwen_template_check(msgs):
        roles = [getattr(x, "type", None) or x[0] for x in msgs]
        roles = ["system" if r == "system" else r for r in roles]
        for i, r in enumerate(roles):
            if r == "system" and i != 0:
                raise ValueError("System message must be at the beginning.")
    hist = [("user", "앞 질문"), ("assistant", "앞 답")]
    for notes in ("\n\n[페르소나]\n품질 전문가", "\n\n[사용자 지정 우선 앱]\nsignalforge", "\n\n[붙인 문서]\n본문"):
        qwen_template_check(a._turn_msgs("고정 시스템", notes, hist, None, ("user", "질문")))


# ── HA-26 재시도 몫 — 그래도 'System message' 400 이면 자동 재시도는 안내를 첫 시스템 메시지에 합친다(HWAXPortal docs/request_261030.md) ──────
_HA26_DOC = "붙인 문서의 본문 글"
_HA26_LOG = f"[agent] '{a._SYS_NOT_FIRST}' 400 — 남은 재시도는 이 턴의 안내를 첫 시스템 메시지에 합쳐 보낸다(HA-26)"   # 합칠 때의 운영 로그 — README 도 이 줄 그대로


def _ha26_doc_turns(monkeypatch) -> None:
    """이 시험의 챗 턴마다 문서를 붙이고(이 턴의 안내가 생긴다) 자동 재시도 둘을 연다 — _turn 은 재시도를 0 으로 닫는다."""
    monkeypatch.setenv("CHAT_SELF_CHECK", "0")
    orig = a._agent_stream

    def with_doc(app_, req):
        monkeypatch.setenv("CHAT_AUTO_RETRY", "2")
        return orig(app_, req.model_copy(update={"documents": [{"name": "a.txt", "text": _HA26_DOC}]}))
    monkeypatch.setattr(a, "_agent_stream", with_doc)


def _script_acts(base: str, acts: list) -> None:
    """대본을 거동째로 — 오류 거동(system_not_first · http_400)을 끼운다(_script 는 글만 받는다)."""
    import httpx
    with httpx.Client(timeout=5, trust_env=False) as c:
        c.post(f"{base}/_script", json={"script": acts}).raise_for_status()


def _notes_at(st: dict, i: int) -> str:
    """i 번째 요청에서 이 턴의 안내(붙인 문서)가 실린 자리 — head(사람 발화 앞머리) · system(첫 시스템 메시지). 빠지거나 두 곳이면 실패."""
    head = _HA26_DOC in st["humans"][i] and st["humans"][i].startswith(a._TURN_NOTES_HEAD)
    system = _HA26_DOC in (st["first_system"][i] or "")
    assert head != system, f"{i}번째 요청 — 발화 머리 {head} · 첫 시스템 {system}(안내는 꼭 한 곳에 있어야 한다)"
    return "head" if head else "system"


def test_HA26_merged_는_손잡이와_상관없이_안내를_첫_시스템_메시지에_잇고_발화는_그대로다(monkeypatch):
    """CHAT_STABLE_PREFIX=0 과 같은 꼴이다 — 그림이 든 발화도 손대지 않는다. 안내가 없으면 merged 여도 종전 한 칸이다."""
    from langchain_core.messages import HumanMessage
    monkeypatch.setattr(a, "_STABLE_PREFIX", True)
    hist, user, notes = [("user", "앞 질문"), ("assistant", "앞 답")], ("user", "이번 질문"), "\n\n[붙인 문서]\n본문"
    assert a._turn_msgs("고정 시스템", notes, hist, None, user, merged=True) == [("system", "고정 시스템" + notes), *hist, user]
    img = HumanMessage(content=[{"type": "text", "text": "이 그림을 봐"},
                                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])
    m = a._turn_msgs("고정 시스템", notes, hist, ("user", "그림"), img, merged=True)
    assert m[:-1] == [("system", "고정 시스템" + notes), *hist, ("user", "그림")] and m[-1] is img, "그림이 든 발화는 그대로"
    assert a._turn_msgs("고정 시스템", "", hist, None, user, merged=True) == [("system", "고정 시스템"), *hist, user]


def test_HA26_실제_경로_그래도_시스템_자리_400_이면_재시도는_안내를_첫_시스템_메시지에_합쳐_답한다(monkeypatch, fake_llm, capsys):
    """첫 시도는 그대로(안내는 발화 머리)다. 가짜 서버가 그 차례를 꼴과 상관없이 'System message must be at the beginning.' 400 으로
    거절하면(system_not_first) 자동 재시도는 안내를 첫 시스템 메시지에 합쳐(CHAT_STABLE_PREFIX=0 꼴) 보내고 답을 낸다."""
    _ha26_doc_turns(monkeypatch)
    _script_acts(fake_llm, ["system_not_first", {"behavior": "oracle", "text": "합친 꼴로 받은 답"}])
    evs = _turn(monkeypatch, fake_llm, "붙인 문서를 요약해 줘")
    st = _stats(fake_llm)
    assert st["behaviors"] == ["system_not_first", "oracle"], st["behaviors"]
    assert [_notes_at(st, i) for i in (0, 1)] == ["head", "system"], "첫 시도는 그대로 · 재시도는 첫 시스템 메시지에"
    assert st["first_system"][1].startswith(st["first_system"][0]) and len(st["first_system"][1]) > len(st["first_system"][0]), \
        "합친 첫 시스템 메시지는 고정 시스템 글 뒤에 이 턴의 안내"
    assert st["humans"][1] == "붙인 문서를 요약해 줘", "재시도의 발화는 사람의 말 그대로"
    assert _text(evs, "token") == "합친 꼴로 받은 답" and not [d for e, d in evs if e == "error"]
    assert _HA26_LOG in capsys.readouterr().out, "운영 로그에 한 줄"


# 갈래마다 시험 하나(가짜 서버 하나)다 — 한 시험에서 _turn 을 두 번 부르면 둘째 턴은 첫 턴의 에이전트를 쓰고(_agent_for 가 바꿔 끼운 채다),
# 첫 턴이 풀에 남긴 연결은 닫힌 이벤트 루프의 것이라 둘째 턴의 첫 요청이 대본 한 칸을 먹고 연결 오류로 넘김(VLLM_*)된다.
@pytest.mark.parametrize("acts, want", [
    (["http_400", "system_not_first"], ["head", "head", "system"]),    # 다른 오류의 재시도는 앞부분 고정 꼴 그대로(캐시 몫)
    (["system_not_first", "http_400"], ["head", "system", "system"]),  # 한 번 합치면 다른 오류가 끼어도 이 턴 끝까지 합친 꼴
])
def test_HA26_실제_경로_합치기는_그_400_을_받은_뒤의_재시도부터_이_턴_끝까지다(monkeypatch, fake_llm, acts, want):
    """첫 시도는 늘 발화 머리다. 'System message' 400 을 받기 전의 재시도는 그대로 두고, 받은 뒤의 재시도는 다른 오류가 끼어도 합친다."""
    _ha26_doc_turns(monkeypatch)
    _script_acts(fake_llm, [*acts, {"behavior": "oracle", "text": "셋째에 받은 답"}])
    evs = _turn(monkeypatch, fake_llm, "붙인 문서를 요약해 줘")
    st = _stats(fake_llm)
    assert st["behaviors"] == [*acts, "oracle"], st["behaviors"]
    assert [_notes_at(st, i) for i in range(3)] == want
    assert _text(evs, "token") == "셋째에 받은 답" and not [d for e, d in evs if e == "error"]


def test_HA26_README_의_W3_절은_안내의_자리와_합치는_재시도를_코드_그대로_적는다():
    """사람은 코드가 아니라 문서를 따른다 — 박스 우회(CHAT_STABLE_PREFIX=0, OB-91)를 걷을지 보는 사람이 이 절을 읽는다. 둘째 시스템 메시지라고
    적혀 있으면 HA-26 이 걷어 낸 꼴을 지금 설계로 읽고, 로그 줄이 코드와 다르면 cae00 로그에서 찾지 못한다."""
    text = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    i = text.index("**Stable prompt prefix (W-3).**")
    para = " ".join(text[i:text.index("\n\n", i)].split())
    assert "second system message right before" not in para, "HA-26 이 걷어 낸 꼴(둘째 시스템 메시지)"
    for must in (a._TURN_NOTES_HEAD.strip(), a._TURN_NOTES_TAIL.strip(), _HA26_LOG):
        assert must in para, must


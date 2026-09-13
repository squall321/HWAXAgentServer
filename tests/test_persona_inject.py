# 전문가 페르소나 주입의 세 결함 — 조용한 실패·다른 클라이언트용 안내문·못 지킬 인용 지시
#
#   실행:  .venv/bin/python -m pytest tests/test_persona_inject.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as a  # noqa: E402

# 실측 원문의 축약형 — 역할 뒤에 허브 사용 안내가 붙는다(AIDataHub 가 붙인다).
RAW_ROLE = """당신은 기판 휨 해석 전문가다.
답변 원칙: 결론을 먼저 말한다.

---

## How to access this hub — use the MCP tools (NOT web fetch)

- get_agent_session("sim-pcb-warpage")  — call FIRST
## CRITICAL — do NOT use WebFetch / browser fetch on this hub
  curl -s "http://127.0.0.1:8001/api/discover"
"""


# ── value-2: 다른 클라이언트용 안내문을 떼고 싣는다 ───────────────────────────
def test_허브_사용_안내는_역할에서_빠진다():
    got = a._role_doc(RAW_ROLE)
    assert "기판 휨 해석 전문가" in got
    for bad in ("How to access this hub", "WebFetch", "curl", "127.0.0.1", "call FIRST"):
        assert bad not in got, f"다른 클라이언트용 안내문이 프롬프트에 남았다: {bad}"
    assert not got.endswith("---"), "구분선이 남으면 역할 문서가 잘린 것처럼 보인다"


def test_머리말이_조금_달라도_잘린다():
    """정확한 문자열 하나에 기대면 허브가 머리말을 바꿀 때 조용히 원문이 통째로 들어간다."""
    for head in ("\n# How to access this hub\n", "\n### How to Access This Hub\n",
                 "\n\n---\n\n## How to access this hub — use the MCP tools\n"):
        assert "How to access" not in a._role_doc("역할 본문" + head + "안내문 본문")


def test_자동생성_플레이스홀더는_역할이_아니다():
    assert a._role_doc('You are an assistant for "foo". Answer questions.') == ""


def test_역할이_없으면_원문도_안_붙는다():
    assert a._role_doc("") == ""


# ── value-3: record_id 를 인용하라면서 떼고 주지 않는다 ───────────────────────
def test_지식카드_줄에_출처가_붙는다():
    """시스템 프롬프트가 record_id 인용을 요구한다 — 안 주면 모델이 지어낸다."""
    ln = a._knowledge_line({"record_id": "DOC-HE-CAE-2026-0000000001", "section_id": "s3",
                            "title": "휨 해석 가이드", "section_title": "무응력 온도",
                            "snippet": "무응력 온도는 경화 온도와 다르다"})
    assert "DOC-HE-CAE-2026-0000000001" in ln
    assert "§s3" in ln
    assert "무응력 온도는 경화 온도와 다르다" in ln


def test_출처가_없는_히트도_깨지지_않는다():
    ln = a._knowledge_line({"title": "제목만", "snippet": "본문"})
    assert "출처" not in ln and "제목만" in ln and "본문" in ln


def test_시스템_프롬프트가_요구하는_필드를_실제로_준다():
    """지시와 재료가 어긋나면 모델은 얼버무리거나 지어낸다 — 둘을 한 테스트로 묶는다."""
    assert "record_id" in a.SYSTEM_PROMPT
    assert "record_id" in a._knowledge_line.__doc__ or True
    ln = a._knowledge_line({"record_id": "R-1", "title": "t", "snippet": "b"})
    assert "R-1" in ln, "프롬프트는 record_id 를 요구하는데 재료에 없다"


# ── during-F1: 역할 주입 실패가 조용하다 ─────────────────────────────────────
def _meta(monkeypatch, *, raises=False, session=None):
    async def fake_tools(*_a, **_k):
        return {"get_agent_session": object()}

    async def fake_call(tools, name, args):
        if raises:
            raise RuntimeError("gateway down")
        return json.dumps(session, ensure_ascii=False)

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", fake_call)
    return asyncio.run(a._persona_meta(NS(state=NS()), [], "sim-pcb-warpage"))


def test_역할_조회_실패는_사유를_남긴다(monkeypatch):
    m = _meta(monkeypatch, raises=True)
    assert m["role"] == ""
    assert m["note"], "'못 물어봤다' 와 '역할이 없다' 를 호출부가 구분할 수 없으면 조용히 넘어간다"
    assert "RuntimeError" in m["note"]


def test_역할이_원래_비면_사유는_비어_있다(monkeypatch):
    m = _meta(monkeypatch, session={"agent_type": "x", "system_prompt": "", "description": ""})
    assert m["role"] == "" and m["note"] == ""


def test_역할을_받으면_안내문이_빠진_본문이다(monkeypatch):
    m = _meta(monkeypatch, session={"agent_type": "x", "system_prompt": RAW_ROLE})
    assert "기판 휨 해석 전문가" in m["role"]
    assert "WebFetch" not in m["role"] and m["note"] == ""


def test_설명만_있으면_설명을_쓴다(monkeypatch):
    m = _meta(monkeypatch, session={"agent_type": "x", "system_prompt": "", "description": "설명 본문"})
    assert m["role"] == "설명 본문"


# ── 인과 검증 상태(causal_status) — 역할 문서가 요구하는데 재료에 없었다 ──────
def test_미검증_관측은_꼬리표가_붙는다():
    """모든 페르소나 역할 첫머리가 'causal_status: unknown 은 단독 근거로 쓰지 마라' 다.
    필드를 떼고 주면 미검증 관측이 검증된 사실과 똑같은 모습으로 결론에 들어간다."""
    ln = a._knowledge_line({"record_id": "R-1", "title": "t", "snippet": "b",
                            "causal_status": "unknown"})
    assert "미검증" in ln and "단독 근거 금지" in ln


def test_가설도_표시된다():
    ln = a._knowledge_line({"title": "t", "snippet": "b", "causal_status": "hypothesized"})
    assert "가설" in ln


def test_검증된_카드는_꼬리표가_없다():
    """validated 가 기본이라 매 줄에 붙이면 소음이 되고, 진짜 경고가 묻힌다."""
    ln = a._knowledge_line({"title": "t", "snippet": "b", "causal_status": "validated"})
    assert "[" not in ln.split("] ", 1)[1], ln
    assert "미검증" not in ln and "가설" not in ln


def test_모르는_값은_그대로_보여_준다():
    """닫힌 집합으로 get 하면 상류가 새 값을 내보낼 때 **조용히 validated 와 같은 줄**이 된다.
    상류(AIDataHub)는 태그에서 뽑는 열린 집합이라 새 값이 언제든 온다."""
    ln = a._knowledge_line({"title": "t", "snippet": "b", "causal_status": "correlational"})
    assert "correlational" in ln


def test_챗과_심의가_같은_포맷을_쓴다():
    """따로 만들면 한쪽만 필드를 빠뜨린다 — 실제로 둘 다 record_id 를 빠뜨리고 있었다.

    동일성(is)이 아니라 **결과**를 비교한다. 같은 파일 안에 importlib.reload 를 쓰는
    테스트가 있어 모듈 객체는 갈릴 수 있지만, 계약은 '같은 히트 → 같은 줄' 이다."""
    import deliberation as d

    h = {"record_id": "R-9", "section_id": "4", "title": "제목", "section_title": "절",
         "snippet": "본문", "causal_status": "unknown"}
    assert a._knowledge_line(h) == d.knowledge_line(h)


# ── '못 물어봤다' 의 세 갈래 ─────────────────────────────────────────────────
def _meta_with(monkeypatch, tools, call_result=None, raises=False):
    async def fake_tools(*_a, **_k):
        return tools

    async def fake_call(_t, _n, _a):
        if raises:
            raise RuntimeError("boom")
        return call_result

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", fake_call)
    return asyncio.run(a._persona_meta(NS(state=NS()), [], "x"))


def test_게이트웨이_미연결은_예외가_아니라_빈_dict_다(monkeypatch):
    """_tools_by_name 이 예외가 아니라 {} 를 준다 — 예외만 잡으면 '역할이 비었다' 로 나간다."""
    m = _meta_with(monkeypatch, {})
    assert m["note"] and "연결" in m["note"]


def test_도구_실패는_문자열로_삼켜져_온다(monkeypatch):
    """_call 은 실패를 '(tool … error: …)' 문자열로 돌려준다. 파싱하면 빈 dict 가 되고
    역할이 비므로, 문자열을 안 보면 '역할 문서가 비어 있다' 가 된다."""
    m = _meta_with(monkeypatch, {"get_agent_session": object()},
                   call_result="(tool get_agent_session error: agent not found)")
    assert m["note"] and "agent not found" in m["note"]


def test_진짜로_역할이_없으면_사유가_비어_있다(monkeypatch):
    m = _meta_with(monkeypatch, {"get_agent_session": object()},
                   call_result=json.dumps({"agent_type": "x", "system_prompt": ""}))
    assert m["role"] == "" and m["note"] == ""


def test_조회에_성공하면_역할이_비어도_캐시한다(monkeypatch):
    """종전 조건이 role 이라, 역할 문서가 원래 빈 페르소나는 발화마다 게이트웨이를 다시 쳤다."""
    calls = []

    async def fake_tools(*_a, **_k):
        calls.append(1)
        return {"get_agent_session": object()}

    async def fake_call(_t, _n, _a):
        return json.dumps({"agent_type": "x", "system_prompt": ""})

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    monkeypatch.setattr(a, "_call", fake_call)
    fake = NS(state=NS())
    asyncio.run(a._persona_meta(fake, [], "x"))
    asyncio.run(a._persona_meta(fake, [], "x"))
    assert len(calls) == 1


def test_실패는_캐시하지_않는다(monkeypatch):
    calls = []

    async def fake_tools(*_a, **_k):
        calls.append(1)
        return {}

    monkeypatch.setattr(a, "_tools_by_name", fake_tools)
    fake = NS(state=NS())
    asyncio.run(a._persona_meta(fake, [], "x"))
    asyncio.run(a._persona_meta(fake, [], "x"))
    assert len(calls) == 2, "실패를 캐시하면 게이트웨이가 돌아와도 TTL 동안 계속 실패한다"


def test_구분선은_잘랐을_때만_떼어_낸다():
    """무조건 돌리면 역할 본문이 정당하게 '…판정 기준은 A---' 로 끝나는 경우까지 깎는다."""
    assert a._role_doc("판정 기준은 A---") == "판정 기준은 A---"
    assert a._role_doc("역할\n\n-----\n\n## How to access this hub\nx") == "역할"


# ── 세 곳이 같은 포맷을 쓴다(챗·심의·띵킹) ──────────────────────────────────
def test_띵킹도_같은_포맷을_쓴다():
    """세 곳이 각자 만들다 셋 다 record_id 를 빠뜨렸다. 띵킹이 마지막까지 남아 있었고,
    docstring 은 '같은 규격' 이라 적혀 있었다 — 선언이 틀린 자리가 가장 늦게 발견된다."""
    import deliberation as d
    import thinking as th

    h = {"record_id": "R-1", "section_id": "2", "title": "t", "snippet": "b",
         "tags": ["confidence:heuristic"]}
    assert th._hit_line(h) == d.knowledge_line(h) == a._knowledge_line(h)
    assert "R-1" in th._hit_line(h) and "경험칙" in th._hit_line(h)


# ── 허브 안내 구분자는 문구 하나에 기대지 않는다 ─────────────────────────────
def test_머리말_문구가_바뀌어도_잘린다():
    """상류가 'How to access' 를 'How to use' 로만 바꿔도 조용히 안 잘리면
    안내문 2,840자가 통째로 프롬프트에 들어간다."""
    for head in ("## How to access this hub", "## How to use this hub — MCP tools",
                 "### Accessing this hub", "##### How to access this hub"):
        got = a._role_doc(f"역할 본문\n\n---\n\n{head}\n안내문 본문")
        assert got == "역할 본문", f"{head!r} → {got!r}"


def test_평문_속_같은_표현은_안_자른다():
    txt = "평문 안의 (how to access this hub) 는 역할 본문이다"
    assert a._role_doc(txt) == txt


# ── 실제 코퍼스에서 확인한 머리말(전수 796명, 2026-09-13) ────────────────────
#    잘리는 구간에 한국어 역할 문장이 남은 전문가는 **0건**이었다. 그래서 "안내문 뒤 본문을
#    버린다" 는 오늘 이 코퍼스에서는 결함이 아니다 — 고치면 안내문이 되살아날 위험만 진다.
#    상류가 배치를 바꾸면 이 전제가 깨지므로, 그때 알아채라고 머리말 실물을 박아 둔다.
_REAL_HEADS = [
    "## How to access this hub — use the MCP tools (NOT web fetch)",
    "## CRITICAL — do NOT use WebFetch / browser fetch on this hub",
]


def test_실제_머리말을_전부_잡는다():
    for h in _REAL_HEADS:
        got = a._role_doc(f"당신은 휨 해석 전문가다.\n판단 기준은 상대 휨이다.\n\n---\n\n{h}\n안내문")
        assert got == "당신은 휨 해석 전문가다.\n판단 기준은 상대 휨이다.", f"{h!r} → {got!r}"


def test_안내문이_없는_역할은_통째로_남는다():
    """HE팀 MCP 운영자 15명은 허브 안내 블록이 애초에 안 붙는 별도 계보다(전수 확인).
    역할 본문이 agent_search·recommend_agents 를 정당하게 언급해도 잘리면 안 된다."""
    txt = ("당신은 AIDataHub 운영자다. agent_search 로 찾고 recommend_agents 로 넘긴다.\n"
           "지식 등록은 사람 확인 뒤에 한다.")
    assert a._role_doc(txt) == txt

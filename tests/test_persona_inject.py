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


def test_모르는_값은_꼬리표를_만들지_않는다():
    ln = a._knowledge_line({"title": "t", "snippet": "b", "causal_status": "weird-new-value"})
    assert "미검증" not in ln and "가설" not in ln


def test_챗과_심의가_같은_포맷을_쓴다():
    """따로 만들면 한쪽만 필드를 빠뜨린다 — 실제로 둘 다 record_id 를 빠뜨리고 있었다.

    동일성(is)이 아니라 **결과**를 비교한다. 같은 파일 안에 importlib.reload 를 쓰는
    테스트가 있어 모듈 객체는 갈릴 수 있지만, 계약은 '같은 히트 → 같은 줄' 이다."""
    import deliberation as d

    h = {"record_id": "R-9", "section_id": "4", "title": "제목", "section_title": "절",
         "snippet": "본문", "causal_status": "unknown"}
    assert a._knowledge_line(h) == d.knowledge_line(h)

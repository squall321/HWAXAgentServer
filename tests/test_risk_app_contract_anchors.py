# 리스크 앱·포털이 기대는 엔진 계약의 닻 — 이름·수·글자열을 손으로 적은 값과 견준다(다른 리포가 이 값에 자기를 맞춘다)
#
# 리스크 앱(HWAXRisk)과 포털(HWAXPortal)의 시험은 이 엔진의 값을 읽어 자기와 견준다(엔진이 오라클이다). 그런데 엔진
# 쪽에 그 값을 붙드는 시험이 없으면, 엔진에서 값을 바꿔도 엔진의 시험은 전부 통과하고 그쪽은 '엔진과 다르다' 고만
# 말한다. 일치 시험은 둘 다 틀리면 통과한다(tests/test_evidence_contract_anchors 와 같은 까닭).
#
# 그래서 여기서는 엔진의 상수를 **읽지 않고** 손으로 적은 값과 견준다. 바꾸려면 이 파일과 그 리포를 같이 고친다.
#
#   실행:  .venv/bin/python -m pytest tests/test_risk_app_contract_anchors.py -q
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import deliberation as d  # noqa: E402

# 스트림을 돌리는 닻은 같은 하네스를 쓴다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context, _stream, _Tool  # noqa: E402, F401


# ── 좌석 지식카드 검색어(knowledge_query) — 포털 DelibOpts 와 같은 수 ─────────────────────
def test_검색어_상한은_600자와_64석이다():
    assert d._KNOWLEDGE_Q_MAX == 600
    assert d._KNOWLEDGE_Q_KEYS_MAX == 64


def test_포털이_읽는_줄_모양이_그대로다():
    """포털의 계약 시험은 엔진 **소스의 줄**을 `^이름\\s*=\\s*(\\d+)` 로 읽는다 — 식으로 바꾸거나 한 줄에 둘을 묶으면
    그쪽이 못 찾는다."""
    src = (ROOT / "deliberation.py").read_text(encoding="utf-8")
    for name, want in (("_KNOWLEDGE_Q_MAX", 600), ("_KNOWLEDGE_Q_KEYS_MAX", 64)):
        found = re.search(rf"^{name}\s*=\s*(\d+)\s*$", src, re.M)
        assert found and int(found.group(1)) == want, (name, found and found.group(0))
    assert 'req_opts.get("knowledge_query")' in src, "포털 계약 시험이 찾는 읽는 식이 없다"


# ── 지식카드 집계 상태줄(knowledge)과 카드 근거 프레임 — 리스크 앱 collect_stream 이 읽는다 ────────────────
_KN_KEYS = {"persona", "hits", "degraded", "query"}
_KN_SOURCE_TAIL = " · 지식카드"


def test_지식카드_집계의_키와_카드_근거의_꼬리가_그대로다(monkeypatch):
    """리스크 앱은 상태줄의 `knowledge` 키 넷으로 좌석별 적중을 세고, 근거 프레임 `source` 의 `<키> · 지식카드` 꼬리로
    카드 근거를 좌석에 귀속한다. 이름을 바꾸면 그쪽 집계가 말없이 빈다."""
    hit = '{"hits": [{"record_id": "R-1", "section_id": "S1", "title": "카드", "snippet": "본문"}]}'
    events = _stream(monkeypatch, {}, tools={"agent_search": _Tool("agent_search", hit)})
    kn = [data["knowledge"] for ev, data in events if ev == "status" and "knowledge" in data]
    assert kn and all(set(x) == _KN_KEYS for x in kn), kn
    assert {x["query"] for x in kn} <= {"caller", "question"}
    cards = [data["source"] for ev, data in events if ev == "delib" and data.get("kind") == "evidence"
             and str(data.get("source", "")).endswith(_KN_SOURCE_TAIL)]
    assert sorted(cards) == ["mech-a · 지식카드", "rel-b · 지식카드"], cards


# ── 근거 프레임 source 의 글자열 — 리스크 앱이 근거를 좌석·원천에 귀속하는 규칙이 이 셋을 본다 ─────────────────
def test_리스크_앱이_귀속에_쓰는_근거_source_글자열이_그대로다(monkeypatch, tmp_path):
    """지식카드(`<키> · 지식카드`)·자유 조회 실패(`<키> · 자유 조회 실패`)·호출자 근거 되울림(`챗 정리 · <원천>`).
    예제 파일(tests/fixtures/risk_contract/panel_frames.jsonl)을 뜨는 실행과 같은 실행으로 본다."""
    from test_risk_contract_frames import _panel_frames
    srcs = {str(data.get("source", "")) for ev, data in _panel_frames(monkeypatch, tmp_path)
            if ev == "delib" and data.get("kind") == "evidence"}
    assert "mech-a · 지식카드" in srcs, sorted(srcs)
    assert "rel-b · 자유 조회 실패" in srcs, sorted(srcs)
    assert "챗 정리 · 변경 비교 요약" in srcs, sorted(srcs)

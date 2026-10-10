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

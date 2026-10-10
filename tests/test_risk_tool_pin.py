# 리스크 심사 좌석의 도구 추림에서 변경 원장 도구(risk_get_diff)를 맨 앞에 세운다 — 예산을 더 쓰지 않고 순위만 올린다
#
# 좌석마다 도구를 자기 분야 순으로 훑어 스키마 예산 안에서 남긴다(_tools_for_seat). 변경 전체를 보는 도구는 이름이
# 어느 분야와도 안 걸려 뒤로 밀리고, 작은 창에서는 예산 밖으로 떨어진다. 질문에 변경 비교 표식(diff_id=)이 있는
# 리스크 심사에서만 그 도구를 앞에 세운다(고정). 얹지 않는다 — 고정한 만큼 뒤 순위가 밀려 나간다(설계 P-07).
#
#   실행:  .venv/bin/python -m pytest tests/test_risk_tool_pin.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _pin_context  # noqa: E402, F401


class _T:
    def __init__(self, n, size=200):
        self.name, self.description, self.args_schema = n, "", {"a": "b" * size}


_NAMES = ["list_materials", "search_reports", "get_material", "query_voc", "interface_graph",
          "report_part_risk", "inspect_report", "risk_get_diff", "risk_get_snapshot", "get_top_issues"]
_SEAT = {"key": "mech-housing-structure", "role": "기구 하우징 구조 강성"}
_Q = "힌지 크랙 원인 diff_id=3f2a"


def _pool(names=_NAMES, size=200):
    return {n: _T(n, size) for n in names}


def _by_rank(g, budget):
    """종전 규칙 그대로 — _seat_tool_rank 순으로 훑으며 예산 안에서 남긴다."""
    kept, used = [], 0
    for n in d._seat_tool_rank(list(g), _SEAT, _Q):
        c = d._tool_cost(g[n])
        if kept and used + c > budget:
            continue
        kept.append(n)
        used += c
    return kept


def test_고정을_안_주면_좌석_도구_목록이_종전과_같다(monkeypatch):
    """특성화 — 고정 인자를 넣기 전 코드에서 초록이었다. 리스크 심사가 아닌 심의는 이 길이다."""
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    g = _pool()
    for budget in (10 ** 6, 300, 120):
        assert list(d._tools_for_seat(g, _SEAT, _Q, budget)) == _by_rank(g, budget), budget


def test_고정한_도구는_맨_앞에_서고_후보에_없으면_아무_일도_없다(monkeypatch):
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    g = _pool()
    kept = list(d._tools_for_seat(g, _SEAT, _Q, 10 ** 6, pin=d._RISK_PIN_TOOLS))
    assert kept[0] == "risk_get_diff" and sorted(kept) == sorted(_NAMES), kept
    assert kept[1:] == [n for n in _by_rank(g, 10 ** 6) if n != "risk_get_diff"], "나머지 순서가 바뀌었다"
    # 후보에 없는 도구를 고정해도 목록이 그대로다(도구를 못 쓰는 자격·옛 게이트웨이).
    g2 = _pool([n for n in _NAMES if n != "risk_get_diff"])
    assert list(d._tools_for_seat(g2, _SEAT, _Q, 10 ** 6, pin=d._RISK_PIN_TOOLS)) == _by_rank(g2, 10 ** 6)


def test_좁은_창에서도_고정_뒤_스키마_합이_예산을_넘지_않는다(monkeypatch):
    """얹지 않고 순위만 올린다 — 16K 창(dev)에서 고정 뒤에도 스키마 합이 좌석 도구 예산 안이다(설계 P-07)."""
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    app._ctx_cache["n"] = 16384
    d._free_tok_cache.clear()
    budget = d._free_tool_tokens()
    names = [f"search_tool_{i:02d}" for i in range(28)] + ["risk_get_diff", "risk_get_snapshot"]
    g = _pool(names, size=900)
    plain = d._tools_for_seat(g, _SEAT, _Q, budget)
    kept = d._tools_for_seat(g, _SEAT, _Q, budget, pin=d._RISK_PIN_TOOLS)
    assert "risk_get_diff" not in plain, "시험 전제 — 고정하지 않으면 예산 밖으로 밀리는 크기여야 한다"
    assert "risk_get_diff" in kept, list(kept)
    assert sum(d._tool_cost(t) for t in kept.values()) <= budget
    assert len(kept) <= len(plain), "고정이 도구를 얹었다"
    d._free_tok_cache.clear()

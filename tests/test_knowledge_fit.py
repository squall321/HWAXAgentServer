# 좌석 지식카드를 예산에 담는 방식 — 긴 카드 한 장이 좌석을 0장으로 만들지 않고, 카드 수가 줄지 않는다
#
# AIDataHub 가 히트의 snippet 을 절 본문 전문으로 바꾼 뒤(물성 카드 한 절이 2만~6만 자), 엔진은 한 줄을 좌석 예산까지
# 받고(표식이 붙어 예산보다 몇십 자 길다) 처음 넘는 줄에서 break 했다. 첫 히트가 예산보다 길면 좌석은 카드 0장으로
# 발언하고 상태줄은 '관련 지식 없음' 이라 적었으며, 큰 카드 뒤의 작은 카드도 전부 빠졌다(engine-budget A23-1).
# 좁은 창(dev)에서는 10건이던 카드가 몇 건으로 줄었다(gw-adh GA-3). 띵킹 예심도 같은 꼴이었다.
#
#   실행:  .venv/bin/python -m pytest tests/test_knowledge_fit.py -q
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402
import thinking as th  # noqa: E402

from test_delib_silent_drops import _cards, _steps, _Tool  # noqa: E402
from test_window_budget_b import _BIG, _run  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_ctx():
    saved = dict(app._ctx_cache)
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


def _hit_list(sizes):
    return [{"title": f"카드{i}", "record_id": f"r{i:02d}", "snippet": "가" * n + f" [카드끝{i}]"}
            for i, n in enumerate(sizes)]


def _hits(sizes):
    return json.dumps({"hits": _hit_list(sizes)}, ensure_ascii=False)


def _seat_block(llm, key="mech-a"):
    """그 좌석의 첫 발언 프롬프트에 실린 지식카드 블록."""
    human = next(h for s, h in llm.prompts if f"당신은 '{key}' 전문가" in s)
    m_ = re.search(r"\[당신의 지식카드에서[^\n]*\n(.*?)(?:\n\n\[|\Z)", human, re.S)
    return m_.group(1) if m_ else ""


def _delib(monkeypatch, tokens, sizes):
    events, llm = _run(monkeypatch, tokens, persona_knowledge=1,
                       tools={"agent_search": _Tool("agent_search", _hits(sizes))})
    return events, _seat_block(llm)


def _loaded(blk, n):
    return [i for i in range(n) if f"(출처: r{i:02d})" in blk]


def _whole(blk, n):
    return [i for i in range(n) if f"[카드끝{i}]" in blk]


def _seat_step(events, key="mech-a"):
    return next(s for s in _steps(events) if s.startswith("지식카드 ") and key in s)


# ── A23-1 — 첫 카드가 예산을 넘어도 0장이 되지 않는다 ─────────────────────────────────────────────
@pytest.mark.parametrize("tokens,first", [(16384, 4000), (128000, 4000), (_BIG, 20000)])
def test_첫_카드가_예산보다_길어도_좌석은_카드를_받는다(monkeypatch, tokens, first):
    """종전엔 첫 줄이 예산보다 길어 i=0 에서 멈췄다 — 카드 0장, 상태줄 '관련 지식 없음(10건 중 0건)'."""
    events, blk = _delib(monkeypatch, tokens, [first] + [300] * 9)
    assert _loaded(blk, 10) == list(range(10)), f"좌석이 받은 카드 {_loaded(blk, 10)}"
    assert _whole(blk, 10) == list(range(1, 10)), "뒤의 작은 카드가 온전히 실리지 않았다"
    assert re.search(r"…\[[\d,]+자 중 앞 [\d,]+자\]", blk), "긴 카드를 줄였는데 원문 길이 표식이 없다"
    assert len(blk) <= d._knowledge_budget(), (len(blk), d._knowledge_budget())
    step = _seat_step(events)
    assert "관련 지식 없음" not in step and "10건 중 10건" in step and "1건은 앞부분만" in step, step
    kn = {x["knowledge"]["persona"]: x["knowledge"]["hits"] for ev, x in events
          if ev == "status" and "knowledge" in x}
    assert kn["mech-a"] == 10, kn


def test_큰_카드_뒤의_작은_카드가_빠지지_않는다(monkeypatch):
    """1M 에서 [5000, 16000]+[800]×8 — 뒤의 8장(약 6,600자)은 남은 예산에 드는데 break 로 빠졌다."""
    _events, blk = _delib(monkeypatch, _BIG, [5000, 16000] + [800] * 8)
    assert _loaded(blk, 10) == list(range(10)), _loaded(blk, 10)
    assert _whole(blk, 10) == [0] + list(range(2, 10)), _whole(blk, 10)
    assert len(blk) <= d._knowledge_budget()


# ── GA-3 — 좁은 창에서도 카드 수가 줄지 않는다 ─────────────────────────────────────────────────────
def test_좁은_창은_카드를_같은_몫으로_줄여_전부_싣는다(monkeypatch):
    """절 전문이 된 600자 카드 10장 — 종전엔 앞 5장만 온전히 싣고 뒤 5장을 뺐다(줄 하나가 몫 349자보다 길다)."""
    events, blk = _delib(monkeypatch, 16384, [600] * 10)
    assert _loaded(blk, 10) == list(range(10)), _loaded(blk, 10)
    assert len(blk) <= 3500, len(blk)
    step = _seat_step(events)
    assert "10건 중 10건" in step and "10건은 앞부분만" in step, step
    cards = [c for c in _cards(events, included=False) if c["source"] == "지식카드 예산 초과"]
    assert cards and "mech-a 10건 중 10건(10건은 앞부분만)" in cards[0]["text"], cards


def test_한_줄_몫이_바닥보다_작아지면_뒤쪽을_빼고_수를_남긴다(monkeypatch):
    """히트가 아주 많으면 같은 몫이 몇십 자가 된다 — 바닥(300자)을 지키고 순위가 낮은 쪽을 뺀다."""
    events, blk = _delib(monkeypatch, 16384, [600] * 20)
    got = _loaded(blk, 20)
    assert got == list(range(10)), got
    assert len(blk) <= 3500
    assert all(len(ln) >= d._KN_LINE_FLOOR for ln in blk.split("\n")), [len(x) for x in blk.split("\n")]
    assert "20건 중 10건" in _seat_step(events)


# ── 함수의 약속 — 이은 글은 예산 안, 순위 순, 1위 카드는 남는다 ──────────────────────────────────────
_SIZES = ([300] * 10, [600] * 10, [4000] + [300] * 9, [60000, 1900, 60000, 1900, 1900, 56000, 1900, 1900],
          [5000, 16000] + [800] * 8, [800] * 40, [20000] * 3, [10])


@pytest.mark.parametrize("budget", (40, 100, 333, 2500, 3500, 19385))
@pytest.mark.parametrize("sizes", _SIZES)
def test_담은_글은_예산_안이고_순위_순이며_1위_카드가_남는다(sizes, budget):
    hits = _hit_list(sizes)
    lines, dropped, trimmed = d.fit_knowledge_lines(hits, budget)
    assert len("\n".join(lines)) <= budget, (len("\n".join(lines)), budget)
    assert lines, "히트가 있는데 한 장도 싣지 않았다"
    full = [d.knowledge_line(h, limit=0) for h in hits]
    for i, ln in enumerate(lines):       # i 번째 줄은 i 위 카드 원문 그대로이거나 그 앞부분 + 원문 길이 표식이다
        head, _sep, mark = ln.partition(" …[")
        assert full[i].startswith(head), (i, ln[:40])
        assert ln == full[i] or mark.startswith(f"{len(full[i]):,}자 중 앞 "), (i, ln[-40:])
    assert len(lines) + dropped == len(sizes)
    assert trimmed == sum("…[" in ln for ln in lines)


def test_몫보다_조금_긴_줄은_줄이지_않는다():
    """줄이면 표식이 붙어 오히려 길어지는 줄(몫 + 표식 안)은 온전히 둔다 — 종전 안은 330자 카드를 318자로 줄였다."""
    lines, _dropped, trimmed = d.fit_knowledge_lines(_hit_list([4000] + [300] * 9), 3500)
    assert trimmed == 1 and all(f"[카드끝{i}]" in lines[i] for i in range(1, 10)), [ln[-12:] for ln in lines]


def test_예산이_0_이면_싣지_않는다():
    """종전 루프와 같은 뜻이다 — 0 을 무제한으로 바꾸면 0 으로 꺼 둔 좁은 창 박스에 카드 전문이 실린다."""
    assert d.fit_knowledge_lines(_hit_list([600] * 3), 0) == ([], 3, 0)


def test_중복_줄은_한_번만_세고_싣는다():
    h = _hit_list([600])
    lines, dropped, trimmed = d.fit_knowledge_lines(h + h + _hit_list([300, 300])[1:], 3500)
    assert len(lines) == 2 and dropped == 0 and trimmed == 0, (lines, dropped, trimmed)


# ── 띵킹 예심 — 같은 함수로 담는다 ────────────────────────────────────────────────────────────────
def _think(tokens, sizes):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()

    class _T:
        async def ainvoke(_self, _args):  # noqa: ANN001
            return _hits(sizes)
    seat = {"key": "dom-x", "name": "x", "domain": "dom", "score": 1.0,
            "desc_match": 0.5, "sections": 0, "records": 0, "why": ""}
    return asyncio.run(th._screen_one({"agent_search": _T()}, seat, "질의"))


def test_띵킹_좁은_창은_바닥을_지키며_종전보다_많이_싣는다():
    """2,500자 예산에 525자 카드 10장 — 종전엔 앞 4장(700자 줄)에서 멈췄다. 같은 몫이 바닥(300자)을 지키는 7장까지 싣는다."""
    r = _think(16384, [500] * 10)
    assert _loaded(r["knowledge"], 10) == list(range(7)), r["knowledge"][:300]
    assert len(r["knowledge"]) <= th._knowledge_budget()
    assert "지식카드 10건 중 7건 실음(7건은 앞부분만 · 예산 2,500자)" in r["reason"], r["reason"]


def test_띵킹_첫_카드가_길어도_뒤_카드가_온전하다():
    r = _think(_BIG, [60000] + [300] * 7)
    assert _loaded(r["knowledge"], 8) == list(range(8))
    assert _whole(r["knowledge"], 8) == list(range(1, 8))
    assert len(r["knowledge"]) <= th._knowledge_budget()

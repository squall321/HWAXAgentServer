# 의장 전사 상한(DELIB_DECISION_CTX) — 모델 컨텍스트에서 유도하고, 줄일 때는 좌석마다 같은 몫이다
#
# 의장은 라운드당 6,000자만 받았다. dev 16K 창의 방어값이 운영 창(128K)에서도 그대로였고, 실사용
# 라운드는 37,000자였다 — 의장은 전사의 1/6 로 결정문을 썼고, 결정문이 스스로 '생략된 좌석의 입장은
# 반영하지 못했다' 고 적었다(S26U 피드백 1-7). 좌석은 같은 라운드를 48,000자까지 받는다.
#
# **스트림을 실제로 돌려서** 의장이 받은 프롬프트를 본다. 상한 함수만 떼어 재면 '값은 맞는데 그
# 값으로 자르지 않는다' 를 못 잡는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_chair_ctx.py -q
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _mcp_view, _pin_context, _steps, _stream  # noqa: E402, F401

_CUT = "의장 전사 상한 초과"
_N_SEATS = 20
_SEAT_CHARS = 1800                     # 좌석당 본문 — 20석이면 라운드 하나가 약 37,000자(실사용 실측)
_HEAD = re.compile(r"\[(\d+)R (?:초기입장|심화|최종)\]\n")


def _ctx(monkeypatch, tokens, explicit=None):
    """모델 컨텍스트와 DELIB_DECISION_CTX(명시값, None=안 줌)를 고정한다."""
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()
    monkeypatch.setattr(d, "_DECISION_CTX", explicit)


def _seats(n=_N_SEATS):
    return [{"key": f"dom{i:02d}-seat", "role": "역할"} for i in range(1, n + 1)]


def _run(monkeypatch, *, rounds=3, seats=_N_SEATS, llm=None, **opts):
    """심의를 끝까지 돌려 (이벤트, 의장 시스템 프롬프트, 의장 본문, 라운드별 블록)을 받는다.

    좌석 발언은 좌석·필드마다 다른 꼬리표로 감싼 긴 글이다 — 의장 프롬프트에서 어느 좌석의 어느
    라운드가 얼마나 남았는지 셀 수 있다. 직렬화 값 상한(_SER_CLIP)은 풀어 둔다(라운드 길이를
    좌석당 본문 길이로 정하려고)."""
    seen = []

    async def _llm(_llm_obj, system, human):
        m = re.search(r"당신은 '([^']+)' 전문가", system)
        if not m:
            seen.append((system, human))
            return "결정문 본문"
        k = m.group(1)

        def body(tag):
            return f"<{k}:{tag}>" + "가" * _SEAT_CHARS + f"</{k}:{tag}>"

        return json.dumps({"lens": body("lens"), "reads": [], "recommendation": "권장", "concerns": ["A", "B"],
                           "position_short": "요약", "concede": [], "rebut": ["반박"], "deepen": body("deepen"),
                           "final_position": body("final"), "non_negotiable": "", "vote": "진행",
                           "stance": "동의"}, ensure_ascii=False)

    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_SER_CLIP", 0)
    events = _stream(monkeypatch, {"personas": _seats(seats), "rounds": rounds, "save_report": 0,
                                   "persona_knowledge": 0, "rebut_quote": 0, **opts},
                     until=lambda ev, _data: ev == "done", llm=llm)
    system, human = next((s, h) for s, h in seen if "엔지니어링 톤" in s)
    marks = list(_HEAD.finditer(human))
    assert len(marks) == rounds, f"의장 프롬프트에 라운드 머리가 {len(marks)}개다 — 하네스가 낡았다"
    ends = [m.start() for m in marks[1:]] + [human.index("\n\n[참여 좌석", marks[-1].end())]
    blocks = [human[m.end():e].rstrip("\n") for m, e in zip(marks, ends)]
    return events, system, human, blocks


def _kept(block):
    """블록에 남은 좌석 본문 글자 수(표지·안내 줄은 빼고 센다)."""
    return block.count("가")


def _fits(system, human, tokens):
    """좌석 예산과 같은 환산(자/토큰 최악값)으로 의장 프롬프트 + 출력 몫이 컨텍스트 안인가."""
    return (len(system) + len(human)) / d._EVID_KO_CPT + d._CHAIR_RESERVE <= tokens


# ── 넓은 창 — 종전의 1/6 이 아니라 창이 받는 만큼 ────────────────────────────────
@pytest.mark.parametrize("tokens", [128000, 200000])
def test_넓은_창에서는_37000자_라운드_셋을_라운드당_24000자_넘게_싣는다(monkeypatch, tokens):
    _ctx(monkeypatch, tokens)
    _events, system, human, blocks = _run(monkeypatch)
    for i, b in enumerate(blocks, start=1):
        assert _kept(b) >= 24000, f"{i}라운드에서 {_kept(b):,}자만 실렸다 — 종전 6,000자 상한이 살아 있다"
    assert _fits(system, human, tokens), "의장 프롬프트가 컨텍스트를 넘는다"


def test_200K_창은_37000자_라운드_셋을_한_글자도_안_줄인다(monkeypatch):
    _ctx(monkeypatch, 200000)
    events, _system, _human, blocks = _run(monkeypatch)
    assert all(_kept(b) == _N_SEATS * _SEAT_CHARS for b in blocks), [_kept(b) for b in blocks]
    assert not any("아는 척하지 마라" in b for b in blocks), "안 줄였는데 줄였다고 적었다"
    assert not [c for c in _cards(events, included=False) if c["source"] == _CUT]


def test_줄여도_빠지는_좌석이_없다(monkeypatch):
    """종전엔 이어 붙인 전사의 머리·꼬리만 남겨 가운데 좌석이 통째로 빠졌다."""
    _ctx(monkeypatch, 128000)
    _events, _system, _human, blocks = _run(monkeypatch)
    for i, (b, tag) in enumerate(zip(blocks, ("lens", "deepen", "final")), start=1):
        missing = [p["key"] for p in _seats() if f"<{p['key']}:{tag}>" not in b]
        assert not missing, f"{i}라운드에서 의장이 못 본 좌석 — {missing}"
        assert "아는 척하지 마라" in b and "자 생략" in b, "줄였다는 것과 얼마나 뺐는지를 의장에게 말하지 않았다"


# ── 좁은 창 — 종전보다 나빠지지 않는다 ───────────────────────────────────────────
def test_좁은_창은_종전_6000자_그대로다(monkeypatch):
    _ctx(monkeypatch, 16384)
    _events, _system, _human, blocks = _run(monkeypatch)
    for i, b in enumerate(blocks, start=1):
        assert len(b) <= 6000, f"{i}라운드 블록이 {len(b):,}자다 — 종전 상한(6,000자)보다 많이 싣는다"
        assert len(b) >= 6000 * 0.9, f"{i}라운드 블록이 {len(b):,}자다 — 상한이 남는데 덜 싣는다"
        assert all(f"• {p['key']}: " in b for p in _seats()), f"{i}라운드에서 빠진 좌석이 있다"


def test_유도값은_어느_창에서도_종전_기본값보다_작지_않다(monkeypatch):
    for tokens in (4096, 16384, 32768, 65536, 128000, 200000, 1000000):
        _ctx(monkeypatch, tokens)
        for rounds in (2, 3, 8):
            for fixed in (0, 3000, 600000):
                assert d._decision_ctx(fixed, rounds) >= 6000, (tokens, rounds, fixed)


# ── 명시값이 이긴다 ──────────────────────────────────────────────────────────────
def test_명시한_값이_유도를_덮는다(monkeypatch):
    _ctx(monkeypatch, 128000, explicit=9000)
    assert d._decision_ctx(3000, 3) == 9000
    events, _system, _human, blocks = _run(monkeypatch)
    assert all(len(b) <= 9000 for b in blocks), [len(b) for b in blocks]
    assert all(len(b) >= 9000 * 0.9 for b in blocks), [len(b) for b in blocks]
    card = next(c for c in _cards(events, included=False) if c["source"] == _CUT)
    assert "9,000자" in card["text"], card["text"]
    # 설정 이름은 화면 글에 싣지 않는다 — 따로 실어(knob) 잡 원장에만 붙인다. 상태줄도 화면에 나간다.
    assert "DELIB_DECISION_CTX" not in card["text"] and card["knob"] == "DELIB_DECISION_CTX", card
    assert not [s for s in _steps(events) if "DELIB_DECISION_CTX" in s]
    for name, view in _mcp_view(monkeypatch, events).items():
        kept = next(x for x in view["evidence_omitted"] if x.get("source") == _CUT)
        assert "DELIB_DECISION_CTX" in kept["text"], (name, kept)


def test_0_은_무제한이다(monkeypatch):
    _ctx(monkeypatch, 16384, explicit=0)
    assert d._decision_ctx(3000, 3) == 0
    events, _system, _human, blocks = _run(monkeypatch)
    assert all(_kept(b) == _N_SEATS * _SEAT_CHARS for b in blocks), [_kept(b) for b in blocks]
    assert not [c for c in _cards(events, included=False) if c["source"] == _CUT]


def test_환경변수를_안_주면_유도하고_주면_그_값이다():
    def loaded(value):
        env = {k: v for k, v in os.environ.items() if k != "DELIB_DECISION_CTX"}
        if value is not None:
            env["DELIB_DECISION_CTX"] = value
        r = subprocess.run([sys.executable, "-c", "import deliberation as d; print(repr(d._DECISION_CTX))"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
        assert r.returncode == 0, r.stderr[-600:]
        return r.stdout.split()[-1]

    assert loaded(None) == "None", "안 줬는데 값이 박혀 있다 — 유도가 죽는다"
    assert loaded("") == "None"
    assert loaded("24000") == "24000"
    assert loaded("0") == "0", "0(무제한)을 안 준 것으로 읽었다"
    assert loaded("육천") == "6000", "오타 값은 종전 기본값으로 — 서버 기동을 죽이지 않는다"


# ── 컨텍스트 안에 든다 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("tokens", [128000, 200000])
def test_8라운드에_근거가_가득_차도_컨텍스트_안이다(monkeypatch, tokens):
    """라운드 수로 나누지 않으면 3라운드 몫을 여덟 번 싣는다. 근거는 예산을 채워 넣는다 —
    의장 프롬프트에는 좌석이 받은 근거 블록이 그대로 실린다."""
    _ctx(monkeypatch, tokens)
    big = "근" * (d._evid_budget() - 200)
    _events, system, human, blocks = _run(monkeypatch, rounds=8, evidence=[{"result": big}])
    assert big in human, "근거가 의장 프롬프트에 안 실렸다 — 시험 전제가 깨졌다"
    assert _fits(system, human, tokens), (
        f"의장 프롬프트 {len(system) + len(human):,}자 — {tokens:,}토큰 창을 넘는다")
    assert all(_kept(b) > 6000 for b in blocks), "창이 남는데 종전 기본값까지 줄였다"


def test_상한_계산이_고정부와_라운드_수를_함께_본다(monkeypatch):
    for tokens in (128000, 200000, 1000000):
        _ctx(monkeypatch, tokens)
        for rounds in range(2, 9):
            for fixed in (2000, d._pre_budget() + 12000):
                cap = d._decision_ctx(fixed, rounds)
                if cap > 6000:                      # 바닥이 걸리면 보장이 없다(종전과 같다)
                    assert (fixed + rounds * cap) / d._EVID_KO_CPT + d._CHAIR_RESERVE <= tokens, (
                        tokens, rounds, fixed, cap)
        assert d._decision_ctx(2000, 3) > d._decision_ctx(2000, 8), "라운드가 늘어도 라운드당 몫이 같다"
        assert d._decision_ctx(2000, 3) > d._decision_ctx(60000, 3), "근거가 늘어도 전사 몫이 같다"


def test_LLM_에_max_tokens_가_크게_걸려_있으면_그만큼을_출력_몫으로_뗀다(monkeypatch):
    """서버는 프롬프트 + max_tokens 가 창을 넘으면 자르지 않고 400 으로 거절한다. 의장은 맨 끝에 한 번
    도는 호출이라 거기서 죽으면 라운드를 다 돌고도 결정문이 없다."""
    from types import SimpleNamespace

    _ctx(monkeypatch, 128000)
    big = 60000                                    # 기본 출력 몫(_CHAIR_RESERVE)보다 큰 max_tokens
    assert big > d._CHAIR_RESERVE
    _events, system, human, blocks = _run(monkeypatch, llm=SimpleNamespace(max_tokens=big))
    assert (len(system) + len(human)) / d._EVID_KO_CPT + big <= 128000, (
        f"의장 프롬프트 {len(system) + len(human):,}자 + max_tokens {big:,} — 창을 넘어 400 이 난다")
    assert all(len(b) > 6000 for b in blocks), "창이 남는데 바닥까지 줄였다"
    assert d._decision_ctx(3000, 3, big) < d._decision_ctx(3000, 3)
    assert d._decision_ctx(3000, 3, 8192) == d._decision_ctx(3000, 3), "작은 max_tokens 가 몫을 줄였다"


# ── 한 라운드 맞추기(_cap_ctx) ───────────────────────────────────────────────────
def _rows(n, each):
    return [(f"k{i:02d}", "가" * each) for i in range(n)]


def test_상한_안이면_라운드_전사와_한_글자도_다르지_않다():
    rows = _rows(12, 2000)
    assert d._cap_ctx(rows, 48000) == "\n".join(f"• {k}: {t}" for k, t in rows)
    assert d._cap_ctx(rows, 0) == "\n".join(f"• {k}: {t}" for k, t in rows)


@pytest.mark.parametrize("cap", [6000, 9000, 24000, 33000])
def test_넘치면_표지와_안내까지_상한_안이고_전원이_남는다(cap):
    rows = _rows(21, 1760)                          # 21석 37,000자 라운드
    out = d._cap_ctx(rows, cap)
    assert len(out) <= cap, f"상한 {cap:,}자인데 {len(out):,}자를 싣는다"
    assert out.count("가") >= cap * 0.8, "상한이 남는데 덜 실었다"
    assert all(f"• {k}: 가" in out for k, _ in rows), "빠진 좌석이 있다"


def test_짧은_발언은_자르지_않고_얼마나_뺐는지_말한다():
    rows = [("long", "가" * 30000), ("short", "나" * 300)]
    out = d._cap_ctx(rows, 10000)
    assert "• short: " + "나" * 300 in out
    head = out.split("\n", 1)[0]
    assert "아는 척하지 마라" in head and f"{len('• long: ') + 30000 + 1 + len('• short: ') + 300:,}자" in head
    cut = 30000 - out.count("가")
    assert f"{cut:,}자 생략" in head, head


# ── 줄였으면 화면과 MCP 호출자에게 남긴다 ─────────────────────────────────────────
def test_줄였으면_화면과_잡_원장에_남는다(monkeypatch):
    """종전엔 의장만 알았다 — 읽는 사람은 결정문의 한 줄로만 눈치챘다."""
    _ctx(monkeypatch, 128000)
    events, _system, _human, _blocks = _run(monkeypatch)
    card = next(c for c in _cards(events, included=False) if c["source"] == _CUT)
    for want in ("1R ", "2R ", "3R ", "모델 컨텍스트에서 유도"):
        assert want in card["text"], (want, card["text"])
    # 이 카드는 좌석에 안 준 근거가 아니라 알림이다 — 포털이 '좌석에 주지 않음' 딱지를 붙이지 않게 표시한다.
    assert card.get("notice") is True, card
    assert any(s.startswith("의장 전사 상한") for s in _steps(events))
    for name, view in _mcp_view(monkeypatch, events).items():
        assert [x for x in view["evidence_omitted"] if x.get("source") == _CUT], (name, view["evidence_omitted"])


def test_의장_전사도_짧은_좌석이_남긴_몫을_긴_좌석에_돌린다():
    rows = [(f"long{i}", "가" * 2900) for i in range(10)] + [(f"short{i}", "나" * 700) for i in range(11)]
    out = d._cap_ctx(rows, 29000)
    assert len(out) <= 29000
    assert len(out) >= 29000 * 0.97, f"상한 29,000자 중 {len(out):,}자만 실었다 — 남는 몫을 버렸다"
    assert all(f"• short{i}: " + "나" * 700 in out for i in range(11))

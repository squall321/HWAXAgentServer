# 심의 도중 좌석이 자유 조회로 받은 그림 — 판독해 그 호출 뒤에 근거 항목(vision_transcribe)으로 싣는가(HWAXPortal docs/chat-depth-context T5-b · CD-D14)
#
# 왜 생겼나. 심의의 그림 판독(T5)은 심의를 **열 때** 걸린 그림만 읽었다. 좌석이 심의 도중 조회로 받은 그림은 결과 글의 주소로만 가서 모델이
# 보지 않았다(2026-10-21 사용자 요청 '심의 도중 전문가가 받은 그림도 판독문으로 근거에').
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_seat_images.py -q
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import deliberation as d  # noqa: E402
import vision  # noqa: E402

# 같은 하네스를 쓴다 — 스트림을 실제로 돌린다(_pin_context 는 이 파일에도 걸리게 이름째 가져온다).
from test_delib_silent_drops import _cards, _pin_context, _steps, _stream, _Tool  # noqa: E402, F401
from test_free_gather import _Agent, _Msg  # noqa: E402

IMG1, IMG2 = "/agent/artifacts/plot111.png", "/ai-data-hub/attachments/cards/fig222.png"
_SEAT_JSON = json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["A", "B"],
                         "position_short": "요약", "concede": [], "rebut": ["반박"], "deepen": "심화",
                         "final_position": "최종", "non_negotiable": "", "vote": "진행", "stance": "동의"}, ensure_ascii=False)


class _Reader:
    """판독기 대역 — 그림 주소는 진짜 규칙(아티팩트 · AIDataHub 첨부)으로 찾고, 판독은 정해 둔 글을 준다. texts 가 None 이면 '판독할 모델 없음'."""

    def __init__(self, texts=None, limit=8):
        self.texts, self.limit, self.asked = texts, limit, []

    @staticmethod
    def image_urls(text):
        return vision.image_urls(text)

    async def read(self, urls, hint=""):
        self.asked.append((list(urls), hint))
        if self.texts is None:
            return None
        return {u: self.texts.get(u, "") for u in urls}


def _acc():
    return {"read": 0, "fail": 0, "no_model": 0, "over": 0, "err": 0}


def _tr(monkeypatch, calls, reader, question="힌지 크랙 원인"):
    """_seat_image_transcripts 를 좌석 태스크처럼(셈을 건 문맥에서) 부른다 — (결과, 셈)."""
    monkeypatch.setattr(d, "SEAT_IMAGE_READER", reader)
    acc = _acc()

    async def go():
        d._SEAT_IMG.set(acc)
        return await d._seat_image_transcripts(calls, question)
    return asyncio.run(go()), acc


# ── 판독문의 자리와 꼴 ────────────────────────────────────────────────────────
def test_판독문은_그림을_낸_호출_바로_뒤에_따로_된_근거_항목이다(monkeypatch):
    rd = _Reader({IMG1: "응력 지도 — 최대 432 MPa(부품 B2)"})
    calls = [("plot_stress", '{"part": "B2"}', f"그림: {IMG1}", "c1"), ("get_material", "{}", '{"E": 70}', "c2")]
    out, acc = _tr(monkeypatch, calls, rd)
    assert [c[0] for c in out] == ["plot_stress", "vision_transcribe", "get_material"]
    assert out[0] == calls[0] and out[2] == calls[1], "원래 호출(도구 결과)은 손대지 않는다 — 판독문을 섞지 않는다"
    _n, ap, body, cid = out[1]
    assert json.loads(ap) == {"image": IMG1, "from": "plot_stress"} and cid == "c1#img0"
    assert body.startswith("[그림 판독 — plot_stress") and "도구 결과가 아니다" in body and "432 MPa" in body
    assert rd.asked == [([IMG1], "힌지 크랙 원인")], "맥락(심의 주제)을 판독기에 넘긴다"
    assert acc == {**_acc(), "read": 1}


def test_여러_그림은_처음_나온_호출_뒤에_한_번씩이다(monkeypatch):
    rd = _Reader({IMG1: "그림 1", IMG2: "그림 2"})
    calls = [("a", "{}", f"{IMG1} 와 {IMG2}", "c1"), ("b", "{}", f"다시 {IMG1}", "c2")]
    out, acc = _tr(monkeypatch, calls, rd)
    assert [(c[0], c[3]) for c in out] == [("a", "c1"), ("vision_transcribe", "c1#img0"), ("vision_transcribe", "c1#img1"),
                                          ("b", "c2")], out
    assert rd.asked[0][0] == [IMG1, IMG2] and acc["read"] == 2


def test_판독_실패는_싣지_않고_센다(monkeypatch):
    out, acc = _tr(monkeypatch, [("a", "{}", f"{IMG1} {IMG2}", "c1")], _Reader({IMG1: "읽음", IMG2: ""}))
    assert [c[0] for c in out] == ["a", "vision_transcribe"] and acc["read"] == 1 and acc["fail"] == 1


def test_판독할_모델이_없으면_그대로_두고_센다(monkeypatch):
    calls = [("a", "{}", f"{IMG1} {IMG2}", "c1")]
    out, acc = _tr(monkeypatch, calls, _Reader(None))
    assert out == calls and acc == {**_acc(), "no_model": 2}


def test_조회_한_번의_상한_밖은_읽지_않고_센다(monkeypatch):
    rd = _Reader({IMG1: "그림 1", IMG2: "그림 2"}, limit=1)
    out, acc = _tr(monkeypatch, [("a", "{}", f"{IMG1} {IMG2}", "c1")], rd)
    assert [c[0] for c in out] == ["a", "vision_transcribe"] and rd.asked[0][0] == [IMG1]
    assert acc["read"] == 1 and acc["over"] == 1
    rd0 = _Reader({IMG1: "그림 1"}, limit=0)                     # VISION_TRANSCRIBE_MAX=0 — 판독을 끈 박스
    out, acc = _tr(monkeypatch, [("a", "{}", IMG1, "c1")], rd0)
    assert len(out) == 1 and rd0.asked == [] and acc["over"] == 1, "판독기를 부르지도 않는다"


def test_실패한_조회의_그림_주소는_읽지_않는다(monkeypatch):
    rd = _Reader({IMG1: "읽음"})
    calls = [("a", "{}", f"Error executing tool a: 없는 부품 — {IMG1}", "c1")]
    out, acc = _tr(monkeypatch, calls, rd)
    assert out == calls and rd.asked == [] and acc == _acc()


def test_판독문을_다시_판독하지_않는다(monkeypatch):
    """두 번 거쳐도(되풀이 호출) 판독문 항목 안의 주소를 또 읽지 않는다 — 판독 모델이 원본 주소를 되읊는 일이 있다."""
    rd = _Reader({IMG1: f"판독 — 원본 {IMG1} 의 응력 지도"})
    once, _acc1 = _tr(monkeypatch, [("a", "{}", IMG1, "c1")], rd)
    twice, acc = _tr(monkeypatch, [once[1]], rd)
    assert twice == [once[1]] and len(rd.asked) == 1 and acc == _acc()


def test_판독기가_없으면_종전_그대로다(monkeypatch):
    calls = [("a", "{}", IMG1, "c1")]
    out, acc = _tr(monkeypatch, calls, None)
    assert out == calls and acc == _acc()


def test_짝_키가_없는_호출의_판독문도_짝_키가_없다(monkeypatch):
    out, _acc2 = _tr(monkeypatch, [("a", "{}", IMG1, "")], _Reader({IMG1: "읽음"}))
    assert out[1][3] == "", "없는 짝 키에 '#img0' 만 붙이면 서로 다른 좌석의 판독문이 같은 키로 묶인다"


# ── 좌석 자유 조회 하나 — 그 좌석의 발언 블록에 실리고 조회 예산을 먹지 않는다 ─────────────────────
def _gather_msgs():
    tool = _Msg("tool", json.dumps({"plot": IMG1}), tool_call_id="c1")
    tool.name = "plot_stress"
    return [_Msg("ai", "", tool_calls=[{"id": "c1", "args": {"part": "B2"}}]), tool, _Msg("ai", "조회 요약: 그림 하나")]


def test_좌석_발언_블록에_판독문이_실리고_조회_예산을_먹지_않는다(monkeypatch):
    monkeypatch.setattr(d, "SEAT_IMAGE_READER", _Reader({IMG1: "응력 지도 — 최대 432 MPa"}))
    _k, calls, block, err = asyncio.run(d._free_gather_one(_Agent(_gather_msgs()), {"key": "구조", "role": "구조 해석"},
                                                           "질문", "맥락", 1))
    assert err == "" and [c[0] for c in calls] == ["plot_stress", "vision_transcribe"], "예산 1 — 판독문은 예산 밖이다"
    assert "vision_transcribe(" in block and "432 MPa" in block, block


def test_판독기가_없으면_좌석_블록은_종전과_같다(monkeypatch):
    monkeypatch.setattr(d, "SEAT_IMAGE_READER", None)
    _k, calls, block, _err = asyncio.run(d._free_gather_one(_Agent(_gather_msgs()), {"key": "구조", "role": "구조 해석"},
                                                            "질문", "맥락", 3))
    assert [c[0] for c in calls] == ["plot_stress"] and "vision_transcribe" not in block


def test_판독이_터져도_조회는_살고_셈에_남는다(monkeypatch):
    class _Boom(_Reader):
        async def read(self, urls, hint=""):
            raise RuntimeError("판독 서버 끊김")
    monkeypatch.setattr(d, "SEAT_IMAGE_READER", _Boom({}))
    acc = _acc()

    async def go():
        d._SEAT_IMG.set(acc)
        return await d._free_gather_one(_Agent(_gather_msgs()), {"key": "구조", "role": "구조 해석"}, "질문", "맥락", 3)
    _k, calls, block, err = asyncio.run(go())
    assert [c[0] for c in calls] == ["plot_stress"] and IMG1 in block and err == ""
    assert acc["err"] == 1


# ── 라운드 — 엔진 스트림을 실제로 돌린다 ─────────────────────────────────────────
def _run_stream(monkeypatch, reader):
    """좌석 둘이 1라운드에 같은 그림을 내는 조회를 하는 심의를 끝까지 돌린다. 좌석 조회 자리는 가짜지만 판독은 진짜 함수
    (_seat_image_transcripts)이고, 라운드 셈은 엔진의 좌석 태스크 감싸기(_gather_with_img_acc)를 그대로 지난다."""
    import langgraph.prebuilt

    async def _llm(_llm_obj, system, _human, *_img):
        return _SEAT_JSON if "전문가입니다" in system else "결정문 본문"

    async def _gather(_agent, persona, question, *_a, **_k):
        calls = [("plot_stress", '{"part": "B2"}', f"그림: {IMG1}", f"{persona['key']}-c1")]
        calls = await d._seat_image_transcripts(calls, question)
        return persona["key"], calls, "- 조회 요약", ""

    monkeypatch.setattr(d, "SEAT_IMAGE_READER", reader)
    monkeypatch.setattr(d, "_llm_text", _llm)
    monkeypatch.setattr(d, "_free_gather_one", _gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: object())
    return _stream(monkeypatch, {"rounds": 2, "save_report": 0, "rebut_quote": 0, "free_tools": 1, "persona_knowledge": 0},
                   tools={"agent_search": _Tool("agent_search")}, until=lambda ev, _data: ev == "done")


def test_라운드는_판독한_수를_알리고_판독문이_근거_카드가_된다(monkeypatch):
    events = _run_stream(monkeypatch, _Reader({IMG1: "응력 지도 — 최대 432 MPa"}))
    assert any(s.startswith("좌석 조회 그림 2장을 판독해 근거로 실었다") for s in _steps(events)), _steps(events)
    vt = [c for c in _cards(events, included=True) if c.get("tool") == "vision_transcribe"]
    assert len(vt) == 2 and all("432 MPa" in c["text"] for c in vt), vt
    assert {c.get("call") for c in vt} == {"mech-a-c1#img0", "rel-b-c1#img0"}, "짝 키로 상태 줄과 다시 붙는다"
    assert not [c for c in _cards(events, included=False) if c.get("source") == "좌석 조회 그림 판독 못 함"]


def test_판독할_모델이_없으면_라운드가_까닭과_함께_알린다(monkeypatch):
    events = _run_stream(monkeypatch, _Reader(None))
    steps = _steps(events)
    line = next((s for s in steps if s.startswith("좌석 조회 그림 중 판독하지 못한 것")), "")
    assert "판독할 모델이 없어" in line and "2장" in line and "주소로만" in line, steps
    miss = [c for c in _cards(events, included=False) if c.get("source") == "좌석 조회 그림 판독 못 함"]
    assert len(miss) == 1 and "1라운드" in miss[0]["text"], miss
    assert not [c for c in _cards(events, included=True) if c.get("tool") == "vision_transcribe"]
    assert not any(s.startswith("좌석 조회 그림 0장") for s in steps), "판독한 것이 없으면 그 줄은 없다"


# ── 판독기(에이전트 서버) ─────────────────────────────────────────────────────
class _VisionLLM:
    """그림 조각을 받는 판독 모델 대역 — 받은 사람 메시지의 조각 꼴을 적는다."""

    def __init__(self):
        self.seen = []

    async def ainvoke(self, msgs, **_kw):
        self.seen.append([p.get("type") for p in msgs[-1].content])
        return SimpleNamespace(content="빨간 사각형 하나")


def test_판독기는_진짜_판독_함수로_읽고_모델이_없으면_None(monkeypatch, tmp_path):
    (tmp_path / "red1.png").write_bytes(vision.red_png())
    monkeypatch.setattr(app, "ARTIFACT_DIR", str(tmp_path))
    llm = _VisionLLM()
    monkeypatch.setattr(app, "_transcriber", lambda _a: llm)
    rd = app._SeatImageReader(app.app)
    got = asyncio.run(rd.read(["/agent/artifacts/red1.png", "/agent/artifacts/none9.png"], "힌지"))
    assert got == {"/agent/artifacts/red1.png": "빨간 사각형 하나", "/agent/artifacts/none9.png": ""}, got
    assert llm.seen == [["text", "image_url"]], "판독 모델이 그림 조각을 받지 않았다"
    assert rd.limit == vision.TRANSCRIBE_MAX and rd.image_urls(f"보기 {IMG1}") == [IMG1]
    monkeypatch.setattr(app, "_transcriber", lambda _a: None)
    assert asyncio.run(rd.read(["/agent/artifacts/red1.png"])) is None


def test_판독_묶음은_한_번에_하나고_루프가_바뀌어도_돈다(monkeypatch):
    """20석이 한꺼번에 판독하면 챗 서버를 혼자 차지한다 — 묶음 하나씩. 시험은 asyncio.run 을 여러 번 돈다(루프마다 잠금)."""
    live = {"now": 0, "max": 0}

    async def _tall(_llm, pairs, *_a, **_k):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return [(u, w, "읽음") for u, w in pairs]
    monkeypatch.setattr(app, "_transcriber", lambda _a: object())
    monkeypatch.setattr(app._vision, "transcribe_all", _tall)
    rd = app._SeatImageReader(app.app)

    async def three():
        return await asyncio.gather(*(rd.read([f"/agent/artifacts/p{i}.png"]) for i in range(3)))
    assert all(asyncio.run(three())) and live["max"] == 1
    assert asyncio.run(rd.read(["/agent/artifacts/p9.png"])) == {"/agent/artifacts/p9.png": "읽음"}, "다른 루프에서도 돈다"


def test_판독기는_기동_때_엔진에_달린다():
    """MCP 심의 잡(delib_jobs)은 그림 준비(_with_delib_images)를 지나지 않는다 — 요청 길이 아니라 엔진 모듈에 달아야 덮인다. 다른 시험이
    엔진을 다시 읽으면(reload) 값이 비므로 원문으로 본다."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "_delib_engine.SEAT_IMAGE_READER = _SeatImageReader(app)" in src

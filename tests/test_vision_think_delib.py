# 띵킹 · 심의의 그림 — 좌석 모델이 그림을 받으면 그림 조각, 못 받으면 Qwen 판독문([g:N]) · 심의는 판독문을 원천 근거로(HWAXPortal docs/chat-depth-context T5)
"""사용자 결정(10-18, 질문 카드) — 띵킹 · 심의(GLM)에 그림을 넘길 때 탐침 뒤 자동: GLM 이 그림을 받으면 그림 그대로, 못 받으면 Qwen 이 그림마다
읽은 판독문을 근거로. 판독문은 그림 해시로 디스크에 남아 같은 그림을 다시 읽지 않는다.

  실행:  .venv/bin/python -m pytest tests/test_vision_think_delib.py -q
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation  # noqa: E402
import thinking as t  # noqa: E402
import vision as v  # noqa: E402

PNG = v.red_png(8)
HIST = [{"role": "user", "content": "이 응력 지도 봐", "images": ["/agent/artifacts/aa11.png"]},
        {"role": "assistant", "content": "최대 432 MPa", "evidence": [{"tool": "plot_stress", "result": "그림 /agent/artifacts/bb22.png"}]}]


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "aa11.png").write_bytes(PNG)
    (art / "bb22.png").write_bytes(PNG + b"x")           # 다른 해시
    monkeypatch.setattr(app, "ARTIFACT_DIR", str(art))
    saved = dict(app._ctx_cache)
    app._ctx_cache["n"] = 1_048_576
    v._probe_cache.clear()
    v._DU_CACHE.clear()
    yield art
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)


class _Transcriber:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, msgs, **_k):
        self.calls += 1
        return SimpleNamespace(content=f"판독: 최대 432 MPa (그림 {self.calls})")


def _think(monkeypatch, *, base_vision: str, transcriber=None):
    prompts = []

    class _Rec:
        async def ainvoke(self, args):
            return json.dumps({"agents": [{"agent_type": "mech-drop", "name": "낙하", "desc_match": 0.6}]})

    class _Search:
        async def ainvoke(self, args):
            return json.dumps({"hits": [{"title": "낙하 카드", "snippet": "응력"}]})

    class _Sess:
        async def ainvoke(self, _a):
            return json.dumps({"data": {"agent_type": "mech-drop", "description": "낙하 전문가"}})

    async def _tools(*_a, **_k):
        return {"recommend_agents": _Rec(), "agent_search": _Search(), "get_agent_session": _Sess()}

    async def _topic(h, q):
        return {"topic": "응력 지도의 최대 위치는 어디인가"}

    class _LLM:
        async def ainvoke(self, msgs, **_k):
            prompts.append(msgs[-1])
            return SimpleNamespace(content=json.dumps({"verdict": "answer", "scope": "낙하", "answer": "x", "basis": []}),
                                   response_metadata={})

    monkeypatch.setattr(t, "_tools_by_name", _tools)
    monkeypatch.setattr(t, "WITH_HISTORY", True)
    monkeypatch.setattr(app, "_derive_topic", _topic)
    monkeypatch.setenv("LLM_VISION", base_vision)
    monkeypatch.setattr(app, "_transcriber", lambda _a: transcriber)
    st = SimpleNamespace(llm=_LLM())

    async def go():
        return [c async for c in t.run_thinking(SimpleNamespace(state=st), "최대 위치는?", [], history=HIST)]
    out = b"".join(asyncio.run(go())).decode()
    return out, prompts


def test_좌석_모델이_그림을_받으면_그림_조각을_좌석마다_싣는다(monkeypatch):
    out, prompts = _think(monkeypatch, base_vision="1")
    p = prompts[0]
    assert hasattr(p, "content") and isinstance(p.content, list), type(p)
    assert sum(1 for x in p.content if isinstance(x, dict) and x.get("type") == "image_url") == 2
    assert "앞 대화의 그림 2장을 좌석마다 실었다" in out


def test_좌석_모델이_그림을_못_받으면_판독문을_세션에_싣는다(monkeypatch):
    tr = _Transcriber()
    out, prompts = _think(monkeypatch, base_vision="0", transcriber=tr)
    p = prompts[0]
    text = p[1] if isinstance(p, tuple) else p.content
    assert isinstance(text, str), "그림 조각은 보내지 않는다"
    assert "[g:1] (1번째 발화에 사람이 올린 그림(1턴 전)) 판독: 최대 432 MPa" in text and "[g:2]" in text
    assert "판독문 2장을 좌석마다 실었다" in out and tr.calls == 2


def test_판독할_모델도_없으면_싣지_못했다고_말한다(monkeypatch):
    out, prompts = _think(monkeypatch, base_vision="0", transcriber=None)
    assert "그림을 읽지 못해 싣지 못했다" in out


def test_판독문은_그림_해시로_남아_다시_읽지_않는다(_iso):
    tr = _Transcriber()
    a1 = asyncio.run(v.transcribe(tr, PNG, "image/png", str(_iso)))
    a2 = asyncio.run(v.transcribe(tr, PNG, "image/png", str(_iso)))
    assert a1 == a2 and tr.calls == 1 and (_iso / ".transcripts").is_dir()


def _delib(monkeypatch, *, base_vision: str, evidence=None, extra=None):
    tr = _Transcriber()
    monkeypatch.setattr(app, "_transcriber", lambda _a: tr)
    monkeypatch.setenv("LLM_VISION", base_vision)
    req = app.ChatRequest(message="/심의 최대 위치", groups=[], history=HIST,
                          delib_opts={"evidence": evidence or [], **(extra or {})})
    seen = {}

    async def engine(opts):
        seen["opts"] = opts
        seen["imgs"] = deliberation.DELIB_IMAGES.get()

        async def seat():                       # 좌석은 제 태스크로 돈다 — 문맥 변수를 물려받는지
            return deliberation.DELIB_IMAGES.get()
        seen["seat"] = await asyncio.ensure_future(seat())
        yield b"event: done\ndata: {}\n\n"

    async def go():
        out = [c async for c in app._with_delib_images(req, "최대 위치", engine)]
        seen["after"] = deliberation.DELIB_IMAGES.get()
        return out
    return b"".join(asyncio.run(go())).decode(), seen, tr


def test_심의는_앞_대화와_근거의_그림을_판독해_원천_근거로_싣는다(monkeypatch):
    out, seen, tr = _delib(monkeypatch, base_vision="0",
                           evidence=[{"tool": "render_cad", "result": "보기 /agent/artifacts/aa11.png"}])
    ev = seen["opts"]["evidence"]
    vis = [e for e in ev if e.get("tool") == "vision_transcribe"]
    assert len(vis) == 2 and all(e["result"].startswith("판독: 최대 432 MPa") for e in vis), ev
    assert vis[0]["source"].startswith("그림 판독 · 1번째 발화에 사람이 올린 그림")
    assert ev[0]["tool"] == "render_cad", "넘긴 근거는 그대로 앞에 둔다"
    assert "그림 판독문 2장을 심의 근거로 실었다" in out
    assert seen["imgs"] is None and seen["seat"] is None, "심의 LLM 이 그림을 못 받으면 그림 조각은 싣지 않는다"


def test_심의_LLM_이_그림을_받으면_좌석_의장_호출마다_그림도_싣는다(monkeypatch):
    out, seen, _tr = _delib(monkeypatch, base_vision="1")
    assert len(seen["imgs"] or []) == 2 and seen["seat"] == seen["imgs"], "좌석 태스크가 그림 조각을 물려받는다"
    assert "그림 2장을 좌석 · 의장 호출마다 싣는다" in out
    assert any(e.get("tool") == "vision_transcribe" for e in seen["opts"]["evidence"]), "판독문도 근거로 — 짚을 번호가 있게"
    assert seen["after"] is None, "심의가 끝나면 문맥 변수를 되돌린다"


def test_외부_응답자_심의는_그림_조각을_싣지_않고_판독문만_근거로_간다고_말한다(monkeypatch):
    """외부 응답자(llm=external)의 과제는 글뿐이다 — 심의 LLM 이 그림을 받아도 그림 조각을 문맥에 세우지 않고, 상태 줄이 그렇다고
    말한다(종전엔 '그림 N장을 싣는다' 고 말하고 조각은 과제에서 말없이 빠졌다). 판독문은 근거로 간다."""
    out, seen, _tr = _delib(monkeypatch, base_vision="1", extra={"llm": "external", "consent_external": True})
    assert seen["imgs"] is None and seen["seat"] is None
    assert "외부 응답자 심의다 — 과제가 글뿐이라 그림 2장 자체는 싣지 않는다(판독문은 근거로 간다)" in out
    assert "좌석 · 의장 호출마다 싣는다" not in out
    assert any(e.get("tool") == "vision_transcribe" for e in seen["opts"]["evidence"])


def test_그림이_있으면_사람_메시지를_그림_먼저_글_뒤로_보낸다():
    got = {}

    class _LLM:
        async def ainvoke(self, msgs, **_k):
            got["m"] = msgs
            return SimpleNamespace(content="ok", response_metadata={})
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
    assert asyncio.run(deliberation._llm_text(_LLM(), "sys", "글", [part])) == "ok"
    assert got["m"][1].content == [part, {"type": "text", "text": "글"}], "그림이 앞 — 같은 시스템 글의 호출들이 앞부분을 공유한다"
    asyncio.run(deliberation._llm_text(_LLM(), "sys", "글"))
    assert got["m"] == [("system", "sys"), ("human", "글")], "그림이 없으면 종전 모양 그대로"


def test_시뮬심의_시험계획도_그림을_거친다():
    import inspect
    src = inspect.getsource(app.chat)
    assert src.count("_with_delib_images(req, _q, lambda o:") == 2
    assert "_with_delib_images(req, q, lambda o: run_deliberation(" in inspect.getsource(app._delib_with_topic)


def test_좌석_발언은_문맥의_그림을_싣고_없으면_종전_모양이다():
    """_persona_round — 심의 좌석의 한 라운드. DELIB_IMAGES 가 있으면 사람 메시지에 그림 조각, 없으면 (system, human) 튜플 그대로."""
    got = []

    class _LLM:
        async def ainvoke(self, msgs, **_k):
            got.append(msgs)
            return SimpleNamespace(content='{"say": "근거로 판단", "position": "x"}', response_metadata={})
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
    persona = {"key": "mech-drop", "role": "낙하"}

    async def go(with_img: bool):
        tok = deliberation.DELIB_IMAGES.set([part]) if with_img else None
        try:
            return await deliberation._persona_round(_LLM(), persona, "발언하라", required=("say",))
        finally:
            if tok is not None:
                deliberation.DELIB_IMAGES.reset(tok)
    asyncio.run(go(True))
    assert got[-1][1].content[0] == part, "좌석 호출에 그림이 실렸다"
    asyncio.run(go(False))
    assert got[-1][1] == ("human", "발언하라"), "그림이 없으면 종전 모양"
    import inspect
    src = inspect.getsource(deliberation)
    assert src.count("_llm_text(llm, chair_sys, chair_human, *_img_arg())") == 2, "의장 호출(한 번 · best-of 후보)도 그림을 싣는다"


def test_앞_대화_그림_출처에_몇_턴_전인지_적는다():
    # '방금 올린 것' 을 물을 때 오래전 그림이 방금 것처럼 읽히지 않게(HWAXPortal docs/chat-ppt PP-D5)
    h = [{"role": "user", "content": "첫 그림", "images": ["/agent/artifacts/aa11.png"]},
         {"role": "assistant", "content": "봤다 /agent/artifacts/cc33.png"},
         {"role": "user", "content": "다음"},
         {"role": "assistant", "content": "답", "evidence": [{"tool": "plot_stress", "result": "/agent/artifacts/bb22.png"}]}]
    got = dict(v.history_images(h))
    assert got["/agent/artifacts/aa11.png"] == "1번째 발화에 사람이 올린 그림(2턴 전)"
    assert got["/agent/artifacts/cc33.png"] == "2번째 답에 실린 그림(2턴 전)", "답의 그림은 그 턴의 물음까지 센다"
    assert got["/agent/artifacts/bb22.png"] == "4번째 답 앞에 도구 plot_stress 가 낸 그림(1턴 전)"


def test_띵킹_세션_맥락은_업로드_근거를_사람이_올린_것으로_적는다():
    h = [{"role": "assistant", "content": "PPT 를 변환했습니다",
          "evidence": [{"tool": "convert_file", "args": '{"file": "a.pptx"}', "result": "초안", "kind": "upload"},
                       {"tool": "lookup", "result": "값"}]}]
    text, _st = t._session_block(h, 20000)
    assert '[e:1] [사람이 올려 보낸 파일 · convert_file({"file": "a.pptx"})] 초안' in text
    assert "[e:2] [lookup] 값" in text

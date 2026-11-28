# 판독 · 탐침의 생각 끄기를 끝점마다(HWAXPortal docs/request-1125 IR-51) — 기본은 지금대로, 끄기를 싣지 않는 끝점은 생각을 걷는다
#
#   실행:  .venv/bin/python -m pytest tests/test_vision_thinking_off.py -q
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vision  # noqa: E402


def test_IR51_고르기():
    assert vision.thinking_off("http://stcx:8000/v1", "qwen3", "all") is True
    assert vision.thinking_off("http://glm:9000/v1", "glm-5.3", "none") is False
    assert vision.thinking_off("http://stcx:8000/v1", "qwen3", "qwen,stcx") is True
    assert vision.thinking_off("http://glm:9000/v1", "glm-5.3", "qwen,stcx") is False
    assert vision.THINKING_OFF == "all" or vision.THINKING_OFF, "기본값"


class _LLM:
    def __init__(self, base, model, content):
        self.openai_api_base, self.model_name, self.content, self.kw = base, model, content, []

    async def ainvoke(self, msgs, **kw):
        self.kw.append(kw)
        return SimpleNamespace(content=self.content)


def test_IR51_끄기를_싣지_않는_끝점은_그냥_부르고_붙어_온_생각을_걷는다(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "THINKING_OFF", "qwen")
    glm = _LLM("http://glm:9000/v1", "glm-5.3", "Okay, the user wants me to read the figure...</think>그래프 — 응력 312 MPa")
    out = asyncio.run(vision.transcribe(glm, vision.red_png(), "image/png", str(tmp_path)))
    assert glm.kw == [{}] and out == "그래프 — 응력 312 MPa"
    qwen = _LLM("http://stcx:8000/v1", "qwen3", "표 — 3행")
    out = asyncio.run(vision.transcribe(qwen, vision.red_png(8), "image/png", str(tmp_path)))
    assert qwen.kw == [{"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}] and out == "표 — 3행"


def test_IR51_기본은_지금대로_모든_끝점에_끄기를_싣는다(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "THINKING_OFF", "all")
    glm = _LLM("http://glm:9000/v1", "glm-5.3", "a</think>b")
    out = asyncio.run(vision.transcribe(glm, vision.red_png(4), "image/png", str(tmp_path)))
    assert glm.kw == [{"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}] and out == "a</think>b", "글은 종전 그대로"

# 단면 PNG + SVG 짝(HWAXPortal docs/report-figures RF-40) — 챗 기본값은 그 판이 값을 알 때만 · 짝은 같은 줄기 · 모델은 SVG 를 답에 · 판독과 보고서는 PNG
"""왜 — 사용자가 챗의 단면을 기본으로 호버되는 SVG 로 보이게 하자고 했다(11-13). 모델은 SVG 를 못 읽고(판독은 래스터만) 보고서 · 심의도 래스터라
StepForge 가 format="png+svg_live" 로 짝을 내면 챗 화면은 SVG, 판독 · 보고서는 PNG 를 쓴다. 옛 판은 그 값을 몰라 실패하므로 챗 기본값은 그 도구의
인자 설명에 값이 적혀 있을 때만 채운다."""
import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import app as a  # noqa: E402
import report_figures as RF  # noqa: E402
import vision  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2
PNG2 = b"\x89PNG\r\n\x1a\n" + bytes(reversed(range(256))) * 2
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><g class="part"><title>PCB</title></g>' + b"<!-- pad -->" * 30 + b"</svg>"
SVG2 = b'<svg xmlns="http://www.w3.org/2000/svg"><g class="part"><title>FRAME</title></g>' + b"<!-- pad -->" * 30 + b"</svg>"


def _img(raw: bytes, mime: str) -> dict:
    return {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": mime}


def _tool(name: str, knows: bool, items: list, seen: list):
    desc = "png | jpg · svg_live" + (" · png+svg_live(PNG 와 상호작용 SVG 짝)" if knows else "")
    schema = {"type": "object", "properties": {"project_id": {"type": "string"},
                                               "format": {"type": "string", "description": desc}}}

    async def inner(**kw):
        seen.append(kw)
        return items
    t = StructuredTool(name=name, description="단면 도구 대역", args_schema=schema, coroutine=inner)
    return a._prep_tool(t, arg_defaults=a._CHAT_ARG_DEFAULTS, chat=True)


def _call(tool, args: dict) -> str:
    out = asyncio.run(tool.ainvoke(args))
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


def test_챗_기본값은_그_판이_값을_알_때만_채운다(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "ARTIFACT_DIR", str(tmp_path))
    seen: list = []
    _call(_tool("section_view", True, [{"type": "text", "text": "단면"}], seen), {"project_id": "p1"})
    assert seen[-1].get("format") == "png+svg_live", "새 판은 짝을 청한다"
    _call(_tool("section_view", False, [{"type": "text", "text": "단면"}], seen), {"project_id": "p1"})
    assert "format" not in seen[-1], "옛 판은 모르는 값이라 채우지 않는다(실패한다)"
    _call(_tool("heaxstep_forge_section_batch", True, [{"type": "text", "text": "묶음"}], seen), {"project_id": "p1", "format": "png"})
    assert seen[-1]["format"] == "png", "모델이 정한 값은 그대로다(접두 별칭도 같은 표)"


def test_짝은_같은_줄기로_두고_모델에게는_SVG_를_답에_PNG_는_판독용으로(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "ARTIFACT_DIR", str(tmp_path))
    items = [{"type": "text", "text": "두 장"}, _img(PNG, "image/png"), _img(SVG, "image/svg+xml"),
             _img(PNG2, "image/png"), _img(SVG2, "image/svg+xml")]
    out = _call(_tool("section_pair_view", True, items, []), {"project_id": "p1"})
    s1, s2 = hashlib.sha256(PNG).hexdigest()[:20], hashlib.sha256(PNG2).hexdigest()[:20]
    assert (tmp_path / f"{s1}.svg").read_bytes() == SVG and (tmp_path / f"{s2}.svg").read_bytes() == SVG2, "i 번째 SVG 는 i 번째 PNG 의 줄기"
    assert f"답에 넣을 것: /agent/artifacts/{s1}.svg" in out and f"판독용(답에 넣지 않음): /agent/artifacts/{s1}.png" in out
    assert f"답에 넣을 것: /agent/artifacts/{s2}.svg" in out
    assert vision.image_urls(out) == [f"/agent/artifacts/{s1}.png", f"/agent/artifacts/{s2}.png"], "판독은 PNG 만 집는다"


def test_단면_도구가_아니면_짝으로_묶지_않는다(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "ARTIFACT_DIR", str(tmp_path))
    items = [{"type": "text", "text": "도표"}, _img(PNG, "image/png"), _img(SVG, "image/svg+xml")]
    out = _call(_tool("draw_chart", True, items, []), {})
    assert (tmp_path / (hashlib.sha256(SVG).hexdigest()[:20] + ".svg")).exists(), "SVG 는 제 내용 이름이다"
    assert "답에 넣을 것" not in out and "[생성된 이미지 URL — 답변에 ![설명](url) 로 포함하세요]" in out


def test_한쪽만_오면_종전대로다(monkeypatch, tmp_path):
    monkeypatch.setattr(a, "ARTIFACT_DIR", str(tmp_path))
    out = _call(_tool("section_view", True, [{"type": "text", "text": "한 장"}, _img(PNG, "image/png")], []), {"project_id": "p1"})
    assert "답에 넣을 것" not in out and f"/agent/artifacts/{hashlib.sha256(PNG).hexdigest()[:20]}.png" in out


def test_보고서는_SVG_자리에_짝_PNG_를_올린다(monkeypatch, tmp_path):
    stem = hashlib.sha256(PNG).hexdigest()[:20]
    (tmp_path / f"{stem}.png").write_bytes(PNG)
    (tmp_path / f"{stem}.svg").write_bytes(SVG)
    got = []

    def portal(req: httpx.Request) -> httpx.Response:
        body = req.content
        got.append(body)
        return httpx.Response(200, json={"ok": True, "file_id": f"f{len(got)}"})
    monkeypatch.setattr(RF, "PORTAL_TRANSPORT", httpx.MockTransport(portal))
    blocks = {"results": [f"단면이다.\n![PCB 단면](/agent/artifacts/{stem}.svg)\n끝."]}
    out = asyncio.run(RF.attach(blocks, user_pat="pat-kim", artifact_dir=str(tmp_path)))
    assert len(got) == 1 and f'"filename": "{stem}.png"'.encode() in got[0] or f'"filename":"{stem}.png"'.encode() in got[0]
    assert PNG in got[0] and SVG not in got[0], "보고서에는 래스터를 올린다"
    assert out.blocks["results"] == ["단면이다.\n[그림 1 — PCB 단면]\n끝."]


def test_앞_대화의_답에_SVG_만_있어도_판독은_짝_PNG_를_본다():
    stem = hashlib.sha256(PNG).hexdigest()[:20]
    hist = [{"role": "user", "content": "단면 보여 줘"},
            {"role": "assistant", "content": f"PCB 단면이다.\n![PCB 단면](/agent/artifacts/{stem}.svg)"},
            {"role": "user", "content": "간극이 어디 좁아?"}]
    got = [u for u, _why in vision.history_images(hist)]
    assert got == [f"/agent/artifacts/{stem}.png"], "답에 실린 호버 SVG 의 짝 PNG 를 판독 후보로"
    assert vision.svg_pair_urls("/agent/artifacts/../x.svg") == [], "경로를 벗어나는 이름은 받지 않는다"


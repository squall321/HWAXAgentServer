# 도구 그림 저장은 내용으로 꼴을 보고 크기를 잰다 — 래스터(vision.sniff) · SVG(<svg 머리)만, 상한(VISION_MAX_BYTES)을 넘거나 꼴이 아니면 저장하지 않는다(포털 docs/report-figures RF-36)
"""전에는 선언된 mime 이 image/* 면 무엇이든 그 확장자로 저장했다 — 그림이 아닌 것도 · 아주 큰 것도. 포털 챗이 같은 출처 SVG 를 격리 iframe 으로 그리게 되며
(사용자 결정 11-05) 저장하는 쪽도 내용을 본다. 사용자가 올린 그림의 길(_stash_uploads)은 이미 내용으로 보고 있었다."""
import base64

import pytest

import app as a
import vision as v

SVG = (b'<?xml version="1.0" encoding="UTF-8"?>\n<!-- StepForge section -->\n'
       b'<svg xmlns="http://www.w3.org/2000/svg" class="stepforge-section" width="760" height="400" viewBox="0 0 760 400">'
       + b'<g class="part" data-path="/A/B"><path d="M0 0L10 10"/></g>' * 12 + b"</svg>")


@pytest.fixture(autouse=True)
def art(monkeypatch, tmp_path):
    d = tmp_path / "artifacts"
    d.mkdir()
    monkeypatch.setattr(a, "ARTIFACT_DIR", str(d))
    return d


def _item(raw: bytes, mime: str) -> dict:
    return {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": mime}


def test_svg_는_svg_로_저장한다(art):
    (u,) = a._stash_image_items([_item(SVG, "image/svg+xml")])
    assert u.startswith("/agent/artifacts/") and u.endswith(".svg"), u
    assert (art / u.rsplit("/", 1)[1]).read_bytes() == SVG


def test_svg_머리는_BOM_공백_DOCTYPE_을_지나_본다(art):
    raw = b"\xef\xbb\xbf  \n<!DOCTYPE svg PUBLIC \"-//W3C//DTD SVG 1.1//EN\" \"x\">\n" + SVG[SVG.index(b"<svg"):]
    (u,) = a._stash_image_items([_item(raw, "image/svg+xml")])
    assert u.endswith(".svg"), u


def test_선언과_내용이_다르면_내용을_따른다(art):
    png = v.red_png(8) + b"\0" * 300
    (u,) = a._stash_image_items([_item(png, "image/svg+xml")])
    assert u.endswith(".png"), "확장자는 내용으로 — 선언된 mime 이 svg 라도 PNG 바이트면 png"


def test_그림이_아니면_저장하지_않는다(art):
    html = b"<html><body>" + b"x" * 400 + b"</body></html>"
    assert a._stash_image_items([_item(html, "image/svg+xml"), _item(b"\0" * 400, "image/png")]) == []
    assert list(art.iterdir()) == [], "꼴을 모르는 것은 파일로 남기지 않는다"


def test_상한을_넘으면_저장하지_않는다(monkeypatch, art):
    monkeypatch.setattr(v, "MAX_BYTES", len(SVG) - 1)
    assert a._stash_image_items([_item(SVG, "image/svg+xml")]) == []
    monkeypatch.setattr(v, "MAX_BYTES", len(SVG))
    assert len(a._stash_image_items([_item(SVG, "image/svg+xml")])) == 1, "상한과 같으면 받는다"

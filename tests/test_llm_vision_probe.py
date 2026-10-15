# 그림 입력 탐침(scripts/llm_vision_probe.py)이 content 배열에 text·image_url 을 정확히 싣고 서버의 거절·응답을 바르게 판정하는지
import base64
import importlib.util
import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("llm_vision_probe", ROOT / "scripts" / "llm_vision_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_빨간_PNG_는_올바른_PNG_다():
    png = _load().red_png(32)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h, depth, ctype = struct.unpack(">IIBB", png[16:26])
    assert (w, h, depth, ctype) == (32, 32, 8, 2)
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41:41 + idat_len])
    assert raw[:4] == b"\x00\xff\x00\x00" and len(raw) == 32 * (1 + 32 * 3)


@pytest.fixture()
def fake_llm():
    seen: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401 — 시험 출력 조용히
            return

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
            seen["body"], seen["auth"] = body, self.headers.get("Authorization")
            if seen.get("reject"):
                out, code = {"error": {"message": "x is not a multimodal model"}}, 400
            else:
                out, code = {"choices": [{"message": {"content": "빨강"}}]}, 200
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    seen["base"] = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    yield seen
    srv.shutdown()


def test_요청은_content_배열에_text_와_image_url_을_싣는다(fake_llm, monkeypatch, capsys):
    monkeypatch.setenv("VLLM_BASE_URL", fake_llm["base"])
    monkeypatch.setenv("VLLM_MODEL", "glm-test")
    monkeypatch.setenv("VLLM_API_KEY", "k-secret")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    assert _load().main() == 0
    msg = fake_llm["body"]["messages"][0]
    assert fake_llm["body"]["model"] == "glm-test" and msg["role"] == "user"
    parts = msg["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"], "그림은 content 배열 안의 image_url 이어야 한다"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"
    out = capsys.readouterr().out
    assert "그림을 읽는다" in out and "k-secret" not in out, "판정을 찍고 키는 찍지 않는다"


def test_서버가_그림을_거절하면_그렇다고_판정한다(fake_llm, monkeypatch, capsys):
    fake_llm["reject"] = True
    monkeypatch.setenv("VLLM_BASE_URL", fake_llm["base"])
    monkeypatch.setenv("VLLM_MODEL", "glm-test")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    assert _load().main() == 1
    assert "HTTP 400" in capsys.readouterr().out

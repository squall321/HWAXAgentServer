# 도구 의미 순서가 AIDataHub 임베딩 공간이 바뀌면 도구 벡터를 다시 짓는가 — 옛 공간 벡터와 견주지 않는다(포털 docs/embedder-bge-m3)
"""왜 — 도구 벡터 캐시는 도구 이름만 보고 다시 짓지 않았다. AIDataHub 임베더가 바뀌면(e5-base 768 → BGE-M3 1024) 새 질의 벡터를 옛 공간의
도구 벡터와 견주는데, zip 이 차원을 말없이 잘라 엉뚱한 순서가 오류 없이 나온다. 응답의 (모델, 차원)을 공간으로 보고, 다르면 다시 짓는다.

망은 이 시험이 루프백 빈 포트에 띄운 가짜 /api/embed 뿐이다.
"""
import http.server
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app as a  # noqa: E402


class _Embed(http.server.BaseHTTPRequestHandler):
    """글에 든 낱말로 한 축을 고르는 가짜 임베더 — 공간(모델 · 차원 · 낱말→축)은 state 가 정한다."""
    state: dict = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        st = type(self).state
        vecs = []
        for t in body["texts"]:
            v = [0.0] * st["dim"]
            for word, axis in st["axes"].items():
                if word in t:
                    v[axis] = 1.0
            vecs.append(v)
        raw = json.dumps({"vectors": vecs, "model": st["model"], "dim": st["dim"]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def embed_server(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Embed)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(a, "AIDH_HTTP", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(a, "_TOOL_VEC", {"sig": "", "names": [], "vecs": [], "space": None})
    monkeypatch.setattr(a, "_EMBED_SPACE", {"space": None})
    yield _Embed
    srv.shutdown()
    srv.server_close()


def test_임베딩_공간이_바뀌면_도구_벡터를_다시_짓는다(embed_server):
    tools = [SimpleNamespace(name="a_beta", description="beta 일"), SimpleNamespace(name="z_alpha", description="alpha 일")]
    embed_server.state = {"model": "e5-base", "dim": 4, "axes": {"alpha": 0, "beta": 1}}
    assert a._semantic_order("alpha 를 찾는다", tools) == ["z_alpha", "a_beta"]
    assert a._TOOL_VEC["space"] == ("e5-base", 4)
    # 새 모델 — 차원이 늘고 낱말이 다른 축에 놓인다. 옛 4차원 도구 벡터와 견주면 질의의 5번 축이 잘려 0 이 되고, 동점이라 이름의
    # 역순(z_alpha)이 앞에 온다 — 그것이 '오류 없는 엉뚱한 순서' 다.
    embed_server.state = {"model": "BAAI/bge-m3", "dim": 6, "axes": {"alpha": 4, "beta": 5}}
    assert a._semantic_order("beta 를 찾는다", tools) == ["a_beta", "z_alpha"]
    assert a._TOOL_VEC["space"] == ("BAAI/bge-m3", 6) and len(a._TOOL_VEC["vecs"][0]) == 6


def test_같은_차원의_다른_모델도_다시_짓는다(embed_server):
    tools = [SimpleNamespace(name="a_beta", description="beta 일"), SimpleNamespace(name="z_alpha", description="alpha 일")]
    embed_server.state = {"model": "m-1", "dim": 4, "axes": {"alpha": 0, "beta": 1}}
    a._semantic_order("alpha", tools)
    embed_server.state = {"model": "m-2", "dim": 4, "axes": {"alpha": 1, "beta": 0}}     # 같은 차원, 축이 바뀌었다
    assert a._semantic_order("beta", tools) == ["a_beta", "z_alpha"]
    assert a._TOOL_VEC["space"] == ("m-2", 4)

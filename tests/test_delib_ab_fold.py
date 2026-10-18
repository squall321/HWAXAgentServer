# 심의 A/B 러너(tools/delib_ab.py)가 결정문 · 좌석 · 라운드를 실제 SSE 꼴에서 잡는지 — 결정문 지표가 양쪽 다 0 이던 결함(10-18)
"""왜 — 러너는 `data:` 줄만 보고 결정문을 `type == "result"` 로 찾았는데, 엔진의 결정문은 `event: result` 줄 아래 data
`{"type":"text", …}` 와 `event: delib` 의 kind=decision 이다. 그래서 결정문을 한 번도 못 잡았다(T1 근거 선주입을 cae00 에서 재기 전에 드러남 —
HWAXPortal docs/followups-1017 결정 11). 기본 주소도 :8000(dev vLLM 자리)이었고, 사내 프록시 환경값이 걸린 셸에서는 루프백도 프록시로 갔다.

  실행:  .venv/bin/python -m pytest tests/test_delib_ab_fold.py -q
"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deliberation import _delib, _sse  # noqa: E402
from tools import delib_ab  # noqa: E402

FRAMES = [
    _sse("status", {"step": "심의 시작", "tool": None}),
    _delib("personas", personas=[{"key": "mech-drop", "role": "낙하"}, {"key": "thermal", "role": "열"}]),
    _delib("turn", persona="mech-drop", round=1, say="보스 B2 응력 432 MPa"),
    _delib("turn", persona="thermal", round=1, say="온도 영향 작다"),
    _delib("turn", persona="mech-drop", round=2, say="리브 추가"),
    _delib("decision", text="결정 — 보스 B2 를 보강한다 [e:1]"),
    _sse("result", {"type": "text", "content": "(머리) 결정 — 보스 B2 를 보강한다 [e:1]\n(보고서 안내)"}),
    _sse("done", {}),
]


def _lines(frames):
    out = []
    for f in frames:
        out += [ln + "\n" for ln in f.decode().split("\n")]
    return out


def test_결정문은_delib_decision_을_먼저_없으면_result_를():
    got = delib_ab.fold_sse(_lines(FRAMES))
    assert got["personas"] == ["mech-drop", "thermal"]
    assert [len(r) for r in got["rounds"]] == [2, 1] and got["n_ops"] == 3
    assert got["decision"] == "결정 — 보스 B2 를 보강한다 [e:1]", "머리 · 안내가 섞이지 않은 결정문"
    no_dec = [f for f in FRAMES if b'"kind": "decision"' not in f and b'"kind":"decision"' not in f]
    assert delib_ab.fold_sse(_lines(no_dec))["decision"].startswith("(머리) 결정"), "kind=decision 이 없으면 result 의 content"


def test_기본_주소는_에이전트_서버고_프록시를_타지_않는다(monkeypatch):
    assert delib_ab.DEFAULT_URL.endswith(":9009/chat"), "에이전트 서버 포트(:8000 은 dev vLLM 자리)"

    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for f in FRAMES:
                self.wfile.write(f)

        def log_message(self, *_a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            monkeypatch.setenv(k, "http://127.0.0.1:9")          # 닫힌 프록시 — 타면 연결 거부로 실패한다
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        url = f"http://127.0.0.1:{srv.server_port}/chat"
        doc = asyncio.run(delib_ab.run_one(url, "보스 보강?", {"evidence_prepass": 1}, 30))
        asyncio.run(delib_ab.run_condition(url, ["보스 보강?"], "evidence_prepass", 1, 1, 30, None))
        asyncio.run(delib_ab.run_condition(url, ["보스 보강?"], "evidence_prepass", 1, 1, 30, None, save_report=True))
    finally:
        srv.shutdown()
    assert seen[1]["delib_opts"] == {"evidence_prepass": 1, "save_report": 0}, "측정용 심의는 RA 보고서를 남기지 않는다"
    assert seen[2]["delib_opts"] == {"evidence_prepass": 1}, "--save-report 면 엔진 기본(남긴다)"
    assert doc["decision"].startswith("결정 — 보스 B2") and doc["personas"] == ["mech-drop", "thermal"] and doc["n_ops"] == 3

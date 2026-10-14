# 심의를 누가 띄웠나(delib_opts.started_by — human | app)를 엔진이 기록하고, 카드 대조 관문의 절반 판정만 사람이 띄운 심의로 세는지 본다
#
# M3.md #26 — 리스크 앱 러너가 편성한 패널(쟁점·옛 흐름 coverage)은 started_by:"app" 을 싣는다. 관문의 '심의가 돌면 절반(4)' 은
# started_by ≠ app 인 도는 심의(분리 태스크 + MCP 잡)만 센다. delib_active·delib_queued(재기동 미룸 판정)는 그대로 전부 센다.
# 표지는 스스로 밝히는 값이라 검증하지 않는다 — 없거나 모르는 값은 사람이다(보수 쪽이 절반이다).
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_started_by.py -q
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402


@pytest.mark.parametrize("v,want", [("app", "app"), ("APP ", "app"), ("human", "human"), (None, "human"), ("", "human"),
                                    ("robot", "human"), (1, "human")])
def test_started_by_는_app_만_app_이고_나머지는_사람이다(v, want):
    assert d._started_by(v) == want
    assert d._resolve_opts({"started_by": v}).started_by == want
    assert d._resolve_opts(None).started_by == "human"


def test_MCP_잡은_started_by_를_기록하고_사람_잡만_센다(monkeypatch):
    monkeypatch.setattr(delib_jobs, "_JOBS", {
        "a": {"status": "running", "started_by": "human"}, "b": {"status": "running", "started_by": "app"},
        "c": {"status": "running"}, "d": {"status": "done", "started_by": "human"}})
    assert delib_jobs.running_count() == 3 and delib_jobs.running_human_count() == 2


def test_분리_태스크는_누가_띄웠는지에_따라_사람_심의로_센다(monkeypatch):
    """웹 /심의 경로(_detach_stream) — 리스크 앱 패널(app)은 delib_active 에는 들지만 _delib_human 에는 들지 않는다."""
    monkeypatch.setattr(delib_jobs, "_JOBS", {})

    async def go():
        gate = asyncio.Event()

        async def gen():
            await gate.wait()
            yield b"x"

        a._detach_stream(gen(), "t-app", "app")
        a._detach_stream(gen(), "t-human")
        await asyncio.sleep(0)
        seen = (a._delib_load()[0], a._delib_human())
        gate.set()
        await asyncio.sleep(0.05)
        return seen, (a._delib_load()[0], a._delib_human(), len(a._DETACHED_BY))

    during, after = asyncio.run(go())
    assert during == (2, 1) and after == (0, 0, 0)


def test_웹_심의는_delib_opts_의_started_by_를_분리_태스크에_넘긴다(monkeypatch):
    seen = []

    def capture(stream, label, started_by="human"):
        seen.append((label, started_by))

        async def empty():
            if False:
                yield b""

        return empty()

    monkeypatch.setattr(a, "_detach_stream", capture)
    for opts, want in (({"started_by": "app"}, "app"), ({}, "human"), (None, "human")):
        asyncio.run(a.chat(a.ChatRequest(message="/심의 화두", delib_opts=opts)))
        assert seen[-1] == ("delib", want), opts

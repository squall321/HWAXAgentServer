# 다단 심의(/시뮬심의 · 구축 계획) 스트림이 done 을 맨 끝에 한 번만 내는지 — 단마다 엔진 스트림이 내던 done 을 걷고 단 경계 표지 · 나머지 사건은 그대로 흘리는지, 앞에서 끝나는 길도 오류 뒤 done 하나로 맺는지 본다
#
# 왜 생겼나. run_sim_deliberation 은 단마다 엔진 스트림(_deliberation_stream)을 걸러 내지 않고 흘렸고, 단 스트림은 저마다 done 으로 끝난다. 웹 챗은
# 첫 done 을 턴의 끝으로 읽어 2단부터 멈춤(■)이 사라지고 입력창이 풀렸으며 마지막 done 은 버려졌다(포털 헤드리스 하네스 확인, 사용자 결정 10-31).
# MCP 잡 길은 스트림을 끝까지 읽고 done 을 원장에 싣지 않아 그대로였다. 이제 단의 done 은 걷고 맨 끝에 하나만 낸다.
#
# 하네스는 tests/test_ha09_s4_stages.py 의 가짜(w4 — 바깥 LLM · 게이트웨이 도구 · 좌석 자유 조회만 가짜)다. 웹 길 그대로 run_sim_deliberation 을
# 끝까지 읽는다.
#
#   실행:  .venv/bin/python -m pytest tests/test_sim_stream_one_done.py -q
import asyncio
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
from test_delib_job_queue import ME  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import GROUPS, OPTS, Q, STUB, _kind, world  # noqa: E402, F401 — world 는 픽스처
from test_ha09_s4_stages import w4  # noqa: E402, F401 — 픽스처

_DONE = d._sse("done", {})


def _web(opts: dict, stages: list | None = None) -> list:
    """웹 길 그대로 끝까지 읽은 사건(바이트). stages 를 주면 단 스트림(_deliberation_stream)마다 그 단이 낸 사건을 모은다."""
    if stages is not None:
        real = d._deliberation_stream

        async def spy(*a, **k):
            got: list = []
            stages.append(got)
            async for c in real(*a, **k):
                got.append(c)
                yield c
        d._deliberation_stream = spy

    async def go():
        return [c async for c in d.run_sim_deliberation(STUB, Q, list(GROUPS), copy.deepcopy(opts), ME)]
    try:
        return asyncio.run(go())
    finally:
        if stages is not None:
            d._deliberation_stream = real


def _frames(chunks: list) -> list:
    return [delib_jobs._parse_sse(c) for c in chunks]


def _subseq(small: list, big: list) -> bool:
    it = iter(big)
    return all(any(x == y for y in it) for x in small)


@pytest.mark.parametrize("job, n", [("sim-plan", 2), ("build-plan", 3)])
def test_다단_심의_스트림은_done_을_맨_끝에_한_번만_내고_단의_사건과_단_경계_표지는_그대로다(w4, job, n):
    """단 스트림 n 개가 저마다 done 으로 끝나도 바깥 스트림의 done 은 맨 끝 하나다. 단 스트림이 낸 done 밖의 사건은 하나도 빠지지 않고 같은
    차례로 흐르고, 단 경계 표지(checkpoint — n-1 개)도 그대로다."""
    stages: list = []
    out = _web({**OPTS, **({"build_plan": 1} if job == "build-plan" else {})}, stages)
    assert len(stages) == n and all(s[-1] == _DONE for s in stages), "시험 전제 — 단 스트림마다 done 으로 끝났다"
    assert out.count(_DONE) == 1 and out[-1] == _DONE, f"done 이 {out.count(_DONE)}번 났다 — 맨 끝 하나여야 한다"
    for i, s in enumerate(stages, start=1):
        assert _subseq([c for c in s if c != _DONE], out), f"{i}단 스트림의 사건이 빠졌거나 차례가 바뀌었다"
    kinds = [x.get("kind") for ev, x in _frames(out) if ev == "delib"]
    assert kinds.count("checkpoint") == n - 1 and kinds.count("decision") == n and kinds.count("personas") == n, kinds
    assert [ev for ev, _x in _frames(out)][-2] == "result", "마지막 단의 결과 뒤가 done 이 아니다"


@pytest.mark.parametrize("case", ["기준일", "봉인"])
def test_입구에서_거절하면_오류와_done_하나로_맺는다(w4, case):
    opts = {**OPTS, "as_of": "2025/06/01"} if case == "기준일" else {**OPTS, "sealed": 1}
    out = _frames(_web(opts))
    assert [ev for ev, _x in out] == ["error", "done"], out


def test_1단이_결정문을_못_내면_두_오류_뒤_done_하나로_맺는다(w4, monkeypatch):
    """1단 의장이 실패하면 1단 스트림이 chair_failed 오류와 done 으로 끝나고 래퍼가 sim_no_mechanism 오류를 낸다 — done 은 맨 끝 하나다."""
    real = d._llm_text

    async def chair_fails(llm, system, human, *img):
        if _kind(system) == "chair":
            raise RuntimeError("가짜 의장 실패")
        return await real(llm, system, human, *img)
    monkeypatch.setattr(d, "_llm_text", chair_fails)
    out = _frames(_web(OPTS))
    assert [x.get("code") for ev, x in out if ev == "error"] == ["chair_failed", "sim_no_mechanism"], out[-4:]
    assert [ev for ev, _x in out].count("done") == 1 and out[-1][0] == "done" and out[-2][0] == "error", out[-4:]


def test_2단으로_넘어가다_예외가_나면_오류와_done_하나로_맺는다(w4, monkeypatch):
    """1단이 끝난 뒤(1단 스트림은 done 으로 끝났다) 좌석 전환에서 예외가 나면 래퍼가 sim_deliberation_error 와 done 을 낸다 — done 은 맨 끝
    하나다(1단의 done 이 앞에 남지 않는다)."""
    real = d._discover

    async def boom(tools, q, *a, **k):
        if "수치해석 시뮬레이션 모델링" in q:
            raise RuntimeError("좌석 전환 실패")
        return await real(tools, q, *a, **k)
    monkeypatch.setattr(d, "_discover", boom)
    out = _frames(_web(OPTS))
    kinds = [x.get("kind") for ev, x in out if ev == "delib"]
    assert "checkpoint" in kinds and kinds.count("decision") == 1, "시험 전제 — 1단은 끝까지 돌았다"
    assert [ev for ev, _x in out].count("done") == 1 and out[-1][0] == "done", [ev for ev, _x in out][-5:]
    assert out[-2][0] == "error" and out[-2][1]["code"] == "sim_deliberation_error", out[-2]

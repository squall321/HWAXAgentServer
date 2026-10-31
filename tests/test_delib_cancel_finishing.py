# MCP deliberate_cancel 의 마무리 막음 — 의장이 결정문을 낸 뒤(엔진의 finishing 표지부터 그 단의 결정문 사건까지) 접기를 거절하고 잡이 결정문 · 보고서 번호까지 가는지, 그 전 · 다단 심의의 다음 단에서는 종전대로 접히는지, 제자리로 다시 선 잡(끊기기 전에 결정문이 나왔다)은 처음부터 막히는지 본다
#
# 왜 생겼나. 웹 멈춤(app._web_cancel — A24 (나) 0e1d635 · bf8cc55)은 finishing 표지부터 그 단의 결정문까지 409 finishing 으로 접지 않는다 —
# 요약 · 쉬운 설명 · 대조 · Report Archive 저장(실측 3시간 심의에 1시간 반) 중에 접으면 다 만든 결정문이 버려지고 RA 에 들어간 초안은 번호가 남지
# 않는다. MCP 잡 길(delib_jobs.cancel)에는 그 막음이 없어 결정문이 나온 뒤의 deliberate_cancel 이 잡을 접고 결정문을 버렸다(A24 고침 단계 보고).
# 제자리 이어하기(HA-09)로 다시 선 잡은 결정문 뒤 단계를 되살려 마무리만 하는데, 엔진이 finishing 을 다시 내기 전(줄 · 도구 로드)에 접히면 결정문과
# 일지(재개 자료)가 함께 지워졌다.
#
# 하네스는 tests/test_ha09_s1_resume.py · test_ha09_s4_stages.py 의 가짜(바깥 LLM · 게이트웨이 도구 · 좌석 자유 조회만 가짜)다. 엔진 · 잡 원장 ·
# 진행 일지 · MCP 도구는 진짜다. 도구 호출 · 좌석 호출을 붙잡아 둔 채 같은 루프에서 MCP deliberate_cancel 을 부른다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_cancel_finishing.py -q
import asyncio
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import ME, _on_disk  # noqa: E402
from test_delib_ops import _ctx  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import (AFF, GROUPS, OPTS, Q, STUB, _continue, _cut, _eng, _fresh, _restart, _settle_all,  # noqa: E402, F401
                                 world)
from test_ha09_s4_stages import FIXED_CAE, w4  # noqa: E402, F401 — w4 는 픽스처


class _Gate:
    """도구 호출 붙잡기 — arm(이름) 뒤 그 도구의 첫 호출이 hit 을 세우고 open 이 설 때까지 기다린다. 사건은 잡을 도는 루프 안에서 만든다."""

    def __init__(self):
        self.name, self.hit, self.open = None, None, None

    def arm(self, name: str) -> None:
        self.name, self.hit, self.open = name, asyncio.Event(), asyncio.Event()

    async def pass_(self, name: str) -> None:
        if self.name is not None and name == self.name:
            self.name = None                  # 한 번만 붙잡는다
            self.hit.set()
            await self.open.wait()


class _Held:
    coroutine = None

    def __init__(self, inner, gate: _Gate):
        self.inner, self.gate, self.name = inner, gate, inner.name

    async def ainvoke(self, args):
        await self.gate.pass_(self.name)
        return await self.inner.ainvoke(args)


@pytest.fixture
def gate(monkeypatch):
    """지금 걸린 가짜 게이트웨이 도구(_tools_by_name)를 감싸 붙잡을 수 있게 한다. load 를 arm 하면 도구 로드 자체를 붙잡는다."""
    g = _Gate()
    inner = d._tools_by_name

    async def tools(*a, **k):
        await g.pass_("load")
        return {n: _Held(t, g) for n, t in (await inner(*a, **k)).items()}
    monkeypatch.setattr(d, "_tools_by_name", tools)
    return g


async def _cancel(jid: str):
    """MCP deliberate_cancel — ('refused', 글) 또는 ('ok', 응답)."""
    try:
        return "ok", await m.deliberate_cancel(jid, ctx=_ctx(ME, AFF))
    except Exception as exc:  # noqa: BLE001 — 거절은 도구 오류다
        return "refused", f"{type(exc).__name__}: {exc}"


def _unhold(jid: str, got) -> None:
    """접기가 받아들여지지 않았으면 붙잡힌 좌석 호출(취소될 때까지 기다린다)을 태스크 취소로 푼다 — 시험이 기다림 상한(_settle_all)까지 매달리지
    않고 곧바로 붉어지게."""
    t = delib_jobs._TASKS.get(jid)
    if got[0] != "ok" and t is not None and not t.done():
        t.cancel()


def _start(job: str = "default", opts=None) -> dict:
    return delib_jobs.start(STUB, job, Q, groups=list(GROUPS), delib_opts=copy.deepcopy(opts or OPTS), user_email=ME, owner_aff=AFF)


# ── 한 단 심의 — 결정문이 나온 뒤(보고서 저장 중)의 접기는 거절하고 끝까지 간다 ─────────────────────────────────
def test_보고서_저장_중의_MCP_접기는_finishing_으로_거절되고_잡은_결정문과_보고서_번호까지_간다(world, gate):
    async def go():
        job = _start()
        gate.arm("create_report_draft")
        await asyncio.wait_for(gate.hit.wait(), 20)
        got = await _cancel(job["id"])
        running = job["status"]
        gate.open.set()
        await _settle_all()
        return job, got, running
    job, (how, what), running = asyncio.run(go())
    assert how == "refused" and "finishing" in what and "마무리 중" in what and "결정문" in what, (how, what)
    assert running == "running", "거절한 잡을 접었다"
    led = _on_disk(job)
    assert (led["status"], led["report_id"]) == ("done", 77) and led["decision"], (led["status"], led.get("error"))
    assert "finishing" not in led, "끝난 잡의 원장에 접기 막음이 남았다"
    assert delib_jobs.cancel(job["id"], by=ME)["status"] == "done", "끝난 잡의 접기는 종전대로 '이미 끝났다' 다"


def test_결정문_전에는_종전대로_접힌다(world):
    async def go():
        world.hold, world.held = (2, "rel-drop"), asyncio.Event()
        job = _start()
        await asyncio.wait_for(world.held.wait(), 20)
        got = await _cancel(job["id"])
        _unhold(job["id"], got)
        await _settle_all()
        return job, got
    try:
        job, (how, what) = asyncio.run(go())
    finally:
        world.hold = None
    assert how == "ok" and what["status"] == "cancelling", (how, what)
    led = _on_disk(job)
    assert led["status"] == "cancelled" and not led.get("decision") and not led.get("report_id")


# ── 다단 심의 — 1단 마무리 중에는 거절, 1단 결정문 뒤 2단에서는 접힌다 ───────────────────────────────────────
def test_다단_심의는_1단_마무리_중엔_거절하고_1단_결정문_뒤_2단에서는_접힌다(w4, gate):
    async def go():
        job = _start("sim-plan")
        gate.arm("create_report_draft")                 # 1단 보고서 저장
        await asyncio.wait_for(gate.hit.wait(), 20)
        first = await _cancel(job["id"])
        w4.hold, w4.held = (1, FIXED_CAE[0]), asyncio.Event()      # 2단 1라운드의 CAE 좌석(1단에는 없다)
        gate.open.set()
        if first[0] != "refused":                       # 1단에서 접혔다 — 2단을 기다리지 않는다(곧바로 붉어지게)
            await _settle_all()
            return job, first, None
        await asyncio.wait_for(w4.held.wait(), 20)
        second = await _cancel(job["id"])
        _unhold(job["id"], second)
        await _settle_all()
        return job, first, second
    try:
        job, first, second = asyncio.run(go())
    finally:
        w4.hold = None
    assert first[0] == "refused" and "finishing" in first[1], first
    assert second[0] == "ok" and second[1]["status"] == "cancelling", second
    led = _on_disk(job)
    assert led["status"] == "cancelled" and (led.get("checkpoint") or {}).get("stage") == "sim-1", (led["status"], led.get("checkpoint"))
    assert led["report_id"] == 77, "1단 보고서 번호가 잡에 닿지 않았다"


# ── 제자리 이어하기(HA-09) — 끊기기 전에 결정문이 나왔으면 다시 선 잡은 처음부터 막힌다 ─────────────────────────────
def _cut_after(world, monkeypatch, tmp_path, line: str, name: str) -> str:
    """끊지 않은 실행의 일지에 그 줄이 적힌 바로 뒤에 죽은 꼴로 다시 뜬다 — 끊긴 잡 id. 접은 원장에는 접기 막음 칸이 남지 않는다(끊긴 잡은
    접을 것이 없다 — 마무리 중에 끊겨 finishing 표지가 접혔어도)."""
    from test_ha09_s1_resume import _oracle  # noqa: PLC0415
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, name, _cut(snaps, _eng(line)))
    led = _on_disk({"id": jid})
    assert led["status"] == "interrupted" and "finishing" not in led, "끊긴 잡의 원장에 접기 막음이 남았다"
    return jid


@pytest.mark.parametrize("line", ["e:decision", "e:report_intent"])
def test_결정문_뒤에서_끊겨_줄에_다시_선_잡의_접기는_거절되고_일지가_남는다(world, gate, monkeypatch, tmp_path, line):
    jid = _cut_after(world, monkeypatch, tmp_path, line, f"q-{line}")

    async def go():
        delib_jobs.closing(True)                      # 자리가 있어도 띄우지 않는다 — 줄에 선 채로 둔다
        out = await m.deliberate_continue(previous_job_id=jid, ctx=_ctx(ME, AFF, ",".join(GROUPS)))
        got = await _cancel(jid)
        state = delib_jobs.get(jid)["status"]
        delib_jobs.closing(False)
        delib_jobs._pump()
        await _settle_all()
        return out, got, state
    try:
        out, (how, what), state = asyncio.run(go())
    finally:
        delib_jobs.closing(False)
    assert out["status"] == "queued" and out["resumed"]["mode"] == "in_place", out
    assert how == "refused" and "finishing" in what, (how, what)
    assert state == "queued", "거절한 잡을 줄에서 뺐다"
    led = _on_disk({"id": jid})
    assert led["status"] == "done" and led["decision"] and led["report_id"], (led["status"], led.get("error"))


def test_결정문_뒤에서_끊겨_다시_뜬_잡은_finishing_을_다시_내기_전에도_접기를_거절한다(world, gate, monkeypatch, tmp_path):
    """다시 뜬 잡은 결정문 뒤 단계만 남았다(S3) — 엔진이 도구를 다시 받는 동안(finishing 표지를 다시 내기 전)의 접기도 거절한다. 종전엔 그 틈에
    접혀 결정문 · 일지가 지워졌다."""
    jid = _cut_after(world, monkeypatch, tmp_path, "e:report_intent", "run")

    async def go():
        gate.arm("load")
        out = await m.deliberate_continue(previous_job_id=jid, ctx=_ctx(ME, AFF, ",".join(GROUPS)))
        await asyncio.wait_for(gate.hit.wait(), 20)
        got = await _cancel(jid)
        gate.open.set()
        await _settle_all()
        return out, got
    out, (how, what) = asyncio.run(go())
    assert out["resumed"]["mode"] == "in_place", out
    assert how == "refused" and "finishing" in what, (how, what)
    led = _on_disk({"id": jid})
    assert led["status"] == "done" and led["decision"] and led["report_id"], (led["status"], led.get("error"))
    assert not delib_jobs._journal_path(jid).exists(), "끝난 잡의 일지가 남았다"


def test_결정문_전에서_끊겨_다시_뜬_잡은_종전대로_접힌다(world, gate, monkeypatch, tmp_path):
    jid = _cut_after(world, monkeypatch, tmp_path, "e:round_end", "pre")

    async def go():
        gate.arm("load")
        await m.deliberate_continue(previous_job_id=jid, ctx=_ctx(ME, AFF, ",".join(GROUPS)))
        await asyncio.wait_for(gate.hit.wait(), 20)
        got = await _cancel(jid)
        gate.open.set()
        await _settle_all()
        return got
    how, what = asyncio.run(go())
    assert how == "ok" and what["status"] == "cancelling", (how, what)
    assert _on_disk({"id": jid})["status"] == "cancelled"


def test_결정문_뒤에서_끊겨_줄에_다시_선_채로_재기동해도_원장_값이_아니라_재개_자료로_막는다(world, monkeypatch, tmp_path):
    """줄에 다시 선 제자리 이어하기는 재기동하면 재기동 정리가 다시 세운다(_requeue). 막음은 원장의 칸이 아니라 다시 읽은 재개 자료(e:decision)로
    정한다 — 이 판 전에 줄을 선 잡(원장에 그 칸이 없다)이 새 판으로 다시 서도 막힌다."""
    jid = _cut_after(world, monkeypatch, tmp_path, "e:report_intent", "rq")
    delib_jobs.closing(True)                              # 자리가 있어도 띄우지 않는다 — 줄에 선 채로 둔다
    try:
        out = _continue(jid, wait=False)
    finally:
        delib_jobs.closing(False)
    assert out["status"] == "queued" and out["resumed"]["mode"] == "in_place", out
    p = delib_jobs._path(jid)
    led = json.loads(p.read_text(encoding="utf-8"))
    assert led.get("finishing") is True, "시험 전제 — 줄에 다시 선 잡의 원장에 막음이 적혔다"
    led.pop("finishing")                                  # 이 판 전에 줄을 선 잡의 원장 꼴
    p.write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
    _fresh(monkeypatch, delib_jobs.JOB_DIR)               # 같은 디스크로 다시 뜬다
    m.bind(STUB)
    assert list(delib_jobs._PENDING) == [jid]

    async def go():
        got = await _cancel(jid)
        delib_jobs._pump()
        await _settle_all()
        return got
    how, what = asyncio.run(go())
    assert how == "refused" and "finishing" in what, (how, what)
    led = _on_disk({"id": jid})
    assert led["status"] == "done" and led["decision"] and led["report_id"], (led["status"], led.get("error"))

# 제자리 이어하기로 이어 돈 단의 과제 인사이트 카드 거리 — 지정 도구 표지까지 끊지 않은 실행과 같은지(반대 검토 11-02 의 미확인 하나를 재현해 고쳤다)
"""HWAXPortal docs/project-insight-graph contracts §1 · §2.

되살릴 때(재개 자료 e:pre)는 라운드 전 단계(지정 도구 · VOC · 선주입)를 다시 부르지 않는다. 도구 이름은 e:pre 의 ins_tools 로 되살았지만 지정 도구의
근거 표지(카드 calls · refs)는 빠졌다 — 끊긴 잡을 이으면 같은 단의 카드가 끊지 않은 실행보다 표지가 적었다. 이제 e:pre 에 ins_calls 를 함께 적고
되살린다. 시험은 HA-09 S1 의 결정적 가짜 바깥(test_ha09_s1_resume)으로 주제를 단 잡을 끝까지 돌린 것을 오라클로 삼아, 2라운드 가운데서 끊고 이은
잡의 카드를 견준다.

  실행:  .venv/bin/python -m pytest tests/test_insight_resume.py -q
"""
import asyncio
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import delib_jobs  # noqa: E402

from test_delib_job_queue import ME, _on_disk  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401
from test_ha09_s1_resume import AFF, GROUPS, OPTS, Q, STUB, _continue, _cut, _disk, _restart, _settle_all, _turn_line, world  # noqa: E402, F401

SUBJECT = {"sp_task": "알파 개발_C2", "sp_state": "confirmed", "round": "dv1", "text": "", "artifacts": []}


def _oracle(w, monkeypatch, snaps: list) -> dict:
    """주제를 단 잡을 끊지 않고 끝까지 — 일지가 한 줄 적힐 때마다 (그 줄, 디스크) 를 뜬다. 원장을 돌려준다."""
    orig = delib_jobs._Journal._write

    def spy(self, data, sync):
        orig(self, data, sync)
        snaps.append((json.loads(data), _disk()))
    monkeypatch.setattr(delib_jobs._Journal, "_write", spy)

    async def go():
        job = delib_jobs.start(STUB, "default", Q, groups=list(GROUPS), delib_opts=copy.deepcopy(OPTS), user_email=ME, owner_aff=AFF,
                               subject=dict(SUBJECT))
        await _settle_all()
        return job
    try:
        job = asyncio.run(go())
    finally:
        monkeypatch.setattr(delib_jobs._Journal, "_write", orig)
    w.reset()
    return _on_disk(job)


def test_이어_돈_단의_카드는_지정_도구_표지까지_끊지_않은_실행과_같다(world, monkeypatch, tmp_path):
    snaps: list = []
    want = _oracle(world, monkeypatch, snaps)["insights"]["default"]
    assert [c for c in want["calls"] if c["tool"] == "list_parts"], "전제 — 지정 도구(list_parts)의 표지가 끊지 않은 실행의 카드에 있다"
    jid = _restart(monkeypatch, tmp_path, "mid2", _cut(snaps, _turn_line(2), nth=2))
    out = _continue(jid)
    assert out["resumed"]["mode"] == "in_place", out["resumed"]
    got = _on_disk({"id": jid})["insights"]["default"]
    assert sorted(c["ref"] for c in got["calls"]) == sorted(c["ref"] for c in want["calls"]), "되살린 단의 표지가 빠졌다"
    assert sorted(got["refs"]) == sorted(want["refs"]) and got["tools"] == want["tools"] and got["untracked"] == want["untracked"]
    assert got["untracked"] == ["note"], "OPTS 의 사람 의견 — 범위를 모르는 입력(되살려도 같다)"

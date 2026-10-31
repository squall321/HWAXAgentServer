# 끊긴 자리의 알림을 한 번만(HA-09 남은 일 10-31) — 라운드 조회 · 라운드 머리 · 결정문 단계의 알림(경고 · 좌석에 주지 않은 카드)이 그 자리의 재개 자료 줄보다 먼저 원장에 닿은 뒤 끊겨도, 이은 실행이 같은 일을 다시 하며 낸 같은 알림을 원장에 다시 싣지 않는지 일지의 줄마다 끊어 본다
#
# 왜 생겼나. 라운드의 조회 알림(기준일 거름 확인 안 됨 · not_visible · 그림 판독 못 함 · 조회 상한 카드 · 조회 실패)은 그 라운드의 e:gather 보다
# 먼저 원장에 닿는다. 그 사이에 KILL 이 나면 이은 실행은 조회를 다시 하고(e:gather 가 없다) 같은 알림을 또 내 원장에 두 번 실렸다(HA-09 고침 단계
# 보고). 라운드 머리 카드(좌석 직전 라운드 상한 초과)도 조회 도중 끊기면 같았고(S2 는 진척이 있을 때만 막았다), 결정문 단계의 알림(의장 전사 상한
# 카드 · 의장 출력 상한 경고 · 요약 실패 경고)도 그 단계의 줄(e:decision · e:plain) 앞이라 같았다(README S3 '아직 열린 것'). 이제 엔진이 자리마다
# e:round_in 을 적고, 그 뒤의 알림이 그 자리의 것이다 — 이으면 그 자리에서 다시 낸 같은 알림은 원장 · 일지에 다시 싣지 않는다(delib_resume.Seen).
#
# 하네스는 tests/test_ha09_s1_resume.py 그대로다(가짜는 바깥 LLM · 게이트웨이 도구 · 좌석 자유 조회만). 끊는 자리는 끊지 않은 실행의 일지가 한 줄
# 적힐 때마다 뜬 디스크 꼴이다(KILL 과 같다).
#
#   실행:  .venv/bin/python -m pytest tests/test_ha09_notices_once.py -q
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import delib_resume  # noqa: E402
import deliberation as d  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import (OPTS, ROUNDS, TAGS, _check_resumed, _continue, _kind, _oracle, _restart, _result,  # noqa: E402
                                 _turn_rows, world)  # noqa: F401 — world 는 픽스처
from test_ha09_s2_seat_resume import _spy  # noqa: E402

AS_OF = "2026-06-01"
OPTS_N = {**OPTS, "as_of": AS_OF}
_CARD = "끊긴 심의 되살림"
# 2라운드 조회에서 나는 알림 — 카드 출처(좌석에 주지 않은 카드)와 경고 글귀
R2_CARDS = ("기준일 거름 확인 안 됨 — 자유 조회", "pcb-warp · 자유 조회 실패", "좌석 조회 그림 판독 못 함", "자기 조회 결과 상한 초과",
            "좌석 직전 라운드 상한 초과")
R2_WARNS = ("거름이 걸렸다는 확인(메아리)", "pcb-warp 자유 조회 실패")


@pytest.fixture
def nw(world, monkeypatch):
    """S1 가짜 위에 — 2라운드 조회에서 알림이 난다: rel-drop 의 agent_search 가 기준일 메아리 없이 버려지고(경고 · 카드 — 심의에 한 번),
    pcb-warp 의 조회가 실패하고(경고 · 카드), therm-sink 가 받은 그림 하나를 판독하지 못한다(카드). 조회 결과를 길게 하고 발언 턴 몫을 줄여 조회
    라운드마다 조회 상한 카드가, 좌석 프롬프트 상한을 줄여 2라운드부터 라운드 머리 카드가 난다."""
    real = world.gather

    async def gather(agent, persona, question, ctx, budget, prior=""):
        k, calls, _blk, err = await real(agent, persona, question, ctx, budget, prior)
        r = int((TAGS.get() or {}).get("round") or 0)
        calls = [(n, a, json.dumps({**json.loads(o), "pad": "가나다라" * 150}, ensure_ascii=False), c) for n, a, o, c in calls]
        if r == 2 and k == "rel-drop":
            dropped = json.dumps({d._AS_OF_UNCONFIRMED: f"기준일 {AS_OF} 거름이 걸렸는지 확인하지 못해 이 조회의 지식카드 2장을 싣지 않았다",
                                  "hits": [], "hit_count": 0, "refused": True}, ensure_ascii=False)
            calls.append(("agent_search", json.dumps({"q": k}), dropped, f"c-{r}-{k}-as"))
        if r == 2 and k == "pcb-warp":
            err = "가짜 조회 실패 — 게이트웨이 응답 없음"
        if r == 2 and k == "therm-sink":
            d._SEAT_IMG.get()["fail"] += 1
        blk = "\n".join(f"- {n}({a}): {o}" for n, a, o, _c in calls)
        return k, calls, blk, err

    monkeypatch.setattr(d, "_free_gather_one", gather)
    monkeypatch.setattr(d, "_lookup_budget", lambda: 400)
    monkeypatch.setattr(d, "_seat_ctx", lambda: 300)
    return world


@pytest.fixture
def pw(world, monkeypatch):
    """S1 가짜 위에 — 결정문 단계의 알림: 의장 전사 상한 카드(의장 부르기 전), 의장 출력 상한 경고(의장 뒤 · e:decision 앞), 요약 실패 경고 ·
    후처리 입력 상한 카드(e:plain 앞)."""
    real = world.llm_text

    async def llm_text(llm, system, human, *img):
        kind = _kind(system)
        if kind == "summary":
            raise RuntimeError("가짜 요약 실패")
        out = await real(llm, system, human, *img)
        return out + "\n" + d._OUT_CUT_MARK if kind == "chair" else out

    monkeypatch.setattr(d, "_llm_text", llm_text)
    monkeypatch.setattr(d, "_decision_ctx", lambda *_a, **_k: 120)
    monkeypatch.setattr(d, "_text_turn_room", lambda *_a, **_k: 40)
    return world


def _journal(disk: dict) -> list:
    return [json.loads(x) for x in next(v for k_, v in disk.items() if k_.startswith("journal/")).splitlines()]


def _at(snaps: list, pred) -> int:
    return next(i for i, (rec, _disk) in enumerate(snaps) if pred(rec))


def _resume(w, monkeypatch, tmp_path, name: str, disk: dict) -> dict:
    jid = _restart(monkeypatch, tmp_path, name, disk)
    out = _continue(jid)
    assert out["job_id"] == jid and out["resumed"]["mode"] == "in_place", (name, out["resumed"])
    res = _result(w, {"id": jid})
    w.reset()
    return res


def _notices(led: dict) -> tuple:
    """원장의 알림 — 되살림 카드와 '저장 도중 끊겨 다시 저장한다' 경고(이은 실행이 처음 내는 것)는 뺀다."""
    ws = [x for x in led.get("warnings") or [] if "끝났는지 알 수 없어" not in x]
    return ([c for c in led["evidence_omitted"] if c["source"] != _CARD], ws,
            int(led.get("warnings_total") or 0) - (len(led.get("warnings") or []) - len(ws)))


# ── 라운드 — 조회 · 라운드 머리 · 좌석 · 라운드 끝의 어느 줄에서 끊겨도 ─────────────────────────────────────────
def test_2라운드의_어느_줄에서_끊겨도_그_라운드의_알림이_원장에_한_번씩_남고_나머지는_끊지_않은_실행과_같다(nw, monkeypatch, tmp_path):
    snaps: list = []
    o = _oracle(nw, monkeypatch, snaps, opts=OPTS_N)
    srcs = [c["source"] for c in o["led"]["evidence_omitted"]]
    assert all(srcs.count(s) >= 1 for s in R2_CARDS), f"시험 전제 — 2라운드 조회의 알림 카드가 났다: {srcs}"
    assert srcs.count("기준일 거름 확인 안 됨 — 자유 조회") == 1, "시험 전제 — 기준일 알림은 심의에 한 번이다"
    assert all(any(w in x for x in o["led"]["warnings"]) for w in R2_WARNS), o["led"]["warnings"]
    i0, i1 = (_at(snaps, lambda rec, r=r: rec.get("k") == "e:round_end" and rec.get("r") == r) for r in (1, 2))
    seen_gather = False
    for i in range(i0, i1):
        lines = _journal(snaps[i][1])
        gathered = any(x["k"] == "e:gather" and x.get("r") == 2 for x in lines)
        seen_gather = seen_gather or gathered
        spoken = [x["o"]["persona"] for x in lines if x["k"] == "e:raw" and x.get("r") == 2]
        res = _resume(nw, monkeypatch, tmp_path, f"c{i}", snaps[i][1])
        try:
            _check_resumed(o, res, 2, spoken=spoken, gathered=gathered)
        except AssertionError as exc:
            raise AssertionError(f"{i - i0}번째 줄({snaps[i][0].get('k')} {json.dumps(snaps[i][0].get('d'), ensure_ascii=False)[:80]}) 뒤에서 "
                                 f"끊겼다 — {exc}") from None
    assert seen_gather and i1 - i0 > 20, "시험 전제 — 조회 앞 · 뒤의 줄마다 끊어 봤다"


def test_같은_라운드에서_두_번_끊겨도_앞_시도들의_알림을_다_알아_한_번씩_남는다(nw, monkeypatch, tmp_path):
    """첫 시도는 2라운드 조회의 알림 하나(조회 실패 경고)를 원장에 실은 뒤 끊겼다. 둘째 시도는 그 알림을 다시 싣지 않고(일지에도 적지 않는다) 그
    뒤의 알림(기준일 카드)을 실은 뒤 또 끊겼다. 셋째 시도는 두 시도의 알림을 모두 안다 — 앞 시도의 것은 옮겨 적힌 e:note 로, 둘째 시도의 것은 그
    공개 사건으로."""
    snaps: list = []
    o = _oracle(nw, monkeypatch, snaps, opts=OPTS_N)
    first = _at(snaps, lambda rec: rec.get("k") == "warning" and "pcb-warp 자유 조회 실패" in str((rec.get("d") or {}).get("message")))
    jid = _restart(monkeypatch, tmp_path, "a", snaps[first][1])
    again: list = []
    with _spy(monkeypatch, again):
        _continue(jid)
    nw.reset()
    lines2 = [json.loads(x) for x in again[-1][1][f"journal/{jid}.jsonl"].splitlines()]
    assert not [x for x in lines2 if x["k"] == "warning" and "pcb-warp 자유 조회 실패" in x["d"]["message"]], \
        "둘째 시도가 첫 시도의 알림을 일지에 다시 적었다"
    assert [x["k"] for x in lines2 if x.get("r") == 2 and x["k"].startswith("e:")][:2] == ["e:note", "e:note"], \
        "첫 시도의 알림을 새 일지에 e:note 로 옮겨 적지 않았다"
    second = _at(again, lambda rec: rec.get("k") == "delib" and (rec.get("d") or {}).get("source") == "기준일 거름 확인 안 됨 — 자유 조회")
    res = _resume(nw, monkeypatch, tmp_path, "b", again[second][1])
    _check_resumed(o, res, 2)
    assert len(res["led"]["attempts"]) == 2


# ── 결정문 단계 — 의장 전 · 의장 뒤 · 요약 실패 뒤의 어느 줄에서 끊겨도 ──────────────────────────────────────────
def test_결정문_단계의_어느_줄에서_끊겨도_그_단계의_알림이_원장에_한_번씩_남는다(pw, monkeypatch, tmp_path):
    """의장 전사 상한 카드는 의장을 부르기 전에, 의장 출력 상한 경고는 의장 뒤 e:decision 전에, 요약 실패 경고 · 후처리 입력 상한 카드는 e:plain
    전에 원장에 닿는다. 그 사이에 끊겨 이으면 의장 · 요약을 다시 부르며 같은 알림을 또 냈다 — 이제 한 번이다. 결정문 · 전사도 끊지 않은 실행과
    같다."""
    snaps: list = []
    o = _oracle(pw, monkeypatch, snaps)
    b = o["led"]
    srcs = [c["source"] for c in b["evidence_omitted"]]
    assert srcs.count("의장 전사 상한 초과") == 1 and srcs.count("후처리 입력 상한 초과") == 1, srcs
    assert [x for x in b["warnings"] if "출력 상한에서 끊겼다" in x] and [x for x in b["warnings"] if "핵심 요약을 만들지 못했다" in x], b["warnings"]
    i0 = _at(snaps, lambda rec: rec.get("k") == "e:round_end" and rec.get("r") == ROUNDS)
    i1 = _at(snaps, lambda rec: rec.get("k") == "delib" and (rec.get("d") or {}).get("kind") == "decision")
    assert i1 - i0 > 8, "시험 전제 — 결정문 단계의 줄"
    for i in range(i0, i1):
        res = _resume(pw, monkeypatch, tmp_path, f"p{i}", snaps[i][1])
        a = res["led"]
        where = f"{i - i0}번째 줄({snaps[i][0].get('k')}) 뒤"
        assert res["seat"] == {} and (a["status"], a["decision"], a["report_id"]) == (b["status"], b["decision"], b["report_id"]), where
        assert _turn_rows(a) == _turn_rows(b), where
        assert _notices(a) == _notices(b), f"{where} — 알림이 두 번 실렸거나 빠졌다: {_notices(a)[0][-3:]} / {_notices(a)[1][-3:]}"


# ── 재개 자료의 꼴 — 자리 · 단이 맞을 때만 거르고, 같은 알림은 난 수만큼만 ─────────────────────────────────────────
def _w(ev: str, msg: str) -> tuple:
    return ev, {"code": "x", "message": msg}


def test_Seen_은_그_단_그_자리에서만_켜지고_자리가_끝나면_꺼지며_같은_알림은_난_수만큼만_거른다():
    s = delib_resume.Seen("sim-2", 3, [_w("warning", "가"), _w("warning", "가"), ("delib", {"kind": "evidence", "included": False,
                                                                                         "source": "카드", "text": "글"})])
    assert not s.again(*_w("warning", "가")), "자리에 들기 전에 걸렀다"
    for rec in ({"k": "e:round_in", "sk": "sim-1", "r": 3}, {"k": "e:round_in", "sk": "sim-2", "r": 2}):
        s.gate(rec)
        assert not s.again(*_w("warning", "가")), f"다른 단 · 다른 자리에서 켜졌다: {rec}"
    s.gate({"k": "e:round_in", "sk": "sim-2", "r": 3})
    assert s.again(*_w("warning", "가")) and s.again(*_w("warning", "가")) and not s.again(*_w("warning", "가")), "난 수만큼만 걸러야 한다"
    assert not s.again("delib", {"kind": "evidence", "included": True, "source": "카드", "text": "글"}), "좌석에 준 카드는 알림이 아니다"
    assert s.again("delib", {"kind": "evidence", "included": False, "source": "카드", "text": "글"})
    s2 = delib_resume.Seen("main", 2, [_w("warning", "나")])
    s2.gate({"k": "e:round_in", "sk": "main", "r": 2})
    s2.gate({"k": "e:round_end", "sk": "main", "r": 2})
    assert not s2.again(*_w("warning", "나")), "자리가 끝난 뒤에 걸렀다"
    s3 = delib_resume.Seen("main", 5, [_w("warning", "다")])
    s3.gate({"k": "e:round_in", "sk": "main", "r": 5})
    assert not s3.again("delib", {"kind": "decision", "text": "결정문"}) and not s3.again(*_w("warning", "다")), "결정문 뒤에 걸렀다"


def test_알림_열쇠는_원장이_알림으로_싣는_사건에만_있다():
    """원장(delib_jobs._apply)이 알림으로 싣는 것 — 경고와 좌석에 주지 않은 카드(included 가 거짓)뿐이다. 좌석에 준 근거 카드 · 상태줄 · 턴 같은
    사건은 걸러 내지 않는다(같은 조회를 다시 해 다시 낸 근거 카드는 근거 패널에 다시 떠야 한다)."""
    assert delib_resume.notice_key("warning", {"code": "x", "message": "가"}) is not None
    assert delib_resume.notice_key("delib", {"kind": "evidence", "included": False, "source": "카드", "text": "글"}) is not None
    for ev, data in (("delib", {"kind": "evidence", "included": True, "source": "카드", "text": "글"}),
                     ("delib", {"kind": "evidence", "source": "카드", "text": "글"}), ("delib", {"kind": "turn", "persona": "a"}),
                     ("status", {"step": "가"}), ("error", {"message": "가"}), ("warning", None), ("warning", "글")):
        assert delib_resume.notice_key(ev, data) is None, (ev, data)
    assert delib_resume.notice_key("warning", {"message": "가", "code": "x"}) == delib_resume.notice_key("warning", {"code": "x", "message": "가"})

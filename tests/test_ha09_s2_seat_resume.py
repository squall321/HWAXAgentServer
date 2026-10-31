# 끊긴 라운드를 좌석 단위로 잇기(HA-09 S2) — 라운드 r 에서 k 석이 말한 뒤 끊고 같은 잡을 이으면 부르는 좌석이 정확히 나머지이고 그 프롬프트가 끊지 않은 실행과 지문까지 같은지, 조회 도중 끊기면 조회를 다시 하는지, 실패한 좌석을 다시 부르는지, 전사에 좌석마다 한 번인지 본다
#
# 왜 생겼나. S1 은 끊긴 라운드를 처음부터 다시 돌았다 — 그 라운드에서 끊기기 전에 말한 좌석도 다시 불러, 20석 심의가 19석 말한 뒤 끊기면 그
# 19석의 발언(LLM 호출 몇십 분)을 다시 샀고 조회도 다시 했다. 한 라운드의 좌석은 서로의 이번 라운드 발언을 보지 않아(병렬) 이미 말한 좌석의 원
# 출력을 그대로 두고 나머지만 불러도 뜻이 같다. 엔진이 끊긴 라운드의 조회(e:gather)와 말한 좌석의 원 출력(e:raw)을 되살리고 나머지만 부른다
# (HWAXPortal docs/request-1027/design-ha09-ha20.md A5.3 셋째 · A6 S2).
#
# 하네스는 tests/test_ha09_s1_resume.py 그대로다 — 가짜는 바깥(LLM · 게이트웨이 도구 · 좌석 자유 조회)에만 있고 엔진 · 잡 원장 · 진행 일지 ·
# 재기동 정리 · MCP 도구는 진짜다. 오라클은 같은 화두를 끊지 않고 끝까지 돈 실행이고, 끊는 자리는 그 실행의 일지가 한 줄 적힐 때마다 뜬 디스크
# 꼴이다(KILL 과 같다). 가짜 LLM 은 한 라운드의 좌석을 앉은 차례대로 끝낸다 — 'k 석이 말한 뒤' 는 앉은 차례의 앞 k 석이다.
#
#   실행:  .venv/bin/python -m pytest tests/test_ha09_s2_seat_resume.py -q
import asyncio
import contextlib
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
from test_delib_job_queue import _on_disk  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import (ROUNDS, SEATS, STUB, _check_resumed, _continue, _cut, _disk, _eng, _fresh, _oracle,  # noqa: E402
                                 _restart, _result, _settle_all, _turn_line, _turn_rows, world)  # noqa: F401 — world 는 픽스처

_RESUME_CARD = "끊긴 심의 되살림"


def _card(led: dict) -> str:
    notes = [c for c in led["evidence_omitted"] if c["source"] == _RESUME_CARD]
    assert len(notes) == 1, notes
    return notes[0]["text"]


def _after_lookups(snaps: list, r: int, j: int) -> dict:
    """r 라운드의 단계 줄 뒤 — 좌석 자유 조회 상태줄이 j 개 적힌 바로 뒤의 디스크(j=0 이면 단계 줄 바로 뒤). 그 라운드의 조회가 끝나기 전(e:gather 전)이다."""
    i0 = next(i for i, (rec, _d) in enumerate(snaps) if rec.get("k") == "delib" and (rec.get("d") or {}).get("kind") == "stage"
              and rec["d"].get("stage") == f"r{r}")
    hits = [i for i, (rec, _d) in enumerate(snaps) if i > i0 and rec.get("k") == "status"
            and "조회: search_parts" in str((rec.get("d") or {}).get("step"))]
    i = hits[j - 1] if j else i0
    assert not any(rec.get("k") == "e:gather" for rec, _d in snaps[i0:i + 1]), "시험 전제 — 그 라운드의 조회가 끝나기 전이다"
    return snaps[i][1]


@contextlib.contextmanager
def _spy(monkeypatch, snaps: list):
    """이어 도는 동안에도 일지가 한 줄 적힐 때마다 (그 줄, 디스크) 를 뜬다 — 이은 실행을 또 끊는 자리."""
    orig = delib_jobs._Journal._write

    def spy(self, data, sync):
        orig(self, data, sync)
        snaps.append((json.loads(data), _disk()))
    monkeypatch.setattr(delib_jobs._Journal, "_write", spy)
    try:
        yield snaps
    finally:
        monkeypatch.setattr(delib_jobs._Journal, "_write", orig)


# ── 라운드 r 에서 k 석이 말한 뒤 끊고 이으면 나머지만 부른다 ─────────────────────────────────────
@pytest.mark.parametrize("r, k, at", [(1, 2, "turn"), (2, 1, "turn"), (2, 4, "raw"), (3, 3, "turn"), (3, 5, "turn"), (4, 2, "turn"),
                                      (4, 5, "raw"), (2, 0, "gather"), (3, 0, "gather")])
def test_k석이_말한_뒤_끊고_이으면_부르는_좌석이_정확히_나머지이고_프롬프트가_끊지_않은_실행과_지문까지_같다(world, monkeypatch, tmp_path, r, k, at):
    """at=turn — k 번째 좌석의 턴까지 원장에 닿은 뒤. at=raw — k 번째 좌석의 원 출력(e:raw)이 적히고 그 턴은 원장에 닿기 전(KILL 은 아무 줄
    뒤에서나 난다 — 원 출력이 턴보다 먼저 적힌다). at=gather — 그 라운드의 조회가 끝난 줄(e:gather) 바로 뒤 · 아무도 말하기 전. 어느 쪽이든
    k 석은 다시 부르지 않고 원 출력으로 되살려 그 턴을 다시 내고, 나머지만 끊지 않은 실행과 같은 프롬프트로 부른다. 그 라운드의 조회는
    끝났으니 다시 하지 않는다(4라운드는 수렴이라 조회가 없다). k 가 전 좌석이면 아무도 부르지 않고 그 라운드를 맺는다."""
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    snap = _cut(snaps, _turn_line(r) if at == "turn" else _eng("e:raw", r), nth=k) if k else _cut(snaps, _eng("e:gather", r))
    jid = _restart(monkeypatch, tmp_path, f"r{r}k{k}{at}", snap)
    led = _on_disk({"id": jid})
    spoken, todo = list(SEATS[:k]), list(SEATS[k:])
    sr = led["seat_resume"]
    assert (sr["ok"], sr["round"], sr["spoken"], sr["todo"]) == (True, r, spoken, todo), sr
    turned = [t["persona"] for t in led["turns"] if t["round"] == r]
    assert turned == spoken[:k if at != "raw" else k - 1], "시험 전제 — 끊긴 자리의 원장 턴"
    out = _continue(jid)
    rs = out["resumed"]
    assert (out["job_id"], rs["mode"], rs["round"], rs["spoken"], rs["todo"]) == (jid, "in_place", r, spoken, todo), rs
    if k:
        assert (f"이미 말한 {k}석은 다시 부르지 않고 남은 {len(todo)}석만 부른다" if todo else "아무도 다시 부르지 않는다") in out["note"], out["note"]
    assert ("조회는 끊기기 전에 끝나 다시 하지 않는다" in out["note"]) is (r < ROUNDS), out["note"]
    res = _result(world, {"id": jid})
    assert sorted(s for rr, s in res["seat"] if rr == r) == sorted(todo), "끊긴 라운드에서 부른 좌석이 정확히 나머지가 아니다"
    _check_resumed(o, res, r, spoken=spoken, gathered=r < ROUNDS)
    text = _card(res["led"])
    if k:
        assert f"끊기기 전에 말한 {k}석(" in text and ((f"나머지 {len(todo)}석(" in text) if todo else ("아무도 다시 부르지 않는다" in text)), text
    assert ("끊기기 전에 끝나 다시 하지 않았다" in text) is (r < ROUNDS) and "다시 조회하지 않았다" in text, text


# ── 조회 도중 끊기면 그 라운드의 조회를 다시 한다 ─────────────────────────────────────────
@pytest.mark.parametrize("r, j", [(1, 0), (1, 2), (2, 3), (3, 4)])
def test_조회_도중에_끊기면_그_라운드의_조회를_처음부터_다시_하고_좌석은_끊지_않은_실행과_같게_말한다(world, monkeypatch, tmp_path, r, j):
    """e:gather 는 그 라운드의 조회가 다 끝나야 적힌다 — 그 전에 끊겼으면(조회 j 건이 끝난 뒤) 끝난 조회도 되살릴 것이 없어 그 라운드의 조회를
    처음부터 다시 한다. 좌석은 아무도 말하지 않았으니 전부 부르고, 프롬프트는 끊지 않은 실행과 같다."""
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, f"g{r}{j}", _after_lookups(snaps, r, j))
    sr = _on_disk({"id": jid})["seat_resume"]
    assert (sr["round"], sr["spoken"], sr["todo"]) == (r, [], list(SEATS)), sr
    out = _continue(jid)
    assert out["resumed"]["mode"] == "in_place" and "조회는 끊기기 전에 끝나" not in out["note"], out["note"]
    res = _result(world, {"id": jid})
    assert [x for x in res["lookups"] if x[0] == r] == sorted((r, s) for s in SEATS), "그 라운드의 조회를 다시 하지 않았다"
    _check_resumed(o, res, r)
    assert "끊기기 전에 끝나지 않아 처음부터 다시 한다" in _card(res["led"])


# ── 끊기기 전에 실패한 좌석은 다시 부른다 ────────────────────────────────────────────────
def test_끊기기_전에_실패한_좌석은_같은_프롬프트로_다시_부르고_전사에_한_번_남는다(world, monkeypatch, tmp_path):
    """3라운드에서 마지막 좌석의 호출이 실패한 뒤(일지에는 아무것도 남지 않는다 — 실패는 라운드 끝 줄의 유실로만 적힌다) 다른 두 석이 말하고
    끊겼다. 끊기지 않았다면 그 좌석은 그 라운드에서 유실이었다(시험 전제). 이으면 실패한 좌석은 아직 말하지 않은 좌석과 함께 실패했던 그
    프롬프트로 다시 부르고, 뒤 라운드는 실패 없이 끊지 않은 실행과 같다."""
    o = _oracle(world, monkeypatch)                      # 실패도 끊김도 없는 실행
    last = SEATS[-1]
    world.fail = {(3, last)}
    snaps: list = []
    _fresh(monkeypatch, tmp_path / "failing")           # 끊을 디스크에는 실패한 실행의 잡 하나만
    f = _oracle(world, monkeypatch, snaps)              # 실패한 실행을 끝까지 — 그 좌석은 3라운드에서 유실이다
    assert (3, last) not in {(t["round"], t["persona"]) for t in f["led"]["turns"]} and any(
        c["source"] == "좌석 유실" and last in c["text"] for c in f["led"]["evidence_omitted"]), "시험 전제 — 끊기지 않았다면 유실이다"
    assert f["seat"][(3, last)][0] == o["seat"][(3, last)][0], "시험 전제 — 실패한 호출도 같은 프롬프트였다"
    world.fail = set()
    jid = _restart(monkeypatch, tmp_path, "fail", _cut(snaps, _turn_line(3), nth=2))
    sr = _on_disk({"id": jid})["seat_resume"]
    assert (sr["round"], sr["spoken"], sr["todo"]) == (3, list(SEATS[:2]), list(SEATS[2:])), sr
    _continue(jid)
    res = _result(world, {"id": jid})
    assert sorted(s for rr, s in res["seat"] if rr == 3) == sorted(SEATS[2:]) and res["seat"][(3, last)][0] == f["seat"][(3, last)][0]
    _check_resumed(o, res, 3, spoken=SEATS[:2], gathered=True)
    assert "아직 답하지 않았거나 실패한 좌석 — 실패한 좌석은 끊기지 않았다면 유실이었다" in _card(res["led"])


def test_끊기기_전에_조회가_없던_끊긴_라운드는_이을_때_조회_도구가_생겨도_조회하지_않는다(world, monkeypatch, tmp_path):
    """e:gather 없이 원 출력만 있는 끊긴 라운드는 끊기기 전에도 조회가 없던 라운드다(조회가 끝나야 좌석이 말하고 그 줄이 먼저 적힌다). 이을 때
    게이트웨이에 자유 조회 도구가 새로 보여도 그 라운드는 조회하지 않는다 — 이미 말한 좌석과 나머지 좌석이 같은 재료(조회 없음)로 말한다."""
    real = d._free_tool_ok
    monkeypatch.setattr(d, "_free_tool_ok", lambda _n: False)      # 끊기기 전 — 자유 조회 도구가 하나도 없다
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    assert o["lookups"] == [] and not [rec for rec, _d in snaps if rec.get("k") == "e:gather"], "시험 전제 — 끊기기 전에는 조회가 없다"
    jid = _restart(monkeypatch, tmp_path, "nolook", _cut(snaps, _turn_line(2), nth=2))
    monkeypatch.setattr(d, "_free_tool_ok", real)                  # 이을 때 — 조회 도구가 보인다
    _continue(jid)
    res = _result(world, {"id": jid})
    assert [x for x in res["lookups"] if x[0] == 2] == [], "끊기기 전에 조회가 없던 끊긴 라운드에서 조회했다"
    assert {s: h for (rr, s), (h, _x) in res["seat"].items() if rr == 2} == {s: o["seat"][(2, s)][0] for s in SEATS[2:]}, \
        "끊긴 라운드의 나머지 좌석이 끊기기 전과 다른 재료로 말했다"
    assert [x for x in res["lookups"] if x[0] == 3], "시험 전제 — 끊긴 라운드 뒤의 라운드는 지금 도구로 조회한다"


# ── 이은 라운드에서 또 끊겨도 앞 시도에서 말한 좌석은 남는다 ─────────────────────────────────────
def test_이은_실행이_같은_라운드에서_또_끊겨도_앞_시도에서_말한_좌석은_다시_부르지_않는다(world, monkeypatch, tmp_path):
    """끊긴 라운드의 조회 · 원 출력은 새 일지에 옮겨 적힌다(라운드 끝 줄은 아니다). 첫 이음이 3라운드에서 한 석을 더 말하게 한 뒤 다시 끊기면, 둘째
    이음은 두 시도에서 말한 두 석을 모두 되살리고 나머지 세 석만 부른다 — 3라운드의 좌석마다 호출은 시도를 통틀어 한 번이다."""
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "a", _cut(snaps, _turn_line(3), nth=1))
    again: list = []
    with _spy(monkeypatch, again):
        first = _continue(jid)
    assert first["resumed"]["todo"] == list(SEATS[1:])
    one = _result(world, {"id": jid})
    assert sorted(s for rr, s in one["seat"] if rr == 3) == sorted(SEATS[1:]), "첫 이음이 3라운드의 나머지만 부르지 않았다"
    lines2 = [rec for rec, _d in again]
    assert [x["k"] for x in lines2][:2] == ["j:head", "e:pre"] and [x for x in lines2 if x["k"] == "e:gather" and x["r"] == 3], \
        "끊긴 라운드의 조회를 새 일지에 옮겨 적지 않았다"
    raws = [(x["r"], x["o"]["persona"]) for x in lines2 if x["k"] == "e:raw"]
    assert len(raws) == len(set(raws)) and len([x for x in lines2 if x["k"] == "e:gather" and x["r"] == 3]) == 1, \
        "되살린 좌석의 원 출력 · 끊긴 라운드의 조회를 새 일지에 두 번 적었다"
    # 둘째 시도의 일지에서 3라운드의 둘째 턴(앞 시도에서 말한 좌석의 턴을 다시 낸 것 다음 — 이 시도에서 처음 말한 좌석)까지 적힌 자리
    world.reset()
    jid2 = _restart(monkeypatch, tmp_path, "b", _cut(again, _turn_line(3), nth=2))
    assert jid2 == jid
    led = _on_disk({"id": jid})
    assert (led["seat_resume"]["spoken"], led["seat_resume"]["todo"]) == (list(SEATS[:2]), list(SEATS[2:])), led["seat_resume"]
    assert len(led["attempts"]) == 1
    out = _continue(jid)
    assert out["resumed"]["mode"] == "in_place" and out["resumed"]["attempt"] == 2, out["resumed"]
    res = _result(world, {"id": jid})
    assert sorted(s for rr, s in res["seat"] if rr == 3) == sorted(SEATS[2:])
    _check_resumed(o, res, 3, spoken=SEATS[:2], gathered=True)
    assert len(res["led"]["attempts"]) == 2


def test_제자리로_줄_섰다가_재기동해도_끊긴_라운드의_말한_좌석은_다시_부르지_않는다(world, monkeypatch, tmp_path):
    """줄에 다시 선 제자리 이어하기가 뜨기 전에 재기동하면 재기동 정리가 고쳐 쓴 일지(머리 + 옮겨 적은 재개 자료)를 다시 읽어 세운다(R2 ·
    _requeue) — 끊긴 라운드의 조회 · 원 출력도 그 안에 있어, 다시 선 잡도 그 라운드에서 말한 좌석을 다시 부르지 않는다."""
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "q", _cut(snaps, _turn_line(3), nth=2))
    delib_jobs.closing(True)                              # 자리가 있어도 띄우지 않는다 — 줄에 선 채로 둔다
    out = _continue(jid, wait=False)
    assert out["status"] == "queued" and out["job_id"] == jid, out
    lines = [json.loads(x) for x in delib_jobs._journal_path(jid).read_bytes().splitlines()]
    assert [(x["k"], (x.get("o") or {}).get("persona")) for x in lines if x.get("r") == 3] == [
        ("e:gather", None), ("e:raw", SEATS[0]), ("e:raw", SEATS[1])], "고쳐 쓴 일지에 끊긴 라운드의 조회 · 원 출력이 없다"
    delib_jobs.closing(False)
    _fresh(monkeypatch, delib_jobs.JOB_DIR)
    m.bind(STUB)
    assert list(delib_jobs._PENDING) == [jid] and delib_jobs._REQUEUED == 1

    async def go():
        delib_jobs._pump()
        await _settle_all()
    asyncio.run(go())
    res = _result(world, {"id": jid})
    assert sorted(s for rr, s in res["seat"] if rr == 3) == sorted(SEATS[2:])
    _check_resumed(o, res, 3, spoken=SEATS[:2], gathered=True)


def test_끊긴_라운드에_앉지_않은_좌석의_원_출력이_있으면_재개_자료를_믿지_않고_요약으로_잇는다(world, monkeypatch, tmp_path):
    """끊긴 라운드의 원 출력은 앉은 좌석(e:pre)의 것이어야 한다 — 아니면 일지가 깨진 것이라 되살린 상태를 믿지 않는다(까닭과 함께 요약 길)."""
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    snap = _cut(snaps, _turn_line(3), nth=2)
    key = next(x for x in snap if x.startswith("journal/"))
    lines = snap[key].split(b"\n")
    i = next(n for n, ln in enumerate(lines) if ln and json.loads(ln).get("k") == "e:raw" and json.loads(ln).get("r") == 3)
    rec = json.loads(lines[i])
    lines[i] = json.dumps({**rec, "o": {**rec["o"], "persona": "ghost-seat"}}, ensure_ascii=False).encode("utf-8")
    jid = _restart(monkeypatch, tmp_path, "ghost", {**snap, key: b"\n".join(lines)})
    sr = _on_disk({"id": jid})["seat_resume"]
    assert sr["ok"] is False and "앉지 않은 좌석의 원 출력" in sr["why"], sr       # 진행 조회도 이어하기와 같은 규칙으로 말한다
    out = _continue(jid)
    assert out["resumed"]["mode"] == "summary" and "앉지 않은 좌석의 원 출력" in out["resumed"]["why"], out["resumed"]


# ── 끊기기 전의 교차심문 표적 ───────────────────────────────────────────────────────────
def test_끊긴_라운드의_나머지_좌석은_끊기기_전의_교차심문_표적으로_말한다(world, monkeypatch, tmp_path):
    """심화 라운드의 반박 표적은 라운드에 한 번 배정한다. 끊긴 라운드가 조회를 끝냈으면 e:gather 에 적힌 배정을 그대로 쓴다 — 이미 말한 좌석과
    나머지 좌석이 한 라운드의 같은 배정으로 말한다. 일지의 배정을 다른 값으로 고쳐 두면 나머지 좌석의 프롬프트가 그 값을 싣는다(다시 세지
    않는다)."""
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    snap = _cut(snaps, _turn_line(2), nth=2)
    key = next(x for x in snap if x.startswith("journal/"))
    lines = snap[key].split(b"\n")
    i = next(n for n, ln in enumerate(lines) if ln and json.loads(ln).get("k") == "e:gather" and json.loads(ln).get("r") == 2)
    g = json.loads(lines[i])
    tamper = {s: [SEATS[(n + 2) % len(SEATS)]] for n, s in enumerate(SEATS)}
    assert g["cross"] and tamper != g["cross"], "시험 전제 — 고친 배정은 엔진이 다시 세는 배정이 아니다"
    lines[i] = json.dumps({**g, "cross": tamper}, ensure_ascii=False).encode("utf-8")
    jid = _restart(monkeypatch, tmp_path, "x", {**snap, key: b"\n".join(lines)})
    _continue(jid)
    res = _result(world, {"id": jid})
    for s in SEATS[2:]:
        assert f"표적({tamper[s][0]})" in res["seat"][(2, s)][1], f"{s} 의 프롬프트가 끊기기 전의 배정을 싣지 않았다"


# ── 끊기기 전에 원장에 실린 라운드 머리 카드는 다시 내지 않는다 ───────────────────────────────────
def test_끊긴_라운드의_직전_라운드_상한_카드는_원장에_한_번만_남는다(world, monkeypatch, tmp_path):
    """좌석 프롬프트 상한이 작으면 라운드마다 머리에서 '좌석 직전 라운드 상한 초과' 카드를 낸다. 끊긴 라운드가 조회를 끝냈거나 좌석이 말했으면
    그 카드는 끊기기 전에 이미 원장에 실렸다 — 이을 때 다시 내지 않는다(같은 입력의 같은 카드다 — 원장의 알림이 끊지 않은 실행과 같다)."""
    monkeypatch.setattr(d, "_seat_ctx", lambda: 300)
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    assert [c["source"] for c in o["led"]["evidence_omitted"]].count("좌석 직전 라운드 상한 초과") == ROUNDS - 1, "시험 전제 — 라운드마다 카드"
    jid = _restart(monkeypatch, tmp_path, "cap", _cut(snaps, _turn_line(3), nth=2))
    _continue(jid)
    res = _result(world, {"id": jid})
    _check_resumed(o, res, 3, spoken=SEATS[:2], gathered=True)


# ── 결정적 차례 — 끝난 차례로 전사를 잇는다 ───────────────────────────────────────────────
def test_되살린_좌석의_턴은_나머지_좌석보다_먼저_한_번씩_다시_나고_원장_전사가_좌석마다_한_번이다(world, monkeypatch, tmp_path):
    """제자리 이어하기는 끊긴 라운드의 턴을 원장에서 빼고(delib_jobs._reopen) 엔진이 되살린 좌석의 턴을 원 출력에서 다시 낸 뒤 나머지 좌석의 턴을
    낸다 — 라운드 전사의 차례는 끝난 차례다(되살린 좌석이 먼저). 원 출력만 적히고 턴은 원장에 닿기 전에 끊긴 좌석도 한 번 나온다."""
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "order", _cut(snaps, _eng("e:raw", 2), nth=3))
    assert [t["persona"] for t in _on_disk({"id": jid})["turns"] if t["round"] == 2] == list(SEATS[:2]), "시험 전제 — 셋째 좌석의 턴은 원장에 없다"
    _continue(jid)
    led = _on_disk({"id": jid})
    assert [t["persona"] for t in led["turns"] if t["round"] == 2] == list(SEATS), "끊긴 라운드의 전사 차례가 끝난 차례가 아니다"
    assert _turn_rows(led) == _turn_rows(o["led"])
    assert [(t["round"], t["persona"]) for t in led["turns"]] == [(t["round"], t["persona"]) for t in o["led"]["turns"]]

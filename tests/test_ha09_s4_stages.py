# 다단 심의 · 시험 설계를 단 안에서 잇기(HA-09 S4) — 해석 설계 2단 · 구축 계획 3단 · 시험 설계의 라운드 안에서 끊고 같은 잡을 이으면 앞 단 · 좌석 전환 · 사내 자산 현황 · 물성 근거 현황 호출이 0 이고 남은 프롬프트가 끊지 않은 실행과 지문까지 같은지 본다
#
# 왜 생겼나. S1 ~ S3 는 한 단 심의(엔진 general)만 라운드 · 좌석 단위로 이었다. 다단 심의는 끝난 단(체크포인트)까지만 건너뛰고 끊긴 단은 새 잡이
# 처음부터 돌았다 — 2단 마지막 라운드에서 끊기면 1단 결정문은 살아도 2단 좌석 전환 · 사내 자산 현황 조회 · 2단의 라운드를 전부 다시 샀다. 시험
# 설계는 엔진 종류가 general 이 아니라 늘 처음부터였다(resume_info). 이제 엔진이 단마다 단 열쇠(sim-1 · sim-2 · sim-3 · tp)로 재개 자료를 적고,
# 단에 들기 전에 단 밖에서 부른 것(좌석 전환 · 사내 자산 현황 · 물성 근거 현황)과 앞 단 산출을 e:stage_in 한 줄로 적는다. 이으면 끊긴 단 안을
# S1 ~ S3 그대로 되살리고, 앞 단 · 단 밖 조회는 그 줄로 건너뛴다(HWAXPortal docs/request-1027/design-ha09-ha20.md A5.2 끝 · A5.3 끝 · A6 S4).
#
# 하네스는 tests/test_ha09_s1_resume.py 의 가짜 위에 단을 가리는 표지 하나를 얹는다 — 엔진의 _deliberation_stream 을 감싸 지금 도는 단(그 단 열쇠,
# 단 사이는 after-<앞 단>, 첫 단 앞은 entry)을 적고, LLM · 도구 · 좌석 자유 조회 · 보고서 호출마다 그 단을 붙인다. 엔진 · 잡 원장 · 진행 일지 ·
# 재기동 정리 · MCP 도구는 진짜다. 오라클은 같은 화두를 끊지 않고 끝까지 돈 실행이고, 끊는 자리는 그 실행의 일지에서 고른 줄 바로 뒤의 디스크
# 꼴이다(KILL 과 같다). 해석 설계 2단의 고정 CAE 좌석은 둘로 줄인다(시험 시간 — 좌석 수는 이 시험이 보는 것이 아니다).
#
#   실행:  .venv/bin/python -m pytest tests/test_ha09_s4_stages.py -q
import asyncio
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import delib_resume  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402
from test_delib_job_queue import ME, YOU, _on_disk  # noqa: E402
from test_delib_ops import _ctx  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import (AFF, GROUPS, OPTS, PRE_KINDS, PRE_TOOLS, Q, ROUNDS, STUB, TAGS, _TOOL_NAMES, _continue,  # noqa: E402
                                 _disk, _FTool, _fresh, _h, _kind, _restart, _settle_all, _turn_rows, world)  # noqa: F401 — world 는 픽스처

_CARD = "끊긴 심의 되살림"
_POST_KINDS = ("chair", "summary", "plain")
# 2단 들기 전의 좌석 전환(발굴 · 역할 복원)과 사내 자산 현황(_asset_snapshot), 시험 설계 들기 전의 물성 근거 현황(_material_evidence_snapshot)이
# 부르는 도구 — 가짜 게이트웨이에 싣는다(없으면 엔진이 부르지 않아 '0' 이 거저 나온다)
_ASSET = ("list_tool_apps", "database_summary", "coverage_gaps", "list_materials", "list_operations", "operation_usage",
          "section_contact_usage", "corpus_summary", "slurm_list_templates")
_MATERIAL = ("database_summary", "coverage_gaps", "property_distribution", "list_materials", "material_usage", "report_corpus",
             "list_property_definitions", "instrument_summary", "measurement_gaps")
_SNAP_TOOLS = tuple(dict.fromkeys(_ASSET + _MATERIAL))
ORDER = {"sim-plan": ["entry", "sim-1", "after-sim-1", "sim-2", "after-sim-2"],
         "build-plan": ["entry", "sim-1", "after-sim-1", "sim-2", "after-sim-2", "sim-3", "after-sim-3"],
         "test-plan": ["entry", "tp", "after-tp"]}
FIXED_CAE = ("xd-cae-modeling", "xd-cae-post")


@pytest.fixture
def w4(world, monkeypatch):
    """S1 가짜 위에 — 지금 도는 단을 적고(w.phase), 호출마다 그 단을 붙인다. 좌석 자유 조회 가짜는 SEATS 밖의 좌석(2단 CAE 좌석 · 시험 설계 고정
    좌석)도 받게 다시 쓴다(결과는 맥락 · 앞 조회 서명 · 그때까지 배운 출처 정수에서 나온다 — 되살린 상태가 하나라도 다르면 다음 프롬프트가 다르다).
    현황 도구는 도구마다 정해진 글로 답한다(그 글이 사람 의견에 붙어 화두 블록에 실린다)."""
    w = world
    w.phase, w.calls, w.tcalls, w.looks, w.reps = "entry", [], [], [], []
    real_llm, real_reset = w.llm_text, w.reset

    async def llm_text(llm, system, human, *img):
        tg, kind = TAGS.get() or {}, _kind(system)
        seat = kind == "seat"
        w.calls.append((w.phase, kind, tg.get("round") if seat else None, tg.get("seat") if seat else None, _h(system, human)))
        return await real_llm(llm, system, human, *img)

    real_answer = w.answer

    def answer(name, args):
        if name in _SNAP_TOOLS:
            return json.dumps({"tool": name, "현황": f"{name} 보유 {_h(name)[:6]}"}, ensure_ascii=False)
        return real_answer(name, args)

    class _T(_FTool):
        async def ainvoke(self, args):
            w.tcalls.append((w.phase, self.name))
            if self.name == "create_report_draft":
                w.reps.append((w.phase, copy.deepcopy(args)))
            return await super().ainvoke(args)

    async def tools(*_a, **_k):
        return {n: _T(w, n) for n in _TOOL_NAMES + _SNAP_TOOLS}

    async def gather(_agent, persona, question, ctx, budget, prior=""):
        r, k = int((TAGS.get() or {}).get("round") or 0), persona["key"]
        seen = sorted(x for x in (app._turn_ids.get() or ()) if x < r * 100000)
        w.looks.append((w.phase, r, k))
        out = json.dumps({"seat": k, "round": r, "part": r * 100000 + int(_h(k)[:3], 16) % 997, "h": _h(k, r, question, ctx, prior, seen)[:16]})
        app._learn_ids(out)
        args = json.dumps({"q": k}, ensure_ascii=False)
        return k, [("search_parts", args, out, f"c-{r}-{k}")], f"- search_parts({args}): {out}", ""

    real_stream = d._deliberation_stream

    async def stream(*a, sk=None, **k):
        w.phase = sk or "?"
        try:
            async for c in real_stream(*a, sk=sk, **k):
                yield c
        finally:
            w.phase = f"after-{sk}"

    def reset():
        real_reset()
        w.phase, w.calls, w.tcalls, w.looks, w.reps = "entry", [], [], [], []

    monkeypatch.setattr(d, "_llm_text", llm_text)
    monkeypatch.setattr(d, "_tools_by_name", tools)
    monkeypatch.setattr(d, "_free_gather_one", gather)
    monkeypatch.setattr(d, "_deliberation_stream", stream)
    monkeypatch.setattr(d, "_SIM_FIXED_CAE", FIXED_CAE)
    monkeypatch.setattr(w, "answer", answer)
    monkeypatch.setattr(w, "reset", reset)
    return w


class _Pos:
    """일지 줄을 차례로 받아 지금 단(마지막 단 표지 — e:stage_in · e:pre)과 단 · 라운드마다 원장에 간 턴 수 · 엔진 줄을 센다."""

    def __init__(self):
        self.sk, self.turns, self.eng = None, {}, []

    def feed(self, rec: dict) -> None:
        k = rec.get("k")
        if k in ("e:pre", "e:stage_in"):
            self.sk = rec.get("sk")
        if isinstance(k, str) and k.startswith("e:"):
            self.eng.append((k, rec.get("sk"), rec.get("r")))
        dd = rec.get("d") or {}
        if k == "delib" and dd.get("kind") == "turn":
            self.turns[(self.sk, dd.get("round"))] = self.turns.get((self.sk, dd.get("round")), 0) + 1


def _run(w, monkeypatch, job: str, opts=None, cuts: dict | None = None, *, lines: list | None = None) -> tuple:
    """끊지 않은 실행 — 잡으로 끝까지. cuts {이름: pred(줄, _Pos)} 의 줄이 처음 맞을 때 그 바로 뒤의 디스크를 뜬다(KILL 꼴). lines 를 주면 일지에
    적힌 (줄, 그 줄까지의 일지 바이트)를 모은다(줄마다 끊어 보는 시험 — 일지는 덧붙이기만 하니 앞부분이 그때의 일지다). (결과, {이름: 디스크})."""
    cuts, got, pos = cuts or {}, {}, _Pos()
    orig = delib_jobs._Journal._write
    buf: list = []

    def spy(self, data, sync):
        orig(self, data, sync)
        rec = json.loads(data)
        pos.feed(rec)
        if lines is not None:
            buf.append(data)
            lines.append((rec, b"".join(buf)))
        for name, pred in cuts.items():
            if name not in got and pred(rec, pos):
                got[name] = _disk()
    monkeypatch.setattr(delib_jobs._Journal, "_write", spy)

    async def go():
        job_rec = delib_jobs.start(STUB, job, Q, groups=list(GROUPS), delib_opts=copy.deepcopy(opts or OPTS), user_email=ME,
                                   owner_aff=AFF)
        await _settle_all()
        return job_rec
    try:
        rec = asyncio.run(go())
    finally:
        monkeypatch.setattr(delib_jobs._Journal, "_write", orig)
    assert set(got) == set(cuts), f"시험 전제 — 끊을 자리를 찾지 못했다: {set(cuts) - set(got)}"
    out = _res(w, rec["id"])
    out["eng"] = list(pos.eng)
    w.reset()
    return out, got


def _res(w, jid: str) -> dict:
    seat = {}
    for ph, kind, r, s, h in w.calls:
        if kind == "seat":
            assert (ph, r, s) not in seat, f"같은 단의 같은 좌석을 한 라운드에 두 번 불렀다 — {(ph, r, s)}"
            seat[(ph, r, s)] = h
    return {"led": _on_disk({"id": jid}), "seat": seat, "other": [(ph, k, h) for ph, k, _r, _s, h in w.calls if k != "seat"],
            "tools": list(w.tcalls), "looks": sorted(w.looks), "reps": copy.deepcopy(w.reps), "restore_seen": list(w.restore_seen)}


def _later(job: str, ph: str, cut: str) -> bool:
    return ORDER[job].index(ph) > ORDER[job].index(cut)


def _check(o: dict, res: dict, job: str, cut: str, first: int, *, spoken=(), gathered: bool = False, post=_POST_KINDS,
           ra: bool = True, cards: int = 1) -> None:
    """이어 돈 실행이 끊지 않은 실행과 같은가 — 끊긴 단(cut) 앞의 단 · 단 사이(좌석 전환 · 현황 조회)는 LLM · 도구 · 조회 호출이 0, 끊긴 단은
    first 라운드부터(그 라운드에서 끊기기 전에 말한 좌석 spoken 은 빼고) 좌석 프롬프트가 지문까지 같고 라운드 전 단계 호출이 0, 그 뒤 단은
    통째로 같다. 끊긴 단의 결정문 뒤는 post(다시 부르는 의장 · 요약 · 쉬운 설명)와 ra(보고서를 다시 저장하나)만큼이다(S3). 원장(결정문 ·
    체크포인트 · 전사 — 단마다 좌석마다 한 번 · 알림 카드)도 같다. cards 는 되살림 카드 수(이은 차례마다 한 장)다."""
    def keep(ph, r=None, s=None, look=False):
        if _later(job, ph, cut):
            return True
        if ph != cut or r is None:
            return False
        return r > first or (r == first and (not gathered if look else s not in spoken))
    before = [ph for ph in ORDER[job] if ORDER[job].index(ph) < ORDER[job].index(cut)]
    assert not [x for x in res["other"] + [(k[0],) for k in res["seat"]] + res["tools"] + res["looks"] if x[0] in before], \
        f"끊긴 단 앞의 단 · 단 사이 일을 다시 했다 — {[x for x in res['tools'] + res['other'] if x[0] in before][:6]}"
    want = {key: h for key, h in o["seat"].items() if keep(*key)}
    assert res["seat"] == want, ("좌석 프롬프트가 끊지 않은 실행과 다르다 — 다른 칸: "
                                 + str(sorted(set(res["seat"]) ^ set(want)) or [k for k in want if res["seat"].get(k) != want[k]]))
    assert res["other"] == [x for x in o["other"] if _later(job, x[0], cut) or (x[0] == cut and x[1] in post)], \
        "라운드 전 단계 · 앞 단의 LLM 을 다시 불렀거나 의장 · 요약 · 쉬운 설명이 다르다"
    assert res["tools"] == [x for x in o["tools"] if _later(job, x[0], cut) or (
        x[0] == cut and x[1] not in PRE_TOOLS and (ra or x[1] != "create_report_draft"))], \
        f"끊긴 단의 라운드 전 단계 도구를 다시 불렀거나 뒤 단의 도구가 다르다 — {res['tools'][:8]}"
    assert res["looks"] == sorted(x for x in o["looks"] if keep(*x, look=True)), "끝난 조회를 다시 했거나 남은 조회가 다르다"
    assert res["reps"] == [x for x in o["reps"] if _later(job, x[0], cut) or (x[0] == cut and ra)], "보고서 블록이 다르다"
    assert all(res["restore_seen"]) and not any(o["restore_seen"]), "엔진이 재개 상태를 보지 못했다(또는 오라클이 봤다)"
    a, b = res["led"], o["led"]
    assert (a["status"], a["decision"], a["result_text"], a["report_id"], a["checkpoint"]) == (
        b["status"], b["decision"], b["result_text"], b["report_id"], b["checkpoint"]), "결정문 · 체크포인트가 끊지 않은 실행과 다르다"
    assert _turn_rows(a) == _turn_rows(b) and len(a["turns"]) == len(b["turns"]), "전사가 끊지 않은 실행과 다르다(단마다 좌석마다 한 번이 아니다)"
    assert [c for c in a["evidence_omitted"] if c["source"] != _CARD] == b["evidence_omitted"], "알림 카드가 두 번 실렸거나 빠졌다"
    assert len([c for c in a["evidence_omitted"] if c["source"] == _CARD]) == cards


def _in(sk: str):
    return lambda rec, pos: rec.get("k") == "e:stage_in" and rec.get("sk") == sk


def _turn_at(sk: str, r: int, n: int):
    """그 단 r 라운드에서 n 번째 턴이 원장에 간 줄 — 앞 n 석이 말했다(가짜는 한 라운드의 좌석을 앉은 차례대로 끝낸다)."""
    return lambda rec, pos: (rec.get("k") == "delib" and (rec.get("d") or {}).get("kind") == "turn" and pos.sk == sk
                             and (rec.get("d") or {}).get("round") == r and pos.turns.get((sk, r)) == n)


def _eng_at(kind: str, sk: str, r: int | None = None):
    return lambda rec, pos: rec.get("k") == kind and rec.get("sk") == sk and (r is None or rec.get("r") == r)


def _seated(jid: str) -> list:
    """끊긴 잡의 원장에 앉은 좌석(마지막 personas 사건 — 끊긴 단의 좌석, 앉은 차례). 가짜는 한 라운드의 좌석을 앉은 차례대로 끝내므로 'k 석이
    말한 뒤' 는 앞 k 석이다."""
    return [x["key"] for x in _on_disk({"id": jid})["seats"]]


# ── 2단 · 3단 · 시험 설계의 라운드 안에서 끊고 이으면 앞 단 · 좌석 전환 · 현황 조회 호출이 0 이다 ─────────────────────────────────
@pytest.mark.parametrize("job, sk, r, k", [("sim-plan", "sim-2", 2, 0), ("sim-plan", "sim-2", 2, 2), ("sim-plan", "sim-2", 4, 3),
                                           ("build-plan", "sim-2", 3, 1), ("build-plan", "sim-3", 2, 2), ("build-plan", "sim-3", 1, 0),
                                           ("test-plan", "tp", 2, 3), ("test-plan", "tp", 3, 0)])
def test_단_안_r라운드에서_끊고_이으면_앞_단_좌석_전환_현황_조회_호출이_0이고_남은_프롬프트가_끊지_않은_실행과_같다(w4, monkeypatch, tmp_path, job, sk, r, k):
    """설계 A6 S4 — 2단 안 r 라운드에서 끊고 이으면 1단 · 좌석 전환 · 사내 자산 현황 호출이 0, 3단 안도 같다(1 · 2단까지 0), 시험 설계는 물성
    근거 현황 호출이 0 이다. k 는 그 라운드에서 끊기기 전에 말한 좌석 수(0 이면 라운드 경계 — 앞 라운드 끝 줄 바로 뒤)다. 끊긴 단 안은 S1 · S2
    그대로 — 말한 좌석은 다시 부르지 않고 나머지만 끊지 않은 실행과 같은 프롬프트로 부른다. 같은 job_id 이고 원장의 전사에는 단마다 좌석마다
    한 번이다(앞 단의 턴을 라운드 번호로 빼지 않는다)."""
    cut = _turn_at(sk, r, k) if k else (_eng_at("e:round_end", sk, r - 1) if r > 1 else _eng_at("e:pre", sk))
    o, got = _run(w4, monkeypatch, job, cuts={"cut": cut})
    before = [ph for ph in ORDER[job] if ORDER[job].index(ph) < ORDER[job].index(sk)]
    pre_tools = {t for ph, t in o["tools"] if ph in before}
    if sk in ("sim-2", "sim-3"):
        assert {"recommend_agents", "get_agent_session", *_ASSET} <= pre_tools, f"시험 전제 — 오라클이 좌석 전환 · 사내 자산 현황을 불렀다: {pre_tools}"
        assert [x for x in o["other"] if x[0] == "sim-1"], "시험 전제 — 오라클의 1단이 LLM 을 불렀다"
    else:
        assert set(_MATERIAL) <= pre_tools, f"시험 전제 — 오라클이 물성 근거 현황을 불렀다: {pre_tools}"
    jid = _restart(monkeypatch, tmp_path, f"{job}-{sk}-{r}-{k}", got["cut"])
    seats = _seated(jid)
    spoken = seats[:k]
    assert len(seats) == len({s for (p, rr, s) in o["seat"] if p == sk and rr == r}), "시험 전제 — 원장의 좌석이 끊긴 단의 좌석이다"
    sr = _on_disk({"id": jid})["seat_resume"]
    assert (sr["ok"], sr["sk"], sr["round"], sr["spoken"], sr["todo"]) == (True, sk, r, spoken, seats[k:]), sr
    out = _continue(jid)
    rs = out["resumed"]
    assert (out["job_id"], rs["mode"], rs["sk"], rs["round"], rs["spoken"], rs["todo"]) == (jid, "in_place", sk, r, spoken, seats[k:]), rs
    assert {"sim-2": "2단(해석 설계) 안에서", "sim-3": "3단(구축 계획) 안에서", "tp": "물성 근거 현황은 다시 조회하지 않고"}[sk] in out["note"], out["note"]
    res = _res(w4, jid)
    _check(o, res, job, sk, r, spoken=spoken, gathered=bool(k) and r < ROUNDS)
    a = res["led"]
    assert a["attempts"][0]["sk"] == sk and a["attempts"][0]["round"] == r and not delib_jobs._journal_path(jid).exists()
    card = next(c["text"] for c in a["evidence_omitted"] if c["source"] == _CARD)
    assert {"sim-2": "좌석 전환 · 사내 자산 현황도 끊기기 전의 것을 되살렸다", "sim-3": "앞 단(1 · 2단)도 끊기기 전의 것을 되살렸다",
            "tp": "물성 근거 현황도 끊기기 전의 것을 되살렸다"}[sk] in card, card


# ── 단 사이 승계를 줄인 카드는 끊기기 전에 원장에 실렸다 — 되살릴 때 다시 내지 않는다 ─────────────────────────────────────────
@pytest.mark.parametrize("sk, r, k", [("sim-2", 2, 2), ("sim-3", 3, 1)])
def test_단_사이_승계를_줄인_카드는_되살릴_때_다시_내지_않는다(w4, monkeypatch, tmp_path, sk, r, k):
    """앞 단 결정문이 다음 단 좌석에 싣는 몫(1단 → 2단은 이어하기 요약 몫, 2단 → 3단은 20,000자 바닥)을 넘으면 단에 들기 전에 카드 한 장으로
    알린다(_carry_cut_cards — 잡 원장의 evidence_omitted). 그 단 안에서 끊겨 이으면 그 카드는 이미 원장에 있다 — 다시 내지 않는다(S1-D14 와
    같은 원칙). 뒤 단의 카드는 이은 실행이 처음 내는 것이라 한 번 낸다."""
    real = d._llm_text

    async def long_chair(llm, system, human, *img):
        out = await real(llm, system, human, *img)
        return out + "\n" + "- 근거 " * 4200 if _kind(system) == "chair" else out      # 21,000자 넘게 — 두 단 사이 모두 줄인다
    monkeypatch.setattr(d, "_llm_text", long_chair)
    o, got = _run(w4, monkeypatch, "build-plan", cuts={"cut": _turn_at(sk, r, k)})
    srcs = [c["source"] for c in o["led"]["evidence_omitted"]]
    assert srcs.count("다단 승계 상한 초과") == 2, f"시험 전제 — 두 단 사이에서 승계를 줄였다: {srcs}"
    jid = _restart(monkeypatch, tmp_path, f"cards-{sk}", got["cut"])
    seats = _seated(jid)
    out = _continue(jid)
    assert out["resumed"]["mode"] == "in_place" and out["resumed"]["sk"] == sk, out["resumed"]
    _check(o, _res(w4, jid), "build-plan", sk, r, spoken=seats[:k], gathered=r < ROUNDS)


# ── 1단 안 · 1단 결정문 뒤에서 끊기면 1단을 되살려 맺고 단 사이부터는 끊지 않은 실행 그대로 돈다 ─────────────────────────────────
@pytest.mark.parametrize("at", ["r3", "e:decision", "e:report_saved"])
def test_1단_안에서_끊기면_1단을_되살려_맺고_좌석_전환부터_끊지_않은_실행과_같게_돈다(w4, monkeypatch, tmp_path, at):
    """끊긴 단이 1단이면 앞 단이 없다 — 1단 안을 S1 ~ S3 그대로 되살리고(3라운드에서 두 석이 말한 뒤 · 의장 결정문 뒤 · 보고서 저장 뒤), 1단이
    끝난 뒤의 좌석 전환 · 사내 자산 현황 · 2단은 처음 하는 일이라 끊지 않은 실행과 똑같이 부른다. 1단의 양보 불가 조항(다음 단에 넘기는 것)은
    수렴 라운드의 턴을 낼 때 모으는데, 수렴 라운드를 되살리면 턴을 다시 내지 않는다 — 같은 식으로 모아야 1단 체크포인트와 2단 좌석 프롬프트가
    끊지 않은 실행과 같다(결정문 뒤에서 끊긴 두 자리가 그것을 본다)."""
    cut = _turn_at("sim-1", 3, 2) if at == "r3" else _eng_at(at, "sim-1")
    o, got = _run(w4, monkeypatch, "sim-plan", cuts={"cut": cut})
    jid = _restart(monkeypatch, tmp_path, f"s1-{at}", got["cut"])
    first = 3 if at == "r3" else ROUNDS + 1
    sr = _on_disk({"id": jid})["seat_resume"]
    spoken = _seated(jid)[:2] if at == "r3" else ()      # 이기 전의 원장 — 1단 좌석
    assert (sr["ok"], sr["sk"], sr["round"], sr["spoken"]) == (True, "sim-1", first, list(spoken)), sr
    out = _continue(jid)
    assert out["resumed"]["mode"] == "in_place" and "1단(메커니즘) 안에서" in out["note"], out["note"]
    res = _res(w4, jid)
    _check(o, res, "sim-plan", "sim-1", first, spoken=spoken, gathered=at == "r3",
           post={"r3": _POST_KINDS, "e:decision": ("summary", "plain"), "e:report_saved": ()}[at], ra=at != "e:report_saved")
    nn = res["led"]["checkpoint"]["nn_a"]
    assert nn and nn == o["led"]["checkpoint"]["nn_a"], "1단 체크포인트의 양보 불가 조항이 끊지 않은 실행과 다르다"


# ── 그 단의 라운드 전 단계(e:stage_in 뒤 · e:pre 전)에서 끊기면 되살릴 것이 없다 — 요약 길이 체크포인트로 끝난 단을 건너뛴다 ───────────────
def test_2단의_라운드_전_단계에서_끊기면_재개_요약이_까닭을_말하고_요약_길이_체크포인트로_1단을_건너뛴다(w4, monkeypatch, tmp_path):
    """2단에 든 줄(e:stage_in) 뒤 · 2단 좌석이 앉기 전(e:pre)에 끊겼다 — 그 단에는 되살릴 재개 자료가 없다(S1 의 '라운드 전 단계' 와 같다). 진행
    조회의 재개 요약과 이어하기의 일지 읽기가 같은 까닭으로 제자리 이어하기를 하지 않고, 요약 길(새 잡)이 원장의 체크포인트로 1단을 건너뛴다 —
    1단 LLM 호출 0 이다(종전 4c67229 그대로)."""
    o, got = _run(w4, monkeypatch, "sim-plan", cuts={"cut": _in("sim-2")})
    jid = _restart(monkeypatch, tmp_path, "entered", got["cut"])
    led = _on_disk({"id": jid})
    sr = led["seat_resume"]
    assert sr["ok"] is False and "2단(해석 설계)의 라운드 전 단계에서 끊겨" in sr["why"] and led["checkpoint"]["stage"] == "sim-1", sr
    assert led["error"] == delib_jobs.INTERRUPTED_MSG
    out = _continue(jid)
    r = out["resumed"]
    assert (r["mode"], r["checkpoint"], r["rounds_done"]) == ("summary", "sim-1", 0) and out["job_id"] != jid, r
    assert "2단(해석 설계)의 라운드 전 단계에서 끊겨" in r["why"] and "끝난 1단(메커니즘)은 건너뛰고" in out["note"], out
    res = _res(w4, out["job_id"])
    assert not [x for x in w4.calls if x[0] == "sim-1"] and res["led"]["status"] == "done", "요약 길이 1단을 다시 돌았다"
    # 체크포인트로 1단을 건너뛴 새 잡은 1단 스트림을 열지 않는다 — 좌석 전환 · 현황 조회는 첫 단 앞(entry)에서 돈다
    assert {t for ph, t in res["tools"] if ph == "entry"} >= {"recommend_agents", *_ASSET}, "시험 전제 — 단 사이는 새 잡에서 다시 한다"
    # 2단은 처음부터지만 끊지 않은 실행과 같은 입력(체크포인트의 1단 결정문 · 같은 좌석 전환 · 같은 현황)이다 — 결정문 끝의 보고서 안내만 다르다
    # (요약 길은 앞 잡의 보고서에 이어 붙인다 — 종전 그대로)
    assert res["seat"] == {key: h for key, h in o["seat"].items() if key[0] == "sim-2"}, "체크포인트로 이은 2단이 끊지 않은 실행과 다르다"


def test_끝난_단_뒤_다음_단에_들기_전에_끊기면_되살리지_않고_요약_길이_체크포인트로_건너뛴다(w4, monkeypatch, tmp_path):
    """1단이 끝나 체크포인트가 원장에 간 뒤 · 2단에 들기 전(좌석 전환 · 사내 자산 현황 조회 도중)에 끊겼다 — 되살릴 단 안이 없다. 끝난 1단을
    되살려 다시 맺어도 부르는 것은 없지만, 그 뒤에 다시 하는 단 사이 일이 끊기기 전에 낸 카드(다단 승계 상한 초과)를 또 낼 수 있다. 진행 조회와
    이어하기가 같은 까닭으로 제자리를 하지 않고, 요약 길이 체크포인트로 1단을 건너뛴다(같은 비용 — 1단 호출 0)."""
    _o, got = _run(w4, monkeypatch, "sim-plan", cuts={"cut": lambda rec, pos: rec.get("k") == "delib"
                                                     and (rec.get("d") or {}).get("kind") == "checkpoint"})
    jid = _restart(monkeypatch, tmp_path, "between", got["cut"])
    led = _on_disk({"id": jid})
    sr = led["seat_resume"]
    assert sr["ok"] is False and "1단(메커니즘)은 끝났고 다음 단에 들기 전" in sr["why"] and led["checkpoint"]["stage"] == "sim-1", sr
    out = _continue(jid)
    r = out["resumed"]
    assert (r["mode"], r["checkpoint"]) == ("summary", "sim-1") and "다음 단에 들기 전" in r["why"] and out["job_id"] != jid, r
    assert not [x for x in w4.calls if x[0] == "sim-1"] and _on_disk({"id": out["job_id"]})["status"] == "done"


# ── 다단 일지의 줄마다 — 재개 요약(Tracker)과 이어하기의 일지 읽기(parse)가 같은 단 · 라운드 · 좌석을 말한다 ─────────────────────────
@pytest.mark.parametrize("job", ["build-plan", "test-plan"])
def test_다단_일지의_어느_줄에서_끊겨도_재개_요약과_일지_읽기가_같은_단_라운드_좌석을_말한다(w4, monkeypatch, job):
    """KILL 은 아무 줄 뒤에서나 난다. 줄마다 그때의 일지로 — 진행 조회의 재개 요약(접기가 센 Tracker)과 이어하기의 일지 읽기(parse)가 같은 규칙으로
    되살린다 · 못 한다를 말하고(S1-D13), 되살리면 같은 단 · 처음 부를 라운드 · 말한 좌석 · 나머지 좌석을 말한다. 끊긴 단은 마지막 단 표지(e:stage_in
    · e:pre)의 단이고, e:stage_in 뒤 · e:pre 전이면 되살리지 않는다. 원장에 그 단의 턴이 있는 좌석은 모두 '말한 좌석' 에 든다(원 출력이 먼저 적힌다)."""
    lines: list = []
    o, _got = _run(w4, monkeypatch, job, lines=lines)
    jid = lines[0][0]["id"]
    stages = delib_resume.STAGES[delib_jobs.JOBS[job]["engine"]]
    led0 = {"id": jid, "job": job, "kind": job, "status": "running", "stage": "start", "turns": [], "seats": [], "opts": {}}
    p = delib_jobs._journal_path(jid)
    p.parent.mkdir(parents=True, exist_ok=True)
    assert delib_jobs._write_args(jid, {"job_kind": job, "question": Q, "groups": list(GROUPS), "opts": {}, "user_email": ME})
    last = None
    seen_ok, between = set(), set()
    for i, (rec, blob) in enumerate(lines):
        p.write_bytes(blob)
        led = copy.deepcopy(led0)
        t = delib_resume.Tracker()
        delib_jobs._fold_journal(led, t)
        delib_jobs._mark_interrupted(led, t)          # 재기동 정리 · 내려갈 때와 같은 길 — 진행 조회의 재개 요약
        sr = led["seat_resume"]
        res, why = delib_jobs.resume_load(led, groups=None)       # 이어하기와 같은 길 — 원장의 체크포인트도 본다
        assert sr["ok"] is (res is not None), (i, rec.get("k"), rec.get("sk"), sr, why)
        if res is None:
            assert sr["why"].startswith(why) or why in sr["why"], (i, sr["why"], why)
            if "다음 단에 들기 전" in why:
                between.add((led.get("checkpoint") or {}).get("stage"))
            continue
        info = res.info
        assert (sr["sk"], sr["round"], sr["spoken"], sr["todo"]) == (info["sk"], info["round"], info["spoken"], info["todo"]), (i, sr, info)
        tf = led.get("turns_from") or 0
        turned = {x["persona"] for x in led["turns"][tf:] if x.get("round") == sr["round"]}
        assert turned <= set(sr["spoken"]), f"{i}번째 줄 뒤 — 원장에 턴이 있는 좌석 {turned - set(sr['spoken'])} 의 원 출력이 일지에 없다"
        seen_ok.add(sr["sk"])
        last = (rec.get("k"), sr["sk"])
    p.unlink()
    delib_jobs._drop_args(jid)
    assert seen_ok == set(stages), f"시험 전제 — 단마다 되살릴 수 있는 자리가 있었다: {seen_ok}"
    assert between == ({"sim-1", "sim-2"} if job == "build-plan" else set()), f"단 사이(체크포인트 뒤 · 다음 단에 들기 전) 자리: {between}"
    assert last is not None


# ── 진행 조회 · 요약 길 — rounds_done 은 단이 하나인 잡(main · tp)만 센다 ─────────────────────────────────────────
def test_시험_설계는_남의_이어하기_요약_길에서도_끝난_라운드_뒤부터_잇는다(w4, monkeypatch, tmp_path):
    """resume_info 의 엔진 general 제한을 단 열쇠로 바꿨다 — 단이 하나인 시험 설계(tp)는 끝난 라운드를 세어, 남의 이어하기(요약 길)도 끝난 라운드
    뒤부터 잇는다(종전엔 늘 처음부터 — 1라운드 '초기 입장' 부터 다시 샀다). 남은 재개 자료를 읽지 않는다(HA-06)."""
    _o, got = _run(w4, monkeypatch, "test-plan", cuts={"cut": _eng_at("e:round_end", "tp", 2)})
    jid = _restart(monkeypatch, tmp_path, "tp-sum", got["cut"])
    st = asyncio.run(m.deliberate_status(jid, ctx=_ctx(ME, AFF)))
    assert (st["resume"]["rounds_done"], st["resume"]["seat_resume"]["sk"], st["resume"]["seat_resume"]["round"]) == (2, "tp", 3), st["resume"]
    read = []
    real = delib_resume.parse
    monkeypatch.setattr(delib_resume, "parse", lambda *a, **k: read.append(1) or real(*a, **k))
    out = _continue(jid, user=YOU)                        # 같은 소속의 남 — 재개 자료를 읽지 않고 요약으로
    r = out["resumed"]
    assert (r["mode"], r["foreign"], r["rounds_done"], r["rounds_start_at"]) == ("summary", True, 2, 3) and read == [], r
    new = _on_disk({"id": out["job_id"]})
    assert new["opts"]["rounds_so_far"] == 2 and "끝난 2라운드" in new["opts"]["continue_summary"], new["opts"]
    assert {t["display_round"] for t in new["turns"]} == {3, 4}, "끝난 라운드 뒤부터 잇지 않았다"


def test_다단_심의의_진행_조회는_끊긴_단을_싣고_요약_길의_끝난_라운드는_세지_않는다(w4, monkeypatch, tmp_path):
    """다단 심의는 단 사이 승계가 엔진 안에 있어 요약 길(새 잡)이 끝난 라운드로 잇지 않는다(rounds_done 0 — 체크포인트로 끝난 단을 건너뛰고 끊긴
    단을 처음부터). 끊긴 단 안을 잇는 것은 제자리 이어하기다 — 진행 조회의 재개 요약이 그 단(sk)과 라운드를 싣는다."""
    _o, got = _run(w4, monkeypatch, "build-plan", cuts={"cut": _eng_at("e:round_end", "sim-2", 2)})
    jid = _restart(monkeypatch, tmp_path, "bp-sum", got["cut"])
    st = asyncio.run(m.deliberate_status(jid, ctx=_ctx(ME, AFF)))
    assert (st["resume"]["rounds_done"], st["resume"]["checkpoint"]) == (0, "sim-1"), st["resume"]
    assert (st["resume"]["seat_resume"]["sk"], st["resume"]["seat_resume"]["round"]) == ("sim-2", 3), st["resume"]
    assert st["error"] == delib_jobs.INTERRUPTED_MSG_EXACT and "끊긴 단 안에서 잇는다" in st["error"]


# ── 같은 단에서 두 번 끊겨도 단에 든 줄이 옮겨 적혀 또 그 단 안에서 잇는다 ─────────────────────────────────────────
def test_이은_실행이_같은_단에서_또_끊겨도_단에_든_재개_자료가_남아_다시_그_단_안에서_잇는다(w4, monkeypatch, tmp_path):
    """제자리 이어하기는 일지를 머리와 되살릴 줄로 고쳐 쓴다 — 단에 든 줄(e:stage_in)도 옮겨 적는다. 이은 실행이 같은 2단에서 또 끊기면 그 줄이
    남아 다시 2단 안에서 잇고(1단 · 좌석 전환 · 현황 조회 0), 끝은 끊지 않은 실행과 같다(시도 둘)."""
    o, got = _run(w4, monkeypatch, "build-plan", cuts={"cut": _turn_at("sim-2", 2, 2)})
    jid = _restart(monkeypatch, tmp_path, "twice-a", got["cut"])
    pos, second, orig = _Pos(), {}, delib_jobs._Journal._write
    pos.sk = "sim-2"

    def spy(self, data, sync):
        orig(self, data, sync)
        rec = json.loads(data)
        pos.feed(rec)
        if not second and rec.get("k") == "e:round_end" and rec.get("sk") == "sim-2" and rec.get("r") == 3:
            second["disk"] = _disk()
    monkeypatch.setattr(delib_jobs._Journal, "_write", spy)
    try:
        _continue(jid)
    finally:
        monkeypatch.setattr(delib_jobs._Journal, "_write", orig)
    w4.reset()
    root2 = tmp_path / "twice-b"
    _fresh(monkeypatch, root2)
    from test_ha09_s1_resume import _lay
    _lay(second["disk"], root2)
    m.bind(STUB)
    lines = [json.loads(x) for x in delib_jobs._journal_path(jid).read_bytes().splitlines()]
    assert [x["k"] for x in lines][:3] == ["j:head", "e:stage_in", "e:pre"] and lines[1]["sk"] == "sim-2", "단에 든 줄을 옮겨 적지 않았다"
    sr = _on_disk({"id": jid})["seat_resume"]
    assert (sr["ok"], sr["sk"], sr["round"], sr["spoken"]) == (True, "sim-2", 4, []), sr
    out = _continue(jid)
    assert out["job_id"] == jid and out["resumed"]["mode"] == "in_place" and out["resumed"]["sk"] == "sim-2"
    res = _res(w4, jid)
    _check(o, res, "build-plan", "sim-2", 4, cards=2)
    assert len(res["led"]["attempts"]) == 2 and [x["sk"] for x in res["led"]["attempts"]] == ["sim-2", "sim-2"]


def test_다단_제자리_이어하기로_줄_섰다가_재기동해도_단에_든_재개_자료로_다시_선다(w4, monkeypatch, tmp_path):
    """줄에 다시 선 제자리 이어하기가 뜨기 전에 서버가 재기동하면 재기동 정리가 시작 인자 파일로 다시 세우며 재개 자료를 다시 읽는다(_requeue) —
    단에 든 줄(e:stage_in)도 옮겨 적혀 있어 그대로 2단 안에서 잇는다."""
    o, got = _run(w4, monkeypatch, "sim-plan", cuts={"cut": _turn_at("sim-2", 3, 1)})
    jid = _restart(monkeypatch, tmp_path, "queued", got["cut"])
    seats = _seated(jid)
    delib_jobs.closing(True)                              # 자리가 있어도 띄우지 않는다 — 줄에 선 채로 둔다
    try:
        out = _continue(jid, wait=False)
    finally:
        delib_jobs.closing(False)
    assert out["status"] == "queued" and out["job_id"] == jid and out["resumed"]["sk"] == "sim-2", out
    _fresh(monkeypatch, delib_jobs.JOB_DIR)
    m.bind(STUB)
    assert list(delib_jobs._PENDING) == [jid] and delib_jobs._REQUEUED == 1

    async def go():
        delib_jobs._pump()
        await _settle_all()
    asyncio.run(go())
    _check(o, _res(w4, jid), "sim-plan", "sim-2", 3, spoken=seats[:1], gathered=True)


# ── 재개 자료가 맞지 않으면 쓰지 않는다 — 단 열쇠 · 단에 든 줄 ─────────────────────────────────────────────────
@pytest.mark.parametrize("case", ["다른 단", "단에 든 줄 없음", "단에 든 줄 꼴"])
def test_단_열쇠나_단에_든_줄이_맞지_않으면_되살리지_않고_까닭과_함께_요약으로_잇는다(w4, monkeypatch, tmp_path, case):
    o, got = _run(w4, monkeypatch, "sim-plan", cuts={"cut": _eng_at("e:round_end", "sim-2", 1)})
    snap = dict(got["cut"])
    key = next(k for k in snap if k.startswith("journal/"))
    lines = snap[key].split(b"\n")
    for i, ln in enumerate(lines):
        rec = json.loads(ln) if ln else {}
        if case == "다른 단" and rec.get("k") == "e:round_end" and rec.get("sk") == "sim-2":
            lines[i] = json.dumps({**rec, "sk": "main"}, ensure_ascii=False).encode()
        elif case == "단에 든 줄 없음" and rec.get("k") == "e:stage_in":
            lines[i] = json.dumps({"t": rec["t"], "k": "status", "d": {"step": "지운 줄"}}, ensure_ascii=False).encode()
        elif case == "단에 든 줄 꼴" and rec.get("k") == "e:stage_in":
            lines[i] = json.dumps({**rec, "decision_a": ""}, ensure_ascii=False).encode()
    snap[key] = b"\n".join(lines)
    jid = _restart(monkeypatch, tmp_path, case, snap)
    sr = _on_disk({"id": jid})["seat_resume"]
    why = {"다른 단": "재개 자료의 단이 이 잡의 종류와 다르다", "단에 든 줄 없음": "2단(해석 설계)에 들 때의 재개 자료(e:stage_in)가 없다",
           "단에 든 줄 꼴": "재개 자료(e:stage_in)의 꼴이 이 판과 다르다"}[case]
    if case != "단에 든 줄 꼴":                      # 꼴은 일지 읽기만 본다(e:pre 의 칸 꼴과 같다 — 진행 조회는 줄이 있는지만 센다)
        assert sr["ok"] is False and why in sr["why"], sr
    out = _continue(jid)
    r = out["resumed"]
    assert r["mode"] == "summary" and why in r["why"] and r["checkpoint"] == "sim-1", r


# ── 재개 상태가 없으면 지금 길 그대로 ─────────────────────────────────────────────────
@pytest.mark.parametrize("job", ["sim-plan", "build-plan", "test-plan"])
def test_끊지_않은_다단_시험_설계는_일지를_써도_사건과_프롬프트가_웹_길과_바이트까지_같다(w4, monkeypatch, job):
    """다단 심의 · 시험 설계에도 단 열쇠를 넘겨 잡은 재개 자료를 적는다 — 재개 상태가 없으면 엔진은 아무것도 바꾸지 않는다. 잡(일지에 쓴다)과 웹
    심의(일지가 없다)가 같은 사건 · 같은 프롬프트 · 같은 도구 호출을 낸다. 일지에는 단마다 e:pre 와, 2단 · 3단 · 시험 설계에 든 줄(e:stage_in)이
    적혔다(시험 전제)."""
    seen: list = []
    real = delib_jobs._parse_sse

    def spy(chunk):
        seen.append(chunk)
        return real(chunk)
    monkeypatch.setattr(delib_jobs, "_parse_sse", spy)
    o, _got = _run(w4, monkeypatch, job)
    job_events, job_calls, job_tools = list(seen), list(o["other"]), list(o["tools"])
    stages = delib_resume.STAGES[delib_jobs.JOBS[job]["engine"]][:3 if job == "build-plan" else 2 if job == "sim-plan" else 1]
    assert [sk for k, sk, _r in o["eng"] if k == "e:pre"] == list(stages), o["eng"]
    assert [sk for k, sk, _r in o["eng"] if k == "e:stage_in"] == [x for x in stages if x in delib_resume.STAGE_IN]
    assert [sk for k, sk, _r in o["eng"] if k == "e:round_end"] == [x for x in stages for _ in range(ROUNDS)]

    async def web():
        entry = {"sim-plan": d.run_sim_deliberation, "build-plan": d.run_sim_deliberation, "test-plan": d.run_test_plan}[job]
        opts = {**copy.deepcopy(OPTS), **({"build_plan": 1} if job == "build-plan" else {})}
        return [c async for c in entry(STUB, Q, list(GROUPS), opts, ME)]
    web_events = asyncio.run(web())
    assert web_events == job_events, "재개 자료를 쓰는 잡과 웹 심의의 사건이 다르다 — 갈고리가 지금 길을 바꿨다"
    web_seat = {(ph, r, s_): h for ph, k, r, s_, h in w4.calls if k == "seat"}
    assert [(ph, k, h) for ph, k, _r, _s, h in w4.calls if k != "seat"] == job_calls and web_seat == o["seat"], "프롬프트가 다르다"
    assert list(w4.tcalls) == job_tools, "도구 호출이 다르다"
    assert not any(w4.restore_seen) and not any(o["restore_seen"])

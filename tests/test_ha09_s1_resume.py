# 끊긴 심의를 라운드 경계에서 그대로 잇기(HA-09 S1) — 결정적 가짜 LLM(프롬프트 지문 → 답)으로 끊지 않은 실행을 오라클로 삼아, 끊긴 자리에서 같은 잡을 이으면 남은 라운드의 좌석 프롬프트가 지문까지 같고 라운드 전 단계의 LLM · 도구 호출이 0 인지 본다
#
# 왜 생겼나. 진행 일지(S0)로 끊긴 잡의 원장에 끝난 라운드가 남아도, 이어하기는 끝난 라운드의 마지막 입장을 요약으로 실은 **새 심의**였다(R3 — 새
# 잡의 1라운드는 '초기 입장' 이라 원래 라운드 구조를 잇지 않는다). 라운드 전 단계(VOC · 선주입 · 지정 도구 · 발굴 · 역할 복원 · 지식카드)를 다시
# 부르고 그 결과가 달라질 수 있었다. 이제 엔진이 재개 자료(e:pre · e:gather · e:raw · e:round_end)를 같은 일지에 덧붙이고, 주인이 바꾼 값 없이
# 이으면 같은 잡이 끝난 라운드를 되살려 끊긴 라운드부터 돈다(HWAXPortal docs/request-1027/design-ha09-ha20.md A5.2 ~ A5.5 · A6 S1).
#
# 가짜는 바깥에만 있다 — LLM(_llm_text) · 게이트웨이 도구(_tools_by_name) · 좌석 자유 조회(_free_gather_one) · 호출 추적 표지(call_trace.tag —
# 꺼진 판에서는 아무것도 안 해 가짜가 라운드를 모른다). 엔진 · 잡 원장 · 진행 일지 · 재기동 정리 · MCP 도구는 진짜다. 오라클은 같은 화두를 끊지
# 않고 끝까지 돈 실행이고, 끊는 자리는 그 실행의 일지가 한 줄 적힐 때마다 뜬 디스크 꼴이다(KILL 과 같다 — 죽을 때 디스크에 있던 바이트 그대로).
# 정상 종료(TERM) 꼴은 좌석 호출 하나를 붙잡아 둔 채 delib_jobs.shutdown 으로 내린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_ha09_s1_resume.py -q
import asyncio
import contextvars
import copy
import hashlib
import inspect
import json
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import call_trace  # noqa: E402
import delib_jobs  # noqa: E402
import delib_resume  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_job_queue import ME, YOU, _caps, _on_disk, _tick  # noqa: E402
from test_delib_ops import _ctx  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

Q = "보스 간극 축소가 낙하 충격에서 힌지 크랙을 키우는가"
GROUPS = ["CAEG", "plat:dynaforge"]
AFF = "CAEG"
ROUNDS = 4
OPTS = {"rounds": ROUNDS, "voc": "always", "evidence_prepass": 1, "tools": ["list_parts"], "free_tools": 1,
        "persona_knowledge": 1, "cross_exam": 1, "rebut_quote": 0, "anchor": 1, "chair_bestof": 1, "modifiers": ["premortem"],
        "options": ["A안 — 리브 0.8 mm 로 되돌림", "B안 — 보스 R 확대"], "human_note": "현장 의견 — 리브 두께부터 본다",
        "evidence": [{"source": "측정", "tool": "drop_test", "result": "낙하 1.2 m 에서 보스 뿌리 크랙 3/10 발생"}],
        "save_report": 1, "parse_retries": "두 번"}      # 못 읽는 값 — 라운드 전 단계의 '좌석에 주지 않은' 알림 카드가 하나 난다(되살릴 때 두 번 실리면 안 된다)
STUB = SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None))
SEATS = ("mech-hinge", "rel-drop", "pcb-warp", "therm-sink", "mat-adhesive")
PRE_KINDS = ("counter", "planner", "voc", "prepass")
PRE_TOOLS = ("recommend_agents", "get_agent_session", "agent_search", "list_parts", "alert_check", "get_top_issues", "query_voc",
             "hybrid_search")
_KIND_MARK = {1: "당신의 관점(lens", 2: "수용(concede)", 3: "수용(concede)", 4: "최종 입장(final_position"}

TAGS: contextvars.ContextVar = contextvars.ContextVar("s1_tags", default=None)


def _tag(**ids) -> None:
    """call_trace.tag 자리 — 라운드 · 좌석을 이 시험의 문맥 변수에 단다(꺼진 호출 추적은 아무것도 안 해 가짜가 라운드를 모른다)."""
    cur = dict(TAGS.get() or {})
    for k, v in ids.items():
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = v
    TAGS.set(cur)


def _h(*parts) -> str:
    return hashlib.sha256("\x00".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _kind(system: str) -> str:
    for k, mark in (("seat", "전문가입니다. 전문 영역"), ("counter", "좌석 구성을 검토하는 조정자"), ("planner", "도구 호출 계획자"),
                    ("voc", "심의 준비 보조자입니다. 반드시 유효한 JSON"), ("prepass", "주어진 검색 결과에서만"),
                    ("chair", "심의체 의장입니다. 한국어"), ("summary", "군더더기 없이"), ("plain", "비전문가에게 설명")):
        if mark in system:
            return k
    return "other"


class _World:
    """가짜 바깥 — 답은 프롬프트의 지문에서 나온다(같은 프롬프트면 같은 답 — 다음 라운드의 프롬프트가 앞 답을 싣으므로 한 글자만 달라도 뒤가 다
    달라진다). 무엇을 몇 번 불렀는지와 좌석 프롬프트를 적는다."""

    def __init__(self):
        self.reset()
        self.hold = None            # (라운드, 좌석) — 그 좌석의 발언 호출을 붙잡아 둔다(정상 종료 꼴)
        self.held = None
        self.slow = None            # (라운드, 좌석) — 그 좌석이 그 라운드에서 맨 나중에 끝난다(나머지는 앉은 차례대로 곧바로 끝난다)
        self.fail = set()           # {(라운드, 좌석)} — 그 좌석의 발언 호출이 예외로 실패한다(프롬프트는 적는다)

    def reset(self):
        self.llm, self.tools, self.lookups, self.reports, self.restore_seen = [], [], [], [], []

    async def llm_text(self, _llm, system, human, *_img):
        tg = TAGS.get() or {}
        kind, h = _kind(system), _h(system, human)
        seat = tg.get("seat") if kind == "seat" else None
        self.llm.append((kind, tg.get("round"), seat, h, human if kind == "seat" else ""))
        if kind == "seat":
            self.restore_seen.append(delib_resume.restore.get() is not None)
            if self.hold == (tg.get("round"), seat):
                self.held.set()
                await asyncio.Event().wait()        # 취소될 때까지 붙잡는다
            if (tg.get("round"), seat) in self.fail:
                raise RuntimeError(f"가짜 LLM 실패 — {seat}")
            if self.slow == (tg.get("round"), seat):
                for _ in range(5):                  # 나머지 좌석은 첫 걸음에 끝난다 — 몇 바퀴 늦게 끝내 그 라운드의 맨 끝이 된다
                    await asyncio.sleep(0)
            return json.dumps({
                "lens": f"{seat} 관점 {h[:10]} — 보스 뿌리 응력이 낙하 각도에 따라 달라진다", "reads": [f"(경험칙) {seat} 해석 {h[10:16]}"],
                "recommendation": f"{seat} 권장 {h[16:24]}", "concerns": [f"우려 {h[24:28]}", f"우려 {h[28:32]}"],
                "position_short": f"{seat} 입장 {h[:6]}", "concede": [f"수용 {h[:5]}"],
                "rebut": [{"target": "x", "quote": f"인용 {h[5:20]}", "counter": f"반박 {h[20:30]}", "basis": "낙하 응력"}],
                "deepen": f"{seat} 심화 {h[:12]} — 리브가 얇아 하중이 보드로 간다", "final_position": f"{seat} 최종 {h[:12]}",
                "non_negotiable": f"{seat} 조항 {h[:4]}", "vote": "(1) A안", "stance": "동의"}, ensure_ascii=False)
        return {"counter": "열 방출 경로\n접착 계면", "planner": '{"q": "보스"}',
                "voc": '{"relevant": true, "reason": "힌지 크랙과 연관"}',
                "prepass": f"- 보스 뿌리 응력 120 MPa (출처: hybrid_search) {h[:8]}",
                "chair": f"## 의사결정문 {h[:12]}\n- (1) A안을 고른다 — 리브를 되돌린다 [R4·mech-hinge]",
                "summary": f"- 핵심 {h[:12]}", "plain": f"### 한마디로\n리브를 되돌린다 {h[:12]}"}.get(kind, "{}")

    def answer(self, name: str, args: dict) -> str:
        if name == "recommend_agents":
            keys = SEATS[:3] if args.get("q") == Q else SEATS[3:]
            return json.dumps({"agents": [{"agent_type": k, "name": k, "score": 0.9} for k in keys]})
        if name == "get_agent_session":
            k = str(args.get("agent_type"))
            return json.dumps({"data": {"agent_type": k, "name": k, "description": f"{k} 전문가 — 역할 원문 {_h(k)[:8]} · 표준 {41 + len(k)}"}},
                              ensure_ascii=False)
        if name == "agent_search":
            k = str(args.get("agent_type"))
            return json.dumps({"hits": [{"record_id": f"kc-{k}-{i}", "title": f"{k} 카드 {i}", "snippet": f"{k} 지식 {i} — 시험 {700 + i}",
                                         "score": 0.8} for i in range(2)]}, ensure_ascii=False)
        if name == "list_parts":
            return json.dumps({"items": [{"id": 31, "name": "보스"}, {"id": 32, "name": "리브"}]}, ensure_ascii=False)
        if name == "alert_check":
            return json.dumps({"summary": "경보 2건", "alerts": [{"product_code": "P-100"}]}, ensure_ascii=False)
        if name == "get_top_issues":
            return json.dumps([{"category": "힌지 크랙"}], ensure_ascii=False)
        if name == "query_voc":
            return json.dumps([{"content": "세 번 떨어뜨렸더니 힌지가 깨졌다", "product_code": "P-100", "sentiment_score": -0.8}],
                              ensure_ascii=False)
        if name == "hybrid_search":
            return json.dumps({"hits": [{"title": "보스 응력", "snippet": "보스 뿌리 응력 120 MPa"}]}, ensure_ascii=False)
        if name == "create_report_draft":
            self.reports.append(copy.deepcopy(args))
            return json.dumps({"report_id": 77})
        return "{}"

    async def gather(self, _agent, persona, question, ctx, budget, prior=""):
        """좌석 자유 조회 — 결과가 맥락 · 앞 라운드 조회 서명 · 그 라운드 전에 배운 출처 정수에서 나온다(되살린 풀 · 정수가 하나라도 다르면 다음
        프롬프트가 달라진다). 같은 라운드의 좌석들이 함께 배우는 정수(그 라운드의 결과)는 세지 않는다 — 도는 순서에 흔들리지 않게."""
        r, k = int((TAGS.get() or {}).get("round") or 0), persona["key"]
        seen = sorted(x for x in (app._turn_ids.get() or ()) if x < r * 100000)
        self.lookups.append((r, k))
        out = json.dumps({"seat": k, "round": r, "part": r * 100000 + SEATS.index(k), "h": _h(k, r, question, ctx, prior, seen)[:16]})
        app._learn_ids(out)            # 진짜 도구 래퍼(_prep_tool)처럼 결과의 정수를 출처로 배운다
        args = json.dumps({"q": k}, ensure_ascii=False)
        return k, [("search_parts", args, out, f"c-{r}-{k}")], f"- search_parts({args}): {out}", ""


class _FTool:
    coroutine = None

    def __init__(self, w: _World, name: str):
        self.w, self.name = w, name

    async def ainvoke(self, args):
        self.w.tools.append((self.name, (TAGS.get() or {}).get("round")))
        out = self.w.answer(self.name, args)
        app._learn_ids(out)
        return out


_TOOL_NAMES = PRE_TOOLS + ("create_report_draft", "search_parts")


@pytest.fixture
def world(monkeypatch, tmp_path):
    import langgraph.prebuilt

    w = _World()

    async def _tools(*_a, **_k):
        return {n: _FTool(w, n) for n in _TOOL_NAMES}

    monkeypatch.setattr(call_trace, "tag", _tag)
    monkeypatch.setattr(d, "_llm_text", w.llm_text)
    monkeypatch.setattr(d, "_tools_by_name", _tools)
    monkeypatch.setattr(d, "_free_gather_one", w.gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, _tools, **_k: object())
    _fresh(monkeypatch, tmp_path / "jobs")
    _caps(monkeypatch, 4)
    return w


def _fresh(monkeypatch, job_dir: Path) -> None:
    """빈 원장 자리와 빈 메모리 — 재기동한 프로세스(죽기 전 디스크는 시험이 다시 깐다)."""
    monkeypatch.setattr(delib_jobs, "JOB_DIR", job_dir)
    for name in ("_JOBS", "_TASKS", "_PENDING", "_WROTE", "_JOURNALS", "_PAUSE"):
        monkeypatch.setattr(delib_jobs, name, {})
    for name, v in (("_CLOSING", False), ("_REQUEUED", 0), ("_FOLDED", 0), ("_JOURNAL_ERRORS", 0)):
        monkeypatch.setattr(delib_jobs, name, v)
    monkeypatch.setattr(m, "_APP", STUB)


def _disk() -> dict:
    root = delib_jobs.JOB_DIR
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _lay(snap: dict, root: Path) -> None:
    for rel, b in snap.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)


async def _settle_all() -> None:
    for _ in range(50):
        live = [t for t in delib_jobs._TASKS.values() if not t.done()]
        if not live:
            break
        await asyncio.wait(live, timeout=10)
    await _tick()


def _start_job(opts=None, **kw) -> dict:
    return delib_jobs.start(STUB, "default", Q, groups=list(GROUPS), delib_opts=copy.deepcopy(opts or OPTS), user_email=ME,
                            owner_aff=AFF, **kw)


def _oracle(w: _World, monkeypatch, snaps: list | None = None, opts=None) -> dict:
    """끊지 않은 실행 — 잡으로 끝까지 돌린다. snaps 를 주면 일지가 한 줄 적힐 때마다 (그 줄, 디스크) 를 뜬다."""
    orig = delib_jobs._Journal._write
    if snaps is not None:
        def spy(self, data, sync):
            orig(self, data, sync)
            snaps.append((json.loads(data), _disk()))
        monkeypatch.setattr(delib_jobs._Journal, "_write", spy)

    async def go():
        job = _start_job(opts)
        await _settle_all()
        return job
    try:
        job = asyncio.run(go())
    finally:
        if snaps is not None:
            monkeypatch.setattr(delib_jobs._Journal, "_write", orig)
    out = _result(w, job)
    w.reset()
    return out


def _result(w: _World, job: dict) -> dict:
    led = _on_disk(job)
    seat = {(r, s): (h, human) for k, r, s, h, human in w.llm if k == "seat"}
    assert len(seat) == len([x for x in w.llm if x[0] == "seat"]), "같은 좌석을 한 라운드에 두 번 불렀다"
    return {"job": job, "led": led, "seat": seat, "llm": list(w.llm), "tools": list(w.tools), "lookups": sorted(w.lookups),
            "reports": copy.deepcopy(w.reports), "restore_seen": list(w.restore_seen),
            "chair": [h for k, _r, _s, h, _ in w.llm if k in ("chair", "summary", "plain")]}


def _cut(snaps: list, pred, nth: int = 1) -> dict:
    """그 줄이 적힌 바로 뒤의 디스크 — nth 번째로 맞는 줄."""
    hits = [disk for rec, disk in snaps if pred(rec)]
    assert len(hits) >= nth, "시험 전제 — 끊을 자리를 찾지 못했다"
    return hits[nth - 1]


def _eng(kind: str, r: int | None = None):
    return lambda rec: rec.get("k") == kind and (r is None or rec.get("r") == r)


def _turn_line(r: int):
    return lambda rec: rec.get("k") == "delib" and (rec.get("d") or {}).get("kind") == "turn" and (rec.get("d") or {}).get("round") == r


def _restart(monkeypatch, tmp_path: Path, name: str, snap: dict) -> str:
    """새 프로세스 — 빈 메모리 · 죽기 전 디스크를 깐 원장 자리, 기동(재기동 정리). 끊긴 잡 id 를 돌려준다."""
    root = tmp_path / name
    _fresh(monkeypatch, root)
    _lay(snap, root)
    m.bind(STUB)
    jid = next(p.stem for p in root.glob("*.json"))
    return jid


def _continue(jid: str, user: str = ME, groups=GROUPS, *, wait: bool = True, **kw) -> dict:
    async def go():
        out = await m.deliberate_continue(previous_job_id=jid, ctx=_ctx(user, AFF, ",".join(groups)), **kw)
        if wait:
            await _settle_all()
        return out
    return asyncio.run(go())


def _turn_rows(led: dict) -> list:
    return sorted((t["round"], t.get("display_round"), t["persona"], t["say"]) for t in led["turns"] if t.get("persona"))


def _check_resumed(o: dict, res: dict, first_round: int, *, spoken=(), gathered: bool = False) -> None:
    """이어 돈 실행이 끊지 않은 실행과 같은가 — 남은 라운드의 좌석 프롬프트(지문) · 라운드 성격 · 라운드 전 단계 호출 0 · 조회 · 의장 · 보고서 ·
    결정문 · 전사(좌석마다 한 번). 끊긴 라운드(first_round)에서 끊기기 전에 말한 좌석(spoken)은 부르지 않았어야 하고, 그 라운드의 조회가
    끊기기 전에 끝났으면(gathered) 그 조회를 다시 하지 않았어야 한다(S2)."""
    want = {key: v for key, v in o["seat"].items() if key[0] > first_round or (key[0] == first_round and key[1] not in spoken)}
    assert {key: h for key, (h, _x) in res["seat"].items()} == {key: h for key, (h, _x) in want.items()}, (
        "남은 라운드의 좌석 프롬프트가 끊지 않은 실행과 다르다 — 다른 칸: "
        + str(sorted(set(res["seat"]) ^ set(want)) or [k for k in want if res["seat"].get(k, ("",))[0] != want[k][0]]))
    for (r, _s), (_h2, human) in res["seat"].items():
        assert _KIND_MARK[r] in human, f"{r}라운드 프롬프트가 그 라운드 성격(심화 · 수렴)이 아니다"
    assert [x for x in res["llm"] if x[0] in PRE_KINDS] == [], "라운드 전 단계의 LLM 을 다시 불렀다"
    assert [t for t, _r in res["tools"] if t in PRE_TOOLS] == [], f"라운드 전 단계의 도구를 다시 불렀다: {res['tools']}"
    assert res["lookups"] == [x for x in o["lookups"] if x[0] > first_round or (x[0] == first_round and not gathered)], \
        "끝난 조회를 다시 했거나 남은 라운드의 조회가 다르다"
    assert all(res["restore_seen"]) and not any(o["restore_seen"]), "엔진이 재개 상태를 보지 못했다(또는 오라클이 봤다)"
    assert res["chair"] == o["chair"], "의장 · 요약 · 쉬운 설명 프롬프트가 다르다(되살린 근거 계수 · 좌석 · 라운드 전사)"
    assert res["reports"] == o["reports"], "보고서 블록이 다르다(되살린 VOC · 선주입 · 라운드 전사)"
    a, b = res["led"], o["led"]
    assert (a["status"], a["decision"], a["result_text"], a["report_id"]) == (b["status"], b["decision"], b["result_text"], b["report_id"])
    assert _turn_rows(a) == _turn_rows(b), "전사가 끊지 않은 실행과 다르다(좌석마다 한 번이 아니다)"
    assert [c for c in a["evidence_omitted"] if c["source"] != "끊긴 심의 되살림"] == b["evidence_omitted"], "알림 카드가 두 번 실렸다"
    assert [c["source"] for c in b["evidence_omitted"]].count("요청 값 해석 불가") == 1, "시험 전제 — 라운드 전 단계의 알림 카드가 있다"


# ── 라운드 경계에서 끊고 이으면 끊지 않은 실행과 같다 ─────────────────────────────────────────────
@pytest.mark.parametrize("where, first", [("pre", 1), ("r1", 2), ("r2", 3), ("r3", 4), ("r4", 5), ("mid2", 2)])
def test_끊긴_자리에서_같은_잡을_이으면_남은_좌석_프롬프트가_끊지_않은_실행과_지문까지_같다(world, monkeypatch, tmp_path, where, first):
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    if where == "pre":
        snap = _cut(snaps, _eng("e:pre"))
    elif where == "mid2":
        snap = _cut(snaps, _turn_line(2), nth=2)            # 2라운드에서 두 석이 말한 뒤 — 그 두 석은 다시 부르지 않고 나머지 세 석만 부른다(S2)
    else:
        snap = _cut(snaps, _eng("e:round_end", int(where[1:])))
    jid = _restart(monkeypatch, tmp_path, where, snap)
    led = _on_disk({"id": jid})
    sr = led["seat_resume"]
    spoken = list(SEATS[:2]) if where == "mid2" else []    # 가짜는 같은 라운드의 좌석을 앉은 차례대로 끝낸다
    assert led["status"] == "interrupted" and led["error"] == delib_jobs.INTERRUPTED_MSG_EXACT
    assert (sr["ok"], sr["sk"], sr["round"], sr["spoken"], sr["todo"]) == (
        True, "main", first, spoken, [s for s in SEATS if s not in spoken] if first <= ROUNDS else []), sr
    out = _continue(jid)
    assert out["job_id"] == jid and out["resumed"]["mode"] == "in_place", out["resumed"]
    assert (out["resumed"]["round"], out["resumed"]["rounds_done"], out["resumed"]["attempt"]) == (first, first - 1, 1)
    assert (out["resumed"]["spoken"], out["resumed"]["todo"]) == (sr["spoken"], sr["todo"]), out["resumed"]
    res = _result(world, {"id": jid})
    _check_resumed(o, res, first, spoken=spoken, gathered=where == "mid2")
    a = res["led"]
    assert len(a["attempts"]) == 1 and a["attempts"][0]["by"] == ME and a["attempts"][0]["round"] == first and not a.get("resumed_by")
    assert not delib_jobs._journal_path(jid).exists() and not delib_jobs._args_path(jid).exists(), "끝난 잡의 일지 · 시작 인자 파일이 남았다"
    notes = [c for c in a["evidence_omitted"] if c["source"] == "끊긴 심의 되살림"]
    assert len(notes) == 1 and "다시 조회하지 않았다" in notes[0]["text"], notes
    assert ("끊기기 전에 말한 2석(" in notes[0]["text"]) is (where == "mid2"), notes[0]["text"]


def test_정상_종료로_끊겨도_같은_잡이_끊긴_라운드부터_끊지_않은_실행과_같게_돈다(world, monkeypatch, tmp_path):
    """TERM 꼴 — 3라운드의 한 좌석 호출을 붙잡아 둔 채 내려가면(shutdown) 잡의 finally 가 원장에 진행과 재개 요약을 쓴다(일지가 센 것). 다시 뜨면
    재기동 정리는 끊긴 잡을 건드리지 않고, 주인의 이어하기가 3라운드부터 같은 잡을 돌린다 — 끊기기 전에 3라운드에서 말한 좌석은 다시 부르지 않고
    붙잡혔던 좌석만 부른다(S2 — 전사에 좌석마다 한 번). 오라클은 그 좌석이 3라운드에서 맨 나중에 끝난 끊지 않은 실행이다(라운드 전사는 끝난
    차례다 — 끊긴 실행에서도 그 좌석이 맨 나중에 끝났다)."""
    world.slow = (3, "rel-drop")
    o = _oracle(world, monkeypatch)
    world.hold, world.held = (3, "rel-drop"), None

    async def scenario():
        world.held = asyncio.Event()
        job = _start_job()
        await asyncio.wait_for(world.held.wait(), 20)
        await _tick()
        assert await delib_jobs.shutdown(wait_s=5) == 1
        return job
    try:
        job = asyncio.run(scenario())
    finally:
        delib_jobs.closing(False)
    world.hold = None
    led = _on_disk(job)
    sr = led["seat_resume"]
    assert (led["status"], sr["ok"], sr["round"]) == ("interrupted", True, 3), (led["status"], sr)
    assert "rel-drop" not in sr["spoken"] and sr["spoken"], sr
    assert {t["persona"] for t in led["turns"] if t["round"] == 3} == set(sr["spoken"]), "원장의 3라운드 턴과 재개 요약이 다르다"
    world.reset()
    root = delib_jobs.JOB_DIR
    _fresh(monkeypatch, root)                             # 같은 디스크로 다시 뜬다
    m.bind(STUB)
    assert delib_jobs.folded() == 0, "내려가며 쓴 원장을 다시 접었다"
    assert sr["todo"] == ["rel-drop"] and sr["spoken"] == [s for s in SEATS if s != "rel-drop"], sr
    out = _continue(job["id"])
    assert out["resumed"]["mode"] == "in_place" and out["resumed"]["round"] == 3 and out["resumed"]["todo"] == ["rel-drop"]
    _check_resumed(o, _result(world, job), 3, spoken=sr["spoken"], gathered=True)


def test_제자리로_줄_섰다가_재기동해도_재개_자료로_다시_선다(world, monkeypatch, tmp_path):
    """줄에 다시 선 제자리 이어하기가 뜨기 전에 서버가 재기동하면 재기동 정리가 시작 인자 파일로 다시 세운다(R2) — 그때 재개 자료를 다시 읽어 건다.
    없이 세우면 같은 잡이 처음부터 돌아 원장에 남은 끝난 라운드의 턴과 겹친다."""
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "q", _cut(snaps, _eng("e:round_end", 2)))
    delib_jobs.closing(True)                              # 자리가 있어도 띄우지 않는다 — 줄에 선 채로 둔다
    out = _continue(jid, wait=False)
    assert out["status"] == "queued" and out["job_id"] == jid, out
    disk = _disk()
    j = json.loads(disk[f"{jid}.json"])
    assert (j["status"], len(j["attempts"]), {t["round"] for t in j["turns"]}) == ("queued", 1, {1, 2})
    lines = [json.loads(x) for x in disk[f"journal/{jid}.jsonl"].splitlines()]
    assert [x["k"] for x in lines][:2] == ["j:head", "e:pre"] and all(x["k"].startswith(("j:", "e:")) for x in lines), \
        "줄에 다시 선 잡의 일지에 앞 시도의 공개 사건이 남았다(재기동 정리가 두 번 접는다)"
    jp = delib_jobs._journal_path(jid)
    assert stat.S_IMODE(jp.stat().st_mode) == 0o600 and stat.S_IMODE(jp.parent.stat().st_mode) == 0o700, "고쳐 쓴 일지의 권한이 넓다"
    assert not list(jp.parent.glob(".*.tmp")), "고쳐 쓰다 남은 임시 파일이 있다"
    delib_jobs.closing(False)
    _fresh(monkeypatch, delib_jobs.JOB_DIR)
    m.bind(STUB)
    assert list(delib_jobs._PENDING) == [jid] and delib_jobs._REQUEUED == 1

    async def go():
        delib_jobs._pump()
        await _settle_all()
    asyncio.run(go())
    _check_resumed(o, _result(world, {"id": jid}), 3)


# ── 일지의 줄마다 끊어도 재개 요약이 원장과 맞는다(변이: 줄 빼기 · e:raw 를 turn 뒤로) ─────────────────────────
def test_일지의_어느_줄에서_끊겨도_재개_요약이_접은_원장과_맞는다(world, monkeypatch):
    """KILL 은 아무 줄 뒤에서나 난다. 줄마다 그때의 디스크로 접어 본다 — 재개 자료(e:pre)가 있으면 처음 부를 라운드는 잇달아 끝난(e:round_end)
    라운드 다음이고, 원장에 그 라운드의 턴이 있는 좌석은 모두 '이미 말한 좌석' 에 든다(원 출력 e:raw 가 turn 보다 먼저 적힌다). e:pre 앞이면
    재개 자료가 없다."""
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    jid = next(rec["id"] for rec, _disk_ in snaps if rec.get("k") == "j:head")
    seen_pre = False
    for i, (rec, disk) in enumerate(snaps):
        seen_pre = seen_pre or rec.get("k") == "e:pre"
        led = json.loads(disk[f"{jid}.json"])
        assert led["status"] == "running" and led["stage"] == "start", "시험 전제 — 도는 동안 원장은 띄울 때 모양이다"
        lines = [json.loads(x) for x in disk[f"journal/{jid}.jsonl"].splitlines()]
        p = delib_jobs._journal_path(jid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(disk[f"journal/{jid}.jsonl"])
        t = delib_resume.Tracker()
        delib_jobs._fold_journal(led, t)
        sr = t.summary()
        if not seen_pre:
            assert sr["ok"] is False and "라운드 전 단계" in sr["why"], (i, sr)
            continue
        ends = {x["r"] for x in lines if x["k"] == "e:round_end"}
        k = next(r for r in range(ROUNDS + 1) if r + 1 not in ends)
        assert sr["ok"] and sr["round"] == k + 1, (i, rec.get("k"), sr, ends)
        turned = {x["persona"] for x in led["turns"] if x.get("round") == sr["round"]}
        assert turned <= set(sr["spoken"]), f"{i}번째 줄 뒤 — 원장에 턴이 있는 좌석 {turned - set(sr['spoken'])} 의 원 출력이 일지에 없다"
        assert len(sr["spoken"]) - len(turned) <= 1, "원 출력은 turn 바로 앞에만 먼저 적힌다"
        assert sr["todo"] == ([s for s in SEATS if s not in sr["spoken"]] if sr["round"] <= ROUNDS else []), (i, sr)   # 나머지만 부른다(S2)
    p.unlink()


# ── 공통 — 남 · 손잡이 · 권한 · 꼴 · 두 번 · 바꾼 값 ─────────────────────────────────────────
def test_남의_이어하기는_재개_자료를_읽지_않고_요약으로_잇고_주인의_것은_남는다(world, monkeypatch, tmp_path):
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "f", _cut(snaps, _eng("e:round_end", 2)))
    read = []
    real_load, real_parse = delib_jobs.resume_load, delib_resume.parse
    monkeypatch.setattr(delib_jobs, "resume_load", lambda *a, **k: read.append("load") or real_load(*a, **k))
    monkeypatch.setattr(delib_resume, "parse", lambda *a, **k: read.append("parse") or real_parse(*a, **k))
    out = _continue(jid, user=YOU)                        # 같은 소속의 남 — 볼 수 있고 이을 수도 있다(HA-06)
    assert read == [], "남의 이어하기가 주인의 재개 자료를 읽었다"
    r = out["resumed"]
    assert (r["mode"], r["foreign"], out["job_id"] != jid) == ("summary", True, True) and "남의 심의" in r["why"], r
    assert "같은 잡에서 끊긴 자리부터 잇지 못하고" in out["note"], out["note"]
    prev = _on_disk({"id": jid})
    assert prev["status"] == "interrupted" and prev["seat_resume"]["ok"] and not prev.get("attempts")
    assert delib_jobs._journal_path(jid).exists() and delib_jobs._args_path(jid).exists(), "남의 이어하기가 주인의 재개 자료를 지웠다"
    world.reset()
    mine = _continue(jid)                                 # 주인은 그대로 제자리에서 잇는다
    assert mine["job_id"] == jid and mine["resumed"]["mode"] == "in_place" and read == ["load", "parse"]
    _check_resumed(o, _result(world, {"id": jid}), 3)


def test_손잡이로는_재개_자료를_청하지_못한다(world, monkeypatch, tmp_path):
    """재개 상태는 MCP 입구가 delib_jobs.start 의 키워드로만 넘긴다. 요청 손잡이(delib_opts · MCP advanced)에 남의 잡 id 를 적어도 엔진은 아무것도
    되살리지 않는다 — 라운드 전 단계를 처음부터 부른다."""
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "o", _cut(snaps, _eng("e:round_end", 2)))
    ask = {"resume": jid, "restore": jid, "resume_from": jid, "resume_job": jid, "previous_job_id": jid, "sk": "main", "_resume": 1}

    async def go():
        a = _start_job({**OPTS, **ask})
        await _settle_all()
        b = await m.deliberate_start(question=Q, rounds=ROUNDS, tools=["list_parts"], advanced={**ask, "voc": "always", "rebut_quote": 0},
                                     ctx=_ctx(ME, AFF, ",".join(GROUPS)))
        await _settle_all()
        return a, b
    a, b = asyncio.run(go())
    assert a["id"] != jid and b["job_id"] != jid
    assert not any(world.restore_seen), "손잡이로 재개 상태가 걸렸다"
    assert len([x for x in world.llm if x[0] == "seat"]) == 2 * ROUNDS * len(SEATS), "처음부터 돌지 않았다"
    assert [t for t, _r in world.tools if t == "recommend_agents"], "라운드 전 단계를 부르지 않았다 — 무엇인가 되살렸다"
    assert "resume" not in vars(d._resolve_opts(ask)) and not [p for p in inspect.signature(m.deliberate_start).parameters if "resum" in p]
    assert _on_disk({"id": jid})["status"] == "interrupted" and delib_jobs._journal_path(jid).exists(), "남의 끊긴 잡을 건드렸다"


@pytest.mark.parametrize("case", ["권한", "꼴", "바꾼 값", "일지 없음", "라운드 전", "깨진 줄"])
def test_재개_자료를_쓸_수_없으면_까닭과_함께_요약으로_잇는다(world, monkeypatch, tmp_path, case):
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    snap = (_cut(snaps, _eng("e:round_end", 2)) if case != "라운드 전" else
            _cut(snaps, lambda rec: rec.get("k") == "delib" and (rec.get("d") or {}).get("kind") == "personas"))
    if case in ("꼴", "깨진 줄"):
        key = next(k for k in snap if k.startswith("journal/"))
        lines = snap[key].split(b"\n")
        if case == "꼴":
            lines[0] = json.dumps({**json.loads(lines[0]), "rv": delib_resume.RESUME_SCHEMA + 1}).encode()
        else:
            lines.insert(len(lines) // 2, b'{"t": 1.0, "k": "e:raw", "sk": "main", "r": 1, "o": {"persona"')     # 가운데 깨진 줄
        snap = {**snap, key: b"\n".join(lines)}
    jid = _restart(monkeypatch, tmp_path, case, snap)
    if case == "일지 없음":
        delib_jobs._journal_path(jid).unlink()
    st = asyncio.run(m.deliberate_status(jid, ctx=_ctx(ME, AFF)))
    kw = {"human_note": "재기동 뒤 측정값 추가"} if case == "바꾼 값" else {}
    groups = ["CAEG"] if case == "권한" else GROUPS          # 끊긴 뒤 플랫폼 허가를 잃었다
    out = _continue(jid, groups=groups, **kw)
    r = out["resumed"]
    why = {"권한": "권한이 줄었다", "꼴": "꼴이 이 판과 다르다", "바꾼 값": "바꾼 값이 있다(human_note)", "일지 없음": "진행 일지가 없다",
           "라운드 전": "라운드 전 단계에서 끊겨", "깨진 줄": "깨진 줄"}[case]
    assert r["mode"] == "summary" and out["job_id"] != jid and why in r["why"], r
    assert f"새 잡에서 잇는다 — {r['why']}" in out["note"], out["note"]
    if case in ("꼴", "깨진 줄"):        # 진행 조회의 재개 요약도 이어하기와 같은 까닭으로 안 된다고 말한다
        assert st["resume"]["seat_resume"]["ok"] is False and why in st["resume"]["seat_resume"]["why"], st["resume"]["seat_resume"]
    if case == "라운드 전":
        assert st["resume"]["seat_resume"]["ok"] is False and r["rounds_done"] == 0 and "처음부터" in out["note"]
    elif case in ("꼴", "깨진 줄"):
        # 2라운드 끝 줄 뒤 · 3라운드 단계 전에 끊겼다 — 꼴이 다르거나 깨진 일지는 믿지 않아 단계(r2)로 센다(1라운드 뒤부터)
        assert r["rounds_done"] == 1 and r["rounds_start_at"] == 2
    else:
        # 일지의 라운드 끝 줄이 단계보다 정확하다 — 단계는 아직 r2 지만 2라운드는 끝났다(그 턴은 모두 원장에 있다)
        assert r["rounds_done"] == 2 and r["rounds_start_at"] == 3 and _on_disk({"id": jid})["stage"] == "r2"


def test_같은_잡을_두_번_잇지_않는다(world, monkeypatch, tmp_path):
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "twice", _cut(snaps, _eng("e:round_end", 1)))

    async def go():
        ctx = _ctx(ME, AFF, ",".join(GROUPS))
        again, _why = delib_jobs.resume_load(_on_disk({"id": jid}), groups=None)     # 이기 전에 읽어 둔 재개 상태
        assert again is not None, _why
        first = await m.deliberate_continue(previous_job_id=jid, ctx=ctx)
        with pytest.raises(ValueError, match="이미 같은 잡에서 이어 돌고 있다"):
            await m.deliberate_continue(previous_job_id=jid, ctx=ctx)
        with pytest.raises(ValueError, match="두 번 잇지 않는다"):        # 입구를 거치지 않고 재개 상태를 다시 넘겨도
            delib_jobs.start(STUB, "default", Q, groups=list(GROUPS), delib_opts=copy.deepcopy(OPTS), user_email=ME, resume=again)
        await _settle_all()
        return first
    first = asyncio.run(go())
    led = _on_disk({"id": jid})
    assert first["job_id"] == jid and led["status"] == "done" and len(led["attempts"]) == 1


def test_상태_목록_health_가_재개_요약과_시도와_접은_수를_싣는다(world, monkeypatch, tmp_path):
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "h", _cut(snaps, _turn_line(3), nth=3))
    st = asyncio.run(m.deliberate_status(jid, ctx=_ctx(ME, AFF)))
    sr = st["resume"]["seat_resume"]
    assert st["resume"]["rounds_done"] == 2 and (sr["ok"], sr["round"], sr["spoken"], sr["todo"]) == (
        True, 3, list(SEATS[:3]), list(SEATS[3:])), sr
    assert st["error"] == delib_jobs.INTERRUPTED_MSG_EXACT
    row = next(x for x in asyncio.run(m.deliberate_list(ctx=_ctx(ME, AFF)))["jobs"] if x["job_id"] == jid)
    assert row["resume"]["seat_resume"] == sr, "목록 행이 재개 요약을 싣지 않았다(목록 색인)"
    h = TestClient(app.app).get("/health").json()
    assert h["delib_folded"] == 1 and h["delib_journal"] == {"on": True, "max_mb": delib_jobs.JOURNAL_MAX_BYTES // 1048576, "errors": 0}
    _continue(jid)
    row = next(x for x in asyncio.run(m.deliberate_list(ctx=_ctx(ME, AFF)))["jobs"] if x["job_id"] == jid)
    assert row["status"] == "done" and len(row["attempts"]) == 1 and "resume" not in row
    st = asyncio.run(m.deliberate_status(jid, ctx=_ctx(ME, AFF)))
    assert st["attempts"][0]["round"] == 3 and st["attempts"][0]["stage"] == "r3"


def test_체크포인트_심의는_1라운드를_끝난_라운드로_건너뛰지_않고_끊긴_라운드로_이어_체크포인트에서_멈춘다(world, monkeypatch, tmp_path):
    """stop_after_round=1 — 1라운드가 끝난 줄(e:round_end) 뒤 · 체크포인트 결정문 전에 끊기면, 1라운드를 끝난 라운드로 되살려 건너뛰면 루프가
    체크포인트를 지나쳐 2라운드를 돈다. 1라운드는 끊긴 라운드로 잇는다 — 좌석은 모두 말했으니 아무도 다시 부르지 않고(S2) 그 라운드를 맺어
    체크포인트에서 멈춘다(라운드 전 단계 · 1라운드 조회도 되살린다)."""
    ck = {**OPTS, "stop_after_round": 1}
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps, opts=ck)
    assert o["led"]["decision"].startswith("[체크포인트") and {r for r, _s in o["seat"]} == {1}
    jid = _restart(monkeypatch, tmp_path, "ck", _cut(snaps, _eng("e:round_end", 1)))
    sr = _on_disk({"id": jid})["seat_resume"]
    assert (sr["round"], sr["spoken"], sr["todo"]) == (1, list(SEATS), []), sr
    out = _continue(jid)
    assert out["resumed"]["mode"] == "in_place" and out["resumed"]["round"] == 1 and out["resumed"]["todo"] == []
    res = _result(world, {"id": jid})
    assert res["seat"] == {} and res["lookups"] == [], "1라운드에서 이미 말한 좌석을 다시 불렀거나 끝난 조회를 다시 했다"
    assert [x for x in res["llm"] if x[0] in PRE_KINDS] == [] and res["led"]["decision"] == o["led"]["decision"]
    assert _turn_rows(res["led"]) == _turn_rows(o["led"])


# ── 재개 상태가 없으면 지금 길 그대로 ─────────────────────────────────────────────────
def test_끊지_않은_실행은_일지를_써도_사건과_프롬프트가_웹_길과_바이트까지_같다(world, monkeypatch):
    """엔진 갈고리는 재개 상태가 없으면 아무것도 바꾸지 않는다 — 잡(일지에 재개 자료를 쓴다)과 웹 심의(일지가 없다)가 같은 사건 · 같은 프롬프트를
    낸다. 일지에는 재개 자료가 실제로 적혔다(시험 전제)."""
    seen: list = []
    real = delib_jobs._parse_sse

    def spy(chunk):
        seen.append(chunk)
        return real(chunk)
    monkeypatch.setattr(delib_jobs, "_parse_sse", spy)
    snaps: list = []
    o = _oracle(world, monkeypatch, snaps)
    job_events = list(seen)
    job_llm = o["llm"]
    kinds = [rec["k"] for rec, _disk_ in snaps]
    assert kinds.count("e:pre") == 1 and kinds.count("e:round_end") == ROUNDS and kinds.count("e:raw") == ROUNDS * len(SEATS)
    assert kinds.count("e:gather") == ROUNDS - 1, "수렴 라운드 밖의 조회마다 e:gather 가 하나다"
    world.reset()

    async def web():
        out = []
        async for chunk in d.run_deliberation(STUB, Q, list(GROUPS), copy.deepcopy(OPTS), ME):
            out.append(chunk)
        return out
    web_events = asyncio.run(web())
    assert web_events == job_events, "재개 자료를 쓰는 잡과 웹 심의의 사건이 다르다 — 갈고리가 지금 길을 바꿨다"
    assert world.llm == job_llm, "프롬프트가 다르다"
    assert len([x for x in job_llm if x[0] == "seat"]) == ROUNDS * len(SEATS) and {x[0] for x in job_llm} >= set(PRE_KINDS), \
        "시험 전제 — 라운드 전 단계와 좌석 전부를 불렀다"
    assert not any(world.restore_seen)


# ── 상한 · 쓰기 실패 · 멈춤 ─────────────────────────────────────────────────────────
def test_일지_상한의_4분의_3에_닿으면_재개_자료만_멈추고_공개_사건은_계속_적어_요약으로_잇는다(world, monkeypatch, tmp_path):
    """A5.1 — 재개 자료가 상한을 먹으면 접기(공개 사건)까지 잃는다. 엔진 줄은 상한의 4분의 3에서 한 줄(e:resume_off) 남기고 멈추고, 공개 사건은
    상한까지 계속 적는다 — 접은 원장은 그대로 끊기기 전 진행이고, 이어하기는 까닭과 함께 요약으로 간다."""
    full: list = []
    _oracle(world, monkeypatch, full)
    before = [len(next(v for k_, v in disk.items() if k_.startswith("journal/"))) for _rec, disk in full]
    i = next(n for n, (rec, _d) in enumerate(full) if rec.get("k") == "e:raw" and rec.get("r") == ROUNDS)
    monkeypatch.setattr(delib_jobs, "JOURNAL_MAX_BYTES", before[i - 1] * 4 // 3 + 1)    # 마지막 라운드의 첫 원 출력에서 엔진 줄이 멈춘다
    _fresh(monkeypatch, tmp_path / "capped")
    capped: list = []
    _oracle(world, monkeypatch, capped)
    kinds = [rec.get("k") for rec, _d in capped]
    off = kinds.index("e:resume_off")
    assert not [k for k in kinds[off + 1:] if str(k).startswith("e:")], "엔진 줄이 멈추지 않았다"
    assert "j:cap" not in kinds and "delib" in kinds[off + 1:], "시험 전제 — 공개 사건은 상한 안에서 계속 적힌다"
    jid = _restart(monkeypatch, tmp_path, "cap", _cut(capped, _turn_line(ROUNDS), nth=1))
    led = _on_disk({"id": jid})
    assert led["stage"] == f"r{ROUNDS}" and ROUNDS in {t["round"] for t in led["turns"]}, "멈춘 뒤의 공개 사건을 접지 못했다"
    sr = led["seat_resume"]
    assert sr["ok"] is False and "끝까지 쓰지 못했다" in sr["why"] and "4분의 3" in sr["why"], sr
    out = _continue(jid)
    assert out["resumed"]["mode"] == "summary" and "끝까지 쓰지 못했다" in out["resumed"]["why"], out["resumed"]


def test_재개_자료를_JSON_으로_못_적으면_그_줄부터_멈추고_공개_사건과_실패_수는_남는다(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path / "enc")
    jr = delib_jobs._Journal("enc-job")
    jr.engine({"k": "e:pre", "sk": "main", "personas": [object()]})
    jr.engine({"k": "e:round_end", "sk": "main", "r": 1, "lost": []})
    jr.event("status", {"step": "계속 적는다"}, 1.0, 0)
    jr.close()
    lines = [json.loads(x) for x in delib_jobs._journal_path("enc-job").read_text(encoding="utf-8").splitlines()]
    assert [x["k"] for x in lines] == ["j:head", "e:resume_off", "status"], lines
    assert "JSON 으로 적지 못했다" in lines[1]["why"] and delib_jobs.journal_health()["errors"] == 1
    assert jr.track.summary()["ok"] is False and "끝까지 쓰지 못했다" in jr.track.summary()["why"]


def test_멈춤이면_제자리_이어하기도_거절되고_잡은_끊긴_채_그대로다(world, monkeypatch, tmp_path):
    snaps: list = []
    _oracle(world, monkeypatch, snaps)
    jid = _restart(monkeypatch, tmp_path, "p", _cut(snaps, _eng("e:round_end", 2)))
    before = delib_jobs._journal_path(jid).read_bytes()
    delib_jobs.pause_state().set_on(scope="delib", by="운영", reason="점검", ttl_s=600)
    with pytest.raises(delib_jobs.DelibPaused):
        _continue(jid)
    led = _on_disk({"id": jid})
    assert led["status"] == "interrupted" and not led.get("attempts") and led["seat_resume"]["ok"], "거절된 이어하기가 잡을 바꿨다"
    assert delib_jobs._journal_path(jid).read_bytes() == before, "거절된 이어하기가 일지를 고쳐 썼다"

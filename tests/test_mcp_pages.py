# deliberate_* 도구 응답 한 번이 Claude Code 의 MCP 결과 상한을 넘지 않고, 쪽을 따라가면 원장 그대로 다 받는지 본다
"""검토 engine-contract:A-transcript-size — deliberate_transcript(full=true) 한 쪽(기본 40턴)에 글자 상한이 없었다.

넓은 창(1백만 토큰)에서 기록용 발언(say_full)은 끊지 않고 1라운드 해석·우려와 3라운드 양보 불가 조항까지 싣는다. 리스크 심사를
손수 돌린 사람은 그 전사를 full=true 로 받아 risk_submit_panel_result 로 원장에 낸다. 그런데 한 쪽은 턴 수(limit)로만 나뉘어,
21석 3라운드 패널의 첫 쪽이 15만 자를 넘었다. Claude Code 는 MCP 결과가 MAX_MCP_OUTPUT_TOKENS(기본 25,000토큰)를 넘으면
본문 대신 '파일로 저장했다' 는 안내를 주고(ENABLE_MCP_LARGE_OUTPUT_FILES 를 끈 판은 앞 100,000자에서 자른다) — 에이전트가
그 파일을 끝까지 읽지 않으면 뒤쪽 턴이 빠진 채 원장에 간다. 어느 쪽이든 '받은 쪽이 전부' 라는 전제가 깨진다.

시험은 생산자를 실제로 돌린다 — 심의 스트림(LLM 호출만 가로챔)이 잡 원장에 턴을 쌓고, 소비자 쪽은 FastMCP 의 call_tool 이
내주는 **실제 글**(게이트웨이는 이 글을 그대로 넘긴다)을 잰다. 쪽은 응답이 알려 주는 대로(page.next · next_offset) 따라간다.

  실행:  .venv/bin/python -m pytest tests/test_mcp_pages.py -q
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _pin_context, _Tool  # noqa: E402, F401
from test_window_budget_b import _BIG, _Llm, _seat_plain, _win  # noqa: E402

# Claude Code 의 MCP 결과 상한(MAX_MCP_OUTPUT_TOKENS 기본 25,000토큰)을 글자로 — 1자≈1토큰. HWAXRisk 의
# tests/test_mcp_brief_relay.CLAUDE_MCP_RESULT_CHARS · HWAXPortal 의 JS 파이프라인과 같은 보수 환산이다.
CLAUDE_MCP_RESULT_CHARS = 25_000


def _para(tag: str, n: int) -> str:
    """n 자 글 — 따옴표·줄바꿈(JSON 에서 두 글자로 늘어나는 글자)을 섞어 둔다. 앞머리에 tag 를 둔다."""
    base = (f"{tag} 힌지 브래킷 두께 0.8→0.6 mm 변경으로 \"낙하\" 시 굽힘 응력이 항복 근처까지 오르고\n"
            "계면 박리 가능성이 커진다. ")
    return (base * (n // len(base) + 1))[:n]


def _seat(key: str) -> dict:
    """좌석 하나의 구조화 발언 — 넓은 창 리스크 심사 좌석이 실제로 내는 정도의 길이. 'huge' 좌석은 턴 하나가 쪽보다 크다."""
    huge = key.startswith("huge")
    return {**_seat_plain(key),
            "lens": _para(f"{key} 관점", 60_000 if huge else 1_200),
            "reads": [_para(f"{key} 해석{i}", 300) + f" [e:{i}]" for i in range(10)],
            "recommendation": _para(f"{key} 권장", 600),
            "concerns": [_para(f"{key} 우려{i}", 200) for i in range(5)],
            "concede": [_para(f"{key} 수용", 300)], "deepen": _para(f"{key} 심화", 900),
            "final_position": _para(f"{key} 최종", 1_200),
            "non_negotiable": [_para(f"{key} 조항{i}", 12_000 if huge else 300) for i in range(3)],
            "vote": "진행"}


def _job(monkeypatch, tmp_path, *, seats: int = 12, rounds: int = 3, chair: str = "결정: 진행한다",
         question: str = "힌지 크랙 원인", job_id: str = "pg-1", user: str = "") -> dict:
    """심의를 넓은 창에서 끝까지 돌려 잡 원장에 쌓는다 — delib_jobs._drive 가 실제로 받는 이벤트 그대로."""
    _win(_BIG)
    llm = _Llm(_seat, chair)

    async def _fake_tools(*_a, **_k):
        return {"agent_search": _Tool("agent_search")}

    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", llm.text)
    stub = SimpleNamespace(state=SimpleNamespace(llm=llm, delib_llm=None))
    personas = [{"key": "huge-seat", "role": "기구"}] + [{"key": f"dom{i:02d}-seat", "role": "역할"}
                                                        for i in range(seats - 1)]
    opts = {"personas": personas, "rounds": rounds, "free_tools": 0, "voc": "off", "rescreen": 0,
            "persona_knowledge": 0, "save_report": 0}
    j = {"id": job_id, "job": "risk-review", "status": "running", "question": question, "turns": [],
         "checkpoint": None, "started_at": delib_jobs._now(), "opts": {}, "user": user}
    monkeypatch.setitem(delib_jobs._JOBS, j["id"], j)
    asyncio.run(delib_jobs._drive(j, d.run_deliberation(stub, question, [], opts)))
    assert j["status"] == "done" and not j.get("error"), (j["status"], j.get("error"))
    return j


def _wire(name: str, args: dict) -> str:
    """FastMCP 가 그 도구 결과로 내보내는 글 — Claude Code 가 상한을 재는 바로 그 글이다."""
    r = asyncio.run(m.mcp.call_tool(name, args))
    blocks = r if isinstance(r, list) else r[0]
    return "".join(b.text for b in blocks)


def _follow(name: str, args: dict, cap: int = 400) -> list:
    """응답이 알려 주는 대로 쪽을 따라간다 — page.next(같은 호출의 다음 쪽) · next_offset(전사의 다음 묶음).

    쪽마다 (글, 응답) 을 돌려준다. 응답이 아무것도 알려 주지 않으면 거기서 끝이다(종전 모양이 그렇다)."""
    out, call = [], dict(args)
    for _ in range(cap):
        text = _wire(name, call)
        res = json.loads(text)
        out.append((text, res))
        nxt = (res.get("page") or {}).get("next")
        if nxt:
            call = dict(nxt)
        elif res.get("next_offset") is not None:
            call = {**args, "offset": res["next_offset"]}
            call.pop("part", None)
        else:
            return out
    raise AssertionError(f"{cap}쪽을 넘게 이어진다 — next 가 제자리를 돈다")


def _path(parts) -> str:
    s = ""
    for p in parts:
        s += f"[{p}]" if isinstance(p, int) else (f".{p}" if s else str(p))
    return s


def _merge(acc, val, at, pieces):
    """쪽에 실린 값 하나를 모은 값에 잇는다 — 응답이 적어 준 pieces 만 보고(도구 설명의 규칙 그대로)."""
    key = _path(at)
    if isinstance(val, str):
        cut = pieces.get(key)
        if cut is None:
            return val
        have = acc if isinstance(acc, str) else ""
        assert len(have) == cut["from"], f"{key}: 앞 쪽까지 {len(have)}자인데 이 쪽은 {cut['from']}자부터다"
        assert len(val) == cut["to"] - cut["from"], (key, len(val), cut)
        return have + val
    if isinstance(val, list):
        cut = pieces.get(key)
        start = cut["from"] if cut else 0
        if cut:
            assert len(val) == cut["to"] - cut["from"], (key, len(val), cut)
        have = list(acc) if isinstance(acc, list) else []
        for k, el in enumerate(val):
            idx = start + k
            assert idx <= len(have), f"{key}: {idx}번 항목 앞이 비었다(받은 {len(have)}건)"
            if idx < len(have):
                have[idx] = _merge(have[idx], el, at + [idx], pieces)
            else:
                have.append(_merge(None, el, at + [idx], pieces))
        return have
    if isinstance(val, dict):
        have = dict(acc) if isinstance(acc, dict) else {}
        for k, v in val.items():
            have[k] = _merge(have.get(k), v, at + [k], pieces)
        return have
    return val


def _reassemble(pages: list) -> dict:
    """한 호출(같은 인자)의 쪽들을 이어 원래 응답을 만든다 — 쪽마다 되풀이되는 칸은 값이 같아 덮어써도 된다."""
    acc: dict = {}
    for _text, res in pages:
        pieces = {p["path"]: p for p in (res.get("page") or {}).get("pieces") or []}
        acc = _merge(acc, {k: v for k, v in res.items() if k != "page"}, [], pieces)
    return acc


# ── A-transcript-size — 전사 한 쪽이 상한 안에 들고, 쪽을 따라가면 원장의 발언 전부를 받는다 ───────────────────
def test_전사_full_한_쪽이_MCP_결과_상한을_넘지_않고_따라가면_원장의_발언을_전부_받는다(monkeypatch, tmp_path):
    job = _job(monkeypatch, tmp_path)
    pages = _follow("deliberate_transcript", {"job_id": job["id"], "full": True})
    sizes = [len(t) for t, _r in pages]
    assert max(sizes) <= CLAUDE_MCP_RESULT_CHARS, (
        f"한 쪽이 {max(sizes):,}자다 — Claude Code 의 MCP 결과 상한({CLAUDE_MCP_RESULT_CHARS:,}자 ≈ 25,000토큰)을 넘는다. "
        f"쪽 크기: {sizes[:8]}")
    # 묶음(offset)마다 쪽을 이어 붙여 턴을 모은다 — 원장의 턴을 full 로 본 것과 같아야 한다(잘라 버린 것이 없다).
    windows: dict = {}
    for text, res in pages:
        windows.setdefault(res["offset"], []).append((text, res))
    got = []
    for off in sorted(windows):
        got += _reassemble(windows[off])["turns"]
    want = [delib_jobs._turn_view(t, True) for t in job["turns"]]
    assert len(got) == len(want) == len(job["turns"]), (len(got), len(want))
    for g, w in zip(got, want):
        assert g == w, f"{w.get('persona')} {w.get('round')}R — 쪽을 이어 붙인 턴이 원장과 다르다"
    huge = next(t for t in got if t["persona"] == "huge-seat" and t["round"] == 1)
    assert len(huge["say"]) > CLAUDE_MCP_RESULT_CHARS, "쪽보다 큰 턴 하나를 만들지 못했다 — 시험 전제가 깨졌다"


def test_전사_쪽의_묶음_안내는_다음_묶음과_남은_수를_준다(monkeypatch, tmp_path):
    """종전엔 total·offset·limit 만 줘서 호출자가 다음 묶음을 셈해야 했다 — 쪽이 글자로도 나뉘면 그 셈이 틀리기 쉽다."""
    job = _job(monkeypatch, tmp_path, seats=4, rounds=2)
    total = len(job["turns"])
    res = json.loads(_wire("deliberate_transcript", {"job_id": job["id"], "limit": 3}))
    assert (res["total"], res["next_offset"], res["remaining"]) == (total, 3, total - 3), res
    last = json.loads(_wire("deliberate_transcript", {"job_id": job["id"], "offset": total - 2, "limit": 3}))
    assert (last["next_offset"], last["remaining"]) == (None, 0), last


def test_전사_도구_설명이_쪽을_따라가는_법과_쪽_크기를_적는다():
    import mcp_pages
    desc = next(t.description for t in asyncio.run(m.mcp.list_tools()) if t.name == "deliberate_transcript")
    assert "limit 을 줄여라" not in desc, "글자로 쪽을 나누므로 limit 을 줄일 까닭이 없다 — 낡은 안내다"
    assert f"{mcp_pages.PAGE_CHARS:,}자" in desc and "page.next" in desc and "next_offset" in desc, desc
    assert mcp_pages.PAGE_CHARS == CLAUDE_MCP_RESULT_CHARS, "쪽 크기 기본값이 Claude Code 상한의 환산과 다르다"


# ── 쪽 나누기 자체(mcp_pages) — 어떤 모양의 응답이든 쪽마다 상한 안, 이으면 원래 응답 ─────────────────────────
import random  # noqa: E402

import mcp_pages  # noqa: E402

_ALPHA = "가나다라 마바사아자차\n\"\\\t/{}[],:\x01abc😀"


def _rand_text(rng) -> str:
    return "".join(rng.choice(_ALPHA) for _ in range(rng.choice((0, 1, 7, 60, 400, 2_500, 7_000))))


def _rand_value(rng, depth: int = 0):
    r = rng.random()
    if depth >= 3 or r < 0.4:
        return rng.choice((lambda: _rand_text(rng), lambda: rng.randint(-10 ** 6, 10 ** 12),
                           lambda: round(rng.random() * 10 ** rng.randint(0, 25), 3), lambda: None,
                           lambda: rng.random() < 0.5, lambda: [], lambda: {}))()
    if r < 0.7:
        return [_rand_value(rng, depth + 1) for _ in range(rng.randint(1, 6))]
    return {f"k{i}_{rng.randint(0, 9)}": _rand_value(rng, depth + 1) for i in range(rng.randint(1, 6))}


def _all_pages(out, ids, again, tail=()):
    first = mcp_pages.paged(out, part=1, again=again, ids=ids, tail=tail)
    if "page" not in first:
        return [first]
    pages = [first] + [mcp_pages.paged(out, part=k, again=again, ids=ids, tail=tail)
                       for k in range(2, first["page"]["parts"] + 1)]
    for k, p in enumerate(pages, 1):
        assert p["page"]["part"] == k and p["page"]["next"] == ({**again, "part": k + 1} if k < len(pages) else None)
    return pages


@pytest.mark.parametrize("seed", range(40))
def test_아무_모양의_응답도_쪽마다_상한_안이고_이으면_원래_응답이다(monkeypatch, seed):
    rng = random.Random(seed)
    budget = rng.randint(mcp_pages.PAGE_FLOOR, 9_000)
    monkeypatch.setattr(mcp_pages, "PAGE_CHARS", budget)
    out = {"job_id": f"j-{seed}", "status": rng.choice(("running", "done", "interrupted")),
           **{f"f{i}": _rand_value(rng) for i in range(rng.randint(1, 9))}}
    ids = ("job_id", "status")
    pages = _all_pages(out, ids, {"job_id": out["job_id"]})
    for p in pages:
        assert len(mcp_pages.wire(p)) <= budget, (seed, len(mcp_pages.wire(p)), budget)
        assert all(p[k] == out[k] for k in ids), "되풀이 칸이 쪽마다 있어야 한다"
    got = _reassemble([(mcp_pages.wire(p), p) for p in pages]) if len(pages) > 1 else pages[0]
    assert got == out, f"seed {seed}: 쪽을 이은 응답이 원래 응답과 다르다"
    # 결정적이다 — 같은 내용이면 몇 번을 불러도 같은 쪽이다(쪽 번호가 곧 위치다).
    assert pages == _all_pages(out, ids, {"job_id": out["job_id"]})


@pytest.mark.parametrize("seed", range(40))
def test_쪽_길이_셈은_실제_글과_한_글자도_다르지_않다(seed):
    """쪽을 채울 때 세는 길이(_Layout)가 FastMCP 가 내보내는 글의 길이와 정확히 같아야 상한을 지키면서 쪽을 꽉 채운다."""
    rng = random.Random(1000 + seed)
    content = {f"f{i}": _rand_value(rng) for i in range(rng.randint(1, 9))}
    envelope = {"job_id": "j", "page": {"part": 1, "parts": 3, "next": None, "pieces": [], "note": "안내"}}
    lay = mcp_pages._Layout()
    leaves, _lists = mcp_pages._flatten(content)
    for at, v in leaves:
        lay.take(*lay.cost(at, mcp_pages._jlen(v)))
    assert len(mcp_pages.wire(envelope)) + lay.size == len(mcp_pages.wire({**envelope, **content}))


def test_쪽_크기_안의_응답은_손대지_않는다():
    out = {"job_id": "j", "status": "done", "decision": "짧은 결정문"}
    assert mcp_pages.paged(out, part=1, again={"job_id": "j"}, ids=("job_id",)) is out
    with pytest.raises(ValueError, match="한 쪽"):
        mcp_pages.paged(out, part=2, again={"job_id": "j"}, ids=("job_id",))


def test_없는_쪽을_달라면_몇_쪽까지_있는지_말한다(monkeypatch):
    monkeypatch.setattr(mcp_pages, "PAGE_CHARS", mcp_pages.PAGE_FLOOR)
    out = {"job_id": "j", "decision": "가" * 20_000}
    n = mcp_pages.paged(out, part=1, again={"job_id": "j"}, ids=("job_id",))["page"]["parts"]
    with pytest.raises(ValueError, match=f"1~{n}"):
        mcp_pages.paged(out, part=n + 1, again={"job_id": "j"}, ids=("job_id",))


def test_뒤로_보낸_칸은_마지막에_오고_첫_쪽에는_작은_칸이_다_실린다(monkeypatch):
    """진행 조회는 화두가 크면 상태 칸이 뒤 쪽으로 밀렸다 — 큰 칸을 뒤로 보내면 첫 쪽에 상태가 다 실린다."""
    monkeypatch.setattr(mcp_pages, "PAGE_CHARS", 6_000)
    out = {"job_id": "j", "question": "화" * 30_000, "stage": "r2", "step": "2라운드 좌석 발언", "idle_s": 3.5}
    first = mcp_pages.paged(out, part=1, again={"job_id": "j"}, ids=("job_id",), tail=("question",))
    assert all(k in first for k in ("stage", "step", "idle_s")), list(first)


def test_도는_전사에_턴이_붙어도_앞_쪽의_경계는_그대로다(monkeypatch):
    """쪽 경계는 되풀이 칸(total·note …)의 실제 값이 아니라 상한으로 잡는다 — 뒤에 붙는 턴이 앞 쪽을 흔들지 않는다."""
    monkeypatch.setattr(mcp_pages, "PAGE_CHARS", 8_000)
    turns = [{"round": 1, "persona": f"s{i}", "say": _para(f"s{i}", 2_500 + 700 * i)} for i in range(6)]

    def pages_of(ts, note):
        out = {"job_id": "j", "total": len(ts), "offset": 0, "limit": 40, "next_offset": None, "remaining": 0,
               "turns": ts, "note": note}
        return _all_pages(out, m._TRANSCRIPT_IDS, {"job_id": "j", "offset": 0, "limit": 40}, tail=("note",))

    before = pages_of(turns, None)
    after = pages_of(turns + [{"round": 2, "persona": "s9", "say": _para("s9", 9_000)}],
                     "이 묶음의 7턴은 say 가 화면용으로 줄인 글이다(say_clipped · 온전한 길이는 say_full_chars).")
    assert len(after) > len(before)
    def cuts(p):        # 경계 — 어느 칸의 어디부터 어디까지(of 는 지금 전체 길이라 턴이 붙으면 는다)
        return [(x["path"], x["from"], x["to"]) for x in p["page"]["pieces"]]

    for b, a in zip(before[:-1], after):
        assert b["turns"] == a["turns"] and cuts(b) == cuts(a), "앞 쪽의 경계가 움직였다"


# ── 다른 deliberate_* 도구 — 결과·진행·목록·시작·이어하기도 한 응답이 상한 안이고, 쪽을 이으면 원래 응답이다 ─────────────
def _paged_whole(name: str, args: dict, want: dict) -> list:
    """쪽을 따라가며 상한을 보고, 이은 응답이 원래 응답(want)과 같은지 본다."""
    pages = _follow(name, args)
    sizes = [len(t) for t, _r in pages]
    assert max(sizes) <= CLAUDE_MCP_RESULT_CHARS, f"{name}: 한 쪽이 {max(sizes):,}자다(쪽 크기 {sizes[:6]})"
    got = _reassemble(pages)
    assert got == want, f"{name}: 쪽을 이은 응답이 원래 응답과 다르다 — 다른 칸: " + str(
        sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k)))
    return pages


def test_결과_회수가_상한을_넘지_않고_쪽을_이으면_결정문_전문이다(monkeypatch, tmp_path):
    """넓은 창의 리스크 심사 결정문은 2만~6만 자다 — 한 응답에 다 실으면 상한을 넘어 결정문 뒤가 파일로 간다."""
    job = _job(monkeypatch, tmp_path, seats=6, rounds=2, chair="결정: " + _para("결정문", 60_000))
    want = delib_jobs.summary(job, full=True)
    assert len(want["decision"]) > 50_000, "결정문이 짧다 — 시험 전제가 깨졌다"
    pages = _paged_whole("deliberate_result", {"job_id": job["id"]}, want)
    assert pages[0][1]["status"] == "done" and pages[0][1]["decision"], "첫 쪽에 상태와 결정문 머리가 실린다"


def test_진행_조회는_화두가_커도_상한_안이고_첫_쪽에_상태가_다_실린다(monkeypatch, tmp_path):
    """화두에 변경 요지를 통째로 넣은 심사는 화두만 수만 자다 — 매번 부르는 진행 조회가 상한을 넘으면 상태를 못 본다."""
    job = _job(monkeypatch, tmp_path, seats=3, rounds=2, question="[리스크심사 P1 T1] " + _para("변경 요지", 40_000))
    pages = _follow("deliberate_status", {"job_id": job["id"]})
    first = pages[0][1]
    assert all(k in first for k in ("status", "stage", "round", "total_rounds", "turn_count", "warnings",
                                    "evidence_omitted")), f"첫 쪽에 상태 칸이 없다: {sorted(first)}"
    whole = delib_jobs.summary(job)          # 원래 응답 — 끝난 잡이라 시각에 따라 변하는 칸(idle_s)이 없다
    whole.update(evidence_omitted=job.get("evidence_omitted") or [], warnings=job.get("warnings") or [],
                 warnings_total=int(job.get("warnings_total") or len(job.get("warnings") or [])))
    _paged_whole("deliberate_status", {"job_id": job["id"]}, whole)


def test_목록이_화두가_긴_잡이_많아도_상한_안이고_쪽을_이으면_전부다(monkeypatch, tmp_path):
    base = _job(monkeypatch, tmp_path, seats=3, rounds=2, question="[리스크심사 P1 T1] " + _para("변경 요지", 3_000))
    for k in range(19):
        delib_jobs._persist({**base, "id": f"risk-review-2026101{k % 10}-0000{k:02d}-x{k:05d}",
                             "started_at": base["started_at"] - 60 * (k + 1)})
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    want = {"jobs": delib_jobs.list_jobs(20), "running": 0, "queued": 0, "running_max": delib_jobs.MAX_RUNNING,
            "running_max_per_user": delib_jobs.MAX_RUNNING_PER_USER, "queue_max": delib_jobs.QUEUE_MAX}
    assert len(want["jobs"]) == 20
    _paged_whole("deliberate_list", {"limit": 20}, want)


async def _quick_entry(_app, q, _groups, _opts, _user, _pat, _x):
    yield d._sse("delib", {"kind": "decision", "text": "결정: 진행한다"})
    yield d._sse("done", {})


def test_시작과_이어하기의_응답은_화두가_커도_상한_안이고_전문은_진행_조회로_받는다(monkeypatch, tmp_path):
    """시작 응답은 화두를 되돌려 보인다 — 받은 화두가 수만 자면 그 메아리만으로 상한을 넘는다. 시작 도구는 다시 부르면 심의가
    또 열리므로 쪽을 나눠 '다음 쪽' 으로 다시 부르게 할 수 없다 — 머리만 보이고 전문은 진행 조회(쪽)로 받게 한다."""
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(delib_jobs, "_PENDING", {})
    monkeypatch.setattr(d, "run_deliberation", _quick_entry)
    monkeypatch.setattr(m, "_APP", object())
    q = "[리스크심사 P1 T1] " + _para("변경 요지", 40_000)

    async def go():
        started = await m.mcp.call_tool("deliberate_start", {"question": q})
        await asyncio.wait(list(delib_jobs._TASKS.values()))
        jid = json.loads("".join(b.text for b in (started if isinstance(started, list) else started[0])))["job_id"]
        cont = await m.mcp.call_tool("deliberate_continue", {"previous_job_id": jid, "human_note": "더 본다"})
        await asyncio.wait(list(delib_jobs._TASKS.values()))
        return [("".join(b.text for b in (r if isinstance(r, list) else r[0]))) for r in (started, cont)]

    for text in asyncio.run(go()):
        res = json.loads(text)
        assert len(text) <= CLAUDE_MCP_RESULT_CHARS, f"시작·이어하기 응답이 {len(text):,}자다"
        assert res["question_chars"] == len(q) and q.startswith(res["question"]), "화두 머리와 전체 길이를 보인다"
        assert "deliberate_status" in res["note"], res["note"]
        whole = _reassemble(_follow("deliberate_status", {"job_id": res["job_id"]}))
        assert whole["question"] == q, "진행 조회로 화두 전문을 받지 못했다"

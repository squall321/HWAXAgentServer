# 전문가 고르기 기준선 창구 — POST /seat-eval 이 엔진 함수(_discover · _summon · _screen_one · _seat_tool_prefer)를 그대로 불러 고른 결과와 신호를 낸다(HWAXPortal docs/request-1029 design-e5 E5-1)
"""같은 코드로 잰다 — 다시 짜면 재는 것이 진짜와 달라진다(design-e5 2.6).

질의 하나마다 겹(layer)을 고른다.
  rec       추천 그대로 — recommend_agents(q, top_k=40). 운영자를 섞은 순위 · score · desc_match · matched_* · low_confidence
  discover  심의 첫 좌석 — deliberation._discover(tools, q, N_PERSONAS) 그대로(운영자 · 중복을 거른 뒤라 모자랄 수 있다)
  summon    띵킹 소집 — thinking._summon(tools, q, CANDIDATES) 그대로(summon_k 로 바꿀 수 있다 — 위임 홉은 3)
  screen    띵킹 예심 — 소집한 좌석마다 thinking._screen_one 그대로, 물어볼 좌석은 통과한 앞 MAX_ANSWERS 명
  tools     지목 전문가의 우선 도구 — 챗과 같은 app._persona_meta → _seat_tool_prefer(…, CHAT_PERSONA_PREFER_N)
LLM 겹(심의가 돌지 않을 때만 — GLM 을 함께 쓴다) —
  judge     띵킹 본심 — 물어볼 좌석마다 thinking._judge_one
  counter   반대 도메인 좌석 — deliberation._counter_seats
  axes      LLM 이 도메인을 고르는 갈래(design-e5 3.3 안 4) — app._seat_axes 를 대화 한 줄(그 질문)에 그대로

읽기만 한다. 엔진 함수에 넘기는 도구 사전은 읽기 도구(READ_TOOLS)만 담고, 부른 도구를 모두 적어 응답의 calls 에 싣는다 — 쓰기 도구는 사전에
없어 부를 길이 없다. 공유 비밀 · 박스 자리는 seat_box 다(게이트웨이 연결의 Authorization 그대로 — 게이트웨이 /search-eval 과 같은 꼴). 기록은 리포
밖 박스 자리(box_dir()/runs)에 질의 글 없이 남는다. 한 번에 하나만 돈다(AIDataHub 를 수백 번 부른다).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

from seat_box import KST, ROOT, append_jsonl, authorized, box_dir, private_dir, secret

READ_TOOLS = frozenset({"recommend_agents", "get_agent_session", "agent_search", "list_agents", "list_agent_domains"})
LAYERS = ("rec", "discover", "summon", "screen", "tools")
LLM_LAYERS = ("judge", "counter", "axes")
_NEEDS = {"screen": ("summon",), "judge": ("summon", "screen"), "counter": ("discover",)}
REC_K = 40                      # 심의 전 미리보기(EXPERT_CANDIDATE_TOP_K)와 같은 깊이 — 운영자를 포함한 순위 40
MAX_ITEMS = 50                  # 한 요청의 질의 수 상한 — 러너가 나눠 보낸다
MAX_Q = 4000
_FIELDS = ("items", "layers", "run", "concurrency", "summon_k")
_ITEM_FIELDS = ("qid", "q", "expert")
_RUN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_QID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_BUSY = {"on": False}


def sha12(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:12]


def health(app) -> dict:
    return {"secret": bool(secret(app)), "busy": _BUSY["on"]}


# ── 읽기 전용 도구 사전 ────────────────────────────────────────────────────────
class _ReadOnly:
    """도구 하나를 감싼다 — 부르기 전에 이름을 적는다. 엔진의 _call 은 ainvoke 만 쓴다."""

    def __init__(self, tool, name: str, calls: list):
        self._t, self.name, self._calls = tool, name, calls
        self.description = getattr(tool, "description", "") or ""

    async def ainvoke(self, args):
        self._calls.append(self.name)
        return await self._t.ainvoke(args)


def read_only(tools: dict, calls: list) -> dict:
    """읽기 도구만 담은 사전 — 그 밖의 도구는 사전에 없어 엔진 함수가 불러도 None(_call 의 '도구 없음')이다."""
    return {n: _ReadOnly(t, n, calls) for n, t in (tools or {}).items() if n in READ_TOOLS}


# ── 요청 검사 ─────────────────────────────────────────────────────────────────
def check(body) -> tuple[dict | None, str]:
    """(정리한 요청, 까닭) — 까닭이 있으면 422 다. 선언하지 않은 칸은 조용히 버리지 않고 거절한다(/limits 와 같은 규칙)."""
    if not isinstance(body, dict):
        return None, "본문은 JSON 객체여야 한다"
    extra = sorted(set(body) - set(_FIELDS))
    if extra:
        return None, f"선언하지 않은 칸 {[str(x)[:40] for x in extra[:10]]} — 받는 칸은 {list(_FIELDS)}"
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        return None, f"items 는 질의 1 ~ {MAX_ITEMS}건의 목록이다"
    layers = body.get("layers") or list(LAYERS)
    if not isinstance(layers, list) or not layers or any(x not in LAYERS + LLM_LAYERS for x in layers):
        return None, f"layers 는 {list(LAYERS + LLM_LAYERS)} 가운데서 고른다"
    eff = set(layers)
    for x in list(eff):
        eff.update(_NEEDS.get(x, ()))
    run = body.get("run")
    if run is not None and not (isinstance(run, str) and _RUN_RE.match(run)):
        return None, "run 은 영문 · 숫자 · '._-' 64자 이내다"
    conc = body.get("concurrency", 2)
    if not isinstance(conc, int) or isinstance(conc, bool) or not 1 <= conc <= 4:
        return None, "concurrency 는 1 ~ 4 의 정수다"
    sk = body.get("summon_k")
    if sk is not None and (not isinstance(sk, int) or isinstance(sk, bool) or not 1 <= sk <= REC_K):
        return None, f"summon_k 는 1 ~ {REC_K} 의 정수다"
    seen, clean = set(), []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            return None, f"items[{i}] 는 객체여야 한다"
        bad = sorted(set(it) - set(_ITEM_FIELDS))
        if bad:
            return None, f"items[{i}] 에 선언하지 않은 칸 {[str(x)[:40] for x in bad[:10]]} — 받는 칸은 {list(_ITEM_FIELDS)}"
        qid, q, ex = it.get("qid"), it.get("q", ""), it.get("expert")
        if not (isinstance(qid, str) and _QID_RE.match(qid)) or qid in seen:
            return None, f"items[{i}].qid 가 없거나 꼴이 틀렸거나 겹친다"
        if not isinstance(q, str) or len(q) > MAX_Q or (not q.strip() and not ex):
            return None, f"items[{i}].q 는 {MAX_Q}자 이내의 글이다(도구 겹만 재면 비워도 된다)"
        if ex is not None and not (isinstance(ex, str) and _KEY_RE.match(ex)):
            return None, f"items[{i}].expert 는 전문가 키다"
        seen.add(qid)
        clean.append({"qid": qid, "q": q.strip(), "expert": ex})
    order = [x for x in LAYERS + LLM_LAYERS if x in eff]
    return {"items": clean, "layers": order, "run": run, "concurrency": conc, "summon_k": sk}, ""


# ── 겹 ───────────────────────────────────────────────────────────────────────
def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _tool_failed(raw) -> str:
    """도구 실패 문구면 그 글(앞 200자), 아니면 "". 실패는 예외가 아니라 글로 온다(_call · app._cap_tool)."""
    if raw is None:
        return "도구가 없다"
    if isinstance(raw, str) and raw.lstrip().startswith(("(tool ", "✖")):
        return raw.strip()[:200]
    return ""


async def _rec(tools: dict, q: str) -> dict:
    from deliberation import _call, _first_dict, _parse_json, is_operator  # noqa: PLC0415 — 순환 import 회피(늦은 로드)
    t0 = time.monotonic()
    raw = await _call(tools, "recommend_agents", {"q": q, "top_k": REC_K})
    bad = _tool_failed(raw)
    d = None if bad else _parse_json(raw)
    if isinstance(d, list):
        d = {"agents": d}
    if not isinstance(d, dict):
        return {"error": bad or "추천 응답을 해석하지 못했다", "ranked": [], "ms": _ms(t0)}
    items = d.get("agents") or d.get("recommendations") or d.get("data") or []
    ranked = []
    for it in items if isinstance(items, list) else []:
        it = _first_dict(it)
        key = str(it.get("agent_type") or it.get("id") or "").strip()
        if not key:
            continue
        ranked.append({"key": key, "score": it.get("score"), "desc_match": it.get("desc_match"),
                       "matched_records": it.get("matched_records"), "matched_sections": it.get("matched_sections"),
                       "matched_samples": it.get("matched_samples"), "low_confidence": bool(it.get("low_confidence")),
                       "op": is_operator(key)})
    return {"ranked": ranked, "ranked_total": d.get("ranked_total"), "fallback": bool(d.get("fallback")),
            "low_confidence": any(r["low_confidence"] for r in ranked), "ms": _ms(t0)}


async def _discover_layer(tools: dict, q: str) -> dict:
    import deliberation  # noqa: PLC0415
    t0 = time.monotonic()
    seats = await deliberation._discover(tools, q, deliberation.N_PERSONAS)
    return {"seats": [s["key"] for s in seats], "want": deliberation.N_PERSONAS, "ms": _ms(t0)}


async def _summon_layer(tools: dict, q: str, k: int) -> list[dict]:
    import thinking  # noqa: PLC0415
    return await thinking._summon(tools, q, k, set())


async def _screen_layer(tools: dict, q: str, cands: list[dict]) -> list[dict]:
    """띵킹 _run_hop 과 같이 — 한 번에 도는 수를 DELIB_KNOWLEDGE_CONCURRENCY 로 묶고 좌석마다 _screen_one 그대로."""
    import thinking  # noqa: PLC0415
    from deliberation import _KN_CONC  # noqa: PLC0415
    if not cands:
        return []
    sem = asyncio.Semaphore(max(1, min(len(cands), _KN_CONC) if _KN_CONC > 0 else len(cands)))

    async def one(s):
        async with sem:
            return await thinking._screen_one(tools, s, q)
    return list(await asyncio.gather(*[one(s) for s in cands]))


def _screen_out(screened: list[dict]) -> dict:
    import thinking  # noqa: PLC0415
    passed = [s["key"] for s in screened if s.get("passed")]
    return {"seats": [{"key": s["key"], "passed": bool(s.get("passed")), "hits": s.get("hits"), "top": s.get("top"),
                       "note": s.get("search_note") or ""} for s in screened],
            "asked": passed[:thinking.MAX_ANSWERS], "capped": passed[thinking.MAX_ANSWERS:], "max": thinking.MAX_ANSWERS}


async def _tools_layer(app, appmod, key: str, q: str) -> dict:
    """챗이 지목 전문가에게 앞세우는 도구 — app._agent_stream 과 같은 함수 · 같은 수. 운영자는 자기 앱 도구로 묶이므로 우선 목록을 쓰지 않는다."""
    t0 = time.monotonic()
    persona = await appmod._persona_meta(app, [], key)
    out: dict = {"expert": key, "operator": bool(persona.get("operator")), "note": persona.get("note") or ""}
    names = list(await asyncio.to_thread(appmod._tools_map))
    if persona.get("operator"):
        out["prefer"] = None
    else:
        out["prefer"] = appmod._seat_tool_prefer(names, persona, q, appmod._PERSONA_PREFER_N) if names else []
    m = appmod._TOOLS_MAP_CACHE.get("map") or {}
    shown = out["prefer"] or []
    out["app"] = {n: m.get(n, "") for n in shown}
    out["area"] = {n: appmod._area_of(n)[0] for n in shown}
    # 켠 쪽(E5-5)이 골랐을 도구 — 코어 어휘표 personas 칸이 있을 때만(그림자와 같은 셈)
    try:
        v = appmod._persona_links()
        if v and v.get("present") and names:
            import persona_links  # noqa: PLC0415
            out["link"] = persona_links.link_tools(v, key=key, names=names, question=q, n=appmod._PERSONA_PREFER_N,
                                                   tool_app=m.get, tool_area=lambda n: appmod._area_of(n)[0])
    except Exception as exc:  # noqa: BLE001 — 연결 표 셈이 도구 겹을 막지 않는다
        out["link_error"] = f"{type(exc).__name__}: {exc}"[:200]
    out["ms"] = _ms(t0)
    return out


async def _judge_layer(app, tools: dict, q: str, screened: list[dict]) -> dict:
    """띵킹 본심 — 물어볼 좌석(통과한 앞 MAX_ANSWERS)에게만 역할을 되살려 묻는다(_run_hop 과 같은 순서 · 같은 함수)."""
    import thinking  # noqa: PLC0415
    from deliberation import _restore_role  # noqa: PLC0415
    t0 = time.monotonic()
    live = [dict(s) for s in screened if s.get("passed")][:thinking.MAX_ANSWERS]
    if not live:
        return {"seats": [], "answered": [], "no_answer": True, "ms": _ms(t0)}
    roles = await asyncio.gather(*[_restore_role(tools, s["key"], s.get("name") or "") for s in live])
    for s, r in zip(live, roles):
        s["role"] = r
    llm, tok = await thinking._think_llm(app)
    try:
        sem = asyncio.Semaphore(max(1, thinking.CONCURRENCY))
        got = await asyncio.gather(*[thinking._judge_one(llm, s, q, sem) for s in live])
    finally:
        if tok is not None:
            import external_llm  # noqa: PLC0415
            try:
                external_llm.ctx_override.reset(tok)
            except ValueError:
                pass
    seats = [{"key": r["key"], "verdict": r.get("verdict"), "refer": r.get("refer") or [], "error": r.get("error") or ""}
             for r in got]
    answered = [s["key"] for s in seats if s["verdict"] == "answer"]
    return {"seats": seats, "answered": answered, "no_answer": not answered, "ms": _ms(t0)}


async def _counter_layer(app, tools: dict, q: str, seated: list[str]) -> dict:
    import deliberation  # noqa: PLC0415
    t0 = time.monotonic()
    llm = getattr(app.state, "delib_llm", None) or app.state.llm
    got = await deliberation._counter_seats(tools, llm, q, [{"key": k} for k in seated], deliberation._COUNTER_SEATS)
    return {"seats": [{"key": c["key"], "axis": c.get("axis") or ""} for c in got], "limit": deliberation._COUNTER_SEATS,
            "ms": _ms(t0)}


async def _axes_layer(app, appmod, tools: dict, q: str) -> dict:
    """안 4 — 심의 전 미리보기의 _seat_axes 를 그 질문 한 줄을 대화로 주어 그대로 부른다. 축마다 추천 앞 3명(운영자 뺌)을 함께 낸다
    (/deliberate/experts 의 axis.seats 와 같은 셈)."""
    from deliberation import _call, _first_dict, _parse_json, is_operator  # noqa: PLC0415
    t0 = time.monotonic()
    axes = await appmod._seat_axes(app.state.llm, tools, q, [{"role": "user", "content": q}])
    out = []
    for a in axes:
        raw = await _call(tools, "recommend_agents", {"q": a["phrase"], "top_k": 8})
        d = _parse_json(raw) if not _tool_failed(raw) else None
        items = (d.get("agents") or []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
        keys = []
        for it in items:
            k = str(_first_dict(it).get("agent_type") or "").strip()
            if k and not is_operator(k) and k not in keys:
                keys.append(k)
        out.append({"domain": a["domain"], "phrase": a["phrase"], "seats": keys[:3]})
    return {"axes": out, "ms": _ms(t0)}


async def _one(app, appmod, tools: dict, it: dict, layers: list, summon_k: int, pool: set | None) -> dict:
    q, out = it["q"], {"qid": it["qid"]}
    try:
        if q and "rec" in layers:
            out["rec"] = await _rec(tools, q)
        if q and "discover" in layers:
            out["discover"] = await _discover_layer(tools, q)
        screened: list = []
        if q and "summon" in layers:
            t0 = time.monotonic()
            cands = await _summon_layer(tools, q, summon_k)
            out["summon"] = {"cands": [{k: c.get(k) for k in ("key", "score", "desc_match", "sections", "records")}
                                       for c in cands], "k": summon_k, "ms": _ms(t0)}
            if "screen" in layers:
                t1 = time.monotonic()
                screened = await _screen_layer(tools, q, cands)
                out["screen"] = {**_screen_out(screened), "ms": _ms(t1)}
        if "tools" in layers and it.get("expert"):
            out["tools"] = await _tools_layer(app, appmod, it["expert"], q)
        if q and "judge" in layers:
            out["judge"] = await _judge_layer(app, tools, q, screened)
        if q and "counter" in layers:
            out["counter"] = await _counter_layer(app, tools, q, (out.get("discover") or {}).get("seats") or [])
        if q and "axes" in layers:
            out["axes"] = await _axes_layer(app, appmod, tools, q)
    except Exception as exc:  # noqa: BLE001 — 한 질의의 실패가 묶음을 죽이지 않는다
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    from deliberation import is_operator  # noqa: PLC0415
    picked = list((out.get("discover") or {}).get("seats") or []) + [c["key"] for c in (out.get("summon") or {}).get("cands") or []]
    out["guard"] = {"operators": sum(1 for k in picked if is_operator(k)),
                    "unknown": (sum(1 for k in picked if k not in pool) if pool else None)}
    return out


# ── 판 지문 ───────────────────────────────────────────────────────────────────
def _git_head(path: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def _aidh_health(base: str) -> dict:
    """AIDataHub /api/system/health — 판 · 빌드 · 임베더 슬롯. 프록시를 타지 않는다(cae00 셸의 사내 프록시 — tools/delib_ab 와 같은 까닭)."""
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"{base.rstrip('/')}/api/system/health", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — 지문 한 칸이 평가를 막지 않는다
        return {"error": f"{type(exc).__name__}"}
    slot = d.get("embed_slot") if isinstance(d.get("embed_slot"), dict) else {}
    return {"version": d.get("version"), "build": d.get("build"), "embedder": d.get("embedder"),
            "embed_slot": {"active": slot.get("active"), "model": slot.get("model")}}


_VER_TTL_S = 120.0                      # 판 지문을 다시 재는 간격 — 러너는 묶음을 잇달아 보낸다(전문가 목록은 한 번에 수 MB 일 수 있다)
_VER_CACHE: dict = {"at": 0.0, "ver": None, "pool": None}


async def _pool(tools: dict) -> tuple[dict, set | None]:
    """풀 지문 — 키 · 갱신 시각(list_agents 전체 꼴). 그 응답을 못 읽으면(창구 결과 상한 · 실패) 키만(compact)으로 잰다(updated 거짓)."""
    from deliberation import _call, _first_dict, _parse_json_multi, is_operator  # noqa: PLC0415
    for args, with_upd in (({}, True), ({"compact": True}, False)):
        try:
            raw = await _call(tools, "list_agents", args)
            rows = [] if _tool_failed(raw) else _parse_json_multi(raw)
        except Exception:  # noqa: BLE001
            rows = []
        pairs = sorted({(str(_first_dict(r).get("agent_type") or _first_dict(r).get("id") or "").strip(),
                         str(_first_dict(r).get("updated_at") or "") if with_upd else "") for r in rows} - {("", "")})
        keys = {k for k, _u in pairs if k}
        if keys:
            return ({"n": len(keys), "operators": sum(1 for k in keys if is_operator(k)), "updated": with_upd,
                     "fp": sha12(json.dumps(pairs, ensure_ascii=False).encode())}, keys)
    return {"error": "전문가 목록(list_agents)을 받지 못했다"}, None


async def version(app, appmod, tools: dict) -> tuple[dict, set | None]:
    """(판 지문, 풀의 전문가 키) — 같은 날 같은 풀에서 끈 쪽과 켠 쪽을 견주는 근거(L8). 칸 하나를 못 재면 그 칸만 비운다. _VER_TTL_S 동안 다시 쓴다."""
    if _VER_CACHE["ver"] is not None and time.monotonic() - _VER_CACHE["at"] < _VER_TTL_S:
        return _VER_CACHE["ver"], _VER_CACHE["pool"]
    pool, pool_keys = await _pool(tools)
    aidh = await asyncio.to_thread(_aidh_health, getattr(appmod, "AIDH_HTTP", "http://127.0.0.1:8001"))
    aidh["repo"] = await asyncio.to_thread(_git_head, ROOT.parent / "AIDataHub")
    ft = ROOT.parent / "AIDataHub" / "api_server" / "config" / "field_terms.json"
    try:
        ft_sha = sha12(ft.read_bytes())
    except OSError:
        ft_sha = None
    cv = appmod._core_vocab()
    tm = await asyncio.to_thread(appmod._tools_map)
    ver = {"at": datetime.now(KST).isoformat(timespec="seconds"), "agent": await asyncio.to_thread(_git_head, ROOT),
           "aidh": aidh, "field_terms": ft_sha, "pool": pool,
           "core_vocab": (cv or {}).get("fingerprint"),
           "tools_map": {"n": len(tm or {}), "fp": sha12(json.dumps(sorted((tm or {}).items())).encode())},
           "knobs": _knobs()}
    _VER_CACHE.update(at=time.monotonic(), ver=ver, pool=pool_keys)
    return ver, pool_keys


def _knobs() -> dict:
    """고르기를 바꾸는 손잡이 — 값이 다르면 같은 판이 아니다."""
    import deliberation  # noqa: PLC0415
    import thinking  # noqa: PLC0415
    return {"DELIB_PERSONAS": deliberation.N_PERSONAS, "DELIB_COUNTER_SEATS": deliberation._COUNTER_SEATS,
            "THINK_CANDIDATES": thinking.CANDIDATES, "THINK_MAX_ANSWERS": thinking.MAX_ANSWERS,
            "THINK_DESC_FLOOR": thinking.DESC_FLOOR, "THINK_SEARCH_MODE": thinking._SEARCH_MODE}


# ── 창구 ─────────────────────────────────────────────────────────────────────
def _record(run: str, ver: dict, results: list) -> str | None:
    """질의마다 고른 결과 · 신호를 박스 자리에 — 질의 글은 싣지 않는다(평가셋 파일에 있다). 실패는 응답의 record_error 로만 알린다."""
    try:
        d = private_dir(box_dir() / "runs")
        t = int(time.time())
        append_jsonl(d / f"{run}.version.jsonl", {"t": t, **ver})
        for r in results:
            append_jsonl(d / f"{run}.jsonl", {"t": t, "run": run, **r})
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"[:200]


async def handle(app, appmod, headers: dict, body_bytes: bytes) -> tuple[int, dict]:
    """POST /seat-eval — (상태, 본문). 비밀 · 꼴 · 한 번에 하나 · LLM 겹은 심의가 없을 때만."""
    st, why = authorized(app, headers.get("authorization", ""))
    if st:
        return st, {"error": why}
    try:
        body = json.loads(body_bytes or b"{}")
    except ValueError:
        return 422, {"error": "본문이 JSON 이 아니다"}
    req, why = check(body)
    if req is None:
        return 422, {"error": why}
    if any(x in LLM_LAYERS for x in req["layers"]):
        if appmod._delib_load()[0] > 0:
            return 409, {"error": "심의가 도는 중이다 — LLM 겹(judge · counter · axes)은 심의가 없을 때만 돈다(GLM 을 함께 쓴다)"}
        if getattr(app.state, "llm", None) is None:
            return 503, {"error": "LLM 이 없다 — LLM 겹을 잴 수 없다"}
    if _BUSY["on"]:
        return 409, {"error": "평가가 이미 돌고 있다 — 끝난 뒤 다시"}
    _BUSY["on"] = True
    t0 = time.monotonic()
    try:
        import thinking  # noqa: PLC0415
        calls: list = []
        try:
            tools = read_only(await appmod._tools_by_name(app, [], appmod.CATALOG_RESULT_MAX), calls)
        except Exception as exc:  # noqa: BLE001 — 게이트웨이 불통은 503 으로 말한다(500 이 아니라)
            return 503, {"error": f"게이트웨이 도구를 받지 못했다({type(exc).__name__})"}
        if "recommend_agents" not in tools:
            return 503, {"error": "게이트웨이에서 recommend_agents 를 받지 못했다(게이트웨이 · AIDataHub 를 본다)"}
        ver, pool = await version(app, appmod, tools)
        sem = asyncio.Semaphore(req["concurrency"])
        k = req["summon_k"] or thinking.CANDIDATES

        async def gated(it):
            async with sem:
                return await _one(app, appmod, tools, it, req["layers"], k, pool)
        results = list(await asyncio.gather(*[gated(it) for it in req["items"]]))
        out = {"version": ver, "layers": req["layers"], "items": results, "calls": dict(Counter(calls)),
               "read_only": all(c in READ_TOOLS for c in calls), "elapsed_s": round(time.monotonic() - t0, 2)}
        if req["run"]:
            err = _record(req["run"], ver, results)
            if err:
                out["record_error"] = err
        return 200, out
    finally:
        _BUSY["on"] = False

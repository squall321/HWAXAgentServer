# 전문가 고르기 기준선 러너 — 평가셋을 /seat-eval 창구에 나눠 보내 고른 결과를 박스에 남기고, 지표 · 갈래 · 짝 비교(McNemar) · 판 지문을 숫자로만 낸다(HWAXPortal docs/request-1029 design-e5 E5-1)
"""재는 법은 design-e5 2.5 · 2.6, 성공 기준은 2.7 이다. tools/delib_ab.py 처럼 dev 는 스모크이고 판정은 cae00 이다(C-31).

묶음 — 평가셋 자리(tools/seat_evalset.py 가 지은 것, Drive 로 받은 것)의 파일과 박스의 사람 표시.
  A     A.jsonl — 저작 혼동(dev · test 를 따로 낸다). A-name.jsonl 은 이름 묶음(따로)
  P     P.jsonl — 위임 문구 → 띵킹 위임 홉의 소집(top_k 3)에 대상이 드나(진단)
  C     C.jsonl — 카드 유래 → 카드 주인이 추천 상위 5 에 드나(진단)
  H     박스의 사람 표시(seat_questions.export) — 기준. 출처 넷 · '없음' 표시
  N     N.jsonl — 풀 밖이 분명한 질문(N4, 사람이 쓴다 — 없으면 건너뛴다). N1 · N2 는 H 의 '없음' 표시다
  T     T.jsonl — 도구 겹 {qid, expert, apps?, expect_empty?} (앱 전문가 · 비어야 할 전문가 — 사람이 쓴다)
H · N 의 질문 글은 박스 밖으로 나가지 않는다 — 결과 파일 · 보고에는 qid 와 숫자만 싣는다.

  실행:
    python -m tools.seat_eval run --sets <dir> [--only A,P,C,H,N,T] [--layers rec,discover,summon,screen] [--run <id>]
                                  [--url http://127.0.0.1:9009] [--batch 20] [--limit N] [--by <표시자>]
    python -m tools.seat_eval report <결과 자리>
    python -m tools.seat_eval compare <끈 쪽 결과 자리> <켠 쪽 결과 자리>
공유 비밀은 에이전트 서버의 MCP 설정(MCP_CONFIG, 없으면 리포의 mcp_servers.json)의 게이트웨이 Authorization 을 읽는다 — 사람이 옮겨 적지 않는다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_URL = "http://127.0.0.1:9009"
LOW_CONF_DESC = 0.40            # AIDataHub _LOW_CONF_DESC — 어휘 일치 칸의 경계(L3)
SETS = ("A", "P", "C", "H", "N", "T")
LAYERS_OF = {"A": ["rec", "discover", "summon", "screen"], "C": ["rec"], "P": ["summon"], "H": ["rec", "discover", "summon", "screen"],
             "N": ["rec", "discover", "summon", "screen"], "T": ["tools"]}
LLM = ("judge", "counter", "axes")


def dom_of(key: str) -> str:
    k = str(key or "")
    return k.split("-", 1)[0] if "-" in k else k


def branch(dom: str) -> str:
    from tools.seat_evalset import branch as _b  # noqa: PLC0415 — 갈래 셈은 평가셋과 같은 함수
    return _b(dom)


# ── 창구 부르기 ───────────────────────────────────────────────────────────────
def auth_header(path: str | None = None) -> str:
    """에이전트 서버 MCP 설정의 게이트웨이 Authorization — 창구의 공유 비밀(seat_box.secret 과 같은 값)."""
    p = Path(path or os.environ.get("MCP_CONFIG") or ROOT / "mcp_servers.json")
    try:
        conns = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"MCP 설정을 못 읽었다({p.name}: {type(exc).__name__}) — MCP_CONFIG 를 준다") from exc
    for n in sorted(conns, key=lambda n: (n != "gateway", n)):
        h = (conns.get(n) or {}).get("headers") or {}
        v = str(h.get("Authorization") or h.get("authorization") or "")
        if v.startswith("Bearer "):
            return v
    raise SystemExit("MCP 설정에 게이트웨이 Authorization 이 없다 — 창구는 그 값을 공유 비밀로 받는다")


def call(url: str, auth: str, body: dict, timeout: float = 900.0) -> dict:
    """POST /seat-eval — 프록시를 타지 않는다(cae00 셸의 사내 프록시 — tools/delib_ab 와 같은 까닭)."""
    req = urllib.request.Request(f"{url.rstrip('/')}/seat-eval", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": auth}, method="POST")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            why = json.loads(exc.read().decode("utf-8")).get("error")
        except Exception:  # noqa: BLE001
            why = ""
        raise RuntimeError(f"/seat-eval {exc.code} — {why}") from exc


# ── 묶음 읽기 ─────────────────────────────────────────────────────────────────
def _jsonl(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def load_set(name: str, sets: Path | None, by: str | None = None) -> list[dict]:
    """묶음 하나 → [{qid, q, answers(엄격), lenient, kind, …}]. kind — owned(맡은 질문) · none(맡은 사람 없음)."""
    if name == "H":
        import seat_questions  # noqa: PLC0415 — 박스의 사람 표시
        out = []
        for r in seat_questions.export(by):
            none = r.get("none")
            out.append({"qid": r["qid"], "q": r["q"], "src": r.get("src"), "answers": r["primary"],
                        "lenient": r["primary"] + r["also"], "kind": "none" if none else "owned", "none": none,
                        "picked": r.get("picked") or [], "others": r.get("others") or [], "by": r.get("by")})
        return out
    rows = _jsonl((sets or Path(".")) / f"{name}.jsonl")
    out = []
    for r in rows:
        if name == "A":
            out.append({**r, "answers": [r["answer"]], "lenient": r.get("answers_ok") or [r["answer"]], "kind": "owned"})
        elif name == "P":
            out.append({**r, "answers": r.get("answers_ok") or [r["answer"]], "lenient": r.get("answers_ok") or [r["answer"]],
                        "kind": "owned"})
        elif name == "C":
            out.append({**r, "answers": [r["answer"]], "lenient": [r["answer"]], "kind": "owned"})
        elif name == "N":
            out.append({**r, "answers": [], "lenient": [], "kind": "none", "none": r.get("none") or "out_of_pool"})
        elif name == "T":
            out.append({**r, "q": r.get("q") or "", "answers": [], "lenient": [], "kind": "tools"})
    return out


def run(url: str, auth: str, name: str, rows: list[dict], *, layers: list | None, run_id: str, batch: int, out: Path,
        concurrency: int = 2) -> list[dict]:
    """묶음 하나를 나눠 보내고 결과(qid · 겹 · 판 지문 — 질문 글 없이)를 out/<묶음>.results.jsonl 에 둔다."""
    lay = layers or LAYERS_OF[name]
    body_extra = {"summon_k": 3} if name == "P" else {}
    out.mkdir(parents=True, exist_ok=True)
    res, vers = [], []
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        items = [{"qid": r["qid"], "q": r.get("q") or "", **({"expert": r["expert"]} if r.get("expert") else {})} for r in chunk]
        t0 = time.time()
        got = call(url, auth, {"items": items, "layers": lay, "run": f"{run_id}-{name}", "concurrency": concurrency,
                               **body_extra})
        if not got.get("read_only", False):
            raise RuntimeError(f"창구가 읽기 도구 밖을 불렀다 — {got.get('calls')}")
        vers.append(got.get("version") or {})
        res += got.get("items") or []
        print(f"  {name} {min(i + batch, len(rows))}/{len(rows)} · {time.time() - t0:.1f}초 · 부른 도구 {got.get('calls')}", flush=True)
    with open(out / f"{name}.results.jsonl", "w", encoding="utf-8") as f:
        for r in res:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out / f"{name}.version.json").write_text(json.dumps(vers, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


# ── 셈 ───────────────────────────────────────────────────────────────────────
def ranked(res: dict) -> list[str]:
    """운영자를 뺀 추천 순위(키)."""
    return [r["key"] for r in ((res.get("rec") or {}).get("ranked") or []) if not r.get("op")]


def rank_of(keys: list, answers) -> int | None:
    s = set(answers or ())
    for i, k in enumerate(keys, 1):
        if k in s:
            return i
    return None


def desc_of(res: dict, key: str):
    for r in (res.get("rec") or {}).get("ranked") or []:
        if r.get("key") == key:
            return r.get("desc_match")
    return None


def item_metrics(row: dict, res: dict) -> dict:
    """한 질의의 이진 결과 — 짝 비교(McNemar)의 단위다. 그 겹이 없으면 그 칸을 싣지 않는다."""
    m: dict = {}
    keys = ranked(res)
    for tag, ans in (("strict", row["answers"]), ("lenient", row["lenient"])):
        if not ans or "rec" not in res:
            continue
        rk = rank_of(keys, ans)
        m[f"acc1_{tag}"] = rk == 1
        m[f"acc3_{tag}"] = rk is not None and rk <= 3
        m[f"hit5_{tag}"] = rk is not None and rk <= 5
        m[f"rr10_{tag}"] = (1.0 / rk) if rk and rk <= 10 else 0.0
    if row.get("confuser") and "rec" in res:
        ra, rc = rank_of(keys, row["answers"]), rank_of(keys, [row["confuser"]])
        m["confused"] = rc is not None and (ra is None or rc < ra)
    if "rec" in res:
        m["low_conf"] = bool((res.get("rec") or {}).get("low_confidence"))
    d = res.get("discover")
    if d is not None:
        seats = d.get("seats") or []
        if row["lenient"]:
            m["discover_hit5"] = bool(set(seats[:5]) & set(row["lenient"]))
            m["discover_hit5_strict"] = bool(set(seats[:5]) & set(row["answers"]))
        m["short"] = len(seats) < int(d.get("want") or 0)
        doms = {dom_of(k) for k in row["lenient"]}
        if len(doms) >= 2:
            seated = {dom_of(k) for k in seats} | {dom_of(c["key"]) for c in ((res.get("counter") or {}).get("seats") or [])}
            m["dom_cover"] = len(doms & seated) / len(doms)
    s = res.get("summon")
    if s is not None and row["lenient"]:
        m["summon_recall"] = bool({c["key"] for c in s.get("cands") or []} & set(row["lenient"]))
    sc = res.get("screen")
    if sc is not None and row["lenient"]:
        m["screen_pass"] = any(x.get("passed") for x in sc.get("seats") or [] if x["key"] in set(row["lenient"]))
        m["asked_hit"] = bool(set(sc.get("asked") or []) & set(row["lenient"]))
    j = res.get("judge")
    if j is not None:
        m["no_answer"] = bool(j.get("no_answer"))
        if row["lenient"]:
            m["answered_hit"] = bool(set(j.get("answered") or []) & set(row["lenient"]))
    t = res.get("tools")
    if t is not None:
        prefer = t.get("prefer") or []
        if row.get("apps"):
            m["tool_app_hit"] = any((t.get("app") or {}).get(n) in set(row["apps"]) for n in prefer)
        if row.get("expect_empty") is not None:
            m["tool_empty_ok"] = (not prefer) == bool(row["expect_empty"])
        m["tool_n"] = len(prefer)
    return m


def band(row: dict, res: dict) -> str:
    """어휘 일치 칸(L3) — 정답의 desc_match 가 0.40 이상이면 hi(추천 40 밖이면 lo)."""
    vals = [desc_of(res, k) for k in row["answers"]]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return "hi" if vals and max(vals) >= LOW_CONF_DESC else "lo"


def groups_of(name: str, row: dict, res: dict) -> list[str]:
    g = ["all"]
    if row.get("split"):
        g.append(f"split:{row['split']}")
    if row["answers"]:
        g.append(f"branch:{branch(dom_of(row['answers'][0]))}")
        g.append(f"band:{band(row, res)}")
    if row.get("src"):
        g.append(f"src:{row['src']}")
    if row.get("kind"):
        g.append(f"kind:{row['kind']}")
    return g


def _mean(xs):
    xs = [float(x) for x in xs]
    return round(sum(xs) / len(xs), 4) if xs else None


def _pct(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def none_scores(rows: list[dict], per: dict, signal: str) -> dict | None:
    """'없음' 신호의 정밀도 · 재현율 · F0.5 · 맡은 질문에서 잘못 '없음'. 양성은 kind none, 음성은 kind owned."""
    pairs = [(r["kind"] == "none", bool(per[r["qid"]][signal])) for r in rows
             if r["qid"] in per and signal in per[r["qid"]] and r["kind"] in ("none", "owned")]
    if not pairs or not any(t for t, _p in pairs):
        return None
    tp = sum(1 for t, p in pairs if t and p)
    fp = sum(1 for t, p in pairs if not t and p)
    fn = sum(1 for t, p in pairs if t and not p)
    owned = sum(1 for t, _p in pairs if not t)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f05 = ((1.25 * prec * rec) / (0.25 * prec + rec)) if prec and rec else 0.0
    return {"n": len(pairs), "pos": tp + fn, "precision": prec and round(prec, 4), "recall": rec and round(rec, 4),
            "f05": round(f05, 4), "wrong_none_owned": round(fp / owned, 4) if owned else None}


def domain_removed(rows: list[dict], results: dict, rels: list[dict]) -> dict | None:
    """N3 — 정답 도메인 전원과 정답의 관계 전문가(sibling · counterpart)를 추천에서 뺀 채 low_confidence 를 다시 센다(추천 40 안의 desc_match 로 —
    40 밖은 모른다). 기대는 '없음'. 숫자는 잡음 띠와 함께 보는 보조다."""
    rel = defaultdict(set)
    for r in rels:
        if r.get("rel") == "sibling" or "counterpart" in str(r.get("rel") or ""):
            rel[r["a"]].add(r["b"])
            rel[r["b"]].add(r["a"])
    flagged = n = 0
    for row in rows:
        res = results.get(row["qid"])
        if not res or not row["answers"] or "rec" not in res or row.get("kind") != "owned":
            continue
        gone = {dom_of(k) for k in row["answers"]}
        drop = set(row["answers"]) | set().union(*(rel[k] for k in row["answers"]))
        left = [r for r in (res["rec"].get("ranked") or []) if not r.get("op") and dom_of(r["key"]) not in gone and r["key"] not in drop]
        best = max((r.get("desc_match") or 0.0 for r in left), default=0.0)
        n += 1
        flagged += best < LOW_CONF_DESC
    return {"n": n, "none_rate": round(flagged / n, 4) if n else None} if n else None


def agreement(rows: list[dict]) -> dict | None:
    """H 두 사람 일치 — 주 전문가가 겹치는 비율 · 도메인 일치도(Cohen κ, 첫 주 전문가의 도메인 또는 '없음' 갈래)."""
    pairs = [(r, o) for r in rows for o in r.get("others") or [] if not o.get("unsure")]
    if not pairs:
        return None

    def cat(lb):
        return f"none:{lb.get('none')}" if lb.get("none") else (dom_of((lb.get("primary") or [""])[0]) or "?")
    overlap = sum(1 for r, o in pairs if set(r["answers"]) & set(o.get("primary") or []))
    a = [cat({"none": r.get("none"), "primary": r["answers"]}) for r, _o in pairs]
    b = [cat(o) for _r, o in pairs]
    po = sum(1 for x, y in zip(a, b) if x == y) / len(pairs)
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (len(pairs) ** 2)
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    return {"n": len(pairs), "primary_overlap": round(overlap / len(pairs), 4), "domain_kappa": round(kappa, 4)}


def picked_agreement(rows: list[dict]) -> dict | None:
    """사람이 고른 전문가(지목 · 지정 좌석)와 표시가 맞는 비율 — 사람이 고른 전문가를 얼마나 믿을 수 있나(design-e5 2.4)."""
    xs = [r for r in rows if r.get("picked") and r["kind"] == "owned"]
    if not xs:
        return None
    hit = sum(1 for r in xs if set(r["picked"]) & set(r["lenient"]))
    return {"n": len(xs), "picked_in_label": round(hit / len(xs), 4)}


def summarize(name: str, rows: list[dict], results: dict, rels: list[dict] | None = None) -> dict:
    """묶음 하나의 보고 — 갈래마다 지표 평균(이진은 비율), '없음' 신호, 지킴, 지연. 질문 글은 싣지 않는다."""
    per = {r["qid"]: item_metrics(r, results[r["qid"]]) for r in rows if r["qid"] in results}
    by_group: dict = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["qid"] not in per:
            continue
        for g in groups_of(name, r, results[r["qid"]]):
            for k, v in per[r["qid"]].items():
                by_group[g][k].append(v)
    out = {"n": len(per), "missing": len(rows) - len(per),
           "groups": {g: {"n": max(len(v) for v in ms.values()) if ms else 0, **{k: _mean(v) for k, v in sorted(ms.items())}}
                      for g, ms in sorted(by_group.items())}}
    sig = {s: none_scores(rows, per, s) for s in ("low_conf", "no_answer")}
    sig = {k: v for k, v in sig.items() if v}
    if sig:
        out["none"] = sig
    guard = Counter()
    lat = []
    for q, res in results.items():
        g = res.get("guard") or {}
        guard["operators"] += int(g.get("operators") or 0)
        guard["unknown"] += int(g.get("unknown") or 0)
        guard["errors"] += 1 if res.get("error") else 0
        ms = int((res.get("summon") or {}).get("ms") or 0) + int((res.get("screen") or {}).get("ms") or 0)
        if res.get("summon"):
            lat.append(ms)
    out["guard"] = dict(guard)
    if lat:
        out["think_ms"] = {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95)}
    if name in ("A", "H") and rels:
        n3 = domain_removed(rows, results, rels)
        if n3:
            out["n3"] = n3
    if name == "H":
        ag, pa = agreement(rows), picked_agreement(rows)
        if ag:
            out["agreement"] = ag
        if pa:
            out["picked"] = pa
    return out


def mcnemar(b: int, c: int) -> float:
    """짝 비교 — b(끈 쪽만 맞음) · c(켠 쪽만 맞음). 엇갈린 짝이 25 아래면 정확 이항(양쪽), 아니면 연속성 보정 χ²(자유도 1)."""
    n = b + c
    if n == 0:
        return 1.0
    if n < 25:
        k = min(b, c)
        return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    chi2 = (abs(b - c) - 1) ** 2 / n
    return math.erfc(math.sqrt(chi2 / 2))


def paired(rows: list[dict], off: dict, on: dict) -> dict:
    """끈 쪽 · 켠 쪽을 같은 qid 로 맞대어 이진 지표마다 (끈 쪽 비율, 켠 쪽 비율, 차, b, c, p)."""
    out = {}
    pm_off = {r["qid"]: item_metrics(r, off[r["qid"]]) for r in rows if r["qid"] in off and r["qid"] in on}
    pm_on = {r["qid"]: item_metrics(r, on[r["qid"]]) for r in rows if r["qid"] in off and r["qid"] in on}
    keys = sorted({k for m in pm_off.values() for k, v in m.items() if isinstance(v, bool)})
    for k in keys:
        both = [q for q in pm_off if k in pm_off[q] and k in pm_on.get(q, {})]
        if not both:
            continue
        b = sum(1 for q in both if pm_off[q][k] and not pm_on[q][k])
        c = sum(1 for q in both if not pm_off[q][k] and pm_on[q][k])
        a0 = sum(1 for q in both if pm_off[q][k]) / len(both)
        a1 = sum(1 for q in both if pm_on[q][k]) / len(both)
        out[k] = {"n": len(both), "off": round(a0, 4), "on": round(a1, 4), "diff": round(a1 - a0, 4), "b": b, "c": c,
                  "p": round(mcnemar(b, c), 4)}
    return out


def same_version(v0: list, v1: list) -> list[str]:
    """L8 — 같은 날 같은 풀에서 쟀나. 다른 칸 이름을 돌려준다(빈 목록이면 같다). 에이전트 커밋 · 어휘표 지문은 켠 쪽이 바꾸는 칸이라 견주지 않는다."""
    a, b = (v0 or [{}])[0], (v1 or [{}])[0]
    diff = []
    for k in ("pool", "field_terms", "tools_map", "knobs"):
        if a.get(k) != b.get(k):
            diff.append(k)
    for k in ("version", "build", "embed_slot"):
        if (a.get("aidh") or {}).get(k) != (b.get("aidh") or {}).get(k):
            diff.append(f"aidh.{k}")
    da, db = str(a.get("at") or "")[:10], str(b.get("at") or "")[:10]
    if da != db:
        diff.append("day")
    return diff


# ── 결과 자리 ─────────────────────────────────────────────────────────────────
def _results(d: Path, name: str) -> dict:
    return {r["qid"]: r for r in _jsonl(d / f"{name}.results.jsonl")}


def report(d: Path) -> dict:
    meta = json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").is_file() else {}
    sets = Path(meta["sets"]) if meta.get("sets") else None
    rels = _jsonl(sets / "rels.jsonl") if sets else []
    out = {"run": meta.get("run"), "version": {}, "sets": {}}
    for name in SETS:
        res = _results(d, name)
        if not res:
            continue
        rows = load_set(name, sets, meta.get("by"))
        out["sets"][name] = summarize(name, rows, res, rels)
        vf = d / f"{name}.version.json"
        if vf.is_file():
            out["version"][name] = (json.loads(vf.read_text(encoding="utf-8")) or [{}])[0]
    return out


def compare(d0: Path, d1: Path) -> dict:
    meta = json.loads((d0 / "run.json").read_text(encoding="utf-8")) if (d0 / "run.json").is_file() else {}
    sets = Path(meta["sets"]) if meta.get("sets") else None
    out = {"sets": {}}
    for name in SETS:
        r0, r1 = _results(d0, name), _results(d1, name)
        if not (r0 and r1):
            continue
        rows = load_set(name, sets, meta.get("by"))
        v0 = json.loads((d0 / f"{name}.version.json").read_text(encoding="utf-8")) if (d0 / f"{name}.version.json").is_file() else []
        v1 = json.loads((d1 / f"{name}.version.json").read_text(encoding="utf-8")) if (d1 / f"{name}.version.json").is_file() else []
        out["sets"][name] = {"paired": paired(rows, r0, r1), "version_diff": same_version(v0, v1)}
    return out


def _print_report(rep: dict) -> None:
    for name, s in rep["sets"].items():
        print(f"[{name}] n {s['n']} · 빠짐 {s['missing']} · 지킴 {s.get('guard')} · 띵킹 ms {s.get('think_ms')}")
        for g, ms in s["groups"].items():
            cells = " · ".join(f"{k} {v}" for k, v in ms.items() if k != "n" and v is not None)
            print(f"   {g:<16} n {ms['n']:>4} · {cells}")
        for k in ("none", "n3", "agreement", "picked"):
            if s.get(k):
                print(f"   {k}: {s[k]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--sets", type=Path, default=None)
    r.add_argument("--only", default="A,P,C,H,N,T")
    r.add_argument("--layers", default="")
    r.add_argument("--run", default="")
    r.add_argument("--url", default=DEFAULT_URL)
    r.add_argument("--batch", type=int, default=20)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--by", default=None)
    r.add_argument("--concurrency", type=int, default=2)
    r.add_argument("--out", type=Path, default=None)
    rp = sub.add_parser("report")
    rp.add_argument("dir", type=Path)
    cp = sub.add_parser("compare")
    cp.add_argument("off", type=Path)
    cp.add_argument("on", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "report":
        rep = report(a.dir)
        _print_report(rep)
        (a.dir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    if a.cmd == "compare":
        cmp_ = compare(a.off, a.on)
        for name, s in cmp_["sets"].items():
            print(f"[{name}] 판 차이 {s['version_diff'] or '없음'}")
            for k, v in s["paired"].items():
                print(f"   {k:<22} {v}")
        return 0
    import seat_box  # noqa: PLC0415
    run_id = a.run or datetime.now().strftime("e5-%Y%m%d-%H%M%S")
    out = a.out or seat_box.box_dir() / "runs" / run_id
    auth = auth_header()
    layers = [x for x in a.layers.split(",") if x] or None
    if layers and any(x in LLM for x in layers):
        print("LLM 겹이 들었다 — 심의가 도는 동안에는 창구가 거절한다(GLM 을 함께 쓴다)")
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps({"run": run_id, "sets": str(a.sets) if a.sets else None, "by": a.by,
                                              "layers": layers, "at": datetime.now().isoformat(timespec="seconds")},
                                             ensure_ascii=False), encoding="utf-8")
    for name in [x for x in a.only.split(",") if x in SETS]:
        rows = load_set(name, a.sets, a.by)
        if a.limit:
            rows = rows[:a.limit]
        if not rows:
            print(f"[{name}] 건너뜀 — 묶음이 비었다")
            continue
        run(a.url, auth, name, rows, layers=layers, run_id=run_id, batch=a.batch, out=out, concurrency=a.concurrency)
    rep = report(out)
    _print_report(rep)
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"결과 자리 — {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

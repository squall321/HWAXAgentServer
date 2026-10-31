# 전문가 고르기 기준선 러너 — 평가셋을 /seat-eval 창구에 나눠 보내 고른 결과를 박스에 남기고, 지표 · 갈래 · 짝 비교(McNemar) · 판 지문을 숫자로만 낸다(HWAXPortal docs/request-1029 design-e5 E5-1)
"""재는 법은 design-e5 2.5 · 2.6, 성공 기준은 2.7 이다. tools/delib_ab.py 처럼 dev 는 스모크이고 판정은 cae00 이다(C-31).

묶음 — 평가셋 자리(tools/seat_evalset.py 가 지은 것, Drive 로 받은 것)의 파일과 박스의 사람 표시.
  A     A.jsonl — 저작 혼동. dev · test 는 섞지 않는다(L7) — 실행 · 보고 · 짝 비교가 한쪽씩(--split, 기본 test — 관문 2 · 3). 손보기는
        --split dev 로만 돌린다(A-test 숫자를 보지 않는다). A-name.jsonl 은 이름 묶음(따로)
  P     P.jsonl — 위임 문구 → 띵킹 위임 홉의 소집(top_k 3)에 대상이 드나(진단)
  C     C.jsonl — 카드 유래 → 카드 주인이 추천 상위 5 에 드나(진단)
  H     박스의 사람 표시(seat_questions.export) — 기준. 출처 넷 · '없음' 표시
  N     N.jsonl — 풀 밖이 분명한 질문(N4, 사람이 쓴다 — 없으면 건너뛴다). N1 · N2 는 H 의 '없음' 표시다
  T     T.jsonl — 도구 겹 {qid, expert, apps?, expect_empty?} (앱 전문가 · 비어야 할 전문가 — 사람이 쓴다)
H · N 의 질문 글은 박스 밖으로 나가지 않는다 — 결과 파일 · 보고에는 qid 와 숫자만 싣는다.

  실행:
    python -m tools.seat_eval run --sets <dir> [--only A,P,C,H,N,T] [--layers rec,discover,summon,screen] [--run <id>]
                                  [--split test|dev|all] [--url http://127.0.0.1:9009] [--batch 20] [--limit N] [--by <표시자>]
    python -m tools.seat_eval bands <B0(끈 쪽) 결과 자리> [--force]   # 질의마다 어휘 칸을 한 번 정해 <평가셋>/bands.json 에(L3)
    python -m tools.seat_eval report <결과 자리> [--split test|dev|all]
    python -m tools.seat_eval compare <끈 쪽 결과 자리> <켠 쪽 결과 자리> [--split test|dev]
공유 비밀은 에이전트 서버의 MCP 설정(MCP_CONFIG, 없으면 리포의 mcp_servers.json)의 게이트웨이 Authorization 을 읽는다 — 사람이 옮겨 적지 않는다.

어휘 칸(L3) — 정답의 desc_match 는 추천 40 안에서만 보인다. 실행마다 칸을 셈하면 켠 쪽이 정답을 40 안으로 올린 질의가 칸을 옮겨 두 쪽이 다른
무리를 견주게 된다. 그래서 칸은 한 번 정한다 — bands 가 B0(끈 쪽)의 결과로 <평가셋>/bands.json 에 적고(이미 적힌 질의는 다시 쓰지 않는다),
report · compare 가 그것을 쓴다. 적힌 것이 없으면 compare 는 끈 쪽의 칸을, report 는 그 실행의 칸을 쓴다(보고에 출처를 적는다).
도구 실패(창구의 겹마다 tool_error — 예외 · 실패 글 · 추천 폴백)는 틀린 답이 아니다 — 그 겹의 지표와 짝 비교의 분모에서 빼고 guard.errors ·
dropped 로 센다. 띵킹 지연은 그 질의의 첫 추천 호출(summon.first)과 다시 부른 것을 따로 낸다.
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
SPLITS = ("dev", "test")                # A 의 가름(L7) — all 은 실행 · 보고에서만(둘을 따로 낸다)
# 겹이 기대는 겹 — 앞 겹이 실패하면 뒤 겹의 결과도 믿지 않는다(창구 seat_probe._NEEDS 와 같다)
DEPENDS = {"screen": ("summon",), "judge": ("summon", "screen"), "counter": ("discover",)}
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
            raise RuntimeError(f"창구가 읽기 도구 밖을 부르려 했다 — 막은 이름 {got.get('blocked')} · 부른 도구 {got.get('calls')}")
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


def failed_layers(res: dict) -> set:
    """도구가 실패한 겹(창구가 단 tool_error · 추천 오류 · AIDataHub 빈 폴백)과 그 겹에 기대는 겹. 그 겹의 지표는 셈하지 않는다 — 실패는 틀린
    답이 아니다(한쪽 일시 장애가 McNemar 에서 퇴보로 읽히지 않게)."""
    res = res or {}
    bad = {k for k, v in res.items() if isinstance(v, dict) and v.get("tool_error")}
    rec = res.get("rec")
    if isinstance(rec, dict) and (rec.get("error") or rec.get("fallback")):
        bad.add("rec")
    for lay, deps in DEPENDS.items():
        if lay in res and bad & set(deps):
            bad.add(lay)
    return bad


def errored(res: dict) -> bool:
    """이 질의가 도구 실패 · 창구 예외를 겪었나 — guard.errors 로 센다."""
    return bool((res or {}).get("error")) or bool(failed_layers(res))


def item_metrics(row: dict, res: dict) -> dict:
    """한 질의의 이진 결과 — 짝 비교(McNemar)의 단위다. 그 겹이 없거나 도구가 실패했으면(failed_layers) 그 칸을 싣지 않는다. 창구 예외로
    끝난 질의(error)는 아무 칸도 싣지 않는다."""
    m: dict = {}
    if res.get("error"):
        return m
    bad = failed_layers(res)
    has = {k for k in ("rec", "discover", "summon", "screen", "judge", "counter", "tools") if k in res and k not in bad}
    keys = ranked(res)
    for tag, ans in (("strict", row["answers"]), ("lenient", row["lenient"])):
        if not ans or "rec" not in has:
            continue
        rk = rank_of(keys, ans)
        m[f"acc1_{tag}"] = rk == 1
        m[f"acc3_{tag}"] = rk is not None and rk <= 3
        m[f"hit5_{tag}"] = rk is not None and rk <= 5
        m[f"rr10_{tag}"] = (1.0 / rk) if rk and rk <= 10 else 0.0
    if row.get("confuser") and "rec" in has:
        ra, rc = rank_of(keys, row["answers"]), rank_of(keys, [row["confuser"]])
        m["confused"] = rc is not None and (ra is None or rc < ra)
    if "rec" in has:
        m["low_conf"] = bool((res.get("rec") or {}).get("low_confidence"))
    d = res.get("discover") if "discover" in has else None
    if d is not None:
        seats = d.get("seats") or []
        if row["lenient"]:
            m["discover_hit5"] = bool(set(seats[:5]) & set(row["lenient"]))
            m["discover_hit5_strict"] = bool(set(seats[:5]) & set(row["answers"]))
        m["short"] = len(seats) < int(d.get("want") or 0)
        doms = {dom_of(k) for k in row["lenient"]}
        if len(doms) >= 2 and ("counter" in has or "counter" not in res):
            counter = ((res.get("counter") or {}).get("seats") or []) if "counter" in has else []
            seated = {dom_of(k) for k in seats} | {dom_of(c["key"]) for c in counter}
            m["dom_cover"] = len(doms & seated) / len(doms)
    s = res.get("summon") if "summon" in has else None
    if s is not None and row["lenient"]:
        m["summon_recall"] = bool({c["key"] for c in s.get("cands") or []} & set(row["lenient"]))
    sc = res.get("screen") if "screen" in has else None
    if sc is not None and row["lenient"]:
        m["screen_pass"] = any(x.get("passed") for x in sc.get("seats") or [] if x["key"] in set(row["lenient"]))
        m["asked_hit"] = bool(set(sc.get("asked") or []) & set(row["lenient"]))
    j = res.get("judge") if "judge" in has else None
    if j is not None:
        m["no_answer"] = bool(j.get("no_answer"))
        if row["lenient"]:
            m["answered_hit"] = bool(set(j.get("answered") or []) & set(row["lenient"]))
    t = res.get("tools") if "tools" in has else None
    if t is not None:
        prefer = t.get("prefer") or []
        if row.get("apps"):
            m["tool_app_hit"] = any((t.get("app") or {}).get(n) in set(row["apps"]) for n in prefer)
        if row.get("expect_empty") is not None:
            m["tool_empty_ok"] = (not prefer) == bool(row["expect_empty"])
        m["tool_n"] = len(prefer)
    return m


# ── 어휘 칸(L3) ──────────────────────────────────────────────────────────────
BANDS_FILE = "bands.json"


def band(row: dict, res: dict) -> str:
    """어휘 일치 칸 — 정답의 desc_match 가 0.40 이상이면 hi(추천 40 밖이면 lo). 한 실행의 결과로 셈한 값이다 — 두 실행을 견줄 때는 band_for 로
    한 번 정한 칸을 쓴다."""
    vals = [desc_of(res, k) for k in row["answers"]]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return "hi" if vals and max(vals) >= LOW_CONF_DESC else "lo"


def bands_of(rows: list[dict], results: dict) -> dict:
    """{qid: 칸} — 정답이 있고 추천 겹이 성한 질의만(B0 끈 쪽 결과로 한 번 정할 때 쓴다)."""
    out = {}
    for r in rows:
        res = results.get(r["qid"])
        if r.get("answers") and res and "rec" in res and not res.get("error") and "rec" not in failed_layers(res):
            out[r["qid"]] = band(r, res)
    return out


def load_bands(sets: Path | None) -> dict:
    """평가셋 자리에 한 번 정해 둔 칸(bands.json) — 없거나 못 읽으면 빈 사전."""
    if not sets:
        return {}
    try:
        d = json.loads((Path(sets) / BANDS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in d.items() if v in ("hi", "lo")} if isinstance(d, dict) else {}


def band_for(row: dict, res: dict | None, fixed: dict | None) -> tuple[str, str]:
    """(칸, 출처) — 한 번 정해 둔 칸(set)이 먼저, 없으면 주어진 결과로 셈한다(run)."""
    b = (fixed or {}).get(row["qid"])
    if b:
        return b, "set"
    return band(row, res or {}), "run"


def write_bands(d: Path, force: bool = False) -> tuple[Path, int, int]:
    """B0(끈 쪽) 결과 자리 → <평가셋>/bands.json. 이미 적힌 질의는 다시 쓰지 않는다(force 가 아니면) — 칸은 질의 · 정답의 성질로 한 번만 정한다."""
    meta = json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").is_file() else {}
    if not meta.get("sets"):
        raise SystemExit("이 결과 자리는 --sets 없이 돌았다 — 칸을 적을 평가셋 자리가 없다")
    sets = Path(meta["sets"])
    got: dict = {}
    for name in SETS:
        res = _results(d, name)
        if res:
            got.update(bands_of(load_set(name, sets, meta.get("by")), res))
    have = load_bands(sets)
    merged = {**have, **got} if force else {**got, **have}
    added = sum(1 for q in got if q not in have)
    path = sets / BANDS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sorted(merged.items())), ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)
    return path, added, len(merged)


def band_shares(rows: list[dict], results: dict, bands: dict | None = None) -> dict | None:
    """H(맡은 질문)의 어휘 칸 비율 — A 의 머리 숫자를 맞출 무게(L3 의 뒤 절반)."""
    c: Counter = Counter()
    for r in rows:
        res = results.get(r["qid"])
        if not res or not r.get("answers") or r.get("kind") != "owned":
            continue
        if not (bands or {}).get(r["qid"]) and (res.get("error") or "rec" not in res or "rec" in failed_layers(res)):
            continue
        c[band_for(r, res, bands)[0]] += 1
    n = sum(c.values())
    return {"hi": round(c["hi"] / n, 4), "lo": round(c["lo"] / n, 4), "n": n} if n else None


def weighted(groups: dict, shares: dict | None) -> dict:
    """칸마다의 평균을 무게(shares 의 hi · lo)로 더한다 — 무게가 있는 칸에 값이 없는 지표는 셈하지 않는다."""
    if not shares:
        return {}
    hi, lo = groups.get("band:hi") or {}, groups.get("band:lo") or {}
    wa, wb = float(shares.get("hi") or 0.0), float(shares.get("lo") or 0.0)
    out = {}
    for k in sorted((set(hi) | set(lo)) - {"n"}):
        a, b = hi.get(k), lo.get(k)
        if (a is None and wa) or (b is None and wb):
            continue
        out[k] = round(float(a or 0.0) * wa + float(b or 0.0) * wb, 4)
    return out


def groups_of(name: str, row: dict, res: dict, bands: dict | None = None) -> list[str]:
    """갈래 — all · 갈래(branch) · 어휘 칸(band — 한 번 정한 칸이 먼저) · H 출처(src) · 종류(kind). A 의 dev · test 는 묶음을 나눠 내므로
    (A:dev · A:test) 한 묶음 안에 섞이지 않는다."""
    g = ["all"]
    if row["answers"]:
        g.append(f"branch:{branch(dom_of(row['answers'][0]))}")
        g.append(f"band:{band_for(row, res, bands)[0]}")
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


def _lat(xs: list) -> dict | None:
    return {"n": len(xs), "p50": _pct(xs, 0.5), "p95": _pct(xs, 0.95)} if xs else None


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
    40 밖은 모른다). 기대는 '없음'. 숫자는 잡음 띠와 함께 보는 보조다. 추천 겹이 실패한 질의는 뺀다."""
    rel = defaultdict(set)
    for r in rels:
        if r.get("rel") == "sibling" or "counterpart" in str(r.get("rel") or ""):
            rel[r["a"]].add(r["b"])
            rel[r["b"]].add(r["a"])
    flagged = n = 0
    for row in rows:
        res = results.get(row["qid"])
        if not res or not row["answers"] or "rec" not in res or row.get("kind") != "owned" or res.get("error") \
                or "rec" in failed_layers(res):
            continue
        gone = {dom_of(k) for k in row["answers"]}
        drop = set(row["answers"]) | set().union(*(rel[k] for k in row["answers"]))
        left = [r for r in (res["rec"].get("ranked") or []) if not r.get("op") and dom_of(r["key"]) not in gone and r["key"] not in drop]
        best = max((r.get("desc_match") or 0.0 for r in left), default=0.0)
        n += 1
        flagged += best < LOW_CONF_DESC
    return {"n": n, "none_rate": round(flagged / n, 4) if n else None} if n else None


def agreement(rows: list[dict]) -> dict | None:
    """H 두 사람 일치 — 주 전문가 겹침은 둘 다 주 전문가를 단 짝에서만(primary_n), '없음' 은 따로(none_n — 한쪽이라도 '없음' 을 단 짝에서 둘 다
    '없음' 이면 맞음), 도메인 일치도(Cohen κ — 첫 주 전문가의 도메인 또는 '없음' 갈래)는 모든 짝에서. 없음/없음 짝을 겹침 0 으로 세면 둘이 다
    맞게 단 질문이 어긋남으로 읽힌다."""
    pairs = [(r, o) for r in rows for o in r.get("others") or [] if not o.get("unsure")]
    if not pairs:
        return None

    def cat(lb):
        return f"none:{lb.get('none')}" if lb.get("none") else (dom_of((lb.get("primary") or [""])[0]) or "?")
    prim = [(r, o) for r, o in pairs if r["answers"] and o.get("primary")]
    nonp = [(r, o) for r, o in pairs if r.get("none") or o.get("none")]
    overlap = sum(1 for r, o in prim if set(r["answers"]) & set(o.get("primary") or []))
    none_ok = sum(1 for r, o in nonp if r.get("none") and o.get("none"))
    a = [cat({"none": r.get("none"), "primary": r["answers"]}) for r, _o in pairs]
    b = [cat(o) for _r, o in pairs]
    po = sum(1 for x, y in zip(a, b) if x == y) / len(pairs)
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (len(pairs) ** 2)
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    return {"n": len(pairs), "primary_n": len(prim), "primary_overlap": round(overlap / len(prim), 4) if prim else None,
            "none_n": len(nonp), "none_agree": round(none_ok / len(nonp), 4) if nonp else None, "domain_kappa": round(kappa, 4)}


def picked_agreement(rows: list[dict]) -> dict | None:
    """사람이 지목한 전문가(지목 전문가 챗 — src persona)와 표시가 맞는 비율 — 사람이 고른 전문가를 얼마나 믿을 수 있나(design-e5 2.4 '지목
    전문가 출처는'). 표시가 '없음' 인 줄은 어긋남으로 센다. 심의 화두(delib)는 세지 않는다 — 웹 심의 화면은 추천 상위를 기본으로 골라 두어 좌석이
    사람이 고른 것인지 모른다(L6 — 시스템이 고른 좌석은 정답이 아니다)."""
    xs = [r for r in rows if r.get("picked") and r.get("src") == "persona" and r.get("kind") in ("owned", "none")]
    if not xs:
        return None
    hit = sum(1 for r in xs if set(r["picked"]) & set(r.get("lenient") or []))
    return {"n": len(xs), "picked_in_label": round(hit / len(xs), 4)}


def summarize(name: str, rows: list[dict], results: dict, rels: list[dict] | None = None, bands: dict | None = None) -> dict:
    """묶음 하나의 보고 — 갈래마다 지표 평균(이진은 비율), '없음' 신호, 지킴, 지연. 질문 글은 싣지 않는다. 도구가 실패한 겹은 그 지표의
    분모에서 빠지고 dropped 로 센다."""
    per = {r["qid"]: item_metrics(r, results[r["qid"]]) for r in rows if r["qid"] in results}
    by_group: dict = defaultdict(lambda: defaultdict(list))
    src: Counter = Counter()
    for r in rows:
        if r["qid"] not in per:
            continue
        res = results[r["qid"]]
        if r["answers"]:
            src[band_for(r, res, bands)[1]] += 1
        for g in groups_of(name, r, res, bands):
            for k, v in per[r["qid"]].items():
                by_group[g][k].append(v)
    out = {"n": len(per), "missing": len(rows) - len(per),
           "groups": {g: {"n": max(len(v) for v in ms.values()) if ms else 0, **{k: _mean(v) for k, v in sorted(ms.items())}}
                      for g, ms in sorted(by_group.items())}}
    if src:
        out["band_src"] = dict(src)
    sig = {s: none_scores(rows, per, s) for s in ("low_conf", "no_answer")}
    sig = {k: v for k, v in sig.items() if v}
    if sig:
        out["none"] = sig
    guard: Counter = Counter()
    dropped: Counter = Counter()
    first, repeat = [], []
    for r in rows:
        res = results.get(r["qid"])
        if res is None:
            continue
        g = res.get("guard") or {}
        guard["operators"] += int(g.get("operators") or 0)
        guard["unknown"] += int(g.get("unknown") or 0)
        bad = failed_layers(res)
        if errored(res):
            guard["errors"] += 1
            for lay in sorted(bad) or ["all"]:
                dropped[lay] += 1
        s, sc = res.get("summon"), res.get("screen")
        if s and not res.get("error") and not ({"summon", "screen"} & bad):
            ms = int(s.get("ms") or 0) + int((sc or {}).get("ms") or 0)
            (first if s.get("first") else repeat).append(ms)
    out["guard"] = {"operators": guard["operators"], "unknown": guard["unknown"], "errors": guard["errors"]}
    out["dropped"] = {"queries": guard["errors"], "layers": dict(sorted(dropped.items()))}
    if first or repeat:
        # 소집이 그 질의의 첫 추천 호출이다(창구가 겹을 summon → screen → rec → discover 차례로 돈다). first 는 이 창구 프로세스가 그
        # 질의를 처음 보낸 것 — AIDataHub · DB 쪽 데움은 알 수 없다. 관문의 지연(p95 +10%)은 first 로 견준다.
        out["think_ms"] = {"first": _lat(first), "repeat": _lat(repeat)}
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
    """짝 비교 — b(끈 쪽만 맞음) · c(켠 쪽만 맞음). 엇갈린 짝이 25 아래면 정확 이항(양쪽), 아니면 연속성 보정 χ²(자유도 1 — 보정은 0 아래로
    내려가지 않는다, b = c 면 p = 1)."""
    n = b + c
    if n == 0:
        return 1.0
    if n < 25:
        k = min(b, c)
        return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    chi2 = max(0, abs(b - c) - 1) ** 2 / n
    return math.erfc(math.sqrt(chi2 / 2))


def paired(rows: list[dict], off: dict, on: dict) -> dict:
    """끈 쪽 · 켠 쪽을 같은 qid 로 맞대어 이진 지표마다 (끈 쪽 비율, 켠 쪽 비율, 차, b, c, p). 어느 한쪽이라도 그 지표가 없는(겹이 없거나 도구가
    실패한) 질의는 그 지표의 분모에서 빠진다."""
    out = {}
    both_q = [r for r in rows if r["qid"] in off and r["qid"] in on]
    pm_off = {r["qid"]: item_metrics(r, off[r["qid"]]) for r in both_q}
    pm_on = {r["qid"]: item_metrics(r, on[r["qid"]]) for r in both_q}
    keys = sorted({k for pm in (pm_off, pm_on) for m in pm.values() for k, v in m.items() if isinstance(v, bool)})
    for k in keys:
        both = [q for q in pm_off if k in pm_off[q] and k in pm_on[q]]
        if not both:
            continue
        b = sum(1 for q in both if pm_off[q][k] and not pm_on[q][k])
        c = sum(1 for q in both if not pm_off[q][k] and pm_on[q][k])
        a0 = sum(1 for q in both if pm_off[q][k]) / len(both)
        a1 = sum(1 for q in both if pm_on[q][k]) / len(both)
        out[k] = {"n": len(both), "off": round(a0, 4), "on": round(a1, 4), "diff": round(a1 - a0, 4), "b": b, "c": c,
                  "p": round(mcnemar(b, c), 4)}
    return out


def paired_groups(name: str, rows: list[dict], off: dict, on: dict, bands: dict | None = None) -> dict:
    """갈래마다 짝 비교(관문 3 — 갈래 · 어휘 칸 어디도 −5%p 넘게 떨어지지 않는다). 어휘 칸은 한 번 정한 칸(bands — 없으면 끈 쪽의 칸)이라 두 쪽이
    같은 무리를 견준다 — 켠 쪽이 정답을 40 안으로 올려도 칸이 옮지 않는다."""
    both = [r for r in rows if r["qid"] in off and r["qid"] in on]
    fixed = {**{r["qid"]: band(r, off[r["qid"]]) for r in both if r["answers"]}, **(bands or {})}
    groups: dict = defaultdict(list)
    for r in both:
        for g in groups_of(name, r, off[r["qid"]], fixed):
            if g != "all":
                groups[g].append(r)
    return {g: paired(rs, off, on) for g, rs in sorted(groups.items())}


_VER_KEYS = ("pool", "field_terms", "tools_map", "knobs")
# AIDataHub 의 version · build 는 배치마다 고정값이다(0.7.0 · BUILD_SHA=apptainer-deploy) — 형제 리포 커밋과 추천 코드 지문을 함께 본다
_AIDH_KEYS = ("version", "build", "embed_slot", "repo", "recommend_svc")


def _fp(v: dict) -> dict:
    a = (v or {}).get("aidh") or {}
    out = {k: (v or {}).get(k) for k in _VER_KEYS}
    out.update({f"aidh.{k}": a.get(k) for k in _AIDH_KEYS})
    return {k: json.dumps(x, sort_keys=True, ensure_ascii=False) for k, x in out.items()}


def drift(vs: list) -> list[str]:
    """한 실행 안 묶음들 사이에 바뀐 칸 — 긴 B0 도중의 동기화 · 풀 갱신 · 도구 지도 바뀜(L8). 빈 목록이면 처음부터 끝까지 한 판이다."""
    fps = [_fp(v) for v in vs or []]
    if len(fps) < 2:
        return []
    return [k for k in fps[0] if any(f[k] != fps[0][k] for f in fps[1:])]


def same_version(v0: list, v1: list) -> list[str]:
    """L8 — 같은 날 같은 풀에서 쟀나. 다른 칸 이름을 돌려준다(빈 목록이면 같다). 묶음 지문을 모두 본다 — 한 실행 안에서 바뀐 칸은 off: · on: 을
    붙이고, 두 실행 사이에 값이 다른 칸은 이름만. 에이전트 커밋 · 어휘표 지문은 켠 쪽이 바꾸는 칸이라 견주지 않는다."""
    a, b = list(v0 or [{}]), list(v1 or [{}])
    diff = [f"off:{k}" for k in drift(a)] + [f"on:{k}" for k in drift(b)]
    fa, fb = [_fp(v) for v in a], [_fp(v) for v in b]
    for k in fa[0]:
        if {f[k] for f in fa} != {f[k] for f in fb}:
            diff.append(k)
    da, db = str(a[0].get("at") or "")[:10], str(b[0].get("at") or "")[:10]
    if da != db:
        diff.append("day")
    return diff


# ── 결과 자리 ─────────────────────────────────────────────────────────────────
def _results(d: Path, name: str) -> dict:
    return {r["qid"]: r for r in _jsonl(d / f"{name}.results.jsonl")}


def _meta(d: Path) -> dict:
    return json.loads((d / "run.json").read_text(encoding="utf-8")) if (d / "run.json").is_file() else {}


def _versions(d: Path, name: str) -> list:
    f = d / f"{name}.version.json"
    return (json.loads(f.read_text(encoding="utf-8")) or []) if f.is_file() else []


def split_sets(name: str, rows: list[dict], split: str) -> list[tuple[str, list[dict]]]:
    """(보고 이름, 줄) — A 는 dev · test 를 따로(A:dev · A:test, all 이면 둘 다 따로), 나머지 묶음은 그대로(H 는 전부 시험이다)."""
    if name != "A":
        return [(name, rows)]
    sides = SPLITS if split == "all" else (split,)
    return [(f"A:{s}", [r for r in rows if r.get("split") == s]) for s in sides]


def report(d: Path, split: str | None = None) -> dict:
    meta = _meta(d)
    sets = Path(meta["sets"]) if meta.get("sets") else None
    split = split or meta.get("split") or "test"
    rels = _jsonl(sets / "rels.jsonl") if sets else []
    fixed = load_bands(sets)
    out = {"run": meta.get("run"), "split": split, "version": {}, "drift": {}, "sets": {}}
    h_shares = None
    for name in SETS:
        res = _results(d, name)
        if not res:
            continue
        rows = load_set(name, sets, meta.get("by"))
        for key, rs in split_sets(name, rows, split):
            if any(r["qid"] in res for r in rs):
                out["sets"][key] = summarize(name, rs, res, rels, fixed)
        vs = _versions(d, name)
        if vs:
            out["version"][name] = vs                   # 묶음마다의 지문을 모두 둔다(L8)
            out["drift"][name] = drift(vs)
        if name == "H":
            h_shares = band_shares(rows, res, fixed)
    # L3 의 뒤 절반 — A 의 머리 숫자를 H 의 어휘 칸 비율로 무게를 맞춘다(H 가 이 결과 자리에 있을 때만)
    for key, s in out["sets"].items():
        if key.startswith("A:"):
            s["weighted"] = {"by": "H", "shares": h_shares, "metrics": weighted(s["groups"], h_shares)} if h_shares else None
    return out


def compare(d0: Path, d1: Path, split: str = "test") -> dict:
    """끈 쪽 · 켠 쪽 짝 비교 — A 는 한쪽 가름만(기본 test — 관문 2 · 3, 손보기는 dev). 갈래 · 어휘 칸마다의 짝 비교 · 도구 실패로 뺀 질의 수 ·
    판 차이를 함께 낸다."""
    if split not in SPLITS:
        raise ValueError(f"split 은 {list(SPLITS)} 가운데 하나다")
    meta = _meta(d0)
    sets = Path(meta["sets"]) if meta.get("sets") else None
    fixed = load_bands(sets)
    out = {"split": split, "sets": {}}
    for name in SETS:
        r0, r1 = _results(d0, name), _results(d1, name)
        if not (r0 and r1):
            continue
        rows = load_set(name, sets, meta.get("by"))
        vdiff = same_version(_versions(d0, name), _versions(d1, name))
        for key, rs in split_sets(name, rows, split):
            rs = [r for r in rs if r["qid"] in r0 and r["qid"] in r1]
            if not rs:
                continue
            out["sets"][key] = {"paired": paired(rs, r0, r1), "groups": paired_groups(name, rs, r0, r1, fixed),
                                "dropped": sum(1 for r in rs if errored(r0[r["qid"]]) or errored(r1[r["qid"]])),
                                "version_diff": vdiff}
    return out


def _print_report(rep: dict) -> None:
    for name, vs in (rep.get("drift") or {}).items():
        if vs:
            print(f"⚠ [{name}] 실행 도중 판이 바뀌었다 — {vs}(L8 — 바뀐 뒤의 묶음은 다른 판이다. 같은 판에서 다시 잰다)")
    for key, s in rep["sets"].items():
        print(f"[{key}] n {s['n']} · 빠짐 {s['missing']} · 지킴 {s.get('guard')} · 도구 실패로 뺀 질의 {s.get('dropped')}")
        th = s.get("think_ms")
        if th:
            print(f"   띵킹 ms(소집 + 예심) — 그 질의의 첫 추천 호출 {th.get('first')} · 다시 부른 질의 {th.get('repeat')}")
        for g, ms in s["groups"].items():
            cells = " · ".join(f"{k} {v}" for k, v in ms.items() if k != "n" and v is not None)
            print(f"   {g:<16} n {ms['n']:>4} · {cells}")
        for k in ("band_src", "weighted", "none", "n3", "agreement", "picked"):
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
    r.add_argument("--split", choices=(*SPLITS, "all"), default="test",
                   help="A 의 가름(L7) — test(기본 · 관문) · dev(손보기 — A-test 숫자를 내지 않는다) · all(둘을 따로 낸다)")
    r.add_argument("--url", default=DEFAULT_URL)
    r.add_argument("--batch", type=int, default=20)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--by", default=None)
    r.add_argument("--concurrency", type=int, default=2)
    r.add_argument("--out", type=Path, default=None)
    bd = sub.add_parser("bands")
    bd.add_argument("dir", type=Path)
    bd.add_argument("--force", action="store_true")
    rp = sub.add_parser("report")
    rp.add_argument("dir", type=Path)
    rp.add_argument("--split", choices=(*SPLITS, "all"), default=None)
    cp = sub.add_parser("compare")
    cp.add_argument("off", type=Path)
    cp.add_argument("on", type=Path)
    cp.add_argument("--split", choices=SPLITS, default="test")
    a = ap.parse_args(argv)
    if a.cmd == "bands":
        path, added, total = write_bands(a.dir, a.force)
        print(f"어휘 칸 — 새로 {added}개 · 모두 {total}개 → {path}")
        return 0
    if a.cmd == "report":
        rep = report(a.dir, a.split)
        _print_report(rep)
        (a.dir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    if a.cmd == "compare":
        cmp_ = compare(a.off, a.on, a.split)
        for name, s in cmp_["sets"].items():
            print(f"[{name}] 판 차이 {s['version_diff'] or '없음'} · 도구 실패로 뺀 질의 {s['dropped']}")
            for k, v in s["paired"].items():
                print(f"   {k:<22} {v}")
            for g, pm in s["groups"].items():
                cells = " · ".join(f"{k} {v['off']}→{v['on']}(p {v['p']})" for k, v in pm.items())
                print(f"   [{g}] {cells}")
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
                                              "layers": layers, "split": a.split, "at": datetime.now().isoformat(timespec="seconds")},
                                             ensure_ascii=False), encoding="utf-8")
    for name in [x for x in a.only.split(",") if x in SETS]:
        rows = load_set(name, a.sets, a.by)
        if name == "A" and a.split != "all":
            rows = [x for x in rows if x.get("split") == a.split]      # 손보기(dev)는 A-test 를 보내지도 않는다(L7)
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

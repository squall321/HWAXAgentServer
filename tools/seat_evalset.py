# 전문가 고르기 기준선 평가셋 짓기 — ExpertAgents(읽기만)에서 A 저작 혼동 · P 위임 문구 · 관계 목록을, AIDataHub 한국어 검색 기준선 질의에서 C 카드 유래를 짓고 A-test 를 봉인한다(HWAXPortal docs/request-1029 design-e5 E5-0)
"""누설 없는 평가셋(design-e5 2.1 · 2.2).

  A  저작 혼동 — 전문가의 routing.anti_examples 줄 끝 주석 '# → 대상' 이 정답이다(글을 쓴 전문가가 곧 혼동 상대). 거름 —
     대상 없음 · 일반 대상('도메인 전문가' · 'sim-*') · 풀에 없는 키 · 자기 자신 · L1(정답의 표본 · 골든과 3글자 조각 자카드 0.5 이상) ·
     L2(정답 이름 · 키가 든 질의 → 따로 '이름 묶음' A-name). 층화 600 — HW 12 도메인 각 30 · 업무(xd) 60 · std · oss · misc 있는 만큼 · SW 120,
     남는 몫은 HW 로. 한 정답 전문가 2건 · 한 (정답, 글쓴이) 쌍 1건까지. 정답 전문가 단위로 A-dev 300 · A-test 300 을 가르고 A-test 의 qid 목록
     sha256 을 봉인 줄로 낸다(L7 — design-e5 8절에 옮긴다).
  P  위임 문구(진단) — expertise.out_of_scope 의 '문구 → 대상'. 300건(한 대상 2건까지).
  C  카드 유래(진단) — AIDataHub 한국어 검색 기준선 질의(queries.jsonl — 제목 베낌을 뺀 것). 정답은 카드 주인. --c-queries 로 준다.
  관계 — related_experts 의 (전문가, 대상, 관계 이름)만(N3 도메인 지우기 몫 — 글은 옮기지 않는다).

같은 씨앗이면 같은 qid 목록이다. 형제 리포(../ExpertAgents)가 없으면 까닭을 찍고 건너뛴다(종료 0). 결과는 리포 밖 박스 자리(기본
<seat-eval>/sets/<씨앗>)에만 쓴다 — 이 리포 안으로는 쓰지 않는다(공개 리포 · AIDataHub 에 싣지 않는다 — 결정 D-E5-8, Drive 로 cae00 에 보낸다).

  실행:
    python -m tools.seat_evalset build [--experts ../ExpertAgents] [--c-queries <queries.jsonl>] [--pool-file <keys>] [--seed 20261031]
                                       [--out <dir>]
    python -m tools.seat_evalset seal <dir>       # A-test 봉인 줄을 다시 셈해 manifest 와 맞대 본다
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_SEED = 20261031
# 포털 조직도 라벨 정본(orgTaxonomy.json categories.hw)과 같은 열둘 — tests/test_seat_evalset.py 가 형제 파일과 맞대 본다
HW = ("rel", "disp", "mech", "cam", "soc", "pcb", "rf", "passive", "pwr", "sh", "mem", "sim")
OTHER_ALL = ("std", "oss", "misc")          # 있는 만큼 싣는다
QUOTA = {**{d: 30 for d in HW}, "xd": 60, "sw": 120}
A_TOTAL = 600
P_TOTAL = 300
PER_ANSWER = 2
L1_JACCARD = 0.5
_KEY = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+"
_KEY_PART = re.compile(rf"^({_KEY})(?:\s*\([^)]*\))?$")
_ARROW_COMMENT = re.compile(r"(?:^|\s)#\s*→\s*(.+?)\s*$")
_SPLIT = re.compile(r"\s*[·,/]\s*|\s+또는\s+|\s+or\s+")
KST = timezone(timedelta(hours=9))


def dom_of(key: str) -> str:
    k = str(key or "")
    return k.split("-", 1)[0] if "-" in k else k


def branch(dom: str) -> str:
    return "hw" if dom in HW else "sw" if dom == "sw" else "xd" if dom == "xd" else "other"


def grams(s: str) -> set:
    """3글자 조각 — 소문자 · 공백을 뺀 글에서(design-e5 L1 · H 의 겹침 거름과 같은 꼴)."""
    t = re.sub(r"\s+", "", str(s or "").lower())
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))} or ({t} if t else set())


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def sha(s: str, n: int = 12) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


# ── ExpertAgents 읽기 ──────────────────────────────────────────────────────────
def parse_target(comment: str) -> list[str]:
    """'→ 대상' 주석 → 전문가 키 목록(차례 그대로). 일반 대상('도메인 전문가' · 'disp 계열' · 'sim-*')은 빈 목록이다."""
    out = []
    for part in _SPLIT.split(str(comment or "").strip()):
        p = part.strip().strip("`'\"")
        if "*" in p:
            return []
        m = _KEY_PART.match(p)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _anti_comments(text: str) -> list[str | None] | None:
    """expert.yaml 원문에서 routing.anti_examples 항목마다 '# → …' 주석(없으면 None). YAML 읽기는 주석을 버려서 줄을 따로 본다 —
    여러 줄 항목은 이어진 줄 어디에나 주석이 온다. 목록을 못 찾으면 None."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^\s*anti_examples:\s*(#.*)?$", ln):
            base = len(ln) - len(ln.lstrip())
            items: list[list[str]] = []
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    continue
                ind = len(nxt) - len(nxt.lstrip())
                if ind <= base and not nxt.lstrip().startswith("- "):
                    break
                if ind < base:
                    break
                if nxt.lstrip().startswith("- ") and (not items or ind <= items[-1][0]):
                    items.append([ind, nxt])
                elif items:
                    items[-1].append(nxt)
                else:
                    break
            out = []
            for it in items:
                hit = None
                for ln2 in it[1:]:
                    m = _ARROW_COMMENT.search(ln2)
                    if m:
                        hit = m.group(1)
                out.append(hit)
            return out
    return None


def load_experts(root: Path) -> tuple[dict, dict]:
    """(전문가 id → 정리한 정의, 읽기 셈). 정의 — name_ko · name_en · samples · golden · anti[(글, 주석)] · oos[글] · related[(id, 관계)]."""
    import yaml  # noqa: PLC0415 — 에이전트 venv 에 있다(langchain 의존). 서버 코드는 YAML 을 읽지 않는다
    experts, stats = {}, Counter()
    for d in sorted(p for p in (root / "knowledge").iterdir() if p.is_dir()):
        f = d / "expert.yaml"
        if not f.is_file():
            continue
        raw = f.read_text(encoding="utf-8")
        try:
            y = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            stats["yaml_error"] += 1
            continue
        eid = str(y.get("id") or d.name)
        routing = y.get("routing") or {}
        anti = [str(a) for a in (routing.get("anti_examples") or []) if isinstance(a, (str, int, float))]
        comments = _anti_comments(raw)
        if comments is None or len(comments) != len(anti):
            stats["anti_comment_mismatch"] += 1 if anti else 0
            comments = [None] * len(anti)
        golden = []
        g = d / "eval" / "golden.jsonl"
        if g.is_file():
            for ln in g.read_text(encoding="utf-8").splitlines():
                try:
                    golden.append(str(json.loads(ln).get("q") or ""))
                except (ValueError, AttributeError):
                    continue
        experts[eid] = {
            "id": eid, "name_ko": str(y.get("name_ko") or ""), "name_en": str(y.get("name_en") or ""),
            "samples": [str(x) for x in (routing.get("example_questions") or [])],
            "golden": [x for x in golden if x],
            "anti": list(zip(anti, comments)),
            "oos": [str(x) for x in ((y.get("expertise") or {}).get("out_of_scope") or [])],
            "related": [(str(r.get("id")), str(r.get("relation") or "")) for r in (y.get("related_experts") or [])
                        if isinstance(r, dict) and r.get("id")],
        }
        stats["experts"] += 1
    return experts, stats


def _git_head(path: Path) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


# ── 거름 ─────────────────────────────────────────────────────────────────────
_ROLE_KO = re.compile(r"\s*(?:초?전문가|담당)\s*$")
# 영문 이름의 역할 꼬리 — dev 773명 가운데 765명이 이 넷으로 끝난다(꼬리를 달고 견주면 질의에 거의 안 맞는다)
_ROLE_EN = re.compile(r"\s+(?:Specialist|Expert|Agent|Engineer)s?\s*$", re.IGNORECASE)
_PAREN = re.compile(r"\(([^)]*)\)")
_ALIAS_SPLIT = re.compile(r"\s*[·,/]\s*")
NAME_PIECE_MIN = 3              # 조각의 글자 수(띄어쓰기 · 붙임표 뺌) — 'TIM' · '현지화' 는 보고 'IP' · 'SE' 같은 2자 별칭은 보지 않는다


def name_pieces(ex: dict) -> list[str]:
    """L2 가 보는 정답 이름 조각 — 한국어 이름(역할 꼬리 '… 전문가' 를 뗀 것)과 영문 이름(역할 꼬리 Specialist · Expert · Agent · Engineer 를
    뗀 것)마다 괄호를 뗀 핵심, 괄호 속 별칭 조각(· , / 로 나눈 것). 'TIM(열계면재료) 전문가' → TIM · 열계면재료."""
    out: list[str] = []
    for raw, tail in ((ex.get("name_ko") or "", _ROLE_KO), (ex.get("name_en") or "", _ROLE_EN)):
        name = tail.sub("", str(raw)).strip()
        if not name:
            continue
        for piece in [" ".join(_PAREN.sub(" ", name).split()),
                      *(x.strip() for inner in _PAREN.findall(name) for x in _ALIAS_SPLIT.split(inner))]:
            if len(re.sub(r"[\s\-]", "", piece)) >= NAME_PIECE_MIN and piece not in out:
                out.append(piece)
    return out


def _flat(s: str) -> str:
    """맞대기 꼴 — 띄어쓰기 · 붙임표를 빼고 소문자('OLED 발광 재료' = 'OLED발광재료', 'Under-Display' = 'underdisplay')."""
    return re.sub(r"[\s\-]+", "", str(s or "")).lower()


def _name_hit(q: str, ex: dict) -> bool:
    """L2 — 정답의 키나 이름 조각(name_pieces)이 질의에 그대로 있나. 그런 질의는 어휘 점수(이름 · 설명 · 태그)에서 정답을 거저 맞힌다.
    조각은 띄어쓰기 · 붙임표를 보지 않고 견준다(종전 규칙과 같다 — 'AppWidgetProvider' 에 'App Widget' 이 든다). 3자 영문 약어만 영숫자
    경계 안에서 본다('TIM' 이 'TIMING' 에 맞지 않게)."""
    text = str(q or "")
    if ex["id"].lower() in text.lower():
        return True
    flat = _flat(text)
    for piece in name_pieces(ex):
        pf = _flat(piece)
        if len(pf) > NAME_PIECE_MIN or not pf.isascii():
            if pf in flat:
                return True
        elif re.search(r"(?<![0-9a-z])" + re.escape(pf) + r"(?![0-9a-z])", text.lower()):
            return True
    return False


def a_candidates(experts: dict, pool: set) -> tuple[list[dict], list[dict], Counter]:
    """(A 후보, 이름 묶음, 거른 까닭 셈) — 거름 차례는 대상 없음 · 일반 대상 · 풀에 없음 · 자기 자신 · L1 · L2."""
    gram_cache: dict = {}

    def ref_grams(eid: str) -> list[set]:
        if eid not in gram_cache:
            ex = experts.get(eid) or {}
            gram_cache[eid] = [grams(x) for x in (ex.get("samples") or []) + (ex.get("golden") or [])]
        return gram_cache[eid]

    rows, names, why, seen = [], [], Counter(), set()
    for author, ex in experts.items():
        for q, comment in ex["anti"]:
            q = " ".join(str(q).split())
            if not q:
                continue
            if comment is None:
                why["no_target"] += 1
                continue
            keys = parse_target(comment)
            if not keys:
                why["generic_target"] += 1
                continue
            ok = [k for k in keys if k in pool]
            if not ok:
                why["not_in_pool"] += 1
                continue
            ans = ok[0]
            if ans == author:
                why["self"] += 1
                continue
            g = grams(q)
            if any(jaccard(g, r) >= L1_JACCARD for r in ref_grams(ans)):
                why["near_sample"] += 1
                continue
            if (q, ans) in seen:
                why["dup"] += 1
                continue
            seen.add((q, ans))
            row = {"qid": "A-" + sha(f"{ans}|{q}"), "q": q, "answer": ans, "answers_ok": ok, "confuser": author,
                   "same_domain": dom_of(ans) == dom_of(author), "domain": dom_of(ans), "branch": branch(dom_of(ans))}
            if experts.get(ans) and _name_hit(q, experts[ans]):
                why["name_in_query"] += 1
                names.append({**row, "name_bundle": True, "split": "name"})
                continue
            rows.append({**row, "name_bundle": False})
    return rows, names, why


def _take(rows: list[dict], n: int, per_answer: Counter, pairs: set, rng: random.Random) -> list[dict]:
    """rows 를 섞어 상한(한 정답 PER_ANSWER · 한 (정답, 글쓴이) 1)을 지키며 n 건까지."""
    pool = list(rows)
    rng.shuffle(pool)
    got = []
    for r in pool:
        if len(got) >= n:
            break
        if per_answer[r["answer"]] >= PER_ANSWER or (r["answer"], r["confuser"]) in pairs:
            continue
        per_answer[r["answer"]] += 1
        pairs.add((r["answer"], r["confuser"]))
        got.append(r)
    return got


def stratify(rows: list[dict], seed: int, total: int = A_TOTAL) -> tuple[list[dict], dict]:
    """층화 뽑기 — QUOTA · OTHER_ALL 을 먼저, 남는 몫은 HW 도메인을 돌아가며(HW 가 다하면 xd · sw). (뽑은 것, {by_domain, fill})."""
    rng = random.Random(seed)
    by_dom = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["qid"]):
        by_dom[r["domain"]].append(r)
    per_answer, pairs, picked = Counter(), set(), []
    left = {d: [] for d in by_dom}
    for d in sorted(by_dom):
        want = QUOTA.get(d, 10 ** 6 if d in OTHER_ALL else 0)
        got = _take(by_dom[d], want, per_answer, pairs, rng)
        picked += got
        ids = {r["qid"] for r in got}
        left[d] = [r for r in by_dom[d] if r["qid"] not in ids]
    if len(picked) > total:
        rng.shuffle(picked)
        picked = picked[:total]
    # 남는 몫 — HW 도메인을 돌아가며 한 건씩(도메인 차례는 씨앗으로). HW 가 상한(한 정답 2건)에 다 걸리면 업무(xd) · SW 차례로 채운다 —
    # dev 에서 HW 는 399건이 끝이다(전문가 226명 · 반례를 받은 정답이 그보다 적다). 어디서 몇 건을 채웠는지 fill 에 적는다.
    fill = Counter()
    for tier in (HW, ("xd",), ("sw",)):
        order = [d for d in tier if left.get(d)]
        rng.shuffle(order)
        for d in order:
            rng.shuffle(left[d])
        while len(picked) < total and order:
            nxt = []
            for d in order:
                if len(picked) >= total:
                    break
                got = _take(left[d], 1, per_answer, pairs, rng)
                if got:                     # 못 뽑았으면 그 도메인은 상한에 다 걸렸다 — 다음 판에서 뺀다
                    picked += got
                    fill[d] += 1
                    left[d] = [r for r in left[d] if r["qid"] != got[0]["qid"]]
                    nxt.append(d)
            order = nxt
    counts = Counter(r["domain"] for r in picked)
    return sorted(picked, key=lambda r: r["qid"]), {"by_domain": dict(sorted(counts.items())), "fill": dict(sorted(fill.items()))}


def split_by_answer(rows: list[dict], seed: int) -> list[dict]:
    """정답 전문가 단위로 dev · test 를 가른다 — 한 전문가의 질의는 한쪽에만. 갈래(branch)마다 건수를 맞추고, 끝에 전체를 반반에 맞춘다."""
    rng = random.Random(seed + 1)
    by_ans = defaultdict(list)
    for r in rows:
        by_ans[r["answer"]].append(r)
    side: dict = {}
    n = {"dev": 0, "test": 0}
    for br in ("hw", "sw", "xd", "other"):
        experts = sorted(a for a, rs in by_ans.items() if rs[0]["branch"] == br)
        rng.shuffle(experts)
        nb = {"dev": 0, "test": 0}
        for a in experts:
            s = "dev" if (nb["dev"], n["dev"]) <= (nb["test"], n["test"]) else "test"
            side[a] = s
            nb[s] += len(by_ans[a])
            n[s] += len(by_ans[a])
    # 전체 반반 — 큰 쪽에서 1건짜리 전문가를 옮기거나(차 2 줄어듦) 2건짜리와 1건짜리를 맞바꾼다
    for _ in range(len(by_ans) * 2):
        big, small = ("dev", "test") if n["dev"] > n["test"] else ("test", "dev")
        if n[big] - n[small] < 2:
            break
        ones = sorted(a for a, s in side.items() if s == big and len(by_ans[a]) == 1)
        if ones:
            a = rng.choice(ones)
            side[a] = small
            n[big] -= 1
            n[small] += 1
            continue
        twos = sorted(a for a, s in side.items() if s == big and len(by_ans[a]) == 2)
        ones_s = sorted(a for a, s in side.items() if s == small and len(by_ans[a]) == 1)
        if twos and ones_s:
            a, b = rng.choice(twos), rng.choice(ones_s)
            side[a], side[b] = small, big
            n[big] -= 1
            n[small] += 1
            continue
        if twos and n[big] - n[small] >= 4:
            a = rng.choice(twos)
            side[a] = small
            n[big] -= 2
            n[small] += 2
            continue
        break
    return [{**r, "split": side[r["answer"]]} for r in rows]


def seal(rows: list[dict]) -> str:
    """A-test 봉인 — qid 를 정렬해 줄바꿈으로 이은 글의 sha256(전체)."""
    return hashlib.sha256("\n".join(sorted(r["qid"] for r in rows if r.get("split") == "test")).encode()).hexdigest()


def p_rows(experts: dict, pool: set, seed: int, total: int = P_TOTAL) -> tuple[list[dict], Counter, int]:
    """P — out_of_scope '문구 → 대상'. (뽑은 것, 거른 까닭, 거른 뒤 수)."""
    rows, why, seen = [], Counter(), set()
    for author, ex in experts.items():
        for line in ex["oos"]:
            if "→" not in line:
                why["no_target"] += 1
                continue
            phrase, _a, tgt = line.rpartition("→")
            phrase = " ".join(phrase.split()).strip(" -·:")
            keys = parse_target(tgt)
            if not keys:
                why["generic_target"] += 1
                continue
            ok = [k for k in keys if k in pool and k != author]
            if not ok:
                why["not_in_pool" if not [k for k in keys if k in pool] else "self"] += 1
                continue
            if not phrase or (phrase, tuple(ok)) in seen:
                why["dup"] += 1
                continue
            seen.add((phrase, tuple(ok)))
            rows.append({"qid": "P-" + sha(f"{author}|{phrase}|{','.join(ok)}"), "q": phrase, "answers_ok": ok,
                         "answer": ok[0], "author": author, "domain": dom_of(ok[0]), "branch": branch(dom_of(ok[0]))})
    rng = random.Random(seed + 2)
    base = sorted(rows, key=lambda r: r["qid"])
    rng.shuffle(base)
    per, got = Counter(), []
    for r in base:
        if len(got) >= total:
            break
        if per[r["answer"]] >= PER_ANSWER:
            continue
        per[r["answer"]] += 1
        got.append(r)
    return sorted(got, key=lambda r: r["qid"]), why, len(rows)


def c_rows(path: Path, pool: set | None) -> tuple[list[dict], Counter]:
    """C — 한국어 검색 기준선 질의(제목 베낌을 뺀 것). 정답은 카드 주인(agent_type)."""
    why, out = Counter(), []
    for ln in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            why["bad_line"] += 1
            continue
        if str(r.get("title_copy")).strip().lower() in ("true", "1"):
            why["title_copy"] += 1
            continue
        ans, q = str(r.get("agent_type") or "").strip(), " ".join(str(r.get("query") or "").split())
        if not ans or not q:
            why["no_answer"] += 1
            continue
        if pool and ans not in pool:
            why["not_in_pool"] += 1
            continue
        out.append({"qid": "C-" + sha(str(r.get("qid") or q)), "q": q, "answer": ans, "domain": dom_of(ans),
                    "branch": branch(dom_of(ans)), "style": r.get("style"), "card_lang": r.get("card_lang")})
    return sorted(out, key=lambda r: r["qid"]), why


def rel_rows(experts: dict, pool: set) -> list[dict]:
    out = set()
    for a, ex in experts.items():
        for b, rel in ex["related"]:
            if a in pool and b in pool and a != b:
                out.add((a, b, rel))
    return [{"a": a, "b": b, "rel": rel} for a, b, rel in sorted(out)]


# ── 쓰기 ─────────────────────────────────────────────────────────────────────
def default_out(seed: int) -> Path:
    import seat_box  # noqa: PLC0415 — 박스 자리 셈(서버와 같은 함수)
    return seat_box.box_dir() / "sets" / f"seed-{seed}"


def _inside_repo(p: Path) -> bool:
    try:
        p.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build(experts_root: Path, out: Path, *, seed: int = DEFAULT_SEED, c_queries: Path | None = None,
          pool_file: Path | None = None) -> dict:
    """평가셋을 짓고 manifest 를 돌려준다. 형제 리포가 없으면 {"skipped": 까닭}."""
    if not (experts_root / "knowledge").is_dir():
        return {"skipped": f"ExpertAgents 가 없다({experts_root.name}/knowledge) — A · P · 관계는 Drive 로 받은 것을 쓴다(결정 D-E5-8)"}
    if _inside_repo(out):
        raise SystemExit("평가셋을 이 리포 안에 쓰지 않는다(공개 리포 — 결정 D-E5-8). --out 을 리포 밖으로 준다")
    experts, rstats = load_experts(experts_root)
    pool = set(experts)
    if pool_file:
        txt = pool_file.read_text(encoding="utf-8").strip()
        try:
            got = json.loads(txt)
            got = [x.get("agent_type") if isinstance(x, dict) else x for x in (got.get("agents") if isinstance(got, dict) else got)]
        except ValueError:
            got = txt.split()
        pool = {str(x).strip() for x in got if str(x or "").strip()}
    cand, names, a_why = a_candidates(experts, pool)
    picked, quota = stratify(cand, seed)
    a = split_by_answer(picked, seed)
    p, p_why, p_n = p_rows(experts, pool, seed)
    rels = rel_rows(experts, pool)
    c, c_why = (c_rows(c_queries, pool if pool_file else None) if c_queries and c_queries.is_file() else ([], Counter()))
    import seat_box  # noqa: PLC0415
    seat_box.private_dir(out)
    files = {"A.jsonl": _write_jsonl(out / "A.jsonl", a), "A-name.jsonl": _write_jsonl(out / "A-name.jsonl", names),
             "P.jsonl": _write_jsonl(out / "P.jsonl", p), "rels.jsonl": _write_jsonl(out / "rels.jsonl", rels)}
    if c:
        files["C.jsonl"] = _write_jsonl(out / "C.jsonl", c)
    test = [r for r in a if r["split"] == "test"]
    man = {
        "built_at": datetime.now(KST).isoformat(timespec="seconds"), "seed": seed,
        "source": {"experts": _git_head(experts_root), "experts_n": len(experts), "pool_n": len(pool),
                   "pool_file": bool(pool_file), "c_queries": (hashlib.sha256(c_queries.read_bytes()).hexdigest()[:12]
                                                               if c_queries and c_queries.is_file() else None)},
        "read": dict(rstats),
        "counts": {"anti_total": sum(len(e["anti"]) for e in experts.values()), "A_candidates": len(cand),
                   "A": len(a), "A_dev": sum(1 for r in a if r["split"] == "dev"), "A_test": len(test),
                   "A_name": len(names), "A_answers": len({r["answer"] for r in a}),
                   "A_same_domain": sum(1 for r in a if r["same_domain"]),
                   "P_candidates": p_n, "P": len(p), "C": len(c), "rels": len(rels)},
        "filters": {"A": dict(a_why), "P": dict(p_why), "C": dict(c_why)},
        "quota": quota, "branches": dict(Counter(r["branch"] for r in a)),
        "seal": {"A-test": seal(a), "n": len(test)},
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    (out / "SEAL.txt").write_text(seal_line(man) + "\n", encoding="utf-8")
    return man


def seal_line(man: dict) -> str:
    return (f"A-test sha256={man['seal']['A-test']} n={man['seal']['n']} seed={man['seed']} "
            f"experts={man['source'].get('experts') or '?'} built={man['built_at']}")


def check_seal(d: Path) -> tuple[bool, str]:
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(x) for x in (d / "A.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    got = seal(rows)
    return got == man["seal"]["A-test"], f"{seal_line(man)} · 다시 셈 {got[:16]}…"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--experts", type=Path, default=ROOT.parent / "ExpertAgents")
    b.add_argument("--c-queries", type=Path, default=None)
    b.add_argument("--pool-file", type=Path, default=None)
    b.add_argument("--seed", type=int, default=DEFAULT_SEED)
    b.add_argument("--out", type=Path, default=None)
    s = sub.add_parser("seal")
    s.add_argument("dir", type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "seal":
        ok, line = check_seal(a.dir)
        print(("봉인 맞음 — " if ok else "⚠ 봉인이 다르다 — ") + line)
        return 0 if ok else 1
    out = a.out or default_out(a.seed)
    man = build(a.experts, out, seed=a.seed, c_queries=a.c_queries, pool_file=a.pool_file)
    if man.get("skipped"):
        print(f"건너뜀 — {man['skipped']}")
        return 0
    c = man["counts"]
    print(f"A {c['A']}(dev {c['A_dev']} · test {c['A_test']} · 정답 {c['A_answers']}명 · 같은 도메인 {c['A_same_domain']}) · "
          f"이름 묶음 {c['A_name']} · P {c['P']} · C {c['C']} · 관계 {c['rels']} → {out}")
    print(f"거름 A {man['filters']['A']} · P {man['filters']['P']}")
    print(seal_line(man))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

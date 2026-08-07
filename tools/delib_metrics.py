# 심의 산출물에서 품질 지표를 집계하는 계측 하네스 — 손잡이 A/B 판정의 기준선을 만든다.
#
# 왜 필요한가: deliberation.py 의 품질 손잡이 7종(DELIB_CROSS_EXAM 등)은 전부 기본 꺼짐이고,
# GLM-DELIB-TUNING-REVIEW.md 가 "단일 변수 A/B만" 을 규율로 정해뒀다. 무엇을 켤지 판단하려면
# 켜기 전후를 같은 자로 재야 하는데, 그 자가 없어서 아무것도 켜지 못한 상태였다.
#
# 계층 A 지표(발언 품질)는 선행 검토가 정의한 것을 따르고, 계층 B 지표(좌석 구성)는
# docs/deliberation-quality/plan.md 가 새로 요구하는 것이다.
#
# 에이전트를 부르지 않는다 — 저널·결정문 텍스트 파싱만으로 산출한다(비용 0).
#
#   실행:  python -m tools.delib_metrics <journal.jsonl | 워크플로 디렉토리 | 대화 JSON>
#          python -m tools.delib_metrics <경로> --json      # 기계 판독용
import json
import re
import sys
from pathlib import Path

# ── 토큰화 — 형태소 분석기 없이 쓰는 근사 ─────────────────────────────────────
# 한국어 형태소 분석기를 의존성으로 들이지 않는다(설치 환경이 갈린다). 대신 '신규 개념 도입률'
# 같은 지표는 절대값이 아니라 조건 간 비교(A/B)에만 쓰므로, 같은 자로 재기만 하면 근사로 충분하다.
_NUM_RE = re.compile(r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?(?:\s*[%°]|\s*[a-zA-ZµΩ°]{1,6})?")
_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9_\-]{2,}")
_SRC_TOOL_RE = re.compile(r"\(도구\)|\(측정\)|\(실측\)")
_SRC_RULE_RE = re.compile(r"\(경험칙\)|\(추정\)|\(가정\)")

# 도메인 접두사 — 페르소나 키의 첫 하이픈 앞 조각을 도메인으로 본다(disp-burnin → disp).
# 접두사가 없는 키는 키 전체를 도메인으로 취급한다.
_STOP = frozenset("""
그리고 하지만 그러나 따라서 때문에 있다 없다 한다 된다 이다 하는 되는 있는 없는 같은 위해 대한
이런 저런 그런 어떤 모든 각각 경우 수준 정도 이상 이하 여기 거기 우리 당신 지금 현재 다음
""".split())


def _numbers(text: str) -> list:
    return _NUM_RE.findall(text or "")


# 조사·어미 절단 — 형태소 분석기 없이 쓰는 근사 어간 추출. 긴 접미사부터 시도하고,
# 절단 결과가 2자 미만이면 원형을 유지한다. 없으면 '판정을'과 '판정은'이 다른 개념으로 잡혀
# non_negotiable 보존율이 문구 재작성만으로 0 이 되고, 신규개념 도입률은 과대 계상된다.
_SUFFIXES = ("에서는", "으로는", "에게는", "하는", "한다", "하며", "하고", "하여", "되는", "된다",
             "되어", "이다", "에서", "으로", "에게", "까지", "부터", "이나", "라도", "이며",
             "을", "를", "은", "는", "이", "가", "의", "에", "로", "도", "만", "과", "와", "나")


def _stem(t: str) -> str:
    if not ("가" <= t[0] <= "힣"):
        return t          # 영문·기호 토큰은 절단하지 않는다
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 2:
            return t[: -len(suf)]
    return t


def _tokens(text: str) -> set:
    return {_stem(t) for t in _TOKEN_RE.findall(text or "") if t not in _STOP}


_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*")


def _norm_key(raw) -> str:
    """저널의 persona 필드를 정본 키로 정규화한다.

    MCP 워크플로는 agent() 반환 후 withKey() 로 키를 덮어쓰지만 저널은 그 이전 원문을
    기록한다. 모델이 'disp-burnin — OLED 영구 잔상 전문가…' 처럼 역할 설명을 붙여 반환하면
    같은 전문가가 여러 명으로 세어져 착석 인원·결손율이 전부 틀어진다(실측 11 vs 실제 6)."""
    m = _KEY_RE.match(str(raw or "").strip().lower())
    return m.group(0) if m else str(raw or "").strip()[:60]


def _domain(key: str) -> str:
    k = _norm_key(key)
    return k.split("-", 1)[0] if "-" in k else k


# ── 발언 직렬화 — 라운드 종류별 필드가 다르다 ─────────────────────────────────
# 고정 목록 대신 persona 를 뺀 전 필드를 직렬화한다. 라운드·경로별로 필드가 다르고(초기는
# lens/reads/recommendation, 심화는 concede/rebut/deepen, 수렴은 final_position/vote),
# 목록을 고정하면 스키마가 늘어날 때 조용히 누락된다 — reads 를 빠뜨려 근거 표기를 놓쳤다.


def _flatten(v) -> str:
    if isinstance(v, list):
        return "\n".join(_flatten(x) for x in v)
    if isinstance(v, dict):
        return "\n".join(f"{k}: {_flatten(x)}" for k, x in v.items())
    return str(v or "")


def _speech_text(op: dict) -> str:
    return "\n".join(_flatten(v) for k, v in op.items() if k != "persona" and v)


# ── 입력 파서 ────────────────────────────────────────────────────────────────
def load_mcp_journal(path: Path) -> dict:
    """워크플로 journal.jsonl → {personas, rounds:[[op,…],…], decision}.

    라운드 경계는 저널에 없으므로 발언 스키마로 판정한다 — lens 를 가지면 초기,
    final_position 을 가지면 수렴, 그 외 concede/rebut 이면 심화다."""
    ops, texts = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "result":
            continue
        r = e.get("result")
        if isinstance(r, dict):
            ops.append(r)
        elif isinstance(r, str):
            texts.append(r)

    first = [o for o in ops if o.get("lens")]
    last = [o for o in ops if o.get("final_position")]
    mid = [o for o in ops if not o.get("lens") and not o.get("final_position")]
    rounds = [r for r in (first, mid, last) if r]
    # 결정문 — 문자열 결과 중 가장 긴 것(쉬운 설명본보다 의사결정문이 길다)
    decision = max(texts, key=len) if texts else ""
    personas = []
    for o in ops:
        k = _norm_key(o.get("persona"))
        if k and k not in personas:
            personas.append(k)
        o["persona"] = k
    return {"personas": personas, "rounds": rounds, "decision": decision,
            "n_ops": len(ops), "source": "mcp-journal"}


def load_conversation(path: Path) -> dict:
    """save_conversation 형식(JSON) → 동일 구조. 웹 경로 산출물 판독용."""
    d = json.loads(path.read_text(encoding="utf-8"))
    msgs = d.get("messages", d if isinstance(d, list) else [])
    by_round: dict = {}
    personas, decision = [], ""
    for m in msgs:
        role = m.get("role")
        if role == "persona":
            rnd = m.get("round", 1)
            key = _norm_key(m.get("persona"))
            by_round.setdefault(rnd, []).append({"persona": key, "lens": m.get("content", "")})
            if key not in personas:
                personas.append(key)
        elif role == "assistant":
            decision = m.get("content", "") or decision
    rounds = [by_round[k] for k in sorted(by_round)]
    return {"personas": personas, "rounds": rounds, "decision": decision,
            "n_ops": sum(len(r) for r in rounds), "source": "conversation"}


def load_any(target: Path) -> dict:
    if target.is_dir():
        j = target / "journal.jsonl"
        if not j.exists():
            raise SystemExit(f"journal.jsonl 없음: {target}")
        return load_mcp_journal(j)
    if target.name == "journal.jsonl" or target.suffix == ".jsonl":
        return load_mcp_journal(target)
    return load_conversation(target)


# ── 계층 A 지표 — 발언 품질(선행 검토 정의) ──────────────────────────────────
def metrics_layer_a(doc: dict) -> dict:
    rounds, decision = doc["rounds"], doc["decision"]
    n_tool = n_rule = n_num = n_chars = 0
    lens_all, targeted, n_rebut = [], 0, 0

    for rd in rounds:
        for op in rd:
            t = _speech_text(op)
            n_chars += len(t)
            n_num += len(_numbers(t))
            n_tool += len(_SRC_TOOL_RE.findall(t))
            n_rule += len(_SRC_RULE_RE.findall(t))
            lens_all.append(len(t))
            # 반박 타겟률 — rebut 항목이 특정 페르소나 키를 지목했는가
            keys = [k for k in doc["personas"] if k and k != op.get("persona")]
            for item in (op.get("rebut") or []):
                n_rebut += 1
                s = _flatten(item)
                if any(k in s for k in keys):
                    targeted += 1

    # 신규 개념 도입률 — 라운드 N 토큰 중 N-1 에 없던 비율
    novelty = []
    prev: set = set()
    for rd in rounds:
        cur = set()
        for op in rd:
            cur |= _tokens(_speech_text(op))
        if prev:
            novelty.append(round(len(cur - prev) / max(1, len(cur)), 3))
        prev |= cur

    dec_paras = [p for p in (decision or "").split("\n\n") if p.strip()]
    return {
        "수치_인용_밀도_천자당": round(n_num / max(1, n_chars) * 1000, 2),
        "근거유래_표기_건수": n_tool,
        "경험칙_표기_건수": n_rule,
        "근거유래_비율": round(n_tool / max(1, n_tool + n_rule), 3),
        "반박_타겟률": round(targeted / max(1, n_rebut), 3),
        "반박_총건수": n_rebut,
        "신규개념_도입률_라운드별": novelty,
        "결정문_수치밀도_문단당": round(len(_numbers(decision)) / max(1, len(dec_paras)), 2),
        "발언_평균길이": round(sum(lens_all) / max(1, len(lens_all))),
        "발언_최단길이": min(lens_all) if lens_all else 0,
    }


# ── 계층 B 지표 — 좌석 구성(본 계획 신규) ────────────────────────────────────
def metrics_layer_b(doc: dict, prev_doc: dict = None) -> dict:
    personas = doc["personas"]
    domains = {_domain(k) for k in personas if k}
    out = {
        "착석_인원": len(personas),
        "착석_도메인_수": len(domains),
        "착석_도메인": sorted(domains),
        "도메인_다양성": round(len(domains) / max(1, len(personas)), 3),
    }
    if prev_doc:
        prev_keys = set(prev_doc["personas"])
        cur_keys = set(personas)
        new = cur_keys - prev_keys
        out.update({
            "좌석_유임": len(cur_keys & prev_keys),
            "좌석_신규": len(new),
            "좌석_신규_목록": sorted(new),
            "좌석_변동률": round(len(new) / max(1, len(cur_keys)), 3),
        })
        # non_negotiable 보존율 — 이전 수렴 라운드의 양보 불가가 이번 산출물에 남았는가
        prev_nn = []
        for rd in prev_doc["rounds"]:
            for op in rd:
                if op.get("non_negotiable"):
                    prev_nn.append(_flatten(op["non_negotiable"]))
        haystack = (doc["decision"] or "") + "\n" + "\n".join(
            _speech_text(op) for rd in doc["rounds"] for op in rd)
        hay_tokens = _tokens(haystack)
        kept = 0
        for nn in prev_nn:
            nt = _tokens(nn)
            # 핵심어 절반 이상이 살아 있으면 승계된 것으로 본다(문구 재작성 허용)
            if nt and len(nt & hay_tokens) / len(nt) >= 0.5:
                kept += 1
        out["이전_non_negotiable_건수"] = len(prev_nn)
        out["non_negotiable_보존율"] = round(kept / max(1, len(prev_nn)), 3) if prev_nn else None
    return out


# ── 가드레일 ─────────────────────────────────────────────────────────────────
def metrics_guard(doc: dict) -> dict:
    expected = len(doc["personas"]) * max(1, len(doc["rounds"]))
    got = doc["n_ops"]
    empties = sum(1 for rd in doc["rounds"] for op in rd if not _speech_text(op).strip())
    return {
        "기대_발언수": expected,
        "실제_발언수": got,
        "결손율": round(max(0, expected - got) / max(1, expected), 3),
        "빈_발언수": empties,
        "라운드수": len(doc["rounds"]),
    }


def report(target: Path, prev: Path = None) -> dict:
    doc = load_any(target)
    prev_doc = load_any(prev) if prev else None
    return {
        "대상": str(target),
        "출처": doc["source"],
        "계층A_발언품질": metrics_layer_a(doc),
        "계층B_좌석구성": metrics_layer_b(doc, prev_doc),
        "가드레일": metrics_guard(doc),
    }


def _print_human(r: dict) -> None:
    print(f"■ {r['대상']}  ({r['출처']})")
    for section in ("계층A_발언품질", "계층B_좌석구성", "가드레일"):
        print(f"\n[{section}]")
        for k, v in r[section].items():
            print(f"  {k:<28} {v}")


def main(argv: list) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__ or "usage: python -m tools.delib_metrics <경로> [--prev <이전경로>] [--json]",
              file=sys.stderr)
        return 2
    prev = None
    if "--prev" in argv:
        i = argv.index("--prev")
        if i + 1 < len(argv):
            prev = Path(argv[i + 1])
            args = [a for a in args if a != str(prev)]
    r = report(Path(args[0]), prev)
    if "--json" in argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        _print_human(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

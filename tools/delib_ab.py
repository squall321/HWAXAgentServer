# 품질 손잡이 단일 변수 A/B 러너 — 조건별 심의를 돌리고 계측 하네스로 대조표를 만든다.
#
# 왜 필요한가: 손잡이 7종(DELIB_EVIDENCE_PREPASS 등)이 2026-07-21 이후 전부 꺼진 채인 이유는
# "무엇이 좋아지는지 잴 방법이 없어서"였다. GLM-DELIB-TUNING-REVIEW.md 가 T0 계측 하네스를
# 선행 필수로, T1~T6 을 단일 변수 A/B 로 규정했고, 하네스(tools/delib_metrics.py)는 만들었으나
# 조건을 바꿔가며 돌리는 손이 없었다. 이 스크립트가 그 손이다.
#
# ── 실행 위치 주의 ────────────────────────────────────────────────────────────
# 손잡이의 효과는 모델 의존이다. 개발 박스는 qwen2.5-7b 급이고 운영(cae00)은 GLM 이라,
# 여기서 나온 A/B 결과를 운영 기본값 결정의 근거로 쓰면 안 된다. 개발 박스에서는 러너
# 자체의 동작 확인(스모크)만 하고, 판정용 실행은 cae00 에서 한다.
#
# ── 단일 변수 원칙 ────────────────────────────────────────────────────────────
# 한 번에 손잡이 하나만 바꾼다. GLM 급은 다중 제약에서 지시 추종 예산이 분산돼 기법 적층이
# 상쇄되므로, 두 개를 같이 켜면 각각의 효과를 분리할 수 없다. --knob 은 하나만 받는다.
#
#   실행:
#     python -m tools.delib_ab --knob evidence_prepass --questions q.txt --repeat 3
#     python -m tools.delib_ab --knob chair_cite --questions q.txt --out ab_chair_cite/
#     python -m tools.delib_ab --report ab_chair_cite/          # 저장된 결과만 재집계
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.delib_metrics import metrics_layer_a, metrics_layer_b  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8000/chat"

# T-서열 순서(GLM-DELIB-TUNING-REVIEW §실행 순서). 이 순서대로 하나씩 A/B 한다.
KNOB_ORDER = ("evidence_prepass", "rebut_quote", "prose_first",
              "chair_bestof", "chair_cite", "cross_exam", "anchor")
# best-of 는 0/1 이 아니라 1/3 이 대조군이다(1=끔).
KNOB_LEVELS = {"chair_bestof": (1, 3)}


def levels_of(knob: str) -> tuple:
    return KNOB_LEVELS.get(knob, (0, 1))


# ── 심의 1회 실행 ────────────────────────────────────────────────────────────
async def run_one(url: str, question: str, opts: dict, timeout: float) -> dict:
    """/chat 에 심의 트리거를 보내고 SSE 를 접어 {personas, rounds, decision} 로 만든다.

    aiohttp 를 새로 들이지 않고 표준 라이브러리만 쓴다 — 러너 때문에 서버 의존성이 늘면
    cae00 배포에서 걸린다."""
    import urllib.request

    body = json.dumps({"message": f"/심의 {question}", "groups": [], "delib_opts": opts},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    personas, rounds, decision = [], {}, ""
    t0 = time.time()

    def _pump():
        nonlocal decision
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                kind = ev.get("kind") or ev.get("type")
                if kind == "personas":
                    for p in ev.get("personas", []):
                        if p.get("key") and p["key"] not in personas:
                            personas.append(p["key"])
                elif kind == "turn":
                    rnd = ev.get("round") or ev.get("r") or 1
                    rounds.setdefault(rnd, []).append(ev.get("data") or ev)
                elif ev.get("type") == "result":
                    decision = ev.get("content", "") or decision

    await asyncio.get_running_loop().run_in_executor(None, _pump)
    return {"personas": personas, "rounds": [rounds[k] for k in sorted(rounds)],
            "decision": decision, "n_ops": sum(len(v) for v in rounds.values()),
            "source": "chat-sse", "elapsed_s": round(time.time() - t0, 1)}


# ── 조건 실행과 집계 ─────────────────────────────────────────────────────────
async def run_condition(url, questions, knob, level, repeat, timeout, out_dir) -> list:
    docs = []
    for qi, q in enumerate(questions):
        for rep in range(repeat):
            opts = {knob: level}
            tag = f"{knob}={level}_q{qi}_r{rep}"
            print(f"  … {tag}", flush=True)
            try:
                doc = await run_one(url, q, opts, timeout)
            except Exception as exc:  # noqa: BLE001 — 1건 실패가 조건 전체를 죽이지 않게
                print(f"    ✗ 실패: {str(exc)[:120]}")
                continue
            doc["tag"], doc["question"] = tag, q
            docs.append(doc)
            if out_dir:
                (out_dir / f"{tag}.json").write_text(
                    json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return docs


def aggregate(docs: list) -> dict:
    """조건 내 반복의 평균 — 단일 실행의 분산에 속지 않게."""
    if not docs:
        return {}
    keys = ("수치_인용_밀도_천자당", "근거유래_비율", "반박_타겟률",
            "결정문_수치밀도_문단당", "발언_평균길이", "발언_최단길이")
    acc, n = {k: 0.0 for k in keys}, 0
    dom, seats = 0.0, 0.0
    for d in docs:
        a, b = metrics_layer_a(d), metrics_layer_b(d)
        for k in keys:
            acc[k] += float(a.get(k) or 0)
        dom += float(b.get("도메인_다양성") or 0)
        seats += float(b.get("착석_인원") or 0)
        n += 1
    out = {k: round(v / n, 3) for k, v in acc.items()}
    out["도메인_다양성"] = round(dom / n, 3)
    out["착석_인원"] = round(seats / n, 1)
    out["표본수"] = n
    out["평균_소요s"] = round(sum(d.get("elapsed_s", 0) for d in docs) / n, 1)
    return out


def compare(off: dict, on: dict, knob: str, level_on) -> str:
    lines = [f"■ 손잡이 {knob} — 대조군 vs {level_on}", ""]
    if not off or not on:
        return "\n".join(lines + ["  표본 부족 — 판정 불가"])
    w = max(len(k) for k in off)
    lines.append(f"  {'지표':<{w}}  {'대조군':>10} {'실험군':>10} {'변화':>10}")
    for k in off:
        a, b = off[k], on.get(k, 0)
        d = b - a
        pct = f"{d / a * 100:+.1f}%" if a else ("—" if not d else "신규")
        lines.append(f"  {k:<{w}}  {a:>10} {b:>10} {pct:>10}")
    lines += ["", "  판정은 사람이 한다 — 지표는 근거이지 결론이 아니다.",
              "  특히 발언 길이 증가는 품질 증가가 아니다(verbose-but-shallow 는 대리 지표로 판별 불가).",
              "  결론 미주입 홀드아웃 재현이 유일한 실측 장치라는 선행 검토 §7 을 함께 볼 것."]
    return "\n".join(lines)


async def main_async(a) -> int:
    out_dir = Path(a.out) if a.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    questions = [q.strip() for q in Path(a.questions).read_text(encoding="utf-8").splitlines()
                 if q.strip() and not q.startswith("#")]
    if not questions:
        print("질문 파일이 비어 있다", file=sys.stderr)
        return 2
    lo, hi = levels_of(a.knob)
    print(f"■ {a.knob} A/B — 질문 {len(questions)}개 × 반복 {a.repeat} × 조건 2 "
          f"= 심의 {len(questions) * a.repeat * 2}회\n")
    print(f"[대조군 {a.knob}={lo}]")
    off = await run_condition(a.url, questions, a.knob, lo, a.repeat, a.timeout, out_dir)
    print(f"[실험군 {a.knob}={hi}]")
    on = await run_condition(a.url, questions, a.knob, hi, a.repeat, a.timeout, out_dir)
    print("\n" + compare(aggregate(off), aggregate(on), a.knob, hi))
    if out_dir:
        (out_dir / "summary.json").write_text(json.dumps(
            {"knob": a.knob, "levels": [lo, hi],
             "off": aggregate(off), "on": aggregate(on)}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n  저장: {out_dir}")
    return 0


def main(argv: list) -> int:
    p = argparse.ArgumentParser(description="심의 품질 손잡이 단일 변수 A/B")
    p.add_argument("--knob", choices=KNOB_ORDER, help=f"T-서열 순서: {' → '.join(KNOB_ORDER)}")
    p.add_argument("--questions", help="질문 파일(한 줄에 하나, # 주석)")
    p.add_argument("--repeat", type=int, default=3, help="조건·질문당 반복 (기본 3)")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--timeout", type=float, default=1800)
    p.add_argument("--out", help="결과 저장 디렉토리")
    p.add_argument("--report", help="저장된 디렉토리에서 재집계만")
    a = p.parse_args(argv)

    if a.report:
        d = Path(a.report)
        docs = [json.loads(f.read_text(encoding="utf-8"))
                for f in sorted(d.glob("*.json")) if f.name != "summary.json"]
        if not docs:
            print("결과 파일이 없다", file=sys.stderr)
            return 2
        knob = docs[0]["tag"].split("=")[0]
        lo, hi = levels_of(knob)
        off = [x for x in docs if x["tag"].startswith(f"{knob}={lo}_")]
        on = [x for x in docs if x["tag"].startswith(f"{knob}={hi}_")]
        print(compare(aggregate(off), aggregate(on), knob, hi))
        return 0

    if not (a.knob and a.questions):
        p.error("--knob 과 --questions 가 필요하다(또는 --report)")
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

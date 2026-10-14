# 챗 첫 조각 지연 탐침 — 에이전트 서버와 같은 .env 로 LLM 에 1토큰짜리 스트리밍 요청을 N초마다 보내 첫 조각까지의 초와 그때의 /health(카드 대조·심의 수)를 파일에 적는다
"""리스크 심사 재설계 M3 E3-9. D-34 '챗 첫 응답 지연이 평소의 2배를 넘으면 알린다' 를 잴 표본을 만든다.

엔진의 /health.load.chat 은 실제 챗 턴에서만 표본이 생긴다 — 며칠짜리 전원 실주행 동안 챗이 드물면 표본이 없다. 이 탐침은
1분마다(기본) LLM 에 max_tokens 1 짜리 스트리밍 요청을 보내 첫 조각까지의 초를 재고, 그 순간의 에이전트 서버 /health 에서
review_active(카드 대조)·delib_active(심의)·card_review.limit 을 같이 적는다. 실주행 전 한 시간(기준선)과 실주행 중에 켜 둔다.

안전(M0-b 탐침과 같은 규율).
  · 포트를 열지 않는다. 쓰는 파일은 --out 하나다(이미 있으면 덮지 않고 멈춘다).
  · 비밀을 찍지 않는다 — LLM 키는 .env(또는 환경)에서만 읽고 어디에도 쓰지 않는다. 주소는 사용자 정보·쿼리를 떼고 찍는다.
  · 프롬프트는 고정된 짧은 글이다(카드·과제 원문이 없다). /health 는 루프백으로 프록시 없이 묻는다.
  · --dry-run 은 LLM 도 /health 도 부르지 않는다(설정을 읽어 계획만 찍고, 그 수만큼 빈 표본을 쓴다).

  .venv/bin/python scripts/llm_ttft_probe.py --dry-run --count 3
  .venv/bin/python scripts/llm_ttft_probe.py --out ttft-1014.jsonl                      (Ctrl-C·TERM 이면 요약을 쓰고 끝낸다)
  .venv/bin/python scripts/llm_ttft_probe.py --out ttft.jsonl --interval 30 --count 120
  .venv/bin/python scripts/llm_ttft_probe.py --summarize ttft-1014.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from m0b_probe import read_env_file, scrub, show_url  # noqa: E402

PROBE_REV = "ttft-1"
PROMPT = "한 글자로 답하라: 네"
DEFAULT_HEALTH = "http://127.0.0.1:9009/health"


def _pct(xs: list, q: float):
    if not xs:
        return None
    ys = sorted(xs)
    return round(ys[min(len(ys) - 1, max(0, math.ceil(q * len(ys)) - 1))], 3)


def summarize(rows: list) -> dict:
    """표본 → {n, ok, idle{n, p50, p95}, review{n, p50, p95}, ratio_p50}. review 는 카드 대조가 1건 이상 돌던 표본이다."""
    ok = [r for r in rows if r.get("ok") and isinstance(r.get("ttft_s"), (int, float))]
    idle = [r["ttft_s"] for r in ok if not r.get("review_active")]
    rev = [r["ttft_s"] for r in ok if r.get("review_active")]
    i50, r50 = _pct(idle, 0.5), _pct(rev, 0.5)
    return {"t": "summary", "rev": PROBE_REV, "n": len(rows), "ok": len(ok),
            "idle": {"n": len(idle), "p50": i50, "p95": _pct(idle, 0.95)},
            "review": {"n": len(rev), "p50": r50, "p95": _pct(rev, 0.95)},
            "ratio_p50": round(r50 / i50, 2) if i50 and r50 is not None else None}


def health_now(url: str, client) -> dict:
    """그 순간의 에이전트 서버 수 — 못 읽으면 빈 값과 사유(재지 않은 것을 0 으로 적지 않는다)."""
    try:
        h = client.get(url, timeout=4).json()
        cr = h.get("card_review") or {}
        return {"review_active": h.get("review_active"), "delib_active": h.get("delib_active"),
                "limit": cr.get("limit"), "limit_reason": cr.get("limit_reason")}
    except Exception as exc:  # noqa: BLE001
        return {"review_active": None, "delib_active": None, "health_error": type(exc).__name__}


def ttft_once(base: str, model: str, key: str, client, timeout_s: float) -> dict:
    """스트리밍 요청 한 번 — 첫 data 조각까지의 초. 실패면 ok=false 와 오류 이름(본문은 쓰지 않는다)."""
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": 1, "stream": True}
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    t0 = time.monotonic()
    try:
        with client.stream("POST", f"{base.rstrip('/')}/chat/completions", json=body, headers=headers,
                           timeout=timeout_s) as r:
            if r.status_code != 200:
                return {"ok": False, "error": f"http_{r.status_code}", "ttft_s": None}
            for line in r.iter_lines():
                if line.startswith("data:") and line[5:].strip() and line[5:].strip() != "[DONE]":
                    return {"ok": True, "ttft_s": round(time.monotonic() - t0, 3)}
        return {"ok": False, "error": "no_chunk", "ttft_s": None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__, "ttft_s": None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="챗 첫 조각 지연 탐침(M3 E3-9)")
    ap.add_argument("--out", help="결과 파일(JSON Lines). 이미 있으면 덮지 않고 멈춘다")
    ap.add_argument("--interval", type=float, default=60.0, help="표본 사이 초(기본 60)")
    ap.add_argument("--count", type=int, default=0, help="표본 수(0 = 멈출 때까지)")
    ap.add_argument("--health", default=DEFAULT_HEALTH, help="에이전트 서버 /health 주소")
    ap.add_argument("--env", default=str(ROOT / ".env"), help="에이전트 서버의 .env(같은 LLM 을 잰다)")
    ap.add_argument("--timeout", type=float, default=300.0, help="요청 하나의 기한(초)")
    ap.add_argument("--dry-run", action="store_true", help="LLM·/health 를 부르지 않는다")
    ap.add_argument("--summarize", metavar="FILE", help="파일의 표본에서 요약만 다시")
    a = ap.parse_args(argv)
    if a.summarize:
        rows = [json.loads(ln) for ln in Path(a.summarize).read_text(encoding="utf-8").splitlines() if ln.strip()]
        print(json.dumps(summarize([r for r in rows if r.get("t") == "sample"]), ensure_ascii=False))
        return 0
    if a.out and Path(a.out).exists():
        print(f"[ttft] {a.out} 가 이미 있다 — 덮지 않는다. 다른 이름을 준다", file=sys.stderr)
        return 2
    if not a.out and not a.dry_run:
        print("[ttft] --out 이 없다 — 결과를 쓸 파일 하나를 준다(--dry-run 은 없어도 된다)", file=sys.stderr)
        return 2
    env, _skipped = read_env_file(Path(a.env))
    base = os.environ.get("VLLM_BASE_URL") or env.get("VLLM_BASE_URL") or "http://localhost:8000/v1"
    model = os.environ.get("VLLM_MODEL") or env.get("VLLM_MODEL") or ""
    key = os.environ.get("VLLM_API_KEY") or env.get("VLLM_API_KEY") or ""
    print(f"[ttft] {PROBE_REV} LLM {show_url(base)} model={model or '(없음)'} 간격 {a.interval:g}초 "
          f"표본 {a.count or '멈출 때까지'} /health {show_url(a.health)}{' (dry-run)' if a.dry_run else ''}")
    stop = {"now": False}

    def _halt(*_):
        stop["now"] = True

    signal.signal(signal.SIGTERM, _halt)
    signal.signal(signal.SIGINT, _halt)
    out = open(a.out, "x", encoding="utf-8") if a.out else None
    rows: list = []
    client = None
    try:
        if not a.dry_run:
            import httpx  # noqa: PLC0415

            client = httpx.Client(trust_env=False)
        n = 0
        while not stop["now"] and (a.count <= 0 or n < a.count):
            n += 1
            if a.dry_run:
                row = {"t": "sample", "i": n, "ts": int(time.time()), "dry_run": True, "ok": False, "ttft_s": None,
                       "review_active": None, "delib_active": None}
            else:
                hl = health_now(a.health, client)
                row = {"t": "sample", "i": n, "ts": int(time.time()), **ttft_once(base, model, key, client, a.timeout),
                       **hl}
            rows.append(row)
            line = scrub(json.dumps(row, ensure_ascii=False), [key])
            if out:
                out.write(line + "\n")
                out.flush()
            print(f"[ttft] {n} ok={row['ok']} ttft={row['ttft_s']}s 카드 대조 {row.get('review_active')} "
                  f"심의 {row.get('delib_active')}")
            if a.count > 0 and n >= a.count:
                break
            t_end = time.monotonic() + a.interval
            while not stop["now"] and time.monotonic() < t_end:
                time.sleep(min(0.5, max(0.0, t_end - time.monotonic())))
    finally:
        if client is not None:
            client.close()
        s = summarize(rows)
        print("[ttft] 요약 " + json.dumps(s, ensure_ascii=False))
        if out:
            out.write(json.dumps(s, ensure_ascii=False) + "\n")
            out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

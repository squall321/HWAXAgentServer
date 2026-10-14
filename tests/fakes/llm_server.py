# 카드 대조 배선 리허설용 OpenAI 호환 가짜 LLM 서버 — /v1/models 는 정해 준 창을, /v1/chat/completions 는 대본(fakes.llm)대로 답한다
"""dev LLM 은 16K 창이라 1백만 토큰 예산을 실제 스택으로 볼 수 없다(M2.md 4.6). 이 서버를 빈 포트에 띄우고 임시 에이전트 서버의
VLLM_BASE_URL 을 여기로 돌리면, 리스크 앱 → 포털 중계 → 엔진 → (가짜) LLM 전체 배선을 1,048,576 창으로 돌려 볼 수 있다.
판정의 질은 보지 않는다(cae00 파일럿 몫). 시험 디렉터리에만 있다 — 운영 코드에 가짜 손잡이를 두지 않는다(D-15).

    .venv/bin/python tests/fakes/llm_server.py --port 18123 --ctx 1048576 --model fake-glm --script oracle

  --ctx 0       /v1/models 에 창 칸을 싣지 않는다(엔진의 창 출처가 env·fallback 으로 가는 길).
  --script      거동 이름을 쉼표로(fakes/llm.py 의 BEHAVIORS). 다 떨어지면 마지막 거동을 되풀이한다.
  --capacity N  동시에 처리하는 요청 수(0 = 무제한). 넘치는 요청은 줄 서서 기다린다 — 공유 LLM 이 카드 대조로 밀릴 때 챗 첫
                조각이 늦어지는 것을 흉내 낸다(M3 E3-9 — 챗 첫 조각 지연 계측의 시험).

끝점 — GET /health · GET /v1/models · POST /v1/chat/completions(stream:true 면 OpenAI 꼴의 SSE 조각으로 — 챗 경로가 쓴다.
카드 대조·심의는 비스트리밍이다) · POST /_script {"script": [...]}(대본을 갈고 호출 수를 0 으로) ·
POST /_hold?s=<초>(--capacity 자리 하나를 그만큼 쥔다) · GET /_stats {calls, active, peak, behaviors, capacity, waiting,
waited_peak}.
오류 거동 — http_NNN 은 그 상태 코드로 답하고, timeout 은 답하지 않고 기다린다(호출자의 시간 한도가 먼저 걸린다).
unreachable 은 서버가 흉내 낼 수 없다 — 닫힌 포트를 가리켜라(여기서는 503 으로 답한다).
9009(떠 있는 에이전트 서버)에는 뜨지 않는다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # tests/ — fakes 패키지

from fakes.llm import HTTP_ERRORS, act, respond, usage_of  # noqa: E402

HANG_S = 24 * 3600


def _text(content) -> str:
    """OpenAI 메시지 content — 문자열이거나 [{type:"text", text}] 목록이다."""
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def make_app(*, ctx: int, model: str, script, capacity: int = 0):
    # Request 를 모듈에서 가져와야 한다 — from __future__ annotations 아래에서 함수 안에서 가져오면 FastAPI 가 그 주석을
    # 풀지 못해 request 를 쿼리 인자로 읽는다(모든 호출이 422).
    state = {"script": [act(x) for x in script] or [act("oracle")], "calls": 0, "active": 0, "peak": 0,
             "behaviors": [], "capacity": max(0, int(capacity)), "waiting": 0, "waited_peak": 0}
    slots: dict = {}            # 이벤트 루프 안에서 처음 쓸 때 만든다(모듈 전역 asyncio 객체는 다른 루프에서 죽는다)
    app = FastAPI(title="fake LLM")

    async def take():
        """--capacity 자리 하나 — 없으면 줄 서서 기다린다. 무제한이면 곧바로."""
        if not state["capacity"]:
            return
        sem = slots.setdefault("sem", asyncio.Semaphore(state["capacity"]))
        state["waiting"] += 1
        state["waited_peak"] = max(state["waited_peak"], state["waiting"])
        try:
            await sem.acquire()
        finally:
            state["waiting"] -= 1

    def give():
        if state["capacity"]:
            slots["sem"].release()

    @app.get("/health")
    def health():
        return {"status": "ok", "model": model, "ctx": ctx}

    @app.get("/v1/models")
    def models():
        m = {"id": model, "object": "model", "created": 0, "owned_by": "fake"}
        if ctx > 0:
            m["max_model_len"] = ctx
        return {"object": "list", "data": [m]}

    @app.post("/_script")
    async def set_script(request: Request):
        body = await request.json()
        state["script"] = [act(x) for x in body.get("script") or ["oracle"]]
        state.update(calls=0, peak=0, behaviors=[])
        return {"ok": True, "script": [a.behavior for a in state["script"]]}

    @app.post("/_hold")
    async def hold(s: float = 0.5):
        """--capacity 자리 하나를 s 초 쥔다 — 카드 대조 호출이 공유 LLM 을 차지한 것을 흉내 낸다(챗 첫 조각 지연 시험)."""
        await take()
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(max(0.0, min(float(s), 60.0)))
        finally:
            state["active"] -= 1
            give()
        return {"held_s": s}

    @app.get("/_stats")
    def stats():
        return {k: state[k] for k in ("calls", "active", "peak", "behaviors", "capacity", "waiting", "waited_peak")}

    def chunk(i: int, delta: dict, finish=None) -> bytes:
        c = {"id": f"fake-{i}", "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode()

    async def stream(a, system: str, human: str):
        """stream:true — 자리를 잡고(줄 서기 포함), 대본의 지연 뒤 본문을 조각으로 흘린다. 자리는 다 흘린 뒤 돌려준다."""
        await take()
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            if a.delay:
                await asyncio.sleep(a.delay)
            text, finish = respond(a, system, human)
            i = state["calls"]
            yield chunk(i, {"role": "assistant", "content": ""})
            for k in range(0, len(text), 40):
                yield chunk(i, {"content": text[k:k + 40]})
            yield chunk(i, {}, finish or "stop")
            yield b"data: [DONE]\n\n"
        finally:
            state["active"] -= 1
            give()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        msgs = body.get("messages") or []
        system = "".join(_text(m.get("content")) for m in msgs if m.get("role") == "system")
        human = "".join(_text(m.get("content")) for m in msgs if m.get("role") != "system")
        a = state["script"][min(state["calls"], len(state["script"]) - 1)]
        state["calls"] += 1
        state["behaviors"].append(a.behavior)
        if body.get("stream") and not (a.text is None and (a.behavior in HTTP_ERRORS or a.behavior
                                                           in ("unreachable", "timeout"))):
            return StreamingResponse(stream(a, system, human), media_type="text/event-stream")
        await take()
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            if a.delay:
                await asyncio.sleep(a.delay)
            if a.text is None and a.behavior == "timeout":
                await asyncio.sleep(HANG_S)
            if a.text is None and (a.behavior in HTTP_ERRORS or a.behavior == "unreachable"):
                code = HTTP_ERRORS.get(a.behavior, 503)
                return JSONResponse({"error": {"message": f"fake {code}", "type": "fake", "code": code}},
                                    status_code=code)
            text, finish = respond(a, system, human)
            out = {"id": f"fake-{state['calls']}", "object": "chat.completion", "created": int(time.time()),
                   "model": model,
                   "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                "finish_reason": finish}]}
            if a.usage:
                out["usage"] = usage_of(system, human, text)
            return out
        finally:
            state["active"] -= 1
            give()

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="카드 대조 배선 리허설용 OpenAI 호환 가짜 LLM 서버")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ctx", type=int, default=1_048_576, help="/v1/models 의 max_model_len(0 이면 싣지 않는다)")
    ap.add_argument("--model", default="fake-glm")
    ap.add_argument("--script", default="oracle", help="거동 이름을 쉼표로")
    ap.add_argument("--capacity", type=int, default=0, help="동시 처리 수(0 = 무제한) — 넘치면 줄 서서 기다린다")
    args = ap.parse_args(argv)
    if args.port == 9009:
        print("9009 는 떠 있는 에이전트 서버의 포트다 — 빈 포트를 고른다", file=sys.stderr)
        return 2
    import uvicorn

    app = make_app(ctx=args.ctx, model=args.model, script=[s.strip() for s in args.script.split(",") if s.strip()],
                   capacity=args.capacity)
    print(f"[fake-llm] http://{args.host}:{args.port}/v1 model={args.model} ctx={args.ctx:,} script={args.script} "
          f"capacity={args.capacity or '무제한'}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())

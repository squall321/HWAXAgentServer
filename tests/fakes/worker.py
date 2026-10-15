# 외부 응답자 시험용 가짜 일꾼 — 실제 MCP 도구 함수(review_task_next·read·submit)로 과제를 집어 쪽으로 읽고 답을 조각내 낸다
"""Claude Code 워크플로(X-3) 자리의 대역이다. 실제 Claude 는 부르지 않는다(사용자 결정 X-D1 — 실제 Claude 시험은 하지 않는다).

답(brain)은 가짜 LLM 의 대본(`fakes.llm.respond`)이다 — 카드 대조 프롬프트면 정답 행, 심의 프롬프트면 심의 대본. 그래서 같은
프롬프트에 GLM 자리의 FakeLLM 과 같은 글을 낸다(두 길의 결과를 견줄 수 있다).

도는 법 둘 — 같은 이벤트 루프의 태스크(`serve`)거나, 따로 루프를 가진 스레드(`thread`). TestClient 로 실제 라우트를 부를 때는
앱이 다른 스레드의 루프에서 돌므로 스레드로 돈다(과제 줄은 잠금으로 지키고 답은 그 루프로 넘긴다).
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from fakes.llm import act, default_oracle, respond


def ctx_of(user: str):
    """게이트웨이가 신원 헤더를 실어 보낸 호출 — mcp_server._caller 가 읽는 모양."""
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={"x-hwax-user": user})))


def oracle_brain(head: dict, system: str, human: str) -> str:
    return respond(act("oracle"), system, human, default_oracle)[0]


class FakeWorker:
    """review_task_next → review_task_read(쪽) → review_task_submit(조각) 을 되풀이한다.

    seen — 집은 과제마다 {head, system, human, answer, pages, submits}. brain(head, system, human) → 답 글. chunk — 답 조각 크기(자).
    hold — 참이면 집기만 하고 답하지 않는다(답 한도 시험). stop() 으로 멈춘다."""

    def __init__(self, user: str, *, brain=oracle_brain, chunk: int = 700, hold: bool = False, poll_s: float = 0.2):
        import mcp_server  # noqa: PLC0415 — 시험이 _APP 을 바꿔 끼운 뒤에 부른다

        self.m, self.ctx = mcp_server, ctx_of(user)
        self.brain, self.chunk, self.hold, self.poll_s = brain, chunk, hold, poll_s
        self.seen: list = []
        self.errors: list = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    async def read_all(self, head: dict) -> tuple[str, str, list]:
        """프롬프트 전문을 쪽으로 받아 머리의 parts 로 system·human 을 가른다 — (system, human, 쪽 머리들)."""
        text, pages, offset = "", [], 0
        while offset is not None:
            page = await self.m.review_task_read(head["task_id"], offset, ctx=self.ctx)
            assert page.get("ok", True), page
            pages.append({k: page[k] for k in ("offset", "next_offset", "chars")} | {"text_chars": len(page["text"])})
            text += page["text"]
            offset = page["next_offset"]
        assert len(text) == head["chars"], (len(text), head["chars"])
        by = {p["role"]: text[p["start"]:p["start"] + p["chars"]] for p in head["parts"]}
        return by.get("system", ""), by.get("human", ""), pages

    async def submit_all(self, task_id: str, answer: str) -> list:
        """답을 chunk 자씩 offset 을 달아 낸다 — 마지막 조각에 final."""
        out, at = [], 0
        pieces = [answer[i:i + self.chunk] for i in range(0, len(answer), self.chunk)] or [""]
        for i, piece in enumerate(pieces):
            r = await self.m.review_task_submit(task_id, piece, offset=at, final=i == len(pieces) - 1, ctx=self.ctx)
            out.append(r)
            if not r.get("ok"):
                break
            at += len(piece)
        return out

    async def handle(self, head: dict) -> None:
        system, human, pages = await self.read_all(head)
        rec = {"head": head, "system": system, "human": human, "pages": pages}
        self.seen.append(rec)
        if self.hold:
            return
        rec["answer"] = self.brain(head, system, human)
        rec["submits"] = await self.submit_all(head["task_id"], rec["answer"])

    async def serve(self) -> None:
        """멈출 때까지 집고 답한다(같은 루프의 태스크로 돌릴 때)."""
        while not self._stop.is_set():
            try:
                head = await self.m.review_task_next(wait_s=self.poll_s, ctx=self.ctx)
                if not head.get("empty"):
                    await self.handle(head)
            except Exception as exc:  # noqa: BLE001 — 시험이 errors 로 본다
                self.errors.append(repr(exc))
                await asyncio.sleep(self.poll_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def thread(self) -> "FakeWorker":
        """따로 루프를 가진 스레드에서 serve 를 돌린다(TestClient 로 실제 라우트를 부를 때)."""
        self._thread = threading.Thread(target=lambda: asyncio.run(self.serve()), name="fake-external-worker",
                                        daemon=True)
        self._thread.start()
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stop()

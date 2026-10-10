# deliberate_* MCP 도구 응답을 Claude Code 의 MCP 결과 상한 안으로 쪽을 나눠 내준다 — 잘라 버리지 않는다
"""MCP 결과 쪽 나누기 — deliberate_* 도구가 같이 쓴다.

**바깥 한도.** Claude Code 는 MCP 도구 결과가 MAX_MCP_OUTPUT_TOKENS(기본 25,000토큰)를 넘으면 본문 대신 '파일로 저장했다' 는
안내를 주고, 그 판정을 끈 판(ENABLE_MCP_LARGE_OUTPUT_FILES=0)은 앞 100,000자에서 자른다(2.0.74 판에서 읽은 동작 — 글자 수/4 로
어림해 상한의 절반 이하면 세지 않고 넘긴다). 운영 모델의 창(1백만 토큰)과 상관없는 바깥 한도다. 넓은 창에서 끊지 않게 된 발언·
결정문이 한 응답에 다 실리면, 받는 쪽은 '받은 것이 전부' 라고 믿을 수 없다 — 저장된 파일을 끝까지 읽지 않은 에이전트가 뒤쪽 턴이
빠진 전사를 리스크 원장에 낼 수 있었다(검토 engine-contract:A-transcript-size).

**푸는 법 — 자르지 않고 쪽을 나눈다.** 응답은 FastMCP 가 내보내는 글(pydantic_core.to_json indent=2 — 게이트웨이는 그 글을 그대로
넘긴다)로 잰다. PAGE_CHARS 안이면 손대지 않는다. 넘으면 칸을 순서대로 채워 쪽을 만든다 — 글은 글자 경계에서, 목록은 항목 경계에서
나누고, 일부만 실린 칸은 그 쪽의 page.pieces 에 적는다(path · from · to · of). 다음 쪽은 page.next 의 인자로 같은 도구를 부른다.
쪽은 부를 때마다 처음부터 같은 규칙으로 다시 나눈다 — 끝난 심의는 내용이 바뀌지 않으므로 쪽 번호가 곧 위치다. 쪽 경계는 쪽마다
되풀이하는 숫자 칸(ids)과 쪽 안내(page)의 실제 값이 아니라 그 상한으로 잡는다 — 뒤에만 붙는 응답(도는 심의의 전사)은 앞 쪽들의
경계가 바뀌지 않는다.
"""
from __future__ import annotations

import logging

import pydantic_core

from delib_jobs import _env_int

log = logging.getLogger("agent.mcp_pages")

# 쪽 크기(글자) — Claude Code 의 MCP 결과 상한(MAX_MCP_OUTPUT_TOKENS 기본 25,000토큰)을 1자≈1토큰으로 잡은 값이다. 한글은 1자가
# 1토큰 남짓이고 JSON 의 키·들여쓰기는 그보다 싸다 — HWAXRisk(tests/test_mcp_brief_relay 의 CLAUDE_MCP_RESULT_CHARS)·포털 JS
# 파이프라인과 같은 보수 환산이다. 2.0.74 판은 글자 수/4 가 상한의 절반(50,000자) 이하면 세지도 않으니 그 판에서는 두 배 여유다.
# 사용자 PC 의 MAX_MCP_OUTPUT_TOKENS 를 낮춰 쓰면 이 값도 그만큼 낮춘다. 바닥은 쪽 안내와 되풀이 칸이 들어갈 자리다.
PAGE_FLOOR = 4_000
PAGE_CHARS = _env_int("DELIB_MCP_PAGE_CHARS", 25_000)
if PAGE_CHARS < PAGE_FLOOR:
    log.warning("env DELIB_MCP_PAGE_CHARS=%d 는 바닥 %d 미만 — %d 로 읽는다", PAGE_CHARS, PAGE_FLOOR, PAGE_FLOOR)
    PAGE_CHARS = PAGE_FLOOR

# 쪽마다 되풀이하는 숫자·null 칸(ids)의 자리 — 쪽 경계를 실제 값이 아니라 9자리로 잡는다. 도는 전사의 total·next_offset 이
# 늘어 자릿수가 바뀌어도 앞 쪽들의 경계가 그대로이게 한다. 글 칸은 실제 값으로 잡는다 — 되풀이하는 글 칸은 잡 id 처럼 쪽을
# 받는 동안 바뀌지 않는 것만 둔다(글과 null 을 오가는 전사의 note 는 ids 에 넣지 않고 tail 로 맨 뒤에 둔다).
_NUM = 999_999_999
_PART = 99_999
# 쪽에 이미 실린 것이 있으면 이보다 짧은 글 조각으로 쪽을 끝내지 않는다(다음 쪽에 통째로 넘긴다).
_MIN_SLICE = 200


def wire(obj) -> str:
    """FastMCP 가 도구 결과(dict)로 내보내는 글 그대로 — mcp.server.fastmcp 의 _convert_to_content 와 같은 호출이다."""
    return pydantic_core.to_json(obj, fallback=str, indent=2).decode()


def _jlen(v) -> int:
    """값 하나의 JSON 글 길이(글자 — to_json 은 UTF-8 바이트를 주니 풀어서 센다. 글·숫자·빈 목록은 들여쓰기와 상관없다)."""
    return len(pydantic_core.to_json(v, fallback=str).decode())


def _flatten(body: dict) -> tuple[list, dict]:
    """응답을 잎(경로, 값)으로 편다 — 둘째 값은 목록 칸마다 (첫 잎, 끝 잎, 항목 수)다.

    빈 목록·빈 사전은 그 자체가 잎이다. 사전 키는 글로 읽는다 — 경로의 정수는 늘 목록 번호다."""
    leaves: list = []
    lists: dict = {}

    def walk(v, at):
        if isinstance(v, dict) and v:
            for k, x in v.items():
                walk(x, at + (str(k),))
        elif isinstance(v, (list, tuple)) and v:
            start = len(leaves)
            for i, x in enumerate(v):
                walk(x, at + (i,))
            lists[at] = (start, len(leaves), len(v))
        else:
            leaves.append((at, v))

    for k, v in body.items():
        walk(v, (str(k),))
    return leaves, lists


def _path(at) -> str:
    """경로 → 'turns[7].say' 꼴."""
    s = ""
    for p in at:
        s += f"[{p}]" if isinstance(p, int) else (f".{p}" if s else p)
    return s


class _Layout:
    """쪽 하나의 글 길이를 잎을 더할 때마다 정확히 센다 — indent=2 의 줄바꿈·들여쓰기·쉼표 그대로.

    깊이 i 인 칸에 항목을 하나 더하면, 비어 있던 칸('{}' 2자)은 4i+4, 항목이 있던 칸은 2i+4 에 키('"키": ')와 값의 길이가
    늘어난다. 새로 여는 칸은 값 자리에서 빈 칸(2자)으로 센다. 뿌리(쪽 사전)에는 늘 되풀이 칸·쪽 안내가 있다."""

    def __init__(self):
        self.size = 0
        self.count: dict = {(): 1}

    def cost(self, at, vlen: int) -> tuple[int, dict]:
        add, new = 0, {}
        for i in range(len(at)):
            parent, child, last = at[:i], at[:i + 1], i == len(at) - 1
            if not last and (child in self.count or child in new):
                continue
            n = new.get(parent, self.count.get(parent, 0))
            key = at[i]
            keypart = 0 if isinstance(key, int) else _jlen(key) + 2
            add += (4 * i + 4 if n == 0 else 2 * i + 4) + keypart + (vlen if last else 2)
            new[parent] = n + 1
            if not last:
                new[child] = 0
        return add, new

    def take(self, add: int, new: dict) -> None:
        self.size += add
        self.count.update(new)


def _placeholder(v):
    """되풀이 칸의 자리값 — 숫자·null 은 9자리(실제 값 이상의 길이), 그 밖은 실제 값 그대로."""
    if v is None or isinstance(v, (bool, int, float)):
        return _NUM
    return v


def _list_parents(lists: dict, at) -> list:
    return [at[:i] for i in range(1, len(at)) if at[:i] in lists]


def _note(part: int, parts: int, ids: tuple) -> str:
    rep = f" 쪽마다 되풀이하는 칸({', '.join(ids)})은 한 번만 쓴다." if ids else ""
    return (f"응답이 쪽 크기 {PAGE_CHARS:,}자를 넘어 {parts}쪽으로 나눴다 — 지금 {part}쪽이다. 잘라 버린 것은 없다. 다음 쪽은 next 의 "
            "인자로 같은 도구를 다시 부른다(next 가 null 이면 마지막 쪽이다). pieces 는 이 쪽에 일부만 실린 칸이다 — 글은 from~to 자"
            "(of 는 전체 길이), 목록은 from~to 번 항목이다. 쪽 순서대로 이으면 원래 응답이 된다 — 글은 이어 쓰고, 목록 항목은 그 번호 "
            "자리에 넣되 앞 쪽과 번호가 겹치는 항목은 한 항목을 나눠 실은 것이니 합친다." + rep + " 심의가 도는 중이면 내용이 늘어 "
            "쪽 수가 바뀔 수 있다 — 끝난 뒤 받은 쪽이 확정본이다. 쪽 크기는 Claude Code 의 MCP 결과 상한(MAX_MCP_OUTPUT_TOKENS "
            "기본 25,000토큰)을 1자≈1토큰으로 잡은 값이다(설정 DELIB_MCP_PAGE_CHARS).")


def _cut(leaves: list, lists: dict, ids_ph: dict, again: dict, ids: tuple, budget: int) -> list:
    """쪽 경계를 끝까지 센다 — 쪽마다 [(잎 번호, 글 시작, 글 끝)] (글이 아닌 잎은 시작·끝이 None)."""
    env_cache: dict = {}

    def envelope(paths: tuple) -> int:
        """되풀이 칸 + 쪽 안내의 상한 길이 — pieces 는 그 쪽에 걸칠 수 있는 칸 전부(paths), 숫자는 9자리로 잡는다."""
        if paths not in env_cache:
            meta = {"part": _PART, "parts": _PART, "next": {**again, "part": _PART},
                    "pieces": [{"path": p, "from": _NUM, "to": _NUM, "of": _NUM} for p in paths],
                    "note": _note(_PART, _PART, ids)}
            env_cache[paths] = len(wire({**ids_ph, "page": meta}))
        return env_cache[paths]

    def bound(first, first_cont: bool, at, is_str: bool) -> int:
        """첫 잎이 first 이고 마지막 잎이 at 인 쪽의 되풀이 칸 + 쪽 안내 상한. 일부만 실린 목록은 처음·끝 잎의 조상이다."""
        paths = [_path(p) for p in _list_parents(lists, first)]
        paths += [s for s in (_path(p) for p in _list_parents(lists, at)) if s not in paths]
        if first_cont:
            paths.append(_path(first))
        if is_str and _path(at) not in paths:
            paths.append(_path(at))
        return envelope(tuple(paths))

    def forced(at) -> None:
        # 빈 쪽에도 안 들어간다 — 되풀이 칸이 쪽 크기를 거의 다 먹은 것이다. 일어나면 안 되는 일이라 로그를 남기고, 앞으로
        # 가야 하니 쪽을 넘겨서라도 싣는다(글이면 한 글자).
        log.warning("쪽 크기 %d 자에 잎 하나도 못 싣는다(%s) — 쪽을 넘겨 싣는다", budget, _path(at))

    whole = [None] * len(leaves)
    pages, i, off = [], 0, 0
    while i < len(leaves):
        lay, taken = _Layout(), []
        first, first_cont = leaves[i][0], off > 0
        while i < len(leaves):
            at, v = leaves[i]
            is_str = isinstance(v, str)
            room = budget - bound(first, first_cont, at, is_str) - lay.size     # 이 잎이 쓸 수 있는 자리
            if not is_str:                                     # 숫자·참거짓·null·빈 칸 — 작다, 나누지 않는다
                add, new = lay.cost(at, _jlen(v))
                if add > room and taken:
                    break
                if add > room:
                    forced(at)
                lay.take(add, new)
                taken.append((i, None, None))
                i += 1
                if add > room:
                    break
                continue
            # 글은 JSON 으로 적어도 글자 수만큼 길다 — 남은 글자가 자리보다 많으면 통째로 잴 것도 없다(긴 글을 쪽마다 끝까지
            # 다시 적으면 쪽 수만큼 느려진다).
            if len(v) - off <= room:
                if off:
                    vlen = _jlen(v[off:])
                else:
                    if whole[i] is None:
                        whole[i] = _jlen(v)
                    vlen = whole[i]
                add, new = lay.cost(at, vlen)
                if add <= room:
                    lay.take(add, new)
                    taken.append((i, off, len(v)))
                    i, off = i + 1, 0
                    continue
            # 들어가는 만큼 나눈다 — v[off:e] 가 들어가는 가장 큰 e(조각도 글자 수 이상이라 남은 자리 너머는 볼 것이 없다).
            lo, hi, best = off + 1, min(len(v) - 1, off + max(1, room)), off
            while lo <= hi:
                mid = (lo + hi) // 2
                if lay.cost(at, _jlen(v[off:mid]))[0] <= room:
                    best, lo = mid, mid + 1
                else:
                    hi = mid - 1
            if best > off and (not taken or best - off >= _MIN_SLICE):
                lay.take(*lay.cost(at, _jlen(v[off:best])))
                taken.append((i, off, best))
                off = best
                break
            if taken:
                break
            forced(at)
            best = off + 1
            lay.take(*lay.cost(at, _jlen(v[off:best])))
            taken.append((i, off, best))
            i, off = (i + 1, 0) if best >= len(v) else (i, best)
            break
        pages.append(taken)
    return pages


class _Seq(dict):
    """쪽에 실린 목록 항목 — 원래 번호 → 값. 다 모은 뒤 번호 순서의 목록으로 바꾼다."""


def _put(root: dict, at, val) -> None:
    node = root
    for i, key in enumerate(at[:-1]):
        if key not in node:
            node[key] = _Seq() if isinstance(at[i + 1], int) else {}
        node = node[key]
    node[at[-1]] = val


def _plain(v):
    if isinstance(v, _Seq):
        return [_plain(v[k]) for k in sorted(v)]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _render(leaves: list, lists: dict, taken: list) -> tuple[dict, list]:
    """쪽 하나의 칸과 pieces — 일부만 실린 글(from~to 자)과 일부만 실린 목록(from~to 번 항목)."""
    body: dict = {}
    pieces: list = []
    full, seen = set(), []
    for idx, frm, to in taken:
        at, v = leaves[idx]
        if isinstance(v, str):
            val = v[frm:to]
            if frm > 0 or to < len(v):
                pieces.append(((idx, len(at)), {"path": _path(at), "from": frm, "to": to, "of": len(v)}))
            else:
                full.add(idx)
        else:
            val = v
            full.add(idx)
        _put(body, at, val)
        for p in _list_parents(lists, at):
            if p not in seen:
                seen.append(p)
    for p in seen:
        start, end, count = lists[p]
        if all(j in full for j in range(start, end)):
            continue
        nums = sorted({leaves[idx][0][len(p)] for idx, _f, _t in taken if leaves[idx][0][:len(p)] == p})
        pieces.append(((start, len(p)), {"path": _path(p), "from": nums[0], "to": nums[-1] + 1, "of": count}))
    pieces.sort(key=lambda x: x[0])          # 문서 순서 — 같은 잎에서 시작하면 바깥 칸이 먼저다
    return _plain(body), [x for _k, x in pieces]


def echo_head(out: dict, key: str, where: str) -> dict:
    """되돌려 보이는 글 칸(key) 때문에 응답이 쪽 크기를 넘으면, 그 칸은 머리만 싣고 전체 길이(key_chars)와 전문을 받는 곳을 note 에 적는다.

    다시 부르면 일이 또 일어나는 도구(심의 시작·이어하기)의 응답용이다 — 쪽을 나눠 '다음 쪽' 으로 다시 부르게 할 수 없다.
    그 칸은 호출자가 보낸 글의 메아리이고, 받은 글은 전부 쓰였다 — 전문은 where 가 쪽으로 나눠 준다."""
    if len(wire(out)) <= PAGE_CHARS:
        return out
    full = out[key]

    def with_note(n: int) -> str:
        add = (f"{key} 가 {len(full):,}자라 응답이 쪽 크기({PAGE_CHARS:,}자)를 넘어, 여기에는 앞 {n:,}자만 되돌려 보인다 — "
               f"받은 글은 전부 그대로 쓰였다(잘린 것이 아니다). 전문은 {where} 로 받는다.")
        return f"{out['note']} {add}" if out.get("note") else add

    base = {**out, key: "", f"{key}_chars": len(full), "note": with_note(len(full))}   # 머리 길이 숫자의 자리를 가장 길게 잡는다
    room = PAGE_CHARS - len(wire(base))
    lo, hi, n = 1, min(len(full), max(0, room)), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _jlen(full[:mid]) - 2 <= room:
            n, lo = mid, mid + 1
        else:
            hi = mid - 1
    return {**base, key: full[:n], "note": with_note(n)}


def paged(out: dict, *, part: int = 1, again: dict, ids: tuple = (), tail: tuple = ()) -> dict:
    """out 이 쪽 크기(PAGE_CHARS) 안이면 그대로 돌려준다(part 1). 넘으면 part 번째 쪽이다.

    again — 다음 쪽을 부를 인자(page.next 는 여기에 part 만 바꿔 싣는다). ids — 쪽마다 되풀이하는 칸(작은 것만 — 잡 id 처럼
    어느 쪽을 받든 있어야 하는 것). tail — 쪽을 나눌 때 뒤로 보내는 칸(크고 덜 급한 것 — 진행 조회의 화두처럼, 첫 쪽에 상태가
    다 실리게 한다)."""
    try:
        part = int(part or 1)
    except (TypeError, ValueError):
        raise ValueError(f"part 는 1 이상의 정수다 — 받은 값: {part!r}") from None
    if part < 1:
        raise ValueError(f"part 는 1 이상의 정수다 — 받은 값: {part}")
    if len(wire(out)) <= PAGE_CHARS:
        if part != 1:
            raise ValueError(f"part={part} 는 없다 — 이 응답은 쪽 크기({PAGE_CHARS:,}자) 안이라 한 쪽이다.")
        return out
    idv = {k: out[k] for k in ids if k in out}
    order = [k for k in out if k not in idv and k not in tail] + [k for k in tail if k in out and k not in idv]
    body = {k: out[k] for k in order}
    leaves, lists = _flatten(body)
    specs = _cut(leaves, lists, {k: _placeholder(v) for k, v in idv.items()}, again, tuple(idv), PAGE_CHARS)
    parts = len(specs)
    if part > parts:
        raise ValueError(f"part={part} 는 없다 — 이 응답은 {parts}쪽이다(1~{parts}).")
    content, pieces = _render(leaves, lists, specs[part - 1])
    return {**idv, **content,
            "page": {"part": part, "parts": parts, "next": {**again, "part": part + 1} if part < parts else None,
                     "pieces": pieces, "note": _note(part, parts, tuple(idv))}}

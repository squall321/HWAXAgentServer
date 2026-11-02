# 심의 주제의 꼴 · 차수 정규화 · SP 과제 key 다시 확인(서버가 부른 풀이의 결과 행에서만)을 본다 — 과제 인사이트 그래프 M1
"""HWAXPortal docs/project-insight-graph PLAN §4.3 · §4.6 · 사용자 결정 11-02(PG-15 · PG-17).

  · 차수는 NFKC · 공백 빼기 · 소문자만 하고 글자 그대로 견준다 — '2차' · 'C2' · '양산' 은 차수가 아니다(받은 글은 text 에 남는다).
  · SP 과제 key 는 부르는 쪽이 준 값이 근거가 아니다 — 서버가 부른 풀이(StepForge find_plm_task)의 결과 행 가운데 key 가 글자 그대로 같은 행이
    하나일 때만 확인이다. 질의를 되실은 칸 · 부분 일치 · 과제코드로 걸린 행 · 같은 key 둘은 확인이 아니다.
  · 풀이의 가짜는 **실제 StepForge 응답**이다(tests/fixtures/subject/find_plm_task.json — 그 리포의 find_tasks 를 가짜 허브 거울에 대고 돌려
    받아 적었다). 마지막 묶음은 진짜 MCP 서버(같은 프로세스의 FastMCP)를 띄워 게이트웨이 자리에 두고 부르는 길 · 자격 머리까지 본다.

  실행:  .venv/bin/python -m pytest tests/test_delib_subject.py -q
"""
import asyncio
import copy
import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import delib_subject as s  # noqa: E402

_FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "subject" / "find_plm_task.json").read_text(encoding="utf-8"))
KEY = "알파 개발_C2"          # 운영 허브 꼴 — key 가 과제명이다(과제코드 TC-1001 은 properties.task_code)


def _text(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, indent=2)        # FastMCP 가 dict 를 글로 옮기는 꼴(들여쓰기는 판정과 무관)


def _fake(monkeypatch, *, body=None, err=False, text=None, exc=None, delay=0.0):
    """풀이 호출을 바꿔 끼운다 — 받은 인자 · 자격을 적는다."""
    seen = []

    async def call(_app, *, groups, user, user_pat, args):
        seen.append({"groups": groups, "user": user, "user_pat": user_pat, "args": args})
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        return err, (text if text is not None else _text(body))
    monkeypatch.setattr(s, "call_resolver", call)
    return seen


def _confirm(raw, **kw):
    return asyncio.run(s.confirm(object(), s.parse(raw), groups=kw.get("groups", ["CAEG"]), user=kw.get("user", "me@example.com"),
                                 user_pat=kw.get("user_pat", "")))


# ── 차수 ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ("dv1", "dv1"), ("DV1", "dv1"), (" Dv 2 ", "dv2"), ("ＤＶ１", "dv1"), ("ｐｖｒ", "pvr"), ("PRE", "pre"), ("mp", "mp"),
    ("M P", "mp"), ("pra", "pra"), ("dvr", "dvr"), ("pv3", "pv3"), ("　dv3　", "dv3"),
])
def test_차수는_전각_공백_대소문자만_맞추고_코드로_읽는다(raw, want):
    assert s.norm_round(raw) == want


@pytest.mark.parametrize("raw", ["2차", "C2", "c2", "양산", "선행", "DV", "dv", "pre1", "pv4", "dv-1", "dv_1", "dv0", "mp1",
                                 "", "  ", "dv1차", "PV 1차"])
def test_꼴에_안_맞으면_차수가_아니다(raw):
    """하이픈을 빼지 않고 '2차' · 'C2' · '양산' 을 dv2 · mp 로 읽지 않는다 — 원천이 그 대응을 적지 않았다(PLAN §4.6)."""
    assert s.norm_round(raw) is None
    assert s.norm_round(None) is None and s.norm_round(1) is None


def test_꼴에_안_맞는_차수_글은_차수_모름이고_text_에_남는다(monkeypatch):
    _fake(monkeypatch, body=_FIX["exact_and_partial"]["body"])
    rec = _confirm({"sp_task": KEY, "round": "2차", "text": "알파 낙하"})
    assert rec["round"] is None and rec["round_by"] is None
    assert rec["text"] == "알파 낙하 · 2차", "받은 차수 글을 버렸다"
    only = _confirm({"sp_task": KEY, "round": "C2"})
    assert only["round"] is None and only["text"] == "C2"
    same = _confirm({"sp_task": KEY, "round": "양산", "text": "양산 전 점검"})
    assert same["text"] == "양산 전 점검", "글에 이미 있는 차수 글은 다시 붙이지 않는다"
    ok = _confirm({"sp_task": KEY, "round": " ＤＶ２ "})
    assert ok["round"] == "dv2" and ok["round_by"] == s.BY_PERSON and ok["text"] == ""


# ── 꼴 ─────────────────────────────────────────────────────────────────────────────────────
def test_비었거나_없으면_주제가_없다():
    assert s.parse(None) is None
    assert s.parse({}) is None
    assert s.parse({"sp_task": "  ", "round": "", "text": " ", "artifacts": []}) is None


@pytest.mark.parametrize("raw,word", [
    ({"sp_task": KEY, "sp_state": "confirmed"}, "모르는 칸"),
    ({"sp_task": KEY, "sp_resolved_by": {"tool": "x"}}, "모르는 칸"),
    ({"round_by": "person", "round": "dv1"}, "모르는 칸"),
    ({"project": KEY}, "모르는 칸"),
    ("알파", "객체다"),
    ({"sp_task": 12}, "글이다"),
    ({"sp_task": "가" * 201}, "200자"),
    ({"text": "가" * 501}, "500자"),
    ({"round": "d" * 41}, "40자"),
    ({"artifacts": {"kind": "mcad"}}, "목록이다"),
    ({"artifacts": [{"kind": "mcad", "id": "0" * 23}] * 9}, "8건"),
    ({"artifacts": [{"kind": "pcb", "id": "0" * 16}]}, "kind"),
    ({"artifacts": [{"kind": "mcad", "id": "0" * 22}]}, "16진 23자"),
    ({"artifacts": [{"kind": "ecad", "id": "g" * 16}]}, "16진 16자"),
    ({"artifacts": [{"kind": "cae", "id": "I" * 26}]}, "ULID"),
    ({"artifacts": [{"kind": "mcad", "id": "0" * 23, "owner": "x"}]}, "모르는 칸"),
    ({"artifacts": [{"kind": "mcad", "id": "0" * 23, "app": "Step Forge!"}]}, "앱 키"),
    ({"artifacts": ["0" * 23]}, "객체다"),
])
def test_꼴이_틀리면_무엇이_틀렸는지_말하고_거절한다(raw, word):
    with pytest.raises(s.SubjectError, match=word):
        s.parse(raw)


def test_설계_자료_열쇠는_원천의_꼴로_맞추고_겹치면_하나다():
    got = s.parse({"artifacts": [
        {"kind": "mcad", "id": "019A2B3C4D5E6F7A8B9C0D1E2"[:23].upper(), "app": "HEAX-Step_Forge", "rev": " 1a2b3c4d5e6f7a8b "},
        {"kind": "mcad", "id": "019a2b3c4d5e6f7a8b9c0d1e2"[:23]},
        {"kind": "ecad", "id": "ABCDEF0123456789"},
        {"kind": "cae", "id": "01hzx3k5q9v8n7m6p5r4s3t2w1"}]})
    arts = got["artifacts"]
    assert arts[0] == {"kind": "mcad", "id": "019a2b3c4d5e6f7a8b9c0d1", "app": "heax-step_forge", "rev": "1a2b3c4d5e6f7a8b"}
    assert len(arts) == 3, "같은 (kind, id) 는 하나다"
    assert arts[1] == {"kind": "ecad", "id": "abcdef0123456789"} and arts[2] == {"kind": "cae", "id": "01HZX3K5Q9V8N7M6P5R4S3T2W1"}


# ── 서버가 부른 풀이의 결과 행에서만 ─────────────────────────────────────────────────────────
def test_결과_행에_key_가_글자_그대로_같은_행이_하나면_확인이다(monkeypatch):
    body = _FIX["exact_and_partial"]["body"]
    seen = _fake(monkeypatch, body=body)
    rec = _confirm({"sp_task": KEY, "round": "dv1", "text": "알파 낙하"}, user="me@example.com", groups=["CAEG"])
    assert rec["sp_state"] == s.CONFIRMED and rec["sp_task"] == KEY and "sp_why" not in rec
    by = rec["sp_resolved_by"]
    assert by["tool"] == s.RESOLVER_CALL == "heaxstep_forge_find_plm_task" and by["app"] == "heax-step_forge"
    assert by["sha256"] == hashlib.sha256(_text(body).encode("utf-8")).hexdigest() and by["at"] > 0
    assert seen == [{"groups": ["CAEG"], "user": "me@example.com", "user_pat": "",
                     "args": {"query": KEY, "limit": s.RESOLVE_LIMIT, "full": True}}], "그 심의의 자격으로 key 그대로 한 번 부른다"
    assert len(body["tasks"]) == 2 and body["tasks"][1]["key"].startswith(KEY), "가짜가 부분 일치 행을 함께 낸다(실제 응답 그대로)"


def test_질의를_되실은_결과는_확인이_아니다(monkeypatch):
    """find_plm_task 는 질의를 결과 머리(query)에 되싣는다 — 그 칸을 '결과에 있다' 로 읽으면 무엇이든 확인된다(PLAN §4.3)."""
    body = _FIX["no_match"]["body"]
    assert body["query"] == "없는 과제" and body["tasks"] == []
    _fake(monkeypatch, body=body)
    rec = _confirm({"sp_task": "없는 과제"})
    assert rec["sp_state"] == s.CLAIMED and rec["sp_task"] == "없는 과제" and rec["sp_resolved_by"] is None
    assert "0행" in rec["sp_why"] and "no_match" in rec["sp_why"]


def test_부분_일치만_걸린_행은_확인이_아니다(monkeypatch):
    _fake(monkeypatch, body=_FIX["exact_and_partial"]["body"])       # 두 행 다 '알파 개발' 을 품지만 key 는 다르다
    rec = _confirm({"sp_task": "알파 개발"})
    assert rec["sp_state"] == s.CLAIMED and "글자 그대로 같은 행이 없다" in rec["sp_why"]
    assert "「알파 개발_C2」" in rec["sp_why"], "결과 행의 key 를 알려 그 key 로 다시 달게 한다"


def test_과제코드로_걸린_행은_key_가_달라_확인이_아니다(monkeypatch):
    """운영 허브의 key 는 과제명이다 — 과제코드(task_code 칸)로 찾은 행의 key 는 질의와 다르다. 과제코드를 key 로 적지 않는다."""
    body = _FIX["task_code"]["body"]
    assert body["tasks"][0]["matched_by"] == ["task_code"] and body["tasks"][0]["key"] == KEY
    _fake(monkeypatch, body=body)
    rec = _confirm({"sp_task": "TC-1001"})
    assert rec["sp_state"] == s.CLAIMED and rec["sp_task"] == "TC-1001" and f"「{KEY}」" in rec["sp_why"]


def test_같은_key_가_둘이면_고르지_않는다(monkeypatch):
    """실제 거울에서는 나지 않는 꼴(거울의 key 는 하나다) — 앞 사례의 확인 행을 둘로 지었다. 그래도 하나를 고르지 않는다."""
    body = copy.deepcopy(_FIX["exact_and_partial"]["body"])
    body["tasks"].insert(1, copy.deepcopy(body["tasks"][0]))
    _fake(monkeypatch, body=body)
    rec = _confirm({"sp_task": KEY})
    assert rec["sp_state"] == s.CLAIMED and "2개" in rec["sp_why"]


def test_허브_설정이_없는_박스는_주장으로_두고_까닭을_단다(monkeypatch):
    _fake(monkeypatch, body=_FIX["not_configured"]["body"])
    rec = _confirm({"sp_task": KEY, "round": "dv1"})
    assert rec["sp_state"] == s.CLAIMED and "not_configured" in rec["sp_why"] and rec["round"] == "dv1"


@pytest.mark.parametrize("kw,word", [
    ({"err": True, "text": "forbidden: heaxstep_forge_find_plm_task — 이 플랫폼 허가가 없다"}, "오류로 답했다"),
    ({"err": True, "text": "unknown tool: heaxstep_forge_find_plm_task"}, "오류로 답했다"),
    ({"text": "<html>bad gateway</html>"}, "JSON 으로 읽지 못했다"),
    ({"exc": ConnectionError("게이트웨이가 없다")}, "부르지 못했다(ConnectionError)"),
])
def test_풀이를_못_쓰면_주장이다(monkeypatch, kw, word):
    _fake(monkeypatch, **kw)
    rec = _confirm({"sp_task": KEY})
    assert rec["sp_state"] == s.CLAIMED and word in rec["sp_why"], rec


def test_기한을_넘기면_주장이다(monkeypatch):
    monkeypatch.setattr(s, "RESOLVE_TIMEOUT_S", 0.05)
    _fake(monkeypatch, body=_FIX["exact_and_partial"]["body"], delay=1.0)
    t0 = time.monotonic()
    rec = _confirm({"sp_task": KEY})
    assert rec["sp_state"] == s.CLAIMED and "안에 답하지 않았다" in rec["sp_why"] and time.monotonic() - t0 < 0.9


def test_과제가_없으면_부르지_않고_없음이다(monkeypatch):
    seen = _fake(monkeypatch, body=_FIX["exact_and_partial"]["body"])
    rec = _confirm({"text": "알파 낙하", "round": "pv1", "artifacts": [{"kind": "ecad", "id": "abcdef0123456789", "rev": "B"}]})
    assert seen == [] and rec == {"sp_task": None, "sp_state": s.NONE, "sp_resolved_by": None, "round": "pv1", "round_by": s.BY_PERSON,
                                  "text": "알파 낙하", "artifacts": [{"kind": "ecad", "id": "abcdef0123456789", "rev": "B"}]}


def test_리스크_심사_계열을_가린다():
    assert s.risk_family("risk-review") and s.risk_family(" Risk-Review ") and s.risk_family("risk-review-sealed")
    assert s.risk_family(None, "app") and s.risk_family("default", " APP ")
    assert not s.risk_family(None) and not s.risk_family("diagnosis", "human") and not s.risk_family("", "")


# ── 목록 거름 ────────────────────────────────────────────────────────────────────────────────
def test_목록_거름의_꼴():
    assert s.parse_filter({"sp_task": f" {KEY} "}) == {"sp_task": KEY, "round": None}
    assert s.parse_filter({"sp_task": KEY, "round": "ＤＶ１"}) == {"sp_task": KEY, "round": "dv1"}
    assert s.parse_filter({"sp_task": KEY, "round": "unknown"})["round"] == s.ROUND_UNKNOWN
    assert s.parse_filter({"sp_task": KEY, "round": "모름"})["round"] == s.ROUND_UNKNOWN
    assert s.parse_filter({"sp_task": KEY, "round": None})["round"] is None
    for bad, word in (({"sp_task": ""}, "있어야"), ({"round": "dv1"}, "있어야"), ({"sp_task": KEY, "round": "2차"}, "차수가 아니다"),
                      ({"sp_task": KEY, "text": "x"}, "모르는 칸"), ("알파", "객체다")):
        with pytest.raises(s.SubjectError, match=word):
            s.parse_filter(bad)


def test_목록_거름은_확인된_주제만_정확히():
    def job(state=s.CONFIRMED, task=KEY, rnd="dv1", chair=None):
        return {"chair_template": chair, "subject": {"sp_task": task, "sp_state": state, "round": rnd}}
    f_all, f_dv1, f_unk = ({"sp_task": KEY, "round": r} for r in (None, "dv1", s.ROUND_UNKNOWN))
    assert s.matches(job(), f_all) and s.matches(job(), f_dv1) and not s.matches(job(), f_unk)
    assert s.matches(job(rnd=None), f_all) and s.matches(job(rnd=None), f_unk) and not s.matches(job(rnd=None), f_dv1), \
        "차수 모름은 차수 모름 칸에만 — 어느 차수에도 섞지 않는다"
    assert not s.matches(job(rnd="dv2"), f_dv1)
    assert not s.matches(job(state=s.CLAIMED), f_all) and not s.matches(job(state=s.NONE, task=None), f_all), "확인된 주제만"
    assert not s.matches(job(task="알파 개발_C2 보강"), f_all) and not s.matches(job(task=KEY.lower()), f_all), "key 는 글자 그대로"
    assert not s.matches({"subject": None}, f_all) and not s.matches({}, f_all), "주제가 없으면 아무것도 잇지 않는다"
    assert not s.matches(job(chair="risk-review"), f_all), "리스크 심사 계열은 맞대지 않는다"


# ── 진짜 MCP 길 — 같은 프로세스의 FastMCP 를 게이트웨이 자리에 ───────────────────────────────────
class _Gw:
    def __init__(self):
        self.calls, self.body = [], _FIX["exact_and_partial"]["body"]


@pytest.fixture(scope="module")
def gw():
    """게이트웨이 대역 — 호출 전용 별칭 이름의 풀이 도구 하나(실제 StepForge 응답을 낸다)와 그 호출의 자격 머리를 적는다."""
    state = _Gw()
    mcp = FastMCP("subject-gw", stateless_http=True, streamable_http_path="/")
    hdrs: dict = {}

    @mcp.tool(name=s.RESOLVER_CALL)
    async def resolver(query: str, limit: int = 20, full: bool = False) -> str:
        state.calls.append({"query": query, "limit": limit, "full": full, **hdrs})
        if query == "느린 과제":
            await asyncio.sleep(3)        # 기한(RESOLVE_TIMEOUT_S)을 넘기는 풀이 — 진짜 MCP 세션이 기한에 끊겨도 예외가 새지 않아야 한다
        return _text(state.body)

    inner = mcp.streamable_http_app()

    async def gate(scope, receive, send):
        if scope["type"] == "http":
            h = {k.decode(): v.decode() for k, v in scope["headers"]}
            hdrs.clear()
            hdrs.update({k: h.get(k, "") for k in ("authorization", "x-hwax-user", "x-hwax-groups")})
        await inner(scope, receive, send)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(gate, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "시험용 MCP 서버가 뜨지 않았다"
    state.app = SimpleNamespace(state=SimpleNamespace(connections={
        "gateway": {"url": f"http://127.0.0.1:{port}/", "transport": "streamable_http", "headers": {"Authorization": "Bearer svc-token"}}}))
    yield state
    server.should_exit = True
    th.join(timeout=10)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)


def test_진짜_MCP_길로_부르고_웹은_그_사람의_PAT_MCP_잡은_공유_비밀과_신원_머리다(gw):
    gw.calls.clear()
    web = asyncio.run(s.confirm(gw.app, s.parse({"sp_task": KEY, "round": "dv1"}), groups=["CAEG", "연구소"], user="Me@Example.com",
                                user_pat="user-pat"))
    mcp = asyncio.run(s.confirm(gw.app, s.parse({"sp_task": KEY}), groups=["CAEG"], user="me@example.com"))
    assert web["sp_state"] == mcp["sp_state"] == s.CONFIRMED and web["round"] == "dv1"
    a, b = gw.calls
    assert (a["query"], a["limit"], a["full"]) == (KEY, s.RESOLVE_LIMIT, True)
    assert a["authorization"] == "Bearer user-pat" and a["x-hwax-user"] == "me@example.com", "웹 — 그 사람의 PAT 와 신원"
    assert a["x-hwax-groups"] == "CAEG,%EC%97%B0%EA%B5%AC%EC%86%8C", "그룹은 엔진의 도구 로드와 같은 꼴(퍼센트 인코딩)"
    assert b["authorization"] == "Bearer svc-token" and b["x-hwax-user"] == "me@example.com", "MCP 잡 — 공유 비밀 + 신원 머리(엔진과 같다)"


def test_진짜_MCP_길에서도_부분_일치는_주장이다(gw):
    rec = asyncio.run(s.confirm(gw.app, s.parse({"sp_task": "알파"}), groups=[], user="me@example.com"))
    assert rec["sp_state"] == s.CLAIMED and "글자 그대로 같은 행이 없다" in rec["sp_why"]


def test_진짜_MCP_길에서_기한을_넘기면_세션을_끊고_주장이다(gw, monkeypatch):
    monkeypatch.setattr(s, "RESOLVE_TIMEOUT_S", 0.5)
    t0 = time.monotonic()
    rec = asyncio.run(s.confirm(gw.app, s.parse({"sp_task": "느린 과제"}), groups=[], user="me@example.com"))
    assert rec["sp_state"] == s.CLAIMED and "안에 답하지 않았다" in rec["sp_why"] and time.monotonic() - t0 < 2.5, rec
    again = asyncio.run(s.confirm(gw.app, s.parse({"sp_task": KEY}), groups=[], user="me@example.com"))
    assert again["sp_state"] == s.CONFIRMED, "끊긴 뒤에도 다음 풀이는 그대로 선다"


def test_게이트웨이에_풀이_도구가_없으면_주장이다(gw, monkeypatch):
    monkeypatch.setattr(s, "RESOLVER_CALL", "heaxstep_forge_no_such_tool")
    rec = asyncio.run(s.confirm(gw.app, s.parse({"sp_task": KEY}), groups=[], user="me@example.com"))
    assert rec["sp_state"] == s.CLAIMED and "오류로 답했다" in rec["sp_why"], rec


def test_연결_설정이_없으면_주장이다():
    rec = asyncio.run(s.confirm(SimpleNamespace(state=SimpleNamespace(connections={})), s.parse({"sp_task": KEY}), groups=[], user=""))
    assert rec["sp_state"] == s.CLAIMED and "RuntimeError" in rec["sp_why"]


def test_한_줄_안내():
    assert "확인" in s.say({"sp_state": s.CONFIRMED, "sp_task": KEY, "round": "dv1"}) and "차수 dv1" in s.say(
        {"sp_state": s.CONFIRMED, "sp_task": KEY, "round": "dv1"})
    assert "잇지 않는다" in s.say({"sp_state": s.CLAIMED, "sp_task": KEY, "round": None, "sp_why": "x"})
    assert "과제 없음" in s.say({"sp_state": s.NONE, "sp_task": None, "round": None})

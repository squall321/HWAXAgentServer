# 운영 요청 모음 11-24(HWAXPortal docs/request-1125 1-3 · 2절 A-1) — 위임 · 초안의 고침 OF-1 ~ OF-5 시험(가짜는 test_offload 의 것을 쓴다)
#
#   실행:  .venv/bin/python -m pytest tests/test_offload_r1125.py -q
import asyncio
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server as m  # noqa: E402

from test_offload import _ai, _call, _ctx, _done, _run, kit  # noqa: E402, F401


# ── OF-5 위임 상태의 기본 기다림 ────────────────────────────────────────────────────────────────────────────────
def test_OF5_위임_상태는_기본으로_기다리고_설명에_줄이_길면_직접_읽으라고_적는다():
    sig = inspect.signature(m.delegate_status)
    assert sig.parameters["wait_s"].default == m.DELEGATE_WAIT_DEFAULT_S == 240.0, "기본 0 이면 첫 호출부터 기다리지 않는다(OF-5)"
    assert m.DELEGATE_WAIT_DEFAULT_S < 600, "게이트웨이 호출 한도 600초 안쪽이어야 한다"
    for desc in (m._DG_STATUS_DESC, m._DG_START_DESC):
        assert "직접 읽는다" in desc and "delegate_cancel" in desc


def test_OF5_기본값은_서버_상한까지만_말없이_죄고_준_값은_그대로_간다(kit, monkeypatch):
    asked: list = []

    async def _sw(job_id, job, wait_s):
        asked.append(wait_s)
        return job, {"wait_s": wait_s, "waited_s": 0.0, "woke": "ended"}
    monkeypatch.setattr(m, "_status_wait", _sw)
    monkeypatch.setattr(m, "STATUS_WAIT_MAX_S", 90.0)
    kit.llm.script = [_ai("끝났다.")]

    async def scenario():
        out = await m.delegate_start(task="S26 VOC 불량 수를 알아내라", ctx=_ctx())
        await _done(out["job_id"])
        a = await m.delegate_status(out["job_id"], ctx=_ctx())
        b = await m.delegate_status(out["job_id"], wait_s=30, ctx=_ctx())
        c = await m.delegate_status(out["job_id"], wait_s=0, ctx=_ctx())
        return out, a, b, c
    out, a, b, c = _run(scenario)
    assert asked == [90.0, 30], "기본값은 상한 90 으로, 준 30 은 그대로, 0 은 기다리지 않는다"
    assert a["wait"]["wait_s"] == 90.0 and "wait" not in c
    assert "기본으로 최대 240초" in out["note"]


# ── OF-4 초안 권고는 긴 절에만 ─────────────────────────────────────────────────────────────────────────────────
def test_OF4_초안_설명은_긴_절에만_쓰라고_첫머리에_적는다():
    head = m._DR_DESC[:160]
    assert "긴 절" in head and "직접 쓴다" in head and f"{m.offload.DRAFT_TARGET_DEFAULT:,}자" in head
    assert "통째로 다시 쓰지 말고" in m._DR_DESC


# ── OF-2 멈춘 까닭 · 덜 찾음 ─────────────────────────────────────────────────────────────────────────────────────
from test_offload import MAP, RR, TOOLS, _tool  # noqa: E402

import offload  # noqa: E402


def _kit_tools(kit, extra: dict):
    tools = {**TOOLS, **extra}

    async def _pt(_app, groups, user):
        return dict(tools)

    async def _rm(_app):
        return {**MAP, **{n: "signalforge" for n in extra}}, frozenset(RR | set(extra))
    kit.mp.setattr(offload, "_person_tools", _pt)
    kit.mp.setattr(offload, "_read_map", _rm)


def test_OF2_답에_멈춘_까닭_예산_incomplete_못_채운_것이_구조로_실린다(kit):
    kit.llm.script = [_ai("", _call("voc_search", q="1")), _ai("불량 1,234건 [e1].\n[못 채운 것] 없음")]

    async def scenario():
        out = await m.delegate_start(task="S26 불량 수", ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    assert res["incomplete"] is False and "unfilled" not in res
    st = res["stop"]
    assert st["reason"] == "answered" and st["calls"] == 1 and st["steps_budget"] == offload.STEPS_DEFAULT
    assert st["minutes_budget"] == offload.MINUTES_DEFAULT and st["elapsed_s"] >= 0


def test_OF2_호출_예산으로_멈추면_incomplete_와_못_채운_항목(kit):
    kit.llm.script = [_ai("", _call("voc_search", q="1")), _ai("", _call("voc_search", q="2")),
                      _ai("불량 1,234건 [e1].\n[못 채운 것] 원인 분류; 대책 이력")]

    async def scenario():
        out = await m.delegate_start(task="부품 × 증상 표를 채워라", max_steps=1, ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], view="summary", ctx=_ctx())
    res = _run(scenario)
    assert res["stop"]["reason"] == "steps" and res["stop"]["calls"] == 1 and res["incomplete"] is True
    assert res["unfilled"] == ["원인 분류", "대책 이력"]


def test_OF2_시간으로_멈추고_못_채운_것을_적지_않았으면_그렇다고_적는다(kit):
    gate = asyncio.Event()

    async def _stall(_msgs):
        await gate.wait()
        return _ai("오지 않는다")
    kit.mp.setattr(offload, "_MINUTE_S", 0.3)
    kit.llm.script = [_ai("", _call("voc_search", q="S26")), _stall, _ai("정리 — 불량 1,234건 [e1].")]

    async def scenario():
        out = await m.delegate_start(task="S26 불량", max_minutes=1, ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    assert res["stop"]["reason"] == "time" and res["incomplete"] is True
    assert len(res["unfilled"]) == 1 and "적지 않았다" in res["unfilled"][0]
    wrap = kit.llm.seen[-1]
    assert "[못 채운 것]" in wrap[0].content, "정리 턴도 못 채운 것을 적게 한다"


def test_OF2_적중이_적으면_다시_찾으라는_힌트를_LLM_에만_단다(kit):
    _kit_tools(kit, {"voc_find": _tool("voc_find", '{"total": 0, "results": []}')})
    kit.llm.script = [_ai("", _call("voc_find", q="S26 화면 깨짐")), _ai("없다.\n[못 채운 것] 화면 깨짐 건수")]

    async def scenario():
        out = await m.delegate_start(task="S26 화면 깨짐 건수", ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    seen_tool = [x for x in kit.llm.seen[-1] if getattr(x, "type", "") == "tool"]
    assert seen_tool and "[실행기 힌트] 적중이 0~1건" in str(seen_tool[0].content)
    assert res["few_hits"] == 1 and res["evidence"][0]["chars"] == len('{"total": 0, "results": []}'), "근거 표의 글자 수는 힌트 앞 원문"
    assert offload._few_hits("[]") and offload._few_hits('{"count": 1}') and not offload._few_hits('{"count": 5}')
    assert not offload._few_hits("결과 없음") and not offload._few_hits('{"results": [1, 2]}')


def test_OF2_설명에_일_크기별_권장과_incomplete_를_적는다():
    for d in (m._DG_START_DESC, m._A_DG_STEPS):
        assert "20~30" in d
    assert "incomplete" in m._DG_START_DESC and "unfilled" in m._DG_RESULT_DESC and "stop{reason" in m._DG_RESULT_DESC
    assert "다시 찾는다" in offload._DELEGATE_SYS and "[못 채운 것]" in offload._DELEGATE_SYS


# ── 2절 A-1 앞선 결과 줄여 싣기 ────────────────────────────────────────────────────────────────────────────────
def test_A1_앞선_결과는_LLM_입력에서만_줄이고_방금_받은_결과는_그대로(kit):
    long_a, long_b = "가" * 4000, "나" * 4000
    _kit_tools(kit, {"rep_a": _tool("rep_a", long_a), "rep_b": _tool("rep_b", long_b)})
    kit.mp.setattr(offload, "PRIOR_RESULT_CHARS", 1000)
    kit.llm.script = [_ai("", _call("rep_a", q="1")), _ai("", _call("rep_b", q="2")), _ai("끝.\n[못 채운 것] 없음")]

    async def scenario():
        out = await m.delegate_start(task="두 보고서를 읽어라", ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    last = [x for x in kit.llm.seen[-1] if getattr(x, "type", "") == "tool"]
    assert len(last) == 2
    assert last[0].content.startswith("[근거 e1] " + "가" * 900) and long_a not in last[0].content and "앞선 결과" in last[0].content
    assert len(last[0].content) < 1200
    assert long_b in last[1].content, "방금 받은 결과는 줄이지 않는다"
    second = [x for x in kit.llm.seen[1] if getattr(x, "type", "") == "tool"]
    assert long_a in second[0].content, "그 결과를 받은 바로 다음 차례에는 전문이 보인다"
    assert [e["chars"] for e in res["evidence"]] == [4000, 4000], "근거 표 · 숫자 대조는 원문 그대로"


def test_A1_0_이면_종전처럼_줄이지_않는다(kit):
    _kit_tools(kit, {"rep_a": _tool("rep_a", "가" * 4000)})
    kit.mp.setattr(offload, "PRIOR_RESULT_CHARS", 0)
    kit.llm.script = [_ai("", _call("rep_a", q="1")), _ai("", _call("voc_search", q="2")), _ai("끝.")]

    async def scenario():
        out = await m.delegate_start(task="읽어라", ctx=_ctx())
        await _done(out["job_id"])
    _run(scenario)
    last = [x for x in kit.llm.seen[-1] if getattr(x, "type", "") == "tool"]
    assert "가" * 4000 in last[0].content


# ── OF-1 첨부 본문 읽기 · 울타리 밖 도구 ──────────────────────────────────────────────────────────────────────────
import base64  # noqa: E402
import json  # noqa: E402

_HTML = ("<html><head><style>.x{color:red}</style><script>var a=1;</script></head><body><h1>낙하 시험 보고</h1>"
         "<p>시료 S26 은 1.5 m 낙하에서 화면 깨짐.</p><table><tr><td>원인</td><td>모서리 응력 312.5 MPa</td></tr></table>"
         "<p>대책 — 모서리 보강 리브 추가.</p></body></html>")


def _dl_json(text: str, mime="text/html", name="report.html") -> str:
    return json.dumps({"file_id": "f1", "filename": name, "mime_type": mime, "size": len(text.encode()), "encoding": "base64",
                       "data_base64": base64.b64encode(text.encode()).decode()})


def test_OF1_첨부_글은_HTML_을_걷고_바이너리는_읽지_않는다():
    text, why = offload.attachment_text(_dl_json(_HTML))
    assert why == "" and "낙하 시험 보고" in text and "모서리 응력 312.5 MPa" in text and "color:red" not in text and "var a" not in text
    assert offload.attachment_text(_dl_json("PNG", mime="image/png", name="a.png"))[1].startswith("글이 아닌 첨부다")
    assert "받지 못했다" in offload.attachment_text(json.dumps({"error": "권한 없음"}))[1]
    assert "JSON 이 아니다" in offload.attachment_text("잘린 글 {")[1]


def test_OF1_첨부는_쪽과_찾기로_읽는다():
    body = "\n".join(f"줄 {i} 내용" for i in range(1, 51)) + "\n원인은 모서리 응력\n끝"
    page = offload._attach_page(body, "f1", 0, 30, "")
    assert page.startswith(f"[첨부 f1 — 전체 {len(body):,}자 · 0~30 · 다음 offset=30]")
    hit = offload._attach_page(body, "f1", 0, 6000, "모서리")
    assert "든 줄 1곳" in hit and "원인은 모서리 응력" in hit and "줄 50 내용" in hit and "줄 10 내용" not in hit
    assert "든 줄이 없다" in offload._attach_page(body, "f1", 0, 6000, "없는낱말")


def test_OF1_첨부_받기가_울타리_안이면_그대로_묶지_않고_첨부_읽기를_묶는다(kit):
    _kit_tools(kit, {"download_file": _tool("download_file", "이건 부르면 안 된다"),
                     "get_report": _tool("get_report", '{"blocks": [{"type": "file", "file_id": "f1"}]}')})
    got = []

    async def _dt(_app, groups, user, name):
        got.append((name, user))
        return _tool("download_file", _dl_json(_HTML))
    kit.mp.setattr(offload, "_download_tool", _dt)
    kit.llm.script = [_ai("", _call("get_report", q="R-1")), _ai("", _call("read_attachment", file_id="f1", query="원인")),
                      _ai("", _call("read_attachment", file_id="f1", offset=0, limit=500)),
                      _ai("원인은 모서리 응력 312.5 MPa [e2].\n[못 채운 것] 없음")]

    async def scenario():
        out = await m.delegate_start(task="보고서 R-1 첨부의 원인", ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    assert "read_attachment" in kit.llm.bound[0] and "download_file" not in kit.llm.bound[0]
    assert [e["tool"] for e in res["evidence"]] == ["get_report", "read_attachment", "read_attachment"]
    assert got == [("download_file", "me@example.com")], "첨부마다 한 번만 받는다(그 사람의 신원으로)"
    assert res["numbers"]["mismatched"] == [], "첨부 본문이 숫자 대조의 근거다"
    assert "blocked_tools" not in res and res["incomplete"] is False


def test_OF1_첨부_받기가_울타리_밖이면_막이가_까닭을_주고_blocked_tools_에_싣는다(kit):
    dl = _tool("download_file", "부르면 안 된다")
    _kit_tools(kit, {"get_report": _tool("get_report", '{"blocks": [{"file_id": "f1"}]}')})
    base_pt = offload._person_tools

    async def _pt(_app, groups, user):
        d = dict(await base_pt(_app, groups, user))
        d["download_file"] = dl
        return d
    kit.mp.setattr(offload, "_person_tools", _pt)
    kit.llm.script = [_ai("", _call("download_file", file_id="f1")), _ai("", _call("voc_write_note", q="x")),
                      _ai("첨부는 울타리 밖이라 못 읽음.\n[못 채운 것] 첨부 본문")]

    async def scenario():
        out = await m.delegate_start(task="보고서 첨부 본문 요약", ctx=_ctx())
        await _done(out["job_id"])
        return await m.delegate_result(out["job_id"], ctx=_ctx())
    res = _run(scenario)
    from test_offload import CALLS
    assert not any(c[0] in ("download_file", "voc_write_note") for c in CALLS), "울타리 밖은 부르지 않는다"
    bt = {b["tool"]: b["why"] for b in res["blocked_tools"]}
    assert set(bt) == {"download_file", "voc_write_note"} and "울타리 밖" in bt["download_file"] and "울타리 밖" in bt["voc_write_note"]
    stub = [x for x in kit.llm.seen[1] if getattr(x, "type", "") == "tool"]
    assert "울타리 밖이라 못 읽음" in stub[0].content
    assert res["unfilled"] == ["첨부 본문"]


def test_OF1_설명에_첨부_읽기와_blocked_tools_를_적는다():
    assert "read_attachment" in m._DG_START_DESC and "blocked_tools" in m._DG_START_DESC and "직접 읽는다" in m._DG_START_DESC
    assert "blocked_tools" in m._DG_RESULT_DESC and "울타리 밖이라 못 읽음" in offload._DELEGATE_SYS

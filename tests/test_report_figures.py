# 보고서(/보고서 · 심의 보고서)에 도구 그림을 그 사람 명의로 — 글 속 그림을 포털 /agent/figures/upload 로 올려 그림 칸으로, 못 올린 그림은 그 자리에 까닭(HWAXPortal docs/report-figures RF-9 ~ RF-12)
"""왜 — 웹 챗은 도구 그림을 ARTIFACT_DIR 에 두고 /agent/artifacts/… 로 보이는데, /보고서 와 심의 보고서는 글만 블록으로 만들어 그림이 포털 상대 주소의
마크다운 글자로 RA 에 들어갔다(사용자 신고 11-02 — StepForge · DynaForge 그림이 보고서에 한 장도 없다). 가짜 포털(httpx MockTransport —
report_figures.PORTAL_TRANSPORT)과 가짜 게이트웨이 도구로 고정하는 것.
  · 마크다운 그림 · 맨 주소 · 절대 주소를 찾고 같은 그림은 한 번 올린다 — 그 사람의 PAT 로만(서비스 자격 없음).
  · 그 자리는 [그림 N — 설명], 못 올린 그림은 [그림 — 올리지 못함: 까닭] — 조용히 빠지는 그림이 없다. 그림 칸은 저장 호출의 extra_blocks 에.
  · PAT 가 없으면(MCP 잡) 부르지 않고 까닭, 자격 · 연결이 막히면 남은 그림은 부르지 않는다, 그림 하나의 문제(SVG 등)는 다음 그림을 막지 않는다.
  · RA 가 칸을 경고로 버리면 '못 들어감' 으로 센다. 몇 장 들어갔는지 /보고서 결과 · 심의 outcome 에 센다.
"""
import asyncio
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as D  # noqa: E402
import delib_jobs  # noqa: E402
import report_figures as RF  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import OPTS, _result, _settle_all, _start_job, world  # noqa: E402, F401 — world 는 픽스처

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300
PNG2 = b"\x89PNG\r\n\x1a\n" + b"\x01" * 300
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def _name(raw: bytes, ext: str) -> str:
    return hashlib.sha256(raw).hexdigest()[:20] + "." + ext


class _Portal:
    """가짜 포털 /agent/figures/upload — 받은 것(자격 · meta · 파일)을 적고, 이름마다 답을 바꿀 수 있다(refuse — 상태 · error · detail)."""

    def __init__(self, refuse: dict | None = None, down: bool = False):
        self.refuse, self.down, self.got = refuse or {}, down, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        assert req.url.path == "/agent/figures/upload" and req.method == "POST", req.url
        body = req.content
        meta = json.loads(re.search(rb'name="meta"\r\n\r\n(.*?)\r\n--', body, re.S).group(1))
        raw = body.split(b'name="file"', 1)[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
        self.got.append({"auth": req.headers.get("authorization"), "meta": meta, "raw": raw})
        st = self.refuse.get(meta["filename"])
        if st:
            return httpx.Response(st[0], json={"ok": False, "error": st[1], "detail": st[2]})
        return httpx.Response(200, json={"ok": True, "file_id": f"f{len(self.got)}"})


@pytest.fixture
def figs(monkeypatch, tmp_path):
    """그림 둘(PNG · PNG2) · SVG 하나를 둔 ARTIFACT_DIR 와 가짜 포털."""
    d = tmp_path / "artifacts"
    d.mkdir()
    for raw, ext in ((PNG, "png"), (PNG2, "png"), (SVG, "svg")):
        (d / _name(raw, ext)).write_bytes(raw)
    portal = _Portal()
    monkeypatch.setattr(RF, "PORTAL_TRANSPORT", httpx.MockTransport(lambda req: portal(req)))
    monkeypatch.setattr(app, "ARTIFACT_DIR", str(d))
    return portal, d


def _attach(blocks, pat="pat-kim", **kw):
    return asyncio.run(RF.attach(blocks, user_pat=pat, **kw))


# ── 단위 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_마크다운_맨_주소_절대_주소를_찾아_한_번씩_올리고_그_자리를_표지로(figs):
    portal, _d = figs
    a, b = _name(PNG, "png"), _name(PNG2, "png")
    blocks = {"results": [f"응력은 이렇다.\n![응력 분포](/agent/artifacts/{a})\n끝."],
              "minutes": [f"[생성된 이미지 URL — 답변에 ![설명](url) 로 포함하세요]\n/agent/artifacts/{b}",
                          f"다시 본다 ![](https://portal.example/agent/artifacts/{a}) 그리고 /agent/artifacts/{b}."]}
    out = _attach(blocks)
    assert [g["meta"]["filename"] for g in portal.got] == [a, b], "같은 그림은 한 번만 올린다"
    assert all(g["auth"] == "Bearer pat-kim" for g in portal.got), "그 사람의 PAT 로만"
    assert portal.got[0]["meta"]["sha256"] == hashlib.sha256(PNG).hexdigest() and portal.got[0]["raw"] == PNG
    assert portal.got[0]["meta"]["mime"] == "image/png" and portal.got[0]["meta"]["caption"] == "응력 분포"
    assert out.blocks["results"] == ["응력은 이렇다.\n[그림 1 — 응력 분포]\n끝."]
    assert out.blocks["minutes"] == ["[생성된 이미지 URL — 답변에 ![설명](url) 로 포함하세요]\n[그림 2]",
                                     "다시 본다 [그림 1] 그리고 [그림 2]."]
    assert "/agent/artifacts/" not in json.dumps(out.blocks, ensure_ascii=False), "포털 상대 주소가 RA 로 가지 않는다"
    assert out.extra_blocks == [
        {"id": f"fig_{hashlib.sha256(PNG).hexdigest()[:16]}", "type": "image", "props": {"max_count": 1},
         "content": {"files": [{"file_id": "f1", "caption": "그림 1 — 응력 분포", "alt": "응력 분포"}]}},
        {"id": f"fig_{hashlib.sha256(PNG2).hexdigest()[:16]}", "type": "image", "props": {"max_count": 1},
         "content": {"files": [{"file_id": "f2", "caption": "그림 2", "alt": "도구 그림"}]}}]
    assert all(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", x["id"]) for x in out.extra_blocks), "RA 블록 id 규칙"
    assert out.summary() == {"found": 2, "attached": 2, "failed": 0, "reasons": []}
    assert blocks["results"][0].endswith("끝.") and "/agent/artifacts/" in blocks["results"][0], "받은 블록은 바꾸지 않는다(새 사본)"


def test_그림이_없으면_블록을_그대로_돌려주고_포털을_부르지_않는다(figs):
    portal, _d = figs
    blocks = {"results": ["그림 없음"], "minutes": []}
    out = _attach(blocks)
    assert out.blocks is blocks and out.extra_blocks == [] and portal.got == []
    assert RF.count(blocks) == 0


def test_PAT_가_없으면_부르지_않고_모든_그림_자리에_까닭(figs):
    portal, _d = figs
    a, b = _name(PNG, "png"), _name(PNG2, "png")
    out = _attach({"results": [f"![x](/agent/artifacts/{a}) /agent/artifacts/{b}"]}, pat="")
    assert portal.got == [], "서비스 자격으로는 올리지 않는다"
    assert out.blocks["results"] == [f"[그림 — 올리지 못함: {RF.NO_PAT}] [그림 — 올리지 못함: {RF.NO_PAT}]"]
    assert out.extra_blocks == [] and out.summary()["failed"] == 2 and out.summary()["attached"] == 0


def test_파일이_없거나_SVG_면_그_그림만_까닭을_남기고_다음_그림은_올린다(figs):
    portal, d = figs
    gone, s, ok = "0" * 20 + ".png", _name(SVG, "svg"), _name(PNG, "png")
    portal.refuse[s] = (415, "unsupported_media_type", "SVG 는 Report Archive 그림으로 넣지 않습니다 — PNG · JPEG · GIF · WebP 만.")
    out = _attach({"results": [f"![a](/agent/artifacts/{gone})", f"![b](/agent/artifacts/{s})", f"![c](/agent/artifacts/{ok})"]})
    assert out.blocks["results"][0].startswith("[그림 — 올리지 못함: 그림 파일이 서버에 없다")
    assert out.blocks["results"][1] == "[그림 — 올리지 못함: SVG 는 Report Archive 그림으로 넣지 않습니다 — PNG · JPEG · GIF · WebP 만(HTTP 415)]"
    assert out.blocks["results"][2] == "[그림 1 — c]", "그림 하나의 문제는 다음 그림을 막지 않는다"
    assert [g["meta"]["filename"] for g in portal.got] == [s, ok] and portal.got[0]["meta"]["mime"] == "image/svg+xml"
    assert out.summary()["found"] == 3 and out.summary()["attached"] == 1 and out.summary()["failed"] == 2


@pytest.mark.parametrize("case", ["down", "401", "403"])
def test_자격이나_연결이_막히면_남은_그림은_부르지_않고_같은_까닭(figs, case):
    portal, _d = figs
    if case == "down":
        portal.down = True
    else:
        portal.refuse[_name(PNG, "png")] = (int(case), "read_only_token" if case == "403" else None,
                                            "읽기 전용 토큰으로는 넣지 못합니다." if case == "403" else "invalid or expired PAT")
    a, b = _name(PNG, "png"), _name(PNG2, "png")
    out = _attach({"results": [f"/agent/artifacts/{a}", f"/agent/artifacts/{b}"]})
    assert len(portal.got) <= 1, "같은 까닭으로 실패할 그림을 계속 부르지 않는다(포털이 막히면 장마다 시간 한도를 기다린다)"
    assert out.blocks["results"][0] == out.blocks["results"][1] and out.blocks["results"][0].startswith("[그림 — 올리지 못함:")
    assert out.extra_blocks == []


def test_한_보고서의_그림_수_상한(figs, monkeypatch):
    portal, _d = figs
    monkeypatch.setattr(RF, "FIGURES_MAX", 1)
    a, b = _name(PNG, "png"), _name(PNG2, "png")
    out = _attach({"results": [f"/agent/artifacts/{a} /agent/artifacts/{b}"]})
    assert out.blocks["results"] == ["[그림 1] [그림 — 올리지 못함: 보고서 하나에 그림은 1장까지 싣는다 — 나머지는 대화에서 본다]"]
    assert len(portal.got) == 1


def test_이름이_달라도_같은_그림이면_칸_하나(figs):
    portal, d = figs
    a = _name(PNG, "png")
    (d / ("f" * 20 + ".png")).write_bytes(PNG)
    out = _attach({"results": [f"/agent/artifacts/{a} /agent/artifacts/{'f' * 20}.png"]})
    assert out.blocks["results"] == ["[그림 1] [그림 1]"] and len(out.extra_blocks) == 1 and len(portal.got) == 1


def test_RA_가_칸을_경고로_버리면_못_들어감으로_센다(figs):
    _portal, _d = figs
    out = _attach({"results": [f"/agent/artifacts/{_name(PNG, 'png')}", f"/agent/artifacts/{_name(PNG2, 'png')}"]})
    bid = out.extra_blocks[0]["id"]
    out.saved({"report_id": 7, "warnings": [f"extra block '{bid}'(image): 내용 없음/형식 불일치 — 건너뜀"]})
    s = out.summary()
    assert (s["attached"], s["failed"]) == (1, 1) and bid in s["reasons"][0]
    assert "2장 중 1장" in out.note()


# ── /보고서 ──────────────────────────────────────────────────────────────────────────────────────────────────────
class _Tool:
    def __init__(self, out):
        self.out, self.calls = out, []

    async def ainvoke(self, args):
        self.calls.append(copy.deepcopy(args))
        return self.out


def _report_save(monkeypatch, history, out=None, pat="pat-kim"):
    tool = _Tool(out if out is not None else json.dumps({"report_id": 58, "title": "t", "page_count": 1, "warnings": []}))

    async def fake_tools(app_, groups, user="", user_pat=""):
        return {"create_report_draft": tool}
    monkeypatch.setattr(D, "_tools_by_name", fake_tools)

    async def run():
        return [chunk async for chunk in D.run_report_save(None, "", history, [], user="kim@corp.com", user_pat=pat)]
    events = asyncio.run(run())
    results = [json.loads(e.decode().split("data: ", 1)[1]) for e in events if e.startswith(b"event: result")]
    return tool, results[0]


def test_보고서는_대화의_도구_그림을_그림_칸으로_싣고_몇_장인지_센다(figs, monkeypatch):
    portal, _d = figs
    a = _name(PNG, "png")
    history = [{"role": "user", "content": "낙하 응력 분포를 그려 줘"},
               {"role": "assistant", "content": f"보스 뿌리에 응력이 몰린다.\n\n![생성된 그래프](/agent/artifacts/{a})"}]
    tool, res = _report_save(monkeypatch, history)
    (args,) = tool.calls
    assert args["extra_blocks"][0]["content"]["files"][0]["file_id"] == "f1"
    dumped = json.dumps(args["blocks"], ensure_ascii=False)
    assert "[그림 1 — 생성된 그래프]" in dumped and "/agent/artifacts/" not in dumped
    assert list(args) == ["template_id", "template_version", "title", "blocks", "extra_blocks", "tags"]
    assert res["figures"] == {"found": 1, "attached": 1, "failed": 0, "reasons": []}
    assert "그림 1장을 보고서에 그림 칸으로 실었다" in res["content"] and res["ra_report"]["id"] == 58
    assert portal.got[0]["auth"] == "Bearer pat-kim"


def test_보고서에서_못_올린_그림은_그_자리에_까닭이_남는다(figs, monkeypatch):
    portal, _d = figs
    s = _name(SVG, "svg")
    portal.refuse[s] = (415, "unsupported_media_type", "SVG 는 Report Archive 그림으로 넣지 않습니다.")
    history = [{"role": "user", "content": "그려 줘"}, {"role": "assistant", "content": f"![도면](/agent/artifacts/{s})"}]
    tool, res = _report_save(monkeypatch, history)
    (args,) = tool.calls
    assert "extra_blocks" not in args, "실은 그림이 없으면 칸도 없다"
    assert "[그림 — 올리지 못함: SVG 는 Report Archive 그림으로 넣지 않습니다(HTTP 415)]" in json.dumps(args["blocks"], ensure_ascii=False)
    assert res["figures"]["failed"] == 1 and "싣지 못했다" in res["content"]


def test_보고서_저장이_실패하면_실은_그림도_못_들어감(figs, monkeypatch):
    _portal, _d = figs
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": f"/agent/artifacts/{_name(PNG, 'png')}"}]
    _tool, res = _report_save(monkeypatch, history, out="✖ 도구 create_report_draft 호출 실패: RA 오류")
    assert res["figures"]["attached"] == 0 and res["figures"]["reasons"] == ["보고서가 저장되지 않았다"]
    assert "ra_report" not in res


# ── 심의 보고서 ─────────────────────────────────────────────────────────────────────────────────────────────────
def _with_figure(w, monkeypatch, name: str):
    """의장 결정문 끝에 도구 그림 한 장 — 결정문이 보고서의 '권고' 칸으로 간다."""
    real = w.llm_text

    async def llm_text(llm, system, human, *img):
        out = await real(llm, system, human, *img)
        return out + f"\n\n![응력](/agent/artifacts/{name})" if out.startswith("## 의사결정문") else out
    monkeypatch.setattr(D, "_llm_text", llm_text)


def _run_job(w, **kw) -> tuple[dict, dict]:
    """잡으로 끝까지 — (결과, 잡 결과 보기 delib_jobs.summary(full)). 원장은 outcome 사건의 figures 를 report_figures 로 접는다."""
    async def go():
        job = _start_job(OPTS, **kw)
        await _settle_all()
        return job
    job = asyncio.run(go())
    res = _result(w, job)
    return res, delib_jobs.summary(res["led"], full=True)


def test_심의_보고서는_사람_PAT_로_그림을_그림_칸으로_싣고_outcome_에_센다(world, figs, monkeypatch):
    portal, _d = figs
    res0, view0 = _run_job(world, user_pat="pat-kim")
    assert "extra_blocks" not in res0["reports"][0] and "report_figures" not in view0 and portal.got == [], \
        "그림이 없는 심의는 저장 인자 · 잡 결과가 종전과 같다"
    world.reset()
    a = _name(PNG, "png")
    _with_figure(world, monkeypatch, a)
    res, view = _run_job(world, user_pat="pat-kim")
    (args,) = res["reports"]
    assert args["extra_blocks"][0]["content"]["files"][0] == {"file_id": "f1", "caption": "그림 1 — 응력", "alt": "응력"}
    rec = json.dumps(args["blocks"]["recommendation"], ensure_ascii=False)
    assert "[그림 1 — 응력]" in rec and "/agent/artifacts/" not in json.dumps(args["blocks"], ensure_ascii=False)
    assert portal.got[0]["auth"] == "Bearer pat-kim"
    assert view["report_figures"] == {"found": 1, "attached": 1, "failed": 0, "reasons": []}, "잡 결과가 센다"
    assert "그림 1장을 보고서에 그림 칸으로 실었다" in view["decision"], "결정문 끝 안내"
    assert "![응력](/agent/artifacts/" in view["decision"], "대화에 보이는 결정문의 그림은 그대로다(보고서 사본만 바꾼다)"


def test_사람_PAT_가_없는_심의_MCP_잡_은_올리지_않고_보고서_그_자리에_까닭(world, figs, monkeypatch):
    portal, _d = figs
    _with_figure(world, monkeypatch, _name(PNG, "png"))
    res, view = _run_job(world)
    (args,) = res["reports"]
    assert portal.got == [], "서비스 자격으로는 올리지 않는다"
    assert "extra_blocks" not in args
    assert f"[그림 — 올리지 못함: {RF.NO_PAT}]" in json.dumps(args["blocks"]["recommendation"], ensure_ascii=False)
    assert view["report_figures"] == {"found": 1, "attached": 0, "failed": 1, "reasons": [RF.NO_PAT]}
    assert "그림 1장을 보고서에 싣지 못했다" in view["decision"]


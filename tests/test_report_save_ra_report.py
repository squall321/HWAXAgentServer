# '/보고서' 저장이 성공하면 result 사건에 보고서 번호 · 제목을 구조로 싣는다 — 포털 '발표자료로' 단추가 글을 파싱하지 않게(포털 docs/wda-deck WD-D12)
"""왜 — 포털은 '/보고서' 저장 답 옆에 '발표자료로' 단추를 둔다. 종전 result 는 글('📄 Report Archive 보고서 #58 로 저장했습니다')뿐이라 화면이
번호를 얻으려면 글을 파싱해야 했다(글은 바뀌고 파싱은 조용히 깨진다). 성공했을 때만 `ra_report: {id, title}` 를 싣고, 실패 · 불가는 싣지 않는다
(없는 보고서로 단추가 서면 안 된다).
"""
import asyncio
import json

import deliberation as D

REFUSAL = ("✖ 도구 create_report_draft 호출 실패: reportarchive: u@corp.com — Report Archive 토큰이 포털에 "
           "등록되어 있지 않습니다.")


class _Tool:
    def __init__(self, out):
        self.out, self.calls = out, []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self.out


def _result(monkeypatch, out, note: str = "") -> dict:
    tool = _Tool(out)

    async def fake_tools(app, groups, user="", user_pat=""):
        return {"create_report_draft": tool} if out is not None else {}
    monkeypatch.setattr(D, "_tools_by_name", fake_tools)
    history = [{"role": "user", "content": "굽힘 반경 3mm 에서 구리 두께를 낮춰도 되나"},
               {"role": "assistant", "content": "결론: 12um 는 위험하다. 근거: 피로 수명 시험."}]

    async def run():
        return [chunk async for chunk in D.run_report_save(None, note, history, [], user="u@corp.com")]
    events = asyncio.run(run())
    results = [json.loads(e.decode().split("data: ", 1)[1]) for e in events if e.startswith(b"event: result")]
    assert len(results) == 1, "result 는 하나다"
    return results[0]


def test_저장에_성공하면_보고서_번호와_제목을_구조로_싣는다(monkeypatch):
    r = _result(monkeypatch, json.dumps({"report_id": 58, "title": "심의 — 굽힘", "page_count": 1}))
    assert r["type"] == "text" and "#58" in r["content"], "글은 그대로다"
    assert r["ra_report"] == {"id": 58, "title": "심의 — 굽힘 반경 3mm 에서 구리 두께를 낮춰도 되나"}, \
        "제목은 RA 에 보낸 제목이다(화면 · 알림이 같은 이름을 쓴다)"


def test_번호가_문자열로_와도_정수로_싣는다(monkeypatch):
    r = _result(monkeypatch, json.dumps({"report": {"id": "77"}}))
    assert r["ra_report"]["id"] == 77


def test_거부되거나_불가하면_ra_report_를_싣지_않는다(monkeypatch):
    refused = _result(monkeypatch, REFUSAL)
    assert "ra_report" not in refused and refused["content"].startswith("Report Archive 저장 실패")
    missing = _result(monkeypatch, json.dumps({"title": "번호 없음"}))
    assert "ra_report" not in missing, "번호를 못 받았으면 단추가 서면 안 된다"
    no_tool = _result(monkeypatch, None)
    assert "ra_report" not in no_tool, "도구가 없으면(응답 없음) 실패다 — 단추를 세우지 않는다"

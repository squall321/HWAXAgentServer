# RA 저장 실패의 사유를 버리지 않는다 — 게이트웨이의 'RA 토큰을 등록하라' 거부가 /보고서·심의 저장·심의 근거로 올 때
#
# 게이트웨이는 2026-09-29 부터 RA 토큰을 등록하지 않은 사용자의 RA 호출을 거부한다(isError). 그 문구가 여기서는
# app._cap_tool 의 ✖ 표식이나 _call 의 "(tool … error" 로 싸여 **정상 반환**된다. 종전엔 ① /보고서가
# 'RA 미가용, 나중에 다시' 로 뭉개고 ② 심의 이어하기가 '#N 에 이어붙임' 으로 거짓 성공을 알리고 ③ 심의가 거부 문구를
# '실호출 정량 결과' 근거로 주입했다(적대적 검토에서 셋 다 재현).
import asyncio
import json

import deliberation as D

REFUSAL = ("✖ 도구 create_report_draft 호출 실패: reportarchive: u@corp.com — Report Archive 토큰이 포털에 "
           "등록되어 있지 않습니다. 포털 'API 토큰' 페이지에서 Report Archive 토큰(rat_…)을 등록한 뒤 다시 시도하세요.")


def test_실패_판정은_포장을_벗기고_사유를_그대로_준다():
    f = D._ra_save_failure(REFUSAL)
    assert f.startswith("reportarchive: u@corp.com") and "API 토큰" in f, "✖ 도구 … 호출 실패: 머리는 벗긴다"
    assert D._ra_save_failure("(tool create_report_draft error: timeout)") == "timeout"
    assert D._ra_save_failure(json.dumps({"error": "not_visible", "message": "볼 수 없다"}))
    assert D._ra_save_failure(None) and D._ra_save_failure("   ")
    assert D._ra_save_failure(json.dumps({"report_id": 58, "page_count": 1})) == ""
    assert D._ra_save_failure({"ok": True, "page_count": 3}) == ""


def test_거부된_이어붙이기는_이어붙였다고_하지_않는다():
    rid, note = D._ra_save_outcome(REFUSAL, 1567, True)
    assert rid is None and "저장 실패" in note and "API 토큰" in note and "이어붙임" not in note


def test_성공한_이어붙이기는_번호가_없어도_대상_번호를_쓴다():
    rid, note = D._ra_save_outcome(json.dumps({"ok": True, "page_count": 3}), 1567, True)
    assert rid == 1567 and "#1567 에 페이지로 이어붙임" in note
    rid, note = D._ra_save_outcome(json.dumps({"report_id": 58}), 0, True)
    assert rid == 58 and "#58 로 저장됨" in note


def test_새_저장인데_번호가_없으면_모른다고_말한다():
    rid, note = D._ra_save_outcome(json.dumps({"title": "x"}), 0, True)
    assert rid is None and "번호를 받지 못했다" in note


def test_저장을_끈_심의는_아무것도_안_붙인다():
    assert D._ra_save_outcome(None, 0, False) == (None, "")


def test_심의_근거_판정은_실패_표식을_근거로_받지_않는다():
    assert D._delib_tool_result_ok("✖ 도구 search_reports 호출 실패: reportarchive: …") is False
    assert D._delib_tool_result_ok('{"reports": [{"report_id": 1}]}') is True


class _Tool:
    def __init__(self, out):
        self.out, self.calls = out, []

    async def ainvoke(self, args):
        self.calls.append(args)
        return self.out


def _report_save(monkeypatch, out) -> str:
    tool = _Tool(out)

    async def fake_tools(app, groups, user="", user_pat=""):
        return {"create_report_draft": tool}
    monkeypatch.setattr(D, "_tools_by_name", fake_tools)
    history = [{"role": "user", "content": "굽힘 반경 3mm 에서 구리 두께를 낮춰도 되나"},
               {"role": "assistant", "content": "결론: 12um 는 위험하다. 근거: 피로 수명 시험."}]

    async def run():
        return [chunk async for chunk in D.run_report_save(None, "", history, [], user="u@corp.com")]
    events = asyncio.run(run())
    results = [json.loads(e.decode().split("data: ", 1)[1]) for e in events if e.startswith(b"event: result")]
    assert tool.calls, "도구를 실제로 불러야 한다"
    return results[-1]["content"]


def test_보고서_저장이_거부되면_등록_안내를_그대로_보여_준다(monkeypatch):
    text = _report_save(monkeypatch, REFUSAL)
    assert text.startswith("Report Archive 저장 실패") and "API 토큰" in text and "나중에 다시" not in text


def test_보고서_저장_성공은_번호를_알린다(monkeypatch):
    assert "#58 로 저장했습니다" in _report_save(monkeypatch, json.dumps({"report_id": 58}))

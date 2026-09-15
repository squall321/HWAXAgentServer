# 도구 성패 판정 — **양쪽으로 다 틀리고 있었다**(2026-09-15 감사)
#
# ① 거짓 실패. 결과 전체(최대 20만 자)에서 ✖ 를 찾았다. 합격/불합격 표를 내는 도구나
#    ✔/✖ 를 쓰는 한국어 문서가 결과에 있으면 **성공한 호출이 실패로** 기록됐다. 절차
#    원장까지 번져, 쓰기 도구면 "실제로 만들어졌을 수 있으니 확인하라" 를 띄웠다 —
#    이미 잘 끝난 쓰기를 확인하러 보낸 셈이다.
# ② 거짓 성공. `deliberation._call` 이 예외를 문자열로 삼켜 돌려주므로 `except` 로는
#    실패를 못 잡는다. 직접 실행 경로가 실패를 `ok: True` 로 내보내고 있었다.
import app as A


def test_뒤쪽의_표식은_남의_것이다():
    """표식은 실패를 만들 때 **맨 앞**에 찍힌다 — 뒤에서 찾은 ✖ 는 결과 내용이다."""
    table = "규칙 점검 결과\n" + "\n".join(f"R{i}: {'✔' if i % 2 else '✖'}" for i in range(50))
    assert A._result_ok(table) is True, "합격/불합격 표를 실패로 읽었다"
    assert A._result_ok("본문이 길다 " * 5000 + "✖") is True


def test_맨_앞의_표식은_진짜_실패다():
    assert A._result_ok(f"{A._TOOL_FAIL_MARK} 도구 x 호출 실패: timeout") is False
    assert A._result_ok(f"   \n{A._TOOL_FAIL_MARK} 도구 x 호출 실패") is False, "공백은 무시한다"


def test_삼켜진_예외_문자열도_실패로_본다():
    """`_call` 은 예외를 `"(tool X error: …)"` 로 바꿔 **정상 반환한다.**"""
    assert A._result_ok("(tool get_material error: Connection refused)") is False


def test_차단_표식도_실패다():
    assert A._result_ok(f"id=X {A._PHANTOM_ID_MARK}. 추측한 ID 로 부르면 …") is False


def test_문자열이_아니면_실패로_몰지_않는다():
    """성패를 말해 주는 문자열이 아닌 것과, 실패라고 말한 것은 다르다."""
    for v in ({"rows": []}, [1, 2], None, 0):
        assert A._result_ok(v) is True, v


# ── 5차 감사 — 한 리포에 판정기가 둘인데 답이 달랐다 ──────────────────────
def test_본문이_실패라고_말하는_것도_실패다():
    """`isError=false` 로 오는 실패를 표식만 보고 전부 성공으로 적었다. 아래 둘은
    **실호출로 확인된 실물 본문**이다 — 챗 판정기는 성공, 심의 판정기는 실패로 봤다.
    챗 쪽이 성공으로 보면 화면에 '실패' 배지가 안 붙고(실패가 성공과 똑같이 보인다),
    절차 원장에도 성공으로 적혀 **검증된 단계인 양** 굳는다."""
    import json

    import deliberation as D

    fails = [{"status": "error", "data": None, "errors": [{"code": "E101"}]},
             {"status": "error", "data": None, "errors": []},      # errors 가 비어도 실패다
             {"error": "not_visible", "message": "볼 수 없는 id 입니다"},
             {"refused": True, "reason": "근거 점수가 임계 밑"},     # 허브 관례
             {"exit_code": 2, "stderr": "죽었다"}]
    for obj in fails:
        txt = json.dumps(obj, ensure_ascii=False)
        assert A._result_ok(txt) is False, obj
        assert D._delib_tool_result_ok(txt) is False, obj

    goods = [{"ok": True, "rows": [1, 2]}, {"ok": True, "errors": []},
             {"exit_code": 0, "stdout": "{}"}, {"status": "warning", "data": 1}]
    for obj in goods:
        txt = json.dumps(obj, ensure_ascii=False)
        assert A._result_ok(txt) is True, obj


def test_평문_결과는_그대로_성공이다():
    """봉투 검사가 넓어졌다고 JSON 이 아닌 결과까지 실패로 몰면 안 된다."""
    for txt in ("그냥 결과 문자열", "", "규칙 점검\nR1: ✔\nR2: ✖", "[1,2,3]"):
        assert A._result_ok(txt) is True, txt

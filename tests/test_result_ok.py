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

# 도구 호출 기록이 **절차로 펼 수 있는 모양**인가 (HWAXPortal docs/procedures/PLAN.md §9-8·§9-9)
#
# 왜 이게 필요했나. 세 표면을 다 읽어 보니 어느 것도 절차를 복원할 수 없었다 —
#   · 인자는 시작 이벤트, 결과는 완료 이벤트에 실리는데 **둘을 잇는 키가 없다**
#   · 도구 예외는 결과 문자열로 삼켜져 **성공과 구분되지 않는다**
#   · 표식 없이 자르므로 n 자 뒤에서만 갈리는 두 호출이 **같은 서명**이 된다
# 셋 다 "실패가 정상 응답과 똑같이 생겼다" 의 변주다. 아래가 그 셋을 건다.
import json

import app as A
import deliberation as D


# ── ① 짝 — 인자와 결과를 잇는 키 ─────────────────────────────────────────
def _block(src: str, head: str, n: int = 520) -> str:
    i = src.index(head)
    return src[i:i + n]


def test_도구_이벤트에_호출_식별자가_실린다():
    """`run_id` 는 같은 호출의 시작·완료에 같은 값으로 온다. 안 실으면 짝을 못 짓는다."""
    src = open("app.py", encoding="utf-8").read()
    for name, head in (("시작", '"step": f"도구 호출: {_tname}"'),
                       ("완료", '"step": f"도구 완료: {_tname2}"')):
        assert '"call": str(event["run_id"])' in _block(src, head), f"{name} 이벤트에 call 이 없다"


def test_같은_도구_두_번_호출이_서로_다른_줄로_남는다():
    """§5-5 의 실측 — `predict_sed` 다섯 번 병렬 호출의 프리뷰가 전부 같았다.
    짝 키가 있으면 프리뷰가 같아도 **다른 호출임이 남는다**."""
    ev = [{"tool": "predict_sed", "call": "r1", "detail": "{…}"},
          {"tool": "predict_sed", "call": "r2", "detail": "{…}"}]
    assert len({e["call"] for e in ev}) == 2


# ── ② 성패 — 실패가 성공과 달라 보이는가 ─────────────────────────────────
def test_실패_표지가_있고_완료_이벤트가_성패를_싣는다():
    src = open("app.py", encoding="utf-8").read()
    assert "_TOOL_FAIL_MARK" in src
    assert '"ok": _ok' in _block(src, '"step": f"도구 완료: {_tname2}"'), "완료 이벤트에 ok 가 없다"


def test_도구_예외는_표지를_달고_돌아온다():
    """예외를 문자열로 바꾸는 것은 의도다(LLM 이 스키마를 보고 교정한다).
    다만 **성패 판단까지 버리면** 기록이 거짓이 된다."""
    src = open("app.py", encoding="utf-8").read()
    i = src.index("호출 실패: {str(exc)[:500]}")
    assert "_TOOL_FAIL_MARK" in src[i - 120:i], "실패 메시지에 표지가 안 붙는다"


def test_직접_실행_경로도_같은_계약을_준다():
    """핀 도구로 돌린 것만 계약이 다르면 그 턴은 절차로 못 편다."""
    src = open("app.py", encoding="utf-8").read()
    blk = src[src.index('f"도구 직접 실행: {_tn}"'):][:900]
    for k in ('"call": _cid', '"ok": _ok', "result_preview"):
        assert k in blk, f"직접 실행 경로에 {k} 가 없다"


# ── ③ 절단 — 다른 호출이 같아지지 않는가 ─────────────────────────────────
def test_잘릴_때_지문이_붙어_다른_호출이_구분된다():
    a = {"q": "가" * 300, "n": 1}
    b = {"q": "가" * 300, "n": 2}          # 300자 뒤에서만 갈린다
    pa, pb = A._tool_preview(a, 100), A._tool_preview(b, 100)
    assert pa != pb, "표식 없이 자르면 두 호출이 같은 서명이 된다"
    assert pa.count("…#") == 1 and len(pa.split("…#")[1]) == 6


def test_안_잘리면_지문을_안_붙인다():
    """짧은 값에 군더더기를 붙이면 사람이 읽는 화면이 지저분해진다."""
    got = A._tool_preview({"n": 1}, 100)
    assert "…#" not in got and got == '{"n": 1}'


def test_심의_절단도_같은_규약이다():
    a, b = {"q": "나" * 200, "n": 1}, {"q": "나" * 200, "n": 2}
    assert D._delib_preview(a, 60) != D._delib_preview(b, 60)
    assert "…#" in D._delib_preview(a, 60)
    assert D._delib_preview({"n": 1}, 60) == '{"n": 1}'


# ── ④ 심의 — 안에서 맞던 짝을 나갈 때 안 버린다 ──────────────────────────
def test_심의는_tool_call_id_를_실어_보낸다():
    src = open("deliberation.py", encoding="utf-8").read()
    i = src.index("calls.append((getattr(m,")
    assert 'tool_call_id' in src[i:i + 300], "짝 키를 삼중항에 안 싣는다"
    j = src.index("for _tn, _ap, _out, _cid in _calls:")
    blk = src[j:j + 900]
    assert '"call": _cid' in blk, "status 에 call 이 없다"
    assert '"ok": _ok' in blk, "status 에 ok 가 없다"
    assert "args=_ap" in blk, "evidence 가 여전히 인자 없이 나간다"


def test_evidence_가_인자_없이_나가지_않는다():
    """핸드오프가 '결과는 있는데 인자가 없다' 가 되던 뿌리다."""
    src = open("deliberation.py", encoding="utf-8").read()
    j = src.index('_delib("evidence", source=f"{_k} · {_tn}"')
    assert "args=_ap" in src[j:j + 260]


# ── 경유(invoke_tool)는 벗긴다 ───────────────────────────────────────────
def test_경유를_벗겨_안쪽_도구_이름으로_기록한다():
    """이 허브의 안내가 "목록에 없는 도구는 invoke_tool 로 부르라" 라서 실제 호출의 상당수가
    경유를 탄다. 안 벗기면 기록이 전부 `invoke_tool` 이 되고 —
      · 절차 도출이 `invoke_tool` 단계만 늘어놓고
      · 어느 앱인지 못 찾아 **전부 결손**이 된다.
    """
    name, args = A._inner_tool("invoke_tool",
                               {"name": "predict_sed", "arguments": {"ap_cx": 50.1}})
    assert name == "predict_sed" and args == {"ap_cx": 50.1}


def test_경유가_아니면_그대로_둔다():
    assert A._inner_tool("search_voc", {"keyword": "발열"}) == ("search_voc", None)
    # 안쪽 이름이 없으면 벗길 수 없다 — 지어내지 않는다
    assert A._inner_tool("invoke_tool", {"arguments": {}}) == ("invoke_tool", None)
    assert A._inner_tool("invoke_tool", "문자열") == ("invoke_tool", None)


def test_시작과_완료가_같은_이름을_쓴다():
    """한쪽만 안쪽 이름이면 화면과 원장이 두 줄로 갈라진다."""
    src = open("app.py", encoding="utf-8").read()
    assert '"tool": _tname,' in src and '"tool": _tname2,' in src
    assert '"via": "invoke_tool"' in src, "경유했다는 사실이 안 남는다"

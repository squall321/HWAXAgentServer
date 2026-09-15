# `invoke_tool` 경유 벗기기 — 안쪽 이름과 **인자**가 기록에 제대로 남는가
#
# 이 허브의 안내가 "목록에 없는 도구는 invoke_tool 로 부르라" 라서 실제 챗 호출의 상당수가
# 이 경유를 탄다. 기록에 `invoke_tool` 만 남으면 절차 도출이 그것만 늘어놓고, 어느 앱인지
# 못 찾아 전부 결손이 된다.
import json

import app as A


def test_안쪽_이름과_인자를_꺼낸다():
    name, args = A._inner_tool("invoke_tool",
                               {"name": "bend_profile", "arguments": {"project_id": "p1"}})
    assert name == "bend_profile" and args == {"project_id": "p1"}


def test_인자가_JSON_문자열이어도_푼다():
    """모델은 `arguments` 를 **문자열**로 곧잘 보낸다. 예전엔 그때 `{}` 를 돌려줘
    호출부가 진짜 입력을 그것으로 갈아 끼웠다 — 절차 초안이 **인자 없는 단계**를 만들고,
    날것을 싣는 `detail_full` 의 존재 이유가 사라진다."""
    name, args = A._inner_tool(
        "invoke_tool", {"name": "evaluate_laminate",
                        "arguments": json.dumps({"laminate": {"plies": 4}})})
    assert name == "evaluate_laminate"
    assert args == {"laminate": {"plies": 4}}, args


def test_인자가_없으면_빈_것으로_둔다():
    """이 허브가 권하는 정상 모양이다 — `invoke_tool(name="list_materials")`.

    ⚠ 한때 여기서 **바깥 dict 를 통째로** 돌려줬다. 그러면 `name` 이 그 도구의 인자인 양
    기록돼, 저장 검증이 "스키마에 없는 인자 — name" 으로 **사람이 넣은 적 없는 것**을
    거절한다. 스키마가 자유 object 인 도구면 재생 때 실제로 전송된다.
    """
    for inp in ({"name": "list_materials"},
                {"name": "x", "arguments": None},
                {"name": "x", "arguments": "   "}):
        name, args = A._inner_tool("invoke_tool", inp)
        assert args == {}, (inp, args)
        assert "name" not in args


def test_못_풀면_원문을_그대로_둔다():
    """`{}` 로 뭉개면 '인자 없음' 과 같아지고, 바깥 dict 면 `name` 을 지어낸다.
    원문을 두면 기록에 남고 초안이 `args_not_structured` 결손으로 잡는다."""
    name, args = A._inner_tool("invoke_tool", {"name": "x", "arguments": "이건 JSON 이 아니다"})
    assert name == "x" and args == "이건 JSON 이 아니다"
    assert args is not None, "None 이면 `via: invoke_tool` 표시까지 사라진다"
    # dict 가 아닌 JSON 도 마찬가지 — 인자 이름을 지어내지 않는다
    _n, a2 = A._inner_tool("invoke_tool", {"name": "x", "arguments": [1, 2]})
    assert not isinstance(a2, dict), a2


def test_경유가_아니면_건드리지_않는다():
    for nm, inp in (("bend_profile", {"a": 1}),
                    ("someapp_invoke_tool", {"name": "x"}),   # 접두 붙은 다른 도구
                    ("invoke_tool", {"name": ""}),            # 안쪽 이름이 비었다
                    ("invoke_tool", "dict 가 아니다")):
        name, args = A._inner_tool(nm, inp)
        assert name == nm and args is None, (nm, name, args)

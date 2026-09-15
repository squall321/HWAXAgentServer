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


def test_못_풀면_바깥_입력을_그대로_둔다():
    """`{}` 로 뭉개면 기록이 빈다. 못 푸는 것과 비어 있는 것은 다르다."""
    inp = {"name": "x", "arguments": "이건 JSON 이 아니다"}
    name, args = A._inner_tool("invoke_tool", inp)
    assert name == "x" and args == inp, args
    assert args is not None, "None 이면 `via: invoke_tool` 표시까지 사라진다"


def test_경유가_아니면_건드리지_않는다():
    for nm, inp in (("bend_profile", {"a": 1}),
                    ("someapp_invoke_tool", {"name": "x"}),   # 접두 붙은 다른 도구
                    ("invoke_tool", {"name": ""}),            # 안쪽 이름이 비었다
                    ("invoke_tool", "dict 가 아니다")):
        name, args = A._inner_tool(nm, inp)
        assert name == nm and args is None, (nm, name, args)

# 작은 모델의 도구호출 실패를 코드가 구제하는지 고정하는 회귀 테스트 — 안전망이 조용히 깨지는 것을 막는다.
#
# 여기 담긴 샘플은 전부 **실제로 관측된 출력**이거나 그 변형이다. 안전망은 모델 실력에
# 의존하지 않는 결정적 코드이므로, 모델 없이도 계약을 검증할 수 있다.
#   실행:  .venv/bin/python -m pytest tests/test_toolcall_recovery.py -q
#         (pytest 없으면)  .venv/bin/python tests/test_toolcall_recovery.py
import json
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"


def _load_recovery_fns() -> dict:
    """app.py 에서 안전망 함수 3종만 떼어 실행한다.

    app 전체를 import 하면 LLM·게이트웨이 연결이 필요해 테스트가 환경에 묶인다. 안전망은
    순수 함수(re/json 만 사용)라 소스에서 추출해도 동일하게 동작한다.
    """
    src = APP.read_text(encoding="utf-8")
    ns: dict = {"re": re, "json": json}
    blocks = [
        r"_LEAK_RE = .*?return bool\(_LEAK_RE\.search\(stripped\)\)",
        r"def _extract_leaked_calls.*?\n    return out",
        r"_ANNOUNCE_RE = .*?return bool\(text\) and bool\(_ANNOUNCE_RE\.search\(text\)\)",
    ]
    for pat in blocks:
        m = re.search(pat, src, re.S)
        assert m, f"app.py 에서 안전망 블록을 찾지 못했다: {pat[:40]}"
        exec(m.group(0), ns)  # noqa: S102 — 자기 레포 소스만 대상
    return ns


FNS = _load_recovery_fns()
leaked = FNS["_looks_like_leaked_tool_call"]
extract = FNS["_extract_leaked_calls"]
announced = FNS["_announced_without_calling"]


# ── 실제 관측된 실패 출력 ────────────────────────────────────────────────
# (라벨, 원문, 도구가_한번도_안돌았나, 기대: 구제경로)
CASES = [
    # dev qwen 7B 실측 — 특수토큰이 깨져 'leton' 파편 + 호출문 누출
    ("qwen-broken-token",
     'leton\n{"name": "get_dataset_summary", "arguments": {}}\n</tool_call>',
     True, "extract"),
    # 정상 형태의 누출
    ("plain-leak",
     '<tool_call>{"name":"query_voc","arguments":{"limit":10}}</tool_call>',
     True, "extract"),
    # dev qwen 7B 실측 — 예고만 하고 인자를 코드펜스에 찍고 종료(도구명 없음)
    ("announce-only",
     'SignalForge VOC에서 상위 이슈를 확인하겠습니다. 특정 제품 코드를 지정하지 않으면 '
     '전체 제품에서 가장 많이 언급된 이슈를 알려드리겠습니다.\n```\n{"product_code": "GS25U"}\n```',
     True, "forced-retry"),
    # 예고 + 코드펜스 안에 완전한 호출문 — 도구 0회면 펜스도 후보로 봐야 한다
    ("announce-with-fenced-call",
     '확인하겠습니다.\n```json\n{"name":"get_top_issues","arguments":{}}\n```',
     True, "extract"),
    # 영어 예고
    ("announce-english",
     "Let me check the dataset summary for you.",
     True, "forced-retry"),
    # Haiku 실측 — Anthropic 계열은 {"type":"tool_use","name":…,"input":…} 를 쓴다.
    # qwen 형식("arguments")만 알던 시절엔 이 출력이 통째로 안 잡혔다.
    ("haiku-tool_use-input",
     'VOC 상위 이슈 조회 중...\n\n<tool_call>\n{\n  "type": "tool_use",\n  "name": "get_top_issues",\n'
     '  "input": {\n    "product_code": null,\n    "limit": 10\n  }\n}\n</tool_call>',
     True, "extract"),
    # ── 오탐 금지(정상 응답) ────────────────────────────────────────────
    ("normal-answer",
     "열충격 데이터셋은 총 294건이며 SED 평균은 1.736입니다.",
     True, "none"),
    # 도구가 이미 돈 턴에서 설명용 JSON 예시를 보여주는 것은 누출이 아니다
    ("explanatory-json-after-tool",
     '조회했습니다. 필터를 좁히려면 이렇게 쓰세요:\n```json\n{"name":"query_voc","arguments":{"limit":50}}\n```',
     False, "none"),
]


def classify(text: str, no_tool_ran: bool) -> str:
    """안전망이 이 원문을 어느 경로로 구제하는지."""
    calls = extract(text, no_tool_ran) if leaked(text, no_tool_ran) else []
    if calls:
        return "extract"
    if no_tool_ran and announced(text):
        return "forced-retry"
    return "none"


def test_recovery_paths():
    fails = []
    for label, text, no_tool, want in CASES:
        got = classify(text, no_tool)
        if got != want:
            fails.append(f"{label}: 기대={want} 실제={got}")
    assert not fails, "안전망 계약 위반:\n  " + "\n  ".join(fails)


def test_extract_returns_usable_call():
    """복원된 호출은 그대로 실행 가능한 형태여야 한다(이름 + dict 인자)."""
    calls = extract('<tool_call>{"name":"query_voc","arguments":{"limit":10}}</tool_call>', True)
    assert calls, "호출을 복원하지 못했다"
    assert calls[0]["name"] == "query_voc"
    assert isinstance(calls[0]["arguments"], dict) and calls[0]["arguments"]["limit"] == 10


def test_supports_anthropic_shape():
    """Anthropic 계열의 tool_use/input 형식도 실행 가능한 호출로 복원해야 한다."""
    raw = ('<tool_call>\n{\n  "type": "tool_use",\n  "name": "get_top_issues",\n'
           '  "input": {"product_code": null, "limit": 10}\n}\n</tool_call>')
    calls = extract(raw, True)
    assert calls, "Anthropic 형식을 복원하지 못했다"
    assert calls[0]["name"] == "get_top_issues"
    assert calls[0]["arguments"].get("limit") == 10


def test_no_runaway_extraction():
    """한 턴에 과도한 호출을 복원하지 않는다(폭주 방지 상한)."""
    many = "".join(f'<tool_call>{{"name":"t{i}","arguments":{{}}}}</tool_call>' for i in range(10))
    assert len(extract(many, True)) <= 3


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  ✗ {name}\n    {exc}")
    print(f"\n  {'실패 ' + str(failed) + '건' if failed else '전부 통과'}")
    sys.exit(1 if failed else 0)

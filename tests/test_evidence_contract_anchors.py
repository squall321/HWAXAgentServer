# 근거 계약의 닻 — 본문 키의 순서·키 길이 상한·키 형식을 **글자 그대로** 못박는다(다른 리포가 이 값에 자기를 맞춘다)
#
# 근거 본문 키의 순서(_EVID_BODY_KEYS), 호출자 키의 길이 상한(_EVID_KEY_MAX)과 형식(_EVID_KEY_RE)은 세 곳이
# 같이 쓴다 — 이 엔진, MCP 길의 워크플로(HWAXPortal infra/pipeline/hwax-deliberate.js), 리스크 앱(HWAXRisk).
# 그쪽 시험들은 **이 엔진의 값을 읽어** 자기와 견준다(엔진이 오라클이다). 그런데 엔진 쪽에는 그 값을 붙드는 시험이
# 없었다 — 엔진에서 순서 둘을 바꾸거나 상한을 고치면 그쪽 시험은 '엔진과 다르다' 고만 말하고, 엔진의 시험은 전부
# 통과한다. 어느 쪽이 틀렸는지 알려 줄 닻이 없다. 일치 시험은 둘 다 틀리면 통과한다.
#
# 그래서 여기서는 엔진의 상수를 **읽지 않고** 손으로 적은 값과 견준다. 바꾸려면 이 파일과 그 두 리포를 같이 고친다.
#
#   실행:  .venv/bin/python -m pytest tests/test_evidence_contract_anchors.py -q
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import deliberation as d  # noqa: E402

# 손으로 적은 정본 — 엔진 상수에서 만들지 않는다(만들면 엔진이 틀려도 같이 틀린다).
_BODY_KEYS = ["result", "text", "content", "excerpt", "summary", "body", "output", "data"]
_KEY_MAX = 24
_KEY_PATTERN = "[A-Za-z0-9_.-]{1,24}"


def _body(item):
    """엔진이 그 항목에서 본문으로 실은 것 — 요청을 읽는 실제 길(_resolve_opts)로 본다."""
    (ev,) = d._resolve_opts({"evidence": [item]}).evidence
    return ev["result"]


# ── 본문 키의 순서 ───────────────────────────────────────────────────────────
def test_본문_키_목록이_글자_그대로다():
    assert list(d._EVID_BODY_KEYS) == _BODY_KEYS


@pytest.mark.parametrize("first,second", list(zip(_BODY_KEYS, _BODY_KEYS[1:])))
def test_이웃한_두_키가_함께_오면_앞선_키가_실린다(first, second):
    """순서가 우선순위다. 이웃한 쌍마다 본다 — 둘의 자리가 바뀌면 `{summary: 한 줄, body: 본문}` 같은 항목에서
    좌석이 받는 글이 달라지고, 다른 길(워크플로·리스크 앱)과 같은 근거를 다르게 싣는다.
    항목에는 뒤엣것을 먼저 적는다 — 사전에 적힌 순서를 따라가는 구현이면 여기서 걸린다."""
    assert _body({second: f"{second} 의 글", first: f"{first} 의 글"}) == f"{first} 의 글"
    # 앞선 키가 비어 있으면 뒤엣것으로 넘어간다(없는 것·빈 문자열·공백).
    for empty in ({}, {first: ""}, {first: "   "}):
        assert _body({**empty, second: f"{second} 의 글"}) == f"{second} 의 글", empty


def test_맨_앞_키는_나머지_전부를_이긴다():
    item = {k: f"{k} 의 글" for k in reversed(_BODY_KEYS)}
    assert _body(item) == "result 의 글"
    shadowed = d._resolve_opts({"evidence": [item]}).evidence_shadowed
    assert [k for k, _n in shadowed[0][3]] == _BODY_KEYS[1:], "뺀 키를 순서대로 적지 않았다"


def test_목록_밖의_키는_본문으로_읽지_않는다():
    o = d._resolve_opts({"evidence": [{"payload": "본문", "value": "본문", "description": "본문"}]})
    assert o.evidence == [] and o.evidence_dropped_empty == 1


# ── 호출자 키 ────────────────────────────────────────────────────────────────
def test_키_길이_상한은_24자다():
    assert d._EVID_KEY_MAX == _KEY_MAX
    ok, over = "K" * _KEY_MAX, "K" * (_KEY_MAX + 1)
    got = d._resolve_opts({"evidence": [{"key": ok, "result": "가"}, {"key": over, "result": "나"}]}).evidence
    assert [e["key"] for e in got] == [ok, ""], "24자는 받고 25자는 버린다"


def test_키_형식의_식이_글자_그대로다():
    assert d._EVID_KEY_RE.pattern == _KEY_PATTERN
    for good in ("E3", "E1-CH-015", "v1.2_rev-A", "0", "a.b_c-d"):
        assert d._EVID_KEY_RE.fullmatch(good), good
    for bad in ("", " E3", "E3 ", "E]3", "E|3", "E:3", "E/3", "키", "E3\n"):
        assert not d._EVID_KEY_RE.fullmatch(bad), repr(bad)


def test_인용_표지의_식은_키_형식을_그대로_품는다():
    """`[e:N]` 과 `[e:N|KEY]` — 키 형식이 바뀌면 표지를 읽는 식도 같이 바뀌어야 한다."""
    assert d._EV_CITE_RE.pattern == r"\[e:(\d+)(?:\|" + _KEY_PATTERN + r")?\]"
    text = f"가 [e:1] 나 [e:22|{'K' * _KEY_MAX}] 다 [e:3|{'K' * (_KEY_MAX + 1)}] 라 [e:4|나쁜 키]"
    assert [m.group(1) for m in d._EV_CITE_RE.finditer(text)] == ["1", "22"]


def test_다른_리포가_읽는_줄_모양이_그대로다():
    """리스크 앱의 시험은 엔진 **소스의 줄**을 정규식으로 읽는다(`^_EVID_KEY_MAX\\s*=\\s*(\\d+)\\s*$`). 줄 끝에
    주석을 달거나 식으로 바꾸면 그쪽이 못 찾는다. 포털의 시험은 모듈 최상위의 대입문 넷을 이름으로 찾는다."""
    src = (ROOT / "deliberation.py").read_text(encoding="utf-8")
    found = re.search(r"^_EVID_KEY_MAX\s*=\s*(\d+)\s*$", src, re.M)
    assert found and int(found.group(1)) == _KEY_MAX, found and found.group(0)
    for name in ("_EVID_BODY_KEYS", "_EVID_KEY_MAX", "_EVID_KEY_RE", "_EV_CITE_RE"):
        assert re.search(rf"^{name} = ", src, re.M), f"{name} 이 모듈 최상위의 대입문이 아니다"

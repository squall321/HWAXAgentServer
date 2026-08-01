# 답변의 '출처가 도구인지'를 지키는 안전망의 회귀 테스트 — 유령 ID 게이트와 자작 차트 탐지.
#
# 픽스처는 전부 라이브 /chat 재현에서 실제로 관측된 값이다(재료 4건, 2026-08-01).
#   · Al6061-T6  → id/test_id 19, E 68.9 GPa, UTS 398.26 MPa
#   · SUS304_annealed → id/test_id 3,  E 193.0 GPa, UTS 311.71 MPa
#   · SCM440_alloy_steel → id/test_id 12, E 205.0 GPa, UTS 1204.74 MPa
# 모델이 찍었던 값: test_id=1(→ SUS201_annealed 카드), material_id=12345(→ not found).
#
#   실행:  .venv/bin/python -m pytest tests/test_grounding.py -q
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app.py"


def _load() -> dict:
    """app.py 에서 순수 함수·정규식만 떼어 실행한다(모델·게이트웨이 불필요).

    app 전체를 import 하면 LLM·MCP 연결이 필요해 테스트가 환경에 묶인다."""
    src = APP.read_text(encoding="utf-8")
    ns: dict = {"re": re}
    blocks = [
        r"_ID_ARG_RE = .*?_TURN_IDS_MAX = \d+",
        r"def _int_tokens.*?\n    return \{int\(m\) for m in _INT_TOK_RE\.findall\(s\)\}",
        r"def _phantom_id_arg.*?\n    return None",
        r"_CHART_SURFACE_RE = .*?bool\(_ENTITY_RE\.search\(message or \"\"\)\)",
    ]
    for pat in blocks:
        m = re.search(pat, src, re.S)
        assert m, f"app.py 에서 블록을 찾지 못했다: {pat[:40]}"
        exec(m.group(0), ns)  # noqa: S102 — 자기 레포 소스만 대상
    return ns


FNS = _load()
int_tokens = FNS["_int_tokens"]
phantom = FNS["_phantom_id_arg"]
drew_own_chart = FNS["_drew_own_chart"]


# ── 정수 토큰 추출 ────────────────────────────────────────────────────────────
def test_int_tokens_는_부분수열을_잡지_않는다():
    """\\d+ 로 잡으면 'Al6061-T6' 안의 1 이 test_id=1 을 통과시켜 게이트가 무력해진다 —
    이 케이스가 정확히 SUS201 카드를 Al6061 로 둔갑시킨 사고다."""
    got = int_tokens("Al6061-T6 로 LS-DYNA 재료 카드 만들어줘")
    assert 6061 in got and 6 in got
    assert 1 not in got, "부분수열 1 이 잡히면 게이트가 최악 케이스를 못 막는다"


def test_int_tokens_는_문자열이_아니어도_처리한다():
    assert int_tokens(None) == set()
    assert 19 in int_tokens({"test_id": 19})


# ── 유령 ID 게이트 ────────────────────────────────────────────────────────────
def test_추측한_test_id_는_차단된다():
    """실측: get_mat_card(test_id=1) → SUS201_annealed 카드가 돌아왔다."""
    src = int_tokens("Al6061-T6 로 LS-DYNA 재료 카드 만들어줘")
    assert phantom({"test_id": 1, "units": "ton_mm_s"}, src) == ("test_id", 1)


def test_조회로_얻은_id_는_통과한다():
    """list_materials 결과에서 배운 19 는 출처가 있으므로 막히면 안 된다."""
    src = int_tokens("Al6061-T6 카드 만들어줘") | int_tokens('{"id": 19, "name": "Al6061-T6"}')
    assert phantom({"test_id": 19}, src) is None


def test_사용자가_직접_준_id_는_통과한다():
    """'잡 12345 로그 봐줘' 처럼 발화에 있는 ID 는 추측이 아니다."""
    assert phantom({"job_id": 12345}, int_tokens("잡 12345 로그 봐줘")) is None


def test_식별자가_아닌_인자는_대상이_아니다():
    """limit·top_k 는 ID 가 아니다 — 여기서 막으면 정상 조회가 전멸한다."""
    assert phantom({"limit": 10, "top_k": 40}, set()) is None


def test_bool_은_id_로_오인되지_않는다():
    """bool 은 int 의 서브클래스라 명시적으로 제외하지 않으면 compact=True 가 걸린다."""
    assert phantom({"compact": True, "valid_id": False}, set()) is None


def test_문자열_id_는_건드리지_않는다():
    """정수 추측만 대상이다 — 문자열 키는 fail-open."""
    assert phantom({"agent_id": "pcb-rigid-flex"}, set()) is None


# ── 자작 표·차트 탐지 ─────────────────────────────────────────────────────────
Q4_MSG = "SUS304_annealed 물성을 표로 정리하고 그래프로 비교해줘"
Q4_TEXT = (
    "| 물성 | 값 |\n|---|---|\n| 인장강도 | 600-700 MPa |\n\n"
    '```html\n<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>\n```'
)


def test_지목된_대상에_자작차트를_그리면_잡는다():
    """실측 Q4 — 도구 0회로 물성표 + Plotly CDN 차트를 지어냈다."""
    assert drew_own_chart(Q4_MSG, Q4_TEXT) is True


def test_사용자가_준_숫자로_그리는_자작은_정당하다():
    """DB 조회 대상이 아닌 질문까지 재시도를 걸면 '안 불러도 되는 걸 부르게 만드는' 회귀다."""
    assert drew_own_chart("우리 팀 인원 구성 비율을 도표로 그려줘", Q4_TEXT) is False


def test_차트_표면이_없으면_잡지_않는다():
    assert drew_own_chart(Q4_MSG, "SUS304_annealed 는 해당 도구가 없습니다.") is False


def test_포털_안내문은_잡지_않는다():
    msg = "포털 어떻게 쓰는 거야?"
    text = "① /tokens 에서 토큰을 발급합니다. ② claude mcp add --transport http hwax …"
    assert drew_own_chart(msg, text) is False


if __name__ == "__main__":  # pytest 없이도 돌릴 수 있게
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✓ {name}")
            except AssertionError as exc:
                fails += 1
                print(f"  ✗ {name}: {exc}")
    print(f"\n{'실패 ' + str(fails) + '건' if fails else '전부 통과'}")
    sys.exit(1 if fails else 0)

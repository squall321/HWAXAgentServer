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
        r"_NUM_TOK_RE = re\.compile\(r\"[^\n]*\"\)",
        r"def _sig_numbers.*?\n    return out",
        r"def _unsourced_numbers.*?\n    return bad",
        r"def _evidence_block.*?\n    return \"\\n\"\.join\(lines\)",
        r"_NUDGE_RE = re\.compile\(.*?\)\n",
        r"_INTERRUPTED_RE = re\.compile\(.*?\)\n",
        r"_NUDGE_MAX_LEN = \d+",
        r"def _is_nudge.*?\n    return bool\(s\)[^\n]*\n",
        r"def _resume_target.*?\n    return \(last_q, \(last_a or \"\"\)\.strip\(\)\)",
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
unsourced = FNS["_unsourced_numbers"]
evidence_block = FNS["_evidence_block"]
is_nudge = FNS["_is_nudge"]
resume_target = FNS["_resume_target"]


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


# ── 산문 속 호출문 예고 ────────────────────────────────────────────────────────
def _announced():
    src = APP.read_text(encoding="utf-8")
    ns: dict = {"re": re}
    m = re.search(r"_ANNOUNCE_RE = .*?return bool\(text\) and bool\(_ANNOUNCE_RE\.search\(text\)\)",
                  src, re.S)
    assert m, "app.py 에서 _ANNOUNCE_RE 블록을 찾지 못했다"
    exec(m.group(0), ns)  # noqa: S102
    return ns["_announced_without_calling"]


ANNOUNCED = _announced()


def test_산문_속_호출문_예고를_잡는다():
    """실측 Q3 — 게이트가 추측 ID 를 막자 모델이 호출문을 산문으로 쓰고 멈췄다.
    기존 _LEAK_RE 는 JSON/XML 형식만 봐서 이 형태를 놓쳤다."""
    assert ANNOUNCED('list_materials(query="Al6061-T6")를 호출하여 material_id를 얻어coming.') is True


def test_기존_예고_문구는_그대로_잡는다():
    assert ANNOUNCED("SCM440_alloy_steel의 물성을 확인하겠습니다.") is True


def test_기능_없음_안내는_예고가_아니다():
    """G2 가드 — 여기서 재시도가 돌면 '없다'는 정답을 뒤집을 수 있다."""
    assert ANNOUNCED("CFD 유동해석 관련 기능은 없습니다. 외부 소프트웨어를 사용해야 합니다.") is False


def test_호출_결과_인용은_예고가_아니다():
    """괄호가 있어도 '호출/사용/실행' 의사가 없으면 이미 부른 결과를 인용한 것이다."""
    assert ANNOUNCED("get_material(material_id=12) 결과에 따르면 E는 205 GPa 입니다.") is False


def test_포털_안내는_예고가_아니다():
    assert ANNOUNCED("포털 상단 'API 토큰' 메뉴(/tokens)에서 토큰을 발급합니다.") is False


# ── 확정 종결: 예고문 속 도구 이름 복원 ───────────────────────────────────────
def _mentioned():
    src = APP.read_text(encoding="utf-8")
    ns: dict = {"re": re}
    m = re.search(r"def _mentioned_tools.*?\n    return out", src, re.S)
    assert m, "app.py 에서 _mentioned_tools 를 찾지 못했다"
    exec(m.group(0), ns)  # noqa: S102
    return ns["_mentioned_tools"]


MENTIONED = _mentioned()
NAMES = ["list_materials", "get_material", "get_mat_card", "query_voc", "search_reports"]


def test_예고문에서_정확한_도구명을_찾는다():
    t = "먼저 list_materials(query=\"Al6061\")를 호출하여 재료를 찾겠습니다."
    assert MENTIONED(t, NAMES) == ["list_materials"]


def test_오타는_유일_근접일_때만_교정한다():
    assert MENTIONED("이제 list_material 을 조회하겠습니다.", NAMES) == ["list_materials"]


def test_도구명_없는_예고문은_빈_목록():
    assert MENTIONED("확인해 보겠습니다.", NAMES) == []


def test_일반_한국어_영단어를_도구로_오인하지_않는다():
    assert MENTIONED("performance 개선을 확인하겠습니다.", NAMES) == []


# ── 자유 조회 화이트리스트 ────────────────────────────────────────────────────
def _free_ok():
    src = (APP.parent / "deliberation.py").read_text(encoding="utf-8")
    ns: dict = {}
    for pat in (r"_FREE_ALLOW = \([^)]*\)", r"_FREE_DENY = \([^)]*\)",
                r"def _free_tool_ok.*?startswith\(_FREE_ALLOW\)"):
        m = re.search(pat, src, re.S)
        assert m, f"deliberation.py 에서 블록을 찾지 못했다: {pat[:30]}"
        exec(m.group(0), ns)  # noqa: S102
    return ns["_free_tool_ok"]


FREE_OK = _free_ok()


def test_읽기_전용_도구는_허용():
    for n in ("list_materials", "get_material_properties", "compute_abd_matrix",
              "search_by_property", "plot_curve", "hybrid_search", "agent_search"):
        assert FREE_OK(n), n


def test_쓰기·부작용_도구는_전부_차단():
    """deny-by-default — 화이트리스트 접두사에 없으면 무조건 닫힌다."""
    for n in ("register_material", "update_material", "create_report_draft", "delete_session",
              "upload_kfile", "slurm_submit_job", "run_operation", "train_model",
              "save_conversation", "meeting_start", "render_submit", "bind_records_to_agent"):
        assert not FREE_OK(n), n


def test_페르소나_세션_원문은_조회_근거가_아니다():
    assert not FREE_OK("get_agent_session")


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


# ── 답변 수치 대조 ────────────────────────────────────────────────────────────
# 도구를 부르고도 답변이 그 범위를 넘어서는 경우를 잡는 안전망. 유령 ID 게이트가 도구 '인자'를
# 막는다면 이쪽은 답변 '본문'을 본다. 과다 경고는 기능을 죽이므로 오탐 억제가 설계의 핵심이다.
TOOL_OUT = '{"name":"Al6061-T6","yield_mpa":276.0,"E_gpa":68.9,"id":1234,"count":12}'


def test_도구_값을_그대로_인용하면_경고하지_않는다():
    assert unsourced("항복 276.0 MPa, 탄성계수 68.9 GPa 입니다.", TOOL_OUT) == []


def test_도구에_없는_수치는_잡는다():
    """이 경우가 '확신에 찬 오답' 이다 — 조회는 했는데 답이 조회 밖으로 나간 것."""
    assert unsourced("항복강도는 310.5 MPa 입니다.", TOOL_OUT) == ["310.5"]


def test_작은_정수와_백분율은_잡지_않는다():
    """개수·순번·백분율은 정상 생성값이다. 여기까지 경고하면 아무도 표시를 안 본다."""
    assert unsourced("재료 3종 중 2번째, 총 12개이며 약 45% 입니다.", TOOL_OUT) == []


def test_천단위_콤마_표기를_같은_값으로_본다():
    """모델은 21,279 로 쓰고 도구는 21279 로 준다 — 표기 차이를 날조로 보면 안 된다."""
    assert unsourced("물성값은 21,279 건입니다.", '{"property_values":21279}') == []


def test_사용자가_준_수치는_출처로_인정한다():
    assert unsourced("두께 2.5mm 기준입니다.", TOOL_OUT + "\n두께 2.5mm 로 계산해줘") == []


def test_도구_출력이_없으면_판정하지_않는다():
    """도구 0회 턴은 기존 '도구 미조회' 경고가 담당한다 — 여기서 중복 경고하지 않는다."""
    assert unsourced("아무 값 999.9", "") == []


def test_근거_블록은_도구_호출이_있을_때만_만든다():
    assert evidence_block([], []) == ""
    out = evidence_block([("list_materials", "query=Al6061")], [])
    assert "list_materials" in out and "근거" in out


def test_근거_블록에_출처없는_수치가_실린다():
    out = evidence_block([("get_material", "id=1234")], ["310.5"])
    assert "310.5" in out and "확인되지 않았습니다" in out


# ── 재촉 → 이어하기 ───────────────────────────────────────────────────────────
# 응답이 끊긴 뒤의 "야! 하라니까!" 는 내용이 없어, 그대로 넘기면 모델이 새 질문으로 읽는다.
# 오판(진짜 새 질문을 재촉으로 봄)이 재촉을 놓치는 것보다 나쁘므로 판정을 좁게 잡는다.
def test_짧은_재촉을_잡는다():
    for s in ("야! 하라니까!", "계속", "ㄱㄱ", "왜 안해?", "continue", "다시"):
        assert is_nudge(s), s


def test_내용이_있으면_재촉이_아니다():
    """'계속해서 …' 처럼 접두만 같은 새 질문을 재촉으로 보면 원래 질문이 통째로 무시된다."""
    for s in ("계속해서 배터리 스웰링 원인을 알려줘", "야 그런데 이거 말고 다른 재료는?",
              "Al6061-T6 물성 알려줘", ""):
        assert not is_nudge(s), s


def test_끊긴_턴만_이어한다():
    interrupted = [{"role": "user", "content": "Al6061 물성 알려줘"},
                   {"role": "assistant", "content": "조회하겠습니다"}]
    q, _ = resume_target(interrupted)
    assert q == "Al6061 물성 알려줘"


def test_내부오류_중단도_이어한다():
    h = [{"role": "user", "content": "낙하 해석 돌려줘"},
         {"role": "assistant", "content": "부분…\n\n⚠ 처리 중 내부 오류로 응답이 여기서 중단되었습니다."}]
    q, part = resume_target(h)
    assert q == "낙하 해석 돌려줘" and part


def test_빈_응답도_이어한다():
    h = [{"role": "user", "content": "물성표 만들어줘"}, {"role": "assistant", "content": ""}]
    assert resume_target(h)[0] == "물성표 만들어줘"


def test_정상_답변_뒤에는_이어하지_않는다():
    """정상 종료 뒤의 '계속' 은 '더 말해달라' 는 새 요구다 — 원래 질문을 다시 돌리면 안 된다."""
    h = [{"role": "user", "content": "물성 알려줘"},
         {"role": "assistant", "content": "Al6061-T6 의 항복강도는 276 MPa 입니다. 자세한 값은…"}]
    assert resume_target(h) == (None, None)

# 붙인 문서 주입(_doc_block) 회귀 테스트 — 예산은 건수로 나누고, 자른 건 잘랐다고 말한다
#
#   실행:  .venv/bin/python -m pytest tests/test_doc_attach.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402


def _doc(name, chars):
    return {"name": name, "text": "가" * chars}


def test_붙인_문서가_없으면_빈_문자열():
    assert app._doc_block(None) == ""
    assert app._doc_block([]) == ""


def test_글자_없는_항목은_버린다():
    """빈 문서를 근거라고 실으면 좌석·모델이 '자료는 있는데 내용이 없다' 로 읽는다."""
    assert app._doc_block([{"name": "빈것", "text": "   "}]) == ""
    assert app._doc_block(["문자열은 문서가 아니다"]) == ""


def test_상한_안이면_본문이_그대로_실린다():
    out = app._doc_block([_doc("설계검토.hwax.md", 5000)])
    assert "가" * 5000 in out
    assert "설계검토.hwax.md" in out
    # 자름 표시는 **문서 머리말**에만 나와야 한다. 규율 문구에도 같은 말이 나오므로
    # 전체 문자열로 보면 안 된다(이 테스트가 처음에 거기서 틀렸다).
    assert ", 이 중 앞" not in out


def test_예산은_건수로_균등_분배된다():
    """앞 문서가 예산을 다 먹으면 사용자는 두 건을 붙였는데 답은 한 건만 본 채로 나온다.

    그 실패는 화면에 아무 흔적도 안 남는다 — 그래서 코드가 몫을 나눈다."""
    big = app.DOC_TOTAL_CHARS * 2
    out = app._doc_block([_doc("앞.md", big), _doc("뒤.md", big)])
    assert "앞.md" in out and "뒤.md" in out, "뒤 문서가 통째로 사라졌다"
    share = app.DOC_TOTAL_CHARS // 2
    # 두 문서 모두 같은 몫만큼 실린다(합계가 예산 근처, 한쪽으로 쏠리지 않는다).
    assert out.count("가" * share) == 2


def test_잘랐으면_잘랐다고_적는다():
    """모델이 '뒷부분을 못 봤다'는 걸 알아야 지어내지 않고 되물을 수 있다."""
    out = app._doc_block([_doc("긴문서.md", app.DOC_TOTAL_CHARS + 1000)])
    assert "만 실림" in out
    assert f"{app.DOC_TOTAL_CHARS + 1000:,}자" in out   # 원문 길이를 밝힌다


def test_건수_상한을_넘으면_자른다():
    out = app._doc_block([_doc(f"d{i}.md", 100) for i in range(app.DOC_MAX_FILES + 3)])
    assert f"d{app.DOC_MAX_FILES - 1}.md" in out
    assert f"d{app.DOC_MAX_FILES}.md" not in out


def test_근거_규율이_함께_실린다():
    """문서만 주고 규율을 안 주면 모델이 기억으로 메운다 — 이 허브에서 가장 위험한 실패다."""
    out = app._doc_block([_doc("a.md", 10)])
    assert "여기 적힌 것만" in out          # 기억으로 채우지 마라
    assert "지어내지 마라" in out           # 못 본 구간을 만들어내지 마라
    assert "어긋난다고 말하라" in out       # 사전 지식과 충돌하면 조용히 덮지 마라


def test_핸드오프_발췌가_심의_예산에_맞다():
    """심의 예산(_EVID_BUDGET)만 올리고 발췌 길이를 안 올리면, 예산은 크고 실제로 실리는
    건 그대로다 — 종전에 정확히 그 상태였다(11KB 예산에 2.6KB 만 도착)."""
    import deliberation as d

    assert app.HANDOFF_RESULT_CHARS * d._EVID_ITEMS >= d._EVID_BUDGET, (
        f"발췌 {app.HANDOFF_RESULT_CHARS}자 × {d._EVID_ITEMS}건 = "
        f"{app.HANDOFF_RESULT_CHARS * d._EVID_ITEMS:,}자로 예산 {d._EVID_BUDGET:,}자를 못 채운다"
    )

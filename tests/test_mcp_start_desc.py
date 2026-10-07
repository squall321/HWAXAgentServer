# deliberate_start 도구 설명이 엔진 상수와 어긋나지 않는지 — MCP 호출자가 **실제로 받는** 설명을 본다
#
# 종전 설명은 근거 상한을 "최대 40, 항목당 12,000자" 로 손으로 적어 두었는데 엔진 상수는
# 150,000 이었다. 실사용 팀이 그 글을 믿고 제 근거를 미리 잘라 92% 를 버렸다(2026-10-07).
# 숫자를 고쳐 적으면 같은 일이 또 난다 — 설명을 상수에서 읽어 만들고, 여기서는 도구 목록
# (tools/list)이 내주는 글에 지금 값이 들어 있는지 본다.
#
#   실행:  .venv/bin/python -m pytest tests/test_mcp_start_desc.py -q
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_context():
    """모델 컨텍스트를 고정한다(tests/test_doc_attach 와 같은 이유 — 안 하면 /v1/models 를 기다린다)."""
    saved = dict(app._ctx_cache)
    app._ctx_cache["n"] = 128000
    d._evid_cache.clear()
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


def _listed(name: str) -> str:
    """tools/list 가 내주는 설명 — 독스트링이 아니라 클라이언트가 받는 글이다."""
    tools = asyncio.run(m.mcp.list_tools())
    return next(t.description for t in tools if t.name == name)


def test_근거_상한이_엔진_상수_값_그대로_적혀_있다():
    desc = _listed("deliberate_start")
    for label, want in (("건수 상한", f"{d._EVID_ITEMS}건"),
                        ("항목당 천장", f"{d._EVID_ITEM_MAX:,}자"),
                        ("합계 천장", f"{d._EVID_BUDGET:,}자")):
        assert want in desc, f"{label} {want} 이 설명에 없다 — 상수와 설명이 어긋났다"


def test_낡은_숫자가_설명에도_독스트링에도_없다():
    """독스트링은 클라이언트에 안 가지만 소스를 읽는 사람·에이전트가 믿는다 — 거기 남으면 또 낡는다."""
    assert "12,000" not in _listed("deliberate_start")
    assert "12,000" not in (m.deliberate_start.__doc__ or "")


def test_본문_키와_자르지_말라는_안내가_있다():
    desc = _listed("deliberate_start")
    assert "`result`" in desc
    for k in d._EVID_BODY_KEYS[1:]:
        assert k in desc, f"폴백 키 {k} 가 설명에 없다"
    assert "미리 자르지 마라" in desc and "무엇을 뺐는지" in desc
    assert "합계 예산" in desc and "통째로 빠지" in desc    # 먼저 걸리는 것은 항목 천장이 아니라 합계다
    assert "`source`" in desc and "[e:N]" in desc          # 호출자 표식이 어디에 찍히는지
    assert "evidence_omitted" in desc                       # 버려진 것을 어디서 보는지


def test_설명은_상수를_따라가고_로드_때_예산을_재지_않는다():
    """상수를 바꾸면 설명이 따라 바뀐다. 그리고 **모듈 로드 때 합계 예산을 부르지 않는다** —
    app.py 가 mcp_server 를 반쯤 import 된 채로 부르므로, 그때 재면 조회가 실패하고 엔진이
    폴백 컨텍스트를 프로세스 내내 캐시한다(dev 16K 창에서 좌석이 전원 400 으로 죽는다)."""
    code = (
        "import asyncio, app, deliberation as d, mcp_server as m\n"
        "assert 'pre' not in d._evid_cache and 'n' not in app._ctx_cache, '로드 때 예산을 쟀다'\n"
        "t = next(t for t in asyncio.run(m.mcp.list_tools()) if t.name == 'deliberate_start')\n"
        "print(t.description)\n")
    env = {**os.environ, "DELIB_EVID_ITEMS": "77", "DELIB_EVID_ITEM_MAX": "33333",
           "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-800:]
    assert "77건" in r.stdout and "33,333자" in r.stdout, r.stdout[-600:]


def test_지금_걸리는_상한은_메뉴가_부를_때_재서_준다():
    """합계 예산은 모델 컨텍스트에서 유도된다 — 128K 창이면 항목당 천장보다 훨씬 작다.
    천장만 적어 두면 호출자가 이번엔 반대로 틀린다."""
    lim = asyncio.run(m.deliberate_jobs())["limits"]
    assert lim["evidence_items"] == d._EVID_ITEMS
    assert lim["evidence_total_chars"] == d._evid_budget()
    assert lim["evidence_item_chars"] == min(d._EVID_ITEM_MAX, d._evid_budget())
    assert lim["evidence_item_chars"] < d._EVID_ITEM_MAX, "시험 전제 — 128K 창에서는 예산이 천장보다 작다"
    assert "limits" in _listed("deliberate_start")


def test_voc_와_chair_template_이_advanced_안내에_있다():
    help_adv = asyncio.run(m.deliberate_jobs())["options"]["advanced"]
    for text in (_listed("deliberate_start"), help_adv, m.deliberate_start.__doc__ or ""):
        assert "voc" in text and "chair_template" in text, text[-300:]
    for text in (_listed("deliberate_start"), help_adv):
        for want in ("auto", "off", "always", "이슈", "품질", "소급"):
            assert want in text, want
        for tpl in d._CHAIR_ITEMS:
            assert tpl in text, f"의장 틀 {tpl} 이 안내에 없다"


def test_안내한_손잡이가_실제로_엔진까지_간다():
    o = d._resolve_opts(m._build_opts(advanced={"voc": "off", "chair_template": "mechanism"}))
    assert (o.voc, o.chair_template) == ("off", "mechanism")

# 기동 절차를 연 시험이 심의 MCP 의 앱 자리(mcp_server._APP)를 뒤 시험에 남기지 않는지 — 보고된 시험 순서를 새 pytest 프로세스로 그대로 돌려 본다
#
# 11-01 보고 — tests/test_external_llm_review2_fixes.py 를 tests/test_human_note_cap.py::test_잘린_것이_MCP_호출자에게도_보인다 바로 앞에 돌리면
# 그 시험이 mcp_server._quiet_ok_s 의 OverflowError 로 붉었다(파일 순서에 따라 test_delib_silent_drops 의 진행 조회 시험들도 같았다). 앞 파일의
# 기동 시험이 DELIB_LLM_MAX_RETRIES 를 셈할 수 없을 만큼 크게(9…9 400자리) 걸고 app.lifespan 을 열었다 — 기동 절차의 mcp_server.bind 가 건 앱이
# 시험이 끝나도 남아, 뒤 시험의 진행 조회가 그 앱의 심의 LLM(시도 횟수 10^400)을 읽었다. 고친 것 둘 — 루트 conftest 가 시험마다 그 자리를
# 되돌린다(_no_leaked_mcp_app), _quiet_ok_s 는 셈할 수 없는 한도를 null 로 둔다(tests/test_delib_job_idle.py 가 본다). 이 시험은 앞의 것을 본다 —
# 둘 중 하나만 있어도 보고된 시험은 푸르므로, 맨 끝에 앱 자리가 비었는지 보는 탐침을 함께 돌린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_mcp_app_isolation.py -q
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_PROBE = "HWAX_TEST_APP_LEAK_PROBE"      # 아래 탐침을 켜는 표지 — 순서 시험이 새 프로세스로 부를 때만 단다


@pytest.mark.skipif(os.environ.get(_PROBE) != "1", reason="순서 시험이 새 pytest 프로세스로 부를 때만 돈다")
def test_탐침_앞_시험들이_건_앱이_남지_않았다():
    import mcp_server  # noqa: PLC0415

    assert mcp_server._APP is None, f"앞 시험이 건 앱이 남았다 — {mcp_server._APP!r}"


def test_기동_절차를_연_시험_파일_뒤에_진행_조회_시험을_돌려도_붉지_않고_앱_자리가_남지_않는다():
    probe = f"{Path(__file__).relative_to(ROOT)}::test_탐침_앞_시험들이_건_앱이_남지_않았다"
    nodes = ["tests/test_external_llm_review2_fixes.py", "tests/test_human_note_cap.py::test_잘린_것이_MCP_호출자에게도_보인다", probe]
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rp", "-p", "no:cacheprovider", "-p", "no:randomly", *nodes],
                       cwd=str(ROOT), env={**os.environ, _PROBE: "1"}, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    assert f"PASSED {probe}" in r.stdout, "탐침이 돌지 않았다 — " + r.stdout[-1500:]

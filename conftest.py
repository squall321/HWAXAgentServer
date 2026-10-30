# 시험이 실 잡 원장·비우기 깃발에 쓰지 못하게 두 경로(DELIB_JOB_DIR·DRAIN_STATE_PATH)를 시험 세션 내내 임시 디렉터리로 돌려 두고, 셸이 내보낸 외부 응답자 값(EXTERNAL_LLM_*) · 호출 추적 스위치(CALL_TRACE_URL) · 프롬프트 앞부분 고정 손잡이(CHAT_STABLE_PREFIX)를 지우며, 챗 자기검증(CHAT_SELF_CHECK) · 확인 관문(CHAT_CONFIRM_GATE)을 끈다, 형제 포털 리포의 코어 어휘표 · 운영자 정의(확인 관문 목록)는 시험마다 '없음' 으로 둔다
"""시험마다 `delib_jobs.JOB_DIR` 을 임시 경로로 바꿔 끼우지만 그건 **시험이 끝나면 풀린다.**

끝난 뒤에 도는 뒷정리가 하나라도 있으면(버려진 심의 태스크의 finally 가 그렇다) 그 기록은 모듈의 기본
경로에 쓰인다. 기본 경로는 실 원장(/data 쪽 delib-jobs)이라, 시험 기록이 운영 중인 서버의 심의 목록에 뜬다 —
2026-10-07 에 실제로 한 건이 그렇게 쓰였다(대기열 시험이 깨진 채로 끝났을 때).

모듈이 읽는 기본 경로 자체를 임시 디렉터리로 돌려 두면, 바꿔 끼운 것이 풀린 뒤에도 실 원장에는 닿지 않는다.
"""
import atexit
import os
import shutil
import sys
import tempfile
from collections import OrderedDict

import pytest

_JOB_DIR = tempfile.mkdtemp(prefix="hwax-delib-jobs-test-")
os.environ["DELIB_JOB_DIR"] = _JOB_DIR      # delib_jobs 가 import 될 때 읽는다 — 시험 모듈보다 먼저 돈다
atexit.register(shutil.rmtree, _JOB_DIR, ignore_errors=True)

# 비우기 깃발(review_gate.DrainState)도 같은 까닭으로 돌린다. 기본 경로는 <ARTIFACT_DIR>/review-drain.json 이고 ARTIFACT_DIR 의
# 기본값은 실 /data 를 가리키는 심링크다 — 시험이 세운 깃발이 떠 있는 서버의 카드 대조를 멈추면 안 된다(읽기도 실 깃발에 흔들린다).
_DRAIN_DIR = tempfile.mkdtemp(prefix="hwax-drain-test-")
os.environ["DRAIN_STATE_PATH"] = os.path.join(_DRAIN_DIR, "review-drain.json")
atexit.register(shutil.rmtree, _DRAIN_DIR, ignore_errors=True)

# 셸이 내보낸 외부 응답자 값(EXTERNAL_LLM_ENABLED 와 그 손잡이)을 지운다 — 스위치를 켠 셸에서 돌리면 import 때 과제 도구가 등록되고 꺼진
# 서버를 전제한 시험이 붉어지고 전체 시험이 끝나지 않았다(검토 2라운드). 켜야 하는 시험은 제 손으로 켠다(monkeypatch).
# EXTERNAL_LLM_CONTRACT_UPDATE 는 서버 설정이 아니라 계약 예제를 다시 뜨라는 시험 손잡이라 남긴다.
for _k in [k for k in os.environ if k.startswith("EXTERNAL_LLM_") and k != "EXTERNAL_LLM_CONTRACT_UPDATE"]:
    del os.environ[_k]

# 호출 추적 스위치(CALL_TRACE_URL)도 같은 까닭으로 지운다 — 켠 셸에서 돌리면 시험의 LLM·도구 호출 기록이 그 셸이 가리키는 Phoenix 로 간다.
# 켜야 하는 시험은 제 손으로 켠다(tests/test_call_trace.py).
os.environ.pop("CALL_TRACE_URL", None)

# 프롬프트 앞부분 고정 손잡이(CHAT_STABLE_PREFIX)도 같은 까닭으로 지운다 — app 이 import 때 한 번 읽는 값이라, 박스 우회 값(=0, HWAXPortal
# docs/request_261030.md OB-91)을 내보낸 셸에서 돌리면 기본 꼴(안내는 사람 발화 머리)을 보는 W-3 · HA-26 시험이 붉어진다. 끈 꼴을 보는 시험은
# 제 손으로 app._STABLE_PREFIX 를 바꾼다(tests/test_request_1029.py).
os.environ.pop("CHAT_STABLE_PREFIX", None)

# 챗 자기검증(CHAT_SELF_CHECK — 기본 켬)은 시험에서 끈다 — 챗 턴 시험들이 LLM 호출 수 · 흘린 글을 그대로 센다. 검증을 보는 시험은 제 손으로
# 켠다(tests/test_chat_self_check.py · HWAXPortal docs/chat-self-check).
os.environ["CHAT_SELF_CHECK"] = "0"

# 챗 상태 기다림(CHAT_STATUS_WAIT_MAX_S — 기본 600초)도 시험에서 끈다 — 켜 두면 상태 도구가 '도는 중' 을 준 턴 시험이 10초씩 자고, 되풀이 꼬리 ·
# 반복 경고를 보는 시험의 글이 바뀐다. status_wait 가 import 때 한 번 읽는 값이다. 기다림을 보는 시험은 제 손으로 켠다(모듈 속성 —
# tests/test_status_wait.py · HWAXPortal docs/request-1030 HA-28 · design-integration C-5).
os.environ["CHAT_STATUS_WAIT_MAX_S"] = "0"

# 확인 관문(CHAT_CONFIRM_GATE — 기본 켬, app 이 import 때 한 번 읽는다)도 같은 까닭으로 끈다 — 챗 턴 시험들이 잡 · 쓰기 꼴 도구 대역(run_job ·
# submit_job …)을 부르고 호출 수 · 흘린 글을 센다. 관문을 보는 시험은 제 손으로 켠다(app.CHAT_CONFIRM_GATE — tests/test_confirm_gate.py ·
# HWAXPortal docs/request-1030 design-integration C-5).
os.environ["CHAT_CONFIRM_GATE"] = "0"


@pytest.fixture(autouse=True)
def _no_core_vocab(monkeypatch, tmp_path):
    """코어 어휘표 격리 — 기본은 '없음'(코어 규칙 기본 글)이다. 형제 리포의 실제 표를 읽는 시험은 본문에서 app.CORE_VOCAB_PATH 를 다시 건다
    (tests/test_request_1029.py). 안 걸면 포털 리포의 표가 챗 시스템 프롬프트를 바꾸고, 모듈 상태라 앞 시험의 것이 뒤 시험에 남는다
    (HWAXPortal docs/request-1029 C-11). app 을 import 하지 않는 시험은 건드리지 않는다."""
    app_mod = sys.modules.get("app")
    if app_mod is not None and hasattr(app_mod, "CORE_VOCAB_PATH"):
        monkeypatch.setattr(app_mod, "CORE_VOCAB_PATH", tmp_path / "no-core-vocab.json")
        # 박스 덧씌우기도 늘 '없음' — 실제 표를 읽는 시험에도 박스의 core_vocab.local.json 이 끼어들지 않게 한다(E-7 ②)
        monkeypatch.setattr(app_mod, "CORE_VOCAB_LOCAL_PATH", tmp_path / "no-core-vocab.local.json")
        monkeypatch.setattr(app_mod, "_CORE_VOCAB", {"mtime": None, "data": None, "error": "없음", "overlay": None})


@pytest.fixture(autouse=True)
def _no_confirm_lists(monkeypatch, tmp_path):
    """확인 관문의 운영자 정의(형제 포털 리포 infra/personas/he-team.json) 격리 — 기본은 '없음'(잡 시작 바닥 목록만)이다. 목록을 보는 시험은 본문에서
    app.CONFIRM_LISTS_PATH 를 다시 건다(tests/test_confirm_gate.py). 안 걸면 포털 리포의 목록이 관문 판정을 바꾸고, 읽은 목록 · 카드 저장분은 모듈
    상태라 앞 시험의 것이 뒤 시험에 남는다(코어 어휘표 _no_core_vocab 와 같은 꼴 — HWAXPortal docs/request-1030 design-integration C-5)."""
    app_mod = sys.modules.get("app")
    if app_mod is not None and hasattr(app_mod, "CONFIRM_LISTS_PATH"):
        monkeypatch.setattr(app_mod, "CONFIRM_LISTS_PATH", tmp_path / "no-he-team.json")
        monkeypatch.setattr(app_mod, "_CONFIRM_LISTS", {"key": None, "writes": frozenset(), "confirm": frozenset(), "error": "없음"})
        monkeypatch.setattr(app_mod, "_CONFIRM_PENDING", OrderedDict())

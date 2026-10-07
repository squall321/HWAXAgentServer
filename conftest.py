# 시험이 실 잡 원장에 쓰지 못하게, 원장 경로(DELIB_JOB_DIR)를 시험 세션 내내 임시 디렉터리로 돌려 둔다
"""시험마다 `delib_jobs.JOB_DIR` 을 임시 경로로 바꿔 끼우지만 그건 **시험이 끝나면 풀린다.**

끝난 뒤에 도는 뒷정리가 하나라도 있으면(버려진 심의 태스크의 finally 가 그렇다) 그 기록은 모듈의 기본
경로에 쓰인다. 기본 경로는 실 원장(/data 쪽 delib-jobs)이라, 시험 기록이 운영 중인 서버의 심의 목록에 뜬다 —
2026-10-07 에 실제로 한 건이 그렇게 쓰였다(대기열 시험이 깨진 채로 끝났을 때).

모듈이 읽는 기본 경로 자체를 임시 디렉터리로 돌려 두면, 바꿔 끼운 것이 풀린 뒤에도 실 원장에는 닿지 않는다.
"""
import atexit
import os
import shutil
import tempfile

_JOB_DIR = tempfile.mkdtemp(prefix="hwax-delib-jobs-test-")
os.environ["DELIB_JOB_DIR"] = _JOB_DIR      # delib_jobs 가 import 될 때 읽는다 — 시험 모듈보다 먼저 돈다
atexit.register(shutil.rmtree, _JOB_DIR, ignore_errors=True)

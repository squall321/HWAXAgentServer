# 이어하기 재심사 요청 단위 스위치 — 사람이 명단을 고르면 재심사가 좌석을 몰래 더 얹지 않는다
#
#   실행:  .venv/bin/python -m pytest tests/test_rescreen_opt.py -q
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deliberation as d  # noqa: E402


def test_기본은_환경변수_값이다():
    assert d._resolve_opts({}).rescreen == (1 if d._RESCREEN else 0)


def test_요청으로_끄고_켠다():
    assert d._resolve_opts({"rescreen": 0}).rescreen == 0
    assert d._resolve_opts({"rescreen": 1}).rescreen == 1
    assert d._resolve_opts({"rescreen": "0"}).rescreen == 0


def test_이상한_값은_켜짐으로_접는다():
    assert d._resolve_opts({"rescreen": 7}).rescreen == 1


def test_재심사_분기가_요청값을_읽는다():
    # 환경변수 상수를 직접 읽으면 요청 스위치가 죽은 토글이 된다(①에서 본 모양 그대로).
    src = Path(d.__file__).read_text(encoding="utf-8")
    assert "if opts.rescreen and (opts.continue_summary or opts.human_note):" in src
    assert "if _RESCREEN and (opts.continue_summary" not in src

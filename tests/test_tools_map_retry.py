# 게이트웨이 /tools-map 조회가 실패하면 잠시 다시 묻지 않는지 — 매달린 게이트웨이를 호출마다 5초씩 기다리지 않는다
#
# 이 조회는 동기 urlopen(5초 한도)이라 그 동안 이벤트 루프가 통째로 멈춘다. 성공만 캐시하고 실패는 캐시하지
# 않아서, 게이트웨이가 매달리면 도구 수 × 좌석 수만큼 5초씩 다시 물었다 — 좌석별 도구 배정 한 번에 수백 번이다.
# 그 동안은 심의의 heartbeat(ping)도 못 나가 바깥에서는 서버가 죽은 것처럼 보인다.
#
#   실행:  .venv/bin/python -m pytest tests/test_tools_map_retry.py -q
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app as a  # noqa: E402


class _Gateway:
    """urlopen 대역 — 죽어 있으면 조금 기다렸다가 시간 초과로 끝나고, 살아 있으면 지도를 준다."""

    def __init__(self, monkeypatch):
        self.calls, self.up = 0, False
        monkeypatch.setattr(urllib.request, "urlopen", self)
        # 빈 캐시에서 시작한다(다른 시험이 채워 둔 것·실패 표식이 새지 않게).
        monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": 0.0, "map": {}, "areas": {}, "area_meta": {}})
        monkeypatch.setattr(a, "_APPS_CACHE", {"apps": {}, "at": 0.0})

    def __call__(self, url, timeout=None):
        self.calls += 1
        assert url.endswith("/tools-map") and timeout == 5, (url, timeout)
        if not self.up:
            time.sleep(0.05)                    # 실제로는 5초다
            raise TimeoutError("timed out")
        body = {"map": {"list_materials": "heax-materialtwin_web"}, "areas": {"list_materials": "material"},
                "apps": [{"app": "heax-materialtwin_web", "label": "재료 물성"}]}
        return io.BytesIO(json.dumps(body).encode())


@pytest.fixture
def gw(monkeypatch):
    return _Gateway(monkeypatch)


def _later(monkeypatch, seconds):
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + seconds)


def test_실패한_뒤에는_60초_동안_다시_묻지_않는다(gw, monkeypatch, capsys):
    names = [f"tool_{i}" for i in range(200)]       # 좌석별 도구 배정이 이렇게 도구마다 묻는다
    t0 = time.monotonic()
    assert [a._group_of(n) for n in names] == [("", "")] * 200
    took = time.monotonic() - t0
    assert gw.calls == 1, f"실패를 캐시하지 않아 {gw.calls}번 물었다 — 종전엔 200번 × 5초다"
    assert took < 1.0, f"{took:.1f}초를 막았다"
    assert "60초 뒤 다시 묻는다" in capsys.readouterr().out
    _later(monkeypatch, 59)
    a._tools_map()
    assert gw.calls == 1
    _later(monkeypatch, 61)
    a._tools_map()
    assert gw.calls == 2, "60초가 지났는데 다시 묻지 않는다 — 살아난 게이트웨이를 영영 못 본다"


def test_게이트웨이가_살아나면_다시_물어_받는다(gw, monkeypatch):
    assert a._tools_map() == {}
    gw.up = True
    assert a._tools_map() == {}, "방금 실패했다 — 60초 안에는 묻지 않는다"
    _later(monkeypatch, 61)
    assert a._tools_map() == {"list_materials": "heax-materialtwin_web"}
    assert a._area_of("list_materials")[0] == "material"
    calls = gw.calls
    a._tools_map()
    assert gw.calls == calls, "받은 지도는 종전대로 300초 캐시한다"


def test_앱_목록_조회도_같다(gw, monkeypatch, capsys):
    for _ in range(50):
        assert a._gw_apps() == {}
    assert gw.calls == 1, gw.calls
    assert "apps fetch failed" in capsys.readouterr().out, "종전엔 이 실패가 로그에도 없었다"
    gw.up = True
    _later(monkeypatch, 61)
    assert a._app_label("heax-materialtwin_web") == "재료 물성"


def test_낡은_지도가_있으면_실패하는_동안_그것으로_간다(gw, monkeypatch):
    """300초가 지난 지도라도 못 물어본 동안에는 버리지 않는다(종전 동작 그대로) — 다만 호출마다 다시 묻지 않는다."""
    a._TOOLS_MAP_CACHE.update({"at": time.time() - 1000, "map": {"old_tool": "old-app"}})
    for _ in range(30):
        assert a._tools_map() == {"old_tool": "old-app"}
    assert gw.calls == 1

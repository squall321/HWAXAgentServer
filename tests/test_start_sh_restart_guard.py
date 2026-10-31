# start.sh 의 재기동 가드와 서버에 넘기는 환경 — 도는 심의가 있으면 떠 있는 서버를 내리지 않는지, /health 가 그 수를 주는지
#
# 재기동은 도는 심의와 줄 선 심의를 전부 끊는다. 좌석 20석 넘는 패널은 수 시간 도는데, 코드를 고치고
# ./start.sh 를 부르는 순간(update-forges 도 직접 부른다) 그 심의가 말없이 사라졌다. 한도를 아무리 넉넉히 잡아도
# 배포 한 번이 전부 무효로 만든다 — 가장 흔한 '벽시계 절단' 이다.
#
# **스크립트를 실제로 돌린다.** 임시 디렉터리의 사본을, 대역(ss·curl·sleep·pip·uvicorn)을 깐 PATH 로 부른다.
# '떠 있는 서버' 는 시험이 띄운 잠자는 프로세스다 — 스크립트가 그것을 실제로 죽이는지 살려 두는지를 본다.
# ⚠ 실 서버(9009)는 건드리지 않는다. 포트는 커널이 고른 빈 포트이고, 대역이 PATH 맨 앞에 선 것을 돌리기 전에 확인한다.
#
#   실행:  .venv/bin/python -m pytest tests/test_start_sh_restart_guard.py -q
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402

_STUBS = {
    # 시험이 띄운 프로세스가 살아 있는 동안만 그 pid 가 시험 포트를 잡고 있다고 답한다.
    # closed 가 있으면 리스너만 닫고 살아 있는 꼴이다(uvicorn 은 TERM 을 받자마자 리스너를 닫고 열린 연결을 기다린다 — 아래 _LINGER).
    "ss": '''#!/usr/bin/env bash
pid="$(cat "$STUB_DIR/pid" 2>/dev/null || true)"
[ -f "$STUB_DIR/closed" ] && exit 0
if [ -n "$pid" ] && [ -d "/proc/$pid" ] && ! grep -q '^State:.*Z' "/proc/$pid/status" 2>/dev/null; then
  echo "LISTEN 0 128 127.0.0.1:${STUB_PORT} 0.0.0.0:* users:((\\"uvicorn\\",pid=${pid},fd=3))"
fi
exit 0
''',
    # /health 대역 — 파일이 있으면 그 내용을, 없으면 연결 실패(7)를 돌려준다. 부른 주소를 적어 둔다.
    # --max-time 에 숫자가 아닌 것이 오면 실물처럼 거절한다(2) — 대역이 뭐든 받아 주면 나쁜 값이 시험에서만 통한다.
    "curl": '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_DIR/curl.args"
prev=""
for arg in "$@"; do
  if [ "$prev" = "--max-time" ]; then
    case "$arg" in ''|.|*[!0-9.]*|*.*.*) echo "curl: option --max-time: expected a proper numerical parameter" >&2; exit 2 ;; esac
  fi
  prev="$arg"
done
[ -f "$STUB_DIR/health" ] || exit 7
cat "$STUB_DIR/health"
''',
    # 자지 않고 몇 초를 자려 했는지만 적는다. 숫자가 아니면 실물처럼 실패한다(1). STUB_SLEEP_RC 를 주면 늘 그 값으로 끝난다.
    "sleep": '''#!/usr/bin/env bash
printf '%s\\n' "$1" >> "$STUB_DIR/sleep.args"
case "$1" in ''|.|*[!0-9.]*|*.*.*) echo "sleep: invalid time interval '$1'" >&2; exit 1 ;; esac
exit "${STUB_SLEEP_RC:-0}"
''',
}
_VENV = {
    "pip": "#!/usr/bin/env bash\nexit 0\n",
    "uvicorn": ("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$STUB_DIR/uvicorn.args\"\n"
                "printf '%s\\n' \"${LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S:-없음}\" > \"$STUB_DIR/chunk\"\n"
                # 뜰 때 옛 서버가 살아 있었나 — 좀비(시험이 아직 거두지 않았다)는 끝난 것이다
                "old=\"$(cat \"$STUB_DIR/pid\" 2>/dev/null || true)\"\n"
                "st=\"$(ps -o stat= -p \"$old\" 2>/dev/null || true)\"\n"
                "case \"${st// /}\" in ''|Z*) echo gone ;; *) echo alive ;; esac > \"$STUB_DIR/old_at_start\"\n"
                "exit 0\n"),
}


class _Box:
    """임시 디렉터리에 깐 start.sh 사본 하나와 '떠 있는 서버' 하나."""

    def __init__(self, tmp_path):
        self.dir = tmp_path / "repo"
        self.stub = tmp_path / "stub"
        (self.dir / ".venv" / "bin").mkdir(parents=True)
        self.stub.mkdir()
        shutil.copy(ROOT / "start.sh", self.dir / "start.sh")
        for name, body in _STUBS.items():
            (self.stub / name).write_text(body, encoding="utf-8")
            (self.stub / name).chmod(0o755)
        for name, body in _VENV.items():
            (self.dir / ".venv" / "bin" / name).write_text(body, encoding="utf-8")
            (self.dir / ".venv" / "bin" / name).chmod(0o755)
        with socket.socket() as s:              # 아무도 안 듣는 포트 — 대역이 빠져도 실 ss 가 잡을 것이 없다
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.env = {"PATH": f"{self.stub}:/usr/bin:/bin", "HOME": str(tmp_path), "STUB_DIR": str(self.stub),
                    "STUB_PORT": str(self.port), "AGENT_PORT": str(self.port)}
        # 대역이 실제로 앞에 서는지 먼저 본다 — 아니면 스크립트가 실 ss·curl 을 부른다.
        for name in _STUBS:
            got = subprocess.run(["bash", "-c", f"command -v {name}"], env=self.env, capture_output=True, text=True)
            assert got.stdout.strip() == str(self.stub / name), (name, got.stdout)
        self.old = subprocess.Popen(["/usr/bin/sleep", "300"])      # '떠 있는 서버'
        (self.stub / "pid").write_text(str(self.old.pid), encoding="utf-8")

    def health(self, body):
        (self.stub / "health").write_text(body, encoding="utf-8")

    def linger(self, drain_s: float):
        """'떠 있는 서버' 를 uvicorn 꼴로 바꾼다 — TERM 을 받으면 리스너만 닫고(closed) 열린 연결을 drain_s 초 기다린 뒤 스스로 끝난다(크면
        TERM 을 못 듣는 서버다). 파이썬 한 프로세스라 kill -9 를 맞아도 남는 자식이 없다."""
        self.close()
        self.old = subprocess.Popen([sys.executable, "-c", _LINGER, str(drain_s)], env={**self.env})
        (self.stub / "pid").write_text(str(self.old.pid), encoding="utf-8")
        t0 = time.monotonic()
        while not (self.stub / "ready").exists():       # 처리기를 건 뒤에 TERM 을 보내야 한다
            assert time.monotonic() - t0 < 10 and self.old.poll() is None, "시험용 옛 서버가 뜨지 않았다"
            time.sleep(0.02)

    def real_sleep(self):
        """sleep 대역을 걷는다 — 기다리는 시간을 실제로 재는 시험용(옛 서버가 유예 안에 스스로 끝나는지)."""
        (self.stub / "sleep").unlink()

    def run(self, **env):
        r = subprocess.run(["bash", str(self.dir / "start.sh")], env={**self.env, **env}, capture_output=True,
                           text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def old_alive(self):
        return self.old.poll() is None

    def started(self):
        return (self.stub / "uvicorn.args").exists()

    def read(self, name):
        f = self.stub / name
        return f.read_text(encoding="utf-8").split("\n")[:-1] if f.exists() else []

    def close(self):
        if self.old.poll() is None:
            self.old.kill()
        self.old.wait(timeout=10)


# uvicorn 꼴의 옛 서버 — TERM 처리기를 건 뒤 ready 를 남기고, TERM 이면 closed(리스너를 닫았다)를 남기고 drain 초 뒤 0 으로 끝난다
_LINGER = """
import os, pathlib, signal, sys, time
stub, drain = pathlib.Path(os.environ["STUB_DIR"]), float(sys.argv[1])
def on_term(*_a):
    (stub / "closed").touch()
    time.sleep(drain)
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
(stub / "ready").touch()
while True:
    time.sleep(0.05)
"""


@pytest.fixture
def box(tmp_path):
    b = _Box(tmp_path)
    yield b
    b.close()


# ── 도는 심의가 있으면 내리지 않는다 ──────────────────────────────────────────
@pytest.mark.parametrize("health", ['{"status":"ok","delib_active":2,"delib_queued":1}',
                                    '{"status": "ok", "delib_active": 0, "delib_queued": 3}'])
def test_심의가_돌거나_줄을_서_있으면_서버를_내리지_않고_3_으로_나간다(box, health):
    box.health(health)
    rc, out = box.run()
    assert rc == 3, (rc, out)
    assert box.old_alive(), "도는 심의가 있는데 떠 있는 서버를 죽였다"
    assert not box.started(), "내리지 않았는데 새 서버를 띄우려 했다"
    assert "재기동 건너뜀" in out and "AGENT_RESTART_FORCE=1" in out, out
    n_act, n_que = ("2", "1") if '"delib_active":2' in health else ("0", "3")
    assert f"심의 {n_act}건 진행 중, {n_que}건 대기" in out, out
    assert box.read("sleep.args") == [], "내리지도 않았는데 유예를 기다렸다"


def test_강행하면_수를_크게_알리고_내린다(box):
    box.health('{"status":"ok","delib_active":2,"delib_queued":1}')
    rc, out = box.run(AGENT_RESTART_FORCE="1")
    assert rc == 0, (rc, out)
    assert not box.old_alive(), "강행했는데 옛 서버가 살아 있다"
    assert box.started()
    assert "⚠⚠ 심의 2건 진행 중, 1건 대기" in out and "강행한다" in out, out
    assert "interrupted" in out, "끊긴 심의가 어떻게 남는지 말하지 않았다"


def test_심의가_없으면_종전대로_재기동한다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    rc, out = box.run()
    assert rc == 0 and not box.old_alive() and box.started(), (rc, out)
    assert "건너뜀" not in out and "강행" not in out and "확인하지 못했다" not in out, out
    assert f"--port {box.port}" in box.read("uvicorn.args")[0]


# ── 모르는 것은 '없다' 가 아니다 ──────────────────────────────────────────────
@pytest.mark.parametrize("health", [None, '{"status":"ok"}', "<html>502 Bad Gateway</html>", ""])
def test_수를_모르면_그렇다고_말하고_재기동한다(box, health):
    """/health 무응답 · 그 수를 안 주는 옛 빌드 · 엉뚱한 답. 답 없는 서버는 다시 띄울 수 있어야 한다 —
    다만 '심의 없음' 으로 읽고 조용히 내리지는 않는다."""
    if health is not None:
        box.health(health)
    rc, out = box.run()
    assert rc == 0 and not box.old_alive() and box.started(), (rc, out)
    assert "진행 중 심의 수를 확인하지 못했다" in out, out


def test_떠_있는_서버가_없으면_묻지도_않는다(box):
    box.close()                                   # 옛 서버가 이미 내려갔다(서비스 관리자가 먼저 내린 경우)
    rc, out = box.run()
    assert rc == 0 and box.started(), (rc, out)
    assert box.read("curl.args") == [] and "확인하지 못했다" not in out, out


def test_health_는_루프백으로_프록시_없이_짧게_묻는다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    box.run(AGENT_HOST="0.0.0.0")
    (args,) = box.read("curl.args")
    assert f"http://127.0.0.1:{box.port}/health" in args and "--noproxy" in args and "--max-time 3" in args, args


# ── 유예 ─────────────────────────────────────────────────────────────────────
def test_TERM_뒤_유예는_기본_4초이고_설정으로_바꾼다(box):
    """정상 종료 상한(AGENT_GRACEFUL_S 1) + 도는 심의 접기(1초) + 루틴 접기(2초) — 그 안에 내려가야 원장에 진행이 남는다(HA-09 S0, 종전 기본 2).
    유예는 상한이다 — 옛 프로세스가 먼저 끝나면 거기서 띄운다(아래 시험)."""
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    rc, out = box.run()
    assert rc == 0 and "내려가기를 최대 4초 기다린다" in out, out


def test_유예를_설정으로_바꾼다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    rc, out = box.run(AGENT_STOP_GRACE_S="7")
    assert rc == 0 and "내려가기를 최대 7초 기다린다" in out, out


# ── 리스너가 아니라 옛 프로세스를 기다린다(HA-09 S0 반대 검토 10-31) ─────────────────────────────────────────────────────
# uvicorn 은 TERM 을 받자마자 리스너를 닫고 열린 연결(웹 챗 · 웹 심의 구독)을 AGENT_GRACEFUL_S 까지 기다린 뒤 lifespan 종료(도는 심의 접기)를 돈다.
# 종전엔 유예 sleep 뒤 리스너로 남은 것을 보고 kill -9 해, 그 사이 옛 프로세스가 살아 있는데 새 것을 띄웠다 — 같은 원장을 둘이 함께 썼다(옛 것이
# 끊긴 잡을 계속 돌리고 내려가며 새 것이 적은 이어하기 표지를 지워 같은 잡을 두 번 이을 수 있었다).
def test_리스너가_닫혀도_옛_프로세스가_끝날_때까지_기다렸다_띄운다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    box.linger(1.5)
    box.real_sleep()
    t0 = time.monotonic()
    rc, out = box.run()
    took = time.monotonic() - t0
    assert rc == 0 and box.started(), (rc, out)
    assert box.read("old_at_start") == ["gone"], "옛 서버가 살아 있는데 새 서버를 띄웠다"
    assert box.old.wait(timeout=5) == 0, "스스로 내려갈 서버를 유예 안에 kill -9 했다"
    assert 1.4 < took < 3.9, f"옛 프로세스가 끝나기를 기다리지 않았거나(리스너만 봤다) 유예를 다 썼다 — {took:.1f}초"
    assert "강제로 내린다" not in out, out


def test_유예_안에_안_끝나면_옛_PID_를_kill_9_로_내리고_띄운다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    box.linger(60)
    box.real_sleep()
    t0 = time.monotonic()
    rc, out = box.run(AGENT_GRACEFUL_S="0", AGENT_STOP_GRACE_S="3")
    took = time.monotonic() - t0
    assert rc == 0 and box.started(), (rc, out)
    assert box.old.wait(timeout=5) == -9, "리스너가 없다고 옛 프로세스를 그대로 두었다"
    assert box.read("old_at_start") == ["gone"], "옛 서버가 살아 있는데 새 서버를 띄웠다"
    assert 2.9 < took < 6.5, f"{took:.1f}초"
    assert "3초 안에 내려가지 않았다 — 강제로 내린다" in out, out


@pytest.mark.parametrize("env, want, warned", [
    ({"AGENT_STOP_GRACE_S": "0.5"}, "4", True),                                # 기본 상한 1 + 3
    ({"AGENT_GRACEFUL_S": "20"}, "23", True),                                  # 상한을 올렸는데 유예는 기본 4
    ({"AGENT_GRACEFUL_S": "20", "AGENT_STOP_GRACE_S": "30"}, "30", False),
    ({"AGENT_STOP_GRACE_S": "5.5"}, "5.5", False),
])
def test_유예는_정상_종료_상한과_접기보다_짧지_않다(box, env, want, warned):
    """TERM 뒤 KILL 까지가 uvicorn 정상 종료 상한(AGENT_GRACEFUL_S) + 도는 심의 접기(1초) + 루틴 접기(2초)보다 짧으면, 옛 프로세스가 그 사이에
    KILL 을 맞아 잡의 진행을 원장에 쓰지 못한다(진행 일지가 남아 다음 기동이 접기는 한다). 짧게 적었으면 그 합으로 올리고 그렇다고 말한다."""
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    rc, out = box.run(**env)
    assert rc == 0 and box.started() and f"내려가기를 최대 {want}초 기다린다" in out, out
    assert ("AGENT_STOP_GRACE_S" in out and "올린다" in out) is warned, out


# ── 숫자로 못 읽는 초 단위 손잡이 — 경고하고 기본값으로 돈다 ─────────────────────────────
# start.sh 의 .env 로더는 `=` 뒤를 통째로 값으로 읽는다(줄 끝 설명·끝 공백이 값에 남는다). 파이썬이 읽는 손잡이는
# 그런 값에 경고하고 기본값으로 도는데(킷과 env-kits README 가 그렇게 적는다), 셸이 읽는 이 둘은 그러지 않았다.
_BAD_SECS = ["2   # 종료 유예", "7 ", "abc", "1.2.3", "."]
_BAD_PROBE = [*_BAD_SECS, "10s"]        # 단위를 붙인 값 — sleep 은 받지만 curl 은 거절한다


def test_대역이_실물과_같은_값을_거절한다():
    """아래 시험들은 sleep·curl 대역이 나쁜 값에 **실물처럼 실패해야** 뜻이 있다 — 박스의 실물에 같은 값을 줘 본다.
    실제로 자는 값은 주지 않는다(전부 거절되거나 0초다)."""
    for bad in _BAD_SECS:
        assert subprocess.run(["/usr/bin/sleep", bad], capture_output=True).returncode != 0, bad
    for ok in ("0", "0.", ".0", "00"):                      # start.sh 가 그대로 넘기는 모양은 실물도 받는다
        assert subprocess.run(["/usr/bin/sleep", ok], capture_output=True).returncode == 0, ok
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("이 박스에 curl 이 없다 — start.sh 는 그때 '모름' 으로 읽는다")
    for bad in _BAD_PROBE:                                  # 2 = 인자 거절(연결을 해 보기도 전에 끝난다)
        r = subprocess.run([curl, "-s", "--noproxy", "*", "--max-time", bad, "http://127.0.0.1:9/"], capture_output=True)
        assert r.returncode == 2, (bad, r.returncode)


@pytest.mark.parametrize("bad", _BAD_SECS)
def test_유예를_숫자로_못_읽어도_옛_서버만_내리고_끝나지_않는다(box, bad):
    """sleep 이 그 값에 실패하면 set -e 가 **옛 서버를 내린 직후** 스크립트를 끝냈다 — 옛 것은 죽고 새 것은 안 떴다."""
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    (box.dir / ".env").write_text(f"AGENT_STOP_GRACE_S={bad}\n", encoding="utf-8")
    rc, out = box.run()
    assert rc == 0 and not box.old_alive(), (rc, out)
    assert box.started(), f"옛 서버를 내리고 새 서버를 띄우지 않았다 — {out}"
    assert "내려가기를 최대 4초 기다린다" in out, out
    assert "AGENT_STOP_GRACE_S" in out and "기본값 4초" in out, out


@pytest.mark.parametrize("bad", _BAD_PROBE)
def test_묻는_한도를_숫자로_못_읽어도_도는_심의를_끊지_않는다(box, bad):
    """curl 이 --max-time 을 거절하면 답이 비고, 빈 답은 '모름' 이라 그대로 재기동했다 — 바쁜 서버를 더 기다리려고
    올린 값이 보호를 껐고, 문구는 /health 탓을 해 같은 값을 또 올리게 했다."""
    box.health('{"status":"ok","delib_active":2,"delib_queued":1}')
    (box.dir / ".env").write_text(f"AGENT_HEALTH_PROBE_S={bad}\n", encoding="utf-8")
    rc, out = box.run()
    assert rc == 3 and box.old_alive() and not box.started(), (rc, out)
    assert "--max-time 3 " in box.read("curl.args")[0], box.read("curl.args")
    assert "AGENT_HEALTH_PROBE_S" in out and "기본값 3초" in out, out
    assert "확인하지 못했다" not in out, out


def test_유예_sleep_이_실패해도_새_서버를_띄운다(box):
    """옛 서버를 이미 내린 자리다 — 값이 멀쩡해도 sleep 이 무슨 까닭으로든 실패하면(신호로 끊기는 등) set -e 가
    거기서 스크립트를 끝내 아무것도 안 떴다. 기다리는 동안의 sleep 이 실패해도 횟수로 끝나 kill -9 뒤 새 서버를 띄운다(끝없이 돌지 않는다)."""
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    box.linger(60)
    rc, out = box.run(STUB_SLEEP_RC="130")
    assert rc == 0 and not box.old_alive(), (rc, out)
    assert box.started(), f"옛 서버를 내리고 새 서버를 띄우지 않았다 — {out}"
    assert len(box.read("sleep.args")) <= 60, "기다림이 횟수로 끝나지 않았다"


def test_숫자로_읽히는_값에는_경고가_없다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    (box.dir / ".env").write_text("AGENT_STOP_GRACE_S=4.5\nAGENT_HEALTH_PROBE_S=10\n", encoding="utf-8")
    rc, out = box.run()
    assert rc == 0 and box.started(), (rc, out)
    assert "내려가기를 최대 4.5초 기다린다" in out and "--max-time 10 " in box.read("curl.args")[0], out
    assert "기본값" not in out and "올린다" not in out, out


# ── uvicorn 정상 종료 상한(HA-09 S0) ────────────────────────────────────────────
def _graceful(box) -> str:
    args = box.read("uvicorn.args")[0].split()
    assert args.count("--timeout-graceful-shutdown") == 1, args
    return args[args.index("--timeout-graceful-shutdown") + 1]


def test_uvicorn_에_정상_종료_상한을_기본_1초로_주고_설정으로_바꾼다(box):
    """상한이 없으면(uvicorn 기본) 열린 SSE 하나가 닫히기를 끝없이 기다려 lifespan 종료(도는 심의 접기)에 닿기 전에 KILL 을 맞았다."""
    box.close()
    box.run()
    assert _graceful(box) == "1"
    box.run(AGENT_GRACEFUL_S="3")
    assert _graceful(box) == "3"
    (box.dir / ".env").write_text("AGENT_GRACEFUL_S=0\n", encoding="utf-8")
    rc, out = box.run()
    assert rc == 0 and _graceful(box) == "0" and "기본값" not in out, out


@pytest.mark.parametrize("bad", ["1.5", "2s", "abc", "1 # 설명", "-1"])
def test_정상_종료_상한을_정수로_못_읽으면_경고하고_기본값이다(box, bad):
    """uvicorn 은 --timeout-graceful-shutdown 에 정수만 받는다 — 그대로 넘기면 새 서버가 뜨지 않는다(옛 서버는 이미 내렸다)."""
    box.close()
    (box.dir / ".env").write_text(f"AGENT_GRACEFUL_S={bad}\n", encoding="utf-8")
    rc, out = box.run()
    assert rc == 0 and box.started() and _graceful(box) == "1", (rc, out)
    assert "AGENT_GRACEFUL_S" in out and "기본값 1초" in out, out


def test_백그라운드_기동도_같은_상한을_준다():
    """-d(nohup) 갈래와 포그라운드(exec) 갈래가 같은 인자로 뜬다 — 한쪽만 고치면 다른 길로 뜬 서버(손으로 ./start.sh -d · 서비스 매니페스트의
    ./start.sh)는 종전대로 상한 없이 뜬다."""
    src = (ROOT / "start.sh").read_text(encoding="utf-8")
    lines = [ln for ln in src.splitlines() if ".venv/bin/uvicorn app:app" in ln and not ln.lstrip().startswith("#")]
    assert len(lines) == 2 and all('--timeout-graceful-shutdown "$GRACEFUL_S"' in ln for ln in lines), lines


# ── 서버에 넘기는 환경 ───────────────────────────────────────────────────────
def test_챗_스트리밍_청크_한도를_300초로_내보내고_설정이_있으면_그_값이다(box):
    """langchain-openai 의 기본은 120초다 — 심의가 공유 LLM 을 차지한 동안 챗의 첫 토큰이 그 안에 안 온다."""
    box.close()                                   # 옛 서버 없이 곧바로 띄운다
    box.run()
    assert box.read("chunk") == ["300"], box.read("chunk")
    box.run(LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S="45")
    assert box.read("chunk") == ["45"]
    (box.dir / ".env").write_text("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S=600\n", encoding="utf-8")
    box.run()
    assert box.read("chunk") == ["600"], "박스 .env 의 값이 기본값에 덮였다"


def test_라이브러리가_그_이름의_환경변수를_읽는다(monkeypatch):
    """이름이 틀리면 내보내도 아무 일도 없다 — 라이브러리를 올릴 때 이 이름이 바뀌면 여기서 걸린다."""
    from langchain_openai import ChatOpenAI

    monkeypatch.setenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "300")
    assert ChatOpenAI(base_url="http://127.0.0.1:1/v1", api_key="EMPTY", model="x").stream_chunk_timeout == 300.0
    monkeypatch.delenv("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S")
    assert ChatOpenAI(base_url="http://127.0.0.1:1/v1", api_key="EMPTY", model="x").stream_chunk_timeout == 120.0


# ── /health 가 그 수를 준다 ───────────────────────────────────────────────────
def test_health_가_도는_심의와_줄_선_심의를_센다(monkeypatch):
    """웹 경로(분리 태스크)와 MCP 잡 원장을 합친다. 필드 이름은 start.sh 가 읽는 그 이름이다."""
    monkeypatch.setattr(a, "_model_context_tokens", lambda: 128000)       # 모델 서버에 묻지 않는다
    monkeypatch.setattr(delib_jobs, "_JOBS", {"가": {"id": "가", "status": "running"},
                                              "나": {"id": "나", "status": "done"},
                                              "다": {"id": "다", "status": "queued"}})
    monkeypatch.setattr(delib_jobs, "_PENDING", {"다": (None, None)})
    monkeypatch.setattr(a, "_DETACHED_TASKS", set())
    assert (a.health()["delib_active"], a.health()["delib_queued"]) == (1, 1)

    async def with_web_delib():
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            yield a._sse("done", {})

        stream = a._detach_stream(slow(), "t")
        during = a.health()["delib_active"]
        gate.set()
        async for _ in stream:
            pass
        await asyncio.sleep(0)
        return during, a.health()["delib_active"]

    assert asyncio.run(with_web_delib()) == (2, 1), "웹 경로의 심의를 세지 않았다"
    src = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert '"delib_active"' in src and '"delib_queued"' in src, "start.sh 가 다른 이름을 읽는다"


# ── /health 가 좌석 근거 예산(evid_budget)을 준다 — 리스크 앱이 박스의 창에 맞춰 브리프 크기를 잡는 값이다 ──────────
import json  # noqa: E402
import re  # noqa: E402

import deliberation as d  # noqa: E402


def _health_at(monkeypatch, ctx):
    monkeypatch.setattr(a, "_model_context_tokens", lambda: ctx)     # 엔진의 예산도 같은 이름을 늦게 읽는다
    d._evid_cache.clear()
    return a.health()


def test_health_가_근거_예산을_싣는다(monkeypatch):
    """값은 창에서 유도하고 **요청 때** 잰다 — 모듈을 읽을 때 재면 창 조회가 실패한 가정값이 굳는다."""
    try:
        assert _health_at(monkeypatch, 128000)["evid_budget"] == 17967
        assert _health_at(monkeypatch, 16384)["evid_budget"] == 2000          # 바닥값(작은 창)
        # 운영 창(1,048,576 — GLM-5.2) — 창에서 유도한 값이다. 직전 라운드·조회 결과·지정 도구·이어하기 요약의 몫을
        # 창에서 먼저 떼므로(tests/test_window_budget) 설정 천장(DELIB_EVID_BUDGET)에는 걸리지 않는다.
        big = _health_at(monkeypatch, 1048576)["evid_budget"]
        assert 17967 < big < d._EVID_BUDGET and big == int(d._pre_budget() * (1.0 - d._CHAT_CTX_SHARE)), big
    finally:
        d._evid_cache.clear()


def test_근거_예산을_실어도_start_sh_가_심의_수를_같은_식으로_읽는다(monkeypatch):
    """start.sh 는 /health 본문을 grep 두 줄로 읽는다 — 필드를 더해도 같은 수가 나와야 한다(실제 grep 으로 본다)."""
    monkeypatch.setattr(delib_jobs, "_JOBS", {"가": {"id": "가", "status": "running"}})
    monkeypatch.setattr(delib_jobs, "_PENDING", {"다": (None, None)})
    monkeypatch.setattr(a, "_DETACHED_TASKS", set())
    try:
        body = json.dumps(_health_at(monkeypatch, 128000))
    finally:
        d._evid_cache.clear()
    src = (ROOT / "start.sh").read_text(encoding="utf-8")
    for name, want in (("delib_active", "1"), ("delib_queued", "1")):
        pat = re.search(rf"grep -oE '(\"{name}\": \*\[0-9\]\+)'", src)
        assert pat, f"start.sh 가 {name} 을 읽는 식을 못 찾았다 — 모양이 바뀌었으면 이 시험도 고쳐라"
        got = subprocess.run(["bash", "-c", f"grep -oE '{pat.group(1)}' | grep -oE '[0-9]+$'"],
                             input=body, capture_output=True, text=True, timeout=10).stdout.strip()
        assert got == want, (name, got, body[:200])


# ── 줄이 재기동을 넘기는 판(HWAXPortal docs/delib-ops R2') ───────────────────────
def test_줄을_파일로_넘기는_판이면_줄_선_수만으로는_막지_않는다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":3,"delib_queue_durable":true}')
    rc, out = box.run()
    assert rc == 0 and not box.old_alive() and box.started(), (rc, out)
    assert "대기 3건은 재기동을 넘겨 같은 차례로 다시 선다" in out and "건너뜀" not in out, out


def test_줄을_넘겨도_도는_심의가_있으면_막고_대기는_다시_선다고_말한다(box):
    box.health('{"status":"ok","delib_active":1,"delib_queued":2,"delib_queue_durable":true}')
    rc, out = box.run()
    assert rc == 3 and box.old_alive() and not box.started(), (rc, out)
    assert "진행 중인 것은 끊긴다(대기는 다시 선다)" in out and "심의 1건 진행 중, 2건 대기" in out, out
    rc, out = box.run(AGENT_RESTART_FORCE="1")
    assert rc == 0 and "같은 차례로 다시 선다" in out and "deliberate_continue" in out, out


@pytest.mark.parametrize("flag", ['"delib_queue_durable":false', '"delib_queue_durable":"true"', ""])
def test_줄을_넘기는지_모르거나_거짓이면_종전대로_줄_선_수도_막는다(box, flag):
    """하나라도 시작 인자 파일을 못 쓴 줄은 durable 이 거짓이다 — 그 잡은 재기동에 사라진다. 모르는 것도 '넘긴다' 로 읽지 않는다."""
    body = '{"status":"ok","delib_active":0,"delib_queued":2' + (f",{flag}" if flag else "") + "}"
    box.health(body)
    rc, out = box.run()
    assert rc == 3 and box.old_alive(), (rc, out)
    assert "재기동하면 전부 끊긴다" in out, out

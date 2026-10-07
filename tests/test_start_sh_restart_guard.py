# start.sh 의 재기동 가드 — 도는 심의가 있으면 떠 있는 서버를 내리지 않는지, /health 가 그 수를 주는지
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import app as a  # noqa: E402
import delib_jobs  # noqa: E402

_STUBS = {
    # 시험이 띄운 프로세스가 살아 있는 동안만 그 pid 가 시험 포트를 잡고 있다고 답한다.
    "ss": '''#!/usr/bin/env bash
pid="$(cat "$STUB_DIR/pid" 2>/dev/null || true)"
if [ -n "$pid" ] && [ -d "/proc/$pid" ] && ! grep -q '^State:.*Z' "/proc/$pid/status" 2>/dev/null; then
  echo "LISTEN 0 128 127.0.0.1:${STUB_PORT} 0.0.0.0:* users:((\\"uvicorn\\",pid=${pid},fd=3))"
fi
exit 0
''',
    # /health 대역 — 파일이 있으면 그 내용을, 없으면 연결 실패(7)를 돌려준다. 부른 주소를 적어 둔다.
    "curl": '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_DIR/curl.args"
[ -f "$STUB_DIR/health" ] || exit 7
cat "$STUB_DIR/health"
''',
    # 자지 않고 몇 초를 자려 했는지만 적는다.
    "sleep": '''#!/usr/bin/env bash
printf '%s\\n' "$1" >> "$STUB_DIR/sleep.args"
exit 0
''',
}
_VENV = {
    "pip": "#!/usr/bin/env bash\nexit 0\n",
    "uvicorn": "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$STUB_DIR/uvicorn.args\"\nexit 0\n",
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
def test_TERM_뒤_유예는_기본_2초이고_설정으로_바꾼다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    box.run()
    assert box.read("sleep.args")[0] == "2", box.read("sleep.args")


def test_유예를_설정으로_바꾼다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    box.run(AGENT_STOP_GRACE_S="7")
    assert box.read("sleep.args")[0] == "7", box.read("sleep.args")


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

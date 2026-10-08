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
    # 자지 않고 몇 초를 자려 했는지만 적는다. 숫자가 아니면 실물처럼 실패한다(1).
    "sleep": '''#!/usr/bin/env bash
printf '%s\\n' "$1" >> "$STUB_DIR/sleep.args"
case "$1" in ''|.|*[!0-9.]*|*.*.*) echo "sleep: invalid time interval '$1'" >&2; exit 1 ;; esac
exit 0
''',
}
_VENV = {
    "pip": "#!/usr/bin/env bash\nexit 0\n",
    "uvicorn": ("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$STUB_DIR/uvicorn.args\"\n"
                "printf '%s\\n' \"${LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S:-없음}\" > \"$STUB_DIR/chunk\"\nexit 0\n"),
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
    assert box.read("sleep.args")[0] == "2", box.read("sleep.args")
    assert "AGENT_STOP_GRACE_S" in out and "기본값 2초" in out, out


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


def test_숫자로_읽히는_값에는_경고가_없다(box):
    box.health('{"status":"ok","delib_active":0,"delib_queued":0}')
    (box.dir / ".env").write_text("AGENT_STOP_GRACE_S=0.5\nAGENT_HEALTH_PROBE_S=10\n", encoding="utf-8")
    rc, out = box.run()
    assert rc == 0 and box.started(), (rc, out)
    assert box.read("sleep.args")[0] == "0.5" and "--max-time 10 " in box.read("curl.args")[0]
    assert "기본값" not in out, out


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

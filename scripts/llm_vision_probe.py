# GLM(OpenAI 호환) 엔드포인트가 content 배열의 image_url 을 받는지 HWAX 를 거치지 않고 직접 확인하는 탐침
"""쓰는 법(에이전트 서버 리포 루트에서 — .env 의 VLLM_* 를 쓴다, 키는 화면·argv 에 찍지 않는다).

    ( set -a; . ./.env; set +a; python3 scripts/llm_vision_probe.py )

32×32 빨간 PNG 를 data URL 로 실어 '이 그림의 색' 을 묻는다. 답에 빨강·red 가 있으면 서버와 모델이 그림을 읽는다.
오류면 서버가 다중모달을 받지 않는 것이다(모델이 텍스트 전용이거나 vLLM 이 그림 입력을 막았다).
"""
import base64
import json
import os
import struct
import sys
import urllib.error
import urllib.request
import zlib


def red_png(n: int = 32) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * n for _ in range(n))     # 줄마다 필터 0 + RGB 빨강

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main() -> int:
    # 에이전트 서버와 같은 차례 — .env 의 VLLM_*, 없으면 포털 정본 LLM_*, 그것도 없으면 코드 기본값(로컬 vLLM).
    base = (os.environ.get("VLLM_BASE_URL") or os.environ.get("LLM_BASE_URL") or "http://127.0.0.1:8000/v1").rstrip("/")
    key = os.environ.get("VLLM_API_KEY") or os.environ.get("LLM_API_KEY") or "EMPTY"
    model = os.environ.get("VLLM_MODEL") or os.environ.get("LLM_MODEL") or ""
    if not model:                                   # 서버가 내는 모델 이름을 그대로 쓴다
        try:
            req = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                model = (json.loads(r.read().decode()).get("data") or [{}])[0].get("id") or ""
        except Exception as e:  # noqa: BLE001
            print("모델 이름을 몰라 /models 를 물었는데 실패했다:", type(e).__name__, str(e)[:200])
            return 2
    url = base + "/chat/completions"
    data_url = "data:image/png;base64," + base64.b64encode(red_png()).decode()
    body = {"model": model, "max_tokens": 50, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "이 그림은 무슨 색인가? 한 단어로만 답하라."},
                {"type": "image_url", "image_url": {"url": data_url}}]}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    host = url.split("/")[2]
    print(f"엔드포인트 {host} · 모델 {model} · content 배열 = [text, image_url(data:image/png, {len(data_url)}자)]")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} — 서버가 그림 입력을 거절했다:", e.read().decode(errors="replace")[:400])
        return 1
    except Exception as e:  # noqa: BLE001 — 연결 문제는 그대로 보여 준다
        print("연결 실패:", type(e).__name__, str(e)[:300])
        return 1
    text = ((out.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    print("답:", text.strip()[:200])
    print("판정:", "그림을 읽는다" if any(w in text.lower() for w in ("빨", "red", "적색")) else "그림을 못 읽는다(또는 무시했다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

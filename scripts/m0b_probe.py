# M0-b 탐침 — 운영 LLM 에 카드 대조 프롬프트를 직접 보내 호출 초·글자/토큰 비·출력 상한·창 초과 문구·K 별 행 누락을 잰다
"""리스크 심사 재설계 M0-b. 절차서는 HWAXPortal `docs/risk-review-redesign/server-setup-m0b.md` 다.

M2 가 아직 가정으로 두는 값을 잰다(`reference/m2-wp3a.md` '실측이 필요한 값').
  · 호출 초 p50·p95 — sweep·verdict·offcard 따로
  · 글자/토큰 비 — prompt_chars ÷ tokens_in
  · max_tokens 를 안 보낼 때의 실제 출력 상한 — finish_reason=length 비율과 가장 긴 출력 토큰
  · 응답에 finish_reason·token_usage 가 실리나, 본문에 <think> 가 섞이나
  · 창을 넘는 프롬프트를 어떤 HTTP 상태·문구로 거절하나(창을 조금 넘게 채운 호출 한 번)
  · 묶음 장수 K ∈ {1, 4, 8, K_out} 별 빠진 행·못 읽은 원소·보충이 필요한 비율
  · 같은 프롬프트를 다시 보낼 때 접두 캐시가 듣나(덤)

엔진 함수를 그대로 부른다 — 조립 card_review.build_prompt, LLM 한 번 _ask, 검사 parse_rows·check_rows, 오류 부류 classify.
창은 app._model_context_state(), LLM 은 에이전트 서버 기동 절차(app.lifespan)가 만드는 심의 LLM 그대로다(같은 .env·같은
손잡이). 관문·HTTP 라우트·포털 중계를 지나지 않는다. K 팔은 운영 손잡이가 아니라 이 스크립트가 limits 의 K 를 갈아 끼워 고른다.

안전.
  · 잡 원장(DELIB_JOB_DIR)을 건드리지 않는다 — 기동 절차 가운데 심의 MCP 를 붙이는 길(bind 가 도는 잡을 interrupted 로
    정리한다)을 끄고 LLM 만 만든다. 게이트웨이 설정(MCP_CONFIG)도 읽지 않는다.
  · 포트를 열지 않는다. 쓰는 파일은 --out 하나다(이미 있으면 덮지 않고 멈춘다).
  · 비밀을 찍지 않는다 — LLM 키는 .env 에서만, 포털 PAT 는 환경변수 HWAX_PROBE_PAT 에서만 읽는다(인자로 받지 않는다).
  · 카드·단위 원문을 결과에 쓰지 않는다 — 수와 해시만 쓴다. 오류 발췌는 200자이고 원래 길이를 같이 적는다.
  · 호출은 한 번에 하나다(동시 1). 시작 전에 /health 를 보고 심의·카드 대조가 돌면 멈춘다(--allow-busy 로 강행).

  .venv/bin/python scripts/m0b_probe.py --agent <전문가 키> --dry-run          (포털 PAT 는 HWAX_PROBE_PAT)
  .venv/bin/python scripts/m0b_probe.py --agent <전문가 키> --out m0b-1012.jsonl
  .venv/bin/python scripts/m0b_probe.py --agent <키1>,<키2> --out m0b.jsonl     (두 전문가의 카드를 합쳐 K_out 팔을 채운다)
  .venv/bin/python scripts/m0b_probe.py --pack-file pack.json --unit-file unit.json --out m0b.jsonl
  .venv/bin/python scripts/m0b_probe.py --summarize m0b-1012.jsonl             (파일에서 요약만 다시)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import signal
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "card_review" / "contract"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROBE_REV = "m0b-1"
PAT_ENV = "HWAX_PROBE_PAT"
DEFAULT_PORTAL = "http://127.0.0.1:8723"
DEFAULT_K_ARMS = "1,4,8,kout"
SKIPPABLE = ("sweep", "offcard", "cache", "overflow")
CACHE_PAD_CHARS = 200_000     # 접두 캐시 셋의 단위 채움(자) — 미리 채우기(prefill)가 호출 초에 드러날 만큼. 창 한도 안으로 줄인다
OVERFLOW_OVER = 1.10          # 창 초과 호출이 노리는 입력 토큰 = 창 × 이 값
# 글자/토큰 비를 하나도 못 읽었을 때 채움 글에 거는 값. 크게 잡을수록 글이 길어져 확실히 넘친다 — 모자라게 잡으면 창 안에
# 들어 창 전체를 미리 채우는 비싼 호출이 된다(넘치는 쪽이 싸다).
OVERFLOW_CPT_FALLBACK = 3.0
OVERFLOW_TIMEOUT_S = 600.0    # 창 초과 호출의 시도 한도(재시도 0) — 혹시 받아들여지면 오래 붙들지 않는다
MAX_CONSEC_FAIL = 3           # 잇단 실패가 이만큼이면 멈춘다(창 초과 호출은 세지 않는다)
EXCERPT = 200                 # 오류 발췌의 글자 — 원래 길이를 excerpt_chars 에 같이 적는다
PAD_LINE = 1_000              # 채움 줄 하나의 글자
KNOWN_W = 8                   # offcard 의 '이미 판정한 것' 창 — 회차마다 옮겨 프롬프트를 겹치지 않게 한다
# 탐침이 이름만 찍는 .env 키(값은 찍지 않는다)
_SHOW_KEYS = re.compile(r"^(VLLM_|LLM_|DELIB_|CARD_REVIEW_)")
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")


class ProbeError(Exception):
    """탐침을 시작할 수 없다 — 사람이 고칠 것을 말한다."""


# ── 설정 ─────────────────────────────────────────────────────────────────────
_ENV_LINE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def read_env_file(path: Path) -> tuple[dict, int]:
    """start.sh 와 같은 규칙으로 .env 를 읽는다 — (값, 건너뛴 줄 수). 빈 줄·'#' 줄은 넘기고, KEY=VALUE 로 읽히는 줄만 받고,
    값 양끝의 같은 따옴표 한 쌍만 벗긴다. 그 밖의 줄은 건너뛴 줄로 센다. 서버가 보는 값과 같아야 같은 LLM 을 잰다."""
    if not path.is_file():
        return {}, 0
    out, skipped = {}, 0
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        if line == "" or line.startswith("#"):
            continue
        if not _ENV_LINE.match(line):
            skipped += 1
            continue
        k, v = line.split("=", 1)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out, skipped


def show_url(u: str) -> str:
    """주소에서 사용자 정보(user:pass@)와 쿼리를 뗀 것 — 찍어도 되는 모양."""
    try:
        p = urlsplit(str(u or ""))
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{p.hostname or ''}{port}{p.path}" if p.scheme else "(주소를 읽지 못했다)"
    except ValueError:
        return "(주소를 읽지 못했다)"


def scrub(s: str, hidden: list) -> str:
    for x in hidden:
        if x and len(x) >= 6:
            s = s.replace(x, "***")
    return s


def _hidden() -> list:
    key = os.environ.get("VLLM_API_KEY") or ""
    return [x for x in (key if key != "EMPTY" else "", os.environ.get(PAT_ENV) or "") if x]


def _sha(obj) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── 기동 절차가 만드는 LLM ──────────────────────────────────────────────────────
@contextlib.asynccontextmanager
async def _booted(env_over: dict):
    """에이전트 서버 기동 절차(app.lifespan)를 버리는 FastAPI 에 열어 app.state 를 준다 — 심의 LLM 을 서버와 같은 손잡이로
    만든다. 심의 MCP 를 붙이는 길(mcp_server.bind → delib_jobs.reap_orphans 가 실 원장의 도는 잡을 interrupted 로 바꾼다)과
    게이트웨이 설정 읽기를 끈다. 열고 닫는 동안만 바꾸고 되돌린다."""
    import app as a  # noqa: PLC0415 — .env 를 먼저 읽은 뒤에 가져온다
    from fastapi import FastAPI  # noqa: PLC0415

    saved_env = {k: os.environ.get(k) for k in env_over}
    saved = (a._DELIB_MCP, a._load_mcp_config)
    os.environ.update({k: str(v) for k, v in env_over.items()})
    a._DELIB_MCP, a._load_mcp_config = None, dict
    try:
        api = FastAPI()
        async with a.lifespan(api):
            yield api.state
    finally:
        a._DELIB_MCP, a._load_mcp_config = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def boot_llms() -> tuple[object, object, list]:
    """(심의 LLM, 창 초과 호출용 LLM, 기동 절차가 찍은 LLM 한도 줄). 창 초과용은 같은 LLM 을 시도 한도 OVERFLOW_TIMEOUT_S·
    재시도 0 으로 만든 것이다(손잡이 DELIB_TIMEOUT_S·DELIB_LLM_MAX_RETRIES 를 그 동안만 바꿔 기동 절차를 한 번 더 연다)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        async with _booted({}) as st:
            llm = st.delib_llm
        async with _booted({"DELIB_TIMEOUT_S": OVERFLOW_TIMEOUT_S, "DELIB_LLM_MAX_RETRIES": 0}) as st:
            over = st.delib_llm
    lines = [ln for ln in buf.getvalue().splitlines() if "LLM 한도" in ln or "심의 전용 LLM" in ln]
    return llm, over, lines[:2]


def llm_info(llm) -> dict:
    from deliberation import _llm_limit  # noqa: PLC0415

    read, tries = _llm_limit(llm)
    mt = getattr(llm, "max_tokens", None)
    eff = ((getattr(llm, "extra_body", None) or {}).get("chat_template_kwargs") or {}).get("reasoning_effort")
    return {"timeout_s": read or None, "tries": tries or None,
            "max_tokens": mt if isinstance(mt, int) and not isinstance(mt, bool) else None,
            "temperature": getattr(llm, "temperature", None), "effort": eff or None}


def agent_load(url: str) -> dict | None:
    """에이전트 서버 /health 의 도는 일 — {delib_active, delib_queued, review_active}. 못 읽으면 None(모름 — 0 이 아니다)."""
    import httpx  # noqa: PLC0415

    try:
        with httpx.Client(timeout=3.0, trust_env=False) as c:
            h = c.get(f"{url.rstrip('/')}/health").json()
    except Exception:  # noqa: BLE001 — 모름
        return None
    if not isinstance(h, dict):
        return None
    out = {k: h[k] for k in ("delib_active", "delib_queued", "review_active")
           if isinstance(h.get(k), int) and not isinstance(h.get(k), bool)}
    return out or None


def busy(load: dict | None) -> bool:
    return bool(load) and any(v > 0 for v in load.values())


# ── 카드·단위 ────────────────────────────────────────────────────────────────
def fetch_pack(portal: str, agent_key: str, pat: str, *, transport=None) -> dict:
    """포털 중계 POST /agent/card-review/pack — 이 PAT 주인의 자격으로 엔진이 짓는 팩(서비스 계정으로 내려가지 않는다)."""
    import httpx  # noqa: PLC0415

    url = f"{portal.rstrip('/')}/agent/card-review/pack"
    try:
        with httpx.Client(timeout=httpx.Timeout(660.0, connect=10.0), trust_env=False, transport=transport) as c:
            r = c.post(url, json={"agent_key": agent_key}, headers={"Authorization": f"Bearer {pat}"})
    except httpx.HTTPError as exc:
        raise ProbeError(f"포털 중계({show_url(url)})에 닿지 못했다 — {type(exc).__name__}. --portal 주소와 포털이 떠 있는지 본다") \
            from None
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code == 200 and isinstance(body, dict):
        return body
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        body = body["detail"]
    code = body.get("code") if isinstance(body, dict) else None
    msg = (body.get("message") or body.get("detail")) if isinstance(body, dict) else None
    why = scrub(str(msg or r.text or "")[:300], [pat])
    hint = {401: " — PAT 가 틀렸거나 만료됐다(포털 토큰 페이지에서 다시 받는다)",
            403: " — 이 계정에 전문가 심의(feat:deliberation) 권한이 없다"}.get(r.status_code, "")
    raise ProbeError(f"팩을 받지 못했다 — HTTP {r.status_code}{' ' + str(code) if code else ''}{hint}: {why}")


def check_pack(pack) -> dict:
    if not isinstance(pack, dict) or not isinstance(pack.get("cards"), list):
        raise ProbeError("팩 모양이 아니다 — cards 목록이 없다(POST /card-review/pack 의 응답을 준다)")
    if not re.fullmatch(r"[^\s«»{}\[\]]{1,120}", str(pack.get("agent_key") or "")):
        raise ProbeError("팩의 agent_key 가 비었거나 틀에 넣을 수 없는 글자가 있다")
    for c in pack["cards"]:
        if not (isinstance(c, dict) and re.fullmatch(r"K\d{2,}", str(c.get("alias") or ""))
                and isinstance(c.get("sections"), list)):
            raise ProbeError("팩의 카드 항목이 팩 카드 모양이 아니다(alias K01…·sections)")
    if not pack["cards"]:
        raise ProbeError(f"전문가 '{pack['agent_key']}' 의 카드가 0장이다 — 카드가 있는 전문가를 고른다")
    if not any(c.get("checkable") for c in pack["cards"]):
        raise ProbeError(f"전문가 '{pack['agent_key']}' 의 점검 카드(checkable)가 0장이다 — verdict 를 잴 수 없다. 다른 전문가를 고른다")
    return pack


def merge_packs(packs: list) -> dict:
    """여러 전문가의 팩을 하나로 — 카드를 record_id 로 겹침 없이 모아 엔진의 assign_aliases 로 별칭·길이를 다시 단다(팩의 고정
    순서 규칙 그대로). 키·이름·역할은 첫 전문가의 것이다. 전문가 한 명의 점검 카드는 13장 안팎이라(dev 코퍼스) K_out(1M 창에서 19)
    팔을 채우려면 둘을 합친다 — 운영 묶음은 한 전문가 것이지만 K 별 정확도는 장수의 효과를 본다."""
    if len(packs) == 1:
        return packs[0]
    import card_review as cr  # noqa: PLC0415

    seen, cards = set(), []
    for p in packs:
        for c in p["cards"]:
            if c.get("record_id") in seen:
                continue
            seen.add(c.get("record_id"))
            cards.append({k: v for k, v in c.items() if k not in ("alias", "chars")})
    first = packs[0]
    merged = cr.assign_aliases(cards)
    return {**first, "cards": merged, "merged_from": [str(p.get("agent_key")) for p in packs],
            "unrenderable": [u for p in packs for u in p.get("unrenderable") or []],
            "pack_hash": cr.pack_hash(str(first["agent_key"]), cr.role_sha(str(first.get("role") or "")), merged)}


def load_units(path: str | None) -> tuple[list, str]:
    """단위 목록과 출처. 없으면 계약 예제(req_verdict_fwd.json)의 합성 단위 하나다. 파일은 단위 하나({unit_id, …})·{"unit": …}·
    목록·{"units": [...]} 를 받고, 엔진 요청 모델(RunUnit)로 본다 — 선언 밖 칸이 있으면 엔진이 거절할 단위라 시작하지 않는다."""
    import card_review as cr  # noqa: PLC0415

    if path is None:
        units, src = [json.loads((FIXTURES / "req_verdict_fwd.json").read_text(encoding="utf-8"))["unit"]], "synthetic"
    else:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProbeError(f"단위 파일을 읽지 못했다 — {type(exc).__name__}: {exc}") from None
        if isinstance(raw, dict) and isinstance(raw.get("units"), list):
            raw = raw["units"]
        elif isinstance(raw, dict) and isinstance(raw.get("unit"), dict):
            raw = [raw["unit"]]
        elif isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ProbeError("단위 파일에 단위가 없다")
        units, src = raw, "file"
    for i, u in enumerate(units):
        try:
            cr.check_shape(cr.RunUnit, u)
        except cr.RequestError as exc:
            raise ProbeError(f"단위 {i}({str((u or {}).get('unit_id'))[:40]})가 엔진 계약과 다르다 — {exc.code}: {exc.message}") \
                from None
        if not (u.get("lines") or u.get("context")):
            raise ProbeError(f"단위 {i} 에 변경 줄도 문맥 줄도 없다 — 대조할 것이 없다")
    return units, src


def _taxonomy() -> dict:
    """offcard·sweep 의 영역·메커니즘 목록 — 계약 예제(req_offcard.json)의 것. 리스크 앱 택소노미가 아니라 모양만 같다."""
    ex = json.loads((FIXTURES / "req_offcard.json").read_text(encoding="utf-8")).get("extra") or {}
    return {"domains": list(ex.get("domains") or []), "mechanisms": list(ex.get("mechanisms") or [])}


def _corpus(pack: dict, units: list) -> str:
    """채움 줄의 글감 — 카드 절 본문과 단위의 글(카드와 같은 글자 구성이라 글자/토큰 비가 맞는다)."""
    parts = [s.get("text") or "" for c in pack["cards"] for s in c.get("sections") or [] if isinstance(s, dict)]
    for u in units:
        parts += [u.get("title") or "", u.get("summary") or ""]
        parts += [x.get("text") or "" for k in ("lines", "context", "evidence") for x in u.get(k) or [] if isinstance(x, dict)]
    text = "\n".join(p for p in parts if p).replace("«", "").replace("»", "")
    return text or "후면 커버 둘레 접착 계면의 치수 변경 1.2 → 0.9 mm"


def pad_unit(unit: dict, chars: int, corpus: str, tag: str) -> dict:
    """단위 사본에 채움 변경 줄을 더한다 — chars 자를 PAD_LINE 자씩 줄로 나누고 글감을 돌려 쓴다(줄마다 시작점이 다르다)."""
    u = json.loads(json.dumps(unit, ensure_ascii=False))
    lines = list(u.get("lines") or [])
    n = math.ceil(chars / PAD_LINE) if chars > 0 else 0
    big = corpus * (PAD_LINE // len(corpus) + 2)
    for i in range(n):
        size = min(PAD_LINE, chars - i * PAD_LINE)
        off = (i * 7_919) % len(corpus)
        lines.append({"ref": f"c:m0b-{tag}{i:05d}", "text": big[off: off + size]})
    u["lines"] = lines
    return u


# ── 계획 ─────────────────────────────────────────────────────────────────────
def parse_k_arms(spec: str, k_out: int) -> list:
    """'1,4,8,kout' → [(팔 이름, K)]. kout 은 이 창의 K_out 이다. 같은 이름은 한 번만."""
    out: dict = {}
    for tok in str(spec or "").split(","):
        t = tok.strip().lower()
        if not t:
            continue
        if t in ("kout", "k_out"):
            out.setdefault("kout", k_out)
            continue
        try:
            k = int(t)
        except ValueError:
            raise ProbeError(f"--k-arms 의 '{tok.strip()}' 를 읽지 못했다 — 양의 정수나 kout") from None
        if k < 1:
            raise ProbeError(f"--k-arms 의 K 는 1 이상이다({k})")
        out.setdefault(f"k{k}", k)
    if not out:
        raise ProbeError("--k-arms 가 비었다")
    return list(out.items())


def dedupe_arms(k_arms: list, check_n: int) -> tuple[list, list]:
    """점검 카드가 check_n 장이면 K 팔의 실제 묶음은 min(K, check_n) 장이다 — 실제 장수가 앞 팔과 같은 팔은 뺀다(같은 묶음·
    같은 프롬프트를 두 번 잰다). (남긴 팔, [(뺀 팔, 같은 앞 팔, 장수)])."""
    keep, merged, seen = [], [], {}
    for label, k in k_arms:
        eff = min(k, check_n)
        if eff in seen:
            merged.append((label, seen[eff], eff))
            continue
        seen[eff] = label
        keep.append((label, k))
    return keep, merged


def _expert(pack: dict, role_prefix: str = "") -> dict:
    return {"key": str(pack["agent_key"]), "name": str(pack.get("name") or ""), "domain": str(pack.get("domain") or ""),
            "role": role_prefix + str(pack.get("role") or "")}


def _digest(cards: list, loaded: list) -> list:
    """전문이 실리지 않은 카드의 제목 줄 — 묶음에 든 카드를 뺀 팩 전부(계약 시험의 요청과 같은 모양)."""
    keep = {c["alias"] for c in loaded}
    return [{"alias": c["alias"], "type": str(c.get("type") or ""), "title": str(c.get("title") or ""),
             "record_id": str(c.get("record_id") or "")} for c in cards if c["alias"] not in keep]


def _req(kind, variant, expert, cards, digest, unit=None, manifest=None, extra=None) -> dict:
    return {"kind": kind, "variant": variant, "target_key": "m0b-probe", "expert": expert, "cards": list(cards),
            "digest": digest, "unit": unit, "manifest": manifest or [], "extra": extra or {}}


def _manifest(units: list, lim: dict, rep: int) -> list:
    """sweep 의 단위 목록 한 쪽 — 이 창의 쪽당 단위 수(units_per_page_max)만큼, 단위들의 글을 돌려 쓴 합성 항목이다."""
    import card_review as cr  # noqa: PLC0415

    pool = []
    for u in units:
        for x in [{"text": u.get("summary") or ""}] + [y for k in ("lines", "context") for y in u.get(k) or []]:
            if isinstance(x, dict) and (x.get("text") or "").strip():
                pool.append((str(u.get("kind") or "bundle"), str(u.get("title") or ""), str(x["text"])))
    pool = pool or [("bundle", "검토 단위", "변경")]
    n = int(lim["sweep"]["units_per_page_max"])
    room = max(80, int(lim["sweep"]["manifest_item_max"]) - cr.MANIFEST_ITEM_OVERHEAD)
    out = []
    for i in range(n):
        kind, title, text = pool[(i + rep) % len(pool)]
        title = f"{title} {i + 1}"
        out.append({"unit_id": f"u-m0b-{i:03d}", "kind": kind, "title": title,
                    "text": cr._clip(text, max(20, room - len(title)))})
    return out


def lim_for(lim: dict, item: dict, *, unbounded: bool = False) -> dict:
    """item 에 쓰는 limits — verdict 는 K 를 그 묶음 장수로 갈아 끼운다(K 팔 — 운영 손잡이가 아니다). unbounded 면 창 한도
    검사(P_hard)를 끈다(창 초과 호출만 — 일부러 넘긴다)."""
    out = dict(lim)
    if item["kind"] == "verdict":
        out["verdict"] = {**lim["verdict"], "k": max(1, int(item["k"]))}
    if unbounded:
        out["p_hard"] = 10 ** 12
    return out


def _fit_pad(req: dict, lim: dict, want: int, corpus: str, tag: str) -> tuple[dict, int]:
    """want 자를 채운 단위가 창 한도(P_hard) 안에 들 때까지 줄인다 — (요청, 실제 채운 글자)."""
    import card_review as cr  # noqa: PLC0415

    pad = max(0, int(want))
    li = lim_for(lim, {"kind": req["kind"], "k": len(req["cards"])})
    for _ in range(12):
        r = {**req, "unit": pad_unit(req["unit"], pad, corpus, tag)}
        try:
            cr.build_prompt(r, li)
            return r, pad
        except cr.OverBudget as exc:
            if pad == 0:
                raise ProbeError(f"채움 없이도 창 한도를 넘는다({exc.need:,} > {exc.have:,}자)") from None
            pad = max(0, pad - (exc.need - exc.have) - 2_000)
    raise ProbeError("접두 캐시 호출의 채움을 창 한도 안으로 줄이지 못했다")


def build_plan(pack: dict, units: list, lim: dict, *, reps: int, k_arms: list, skip: set, nonce: str) -> list:
    """부를 차례 — 회차마다 K 팔 전부 → sweep → offcard 를 번갈아(시간대의 부하가 팔마다 고르게 든다), 끝에 접두 캐시 셋과
    창 초과 하나. 묶음은 점검 카드를 팩 순서로 돌려 가며 고른다(회차마다 다른 카드 — 같은 프롬프트를 되풀이하지 않게).
    verdict 의 순서는 짝수 회차 fwd·홀수 회차 rev 다(운영의 이중 판정과 같다). sweep·offcard 는 reps // 2 회(최소 1)."""
    import card_review as cr  # noqa: PLC0415

    cards = pack["cards"]
    check = [c for c in cards if c.get("checkable")]
    bg = [c for c in cards if not c.get("checkable")]
    n = len(check)
    expert = _expert(pack)
    tax = _taxonomy()
    corpus = _corpus(pack, units)
    side = max(1, reps // 2) if reps > 0 else 0
    by_alias = {c["alias"]: c for c in cards}
    sweep_decks = cr.plan([{"alias": c["alias"], "record_id": str(c.get("record_id") or ""),
                            "checkable": bool(c.get("checkable")), "chars": int(c.get("chars") or 0)} for c in cards],
                          [], lim)["sweep_decks"]
    items: list = []
    for r in range(max(reps, side)):
        unit = units[r % len(units)]
        if r < reps:
            variant = "fwd" if r % 2 == 0 else "rev"
            for label, k in k_arms:
                k_eff = min(k, n)
                start = (r * k_eff) % n
                deck = [check[i] for i in sorted({(start + j) % n for j in range(k_eff)})]
                items.append({"kind": "verdict", "variant": variant, "arm": label, "k": k_eff, "k_arm": k, "rep": r,
                              "req": _req("verdict", variant, expert, deck, _digest(cards, deck), unit=unit)})
        if r < side and "sweep" not in skip and sweep_decks:
            deck = [by_alias[a] for a in sweep_decks[r % len(sweep_decks)]["aliases"]]
            items.append({"kind": "sweep", "variant": None, "arm": "sweep", "k": len(deck), "rep": r,
                          "req": _req("sweep", None, expert, deck, [], manifest=_manifest(units, lim, r),
                                      extra={"domains": tax["domains"]})})
        if r < side and "offcard" not in skip:
            w = min(KNOWN_W, n)
            known = [{"alias": check[(r * w + j) % n]["alias"], "judgement": "OK", "why": "앞선 카드 대조에서 판정했다"}
                     for j in range(w)]
            items.append({"kind": "offcard", "variant": None, "arm": "offcard", "k": len(bg), "rep": r,
                          "req": _req("offcard", None, expert, bg, _digest(cards, bg), unit=unit,
                                      extra={**tax, "known": known})})
    if "cache" not in skip:
        # 역할 머리에 이 실행만의 표지를 넣어 첫 호출은 캐시에 없는 글이 되게 한다(같은 전문가의 앞 호출과 [당신] 뒤로 갈린다).
        deck = check[:1]
        base = _req("verdict", "fwd", _expert(pack, f"(탐침 {nonce}) "), deck, _digest(cards, deck), unit=units[0])
        req, pad = _fit_pad(base, lim, CACHE_PAD_CHARS, corpus, "cache")
        hint = cr.repair_hint([("", "missing", deck[0]["alias"])], 2, 3)
        for arm, h in (("cache-cold", ""), ("cache-repeat", ""), ("cache-repair", hint)):
            items.append({"kind": "verdict", "variant": "fwd", "arm": arm, "k": 1, "rep": 0, "req": req, "hint": h,
                          "pad_chars": pad})
    if "overflow" not in skip:
        deck = check[min(1, n - 1): min(1, n - 1) + 1]
        items.append({"kind": "verdict", "variant": "fwd", "arm": "overflow", "k": 1, "rep": 0, "pad_chars": 0,
                      "req": _req("verdict", "fwd", expert, deck, _digest(cards, deck), unit=units[0]),
                      "corpus": corpus})
    return items


def cap_plan(items: list, max_calls: int) -> tuple[list, int]:
    """--max-calls 로 자른다 — 회차 쪽 꼬리부터 버리고 접두 캐시 셋·창 초과(끝 묶음)는 남긴다. 버린 수를 돌려준다(말한다)."""
    tail = [x for x in items if _special(x["arm"])]
    head = [x for x in items if not _special(x["arm"])]
    keep_head = head[: max(0, max_calls - len(tail))]
    keep = keep_head + tail[: max(0, max_calls - len(keep_head))]
    return keep, len(items) - len(keep)


def prepare(item: dict, lim: dict) -> dict:
    """item 의 프롬프트 — {system, human, ctx, lim}. 계약에 안 맞으면 card_review.RequestError."""
    import card_review as cr  # noqa: PLC0415

    li = lim_for(lim, item, unbounded=item["arm"] == "overflow")
    system, human, ctx = cr.build_prompt(item["req"], li, hint=item.get("hint") or "")
    return {"system": system, "human": human, "ctx": ctx, "lim": li}


def overflow_item(item: dict, lim: dict, cpt: float, cpt_source: str) -> dict:
    """창 초과 호출 — 단위를 채워 입력이 창 × OVERFLOW_OVER 토큰쯤 되게 한다(글자/토큰 비 cpt 로 글자를 정한다)."""
    target = math.ceil(int(lim["ctx_tokens"]) * OVERFLOW_OVER)
    base = prepare(item, lim)["ctx"]["prompt_chars"]
    pad = max(0, math.ceil(target * cpt) - base)
    req = {**item["req"], "unit": pad_unit(item["req"]["unit"], pad, item["corpus"], "over")}
    return {**item, "req": req, "pad_chars": pad, "target_tokens": target, "cpt_used": round(cpt, 4),
            "cpt_source": cpt_source}


def overflow_cpt(calls: list) -> tuple[float, str]:
    """창 초과 채움에 쓸 글자/토큰 비 — 채움 글이 대부분인 접두 캐시 첫 호출의 것, 없으면 지금까지 잰 것 가운데 가장 큰 것
    (크게 잡아야 넘친다), 하나도 없으면 OVERFLOW_CPT_FALLBACK."""
    for r in calls:
        if r["arm"] == "cache-cold" and r.get("chars_per_token"):
            return float(r["chars_per_token"]), "cache-cold"
    seen = [r["chars_per_token"] for r in calls if r.get("chars_per_token")]
    if seen:
        return float(max(seen)), "max-observed"
    return OVERFLOW_CPT_FALLBACK, "fallback"


# ── 한 번 재기 ────────────────────────────────────────────────────────────────
_MAIN_ROW = {"verdict": "v", "sweep": "u", "offcard": "f"}


def _tally(kind: str, rows: list, chk: dict) -> dict:
    """행의 셈만(원문 없이) — verdict 판정별 수, sweep 관련도별 수, offcard 의 f 줄 수와 '더 있나'."""
    main = [r for r in rows if r.get("t") == _MAIN_ROW[kind]]
    if kind == "verdict":
        return dict(Counter(str(r.get("judgement")) for r in main))
    if kind == "sweep":
        return dict(Counter(str(r.get("rel")) for r in main))
    return {"f": len(main), "maybe_more": chk.get("maybe_more")}


async def measure(item: dict, prep: dict, llm, base_lim: dict) -> dict:
    """LLM 한 번 → 결과 한 줄(원문 없이). 실패는 엔진의 classify 로 부류를 붙이고 발췌 200자와 원래 길이를 남긴다."""
    import app as a  # noqa: PLC0415
    import card_review as cr  # noqa: PLC0415

    system, human, ctx = prep["system"], prep["human"], prep["ctx"]
    prompt = system + human
    req = item["req"]
    rec = {"rec": "call", "ts": round(time.time(), 1), "kind": item["kind"], "variant": item.get("variant"),
           "arm": item["arm"], "k": item["k"], "rep": item["rep"],
           "deck_sha": _sha([[c["alias"], c.get("record_id")] for c in req["cards"]]),
           "unit_sha": _sha(req["unit"]) if req.get("unit") else None,
           "prompt_sha": _sha(prompt), "prompt_chars": ctx["prompt_chars"], "prompt_bytes": len(prompt.encode("utf-8")),
           "est_tokens": a._est_tokens_all(prompt), "pad_chars": item.get("pad_chars", 0),
           "soft_over": bool(ctx["soft_over"])}
    for k in ("target_tokens", "cpt_used", "cpt_source"):
        if k in item:
            rec[k] = item[k]
    nulls = dict.fromkeys(("tokens_in", "tokens_out", "chars_per_token", "finish_reason", "out_chars", "think_seen",
                           "rows_expected", "rows_ok", "rows_flagged", "rows_missing", "bad_lines", "tail_broken",
                           "foreign", "dropped_alias", "dup", "repair_needed", "tally"))
    t0 = time.monotonic()
    try:
        text, meta = await cr._ask(llm, system, human)
    except Exception as exc:  # noqa: BLE001 — 실패의 모양이 재는 대상이다
        rec["seconds"] = round(time.monotonic() - t0, 3)
        err = cr.classify(exc, llm, est_tokens=rec["est_tokens"], lim=base_lim)
        raw = " ".join(scrub(f"{type(exc).__name__}: {exc}", _hidden()).split())
        rec.update(nulls)
        rec["error"] = {"code": err["code"], "status": err.get("status"), "infra": err["infra"],
                        "excerpt": raw[:EXCERPT], "excerpt_chars": len(raw)}
        return rec
    rec["seconds"] = round(time.monotonic() - t0, 3)
    usage = meta.get("usage") or {}
    tin, tout = usage.get("tokens_in"), usage.get("tokens_out")
    rec.update(tokens_in=tin, tokens_out=tout,
               chars_per_token=round(ctx["prompt_chars"] / tin, 4) if isinstance(tin, int) and tin > 0 else None,
               finish_reason=meta.get("finish_reason"), out_chars=len(text),
               think_seen="<think>" in text or "</think>" in text)
    try:
        rows, ps = cr.parse_rows(text, item["kind"])
        chk = cr.check_rows(item["kind"], item.get("variant"), rows, ctx, attempt=1)
    except Exception as exc:  # noqa: BLE001 — 엔진 파서의 결함도 잰 것이다(엔진은 internal_error 로 낸다)
        raw = f"{type(exc).__name__}: {exc}"
        rec.update({k: None for k in nulls if k not in rec})
        rec["error"] = {"code": "internal_error", "status": None, "infra": False, "excerpt": raw[:EXCERPT],
                        "excerpt_chars": len(raw)}
        return rec
    rec.update(rows_expected=chk["expected"], rows_ok=chk["ok"], rows_flagged=chk["flagged"],
               rows_missing=len(chk["missing"]), bad_lines=ps["bad_lines"], tail_broken=bool(ps["tail_broken"]),
               foreign=ps["foreign"], dropped_alias=chk["stats"]["dropped_alias"], dup=chk["stats"]["dup"],
               repair_needed=bool(chk["repair"] or ps["bad_lines"]), tally=_tally(item["kind"], chk["rows"], chk),
               error=None)
    return rec


# ── 요약 ─────────────────────────────────────────────────────────────────────
def pct(vals: list, p: float):
    """가장 가까운 순위(nearest-rank) 백분위 — 정렬해 ceil(p/100 × n) 번째 값. 비면 None."""
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)]


def _special(arm: str) -> bool:
    return arm.startswith("cache") or arm == "overflow"


def summarize(meta: dict | None, calls: list, *, interrupted: bool = False) -> dict:
    """결과 줄들 → 요약(숫자만). 호출 초·글자/토큰 비는 성공한 호출만, 종류별 초는 채움이 든 호출(접두 캐시·창 초과)을 뺀다."""
    meta = meta or {}
    ok = [r for r in calls if not r.get("error")]
    normal = [r for r in calls if not _special(r["arm"])]
    out: dict = {"rec": "summary", "probe_rev": PROBE_REV, "interrupted": bool(interrupted),
                 "calls_planned": (meta.get("plan") or {}).get("calls"), "calls_done": len(calls),
                 "calls_failed": len(calls) - len(ok)}
    for k in ("model", "ctx_tokens", "ctx_source", "llm_max_tokens", "out_tokens", "k_default", "k_out"):
        out[k] = meta.get(k)
    if calls:
        t_end = max(r["ts"] + (r.get("seconds") or 0) for r in calls)
        out["wall_s"] = round(t_end - min(r["ts"] for r in calls), 1)
    else:
        out["wall_s"] = 0
    by_kind = {}
    for kind in ("verdict", "sweep", "offcard"):
        rs = [r for r in normal if r["kind"] == kind]
        if not rs:
            continue
        secs = [r["seconds"] for r in rs if not r.get("error")]
        by_kind[kind] = {"n": len(rs), "ok": len(secs), "p50_s": pct(secs, 50), "p95_s": pct(secs, 95),
                         "max_s": max(secs) if secs else None}
    out["by_kind"] = by_kind

    def ratio_stats(rs):
        cpt = [r["chars_per_token"] for r in rs if r.get("chars_per_token")]
        est = [r["est_tokens"] / r["tokens_in"] for r in rs if isinstance(r.get("tokens_in"), int) and r["tokens_in"] > 0]
        return {"n": len(cpt), "median": pct(cpt, 50), "min": min(cpt) if cpt else None,
                "max": max(cpt) if cpt else None, "est_over_actual": round(pct(est, 50), 4) if est else None}

    out["cpt"] = ratio_stats([r for r in ok if not _special(r["arm"])])
    out["cpt_padded"] = ratio_stats([r for r in ok if r["arm"].startswith("cache")])
    touts = [r["tokens_out"] for r in ok if isinstance(r.get("tokens_out"), int)]
    out["output"] = {
        "ok_calls": len(ok),
        "finish_known": sum(1 for r in ok if r.get("finish_reason")),
        "usage_known": sum(1 for r in ok if r.get("tokens_in") is not None or r.get("tokens_out") is not None),
        "length": sum(1 for r in ok if r.get("finish_reason") == "length"),
        "length_arms": sorted({r["arm"] for r in ok if r.get("finish_reason") == "length"}),
        "finish_reasons": dict(Counter(str(r.get("finish_reason")) for r in ok)),
        "tokens_out_max": max(touts) if touts else None, "tokens_out_p95": pct(touts, 95),
        "out_chars_max": max((r.get("out_chars") or 0 for r in ok), default=None),
        "think_seen": sum(1 for r in ok if r.get("think_seen")),
    }
    arms = []
    for arm in dict.fromkeys(r["arm"] for r in normal):
        rs = [r for r in normal if r["arm"] == arm]
        good = [r for r in rs if not r.get("error")]
        exp = sum(r["rows_expected"] or 0 for r in good)
        miss = sum(r["rows_missing"] or 0 for r in good)
        flag = sum(r["rows_flagged"] or 0 for r in good)
        arms.append({"arm": arm, "kind": rs[0]["kind"], "k": sorted({r["k"] for r in rs}), "n": len(rs), "ok": len(good),
                     "expected": exp, "missing": miss, "missing_rate": round(miss / exp, 4) if exp else None,
                     "bad_lines": sum(r["bad_lines"] or 0 for r in good),
                     "repair_needed": sum(1 for r in good if r.get("repair_needed")),
                     "flagged": flag, "flag_rate": round(flag / exp, 4) if exp else None,
                     "length": sum(1 for r in good if r.get("finish_reason") == "length"),
                     "p50_s": pct([r["seconds"] for r in good], 50)})
    out["arms"] = arms
    ov = next((r for r in calls if r["arm"] == "overflow"), None)
    out["overflow"] = None if ov is None else {
        "accepted": not ov.get("error"), "status": (ov.get("error") or {}).get("status"),
        "code": (ov.get("error") or {}).get("code"), "seconds": ov.get("seconds"), "est_tokens": ov.get("est_tokens"),
        "target_tokens": ov.get("target_tokens"), "cpt_used": ov.get("cpt_used"), "cpt_source": ov.get("cpt_source"),
        "prompt_chars": ov.get("prompt_chars"), "prompt_bytes": ov.get("prompt_bytes"),
        "tokens_in": ov.get("tokens_in"), "finish_reason": ov.get("finish_reason"),
        "excerpt": (ov.get("error") or {}).get("excerpt"), "excerpt_chars": (ov.get("error") or {}).get("excerpt_chars")}
    cache = {r["arm"].split("-", 1)[1]: {"seconds": r.get("seconds"), "tokens_in": r.get("tokens_in"),
                                         "tokens_out": r.get("tokens_out"), "prompt_chars": r.get("prompt_chars"),
                                         "pad_chars": r.get("pad_chars"), "error": (r.get("error") or {}).get("code")}
             for r in calls if r["arm"].startswith("cache")}
    out["cache"] = cache or None
    out["load"] = {"busy_calls": sum(1 for r in calls if busy(r.get("load"))),
                   "unknown_calls": sum(1 for r in calls if r.get("load") is None)}
    out["dup_prompts"] = sum(1 for r in calls if r.get("dup_prompt") and not r["arm"].startswith("cache"))
    out["errors"] = dict(Counter(r["error"]["code"] for r in calls if r.get("error")))
    return out


def _n(x, fmt: str = "{:,}") -> str:
    return "모름" if x is None else fmt.format(x)


def _s(x) -> str:
    return "—" if x is None else f"{x:.1f}"


def _rate(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _hms(s) -> str:
    s = int(round(s or 0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def render(sm: dict) -> str:
    """사람이 붙여 보낼 요약 블록."""
    L = ["===== M0-b 탐침 요약 — 이 블록을 그대로 붙여 보낸다 ====="]
    mt = sm.get("llm_max_tokens")
    L.append(f"탐침 {sm['probe_rev']} · 모델 {sm.get('model') or '모름'} · 창 {_n(sm.get('ctx_tokens'))} 토큰"
             f"({sm.get('ctx_source') or '모름'}) · 출력 상한 {'미전송' if not mt else f'{mt:,}'}"
             f"(out 몫 {_n(sm.get('out_tokens'))}) · K 기본 {_n(sm.get('k_default'))} · K_out {_n(sm.get('k_out'))}")
    ld = sm.get("load") or {}
    L.append(f"호출 {sm['calls_done']}/{_n(sm.get('calls_planned'))}{'(중단됨)' if sm.get('interrupted') else ''} · "
             f"실패 {sm['calls_failed']} · 벽시계 {_hms(sm.get('wall_s'))} · 다른 심의·검토가 돌던 호출 "
             f"{ld.get('busy_calls', 0)}(모름 {ld.get('unknown_calls', 0)}) · 같은 프롬프트 되풀이 {sm.get('dup_prompts', 0)}")
    L.append("[호출 초 — 성공한 호출, 채움 호출 제외]")
    L.append(f"  {'종류':<8}{'호출':>5}{'성공':>5}{'p50':>9}{'p95':>9}{'최대':>9}")
    for kind, b in (sm.get("by_kind") or {}).items():
        L.append(f"  {kind:<8}{b['n']:>6}{b['ok']:>6}{_s(b['p50_s']):>9}{_s(b['p95_s']):>9}{_s(b['max_s']):>9}")
    c, cp = sm.get("cpt") or {}, sm.get("cpt_padded") or {}
    L.append(f"[글자/토큰] prompt_chars÷tokens_in 중앙 {_n(c.get('median'), '{:.3f}')}(최소 {_n(c.get('min'), '{:.3f}')} · "
             f"최대 {_n(c.get('max'), '{:.3f}')} · n={c.get('n', 0)}) · 엔진 추정÷실제 {_n(c.get('est_over_actual'), '{:.3f}')}"
             f" · 채움 글 {_n(cp.get('median'), '{:.3f}')}(n={cp.get('n', 0)})")
    o = sm.get("output") or {}
    n_ok = o.get("ok_calls", 0)
    L.append(f"[출력] finish_reason 실림 {o.get('finish_known', 0)}/{n_ok} · token_usage 실림 {o.get('usage_known', 0)}/{n_ok}"
             f" · length {o.get('length', 0)}/{n_ok}{' ' + ','.join(o['length_arms']) if o.get('length_arms') else ''}"
             f" · 가장 긴 출력 {_n(o.get('tokens_out_max'))} 토큰(p95 {_n(o.get('tokens_out_p95'))}) · 가장 긴 본문 "
             f"{_n(o.get('out_chars_max'))}자 · <think> {o.get('think_seen', 0)}/{n_ok}")
    L.append(f"  finish_reason 분포 {json.dumps(o.get('finish_reasons') or {}, ensure_ascii=False)}")
    L.append("[K 팔 · 종류별 행 — 첫 시도만(보충 호출 없음)]")
    L.append(f"  {'팔':<9}{'K/카드':>8}{'호출':>5}{'성공':>5}{'기대행':>7}{'빠진행':>7}{'비율':>7}{'못읽음':>7}"
             f"{'보충필요':>9}{'표지행':>7}{'비율':>7}{'length':>7}{'p50초':>8}")
    for a in sm.get("arms") or []:
        ks = "/".join(str(k) for k in a["k"])
        L.append(f"  {a['arm']:<9}{ks:>8}{a['n']:>6}{a['ok']:>6}{a['expected']:>9}{a['missing']:>8}"
                 f"{_rate(a['missing_rate']):>8}{a['bad_lines']:>7}{str(a['repair_needed']) + '/' + str(a['ok']):>10}"
                 f"{a['flagged']:>7}{_rate(a['flag_rate']):>8}{a['length']:>6}{_s(a['p50_s']):>9}")
    ov = sm.get("overflow")
    if ov:
        head = ("받아들였다(거절 없음 — 창이 알려 준 값보다 크거나 넘치지 않았다)" if ov["accepted"]
                else f"HTTP {ov.get('status') or '없음'} → {ov.get('code')}")
        L.append(f"[창 초과] {head} · {_s(ov.get('seconds'))}초 · 추정 {_n(ov.get('est_tokens'))} 토큰(노림 "
                 f"{_n(ov.get('target_tokens'))} · 글자/토큰 {_n(ov.get('cpt_used'), '{:.3f}')} {ov.get('cpt_source') or ''})"
                 f" · {_n(ov.get('prompt_chars'))}자 · {_n(ov.get('prompt_bytes'))}바이트"
                 + (f" · 입력 {_n(ov.get('tokens_in'))} 토큰 · {ov.get('finish_reason')}" if ov["accepted"] else ""))
        if ov.get("excerpt"):
            L.append(f"  «{ov['excerpt']}»(원문 {_n(ov.get('excerpt_chars'))}자)")
    cache = sm.get("cache")
    if cache:
        parts = []
        for k, label in (("cold", "처음"), ("repeat", "같은 글"), ("repair", "보충 꼴")):
            x = cache.get(k)
            if x:
                parts.append(f"{label} {_s(x['seconds'])}초(입력 {_n(x['tokens_in'])} · 출력 {_n(x['tokens_out'])}"
                             + (f" · 오류 {x['error']}" if x.get("error") else "") + ")")
        first = next(iter(cache.values()))
        L.append(f"[접두 캐시 — 프롬프트 {_n(first.get('prompt_chars'))}자(채움 {_n(first.get('pad_chars'))}자)·카드 1장] "
                 + " · ".join(parts))
    if sm.get("errors"):
        L.append(f"[오류] {json.dumps(sm['errors'], ensure_ascii=False)}")
    L.append("===== 요약 끝 =====")
    return "\n".join(L)


def read_jsonl(path: str) -> tuple[dict | None, list, dict | None]:
    """결과 파일 → (meta, 호출 줄, summary). 읽지 못한 줄(끊긴 마지막 줄 등)은 건너뛰고 그 수를 말한다."""
    meta, calls, summary, bad = None, [], None, 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(r, dict):
            bad += 1
        elif r.get("rec") == "meta":
            meta = r
        elif r.get("rec") == "call":
            calls.append(r)
        elif r.get("rec") == "summary":
            summary = r
    if bad:
        print(f"[읽기] {path} 에서 JSON 으로 읽지 못한 줄 {bad}개를 건너뛰었다", file=sys.stderr)
    return meta, calls, summary


# ── 실행 ─────────────────────────────────────────────────────────────────────
def _progress(rec: dict, i: int, n: int, eta: float | None) -> str:
    head = f"[{i:>{len(str(n))}}/{n}] {rec['kind']:<7} {rec['arm']:<12} {rec.get('variant') or '-':<4} k={rec['k']:<3}"
    if rec.get("error"):
        e = rec["error"]
        tail = f"오류 {e['code']}" + (f"(HTTP {e['status']})" if e.get("status") else "")
    else:
        tail = (f"토큰 {_n(rec['tokens_in'])}→{_n(rec['tokens_out'])} · {rec.get('finish_reason') or 'finish 모름'} · "
                f"빠짐 {rec['rows_missing']}/{rec['rows_expected']} · 못읽음 {rec['bad_lines']}")
    more = "" if eta is None else f" · 남은 어림 {_hms(eta)}"
    return f"{head} · {rec['prompt_chars']:,}자 · {rec['seconds']:.1f}초 · {tail}{more}"


def _plan_lines(items: list, preps: dict) -> list:
    import app as a  # noqa: PLC0415

    rows: dict = {}
    for i, it in enumerate(items):
        p = preps.get(i)
        g = rows.setdefault(it["arm"], {"kind": it["kind"], "n": 0, "k": set(), "chars": [], "tok": []})
        g["n"] += 1
        g["k"].add(it["k"])
        if p:
            g["chars"].append(p["ctx"]["prompt_chars"])
            g["tok"].append(a._est_tokens_all(p["system"] + p["human"]))
    out = [f"  {'팔':<13}{'종류':<9}{'호출':>4}{'K/카드':>8}{'프롬프트 글자':>20}{'추정 입력 토큰':>20}"]
    for arm, g in rows.items():
        ch = f"{min(g['chars']):,}~{max(g['chars']):,}" if g["chars"] else "실행 때 정함"
        tk = f"{min(g['tok']):,}~{max(g['tok']):,}" if g["tok"] else "창×1.1"
        out.append(f"  {arm:<13}{g['kind']:<9}{g['n']:>4}{'/'.join(map(str, sorted(g['k']))):>8}{ch:>20}{tk:>20}")
    return out


async def run_probe(args) -> int:
    import app as a  # noqa: PLC0415
    import card_review as cr  # noqa: PLC0415

    hidden = _hidden()
    llm, over_llm, boot_lines = await boot_llms()
    info = llm_info(llm)
    state = a._model_context_state()
    mt = info["max_tokens"] or 0
    lim = cr.limits(state["tokens"], state["source"], mt, retrying=state["retrying"], model=a.VLLM_MODEL)
    has_key = (os.environ.get("VLLM_API_KEY") or "EMPTY") != "EMPTY"
    print(f"[LLM] 모델 {a.VLLM_MODEL} · 주소 {show_url(a.VLLM_BASE_URL)} · 키 {'있음(값은 찍지 않는다)' if has_key else '없음(EMPTY)'}")
    for ln in boot_lines:
        print(f"      {ln.strip()}")
    print(f"[창] {lim['ctx_tokens']:,} 토큰 · 출처 {lim['ctx_source']}{' · 다시 묻는 중(조회 실패)' if lim['ctx_retrying'] else ''}"
          f" · P_hard {lim['p_hard']:,}자 · out 몫 {lim['out_tokens']:,} 토큰 · K {lim['verdict']['k']} · K_out "
          f"{lim['verdict']['k_out']} · 쪽당 단위 {lim['sweep']['units_per_page_max']} · findings_max "
          f"{lim['offcard']['findings_max']} · limits_rev {lim['limits_rev']}")
    if state["retrying"] or state["source"] == "fallback":
        msg = ("LLM 서버에 창 크기를 묻지 못했다(/v1/models)" if state["retrying"] else
               "LLM 서버가 창 크기를 알려 주지 않았고 LLM_CONTEXT_TOKENS 도 없다")
        if not args.dry_run:
            raise ProbeError(f"{msg} — 가정한 창({lim['ctx_tokens']:,})으로는 K_out·창 초과를 잴 수 없다. VLLM_BASE_URL·키를 "
                             "확인하거나 LLM_CONTEXT_TOKENS 를 .env 에 적는다")
        print(f"  ⚠ {msg} — 실제 실행은 여기서 멈춘다")
    if not all(lim["runnable"].values()):
        raise ProbeError((lim.get("ctx_too_small") or {}).get("message") or "이 창으로는 카드 대조를 돌릴 수 없다")

    if args.pack_file:
        try:
            pack = json.loads(Path(args.pack_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProbeError(f"팩 파일을 읽지 못했다 — {type(exc).__name__}: {exc}") from None
        pack_src = "file"
    else:
        pat = (os.environ.get(PAT_ENV) or "").strip()
        if not pat:
            raise ProbeError(f"--agent 는 포털 PAT 가 필요하다 — 셸에서 `read -rs {PAT_ENV}; export {PAT_ENV}` 로 넣는다"
                             "(인자로 받지 않는다)")
        packs = []
        for key in [k.strip() for k in args.agent.split(",") if k.strip()]:
            print(f"[팩] 포털 중계 {show_url(args.portal)}/agent/card-review/pack 에서 '{key}' 의 카드를 받는다…", flush=True)
            t0 = time.monotonic()
            packs.append(check_pack(fetch_pack(args.portal, key, pat)))
            print(f"      {time.monotonic() - t0:.1f}초 · 카드 {len(packs[-1]['cards'])}장(점검 "
                  f"{sum(1 for c in packs[-1]['cards'] if c.get('checkable'))})")
        if not packs:
            raise ProbeError("--agent 가 비었다")
        pack = merge_packs(packs)
        pack_src = "portal"
    pack = check_pack(pack)
    units, unit_src = load_units(args.unit_file)
    check_n = sum(1 for c in pack["cards"] if c.get("checkable"))
    k_arms, merged = dedupe_arms(parse_k_arms(args.k_arms, int(lim["verdict"]["k_out"])), check_n)
    skip = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    if skip - set(SKIPPABLE):
        raise ProbeError(f"--skip 은 {', '.join(SKIPPABLE)} 가운데서 고른다({', '.join(sorted(skip - set(SKIPPABLE)))})")
    print(f"[팩] {pack['agent_key']} · 카드 {len(pack['cards'])}장(점검 {check_n}·배경 {len(pack['cards']) - check_n}) · "
          f"{sum(int(c.get('chars') or 0) for c in pack['cards']):,}자 · 역할 {len(str(pack.get('role') or '')):,}자 · "
          f"pack_hash {str(pack.get('pack_hash') or '')[:12] or '없음'} · 출처 {pack_src}")
    if pack.get("merged_from"):
        print(f"      {'+'.join(pack['merged_from'])} 의 카드를 합쳤다 — 역할·키는 {pack['agent_key']} 의 것이다")
    if pack.get("unrenderable"):
        print(f"      절이 없어 못 싣는 카드 {len(pack['unrenderable'])}장(팩 밖)")
    print(f"[단위] {len(units)}개 · 출처 {unit_src} · 크기 " + ", ".join(f"{cr.unit_size(u):,}" for u in units[:8])
          + (" …" if len(units) > 8 else ""))
    short = [f"{lab}(K {k} → {check_n})" for lab, k in k_arms if k > check_n]
    if short:
        print(f"  ⚠ 점검 카드가 {check_n}장이라 K 팔 {', '.join(short)} 는 있는 만큼만 싣는다 — 채우려면 --agent 에 같은 분야 "
              "전문가 키를 쉼표로 하나 더 준다(카드를 합친다)")
    for lab, prev, k in merged:
        print(f"  ⚠ K 팔 {lab} 은 {prev} 와 같은 묶음(카드 {k}장)이라 뺀다 — 같은 프롬프트를 두 번 재지 않는다")
    beyond = [lab for lab, k in k_arms if k > lim["verdict"]["k_out"]]
    if beyond:
        print(f"  ⚠ K 팔 {', '.join(beyond)} 는 이 창의 K_out({lim['verdict']['k_out']})보다 크다 — 운영 엔진은 그 묶음을 거절한다")

    nonce = secrets.token_hex(4)
    items = build_plan(pack, units, lim, reps=args.reps, k_arms=k_arms, skip=skip, nonce=nonce)
    items, dropped = cap_plan(items, args.max_calls)
    preps: dict = {}
    for i, it in enumerate(items):
        if it["arm"] == "overflow":
            continue
        try:
            preps[i] = prepare(it, lim)
        except cr.RequestError as exc:
            raise ProbeError(f"{it['arm']} 회차 {it['rep']} 의 프롬프트를 엔진이 받지 않는다 — {exc.code}: {exc.message}") \
                from None
    load0 = agent_load(args.agent_url)
    print(f"[계획] 호출 {len(items)}회(동시 1)" + (f" · --max-calls 로 {dropped}회를 버렸다" if dropped else "")
          + f" · 표지 {nonce}")
    for ln in _plan_lines(items, preps):
        print(ln)
    est_in = sum(a._est_tokens_all(p["system"] + p["human"]) for p in preps.values())
    print(f"  입력 추정 합 {est_in:,} 토큰(창 초과 호출 제외) · 호출 하나 30초면 약 {_hms(30 * len(items))}, "
          f"5분이면 약 {_hms(300 * len(items))}")
    print(f"[부하] 에이전트 서버 {show_url(args.agent_url)}/health — "
          + ("모름(답이 없다)" if load0 is None else ", ".join(f"{k} {v}" for k, v in load0.items())))
    proxies = [k for k in _PROXY_KEYS if os.environ.get(k)]
    if proxies:
        print(f"[프록시] 이 셸에 {', '.join(proxies)} 가 있다 — LLM 호출이 그것을 따른다(서버를 띄운 환경과 다르면 결과가 다르다)")
    if args.dry_run:
        print("[dry-run] LLM 을 부르지 않았다. 위 계획대로 돌리려면 --dry-run 을 --out <파일> 로 바꾼다")
        return 0
    if busy(load0) and not args.allow_busy:
        print(f"멈춤 — 에이전트 서버에서 다른 일이 돈다({', '.join(f'{k} {v}' for k, v in load0.items())}). 재는 호출 초에 줄 "
              "기다림이 섞이고 그 일을 느리게 한다. 끝난 뒤 다시 돌리거나 --allow-busy 로 강행한다", file=sys.stderr)
        return 3

    out_path = Path(args.out)
    try:
        fh = open(out_path, "x", encoding="utf-8")  # noqa: SIM115 — 끝에서 닫는다
    except FileExistsError:
        raise ProbeError(f"{out_path} 가 이미 있다 — 덮지 않는다. 다른 이름을 준다") from None
    meta = {"rec": "meta", "probe_rev": PROBE_REV, "started": round(time.time(), 1), "model": a.VLLM_MODEL,
            "llm_host": show_url(a.VLLM_BASE_URL), "ctx_tokens": lim["ctx_tokens"], "ctx_source": lim["ctx_source"],
            "llm_max_tokens": lim["llm_max_tokens"], "out_tokens": lim["out_tokens"], "p_hard": lim["p_hard"],
            "k_default": lim["verdict"]["k"], "k_out": lim["verdict"]["k_out"],
            "units_per_page_max": lim["sweep"]["units_per_page_max"], "findings_max": lim["offcard"]["findings_max"],
            "limits_rev": lim["limits_rev"], "prompt_rev": lim["prompt_rev"], "norm_rev": lim["norm_rev"],
            "llm": info, "overflow_llm": llm_info(over_llm),
            "pack": {"agent_key": pack["agent_key"], "source": pack_src, "cards": len(pack["cards"]), "checkable": check_n,
                     "chars": sum(int(c.get("chars") or 0) for c in pack["cards"]), "role_chars": len(str(pack.get("role") or "")),
                     "pack_hash": pack.get("pack_hash"), "norm_rev": pack.get("norm_rev"),
                     "merged_from": pack.get("merged_from")},
            "units": [{"unit_sha": _sha(u), "size": cr.unit_size(u), "lines": len(u.get("lines") or []),
                       "context": len(u.get("context") or []), "evidence": len(u.get("evidence") or [])} for u in units],
            "unit_source": unit_src,
            "plan": {"calls": len(items), "dropped": dropped, "reps": args.reps,
                     "k_arms": [[lab, k] for lab, k in k_arms], "skip": sorted(skip),
                     "arms": dict(Counter(it["arm"] for it in items))},
            "nonce": nonce, "load_at_start": load0}
    fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
    fh.flush()
    print(f"[실행] 결과 {out_path} — 한 줄씩 바로 쓴다. Ctrl-C 로 멈춰도 거기까지의 요약을 낸다", flush=True)

    calls: list = []
    seen: set = set()
    fails = 0
    interrupted = False
    stopped = ""
    try:
        for i, it in enumerate(items):
            if it["arm"] == "overflow":
                cpt, src = overflow_cpt(calls)
                it = overflow_item(it, lim, cpt, src)
                try:
                    prep = prepare(it, lim)
                except cr.RequestError as exc:
                    print(f"  창 초과 호출을 짓지 못했다 — {exc.code}: {exc.message}", file=sys.stderr)
                    continue
                use = over_llm
            else:
                prep, use = preps[i], llm
            load = agent_load(args.agent_url)
            rec = await measure(it, prep, use, lim)
            rec.update(i=i + 1, n=len(items), load=load, dup_prompt=rec["prompt_sha"] in seen)
            seen.add(rec["prompt_sha"])
            calls.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            done = [r["seconds"] for r in calls if r["arm"] != "overflow"]
            left = sum(1 for x in items[i + 1:] if x["arm"] != "overflow")
            print(_progress(rec, i + 1, len(items), sum(done) / len(done) * left if done and left else None), flush=True)
            if it["arm"] != "overflow":
                fails = fails + 1 if rec.get("error") else 0
                if fails >= MAX_CONSEC_FAIL:
                    stopped = (f"잇단 실패 {fails}회({rec['error']['code']}) — 멈춘다. 마지막 오류: "
                               f"{rec['error']['excerpt']}")
                    break
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
        raise
    finally:
        sm = summarize(meta, calls, interrupted=interrupted or bool(stopped))
        fh.write(json.dumps(sm, ensure_ascii=False) + "\n")
        fh.close()
        if stopped:
            print(stopped, file=sys.stderr)
        print()
        print(scrub(render(sm), hidden))
        print(f"결과 파일: {out_path.resolve()} — meta 1줄 + 호출 {len(calls)}줄 + summary 1줄. 이 파일과 위 요약 블록을 보낸다")
    return 4 if stopped else 0


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="m0b_probe.py",
        description="M0-b 탐침 — 운영 LLM 에 카드 대조 프롬프트를 직접 보내 호출 초·글자/토큰 비·출력 상한·창 초과 문구·K 별 "
                    "행 누락을 잰다. 에이전트 서버 리포 루트에서 .venv/bin/python 으로 돈다.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--agent", help=f"전문가 키 — 포털 중계로 팩을 받는다(PAT 는 환경변수 {PAT_ENV}). 쉼표로 여럿이면 카드를 "
                                     "합친다(역할은 첫 전문가)")
    src.add_argument("--pack-file", help="팩 JSON(POST /card-review/pack 의 응답)")
    src.add_argument("--summarize", metavar="JSONL", help="이미 쓴 결과 파일에서 요약만 다시 낸다(LLM 을 부르지 않는다)")
    ap.add_argument("--unit-file", help="검토 단위 JSON(리스크 앱 review_payload 의 단위 하나·목록). 없으면 합성 단위")
    ap.add_argument("--out", help="결과 JSONL(이미 있으면 멈춘다). --dry-run 에는 필요 없다")
    ap.add_argument("--dry-run", action="store_true", help="프롬프트를 짓고 계획만 찍는다(LLM 을 부르지 않는다)")
    ap.add_argument("--reps", type=int, default=12, help="K 팔마다의 회차(기본 12). sweep·offcard 는 그 절반")
    ap.add_argument("--k-arms", default=DEFAULT_K_ARMS, help=f"verdict 묶음 장수(기본 {DEFAULT_K_ARMS})")
    ap.add_argument("--max-calls", type=int, default=100, help="LLM 호출 상한(기본 100) — 넘으면 회차 꼬리를 버리고 말한다")
    ap.add_argument("--skip", default="", help=f"뺄 것을 쉼표로 — {', '.join(SKIPPABLE)}")
    ap.add_argument("--portal", default=DEFAULT_PORTAL, help=f"포털 백엔드 주소(기본 {DEFAULT_PORTAL})")
    ap.add_argument("--env-file", default=str(ROOT / ".env"), help="에이전트 서버 .env(기본 리포 루트의 .env)")
    ap.add_argument("--allow-busy", action="store_true", help="심의·카드 대조가 돌아도 강행한다")
    args = ap.parse_args(argv)

    if args.summarize:
        meta, calls, saved = read_jsonl(args.summarize)
        sm = summarize(meta, calls, interrupted=bool(saved and saved.get("interrupted")))
        print(render(sm))
        return 0
    if not (args.agent or args.pack_file):
        ap.error("--agent 나 --pack-file 가운데 하나가 있어야 한다")
    if not args.dry_run and not args.out:
        ap.error("--out 이 있어야 한다(또는 --dry-run)")
    if args.reps < 1 or args.max_calls < 1:
        ap.error("--reps·--max-calls 는 1 이상이다")

    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)     # 진행 줄이 nohup·파이프에서도 바로 보이게
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)   # 요청마다의 INFO 줄(주소)을 찍지 않는다
    env, skipped = read_env_file(Path(args.env_file))
    os.environ.update(env)        # start.sh 처럼 .env 가 셸 값을 이긴다 — 서버가 보는 값과 같게
    shown = sorted(k for k in env if _SHOW_KEYS.match(k))
    print(f"[설정] {args.env_file} — {len(env)}개 키" + (f"(LLM·심의·카드 대조: {', '.join(shown)})" if shown else "")
          + (f" · 형식이 아닌 줄 {skipped}개 건너뜀" if skipped else "") + (" · 파일 없음" if not Path(args.env_file).is_file()
                                                                     else ""))
    args.agent_url = f"http://127.0.0.1:{(os.environ.get('AGENT_PORT') or '9009').strip()}"
    # kill(TERM)·kill -INT 둘 다 Ctrl-C 처럼 — 거기까지의 요약을 쓰고 끝낸다. 비대화 셸이 띄운 백그라운드 작업은 SIGINT 를
    # 무시한 채 시작해 파이썬이 Ctrl-C 처리기를 달지 않으므로 둘 다 여기서 단다.
    prev: dict = {}
    with contextlib.suppress(ValueError):          # 주 스레드가 아니면 달 수 없다
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev[sig] = signal.signal(sig, _interrupt)
    try:
        return asyncio.run(run_probe(args))
    except ProbeError as exc:
        print(f"멈춤 — {scrub(str(exc), _hidden())}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("중단했다(Ctrl-C·kill) — 위 요약은 거기까지의 호출이다", file=sys.stderr)
        return 130
    finally:
        for sig, h in prev.items():                # 같은 프로세스에서 main 을 부른 쪽(시험)의 처리기를 되돌린다
            signal.signal(sig, h if h is not None else signal.SIG_DFL)


if __name__ == "__main__":
    sys.exit(main())

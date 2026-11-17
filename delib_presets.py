# 심의 기본값 묶음(preset) — 추적 JSON + 박스 덧씌우기(merge patch · 바뀐 시각으로 다시 읽기), 부른 쪽 인자가 이기고 최종 값과 출처를 되돌려 준다(DL-05)
"""HWAXPortal docs/request-1117 (DL-05) — deliberate_start(preset=…) · deliberate_jobs 의 presets.

**표.** 추적 파일 delib_presets.json 의 presets{이름: {label, what, args}} 에 박스 덧씌우기 delib_presets.local.json(gitignore)을 JSON merge patch
(RFC 7386 — 객체는 재귀로 합치고 null 은 그 칸을 지운다 · 목록은 통째로 바뀐다 — app._merge_patch 와 같은 셈)로 덮는다. 두 파일의 바뀐 시각이 달라지면
다시 읽는다(재기동 없음). 덧씌우기가 깨졌으면 버리고 추적 표로 돈다 — 까닭은 state() 의 overlay.error.

**인자 이름은 진짜 이름이다.** args 는 deliberate_start 의 인자(ARGS)만 받고, advanced 의 손잡이는 엔진이 같은 이름으로 읽는 것만 받는다 — 읽을 때
엔진(_resolve_opts)에 넣어 그 값으로 읽히는지 본다(읽지 못하거나 범위 밖에서 죄이면 그 preset 을 버리고 까닭을 남긴다). 종전에 'mode: credibility' 를
보낸 호출이 말없이 자유 심의로 돌았다 — 심의 종류는 job 이다.

**이기는 쪽.** 부른 쪽이 직접 준 인자가 preset 을 이긴다. '직접 줬다' 는 도구 기본값(DEFAULTS)과 다른 값이다 — job='default' · rounds=0 처럼 기본값과 같은
값을 주면 준 것으로 보지 않는다(preset 이 채운다). advanced 는 손잡이마다 가린다. 부른 쪽 advanced 에 rounds · modifiers · save_report 가 있으면 그것이
위 인자 · preset 을 다 이긴다(엔진에 가는 손잡이는 advanced 가 맨 뒤에 덮는다 — mcp_server._build_opts) — 메아리는 from caller · via advanced 다. preset 의
advanced 에는 그 셋을 두지 않는다(위 인자로 둔다). 응답의 preset.applied 가 칸마다 최종 값과 출처(preset · caller)다.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import deliberation as _engine   # 엔진이 손잡이를 같은 이름 · 같은 값으로 읽는지 본다
import delib_jobs

log = logging.getLogger("agent.presets")

PATH = Path(__file__).resolve().parent / "delib_presets.json"
LOCAL_PATH = PATH.with_name("delib_presets.local.json")
ARGS = ("job", "rounds", "modifiers", "save_report", "advanced")
# deliberate_start 의 인자 기본값 — 이 값이면 부른 쪽이 주지 않은 것으로 본다
DEFAULTS = {"job": "default", "rounds": 0, "modifiers": None, "save_report": True, "advanced": None}
_CACHE: dict = {"key": None, "presets": {}, "errors": [], "overlay": {"present": False}}


def _merge_patch(target, patch):
    """JSON merge patch(RFC 7386) — app._merge_patch 와 같은 셈(그쪽을 import 하면 app 전체가 딸려 온다)."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _merge_patch(out.get(k), v)
    return out


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b))
    return a == b


def _check(name: str, spec) -> dict:
    """preset 하나를 보고 고친 꼴로 — 틀리면 ValueError(까닭)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("args"), dict):
        raise ValueError("{label, what, args{…}} 객체가 아니다")
    args = dict(spec["args"])
    bad = sorted(set(args) - set(ARGS))
    if bad:
        raise ValueError(f"모르는 인자 {bad[:5]} — 받는 인자는 {' · '.join(ARGS)} 다(심의 종류는 job 이다)")
    out: dict = {}
    if "job" in args:
        out["job"] = delib_jobs.resolve_job(str(args["job"]))
    if "rounds" in args:
        r = args["rounds"]
        if isinstance(r, bool) or not isinstance(r, int) or not 2 <= r <= 8:
            raise ValueError(f"rounds 는 2~8 의 정수다 — {r!r}")
        out["rounds"] = r
    if "modifiers" in args:
        md = args["modifiers"]
        if not isinstance(md, list) or len(md) > 5 or any(x not in _engine._ALLOWED_MODIFIERS for x in md):
            raise ValueError(f"modifiers 는 {' · '.join(sorted(_engine._ALLOWED_MODIFIERS))} 가운데 다섯까지의 목록이다 — {str(md)[:80]}")
        out["modifiers"] = list(md)
    if "save_report" in args:
        if not isinstance(args["save_report"], bool):
            raise ValueError("save_report 는 true|false 다")
        out["save_report"] = args["save_report"]
    if "advanced" in args:
        adv = args["advanced"]
        if not isinstance(adv, dict) or not adv:
            raise ValueError("advanced 는 비지 않은 객체다")
        known = set(vars(_engine._DEFAULT_OPTS))
        top = sorted(k for k in adv if k in ARGS)
        if top:
            raise ValueError(f"advanced 의 {top} 는 위 인자로 준다(같은 값이 두 자리에 있으면 어느 쪽이 걸렸는지 메아리가 틀린다)")
        unknown = sorted(k for k in adv if k not in known)
        if unknown:
            raise ValueError(f"advanced 의 {unknown[:5]} 는 엔진이 같은 이름으로 읽는 손잡이가 아니다")
        o = _engine._resolve_opts(dict(adv))
        if o.req_unread or o.timeout_clamped:
            raise ValueError("엔진이 그 값으로 읽지 않는다 — " + " / ".join(o.req_unread.values() if o.req_unread else
                                                               [f"timeout_s {o.timeout_clamped[0]} → {o.timeout_clamped[1]}(범위 밖)"]))
        off = [k for k, v in adv.items() if not _same(getattr(o, k), v)]
        if off:
            raise ValueError("엔진이 그 값으로 읽지 않는다 — " + ", ".join(f"{k}={adv[k]!r} → {getattr(o, k)!r}" for k in off[:5]))
        out["advanced"] = dict(adv)
    if not out:
        raise ValueError("args 가 비었다")
    return {"label": str(spec.get("label") or name)[:120], "what": str(spec.get("what") or "")[:400], "args": out}


def _mtime(p: Path):
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def _load() -> None:
    key = (_mtime(PATH), _mtime(LOCAL_PATH))
    if key == _CACHE["key"]:
        return
    errors, overlay = [], {"present": key[1] is not None}
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("presets"), dict):
            raise ValueError("presets 객체가 없다")
    except (OSError, ValueError) as exc:
        raw = {"presets": {}}
        errors.append(f"추적 표를 읽지 못했다({type(exc).__name__}: {str(exc)[:120]})")
    eff = raw
    if key[1] is not None:
        try:
            patch = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
            if not isinstance(patch, dict):
                raise ValueError("덧씌우기는 객체여야 한다")
            eff = _merge_patch(raw, patch)
            if not isinstance(eff.get("presets"), dict):
                raise ValueError("덧씌운 뒤 presets 가 객체가 아니다")
        except (OSError, ValueError) as exc:
            eff = raw
            overlay["error"] = f"덧씌우기를 버렸다({type(exc).__name__}: {str(exc)[:120]})"
            log.warning("심의 기본값 덧씌우기(%s)를 버리고 추적 표로 돈다: %r", LOCAL_PATH.name, exc)
    out = {}
    for name, spec in (eff.get("presets") or {}).items():
        try:
            out[str(name)] = _check(str(name), spec)
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(f"{str(name)[:60]} — {str(exc)[:200]}")
            log.warning("심의 기본값 %s 를 버렸다: %s", name, exc)
    _CACHE.update(key=key, presets=out, errors=errors, overlay=overlay)


def presets() -> dict:
    _load()
    return _CACHE["presets"]


def listing() -> list[dict]:
    """deliberate_jobs 의 presets — [{name, label, what, args}]."""
    return [{"name": n, **p} for n, p in presets().items()]


def state() -> dict:
    """버린 preset · 덧씌우기 사정(deliberate_jobs 의 presets_state — 문제가 있을 때만 싣는다)."""
    _load()
    return {"errors": list(_CACHE["errors"]), "overlay": dict(_CACHE["overlay"])}


def apply(name: str, given: dict) -> tuple[dict, dict]:
    """preset 을 부른 쪽 인자(given — ARGS 칸) 밑에 깐다 → (최종 인자, 메아리 {name, applied{칸: {value, from}}})."""
    spec = presets().get(str(name or "").strip())
    if spec is None:
        raise ValueError(f"그런 심의 기본값(preset)이 없다: {str(name)[:60]!r} — 있는 것: {', '.join(presets()) or '없음'}(deliberate_jobs 의 presets)")
    final, applied = dict(given), {}
    adv_given = dict(given.get("advanced") or {})
    for k, v in spec["args"].items():
        if k == "advanced":
            cur = dict(given.get("advanced") or {})
            for ak, av in v.items():
                if cur.get(ak) is not None:
                    applied[f"advanced.{ak}"] = {"value": cur[ak], "from": "caller"}
                else:
                    cur[ak] = av
                    applied[f"advanced.{ak}"] = {"value": av, "from": "preset"}
            final["advanced"] = cur
        elif adv_given.get(k) is not None:
            # 부른 쪽 advanced 의 같은 이름 손잡이가 위 인자를 이긴다(mcp_server._build_opts 가 advanced 를 맨 뒤에 덮는다) — 메아리도 그것을 말한다
            applied[k] = {"value": adv_given[k], "from": "caller", "via": "advanced"}
        elif given.get(k) != DEFAULTS[k]:
            applied[k] = {"value": given[k], "from": "caller"}
        else:
            final[k] = v
            applied[k] = {"value": v, "from": "preset"}
    return final, {"name": str(name).strip(), "applied": applied}

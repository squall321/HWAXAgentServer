# 끊긴 심의를 라운드 경계에서 그대로 잇는 재개 자료 — 엔진이 진행 일지에 쓰는 줄(e:*)의 꼴 · 쓰는 길 · 읽어 되살릴 상태(HA-09 S1)
"""재개 자료(HWAXPortal docs/request-1027/design-ha09-ha20.md A5.2 · A5.3 · A5.4).

**왜 있나.** 진행 일지(delib_jobs._Journal — S0)는 화면 사건만 적어, 재기동 뒤 원장에 끝난 라운드 · 좌석 · 턴이 남아도 이어하기는 요약 위의 새
심의였다(R3 — 끝난 라운드의 마지막 입장을 요약으로 싣고 1라운드 '초기 입장' 부터). 라운드 구조를 그대로 이을 재료(좌석의 원 출력 · 라운드 전
단계의 산출 · 조회 풀)가 사건에도 원장에도 없었다. 엔진이 그 재료를 같은 일지에 덧붙이고(여기의 줄 꼴), 주인의 이어하기가 그것을 읽어 같은 잡을
제자리에서 끊긴 라운드부터 돌린다.

**두 문맥 변수.** `journal` 은 잡 태스크의 진행 일지다 — delib_jobs._drive 가 건다. 웹 심의는 잡이 아니라 걸리지 않아 아무것도 쓰지 않는다(큰 자료가
브라우저 쪽으로 새지 않는다). `restore` 는 되살릴 상태다 — MCP 입구(mcp_server._resume)가 주인의 일지에서 만들어 delib_jobs.start(..., resume=)
의 키워드로만 넘기고, start 가 그 잡의 태스크 문맥에만 건다. **요청 손잡이(delib_opts)로는 받지 않는다** — 손잡이로 받으면 호출자가 남의 잡 id 를
적어 그 사람의 재개 자료를 읽는 길이 생긴다. 시험이 다시 읽지(reload) 않는 작은 모듈에 둔다 — deliberation 을 다시 읽어도 변수가 같다.

**줄 꼴**(머리 줄 rv = RESUME_SCHEMA, 단 열쇠 sk — 이 판은 한 단 심의 main 만 쓰고 되살린다).
  e:pre       라운드 루프 직전 — 좌석 전부(복원된 역할 · origin · lookup_hint) · 지식카드 · 근거 계수 · VOC · 선주입 · 지정 도구 결과 ·
              기준일 셈 · 출처 정수 · 자유 조회 도구 이름 · 그룹 · 창 · 라운드 수 · 화두 블록(base · base_blind)의 지문.
              호출자가 준 근거 본문(evidence)은 적지 않는다 — 시작 인자 파일에 있고, 되살릴 때 같은 인자로 다시 만든 블록을 지문으로 견준다.
  e:gather    라운드 r 의 자유 조회가 끝난 뒤 · 발언 전 — 좌석별 조회 블록 · 이번에 풀에 더한 항목과 원문 길이 · 기준일 셈 · 알림 깃발 ·
              근거 계수 · 새로 배운 출처 정수 · 교차심문 표적. 끊긴 라운드에 이 줄이 있으면 조회를 다시 하지 않는다(S2).
  e:raw       좌석 하나가 말한 직후, turn 사건을 내기 **전** — 원 출력(JSON 칸 전부). 끊긴 라운드에서 이 줄이 있는 좌석은 다시 부르지
              않는다 — 나머지(아직 안 함 · 실패)만 부른다(S2).
  e:round_end 라운드 r 끝 — 유실 좌석. 이 줄이 있는 라운드만 끝난 라운드로 되살린다(그 라운드의 사건은 모두 이 줄 앞에 적혔다).
  결정문 뒤(S3 — 전 라운드가 끝난 뒤에만 적히고, 전 라운드를 되살릴 때만 읽는다. 줄 하나가 그 단계가 끝났다는 표지다 — 그 단계의 사건은 모두
  그 줄 앞에 적혔다. 없으면 그 단계를 아직 하지 않은 것이다).
  e:decision  의장 결정문이 나온 뒤 — 결정문(의장이 못 냈으면 회의록 보존 글) · 의장 실패 표지(failed · knob · cause). 있으면 의장을 다시
              부르지 않는다.
  e:plain     핵심 요약 · 쉬운 설명을 붙인 뒤 — 쉬운 설명 · 그것까지 붙인 결정문. 있으면 요약 · 쉬운 설명을 다시 부르지 않는다.
  e:report_intent  Report Archive 호출 직전 — 저장하나(save) · 붙일 보고서 번호(append_to) · 제목 · 저장할 결정문(웹 인용 · 수치 대조까지 붙인
              것). 이 줄만 있으면 저장 도중 끊긴 것이라 이미 저장됐을 수 있다 — 이으면 경고 한 번과 함께 새로 저장한다(RA 에 멱등 열쇠가 없다).
  e:report_saved   보고서 단계 뒤 — 받은 번호(rid) · 결정문 끝에 붙는 안내(note). 있으면 다시 저장하지 않고 그 번호를 쓴다.
  e:wiki_sent 위키 단계 뒤(켠 심의만) — 결정문 끝에 붙는 안내(note). 있으면 다시 보내지 않는다. 원천 열쇠는 적지 않는다 — 잡 id 에서
              정해져(delib:<잡 id>:<틀>) 같은 잡을 잇는 한 늘 같다(보내던 중에 끊겨 다시 보내도 MX 백서가 같은 원천으로 받는다).
  e:resume_off 상한 · 꼴 때문에 엔진 줄을 멈췄다 — 그 잡의 이어하기는 요약으로 간다.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
from pathlib import Path

# 재개 자료의 꼴 번호 — 줄 꼴(위)을 바꾸는 커밋이 올린다. 일지 머리(rv)와 다르면 되살리지 않고 요약으로 잇는다.
# 2 — 결정문 뒤 줄(S3). 그 줄이 없으면 '그 단계를 아직 하지 않았다' 로 읽으므로, 그 줄을 쓰지 않던 판(1)의 일지는 그렇게 읽을 수 없다.
RESUME_SCHEMA = 2
STAGE = "main"                 # 이 판이 되살리는 단 — 한 단 심의(run_deliberation). 해석 설계 · 구축 계획(sim-*) · 시험 설계(tp)는 S4
# 결정문 뒤 줄 — 엔진이 적는 차례다(이 차례로 옮겨 적는다).
POST = ("e:decision", "e:plain", "e:report_intent", "e:report_saved", "e:wiki_sent")


def _engine_rev() -> str:
    """엔진 판 — deliberation.py 내용의 지문(앞 12자). 판만 다르면 막지 않고 카드로 알린다(남은 좌석은 새 판의 프롬프트로 말한다)."""
    try:
        return hashlib.sha256(Path(__file__).with_name("deliberation.py").read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


ENGINE = _engine_rev()

journal: contextvars.ContextVar = contextvars.ContextVar("delib_resume_journal", default=None)
restore: contextvars.ContextVar = contextvars.ContextVar("delib_resume_restore", default=None)


def digest(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def on(sk) -> bool:
    """이 단의 재개 자료를 일지에 쓰는가 — 잡 태스크(진행 일지가 걸렸다)이고 단 열쇠가 있을 때만."""
    return bool(sk) and journal.get() is not None


def write(sk, kind: str, *, sync: bool = False, **data) -> None:
    """엔진의 재개 자료 한 줄 — 일지가 없거나(웹 심의) 단 열쇠가 없으면(다단 · 시험 설계 — S4) 아무것도 안 한다."""
    jr = journal.get()
    if jr is not None and sk:
        jr.engine({"k": kind, "sk": sk, **data}, sync=sync)


def stage(sk):
    """이 단을 되살릴 상태(StageData) — 제자리 이어하기의 그 단만. 없으면 None(지금과 같은 길)."""
    r = restore.get()
    return r.stages.get(sk) if (r is not None and sk) else None


def _restorable(seats: list, rounds: int, ended: dict, spoke: dict, stop_after_round: int = 0) -> int:
    """되살릴 끝난 라운드 수 k — 1 부터 잇달아 e:round_end 가 있고 그 라운드의 말한 좌석 ∪ 유실 좌석이 앉은 좌석과 같은 데까지.
    체크포인트 심의(stop_after_round=1)는 1라운드를 되살리지 않는다 — 되살리면 루프가 체크포인트(결정문 자리)를 지나쳐 2라운드를 돈다."""
    k = 0
    want = set(seats)
    for r in range(1, int(rounds) + 1):
        if r == 1 and stop_after_round == 1:
            break
        if r not in ended or (set(spoke.get(r) or ()) | set(ended[r] or ())) != want:
            break
        k = r
    return k


def _seat_list(xs) -> list:
    return list(dict.fromkeys(x for x in xs if isinstance(x, str) and x))


class Tracker:
    """e:* 줄을 받아 좌석 단위 재개 요약(seat_resume)을 만든다 — 일지가 쓰는 동안(delib_jobs._Journal)과 재기동 정리가 접는 동안(_fold_journal)이
    같은 것을 써서, 내려가며 쓴 원장과 재기동이 접은 원장이 같은 요약을 싣는다. 원 출력은 들고 있지 않는다(좌석 키만)."""

    def __init__(self):
        self.pre: dict | None = None      # {"seats": [...], "rounds": N}
        self.ended: dict = {}             # r → [유실 좌석]
        self.spoke: dict = {}             # r → [말한 좌석]
        self.off = ""                     # 엔진 줄을 멈춘 까닭(e:resume_off)
        self.rv = None                    # 일지 머리의 재개 자료 꼴 번호 — 없으면 이 판 이전의 일지다
        self.bad = 0                      # 가운데 깨진 줄 수(읽는 쪽이 센다) — 있으면 되살리지 않는다(parse 와 같은 규칙)

    def feed(self, rec: dict) -> None:
        k = rec.get("k")
        if k == "j:head":
            self.rv = rec.get("rv")
            return
        if not (isinstance(k, str) and k.startswith("e:")):
            return
        if k == "e:resume_off":
            self.off = str(rec.get("why") or "까닭 없음")[:200]
            return
        if rec.get("sk") != STAGE:
            return
        if k == "e:pre":
            ps = rec.get("personas")
            self.pre = {"seats": _seat_list(p.get("key") for p in ps if isinstance(p, dict)) if isinstance(ps, list) else [],
                        "rounds": rec.get("rounds") if isinstance(rec.get("rounds"), int) else 0}
            self.ended, self.spoke = {}, {}
            return
        try:
            r = int(rec.get("r"))
        except (TypeError, ValueError):
            return
        if k == "e:raw":
            o = rec.get("o")
            if isinstance(o, dict) and isinstance(o.get("persona"), str):
                self.spoke.setdefault(r, []).append(o["persona"])
        elif k == "e:round_end":
            lost = rec.get("lost")
            self.ended[r] = _seat_list(lost) if isinstance(lost, list) else []

    def summary(self, *, general: bool = True, stop_after_round: int = 0) -> dict:
        """{ok, sk, round, spoken, todo, why} — round 는 이으면 처음 부를 라운드(전 라운드가 끝났으면 라운드 수 + 1 — 의장부터), spoken 은 그
        라운드에서 끊기기 전에 말한 좌석(원 출력이 적힌 좌석 — 다시 부르지 않는다), todo 는 그 라운드에서 부를 나머지 좌석이다(아직 말하지
        않았거나 실패한 좌석 — 앉은 차례대로, S2)."""
        out = {"ok": False, "sk": None, "round": None, "spoken": [], "todo": [], "why": ""}
        if not general:
            out["why"] = ("다단 심의 · 시험 설계는 끝난 단(체크포인트)까지만 잇는다 — 단 안을 라운드 경계에서 잇는 재개 자료는 아직 쓰지 "
                          "않는다(HA-09 S4)")
        elif self.rv is None:
            out["why"] = "재개 자료가 없다(좌석 단위 이어하기 이전 판의 일지이거나 진행 일지가 없다) — 이으면 요약으로 잇는다"
        elif self.rv != RESUME_SCHEMA:
            out["why"] = f"재개 자료의 꼴이 이 판과 다르다(일지 {self.rv} · 이 판 {RESUME_SCHEMA}) — 이으면 요약으로 잇는다"
        elif self.off:
            out["why"] = f"재개 자료를 끝까지 쓰지 못했다({self.off}) — 이으면 요약으로 잇는다"
        elif self.bad:
            out["why"] = f"진행 일지에 깨진 줄이 {self.bad}개 있다 — 되살린 상태를 믿을 수 없어 이으면 요약으로 잇는다"
        elif self.pre is None:
            out["why"] = "라운드 전 단계에서 끊겨 되살릴 재개 자료가 없다(좌석 · 근거가 서기 전) — 이으면 같은 인자로 처음부터 돈다"
        if out["why"]:
            return out
        seats, rounds = self.pre["seats"], int(self.pre["rounds"] or 0)
        k = _restorable(seats, rounds, self.ended, self.spoke, stop_after_round)
        cut = k + 1
        spoken = _seat_list(self.spoke.get(cut) or ()) if cut <= rounds else []
        if set(spoken) - set(seats):          # parse 와 같은 규칙 — 이어하기가 쓰지 않을 재개 자료를 진행 조회가 된다고 말하지 않는다
            return {**out, "why": "재개 자료에 앉지 않은 좌석의 원 출력이 있다 — 되살린 상태를 믿을 수 없어 이으면 요약으로 잇는다"}
        return {"ok": True, "sk": STAGE, "round": cut, "spoken": spoken,
                "todo": [s for s in seats if s not in spoken] if cut <= rounds else [], "why": ""}


class StageData:
    """한 단을 되살릴 상태 — 엔진 갈고리가 읽는다. pre 는 e:pre 줄 그대로, rounds 는 되살릴 끝난 라운드 r → {cur, gather, lost}.
    cut 은 끊긴 라운드(끝난 라운드 다음 — 라운드 수 안일 때만, 아니면 None) {r, gather, raw} 다(S2). gather 는 그 라운드의 e:gather(조회가
    끝나기 전에 끊겼으면 None — 조회를 다시 한다), raw 는 끊기기 전에 말한 좌석의 원 출력이다(일지 순서 = 그 라운드에서 끝난 순서).
    post 는 결정문 뒤의 끝난 단계(S3) — 줄 종류(POST) → 그 줄. 전 라운드를 되살릴 때만 찬다."""

    def __init__(self, pre: dict, rounds: dict, info: dict, cut: dict | None = None, post: dict | None = None):
        self.pre, self.rounds, self.info, self.cut, self.post = pre, rounds, info, cut, post or {}

    def round(self, r: int):
        return self.rounds.get(r)


class Resume:
    """제자리 이어하기의 재개 상태 — MCP 입구가 주인의 일지에서 만들어 delib_jobs.start(..., resume=) 로만 넘긴다.
    carry 는 새 일지에 그대로 옮겨 쓸 e:* 줄(되살릴 것만 — 다음 재기동에도 남게), info 는 응답 · 안내에 싣는 것, groups 는 끊기기 전의 그룹이다."""

    def __init__(self, job_id: str, stages: dict, carry: list, info: dict, groups: list):
        self.job_id, self.stages, self.carry, self.info, self.groups = job_id, stages, carry, info, groups
        self.by = ""                       # 이은 사람(시도 기록 attempts 의 by)


def _rec(raw: bytes):
    try:
        x = json.loads(raw)
    except ValueError:
        return None
    return x if isinstance(x, dict) else None


_PRE_KEYS = {"personas": list, "rounds": int, "knowledge": dict, "ev_count": dict, "ids": list, "groups": list,
             "ao_pre": dict, "ao_des": dict, "free": list}


def parse(raw: bytes, job_id: str, *, journal_v: int, stop_after_round: int = 0):
    """진행 일지 바이트 → (Resume, "") 또는 (None, 못 쓰는 까닭). 반쯤 쓴 마지막 줄은 버린다. 깨진 줄 · 꼴이 다른 머리 · 멈춘 엔진 줄이 있으면 쓰지 않는다
    — 되살린 상태가 끊기기 전과 같다는 보장이 없으면 요약으로 잇는 것이 낫다."""
    lines = raw.split(b"\n")
    lines.pop()
    head = _rec(lines[0]) if lines else None
    if not (head and head.get("k") == "j:head" and head.get("v") == journal_v and head.get("id") == job_id):
        return None, "진행 일지의 머리가 이 잡 · 이 판의 꼴이 아니다"
    if head.get("rv") != RESUME_SCHEMA:
        return None, f"재개 자료의 꼴이 이 판과 다르다(일지 {head.get('rv')} · 이 판 {RESUME_SCHEMA})"
    pre, gather, raws, ended, post = None, {}, {}, {}, {}
    for ln in lines[1:]:
        rec = _rec(ln)
        if rec is None:
            return None, "진행 일지에 깨진 줄이 있다"
        k = rec.get("k")
        if k == "e:resume_off":
            return None, f"재개 자료를 끝까지 쓰지 못했다({str(rec.get('why') or '까닭 없음')[:200]})"
        if not (isinstance(k, str) and k.startswith("e:")) or rec.get("sk") != STAGE:
            continue
        if k == "e:pre":
            pre, gather, raws, ended, post = rec, {}, {}, {}, {}
            continue
        if pre is None:
            continue
        if k in POST:                     # 결정문 뒤 줄(S3) — 라운드 번호가 없다
            post[k] = rec
            continue
        try:
            r = int(rec.get("r"))
        except (TypeError, ValueError):
            return None, "재개 자료에 라운드 번호가 없는 줄이 있다"
        if k == "e:gather":
            gather[r] = rec
        elif k == "e:raw":
            o = rec.get("o")
            if not (isinstance(o, dict) and isinstance(o.get("persona"), str)):
                return None, "재개 자료에 좌석 원 출력의 꼴이 다른 줄이 있다"
            raws.setdefault(r, {}).setdefault(o["persona"], rec)          # 같은 좌석이 두 번이면 먼저 적힌 것(한 일지에서는 없다)
        elif k == "e:round_end":
            ended[r] = rec
    if pre is None:
        return None, "라운드 전 단계에서 끊겨 되살릴 재개 자료가 없다(좌석 · 근거가 서기 전)"
    if any(not isinstance(pre.get(key), typ) or isinstance(pre.get(key), bool) for key, typ in _PRE_KEYS.items()):
        return None, "재개 자료(e:pre)의 꼴이 이 판과 다르다"
    seats = _seat_list(p.get("key") for p in pre["personas"] if isinstance(p, dict))
    if len(seats) != len(pre["personas"]) or len(seats) < 2:
        return None, "재개 자료(e:pre)의 좌석 꼴이 이 판과 다르다"
    n = pre["rounds"]
    k = _restorable(seats, n, {r: e.get("lost") or [] for r, e in ended.items()},
                    {r: list(x) for r, x in raws.items()}, stop_after_round)
    rounds, carry = {}, [pre]
    for r in range(1, k + 1):
        cur = [x["o"] for x in raws.get(r, {}).values()]
        lost = _seat_list(ended[r].get("lost") or [])
        rounds[r] = {"cur": cur, "gather": gather.get(r), "lost": lost}
        carry += ([gather[r]] if r in gather else []) + list(raws.get(r, {}).values()) + [ended[r]]
    cut, cutd, spoken = k + 1, None, []
    if k < n:
        # 끊긴 라운드(S2) — 끊기기 전에 말한 좌석(e:raw)은 다시 부르지 않고 원 출력으로 되살리고, 조회가 끝났으면(e:gather) 다시 하지 않는다.
        # 그 줄도 새 일지에 옮겨 쓴다 — 이은 실행이 그 라운드에서 또 끊겨도 앞 시도에서 말한 좌석 · 조회가 남는다(라운드 끝 줄은 옮기지 않는다 —
        # 그 라운드는 이은 실행이 맺는다).
        craw = raws.get(cut, {})
        if set(craw) - set(seats):
            return None, "재개 자료에 앉지 않은 좌석의 원 출력이 있다"
        spoken = list(craw)
        cutd = {"r": cut, "gather": gather.get(cut), "raw": [x["o"] for x in craw.values()]}
        carry += ([gather[cut]] if cut in gather else []) + list(craw.values())
    # 결정문 뒤(S3) — 그 줄은 전 라운드가 끝난 뒤에만 적힌다. 전 라운드를 되살릴 때만 쓰고 새 일지에 옮겨 쓴다 — 이은 실행이 또 끊겨도 끝난
    # 단계(저장한 보고서 번호 · 보낸 위키)가 남는다.
    post = post if k == n else {}
    carry += [post[x] for x in POST if x in post]
    sav, it = post.get("e:report_saved"), post.get("e:report_intent") or {}
    info = {"sk": STAGE, "round": cut, "restored": k, "total": n, "spoken": spoken,
            "todo": [s for s in seats if s not in spoken] if k < n else [], "gathered": cut in gather if k < n else False,
            "eng_changed": bool(head.get("eng")) and head.get("eng") != ENGINE, "ctx": pre.get("ctx"),
            # 응답 안내에 싣는 결정문 뒤 단계 — report 는 saved(저장했다) · intent(저장 도중 끊겼다 — 이미 저장됐을 수 있다) · "" 다
            "post": {"decision": "e:decision" in post, "chair_failed": bool((post.get("e:decision") or {}).get("failed")),
                     "plain": "e:plain" in post, "wiki": "e:wiki_sent" in post,
                     "report": "saved" if sav else "intent" if it.get("save") else "",
                     "report_id": sav.get("rid") if sav else None, "append_to": it.get("append_to") or 0,
                     "title": it.get("title") or ""}}
    return Resume(job_id, {STAGE: StageData(pre, rounds, info, cutd, post)}, carry, info, list(pre["groups"])), ""

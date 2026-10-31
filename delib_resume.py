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

**줄 꼴**(머리 줄 rv = RESUME_SCHEMA, 단 열쇠 sk — 한 단 심의 main, 해석 설계 · 구축 계획 sim-1 · sim-2 · sim-3, 시험 설계 tp. 단마다 같은
줄을 쓴다 — 다단 심의는 단이 차례로 돌아 뒤 단의 e:pre 가 적혔으면 앞 단은 끝났다. 되살리는 단은 마지막 단 표지(e:stage_in · e:pre)의 단 하나다).
  e:stage_in  그 단에 들기 직전(2단 · 3단 · 시험 설계 — S4) — 엔진이 단 밖에서 따로 부른 것과 앞 단의 산출. 2단은 1단 결정문 · 양보 불가
              조항 · 1단 좌석(체크포인트와 같은 값) · 좌석 전환 결과 · 사내 자산 현황 글(2단 사람 의견에 붙는 것), 3단은 2단 결정문 · 좌석,
              시험 설계는 물성 근거 현황 글. 2단 · 3단은 wiki_off(앞 단이 위키 꺼짐을 이미 알렸나 — HA-08 한 번만)도 적는다(시험 설계는 앞 단이
              없다). 끊긴 단이 이 단이면 앞 단 · 좌석 전환 · 현황 조회를 다시 하지 않고 이 값으로 그 단에 든다. 이 줄
              뒤 · 그 단의 e:pre 전에 끊겼으면(그 단의 라운드 전 단계) 되살리지 않는다 — 요약 길이 체크포인트로 끝난 단을 건너뛴다.
  e:pre       라운드 루프 직전 — 좌석 전부(복원된 역할 · origin · lookup_hint) · 지식카드 · 근거 계수 · VOC · 선주입 · 지정 도구 결과 ·
              기준일 셈 · 출처 정수 · 자유 조회 도구 이름 · 그룹 · 창 · 라운드 수 · 화두 블록(base · base_blind)의 지문.
              호출자가 준 근거 본문(evidence)은 적지 않는다 — 시작 인자 파일에 있고, 되살릴 때 같은 인자로 다시 만든 블록을 지문으로 견준다.
  e:gather    라운드 r 의 자유 조회가 끝난 뒤 · 발언 전 — 좌석별 조회 블록 · 이번에 풀에 더한 항목과 원문 길이 · 기준일 셈 · 알림 깃발 ·
              근거 계수 · 새로 배운 출처 정수 · 교차심문 표적. 끊긴 라운드에 이 줄이 있으면 조회를 다시 하지 않는다(S2).
  e:raw       좌석 하나가 말한 직후, turn 사건을 내기 **전** — 원 출력(JSON 칸 전부). 끊긴 라운드에서 이 줄이 있는 좌석은 다시 부르지
              않는다 — 나머지(아직 안 함 · 실패)만 부른다(S2).
  e:round_end 라운드 r 끝 — 유실 좌석. 이 줄이 있는 라운드만 끝난 라운드로 되살린다(그 라운드의 사건은 모두 이 줄 앞에 적혔다).
  e:round_in  라운드 r 에 든다(되살리지 않고 도는 라운드마다 — 라운드 머리 카드보다 먼저). r 이 라운드 수 + 1 이면 결정문 단계(의장 · 요약 ·
              보고서 · 위키)에 든다. 이 줄 뒤에 원장에 실린 알림(경고 · 좌석에 주지 않은 카드)이 그 자리의 알림이다 — 끊긴 자리가 그 라운드
              (단계)이면 이은 실행이 같은 일을 다시 하며 같은 알림을 내도 원장에 다시 싣지 않는다(Seen). 그 라운드 · 단계의 재개 자료 줄
              (e:gather · e:raw · e:round_end · e:decision …)은 그 일이 끝나야 적혀, 알림만 원장에 닿고 끊기면 종전엔 알림이 두 번 실렸다.
  e:note      끊긴 자리의 그 알림(사건 이름 ev · 본문 d) — 일지를 고쳐 쓸 때 공개 사건 대신 옮겨 적는다(공개 사건은 옮기면 재기동 정리가
              또 접는다). 이은 실행이 같은 자리에서 또 끊겨도 앞 시도들의 알림을 모두 안다.
  결정문 뒤(S3 — 전 라운드가 끝난 뒤에만 적히고, 전 라운드를 되살릴 때만 읽는다. 줄 하나가 그 단계가 끝났다는 표지다 — 그 단계의 사건은 모두
  그 줄 앞에 적혔다. 없으면 그 단계를 아직 하지 않은 것이다).
  e:decision  의장 결정문이 나온 뒤 — 결정문(의장이 못 냈으면 회의록 보존 글) · 의장 실패 표지(failed · knob · cause). 있으면 의장을 다시
              부르지 않는다.
  e:plain     핵심 요약 · 쉬운 설명을 붙인 뒤 — 쉬운 설명 · 그것까지 붙인 결정문. 있으면 요약 · 쉬운 설명을 다시 부르지 않는다.
  e:report_intent  Report Archive 호출 직전 — 저장하나(save) · 붙일 보고서 번호(append_to) · 제목 · 저장할 결정문(웹 인용 · 수치 대조까지 붙인
              것). 이 줄만 있으면 저장 도중 끊긴 것이라 이미 저장됐을 수 있다 — 이으면 경고 한 번과 함께 새로 저장한다(RA 에 멱등 열쇠가 없다).
  e:report_saved   보고서 단계 뒤 — 받은 번호(rid) · 결정문 끝에 붙는 안내(note). 있으면 다시 저장하지 않고 그 번호를 쓴다.
  e:wiki_sent 위키 단계 뒤(켠 심의만) — 결정문 끝에 붙는 안내(note)와 그 뒤의 위키 꺼짐 상태(off). 있으면 다시 보내지 않고 꺼짐도
              되돌린다(다단 심의의 뒤 단이 꺼짐 안내를 또 붙이지 않게). 원천 열쇠는 적지 않는다 — 잡 id 에서
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
# S4(다단 · 시험 설계의 단 열쇠와 e:stage_in)는 올리지 않는다 — 한 단 심의의 줄은 그대로고, 그 앞 판의 다단 · 시험 설계 일지에는 재개 자료가
# 한 줄도 없어(그 엔진은 단 열쇠 없이 돌았다) 잘못 읽을 줄이 없다(e:pre 가 없어 요약으로 잇는다).
# 위키 꺼짐 칸(e:wiki_sent off · e:stage_in wiki_off — 검토 10-31)도 올리지 않는다 — 덧붙인 칸이고, 없으면 꺼지지 않은 것으로 읽어 그 칸 앞의
# 읽기 그대로다(뒤 단이 꺼짐 안내를 한 번 더 붙일 뿐 되살린 라운드 · 좌석은 같다). 올리면 그 앞 일지는 요약 길로 가 라운드를 다시 산다.
RESUME_SCHEMA = 2
STAGE = "main"                 # 한 단 심의(run_deliberation)의 단 열쇠
SIM1, SIM2, SIM3 = "sim-1", "sim-2", "sim-3"      # 해석 설계 · 구축 계획(run_sim_deliberation)의 1단(메커니즘) · 2단(해석 설계) · 3단(구축 계획)
TP = "tp"                      # 시험 설계(run_test_plan)
# 엔진 종류(delib_jobs.JOBS 의 engine) → 그 잡의 단 열쇠(도는 차례). 일지의 줄이 이 밖의 단이면 되살리지 않는다.
STAGES = {"general": (STAGE,), "sim": (SIM1, SIM2, SIM3), "test-plan": (TP,)}
# 단에 들기 전에 엔진이 단 밖에서 부르는 단(좌석 전환 · 사내 자산 현황 · 물성 현황) — 그 단을 되살리려면 e:stage_in 이 있어야 한다. 값은 그
# 줄에 있어야 할 칸과 꼴이다(글 칸은 문자열, 목록 칸은 목록 — 좌석 목록은 key 가 있는 객체만).
STAGE_IN = {SIM2: {"decision_a": str, "nn_a": list, "personas_a": list, "seats": list, "note": str},
            SIM3: {"decision_b": str, "seats": list},
            TP: {"note": str}}
STAGE_LABEL = {SIM1: "1단(메커니즘)", SIM2: "2단(해석 설계)", SIM3: "3단(구축 계획)", TP: "시험 설계"}
# 결정문 뒤 줄 — 엔진이 적는 차례다(이 차례로 옮겨 적는다).
POST = ("e:decision", "e:plain", "e:report_intent", "e:report_saved", "e:wiki_sent")


def notice_key(ev: str, data) -> str | None:
    """원장에 알림으로 남는 사건(경고 · 좌석에 주지 않은 카드 — delib_jobs._apply 가 warnings · evidence_omitted 에 싣는 것)이면 견줄 열쇠, 아니면
    None. 일지는 그 둘을 받은 그대로 적는다(_journal_form) — 일지의 줄과 다시 낸 사건이 같은 열쇠다."""
    if not isinstance(data, dict):
        return None
    if ev == "warning" or (ev == "delib" and data.get("kind") == "evidence" and data.get("included") is False):
        return json.dumps([ev, data], ensure_ascii=False, sort_keys=True)
    return None


class Seen:
    """끊긴 자리(라운드 r — 라운드 수 + 1 이면 결정문 단계)에서 끊기기 전에 원장에 실린 알림 — 이은 실행이 그 자리에서 같은 알림을 다시 내면
    원장 · 일지에 다시 싣지 않는다(delib_jobs._drive). 엔진의 e:round_in(그 단 · 그 자리)에서 켜지고 그 자리가 끝나면(e:round_end · 그 단의 결정문
    사건 · 다른 단의 표지) 꺼진다. 같은 알림이 그 자리에서 여러 번 났으면 그 수만큼만 거른다 — 끊지 않은 실행의 수와 같아진다. 다른 알림(조회가
    이번엔 다르게 끝났다)은 그대로 싣는다."""

    def __init__(self, sk: str, r: int, notes: list):
        self.sk, self.r, self.on = sk, r, False
        self.left = [x for x in (notice_key(ev, d) for ev, d in notes) if x is not None]

    def _here(self, rec: dict) -> bool:
        return rec.get("sk") == self.sk and rec.get("r") == self.r

    def gate(self, rec: dict) -> None:
        """엔진의 재개 자료 한 줄 — 그 자리에 들면 켜고, 그 자리가 끝나면 끈다(다시 켜지지 않는다)."""
        k = rec.get("k")
        if k == "e:round_in" and self._here(rec):
            self.on = True
        elif self.on and (k in ("e:pre", "e:stage_in") or (k == "e:round_end" and self._here(rec))):
            self.on, self.left = False, []

    def again(self, ev: str, data) -> bool:
        """이 사건이 끊기기 전에 원장에 이미 실린 그 자리의 알림인가(참이면 거른다 — 하나를 지운다)."""
        if not self.on:
            return False
        if ev == "delib" and isinstance(data, dict) and data.get("kind") == "decision":
            self.on, self.left = False, []          # 그 단의 결정문 — 결정문 단계가 끝났다(다단 심의의 다음 단 알림을 거르지 않는다)
            return False
        key = notice_key(ev, data)
        if key is None or key not in self.left:
            return False
        self.left.remove(key)
        return True


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
    """엔진의 재개 자료 한 줄 — 일지가 없거나(웹 심의) 단 열쇠가 없으면(리스크 패널처럼 단 열쇠 없이 부르는 스트림) 아무것도 안 한다."""
    jr = journal.get()
    if jr is not None and sk:
        jr.engine({"k": kind, "sk": sk, **data}, sync=sync)


def stage(sk):
    """이 단을 되살릴 상태(StageData) — 제자리 이어하기의 그 단만. 없으면 None(지금과 같은 길)."""
    r = restore.get()
    return r.stages.get(sk) if (r is not None and sk) else None


def stage_in(sk):
    """이 단에 들 때의 재개 자료(e:stage_in 줄 — S4) — 제자리 이어하기의 끊긴 단이 이 단일 때만. 있으면 엔진이 앞 단 · 좌석 전환 · 현황 조회를
    건너뛰고 이 값으로 그 단에 든다. 없으면 None(지금과 같은 길)."""
    r = restore.get()
    return r.stage_in.get(sk) if (r is not None and sk) else None


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


# 되살리지 않는 까닭 — 진행 조회의 재개 요약(Tracker)과 이어하기의 일지 읽기(parse)가 같은 글을 쓴다(S1-D13).
_WHY_PRE = "라운드 전 단계에서 끊겨 되살릴 재개 자료가 없다(좌석 · 근거가 서기 전)"
_WHY_ODD = "재개 자료의 단이 이 잡의 종류와 다르다"


def _why_entered(sk) -> str:
    """그 단에 들었는데(e:stage_in) 그 단의 라운드 전 단계에서 끊겼다(e:pre 전) — 그 단을 되살릴 것이 없다. 끝난 단은 요약 길이 체크포인트로
    건너뛴다(2단 · 3단) — 시험 설계는 단이 하나라 처음부터다."""
    return (f"{STAGE_LABEL.get(sk, sk)}의 라운드 전 단계에서 끊겨 그 단을 되살릴 재개 자료가 없다(좌석 · 근거가 서기 전) — 이으면 "
            + ("끝난 단은 체크포인트로 건너뛰고 그 단을 같은 인자로 처음부터 돈다" if sk in (SIM2, SIM3) else "같은 인자로 처음부터 돈다"))


def _why_no_in(sk) -> str:
    return f"{STAGE_LABEL.get(sk, sk)}에 들 때의 재개 자료(e:stage_in)가 없다 — 앞 단 · 좌석 전환 · 현황 조회의 값을 되살릴 수 없다"


def why_between(sk) -> str:
    """끊긴 단이 끝났다(원장의 체크포인트가 그 단이다) — 다음 단에 들기 전(좌석 전환 · 현황 조회)에 끊겼다. 되살릴 단 안이 없고, 끝난 단을
    되살려 다시 맺으면 다음 단 들머리의 카드가 두 번 실릴 수 있다 — 요약 길이 체크포인트로 끝난 단을 건너뛴다(같은 비용, S4)."""
    return (f"{STAGE_LABEL.get(sk, sk)}은 끝났고 다음 단에 들기 전(좌석 전환 · 현황 조회)에 끊겼다 — 이으면 끝난 단은 체크포인트로 "
            "건너뛰고 다음 단을 같은 인자로 처음부터 돈다")


class Tracker:
    """e:* 줄을 받아 좌석 단위 재개 요약(seat_resume)을 만든다 — 일지가 쓰는 동안(delib_jobs._Journal)과 재기동 정리가 접는 동안(_fold_journal)이
    같은 것을 써서, 내려가며 쓴 원장과 재기동이 접은 원장이 같은 요약을 싣는다. 원 출력은 들고 있지 않는다(좌석 키만).

    다단 심의 · 시험 설계(S4)는 마지막 단 표지(e:stage_in · e:pre)의 단을 센다 — 단은 차례로 돌아 뒤 단의 표지가 적혔으면 앞 단은 끝났다."""

    def __init__(self):
        self.pre: dict | None = None      # 마지막 e:pre 의 {"seats": [...], "rounds": N}
        self.sk = None                    # 마지막 단 표지의 단 — 끊긴 단
        self.entered = False              # 마지막 단 표지가 e:stage_in 이다(그 단의 라운드 전 단계에서 끊겼다 — e:pre 전)
        self.sin: set = set()             # e:stage_in 이 적힌 단
        self.sks: set = set()             # 일지에 나온 단 열쇠 전부 — 잡의 종류와 견준다(parse 와 같은 규칙)
        self.ended: dict = {}             # r → [유실 좌석] (끊긴 단의 것)
        self.spoke: dict = {}             # r → [말한 좌석] (끊긴 단의 것)
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
        sk = rec.get("sk")
        sk = sk if isinstance(sk, str) else "?"           # 꼴이 다른 단 열쇠 — 어느 잡의 단도 아니다(summary 가 되살리지 않는다)
        self.sks.add(sk)
        if k == "e:stage_in":
            self.sk, self.entered = sk, True
            self.sin.add(sk)
            return
        if k == "e:pre":
            ps = rec.get("personas")
            self.sk, self.entered = sk, False
            self.pre = {"seats": _seat_list(p.get("key") for p in ps if isinstance(p, dict)) if isinstance(ps, list) else [],
                        "rounds": rec.get("rounds") if isinstance(rec.get("rounds"), int) else 0}
            self.ended, self.spoke = {}, {}
            return
        if sk != self.sk or self.entered:
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

    def summary(self, *, stages: tuple = (STAGE,), stop_after_round: int = 0, done_sk=None) -> dict:
        """{ok, sk, round, spoken, todo, why} — sk 는 끊긴 단(stages — 그 잡의 단 열쇠, STAGES), round 는 이으면 그 단에서 처음 부를 라운드(전
        라운드가 끝났으면 라운드 수 + 1 — 의장부터), spoken 은 그 라운드에서 끊기기 전에 말한 좌석(원 출력이 적힌 좌석 — 다시 부르지 않는다),
        todo 는 그 라운드에서 부를 나머지 좌석이다(아직 말하지 않았거나 실패한 좌석 — 앉은 차례대로, S2). done_sk 는 원장의 체크포인트가 말하는
        끝난 단이다 — 끊긴 단이 그 단이면 단 사이에서 끊긴 것이라 되살리지 않는다(delib_jobs.resume_load 와 같은 규칙)."""
        out = {"ok": False, "sk": None, "round": None, "spoken": [], "todo": [], "why": ""}
        if not stages:
            out["why"] = "알 수 없는 심의 종류다 — 이으면 요약으로 잇는다"
        elif self.rv is None:
            out["why"] = "재개 자료가 없다(좌석 단위 이어하기 이전 판의 일지이거나 진행 일지가 없다) — 이으면 요약으로 잇는다"
        elif self.rv != RESUME_SCHEMA:
            out["why"] = f"재개 자료의 꼴이 이 판과 다르다(일지 {self.rv} · 이 판 {RESUME_SCHEMA}) — 이으면 요약으로 잇는다"
        elif self.off:
            out["why"] = f"재개 자료를 끝까지 쓰지 못했다({self.off}) — 이으면 요약으로 잇는다"
        elif self.bad:
            out["why"] = f"진행 일지에 깨진 줄이 {self.bad}개 있다 — 되살린 상태를 믿을 수 없어 이으면 요약으로 잇는다"
        elif self.sks - set(stages):
            out["why"] = f"{_WHY_ODD} — 되살린 상태를 믿을 수 없어 이으면 요약으로 잇는다"
        elif self.entered:
            out["why"] = _why_entered(self.sk)
        elif self.pre is None:
            out["why"] = f"{_WHY_PRE} — 이으면 같은 인자로 처음부터 돈다"
        elif self.sk in STAGE_IN and self.sk not in self.sin:
            out["why"] = f"{_why_no_in(self.sk)} — 이으면 요약으로 잇는다"
        elif done_sk is not None and self.sk == done_sk:
            out["why"] = why_between(self.sk)
        if out["why"]:
            return out
        seats, rounds = self.pre["seats"], int(self.pre["rounds"] or 0)
        k = _restorable(seats, rounds, self.ended, self.spoke, stop_after_round)
        cut = k + 1
        spoken = _seat_list(self.spoke.get(cut) or ()) if cut <= rounds else []
        if set(spoken) - set(seats):          # parse 와 같은 규칙 — 이어하기가 쓰지 않을 재개 자료를 진행 조회가 된다고 말하지 않는다
            return {**out, "why": "재개 자료에 앉지 않은 좌석의 원 출력이 있다 — 되살린 상태를 믿을 수 없어 이으면 요약으로 잇는다"}
        return {"ok": True, "sk": self.sk, "round": cut, "spoken": spoken,
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
    carry 는 새 일지에 그대로 옮겨 쓸 e:* 줄(되살릴 것만 — 다음 재기동에도 남게), info 는 응답 · 안내에 싣는 것, groups 는 끊기기 전의 그룹이다.
    stages 는 끊긴 단 하나 → StageData, stage_in 은 그 단에 들 때의 e:stage_in 줄(2단 · 3단 · 시험 설계만 — S4)이다."""

    def __init__(self, job_id: str, stages: dict, carry: list, info: dict, groups: list, stage_in: dict | None = None,
                 notes: tuple | None = None):
        self.job_id, self.stages, self.carry, self.info, self.groups = job_id, stages, carry, info, groups
        self.stage_in = stage_in or {}
        self.by = ""                       # 이은 사람(시도 기록 attempts 의 by)
        # 끊긴 자리(단 · 라운드 — 라운드 수 + 1 은 결정문 단계)에서 끊기기 전에 원장에 실린 알림 (sk, r, [(ev, d)]) — 없으면 None. 잡이 Seen 으로 건다
        self.notes = notes

    def seen(self) -> "Seen | None":
        return Seen(*self.notes) if self.notes and self.notes[2] else None


def _rec(raw: bytes):
    try:
        x = json.loads(raw)
    except ValueError:
        return None
    return x if isinstance(x, dict) else None


_PRE_KEYS = {"personas": list, "rounds": int, "knowledge": dict, "ev_count": dict, "ids": list, "groups": list,
             "ao_pre": dict, "ao_des": dict, "free": list}


def _stage_in_ok(sk: str, rec: dict) -> bool:
    """e:stage_in 줄의 꼴 — 칸마다 꼴이 맞고, 앞 단 결정문은 비지 않았고, 좌석 목록은 key 가 있는 객체만이다."""
    want = STAGE_IN.get(sk) or {}
    if any(not isinstance(rec.get(key), typ) for key, typ in want.items()):
        return False
    if any(not (isinstance(x, dict) and isinstance(x.get("key"), str) and x["key"]) for x in rec.get("seats") or []):
        return False
    return all(str(rec.get(key)).strip() for key in ("decision_a", "decision_b") if key in want)


def parse(raw: bytes, job_id: str, *, journal_v: int, stop_after_round: int = 0, stages: tuple = (STAGE,)):
    """진행 일지 바이트 → (Resume, "") 또는 (None, 못 쓰는 까닭). 반쯤 쓴 마지막 줄은 버린다. 깨진 줄 · 꼴이 다른 머리 · 멈춘 엔진 줄이 있으면 쓰지 않는다
    — 되살린 상태가 끊기기 전과 같다는 보장이 없으면 요약으로 잇는 것이 낫다.

    stages 는 그 잡의 단 열쇠(STAGES — 엔진 종류로 정해진다)다. 되살리는 단은 마지막 단 표지(e:stage_in · e:pre)의 단 하나다(S4) — 단은 차례로
    돌아 뒤 단의 표지가 적혔으면 앞 단은 끝났고, 그 산출은 그 단의 e:stage_in 에 있다. 마지막 표지가 e:stage_in 이면 그 단의 라운드 전 단계에서
    끊긴 것이라 되살릴 것이 없다(요약 길 — 체크포인트로 끝난 단을 건너뛴다). 이 잡의 단이 아닌 줄이 있으면 쓰지 않는다."""
    lines = raw.split(b"\n")
    lines.pop()
    head = _rec(lines[0]) if lines else None
    if not (head and head.get("k") == "j:head" and head.get("v") == journal_v and head.get("id") == job_id):
        return None, "진행 일지의 머리가 이 잡 · 이 판의 꼴이 아니다"
    if head.get("rv") != RESUME_SCHEMA:
        return None, f"재개 자료의 꼴이 이 판과 다르다(일지 {head.get('rv')} · 이 판 {RESUME_SCHEMA})"
    sk, entered, sin = None, False, {}
    pre, gather, raws, ended, post = None, {}, {}, {}, {}
    # 자리마다의 알림 — notes[r] 는 그 단의 e:round_in r 뒤에 원장에 실린 알림(공개 사건)과 옮겨 적힌 앞 시도의 알림(e:note). here 는 지금 든 자리
    notes: dict = {}
    here = None
    for ln in lines[1:]:
        rec = _rec(ln)
        if rec is None:
            return None, "진행 일지에 깨진 줄이 있다"
        k = rec.get("k")
        if k == "e:resume_off":
            return None, f"재개 자료를 끝까지 쓰지 못했다({str(rec.get('why') or '까닭 없음')[:200]})"
        if not (isinstance(k, str) and k.startswith("e:")):
            if here is not None and notice_key(k, rec.get("d")) is not None:
                notes[here].append([k, rec["d"]])
            continue
        rsk = rec.get("sk")
        if not (isinstance(rsk, str) and rsk in stages):
            return None, _WHY_ODD
        if k == "e:stage_in":             # 그 단에 든다(S4) — 앞 단은 끝났다. 그 단의 e:pre 가 오기 전이면 그 단의 라운드 전 단계다
            sk, entered, sin[rsk] = rsk, True, rec
            here = None
            continue
        if k == "e:pre":
            sk, entered = rsk, False
            pre, gather, raws, ended, post, notes, here = rec, {}, {}, {}, {}, {}, None
            continue
        if pre is None or rsk != sk or entered:
            continue
        if k in POST:                     # 결정문 뒤 줄(S3) — 라운드 번호가 없다
            post[k] = rec
            continue
        try:
            r = int(rec.get("r"))
        except (TypeError, ValueError):
            return None, "재개 자료에 라운드 번호가 없는 줄이 있다"
        if k == "e:round_in":             # 그 자리에 든다 — 이 뒤의 알림이 그 자리의 것이다(이 시도의 것 — 앞 시도의 것은 e:note 로 옮겨 적혔다)
            here = r
            notes.setdefault(r, [])
            continue
        if k == "e:note":
            if isinstance(rec.get("ev"), str) and notice_key(rec["ev"], rec.get("d")) is not None:
                notes.setdefault(r, []).append([rec["ev"], rec["d"]])
            continue
        if k == "e:gather":
            gather[r] = rec
        elif k == "e:raw":
            o = rec.get("o")
            if not (isinstance(o, dict) and isinstance(o.get("persona"), str)):
                return None, "재개 자료에 좌석 원 출력의 꼴이 다른 줄이 있다"
            raws.setdefault(r, {}).setdefault(o["persona"], rec)          # 같은 좌석이 두 번이면 먼저 적힌 것(한 일지에서는 없다)
        elif k == "e:round_end":
            ended[r] = rec
    if entered:
        return None, _why_entered(sk)
    if pre is None:
        return None, _WHY_PRE
    sin_rec = sin.get(sk)
    if sk in STAGE_IN and sin_rec is None:
        return None, _why_no_in(sk)
    if sk in STAGE_IN and not _stage_in_ok(sk, sin_rec):
        return None, "재개 자료(e:stage_in)의 꼴이 이 판과 다르다"
    if any(not isinstance(pre.get(key), typ) or isinstance(pre.get(key), bool) for key, typ in _PRE_KEYS.items()):
        return None, "재개 자료(e:pre)의 꼴이 이 판과 다르다"
    seats = _seat_list(p.get("key") for p in pre["personas"] if isinstance(p, dict))
    if len(seats) != len(pre["personas"]) or len(seats) < 2:
        return None, "재개 자료(e:pre)의 좌석 꼴이 이 판과 다르다"
    n = pre["rounds"]
    k = _restorable(seats, n, {r: e.get("lost") or [] for r, e in ended.items()},
                    {r: list(x) for r, x in raws.items()}, stop_after_round)
    # 옮겨 쓸 줄 — 그 단에 들 때의 줄(S4)이 e:pre 보다 먼저다(일지에 적힌 차례 — 다음 재기동의 읽기가 같은 단을 끊긴 단으로 본다)
    rounds, carry = {}, ([sin_rec] if sin_rec is not None else []) + [pre]
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
    # 끊긴 자리(라운드 cut — 전 라운드가 끝났으면 결정문 단계 n + 1)에서 끊기기 전에 원장에 실린 알림 — 이은 실행이 그 자리를 다시 하며 같은 알림을
    # 내면 다시 싣지 않는다(Seen). 공개 사건은 옮겨 적지 못하니(재기동 정리가 또 접는다) e:note 로 옮긴다 — 또 끊겨도 앞 시도들의 것을 다 안다.
    cut_notes = notes.get(cut) or []
    carry += [{"k": "e:note", "sk": sk, "r": cut, "ev": ev, "d": dd} for ev, dd in cut_notes]
    sav, it = post.get("e:report_saved"), post.get("e:report_intent") or {}
    info = {"sk": sk, "round": cut, "restored": k, "total": n, "spoken": spoken,
            "todo": [s for s in seats if s not in spoken] if k < n else [], "gathered": cut in gather if k < n else False,
            "eng_changed": bool(head.get("eng")) and head.get("eng") != ENGINE, "ctx": pre.get("ctx"),
            # 응답 안내에 싣는 결정문 뒤 단계 — report 는 saved(저장했다) · intent(저장 도중 끊겼다 — 이미 저장됐을 수 있다) · "" 다
            "post": {"decision": "e:decision" in post, "chair_failed": bool((post.get("e:decision") or {}).get("failed")),
                     "plain": "e:plain" in post, "wiki": "e:wiki_sent" in post,
                     "report": "saved" if sav else "intent" if it.get("save") else "",
                     "report_id": sav.get("rid") if sav else None, "append_to": it.get("append_to") or 0,
                     "title": it.get("title") or ""}}
    return Resume(job_id, {sk: StageData(pre, rounds, info, cutd, post)}, carry, info, list(pre["groups"]),
                  stage_in={sk: sin_rec} if sin_rec is not None else {}, notes=(sk, cut, cut_notes)), ""

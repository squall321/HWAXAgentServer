# 결정문 뒤를 한 번만 하기(HA-09 S3) — 의장 결정문 · 보고서 · 위키 뒤에서 끊고 같은 잡을 이으면 끝난 단계를 다시 하지 않는지(보고서를 저장한 뒤 · 결과 사건 전이면 RA 호출 0 · 같은 번호 · 위키 0), 저장 도중 끊겼으면 경고 한 번과 새 저장인지 본다
#
# 왜 생겼나. S1 · S2 는 결정문 뒤 단계에 표지가 없어, 끊긴 잡을 이으면 의장 · 요약 · 쉬운 설명을 다시 부르고 보고서를 다시 저장했다 — 보고서를
# 저장한 뒤 결과 사건 전에 끊기면 Report Archive 에 같은 보고서가 하나 더 생겼다(설계 A3 다섯째). 결정문 사건이 원장에 접혔으면 제자리로 잇지
# 못하고 요약 길(새 잡 · 새 심의)로 갔다. 이제 엔진이 결정문 뒤 줄(e:decision · e:plain · e:report_intent · e:report_saved · e:wiki_sent)을 일지에
# 적고, 이으면 그 줄이 있는 단계는 다시 하지 않는다(HWAXPortal docs/request-1027/design-ha09-ha20.md A5.2 · A5.3 넷째 · A6 S3).
#
# 하네스는 tests/test_ha09_s1_resume.py 그대로다 — 가짜는 바깥(LLM · 게이트웨이 도구 · 좌석 자유 조회)에만 있고 엔진 · 잡 원장 · 진행 일지 ·
# 재기동 정리 · MCP 도구는 진짜다. 끊는 자리는 끊지 않은 실행의 일지가 한 줄 적힐 때마다 뜬 디스크 꼴이다(KILL 과 같다). 여기서는 가짜 Report
# Archive 가 저장마다 새 번호를 준다(77, 78, …) — 다시 저장하면 번호가 달라져 보인다. 가짜 위키 도구는 받은 봉투를 적고 늘 같은 원천으로 답한다.
#
#   실행:  .venv/bin/python -m pytest tests/test_ha09_s3_post_idempotent.py -q
import asyncio
import copy
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import delib_jobs  # noqa: E402
import delib_resume  # noqa: E402
import deliberation as d  # noqa: E402
from test_delib_job_queue import ME, _on_disk  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import (AFF, GROUPS, OPTS, PRE_KINDS, PRE_TOOLS, Q, ROUNDS, STUB, _TOOL_NAMES, _continue, _cut,  # noqa: E402
                                 _eng, _FTool, _kind, _oracle, _restart, _result, _turn_rows, world)  # noqa: F401 — world 는 픽스처
from test_ha09_s2_seat_resume import _spy  # noqa: E402

OPTS3 = {**OPTS, "wiki": 1}             # 위키에 남기는 심의 — 보고서 다음 단계까지 본다
_CARD = "끊긴 심의 되살림"
_WARN = "끝났는지 알 수 없어"            # 저장 도중 끊긴 보고서의 경고(report_maybe_saved)
_POST = delib_resume.POST


@pytest.fixture
def w3(world, monkeypatch):
    """S1 가짜 위에 — Report Archive 는 저장마다 새 번호(77, 78, …)를 주고, 위키 도구는 받은 봉투를 적고 늘 같은 원천으로 답한다(같은 열쇠 ·
    같은 본문이면 MX 백서가 앞의 원천으로 받는다 — 여기서는 답만 같게 둔다). reset 은 그때까지 받은 봉투를 last_wiki 에 남긴다. 의장 결정문에는
    근거에 없는 수치를 하나 붙인다 — 보고서를 저장하기 전에 수치 대조가 경고 줄을 붙여, 저장할 결정문(e:report_intent)이 e:plain 의 것과 다르다."""
    ids = itertools.count(77)
    real_answer, real_reset = world.answer, world.reset

    def answer(name, args):
        if name == "create_report_draft":
            world.reports.append(copy.deepcopy(args))
            return json.dumps({"report_id": next(ids)})
        if name == "update_report_draft":
            world.reports.append(copy.deepcopy(args))
            return json.dumps({"report_id": args["report_id"]})
        if name == "get_report":
            return json.dumps({"report": {"id": args["report_id"], "pages": [{"page": 1}]}})
        if name == "wiki_submit_source":
            world.wiki.append(copy.deepcopy(args["envelope"]))
            return json.dumps({"id": "src-1", "short": "W-1", "ingest_state": "queued"})
        return real_answer(name, args)

    def reset():
        real_reset()
        world.last_wiki, world.wiki = world.wiki, []

    async def tools(*_a, **_k):
        return {n: _FTool(world, n) for n in _TOOL_NAMES + ("wiki_submit_source", "get_report", "update_report_draft")}

    async def llm_text(llm, system, human, *img):
        out = await real_llm(llm, system, human, *img)
        return out + "\n- 보스 두께를 13.75 mm 로 바꾼다" if _kind(system) == "chair" else out

    real_llm = world.llm_text
    world.wiki = []
    monkeypatch.setattr(world, "answer", answer)
    monkeypatch.setattr(world, "reset", reset)
    monkeypatch.setattr(d, "_tools_by_name", tools)
    monkeypatch.setattr(d, "_llm_text", llm_text)
    return world


def _post_kinds(snap: dict) -> set:
    """그 디스크의 일지에 적힌 결정문 뒤 줄 — 끊긴 자리에서 끝난 단계."""
    key = next(k for k in snap if k.startswith("journal/"))
    return {json.loads(ln)["k"] for ln in snap[key].splitlines() if ln} & set(_POST)


def _resume_at(w, monkeypatch, tmp_path, name: str, snap: dict) -> tuple:
    """끊긴 디스크로 다시 뜨고 주인이 한 번 이어 끝까지 — (이어하기 응답, 결과, 이은 실행이 위키에 보낸 봉투)."""
    jid = _restart(monkeypatch, tmp_path, name, snap)
    out = _continue(jid)
    assert out["job_id"] == jid and out["resumed"]["mode"] == "in_place", out["resumed"]
    res = _result(w, {"id": jid})
    w.reset()
    return out, res, w.last_wiki


def _chain(res: dict) -> list:
    return [(k, h) for k, _r, _s, h, _x in res["llm"] if k in ("chair", "summary", "plain")]


def _same_outside(o: dict, res: dict) -> None:
    """결정문 뒤에서 이었으면 좌석 · 라운드 전 단계는 아무것도 다시 부르지 않고, 전사 · 알림 카드는 끊지 않은 실행과 같다."""
    assert res["seat"] == {} and res["lookups"] == [], "결정문 뒤에서 이었는데 좌석을 다시 불렀다"
    assert [x for x in res["llm"] if x[0] in PRE_KINDS] == [] and [t for t, _r in res["tools"] if t in PRE_TOOLS] == []
    a, b = res["led"], o["led"]
    assert _turn_rows(a) == _turn_rows(b), "전사가 끊지 않은 실행과 다르다"
    assert [c for c in a["evidence_omitted"] if c["source"] != _CARD] == b["evidence_omitted"], "알림 카드가 두 번 실렸거나 빠졌다"
    assert a["status"] == b["status"] and len(a["attempts"]) == 1 and not a.get("resumed_by")


# ── 보고서를 저장한 뒤 · 결과 사건 전에 끊기면 — RA 호출 0 · 같은 번호 · 위키는 보낸 뒤면 0 ───────────────────────────────
@pytest.mark.parametrize("at", ["e:report_saved", "위키 남기는 중", "e:wiki_sent", "결정문 사건"])
def test_보고서를_저장한_뒤_결과_사건_전에_끊고_이으면_RA_호출_0_같은_번호이고_위키는_보낸_뒤면_0이다(w3, monkeypatch, tmp_path, at):
    """설계 A6 S3 — 보고서 저장 뒤 · 결과 사건(outcome) 전에 끊고 이으면 RA 호출 0 · 같은 보고서 번호 · 의장 · 요약 · 쉬운 설명 0 이고, 위키는
    보낸 뒤(e:wiki_sent)면 0 이다. 위키를 보내던 중에 끊겼으면 한 번 더 보내되 같은 원천 열쇠 · 같은 본문이다(제자리라 잡 id 가 같다 — MX 백서가
    앞의 원천으로 받는다). 결정문 사건이 원장에 접혔어도(그 뒤 · 결과 전) 요약 길로 가지 않고 같은 잡을 끝까지 맺는다."""
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    o_wiki = w3.last_wiki
    assert (len(o["reports"]), o["led"]["report_id"], len(o_wiki)) == (1, 77, 1), "시험 전제 — 끊지 않은 실행은 한 번 저장 · 한 번 보낸다"
    pred = (_eng(at) if at.startswith("e:") else
            (lambda rec: rec.get("k") == "status" and "MX 백서 위키에 결정문 남기는 중" in str((rec.get("d") or {}).get("step")))
            if at == "위키 남기는 중" else
            (lambda rec: rec.get("k") == "delib" and (rec.get("d") or {}).get("kind") == "decision"))
    snap = _cut(snaps, pred)
    kinds = _post_kinds(snap)
    assert {"e:decision", "e:plain", "e:report_intent", "e:report_saved"} <= kinds, "시험 전제 — 보고서 저장 뒤다"
    sent = "e:wiki_sent" in kinds
    out, res, wiki = _resume_at(w3, monkeypatch, tmp_path, at, snap)
    assert res["reports"] == [], "이미 저장한 보고서를 다시 저장했다"
    assert _chain(res) == [], "끝난 의장 · 요약 · 쉬운 설명을 다시 불렀다"
    a, b = res["led"], o["led"]
    assert (a["report_id"], a["decision"], a["result_text"], a["status"]) == (77, b["decision"], b["result_text"], "done"), \
        "같은 번호 · 같은 결정문으로 끝나지 않았다"
    assert a["warnings"] == b["warnings"], "저장을 마친 잡에 경고를 냈다"
    _same_outside(o, res)
    if sent:
        assert wiki == [], "이미 보낸 위키에 다시 보냈다"
    else:
        assert len(wiki) == 1 and wiki[0]["origin"]["ref"] == o_wiki[0]["origin"]["ref"] == f"delib:{a['id']}:default" \
            and wiki[0]["body"] == o_wiki[0]["body"], "보내던 위키를 다른 열쇠 · 다른 본문으로 보냈다"
    assert "보고서는 끊기기 전에 저장돼(#77) 다시 저장하지 않는다" in out["note"], out["note"]
    assert ("위키에도 다시 보내지 않는다" in out["note"]) is sent, out["note"]
    card = next(c["text"] for c in a["evidence_omitted"] if c["source"] == _CARD)
    assert card.startswith("끊긴 심의를 같은 잡에서 잇는다(시도 기록은 attempts). 의장 결정문 · 요약 · 쉬운 설명은 끊기기 전의 것을 쓴다") \
        and "저장돼(#77) 다시 저장하지 않는다" in card, card


# ── 저장 도중 끊겼으면 — 경고 한 번과 새 저장 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("append", [False, True])
def test_보고서를_저장하던_중에_끊겼으면_경고를_한_번_내고_새로_저장한다(w3, monkeypatch, tmp_path, append):
    """e:report_intent 만 있으면 RA 가 받았는지 알 길이 없다(멱등 열쇠가 없다) — 이으면 '이미 저장됐을 수 있다' 경고를 한 번 내고 같은 블록으로
    새로 저장한다(이어붙이기면 같은 보고서에 다시 붙인다). 의장 · 요약 · 쉬운 설명은 다시 부르지 않는다."""
    opts = {**OPTS3, **({"append_to_report_id": 41} if append else {})}
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts=opts)
    assert len(o["reports"]) == 1 and o["led"]["report_id"] == (41 if append else 77), "시험 전제"
    assert "심의에 제시된 근거에서 확인되지 않았습니다" in o["led"]["decision"], "시험 전제 — 저장한 결정문에 수치 대조 경고가 붙었다"
    snap = _cut(snaps, _eng("e:report_intent"))
    assert _post_kinds(snap) == {"e:decision", "e:plain", "e:report_intent"}
    out, res, wiki = _resume_at(w3, monkeypatch, tmp_path, "intent", snap)
    a, b = res["led"], o["led"]
    assert res["reports"] == o["reports"], "새로 저장한 블록이 끊기기 전에 저장하려던 것과 다르다"
    assert _chain(res) == [], "끝난 의장 · 요약 · 쉬운 설명을 다시 불렀다"
    warns = [x for x in a["warnings"] if _WARN in x]
    assert len(warns) == 1 and [x for x in a["warnings"] if _WARN not in x] == b["warnings"], a["warnings"]
    assert ("보고서 #41 에 이 회차의 페이지를 붙이던 중이었다" if append else f"보고서 「{d._ra_report_title(Q)}」 를 저장하던 중이었다") \
        in warns[0], warns[0]
    rid = a["report_id"]
    assert rid == 41 if append else rid == 78, rid
    assert a["decision"] == b["decision"].replace("#77 로 저장됨", f"#{rid} 로 저장됨"), "결정문이 끊지 않은 실행과 다르다"
    _same_outside(o, res)
    assert len(wiki) == 1 and wiki[0]["origin"]["ref"] == f"delib:{a['id']}:default", "위키를 보내지 않았거나 다른 열쇠로 보냈다"
    assert "중에 끊겨 이미 저장됐을 수 있다 — 새로 저장하니" in out["note"], out["note"]
    card = next(c["text"] for c in a["evidence_omitted"] if c["source"] == _CARD)
    assert "저장하던 중에 끊겨 이미 저장됐을 수 있다 — 새로 저장한다" in card, card


@pytest.mark.parametrize("line", ["e:report_intent", "e:report_saved"])
def test_저장하지_않는_심의는_보고서_단계에서_끊겨도_경고하지_않고_저장하지_않는다(w3, monkeypatch, tmp_path, line):
    """save_report=0 — RA 를 부르지 않으니 저장 도중이라는 것이 없다. e:report_intent(save 거짓) 뒤에 끊겨도 경고 없이 같은 결과로 맺고, 보고서
    단계가 끝난 뒤(e:report_saved — 번호 없음)면 저장했다고 말하지 않고 그 단계를 다시 하지 않는다고만 말한다."""
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts={**OPTS3, "save_report": 0})
    assert o["reports"] == [] and o["led"]["report_id"] is None
    out, res, _wiki = _resume_at(w3, monkeypatch, tmp_path, "nosave", _cut(snaps, _eng(line)))
    a, b = res["led"], o["led"]
    assert res["reports"] == [] and not [x for x in a["warnings"] if _WARN in x] and a["decision"] == b["decision"], a["warnings"]
    card = next(c["text"] for c in a["evidence_omitted"] if c["source"] == _CARD)
    assert "저장돼" not in out["note"] and "이미 저장됐을 수 있다" not in out["note"] and "저장돼" not in card, (out["note"], card)
    if line == "e:report_saved":
        assert "보고서 단계는 끊기기 전에 끝나 다시 하지 않는다" in out["note"] and "보고서 단계는 끊기기 전에 끝나 다시 하지 않는다." in card
    else:
        assert "보고서 단계부터 이어 간다" in out["note"], out["note"]


# ── 일지의 어느 줄에서 끊겨도 — 끝난 단계는 다시 하지 않고 결과는 끊지 않은 실행과 같다 ───────────────────────────────
def test_결정문_뒤_일지의_어느_줄에서_끊겨도_끝난_단계는_다시_하지_않고_결과는_끊지_않은_실행과_같다(w3, monkeypatch, tmp_path):
    """KILL 은 아무 줄 뒤에서나 난다. 마지막 라운드 끝 줄부터 일지의 끝까지 줄마다 그때의 디스크로 다시 뜨고 이어 본다. 끊긴 자리에서 끝난 단계
    (일지의 결정문 뒤 줄)는 다시 하지 않고 — 의장 · 요약 · 쉬운 설명 · 저장 · 위키 — 아직인 것은 끊지 않은 실행과 같은 프롬프트 · 같은 블록 ·
    같은 열쇠로 한 번 한다. 저장 도중(e:report_intent 만)이면 경고 한 번과 새 번호. 결정문 · 결과는 끊지 않은 실행과 같다(새로 저장했으면 번호만
    다르다). 어느 자리든 같은 잡에서 잇는다(결정문 사건이 원장에 있어도)."""
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    o_wiki, b = w3.last_wiki, o["led"]
    oc = _chain(o)
    assert [k for k, _h in oc] == ["chair", "summary", "plain"], "시험 전제 — 끊지 않은 실행의 의장 · 요약 · 쉬운 설명"
    i0 = max(i for i, (rec, _d) in enumerate(snaps) if rec.get("k") == "e:round_end")
    seen = set()
    for i in range(i0, len(snaps)):
        kinds = _post_kinds(snaps[i][1])
        seen.add(frozenset(kinds))
        out, res, wiki = _resume_at(w3, monkeypatch, tmp_path, f"line{i}", snaps[i][1])
        a, where = res["led"], f"{i}번째 줄({snaps[i][0].get('k')}) 뒤 · 끝난 단계 {sorted(kinds)}"
        saved = "e:report_saved" in kinds
        want = ([] if "e:decision" in kinds else oc[:1]) + ([] if "e:plain" in kinds else oc[1:])
        assert _chain(res) == want, f"{where} — 의장 · 요약 · 쉬운 설명을 다시 불렀거나 다른 프롬프트로 불렀다"
        assert res["reports"] == ([] if saved else o["reports"]), f"{where} — 저장 횟수 · 블록이 틀렸다"
        assert len([x for x in a["warnings"] if _WARN in x]) == ("e:report_intent" in kinds and not saved), f"{where} — 경고"
        rid = a["report_id"]
        assert (rid == 77) is saved and rid, f"{where} — 보고서 번호 {rid}"
        assert (a["decision"], a["result_text"]) == (b["decision"].replace("#77 로", f"#{rid} 로"),
                                                    b["result_text"].replace("#77 로", f"#{rid} 로")), f"{where} — 결정문 · 결과가 다르다"
        assert len(wiki) == (0 if "e:wiki_sent" in kinds else 1), f"{where} — 위키를 {len(wiki)}번 보냈다"
        assert all(x["origin"]["ref"] == o_wiki[0]["origin"]["ref"] for x in wiki), f"{where} — 위키 원천 열쇠가 다르다"
        _same_outside(o, res)
        assert out["resumed"]["round"] == ROUNDS + 1 and out["resumed"]["todo"] == []
    assert seen == {frozenset(_POST[:n]) for n in range(len(_POST) + 1)}, f"시험 전제 — 결정문 뒤의 단계마다 끊어 봤다: {seen}"


# ── 되살린 단계의 알림 카드는 다시 내지 않는다 ────────────────────────────────────────────────
@pytest.mark.parametrize("line", ["e:decision", "e:plain", "e:report_intent", "e:report_saved"])
def test_되살린_단계의_알림_카드는_원장에_한_번만_남는다(w3, monkeypatch, tmp_path, line):
    """결정문 뒤 단계의 카드(의장 전사 상한 초과 · 후처리 입력 상한 초과 · 보고서 블록 상한 초과)는 그 단계의 줄보다 먼저 원장에 실린다 — 그
    단계를 되살리면 다시 내지 않는다. 아직인 단계의 카드는 이번에 처음 낸다(끊기기 전에 그 자리에 닿지 않았다)."""
    monkeypatch.setattr(d, "_decision_ctx", lambda *_a, **_k: 120)
    monkeypatch.setattr(d, "_text_turn_room", lambda *_a, **_k: 40)
    monkeypatch.setattr(d, "_RA_MINUTES_MAX", 6)
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    srcs = [c["source"] for c in o["led"]["evidence_omitted"]]
    assert all(srcs.count(s) == 1 for s in ("의장 전사 상한 초과", "후처리 입력 상한 초과", "보고서 블록 상한 초과")), srcs
    _out, res, _wiki = _resume_at(w3, monkeypatch, tmp_path, line, _cut(snaps, _eng(line)))
    _same_outside(o, res)


# ── 의장이 결정문을 못 낸 뒤 ─────────────────────────────────────────────────────────────
def test_의장이_결정문을_못_낸_뒤_끊겼으면_의장을_다시_부르지_않고_같은_오류로_맺는다(w3, monkeypatch, tmp_path):
    """e:decision 은 의장 실패 표지도 싣는다 — 끊지 않았다면 회의록을 저장하고 chair_failed 로 끝났다. 이으면 의장을 다시 부르지 않고 같은 글 ·
    같은 오류(사유 · 설정 이름)로 맺는다. 결정문 없이 위키에는 보내지 않는다(끊지 않은 실행과 같다)."""
    real = w3.llm_text

    async def chair_fails(llm, system, human, *img):
        out = await real(llm, system, human, *img)
        if _kind(system) == "chair":
            raise RuntimeError("가짜 의장 실패")
        return out
    monkeypatch.setattr(d, "_llm_text", chair_fails)
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    b = o["led"]
    assert b["status"] == "error" and "의장이 결정문을 내지 못했다" in b["error"] and len(o["reports"]) == 1 and w3.last_wiki == [], \
        "시험 전제 — 의장이 두 번 실패하고 회의록을 저장했다"
    out, res, wiki = _resume_at(w3, monkeypatch, tmp_path, "chairfail", _cut(snaps, _eng("e:decision")))
    a = res["led"]
    assert _chain(res) == [] and wiki == [], "실패한 의장을 다시 불렀거나 결정문 없이 위키에 보냈다"
    assert res["reports"] == o["reports"] and a["report_id"] == 78, "회의록을 끊지 않은 실행과 같은 블록으로 한 번 저장하지 않았다"
    assert (a["status"], a["error"], a["decision"]) == ("error", b["error"], b["decision"].replace("#77 로", "#78 로"))
    assert "의장은 끊기기 전에 결정문을 내지 못했다 — 다시 부르지 않는다" in out["note"], out["note"]
    _same_outside(o, res)


# ── 이은 실행이 또 끊겨도 ──────────────────────────────────────────────────────────────
def test_이은_실행이_또_끊겨도_앞_시도의_결정문_뒤_단계가_남아_두_번_저장하지_않는다(w3, monkeypatch, tmp_path):
    """결정문 뒤 줄은 새 일지에 옮겨 적힌다. 저장 도중 끊긴 잡을 이어 새로 저장한 뒤 다시 끊기면, 셋째 시도는 둘째 시도의 저장(e:report_saved)을
    보고 다시 저장하지 않는다 — 경고도 둘째 시도의 한 번뿐이다."""
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    jid = _restart(monkeypatch, tmp_path, "a", _cut(snaps, _eng("e:report_intent")))
    again: list = []
    with _spy(monkeypatch, again):
        first = _continue(jid)
    assert first["resumed"]["mode"] == "in_place"
    one = _result(w3, {"id": jid})
    w3.reset()
    assert len(one["reports"]) == 1 and one["led"]["report_id"] == 78
    lines = [rec for rec, _d in again]
    assert [x["k"] for x in lines if x["k"] in _POST] == list(_POST), "결정문 뒤 줄을 옮겨 적지 않았거나 두 번 적었다"
    _out, res, wiki = _resume_at(w3, monkeypatch, tmp_path, "b", _cut(again, _eng("e:report_saved")))
    a = res["led"]
    assert res["reports"] == [] and _chain(res) == [] and a["report_id"] == 78, "둘째 시도의 저장을 셋째 시도가 다시 했다"
    assert len([x for x in a["warnings"] if _WARN in x]) == 1 and len(a["attempts"]) == 2 and len(wiki) == 1
    assert a["decision"] == o["led"]["decision"].replace("#77 로", "#78 로")


# ── 체크포인트 결정문 뒤 ─────────────────────────────────────────────────────────────
def test_체크포인트_결정문_사건_뒤에_끊긴_잡도_제자리로_잇고_아무도_다시_부르지_않는다(w3, monkeypatch, tmp_path):
    """결정문이 원장에 있어도 끊긴 잡은 같은 잡으로 맺는다(종전엔 '결정문이 이미 나왔다' 로 요약 길 — 새 잡이 새 심의를 돌았다). 체크포인트
    심의는 1라운드를 끊긴 라운드로 잇고(S2) 좌석이 모두 말했으니 아무도 부르지 않고 같은 체크포인트로 멈춘다."""
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts={**OPTS3, "stop_after_round": 1})
    assert o["led"]["decision"].startswith("[체크포인트")
    snap = _cut(snaps, lambda rec: rec.get("k") == "delib" and (rec.get("d") or {}).get("kind") == "decision")
    jid = _restart(monkeypatch, tmp_path, "ck", snap)
    assert _on_disk({"id": jid})["decision"] == o["led"]["decision"], "시험 전제 — 결정문 사건이 원장에 접혔다"
    out = _continue(jid)
    assert out["job_id"] == jid and out["resumed"]["mode"] == "in_place", out["resumed"]
    res = _result(w3, {"id": jid})
    assert res["llm"] == [] and res["reports"] == [] and res["led"]["decision"] == o["led"]["decision"]
    assert res["led"]["status"] == "done" and _turn_rows(res["led"]) == _turn_rows(o["led"])


# ── 결정문 뒤 줄을 쓰지 않던 판의 일지 ─────────────────────────────────────────────────────
def test_결정문_뒤_줄을_쓰지_않던_판의_일지는_저장_여부를_알_수_없어_요약으로_잇는다(w3, monkeypatch, tmp_path):
    """결정문 뒤 줄이 없으면 '그 단계를 아직 하지 않았다' 로 읽는다. 그 줄을 쓰지 않던 판(재개 자료 꼴 1)의 일지는 저장한 뒤에 끊겨도 줄이 없으니
    그렇게 읽을 수 없다 — 꼴 번호가 달라 되살리지 않고 까닭과 함께 요약 길로 간다."""
    snaps: list = []
    _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    snap = _cut(snaps, _eng("e:report_saved"))
    key = next(k for k in snap if k.startswith("journal/"))
    lines = [ln for ln in snap[key].split(b"\n") if not (ln and json.loads(ln).get("k") in _POST)]
    lines[0] = json.dumps({**json.loads(lines[0]), "rv": 1}).encode()
    assert delib_resume.RESUME_SCHEMA == 2
    jid = _restart(monkeypatch, tmp_path, "rv1", {**snap, key: b"\n".join(lines)})
    out = _continue(jid)
    r = out["resumed"]
    assert r["mode"] == "summary" and "꼴이 이 판과 다르다(일지 1 · 이 판 2)" in r["why"], r


# ── 재개 상태가 없으면 지금 길 그대로 ─────────────────────────────────────────────────
def test_끊지_않은_잡은_결정문_뒤_줄을_한_번씩_적고_사건과_프롬프트는_웹_길과_바이트까지_같다(w3, monkeypatch):
    """결정문 뒤 갈고리는 재개 상태가 없으면 아무것도 바꾸지 않는다 — 위키까지 켠 잡(일지에 결정문 뒤 줄을 쓴다)과 같은 잡 id · 소속의 웹 길이
    같은 사건 · 같은 프롬프트를 낸다. 잡의 일지에는 결정문 뒤 줄이 적는 차례대로 한 번씩 적혔다."""
    seen: list = []
    parse = delib_jobs._parse_sse

    def spy(chunk):
        seen.append(chunk)
        return parse(chunk)
    monkeypatch.setattr(delib_jobs, "_parse_sse", spy)
    snaps: list = []
    o = _oracle(w3, monkeypatch, snaps, opts=OPTS3)
    jid = o["job"]["id"]
    assert [rec["k"] for rec, _d in snaps if rec.get("k") in _POST] == list(_POST)
    job_events, job_llm = list(seen), o["llm"]
    after = w3.answer                       # 웹 길도 같은 보고서 번호를 받게 — 가짜 RA 는 저장마다 새 번호를 준다

    def same_rid(name, args):
        if name == "create_report_draft":
            w3.reports.append(copy.deepcopy(args))
            return json.dumps({"report_id": o["led"]["report_id"]})
        return after(name, args)
    monkeypatch.setattr(w3, "answer", same_rid)

    async def web():
        got = []
        async for chunk in d.run_deliberation(STUB, Q, list(GROUPS), copy.deepcopy(OPTS3), ME, job_id=jid, affiliation=AFF):
            got.append(chunk)
        return got
    web_events = asyncio.run(web())
    assert web_events == job_events, "결정문 뒤 줄을 쓰는 잡과 웹 심의의 사건이 다르다 — 갈고리가 지금 길을 바꿨다"
    assert w3.llm == job_llm and len(w3.reports) == 1 and len(w3.wiki) == 1

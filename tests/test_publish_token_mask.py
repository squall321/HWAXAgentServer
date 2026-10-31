# RA 게시 확인 토큰(confirm_token)을 턴 밖으로 나가는 곳에서만 가리는지 — 확인 카드 글 · 카드 사건, 심의가 받는 챗 근거 · 대화 맥락(좌석 · 결정문 · 보고서 · 위키 근거 발췌), '/보고서' 가 RA 에 남기는 대화. 맡아 둔 호출은 진짜 값 그대로 승인 때 나간다
#
# 왜 생겼나. RA 게시 미리보기(preview_publish)는 600초 동안 그 보고서를 그 게시판에 올릴 수 있는 확인 토큰을 준다. 챗의 다음 턴 게시는 그 값을
# 저장된 대화 이력에서 읽으므로 대화 저장소는 그대로 둔다(HWAXPortal 절차 W-106). 그런데 같은 값이 턴보다 오래 남고 진짜 값이 필요 없는 곳으로
# 흘렀다 — HA-29 확인 카드(publish_report 는 he-doc-reportarchive 의 확인 목록이다)가 인자 값을 80자까지 답 본문에 찍어 54자 토큰이 통째로 대화에
# 저장됐고, 심의로 넘긴 챗 근거 · '/보고서' 의 회의록에도 그대로 갔다. 규칙은 포털 절차 실행기와 같다(JSON 칸을 가리고, 그렇게 찾은 20자 이상 값은
# 같은 글의 다른 자리도 — 사용자 결정 10-31 '내보내는 곳만 가린다').
#
#   실행:  .venv/bin/python -m pytest tests/test_publish_token_mask.py -q
import asyncio
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402

import app as a  # noqa: E402
import deliberation as d  # noqa: E402
import publish_token as pt  # noqa: E402
from test_confirm_gate import _GateAgent, _gw, _result, _turn, maps  # noqa: E402, F401 — maps 는 픽스처
from test_delib_silent_drops import _pin_context  # noqa: E402, F401 — 모델 창 고정(자동 픽스처)
from test_ha09_s1_resume import _oracle, world  # noqa: E402, F401 — world 는 픽스처
from test_ha09_s3_post_idempotent import OPTS3, w3  # noqa: E402, F401 — w3 는 픽스처

TOKEN = "1793460000.Zq3vY8sN1kP0aB7cD2eF4gH6iJ8kL0mN2oP4qR6sT8u"     # RA 확인 토큰 꼴 — <만료초>.<서명 43자>, 54자
assert len(TOKEN) == 54
PREVIEW = json.dumps({"report_id": 41, "targets": [{"slug": "eng-board", "audience": 120, "already_mounted": False}],
                      "confirm_token": TOKEN, "expires_in": 600}, ensure_ascii=False)
PUBLISH_ARGS = {"report_id": 41, "target_slugs": ["eng-board"], "confirm_token": TOKEN}


# ── 도우미 — 포털 절차 실행기와 같은 규칙 ──────────────────────────────────────────────────────────────────
def test_칸을_가리고_찾은_긴_값은_같은_글의_다른_자리도_가린다():
    echo = f"{PREVIEW}\n검증 오류: {{'report_id': 41, 'confirm_token': '{TOKEN}'}}"
    out = pt.mask(echo)
    assert TOKEN not in out and out.count(pt.MASK) == 2 and '"confirm_token": "[가림:confirm_token]"' in out, out
    assert '"report_id": 41' in out and '"audience": 120' in out, "나머지(대상 · 볼 사람 수)는 그대로다"
    nested = json.dumps({"text": PREVIEW}, ensure_ascii=False)          # JSON 글 안에 한 겹 더 — 따옴표 앞에 역빗금
    assert TOKEN not in pt.mask(nested) and pt.MASK in pt.mask(nested)
    assert TOKEN not in pt.mask(f"토큰은 {TOKEN} 이다", tokens={TOKEN}), "같은 묶음의 다른 글에서 찾은 값도 가린다"
    assert pt.mask(f"토큰은 {TOKEN} 이다") == f"토큰은 {TOKEN} 이다", "칸 없이 맨글로만 나온 값은 찾을 길이 없다(포털과 같다)"
    short = '{"confirm_token": "yes"} yes 라고 답했다'
    assert pt.mask(short) == '{"confirm_token": "[가림:confirm_token]"} yes 라고 답했다', "짧은 값은 칸만 가린다"
    assert pt.mask(pt.mask(PREVIEW)) == pt.mask(PREVIEW) and pt.mask("") == "" and pt.mask(None) is None
    assert pt.found(PREVIEW, pt.mask(PREVIEW)) == {TOKEN}


# ── HA-29 확인 카드 — 카드 글 · 카드 사건은 가리고, 맡아 둔 호출은 진짜 값으로 승인 때 나간다 ─────────────────────────
@pytest.fixture
def ra_gate(monkeypatch, tmp_path, maps):
    monkeypatch.setattr(a, "CHAT_CONFIRM_GATE", True)
    p = tmp_path / "he-team.json"
    p.write_text(json.dumps({"personas": [{"key": "he-doc-reportarchive", "apps": ["reportarchive"],
                                           "writes": ["create_report_draft"], "confirm": ["publish_report"]}]}), encoding="utf-8")
    monkeypatch.setattr(a, "CONFIRM_LISTS_PATH", p)
    monkeypatch.setattr(a, "_CONFIRM_STAT", a.Counter())
    return p


def test_확인_카드_글과_카드_사건에는_토큰이_없고_승인하면_맡아_둔_진짜_값이_나간다(ra_gate, monkeypatch):
    name = "reportarchive_publish_report"
    reached: list = []
    props = {"report_id": {"type": "integer"}, "target_slugs": {"type": "array", "items": {"type": "string"}},
             "confirm_token": {"type": "string"}}
    tools = {name: _gw(name, reached, props=props)}
    ag = _GateAgent(tools, steps=[[(name, PUBLISH_ARGS)]], pre="보고서 #41 을 eng-board 에 올리겠습니다. ")
    evs, _seen = _turn(monkeypatch, ag, tools, "보고서 41 을 eng-board 에 게시해 줘")
    cards = [x for e, x in evs if e == "confirm"]
    assert len(cards) == 1 and cards[0]["calls"][0]["kind"] == "confirm", cards
    card, res = cards[0], _result(evs)
    assert TOKEN not in res and "confirm_token=[가림:confirm_token]" in res, "답 본문(대화에 저장된다)의 카드 글에 토큰이 찍혔다"
    assert TOKEN not in json.dumps(card, ensure_ascii=False) and pt.MASK in card["calls"][0]["args"], "카드 사건에 토큰이 실렸다"
    assert not [x for e, x in evs if e == "token" and TOKEN in x.get("delta", "")], "흘린 글에 토큰이 실렸다"
    held = a._CONFIRM_PENDING[card["id"]]["calls"][0]
    assert held["args"]["confirm_token"] == TOKEN and TOKEN in held["canon"], "맡아 둔 호출은 진짜 값이어야 한다(승인하면 그것을 보낸다)"
    hist = [{"role": "user", "content": "보고서 41 을 eng-board 에 게시해 줘"}, {"role": "assistant", "content": res}]
    ag2 = _GateAgent(tools, steps=[], say="게시했습니다.")
    _turn(monkeypatch, ag2, tools, card["approve_text"], hist)
    assert reached == [(name, PUBLISH_ARGS)], f"승인한 호출이 진짜 토큰으로 한 번 나가야 한다: {reached}"


# ── 심의 — 챗이 넘긴 근거 · 대화 맥락 ─────────────────────────────────────────────────────────────────
def _ev_items() -> list:
    return [{"source": "챗 정리", "tool": "reportarchive_preview_publish", "args": json.dumps({"report_id": 41}), "result": PREVIEW},
            {"source": "챗 정리", "tool": "reportarchive_publish_report", "args": json.dumps(PUBLISH_ARGS), "result": "게시 실패 — 토큰 만료"},
            {"source": "챗 정리", "tool": "note", "result": f"모델이 되비친 값 {TOKEN} 와 보스 뿌리 응력 120 MPa"}]


def test_심의가_받는_챗_근거는_토큰을_가린_꼴이다():
    o = d._resolve_opts({"evidence": _ev_items()})
    flat = json.dumps(o.evidence, ensure_ascii=False)
    assert TOKEN not in flat and flat.count(pt.MASK) >= 3, flat
    assert '"report_id": 41' in o.evidence[0]["result"] and "120 MPa" in o.evidence[2]["result"], "나머지 근거는 그대로다"


def test_심의가_받는_대화_맥락은_토큰을_가린_꼴이다():
    hist = [{"role": "user", "content": "보고서 41 게시 미리보기 해 줘"},
            {"role": "assistant", "content": f"미리보기 결과입니다.\n```json\n{PREVIEW}\n```"},
            {"role": "user", "content": f"그 토큰 {TOKEN} 로 게시하기 전에 심의해 줘"}]
    note = d._chat_context_note(hist)
    assert TOKEN not in note and pt.MASK in note and "게시하기 전에 심의해 줘" in note, note


def test_심의의_좌석_보고서_위키_근거_발췌에는_토큰이_없다(w3, monkeypatch):
    """챗이 넘긴 근거에 게시 미리보기 결과가 있는 심의를 잡으로 끝까지 — 좌석 프롬프트 · Report Archive 보고서 블록 · MX 백서 위키 봉투(근거 발췌)
    어디에도 토큰이 없다."""
    prompts: list = []
    real = d._llm_text

    async def spy(llm, system, human, *img):
        prompts.append(human)
        return await real(llm, system, human, *img)
    monkeypatch.setattr(d, "_llm_text", spy)
    o = _oracle(w3, monkeypatch, opts={**OPTS3, "evidence": _ev_items()})
    assert o["led"]["status"] == "done" and o["reports"] and w3.last_wiki, "시험 전제 — 끝까지 돌아 보고서 · 위키에 남겼다"
    assert any("eng-board" in p for p in prompts), "시험 전제 — 근거가 좌석에 실렸다"
    assert not [p for p in prompts if TOKEN in p], "좌석 · 의장 프롬프트에 토큰이 실렸다"
    assert TOKEN not in json.dumps(o["reports"], ensure_ascii=False), "Report Archive 보고서 블록에 토큰이 실렸다"
    wiki = json.dumps(w3.last_wiki, ensure_ascii=False)
    assert TOKEN not in wiki and pt.MASK in wiki, "위키 봉투의 근거 발췌에 토큰이 실렸다"
    assert TOKEN not in json.dumps(o["led"], ensure_ascii=False), "잡 원장에 토큰이 남았다"


# ── '/보고서' — 대화를 Report Archive 에 남긴다 ─────────────────────────────────────────────────────────────
class _Tool:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(copy.deepcopy(args))
        return json.dumps({"report_id": 58})


HIST = [{"role": "user", "content": "보고서 41 게시 미리보기 해 줘"},
        {"role": "assistant", "content": f"미리보기 결과입니다 — 볼 사람 120명.\n{PREVIEW}"},
        {"role": "user", "content": f"토큰 {TOKEN} 로 eng-board 에 게시해 줘"},
        {"role": "assistant", "content": "게시했습니다 — eng-board."}]


@pytest.mark.parametrize("note", ["", "정리"])
def test_보고서로_남기는_대화에는_토큰이_없다(monkeypatch, note):
    tool, seen = _Tool(), []

    async def tools(*_a, **_k):
        return {"create_report_draft": tool}

    class _Agent:
        async def ainvoke(self, payload, config=None):
            seen.append(json.dumps(payload, ensure_ascii=False, default=str))
            md = "# 게시 정리\n## 요약\n보고서 41 을 게시했다.\n## 결론과 근거\n볼 사람 120명.\n## 미결·확인 필요\n없음"
            return {"messages": [type("M", (), {"content": md})()]}

    async def agent_for(*_a, **_k):
        return _Agent()
    monkeypatch.setattr(d, "_tools_by_name", tools)
    monkeypatch.setattr(a, "_agent_for", agent_for)
    hist = copy.deepcopy(HIST)

    async def go():
        return [c async for c in d.run_report_save(None, note, hist, [], user="u@corp.com")]
    events = asyncio.run(go())
    assert tool.calls, "시험 전제 — 보고서를 저장했다"
    sent = json.dumps(tool.calls, ensure_ascii=False)
    assert TOKEN not in sent and pt.MASK in sent and "볼 사람 120명" in sent, sent[:400]
    assert not [x for x in seen if TOKEN in x], "정리본 LLM 에 토큰을 넘겼다"
    assert hist == HIST, "대화 이력(호출자의 것)을 고쳤다 — 저장소의 대화는 그대로여야 한다"
    assert all(TOKEN not in c.decode() for c in events)

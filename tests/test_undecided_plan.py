# T7 판정 불가 과반 → 측정 계획(DELIB_UNDECIDED) — 꺼져 있으면 프롬프트가 종전 그대로이고, 켜면 앉은 좌석의 과반일 때만 결론부가 측정 계획이 된다
"""HWAXPortal docs/deliberation-quality plan F6 ② · checklist T7(사용자 결정 2026-11-21 로 다시 엶 — docs/leftovers-1121 L21-5 ③).

엔진을 **실제로 끝까지 돌려** 본다(봉인 시험과 같은 하네스 — 가짜 모델이 받은 프롬프트를 전부 적는다). 고정하는 것.
  · 기본은 꺼짐 — 수렴 라운드 지시가 종전 글자 그대로이고 결정문에 표지가 없다.
  · 켜면 수렴 라운드 좌석에 '판정 불가 · next_measurement' 지시가 붙는다.
  · **앉은 좌석**의 과반이 판정 불가일 때만 의장이 측정 계획 전제를 받고, 코드가 결정문 머리에 표지를 찍고, 경고 사건이 나간다.
  · 리스크 심사 · 계획서 틀에는 켜도 붙지 않는다.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_silent_drops import _pin_context, _Tool  # noqa: E402, F401

_SEATS = [{"key": "mech-a", "role": "기구 좌석 역할"}, {"key": "rel-b", "role": "신뢰성 좌석 역할"},
          {"key": "mat-c", "role": "재료 좌석 역할"}]
_LEGACY_TAIL = ("형성된 다수 의견에 대한 당신의 스탠스(동의/조건부 동의/반대)와 최종 입장 한 줄 요약을 밝혀라. "
                "JSON {final_position,non_negotiable,vote,stance,position_short} 로.")
_NS = lambda **kw: SimpleNamespace(**{"undecided": 1, "chair_template": "default", **kw})  # noqa: E731


def _seat_json(stance: str, nm: str = "") -> str:
    return json.dumps({"lens": "관점", "reads": [], "recommendation": "권장", "concerns": ["가", "나"], "position_short": "요약",
                       "final_position": "최종 입장", "non_negotiable": "", "vote": "진행", "stance": stance,
                       **({"next_measurement": nm} if nm else {}), "relevant": True, "reason": "연관"}, ensure_ascii=False)


class _Tools(dict):
    def __bool__(self):
        return True

    def __contains__(self, _n):
        return True

    def get(self, name, default=None):
        return _Tool(name, {"recommend_agents": json.dumps([{"agent_type": s["key"]} for s in _SEATS]),
                            "create_report_draft": '{"report_id": 7}'}.get(name, '{"hits": []}'))

    def items(self):
        return []


def _run(monkeypatch, tmp_path, *, stances, job="default", advanced=None, personas=_SEATS, chair_fails=False, status="done"):
    """deliberate_start 로 열어 엔진을 끝까지 돌린다. stances — 좌석 역할 낱말 → 수렴 라운드에서 낼 stance. chair_fails — 의장 호출이 늘 실패한다."""
    import langgraph.prebuilt

    seen = []

    async def _fake_tools(*_a, **_k):
        return _Tools()

    async def _fake_llm(_llm, system, human):
        seen.append((system, human))
        if "엔지니어링 톤" in system:
            if chair_fails:
                raise RuntimeError("의장 호출 실패(시험)")
            return "결정문 본문"
        for word, st in stances.items():
            if word in system or word in human[:4000]:
                return _seat_json(st, "접촉각을 재서 30도 이하면 A 안" if st.startswith("판정") else "")
        return _seat_json("동의")

    async def _fake_gather(_agent, persona, *_a, **_k):
        return persona["key"], [], "", ""

    monkeypatch.setattr(d, "_tools_by_name", _fake_tools)
    monkeypatch.setattr(d, "_llm_text", _fake_llm)
    monkeypatch.setattr(d, "_free_gather_one", _fake_gather)
    monkeypatch.setattr(d, "_tools_for_seat", lambda *_a, **_k: {})
    monkeypatch.setattr(d, "_app_of_tools", lambda: {})
    monkeypatch.setattr(app, "_area_of", lambda _n: ("", ""))
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda _llm, tools, **_k: object())
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {})
    monkeypatch.setattr(delib_jobs, "_TASKS", {})
    monkeypatch.setattr(m, "_APP", SimpleNamespace(state=SimpleNamespace(llm=object(), delib_llm=None)))

    async def go():
        out = await m.deliberate_start("코팅 A 안과 B 안 가운데 무엇으로 갈 것인가", job=job, rounds=2, personas=personas,
                                       advanced={"save_report": 0, **(advanced or {})})
        await delib_jobs._TASKS[out["job_id"]]
        return delib_jobs._JOBS[out["job_id"]]

    rec = asyncio.run(go())
    assert rec["status"] == status, f"심의가 기대한 상태({status})로 끝나지 않았다 — {rec['status']} · {rec.get('error')}"
    chair = next((h for s, h in seen if "엔지니어링 톤" in s), "")
    seats = [h for s, h in seen if "엔지니어링 톤" not in s]
    return SimpleNamespace(job=rec, chair=chair, seats=seats, decision=str(rec.get("decision") or rec.get("result") or ""))


_TWO_OF_THREE = {"기구 좌석 역할": "판정 불가", "신뢰성 좌석 역할": "판정 불가 — 데이터 없음", "재료 좌석 역할": "동의"}
_ONE_OF_THREE = {"기구 좌석 역할": "판정 불가", "신뢰성 좌석 역할": "동의", "재료 좌석 역할": "조건부 동의"}


# ── 도우미 ─────────────────────────────────────────────────────────
def test_기본은_꺼짐이고_낱말로도_켠다(monkeypatch):
    assert d._UNDECIDED == 0 and d._resolve_opts({}).undecided == 0
    assert d._resolve_opts({"undecided": 1}).undecided == 1 and d._resolve_opts({"undecided": "on"}).undecided == 1
    assert d._resolve_opts({"undecided": "off"}).undecided == 0
    assert "undecided" in d._INT_KEYS and "undecided" in d._ONOFF_KEYS


def test_꺼져_있으면_덧붙이는_글이_없다():
    off = SimpleNamespace(undecided=0, chair_template="default")
    assert d._undecided_ask(off) == "" and d._undecided_majority(off, [{"stance": "판정 불가"}], 1) is None
    assert d._undecided_chair_note(None) == ""


@pytest.mark.parametrize("tpl", ["risk-review", "sim-plan", "test-plan", "build-plan"])
def test_리스크_심사와_계획서_틀에는_켜도_붙지_않는다(tpl):
    o = _NS(chair_template=tpl)
    assert d._undecided_on(o) is False and d._undecided_ask(o) == ""
    assert d._undecided_majority(o, [{"stance": "판정 불가"}] * 3, 3) is None


def test_판정_불가는_머리에_올_때만_센다():
    yes = [{"stance": "판정 불가"}, {"stance": "판정불가"}, {"stance": "「판정 불가」 — 측정 필요"}, {"vote": "판정 불가 — 다음에 측정할 것: 접촉각"},
           {"stance": "판단 불가"}, {"stance": "Undecided"}, {"stance": "cannot be decided yet"}]
    yes += [{"stance": "판정 불가능"}, {"stance": "판정 불가(근거 부족)"}, {"stance": "판정 불가. 접촉각 자료가 없다"}, {"stance": "판정 불가입니다"}]
    # 머리에 와도 세지 않는 것 — 부정 · 지난 일 · 다른 낱말(반대 검토: 종전 시험은 첫 항목의 글자를 뒤집어 넣어 이 틈을 가렸다).
    no = [{"stance": "판정 불가는 아니다"}, {"stance": "판정 불가가 아님 — A 안"}, {"stance": "판정 불가도 아니고 반대도 아니다"},
          {"stance": "판정 불가능하지 않다"}, {"stance": "판정 불가였으나 근거가 생겨 A 안"}, {"stance": "판정 불가했지만 지금은 동의"},
          {"stance": "판정 불가피 — A 안으로 간다"}, {"stance": "판단 불가결한 자료는 다 있다"},
          {"stance": "동의"}, {"stance": "조건부 동의 — 판정 불가였으나 근거가 생겼다"}, {"vote": "A 안"}, {"stance": None}, {}, "글", None]
    for o in yes:
        assert d._is_undecided(o), o
    for o in no:
        assert not d._is_undecided(o), o
    assert len(d._undecided_seats(yes)) == len(yes) and d._undecided_seats(no) == []


def test_과반의_분모는_앉은_좌석이다():
    """응답자만 세면 좌석 유실이 과반을 만든다 — 5석 가운데 2석만 답했고 둘 다 판정 불가여도 과반이 아니다."""
    o = _NS()
    und = [{"stance": "판정 불가"}] * 2
    assert d._undecided_majority(o, und, 5) is None
    assert d._undecided_majority(o, und + [{"stance": "동의"}], 3) == (2, 3)
    assert d._undecided_majority(o, und, 4) is None, "딱 절반은 과반이 아니다"
    assert d._undecided_majority(o, [], 0) is None


def test_의장_전제와_표지의_글():
    note, mark = d._undecided_chair_note((2, 3)), d._undecided_mark((2, 3))
    assert "앉은 3석 가운데 2석" in note and "측정 계획" in note and "next_measurement" in note and note.endswith("]\n")
    assert mark.startswith("■ 판정 불가 과반 — 앉은 3석 가운데 2석") and "측정 계획" in mark


# ── 끝까지 돌려서 ───────────────────────────────────────────────────────
def test_꺼져_있으면_수렴_지시가_종전_글자_그대로다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, stances=_TWO_OF_THREE)
    conv = [h for h in r.seats if "최종 입장 한 줄 요약을 밝혀라" in h]
    assert conv, "수렴 라운드 프롬프트를 못 찾았다 — 하네스가 엔진과 어긋났다"
    assert all(h.endswith(_LEGACY_TAIL) for h in conv), "꺼져 있는데 수렴 지시가 달라졌다"
    assert not any("next_measurement" in h for h in r.seats) and "판정 불가 과반" not in r.chair
    assert "■ 판정 불가 과반" not in r.decision


def test_켜면_과반일_때_의장이_측정_계획으로_쓰고_코드가_표지를_찍는다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, stances=_TWO_OF_THREE, advanced={"undecided": 1})
    conv = [h for h in r.seats if "최종 입장 한 줄 요약을 밝혀라" in h]
    assert conv and all("stance 를 '판정 불가' 로 적고" in h and h.endswith("position_short,next_measurement} 로.") for h in conv)
    assert "[판정 불가 과반 — 수렴 라운드에서 앉은 3석 가운데 2석" in r.chair
    assert "접촉각을 재서 30도 이하면 A 안" in r.chair, "좌석이 적은 다음 측정이 의장에게 닿아야 합칠 수 있다"
    assert "■ 판정 불가 과반 — 앉은 3석 가운데 2석" in r.decision
    # 잡 원장은 경고의 글만 적는다(delib_jobs._apply) — 화면과 호출자가 보는 것이 이 줄이다.
    assert any("앉은 3석 가운데 2석이 근거 부족으로 '판정 불가'" in w for w in r.job.get("warnings") or []), r.job.get("warnings")


def test_켜도_과반이_아니면_판정문_그대로다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, stances=_ONE_OF_THREE, advanced={"undecided": 1})
    assert any("next_measurement" in h for h in r.seats), "지시는 붙는다"
    assert "판정 불가 과반" not in r.chair and "■ 판정 불가 과반" not in r.decision
    assert not any("판정 불가" in w for w in r.job.get("warnings") or [])


def test_리스크_심사는_켜도_수렴_지시가_그대로다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, stances=_TWO_OF_THREE, job="risk-review-sealed", advanced={"undecided": 1})
    assert not any("next_measurement" in h for h in r.seats) and "판정 불가 과반" not in r.chair
    assert "■ 판정 불가 과반" not in r.decision


# ── 반대 검토가 짚은 것(2026-11-22) ────────────────────────────────────────────
def test_켠_심의의_집계와_표는_판정_불가_좌석을_미표명으로_센다(monkeypatch, tmp_path):
    """종전 정규화는 '판정 불가' 를 판별 안 되는 글로 보고 '조건부 동의' 로 떨어뜨렸다 — 머리에는 '판정 불가 과반' 인데 집계는 '조건부 동의 2' 였다."""
    r = _run(monkeypatch, tmp_path, stances=_TWO_OF_THREE, advanced={"undecided": 1})
    t = r.job["tally"]
    assert (t["agree"], t["conditional"], t["oppose"], t["abstain"], t["total"]) == (1, 0, 0, 2, 3), t
    assert {v["seat"]: v["stance"] for v in r.job["votes"]} == {"mech-a": "미표명", "rel-b": "미표명", "mat-c": "동의"}
    assert all(v["responded"] for v in r.job["votes"]), "답은 했다 — 응답 못 한 좌석과 다르다"
    assert r.job.get("unanimous") is not True


def test_꺼진_심의의_집계는_종전_그대로다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, stances=_TWO_OF_THREE)
    t = r.job["tally"]
    assert (t["agree"], t["conditional"], t["abstain"]) == (1, 2, 0), t
    assert d._stance_label({"stance": "판정 불가"}) == "조건부 동의" and d._stance_label({"stance": "판정 불가"}, True) == "미표명"
    assert d._stance_label({"stance": "조건부 동의", "vote": "판정 불가 — 측정 뒤"}, True) == "미표명", "과반을 세는 판정과 같아야 한다(vote 머리)"
    assert d._stance_label({"stance": "반대"}, True) == "반대"


def test_기록용_회의록에_다음_측정이_실린다():
    o = {"final_position": "근거가 없다", "vote": "판정 불가", "stance": "판정 불가", "next_measurement": "접촉각을 재서 30도 이하면 A 안"}
    full, bubble = d._say_of(3, o, full=True), d._say_of(3, o)
    assert "다음에 잴 것 — 접촉각을 재서 30도 이하면 A 안" in full
    assert "다음에 잴 것" not in bubble, "버블은 종전 그대로다"
    assert "다음에 잴 것" not in d._say_of(3, {"final_position": "가", "vote": "나"}, full=True)
    assert "- " not in d._say_of(3, {**o, "next_measurement": ["접촉각", "표면 에너지"]}, full=True).split("다음에 잴 것 — ")[1][:2]


def test_다단_심의의_1단에는_켜도_붙지_않는다():
    """sim-plan · build-plan 은 꺼짐 목록인데 그 1단은 틀 이름이 mechanism 이라 비켜 갔다 — 1단 결정문이 측정 계획이 되면 2단이 메커니즘 없이 돈다."""
    import ast
    import inspect

    src = inspect.getsource(d)
    tree = ast.parse(src)
    hits = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = [n for n in ast.walk(fn) if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Attribute)]
        mech = [n for n in body if n.targets[0].attr == "chair_template" and isinstance(n.value, ast.Constant) and n.value.value == "mechanism"]
        for n in mech:
            owner = ast.unparse(n.targets[0].value)
            off = [x for x in body if x.targets[0].attr == "undecided" and ast.unparse(x.targets[0].value) == owner
                   and isinstance(x.value, ast.Constant) and x.value.value == 0]
            hits.append((owner, bool(off)))
    assert hits, "1단 틀을 세우는 자리를 못 찾았다 — 엔진이 바뀌었다"
    assert all(ok for _o, ok in hits), f"1단 틀(mechanism)을 세우면서 undecided 를 끄지 않는 자리가 있다 — {hits}"
    assert "mechanism" not in d._UNDECIDED_TEMPLATES_OFF, "틀 이름으로 끄면 단독 mechanism 심의가 생길 때 같이 꺼진다 — 1단에서 끈다"


def test_의장이_못_낸_결정문에는_표지를_찍지_않는다(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, stances=_TWO_OF_THREE, advanced={"undecided": 1}, chair_fails=True, status="error")
    assert "의장 결정문 없음" in r.decision, r.decision[:200]
    assert "■ 판정 불가 과반" not in r.decision, "회의록 보존 글 위에 '결론부는 측정 계획이다' 가 찍혔다"


def test_되살린_결정문은_그때_적어_둔_판정을_쓴다():
    assert d._undecided_restored({"text": "가", "um": [2, 3]}) == (2, 3)
    for bad in ({"text": "가"}, {"um": None}, {"um": [2]}, {"um": [True, 3]}, {"um": "2,3"}, {"um": [2, "3"]}, None, "글"):
        assert d._undecided_restored(bad) is None, bad
    src = Path(d.__file__).read_text(encoding="utf-8")
    assert '**({"um": list(_um)} if _um else {})' in src, "e:decision 줄에 그때의 판정을 적지 않는다"
    assert "if _rdec is None else _undecided_restored(_rdec)" in src, "되살릴 때 다시 센다 — 스위치가 바뀐 박스에서 결정문과 표지가 어긋난다"


def test_러너가_이_손잡이를_안다():
    from tools import delib_ab

    assert "undecided" in delib_ab.KNOB_ORDER and delib_ab.levels_of("undecided") == (0, 1)

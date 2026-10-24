# 심의 결정문을 MX 백서 LLM 위키 원천으로 — delib_opts.wiki(기본 꺼짐) · 사람 명의 · 소속 범위(HWAXPortal docs/mxwp-wiki P8)
#
# 고정하는 것.
#   · 켠 심의만 보낸다. 끄면(기본) wiki_submit_source 를 부르지 않는다.
#   · 봉투는 MX 백서 계약의 칸만이다 — 모르는 칸은 MX 백서가 거부한다. 형제 리포가 있으면 **그쪽 pydantic 모델로** 검증한다.
#   · 표는 착석 전원(답하지 못한 좌석은 미표명) · 소수 의견은 동의가 아닌 좌석의 마지막 입장 전문 · 결정문은 자르지 않는다.
#   · 범위는 시작한 사람의 소속(aff:<소속>), 소속이 없으면 후보(plat:mxwhitepaper 제안). 신원 없는 호출 · 의장 실패는 보내지 않는다.
#   · 결과는 결정문 끝 안내 한 줄 — 원천 번호 또는 남기지 못한 까닭(도구가 준 사유 그대로).
#   · 잡 러너는 위키를 켠 잡에만 잡 id · 소속을 입구에 넘긴다(다른 잡의 진입 호출은 종전 그대로).
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_wiki.py -q
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

from test_delib_sealed import _run, _Tripwire  # noqa: E402
from test_delib_silent_drops import _pin_context  # noqa: E402, F401

_MXWP_ENVELOPE = Path(__file__).resolve().parents[2] / "MXWhitePaper" / "apps" / "api" / "app" / "wiki" / "envelope.py"
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
SEATS = [{"key": "mech-a", "role": "기구"}, {"key": "rel-b", "role": "신뢰성"}, {"key": "ther-c", "role": "열"}]
LAST = [{"persona": "mech-a", "stance": "동의", "final_position": "리브 보강에 동의"},
        {"persona": "rel-b", "stance": "반대합니다", "final_position": "낙하 시험 없이 확정은 이르다", "non_negotiable": "시험 먼저"}]
POOL = [(1, "mech-a", "find_part", "{}", "힌지 브래킷 · 412 MPa"), (2, "rel-b", "find_part", "{}", "힌지 브래킷 · 412 MPa"),
        (2, "rel-b", "drop_result", "{}", "x" * 900)]
EVID = [{"key": "", "source": "챗", "tool": "query_voc", "args": "", "result": "VOC 12건 — 힌지 크랙", "cut": None},
        {"key": "", "source": "회의 메모", "tool": "", "args": "", "result": "지난 분기 같은 불량", "cut": None}]


def _env(**kw):
    base = dict(question="힌지 브래킷 리브를 보강할 것인가", decision="결정 — 보강한다.\n\n소수 의견 — 시험 먼저.",
                stage="default", personas=SEATS, last_list=LAST, rid=7, evidence=EVID, pool=POOL,
                owner="u1@corp.example", affiliation="CAEG", ref="default-20261024-090000-abc123", now=NOW)
    return d._wiki_envelope(**{**base, **kw})


def _mxwp_model():
    if not _MXWP_ENVELOPE.exists():
        pytest.skip(f"형제 리포 없음: {_MXWP_ENVELOPE}")
    spec = importlib.util.spec_from_file_location("_mxwp_wiki_envelope", _MXWP_ENVELOPE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # pydantic 이 지연 주석(from __future__ import annotations)을 이 모듈에서 푼다
    spec.loader.exec_module(mod)
    return mod.SourceEnvelope


# ── 봉투 ─────────────────────────────────────────────────────────────────────────────────────────
def test_봉투는_MX_백서_모델을_통과하고_계약의_칸만이다():
    env = _env()
    got = _mxwp_model().model_validate(env)
    assert got.kind == "deliberation" and got.audience == ["aff:CAEG"]
    assert set(env) == {"schema", "kind", "origin", "title", "captured_at", "owner", "audience", "sensitivity", "consent", "body"}
    assert set(env["body"]) == {"question", "decision", "votes", "evidence", "minority", "job_id"}
    assert env["origin"] == {"system": "hwax-portal", "ref": "delib:default-20261024-090000-abc123:default"}
    assert env["consent"] == {"by": "u1@corp.example", "at": NOW.isoformat(), "mode": "button"}
    assert env["body"]["job_id"] == "default-20261024-090000-abc123" and env["body"]["decision"].endswith("시험 먼저.")


def test_표는_착석_전원이고_소수_의견은_동의가_아닌_좌석의_입장_전문이다():
    body = _env()["body"]
    assert body["votes"] == [{"seat": "mech-a", "stance": "동의"}, {"seat": "rel-b", "stance": "반대"},
                             {"seat": "ther-c", "stance": "미표명(응답 없음)"}]
    assert "[rel-b] 반대 — " in body["minority"] and "낙하 시험 없이 확정은 이르다" in body["minority"]
    assert "시험 먼저" in body["minority"], "양보 불가 조항도 마지막 입장의 일부다(_say_of full)"
    assert "[ther-c] 미표명 — 마지막 라운드에 응답하지 못했다" in body["minority"]
    assert "mech-a" not in body["minority"]
    assert "minority" not in _env(personas=SEATS[:1], last_list=LAST[:1])["body"], "전원 동의면 소수 의견 칸이 없다"


def test_근거는_보고서_호출자_근거_좌석_조회이고_같은_것은_한_번_발췌는_인용_상한까지():
    ev = _env()["body"]["evidence"]
    assert ev[0] == {"kind": "doc", "ref": "report-archive:7", "excerpt": "이 심의의 Report Archive 보고서 #7 — 결정문 · 회의록 전문"}
    assert ev[1] == {"kind": "tool", "ref": "query_voc", "excerpt": "VOC 12건 — 힌지 크랙"}
    assert ev[2] == {"kind": "doc", "ref": "회의 메모", "excerpt": "지난 분기 같은 불량"}, "도구 없는 근거는 doc"
    assert [e["ref"] for e in ev[3:]] == ["find_part", "drop_result"], "두 좌석이 같은 결과를 받았으면 한 번"
    assert len(ev[4]["excerpt"]) == d._WIKI_EXCERPT_MAX and ev[4]["excerpt"].endswith("…")
    assert _env(rid=None)["body"]["evidence"][0]["ref"] == "query_voc", "보고서를 안 남겼으면 보고서 근거가 없다"


def test_소속이_없으면_후보로_MX_백서_쓰는_사람_모두를_제안한다():
    env = _env(affiliation="")
    assert env["audience"] == ["plat:mxwhitepaper"] and env["consent"]["mode"] == "candidate"
    _mxwp_model().model_validate(env)


# ── 안내 ─────────────────────────────────────────────────────────────────────────────────────────
def test_안내는_원천_번호_또는_남기지_못한_까닭이다():
    env, cand = _env(), _env(affiliation="")
    ok = json.dumps({"id": "b19ed62c-6a59", "short": "src_b19ed62c", "dedup": False, "ingest_state": "queued"})
    assert d._wiki_note(ok, env) == "\n\n📚 MX 백서 위키 원천 src_b19ed62c 로 남겼다(범위 aff:CAEG · 들이기 queued)."
    assert "원천 src_b19ed62c" in d._wiki_note(json.dumps({"result": json.loads(ok)}), env), "구조화 꼴도 받는다"
    assert "이미 MX 백서 위키에 있다" in d._wiki_note(json.dumps({"id": "x", "short": "src_x", "dedup": True}), env)
    c = d._wiki_note(json.dumps({"id": "y", "short": "src_y", "ingest_state": "held"}), cand)
    assert "후보 src_y" in c and "'내 후보'" in c and "plat:mxwhitepaper" in c
    assert "wiki_submit_source 가 없다" in d._wiki_note(None, env)
    why = "(tool wiki_submit_source error: 보낸 사람이 갖지 않은 범위 토큰이다 — 잠시 뒤 다시 보내라)"
    assert "보낸 사람이 갖지 않은 범위 토큰이다 — 잠시 뒤 다시 보내라" in d._wiki_note(why, env), "사유를 그대로 싣는다"
    assert "원천 번호를 돌려주지 않았다" in d._wiki_note('{"hits": []}', env)


def test_의장이_결정문을_못_냈거나_신원이_없으면_보내지_않는다():
    import asyncio

    called = []

    class _T:
        async def ainvoke(self, args):
            called.append(args)
            return "{}"

    kw = dict(question="q", decision="d", stage="default", personas=SEATS, last_list=LAST, rid=None, evidence=[], pool=[])
    note = asyncio.run(d._wiki_submit({d._WIKI_TOOL: _T()}, user="u1@corp.example", chair_failed="시간 초과", **kw))
    assert "의장이 결정문을 내지 못해" in note
    note = asyncio.run(d._wiki_submit({d._WIKI_TOOL: _T()}, user="", chair_failed=None, **kw))
    assert "신원 없는 호출" in note and called == []


# ── 끝에서 끝(실제 deliberate_start → 실제 엔진, 가짜 도구 · LLM) ──────────────────────────────────────────
def _ctx(user, aff="", groups=""):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={
        "x-hwax-user": user, **({"x-hwax-affiliation": aff} if aff else {}), **({"x-hwax-groups": groups} if groups else {})})))


@pytest.fixture
def wiki_answer(monkeypatch):
    monkeypatch.setitem(_Tripwire._ANSWERS, d._WIKI_TOOL,
                        '{"id": "4f1e15e6-fcfd", "short": "src_4f1e15e6", "dedup": false, "ingest_state": "queued"}')


def test_켠_심의는_잡_id_와_소속으로_사람_명의_원천을_남기고_결정문_끝에_적는다(monkeypatch, tmp_path, wiki_answer):
    r = _run(monkeypatch, tmp_path, job="default", wiki=True,
             ctx=_ctx("u1@corp.example", "CAEG", "plat:mxwhitepaper"))
    assert r.tools.count(d._WIKI_TOOL) == 1
    args = r.args[d._WIKI_TOOL]
    env = args["envelope"]
    assert args["ingest"] is True
    assert env["owner"] == {"email": "u1@corp.example"} and env["audience"] == ["aff:CAEG"]
    assert env["origin"]["ref"] == f"delib:{r.job['id']}:default" and env["body"]["job_id"] == r.job["id"]
    assert "결정문 본문" in env["body"]["decision"] and "MX 백서 위키" not in env["body"]["decision"], "안내는 봉투 밖"
    assert env["body"]["evidence"][0]["ref"] == "report-archive:7", "보고서를 먼저 저장하고 그 번호를 근거로"
    assert [v["seat"] for v in env["body"]["votes"]] == ["mech-a", "rel-b"]
    _mxwp_model().model_validate(env)
    assert r.job["decision"].endswith("📚 MX 백서 위키 원천 src_4f1e15e6 로 남겼다(범위 aff:CAEG · 들이기 queued).")
    assert (r.job.get("opts") or {}).get("wiki") == 1, "잡 원장에 걸린 값이 남는다"


def test_끄면_위키를_부르지_않는다(monkeypatch, tmp_path, wiki_answer):
    r = _run(monkeypatch, tmp_path, job="default", ctx=_ctx("u1@corp.example", "CAEG"))
    assert d._WIKI_TOOL not in r.tools and "MX 백서 위키" not in (r.job.get("decision") or "")


def test_소속이_없는_사람의_심의는_후보로_간다(monkeypatch, tmp_path, wiki_answer):
    r = _run(monkeypatch, tmp_path, job="default", wiki=True, ctx=_ctx("u1@corp.example"))
    env = r.args[d._WIKI_TOOL]["envelope"]
    assert env["audience"] == ["plat:mxwhitepaper"] and env["consent"]["mode"] == "candidate"
    assert "후보 src_4f1e15e6" in r.job["decision"]


# ── 잡 러너 · 웹 입구 · MCP 인자 ──────────────────────────────────────────────────────────────────────
def test_잡_러너는_위키를_켠_잡에만_잡_id_와_소속을_넘긴다(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(delib_jobs, "_make_spawn", lambda _app, job, _e, _q, _g, _o, _u, _p, ext_kw, _c:
                        seen.append((job["id"], dict(ext_kw))) or (lambda: None))
    monkeypatch.setattr(delib_jobs, "_pump", lambda: None)
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    for store in ("_JOBS", "_TASKS", "_PENDING"):
        monkeypatch.setattr(delib_jobs, store, {})
    a = delib_jobs.start(object(), "default", "화두 하나", delib_opts={"wiki": 1}, user_email="u1@corp.example", owner_aff="CAEG")
    b = delib_jobs.start(object(), "default", "화두 둘", user_email="u1@corp.example", owner_aff="CAEG")
    assert seen == [(a["id"], {"job_id": a["id"], "affiliation": "CAEG"}), (b["id"], {})]
    seen.clear()
    delib_jobs._requeue(object(), a, {"question": "화두 하나", "job_kind": "default", "opts": {"wiki": 1},
                                      "user_email": "u1@corp.example"})
    assert seen == [(a["id"], {"job_id": a["id"], "affiliation": "CAEG"})], "재기동 뒤 다시 선 줄도 같은 칸"


def test_웹_입구는_위키를_켠_요청에만_소속을_넘긴다():
    on = app.ChatRequest(message="/심의 x", affiliation="CAEG", delib_opts={"wiki": 1})
    assert app._wiki_kw(on) == {"affiliation": "CAEG"}
    assert app._wiki_kw(app.ChatRequest(message="/심의 x", affiliation="CAEG")) == {}


def test_MCP_심의_시작은_wiki_를_받고_이어하기가_이어받는다():
    assert m._build_opts(wiki=True) == {"wiki": 1} and m._build_opts() == {}
    assert "wiki" in m._CONT_CARRY
    assert d._resolve_opts({"wiki": "on"}).wiki == 1 and d._resolve_opts({}).wiki == 0

# M0-b 탐침(scripts/m0b_probe.py)이 계획·JSONL 모양·요약 셈·K 팔·창 초과 포착·dry-run 을 지키고, 잡 원장·비밀·카드 원문을 건드리지 않는지 본다
#
# LLM 자리는 대본 LLM(tests/fakes/llm.py)이고, 끝의 두 시험은 가짜 LLM 서버(tests/fakes/llm_server.py)를 빈 포트에 띄워 운영자가
# 치는 명령 그대로(.venv/bin/python scripts/m0b_probe.py …) 서브프로세스로 돌린다 — 실제 ChatOpenAI·HTTP·기동 절차를 지난다.
# 탐침 자체는 포트를 열지 않는다. 시험이 띄운 PID 만 내린다.
#
#   실행:  .venv/bin/python -m pytest tests/test_m0b_probe.py -q
import asyncio
import importlib.util
import json
import math
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import httpx  # noqa: E402
import pytest  # noqa: E402

import app as a  # noqa: E402
import card_review as cr  # noqa: E402
import delib_jobs  # noqa: E402
from deliberation import _llm_limit  # noqa: E402
from fakes.llm import FakeLLM, _split, _status_error  # noqa: E402
from test_card_review_e2e import _free_port, _stop, _wait  # noqa: E402
from test_card_review_prompts import _pack  # noqa: E402

_spec = importlib.util.spec_from_file_location("m0b_probe", ROOT / "scripts" / "m0b_probe.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

FIX = ROOT / "tests" / "fixtures" / "card_review" / "contract"
PACK_OK = FIX / "pack_ok.json"
W1M, W64K = 1_048_576, 65_536
SECRET = "sk-m0b-test-secret-77"


def _big_pack() -> dict:
    """점검 카드가 넉넉한 팩 — 실제 생산자로 만든 카드를 늘린 것(계약 시험과 같은 재료)."""
    cards = _pack(random.Random(7), n=60, size=(300, 900))
    return {"agent_key": "mech-test-seal", "name": "실링 시험", "domain": "mech", "role": "역할 원문", "cards": cards}


def _contents(pack: dict, units: list) -> list:
    """결과 파일에 나오면 안 되는 원문 조각 — 카드 절 본문·제목·역할·단위 글."""
    out = [s["text"][:40] for c in pack["cards"] for s in c.get("sections") or [] if len(s["text"]) >= 12]
    out += [c["title"] for c in pack["cards"] if c.get("title")] + [pack["role"][:20]]
    for u in units:
        out += [x["text"][:20] for k in ("lines", "context", "evidence") for x in u.get(k) or [] if x.get("text")]
    return [x for x in out if x]


class SizedLLM(FakeLLM):
    """대본 LLM 에 창을 단다 — 프롬프트가 limit 자를 넘으면 실제 서버처럼 400 으로 거절한다."""

    def __init__(self, limit_chars: int, script=("oracle",)):
        super().__init__(script)
        self.limit = limit_chars

    async def ainvoke(self, msgs, *a_, **k_):
        system, human = _split(msgs)
        if len(system) + len(human) > self.limit:
            self.calls.append({"system": "", "human": "", "behavior": "over"})
            raise _status_error(400)
        return await super().ainvoke(msgs, *a_, **k_)


@pytest.fixture
def fake_box(monkeypatch, tmp_path):
    """창 64K 의 가짜 박스 — 기동 절차 대신 대본 LLM 을 주고, /health 는 한가하다고 답한다. 작업 디렉터리는 임시다."""
    lim = cr.limits(W64K, "server", 0, env={})
    llm = SizedLLM(lim["p_hard"])
    state = {"llm": llm, "load": {"delib_active": 0, "delib_queued": 0, "review_active": 0}}

    async def boot():
        return state["llm"], state["llm"], []

    monkeypatch.setattr(probe, "boot_llms", boot)
    monkeypatch.setattr(probe, "agent_load", lambda _url: state["load"])
    monkeypatch.setattr(a, "_ctx_cache", {"n": W64K, "src": "server"})
    monkeypatch.chdir(tmp_path)
    state.update(lim=lim, tmp=tmp_path, env=str(tmp_path / "없는.env"))
    return state


def _main(*argv) -> int:
    return probe.main([str(x) for x in argv])


def _read(path):
    rows = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows[0], [r for r in rows if r["rec"] == "call"], rows[-1]


# ── .env 읽기 — start.sh 가 정본이다 ─────────────────────────────────────────────
def _start_sh_env_block() -> str:
    lines = (ROOT / "start.sh").read_text(encoding="utf-8").splitlines()
    i = lines.index("if [ -f .env ]; then")
    j = next(k for k in range(i + 1, len(lines)) if lines[k] == "fi")
    return "\n".join(lines[i: j + 1])


def test_env_파일은_start_sh_와_같은_값으로_읽는다(tmp_path):
    """서버가 보는 LLM 과 같은 것을 재야 한다 — start.sh 의 .env 읽기 블록을 그대로 bash 로 돌려 오라클로 쓴다."""
    body = ("A=1\nB=\"x y\"\nC='q'\n D=bad\nnot a line\n# comment\n\nE=\"unbalanced\nF=a=b\nG=\"\"\nH=\"\n"
            "I=v # 뒤 설명\nJ='mixed\"\nK=끝줄")                # 마지막 줄은 줄바꿈이 없다
    (tmp_path / ".env").write_text(body, encoding="utf-8")
    keys = "A B C D E F G H I J K"
    script = (_start_sh_env_block() + f"\nfor k in {keys}; do if [ -n \"${{!k+x}}\" ]; then printf '%s=%s\\0' \"$k\" "
              "\"${!k}\"; else printf '%s\\0' \"$k\"; fi; done")
    out = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}, timeout=30)
    assert out.returncode == 0, out.stderr
    oracle = dict(x.split("=", 1) for x in out.stdout.split("\0") if "=" in x)
    got, skipped = probe.read_env_file(tmp_path / ".env")
    assert got == oracle
    assert got["B"] == "x y" and got["G"] == "" and got["H"] == '"' and got["K"] == "끝줄" and "D" not in got
    assert skipped == 2 and "건너뜀" in out.stderr
    assert probe.read_env_file(tmp_path / "없음.env") == ({}, 0)


def test_찍는_주소에는_사용자_정보가_없다():
    assert probe.show_url("https://u:pw@llm.example:8443/v1?key=zz") == "https://llm.example:8443/v1"
    assert probe.scrub(f"x {SECRET} y", [SECRET, ""]) == "x *** y"


# ── 계획 ─────────────────────────────────────────────────────────────────────
def test_K_팔은_kout_을_창의_K_out_으로_읽고_같은_묶음은_뺀다():
    assert probe.parse_k_arms("1,4,8,kout", 19) == [("k1", 1), ("k4", 4), ("k8", 8), ("kout", 19)]
    assert probe.parse_k_arms(" 4 , 4 ,K_OUT", 9) == [("k4", 4), ("kout", 9)]
    for bad in ("0", "x", ""):
        with pytest.raises(probe.ProbeError):
            probe.parse_k_arms(bad, 19)
    keep, merged = probe.dedupe_arms([("k1", 1), ("k4", 4), ("k8", 8), ("kout", 19)], 6)
    assert keep == [("k1", 1), ("k4", 4), ("k8", 8)] and merged == [("kout", "k8", 6)]


def test_계획은_회차마다_팔을_번갈아_돌고_끝에_캐시_셋과_창_초과를_둔다():
    pack = _big_pack()
    units, _src = probe.load_units(None)
    lim = cr.limits(W1M, "server", 0, env={})
    arms = probe.parse_k_arms("1,4,8,kout", lim["verdict"]["k_out"])
    items = probe.build_plan(pack, units, lim, reps=6, k_arms=arms, skip=set(), nonce="abcd1234")
    check = [c for c in pack["cards"] if c["checkable"]]
    assert len(check) >= 3 * lim["verdict"]["k_out"] // 2

    assert [x["arm"] for x in items[:6]] == ["k1", "k4", "k8", "kout", "sweep", "offcard"]
    assert [x["arm"] for x in items[-4:]] == ["cache-cold", "cache-repeat", "cache-repair", "overflow"]
    count = {arm: sum(1 for x in items if x["arm"] == arm) for arm in dict.fromkeys(x["arm"] for x in items)}
    assert count == {"k1": 6, "k4": 6, "k8": 6, "kout": 6, "sweep": 3, "offcard": 3, "cache-cold": 1, "cache-repeat": 1,
                     "cache-repair": 1, "overflow": 1}
    for label, k in arms:
        decks = [x for x in items if x["arm"] == label]
        assert {x["k"] for x in decks} == {k} and [x["variant"] for x in decks] == ["fwd", "rev"] * 3
        sets = [frozenset(c["alias"] for c in x["req"]["cards"]) for x in decks]
        assert all(len(s) == k for s in sets) and len(set(sets)) == len(sets)        # 회차마다 다른 묶음
        for x in decks:                                                              # 제목 줄 = 묶음 밖 카드 전부
            assert {d["alias"] for d in x["req"]["digest"]} == {c["alias"] for c in pack["cards"]} - {
                c["alias"] for c in x["req"]["cards"]}
    kout = next(x for x in items if x["arm"] == "kout")
    assert kout["k"] == lim["verdict"]["k_out"] > lim["verdict"]["k"]               # 운영 K(8)를 넘는 팔도 엔진이 받는다

    preps = {i: probe.prepare(x, lim) for i, x in enumerate(items) if x["arm"] != "overflow"}
    assert all(p["ctx"]["prompt_chars"] <= lim["p_hard"] for p in preps.values())
    sweep = next(p for i, p in preps.items() if items[i]["arm"] == "sweep")
    assert len(sweep["ctx"]["units"]) == lim["sweep"]["units_per_page_max"]
    cold, rep, fix = (preps[i] for i, x in enumerate(items) if x["arm"].startswith("cache"))
    assert cold["system"] + cold["human"] == rep["system"] + rep["human"]
    assert fix["human"].startswith(cold["human"]) and "(보충 2/3" in fix["human"][len(cold["human"]):]
    assert "(탐침 abcd1234)" in cold["human"]
    assert items[-2]["pad_chars"] == probe.CACHE_PAD_CHARS                          # 1M 창에서는 줄이지 않는다
    offs = [x["req"]["extra"]["known"] for x in items if x["arm"] == "offcard"]
    assert len({json.dumps(k) for k in offs}) == len(offs)                           # offcard 도 회차마다 다른 글


def test_max_calls_는_회차_꼬리부터_버리고_끝_묶음은_남긴다():
    items = [{"arm": a_} for a_ in ["k1", "k4", "sweep", "k1", "k4", "cache-cold", "cache-repeat", "cache-repair",
                                    "overflow"]]
    keep, dropped = probe.cap_plan(items, 6)
    assert [x["arm"] for x in keep] == ["k1", "k4", "cache-cold", "cache-repeat", "cache-repair", "overflow"]
    assert dropped == 3
    keep, dropped = probe.cap_plan(items, 2)
    assert [x["arm"] for x in keep] == ["cache-cold", "cache-repeat"] and dropped == 7
    assert probe.cap_plan(items, 100) == (items, 0)


def test_창이_좁으면_캐시_채움을_창_한도_안으로_줄인다():
    pack = json.loads(PACK_OK.read_text(encoding="utf-8"))
    units, _src = probe.load_units(None)
    lim = cr.limits(W64K, "server", 0, env={})
    items = probe.build_plan(pack, units, lim, reps=1, k_arms=[("k1", 1)], skip={"sweep", "offcard", "overflow"},
                             nonce="n")
    cold = items[-3]
    assert 0 < cold["pad_chars"] < probe.CACHE_PAD_CHARS
    assert probe.prepare(cold, lim)["ctx"]["prompt_chars"] <= lim["p_hard"]


def test_단위_파일은_엔진_계약으로_보고_여럿이면_회차마다_돌린다(tmp_path):
    base = json.loads((FIX / "req_verdict_fwd.json").read_text(encoding="utf-8"))["unit"]
    two = [{**base, "unit_id": "u-a"}, {**base, "unit_id": "u-b", "title": "둘째"}]
    (tmp_path / "units.json").write_text(json.dumps({"units": two}, ensure_ascii=False), encoding="utf-8")
    units, src = probe.load_units(str(tmp_path / "units.json"))
    assert src == "file" and [u["unit_id"] for u in units] == ["u-a", "u-b"]
    pack = json.loads(PACK_OK.read_text(encoding="utf-8"))
    lim = cr.limits(W1M, "server", 0, env={})
    items = probe.build_plan(pack, units, lim, reps=4, k_arms=[("k1", 1)], skip={"cache", "overflow", "sweep"},
                             nonce="n")
    assert [x["req"]["unit"]["unit_id"] for x in items if x["arm"] == "k1"] == ["u-a", "u-b", "u-a", "u-b"]

    (tmp_path / "bad.json").write_text(json.dumps({**base, "reason": "앱이 칸 이름을 바꿨다"}, ensure_ascii=False),
                                       encoding="utf-8")
    with pytest.raises(probe.ProbeError, match="unknown_fields"):
        probe.load_units(str(tmp_path / "bad.json"))
    (tmp_path / "empty.json").write_text(json.dumps({**base, "lines": [], "context": []}), encoding="utf-8")
    with pytest.raises(probe.ProbeError, match="대조할 것이 없다"):
        probe.load_units(str(tmp_path / "empty.json"))


def test_여러_전문가의_팩은_엔진_별칭으로_다시_묶는다():
    pack = json.loads(PACK_OK.read_text(encoding="utf-8"))
    other = {**pack, "agent_key": "mech-other", "role": "다른 역할",
             "cards": [{**c, "record_id": c["record_id"] + "-o", "card_id": c["card_id"] + "-o"} for c in pack["cards"]]}
    merged = probe.merge_packs([pack, other, pack])                # 같은 카드는 한 번만
    n = len(pack["cards"])
    assert len(merged["cards"]) == 2 * n and merged["merged_from"] == ["mech-test-seal", "mech-other", "mech-test-seal"]
    assert [c["alias"] for c in merged["cards"]] == [f"K{i:02d}" for i in range(1, 2 * n + 1)]
    assert merged["cards"] == cr.assign_aliases([{k: v for k, v in c.items() if k not in ("alias", "chars")}
                                                 for c in pack["cards"] + other["cards"]])
    assert merged["agent_key"] == "mech-test-seal" and merged["role"] == pack["role"]
    assert sum(c["checkable"] for c in merged["cards"]) == 2 * sum(c["checkable"] for c in pack["cards"])
    assert probe.merge_packs([pack]) is pack
    lim = cr.limits(W1M, "server", 0, env={})
    items = probe.build_plan(probe.check_pack(merged), probe.load_units(None)[0], lim, reps=1, k_arms=[("k12", 12)],
                             skip={"sweep", "offcard", "cache", "overflow"}, nonce="n")
    assert items[0]["k"] == 12 and probe.prepare(items[0], lim)["ctx"]["judge"] == [c["alias"] for c in
                                                                                    items[0]["req"]["cards"]]


def test_카드가_없거나_점검_카드가_없으면_시작하지_않는다():
    pack = json.loads(PACK_OK.read_text(encoding="utf-8"))
    with pytest.raises(probe.ProbeError, match="0장"):
        probe.check_pack({**pack, "cards": []})
    with pytest.raises(probe.ProbeError, match="점검 카드"):
        probe.check_pack({**pack, "cards": [c for c in pack["cards"] if not c["checkable"]]})
    with pytest.raises(probe.ProbeError, match="모양"):
        probe.check_pack({"agent_key": "x"})
    assert probe.check_pack(pack) is pack


# ── 팩 받기 — 포털 중계, PAT 는 환경변수로만 ─────────────────────────────────────
def test_팩은_포털_중계에_PAT_로_청하고_PAT_를_찍지_않는다():
    pack = json.loads(PACK_OK.read_text(encoding="utf-8"))
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.headers["authorization"] != f"Bearer {SECRET}":     # 받은 토큰을 되싣는 서버라도 찍지 않는다
            return httpx.Response(401, json={"detail": f"invalid or expired PAT {req.headers['authorization'][7:]}"})
        return httpx.Response(200, json=pack)

    got = probe.fetch_pack("http://portal.test:8723/", "mech-test-seal", SECRET, transport=httpx.MockTransport(handler))
    assert got == pack
    assert seen[0].url.path == "/agent/card-review/pack" and json.loads(seen[0].content) == {"agent_key": "mech-test-seal"}
    with pytest.raises(probe.ProbeError) as ei:
        probe.fetch_pack("http://portal.test:8723", "mech-test-seal", "wrong-token-x", transport=httpx.MockTransport(handler))
    assert "HTTP 401 — PAT" in str(ei.value) and "만료" in str(ei.value) and "wrong-token-x" not in str(ei.value)

    def denied(_req):
        return httpx.Response(403, json={"detail": {"code": "denied", "message": "권한 없음"}})

    with pytest.raises(probe.ProbeError, match="feat:deliberation"):
        probe.fetch_pack("http://portal.test", "k", SECRET, transport=httpx.MockTransport(denied))


def test_agent_는_환경변수_PAT_없이는_멈추고_PAT_인자는_없다(fake_box, monkeypatch, capsys):
    monkeypatch.delenv(probe.PAT_ENV, raising=False)
    assert _main("--agent", "mech-test-seal", "--dry-run", "--env-file", fake_box["env"]) == 2
    assert "read -rs HWAX_PROBE_PAT" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _main("--agent", "mech-test-seal", "--pat", SECRET, "--dry-run")


def test_agent_여럿은_전문가마다_팩을_받아_합친다(fake_box, monkeypatch, capsys):
    pack = json.loads(PACK_OK.read_text(encoding="utf-8"))
    asked = []

    def fetch(portal, key, pat, **_k):
        asked.append((portal, key, pat))
        return {**pack, "agent_key": key, "cards": [{**c, "record_id": f"{c['record_id']}-{key}"} for c in pack["cards"]]}

    monkeypatch.setattr(probe, "fetch_pack", fetch)
    monkeypatch.setenv(probe.PAT_ENV, SECRET)
    assert _main("--agent", "mech-a, mech-b", "--dry-run", "--env-file", fake_box["env"]) == 0
    out = capsys.readouterr().out
    assert asked == [(probe.DEFAULT_PORTAL, "mech-a", SECRET), (probe.DEFAULT_PORTAL, "mech-b", SECRET)]
    assert "카드 20장(점검 12" in out and "mech-a+mech-b 의 카드를 합쳤다" in out and SECRET not in out


# ── 실행 ─────────────────────────────────────────────────────────────────────
def test_dry_run_은_LLM_을_부르지_않고_파일도_쓰지_않는다(fake_box, capsys):
    assert _main("--pack-file", PACK_OK, "--dry-run", "--env-file", fake_box["env"]) == 0
    out = capsys.readouterr().out
    assert fake_box["llm"].calls == []
    assert list(fake_box["tmp"].iterdir()) == []
    assert "[dry-run]" in out and "[계획] 호출" in out and "cache-cold" in out and "overflow" in out


def test_실행은_한_호출에_한_줄을_쓰고_창_초과를_잡고_원문을_쓰지_않는다(fake_box, capsys):
    out_file = fake_box["tmp"] / "m0b.jsonl"
    rc = _main("--pack-file", PACK_OK, "--reps", "2", "--k-arms", "1,4,kout", "--out", out_file,
               "--env-file", fake_box["env"])
    printed = capsys.readouterr().out
    assert rc == 0, printed
    meta, calls, summary = _read(out_file)
    assert meta["rec"] == "meta" and summary["rec"] == "summary"
    assert len(calls) == meta["plan"]["calls"] == len(fake_box["llm"].calls)
    # 점검 카드 6장 — kout(19)은 6장 묶음, k4 는 4장. 회차 2 → 팔마다 2, sweep·offcard 1, 캐시 셋, 창 초과 하나.
    assert meta["plan"]["arms"] == {"k1": 2, "k4": 2, "kout": 2, "sweep": 1, "offcard": 1, "cache-cold": 1,
                                    "cache-repeat": 1, "cache-repair": 1, "overflow": 1}
    keys = {"rec", "ts", "kind", "variant", "arm", "k", "rep", "deck_sha", "unit_sha", "prompt_sha", "prompt_chars",
            "prompt_bytes", "est_tokens", "pad_chars", "soft_over", "seconds", "tokens_in", "tokens_out",
            "chars_per_token", "finish_reason", "out_chars", "think_seen", "rows_expected", "rows_ok", "rows_flagged",
            "rows_missing", "bad_lines", "tail_broken", "foreign", "dropped_alias", "dup", "repair_needed", "tally",
            "error", "i", "n", "load", "dup_prompt"}
    for r in calls:
        assert keys <= set(r), keys - set(r)
    ok = [r for r in calls if r["arm"] != "overflow"]
    assert all(r["error"] is None and r["finish_reason"] == "stop" and r["tokens_in"] > 0 for r in ok)
    assert all(r["chars_per_token"] == round(r["prompt_chars"] / r["tokens_in"], 4) for r in ok)
    assert [r["k"] for r in calls if r["arm"] == "kout"] == [6, 6]
    assert all(r["rows_missing"] == 0 and r["repair_needed"] is False for r in ok if r["kind"] == "verdict")

    ov = calls[-1]
    assert ov["arm"] == "overflow" and ov["error"]["status"] == 400 and ov["error"]["code"] == "context_overflow"
    assert ov["prompt_chars"] > fake_box["lim"]["p_hard"]                         # 창 한도 검사를 끄고 일부러 넘긴다
    assert ov["target_tokens"] == math.ceil(W64K * probe.OVERFLOW_OVER) and ov["cpt_source"] == "cache-cold"
    assert ov["prompt_chars"] >= ov["target_tokens"] * ov["cpt_used"]
    assert len(ov["error"]["excerpt"]) <= probe.EXCERPT <= ov["error"]["excerpt_chars"] or \
        len(ov["error"]["excerpt"]) == ov["error"]["excerpt_chars"]
    assert [r["dup_prompt"] for r in calls if r["arm"].startswith("cache")] == [False, True, False]

    text = out_file.read_text(encoding="utf-8")
    pack = json.loads(PACK_OK.read_text(encoding="utf-8"))
    units, _src = probe.load_units(None)
    assert [x for x in _contents(pack, units) if x in text] == []
    assert summary == {**probe.summarize(meta, calls), "rec": "summary"}
    assert summary["overflow"]["code"] == "context_overflow" and summary["output"]["finish_known"] == len(ok)
    assert "===== M0-b 탐침 요약" in printed and "[창 초과] HTTP 400 → context_overflow" in printed

    # 요약만 다시 — 같은 블록이 나온다. 같은 --out 으로 다시 돌리면 덮지 않고 멈춘다.
    assert _main("--summarize", out_file) == 0
    again = capsys.readouterr().out
    assert again.strip() == probe.render(summary).strip()
    before = out_file.read_bytes()
    assert _main("--pack-file", PACK_OK, "--reps", "1", "--out", out_file, "--env-file", fake_box["env"]) == 2
    assert "덮지 않는다" in capsys.readouterr().err and out_file.read_bytes() == before


def test_다른_일이_돌면_시작하지_않고_allow_busy_면_돈다(fake_box, capsys):
    fake_box["load"] = {"delib_active": 1, "delib_queued": 0, "review_active": 0}
    out_file = fake_box["tmp"] / "busy.jsonl"
    args = ("--pack-file", PACK_OK, "--reps", "1", "--k-arms", "1", "--skip", "sweep,offcard,cache,overflow",
            "--out", out_file, "--env-file", fake_box["env"])
    assert _main(*args) == 3
    assert not out_file.exists() and fake_box["llm"].calls == []
    assert "delib_active 1" in capsys.readouterr().err
    assert _main(*args, "--allow-busy") == 0
    _meta, calls, summary = _read(out_file)
    assert len(calls) == 1 and summary["load"]["busy_calls"] == 1


def test_잇단_실패는_세_번에서_멈추고_거기까지_요약한다(fake_box, capsys):
    fake_box["llm"] = FakeLLM(["unreachable"])
    out_file = fake_box["tmp"] / "fail.jsonl"
    assert _main("--pack-file", PACK_OK, "--reps", "3", "--out", out_file, "--env-file", fake_box["env"]) == 4
    _meta, calls, summary = _read(out_file)
    assert len(calls) == probe.MAX_CONSEC_FAIL and summary["interrupted"] is True
    assert {r["error"]["code"] for r in calls} == {"llm_unreachable"}
    assert summary["errors"] == {"llm_unreachable": 3}
    assert "잇단 실패 3회" in capsys.readouterr().err


def test_Ctrl_C_면_거기까지의_호출과_요약을_남긴다(fake_box, capsys):
    class Interrupt(FakeLLM):
        async def ainvoke(self, msgs, *a_, **k_):
            if len(self.calls) == 2:
                raise KeyboardInterrupt
            return await super().ainvoke(msgs, *a_, **k_)

    fake_box["llm"] = Interrupt(["oracle"])
    out_file = fake_box["tmp"] / "int.jsonl"
    assert _main("--pack-file", PACK_OK, "--reps", "2", "--out", out_file, "--env-file", fake_box["env"]) == 130
    _meta, calls, summary = _read(out_file)
    assert len(calls) == 2 and summary["rec"] == "summary" and summary["interrupted"] is True
    assert "(중단됨)" in capsys.readouterr().out


# ── 요약 셈 ──────────────────────────────────────────────────────────────────
def _call(arm, kind="verdict", **kw):
    r = {"rec": "call", "ts": 100.0, "kind": kind, "variant": "fwd", "arm": arm, "k": 1, "rep": 0, "seconds": 1.0,
         "prompt_chars": 1000, "prompt_bytes": 2000, "est_tokens": 900, "pad_chars": 0, "tokens_in": 800,
         "tokens_out": 100, "chars_per_token": 1.25, "finish_reason": "stop", "out_chars": 300, "think_seen": False,
         "rows_expected": 1, "rows_ok": 1, "rows_flagged": 0, "rows_missing": 0, "bad_lines": 0, "repair_needed": False,
         "error": None, "load": {"delib_active": 0}, "dup_prompt": False}
    r.update(kw)
    return r


def test_백분위는_가장_가까운_순위다():
    assert probe.pct([4, 1, 3, 2], 50) == 2 and probe.pct([4, 1, 3, 2], 95) == 4
    assert probe.pct([5], 50) == 5 and probe.pct([], 50) is None and probe.pct([None, 7], 50) == 7
    assert probe.pct(list(range(1, 21)), 95) == 19 and probe.pct(list(range(1, 101)), 95) == 95


def test_요약은_종류별_초_글자토큰비_절단_K팔_창초과_캐시를_센다():
    calls = [
        _call("k1", seconds=10.0, ts=100.0),
        _call("k1", seconds=30.0, ts=200.0, chars_per_token=1.5, tokens_in=1000, est_tokens=1100),
        _call("k8", k=8, seconds=20.0, rows_expected=8, rows_missing=2, rows_ok=5, rows_flagged=1, bad_lines=3,
              repair_needed=True, finish_reason="length", tokens_out=16000, think_seen=True),
        _call("k8", k=8, seconds=40.0, rows_expected=8, finish_reason=None, tokens_in=None, tokens_out=None,
              chars_per_token=None, load=None, dup_prompt=True),
        _call("sweep", kind="sweep", seconds=5.0, rows_expected=64, rows_missing=4, load={"delib_active": 2}),
        _call("offcard", kind="offcard", error={"code": "llm_timeout", "status": None, "excerpt": "t", "excerpt_chars": 1},
              seconds=3600.0),
        _call("cache-cold", seconds=9.0, pad_chars=200000, chars_per_token=1.1),
        _call("cache-repeat", seconds=2.0, pad_chars=200000, dup_prompt=True),
        _call("overflow", seconds=0.5, ts=900.0, prompt_chars=3_000_000, target_tokens=1_153_434, cpt_used=1.1,
              cpt_source="cache-cold", error={"code": "context_overflow", "status": 400, "excerpt": "max context",
                                              "excerpt_chars": 345}),
    ]
    meta = {"plan": {"calls": 10}, "model": "glm", "ctx_tokens": W1M, "ctx_source": "server", "llm_max_tokens": None,
            "out_tokens": 16000, "k_default": 8, "k_out": 19}
    sm = probe.summarize(meta, calls)
    assert sm["calls_done"] == 9 and sm["calls_planned"] == 10 and sm["calls_failed"] == 2
    assert sm["wall_s"] == 3600.0                     # 처음 시작(100)부터 가장 늦게 끝난 호출(offcard 100 + 3,600)까지
    assert sm["by_kind"]["verdict"] == {"n": 4, "ok": 4, "p50_s": 20.0, "p95_s": 40.0, "max_s": 40.0}
    assert sm["by_kind"]["offcard"] == {"n": 1, "ok": 0, "p50_s": None, "p95_s": None, "max_s": None}
    assert sm["cpt"] == {"n": 4, "median": 1.25, "min": 1.25, "max": 1.5, "est_over_actual": 1.125}
    assert sm["cpt_padded"]["n"] == 2 and sm["cpt_padded"]["median"] == 1.1
    o = sm["output"]
    assert (o["ok_calls"], o["finish_known"], o["usage_known"], o["length"]) == (7, 6, 6, 1)
    assert o["length_arms"] == ["k8"] and o["tokens_out_max"] == 16000 and o["think_seen"] == 1
    k8 = next(x for x in sm["arms"] if x["arm"] == "k8")
    assert (k8["expected"], k8["missing"], k8["missing_rate"], k8["bad_lines"], k8["repair_needed"]) == (16, 2, 0.125, 3, 1)
    assert (k8["flagged"], k8["flag_rate"], k8["length"], k8["p50_s"]) == (1, 0.0625, 1, 20.0)
    assert [x["arm"] for x in sm["arms"]] == ["k1", "k8", "sweep", "offcard"]
    off = next(x for x in sm["arms"] if x["arm"] == "offcard")
    assert off["ok"] == 0 and off["missing_rate"] is None
    assert sm["overflow"]["code"] == "context_overflow" and sm["overflow"]["status"] == 400 and not sm["overflow"]["accepted"]
    assert sm["cache"]["cold"]["seconds"] == 9.0 and sm["cache"]["repeat"]["pad_chars"] == 200000
    assert sm["load"] == {"busy_calls": 1, "unknown_calls": 1} and sm["dup_prompts"] == 1
    assert sm["errors"] == {"llm_timeout": 1, "context_overflow": 1}
    text = probe.render(sm)
    assert "창 1,048,576 토큰(server)" in text and "출력 상한 미전송" in text and "length 1/7 k8" in text
    assert "«max context»(원문 345자)" in text and "같은 글 2.0초" in text and "12.5%" in text
    assert probe.summarize(None, [])["calls_done"] == 0 and "M0-b" in probe.render(probe.summarize(None, []))


# ── 기동 절차 — LLM 만 만들고 잡 원장을 건드리지 않는다 ─────────────────────────────
def test_LLM_은_서버_기동_절차로_만들고_잡_원장은_건드리지_않는다(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("탐침이 잡 원장을 건드렸다")

    for k in ("DELIB_TIMEOUT_S", "DELIB_LLM_MAX_RETRIES", "DELIB_MAX_TOKENS", "LLM_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(delib_jobs, "reap_orphans", boom)
    monkeypatch.setattr(delib_jobs, "closing", boom)
    monkeypatch.setattr(a, "_load_mcp_config", boom)              # 게이트웨이 설정(토큰)도 읽지 않는다
    mcp_before = a._DELIB_MCP
    llm, over, lines = asyncio.run(probe.boot_llms())
    assert _llm_limit(llm) == (1800.0, 2) and _llm_limit(over) == (probe.OVERFLOW_TIMEOUT_S, 1)
    assert getattr(llm, "max_tokens", None) is None                # 출력 상한을 보내지 않는 운영 그대로
    assert any("LLM 한도" in x for x in lines)
    assert a._DELIB_MCP is mcp_before and a._load_mcp_config is boom
    assert "DELIB_TIMEOUT_S" not in __import__("os").environ


# ── 운영자가 치는 명령 그대로 — 가짜 LLM 서버 ─────────────────────────────────────
@pytest.fixture
def fake_llm_server(tmp_path):
    port = _free_port()
    log = tmp_path / "fake-llm.log"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    with open(log, "w") as out:
        proc = subprocess.Popen([sys.executable, str(ROOT / "tests" / "fakes" / "llm_server.py"), "--port", str(port),
                                 "--ctx", str(W64K), "--model", "fake-glm", "--script", "oracle,http_400"],
                                cwd=str(tmp_path), env=env, stdout=out, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    try:
        _wait(f"{url}/health", proc, log)
        yield url
    finally:
        _stop(proc)


def _run_cli(tmp_path, url, *argv):
    (tmp_path / "probe.env").write_text(
        f"VLLM_BASE_URL={url}/v1\nVLLM_MODEL=fake-glm\nVLLM_API_KEY=\"{SECRET}\"\nAGENT_PORT={_free_port()}\n"
        f"DELIB_JOB_DIR={tmp_path / 'jobs'}\nARTIFACT_DIR={tmp_path / 'art'}\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "m0b_probe.py"), "--pack-file", str(PACK_OK),
                           "--env-file", str(tmp_path / "probe.env"), *map(str, argv)],
                          cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300)


def test_명령줄_그대로_가짜_LLM_서버에_돌린다(fake_llm_server, tmp_path):
    with httpx.Client(timeout=10, trust_env=False) as c:
        dry = _run_cli(tmp_path, fake_llm_server, "--dry-run")
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert c.get(f"{fake_llm_server}/_stats").json()["calls"] == 0               # dry-run 은 LLM 을 부르지 않는다
        assert "[창] 65,536 토큰 · 출처 server" in dry.stdout and "키 있음" in dry.stdout

        out_file = tmp_path / "m0b.jsonl"
        run = _run_cli(tmp_path, fake_llm_server, "--reps", "1", "--k-arms", "1", "--skip", "sweep,offcard,cache",
                       "--out", out_file)
        assert run.returncode == 0, run.stdout + run.stderr
        assert c.get(f"{fake_llm_server}/_stats").json()["behaviors"] == ["oracle", "http_400"]
    for blob in (dry.stdout, dry.stderr, run.stdout, run.stderr, out_file.read_text(encoding="utf-8")):
        assert SECRET not in blob
    meta, calls, summary = _read(out_file)
    assert meta["llm"]["tries"] == 2 and meta["overflow_llm"] == {**meta["llm"], "timeout_s": probe.OVERFLOW_TIMEOUT_S,
                                                                  "tries": 1}
    k1, ov = calls
    assert k1["finish_reason"] == "stop" and k1["tokens_in"] > 0 and k1["rows_missing"] == 0     # 실제 응답의 칸을 읽었다
    assert ov["error"]["status"] == 400 and ov["error"]["code"] == "context_overflow" and ov["cpt_source"] == "max-observed"
    assert summary["output"] == {**summary["output"], "finish_known": 1, "usage_known": 1, "length": 0}
    assert not (tmp_path / "jobs").exists()                                          # 잡 원장을 만들지도 않는다

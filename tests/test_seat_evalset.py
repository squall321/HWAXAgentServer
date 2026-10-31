# 전문가 고르기 기준선 평가셋(tools/seat_evalset.py)이 거름 · 몫 · 정답 전문가 단위 가름 · 봉인 · 같은 씨앗 같은 qid · 형제 리포 없음을 지키는지(HWAXPortal docs/request-1029 design-e5 E5-0)
"""가짜 ExpertAgents 나무를 임시 자리에 짓고 본다 — 실제 리포는 마지막 시험 하나만 읽고(없으면 건너뛴다), 결과는 임시 자리에만 쓴다.

  실행:  .venv/bin/python -m pytest tests/test_seat_evalset.py -q
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import seat_evalset as se  # noqa: E402


def _expert(root: Path, eid: str, *, name_ko="", name_en="", samples=(), anti=(), oos=(), related=(), golden=()):
    d = root / "knowledge" / eid
    (d / "eval").mkdir(parents=True, exist_ok=True)
    lines = [f"id: {eid}", f"name_ko: \"{name_ko or eid + ' 전문가'}\"", f"name_en: \"{name_en or eid.title()} Specialist\"",
             "expertise:", "  out_of_scope:"]
    lines += [f"    - {x}" for x in oos] or ["    []"]
    lines += ["routing:", "  example_questions:"]
    lines += [f"    - {x}" for x in samples] or ["    []"]
    lines += ["  anti_examples:"]
    for a in anti:
        if isinstance(a, tuple):                    # (글, 주석) — 주석이 None 이면 주석 없음, 글에 \n 이 있으면 여러 줄
            body, tgt = a
            parts = body.split("\n")
            for i, p in enumerate(parts):
                head = "    - " if i == 0 else "      "
                tail = f"   # → {tgt}" if tgt is not None and i == len(parts) - 1 else ""
                lines.append(f"{head}{p}{tail}")
    if not anti:
        lines.append("    []")
    lines += ["related_experts:"]
    lines += [f"  - id: {b}\n    relation: {r}" for b, r in related] or ["  []"]
    (d / "expert.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "eval" / "golden.jsonl").write_text("".join(json.dumps({"q": g, "expected": eid}, ensure_ascii=False) + "\n"
                                                     for g in golden), encoding="utf-8")


def test_주석_목록을_여러_줄_항목까지_읽는다(tmp_path):
    _expert(tmp_path, "mem-pack", anti=[("PoP 볼 개수를 늘리면\n  TMV 는 어떻게 하나요?", "mem-warp"), ("주석 없는 반례", None),
                                         ("두 대상 반례", "sim-a·sim-b"), ("일반 대상", "도메인 전문가"), ("와일드", "sim-*")])
    ex, _st = se.load_experts(tmp_path)
    anti = ex["mem-pack"]["anti"]
    assert [c for _q, c in anti] == ["mem-warp", None, "sim-a·sim-b", "도메인 전문가", "sim-*"]
    assert anti[0][0].startswith("PoP 볼 개수를 늘리면") and "TMV" in anti[0][0], "여러 줄 항목은 한 질의다"
    assert se.parse_target("sim-a·sim-b") == ["sim-a", "sim-b"] and se.parse_target("도메인 전문가") == []
    assert se.parse_target("sim-*") == [] and se.parse_target("rel-x (S12)") == ["rel-x"]


def test_거름마다_까닭으로_센다(tmp_path):
    _expert(tmp_path, "pcb-warp", name_ko="PCB 휨 전문가", samples=["보드 스택업 잔류동박 불균형으로 보드가 휩니다 대칭 기준은"],
            golden=["리플로우 중 PCB 휨이 커집니다"])
    _expert(tmp_path, "mem-pack", anti=[
        ("보드 스택업 잔류동박 불균형으로 보드가 휩니다 대칭 기준은?", "pcb-warp"),     # L1 — 정답 표본과 거의 같다
        ("PCB 휨 전문가에게 묻습니다 휨 기준", "pcb-warp"),                           # L2 — 정답 이름(전문가를 뗀 'PCB 휨')
        ("pcb-warp 키로 부르는 질문입니다", "pcb-warp"),                             # L2 — 키
        ("없는 키를 가리킨다", "rf-nothere"),                                       # 풀에 없음
        ("자기 자신을 가리킨다", "mem-pack"),                                       # 자기 자신
        ("일반 대상", "해당 도메인 전문가"),                                        # 일반 대상
        ("주석이 없다", None),                                                     # 대상 없음
        ("메모리 패키지의 동적 휨을 언제 재나요", "pcb-warp"),                         # 남는다
    ])
    ex, _st = se.load_experts(tmp_path)
    rows, names, why = se.a_candidates(ex, set(ex))
    assert dict(why) == {"near_sample": 1, "name_in_query": 2, "not_in_pool": 1, "self": 1, "generic_target": 1, "no_target": 1}
    assert [r["q"] for r in rows] == ["메모리 패키지의 동적 휨을 언제 재나요"]
    r = rows[0]
    assert (r["answer"], r["confuser"], r["same_domain"], r["domain"], r["branch"]) == ("pcb-warp", "mem-pack", False, "pcb", "hw")
    assert len(names) == 2 and all(n["name_bundle"] and n["split"] == "name" for n in names), "이름 묶음은 따로 낸다"


def _pool_tree(tmp_path, n_answers=8, per=3, domains=("rel", "sw", "xd", "std")):
    """도메인마다 정답 전문가 n_answers 명 — 각자 글쓴이 per 명에게서 반례를 받는다."""
    for d in domains:
        for i in range(n_answers):
            _expert(tmp_path, f"{d}-a{i}")
        for j in range(per):
            anti = [(f"{d} 도메인 질의 {i} 번 글쓴이 {j} 의 뜻이 다른 문장 {i * 7 + j}", f"{d}-a{i}") for i in range(n_answers)]
            _expert(tmp_path, f"{d}-w{j}", anti=anti)
    ex, _st = se.load_experts(tmp_path)
    return ex


def test_몫과_상한_정답_단위_가름과_봉인(tmp_path):
    ex = _pool_tree(tmp_path)
    rows, _n, _w = se.a_candidates(ex, set(ex))
    picked, q = se.stratify(rows, 7, total=80)      # 도메인마다 정답 8명 × 2건 = 16 이 끝이다 — 64 건
    per_answer = {}
    for r in picked:
        per_answer[r["answer"]] = per_answer.get(r["answer"], 0) + 1
    assert max(per_answer.values()) <= se.PER_ANSWER, "한 정답 전문가 2건까지"
    assert len({(r["answer"], r["confuser"]) for r in picked}) == len(picked), "한 (정답, 글쓴이) 쌍 1건까지"
    assert q["by_domain"] == {"rel": 16, "std": 16, "sw": 16, "xd": 16}, "몫(rel 30 · sw 120 · xd 60)보다 상한이 먼저 걸리고 std 는 있는 만큼"
    assert len(picked) == 64 and q["fill"] == {}, "더 채울 것이 없다(전부 상한)"
    a = se.split_by_answer(picked, 7)
    sides = {}
    for r in a:
        sides.setdefault(r["answer"], set()).add(r["split"])
    assert all(len(s) == 1 for s in sides.values()), "한 정답 전문가의 질의는 한쪽에만"
    n_dev = sum(1 for r in a if r["split"] == "dev")
    assert n_dev == 32, "반반"
    import hashlib
    want = hashlib.sha256("\n".join(sorted(r["qid"] for r in a if r["split"] == "test")).encode()).hexdigest()
    assert se.seal(a) == want


def test_남는_몫은_HW_로_채우고_HW_가_다하면_xd_다음_sw(tmp_path):
    for i in range(20):
        _expert(tmp_path, f"rel-a{i}")
    for j in range(2):
        _expert(tmp_path, f"rel-w{j}", anti=[(f"신뢰성 질의 {i} 글쓴이 {j} 번 문장", f"rel-a{i}") for i in range(20)])
    for i in range(12):
        _expert(tmp_path, f"sw-a{i}")
    _expert(tmp_path, "sw-w0", anti=[(f"소프트웨어 질의 {i} 번", f"sw-a{i}") for i in range(12)])
    ex, _st = se.load_experts(tmp_path)
    rows, _n, _w = se.a_candidates(ex, set(ex))
    picked, q = se.stratify(rows, 3, total=46)        # rel 몫 30 · sw 몫 120(12 가 끝) → 42, 남는 4 는 rel 로
    assert q["by_domain"] == {"rel": 34, "sw": 12} and q["fill"] == {"rel": 4}, q
    picked, q = se.stratify(rows, 3, total=60)
    assert q["by_domain"] == {"rel": 40, "sw": 12} and q["fill"] == {"rel": 10}, "HW 도 sw 도 상한에 다 걸리면 거기서 멈춘다"


def test_같은_씨앗이면_같은_qid_다른_씨앗이면_다르다(tmp_path):
    ex = _pool_tree(tmp_path, n_answers=10, per=4)
    rows, _n, _w = se.a_candidates(ex, set(ex))

    def ids(seed):
        a = se.split_by_answer(se.stratify(rows, seed, total=30)[0], seed)
        return [r["qid"] for r in a], se.seal(a)
    assert ids(11) == ids(11)
    assert ids(11) != ids(12)


def test_빌드는_리포_밖에만_쓰고_형제_리포가_없으면_건너뛴다(tmp_path):
    assert "skipped" in se.build(tmp_path / "nothere", tmp_path / "out")
    _pool_tree(tmp_path / "ea", n_answers=4, per=2, domains=("rel", "sw"))
    with pytest.raises(SystemExit):
        se.build(tmp_path / "ea", ROOT / "tests" / "_should_not_write")
    assert not (ROOT / "tests" / "_should_not_write").exists()
    cq = tmp_path / "queries.jsonl"
    cq.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [
        {"qid": "R1#k", "query": "카드 질의", "agent_type": "rel-a0", "title_copy": "False", "style": "keyword"},
        {"qid": "R2#k", "query": "베낀 질의", "agent_type": "rel-a1", "title_copy": "True"}]), encoding="utf-8")
    man = se.build(tmp_path / "ea", tmp_path / "out", seed=5, c_queries=cq)
    assert man["counts"]["C"] == 1 and man["filters"]["C"] == {"title_copy": 1}
    ok, line = se.check_seal(tmp_path / "out")
    assert ok and line.startswith("A-test sha256=")
    assert (tmp_path / "out" / "SEAL.txt").read_text(encoding="utf-8").startswith("A-test sha256=")
    assert oct((tmp_path / "out" / "A.jsonl").stat().st_mode & 0o777) == "0o600", "질의 글이 든 파일은 0600"
    assert se.main(["build", "--experts", str(tmp_path / "nothere"), "--out", str(tmp_path / "o2")]) == 0


def test_HW_열둘은_조직도_라벨_정본과_같다():
    org = ROOT.parent / "HWAXPortal" / "frontend" / "src" / "components" / "chat" / "orgTaxonomy.json"
    if not org.is_file():
        pytest.skip("형제 포털 리포가 없다")
    cats = json.loads(org.read_text(encoding="utf-8"))["categories"]
    hw = next(c for c in cats if c["id"] == "hw")
    assert tuple(hw["domains"]) == se.HW


def test_실제_ExpertAgents_로_짓는다(tmp_path):
    ea = ROOT.parent / "ExpertAgents"
    if not (ea / "knowledge").is_dir():
        pytest.skip("형제 리포 ExpertAgents 가 없다(dev 에만 있다)")
    man = se.build(ea, tmp_path / "sets")
    c = man["counts"]
    assert c["A"] == 600 and c["A_dev"] == 300 and c["A_test"] == 300, c
    assert c["P"] == 300 and c["rels"] > 1000
    rows = [json.loads(x) for x in (tmp_path / "sets" / "A.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(r["answer"] != r["confuser"] for r in rows) and len({r["qid"] for r in rows}) == 600
    assert man == {**man, "seal": man["seal"]} and len(man["seal"]["A-test"]) == 64

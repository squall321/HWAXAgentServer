# 계측 하네스의 판독 정확성 회귀 테스트 — 지표가 틀리면 A/B 판정이 통째로 무효가 된다.
#
# 픽스처는 S26U 심의 저널(wf_918da226-999 / wf_b4c610dd-b12)에서 실제로 관측된 형태다.
# 두 건 다 이 하네스 초판이 잘못 읽었던 케이스라 그대로 회귀 케이스가 됐다.
#
#   실행:  .venv/bin/python -m pytest tests/test_delib_metrics.py -q
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.delib_metrics import (  # noqa: E402
    _domain, _norm_key, _speech_text, _stem, load_mcp_journal, metrics_layer_b,
)


# ── 페르소나 키 정규화 ────────────────────────────────────────────────────────
def test_역할설명이_붙은_persona_를_정본키로_되돌린다():
    """실측: 저널은 워크플로 withKey() 적용 전 원문이라 모델이 붙인 설명이 그대로 남는다.
    정규화 없이 세면 6인이 11인으로 잡혀 착석 인원·결손율이 전부 틀어진다."""
    raw = ("disp-burnin — OLED 영구 잔상(번인) 복합 메커니즘 전문가. 발광재료 열화/TFT Vth "
           "시프트 기여 분리, IEC 62341-5-3 기반 이미지 스티킹 정량 측정 담당.")
    assert _norm_key(raw) == "disp-burnin"


def test_정상_키는_그대로_둔다():
    assert _norm_key("sw-color-management") == "sw-color-management"


def test_대문자_공백_은_정규화된다():
    assert _norm_key("  Disp-UPC  ") == "disp-upc"


def test_빈값도_처리한다():
    assert _norm_key(None) == ""


# ── 도메인 추출 ──────────────────────────────────────────────────────────────
def test_도메인은_첫_하이픈_앞이다():
    assert _domain("disp-color-calibration") == "disp"
    assert _domain("sw-brightness-control") == "sw"


def test_하이픈_없는_키는_전체가_도메인이다():
    assert _domain("thermal") == "thermal"


def test_역할설명이_붙어도_도메인을_뽑는다():
    assert _domain("disp-upc — 카메라 위를 덮는 UPC 영역 전문가") == "disp"


# ── 발언 직렬화 ──────────────────────────────────────────────────────────────
def test_고정_필드목록이_아니라_전_필드를_읽는다():
    """실측: 초판이 reads 를 필드 목록에 넣지 않아 근거 표기를 놓쳤다(경험칙 12건 → 0건).
    라운드·경로별로 스키마가 달라 목록을 고정하면 조용히 누락된다."""
    op = {"persona": "disp-burnin", "lens": "관점", "reads": ["(경험칙) 가속지수 n≈1.5~2"],
          "recommendation": "권장안", "concerns": ["우려"]}
    t = _speech_text(op)
    assert "(경험칙)" in t and "관점" in t and "권장안" in t and "우려" in t
    assert "disp-burnin" not in t, "persona 키는 본문이 아니라 화자다 — 토큰 통계를 오염시킨다"


def test_중첩_구조도_평탄화한다():
    op = {"rebut": [{"target": "disp-upc", "claim": "위치 통계가 먼저다"}]}
    assert "disp-upc" in _speech_text(op) and "위치 통계" in _speech_text(op)


# ── 조사·어미 절단 ───────────────────────────────────────────────────────────
def test_조사를_떼어_같은_개념으로_묶는다():
    """실측: 이것이 없어 '판정을' vs '판정은' 이 불일치로 잡혔고 보존율이 0.43 으로 떨어졌다."""
    assert _stem("판정을") == _stem("판정은") == "판정"
    assert _stem("이력을") == _stem("이력") == "이력"


def test_어미도_떼어낸다():
    assert _stem("금지한다") == _stem("금지하며") == "금지"


def test_절단해서_1글자가_되면_원형을_유지한다():
    assert _stem("모의") == "모의", "'의' 를 떼면 1글자라 개념이 사라진다"


def test_영문_토큰은_절단하지_않는다():
    assert _stem("colorimeter") == "colorimeter"


# ── 계층 B 지표 ──────────────────────────────────────────────────────────────
def _doc(keys, decision="", nn=None):
    rounds = [[{"persona": k, "lens": f"{k} 발언"} for k in keys]]
    if nn:
        rounds.append([{"persona": keys[0], "final_position": "최종", "non_negotiable": nn}])
    return {"personas": list(keys), "rounds": rounds, "decision": decision,
            "n_ops": sum(len(r) for r in rounds), "source": "test"}


def test_도메인_다양성이_좌석_고정을_드러낸다():
    """S26U 실측 — 6인이 앉았지만 도메인은 disp·sw 둘뿐이었다."""
    m = metrics_layer_b(_doc(["disp-burnin", "disp-upc", "disp-color-calibration",
                              "sw-color-management", "sw-brightness-control",
                              "disp-dimming-flicker"]))
    assert m["착석_인원"] == 6
    assert m["착석_도메인_수"] == 2
    assert m["도메인_다양성"] == round(2 / 6, 3)


def test_반대도메인_좌석이_들어오면_다양성이_오른다():
    """같은 4인이라도 disp 편중이면 도메인 2종, 반대 도메인이 섞이면 3종이 된다."""
    narrow = metrics_layer_b(_doc(["disp-burnin", "disp-upc", "disp-dimming-flicker", "sw-color-management"]))
    wide = metrics_layer_b(_doc(["disp-burnin", "disp-upc", "mat-adhesive", "svc-field-ops"]))
    assert narrow["착석_도메인_수"] == 2
    assert wide["착석_도메인_수"] == 3
    assert wide["도메인_다양성"] > narrow["도메인_다양성"]


def test_이어하기_좌석_변동률_0_을_잡는다():
    """S26U 이어하기 실측 — 신규 0명, 유임 6명이라 변동률 0.0."""
    prev = _doc(["disp-burnin", "disp-upc"])
    cur = _doc(["disp-burnin", "disp-upc"])
    m = metrics_layer_b(cur, prev)
    assert m["좌석_변동률"] == 0.0 and m["좌석_신규"] == 0 and m["좌석_유임"] == 2


def test_신규_좌석이_변동률에_반영된다():
    prev = _doc(["disp-burnin", "disp-upc"])
    cur = _doc(["disp-burnin", "disp-upc", "svc-field-ops"])
    m = metrics_layer_b(cur, prev)
    assert m["좌석_신규"] == 1 and m["좌석_신규_목록"] == ["svc-field-ops"]


def test_non_negotiable_보존율은_문구_재작성을_허용한다():
    """조항이 그대로 복사되지 않고 다시 쓰여도 승계로 본다 — 핵심어 절반 기준."""
    prev = _doc(["disp-burnin"], nn="보상 이력 리셋 전 패널 교체 판정을 금지한다")
    kept = _doc(["disp-burnin"], decision="리셋 전 교체 판정은 금지하며 보상 이력을 먼저 되돌린다")
    assert metrics_layer_b(kept, prev)["non_negotiable_보존율"] == 1.0


def test_승계되지_않은_조항은_보존율에서_빠진다():
    prev = _doc(["disp-burnin"], nn="플리커 co-spec 을 갱신마다 회귀 검증한다")
    lost = _doc(["disp-burnin"], decision="색도 문턱만 정리하고 마친다")
    assert metrics_layer_b(lost, prev)["non_negotiable_보존율"] == 0.0


# ── 웹 경로 파서 ─────────────────────────────────────────────────────────────
def test_save_conversation_형식을_읽는다(tmp_path):
    """웹·MCP 산출물을 같은 자로 재려면 대화 저장 형식도 판독해야 한다.
    픽스처는 포털에 실제로 저장한 S26U 대화(b2ecb437)의 메시지 구조다."""
    from tools.delib_metrics import load_conversation
    f = tmp_path / "conv.json"
    f.write_text(json.dumps({"messages": [
        {"role": "user", "content": "심의 주제"},
        {"role": "persona", "persona": "disp-burnin", "round": 1, "content": "초기 입장"},
        {"role": "persona", "persona": "sw-color-management", "round": 1, "content": "초기 입장"},
        {"role": "persona", "persona": "disp-burnin", "round": 2, "content": "심화"},
        {"role": "persona", "persona": "sw-color-management", "round": 2, "content": "심화"},
        {"role": "assistant", "content": "## 의사결정문\n\n본문"},
    ]}, ensure_ascii=False), encoding="utf-8")
    doc = load_conversation(f)
    assert doc["personas"] == ["disp-burnin", "sw-color-management"]
    assert [len(r) for r in doc["rounds"]] == [2, 2]
    assert doc["decision"].startswith("## 의사결정문")
    assert metrics_layer_b(doc)["착석_도메인_수"] == 2


def test_대화_형식의_역할설명도_정규화된다(tmp_path):
    from tools.delib_metrics import load_conversation
    f = tmp_path / "conv.json"
    f.write_text(json.dumps({"messages": [
        {"role": "persona", "persona": "disp-upc — UPC 영역 전문가", "round": 1, "content": "발언"},
    ]}, ensure_ascii=False), encoding="utf-8")
    assert load_conversation(f)["personas"] == ["disp-upc"]


# ── 저널 파서 ────────────────────────────────────────────────────────────────
def test_저널에서_라운드를_스키마로_가른다(tmp_path):
    """저널에 라운드 경계가 없으므로 필드로 판정한다 — lens=초기, final_position=수렴, 나머지=심화."""
    j = tmp_path / "journal.jsonl"
    rows = [
        {"type": "started"},
        {"type": "result", "result": {"persona": "a-x", "lens": "관점"}},
        {"type": "result", "result": {"persona": "b-y", "lens": "관점"}},
        {"type": "result", "result": {"persona": "a-x", "concede": ["수용"], "deepen": "심화"}},
        {"type": "result", "result": {"persona": "b-y", "concede": ["수용"], "deepen": "심화"}},
        {"type": "result", "result": {"persona": "a-x", "final_position": "최종", "vote": "찬성"}},
        {"type": "result", "result": {"persona": "b-y", "final_position": "최종", "vote": "찬성"}},
        {"type": "result", "result": "## 의사결정문\n\n본문"},
    ]
    j.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    doc = load_mcp_journal(j)
    assert doc["personas"] == ["a-x", "b-y"]
    assert [len(r) for r in doc["rounds"]] == [2, 2, 2]
    assert doc["decision"].startswith("## 의사결정문")


def test_깨진_줄은_건너뛴다(tmp_path):
    j = tmp_path / "journal.jsonl"
    j.write_text('{"type":"result","result":{"persona":"a-x","lens":"ok"}}\n{깨진 줄\n\n',
                 encoding="utf-8")
    assert load_mcp_journal(j)["personas"] == ["a-x"]


if __name__ == "__main__":  # pytest 없이도 돌릴 수 있게
    import tempfile
    fails = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        try:
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            fails += 1
            print(f"  ✗ {name}: {exc}")
    print(f"\n{'실패 ' + str(fails) + '건' if fails else '전부 통과'}")
    sys.exit(1 if fails else 0)

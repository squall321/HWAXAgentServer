# 챗 이력에 앞 턴의 조회 근거 — 어시스턴트 항목의 evidence 를 그 턴의 물음 뒤에 '시스템 첨부' 로 싣고, 몫이 모자라면 오래된 근거부터 줄이는지
"""왜 — 이력이 글만이라 다음 턴이 '아까 조회한 그 값' 을 모르고 다시 조회하거나 지어냈다(HWAXPortal docs/chat-depth-context CD-D5).

  · 근거가 없는 이력은 종전과 같은 결과(1차 채우기 그대로).
  · 근거는 그 턴의 물음 뒤에 — 답(어시스턴트 글)은 바꾸지 않는다.
  · 몫(HIST_EVIDENCE_SHARE)이 모자라면 최신 턴의 근거부터 싣고, 오래된 턴은 앞부분만 → 도구 이름만.
  · 압축본으로 밀려난 턴은 조회한 도구 이름을 남긴다.

  실행:  .venv/bin/python -m pytest tests/test_history_evidence.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app  # noqa: E402
import deliberation as d  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_ctx():
    saved = dict(app._ctx_cache)
    yield
    app._ctx_cache.clear()
    app._ctx_cache.update(saved)
    d._evid_cache.clear()


def _win(tokens):
    app._ctx_cache["n"] = tokens
    d._evid_cache.clear()


def _turns(n, ev_len=200):
    """n 턴 — 턴마다 물음 · 답, 답에는 그 턴의 조회 근거 둘."""
    h = []
    for i in range(n):
        h.append({"role": "user", "content": f"질문 {i} — 부품 {i} 의 두께는?"})
        h.append({"role": "assistant", "content": f"부품 {i} 의 두께는 {i}.5mm 입니다.",
                  "evidence": [{"tool": f"stepforge_part_{i}", "args": f'{{"part": "P{i}"}}',
                                "result": f"RESULT-{i}-" + "x" * ev_len},
                               {"tool": "materialtwin_lookup", "result": f"MAT-{i}"}]})
    return h


def test_근거가_없는_이력은_종전과_같다():
    _win(1048576)
    plain = [{"role": m["role"], "content": m["content"]} for m in _turns(5)]
    assert app._history_messages(plain) == app._history_messages([{**m, "evidence": []} for m in plain])
    assert all("시스템 첨부" not in c for _, c in app._history_messages(plain))


def test_근거는_그_턴의_물음_뒤에_붙고_답은_그대로다():
    _win(1048576)
    st: dict = {}
    out = app._history_messages(_turns(3), st)
    assert [r for r, _ in out] == ["user", "assistant"] * 3, "역할 차례는 그대로(물음 · 답)"
    q1, a1 = out[2][1], out[3][1]
    assert q1.startswith("질문 1 — 부품 1 의 두께는?") and "[시스템 첨부" in q1
    assert '· stepforge_part_1({"part": "P1"}) → RESULT-1-' in q1 and "· materialtwin_lookup → MAT-1" in q1
    assert a1 == "부품 1 의 두께는 1.5mm 입니다.", "답(어시스턴트 글)에는 섞지 않는다 — 모델이 그 꼴을 흉내 낸다"
    assert st["evidence"] == 3 and not st.get("evidence_cut") and not st.get("evidence_names")


def test_모양이_틀린_근거는_건너뛴다():
    _win(1048576)
    h = [{"role": "user", "content": "q"},
         {"role": "assistant", "content": "a", "evidence": ["문자열", {"tool": "", "result": "r"}, {"tool": "t", "result": ""},
                                                            {"tool": "ok_tool", "result": "OK"}, None]},
         {"role": "user", "content": "q2"},
         {"role": "assistant", "content": "a2", "evidence": "목록이 아님"}]
    out = app._history_messages(h)
    assert "· ok_tool → OK" in out[0][1] and out[0][1].count("· ") == 1
    assert out[2][1] == "q2", "근거가 목록이 아니면 붙이지 않는다"


def test_몫이_모자라면_오래된_턴의_근거부터_줄이고_도구_이름만_남긴다(monkeypatch):
    # 근거 몫을 작게 — 최신 턴의 근거(약 2.5천 자)만 온전히 들 크기
    _win(1048576)
    monkeypatch.setattr(app, "HIST_EVIDENCE_SHARE", 3500 / app._hist_budget_chars("가나다라"))
    st: dict = {}
    out = app._history_messages(_turns(4, ev_len=2400), st)
    qs = [c for r, c in out if r == "user"]
    assert "RESULT-3-" in qs[-1], "최신 턴의 근거는 싣는다"
    assert "을(를) 조회했다. 결과는 이력 예산 밖이라" in qs[0] and "RESULT-0-" not in qs[0], "가장 오래된 턴은 도구 이름만"
    assert "stepforge_part_0" in qs[0]
    assert st["evidence"] >= 1 and st["evidence_names"] >= 1


def test_물음이_압축본으로_밀려나면_근거를_그_답_앞에_따로_넣고_압축본엔_도구_이름을_남긴다(monkeypatch):
    _win(1048576)
    # 이력 몫을 바닥(1,200토큰)으로 — 마지막 답(짧다)만 원문, 그 물음(6,000자)부터는 압축본
    monkeypatch.setattr(app, "HIST_CTX_SHARE_SOLO", 0.0)
    h = [{"role": "user", "content": "질문 0"},
         {"role": "assistant", "content": "답 0", "evidence": [{"tool": "stepforge_part_0", "result": "R0"},
                                                               {"tool": "materialtwin_lookup", "result": "M0"}]},
         {"role": "user", "content": "질" * 6000},
         {"role": "assistant", "content": "답 1", "evidence": [{"tool": "stepforge_part_1", "result": "R1"}]}]
    out = app._history_messages(h)
    assert out[0][0] == "user" and out[0][1].startswith("[이전 대화"), out[0][1][:80]
    assert "(조회: stepforge_part_0, materialtwin_lookup)" in out[0][1], "압축본에 그 턴이 조회한 도구 이름"
    assert out[-1] == ("assistant", "답 1") and out[-2][0] == "user"
    assert out[-2][1].startswith("[시스템 첨부") and "· stepforge_part_1 → R1" in out[-2][1], \
        "물음이 원문 몫 밖이면 근거를 그 답 바로 앞에 따로 넣는다"

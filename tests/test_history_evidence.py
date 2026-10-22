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


# ── 업로드 근거(kind=upload) — 사람이 챗 화면에서 파일을 올려 보낸 결과(HWAXPortal docs/chat-ppt PP-D3) ─────────────────────────────
# 10-22 cae00 — PPT 를 AI 데이터 허브로 보냈는데 대화에 남지 않아 '방금 변환한 것 검토해 줘' 에 앞서 올린 그림을 검토했다. 포털이 이제
# 보내기 결과를 어시스턴트 알림 + 업로드 근거로 남긴다. 그 근거에 '어시스턴트가 이 물음에 답하려고 조회한 결과' 머리를 달면 앞 물음의
# 답 근거로 읽힌다 — 따로 된 머리로, 알림 바로 앞에 따로 둔다.

def _upload_notice(name="koo.park-설계검토.pptx", draft="DRAFT-" + "y" * 300):
    return {"role": "assistant", "content": f"PPT 를 AI 데이터 허브로 변환했습니다(inbox {name}).",
            "evidence": [{"tool": "convert_file", "args": f'{{"file": "{name}"}}', "result": draft, "kind": "upload"}]}


def test_업로드_근거는_따로_된_머리로_알림_바로_앞에_따로_선다():
    _win(1048576)
    st: dict = {}
    h = [*_turns(1), _upload_notice()]
    out = app._history_messages(h, st)
    assert [r for r, _ in out] == ["user", "assistant", "user", "assistant"], "역할이 번갈아 간다(알림 앞에 사람 쪽 메시지 하나)"
    q0, up, notice = out[0][1], out[2][1], out[3][1]
    assert "stepforge_part_0" in q0 and "convert_file" not in q0, "업로드 근거는 앞 물음에 붙지 않는다"
    assert up.startswith("[시스템 첨부 — 사람이 챗 화면에서 파일을 올려 보낸 결과다"), up[:80]
    assert "어시스턴트가 이 물음에 답하려고" not in up, "조회 근거의 머리를 달지 않는다"
    assert '· convert_file({"file": "koo.park-설계검토.pptx"}) → DRAFT-' in up
    assert notice.startswith("PPT 를 AI 데이터 허브로 변환했습니다"), "알림 글은 그대로"
    assert st["evidence"] == 2, "조회 근거 한 턴 + 업로드 한 건"


def test_대화의_첫_메시지가_업로드_알림이어도_사람_쪽이_먼저_선다():
    _win(1048576)
    out = app._history_messages([_upload_notice()])
    assert [r for r, _ in out] == ["user", "assistant"]
    assert out[0][1].startswith("[시스템 첨부 — 사람이 챗 화면에서 파일을 올려 보낸 결과다")


def test_답이_없던_물음_뒤의_업로드는_그_물음에_잇되_머리로_갈린다():
    # 답이 실패한 턴(화면이 이력에서 뺀다) 뒤에 알림이 오면 사람 메시지가 둘 잇달아 가지 않게 한 메시지에 잇는다
    _win(1048576)
    out = app._history_messages([{"role": "user", "content": "이거 봐 줘"}, _upload_notice()])
    assert [r for r, _ in out] == ["user", "assistant"]
    assert out[0][1].startswith("이거 봐 줘\n\n[시스템 첨부 — 사람이 챗 화면에서 파일을 올려 보낸 결과다")


def test_몫이_모자라면_업로드도_도구_이름만_남기고_다시_부를_길을_적는다(monkeypatch):
    _win(1048576)
    monkeypatch.setattr(app, "HIST_EVIDENCE_SHARE", 3500 / app._hist_budget_chars("가나다라"))
    st: dict = {}
    h = [_upload_notice(draft="D" * 2400), *_turns(2, ev_len=2400)]
    out = app._history_messages(h, st)
    assert out[0][0] == "user" and "사람이 챗 화면에서 파일을 올려 보냈다(convert_file)" in out[0][1], out[0][1][:200]
    assert "알림에 적힌 파일 이름 · 보고서 번호로 다시 조회하라" in out[0][1] and "DDDD" not in out[0][1]
    assert st["evidence_names"] >= 1


def test_압축본에는_올린_도구와_조회한_도구를_따로_적는다(monkeypatch):
    _win(1048576)
    monkeypatch.setattr(app, "HIST_CTX_SHARE_SOLO", 0.0)
    h = [_upload_notice(), {"role": "user", "content": "질" * 6000},
         {"role": "assistant", "content": "답 1", "evidence": [{"tool": "stepforge_part_1", "result": "R1"}]}]
    out = app._history_messages(h)
    assert out[0][1].startswith("[이전 대화") and "(올림: convert_file) PPT 를 AI 데이터 허브로" in out[0][1], out[0][1][:300]


def test_kind_가_upload_가_아니면_종전_조회_근거다():
    _win(1048576)
    h = [{"role": "user", "content": "q"},
         {"role": "assistant", "content": "a", "evidence": [{"tool": "t", "result": "R", "kind": "something"}]}]
    out = app._history_messages(h)
    assert "어시스턴트가 이 물음에 답하려고" in out[0][1] and "올려 보낸" not in out[0][1]


def test_한_답에_업로드와_조회가_함께면_한_사람_메시지에_업로드_머리가_먼저다():
    # 포털은 섞어 보내지 않지만(알림마다 업로드만) 섞여 와도 역할이 번갈아 가고 두 머리가 갈린다
    _win(1048576)
    h = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
         {"role": "assistant", "content": "섞인 답", "evidence": [{"tool": "lookup", "result": "L"},
                                                               {"tool": "convert_file", "result": "C", "kind": "upload"}]}]
    out = app._history_messages(h)
    assert [r for r, _ in out] == ["user", "assistant", "user", "assistant"], [r for r, _ in out]
    blk = out[2][1]
    assert blk.index("사람이 챗 화면에서 파일을 올려 보낸 결과") < blk.index("어시스턴트가 이 물음에 답하려고"), blk[:200]


def test_시스템_프롬프트가_방금_올린_것은_업로드_근거부터_보라고_한다():
    assert "이력의 업로드 근거('사람이 챗 화면에서 파일을 올려 보낸 결과')를 먼저" in app.SYSTEM_PROMPT
    assert "앞 대화의 그림으로 대신 답하지 말고 어느 파일인지(이름) 물으세요" in app.SYSTEM_PROMPT

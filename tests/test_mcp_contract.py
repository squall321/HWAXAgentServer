# 심의 MCP 공통 계약(HWAXPortal docs/mcp-modernize contract C1 · C2 · C5 · C6 · C8) — 실제 list_tools 로 표지 · 인자 설명 · 설명 길이를 단언한다
"""게이트웨이는 앱의 readOnlyHint 를 읽기 판정(루틴 단계 · 읽기 전용 PAT · 쓰기 분류)에 쓴다. 표지는 도구마다 본체가 실제로 쓰는 것을 코드로 보고
정했다(mcp_server._ann 머리말) — 여기서 그 판정을 고정하고, 스위치 뒤 도구(외부 응답자 셋 · 위임 · 작성 · 변환)까지 켠 서버에 올려 본다."""
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

import delib_jobs  # noqa: E402
import deliberation as d  # noqa: E402
import mcp_server as m  # noqa: E402

HE_TEAM = ROOT.parent / "HWAXPortal" / "infra" / "personas" / "he-team.json"

READ = {"deliberate_status", "deliberate_result", "deliberate_list", "deliberate_transcript", "deliberate_verify",
        "deliberate_resolve_evidence", "deliberate_jobs", "delegate_status", "delegate_result", "draft_section", "llm_transform"}
DESTRUCTIVE = {"deliberate_cancel", "deliberate_batch_stop", "delegate_cancel"}
NOT_IDEMPOTENT = {"deliberate_start", "deliberate_continue", "deliberate_batch_start", "delegate_start", "review_task_next",
                  "review_task_submit"}
OPEN_WORLD = {"deliberate_start", "deliberate_continue", "deliberate_batch_start", "delegate_start", "deliberate_resolve_evidence",
              "draft_section", "llm_transform", "deliberate_save_report"}
DESC_MIN, DESC_MAX = 150, 1500          # contract C1 — 웹 챗 바인딩 상한 TOOL_DESC_MAX 2,400 · 게이트웨이 4,000 보다 넉넉히 아래
ARG_DESC_MAX = 2400                     # 웹 챗이 인자 설명도 같은 상한으로 자른다(app._slim_tool)


def _all_tools() -> list:
    """기본 도구 13 + 켠 서버의 스위치 뒤 도구."""
    tools = list(asyncio.run(m.mcp.list_tools()))
    on = FastMCP("switched")
    m._register_external(on, env={"EXTERNAL_LLM_ENABLED": "1"})
    m._register_offload(on, {"delegate": True, "draft": True, "transform": True})
    return tools + list(asyncio.run(on.list_tools()))


def test_모든_도구에_네_표지가_참거짓으로_있다():
    tools = _all_tools()
    assert len(tools) == 23, [t.name for t in tools]
    for t in tools:
        a = t.annotations
        assert a is not None, t.name
        hints = (a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint)
        assert all(isinstance(h, bool) for h in hints), (t.name, hints)


def test_표지는_코드로_확인한_판정_그대로다():
    ann = {t.name: t.annotations for t in _all_tools()}
    assert {n for n, a in ann.items() if a.readOnlyHint} == READ
    assert {n for n, a in ann.items() if a.destructiveHint} == DESTRUCTIVE
    assert {n for n, a in ann.items() if not a.idempotentHint} == NOT_IDEMPOTENT
    assert {n for n, a in ann.items() if a.openWorldHint} == OPEN_WORLD


def test_이름과_달리_쓰기인_둘은_코드가_쓴다():
    """묶음 진행은 주인이 부르면 그룹을 묶음 원장에 고쳐 적고, 과제 읽기는 일꾼 표지 · 기한 지난 과제 되돌리기를 줄에 적는다 — 읽기 표지를 달면
    읽기 전용 PAT · 루틴 단계가 그것을 부른다."""
    import inspect

    import delib_batch
    import external_llm
    assert "delib_batch.touch" in inspect.getsource(m.batch_read) and "_persist(b)" in inspect.getsource(delib_batch.touch)
    held = inspect.getsource(external_llm.Board._held)
    assert "self._touch(" in held and "self._sweep(" in held
    ann = {t.name: t.annotations for t in _all_tools()}
    assert ann["deliberate_batch_status"].readOnlyHint is False and ann["review_task_read"].readOnlyHint is False


def test_HE팀_심의_운영자_목록과_어긋나지_않는다():
    if not HE_TEAM.exists():
        pytest.skip(f"형제 리포 없음: {HE_TEAM}")
    spec = json.loads(HE_TEAM.read_text(encoding="utf-8"))
    (p,) = [x for x in spec["personas"] if x["key"] == "he-expert-deliberation"]
    ann = {t.name: t.annotations for t in _all_tools()}
    gated = {*p["writes"], *p["confirm"]}
    assert gated and not [n for n in gated if ann[n].readOnlyHint], "운영자 목록의 쓰기를 읽기로 표시했다"
    assert {n for n in p["confirm"] if ann[n].destructiveHint} == set(p["confirm"]), "확인 카드의 접기 · 멈춤은 파괴 표지다"


def test_설명_길이는_계약_범위_안이다():
    history = re.compile(r"(20\d\d-\d\d-\d\d|\b\d{1,2}-\d{2} (?:결정|사용자)|\b[A-Z]{1,4}-D?\d{1,3}\b|실측)")    # 게이트웨이 품질 카드의 셈
    for t in _all_tools():
        assert DESC_MIN <= len(t.description) <= DESC_MAX, (t.name, len(t.description))
        plain = t.description.replace("UTF-16", "")        # 글자 단위 이름 — 품질 카드의 이력 셈이 번호로 잘못 센다
        assert not history.search(plain), (t.name, history.search(plain).group(0))
        for arg, prop in (t.inputSchema.get("properties") or {}).items():
            desc = str(prop.get("description") or "").strip()
            assert desc, f"{t.name}.{arg} 인자 설명이 없다"
            assert len(desc) <= ARG_DESC_MAX, (t.name, arg, len(desc))


def test_시작_도구의_긴_규칙은_인자_설명과_메뉴에_있다():
    """설명을 1,500자 안으로 줄이며 뺀 규칙이 사라지지 않았다 — 근거 상한은 evidence 인자, 손잡이는 advanced 칸, 전문은 deliberate_jobs 다."""
    start = next(t for t in _all_tools() if t.name == "deliberate_start")
    props = start.inputSchema["properties"]
    ev = props["evidence"]["description"]
    for want in (f"{d._EVID_ITEMS}건", f"{d._EVID_ITEM_MAX:,}자", f"{d._EVID_BUDGET:,}자", "`result`", "[e:N|KEY]", "insight_get"):
        assert want in ev, want
    adv = props["advanced"]["properties"]
    assert {"timeout_s", "voc", "persona_knowledge", "knowledge_query", "as_of", "chair_template", "sealed"} <= set(adv)
    menu = asyncio.run(m.deliberate_jobs())
    assert menu["options"]["evidence"] == m._EVID_DESC and menu["options"]["advanced"] == m._ADV_DESC and menu["states"] == m._STATE_DESC


def test_손잡이_칸은_엔진이_읽는_이름뿐이다():
    """advanced 의 칸(contract C5)은 엔진 _resolve_opts 가 실제로 읽는 이름이다 — 읽지 않는 칸을 안내하면 보내도 버려진다."""
    src = Path(d.__file__).read_text(encoding="utf-8")
    read = set(re.findall(r'req_opts\.get\("(\w+)"\)', src)) | set(d._INT_KEYS) | {"modifiers", "consent_external"}
    for tool in ("deliberate_start", "deliberate_continue"):
        props = next(t for t in _all_tools() if t.name == tool).inputSchema["properties"]["advanced"]["properties"]
        assert set(props) <= read, sorted(set(props) - read)
        assert all(str(v.get("description") or "").strip() for v in props.values())
        assert not [k for k, v in props.items() if "enum" in v or "type" in v], "엔진은 1|0 · on|off · 글자 수를 다 받는다 — 형 · enum 으로 막지 않는다"


def test_객체_인자는_칸_꼴을_싣는다():
    tools = {t.name: t for t in _all_tools()}
    start = tools["deliberate_start"].inputSchema["properties"]
    assert set(start["evidence"]["items"]["properties"]) == {"source", "tool", "args", "result", "key", "ref"}
    assert set(start["personas"]["items"]["properties"]) == {"key", "agent_type", "role"}
    assert set(start["subject"]["properties"]) == {"sp_task", "round", "text", "artifacts"}
    assert set(tools["deliberate_list"].inputSchema["properties"]["subject"]["properties"]) == {"sp_task", "round"}
    assert set(tools["deliberate_batch_start"].inputSchema["properties"]["items"]["items"]["properties"]) == {
        "key", "question", "job", "evidence", "advanced"}
    assert tools["deliberate_resolve_evidence"].inputSchema["properties"]["evidence"]["items"]["properties"]


def test_숫자_경계는_코드가_거절하는_범위만이다():
    import delib_batch
    batch = next(t for t in _all_tools() if t.name == "deliberate_batch_start").inputSchema["properties"]
    assert (batch["concurrency"]["minimum"], batch["concurrency"]["maximum"]) == (1, delib_batch.CONCURRENCY_MAX)
    assert (batch["retries"]["minimum"], batch["retries"]["maximum"]) == (0, delib_batch.RETRIES_MAX)
    assert batch["stall_s"]["minimum"] == 0
    for t in _all_tools():
        part = (t.inputSchema.get("properties") or {}).get("part")
        if part is not None:
            assert part["minimum"] == 0, t.name          # 0 은 1 로 읽고 음수만 거절한다(mcp_pages.paged)
    # 죄는 칸(라운드 · 목록 수 · 기다림)에는 경계를 달지 않는다 — 지금 받는 값(죄어서 도는 값)을 클라이언트가 막게 된다
    for name, arg in (("deliberate_start", "rounds"), ("deliberate_list", "limit"), ("deliberate_status", "wait_s")):
        prop = next(t for t in _all_tools() if t.name == name).inputSchema["properties"][arg]
        assert "minimum" not in prop and "maximum" not in prop, (name, arg)


def test_스키마의_경계는_검사를_바꾸지_않는다():
    """part=-1 은 종전대로 mcp_pages 의 거절 글이고(FastMCP 의 칸 검증 글이 아니다) part=0 은 종전대로 1 로 읽는다."""
    with pytest.raises(Exception, match="part 는 1 이상의 정수다"):
        asyncio.run(m.mcp.call_tool("deliberate_list", {"part": -1}))
    asyncio.run(m.mcp.call_tool("deliberate_list", {"part": 0}))


def test_목록이_limit_보다_더_있으면_잘렸다고_말한다(monkeypatch):
    seen = {}

    def fake(limit, **kw):
        seen.update(kw)
        kw["info"]["more"] = True
        return [{"job_id": "a"}]

    monkeypatch.setattr(delib_jobs, "list_jobs", fake)
    out = asyncio.run(m.deliberate_list(limit=1))
    assert out["truncated"] is True and "limit" in out["truncated_note"] and "info" in seen
    monkeypatch.setattr(delib_jobs, "list_jobs", lambda limit, **kw: [])
    assert "truncated" not in asyncio.run(m.deliberate_list(limit=1)), "다 실었으면 칸이 없다(종전 답 그대로)"


def test_list_jobs_는_더_있는지_info_에_적는다(monkeypatch, tmp_path):
    monkeypatch.setattr(delib_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(delib_jobs, "_JOBS", {f"j{i}": {"id": f"j{i}", "started_at": i} for i in range(3)})
    monkeypatch.setattr(delib_jobs, "summary", lambda j: {"job_id": j["id"]})
    monkeypatch.setattr(delib_jobs, "can_read", lambda *a, **k: True)
    for limit, more in ((2, True), (3, False)):
        info: dict = {}
        rows = delib_jobs.list_jobs(limit, info=info)
        assert len(rows) == min(limit, 3) and info["more"] is more

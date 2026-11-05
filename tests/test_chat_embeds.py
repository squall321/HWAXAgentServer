# 챗 뷰어 카드(HWAXPortal docs/chat-embeds) — 도구 결과의 같은 출처 /apps/ 뷰어 주소를 모아 result 앞에 SSE embeds 한 번으로 내는지 본다
"""받아들이는 기준(포털 docs/chat-embeds PLAN 1절 · 4절 — 사용자 결정 11-05 CE-1 · CE-2).

  · 주소 — `/apps/<앱>/…` 같은 출처 경로만. 공개 주소(http(s)://…/apps/…)는 경로(+ 질의)만 쓰고 `#조각` 은 뗀다. javascript: · //다른곳 · 점 마디 ·
    따옴표 · 꺾쇠 · 공백 · 역슬래시 · /apps/ 밖 · 2,048자 넘음은 버린다(포털 safe_path 와 같은 규칙 + `..` · `//` 는 질의까지).
  · 고르기 — embed_url 은 늘, public_url · viewer_url 은 CHAT_EMBED_PATHS 에 맞을 때만(기본은 StepForge 단면 지도 — 3D 뷰어는 아니다). 결과 안
    차례 · 같은 주소 하나 · 턴에 4개 · JSON 이 아닌 결과는 없음. 제목은 같은 객체의 title · name, 없으면 '앱 · 도구'. 앱은 도구 지도의 백엔드.
  · 턴 — 챗 턴 밖(그릇 없음)이면 아무것도 안 한다. 챗 턴은 result 앞에 embeds 를 한 번 낸다 — 자동 재시도(ainvoke) 길에서도, 자기검증 감싸개를
    지나도(감싸개는 그 프레임을 바이트 그대로 흘린다). 모델에 가는 도구 결과는 그대로다.

가짜 게이트웨이는 진짜 FastMCP(mcp 1.28)다 — StepForge section_atlas(viewer=true) · view_3d 와 같은 꼴의 결과, 명시 표지(embed_url)를 단 결과,
JSON 이 아닌 결과, invoke_tool 대역을 낸다. 대본 모델 · 배선 · 검토자 대역은 tests/test_ui_host_ha20 의 것을 그대로 쓴다.
"""
import asyncio
import contextvars
import json
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import pytest  # noqa: E402
from langchain_core.messages import ToolMessage  # noqa: E402

import app as a  # noqa: E402
import embeds as em  # noqa: E402
import test_ui_host_ha20 as h20  # noqa: E402 — 같은 대본 모델 · 배선 · 검토자 대역을 다시 쓰지 않는다

PUB = "http://127.0.0.1:5283"          # dev 의 STEPFORGE_PUBLIC_BASE 꼴 — 포털 정문(8723)과 포트가 달라도 경로만 쓴다
ATLAS = "/apps/step_forge/view/demo/atlas"
ATLAS_ITEM = {"app": "heax-step_forge", "tool": "section_atlas", "url": ATLAS, "title": "heax-step_forge · section_atlas"}


@pytest.fixture(autouse=True)
def _no_env_paths(monkeypatch):
    """셸이 내보낸 허용 목록(CHAT_EMBED_PATHS)을 지운다 — 기본 꼴을 보는 시험이 흔들리지 않게. 바꾼 꼴은 그 시험이 제 손으로 건다."""
    monkeypatch.delenv("CHAT_EMBED_PATHS", raising=False)


def _in_turn(fn) -> list:
    """챗 턴 안처럼 — 새 문맥에서 그릇을 걸고 fn(bag) 를 부른다(시험 사이에 그릇이 새지 않는다)."""
    bag: list = []

    def go():
        em.TURN.set(bag)
        fn(bag)
    contextvars.copy_context().run(go)
    return bag


# ── 주소 → 같은 출처 경로 ────────────────────────────────────────────────────────────────────────────────────────

def test_경로는_그대로_공개_주소는_경로와_질의만_조각은_뗀다():
    assert em.to_path(ATLAS) == ATLAS
    assert em.to_path(f"{PUB}{ATLAS}") == ATLAS, "공개 주소는 오리진 · 포트를 떼고 경로만(정문이 같은 /apps/ 를 내준다)"
    assert em.to_path(f"https://portal.example{ATLAS}?axis=z&n=3") == ATLAS + "?axis=z&n=3", "질의는 남긴다"
    assert em.to_path(f"{ATLAS}#slice-4") == ATLAS and em.to_path(f"{PUB}{ATLAS}?a=1#x") == ATLAS + "?a=1", "#조각은 뗀다"
    assert em.to_path(f"HTTPS://H{ATLAS}") == ATLAS, "스킴은 대소문자를 가리지 않는다"
    ok = "/apps/x/" + "a" * (em.MAX_URL - len("/apps/x/"))
    assert em.to_path(ok) == ok and len(ok) == em.MAX_URL


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "data:text/html,<b>x</b>", "//evil.example/apps/x/y", "/apps/../x", "/apps/x/../../admin",
    "/apps/x/a..b", "/apps/x//y", "/apps/x/y?next=//evil", "/apps/x/./y", "/apps/x/%2e%2e/y", "/apps/x/a%2Fb", "/apps/x/a%5cb",
    '/apps/x/a"b', "/apps/x/a'b", "/apps/x/<script>", "/apps/x/a b", "/apps/x/a\tb", "/apps/x/a\nb", "/apps/x/a\\b",
    "/view/demo/atlas", "/ai-data-hub/attachments/a.png", "https://h/view/demo", "https://h", "/apps/X/y", "/apps/x", "/apps//x/y",
    "apps/x/y", "", "/apps/x/" + "a" * (em.MAX_URL - len("/apps/x/") + 1), None, 3, {"url": ATLAS}, [ATLAS],
])
def test_위험하거나_같은_출처_apps_밖이면_버린다(bad):
    assert em.to_path(bad) is None, bad


# ── 결과 하나에서 고르기 ─────────────────────────────────────────────────────────────────────────────────────────

def _atlas(pid: str) -> dict:
    """StepForge section_atlas(viewer=true) 꼴(app/mcp_server.py _section_atlas_body) — 뷰어 칸에 앱 안 경로(route)와 공개 주소."""
    return {"project_id": pid, "status": "ready", "key": "abc123", "unit": "mm",
            "viewer": {"file": "atlas.html", "bytes": 4096, "slices": 3, "route": f"/view/{pid}/atlas",
                       "public_url": f"{PUB}/apps/step_forge/view/{pid}/atlas", "file_in_data": "view/atlas/x/atlas.html"},
            "note": "장마다 상호작용 SVG"}


def _view3d(pid: str) -> dict:
    """StepForge view_3d 꼴 — 3D 뷰어는 로그인 쿠키로 API 를 불러 격리 창에서 깨진다(기본 허용 목록 밖)."""
    return {"viewer_url": f"/apps/step_forge/view/{pid}", "public_url": f"{PUB}/apps/step_forge/view/{pid}",
            "models_summary": {"count": 2, "bytes": 1000}, "note": "포탈 경유 주소"}


def _sim(case: str) -> dict:
    """명시 표지(embed_url)를 단 결과 — 플랫폼이 '혼자 도는 페이지' 라고 밝힌 것."""
    return {"title": f"시뮬 흐름 {case}", "embed_url": f"/apps/simflow/panel/{case}", "rows": [{"i": 1}]}


def test_기본_허용_목록은_StepForge_단면_지도만이고_3D_뷰어는_아니다():
    assert em.collect(json.dumps([_atlas("demo")]), "section_atlas", "heax-step_forge") == [ATLAS_ITEM]
    assert em.collect(json.dumps(_view3d("demo")), "view_3d", "heax-step_forge") == [], "3D 뷰어(/view/<과제>)는 기본에 없다"
    assert em.allowed_paths()[0].pattern == em.DEFAULT_PATHS
    assert em.collect(json.dumps({"public_url": "/apps/step_forge/view/demo/atlas/extra"}), "t") == [], "통째로 맞아야 한다"
    assert em.collect(json.dumps({"public_url": "/apps/step_forge/view/demo/atlas?x=1"}), "t") == [], "질의가 붙으면 기본 식과 안 맞는다"
    assert em.collect(json.dumps({"other_url": f"{PUB}{ATLAS}"}), "t") == [], "키는 public_url · viewer_url · embed_url 만"


def test_명시_표지는_허용_목록과_상관없이_늘_후보다(monkeypatch):
    for env in (None, "", r"^/apps/nothing/.+$"):
        if env is None:
            monkeypatch.delenv("CHAT_EMBED_PATHS", raising=False)
        else:
            monkeypatch.setenv("CHAT_EMBED_PATHS", env)
        assert em.collect(json.dumps(_sim("c1")), "open_sim", "simflow") == [
            {"app": "simflow", "tool": "open_sim", "url": "/apps/simflow/panel/c1", "title": "시뮬 흐름 c1"}], env
        assert em.collect(json.dumps({"embed_url": "https://other.example/apps/simflow/panel/c2#x"}), "t", "simflow")[0]["url"] \
            == "/apps/simflow/panel/c2", "공개 주소 꼴의 표지도 경로만"
    monkeypatch.setenv("CHAT_EMBED_PATHS", "")
    assert em.collect(json.dumps([_atlas("demo")]), "section_atlas", "heax-step_forge") == [], "비우면 허용 목록이 없다(표지만)"
    assert em.collect(json.dumps({"embed_url": "javascript:alert(1)"}), "t") == [], "표지도 규칙 밖이면 버린다"


def test_허용_목록은_CHAT_EMBED_PATHS_를_부를_때마다_읽고_깨진_식은_버린다(monkeypatch, capsys):
    sim_pub = json.dumps({"viewer_url": f"{PUB}/apps/simflow/panel/q9", "name": "패널 q9"})
    assert em.collect(sim_pub, "t", "simflow") == []
    monkeypatch.setenv("CHAT_EMBED_PATHS", r"(broken, ^/apps/simflow/panel/[^/?#]+$")
    assert em.collect(sim_pub, "t", "simflow") == [{"app": "simflow", "tool": "t", "url": "/apps/simflow/panel/q9", "title": "패널 q9"}]
    assert "정규식 오류" in capsys.readouterr().out, "깨진 식은 버리고 한 줄 남긴다(나머지 식은 쓴다)"
    assert em.collect(json.dumps([_atlas("demo")]), "section_atlas", "heax-step_forge") == [], "기본을 바꾸면 기본 식은 없다"
    monkeypatch.setenv("CHAT_EMBED_PATHS", f"{em.DEFAULT_PATHS}, ^/apps/simflow/panel/[^/?#]+$")
    assert len(em.collect(json.dumps([_atlas("demo"), {"public_url": f"{PUB}/apps/simflow/panel/q9"}]), "t")) == 2


def test_안쪽_딕셔너리_목록까지_결과_안_차례로_걷고_상한을_지킨다():
    deep = {"a": [{"b": {"c": [{"embed_url": "/apps/simflow/panel/n1"}]}}], "d": {"public_url": f"{PUB}{ATLAS}"}}
    assert [x["url"] for x in em.collect(json.dumps(deep), "t")] == ["/apps/simflow/panel/n1", ATLAS]

    def nest(depth: int) -> dict:          # 표지를 단 객체가 깊이 depth(맨 바깥 0)에 있다
        node = {"embed_url": "/apps/simflow/panel/deep"}
        for _ in range(depth):
            node = {"k": node}
        return node
    assert em.collect(json.dumps(nest(8)), "t") and em.collect(json.dumps(nest(9)), "t") == [], "깊이 8 까지"
    assert em.collect(json.dumps([0] * 100 + [{"embed_url": "/apps/simflow/panel/far"}]), "t")
    assert em.collect(json.dumps([0] * 6000 + [{"embed_url": "/apps/simflow/panel/far"}]), "t") == [], "값 5,000개까지만 본다"


def test_JSON_이_아닌_결과는_없음이고_앞의_JSON_값_뒤에_붙은_안내는_버린다():
    assert em.collect('조회 결과 — "embed_url": "/apps/simflow/panel/1"', "t") == [], "글을 파싱하지 않는다(지어낸 주소를 띄우지 않게)"
    assert em.collect('"/apps/simflow/panel/1"', "t") == [] and em.collect("", "t") == [] and em.collect(None, "t") == []
    assert em.collect('{"embed_url": "/apps/simflow/panel/1"', "t") == [], "깨진 JSON"
    # 그림 블록이 함께 온 결과 — 래퍼(_norm)가 JSON 뒤에 그림 주소 안내를 붙인다
    tail = json.dumps(_sim("c3")) + "\n[생성된 이미지 URL — 답변에 ![설명](url) 로 포함하세요]\n/agent/artifacts/ab.png"
    assert [x["url"] for x in em.collect(tail, "open_sim", "simflow")] == ["/apps/simflow/panel/c3"]
    assert em.collect([_sim("c4")], "open_sim", "simflow")[0]["url"] == "/apps/simflow/panel/c4", "이미 풀린 값도 걷는다"


def test_제목은_같은_객체의_title_name_없으면_앱_도구이고_앱은_도구_지도_아니면_경로의_앱_마디():
    u = "/apps/simflow/panel/t1"
    one = lambda obj, tool="open_sim", app="simflow": em.collect(json.dumps(obj), tool, app)[0]  # noqa: E731
    assert one({"title": "  흐름 지도  ", "name": "x", "embed_url": u})["title"] == "흐름 지도"
    assert one({"name": "이름 칸", "embed_url": u})["title"] == "이름 칸"
    assert one({"title": "   ", "name": 5, "embed_url": u})["title"] == "simflow · open_sim", "빈 글 · 글이 아닌 값은 제목이 아니다"
    assert one({"title": "가" * 300, "embed_url": u})["title"] == "가" * 120
    assert one({"embed_url": u}, app="")["app"] == "simflow" and one({"embed_url": u}, app="_gateway")["app"] == "simflow", \
        "도구 지도에 없거나 포털 앱 이름 꼴이 아니면 경로의 앱 마디"
    assert one({"embed_url": u}, tool="", app="")["title"] == "simflow"
    assert one({"embed_url": u}, app="heax-sim_flow")["app"] == "heax-sim_flow"


def test_같은_주소는_먼저_온_것_하나이고_결과_하나에서도_네_개까지():
    same = {"viewer_url": ATLAS, "public_url": f"{PUB}{ATLAS}"}
    assert len(em.collect(json.dumps(same), "t")) == 1, "같은 경로 둘(앱 안 · 공개 주소)은 하나"
    first = em.collect(json.dumps([{"title": "A", "embed_url": "/apps/s/p/1"}, {"title": "B", "embed_url": "/apps/s/p/1#z"}]), "t")
    assert [x["title"] for x in first] == ["A"], "먼저 온 것이 이긴다"
    six = [{"embed_url": f"/apps/s/p/{i}"} for i in range(6)]
    assert [x["url"] for x in em.collect(json.dumps(six), "t")] == [f"/apps/s/p/{i}" for i in range(4)]


# ── 턴 그릇 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_턴_그릇은_같은_주소_하나_턴에_네_개까지이고_rest_는_한_번만_낸다():
    def fill(_bag):
        em.add(json.dumps([{"embed_url": f"/apps/s/p/{i}"} for i in range(3)]), "t1", "s")
        em.add(json.dumps([{"title": "다시", "embed_url": "/apps/s/p/0"}] + [{"embed_url": f"/apps/s/p/{i}"} for i in (3, 4)]), "t2", "s")
        em.add(json.dumps({"embed_url": "/apps/s/p/9"}), "t3", "s")
    bag = _in_turn(fill)
    assert [(x["url"], x["tool"]) for x in bag] == [("/apps/s/p/0", "t1"), ("/apps/s/p/1", "t1"), ("/apps/s/p/2", "t1"),
                                                    ("/apps/s/p/3", "t2")], "턴에 4개 · 같은 주소는 먼저 온 것"
    out = em.rest(bag)
    assert out == [{"items": [{"app": "s", "tool": "t1", "url": "/apps/s/p/0", "title": "s · t1"},
                              {"app": "s", "tool": "t1", "url": "/apps/s/p/1", "title": "s · t1"},
                              {"app": "s", "tool": "t1", "url": "/apps/s/p/2", "title": "s · t1"},
                              {"app": "s", "tool": "t2", "url": "/apps/s/p/3", "title": "s · t2"}]}]
    assert bag == [] and em.rest(bag) == [] and em.rest([]) == [], "한 턴에 한 번 — 낸 뒤엔 비었다"


def test_챗_턴_밖에서는_글을_읽지도_않고_턴_안의_실패는_삼킨다(monkeypatch, capsys):
    seen: list = []
    monkeypatch.setattr(em, "collect", lambda *x: seen.append(x) or [])
    em.add(json.dumps(_sim("c1")), "open_sim", "simflow")
    assert em.TURN.get() is None and seen == [], "그릇이 없으면(심의 · 띵킹 · 루틴 · 카탈로그) 아무것도 안 한다"

    def boom(*_x):
        raise RuntimeError("파싱 버그")
    monkeypatch.setattr(em, "collect", boom)
    assert _in_turn(lambda _b: em.add("{}", "open_sim", "simflow")) == []
    assert "뷰어 주소를 못 모음" in capsys.readouterr().out, "카드를 못 모은 것이 도구 결과를 죽이지 않는다"


# ── 실제 턴 — 진짜 FastMCP 가짜 게이트웨이 ─────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def viewer_gw():
    """진짜 FastMCP(mcp 1.28) — StepForge 꼴 두 도구 · 표지를 단 도구 · JSON 이 아닌 도구 · invoke_tool 대역. 받은 호출을 남긴다."""
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("hwax-fake-viewers", stateless_http=True, streamable_http_path="/")
    calls: list = []

    @mcp.tool()
    async def section_atlas(project_id: str = "", viewer: bool = False) -> list:
        """StepForge 단면 지도 꼴 — viewer=true 면 지도 뷰어(공개 주소)를 싣는다."""
        calls.append(("section_atlas", project_id, viewer))
        return [_atlas(project_id)] if viewer else [{"project_id": project_id, "status": "ready", "slices": 3}]

    @mcp.tool()
    async def view_3d(project_id: str = "") -> dict:
        """StepForge 3D 뷰어 링크 꼴."""
        calls.append(("view_3d", project_id))
        return _view3d(project_id)

    @mcp.tool()
    async def open_sim(case: str = "") -> dict:
        """명시 표지(embed_url)를 단 결과."""
        calls.append(("open_sim", case))
        return _sim(case)

    @mcp.tool()
    async def plain_lookup(q: str = "") -> str:
        """JSON 이 아닌 결과 — 글 속에 표지 낱말이 있어도 카드가 아니다."""
        calls.append(("plain_lookup", q))
        return f'조회 결과 {q} — 글 속의 "embed_url": "/apps/simflow/panel/zz" 는 JSON 이 아니다'

    @mcp.tool()
    async def invoke_tool(name: str, arguments: dict | None = None) -> str:
        """게이트웨이 invoke_tool 대역 — open_sim 이면 그 결과를 낸다."""
        calls.append(("invoke_tool", name))
        return json.dumps(_sim(str((arguments or {}).get("case", "")))) if name == "open_sim" else f"called {name}"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "가짜 게이트웨이가 뜨지 않았다"
    yield {"gateway": {"url": f"http://127.0.0.1:{port}/", "transport": "streamable_http"}}, calls
    server.should_exit = True
    th.join(timeout=10)


@pytest.fixture
def turn_iso(monkeypatch):
    """실제 턴 시험의 격리 — 창 크기 · 도구 지도(백엔드 이름)를 박고 자동 재시도는 끈다(켜는 시험은 제 손으로). 떠 있는 게이트웨이 · LLM 에 묻지 않는다."""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(a, "_ctx_cache", {"n": 1_048_576, "src": "server"})
    monkeypatch.setattr(a, "_TOOLS_MAP_CACHE", {"at": time.time(), "routine_read": None, "areas": {}, "area_meta": {}, "map": {
        "section_atlas": "heax-step_forge", "view_3d": "heax-step_forge", "open_sim": "simflow", "plain_lookup": "fake-viewers"}})
    monkeypatch.setenv("CHAT_AUTO_RETRY", "0")


def _req(**kw):
    return a.ChatRequest(message="demo 과제 단면 지도 보여줘", groups=["portal-admin"], history=[], **kw)


def _embeds_of(evs) -> list:
    return [dd for ev, dd in evs if ev == "embeds"]


def test_챗_턴은_result_앞에_embeds_를_한_번_내고_모델에_가는_결과는_그대로다(viewer_gw, turn_iso, monkeypatch):
    conns, calls = viewer_gw
    before = len(calls)
    llm = h20._Script(steps=[[("section_atlas", {"project_id": "demo", "viewer": True}), ("view_3d", {"project_id": "demo"}),
                              ("plain_lookup", {"q": "demo"})], "단면 지도를 열었습니다."])
    h20._wire(monkeypatch, llm, conns)
    evs = h20._turn(_req())
    names = [ev for ev, _ in evs]
    assert ("section_atlas", "demo", True) in calls[before:] and ("view_3d", "demo") in calls[before:], calls[before:]
    assert names.count("embeds") == 1 and names.index("embeds") < names.index("result") < names.index("done"), names
    assert _embeds_of(evs) == [{"items": [ATLAS_ITEM]}], "단면 지도만 — 3D 뷰어 · JSON 아닌 결과는 카드가 아니다"
    # 모델에 가는 결과 — 턴 밖(그릇 없음)에서 같은 래퍼로 부른 결과와 글자까지 같다(공개 주소도 글에 그대로)
    tm = next(m for m in llm.seen[1] if isinstance(m, ToolMessage) and "atlas" in str(m.content))
    oracle = h20._direct(conns, "section_atlas", {"project_id": "demo", "viewer": True})
    assert tm.content == oracle.content and f"{PUB}{ATLAS}" in str(tm.content)


def test_모은_것이_없으면_embeds_를_내지_않는다(viewer_gw, turn_iso, monkeypatch):
    conns, _ = viewer_gw
    llm = h20._Script(steps=[[("view_3d", {"project_id": "demo"}), ("section_atlas", {"project_id": "demo"})], "3D 링크입니다."])
    h20._wire(monkeypatch, llm, conns)
    evs = h20._turn(_req())
    assert "result" in [ev for ev, _ in evs] and _embeds_of(evs) == [], evs


def test_invoke_tool_경유는_안쪽_도구_이름과_그_백엔드로_싣고_같은_주소는_하나다(viewer_gw, turn_iso, monkeypatch):
    conns, calls = viewer_gw
    before = len(calls)
    llm = h20._Script(steps=[[("invoke_tool", {"name": "open_sim", "arguments": {"case": "c7"}})],
                             [("open_sim", {"case": "c7"})], [("open_sim", {"case": "c8"})], "패널 둘입니다."])
    h20._wire(monkeypatch, llm, conns)
    evs = h20._turn(_req())
    assert ("invoke_tool", "open_sim") in calls[before:] and calls[before:].count(("open_sim", "c7")) == 1, calls[before:]
    assert _embeds_of(evs) == [{"items": [
        {"app": "simflow", "tool": "open_sim", "url": "/apps/simflow/panel/c7", "title": "시뮬 흐름 c7"},
        {"app": "simflow", "tool": "open_sim", "url": "/apps/simflow/panel/c8", "title": "시뮬 흐름 c8"}]}]


def test_도구_래퍼는_챗_턴_그릇이_있을_때만_원문에서_모은다(viewer_gw, turn_iso):
    """심의 · 띵킹 · 루틴 · 카탈로그는 그릇을 걸지 않는다 — 같은 래퍼(_prep_tool)를 지나도 모으지 않는다. 그릇이 있으면 접기 · 자르기 전 원문에서."""
    conns, _ = viewer_gw
    tools = {t.name: t for t in asyncio.run(a._get_tools_retry(conns))}
    tool = a._prep_tool(tools["section_atlas"], result_max=40)          # 모델 사본은 40자에서 잘린다 — 모으기는 원문을 본다
    call = {"name": "section_atlas", "args": {"project_id": "demo", "viewer": True}, "id": "x", "type": "tool_call"}

    async def run(bag):
        if bag is not None:
            em.TURN.set(bag)
        msg = await tool.ainvoke(call)
        return em.TURN.get(), msg
    got, msg = asyncio.run(run(None))
    assert got is None and ATLAS not in str(msg.content), "그릇이 없으면 모으지 않는다(모델 사본은 잘렸다)"
    bag: list = []
    asyncio.run(run(bag))
    assert bag == [ATLAS_ITEM], "앱은 도구 지도의 백엔드(heax-step_forge)"


@pytest.mark.parametrize("self_check", [False, True])
def test_자동_재시도_안에서_부른_도구의_카드도_result_앞에_나간다(viewer_gw, turn_iso, monkeypatch, self_check):
    """첫 스트리밍 호출이 글 없이 터지면 자동 재시도(agent.ainvoke)가 그래프를 다시 돈다 — 그 안에서 부른 도구의 카드도 건진 답의 result 앞에 한 번."""
    conns, calls = viewer_gw
    monkeypatch.setenv("CHAT_AUTO_RETRY", "1")
    monkeypatch.setenv("CHAT_SELF_CHECK", "1" if self_check else "0")
    monkeypatch.setattr(a.app.state, "chat_llm", h20._Reviewer(), raising=False)
    before = len(calls)
    llm = h20._FailFirstStream(steps=[[("section_atlas", {"project_id": "demo", "viewer": True})], "단면 지도를 열었습니다."])
    h20._wire(monkeypatch, llm, conns)
    evs = h20._turn(_req())
    names = [ev for ev, _ in evs]
    assert calls[before:].count(("section_atlas", "demo", True)) == 1 and "error" not in names, names
    assert names.count("embeds") == 1 and names.index("embeds") < names.index("result") < names.index("done"), names
    assert _embeds_of(evs) == [{"items": [ATLAS_ITEM]}]


def test_자기검증을_켜도_embeds_는_검증_앞에_그대로_나가고_result_보다_먼저다(viewer_gw, turn_iso, monkeypatch):
    conns, _ = viewer_gw
    monkeypatch.setenv("CHAT_SELF_CHECK", "1")
    rv = h20._Reviewer()
    monkeypatch.setattr(a.app.state, "chat_llm", rv, raising=False)
    llm = h20._Script(steps=[[("section_atlas", {"project_id": "demo", "viewer": True})], "demo 과제 단면 지도는 3장입니다."])
    h20._wire(monkeypatch, llm, conns)
    evs = h20._turn(_req())
    names = [ev for ev, _ in evs]
    checking = next(i for i, (ev, dd) in enumerate(evs) if ev == "status" and "답을 검증하는 중" in str(dd.get("step")))
    assert rv.calls, "검토자가 돌았다(감싸개 길)"
    assert names.count("embeds") == 1 and names.index("embeds") < checking < names.index("result") < names.index("done"), names
    assert _embeds_of(evs) == [{"items": [ATLAS_ITEM]}]


# ── 자기검증 감싸개 — 프레임을 바이트 그대로 흘린다 ───────────────────────────────────────────────────────────────

FRAME = a._sse(em.EVENT, {"items": [ATLAS_ITEM]})


def _inner(draft: str, *, early=False):
    """안쪽 턴 대역 — 도구 한 번 · 초안 · embeds · (meta) · result · done. early 면 초안을 감싸개에 넘기지 않고 끝난다(종전 끝 길)."""
    async def fake(app_, req, box=None):
        yield a._sse("status", {"step": "도구 완료: section_atlas", "tool": "section_atlas"})
        yield a._sse("token", {"delta": draft})
        if not early and box is not None:
            box.update(draft=draft, calls=[("section_atlas", "project_id=demo")], outputs=["slices 3"])
        yield FRAME
        if not early:
            yield a._sse("meta", {"models": {"qwen-x": 2}})
        yield a._sse("result", {"type": "text", "content": draft})
        yield a._sse("done", {})
    return fake


@pytest.mark.parametrize("early", [False, True])
def test_자기검증_감싸개는_embeds_프레임을_바이트_그대로_result_앞에_흘린다(monkeypatch, early):
    monkeypatch.setattr(a, "_agent_stream_turn", _inner("단면 지도는 3장입니다.", early=early))
    monkeypatch.setattr(a.app.state, "chat_llm", h20._Reviewer(), raising=False)
    req = a.ChatRequest(message="단면 지도", groups=["portal-admin"], history=[])

    async def go():
        return [c async for c in a._self_checked_turn(a.app, req)]
    out = asyncio.run(go())
    kinds = [a._sse_parse(c)[0] for c in out]
    assert out.count(FRAME) == 1 and kinds.count(em.EVENT) == 1, "바꾸지도 겹치지도 않는다"
    assert out.index(FRAME) < kinds.index("result") < kinds.index("done"), kinds
    assert early or any(k == "status" and "검증" in a._sse_parse(c)[1].get("step", "") for k, c in zip(kinds, out)), \
        "초안을 넘긴 턴은 검증 길을 지났다"

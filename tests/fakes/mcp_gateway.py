# 진짜 프로세스 심의 시험용 가짜 게이트웨이 MCP 서버 — 엔진이 심의 들머리에 받는 도구 몇 개만 정해진 답으로 낸다
"""에이전트 서버의 심의 엔진은 게이트웨이 도구가 하나도 없으면 시작하지 않는다(gateway_unavailable). 이 서버를 빈 포트에 띄우고
임시 에이전트 서버의 MCP_CONFIG 를 여기로 돌리면, 실 게이트웨이 · 허브 · AIDataHub 없이 MCP 심의 잡을 끝까지 돌릴 수 있다
(tests/test_ha09_restart_e2e.py). 판정의 질은 보지 않는다. 시험 디렉터리에만 있다 — 운영 코드에 가짜 손잡이를 두지 않는다.

    .venv/bin/python tests/fakes/mcp_gateway.py --port 18124

도구 — get_agent_session(좌석 역할 원문 — 지정 좌석의 역할 복원) · recommend_agents(좌석 셋) · agent_search(지식카드 0건).
9009(떠 있는 에이전트 서버)에는 뜨지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

SEATS = ("mech-a", "rel-b", "pcb-c")


def make_server(port: int) -> FastMCP:
    mcp = FastMCP("fake-gateway", host="127.0.0.1", port=port, stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @mcp.tool()
    def get_agent_session(agent_type: str) -> str:
        """좌석 역할 원문 — 정해진 글이다."""
        return json.dumps({"data": {"agent_type": agent_type, "name": agent_type,
                                    "description": f"{agent_type} 전문가 — 대본 좌석이다. 자기 도메인 관점에서만 판단한다."}},
                          ensure_ascii=False)

    @mcp.tool()
    def recommend_agents(q: str) -> str:
        """좌석 발굴 — 늘 같은 좌석 셋."""
        return json.dumps({"agents": [{"agent_type": k, "name": k, "score": 0.9} for k in SEATS]}, ensure_ascii=False)

    @mcp.tool()
    def agent_search(q: str = "", agent_type: str = "") -> str:
        """지식카드 검색 — 늘 0건."""
        return json.dumps({"hits": []}, ensure_ascii=False)

    return mcp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="진짜 프로세스 심의 시험용 가짜 게이트웨이 MCP 서버")
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args(argv)
    if args.port == 9009:
        print("9009 는 떠 있는 에이전트 서버의 포트다 — 빈 포트를 고른다", file=sys.stderr)
        return 2
    print(f"[fake-gateway] http://127.0.0.1:{args.port}/mcp", flush=True)
    make_server(args.port).run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())

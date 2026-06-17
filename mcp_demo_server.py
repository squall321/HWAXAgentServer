"""Minimal demo MCP server (streamable-http) for HWAX Agent Server dev.

Stands in for the real per-sub-page MCP servers until those exist. Exposes a couple
of tools the LLM can call through the LangGraph ReAct loop, so we can prove the full
tool-calling chain end-to-end. Real MCP servers (stress analysis, etc.) plug in the
same way — the Agent Server just adds their URL to MultiServerMCPClient.

Run:  .venv/bin/python mcp_demo_server.py   → http://127.0.0.1:8011/mcp
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "hwax-demo",
    host="127.0.0.1",
    port=8011,
    stateless_http=True,   # fresh session per call — fine for stateless tools
    json_response=True,
)


@mcp.tool()
def add(a: float, b: float) -> float:
    """두 수를 더한다. 사용자가 계산을 요청하면 이 도구를 사용하라."""
    return a + b


@mcp.tool()
def multiply(a: float, b: float) -> float:
    """두 수를 곱한다."""
    return a * b


@mcp.tool()
def current_time(tz: str = "Asia/Seoul") -> str:
    """현재 시각을 ISO 8601로 반환한다. tz는 IANA 시간대(기본 Asia/Seoul)."""
    # tz has a default → avoids the hermes empty-arguments streaming drop for no-arg tools.
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now(timezone.utc)
    return now.isoformat()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")  # serves at http://127.0.0.1:8011/mcp

"""In-process tool path for hosted adapters (BUILD_PLAN §2.8, M4-T04).

The runner's `_recv_agent` routes an adapter `tool_call` event like this:

    ev.payload = {"id": ..., "name": ..., "arguments": {...}}
    result = await tools.call(ev.payload["name"], ev.payload["arguments"],
                              tool_call_id=ev.payload["id"])
    await adapter.tool_result(ev.payload["id"], result)

`call` hits the same `ToolServer.invoke` as `POST /tools/{token}/{tool}`, so
the `tool_calls` row is identical to the HTTP path. It returns the same JSON
body the HTTP route would return: the result, or an error object
(`{"error": "timeout" | "internal_error" | "policy_violation" | "bad_arguments"
| "unknown_tool", ...}`). `outcome` returns the full ToolOutcome (status, row).
`tools()` lists the JSON schemas the adapter declares to the provider.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duplexbench.tools.server import ToolOutcome, ToolServer


class ToolClient:
    def __init__(self, server: "ToolServer", token: str) -> None:
        self.server = server
        self.token = token
        server.binding(token)              # fail fast on an unregistered token

    def tools(self) -> list[dict]:
        return self.server.tool_schemas(self.token)

    async def outcome(self, name: str, args: dict | None,
                      tool_call_id: str | None = None) -> "ToolOutcome":
        return await self.server.invoke(self.token, name, args, tool_call_id)

    async def call(self, name: str, args: dict | None, tool_call_id: str | None = None) -> dict:
        from duplexbench.tools.server import UnknownTool
        try:
            out = await self.outcome(name, args, tool_call_id)
        except UnknownTool:
            return {"error": "unknown_tool", "detail": name}
        return out.body if out.body is not None else {"error": "timeout"}

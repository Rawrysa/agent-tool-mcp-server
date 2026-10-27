"""
demo_client.py — walks through what an agent framework does with this server.

1. Lists tools and reads each tool's Tool._meta (planning hints, approval flag).
2. Reads the tools://manifest/hr_demo resource.
3. Resolves an employee_id with the resolution-only lookup tool.
4. Calls a fast tool inline.
5. Calls a slow tool, gets a _poll envelope back, and polls job_status_tool.

Run the server first (docker compose up), then:
    python examples/demo_client.py [http://localhost:8000/mcp]
"""
import asyncio
import json
import sys
import time
from typing import Any, Dict

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

SERVER_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"


def _payload(result) -> Dict[str, Any]:
    """Return a tool result as a dict, from structured content or the JSON text block."""
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return json.loads(result.content[0].text)


async def main() -> None:
    async with streamable_http_client(SERVER_URL) as streams:
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            print("== tools/list (with Tool._meta)")
            for tool in (await session.list_tools()).tools:
                meta = tool.meta or {}
                print(
                    f"  {tool.name:28} client={meta.get('client')} "
                    f"async={meta.get('is_async_job')} resolution_only={meta.get('is_resolution_only')} "
                    f"approval={meta.get('require_approval')}"
                )

            print("\n== resource tools://manifest/hr_demo")
            manifest = json.loads((await session.read_resource("tools://manifest/hr_demo")).contents[0].text)
            print(f"  {len(manifest)} tools: {[t['function']['name'] for t in manifest]}")

            print("\n== resolve employee_id (resolution-only tool)")
            found = _payload(await session.call_tool("find_employee_tool", {"email": "thandi.nkosi@example.org"}))
            print(f"  match_count={found['match_count']} -> {found['matches']}")
            ambiguous = _payload(await session.call_tool("find_employee_tool", {"first_name": "Lerato", "last_name": "Mokoena"}))
            print(f"  'Lerato Mokoena' -> match_count={ambiguous['match_count']} (inactive duplicate filtered out)")
            employee_id = found["matches"][0]["employee_id"]

            print("\n== fast tool, inline")
            balance = _payload(await session.call_tool("get_leave_balance_tool", {"employee_id": employee_id}))
            print(f"  {balance['employee_name']}: {balance['balances']}")

            print("\n== slow tool, async job")
            started = time.perf_counter()
            queued = _payload(await session.call_tool("generate_leave_report_tool", {"low_balance_threshold": 10}))
            print(f"  returned in {(time.perf_counter() - started) * 1000:.0f} ms: status={queued['status']} job_id={queued['job_id']}")

            poll = queued["_poll"]
            while True:
                await asyncio.sleep(min(poll["interval_seconds"], 2))
                status = _payload(await session.call_tool(poll["tool"], poll["params"]))
                print(f"  poll: status={status['status']}")
                if status["all_complete"]:
                    break

            print(f"  report: totals={status['totals']} low_annual_balance={status['low_annual_balance']}")
            print(f"  total wall time {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())

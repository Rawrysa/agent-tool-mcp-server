# MCP Tool Server Decoupling

> **Design note from the production deployment.** File paths and module names that are
> not in this repository refer to the original private codebase (the agent server and
> agent framework); the server architecture described here is what this repo contains.

## Overview

This document details the architectural changes made to decouple tool registration and execution from the the agent server server into a standalone MCP tool server. The goal is that adding or modifying a tool only requires redeploying the MCP server — the agent server remains untouched and automatically discovers the changes at runtime.

---

## Problem

Previously, tools lived inside the the agent server server (its own `tools/` package) and were registered at startup via `agent/index.py`. This meant:

- Any tool change (new tool, bug fix, parameter update) required a full the agent server redeploy.
- Tool classes had hard dependencies on `agent_framework` (`BaseTool`, `logger`, `RedisClient`), coupling the tool layer to the framework version.
- There was no independent path to deploy tools.

---

## New Architecture

```
┌─────────────────────────────────────────────────────┐
│                    the agent server Server                     │
│                                                      │
│  agent/index.py                                      │
│    └── RemoteToolRegistry.populate()                 │
│         └── fetches manifest from MCP server        │
│                                                      │
│  PlanExecutor                                        │
│    └── tool.execute(**params)                        │
│         └── RemoteTool → HTTP → MCP server          │
│                                                      │
│  Bootstrapper                                        │
│    └── start_mcp_tools_listener()                   │
│         └── Redis sub → refresh on redeploy         │
└─────────────────────────┬───────────────────────────┘
                          │  MCP streamable-HTTP
┌─────────────────────────▼───────────────────────────┐
│                  MCP Tool Server                     │
│                                                      │
│  app.py                                              │
│    ├── PROJECT_TOOLS dict (all tool instances)       │
│    ├── Dynamic tool registration (loop)              │
│    ├── Resource: tools://manifest                    │
│    ├── Resource: tools://manifest/{client}           │
│    └── On startup: publish tools:updated to Redis   │
│                                                      │
│  tools/lambdatest_tools/                            │
│    └── No framework dependency                       │
└─────────────────────────────────────────────────────┘
```

---

## Files Changed

### MCP Server (`mcp-server/`)

#### New Files

| File | Purpose |
|------|---------|
| `base_tool.py` | Local `BaseTool` ABC. Replaces `agent_framework.utils.tool_registry.BaseTool`. Identical interface — `name`, `description`, `steps`, `is_browser_automation`, `_get_parameters_schema()`, `execute()`, `get_tool_metadata()`, `log_execution()`. |
| `tool_logger.py` | Local `logger` instance backed by Python's `logging` module. Replaces `agent_framework.utils.logger`. Level controlled via `LOG_LEVEL` env var. |
| `tool_redis.py` | Minimal async `RedisClient` for tools that enqueue background jobs (currently only `reset_password_tool`). Replaces `agent_framework.utils.providers.redis_provider.RedisClient`. Reads connection details from env vars (`REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_USERNAME`, `REDIS_PASSWORD`). |

#### Modified Files

**`app.py`** — full rewrite:

- `PROJECT_TOOLS` dict remains the single place to register tools. Adding a tool = adding one line here.
- A flat `_TOOL_LOOKUP` dict (`tool_name → (instance, client)`) is built from `PROJECT_TOOLS` at import time.
- Tools are registered with FastMCP dynamically via a loop calling `mcp.add_tool()` — no manual `@mcp.tool` decorator per tool.
- Two MCP **resources** expose the tool manifest for discovery:
  - `tools://manifest` — full manifest (all clients)
  - `tools://manifest/{client}` — filtered to a specific client (e.g. `project_management`)
- On startup, a `tools:updated` event is published to Redis containing the full manifest and the server URL, so the agent server can refresh without polling.

**`tools/lambdatest_tools/*.py`** — all 14 tool files updated:

| Old import | New import |
|-----------|-----------|
| `from agent_framework.utils.tool_registry import BaseTool` | `from base_tool import BaseTool` |
| `from agent_framework.utils import logger` | `from tool_logger import logger` |
| `from agent_framework.utils.providers.redis_provider import RedisClient` | `from tool_redis import RedisClient` |

No other code was changed in the tool files.

---

### Framework (`agent_framework/`)

#### New Files

**`utils/mcp_client.py`** — `MCPClient`

Thin HTTP client for the MCP server. Two methods:

```python
MCPClient(base_url=None)           # reads MCP_SERVER_URL env var if not provided

# Discovery
client.fetch_manifest(client=None) # GET tools://manifest[/{client}]
                                   # returns List[Dict] — tool metadata with 'client' field

# Execution
await client.call_tool(name, params) # POST /tools/{name}
                                     # returns Dict result from tool
```

**`utils/remote_tool_registry.py`** — `RemoteTool` + `RemoteToolRegistry`

- `RemoteTool` is a `BaseTool` implementation that holds metadata from the manifest and delegates `execute()` to `MCPClient.call_tool()`. `PlanExecutor` calls it identically to a local tool — no changes needed there.
- `RemoteToolRegistry` fetches the manifest and populates a `ToolRegistry` with `RemoteTool` instances:

```python
registry = RemoteToolRegistry()
tool_registry = registry.populate(client_filter="project_management")
registry.refresh(tool_registry, client_filter="project_management")  # used by listener
```

#### Modified Files

**`bootstrap/bootstrapper.py`** — two additions:

- `start_mcp_tools_listener(workflow_managers, client_filter=None)` — public function to call from the agent server startup. Starts a single background asyncio task.
- `_mcp_tools_listener_loop(...)` — internal coroutine. Subscribes to the Redis `tools:updated` channel. On each event, calls `RemoteToolRegistry.refresh()` on every `WorkflowManager`'s `tool_registry` and rebuilds `wm.tools` (the schema list the Planner uses). This means the agent server picks up new tools on the next request after an MCP server redeploy, with zero downtime.

---

## How Tool Discovery Works

```
MCP server starts
      │
      ├── registers tools with FastMCP
      ├── exposes tools://manifest resource
      └── publishes to Redis: tools:updated { event, server_url, tools: [...] }
            │
            ▼
      the agent server bootstrapper listener receives event
            │
            ├── RemoteToolRegistry.refresh(tool_registry)
            │     └── fetches tools://manifest/{client} from MCP server
            │     └── rebuilds ToolRegistry with RemoteTool proxies
            └── rebuilds wm.tools (schema list for Planner)
```

the agent server does **not** need to restart. The next `process_request()` call uses the updated registry.

If Redis is unavailable at MCP startup, the publish fails silently (logged as a warning). the agent server can still manually call `RemoteToolRegistry.populate()` on demand.

---

## How to Wire in the agent server

Replace the tool instantiation block in `agent/index.py`:

```python
# Before
tool_registry = ToolRegistry()
for project_name, tools in PROJECT_TOOLS.items():
    for tool in tools:
        tool_registry.register_tool(tool, project=project_name)

# After
from agent_framework.utils.remote_tool_registry import RemoteToolRegistry

remote_registry = RemoteToolRegistry()  # MCP_SERVER_URL from env
tool_registry = remote_registry.populate(client_filter="project_management")
tools_schema = [reg.tool.get_tool_metadata() for reg in tool_registry._tools.values()]
```

And in the app lifespan / startup event:

```python
from agent_framework.bootstrap.bootstrapper import start_mcp_tools_listener

# Pass all WorkflowManager instances that should refresh on tool changes
start_mcp_tools_listener(
    workflow_managers=[wm for agent in AGENT_INSTANCES.values() for wm in [agent.workflow_manager]],
    client_filter="project_management",
)
```

---

## Adding a New Tool

1. Create the tool class in `tools/<your_package>/your_tool.py` (see `tools/examples/`)
   - Extend `BaseTool` from `base_tool` (local)
   - Use `logger` from `tool_logger` (local)
   - Implement `name`, `description`, `steps`, `_get_parameters_schema()`, `execute()`

2. Add it to `PROJECT_TOOLS` in `app.py`:
   ```python
   from tools.your_package.your_tool import YourTool

   PROJECT_TOOLS = {
       "project_management": [
           ...
           YourTool(),
       ]
   }
   ```

3. Redeploy the MCP server.

the agent server discovers the new tool automatically via the `tools:updated` Redis event. No the agent server changes required.

---

## Environment Variables

| Variable | Default | Used by |
|----------|---------|---------|
| `MCP_SERVER_URL` | `http://localhost:8000` | `MCPClient` (framework), `app.py` (publish payload) |
| `REDIS_HOST` | `localhost` | MCP server, framework listener |
| `REDIS_PORT` | `6379` | MCP server, framework listener |
| `REDIS_DB` | `0` | MCP server, framework listener |
| `REDIS_USERNAME` | _(none)_ | MCP server, framework listener |
| `REDIS_PASSWORD` | _(none)_ | MCP server, framework listener |
| `LOG_LEVEL` | `INFO` | `tool_logger.py` |

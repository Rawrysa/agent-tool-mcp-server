# Agent Tool MCP Server

A standalone [Model Context Protocol](https://modelcontextprotocol.io) tool server for AI agents. It was extracted from a production server I built that gives a multi-agent service-automation platform 22 workflow tools for a public-sector HR system.

This repository contains the **server architecture** from that project, runnable end to end with three example tools and made-up data. The production tools themselves aren't included.

## What it shows

- **One-line tool registry.** Every tool lives in a single `PROJECT_TOOLS` dict in [`app.py`](app.py). Adding a tool means adding one line and redeploying this server. The agent server never needs to change.
- **Discovery two ways.** The full tool manifest is served as MCP resources (`tools://manifest` and `tools://manifest/{client}`). The same planning metadata is also attached to each tool in the standard **`Tool._meta`** field, so any spec-compliant client gets it from a plain `tools/list` call.
- **Agent-facing metadata.** Each tool declares `steps`, `require_approval`, `is_async_job` and `is_resolution_only`. Agents use these to plan, to decide when a human must sign off, and to tell lookup helpers apart from real actions.
- **Input-resolution playbooks.** A tool's `steps` can end with plain-language `RESOLVE …` instructions that the agent's input resolver follows to fill in missing parameters. The guard rails are built in: only accept an exactly-one match, never resolve inactive records, and ask the user instead of guessing.
- **Redis-backed async job queue.** Slow tools (originally Selenium browser automation running up to 5 minutes) were being cut off by Azure's load balancer. Tools marked `is_async_job` are enqueued and return a `_poll` envelope in milliseconds. An in-process `JobWorker` runs them, and the client polls `job_status_tool`, which merges the tool's result back in so callers see the same shape as an inline call.
- **Live refresh.** On startup the server publishes a `tools:updated` event on Redis pub/sub, so a running agent server can reload its tool registry without restarting.
- **MCP 2.x on uvicorn/gunicorn.** A module-level ASGI app whose lifespan composes the job worker with the MCP session manager, which MCP 2.x no longer starts for you.

## Run it

```bash
docker compose up --build
```

This starts Redis and the server at `http://localhost:8000/mcp`. Then run the demo client:

```bash
uv run examples/demo_client.py
```

```
== tools/list (with Tool._meta)
  job_status_tool              client=common async=False resolution_only=False approval=False
  find_employee_tool           client=hr_demo async=False resolution_only=True approval=False
  get_leave_balance_tool       client=hr_demo async=False resolution_only=False approval=False
  generate_leave_report_tool   client=hr_demo async=True resolution_only=False approval=False

== resolve employee_id (resolution-only tool)
  match_count=1 -> [{'employee_id': 'E1001', ...}]
  'Lerato Mokoena' -> match_count=1 (inactive duplicate filtered out)

== fast tool, inline
  Thandi Nkosi: {'annual': 14.0, 'sick': 28.0, 'family': 3.0}

== slow tool, async job
  returned in 28 ms: status=queued job_id=9f674816-...
  poll: status=processing
  poll: status=succeeded
  report: totals={'annual': 41.5, 'sick': 83.0, 'family': 7.0} low_annual_balance=['E1002']
```

To run without Docker, start a local Redis, copy `.env.example` to `.env`, and run `uv run app.py`. The server also works from any MCP client, such as Claude Desktop or the MCP Inspector, pointed at `http://localhost:8000/mcp`.

## Layout

```
app.py                       registry, manifest resources, Tool._meta, dispatch, ASGI lifespan
base_tool.py                 BaseTool: schema, metadata and the agent-facing flags
jobs/queue.py                enqueue / read / mark_processing / mark_terminal (Redis hash + list + pub/sub)
jobs/worker.py               JobWorker: BLPOP loop with bounded concurrency
tools/common/                job_status_tool, the target of every _poll envelope
tools/examples/              resolution-only lookup, inline tool with a playbook, async-job tool
examples/demo_client.py      walks through the whole flow over streamable HTTP
docs/                        design notes from the production deployment
```

## Design notes

- [`docs/MCP_DECOUPLING.md`](docs/MCP_DECOUPLING.md) covers moving tools out of the agent server into this standalone service: the manifest, the remote tool registry and live refresh over Redis.
- [`docs/async-job-queue.md`](docs/async-job-queue.md) covers the async job queue: why it exists, the polling protocol, worker concurrency against a limit of 2 browser sessions, failure modes, and what was deliberately left out.

## Changes from the production version

- The 22 production tools, their client configuration, and the shared agent-framework package are replaced by the three example tools in `tools/examples/` and small local `BaseTool`/logger/Redis modules.
- Tool handlers now get a typed signature built from each tool's own JSON Schema. Under MCP 2.x a bare `**kwargs` handler is advertised as a single required `kwargs` argument, so standard clients sending real arguments were rejected. The production agent framework didn't hit this because it wrapped arguments under `kwargs`.

## Stack

Python 3.13 · MCP Python SDK 2.x · Redis · Starlette/uvicorn · Docker

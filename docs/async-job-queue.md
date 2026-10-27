# Async Job Queue — Team Notes

> **Design note from the production deployment.** File paths and module names that are
> not in this repository refer to the original private codebase (the agent server and
> agent framework); the server architecture described here is what this repo contains.

**Status:** Implemented and running in production
**Author:** Galaletsang Modimola
**Audience:** MCP server + agent framework contributors

---

## Why this exists

Long-running tools (Selenium/LambdaTest browser automation) were timing out
against the MCP client hosted on Azure App Service. The MCP request path was
synchronous: the framework's PlanExecutor would `POST /tools/call`, the server
would `await tool.execute(...)`, and the HTTP socket would sit idle for up to
5 minutes while Selenium clicked through a portal. Azure's load balancer
would terminate the connection before the tool finished.

The fix swaps that synchronous wait for **fire-and-forget enqueue + polling**:
the HTTP call returns immediately with a `job_id`, a background worker
executes the tool, and the framework client transparently polls for the
result. The PlanExecutor sees no difference — the polling is hidden inside
the existing `MCPClient` layer.

---

## Architecture at a glance

```
┌──────────────────────┐                       ┌──────────────────────────────┐
│ Agent framework      │  HTTP POST            │ MCP Server (Azure App        │
│ (MCPClient)          │  /tools/call          │ Service)                     │
│                      │ ─────────────────►    │                              │
│  PlanExecutor        │                       │  _make_handler:              │
│    │                 │                       │    if tool.is_async_job:     │
│    ▼                 │  {_poll: {...},       │      enqueue_job()           │
│  MCPClient           │   job_id, queued}     │      return _poll envelope   │
│  ._drive_poll(...)   │ ◄─────────────────    │    else:                     │
│    │                 │                       │      await tool.execute()    │
│    │ every 5s        │                       │                              │
│    │                 │  HTTP POST            │  ┌────────────────────────┐  │
│    ▼                 │  /tools/call          │  │ JobWorker (asyncio     │  │
│  call("job_status_   │  job_status_tool      │  │ task in lifespan)      │  │
│        tool",        │ ─────────────────►    │  │   BLPOP jobs_queue     │  │
│        {job_id})     │                       │  │   await tool.execute() │  │
│                      │ ◄─────────────────    │  │   mark_terminal()      │  │
│                      │  {all_complete, ...}  │  └────────────────────────┘  │
└──────────────────────┘                       │             │                │
                                               │             ▼                │
                                               │      ┌──────────────┐        │
                                               │      │    Redis     │        │
                                               │      │              │        │
                                               │      │ LIST         │        │
                                               │      │  jobs_queue  │        │
                                               │      │ HASH         │        │
                                               │      │  job:{id}    │        │
                                               │      │ PUBSUB       │        │
                                               │      │  job_result: │        │
                                               │      │  {id}        │        │
                                               │      └──────────────┘        │
                                               └──────────────────────────────┘
```

---

## The end-to-end flow

1. **Framework calls a tool.** `PlanExecutor` invokes a tool via
   `RemoteTool.execute(**kwargs)`, which forwards to
   `MCPClient.call_tool(name, params)` and POSTs to the MCP server.

2. **Server-side dispatch decides inline vs. async.** Inside
   `_make_handler` ([app.py](../app.py)), each request inspects
   `tool.is_async_job`:
   - **False** → executes inline (existing behaviour, returns the result
     directly).
   - **True** → calls `enqueue_job(tool_name, client, params)` and returns
     a `_poll` envelope. The HTTP call completes in <100 ms.

3. **Enqueue.** `enqueue_job` ([jobs/queue.py](../jobs/queue.py)) atomically:
   - Creates a `job:{uuid}` Redis hash with status `queued`, params, and
     timestamps (TTL 24h).
   - `RPUSH`es a JSON payload onto the `jobs_queue` list.

4. **Worker pickup.** A `JobWorker` ([jobs/worker.py](../jobs/worker.py))
   running as an asyncio task inside the server's lifespan loops on
   `BLPOP jobs_queue`:
   - Marks the job `processing`.
   - Looks the tool up in the MCP server's in-process `_TOOL_LOOKUP`.
   - Runs `await tool.execute(**params)`.
   - Marks the job `succeeded`/`failed` with the result, publishes a
     completion event on `job_result:{job_id}`.

5. **Framework polls.** `MCPClient._drive_poll` (which already existed —
   we are reusing the existing `_poll` protocol) sleeps the configured
   interval and POSTs `job_status_tool` with `{job_id}`. The status tool
   reads `job:{id}` and returns the current state. When status is terminal,
   it sets `all_complete=true` and merges the underlying tool's result
   fields onto the response.

6. **Result returned to PlanExecutor.** `_drive_poll` returns the merged
   response. `PlanExecutor` sees a normal tool result — the polling is
   invisible to it.

---

## What gets queued vs. what runs inline

Controlled by a per-tool boolean: `BaseTool.is_async_job`. Default is
`False`.

- **Set `True`** on tools that take more than a couple of seconds —
  primarily Selenium/LambdaTest browser automation.
- **Leave `False`** on quick API calls. They keep their existing synchronous
  request path and pay no polling latency.

Currently flipped to `True`: all 23 tools registered in `PROJECT_TOOLS` —
the 17 HR-platform API tools and the 6 LambdaTest admin/approver tools.

> **`job_status_tool` itself must stay `is_async_job=False`** —
> otherwise polling would enqueue more polls. The status tool runs inline
> on every poll.

---

## Concurrency model

There are **two independent dials** for concurrency. They multiply.

### Layer 1: App Service workers (process-level)

When Azure App Service runs N worker processes (gunicorn `--workers N`,
or the platform's "Number of Workers" setting), each is a **separate Python
interpreter** with its own event loop, its own `AsyncRedisClient` singleton,
and its own `JobWorker` asyncio task.

- All `JobWorker` instances `BLPOP` the **same** `jobs_queue` list.
- `BLPOP` is atomic — Redis hands each job to exactly one consumer. No
  double execution.
- Load balances naturally: whichever worker is free pops next.

### Layer 2: `JOB_WORKER_CONCURRENCY` (asyncio-level, within one process)

The semaphore inside `JobWorker` controls how many in-flight jobs **one**
process runs concurrently on its event loop.

### How they combine

```
total in-flight jobs = app_service_workers × JOB_WORKER_CONCURRENCY
```

### The LambdaTest constraint

LambdaTest gives us **2 parallel sessions**. Exceed that and LambdaTest
queues excess jobs server-side, defeating the point of the queue.

Combinations that stay at 2:

| App Service workers | `JOB_WORKER_CONCURRENCY` | Real concurrency | Notes |
|---|---|---|---|
| 1 | 2 | 2 | Single point of failure; loop blocking risk |
| **2** | **1** | **2** | **Recommended.** Resilient to process recycles |
| N | 2 | 2N | Over the LambdaTest cap |

**Recommended setting:** 2 App Service workers × `JOB_WORKER_CONCURRENCY=1`.

Reasons:
- Caps real concurrency at 2 — exactly the LambdaTest seat count.
- Two processes survive an Azure recycle: the other keeps draining.
- Selenium calls are synchronous and block the asyncio loop. One job
  per process keeps the `BLPOP` heartbeat responsive.

---

## Job lifecycle and Redis schema

### Keys

| Key | Type | Purpose | TTL |
|---|---|---|---|
| `jobs_queue` | LIST | FIFO of pending job JSONs | none |
| `job:{id}` | HASH | Metadata, status, final result | 24 h |
| `job_result:{id}` | PUBSUB channel | Completion event | n/a |

### Hash fields (`job:{id}`)

| Field | Set by | Notes |
|---|---|---|
| `tool_name` | `enqueue_job` | |
| `client` | `enqueue_job` | The "client" namespace the tool belongs to |
| `params` | `enqueue_job` | JSON-encoded `dict` |
| `status` | enqueue/worker | `queued` → `processing` → `succeeded`/`failed` |
| `result` | worker | JSON, set on terminal states |
| `created_at` | `enqueue_job` | ISO-8601 UTC |
| `updated_at` | all writers | |
| `started_at` | worker | When `BLPOP` returned the job |
| `finished_at` | worker | On terminal state |

### State transitions

```
                rpush                     BLPOP                  await tool.execute(...)
   client ─────────────►  queued  ─────────────►  processing  ───────────────────────►  succeeded
                                                                  │                    │
                                                                  │ exception raised    │ HSET, EXPIRE 24h, PUBLISH
                                                                  ▼                    ▼
                                                                failed       (channel: job_result:{id})
```

After 24h the hash expires automatically. The status tool then returns
`status="unknown"` with `all_complete=true` so the framework's poll loop
terminates cleanly instead of timing out.

---

## Polling protocol (reused as-is)

The framework's `MCPClient` (`mcp_client.py` (agent framework))
already implemented a `_poll` directive before this work. We are reusing
it verbatim, with no client changes.

When the server returns a payload containing a `_poll` field, the client
loops on the named status tool until either:
- The status response includes `all_complete: true` — returned to the
  caller as the final result.
- The configured timeout elapses — returned with `success: false,
  timed_out: true`.

The async-job server returns:

```json
{
  "_poll": {
    "tool": "job_status_tool",
    "params": { "job_id": "<uuid>" },
    "interval_seconds": 5,
    "timeout_seconds": 1800
  },
  "job_id": "<uuid>",
  "status": "queued"
}
```

The status tool's terminal response **merges the executed tool's result onto
the response top-level**. So `PlanExecutor` sees the same shape it would
have seen from a synchronous call (e.g. a generated password, `message`,
`public_url`, `video_url`).

### Why `job_status_tool` lives under the `"common"` client

`JobStatusTool` is registered in the MCP server's `PROJECT_TOOLS` under a
dedicated `"common"` namespace, not under `project_management`,
`service_automation`, etc. This is intentional, and worth understanding
because it interacts with how the agent server populates its registries.

In `the agent server's agent/index.py`, each agent's `WorkflowManager` is given a
`RemoteToolRegistry` populated with a **single** `client_filter`:

```python
pm_registry = remote_registry.populate(client_filter="project_management")
sa_registry = remote_registry.populate(client_filter="service_automation")
```

That means the project_management agent's registry **does not contain
`job_status_tool`** — and that is exactly what we want. The status tool
should never appear as a candidate in any LLM-generated plan; it is plumbing,
not a domain capability.

It still works because `MCPClient._drive_poll` resolves the status tool
**by name over HTTP**, not by looking it up in the framework-side
`tool_registry`. The call path is:

```
PlanExecutor                                  ← consults tool_registry
  → RemoteTool.execute()                      ← consults tool_registry
    → MCPClient.call_tool()                   ← HTTP POST tools/call
      → MCPClient._drive_poll()               ← bypasses tool_registry
        → MCPClient._invoke_tool_once(...)    ← HTTP POST tools/call
          → MCP server _make_handler(job_status_tool)   ← resolves via _TOOL_LOOKUP
```

So as long as `JobStatusTool` is in the MCP server's `_TOOL_LOOKUP` (any
client namespace will do — `"common"` is just descriptive), it's
reachable. `tool_registry` filtering on the framework side has no effect
on the polling path.

**Practical implications:**

- Do **not** reference `job_status_tool` in workflow plans or schema
  definitions. PlanExecutor isn't meant to invoke it, and the registries
  per agent are filtered to exclude it.
- If you ever want to inspect the status tool's schema, fetch the
  unfiltered manifest (`tools://manifest`) or the common-specific one
  (`tools://manifest/common`).
- Adding more "plumbing" tools later (e.g. a future job-cancellation
  tool, or a worker heartbeat tool) — keep them under `"common"` for the
  same reason.

---

## Why we publish to a channel nothing currently subscribes to

`mark_terminal` writes the result to the hash **and** publishes it on
`job_result:{job_id}`. Nothing in the framework subscribes to that channel
today — `MCPClient._drive_poll` only reads the hash via `job_status_tool`.
The publish is intentional dead weight: a forward-looking hook for a future
push-based completion path.

If we ever want sub-second completion latency (e.g. an admin dashboard
watching jobs in real time, or many small async jobs where the 5 s poll
interval is wasteful), a subscriber can be wired up with no server-side
changes. That subscriber will need to handle four failure modes — none of
which the current poll-only path has to think about:

1. **Subscribe-before-publish race.** A fast tool can finish before the
   client's subscription is open, and the completion event is lost (Redis
   pub/sub does not buffer for offline subscribers). The standard pattern
   is **subscribe first, then `HGET job:{id}` to check** — if the hash is
   already terminal, read the result and skip the wait; otherwise listen
   on the channel.

2. **Lost messages.** Even after subscribing, a 50 ms TCP blip during the
   job's execution window drops the event silently. Mitigation: keep
   polling as a fallback at a longer interval (e.g. 30 s instead of 5 s).
   Push covers happy-path latency; poll covers correctness.

3. **Pipeline non-atomicity.** The terminal `HSET` + `PUBLISH` go through
   a Redis pipeline, which is **not** atomic — a process kill between the
   two leaves the hash updated but no event sent. Same fallback applies.
   (Upgrading to a Lua script via `EVAL` would make this atomic but is
   probably overkill.)

4. **Subscription connection pressure.** `redis-py` async subscribers take
   a dedicated connection out of the pool. The pool currently caps at 22
   connections, so 22 concurrent waiting jobs would exhaust it. Not a
   near-term problem at our scale (LambdaTest caps real concurrency at 2)
   but worth knowing if you scale workers up.

**Decision today:** keep the `PUBLISH` (cheap, no-op when there are no
subscribers, marked with a comment in [jobs/queue.py](../jobs/queue.py)),
do not implement a subscriber. Revisit when there's a concrete consumer
that benefits from push.

---

## Configuration

All env vars; sensible defaults are baked in.

| Env var | Default | Purpose |
|---|---|---|
| `JOB_WORKER_CONCURRENCY` | `2` | Concurrent in-flight jobs **per process** |
| `JOB_POLL_INTERVAL_SECONDS` | `5` | How often the framework polls `job_status_tool` |
| `JOB_POLL_TIMEOUT_SECONDS` | `1800` | When the framework gives up polling |
| `REDIS_HOST` | `localhost` | (existing) |
| `REDIS_PORT` | `6379` | (existing) |
| `REDIS_DB` | `0` | (existing) |
| `REDIS_USERNAME` | unset | (existing) |
| `REDIS_PASSWORD` | unset | (existing) |
| `MCP_SERVER_URL` | `http://localhost:8000` | Advertised in `tools:updated` event |

**For production on Azure App Service:** set `JOB_WORKER_CONCURRENCY=1`
and configure 2 App Service workers (see Concurrency section above).

---

## What changed in this PR

### MCP server (`this MCP server`)

| File | Change |
|---|---|
| [jobs/__init__.py](../jobs/__init__.py) | **New** — re-exports |
| [jobs/queue.py](../jobs/queue.py) | **New** — `enqueue_job`, `read_job`, `mark_processing`, `mark_terminal`. Centralises Redis key/schema |
| [jobs/worker.py](../jobs/worker.py) | **New** — `JobWorker` class. BLPOPs `jobs_queue`, runs tools via the in-process `_TOOL_LOOKUP`, writes terminal state, publishes completion |
| [tools/common/__init__.py](../tools/common/__init__.py) | **New** — re-export |
| [tools/common/job_status_tool.py](../tools/common/job_status_tool.py) | **New** — `JobStatusTool` implementing the `_drive_poll` contract. Merges the executed tool's result onto the status response so callers see the same shape as a synchronous tool result |
| [app.py](../app.py) | Registered `JobStatusTool` under a new `"common"` client. `_make_handler` now branches on `tool.is_async_job` (enqueues + returns `_poll` envelope when True). `_server_lifespan` launches `JobWorker` as an asyncio task and cancels it cleanly on shutdown. `_init_async_redis()` initialises `AsyncRedisClient` once, used by both `_publish_tools_updated` and the queue. Removed the pre-launch `asyncio.run(...)` that bound the singleton to a throwaway loop |
| `tools/project_management_tools/orchestrator.py` | **Deleted** — superseded by `jobs/worker.py`. The old file had bugs (`RedisClient.get_async()` doesn't exist, instantiated a fresh empty `ToolRegistry()`) |
| `tools/lambdatest_tools/reset_password_tool.py` | Removed the stub `enqueue_execution()` method and its dead imports. Queueing decision lives in the dispatch wrapper now, not per tool |
| All 23 registered tools | Set `is_async_job = True` alongside existing `is_browser_automation = True` |

### Agent framework

| File | Change |
|---|---|
| `agent_framework/utils/tool_registry.py` (agent framework) | Added `is_async_job: bool = False` to `BaseTool`; included in `get_tool_metadata()` payload so the flag crosses the wire in the manifest |
| `agent_framework/utils/remote_tool_registry.py` (agent framework) | `RemoteTool.__init__` reads `is_async_job` from the manifest entry. Useful for observability — the *server* still owns the decision to enqueue |
| `MCPClient`, `_drive_poll`, `PlanExecutor` | **No changes.** The existing `_poll` directive plumbing handles everything |

---

## Operational notes & known limitations

- **Container recycles can orphan in-flight jobs.** Azure App Service may
  recycle a worker process while a job is `processing`. The job hash will
  stay in that state until the 24h TTL. **Mitigation later:** a "stale
  processing" reaper that re-queues jobs stuck in `processing` past some
  threshold.

- **No automatic retries.** If a tool raises, the job is marked `failed`
  and the result returned to PlanExecutor. PlanExecutor's own escalation
  logic handles the retry semantics (it always did).

- **Per-process `tools:updated` publish.** With N App Service workers,
  the framework will receive N `tools:updated` events at startup. The
  framework's registry refresh is idempotent — harmless, just worth knowing
  if you're chasing duplicate-event logs.

- **`AsyncRedisClient` init failure is non-fatal at startup** but
  async-job tools will throw on every call until Redis is reachable. The
  failure surface mirrors the existing `_publish_tools_updated` behaviour.

- **Selenium concurrency.** Selenium WebDriver calls are synchronous and
  block the asyncio loop. Keep `JOB_WORKER_CONCURRENCY=1` for browser
  automation. (If you ever queue non-Selenium tools, you can bump it for
  those.)

- **`FastMCP(lifespan=...)` does nothing under streamable-http.** This is
  a sharp edge worth knowing. The `lifespan=` argument to `FastMCP(...)`
  is wired into the low-level MCPServer, which only the stdio transport
  uses. Under streamable-http, FastMCP builds a Starlette app whose
  lifespan is **hardcoded** to its session manager — your lifespan is
  silently ignored. **Symptom:** no startup logs, Redis never initialised,
  `JobWorker` never launched, async tools enqueue forever with nothing
  consuming. **Fix (already in [app.py](../app.py)):** call
  `mcp.streamable_http_app()` to get the Starlette app, replace its
  `router.lifespan_context` with one that chains `_server_lifespan` onto
  the original session-manager lifespan, and serve via gunicorn/uvicorn
  pointed at the module-level `app` callable. If you add new startup
  work, add it to `_server_lifespan` — don't try passing it to
  `FastMCP(...)` and don't run via `mcp.run(transport=...)`.

- **`mcp.run(transport="streamable-http")` is the wrong production entry
  point.** It calls `mcp.streamable_http_app()` itself and uvicorn-serves
  the result — but that's the *raw* Starlette app with only the session
  manager's lifespan. Our `_server_lifespan` is never entered. Always
  launch via the module-level `app = _build_asgi_app()` callable (see
  the "Production launch" section below).

### Production launch (gunicorn + uvicorn workers)

```bash
gunicorn app:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers 2 \
  --timeout 120 \
  --graceful-timeout 120 \
  --bind 0.0.0.0:8000
```

With `JOB_WORKER_CONCURRENCY=1` in the App Service env, this gives 2
workers × 1 = 2 concurrent in-flight jobs, matching the LambdaTest seat
count. `--graceful-timeout 120` gives in-flight jobs a chance to finish
during a redeploy before gunicorn force-kills the worker.

**Things to watch in the logs on startup** (per gunicorn worker, so you
will see each line twice with `--workers 2`):

```
Starting MCP ToolServer with N registered tools.
AsyncRedisClient ready: <redis-host>:6379, db 0
Published tools:updated event (N tools).
MCP ToolServer startup complete.
Background JobWorker launched.
JobWorker started (concurrency=1, queue=jobs_queue).
```

If those lines don't appear, the chained lifespan isn't firing — most
likely cause is gunicorn pointing at a stale module path or the deploy
not including the latest `app.py`. Run with `--log-level debug` to surface
import errors.

---

## Troubleshooting & inspection

There is no dashboard or HTTP endpoint for inspecting jobs today. Use
`redis-cli` (or any Redis client — RedisInsight is a good GUI option)
against the same Redis instance the server uses. All of the following are
read-only.

### Common diagnostic queries

| Question | Command |
|---|---|
| How many jobs are waiting to start? | `LLEN jobs_queue` |
| Peek at the next jobs without consuming them | `LRANGE jobs_queue 0 9` |
| Show the full state of one job | `HGETALL job:<uuid>` |
| Just the status of one job | `HGET job:<uuid> status` |
| List every known job (use with care) | `SCAN 0 MATCH job:* COUNT 200` |
| Find currently-processing jobs (manual scan) | `SCAN 0 MATCH job:* COUNT 200` then `HGET <key> status` for each |
| Time the job has been queued/processing | compare `created_at` / `started_at` against now |
| When does a job hash expire? | `TTL job:<uuid>` (seconds remaining; 24h default) |

> Avoid `KEYS job:*` against a production Redis — it blocks the server.
> Always use `SCAN` for ad-hoc listing.

### Real-time tap on completions

Because `mark_terminal` publishes to `job_result:{id}` (see "Why we publish
to a channel nothing currently subscribes to"), you can subscribe from a
shell to watch jobs finish live:

```bash
redis-cli PSUBSCRIBE 'job_result:*'
```

Each completion will print the result JSON. Useful for "is my worker
actually processing?" sanity checks.

### Common failure modes & what to look for

**"My PlanExecutor call timed out after 30 minutes."**
- Check `HGET job:<uuid> status`.
- If `processing`: the worker picked it up but the tool itself hung.
  Compare `started_at` to now; if it's stale, the App Service worker
  probably recycled mid-job. The job will sit at `processing` until the
  24 h TTL expires. (See known limitations — a stale-processing reaper
  is on the wish list.)
- If `queued`: no worker is consuming. Confirm the MCP server is actually
  running and check its logs for `JobWorker started (concurrency=N, ...)`.
  If that line is missing, `AsyncRedisClient.init()` probably failed at
  startup — check for the warning `AsyncRedisClient init failed: ...`.
- If `failed`: read `HGET job:<uuid> result` for the error.
- If the key doesn't exist: TTL expired (>24 h since the job ran) or it
  was never enqueued.

**"The queue keeps growing — workers are falling behind."**
- `LLEN jobs_queue` rising over time confirms the symptom.
- Check how many jobs are stuck in `processing` by scanning. If many,
  workers are blocked on slow tools (Selenium hanging on a missing
  element is the usual culprit).
- LambdaTest seat exhaustion: real concurrency is
  `app_service_workers × JOB_WORKER_CONCURRENCY`. Exceed 2 and LambdaTest
  queues jobs server-side, slowing each one down.

**"A specific tool always fails."**
- Pull a recent failed job for that tool:
  `SCAN` for `job:*`, filter where `tool_name == <name>` and
  `status == failed`, then `HGET job:<uuid> result` for the error.
- The result JSON also typically includes `public_url` / `video_url`
  from LambdaTest when applicable — open the video to replay the run.

### Manually re-running a job

There's no built-in retry. To re-run a failed job by hand:

```bash
# 1. Get the original params
redis-cli HGET job:<uuid> params

# 2. Either call the tool again through the framework, or push a new
#    payload onto the queue directly. The latter only makes sense for
#    one-off ops work — normal retries should go through PlanExecutor
#    so escalation logic runs.
```

### Future: read-only HTTP endpoints

If `redis-cli` becomes the daily debugging path, a small set of read-only
HTTP routes on the MCP server (e.g. `GET /jobs/{id}`, `GET /jobs?status=`)
would be the next logical step. The implementation is straightforward —
`read_job` already exists — but the endpoints would need auth gating
(job params can contain employee IDs and similar), a status-set index
for efficient listing, and a redaction layer for sensitive fields. Not
built yet; flag it if you find yourself wanting it.

---

## How to add a new async tool

1. Subclass `BaseTool` as usual.
2. Set `is_async_job = True` on the class.
3. Register it in `PROJECT_TOOLS` in [app.py](../app.py).
4. That's it. The dispatch wrapper, queue, worker, and polling protocol
   pick it up automatically.

---

## Testing locally

You need a Redis instance available — `docker run -p 6379:6379 redis:7`
will do. Then:

1. Start the MCP server: `python app.py`. You should see
   `JobWorker started (concurrency=2, queue=jobs_queue)` in the logs.
2. From the framework, call any async tool through `PlanExecutor`. In the
   server logs you should see:
   - `Tool '<name>' enqueued as job <uuid>; client will poll
     job_status_tool every 5s ...`
   - `JobWorker executing job <uuid> (tool=<name>).`
   - `JobWorker job <uuid> finished with status=succeeded.`
3. Inspect job state in Redis: `redis-cli HGETALL job:<uuid>`.
4. Inspect the queue depth: `redis-cli LLEN jobs_queue`.

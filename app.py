import json
import os
import asyncio
import inspect
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional

import uvicorn
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from tools.examples import (
    FindEmployeeTool,
    GetLeaveBalanceTool,
    GenerateLeaveReportTool,
)

from tool_logger import logger
from tool_redis import AsyncRedisClient

from jobs import JobWorker, enqueue_job
from tools.common import JobStatusTool

load_dotenv()

# ---------------------------------------------------------------------------
# Tool registry — add new tools here, nothing else needs to change
# ---------------------------------------------------------------------------
PROJECT_TOOLS = {
    "common": [
        JobStatusTool(),
    ],
    "hr_demo": [
        FindEmployeeTool(),
        GetLeaveBalanceTool(),
        GenerateLeaveReportTool(),
    ],
}

# ---------------------------------------------------------------------------
# Build a flat lookup: tool_name -> (tool_instance, client)
# ---------------------------------------------------------------------------
_TOOL_LOOKUP: dict = {}
for _client, _tools in PROJECT_TOOLS.items():
    for _tool in _tools:
        if _tool.name in _TOOL_LOOKUP:
            logger.warning(
                f"Duplicate tool name '{_tool.name}' detected; overriding previous registration."
            )
        _TOOL_LOOKUP[_tool.name] = (_tool, _client)

_STARTUP_TASKS_RAN = False

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
_worker_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def _server_lifespan(_: MCPServer):
    global _worker_task
    await _run_startup_tasks_once()
    _worker_task = asyncio.create_task(JobWorker(tool_lookup=_TOOL_LOOKUP).run())
    logger.info("Background JobWorker launched.")
    try:
        yield
    except Exception:
        logger.exception("MCP ToolServer encountered an error during lifespan execution.")
        raise
    finally:
        if _worker_task is not None and not _worker_task.done():
            logger.info("Cancelling JobWorker on shutdown.")
            _worker_task.cancel()
            try:
                await _worker_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("MCP ToolServer has stopped.")


mcp = MCPServer("ToolServer")
# NOTE: transport-specific params (host/port/paths) live on run() and
# streamable_http_app() in mcp 2.x, not on the constructor. We serve the ASGI app
# directly via uvicorn/gunicorn, so host/port are applied at that layer instead.


# ---------------------------------------------------------------------------
# Resource: tool manifest (discovery)
# Clients call mcp://tools to get the full list with metadata + client tag.
# Optional query param ?client=<name> filters to a specific client.
# ---------------------------------------------------------------------------
@mcp.resource("tools://manifest")
def tool_manifest() -> str:
    """
    Returns the full tool manifest as JSON.
    Each entry includes the tool schema plus a 'client' field indicating
    which client/project the tool belongs to.
    """
    manifest = [
        {**tool.get_tool_metadata(), "client": client}
        for tool, client in _TOOL_LOOKUP.values()
    ]
    return json.dumps(manifest, indent=2)


@mcp.resource("tools://manifest/{client}")
def tool_manifest_by_client(client: str) -> str:
    """
    Returns the tool manifest filtered to a specific client.
    E.g. tools://manifest/hr_demo
    """
    manifest = [
        {**tool.get_tool_metadata(), "client": c}
        for tool, c in _TOOL_LOOKUP.values()
        if c == client
    ]
    return json.dumps(manifest, indent=2)


# ---------------------------------------------------------------------------
# Dynamically register every tool as an MCP callable tool
# ---------------------------------------------------------------------------
_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _signature_from_schema(schema: dict) -> inspect.Signature:
    """
    Build a typed signature from a tool's JSON Schema.

    MCPServer infers both the advertised inputSchema and its argument
    validation from the handler's signature. A bare **kwargs handler is
    advertised as a single required "kwargs" field, so standard clients that
    send the real arguments get rejected. Giving the handler a signature that
    mirrors the tool's own schema fixes both.
    """
    required = set(schema.get("required", []))
    params = []
    for prop_name, prop in schema.get("properties", {}).items():
        annotation = _JSON_TYPES.get(prop.get("type"), Any)
        if prop.get("description"):
            annotation = Annotated[annotation, Field(description=prop["description"])]
        if prop_name in required:
            params.append(inspect.Parameter(prop_name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation))
        else:
            params.append(inspect.Parameter(
                prop_name, inspect.Parameter.KEYWORD_ONLY,
                annotation=Optional[annotation], default=None,
            ))
    return inspect.Signature(params, return_annotation=dict)


def _make_handler(tool_instance, client: str):
    """
    Returns an async handler closure bound to a specific tool instance.
    The handler accepts **kwargs and delegates to the tool's own execute(),
    which already knows its schema; _signature_from_schema gives it a typed
    signature so MCPServer advertises and validates the real parameters.
    """
    async def _handler(**kwargs) -> dict:
        params = dict(kwargs)

        # Optional parameters the caller left out arrive as None from the typed
        # signature; drop them so execute() falls back to its own defaults.
        params = {k: v for k, v in params.items() if v is not None}

        start_time = time.perf_counter()
        logger.info(
            f"Executing tool '{tool_instance.name}' (client={client}) with param_keys={sorted(params.keys())}"
        )

        if getattr(tool_instance, "is_async_job", False):
            try:
                job_id = await enqueue_job(
                    tool_name=tool_instance.name,
                    client=client,
                    params=params,
                )
            except Exception:
                logger.exception(
                    f"Tool '{tool_instance.name}' failed to enqueue async job."
                )
                raise

            poll_interval = int(os.getenv("JOB_POLL_INTERVAL_SECONDS", "5"))
            poll_timeout = int(os.getenv("JOB_POLL_TIMEOUT_SECONDS", "1800"))
            logger.info(
                f"Tool '{tool_instance.name}' enqueued as job {job_id}; "
                f"client will poll job_status_tool every {poll_interval}s (timeout {poll_timeout}s)."
            )
            return {
                "_poll": {
                    "tool": "job_status_tool",
                    "params": {"job_id": job_id},
                    "interval_seconds": poll_interval,
                    "timeout_seconds": poll_timeout,
                },
                "job_id": job_id,
                "status": "queued",
            }

        try:
            result = await tool_instance.execute(**params)
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                f"Tool '{tool_instance.name}' completed successfully in {elapsed_ms:.2f}ms."
            )
            return result
        except Exception:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.exception(
                f"Tool '{tool_instance.name}' failed after {elapsed_ms:.2f}ms with params={params}"
            )
            raise

    _handler.__name__ = tool_instance.name
    _handler.__doc__ = tool_instance.description
    _handler.__signature__ = _signature_from_schema(tool_instance._get_parameters_schema())
    return _handler


for _tool_name, (_tool_instance, _client) in _TOOL_LOOKUP.items():
    try:
        _schema = _tool_instance._get_parameters_schema()
        mcp.add_tool(
            fn=_make_handler(_tool_instance, _client),
            name=_tool_name,
            description=_tool_instance.description,
            # Exposes the same steps/is_browser_automation/is_async_job/is_resolution_only
            # data already used by the tools://manifest resource, via the standard
            # Tool._meta field, so spec-compliant MCP clients (tools/list only, no
            # custom resource support) can read it without a second round trip.
            meta={
                "client": _client,
                "steps": _tool_instance.steps,
                "parameters_schema": _tool_instance._get_parameters_schema(),
                "is_browser_automation": _tool_instance.is_browser_automation,
                "is_async_job": _tool_instance.is_async_job,
                "is_resolution_only": _tool_instance.is_resolution_only,
                "require_approval": getattr(_tool_instance, "require_approval", True),
            },
        )
        logger.info(
            f"Registered MCP tool: {_tool_name} (client={_client}, schema_keys={list(_schema.keys())})"
        )
    except Exception:
        logger.exception(f"Failed to register MCP tool: {_tool_name} (client={_client})")
        raise


# ---------------------------------------------------------------------------
# Startup: publish tools:updated event to Redis so the agent server can refresh
# ---------------------------------------------------------------------------
async def _publish_tools_updated():
    """
    Publish a tools:updated event to Redis on startup.
    The agent server subscribes to this channel and refreshes its ToolRegistry.

    Uses the AsyncRedisClient singleton initialised in _init_async_redis so
    we share the same connection pool as the job queue + worker.
    """
    try:
        redis = await AsyncRedisClient.get()

        manifest = [
            {**tool.get_tool_metadata(), "client": client}
            for tool, client in _TOOL_LOOKUP.values()
        ]

        payload = json.dumps({
            "event": "tools:updated",
            "server": os.getenv("MCP_SERVER_URL", "http://localhost:8000"),
            "tools": manifest,
        })

        await redis.publish("tools:updated", payload)
        logger.info(f"Published tools:updated event ({len(_TOOL_LOOKUP)} tools).")

    except Exception as exc:
        # Non-fatal — the agent server can still pull on demand via the resource endpoint
        logger.warning(f"Could not publish tools:updated to Redis: {exc}")


async def _init_async_redis():
    """Initialise the AsyncRedisClient singleton used by the job queue and worker."""
    try:
        await AsyncRedisClient.init(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 0)),
            username=os.getenv("REDIS_USERNAME"),
            password=os.getenv("REDIS_PASSWORD"),
        )
    except Exception as exc:
        logger.warning(
            f"AsyncRedisClient init failed: {exc}. Async-job tools will not be available."
        )


async def _run_startup_tasks_once():
    global _STARTUP_TASKS_RAN

    if _STARTUP_TASKS_RAN:
        logger.debug("Startup tasks already executed; skipping.")
        return

    logger.info(f"Starting MCP ToolServer with {len(_TOOL_LOOKUP)} registered tools.")
    await _init_async_redis()
    await _publish_tools_updated()
    _STARTUP_TASKS_RAN = True
    logger.info("MCP ToolServer startup complete.")

# ---------------------------------------------------------------------------
# ASGI entry point
# ---------------------------------------------------------------------------
def _build_asgi_app():
    """
    Build the Starlette ASGI app for streamable-http, with our startup tasks
    composed around the session-manager lifespan.

    In mcp 2.x the app returned by `streamable_http_app()` does NOT start the
    session manager itself — the host app's lifespan must enter
    `mcp.session_manager.run()`, or every request to the mounted path fails with
    `RuntimeError: Task group is not initialized`.

    The session manager's lifespan is entered once per run and its state shared
    across all sessions, so `_server_lifespan` (JobWorker + Redis) runs exactly
    once outside that scope.

    Called at module import time so `app` is a module-level ASGI callable
    suitable for `gunicorn app:app` / `uvicorn app:app`.
    """
    starlette_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def chained_lifespan(_app):
        async with _server_lifespan(mcp):
            async with mcp.session_manager.run():
                yield

    starlette_app.router.lifespan_context = chained_lifespan
    return starlette_app


# Module-level ASGI callable for production servers (gunicorn, uvicorn).
#   gunicorn app:app --worker-class uvicorn.workers.UvicornWorker --workers 2 ...
#   uvicorn  app:app --host 0.0.0.0 --port 8000
app = _build_asgi_app()


if __name__ == "__main__":
    try:
        logger.info("Launching MCP server with streamable-http transport (uvicorn).")
        uvicorn.run(
            app,
            host=os.getenv("MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("MCP_PORT", 8000)),
            log_level=os.getenv("MCP_LOG_LEVEL", "info").lower(),
        )
    except Exception:
        logger.exception("MCP server startup failed.")
        raise
"""
JobWorker — in-process consumer of ``jobs_queue``.

Runs as an asyncio Task inside the MCP server's lifespan. Each iteration:
  1. BLPOP a job payload from ``jobs_queue``.
  2. Mark the job processing.
  3. Look the tool up in the MCP server's _TOOL_LOOKUP and run ``execute``.
  4. Mark the job succeeded/failed; publish to job_result:{id}.

Concurrency is bounded by JOB_WORKER_CONCURRENCY. Each in-flight job runs as
its own asyncio Task so a slow Selenium job doesn't block the BLPOP loop.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from tool_logger import logger
from tool_redis import AsyncRedisClient
from base_tool import BaseTool

from jobs.queue import JOBS_QUEUE_KEY, mark_processing, mark_terminal


ToolLookup = Dict[str, Tuple[BaseTool, str]]


class JobWorker:
    """Pulls jobs from Redis and executes them against the in-process tool lookup."""

    def __init__(
        self,
        tool_lookup: ToolLookup,
        concurrency: Optional[int] = None,
        blpop_timeout_seconds: int = 5,
    ):
        self._tool_lookup = tool_lookup
        self._concurrency = concurrency or int(os.getenv("JOB_WORKER_CONCURRENCY", "2"))
        self._blpop_timeout = blpop_timeout_seconds
        self._sem = asyncio.Semaphore(self._concurrency)
        self._in_flight: set[asyncio.Task] = set()
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        """Main loop. Cancel the surrounding Task to stop."""
        logger.info(
            f"JobWorker started (concurrency={self._concurrency}, "
            f"queue={JOBS_QUEUE_KEY})."
        )
        try:
            redis = await AsyncRedisClient.get()
        except Exception as exc:
            logger.error(f"JobWorker could not connect to Redis: {exc}. Exiting.")
            return

        try:
            while not self._stopped.is_set():
                try:
                    popped = await redis.blpop(JOBS_QUEUE_KEY, timeout=self._blpop_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(f"JobWorker BLPOP failed: {exc}. Retrying after 1s.")
                    await asyncio.sleep(1)
                    continue

                if not popped:
                    continue

                _, payload = popped
                try:
                    job = json.loads(payload)
                except (TypeError, ValueError) as exc:
                    logger.error(f"JobWorker dropping malformed job payload: {exc}")
                    continue

                await self._sem.acquire()
                task = asyncio.create_task(self._run_job(job))
                self._in_flight.add(task)
                task.add_done_callback(self._on_job_done)
        except asyncio.CancelledError:
            logger.info("JobWorker run loop cancelled. Waiting for in-flight jobs.")
            raise
        finally:
            if self._in_flight:
                await asyncio.gather(*self._in_flight, return_exceptions=True)
            logger.info("JobWorker stopped.")

    def _on_job_done(self, task: asyncio.Task) -> None:
        self._in_flight.discard(task)
        self._sem.release()

    async def _run_job(self, job: Dict[str, Any]) -> None:
        job_id = job.get("id")
        tool_name = job.get("tool_name")
        params = job.get("params") or {}

        if not job_id or not tool_name:
            logger.error(f"JobWorker dropping job with missing id/tool_name: {job}")
            return

        entry = self._tool_lookup.get(tool_name)
        if not entry:
            logger.error(f"JobWorker: tool '{tool_name}' not found in lookup. Failing job {job_id}.")
            await mark_terminal(job_id, "failed", {
                "success": False,
                "error": f"Tool '{tool_name}' not registered on this server.",
            })
            return

        tool, _client = entry

        try:
            await mark_processing(job_id)
        except Exception as exc:
            logger.warning(f"JobWorker could not mark job {job_id} processing: {exc}")

        logger.info(f"JobWorker executing job {job_id} (tool={tool_name}).")

        try:
            result = await tool.execute(**params)
            if not isinstance(result, dict):
                result = {"success": True, "value": result}
            status = "succeeded" if result.get("success", True) else "failed"
        except Exception as exc:
            logger.exception(f"JobWorker: tool '{tool_name}' raised on job {job_id}")
            result = {"success": False, "error": str(exc)}
            status = "failed"

        try:
            await mark_terminal(job_id, status, result)
            logger.info(f"JobWorker job {job_id} finished with status={status}.")
        except Exception as exc:
            logger.exception(f"JobWorker could not record terminal state for {job_id}: {exc}")

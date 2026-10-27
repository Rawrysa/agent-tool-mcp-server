"""
job_status_tool — reports the status of a previously-enqueued async job.

This is the tool that ``_poll`` directives point to. The framework's
``MCPClient._drive_poll`` calls it repeatedly until ``all_complete=true``,
then returns the response verbatim to the PlanExecutor. To keep that
transparent, terminal responses merge the executed tool's result onto the
top level (so callers see the tool's own fields, e.g. ``totals``, directly).
"""
from __future__ import annotations

from typing import Any, Dict

from tool_logger import logger
from base_tool import BaseTool

from jobs.queue import read_job


_TERMINAL_STATES = {"succeeded", "failed"}


class JobStatusTool(BaseTool):
    name = "job_status_tool"
    description = (
        "Polls the status of an asynchronous job previously enqueued by the MCP server. "
        "Returns all_complete=true once the job has succeeded or failed; the underlying "
        "tool's result fields are merged onto the response on completion."
    )
    steps = [
        "1. Look up job:{job_id} in Redis",
        "2. Return current status (queued, processing, succeeded, failed)",
        "3. On terminal status, merge the tool's result onto the response",
    ]
    is_browser_automation = False
    is_async_job = False  # the status tool itself must run inline
    require_approval = False  # read-only poll, never a plan step that needs sign-off

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "UUID returned when the job was enqueued."},
            },
            "required": ["job_id"],
        }

    async def execute(self, job_id: str) -> Dict[str, Any]:
        try:
            job = await read_job(job_id)
        except Exception as exc:
            logger.exception(f"job_status_tool failed reading job {job_id}")
            return {
                "all_complete": True,
                "success": False,
                "status": "failed",
                "error": f"Could not read job status: {exc}",
                "job_id": job_id,
            }

        if job is None:
            return {
                "all_complete": True,
                "success": False,
                "status": "unknown",
                "error": f"Job {job_id} not found (expired or never existed).",
                "job_id": job_id,
            }

        status = job.get("status", "unknown")
        response: Dict[str, Any] = {
            "job_id": job_id,
            "status": status,
            "tool_name": job.get("tool_name"),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "all_complete": status in _TERMINAL_STATES,
        }

        if status in _TERMINAL_STATES:
            result = job.get("result") or {}
            if isinstance(result, dict):
                response = {**result, **response}
                response["success"] = result.get("success", status == "succeeded")
            else:
                response["result"] = result
                response["success"] = status == "succeeded"

        return response

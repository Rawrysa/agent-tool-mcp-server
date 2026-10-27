"""
generate_leave_report_tool — slow tool that runs as an async job.

Stands in for the long-running browser-automation tools that were timing
out behind Azure's load balancer. Because ``is_async_job`` is True, the
server enqueues it and immediately returns a ``_poll`` envelope; the
JobWorker runs it in the background and the client polls job_status_tool.
DEMO_REPORT_SECONDS controls how long it pretends to work.
"""
import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict

from base_tool import BaseTool
from tool_logger import logger
from tools.examples.demo_data import EMPLOYEES, LEAVE_BALANCES


class GenerateLeaveReportTool(BaseTool):
    name = "generate_leave_report_tool"
    description = (
        "Builds a leave report for all active employees. Slow, so it runs as a background "
        "job: the call returns a job_id and the result is fetched with job_status_tool."
    )
    steps = [
        "1. Collect leave balances for every active employee",
        "2. Total each leave type across the organisation",
        "3. Flag employees below the low-balance threshold",
    ]
    is_async_job = True
    require_approval = False

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "low_balance_threshold": {
                    "type": "number",
                    "description": "Annual-leave days below which an employee is flagged. Defaults to 10.",
                },
            },
            "required": [],
        }

    async def execute(self, low_balance_threshold: float = 10) -> Dict[str, Any]:
        seconds = float(os.getenv("DEMO_REPORT_SECONDS", "15"))
        logger.info(f"generate_leave_report_tool working for {seconds:.0f}s (simulated).")
        await asyncio.sleep(seconds)

        active_ids = [e["employee_id"] for e in EMPLOYEES if e["active"]]
        totals: Dict[str, float] = {}
        for employee_id in active_ids:
            for leave_type, days in LEAVE_BALANCES.get(employee_id, {}).items():
                totals[leave_type] = totals.get(leave_type, 0.0) + days

        low_balance = [
            employee_id for employee_id in active_ids
            if LEAVE_BALANCES.get(employee_id, {}).get("annual", 0.0) < low_balance_threshold
        ]

        result = {
            "success": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "employees": len(active_ids),
            "totals": totals,
            "low_annual_balance": low_balance,
        }
        self.log_execution({"low_balance_threshold": low_balance_threshold}, result, True)
        return result

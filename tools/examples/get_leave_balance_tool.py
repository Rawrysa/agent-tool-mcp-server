"""
get_leave_balance_tool — fast tool that runs inline.

Its ``steps`` end with an input-resolution playbook: plain-language
instructions the agent's input resolver follows to derive inputs the user
didn't give, using find_employee_tool. The real tools carried the same kind
of playbook, written against the HR system's own lookups.
"""
from typing import Any, Dict

from base_tool import BaseTool
from tool_logger import logger
from tools.examples.demo_data import EMPLOYEES, LEAVE_BALANCES


class GetLeaveBalanceTool(BaseTool):
    name = "get_leave_balance_tool"
    description = "Returns an employee's remaining leave days, for one leave type or all of them."
    steps = [
        "1. Validate the employee_id belongs to an active employee",
        "2. Read the employee's leave balances",
        "3. Return the requested leave type, or all types if none was given",
        # ── Input-resolution playbook (used by the InputResolver to derive any of ──
        # ── the required inputs that the user did not supply directly) ─────────────
        "RESOLVE employee_id: If not provided, derive it from the requester's own "
        "identifying details in the known data. Call find_employee_tool with their "
        "email first; if there is no email, use their last name and first name. "
        "Accept only if match_count is exactly 1; otherwise leave employee_id "
        "unresolved so the user can be asked. Never guess between several matches.",
    ]
    require_approval = False

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string", "description": "Employee ID, e.g. E1001."},
                "leave_type": {
                    "type": "string",
                    "enum": ["annual", "sick", "family"],
                    "description": "Leave type to report. Omit to return all types.",
                },
            },
            "required": ["employee_id"],
        }

    async def execute(self, employee_id: str, leave_type: str = None) -> Dict[str, Any]:
        params = {"employee_id": employee_id, "leave_type": leave_type}

        employee = next((e for e in EMPLOYEES if e["employee_id"] == employee_id and e["active"]), None)
        if employee is None:
            result = {"success": False, "error": f"No active employee with id '{employee_id}'."}
            self.log_execution(params, result, False)
            return result

        balances = LEAVE_BALANCES.get(employee_id, {})
        if leave_type:
            balances = {leave_type: balances.get(leave_type, 0.0)}

        logger.debug(f"get_leave_balance_tool read {len(balances)} balance(s) for {employee_id}.")
        result = {
            "success": True,
            "employee_id": employee_id,
            "employee_name": f"{employee['first_name']} {employee['last_name']}",
            "balances": balances,
        }
        self.log_execution(params, result, True)
        return result

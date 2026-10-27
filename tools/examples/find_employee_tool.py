"""
find_employee_tool — resolution-only lookup.

Marked ``is_resolution_only`` so the agent's input resolver may call it to
fill in a missing ``employee_id``, but the planner never puts it in a plan.
Mirrors the guard rails the real lookups used: inactive people are never
returned, and the caller should only accept an exactly-one-row result.
"""
from typing import Any, Dict

from base_tool import BaseTool
from tool_logger import logger
from tools.examples.demo_data import EMPLOYEES


class FindEmployeeTool(BaseTool):
    name = "find_employee_tool"
    description = (
        "Looks up active employees by email or by first and last name. Used to resolve "
        "an employee_id the user did not supply. Returns every match; callers should "
        "accept the result only if exactly one employee is returned."
    )
    steps = [
        "1. Match active employees on email (exact, case-insensitive) if given",
        "2. Otherwise match on last name (exact) and first name (contains)",
        "3. Return all matches with their employee_id",
    ]
    is_resolution_only = True
    require_approval = False

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "Employee email address."},
                "first_name": {"type": "string", "description": "Employee first name (partial match)."},
                "last_name": {"type": "string", "description": "Employee last name (exact match)."},
            },
            "required": [],
        }

    async def execute(self, email: str = None, first_name: str = None, last_name: str = None) -> Dict[str, Any]:
        active = [e for e in EMPLOYEES if e["active"]]

        if email:
            matches = [e for e in active if e["email"].lower() == email.lower()]
        elif last_name:
            matches = [
                e for e in active
                if e["last_name"].lower() == last_name.lower()
                and (not first_name or first_name.lower() in e["first_name"].lower())
            ]
        else:
            return {"success": False, "error": "Provide an email, or a last name (optionally with a first name)."}

        logger.debug(f"find_employee_tool returned {len(matches)} match(es).")
        result = {
            "success": True,
            "match_count": len(matches),
            "matches": [{k: e[k] for k in ("employee_id", "first_name", "last_name", "email")} for e in matches],
        }
        self.log_execution({"email": email, "first_name": first_name, "last_name": last_name}, result, True)
        return result

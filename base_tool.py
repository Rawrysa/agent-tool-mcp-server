"""
Local BaseTool — no framework dependency.
All tools in the MCP server import from here.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List


class BaseTool(ABC):
    """Base class for all MCP server tools"""
    name: str
    description: str
    steps: List[str]
    is_browser_automation: bool = False
    # True for slow tools: the server enqueues them and returns a _poll envelope
    is_async_job: bool = False
    # True for lookup tools the agent may call while resolving inputs, never as a plan step
    is_resolution_only: bool = False
    # True if a human must approve a plan that contains this tool
    require_approval: bool = True

    def __init__(self):
        self.tool_name = self.__class__.__name__
        self.execution_history: List[Dict] = []

    @abstractmethod
    def _get_parameters_schema(self) -> Dict[str, Any]:
        """Return the JSON Schema for this tool's parameters"""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute the tool with given parameters"""
        pass

    def get_tool_metadata(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "steps": self.steps,
                "parameters": self._get_parameters_schema(),
                "is_browser_automation": self.is_browser_automation,
                "is_async_job": self.is_async_job,
                "is_resolution_only": self.is_resolution_only,
                "require_approval": self.require_approval,
            },
        }

    def log_execution(self, params: Dict[str, Any], result: Any, success: bool):
        self.execution_history.append({
            "timestamp": datetime.now().isoformat(),
            "params": params,
            "result": result,
            "success": success,
        })

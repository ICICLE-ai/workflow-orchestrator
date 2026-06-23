from typing import Any

from pydantic import BaseModel


class WorkflowConfig(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]

"""Model-facing schemas and dispatch, independent of storage and model provider."""
import json
from pathlib import Path
from typing import Callable

from quant_rsi.application.history import PointInTimeHistory
from quant_rsi.domain.shared import require


def history_tool_schemas():
    return json.loads(Path(__file__).with_name("history-tools.v1.json").read_text())["tools"]


class HistoryTools:
    def __init__(self, view: PointInTimeHistory, capture: Callable[[dict], None] | None = None):
        self.view, self.capture = view, capture

    def invoke(self, name: str, arguments: dict):
        from jsonschema import Draft202012Validator

        spec = next((spec for spec in history_tool_schemas() if spec["name"] == name), None)
        require(spec is not None, "unknown history tool")
        require(type(arguments) is dict, "tool arguments must be an object")
        Draft202012Validator(spec["inputSchema"]).validate(arguments)
        try:
            result = (self.view.search(**arguments) if name == "search_history" else self.view.read(**arguments))
        except Exception as error:
            if self.capture:
                self.capture({"tool": name, "arguments": arguments, "error": str(error)})
            raise
        if self.capture:
            self.capture({"tool": name, "arguments": arguments, "result": result})
        return result

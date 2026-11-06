"""
oracle_llm.py
=============
A fake "LLM" that always does the right thing for a test case: it makes the
reference tool calls and then repeats what the tools returned.

It exists only for the self-test  `python -m evaluation.run_evaluation --oracle`.
If the oracle scores 100%, the test sheet, the tools, the agent loop and the
grader all agree with each other, so any lower score with a real model comes
from the model, not from a bug in the test setup. No Groq API call is needed.
"""
from typing import Any, List

from langchain_core.messages import AIMessage, ToolMessage


def render(obj: Any) -> List[str]:
    """Flatten tool output into plain text lines."""
    lines: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("eligible", "decision"):   # raw flags would read like a verdict; the message says it in words
                continue
            if isinstance(v, (dict, list)):
                lines.append(f"{k}:")
                lines.extend(render(v))
            else:
                lines.append(f"{k}: {v}")
    elif isinstance(obj, list):
        for item in obj:
            lines.extend(render(item))
            lines.append("-")
    else:
        lines.append(str(obj))
    return lines


class OracleLLM:
    model = "oracle (no LLM)"

    def __init__(self, case):
        self.case = case

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results and self.case.refs:
            calls = [{"name": n, "args": a, "id": f"ref_{i}", "type": "tool_call"}
                     for i, (n, a) in enumerate(self.case.refs)]
            return AIMessage(content="", tool_calls=calls)
        if self.case.ref_answer:
            return AIMessage(content=self.case.ref_answer)
        return AIMessage(content="\n".join(line for m in results for line in render(_loads(m.content))))


def _loads(text):
    import json
    try:
        return json.loads(text)
    except ValueError:
        return text

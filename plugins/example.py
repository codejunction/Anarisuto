"""Example plugin. Every plugins/*.py file must define register(harness).

A plugin can:
  harness.tools.append(tool)             give the researcher another tool
  harness.kb.loaders[".ext"] = fn        teach ingestion a file type: fn(path) -> text
  harness.mcp_cfg["name"] = {...}        add an MCP server (same shape as config.yaml)
  harness.subagents.append({...})        add a sub-agent: name, role, goal, prompt
"""
import datetime

from crewai.tools import tool


@tool("today")
def today() -> str:
    """Return today's date. Use it to resolve phrases like 'last quarter'."""
    return datetime.date.today().isoformat()


def register(harness):
    harness.tools.append(today)

#!/usr/bin/env python3
"""Agent message formatting and logging with ANSI colors."""

import json
import logging

from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

logger = logging.getLogger(__name__)

AGENT_COLORS = [
    "\033[91m", "\033[92m", "\033[93m", "\033[94m", "\033[95m", "\033[96m",
    "\033[31m", "\033[32m", "\033[33m", "\033[34m", "\033[35m", "\033[36m",
]
RESET = "\033[0m"


def agent_color(run_id: str) -> str:
    """Return a deterministic ANSI color for a given run ID."""
    return AGENT_COLORS[hash(run_id) % len(AGENT_COLORS)]


def tool_summary(block: ToolUseBlock) -> str:
    """Format a one-line summary of a tool call."""
    inp = json.dumps(block.input, ensure_ascii=False)
    if len(inp) > 200:
        inp = inp[:197] + "..."
    return f"{block.name}: {inp}"


def log_blocks(message: AssistantMessage, prefix: str = "", suffix: str = "") -> None:
    """Log TextBlock and ToolUseBlock content from an AssistantMessage."""
    for block in message.content:
        if isinstance(block, TextBlock) and block.text.strip():
            logger.info("%s%s%s", prefix, block.text.strip()[:600], suffix)
        elif isinstance(block, ToolUseBlock):
            logger.info("%s%s%s", prefix, tool_summary(block), suffix)

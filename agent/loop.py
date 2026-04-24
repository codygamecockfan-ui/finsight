"""
Claude agent loop — streaming and non-streaming variants.
Both run tool calls in parallel via ThreadPoolExecutor.

v2: added critique pass. The agent drafts with tools, then a second pass
runs a self-critique using CRITIQUE_PROMPT before the final response streams.
The critique pass reuses the same model (no extra API cost beyond the one call)
and skips if the draft is trivially short (e.g. a greeting).
"""
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from anthropic import Anthropic

from agent.prompts import SYSTEM_PROMPT, CRITIQUE_PROMPT
from agent.tools import TOOLS
from agent.tool_handlers import run_tool

client = Anthropic()
MODEL  = "claude-sonnet-4-6"

# Skip critique pass for trivial responses to save latency
CRITIQUE_MIN_CHARS = 200
# Only critique responses that look like trade recommendations
CRITIQUE_TRIGGERS  = ("PLAY 1", "PLAY 2", "CONF ", "Counter-case", "0DTE", "Kalshi", "Polymarket")


def trim_history(history: list, max_pairs: int = 6) -> list:
    """Keep only the last N user/assistant pairs to reduce token overhead."""
    if len(history) <= max_pairs * 2:
        return history
    return history[-(max_pairs * 2):]


def _system_with_time() -> str:
    current_time = datetime.now().strftime("%Y-%m-%d %I:%M %p ET")
    return (SYSTEM_PROMPT +
            f"\n\n## CURRENT TIME\nThe current date and time is {current_time}. "
            "Always use this as your reference — never estimate or guess the time.")


def _run_tools_parallel(tool_blocks) -> list:
    tool_results = [None] * len(tool_blocks)

    def _run(idx_block):
        idx, block = idx_block
        print(f"[FinSight] Tool: {block.name} | Input: {block.input}")
        result = run_tool(block.name, block.input)
        return idx, {"type": "tool_result", "tool_use_id": block.id,
                     "content": result or json.dumps({"error": "Empty response"})}

    with ThreadPoolExecutor(max_workers=min(len(tool_blocks), 6)) as ex:
        for idx, tr in ex.map(_run, enumerate(tool_blocks)):
            tool_results[idx] = tr
    return tool_results


def _should_critique(draft: str) -> bool:
    """Only run critique pass if the response looks substantive or trade-oriented."""
    if len(draft) < CRITIQUE_MIN_CHARS:
        return False
    return any(trigger in draft for trigger in CRITIQUE_TRIGGERS)


def _critique_pass(draft: str, system_base: str) -> str:
    """
    Takes the draft response and returns a refined version after self-critique.
    Returns the original draft unchanged if the API call fails.
    """
    combined_system = system_base + "\n\n---\n\n" + CRITIQUE_PROMPT
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=combined_system,
            messages=[{
                "role": "user",
                "content": f"DRAFT RESPONSE TO CRITIQUE:\n\n{draft}"
            }]
        )
        refined = "\n".join(b.text for b in response.content if hasattr(b, "text"))
        return refined.strip() or draft
    except Exception as e:
        print(f"[FinSight] Critique pass failed, using draft: {e}")
        return draft


def run_agent(conversation_history: list) -> str:
    """Non-streaming agent — used internally."""
    messages = trim_history(conversation_history.copy())
    system   = _system_with_time()

    while True:
        response = client.messages.create(
            model=MODEL, max_tokens=2048, system=system, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            draft = "\n".join(b.text for b in response.content if hasattr(b, "text"))
            if _should_critique(draft):
                return _critique_pass(draft, system)
            return draft

        if response.stop_reason == "tool_use":
            tool_blocks  = [b for b in response.content if b.type == "tool_use"]
            tool_results = _run_tools_parallel(tool_blocks)
            messages.append({"role": "user", "content": tool_results})
        else:
            return "\n".join(b.text for b in response.content if hasattr(b, "text")) or "Unexpected error."


def run_agent_streaming(conversation_history: list):
    """
    Streaming agent — runs tool calls in parallel, optionally critiques, then streams final.
    Yields SSE chunks: data: {"text": "..."} or data: [DONE]
    """
    messages = trim_history(conversation_history.copy())
    system   = _system_with_time()

    while True:
        response = client.messages.create(
            model=MODEL, max_tokens=2048, system=system, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            draft = "\n".join(b.text for b in response.content if hasattr(b, "text"))

            # Optional critique pass for trade-oriented responses
            if _should_critique(draft):
                # Signal to UI that critique is running
                yield f"data: {json.dumps({'status': 'critiquing'})}\n\n"
                final_text = _critique_pass(draft, system)
            else:
                final_text = draft

            # Stream the final version word-by-word
            words = final_text.split(" ")
            chunk = ""
            for i, word in enumerate(words):
                chunk += word + (" " if i < len(words) - 1 else "")
                if len(chunk) >= 4:
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
                    chunk = ""
            if chunk:
                yield f"data: {json.dumps({'text': chunk})}\n\n"
            yield "data: [DONE]\n\n"
            return

        if response.stop_reason == "tool_use":
            tool_blocks  = [b for b in response.content if b.type == "tool_use"]
            tool_results = _run_tools_parallel(tool_blocks)
            messages.append({"role": "user", "content": tool_results})
        else:
            final_text = "\n".join(b.text for b in response.content if hasattr(b, "text")) or "Unexpected error."
            yield f"data: {json.dumps({'text': final_text})}\n\n"
            yield "data: [DONE]\n\n"
            return

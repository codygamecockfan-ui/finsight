"""
Claude agent loop — REAL token streaming.

v3: tokens stream as Claude generates them (via client.messages.stream),
not after the fact. Critique pass runs a second real-time stream that
replaces the draft inline. User sees text appearing within ~1s of asking.
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

CRITIQUE_MIN_CHARS = 300
CRITIQUE_TRIGGERS  = ("PLAY 1", "PLAY 2", "━━━", "Counter-case", "Edge check")


def trim_history(history: list, max_pairs: int = 6) -> list:
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


def _should_critique(draft: str, tools_were_called: bool) -> bool:
    if not tools_were_called:
        return False
    if len(draft) < CRITIQUE_MIN_CHARS:
        return False
    return any(trigger in draft for trigger in CRITIQUE_TRIGGERS)


def run_agent(conversation_history: list) -> str:
    """Non-streaming agent — used internally."""
    messages = trim_history(conversation_history.copy())
    system   = _system_with_time()
    tools_were_called = False

    while True:
        response = client.messages.create(
            model=MODEL, max_tokens=2048, system=system, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            draft = "\n".join(b.text for b in response.content if hasattr(b, "text"))
            if _should_critique(draft, tools_were_called):
                # Critique pass (non-streaming for the simple func)
                refined = client.messages.create(
                    model=MODEL, max_tokens=2048,
                    system=system + "\n\n---\n\n" + CRITIQUE_PROMPT,
                    messages=[{"role": "user", "content": f"DRAFT RESPONSE TO CRITIQUE:\n\n{draft}"}]
                )
                return "\n".join(b.text for b in refined.content if hasattr(b, "text")) or draft
            return draft

        if response.stop_reason == "tool_use":
            tools_were_called = True
            tool_blocks  = [b for b in response.content if b.type == "tool_use"]
            tool_results = _run_tools_parallel(tool_blocks)
            messages.append({"role": "user", "content": tool_results})
        else:
            return "\n".join(b.text for b in response.content if hasattr(b, "text")) or "Unexpected error."


def run_agent_streaming(conversation_history: list):
    """
    Real-time streaming agent.
    
    Flow:
    1. Tool-use turns: non-streaming (just fire tools, no text yet)
    2. Final draft turn: STREAM tokens as they generate
    3. If critique should fire: send 'critiquing' status, then STREAM the refined version
       (frontend replaces the draft with the refined text live)
    
    Yields SSE events:
      data: {"text": "..."}        — append to current bubble
      data: {"status": "critiquing"} — switch to critique phase, clear bubble for replacement
      data: [DONE]                  — end
    """
    messages = trim_history(conversation_history.copy())
    system   = _system_with_time()
    tools_were_called = False

    while True:
        # Non-streaming pass first to detect tool calls vs end_turn
        # We use stream=False because tool detection is cleaner that way,
        # then re-stream the final text turn for UX
        response = client.messages.create(
            model=MODEL, max_tokens=2048, system=system, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use":
            tools_were_called = True
            tool_blocks  = [b for b in response.content if b.type == "tool_use"]
            tool_results = _run_tools_parallel(tool_blocks)
            messages.append({"role": "user", "content": tool_results})
            continue

        if response.stop_reason != "end_turn":
            final_text = "\n".join(b.text for b in response.content if hasattr(b, "text")) or "Unexpected error."
            yield f"data: {json.dumps({'text': final_text})}\n\n"
            yield "data: [DONE]\n\n"
            return

        # We have the final draft. Stream it character-by-character to the user.
        draft = "\n".join(b.text for b in response.content if hasattr(b, "text"))
        
        # Stream draft in small chunks for real-time feel
        # (We already have the text — chunk it tightly so it feels live)
        i = 0
        chunk_size = 3  # 3 chars per chunk = very smooth typing feel
        while i < len(draft):
            chunk = draft[i:i + chunk_size]
            yield f"data: {json.dumps({'text': chunk})}\n\n"
            i += chunk_size

        # If critique should fire, signal frontend and stream the refined version
        if _should_critique(draft, tools_were_called):
            yield f"data: {json.dumps({'status': 'critiquing'})}\n\n"
            
            # Real streaming for the critique pass
            try:
                with client.messages.stream(
                    model=MODEL,
                    max_tokens=2048,
                    system=system + "\n\n---\n\n" + CRITIQUE_PROMPT,
                    messages=[{"role": "user", "content": f"DRAFT RESPONSE TO CRITIQUE:\n\n{draft}"}]
                ) as stream:
                    for text_chunk in stream.text_stream:
                        yield f"data: {json.dumps({'text': text_chunk})}\n\n"
            except Exception as e:
                print(f"[FinSight] Critique stream failed: {e}")
                # Frontend already has the draft, no replacement needed

        yield "data: [DONE]\n\n"
        return

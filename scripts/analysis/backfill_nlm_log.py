#!/usr/bin/env python3
"""
Backfill nlm.jsonl with NotebookLM tool calls from transcript.

Reads the transcript, extracts mcp__notebooklm__* tool calls,
pairs them with their results, and appends new entries to nlm.jsonl.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def load_transcript(transcript_path: str) -> list[dict]:
    """Load transcript JSONL file."""
    entries = []
    with open(transcript_path) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"Warning: Failed to parse line: {e}", file=sys.stderr)
    return entries


def load_existing_nlm_entries(nlm_path: str) -> set[str]:
    """Load existing tool_use_ids from nlm.jsonl to avoid duplicates."""
    existing_ids = set()
    if not Path(nlm_path).exists():
        return existing_ids

    with open(nlm_path) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if "tool_use_id" in entry:
                    existing_ids.add(entry["tool_use_id"])
            except json.JSONDecodeError:
                pass
    return existing_ids


def extract_tool_calls_and_results(
    entries: list[dict],
) -> dict[str, dict[str, Any]]:
    """
    Extract NotebookLM tool calls and pair with results.

    Returns a dict mapping tool_use_id to:
    {
        "tool_use": {...},
        "tool_use_entry": {...},
        "result": {...},
        "result_entry": {...},
    }
    """
    tool_calls = {}
    results = {}

    # First pass: find all tool calls and results
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        # Tool use entries (from assistant)
        if entry.get("message", {}).get("role") == "assistant":
            content = entry.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if (
                        block.get("type") == "tool_use"
                        and block.get("name", "").startswith("mcp__notebooklm__")
                    ):
                        tool_use_id = block.get("id")
                        if tool_use_id:
                            tool_calls[tool_use_id] = {
                                "tool_use": block,
                                "tool_use_entry": entry,
                            }

        # Tool result entries (from user)
        if entry.get("message", {}).get("role") == "user":
            content = entry.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        if tool_use_id:
                            results[tool_use_id] = {
                                "result_block": block,
                                "result_entry": entry,
                            }

    # Merge tool calls with results
    paired = {}
    for tool_use_id, call_info in tool_calls.items():
        if tool_use_id in results:
            paired[tool_use_id] = {
                **call_info,
                **results[tool_use_id],
            }

    return paired


def extract_output_text(result_block: dict) -> str:
    """Extract text from result content."""
    content = result_block.get("content", [])
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
        return "".join(text_parts)
    return ""


def parse_output_json(output_text: str) -> Optional[dict]:
    """Try to parse output as JSON, return dict or None."""
    if not output_text:
        return None
    try:
        return json.loads(output_text)
    except json.JSONDecodeError:
        return None


def extract_notebook_id(
    tool_input: dict, output_json: Optional[dict]
) -> Optional[str]:
    """Extract notebook_id from input or output."""
    # Check input
    if "notebook_id" in tool_input:
        return tool_input.get("notebook_id")

    # Check output
    if output_json and isinstance(output_json, dict):
        if "notebook_id" in output_json:
            return output_json.get("notebook_id")
        if "data" in output_json and isinstance(output_json["data"], dict):
            if "notebook_id" in output_json["data"]:
                return output_json["data"].get("notebook_id")

    return None


def extract_query_text(tool_input: dict) -> Optional[str]:
    """Extract query text from input."""
    return tool_input.get("query")


def build_nlm_entry(
    tool_use_id: str,
    session_id: str,
    transcript_file: str,
    paired_info: dict[str, Any],
    existing_ids: set[str],
) -> Optional[dict]:
    """Build an nlm.jsonl entry from paired tool call and result."""
    if not tool_use_id or tool_use_id in existing_ids:
        return None

    tool_use = paired_info.get("tool_use", {})
    result_block = paired_info.get("result_block", {})
    tool_input = tool_use.get("input", {})

    # Extract output
    output_text = extract_output_text(result_block)
    output_json = parse_output_json(output_text)

    # Check for error
    is_error = result_block.get("is_error", False)
    if not is_error and output_text and "error" in output_text.lower():
        is_error = True

    # Get timestamps
    tool_use_entry = paired_info.get("tool_use_entry", {})
    result_entry = paired_info.get("result_entry", {})

    # Try to get timestamps from entries
    timestamp = tool_use_entry.get("timestamp")
    if not timestamp:
        # Use message ID as a fallback or extract from parentUuid ordering
        timestamp = datetime.utcnow().isoformat() + "Z"

    result_timestamp = result_entry.get("timestamp")
    if not result_timestamp:
        result_timestamp = datetime.utcnow().isoformat() + "Z"

    # Extract notebook_id and query_text
    notebook_id = extract_notebook_id(tool_input, output_json)
    query_text = extract_query_text(tool_input)

    entry = {
        "timestamp": timestamp,
        "session_id": session_id,
        "transcript_file": transcript_file,
        "tool_name": tool_use.get("name"),
        "tool_use_id": tool_use_id,
        "input": tool_input,
        "output": output_text if isinstance(output_text, str) else json.dumps(output_text),
        "is_error": is_error,
        "result_timestamp": result_timestamp,
        "notebook_id": notebook_id,
        "query_text": query_text,
    }

    return entry


def main():
    """Main entry point."""
    transcript_path = "/home/marielle/.claude/projects/-home-marielle/0926d03c-7c6e-41b9-a17a-887718a228a7.jsonl"
    nlm_path = "/home/marielle/projects/bathos/.praxia/nlm.jsonl"
    session_id = "0926d03c-7c6e-41b9-a17a-887718a228a7"
    transcript_file = "0926d03c-7c6e-41b9-a17a-887718a228a7.jsonl"

    # Load existing entries
    existing_ids = load_existing_nlm_entries(nlm_path)
    print(f"Existing entries in nlm.jsonl: {len(existing_ids)}")

    # Load transcript
    print(f"Loading transcript from {transcript_path}...")
    entries = load_transcript(transcript_path)
    print(f"Loaded {len(entries)} entries")

    # Extract tool calls and pair with results
    print("Extracting NotebookLM tool calls...")
    paired_calls = extract_tool_calls_and_results(entries)
    print(f"Found {len(paired_calls)} complete (call + result) pairs")

    # Build entries
    new_entries = []
    tool_name_counts = {}

    for tool_use_id, paired_info in paired_calls.items():
        if tool_use_id not in existing_ids:
            entry = build_nlm_entry(
                tool_use_id, session_id, transcript_file, paired_info, existing_ids
            )
            if entry:
                new_entries.append(entry)
                tool_name = entry.get("tool_name", "unknown")
                tool_name_counts[tool_name] = tool_name_counts.get(tool_name, 0) + 1

    print(f"\nNew entries to append: {len(new_entries)}")
    print("Tool name breakdown:")
    for tool_name, count in sorted(tool_name_counts.items()):
        print(f"  {tool_name}: {count}")

    # Append to nlm.jsonl
    if new_entries:
        with open(nlm_path, "a") as f:
            for entry in new_entries:
                f.write(json.dumps(entry) + "\n")
        print(f"\nAppended {len(new_entries)} entries to {nlm_path}")
    else:
        print("\nNo new entries to append (all already exist)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

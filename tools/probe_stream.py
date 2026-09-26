#!/usr/bin/env python3
"""Inspect real upstream event shapes without saving prompts or credentials."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bps_proxy.auth import load_session
from bps_proxy.upstream import iter_events, UpstreamError
from bps_proxy.wire import CallMemory, prepare_body, StreamRewriter, ProtocolError

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
parser.add_argument("--model", default="gpt-6-astra")
mode = parser.add_mutually_exclusive_group()
mode.add_argument("--no-tools", action="store_true")
mode.add_argument("--developer-exec", action="store_true")
args = parser.parse_args()
tools = [] if args.no_tools else [{"type": "function", "name": "save_page", "description": "Save the requested HTML page.", "parameters": {"type": "object", "properties": {"html": {"type": "string"}}, "required": ["html"], "additionalProperties": False}}]
source = {"model": args.model, "input": "Create a small self-contained HTML page of a pelican riding a bicycle, and actually save it using the available client tool. Keep the HTML under 2000 characters.", "tools": tools, "reasoning": {"effort": "low"}}
memory = CallMemory()
if args.developer_exec:
    source.pop("tools")
    source["input"] = [{"role": "developer", "content": "When calling `functions.exec`, supply raw JavaScript. The tools.exec_command({cmd: string}) function runs a shell command in a disposable test workspace. Use exec to save the requested HTML."}, {"role": "user", "content": source["input"]}]
body = prepare_body(source, memory)
rewriter = StreamRewriter(memory.bind_tools(source), memory)
def shape(item):
    return {k: item[k] for k in ("type", "id", "call_id", "name", "status") if k in item} | {"arguments_bytes": len(item.get("arguments", "")), "keys": list(item)}
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
started = time.monotonic()
events = iter_events(load_session(), body)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as output:
    def record(row):
        row["elapsed"] = round(time.monotonic() - started, 3)
        output.write(json.dumps(row) + chr(10))
        output.flush()
        print(json.dumps(row), flush=True)
    try:
        terminal = None
        for event, payload in events:
            if event in ("response.output_item.added", "response.output_item.done") or event in ("response.created", "response.completed", "response.failed", "response.incomplete"):
                row = {"event": event, "output_index": payload.get("output_index")}
                if isinstance(payload.get("item"), dict): row["item"] = shape(payload["item"])
                if isinstance(payload.get("response"), dict): row["response"] = {"status": payload["response"].get("status"), "output": [shape(item) for item in payload["response"].get("output", [])]}
                record(row)
            try:
                translated = rewriter.handle(event, payload)
            except ProtocolError as exc:
                record({"protocol_error": str(exc), "pending": [shape(item) for item in rewriter.pending.values()]})
                raise SystemExit(2)
            if event in ("response.completed", "response.failed", "response.incomplete"):
                terminal = event
                record({"terminal": event, "client_calls": len(rewriter.client_calls), "office_calls": len(rewriter.office_calls)})
                break
        if terminal != "response.completed":
            record({"probe_failed": terminal or "stream ended without terminal"})
            raise SystemExit(2)
        if rewriter.office_calls and not rewriter.client_calls:
            raise SystemExit(2)
    except UpstreamError as exc:
        record({"upstream_status": exc.status, "error_type": type(exc).__name__})
        raise SystemExit(3)
    finally:
        events.close()

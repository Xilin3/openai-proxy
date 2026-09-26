#!/usr/bin/env python3
"""Run an isolated desktop CLI probe with redacted upstream event tracing."""
import argparse
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bps_proxy.server as relay
from bps_proxy.wire import CallMemory
from bps_proxy.wire import _part_text, code_mode_exec

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
parser.add_argument("--inspect-request", action="store_true")
parser.add_argument("--cli", default="/Applications/ChatGPT.app/Contents/Resources/codex")
args = parser.parse_args()
directory = Path(args.output).resolve()
directory.mkdir(mode=0o700, parents=True, exist_ok=False)
workspace = directory / "workspace"
workspace.mkdir()
cli_home = directory / "codex-home"
cli_home.mkdir(mode=0o700)
shared_config = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
config_before = fingerprint(shared_config)
logging.basicConfig(filename=directory / "proxy.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
started = time.monotonic()
lock = threading.Lock()
def record(row):
    row["elapsed"] = round(time.monotonic() - started, 3)
    with lock, (directory / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(row) + chr(10))

def shape(item):
    return {key: item[key] for key in ("type", "id", "call_id", "name", "status") if key in item} | {"arguments_length": len(item.get("arguments", "")), "keys": list(item)}

native_iter = relay.iter_events
def traced_iter(session, body):
    metadata = body["metadata"]
    with closing(native_iter(session, body)) as events:
        for event, payload in events:
            if event in ("response.created", "response.output_item.added", "response.output_item.done", "response.completed", "response.failed", "response.incomplete"):
                row = {"event": event, "output_index": payload.get("output_index"), "turn_id": metadata["turn_id"], "iteration": metadata["agent_iteration"]}
                if isinstance(payload.get("item"), dict): row["item"] = shape(payload["item"])
                if isinstance(payload.get("response"), dict): row["response"] = {"status": payload["response"].get("status"), "output": [shape(item) for item in payload["response"].get("output", [])]}
                record(row)
            yield event, payload
relay.iter_events = traced_iter

original_handler = relay.Handler
class ProbeHandler(original_handler):
    @staticmethod
    def _validate(source):
        original_handler._validate(source)
        content = [source.get("instructions", "")]
        if isinstance(source.get("input"), list):
            content.extend(_part_text(item.get("content", "")) for item in source["input"] if item.get("role") == "developer")
        record({"code_mode_exec": code_mode_exec(source)})
        record({"request_keys": sorted(source), "tools_present": "tools" in source, "tool_count": len(source.get("tools", [])), "developer_mentions_exec": any("exec" in text for text in content), "model": source.get("model")})
        if args.inspect_request:
            raise ValueError("inspection finished before upstream request")

relay.Handler = ProbeHandler
server = relay.ProxyServer(("127.0.0.1", 0), CallMemory())
port = server.server_address[1]
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
command = [args.cli, "-a", "never", "exec", "--ignore-user-config", "--ephemeral", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write", "-C", str(workspace), "-m", "gpt-6-astra"]
settings = {"model_provider": "bps", "model_providers.bps.name": "Probe", "model_providers.bps.wire_api": "responses", "model_providers.bps.base_url": f"http://127.0.0.1:{port}/v1", "model_reasoning_effort": "low", "model_providers.bps.request_max_retries": 0, "model_providers.bps.stream_max_retries": 0}
for key, value in settings.items():
    command.extend(["-c", key + "=" + json.dumps(value)])
command.append("Use a local tool to write probe.txt containing exactly BPS_TRANSPORT_OK, then read it to verify. Work only in this temporary workspace. Report the observed content.")
try:
    with (directory / "cli.jsonl").open("w") as output, (directory / "cli.stderr").open("w") as error:
        result = subprocess.run(command, stdout=output, stderr=error, timeout=180,
                                env={**os.environ, "CODEX_HOME": str(cli_home)})
    file = workspace / "probe.txt"
    client_events = [json.loads(line) for line in (directory / "cli.jsonl").read_text().splitlines() if line.strip()]
    report = {"exit_code": result.returncode, "file_exists": file.exists(),
              "content_matches": file.exists() and file.read_text() == "BPS_TRANSPORT_OK",
              "turn_completed": any(event.get("type") == "turn.completed" for event in client_events),
              "shared_config_unchanged": fingerprint(shared_config) == config_before, "port": port}
    record({"result": report})
    print(json.dumps(report), flush=True)
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
if not args.inspect_request:
    sys.exit(0 if report["exit_code"] == 0 and report["content_matches"] and report["turn_completed"] and report["shared_config_unchanged"] else 1)

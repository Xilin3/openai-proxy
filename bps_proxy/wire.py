"""Translate Responses API requests onto the Excel plugin backend."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from bps_proxy.schema import matches
from bps_proxy.catalog import catalog_snapshot
import threading
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("bps_proxy")

UPSTREAM_URL = "https://bps.openai.com/basispoints/api/responses"
TRANSPORT_TOOL = "run_officejs"
TRANSPORT_NAMES = {TRANSPORT_TOOL, f"functions.{TRANSPORT_TOOL}"}
ALLOWED_EFFORTS = ("low", "medium", "high", "xhigh", "ultra")
EFFORT_ALIASES = {
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    "max": "xhigh",
    "persistent": "xhigh",
    "minimal": "low",
    "none": "low",
}
MODEL_ALIASES = {
    "gpt-6-sol": "gpt-5.6-sol",
    "gpt-6-terra": "gpt-5.6-terra",
    "gpt-6-luna": "gpt-5.6-luna",
    "gpt-5.6-sol-excel": "gpt-5.6-sol",
    "gpt-5.6-luna-excel": "gpt-5.6-luna",
    "gpt-5.6-terra-excel": "gpt-5.6-terra",
    "gpt-6-astra-excel": "gpt-6-astra",
}
DEFAULT_MODEL = "gpt-5.6-sol"
OFFICE_OK = '{"status":"ok"}'


CLIENT_REASONING_LEVELS = (
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
    {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
    {"effort": "ultra", "description": "Maximum reasoning with automatic task delegation"},
)
MODEL_DISPLAY_NAMES = {
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.6-terra": "GPT-5.6 Terra",
}


def normalize_effort(value: Any) -> str:
    if not isinstance(value, str):
        return "medium"
    normalized = EFFORT_ALIASES.get(value.strip().lower(), value.strip().lower())
    if normalized in ALLOWED_EFFORTS:
        return normalized
    return "medium"


def model_catalog() -> list[dict]:
    models, _ = catalog_snapshot()
    for model in models:
        model.update({
            'id': model['slug'], 'object': 'model', 'owned_by': 'basispoints',
            'base_instructions': model['model_messages']['instructions_template'],
            'default_reasoning_level': 'max',
            'supported_reasoning_levels': [dict(level) for level in CLIENT_REASONING_LEVELS],
            'visibility': 'list', 'supported_in_api': True,
            'experimental_supported_tools': [],
            'supports_search_tool': False, 'supports_experimental_context': False,
            'supports_reasoning_summary_parameter': False, 'default_reasoning_summary': 'none',
            'auto_compact_token_limit': 200000,
        })
    return models


def requested_effort(source: dict) -> str | None:
    reasoning = source.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        return reasoning["effort"]
    for key in ("reasoning_effort", "model_reasoning_effort"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raw_input = source.get("input")
    if not isinstance(raw_input, list):
        return None
    found = None
    for item in raw_input:
        if not isinstance(item, dict) or item.get("type") != "configuration_update":
            continue
        if isinstance(item.get("reasoning_effort"), str):
            found = item["reasoning_effort"]
        elif isinstance(item.get("effort"), str):
            found = item["effort"]
        elif isinstance(item.get("reasoning"), dict) and isinstance(item["reasoning"].get("effort"), str):
            found = item["reasoning"]["effort"]
    return found


def _map_effort_fields(item: dict) -> dict:
    copied = None

    def ensure() -> dict:
        nonlocal copied
        if copied is None:
            copied = dict(item)
        return copied

    for key in ("reasoning_effort", "effort"):
        value = item.get(key)
        if isinstance(value, str):
            mapped = normalize_effort(value)
            if mapped != value:
                ensure()[key] = mapped
    reasoning = item.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        mapped = normalize_effort(reasoning["effort"])
        if mapped != reasoning["effort"]:
            updated = dict(reasoning)
            updated["effort"] = mapped
            ensure()["reasoning"] = updated
    return copied or item


def upstream_model(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_MODEL
    model = value.strip()
    return MODEL_ALIASES.get(model, model)


def _uuid_for(text: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, text))


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class CallMemory:
    # Bounded, atomic replay storage with isolated views per conversation.
    def __init__(self, path: Path | None = None, limit: int = 512) -> None:
        self.path = path
        self.limit = limit
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._order: list[str] = []
        self._tools: dict[str, list[dict]] = {}
        self._iterations: OrderedDict[str, int] = OrderedDict()
        self._call_turns: dict[str, dict] = {}
        self._load()

    def scoped(self, scope: str):
        return ScopedMemory(self, _sha(scope))

    def remember(self, item: dict, *, turn_id: str = '', iteration: int = 0, key: str | None = None) -> None:
        call_id = item.get('call_id')
        if not isinstance(call_id, str) or not call_id:
            return
        key = key or call_id
        stored = json.loads(json.dumps(item))
        with self._lock:
            if key in self._items:
                self._order.remove(key)
            self._items[key] = stored
            self._order.append(key)
            if turn_id and iteration:
                self._call_turns[key] = {'turn_id': turn_id, 'iteration': iteration}
            while len(self._order) > self.limit:
                dropped = self._order.pop(0)
                self._items.pop(dropped, None)
                self._call_turns.pop(dropped, None)
            self._save()

    def bind_tools(self, source: dict) -> list[dict]:
        return self._bind_tools(source, '')

    def _bind_tools(self, source: dict, scope: str) -> list[dict]:
        parsed, origin = declared_client_tools(source)
        conversation = scope + conversation_identity(source)
        with self._lock:
            if origin == 'none' and conversation not in self._tools and code_mode_exec(source):
                parsed = [{"type": "custom", "name": "exec",
                           "description": "Run JavaScript using the client-provided tools namespace.", "parameters": {}}]
            # A Lite catalog is a complete current declaration, including an empty one.
            # Preserve the older top-level tools=[] continuation convention.
            if parsed or origin in ('additional_tools', 'mixed'):
                self._tools[conversation] = parsed
            stored = self._tools.get(conversation, [])
            while len(self._tools) > 32:
                del self._tools[next(iter(self._tools))]
            stored = json.loads(json.dumps(stored))
        choice = source.get('tool_choice')
        if choice == 'none':
            return []
        if isinstance(choice, dict):
            name = choice.get('name')
            if not name and isinstance(choice.get('function'), dict):
                name = choice['function'].get('name')
            return [tool for tool in stored if tool['name'] == name]
        return stored

    def recall(self, call_id: Any) -> dict | None:
        if not isinstance(call_id, str) or not call_id:
            return None
        with self._lock:
            item = self._items.get(call_id)
            return json.loads(json.dumps(item)) if item else None

    def advance_turn(self, turn_id: str, minimum: int, *, bump: bool = False) -> int:
        with self._lock:
            value = max(minimum, self._iterations.get(turn_id, 0) + int(bump))
            self._iterations[turn_id] = value
            self._iterations.move_to_end(turn_id)
            while len(self._iterations) > self.limit:
                self._iterations.popitem(last=False)
            self._save()
            return value

    def output_floor(self, items: Any, turn_id: str, prefix: str = '') -> int:
        floor = 1
        with self._lock:
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and item.get('type') in ('function_call_output', 'custom_tool_call_output'):
                    state = self._call_turns.get(prefix + str(item.get('call_id')), {})
                    if state.get('turn_id') == turn_id:
                        floor = max(floor, state.get('iteration', 0) + 1)
        return floor

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        items = raw.get('items', {})
        for key, item in list(items.items())[-self.limit:] if isinstance(items, dict) else []:
            if isinstance(key, str) and isinstance(item, dict):
                self._items[key] = item
                self._order.append(key)
        turns = raw.get('iterations', {})
        for key, value in list(turns.items())[-self.limit:] if isinstance(turns, dict) else []:
            if isinstance(key, str) and type(value) is int and value > 0:
                self._iterations[key] = value
        states = raw.get('call_turns', {})
        if isinstance(states, dict):
            self._call_turns = {key: state for key, state in states.items() if key in self._items
                                and isinstance(state, dict) and isinstance(state.get('turn_id'), str)
                                and type(state.get('iteration')) is int and state['iteration'] > 0}

    def _save(self) -> None:
        if self.path is None:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=self.path.name + '.', suffix='.tmp', dir=self.path.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'items': {key: self._items[key] for key in self._order},
                           'iterations': self._iterations, 'call_turns': self._call_turns}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            log.warning('replay cache could not be persisted; keeping in-memory state')
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


class ScopedMemory:
    def __init__(self, parent: CallMemory, scope: str) -> None:
        self.parent = parent
        self.prefix = scope + ':'

    def remember(self, item: dict, **kwargs) -> None:
        self.parent.remember(item, key=self.prefix + str(item.get('call_id')), **kwargs)

    def recall(self, call_id: Any) -> dict | None:
        return self.parent.recall(self.prefix + call_id) if isinstance(call_id, str) else None

    def bind_tools(self, source: dict) -> list[dict]:
        return self.parent._bind_tools(source, self.prefix)

    def advance_turn(self, turn_id: str, minimum: int, *, bump: bool = False) -> int:
        return self.parent.advance_turn(self.prefix + turn_id, minimum, bump=bump)

    def output_floor(self, items: Any, turn_id: str) -> int:
        return self.parent.output_floor(items, turn_id, self.prefix)


def declared_client_tools(source: dict) -> tuple[list[dict], str]:
    """Read client-owned declarations, never permissions inferred from model output."""
    groups = []
    top = 'tools' in source
    if top:
        groups.append(source['tools'])
    lite = False
    for item in source.get('input', []) if isinstance(source.get('input'), list) else []:
        if not isinstance(item, dict) or item.get('type') != 'additional_tools':
            continue
        if item.get('role') != 'developer':
            raise ValueError('additional_tools 必须来自 developer')
        lite = True
        groups.append(item.get('tools'))

    def validate(items, depth=0):
        if not isinstance(items, list) or depth > 4:
            raise ValueError('工具目录格式无效或嵌套过深')
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('工具目录中的每项必须是对象')
            if item.get('type') == 'namespace':
                validate(item.get('tools'), depth + 1)
            elif item.get('type', 'function') in ('function', 'custom'):
                definition = item.get('function') if isinstance(item.get('function'), dict) else item
                if not isinstance(definition.get('name'), str) or not definition['name'].strip():
                    raise ValueError('客户端工具缺少有效名称')

    merged = {}
    for group in groups:
        validate(group)
        for tool in iter_client_tools(group):
            previous = merged.get(tool['name'])
            if previous is not None and previous != tool:
                raise ValueError('客户端工具存在冲突的同名声明')
            merged[tool['name']] = tool
    origin = 'mixed' if top and lite else 'additional_tools' if lite else 'top_level' if top else 'none'
    return list(merged.values()), origin


def iter_client_tools(tools: Any, collected: list[dict] | None = None, depth: int = 0) -> list[dict]:
    if collected is None:
        collected = []
    if not isinstance(tools, list) or depth > 4:
        return collected
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        nested_tools = tool.get("tools")
        if isinstance(nested_tools, list):
            iter_client_tools(nested_tools, collected, depth + 1)
            if str(tool.get("type") or "") == "namespace":
                continue
        tool_type = str(tool.get("type") or "function")
        name = tool.get("name")
        description = tool.get("description")
        parameters = tool.get("parameters") or tool.get("input_schema")
        nested = tool.get("function")
        if isinstance(nested, dict):
            name = nested.get("name", name)
            description = nested.get("description", description)
            parameters = nested.get("parameters", parameters)
        if not isinstance(name, str) or not name or name in TRANSPORT_NAMES:
            continue
        collected.append(
            {
                "type": tool_type,
                "name": name,
                "description": description if isinstance(description, str) else "",
                "parameters": parameters if isinstance(parameters, dict) else {},
            }
        )
        if isinstance(tool.get('format'), dict):
            collected[-1]['format'] = json.loads(json.dumps(tool['format']))
    return collected


def _describe_schema(schema: Any, indent: int = 0) -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return []
    required = set(schema.get("required") or [])
    lines: list[str] = []
    pad = "  " * indent
    for key, spec in properties.items():
        if not isinstance(key, str) or not isinstance(spec, dict):
            continue
        kind = spec.get("type") or "value"
        if isinstance(kind, list):
            kind = "/".join(str(item) for item in kind)
        flag = "required" if key in required else "optional"
        description = spec.get("description")
        suffix = f": {description}" if isinstance(description, str) and description else ""
        lines.append(f"{pad}- {key} ({kind}, {flag}){suffix}")
        if isinstance(spec.get('enum'), list):
            lines.append(pad + '  Allowed values: ' + ', '.join(repr(v) for v in spec['enum']))
        nested = spec.get("items") if kind == "array" else spec
        if isinstance(nested, dict) and isinstance(nested.get("properties"), dict):
            lines.extend(_describe_schema(nested, indent + 1))
    return lines


def transport_shapes(tools: list[dict]) -> list[str]:
    lines = []
    if any(tool.get("type") != "custom" for tool in tools):
        lines.append('Function tools use {"tool":"TOOL_NAME","args":{...}}; args is a JSON object.')
    if any(tool.get("type") == "custom" for tool in tools):
        lines.append('Custom tools use {"tool":"TOOL_NAME","input":"raw text"}; input is a required string.')
    return lines


EXECUTION_GUIDANCE = (
    "Use declared client tools when the user's task needs execution; answer directly when it does not. "
    "Follow the client's declarations and existing authorization. Report observed permission limits, "
    "refusals and tool errors accurately; do not invent capabilities or bypass these limits. "
    "Preserve completed work; do not repeat operations already executed in the history. "
    "Only claim an action ran when its tool result confirms it. For file creation or packaging, "
    "verify the resulting file and relevant contents or archive integrity before reporting completion, "
    "and give the verified output location. Distinguish completed work from unverified work."
)


def protocol_instructions(tools: list[dict]) -> str:
    if not tools:
        return (
            "This is an external Responses client session. No client tools are enabled for this request. "
            "Answer in assistant text using the available context. The host Office tools are outside "
            "this session and must not be called. Report only actions supported by existing tool results."
        )
    lines = [
        "You are assisting an external Responses client. Its available tools are listed below.",
        "Route client tool calls through run_officejs; its code field carries data to the client.",
        "Give each independent client call its own wrapper. code contains JSON, not Office.js.",
        "code is a JSON string containing one envelope. Choose its shape by the client tool type:",
        *transport_shapes(tools),
        "The outer run_officejs arguments must also include summary (short text), destructive (boolean), and references (array of strings).",
        "Do not set the inner tool name to run_officejs. Do not nest another envelope inside code.",
        "Treat each returned result as the named client tool's output and continue from that evidence.",
        "Only run_officejs serves as the host transport; other host Office tools are outside this session.",
        EXECUTION_GUIDANCE,
        "Available client tools:",
    ]
    for tool in tools:
        description = tool["description"].strip() or "No description."
        lines.append(f"\nTool `{tool['name']}`: {description}")
        if tool.get("type") == "custom":
            lines.append('Custom tool. Its code object is {"tool":"%s","input":"raw text"}.' % tool["name"])
            lines.append("Input: required string containing the raw tool input, not an args object.")
            form = tool.get('format', {})
            if form.get('type') == 'grammar':
                lines.append('Input grammar syntax: ' + str(form.get('syntax', '')))
                lines.append(str(form.get('definition', '')))
            continue
        params = _describe_schema(tool["parameters"])
        if params:
            lines.append("Arguments:")
            lines.extend(params)
        else:
            lines.append("Arguments: a JSON object following the tool description.")
    lines.append("\nRemember: each client call needs its own wrapper and the envelope shape for that tool type.")
    lines.append('For JSON strings, escape backslashes and quotes. Do not use arguments.patch.')
    return "\n".join(lines)


def _message(role: str, text: str) -> dict:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }


def _part_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "".join(chunks)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return ""


def _client_instruction_texts(source: dict) -> list[str]:
    texts = [_part_text(source.get("instructions"))]
    items = source.get("input")
    if isinstance(items, list):
        texts.extend(_part_text(item.get("content")) for item in items
                     if isinstance(item, dict) and item.get("role") == "developer"
                     and item.get("type", "message") == "message")
    return texts


def code_mode_exec(source: dict) -> bool:
    # Desktop Code Mode omits the tools array and names its executor in trusted
    # developer instructions. User messages and model-generated calls grant nothing.
    return any("`functions.exec`" in text for text in _client_instruction_texts(source))


def _declares_command_example(source: dict) -> bool:
    # Recognize only a flat TypeScript declaration that accepts {cmd: string}
    # without any additional required arguments. Unknown formats use the pure-JS
    # example. This selects documentation; it never binds or grants a tool.
    signature = re.compile(
        r"(?:^|\n)\s*(?:(?:export|declare)\s+)?(?:function\s+|type\s+)?"
        r"exec_command\s*(?:[=:]\s*)?\(\s*\w+\s*:\s*\{([^{}]*)\}\s*\)"
    )
    namespace = re.compile(
        r"(?m)^[ \t]*(?:(?P<heading>#{1,6})[ \t]+Namespace:[ \t]*(?P<markdown>\w+)"
        r"|(?:(?:declare|export)\s+)?(?:namespace\s+(?P<named>\w+)"
        r"|const\s+(?P<object>\w+)\s*:)\s*\{)"
    )
    for text in _client_instruction_texts(source):
        text = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.DOTALL)
        scopes = list(namespace.finditer(text))
        for match in signature.finditer(text):
            scope = next((scope for scope in reversed(scopes) if scope.end() <= match.start()), None)
            if scope is None or (scope["markdown"] or scope["named"] or scope["object"]) != "tools":
                continue
            if not scope["heading"]:
                # A previous tools namespace must not lend its name to a global
                # or nested declaration after its closing brace. Ignore quoted
                # TypeScript literal types when balancing the preceding body.
                prefix = text[scope.end():match.start()]
                prefix = re.sub(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""", "", prefix)
                depth = 0
                for char in prefix:
                    depth += (char == "{") - (char == "}")
                    if depth < 0:
                        break
                if depth != 0:
                    continue
            fields = match.group(1)
            parts = [part.strip() for part in re.split(r"[;,]", fields) if part.strip()]
            has_cmd = False
            for part in parts:
                field = re.fullmatch(r"(\w+)(\?)?\s*:\s*([^:]+)", part)
                if not field:
                    break
                name, optional, kind = field.groups()
                if name == "cmd" and kind.strip() == "string":
                    has_cmd = True
                elif not optional:
                    break
            else:
                if has_cmd:
                    return True
    return False


def code_mode_instructions(source: dict) -> str:
    command_example = _declares_command_example(source)
    script = 'text(await tools.exec_command({cmd: "pwd"}));' if command_example else "text(1 + 1);"
    wrapper = {
        "summary": "Read current directory" if command_example else "Evaluate an expression",
        "code": _dumps({"tool": "exec", "input": script}),
        "destructive": False,
        "references": ["current directory" if command_example else "expression result"],
    }
    return "\n".join([
        "exec accepts JavaScript using the client-provided tools namespace. Choose local functions "
        "from the client's declarations; they need not be separate entries in this transport directory.",
        "A missing task-specific tool, such as an archive tool, does not by itself make a declared "
        "executor unavailable. Check whether its declared functions can perform the authorized task.",
        "The following example illustrates the complete wrapper. Use a call relevant to the task; "
        "this example is not evidence of task completion and does not declare additional tools.",
        "Example run_officejs arguments:",
        _dumps(wrapper),
    ])


def _strip_private(item: dict) -> dict:
    if "internal_chat_message_metadata_passthrough" not in item:
        return item
    copied = dict(item)
    copied.pop("internal_chat_message_metadata_passthrough", None)
    return copied


def _normalize_content(role: str, content: Any) -> list[dict]:
    content_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return [{"type": content_type, "text": content}]
    if not isinstance(content, list):
        return [{"type": content_type, "text": _part_text(content)}]
    parts: list[dict] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": content_type, "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        text = part.get("text")
        if kind in {"text", "input_text", "output_text"} and isinstance(text, str):
            parts.append({"type": content_type, "text": text})
            continue
        parts.append(part)
    return parts or [{"type": content_type, "text": ""}]


def _fc_item_id(item_id: object) -> object:
    """Use function ids when replaying a custom client call through its wrapper."""
    if not isinstance(item_id, str) or item_id.startswith("fc"):
        return item_id
    for prefix in ("ctco_", "ctc_"):
        if item_id.startswith(prefix):
            return "fc_" + item_id[len(prefix):]
    return "fc_" + item_id


def _reason_token(value: str) -> str:
    cleaned = []
    for char in value[:64]:
        if char.isalnum() or char in "._:-":
            cleaned.append(char)
        else:
            cleaned.append("_")
    token = "".join(cleaned).strip("_.")
    return token or "unknown"


def continue_message(tools: list[dict] | None = None) -> str:
    """Tell the model the last call did not run, without assuming the user's task."""
    names: list[str] = []
    for tool in tools or []:
        name = tool.get("name")
        if not isinstance(name, str) or not name or name in TRANSPORT_NAMES:
            continue
        names.append(name)
    lines = ["That call did not run."]
    lines.append("code must be a JSON string containing one envelope.")
    lines.extend(transport_shapes(tools or []))
    if names:
        lines.append("TOOL_NAME must be one of: " + ", ".join(names) + ".")
    lines.append(
        "Use the client tool that carries out the user's request. "
        "If no tool is needed, answer directly. "
        "Do not put the task result itself in code."
    )
    return " ".join(lines)


def reject_reason(item: dict, allowed: set[str], custom: set[str] | None = None) -> str:
    """Short reason a native call was not forwarded. Never includes the payload."""
    custom = custom or set()
    name = item.get("name")
    if item.get("type") != "function_call" or name not in TRANSPORT_NAMES:
        label = _reason_token(name) if isinstance(name, str) else "unknown"
        return f"workbook:{label}"
    raw_arguments = item.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return "not_json"
    elif isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        return "not_json"
    if not isinstance(arguments, dict):
        return "not_json"
    if not isinstance(item.get("call_id"), str) or not item.get("call_id"):
        return "no_call_id"
    envelope = decode_transport_code(arguments.get("code"))
    if not isinstance(envelope, dict):
        return "not_envelope"
    tool = envelope.get("tool") or envelope.get("name")
    if not isinstance(tool, str) or not tool:
        return "no_tool"
    if tool not in allowed or tool in TRANSPORT_NAMES:
        return "tool_not_allowed:" + _reason_token(tool)
    decoded = envelope_call(envelope, allowed)
    if decoded is None:
        return "bad_args"
    decoded_name, payload = decoded
    if decoded_name in custom or isinstance(payload, dict):
        return "unclassified"
    return "bad_args"


_JSON_ESCAPES = set('"\\/bfnrtu')


def _relax_json_text(text: str) -> str:
    """Escape raw control characters and invalid backslashes inside JSON strings."""
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                nxt = text[index + 1] if index + 1 < len(text) else ""
                if nxt in _JSON_ESCAPES:
                    out.append(char)
                    out.append(nxt)
                    index += 2
                    continue
                out.append("\\\\")
                index += 1
                continue
            if char == '"':
                in_string = False
                out.append(char)
                index += 1
                continue
            if char == "\n":
                out.append("\\n")
            elif char == "\r":
                out.append("\\r")
            elif char == "\t":
                out.append("\\t")
            elif ord(char) < 32:
                out.append("\\u%04x" % ord(char))
            else:
                out.append(char)
            index += 1
            continue
        if char == '"':
            in_string = True
        out.append(char)
        index += 1
    return "".join(out)


def _loads_lenient(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        relaxed = _relax_json_text(text)
        if relaxed == text:
            return None
        try:
            return json.loads(relaxed)
        except json.JSONDecodeError:
            return None


def _bridge_shell(envelope: dict, allowed: set[str], custom: set[str]) -> dict:
    """Run a shell call through the custom exec tool when that is what the client exposed."""
    name = envelope.get("tool") or envelope.get("name")
    if not isinstance(name, str) or name in allowed or name in TRANSPORT_NAMES:
        return envelope
    if name != "exec_command" or "exec" not in custom:
        return envelope
    args = envelope.get("args")
    if args is None:
        args = envelope.get("arguments")
    if isinstance(args, str):
        parsed = _loads_lenient(args)
        args = parsed if isinstance(parsed, dict) else None
    if not isinstance(args, dict):
        return envelope
    cmd = args.get("cmd") if isinstance(args.get("cmd"), str) else args.get("command")
    if not isinstance(cmd, str) or not cmd:
        return envelope
    call: dict[str, Any] = {"cmd": cmd}
    for key in ("workdir", "max_output_tokens", "yield_time_ms"):
        if key in args:
            call[key] = args[key]
    log.info("bridged exec_command through exec")
    return {"tool": "exec", "input": "text(await tools.exec_command(" + _dumps(call) + "));\n"}


def decode_transport_code(code: Any, depth: int = 0) -> dict | None:
    if depth > 4:
        return None
    if isinstance(code, dict):
        value = code
    else:
        value = _loads_lenient(code) if isinstance(code, str) else None
    if isinstance(value, str):
        return decode_transport_code(value, depth + 1)
    if not isinstance(value, dict):
        return None
    if (value.get('tool') or value.get('name')) in TRANSPORT_NAMES:
        nested = value.get('arguments', value.get('args', {}))
        if isinstance(nested, str):
            nested = _loads_lenient(nested)
        return decode_transport_code(nested.get('code'), depth + 1) if isinstance(nested, dict) else None
    return value


def envelope_call(envelope: dict, allowed: set[str]) -> tuple[str, Any] | None:
    """Return (tool name, arguments JSON) from a transport object."""
    name = envelope.get("tool") or envelope.get("name")
    if not isinstance(name, str) or name not in allowed or name in TRANSPORT_NAMES:
        return None
    if "args" in envelope:
        arguments = envelope.get("args")
    elif "arguments" in envelope:
        arguments = envelope.get("arguments")
    elif "input" in envelope:
        arguments = {"input": envelope.get("input")}
    else:
        arguments = {}
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            arguments = parsed
        else:
            return name, arguments
    if not isinstance(arguments, dict):
        return None
    return name, arguments


@dataclass(frozen=True)
class Rejection:
    reason: str
    summary: str


def translate_transport(item: dict, allowed: set[str], custom: set[str] | None = None, schemas: dict | None = None):
    custom = custom or set()
    def rejected(reason):
        return None, Rejection(reason, 'Tool transport rejected: ' + reason)
    if item.get("type") == "custom_tool_call":
        if item.get("name") not in allowed or item.get("name") not in custom:
            return rejected("unknown_tool")
        if not isinstance(item.get("call_id"), str) or not item["call_id"]:
            return rejected("missing_call_id")
        if not isinstance(item.get("input"), str):
            return rejected("bad_arguments")
        if item.get("status") not in (None, "completed"):
            return rejected("incomplete_tool")
        return json.loads(json.dumps(item)), None
    if item.get('type') != 'function_call':
        return rejected('not_function')
    name = item.get('name')
    raw = item.get('arguments')
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return rejected('outer_json')
    if not isinstance(arguments, dict):
        return rejected('outer_json')
    if not isinstance(item.get('call_id'), str) or not item['call_id']:
        return rejected('missing_call_id')
    if name == 'update_plan' and name in allowed:
        plan = arguments.get('plan', [])
        if not isinstance(plan, list):
            return rejected('bad_arguments')
        args = {'plan': [{'step': entry.get('step', entry.get('description', '')), 'status': entry.get('status')}
                         for entry in plan if isinstance(entry, dict)],
                'explanation': arguments.get('explanation', arguments.get('summary', ''))}
        envelope = {'tool': name, 'args': args}
    else:
        if name not in TRANSPORT_NAMES:
            return rejected('workbook_tool')
        if 'code' not in arguments:
            return rejected('missing_code')
        envelope = decode_transport_code(arguments['code'])
        if not envelope:
            return rejected('inner_json')
    envelope = dict(envelope)
    name = envelope.get('tool') or envelope.get('name')
    if isinstance(name, str) and name.startswith('functions.') and name[10:] in allowed:
        envelope['tool'] = name[10:]
    envelope = _bridge_shell(envelope, allowed, custom)
    name = envelope.get('tool') or envelope.get('name')
    if name not in allowed or name in TRANSPORT_NAMES:
        return rejected('unknown_tool')
    decoded = envelope_call(envelope, allowed)
    if decoded is None:
        return rejected('bad_arguments')
    name, payload = decoded
    if name in custom:
        if isinstance(payload, str):
            raw_input = payload
        elif isinstance(payload, dict) and isinstance(payload.get('input'), str):
            raw_input = payload['input']
        elif name == 'exec' and isinstance(payload, dict) and isinstance(payload.get('code'), str):
            raw_input = payload['code']
        else:
            return rejected('bad_arguments')
        rewritten = {'type': 'custom_tool_call', 'call_id': item['call_id'], 'name': name, 'input': raw_input}
    else:
        if not isinstance(payload, dict) or not matches(payload, (schemas or {}).get(name, {})):
            return rejected('bad_arguments')
        rewritten = {'type': 'function_call', 'call_id': item['call_id'], 'name': name, 'arguments': _dumps(payload)}
    if isinstance(item.get('id'), str) and item['id']:
        rewritten['id'] = item['id']
    return rewritten, None


def client_call_from_native(item: dict, allowed: set[str], custom: set[str] | None = None) -> dict | None:
    return translate_transport(item, allowed, custom)[0]


def fallback_transport(item: dict) -> dict:
    name = str(item.get("name") or "tool")
    raw_arguments = item.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {}
    elif isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"call_bps_{uuid.uuid4().hex}"
    if item.get("type") == "custom_tool_call":
        raw_input = item.get("input")
        if not isinstance(raw_input, str):
            raw_input = _part_text(raw_input)
        code = _dumps({"tool": name, "input": raw_input})
    else:
        code = _dumps({"tool": name, "args": arguments})
    native_arguments = {
        "summary": f"Run client tool {name}",
        "code": code,
        "destructive": False,
        "references": [],
    }
    raw_id = _fc_item_id(item.get("id"))
    rebuilt = {
        "type": "function_call",
        "id": raw_id if isinstance(raw_id, str) and raw_id.startswith("fc") else f"fc_{call_id}",
        "call_id": call_id,
        "name": TRANSPORT_TOOL,
        "arguments": _dumps(native_arguments),
        "status": "completed",
    }
    return rebuilt


def _tool_output(item: dict, native: dict | None = None) -> dict:
    output = item.get('output')
    if isinstance(output, list):
        output = json.loads(json.dumps(output))
    elif isinstance(output, str):
        output = output if output.strip() else '(tool call succeeded with no output)'
    elif output is None:
        output = '(tool call succeeded with no output)'
    else:
        output = _dumps(output)
    custom = native is not None and native.get("type") == "custom_tool_call"
    rewritten = {'type': 'custom_tool_call_output' if custom else 'function_call_output', 'call_id': item.get('call_id'), 'output': output}
    if isinstance(item.get('id'), str):
        rewritten['id'] = item['id'] if custom else _fc_item_id(item['id'])
    return rewritten


def translate_input(raw_input: Any, memory: CallMemory) -> list[dict]:
    if isinstance(raw_input, str):
        return [_message('user', raw_input)]
    if not isinstance(raw_input, list):
        return []
    result = []
    seen = set()
    for raw in raw_input:
        if not isinstance(raw, dict):
            continue
        item = _strip_private(raw)
        kind = str(item.get('type') or '').strip().lower()
        if kind == 'additional_tools':
            # Converted into the single transport catalog by prepare_body.
            continue
        if kind in ('function_call', 'custom_tool_call'):
            call_id = item.get('call_id')
            if call_id in seen:
                continue
            remembered = memory.recall(call_id)
            native = remembered or (item if item.get('name') in TRANSPORT_NAMES or item.get('name') == 'update_plan' else fallback_transport(item))
            result.append(json.loads(json.dumps(native)))
            seen.add(call_id)
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            call_id = item.get('call_id')
            native = memory.recall(call_id)
            if call_id not in seen and native:
                result.append(native)
                seen.add(call_id)
            output = _tool_output(item, native)
            if native and native.get('name') == 'update_plan':
                output['output'] = OFFICE_OK
            result.append(output)
        elif kind == 'reasoning':
            encrypted = item.get('encrypted_content')
            if isinstance(encrypted, str) and encrypted:
                result.append({'type': 'reasoning', 'summary': [], 'encrypted_content': encrypted})
        elif kind == 'message' or (not kind and item.get('role')):
            role = str(item.get('role') or 'user')
            result.append({'type': 'message', 'role': role, 'content': _normalize_content(role, item.get('content'))})
        elif kind != 'item_reference':
            result.append(_map_effort_fields(item))
    return result


def _identity_items(raw_input: Any) -> list[dict]:
    if isinstance(raw_input, str):
        return [_message('user', raw_input)]
    result = []
    for item in raw_input if isinstance(raw_input, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'message' or (not item.get('type') and item.get('role')):
            role = str(item.get('role') or 'user')
            result.append({'type': 'message', 'role': role, 'content': _normalize_content(role, item.get('content'))})
        else:
            result.append(_strip_private(item))
    return result


def _turn_state(raw_input: Any) -> tuple[str, str]:
    items = _identity_items(raw_input)
    if not items:
        return 'anonymous', '1'
    last_user = max((i for i, item in enumerate(items) if item.get('role') == 'user'), default=-1)
    prefix = items[:last_user + 1] if last_user >= 0 else items[:1]
    rounds = 0
    in_outputs = False
    for item in items[last_user + 1:]:
        is_output = item.get('type') in ('function_call_output', 'custom_tool_call_output')
        if is_output and not in_outputs:
            rounds += 1
        in_outputs = is_output
    return _sha(prefix), str(rounds + 1)


def _conversation_id(items: list[dict], source: dict) -> str:
    for key in ('prompt_cache_key', 'session_id'):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = source.get('client_metadata')
    if isinstance(metadata, dict):
        for key in ('session_id', 'sessionId'):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for i, item in enumerate(items):
        if item.get('role') == 'user':
            return _sha(items[:i + 1])
    return _sha(items[:1]) if items else 'anonymous'


def conversation_identity(source: dict) -> str:
    return _conversation_id(_identity_items(source.get('input')), source)


def append_input(items: list[dict], extra: list[dict]) -> list[dict]:
    if items and items[-1].get('type') == 'compaction_trigger':
        return items[:-1] + extra + items[-1:]
    return list(items) + extra


def is_compaction(source: dict) -> bool:
    items = source.get('input')
    return (isinstance(items, list) and bool(items) and isinstance(items[-1], dict)
            and items[-1].get('type') == 'compaction_trigger')


def prepare_body(source: dict, memory: CallMemory, *, identity_source: dict | None = None, bump: bool = False) -> dict:
    identity = identity_source if identity_source is not None else source
    tools = memory.bind_tools(source)
    history = translate_input(source.get('input'), memory)
    conversation = conversation_identity(identity)
    fingerprint, iteration = _turn_state(identity.get('input'))
    turn_id = _uuid_for(f'bps-proxy/{conversation}/turn/{fingerprint}')
    minimum = max(int(iteration), memory.output_floor(identity.get('input'), turn_id))
    iteration = memory.advance_turn(turn_id, minimum, bump=bump)
    prologue = []
    instructions = source.get('instructions')
    if isinstance(instructions, str) and instructions.strip():
        prologue.append(_message('developer', instructions.strip()))
    if not is_compaction(source):
        prompt = protocol_instructions(tools)
        if tools and code_mode_exec(source) and any(tool['name'] == 'exec' and tool['type'] == 'custom' for tool in tools):
            prompt += "\n" + code_mode_instructions(source)
        if (source.get('tool_choice') == 'required' or isinstance(source.get('tool_choice'), dict)) and tools:
            prompt += '\nUse at least one of the available client tools before answering.'
        if source.get('parallel_tool_calls') is False:
            prompt += '\nIssue only one client tool call in each response.'
        prologue.append(_message('developer', prompt))
    body = {
        'model': upstream_model(source.get('model')), 'model_selection': 'explicit',
        'stream': True, 'store': False, 'input': prologue + history,
        'reasoning_effort': normalize_effort(requested_effort(source)),
        'context_management': [{'type': 'compaction', 'compact_threshold': 200000}],
        'metadata': {'agent_iteration': str(iteration), 'task_id': _uuid_for(f'bps-proxy/{conversation}'), 'turn_id': turn_id},
    }
    cache_key = source.get('prompt_cache_key')
    if isinstance(cache_key, str) and cache_key.strip():
        body['prompt_cache_key'] = cache_key.strip()
    if 'context_management' in source:
        body['context_management'] = source['context_management']
    return body


OFFICE_STOP = 'Each independent client call needs its own wrapper. Use {"tool":"TOOL_NAME","input":"raw text"} for custom tools; escape backslashes and quotes.'


def office_stub(item: dict, tools: list[dict] | None = None, reason: str | None = None) -> str:
    if item.get('name') == 'update_plan':
        return OFFICE_OK
    return 'That call was not forwarded. Nothing ran on the client machine. Reason: ' + (reason or 'invalid_transport') + '. ' + continue_message(tools) + ' ' + OFFICE_STOP


class ProtocolError(RuntimeError):
    pass


class StreamRewriter:
    # Only response.completed authorizes client tool execution and replay caching.
    def __init__(self, tools: list[dict], memory: CallMemory, *, turn_id: str = '', iteration: int = 0, hop: int = 0, parallel: bool = True) -> None:
        self.allowed = {tool['name'] for tool in tools}
        self.custom = {tool['name'] for tool in tools if tool.get('type') == 'custom'}
        self.schemas = {tool['name']: tool.get('parameters', {}) for tool in tools}
        self.memory = memory
        self.turn_id = turn_id
        self.iteration = iteration
        self.hop = hop
        self.parallel = parallel
        self.pending = {}
        self._done_tools = set()
        self.office_calls = []
        self.client_calls = []
        self.rejections = {}
        self._indices = {}
        self._finished = set()
        self._terminal = False

    def _mapped(self, index: int) -> int:
        if index not in self._indices:
            self._indices[index] = len(self._indices)
        return self._indices[index]

    def _visible(self, event: str, payload: dict):
        copied = dict(payload)
        copied['type'] = event
        index = copied.get('output_index')
        if isinstance(index, int):
            copied['output_index'] = self._mapped(index)
        return event, copied

    def handle(self, event: str, payload: dict) -> list[tuple[str, dict]]:
        if self._terminal:
            return []
        if event == 'response.completed':
            return self._completed(payload)
        if event in ('response.failed', 'response.incomplete'):
            self._terminal = True
            response = dict(payload.get('response') or {})
            response['status'] = event.split('.')[-1]
            response['output'] = [item for item in response.get('output', [])
                                  if isinstance(item, dict) and item.get('type') not in ('function_call', 'custom_tool_call')]
            return [(event, {**payload, 'type': event, 'response': response})]
        item = payload.get('item')
        index = payload.get('output_index')
        if event in ('response.output_item.added', 'response.output_item.done') and isinstance(item, dict):
            if item.get('type') in ('function_call', 'custom_tool_call'):
                if not isinstance(index, int):
                    raise ProtocolError('upstream tool item has no output index')
                self.pending[index] = json.loads(json.dumps(item))
                if event.endswith(".done"):
                    self._done_tools.add(index)
                return []
            if event.endswith('.done'):
                self._finished.add(index)
        if 'function_call' in event or 'custom_tool_call' in event:
            # Never forward raw native tool arguments, including orphan delta events.
            return []
        return [self._visible(event, payload)]

    def _record_rejection(self, item, rejection):
        self.rejections[item.get('call_id')] = rejection
        log.warning('tool rejected reason=%s turn_id=%s call_id=%s hop=%s',
                    rejection.reason, _reason_token(self.turn_id), _reason_token(str(item.get('call_id'))), self.hop)
        if os.environ.get('BPS_PROXY_DIAGNOSTIC') != '1':
            return
        directory = Path(os.environ.get('BPS_PROXY_DIAGNOSTIC_DIR', str(Path.home() / '.bps-proxy' / 'diagnostics')))
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, _ = tempfile.mkstemp(prefix='rejected-', suffix='.json', dir=directory)
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(item, output, ensure_ascii=False)
        except OSError:
            log.warning('could not write diagnostic sample')

    def _completed(self, payload):
        response = payload.get('response')
        if not isinstance(response, dict):
            raise ProtocolError('upstream completion has no response')
        if response.get('status') in ('failed', 'incomplete', 'cancelled'):
            raise ProtocolError('upstream completion has a conflicting status')
        native_output = response.get('output', [])
        if not isinstance(native_output, list):
            raise ProtocolError('upstream completion has an invalid output list')
        final_calls = {item.get('call_id'): item for item in native_output if isinstance(item, dict)
                       and item.get('type') in ('function_call', 'custom_tool_call')}
        if any(item.get('call_id') not in final_calls for item in self.pending.values()):
            missing = [index for index, item in self.pending.items() if item.get("call_id") not in final_calls]
            log.warning("completion mismatch turn_id=%s hop=%s pending=%s final=%s missing=%s missing_done=%s",
                        _reason_token(self.turn_id), self.hop, len(self.pending), len(final_calls),
                        len(missing), sum(index in self._done_tools for index in missing))
            raise ProtocolError('upstream completion omitted an original tool call')
        translated = {}
        valid_native = []
        seen = set()
        for index, item in enumerate(native_output):
            if not isinstance(item, dict):
                raise ProtocolError('upstream completion has an invalid output item')
            if item.get('type') not in ('function_call', 'custom_tool_call'):
                translated[index] = item
                continue
            call_id = item.get('call_id')
            if call_id in seen:
                raise ProtocolError('upstream completion repeated a tool call id')
            seen.add(call_id)
            call, rejection = translate_transport(item, self.allowed, self.custom, self.schemas)
            if call is None:
                if item.get("type") == "custom_tool_call":
                    raise ProtocolError("upstream custom tool call rejected: " + rejection.reason)
                self.office_calls.append(json.loads(json.dumps(item)))
                self._record_rejection(item, rejection)
            else:
                translated[index] = call
                valid_native.append(item)
        if not self.parallel and len(valid_native) > 1:
            raise ProtocolError('upstream returned parallel tools for a sequential request')
        events = []
        for index, item in translated.items():
            if item.get('type') in ('function_call', 'custom_tool_call'):
                self.client_calls.append(item)
                field = 'input' if item['type'] == 'custom_tool_call' else 'arguments'
                done_event = 'response.custom_tool_call_input.done' if field == 'input' else 'response.function_call_arguments.done'
                events.append(self._visible('response.output_item.added', {'output_index': index, 'item': item}))
                events.append(self._visible(done_event, {'output_index': index, 'item_id': item.get('id'),
                                                       'call_id': item['call_id'], 'name': item['name'], field: item[field]}))
                events.append(self._visible('response.output_item.done', {'output_index': index, 'item': item}))
            elif index not in self._indices:
                events.append(self._visible('response.output_item.added', {'output_index': index, 'item': item}))
                events.append(self._visible('response.output_item.done', {'output_index': index, 'item': item}))
            elif index not in self._finished:
                events.append(self._visible('response.output_item.done', {'output_index': index, 'item': item}))
        for item in valid_native:
            self.memory.remember(item, turn_id=self.turn_id, iteration=self.iteration)
        output = [translated[index] for index in sorted(translated, key=self._mapped)]
        self._terminal = True
        events.append(('response.completed', {**payload, 'type': 'response.completed',
                                             'response': {**response, 'status': 'completed', 'output': output}}))
        return events

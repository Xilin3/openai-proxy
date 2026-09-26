"""Completion checks for client tool calls; never execute tools in the proxy."""

from __future__ import annotations

import re

from bps_proxy.wire import EXECUTION_GUIDANCE, code_mode_exec, transport_shapes


class ToolSelectionError(ValueError):
    """A client requires a tool that is absent from its effective directory."""


def requires_tool_call(source: dict) -> bool:
    choice = source.get("tool_choice")
    return choice == "required" or isinstance(choice, dict)


def has_refusal(output: list[dict]) -> bool:
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "refusal" for part in content
        ):
            return True
    return False


# Deliberately narrow: a present-tense, first-person/session claim about the
# executor, not quoted examples, missing credentials, permissions or tool errors.
_DENIALS = (
    re.compile(
        r"^(?:(?:更准确地说|准确地说)[:：])?"
        r"(?:(?:当前|目前|这一轮|这轮|本轮|现在)(?:的)?(?:会话|对话|环境)?(?:里|中)?[，,：:]?)?"
        r"(?:我)?(?:当前|目前|现在)?(?:没有拿到|未拿到|没有|缺少)"
        r"(?:可调用的|可用的|任何|专门的)?"
        r"(?:本地(?:文件)?执行|终端(?:执行)?|文件(?:打包|执行|读写))(?:工具|入口|接口)"
        r"(?!时|的话|的情况下|的说法|这句话|这个说法)"
    ),
    re.compile(
        r"^(?:这一轮|这轮|本轮|目前|当前)?我(?:无法|不能)(?:发出|使用|调用)"
        r"(?:一次)?(?:真实的|可用的)?本地(?:执行|命令)(?:调用|工具|入口|接口)"
        r"(?!时|的话|的情况下|的说法|这句话|这个说法)"
    ),
    re.compile(
        r"^(?:(?:currently|in this (?:session|environment|chat))[, :]+)?"
        r"(?:i (?:do not|don't) have(?: access to)?|"
        r"this (?:session|environment|chat) (?:has no|does not (?:provide|have))) "
        r"(?:any |an? |available )?(?:local (?:execution|file execution|command execution)|"
        r"terminal|shell|file[- ]packaging) (?:tools?|access|interface|executor|capability)\b",
        re.IGNORECASE,
    ),
)
_ACCESS_FAILURE = re.compile(
    r"permission denied|access denied|not authorized|authorization|approval|sandbox|"
    r"权限|授权|许可|沙箱|凭证|拒绝访问|执行失败|调用失败|工具报错", re.IGNORECASE
)


def missing_call_reason(source: dict, tools: list[dict], output: list[dict]) -> str | None:
    if not tools or has_refusal(output):
        return None
    if requires_tool_call(source):
        return "tool_required"
    if not code_mode_exec(source) or not any(
        tool.get("name") == "exec" and tool.get("type") == "custom" for tool in tools
    ):
        return None
    for item in output:
        if (not isinstance(item, dict) or item.get("type") != "message"
                or item.get("role") != "assistant" or item.get("phase") == "commentary"):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        text = "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "output_text"
                       and isinstance(part.get("text"), str)).strip().replace("**", "")
        if _ACCESS_FAILURE.search(text):
            continue
        compact = re.sub(r"\s+", "", text)
        if any(pattern.match(compact if index < 2 else text)
               for index, pattern in enumerate(_DENIALS)):
            return "executor_unavailable_claim"
    return None


def missing_call_message(reason: str, tools: list[dict]) -> str:
    lines = ["The last response contained no client tool call."]
    if reason == "tool_required":
        lines.append("This request requires a call to one of the declared client tools before completion.")
    else:
        lines.append("The client has declared exec. Its local tools are accessed through the client's "
                     "tools namespace; they need not appear as separate transport tools. "
                     "Reassess the claim that no executor is available. Use a declared tool if the "
                     "user's pending task needs it, or answer directly if no execution is needed.")
    lines.extend(transport_shapes(tools))
    lines.append("Use run_officejs only as the transport, with code containing the JSON envelope. "
                 + EXECUTION_GUIDANCE)
    return " ".join(lines)

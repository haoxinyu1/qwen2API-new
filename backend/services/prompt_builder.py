import json
import logging
import uuid

log = logging.getLogger("qwen2api.prompt")

def _extract_text(content, user_tool_mode: bool = False) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        text_blocks = []
        other_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type", "")
            if t == "text":
                text_blocks.append(part.get("text", ""))
            elif t == "tool_use":
                inp = json.dumps(part.get("input", {}), ensure_ascii=False)
                other_parts.append(
                    f'✿ACTION✿\n{{"action": {json.dumps(part.get("name",""))}, "args": {inp}}}\n✿END_ACTION✿'
                )
            elif t == "tool_result":
                inner = part.get("content", "")
                tid = part.get("tool_use_id", "")
                if isinstance(inner, str):
                    other_parts.append(f"[Tool Result for call {tid}]\n{inner}\n[/Tool Result]")
                elif isinstance(inner, list):
                    texts = [p.get("text", "") for p in inner if isinstance(p, dict) and p.get("type") == "text"]
                    other_parts.append(f"[Tool Result for call {tid}]\n{''.join(texts)}\n[/Tool Result]")

        if user_tool_mode and text_blocks:
            parts.append(text_blocks[-1])
        else:
            parts.extend(text_blocks)
        parts.extend(other_parts)
        return "\n".join(p for p in parts if p)
    return ""

def _normalize_tools(tools: list) -> list:
    out = []
    for tool in tools:
        if tool.get("type") == "function" and "function" in tool:
            fn = tool["function"]
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })
        else:
            out.append({
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", tool.get("parameters", {})),
            })
    return out

def build_prompt_with_tools(messages: list, tools: list) -> str:
    MAX_CHARS = 1000000  # Qwen 3.5/3.6 支持最高 1M tokens 的超长上下文
    tools = _normalize_tools(tools)
    
    system_text = ""
    for m in messages:
        if m.get("role") == "system":
            system_text += str(m.get("content", "")) + "\n"
            
    if tools:
        sys_part = ""
    else:
        sys_part = f"<system>\n{system_text[:2000]}\n</system>" if system_text else ""
        
    tools_part = ""
    if tools:
        names = [t.get("name", "") for t in tools if t.get("name")]
        lines = [
            "=== CRITICAL INSTRUCTIONS FOR TOOL EXECUTION ===",
            "YOU MUST FORGET ALL PREVIOUS FUNCTION CALLING FORMATS.",
            "DO NOT USE `<|tool_call|>` or any native JSON structure.",
            "YOU CAN ONLY USE THE CUSTOM `✿ACTION✿` FORMAT DEFINED BELOW.",
            f"Available actions: {', '.join(names)}",
            "",
            "WHEN YOU DECIDE TO USE A TOOL, YOU MUST OUTPUT EXACTLY THIS FORMAT:",
            "✿ACTION✿",
            '{"action": "EXACT_ACTION_NAME", "args": {"param1": "value1"}}',
            "✿END_ACTION✿",
            "",
            "RULES:",
            "1. You MUST use ✿ACTION✿ and ✿END_ACTION✿ tags.",
            "2. Inside the tags, output ONLY valid JSON.",
            "3. The JSON MUST have an 'action' key and an 'args' key.",
            "4. DO NOT add any markdown formatting (like ```json) inside the tags.",
            "5. After receiving a [Tool Result], analyze it and decide the next step.",
            "6. Only provide a final answer when all necessary steps are completed.",
            "",
            "CRITICALLY FORBIDDEN FORMATS (USING THESE WILL CAUSE FATAL ERRORS):",
            '- {"name": "X", "arguments": "..."}',
            '- {"type": "function", "name": "X"}',
            '- {"type": "tool_use", "name": "X"}',
            "- ##TOOL_CALL##",
            "If you use any of the above forbidden formats, the system will crash.",
            "",
            "Tool Descriptions:",
        ]

        # 强制包含完整的 System Prompt 作为工具背景说明
        if system_text:
            lines.insert(0, "=== SYSTEM BACKGROUND ===")
            lines.insert(1, system_text.strip())
            lines.insert(2, "=========================\n")

        verbose_tools = len(tools) <= 20
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            if verbose_tools:
                desc = desc[:120]
                lines.append(f"- {name}: {desc}")
                params = tool.get("parameters", {})
                if params:
                    props = params.get("properties", {})
                    req = params.get("required", [])
                    if props:
                        ps = ", ".join(f"{k}({'req' if k in req else 'opt'})" for k in props)
                        lines.append(f"  params: {ps}")
            else:
                desc = desc[:60]
                lines.append(f"- {name}: {desc}")
        lines.append("=== END TOOL INSTRUCTIONS ===")
        tools_part = "\n".join(lines)

    overhead = len(sys_part) + len(tools_part) + 50
    budget = MAX_CHARS - overhead
    history_parts = []
    used = 0
    NEEDSREVIEW_MARKERS = ("需求回显", "已了解规则", "等待用户输入", "待执行任务", "待确认事项",
                           "[需求回显]", "**需求回显**")
    msg_count = 0
    
    for msg in reversed(messages):
        role = msg.get("role", "")
        if role not in ("user", "assistant", "system", "tool"):
            continue
        if tools and role == "system":
            continue

        if role == "tool":
            tool_content = msg.get("content", "") or ""
            tool_call_id = msg.get("tool_call_id", "")
            if isinstance(tool_content, list):
                tool_content = "\n".join(
                    p.get("text", "") for p in tool_content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            elif not isinstance(tool_content, str):
                tool_content = str(tool_content)
            if len(tool_content) > 100000:
                tool_content = tool_content[:100000] + "...[truncated]"
            line = f"[Tool Result]{(' id=' + tool_call_id) if tool_call_id else ''}\n{tool_content}\n[/Tool Result]"
            if used + len(line) + 2 > budget and history_parts:
                break
            history_parts.insert(0, line)
            used += len(line) + 2
            msg_count += 1
            continue

        text = _extract_text(msg.get("content", ""),
                             user_tool_mode=(bool(tools) and role == "user"))

        if role == "assistant" and msg.get("tool_calls"):
            tc_parts = []
            if text:
                tc_parts.append(text)
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, ValueError):
                    args = {"raw": args_str}
                tc_parts.append(
                    f'✿ACTION✿\n{{"action": {json.dumps(name)}, "args": {json.dumps(args, ensure_ascii=False)}}}\n✿END_ACTION✿'
                )
            text = "\n\n".join(tc_parts)

        if tools and role == "assistant" and any(m in text for m in NEEDSREVIEW_MARKERS):
            msg_count += 1
            continue
            
        is_tool_result = role == "user" and ("[Tool Result]" in text or "[tool result]" in text.lower()
                                              or text.startswith("{") or "\"results\"" in text[:100])
        max_len = 100000 if is_tool_result else 200000
        if len(text) > max_len:
            text = text[:max_len] + "...[truncated]"
        prefix = {"user": "Human: ", "assistant": "Assistant: ", "system": "System: "}.get(role, "")
        line = f"{prefix}{text}"
        if used + len(line) + 2 > budget and history_parts:
            break
        history_parts.insert(0, line)
        used += len(line) + 2
        msg_count += 1

    # 原始任务保护：若第一条 user 消息被挤出了历史窗口，强制补回最前
    if tools and messages:
        first_user = next((m for m in messages if m.get("role") == "user"), None)
        if first_user:
            t = _extract_text(first_user.get("content", ""), user_tool_mode=True)
            first_short = t[:800] + ("...[Original Task]" if len(t) > 800 else "")
            first_line = f"Human: {first_short}"
            if not history_parts or not history_parts[0].startswith(f"Human: {t[:60]}"):
                history_parts.insert(0, first_line)

    parts = []
    if sys_part: parts.append(sys_part)
    parts.extend(history_parts)
    if tools_part: parts.append(tools_part)
    parts.append("Assistant:")
    return "\n\n".join(parts)


import json
import logging
import re
import uuid
from typing import Any, cast

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE, OPENCLAW_OPENAI_PROFILE
from backend.core.request_logging import get_request_context
from backend.toolcall.normalize import build_tool_name_registry, normalize_tool_name
from backend.toolcall.parser import parse_tool_calls_detailed

__all__ = ["parse_tool_calls", "parse_tool_calls_detailed", "inject_format_reminder", "parse_tool_calls_silent"]

log = logging.getLogger("qwen2api.tool_parser")


CASE_SENSITIVE_TOOL_NAMES = {"Bash", "Edit", "Write", "Read", "Grep", "Glob", "WebFetch", "WebSearch"}


def _normalize_tool_name_case(name: str, tool_names: set[str]) -> str:
    if not isinstance(name, str) or not name:
        return name
    if name in tool_names:
        return name
    lowered = name.lower()
    for candidate in tool_names:
        if candidate.lower() == lowered:
            if candidate in CASE_SENSITIVE_TOOL_NAMES:
                return candidate
            return candidate
    return name


def _find_tool_use_json(text: str, tool_names: set[str]):
    i = 0
    while i < len(text):
        pos = text.find('{', i)
        if pos == -1:
            break
        depth = 0
        for j in range(pos, len(text)):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[pos:j + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and obj.get("type") == "tool_use" and obj.get("name"):
                            normalized_name = normalize_tool_name(obj.get("name", ""), tool_names)
                            if normalized_name in tool_names:
                                obj = dict(obj)
                                obj["name"] = normalized_name
                                return pos, obj

                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
        i = pos + 1

    return None


def _extract_first_xml_tool_call(text: str) -> str | None:
    wrapped_match = re.search(r"<tool_calls>\s*(<tool_call>[\s\S]*?</tool_call>)\s*</tool_calls>", text, re.IGNORECASE)
    if wrapped_match:
        return wrapped_match.group(1)

    tool_call_match = re.search(r"<tool_call>\s*(\{[\s\S]*?\}|[\s\S]*?)\s*</tool_call>", text, re.IGNORECASE)
    if tool_call_match:
        return tool_call_match.group(0)
    return None


def _extract_first_json_tool_call(text: str) -> str | None:
    normalized = text.strip()
    markers = [
        '<tool_call>{"name"',
        '<tool_calls><tool_call>{"name"',
        '{"name"',
        '"name":',
        '"name="',
        'function.name:',
    ]
    start_positions = [normalized.find(marker) for marker in markers if normalized.find(marker) != -1]
    if not start_positions:
        return None
    start = min(start_positions)
    candidate = normalized[start:]

    wrapped_match = re.search(r"<tool_calls>\s*(<tool_call>[\s\S]*?</tool_call>)\s*</tool_calls>", candidate, re.IGNORECASE)
    if wrapped_match:
        return wrapped_match.group(1)

    tool_call_match = re.search(r"<tool_call>\s*(\{[\s\S]*?\}|[\s\S]*?)\s*</tool_call>", candidate, re.IGNORECASE)
    if tool_call_match:
        return tool_call_match.group(0)

    json_start = candidate.find("{")
    if json_start == -1:
        return None
    depth = 0
    for idx in range(json_start, len(candidate)):
        ch = candidate[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return candidate[json_start:idx + 1]
    return candidate[json_start:]


def _normalize_fragmented_tool_call(answer: str) -> str:
    text = answer.strip()
    if "##TOOL_CALL##" in text and "##END_CALL##" in text:
        return text

    extracted_tool_call = _extract_first_xml_tool_call(text) or _extract_first_json_tool_call(text)
    if extracted_tool_call:
        return extracted_tool_call

    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"Tool\s+[A-Za-z0-9_.:-]*\s*does not exists?\\.?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```[\s\S]*?```", "", text)

    extracted_tool_call = _extract_first_xml_tool_call(text) or _extract_first_json_tool_call(text)
    if extracted_tool_call:
        return extracted_tool_call

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[•●·\-*]+\s*", "", line)
        line = line.replace("END_CALL##", "##END_CALL##")
        if line:
            lines.append(line)

    normalized = "\n".join(lines)
    if "TOOL_CALL##" in normalized and "##TOOL_CALL##" not in normalized:
        normalized = normalized.replace("TOOL_CALL##", "##TOOL_CALL##")
    if "##END_CALL##" in normalized and "##TOOL_CALL##" not in normalized and '"name"' in normalized:
        normalized = f"##TOOL_CALL##\n{normalized}"
    return normalized


def _coerce_tool_input(name: str, input_data: Any, tools: list[dict[str, Any]]) -> Any:
    del name
    if not isinstance(input_data, dict):
        return input_data

    query_value = input_data.get("query")
    queries = input_data.get("queries")
    if query_value or "queries" not in input_data:
        return input_data
    if not any(isinstance(tool, dict) and isinstance(tool.get("parameters"), dict) and isinstance(tool["parameters"].get("properties"), dict) and "query" in tool["parameters"]["properties"] for tool in tools):
        return input_data

    if isinstance(queries, list):
        merged = "\n".join(str(item).strip() for item in queries if str(item).strip())
        if merged:
            coerced = dict(input_data)
            coerced.pop("queries", None)
            coerced["query"] = merged
            return coerced
    if isinstance(queries, str) and queries.strip():
        coerced = dict(input_data)
        coerced.pop("queries", None)
        coerced["query"] = queries.strip()
        return coerced

    return input_data


def parse_tool_calls(answer: str, tools: list):
    return _parse_tool_calls(answer, tools, emit_logs=True)


def parse_tool_calls_silent(answer: str, tools: list):
    return _parse_tool_calls(answer, tools, emit_logs=False)


def _parse_tool_calls(answer: str, tools: list, *, emit_logs: bool):
    answer = _normalize_fragmented_tool_call(answer)
    ctx = get_request_context()
    req_tag = f"req={ctx.get('req_id', '-')} chat={ctx.get('chat_id', '-')}"
    if not tools:
        return [{"type": "text", "text": answer}], "end_turn"
    tool_names = {t.get("name") for t in tools if t.get("name")}
    tool_registry = build_tool_name_registry(tool_names)

    def _log_debug(message: str) -> None:
        if emit_logs:
            log.debug(message)

    def _log_info(message: str) -> None:
        if emit_logs:
            log.info(message)

    def _log_warning(message: str) -> None:
        if emit_logs:
            log.warning(message)

    _log_debug(f"[ToolParse] [{req_tag}] 原始回复({len(answer)}字): {answer[:200]!r}")

    def _make_tool_block(name, input_data, prefix=""):
        normalized_name = normalize_tool_name(name, tool_registry.values())
        cased_name = _normalize_tool_name_case(normalized_name, tool_names)
        if cased_name not in tool_names:
            _log_warning(f"[ToolParse] 工具名不匹配，回退为普通文本: name={name!r}, normalized={normalized_name!r}, cased={cased_name!r}, tools={tool_names}")
            return [{"type": "text", "text": answer}], "end_turn"
        coerced_input = _coerce_tool_input(cased_name, input_data, tools)
        tool_id = f"toolu_{uuid.uuid4().hex[:8]}"
        blocks = []
        if prefix:
            blocks.append({"type": "text", "text": prefix})
        blocks.append({"type": "tool_use", "id": tool_id, "name": cased_name, "input": coerced_input})
        _log_info(f"[ToolParse] 返回工具块: original={name!r}, normalized={normalized_name!r}, final={cased_name!r}, input={json.dumps(coerced_input, ensure_ascii=False)[:200]}")
        return blocks, "tool_use"

    detailed = parse_tool_calls_detailed(answer, tool_names)
    detailed_calls = cast(list[dict[str, Any]], detailed["calls"])
    if detailed_calls:
        first_call = detailed_calls[0]
        _log_info(f"[ToolParse] ✓ 详细解析格式: source={detailed['source']}, name={first_call['name']!r}, input={json.dumps(first_call['input'], ensure_ascii=False)[:200]}")
        return _make_tool_block(first_call["name"], first_call["input"])

    tc_m = re.search(r'##TOOL_CALL##\s*(.*?)\s*##END_CALL##', answer, re.DOTALL | re.IGNORECASE)
    if tc_m:
        try:
            obj = json.loads(tc_m.group(1))
            name = obj.get("name", "")
            inp = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except Exception:
                    inp = {"value": inp}
            prefix = answer[:tc_m.start()].strip()
            _log_info(f"[ToolParse] ✓ ##TOOL_CALL## 格式: name={name!r}, input={str(inp)[:120]}")
            return _make_tool_block(name, inp, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            _log_warning(f"[ToolParse] ##TOOL_CALL## 格式解析失败: {e}, content={tc_m.group(1)[:100]!r}")

    xml_m = re.search(r'<tool_call>\s*(.*?)\s*</tool_call>', answer, re.DOTALL | re.IGNORECASE)
    if xml_m:
        try:
            obj = json.loads(xml_m.group(1))
            name = obj.get("name", "")
            inp = obj.get("input", obj.get("args", obj.get("arguments", obj.get("parameters", {}))))
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except Exception:
                    inp = {"value": inp}
            prefix = answer[:xml_m.start()].strip()
            _log_info(f"[ToolParse] ✓ XML格式 <tool_call>: name={name!r}, input={str(inp)[:120]}")
            return _make_tool_block(name, inp, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            _log_warning(f"[ToolParse] XML格式解析失败: {e}, content={xml_m.group(1)[:100]!r}")

    cb_m = re.search(r'```tool_call\s*\n(.*?)\n```', answer, re.DOTALL)
    if cb_m:
        try:
            obj = json.loads(cb_m.group(1).strip())
            name = obj.get("name", "")
            inp = obj.get("input", obj.get("args", {}))
            if isinstance(inp, str):
                try:
                    inp = json.loads(inp)
                except Exception:
                    inp = {"value": inp}
            prefix = answer[:cb_m.start()].strip()
            _log_info(f"[ToolParse] ✓ 代码块格式 tool_call: name={name!r}, input={str(inp)[:120]}")
            return _make_tool_block(name, inp, prefix)
        except (json.JSONDecodeError, ValueError) as e:
            _log_warning(f"[ToolParse] 代码块格式解析失败: {e}")

    stripped = re.sub(r'```json\s*\n?', '', answer)
    stripped = re.sub(r'\n?```', '', stripped)
    result = _find_tool_use_json(stripped, tool_names)
    if result:
        pos, tool_call = result
        prefix = stripped[:pos].strip()
        tool_id = tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:8]}"
        _log_info(f"[ToolParse] ✓ 旧JSON格式 tool_call: name={tool_call['name']!r}")
        blocks = []
        if prefix:
            blocks.append({"type": "text", "text": prefix})
        blocks.append({
            "type": "tool_use",
            "id": tool_id,
            "name": tool_call["name"],
            "input": _coerce_tool_input(tool_call["name"], tool_call.get("input", {}), tools),
        })
        return blocks, "tool_use"

    _log_warning(f"[ToolParse] ✗ 未检测到工具调用，作为普通文本返回。工具列表: {tool_names}")
    return [{"type": "text", "text": answer}], "end_turn"


def inject_format_reminder(prompt: str, tool_name: str, *, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    """Inject a format correction reminder into the prompt before the final 'Assistant:' tag.
    Used when Qwen server returns 'Tool X does not exists.' (native call was intercepted)."""
    if client_profile == CLAUDE_CODE_OPENAI_PROFILE:
        reminder = (
            f"[CORRECTION]: You called '{tool_name}' using the wrong tool-call format, and the server rejected it with "
            f"'Tool {tool_name} does not exists.'. Reissue the tool call as a single valid JSON object only:\n"
            f'{{"name": {json.dumps(tool_name)}, "input": {{...your args here...}}}}\n'
            "Do not wrap it in ##TOOL_CALL## blocks. Do not use XML tags. Do not add explanation text before or after the JSON.\n"
        )
    else:
        reminder = (
            f"[CORRECTION]: You called '{tool_name}' using the WRONG format — "
            f"the server BLOCKED it with 'Tool {tool_name} does not exists.'. "
            f"You MUST use ##TOOL_CALL## format and NOTHING ELSE:\n"
            f"##TOOL_CALL##\n"
            f'{{"name": {json.dumps(tool_name)}, "input": {{...your args here...}}}}\n'
            f"##END_CALL##\n"
            f"DO NOT use JSON without delimiters. DO NOT use any XML tags. ONLY ##TOOL_CALL##.\n"
        )
    prompt = prompt.rstrip()
    if prompt.endswith("Assistant:"):
        return prompt[: -len("Assistant:")] + reminder + "\nAssistant:"
    return prompt + "\n\n" + reminder + "\nAssistant:"



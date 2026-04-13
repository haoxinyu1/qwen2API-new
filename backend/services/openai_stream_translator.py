from __future__ import annotations

import json
from typing import Any, Callable

from backend.runtime.execution import RuntimeToolDirective


class OpenAIStreamTranslator:
    def __init__(
        self,
        *,
        completion_id: str,
        created: int,
        model_name: str,
        build_final_directive: Callable[[str], RuntimeToolDirective] | None = None,
    ):
        self.completion_id = completion_id
        self.created = created
        self.model_name = model_name
        self.build_final_directive = build_final_directive
        self.pending_chunks: list[str] = []
        self.role_chunk_sent = False
        self.emitted_tool_index = 0
        self.answer_fragments: list[str] = []
        self.tool_calls_emitted = False

    def on_delta(self, evt: dict[str, Any], text_chunk: str | None, tool_calls: list[dict[str, Any]] | None) -> None:
        if not self.role_chunk_sent:
            yield_payload = {
                "id": self.completion_id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model_name,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            self.pending_chunks.append(f"data: {json.dumps(yield_payload, ensure_ascii=False)}\n\n")
            self.role_chunk_sent = True

        if text_chunk and evt.get("phase") in ("think", "thinking_summary"):
            return

        if text_chunk and evt.get("phase") == "answer":
            self.answer_fragments.append(text_chunk)
            self.pending_chunks.append(
                f"data: {json.dumps({'id': self.completion_id, 'object': 'chat.completion.chunk', 'created': self.created, 'model': self.model_name, 'choices': [{'index': 0, 'delta': {'content': text_chunk}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
            )
            return

        if tool_calls:
            self.emit_tool_calls(tool_calls)

    def emit_tool_calls(self, tool_calls: list[dict[str, Any]]) -> None:
        for tool_call in tool_calls:
            idx = self.emitted_tool_index
            self.emitted_tool_index += 1
            self.pending_chunks.append(
                f"data: {json.dumps({'id': self.completion_id, 'object': 'chat.completion.chunk', 'created': self.created, 'model': self.model_name, 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': idx, 'id': tool_call['id'], 'type': 'function', 'function': {'name': tool_call['name'], 'arguments': json.dumps(tool_call['input'], ensure_ascii=False)}}]}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
            )
        if tool_calls:
            self.tool_calls_emitted = True

    def finalize(self, finish_reason: str) -> list[str]:
        final_finish_reason = finish_reason
        if self.build_final_directive is not None and not self.tool_calls_emitted:
            directive = self.build_final_directive("".join(self.answer_fragments))
            if directive.stop_reason == "tool_use":
                tool_calls = [
                    {
                        "id": block["id"],
                        "name": block["name"],
                        "input": block.get("input", {}),
                    }
                    for block in directive.tool_blocks
                    if block.get("type") == "tool_use"
                ]
                if tool_calls:
                    self.emit_tool_calls(tool_calls)
                    final_finish_reason = "tool_calls"

        chunks = list(self.pending_chunks)
        chunks.append(
            f"data: {json.dumps({'id': self.completion_id, 'object': 'chat.completion.chunk', 'created': self.created, 'model': self.model_name, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': final_finish_reason}]}, ensure_ascii=False)}\n\n"
        )
        chunks.append("data: [DONE]\n\n")
        return chunks

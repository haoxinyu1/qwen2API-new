from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable
from backend.adapter.standard_request import StandardRequest
from backend.core.config import resolve_model, settings
from backend.core.request_logging import new_request_id, request_context, update_request_context
from backend.services.attachment_preprocessor import preprocess_attachments
from backend.services.auth_quota import resolve_auth_context
from backend.services.completion_bridge import run_retryable_completion_bridge
from backend.services.openai_stream_translator import OpenAIStreamTranslator
from backend.services.prompt_builder import messages_to_prompt
from backend.services.response_formatters import build_openai_completion_payload
from backend.services.qwen_client import QwenClient
from backend.toolcall.normalize import build_tool_name_registry
from backend.runtime.execution import RuntimeAttemptState, build_tool_directive, retryable_usage_delta

log = logging.getLogger("qwen2api.chat")
router = APIRouter()
OpenAIDeltaHandler = Callable[[dict[str, Any], str | None, list[dict[str, Any]] | None], Awaitable[None]]


def _build_standard_request(req_data: dict) -> StandardRequest:
    requested_model = req_data.get("model", "gpt-3.5-turbo")
    prompt_result = messages_to_prompt(req_data)
    prompt = prompt_result.prompt
    tools = prompt_result.tools
    tool_names = [tool_name for tool_name in (tool.get("name") for tool in tools) if isinstance(tool_name, str) and tool_name]
    log.info("[OAI] normalized tools=%s", tool_names)
    return StandardRequest(
        prompt=prompt,
        response_model=requested_model,
        resolved_model=resolve_model(requested_model),
        surface="openai",
        stream=req_data.get("stream", False),
        tools=tools,
        tool_names=tool_names,
        tool_name_registry=build_tool_name_registry(tool_names),
        tool_enabled=prompt_result.tool_enabled,
    )


@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    app = request.app
    users_db = app.state.users_db
    client: QwenClient = app.state.qwen_client

    auth = await resolve_auth_context(request, users_db)
    token = auth.token

    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})

    standard_request = _build_standard_request(req_data)
    file_store = getattr(app.state, "file_store", None)
    if file_store is not None:
        preprocessed = await preprocess_attachments(req_data, file_store)
        req_data = preprocessed.payload
        standard_request = _build_standard_request(req_data)
        standard_request.attachments = preprocessed.attachments
        standard_request.uploaded_file_ids = preprocessed.uploaded_file_ids
    model_name = standard_request.response_model
    qwen_model = standard_request.resolved_model
    prompt = standard_request.prompt
    tools = standard_request.tools
    history_messages = req_data.get("messages", [])

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    with request_context(req_id=new_request_id(), surface="openai", requested_model=model_name, resolved_model=qwen_model):
        log.info(
            "[OAI] model=%s stream=%s tool_enabled=%s tools=%s prompt_len=%s prompt_tail=%r",
            qwen_model,
            standard_request.stream,
            standard_request.tool_enabled,
            [t.get('name') for t in tools],
            len(prompt),
            prompt[-500:],
        )

        if standard_request.stream:
            async def generate():
                try:
                    update_request_context(stream_attempt=1)
                    translator = OpenAIStreamTranslator(
                        completion_id=completion_id,
                        created=created,
                        model_name=model_name,
                        build_final_directive=lambda answer_text: build_tool_directive(
                            standard_request,
                            RuntimeAttemptState(answer_text=answer_text),
                        ),
                    )

                    async def on_delta(evt: dict[str, Any], text_chunk: str | None, tool_calls: list[dict[str, Any]] | None) -> None:
                        translator.on_delta(evt, text_chunk, tool_calls)

                    delta_handler: OpenAIDeltaHandler = on_delta
                    result = await run_retryable_completion_bridge(
                        client=client,
                        standard_request=standard_request,
                        prompt=prompt,
                        users_db=users_db,
                        token=token,
                        history_messages=history_messages,
                        max_attempts=settings.MAX_RETRIES + (1 if standard_request.tools else 0),
                        usage_delta_factory=retryable_usage_delta(prompt),
                        allow_after_visible_output=False,
                        capture_events=False,
                        on_delta=delta_handler,
                    )
                    execution = result.execution
                    final_finish_reason = "tool_calls" if execution.state.tool_calls else execution.state.finish_reason
                    for chunk in translator.finalize(final_finish_reason):
                        yield chunk
                    return
                except HTTPException as he:
                    yield f"data: {json.dumps({'error': he.detail})}\n\n"
                    return
                except Exception as e:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    return

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            update_request_context(stream_attempt=1)
            result = await run_retryable_completion_bridge(
                client=client,
                standard_request=standard_request,
                prompt=prompt,
                users_db=users_db,
                token=token,
                history_messages=history_messages,
                max_attempts=settings.MAX_RETRIES + (1 if standard_request.tools else 0),
                usage_delta_factory=retryable_usage_delta(prompt),
                allow_after_visible_output=False,
            )
            execution = result.execution

            return JSONResponse(build_openai_completion_payload(
                completion_id=completion_id,
                created=created,
                model_name=model_name,
                prompt=prompt,
                execution=execution,
                standard_request=standard_request,
            ))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
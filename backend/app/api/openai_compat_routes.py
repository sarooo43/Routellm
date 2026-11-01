"""OpenAI-compatible text-chat adapter over the existing RouteLLM executor.

Mounted at /v1, independently of the richer /api/v1 API. This module only
translates requests/responses and error envelopes: classification, scoring,
quota checks, circuit breaking, fallback, and persistence stay in the executor.
Streaming, multimodal messages, and tool calls are not supported in this phase.
"""

import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_key, require_provider_api_keys
from app.core.api_key_manager import record_api_key_usage
from app.core.executor import AttemptLog, ExecutionResult, execute_with_fallback
from app.db.session import get_session
from app.models import ApiKeyDB
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)

AUTO_MODEL = "routellm-auto"
MODEL_OWNERS = {"groq": "groq", "gemini": "google", "deepseek": "deepseek"}


class OpenAIChatMessage(BaseModel):
    """A text-only message in an OpenAI chat conversation."""

    role: Literal["user", "system", "assistant"]
    content: str


class OpenAIChatRequest(BaseModel):
    """The supported OpenAI chat request, plus RouteLLM's task_type override."""

    model: str
    messages: list[OpenAIChatMessage] = Field(min_length=1)
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    stream: Optional[bool] = False
    task_type: Optional[str] = None


class OpenAIErrorDetail(BaseModel):
    """OpenAI's error detail; optional fields are omitted when unavailable."""

    message: str
    type: str
    code: Optional[str] = None
    param: Optional[str] = None


class OpenAIErrorResponse(BaseModel):
    """The top-level error envelope understood by OpenAI SDKs."""

    error: OpenAIErrorDetail


class OpenAIUsage(BaseModel):
    """Token counts, with zero for counts the current executor cannot supply."""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class OpenAIResponseMessage(BaseModel):
    """The assistant message returned in a completion choice."""

    role: Literal["assistant"] = "assistant"
    content: str


class OpenAIChatChoice(BaseModel):
    """A single completion choice (the executor produces one response)."""

    index: int = 0
    message: OpenAIResponseMessage
    # "error" is the requested adapter extension; upstream failures normally
    # use the HTTP error envelope below rather than a successful completion.
    finish_reason: Literal["stop", "error"]


class RouteLLMMetadata(BaseModel):
    """Routing transparency carried in a non-standard response extension."""

    confidence_score: float
    fallback_chain_attempted: list[AttemptLog]
    routing_method: Literal["ml_hybrid", "rule_based"]


class OpenAIChatResponse(BaseModel):
    """OpenAI's chat.completion shape plus x_routellm_metadata."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[OpenAIChatChoice]
    usage: OpenAIUsage
    x_routellm_metadata: RouteLLMMetadata


class OpenAIModel(BaseModel):
    """One model entry in OpenAI's model-discovery response."""

    id: str
    object: Literal["model"] = "model"
    # Required by the real SDK even though some gateway examples omit it.
    # The registry has no registration timestamps, so expose 0 (unknown).
    created: int = 0
    owned_by: str


class OpenAIModelList(BaseModel):
    """OpenAI's model-list envelope."""

    object: Literal["list"] = "list"
    data: list[OpenAIModel]


def _error_response(
    status_code: int,
    message: str,
    error_type: str,
    code: str,
    param: Optional[str] = None,
) -> JSONResponse:
    """Serialize an error without leaking provider errors or invalid inputs."""
    body = OpenAIErrorResponse(
        error=OpenAIErrorDetail(
            message=message, type=error_type, code=code, param=param
        )
    )
    return JSONResponse(
        status_code=status_code, content=body.model_dump(exclude_none=True)
    )


class OpenAICompatRoute(APIRoute):
    """Scope OpenAI-style validation/error translation to this router only.

    A global validation handler would change the legacy API's 422 responses.
    Keeping the translation on this route class leaves /api/v1 untouched.
    """

    def get_route_handler(self):
        """Wrap FastAPI validation and unexpected errors for SDK clients."""
        original_handler = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await original_handler(request)
            except RequestValidationError as exc:
                first = exc.errors()[0]
                param = ".".join(str(part) for part in first["loc"][1:]) or None
                location = f" ({param})" if param else ""
                return _error_response(
                    400,
                    f"Invalid request{location}: {first['msg']}",
                    "invalid_request_error",
                    "invalid_request",
                    param,
                )
            except HTTPException as exc:
                code = "invalid_api_key" if exc.status_code == 401 else "request_error"
                return _error_response(
                    exc.status_code,
                    str(exc.detail),
                    "invalid_request_error" if exc.status_code < 500 else "server_error",
                    code,
                )
            except Exception as exc:
                # Do not return exception text: it can contain upstream details.
                logger.error(
                    "openai_compat_unexpected_error", path=request.url.path, error=str(exc)
                )
                return _error_response(
                    500, "Internal server error", "server_error", "internal_error"
                )

        return handle


router = APIRouter(prefix="/v1", tags=["OpenAI compatible"], route_class=OpenAICompatRoute)


def _messages_to_prompt(messages: list[OpenAIChatMessage]) -> str:
    """Flatten role-labelled messages in their original chronological order."""
    # Known phase simplification: the executor accepts one prompt, not native
    # conversation state. True multi-turn/context-window management (including
    # truncation and provider-specific token budgets) is a future enhancement.
    return "\n\n".join(
        f"{message.role.capitalize()}: {message.content}" for message in messages
    )


def _chat_response(result: ExecutionResult) -> OpenAIChatResponse:
    """Translate a successful ExecutionResult without rerouting or extra calls."""
    # ProviderResponse currently exposes only an aggregate token count. Do not
    # invent a prompt/completion split or double-count the aggregate: 0 means
    # prompt usage is unavailable; completion_tokens carries the reported count
    # so prompt_tokens + completion_tokens still equals the reported total.
    tokens = max(0, result.tokens_used)
    return OpenAIChatResponse(
        id=f"chatcmpl-{result.request_id}",
        created=int(time.time()),
        model=result.model_used or "",
        choices=[
            OpenAIChatChoice(
                message=OpenAIResponseMessage(content=result.result_text),
                finish_reason="stop" if result.success else "error",
            )
        ],
        usage=OpenAIUsage(
            prompt_tokens=0, completion_tokens=tokens, total_tokens=tokens
        ),
        x_routellm_metadata=RouteLLMMetadata(
            confidence_score=result.confidence_score,
            fallback_chain_attempted=[
                attempt.model_dump() for attempt in result.fallback_chain_attempted
            ],
            routing_method=result.routing_method,
        ),
    )


@router.post(
    "/chat/completions",
    response_model=OpenAIChatResponse,
    responses={
        400: {"model": OpenAIErrorResponse},
        500: {"model": OpenAIErrorResponse},
        502: {"model": OpenAIErrorResponse},
    },
)
async def chat_completions(
    body: OpenAIChatRequest,
    db_session: AsyncSession = Depends(get_session),
    api_key: ApiKeyDB = Depends(require_api_key),
    provider_api_keys: dict[str, str] = Depends(require_provider_api_keys),
):
    """Run text chat through RouteLLM's existing routing/forced-provider paths."""
    if body.stream:
        return _error_response(
            400,
            "Streaming is not yet supported",
            "invalid_request_error",
            "streaming_not_supported",
        )

    if body.model != AUTO_MODEL and body.model not in PROVIDER_REGISTRY:
        valid_models = ", ".join([AUTO_MODEL, *PROVIDER_REGISTRY])
        return _error_response(
            400,
            f"Unknown model '{body.model}'. Valid options: {valid_models}",
            "invalid_request_error",
            "model_not_found",
        )

    result = await execute_with_fallback(
        _messages_to_prompt(body.messages),
        db_session,
        task_type=body.task_type,
        force_provider=None if body.model == AUTO_MODEL else body.model,
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        provider_api_keys=provider_api_keys,
    )
    logger.info(
        "openai_compat_request_handled",
        request_id=result.request_id,
        success=result.success,
        model_used=result.model_used,
    )
    if not result.success:
        # A named provider has a one-provider chain; its failure is also an
        # upstream failure, never an excuse to fall back to another provider.
        return _error_response(
            502,
            "All providers failed to generate a response",
            "upstream_error",
            "all_providers_failed",
        )
    await record_api_key_usage(db_session, api_key, result.tokens_used)
    return _chat_response(result)


@router.get("/models", response_model=OpenAIModelList)
async def list_models() -> OpenAIModelList:
    """List the automatic router and every currently registered provider."""
    models = [OpenAIModel(id=AUTO_MODEL, owned_by="routellm")]
    models.extend(
        OpenAIModel(id=name, owned_by=MODEL_OWNERS.get(name, name))
        for name in PROVIDER_REGISTRY
    )
    return OpenAIModelList(data=models)

"""One bounded OpenAI-compatible call; no implicit retries or raw SDK error exposure."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from pydantic import ValidationError

from ..domain import BusinessError
from .config import ProductionSettings, strict_integer
from .models import ModelPublic, OutlineInput, OutlineItem

_PRIVATE_CALL = contextvars.ContextVar("production_model_private_call",default=False)
MAX_RESPONSE_BYTES = 204800
MAX_PAYLOAD_BYTES = 2097152


class _PrivateLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_CALL.get()


class ModelFailure(Exception):
    """Safe status only. accepted=False is a transport proof, never a status guess."""
    def __init__(self, code: str, accepted: bool | None):
        if code not in {"not_sent","response_unknown","authentication","invalid_response","output_too_large","invalid_usage"} or (accepted is not None and type(accepted) is not bool):
            raise BusinessError("invalid_model_failure","模型错误状态无效。",409)
        self.code = code
        self.accepted = accepted
        super().__init__(code)

    def __repr__(self) -> str:
        return f"ModelFailure(code={self.code!r}, accepted={self.accepted!r})"


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self):
        strict_integer(self.input_tokens)
        strict_integer(self.output_tokens)
        strict_integer(self.input_tokens+self.output_tokens)


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[dict[str,str],...] = field(repr=False)
    max_output_tokens: int
    config: ModelPublic

    def __post_init__(self):
        if type(self.messages) is not tuple or not 1 <= len(self.messages) <= 8:
            raise BusinessError("invalid_model_request","模型请求字段无效。",409)
        strict_integer(self.max_output_tokens,minimum=1)
        if self.max_output_tokens>4000 or not isinstance(self.config,ModelPublic):
            raise BusinessError("invalid_model_request","模型请求字段无效。",409)
        try:
            ModelPublic.model_validate(self.config.model_dump(warnings=False))
        except ValidationError:
            raise BusinessError("invalid_model_request","模型请求字段无效。",409) from None
        for item in self.messages:
            if type(item) is not dict or set(item)!={"role","content"} or item["role"] not in ("system","user") or type(item["content"]) is not str:
                raise BusinessError("invalid_model_request","模型请求字段无效。",409)
        object.__setattr__(self,"messages",tuple(dict(item) for item in self.messages))


@dataclass(frozen=True)
class ModelResult:
    text: str = field(repr=False)
    usage: Usage | None
    provider_request_id: str | None


class ModelClient(Protocol):
    async def generate(self, request: ModelRequest, *, deadline_seconds: int = 120) -> ModelResult: ...


def serialize_model_payload(request: ModelRequest) -> bytes:
    if not isinstance(request,ModelRequest):
        raise BusinessError("invalid_model_request","模型请求字段无效。",409)
    request.__post_init__()
    try:
        payload = json.dumps({"messages":request.messages,"model":request.config.model,"max_tokens":request.max_output_tokens,"stream":False,
            "thinking":{"type":"disabled"}},
            ensure_ascii=False,separators=(",",":"),allow_nan=False).encode("utf-8")
    except (UnicodeError,TypeError,ValueError):
        raise BusinessError("invalid_model_request","模型请求编码无效。",409) from None
    if len(payload)>MAX_PAYLOAD_BYTES:
        raise BusinessError("model_request_too_large","模型请求过大，请减少输入。",409)
    return payload


def estimated_tokens(request: ModelRequest) -> int:
    context = request.config.context_tokens
    if context is None:
        raise BusinessError("model_context_missing","未配置模型上下文限额，请联系维护者。",409)
    total = len(serialize_model_payload(request))+2048+request.max_output_tokens
    if total>context:
        raise BusinessError("model_context_exceeded","输入与输出预留超出模型上下文，请减少输入。",409)
    return strict_integer(total)


def retry_delay(error: ModelFailure, retry_index: int) -> int | None:
    if not isinstance(error,ModelFailure) or type(retry_index) is not int or error.code!="not_sent" or error.accepted is not False or retry_index not in (0,1):
        return None
    return (2,8)[retry_index]


def _json(text: str | bytes):
    def pairs(items):
        if len(dict(items))!=len(items):
            raise ValueError
        return dict(items)
    return json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def parse_outline(text: str, *, chapter_count: int) -> OutlineInput:
    failed = False
    try:
        if type(text) is not str or type(chapter_count) is not int or not 1<=chapter_count<=60 or not text.strip() or len(text.encode("utf-8"))>MAX_RESPONSE_BYTES:
            raise ValueError
        data = _json(text)
        if type(data) is not dict or set(data)!={"items"} or type(data["items"]) is not list or len(data["items"])!=chapter_count:
            raise ValueError
        items = []
        for item in data["items"]:
            if type(item) is not dict or set(item) not in ({"title","goal"},{"chapter_id","title","goal"}):
                raise ValueError
            items.append(OutlineItem(chapter_id=None,title=item["title"],goal=item["goal"]))
        result = OutlineInput(items=items)
    except (ValueError,TypeError,KeyError,UnicodeError,ValidationError):
        failed = True
    if failed:
        raise ModelFailure("invalid_response",accepted=True)
    return result


class _BodyWitness(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream
        self.started = False

    async def __aiter__(self):
        async for data in self.stream:
            self.started = True
            yield data

    async def aclose(self):
        await self.stream.aclose()


class _NeverSent(Exception):
    pass


class _ResponseLimit(httpx.ReadError):
    def __init__(self, accepted: bool | None):
        self.accepted = accepted
        super().__init__("model_response_limit")


class _LimitedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream, accepted: bool | None):
        self.stream = stream
        self.accepted = accepted

    async def __aiter__(self):
        size = 0
        try:
            async for chunk in self.stream:
                size += len(chunk)
                if size > MAX_PAYLOAD_BYTES:
                    raise _ResponseLimit(self.accepted)
                yield chunk
        finally:
            await self.stream.aclose()

    async def aclose(self):
        await self.stream.aclose()


async def _bound_response(response: httpx.Response):
    # Runs before the SDK can eagerly read a non-2xx error response.
    if _PRIVATE_CALL.get():
        response.stream = _LimitedResponseStream(response.stream,
            True if 200 <= response.status_code < 300 else None)


class _AcceptanceTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        # The installed HTTPX transport feeds request.stream into httpcore.
        # A ConnectError before that stream yields ANY bytes proves no JSON body
        # was supplied. Errors from a caller-supplied transport remain unknown.
        witness = _BodyWitness(request.stream)
        request.stream = witness
        failed = False
        try:
            return await super().handle_async_request(request)
        except httpx.ConnectError:
            if not witness.started:
                failed = True
            else:
                raise
        if failed:
            raise _NeverSent()


class OpenAIModel:
    def __init__(self, config: ProductionSettings, *, http_client: httpx.AsyncClient | None = None):
        self.config = config
        self.http_client = http_client
        # SDK DEBUG logs include prompts/bodies; context-local suppression also
        # covers SDK exception traceback records, without changing log levels.
        for name in ("openai._base_client","httpx","httpcore.connection","httpcore.connection_pool","httpcore.http11","httpcore.http2","httpcore.proxy"):
            logger = logging.getLogger(name)
            if not any(isinstance(item,_PrivateLogFilter) for item in logger.filters):
                logger.addFilter(_PrivateLogFilter())

    async def generate(self, request: ModelRequest, *, deadline_seconds: int = 120) -> ModelResult:
        self.config.require_execution(request.config)
        from .prompts import require_request_limits

        require_request_limits(request)
        estimated_tokens(request)
        if type(deadline_seconds) is not int or not 1<=deadline_seconds<=120:
            raise BusinessError("invalid_model_deadline","模型调用截止时间无效。",409)
        from openai import AsyncOpenAI

        marker = _PRIVATE_CALL.set(True)
        outcome = None
        client = self.http_client or httpx.AsyncClient(transport=_AcceptanceTransport(retries=0),trust_env=False,follow_redirects=False,timeout=deadline_seconds)
        client.event_hooks["response"].insert(0, _bound_response)
        try:
            async with AsyncOpenAI(api_key=self.config.api_key,base_url=self.config.base_url,http_client=client,max_retries=0,timeout=deadline_seconds) as sdk:
                async with asyncio.timeout(deadline_seconds):
                    async with sdk.chat.completions.with_streaming_response.create(
                        messages=request.messages,model=request.config.model,max_tokens=request.max_output_tokens,stream=False,
                        extra_body={"thinking":{"type":"disabled"}}
                    ) as response:
                        body = bytearray()
                        async for chunk in response.iter_bytes():
                            # Bounded raw envelope accommodates escaped 200KiB text.
                            if len(body)+len(chunk)>MAX_PAYLOAD_BYTES:
                                outcome = ModelFailure("output_too_large",accepted=True)
                                break
                            body.extend(chunk)
                        if outcome is None:
                            outcome = self._result(bytes(body),request)
        except Exception as exc:
            from openai import APIStatusError
            if isinstance(exc,APIStatusError) and exc.status_code in (401,403):
                outcome = ModelFailure("authentication",accepted=None)
            elif isinstance(exc.__cause__,_NeverSent):
                outcome = ModelFailure("not_sent",accepted=False)
            elif isinstance(exc,_ResponseLimit):
                outcome = ModelFailure("output_too_large" if exc.accepted else "response_unknown",accepted=exc.accepted)
            elif isinstance(exc,ModelFailure):
                outcome = ModelFailure(exc.code,exc.accepted)
            else:
                outcome = ModelFailure("response_unknown",accepted=None)
        finally:
            client.event_hooks["response"].remove(_bound_response)
            _PRIVATE_CALL.reset(marker)
        # Raise outside the SDK except suite: no raw exception context retained.
        if isinstance(outcome,ModelFailure):
            raise outcome
        return outcome

    @staticmethod
    def _result(body: bytes, request: ModelRequest) -> ModelResult:
        failed = None
        try:
            data = _json(body)
            choices = data["choices"]
            if type(choices) is not list or len(choices)!=1 or choices[0].get("finish_reason")!="stop" or choices[0].get("index")!=0:
                raise ValueError
            message = choices[0]["message"]
            text = message["content"]
            if message.get("role")!="assistant" or message.get("tool_calls") or message.get("function_call") or message.get("reasoning_content") or type(text) is not str or not text.strip():
                raise ValueError
            if len(text.encode("utf-8"))>MAX_RESPONSE_BYTES:
                failed = "output_too_large"
            usage = None
            raw_usage = data.get("usage")
            if raw_usage is not None:
                try:
                    usage = Usage(raw_usage["prompt_tokens"],raw_usage["completion_tokens"])
                    if "total_tokens" in raw_usage and (type(raw_usage["total_tokens"]) is not int or raw_usage["total_tokens"]!=usage.input_tokens+usage.output_tokens):
                        raise ValueError
                    if usage.output_tokens>request.max_output_tokens:
                        raise ValueError
                except (BusinessError,TypeError,KeyError,ValueError):
                    failed = "invalid_usage"
            request_id = data.get("id")
            if type(request_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",request_id) or re.search(r"(?:^|[._:-])(?:sk-|CS-|LK-)",request_id):
                request_id = None
        except (ValueError,TypeError,KeyError,UnicodeError,AttributeError):
            failed = "invalid_response"
        if failed:
            raise ModelFailure(failed,accepted=True)
        return ModelResult(text,usage,request_id)

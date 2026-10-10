"""Controlled model/client fixtures: no environment credentials or external traffic."""

import asyncio
import json
from dataclasses import replace

import httpx

from course_platform.settings import Settings


def production_settings(tmp_path, *, env=None, **changes):
    from course_platform.production.config import load_production_settings

    source = Settings(base_url="https://api.deepseek.com/", api_key="fake-private-key",
        content_root=tmp_path / "content", database_path=tmp_path / "test.db",
        session_ttl_hours=72, environment="development")
    config = load_production_settings(source, env={"COURSE_MODEL":"deepseek-flash", "COURSE_MODEL_CONTEXT_TOKENS":"1000000", **(env or {})})
    return replace(config, **changes)


def response_body(text="# 内容😀", usage=None, **changes):
    value = {"id":"cmpl-test", "object":"chat.completion", "created":1, "model":"deepseek-flash",
        "choices":[{"index":0, "finish_reason":"stop", "logprobs":None,
            "message":{"role":"assistant", "content":text}}],
        "usage": usage if usage is not None else {"prompt_tokens":12, "completion_tokens":4, "total_tokens":16}}
    value.update(changes)
    return json.dumps(value, ensure_ascii=False, separators=(",",":"), allow_nan=False).encode()


class MemoryStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def aclose(self):
        self.closed = True


class ModelTransport(httpx.AsyncBaseTransport):
    """Records real adapter HTTP bytes. Unscripted calls cannot reach a socket."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def handle_async_request(self, request):
        content = await request.aread()
        self.calls.append((str(request.url), content, dict(request.headers)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, bytes):
            return httpx.Response(200, headers={"content-type":"application/json"}, stream=MemoryStream([response]), request=request)
        return response


class ScriptedModel:
    def __init__(self, results, *, barrier=None):
        self.results = list(results)
        self.calls = []
        self.sent = asyncio.Event()
        self.barrier = barrier

    async def generate(self, request, *, deadline_seconds=120):
        self.calls.append(request)
        self.sent.set()
        if self.barrier is not None:
            await self.barrier.wait()
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

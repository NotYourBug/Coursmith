"""Real adapter boundary tests use a controlled in-memory HTTP transport."""

import asyncio
import json
import logging

import httpx
import pytest

from course_platform.domain import BusinessError
from tests.jobs.fakes import production_settings, ModelTransport, MemoryStream, response_body


def run_model(tmp_path, responses, *, messages=None, max_output=32):
    from course_platform.production.llm import OpenAIModel, ModelRequest

    config = production_settings(tmp_path)
    request = ModelRequest(messages=messages or ({"role":"user", "content":"private-input-中文😀"},),
        max_output_tokens=max_output, config=config.public)
    transport = ModelTransport(responses)
    async def run():
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            return await OpenAIModel(config, http_client=client).generate(request)
    return run, transport, request


def test_utf8_payload_is_exact_sent_body_without_auth(tmp_path):
    from course_platform.production.llm import serialize_model_payload

    run, transport, request = run_model(tmp_path, [response_body()])
    result = asyncio.run(run())
    sent = transport.calls[0][1]
    assert sent == serialize_model_payload(request)
    assert sent == b'{"messages":[{"role":"user","content":"private-input-' + "中文😀".encode() + b'"}],"model":"deepseek-flash","max_tokens":32,"stream":false,"thinking":{"type":"disabled"}}'
    assert b"fake-private-key" not in sent
    assert transport.calls[0][2]["authorization"] == "Bearer fake-private-key"
    assert result.text == "# 内容😀"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12,4)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [401,403,429,500,503])
def test_http_failures_never_blind_retry_or_prove_rejection(tmp_path, status, caplog):
    from course_platform.production.llm import ModelFailure, retry_delay

    response = httpx.Response(status, json={"error":{"message":"fake-private-key/private-input"}},
        request=httpx.Request("POST","https://api.deepseek.com/chat/completions"))
    run, transport, _ = run_model(tmp_path, [response])
    with caplog.at_level(logging.DEBUG), pytest.raises(ModelFailure) as denied:
        asyncio.run(run())
    assert denied.value.accepted is None
    assert retry_delay(denied.value,0) is None
    assert len(transport.calls) == 1
    assert "fake-private-key" not in caplog.text + str(denied.value) + repr(denied.value)
    assert "private-input" not in caplog.text
    assert denied.value.__context__ is None


@pytest.mark.parametrize("exception", [httpx.ReadTimeout("private-error"), httpx.ConnectError("private-error"),
    httpx.ReadError("private-error"), httpx.WriteError("private-error")])
def test_network_errors_after_body_consumption_are_unknown(tmp_path, exception):
    from course_platform.production.llm import ModelFailure, retry_delay

    run, transport, _ = run_model(tmp_path, [exception])
    with pytest.raises(ModelFailure) as denied:
        asyncio.run(run())
    assert denied.value.accepted is None
    assert retry_delay(denied.value,0) is None
    assert "private-error" not in str(denied.value)
    assert denied.value.__context__ is None
    assert len(transport.calls) == 1


def test_retry_proven_not_accepted_uses_2_then_8_seconds():
    from course_platform.production.llm import ModelFailure, retry_delay

    failure = ModelFailure("not_sent", accepted=False)
    assert [retry_delay(failure,n) for n in (0,1,2)] == [2,8,None]
    for code, accepted in (("response_unknown",None),("authentication",False),("invalid_response",False),("not_sent",True)):
        assert retry_delay(ModelFailure(code, accepted=accepted),0) is None


@pytest.mark.parametrize("body", [b'not json', response_body(""), response_body(" "),
    response_body("x"*204801), response_body("text", choices=[]),
    response_body("text", usage={"prompt_tokens":True,"completion_tokens":4}),
    response_body("text", usage={"prompt_tokens":-1,"completion_tokens":4}),
    response_body("text", usage={"prompt_tokens":2**63,"completion_tokens":4}),
    response_body("text", usage={"prompt_tokens":12,"completion_tokens":4,"total_tokens":17}),
], ids=lambda _: "invalid-output")
def test_malformed_output_rejects_without_private_response(tmp_path, body):
    from course_platform.production.llm import ModelFailure

    run, _, _ = run_model(tmp_path,[body])
    with pytest.raises(ModelFailure) as denied:
        asyncio.run(run())
    assert denied.value.code in {"invalid_response","output_too_large","invalid_usage"}
    assert denied.value.accepted is True
    assert denied.value.__context__ is None


def test_missing_usage_remains_unknown(tmp_path):
    run, _, _ = run_model(tmp_path,[json.dumps(json.loads(response_body()) | {"usage":None}).encode()])
    assert asyncio.run(run()).usage is None


def test_continuous_stream_has_total_120_second_deadline(tmp_path):
    from course_platform.production.llm import ModelFailure

    class TimedChunks(MemoryStream):
        async def __aiter__(self):
            loop = asyncio.get_running_loop()
            start = loop.time()
            original = loop.time
            elapsed = [0]
            loop.time = lambda: start + elapsed[0]
            try:
                for _ in range(30):
                    elapsed[0] += 5
                    await asyncio.sleep(0)
                    yield b" "
            finally:
                loop.time = original
    stream = TimedChunks([])
    response = httpx.Response(200, headers={"content-type":"application/json"},stream=stream)
    run, transport, _ = run_model(tmp_path,[response])
    with pytest.raises(ModelFailure) as denied:
        asyncio.run(run())
    assert denied.value.code == "response_unknown"
    assert denied.value.accepted is None
    assert len(transport.calls) == 1
    assert stream.closed


def test_outline_json_has_strict_count_and_discards_model_ids():
    from course_platform.production.llm import parse_outline, ModelFailure

    text = '{"items":[{"chapter_id":"model-id","title":"中文😀","goal":"学习"}]}'
    outline = parse_outline(text, chapter_count=1)
    assert outline.items[0].chapter_id is None
    assert outline.items[0].title == "中文😀"
    for bad in ("{}",text, '{"items":[{"title":"x","goal":"y","tools":[]}]}',
        '{"items":[],"items":[{"title":"x","goal":"y"}]}'):
        with pytest.raises(ModelFailure):
            parse_outline(bad,chapter_count=2)


def test_usage_is_strict_and_addition_cannot_overflow():
    from course_platform.production.llm import Usage

    for args in ((True,1),(-1,1),(2**63,1),(2**63-1,1),(1,"2")):
        with pytest.raises(BusinessError):
            Usage(*args)


def test_unsupported_adapter_output_limit_refuses_before_transport(tmp_path):
    from dataclasses import replace
    from course_platform.production.llm import ModelRequest, OpenAIModel

    config = production_settings(tmp_path)
    config = replace(config, public=config.public.model_copy(update={"model":"reasoning-unknown"}))
    transport = ModelTransport([])
    request = ModelRequest(messages=({"role":"user","content":"test"},),max_output_tokens=32,config=config.public)
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await OpenAIModel(config,http_client=client).generate(request)
    with pytest.raises(BusinessError) as denied:
        asyncio.run(run())
    assert denied.value.code == "model_output_unsupported"
    assert transport.calls == []


def test_model_types_repr_omits_source_text(tmp_path):
    from course_platform.production.llm import ModelResult

    _, _, request = run_model(tmp_path,[response_body()])
    assert "private-input" not in repr(request)
    assert "private-result" not in repr(ModelResult("private-result",None,None))


def test_transport_proof_requires_no_request_body_bytes(tmp_path,monkeypatch):
    from course_platform.production.llm import OpenAIModel,ModelFailure
    from course_platform.production.prompts import build_connection_request

    async def refused_before_body(self,request):
        raise httpx.ConnectError("private-connect-error",request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,"handle_async_request",refused_before_body)
    config=production_settings(tmp_path)
    with pytest.raises(ModelFailure) as denied:
        asyncio.run(OpenAIModel(config).generate(build_connection_request(config.public)))
    assert denied.value.code=="not_sent"
    assert denied.value.accepted is False
    assert denied.value.__context__ is None


@pytest.mark.parametrize("body", [response_body("x"*204800),response_body("😀"*51200)],ids=["ascii-boundary","emoji-boundary"])
def test_decoded_200kib_boundary_is_accepted(tmp_path,body):
    run,_,_=run_model(tmp_path,[body])
    assert len(asyncio.run(run()).text.encode())==204800

def test_sdk_error_body_is_bounded_before_sdk_read(tmp_path):
    from course_platform.production.llm import ModelFailure

    class EndlessError(MemoryStream):
        def __init__(self):
            super().__init__([])
            self.consumed=0
        async def __aiter__(self):
            for _ in range(100):
                self.consumed+=1
                await asyncio.sleep(0)
                yield b"private-error"+b"x"*65536
    stream=EndlessError()
    response=httpx.Response(503,headers={"content-type":"application/json"},stream=stream)
    run,transport,_=run_model(tmp_path,[response])
    with pytest.raises(ModelFailure) as denied:
        asyncio.run(run())
    assert denied.value.accepted is None
    assert stream.consumed<=33
    assert stream.closed
    assert len(transport.calls)==1


def test_connect_error_after_any_body_bytes_is_unknown(tmp_path,monkeypatch):
    from course_platform.production.llm import OpenAIModel,ModelFailure
    from course_platform.production.prompts import build_connection_request

    async def after_body(self,request):
        async for _ in request.stream:
            break
        raise httpx.ConnectError("private-connect-error",request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,"handle_async_request",after_body)
    config=production_settings(tmp_path)
    with pytest.raises(ModelFailure) as denied:
        asyncio.run(OpenAIModel(config).generate(build_connection_request(config.public)))
    assert denied.value.accepted is None
    assert denied.value.__context__ is None

def test_adapter_rechecks_controlled_chapter_output_limit(tmp_path):
    from dataclasses import replace
    from course_platform.production.llm import OpenAIModel,ModelRequest
    from course_platform.production.prompts import build_chapter_request
    from course_platform.production.models import BriefInput,ProductionInput,OutlineView,OutlineItem,ChapterView

    base=production_settings(tmp_path)
    config=replace(base,public=base.public.model_copy(update={"chapter_output_tokens":32}))
    chapter_id="00000000-0000-4000-8000-000000000000"
    frozen=ProductionInput(1,1,"quick",1,BriefInput(title="course",chapter_count=1),(),
        OutlineView(1,1,1,(OutlineItem(chapter_id=chapter_id,title="x",goal="y"),),False),
        (ChapterView(chapter_id,None,""),),"a"*64)
    valid=build_chapter_request(frozen,chapter_id,config.public)
    forged=ModelRequest(messages=valid.messages,max_output_tokens=4000,config=config.public)
    transport=ModelTransport([response_body()])
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await OpenAIModel(config,http_client=client).generate(forged)
    with pytest.raises(BusinessError) as denied:
        asyncio.run(run())
    assert denied.value.code=="invalid_model_request"
    assert transport.calls==[]


@pytest.mark.parametrize("path",["/","/v1/"])
def test_flash_wire_disables_thinking_and_key_rotation_still_sends_once(tmp_path,path):
    from dataclasses import replace
    from course_platform.production.config import endpoint_fingerprint
    from course_platform.production.llm import OpenAIModel,serialize_model_payload
    from course_platform.production.prompts import build_connection_request

    base=production_settings(tmp_path)
    endpoint="https://api.deepseek.com"+path
    config=replace(base,base_url=endpoint,api_key="rotated-private-key",
        public=base.public.model_copy(update={"endpoint_fingerprint":endpoint_fingerprint(endpoint)}))
    request=build_connection_request(config.public)
    transport=ModelTransport([response_body("OK")])
    async def run():
        async with httpx.AsyncClient(transport=transport,trust_env=False) as client:
            return await OpenAIModel(config,http_client=client).generate(request)
    assert asyncio.run(run()).text=="OK"
    assert len(transport.calls)==1
    sent=transport.calls[0][1]
    assert sent==b'{"messages":[{"role":"user","content":"Reply OK."}],"model":"deepseek-flash","max_tokens":32,"stream":false,"thinking":{"type":"disabled"}}'
    assert sent==serialize_model_payload(request)
    assert transport.calls[0][2]["authorization"]=="Bearer rotated-private-key"
    assert b"rotated-private-key" not in sent

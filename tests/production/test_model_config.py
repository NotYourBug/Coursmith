"""Production configuration validates without reading dotenv or starting a model."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from course_platform.domain import BusinessError
from tests.jobs.fakes import production_settings


def test_config_drift_blocks_but_key_rotation_does_not(tmp_path):
    from course_platform.production.config import public_model_config
    from course_platform.production.llm import OpenAIModel
    import asyncio

    config = production_settings(tmp_path)
    rotated = replace(config, api_key="rotated-private-key")
    assert public_model_config(rotated) == public_model_config(config)
    from course_platform.production.prompts import build_connection_request
    request = build_connection_request(config.public)
    model = OpenAIModel(replace(config, public=config.public.model_copy(update={"context_tokens":64000})))
    with pytest.raises(BusinessError) as denied:
        asyncio.run(model.generate(request))
    assert denied.value.code == "model_config_changed"


def test_missing_context_limit_refuses_execution_not_m1_startup(tmp_path, monkeypatch):
    from course_platform.production.config import load_production_settings
    from course_platform.production.prompts import build_connection_request
    from course_platform.production.llm import OpenAIModel
    from course_platform.settings import Settings
    import asyncio

    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-secret")
    source = Settings(base_url="https://api.deepseek.com/", api_key="", content_root=tmp_path,
        database_path=tmp_path/"m1.db", session_ttl_hours=72, environment="development")
    config = load_production_settings(source, env={})
    assert config.public.context_tokens is None
    assert config.api_key == ""
    with pytest.raises(BusinessError) as denied:
        asyncio.run(OpenAIModel(config).generate(build_connection_request(config.public)))
    assert denied.value.code == "model_context_missing"
    assert not (tmp_path/"m1.db").exists()


def test_price_snapshot_is_optional_validated_and_historical(tmp_path):
    from course_platform.production.config import PriceSnapshot

    assert production_settings(tmp_path).price is None
    config = production_settings(tmp_path, env={"COURSE_MODEL_PRICE_JSON":
        '{"input_per_million":"0.10","output_per_million":"0.20","currency":"USD","as_of":"2026-10-09"}'})
    assert config.price == PriceSnapshot(Decimal("0.10"), Decimal("0.20"), "USD", date(2026,10,9))
    assert config.price.as_of == date(2026,10,9)


@pytest.mark.parametrize("price", [
    '{"input_per_million":"NaN","output_per_million":"0","currency":"USD","as_of":"2026-10-09"}',
    '{"input_per_million":"-1","output_per_million":"0","currency":"USD","as_of":"2026-10-09"}',
    '{"input_per_million":1,"output_per_million":"0","currency":"USD","as_of":"2026-10-09"}',
    '{"input_per_million":"0","output_per_million":"Infinity","currency":"USD","as_of":"2026-10-09"}',
    '{"input_per_million":"0","output_per_million":"0","currency":"private-secret","as_of":"bad"}',
    '{"input_per_million":"0","output_per_million":"0","currency":"USD","as_of":"2026-10-09","key":"private-secret"}',
])
def test_invalid_price_has_safe_errors(tmp_path, price):
    with pytest.raises(BusinessError) as denied:
        production_settings(tmp_path, env={"COURSE_MODEL_PRICE_JSON":price})
    assert denied.value.code == "invalid_model_price"
    assert "private-secret" not in str(denied.value)


@pytest.mark.parametrize("field,value", [("COURSE_MODEL_CONTEXT_TOKENS","0"),
    ("COURSE_MODEL_CONTEXT_TOKENS","1e6"), ("COURSE_TASK_TOKEN_LIMIT","-1"),
    ("COURSE_TASK_TOKEN_LIMIT",str(2**63)), ("COURSE_MODEL","sk-private-secret")])
def test_invalid_execution_config_is_safe(tmp_path, field, value):
    with pytest.raises(BusinessError):
        production_settings(tmp_path, env={field:value})


def test_private_configuration_never_enters_repr(tmp_path):
    config = production_settings(tmp_path)
    assert "fake-private-key" not in repr(config)
    assert "https://" not in repr(config)


def test_flash_is_default_and_current_executable_profile(tmp_path):
    from course_platform.production.config import load_production_settings
    from course_platform.settings import Settings

    source=Settings(base_url="https://api.deepseek.com/",api_key="fake-private-key",content_root=tmp_path,
        database_path=tmp_path/"not-created.db",session_ttl_hours=72,environment="development")
    config=load_production_settings(source,env={"COURSE_MODEL_CONTEXT_TOKENS":"64000"})
    assert config.public.model=="deepseek-flash"
    config.require_execution()
    assert not (tmp_path/"not-created.db").exists()
    assert config.task_token_limit==200000
    assert config.public.outline_output_tokens==config.public.chapter_output_tokens==4000


@pytest.mark.parametrize("model",["deepseek-chat","deepseek-reasoner","deepseek-v4-pro","other-model"])
def test_legacy_and_unrequested_models_are_refused_before_send(tmp_path,model):
    import asyncio
    import httpx
    from course_platform.production.llm import OpenAIModel
    from course_platform.production.prompts import build_connection_request
    from tests.jobs.fakes import ModelTransport,response_body

    current=production_settings(tmp_path)
    legacy=replace(current,public=current.public.model_copy(update={"model":model}))
    transport=ModelTransport([response_body()])
    async def run():
        async with httpx.AsyncClient(transport=transport,trust_env=False) as client:
            return await OpenAIModel(legacy,http_client=client).generate(build_connection_request(legacy.public))
    with pytest.raises(BusinessError) as denied:
        asyncio.run(run())
    assert denied.value.code=="model_output_unsupported"
    assert transport.calls==[]

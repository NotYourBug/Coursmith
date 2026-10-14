"""Private execution settings and validated, non-secret historical pricing."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from ..domain import BusinessError
from ..settings import Settings
from .models import ModelPublic

SQLITE_MAX = 9223372036854775807
PROMPT_VERSION = "m2a-v1"


def _invalid(code: str) -> BusinessError:
    return BusinessError(code, "模型执行配置无效，请联系维护者。", 409)


def strict_integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= SQLITE_MAX:
        raise _invalid("invalid_model_integer")
    return value


def normalize_endpoint(value: str) -> str:
    try:
        if type(value) is not str or not value or len(value) > 2048 or any(
            c.isspace() or ord(c)<32 or c in "\\%" for c in value
        ):
            raise ValueError
        parts = urlsplit(value)
        if parts.scheme not in ("http","https") or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ValueError
        port = parts.port
        if port == 0 or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+(?::[0-9]+)?",parts.netloc):
            raise ValueError
        path = parts.path.rstrip("/") + "/"
        if not re.fullmatch(r"/[A-Za-z0-9_/-]*",path) or ".." in path or "//" in path:
            raise ValueError
        return urlunsplit((parts.scheme.lower(),parts.netloc.lower(),path,"",""))
    except (TypeError,ValueError):
        raise _invalid("invalid_model_endpoint") from None


def endpoint_fingerprint(base_url: str) -> str:
    return hashlib.sha256(normalize_endpoint(base_url).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PriceSnapshot:
    input_per_million: Decimal
    output_per_million: Decimal
    currency: str
    as_of: date

    def __post_init__(self) -> None:
        for value in (self.input_per_million,self.output_per_million):
            if type(value) is not Decimal or not value.is_finite() or value < 0 or len(str(value))>64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?",str(value)):
                raise _invalid("invalid_model_price")
        if type(self.currency) is not str or not re.fullmatch(r"[A-Z]{3}",self.currency) or type(self.as_of) is not date:
            raise _invalid("invalid_model_price")

    def to_dict(self) -> dict[str,str]:
        return {"input_per_million":str(self.input_per_million),"output_per_million":str(self.output_per_million),
            "currency":self.currency,"as_of":self.as_of.isoformat()}


@dataclass(frozen=True)
class ProductionSettings:
    public: ModelPublic
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    task_token_limit: int = 200000
    timeout_seconds: int = 120
    input_margin: int = 2048
    price: PriceSnapshot | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.public,ModelPublic):
            raise _invalid("invalid_model_config")
        try:
            ModelPublic.model_validate(self.public.model_dump(warnings=False))
        except ValidationError:
            raise _invalid("invalid_model_config") from None
        if type(self.base_url) is not str or type(self.api_key) is not str or len(self.api_key)>4096 or any(ord(c)<32 for c in self.api_key):
            raise _invalid("invalid_model_config")
        if self.base_url and endpoint_fingerprint(self.base_url) != self.public.endpoint_fingerprint:
            raise _invalid("model_config_changed")
        strict_integer(self.task_token_limit,minimum=1)
        if type(self.timeout_seconds) is not int or self.timeout_seconds != 120 or type(self.input_margin) is not int or self.input_margin != 2048:
            raise _invalid("invalid_model_config")
        if self.price is not None:
            if not isinstance(self.price,PriceSnapshot):
                raise _invalid("invalid_model_price")
            self.price.__post_init__()

    def require_execution(self, frozen: ModelPublic | None = None) -> None:
        self.__post_init__()
        if frozen is not None and frozen != self.public:
            raise _invalid("model_config_changed")
        if self.public.context_tokens is None:
            raise _invalid("model_context_missing")
        if not self.base_url or not self.api_key.strip():
            raise _invalid("model_not_configured")
        # Only this explicitly verified provider profile has bounded max_tokens.
        endpoint = normalize_endpoint(self.base_url)
        if self.public.provider != "deepseek" or self.public.model != "deepseek-flash" or endpoint not in (
            "https://api.deepseek.com/","https://api.deepseek.com/v1/"
        ) or self.public.prompt_version != PROMPT_VERSION:
            raise _invalid("model_output_unsupported")


def public_model_config(config: ProductionSettings) -> ModelPublic:
    if not isinstance(config,ProductionSettings):
        raise _invalid("invalid_model_config")
    config.__post_init__()
    return config.public


def _env_integer(values: Mapping[str,str], name: str, default: str | None) -> int | None:
    text = values.get(name,default)
    if text is None or text == "":
        return None
    if type(text) is not str or len(text)>19 or not re.fullmatch(r"[0-9]+",text):
        raise _invalid("invalid_model_config")
    return strict_integer(int(text),minimum=1)


def _price(text: str) -> PriceSnapshot | None:
    if not text:
        return None
    try:
        if type(text) is not str or len(text)>1024:
            raise ValueError
        def pairs(items):
            if len(dict(items))!=len(items):
                raise ValueError
            return dict(items)
        value = json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value,dict) or set(value)!={"input_per_million","output_per_million","currency","as_of"}:
            raise ValueError
        for name in ("input_per_million","output_per_million"):
            item = value[name]
            if type(item) is not str or len(item)>64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?",item):
                raise ValueError
        if type(value["as_of"]) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}",value["as_of"]):
            raise ValueError
        return PriceSnapshot(Decimal(value["input_per_million"]),Decimal(value["output_per_million"]),value["currency"],date.fromisoformat(value["as_of"]))
    except (ValueError,TypeError,KeyError,BusinessError):
        raise _invalid("invalid_model_price") from None


def load_production_settings(settings: Settings, env: Mapping[str,str] | None = None) -> ProductionSettings:
    """Uses already loaded M1 credentials; explicit env replaces ambient options. No dotenv IO."""
    values = os.environ if env is None else env
    if not isinstance(settings,Settings):
        raise _invalid("invalid_model_config")
    try:
        public = ModelPublic(provider="deepseek",endpoint_fingerprint=endpoint_fingerprint(settings.base_url) if settings.base_url else hashlib.sha256(b"").hexdigest(),
            model=values.get("COURSE_MODEL","deepseek-flash"),context_tokens=_env_integer(values,"COURSE_MODEL_CONTEXT_TOKENS",None),prompt_version=PROMPT_VERSION)
        return ProductionSettings(public=public,base_url=settings.base_url,api_key=settings.api_key,
            task_token_limit=_env_integer(values,"COURSE_TASK_TOKEN_LIMIT","200000"),price=_price(values.get("COURSE_MODEL_PRICE_JSON","")))
    except ValidationError:
        raise _invalid("invalid_model_config") from None

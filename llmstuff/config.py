import datetime
import json
import os
import sys
from enum import StrEnum, auto
from pathlib import Path

import locale
locale.setlocale(locale.LC_ALL, "de_DE.UTF-8")

import pytz
from typing import Type, Tuple, Optional, Literal, List, Any, Dict, ClassVar

from cachetools import cached, TTLCache
from pydantic import BaseModel, Field, HttpUrl, RootModel, PostgresDsn

_CONFIGDIRPATH: Path = Path(__file__).parent.resolve()
_CONFIGDIRPATH = Path(os.getenv("ODDOSELENIUM_CONFIG_DIR_PATH")) if os.getenv("ODDOSELENIUM_CONFIG_DIR_PATH") else _CONFIGDIRPATH

_CONFIGPATH: Path = Path(_CONFIGDIRPATH, "config.yaml")
_CONFIGPATH: Path = Path(os.getenv("ODDOSELENIUM_CONFIG_PATH")) if os.getenv("ODDOSELENIUM_CONFIG_PATH") else _CONFIGPATH

_CONFIGLOCALPATH: Path = Path(_CONFIGDIRPATH, "config.local.yaml")
_CONFIGLOCALPATH = Path(os.getenv("ODDOSELENIUM_CONFIG_LOCAL_PATH")) if os.getenv("ODDOSELENIUM_CONFIG_LOCAL_PATH") else _CONFIGLOCALPATH


from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    PydanticBaseSettingsSource,
    EnvSettingsSource,
    YamlConfigSettingsSource,
    InitSettingsSource,
    DotEnvSettingsSource,
)

from loguru import logger

# https://buildmedia.readthedocs.org/media/pdf/loguru/latest/loguru.pdf
os.environ["LOGURU_LEVEL"] = os.getenv("LOGURU_LEVEL", "DEBUG")  # standard is DEBUG
logger.remove()  # remove default-handler
logger_fmt: str = "<g>{time:HH:mm:ssZZ}</> | <lvl>{level}</> | <c>{module}::{extra[classname]}:{function}:{line}</> - {message}"
#
logger.add(
    sys.stderr, level=os.getenv("LOGURU_LEVEL"), format=logger_fmt
)  # TRACE | DEBUG | INFO | WARN | ERROR |  FATAL
logger.configure(extra={"classname": "None"})


logger.info(f"EFFECTIVE CONFIGPATH: {_CONFIGPATH}")
logger.info(f"EFFECTIVE CONFIGLOCALPATH: {_CONFIGLOCALPATH}")


# https://docs.pydantic.dev/latest/concepts/pydantic_settings/

# alias in settings not correctly handled for pydantic v2
# https://github.com/pydantic/pydantic/issues/8379

class Redis(BaseModel):
    host: str = Field(default="127.0.0.1")
    host_in_cluster: Optional[str] = Field(default=None)
    port: int = Field(default=6379)
    port_in_cluster: int = Field(default=6379)

class Google(BaseModel):
    gemini_api_key: str|None = Field(default=None)


class Anthropic(BaseModel):
    anthropic_api_key: str|None = Field(default=None)


class Postgresql(BaseModel):
    host: str = Field(default="127.0.0.1")
    host_in_cluster: Optional[str] = Field(default="postgresql.postgresql.svc")
    username: str = Field(default="postgres")
    password: str = Field(default="empty")
    dbname: str = Field(default="postgres")
    port: int = Field(default=5432)
    port_in_cluster: int = Field(default=5432)
    # url: Optional[PostgresDsn] = Field(default=None)
    url: Optional[str] = Field(default=None)


class Settings(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        populate_by_name=True,
        # env_prefix="TAS_",
        case_sensitive=False,
        yaml_file_encoding="utf-8",
        extra="ignore",  # ignore | forbid | allow
        protected_namespaces=(),
        env_nested_delimiter="__",
        # alias_generator=AliasGenerator(
        #     validation_alias=to_camel,
        #     serialization_alias=to_pascal,
        # )
        yaml_file=[_CONFIGPATH, _CONFIGLOCALPATH]
    )

    # emailsettings: EmailSettings
    redis: Redis
    postgresql: Postgresql
    timezone: str = Field(default="Europe/Berlin")
    google: Google
    anthropic: Anthropic

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: InitSettingsSource,
        env_settings: EnvSettingsSource,
        dotenv_settings: DotEnvSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, YamlConfigSettingsSource(settings_cls)



def str2bool(v: str|bool) -> bool:
    if not v:
        return False

    if isinstance(v, bool):
        return v

    return v.lower() in ("yes", "true", "t", "1")


@cached(cache=TTLCache(maxsize=1, ttl=60))
def is_in_cluster() -> bool:
    sa: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    if sa.exists() and sa.is_dir():
        return os.getenv("KUBERNETES_SERVICE_HOST") is not None
    return False



def log_settings():
    for k, v in os.environ.items():
        if k.startswith("PSQL_"):
            logger.info(f"ENV::{k}: {v}")
    logger.info(json.dumps(settings.model_dump(by_alias=True), indent=4, sort_keys=False, default=str))


settings: Settings = Settings()

if settings.postgresql.url:
    os.environ["PSQL_DB_URL"] = os.getenv("PSQL_DB_URL", settings.postgresql.url)

os.environ["PSQL_DB_HOST"] = os.getenv("PSQL_DB_HOST", settings.postgresql.host_in_cluster if is_in_cluster() else settings.postgresql.host)
os.environ["PSQL_DB_PORT"] = os.getenv("PSQL_DB_PORT", str(settings.postgresql.port_in_cluster if is_in_cluster() else settings.postgresql.port))
os.environ["PSQL_DB_USERNAME"] = os.getenv("PSQL_DB_USERNAME", settings.postgresql.username)
os.environ["PSQL_DB_PASSWORD"] = os.getenv("PSQL_DB_PASSWORD", settings.postgresql.password)
os.environ["PSQL_DB_NAME"] = os.getenv("PSQL_DB_NAME", settings.postgresql.dbname)

TEMPLATEDIRPATH: Path = Path(__file__).parent.resolve()
TEMPLATEDIRPATH = Path(TEMPLATEDIRPATH, "templates")
logger.debug(f"TEMPLATEDIRPATH: {TEMPLATEDIRPATH}")

TIMEZONE: datetime.tzinfo = pytz.timezone(settings.timezone)
logger.debug(f"TIMEZONE: {TIMEZONE}")

if __name__ == "__main__":
    log_settings()


import datetime
import json
import locale
import os
import sys
from enum import StrEnum, auto
from pathlib import Path

from ruamel.yaml import YAML

import Helper

locale.setlocale(locale.LC_ALL, "de_DE.UTF-8")

from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple, Type

import pytz
from cachetools import TTLCache, cached
from pydantic import BaseModel, EmailStr, Field, HttpUrl, model_validator
from pydantic.networks import IPvAnyAddress
from pydantic_extra_types.mac_address import MacAddress

# from pydantic import RootModel
# from pydantic import RootModel, PostgresDsn, field_validator, NameEmail


_PKG = "SOMESTUFF"

_CONFIGDIRPATH: Path = Path(__file__).parent.resolve()
_CONFIGDIRPATH = Path(os.getenv(f"{_PKG}_CONFIG_DIR_PATH")) if os.getenv(f"{_PKG}_CONFIG_DIR_PATH") else _CONFIGDIRPATH  # type: ignore

_CONFIGPATH: Path = Path(_CONFIGDIRPATH, "config.yaml")
_CONFIGPATH = Path(os.getenv(f"{_PKG}_CONFIG_PATH")) if os.getenv(f"{_PKG}_CONFIG_PATH") else _CONFIGPATH  # type: ignore

_CONFIGLOCALPATH: Path = Path(_CONFIGDIRPATH, "config.local.yaml")
_CONFIGLOCALPATH = Path(os.getenv(f"{_PKG}_CONFIG_LOCAL_PATH")) if os.getenv(f"{_PKG}_CONFIG_LOCAL_PATH") else _CONFIGLOCALPATH  # type: ignore


from loguru import logger
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# https://buildmedia.readthedocs.org/media/pdf/loguru/latest/loguru.pdf
os.environ["LOGURU_LEVEL"] = os.getenv("LOGURU_LEVEL", "DEBUG")  # standard is DEBUG
logger.remove()  # remove default-handler

logger_fmt: str = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>::<cyan>{extra[classname]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
# logger_fmt: str = "<g>{time:HH:mm:ssZZ}</> | <lvl>{level}</> | <c>{module}::{extra[classname]}:{function}:{line}</> - {message}"

def _loguru_skiplog_filter(record: dict) -> bool:
    return not record.get("extra", {}).get("skiplog", False)

logger.add(sys.stderr, level=os.getenv("LOGURU_LEVEL"), format=logger_fmt, filter=_loguru_skiplog_filter)  # type: ignore # TRACE | DEBUG | INFO | WARN | ERROR |  FATAL
logger.configure(extra={"classname": "None", "skiplog": False})

logger.info(f"EFFECTIVE CONFIGPATH: {_CONFIGPATH}")
logger.info(f"EFFECTIVE CONFIGLOCALPATH: {_CONFIGLOCALPATH}")

_CONFIG_ORIG: Dict[str, Any] | None = None
try:
    _CONFIG_ORIG = YAML().load(_CONFIGPATH)
except Exception as e:
    logger.opt(exception=e).exception(f"Error loading local config file: {_CONFIGPATH}")

_CONFIG_LOCAL_ORIG: Dict[str, Any] | None = None
try:
    _CONFIG_LOCAL_ORIG = YAML().load(stream=_CONFIGLOCALPATH)
except Exception as e:
    logger.opt(exception=e).exception(f"Error loading local config file: {_CONFIGLOCALPATH}")

_EFFECTIVE_CONFIG: Dict[str, Any] | None = None

if _CONFIG_ORIG is not None:
    _EFFECTIVE_CONFIG = _CONFIG_ORIG

    if _CONFIG_LOCAL_ORIG is not None:
        _EFFECTIVE_CONFIG = Helper.update_deep(_EFFECTIVE_CONFIG, _CONFIG_LOCAL_ORIG)  # type: ignore


# https://docs.pydantic.dev/latest/concepts/pydantic_settings/

# alias in settings not correctly handled for pydantic v2
# https://github.com/pydantic/pydantic/issues/8379


class MqttTopic(BaseModel):
    # modulename: str
    # submodulename: str
    topic: str
    subscribe: bool = False


class MqttTopics(BaseModel):
    root: Dict[str, Dict[str, MqttTopic]]

    @model_validator(mode="before")
    @classmethod
    def _populate_root(cls, v: Any) -> Any:
        if isinstance(v, dict) and "root" not in v:
            return {"root": v}
        return v

    def __iter__(self) -> Any:
        return iter(self.root)  # type: ignore

    def __getitem__(self, item: Any) -> Any:
        return self.root[item]


class NetatmoModule(BaseModel):
    name: str
    id: Optional[MacAddress] = None


class Netatmo(BaseModel):
    username: str
    password: str
    client_id: str
    client_secret: str
    refresh_token: Optional[str] = None
    outdoormodule: NetatmoModule
    rainmodule: NetatmoModule


class Ecowitt(BaseModel):
    application_key: str | None = None
    api_key: str | None = None
    mac: MacAddress | None = None

    # HttpUrlString = Annotated[HttpUrl, AfterValidator(lambda v: str(v))]
    realtime_url: HttpUrl = Field(default=HttpUrl("https://api.ecowitt.net/api/v3/device/real_time"))
    device_info_url: HttpUrl = Field(default=HttpUrl("https://api.ecowitt.net/api/v3/device/info"))

    # @field_validator('mac', mode="before")
    # @classmethod
    # def validate_mac(cls, v):
    #     # logger.debug(f"VALIDATE MAC: {v}")
    #     if not v:
    #         return None
    #
    #     return cls.mac_no_colon_to_colon(v)


class Redis(BaseModel):
    host: str = Field(default="127.0.0.1")
    host_in_cluster: Optional[str] = Field(default=None)
    port: int = Field(default=6379)
    port_in_cluster: int = Field(default=6379)


class Google(BaseModel):
    gemini_api_key: str | None = Field(default=None)


class Anthropic(BaseModel):
    anthropic_api_key: str | None = Field(default=None)


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


class Mqtt(BaseModel):
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=1883)
    username: str = Field()
    password: str = Field()


class MqttMessageDefaultMetadata(BaseModel):
    # created_at is not really needed here -> will be added "on-the-fly"
    created_at: None | str = Field(default=None)
    # latitude, longitude, elevation for sensor/actor
    lat: None | float = Field(default=None)
    lon: None | float = Field(default=None)
    ele: None | float = Field(default=None)


class Hydromail(BaseModel):
    smtpip: IPvAnyAddress
    mailfrom: EmailStr
    mailreplyto: EmailStr
    mailsubject_base: str
    mailrecipients_to: List[EmailStr]
    mailrecipients_cc: List[EmailStr]


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
        yaml_file=[_CONFIGPATH, _CONFIGLOCALPATH],
    )

    # emailsettings: EmailSettings
    redis: Redis
    postgresql: Postgresql
    timezone: str = Field(default="Europe/Berlin")
    google: Google
    anthropic: Anthropic
    ecowitt: Ecowitt
    mqtt: Mqtt
    mqtt_message_default_metadata: MqttMessageDefaultMetadata
    netatmo: Netatmo
    hydromail: Hydromail
    mqtt_topics: MqttTopics

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: InitSettingsSource,  # type: ignore
        env_settings: EnvSettingsSource,  # type: ignore
        dotenv_settings: DotEnvSettingsSource,  # type: ignore
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        return init_settings, env_settings, YamlConfigSettingsSource(settings_cls)


def str2bool(v: str | bool) -> bool:
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


def log_settings() -> None:
    for k, v in os.environ.items():
        if k.startswith("PSQL_"):
            logger.info(f"ENV::{k}: {v}")

    # logger.info(json.dumps(_CONFIG_ORIG, indent=4, sort_keys=False, default=str))
    # logger.info(json.dumps(_CONFIG_LOCAL_ORIG, indent=4, sort_keys=False, default=str))

    logger.info(json.dumps(settings.model_dump(by_alias=True), indent=4, sort_keys=False, default=str))


settings: Settings = Settings()  # type: ignore

if settings.postgresql.url:
    os.environ["PSQL_DB_URL"] = os.getenv("PSQL_DB_URL", settings.postgresql.url)

os.environ["PSQL_DB_HOST"] = os.getenv("PSQL_DB_HOST", settings.postgresql.host_in_cluster if is_in_cluster() else settings.postgresql.host)  # type: ignore
os.environ["PSQL_DB_PORT"] = os.getenv(
    "PSQL_DB_PORT", str(settings.postgresql.port_in_cluster if is_in_cluster() else settings.postgresql.port)
)
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

    # logger.info(json.dumps(_CONFIG_ORIG, indent=4, sort_keys=False, default=str))
    # logger.info(json.dumps(_CONFIG_LOCAL_ORIG, indent=4, sort_keys=False, default=str))
    logger.info(json.dumps(_EFFECTIVE_CONFIG, indent=4, sort_keys=False, default=str))

    Helper.get_loguru_logger_info()

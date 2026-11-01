import datetime
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Type, cast

import pytz
from cachetools import TTLCache, cached
from loguru import logger
from pydantic import BaseModel, EmailStr, Field, HttpUrl, model_validator
from pydantic.networks import IPvAnyAddress
from pydantic_extra_types.mac_address import MacAddress
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from ruamel.yaml import YAML

from somestuff.corestuff import helper

_PKG = "SOMESTUFF"
_PACKAGE_DIR: Path = Path(__file__).parent.resolve()
_CONFIG_FILENAME = "config.yaml"
_CONFIG_LOCAL_FILENAME = "config.local.yaml"


def _search_dirs() -> List[Path]:
    env_dir: str | None = os.getenv(f"{_PKG}_CONFIG_DIR_PATH")
    if env_dir:
        return [Path(env_dir)]
    return [Path.cwd(), Path("/app")]


def _first_existing(filename: str) -> Path | None:
    for d in _search_dirs():
        candidate: Path = Path(d, filename)
        if candidate.is_file():
            return candidate
    return None


def config_path() -> Path:
    env_path: str | None = os.getenv(f"{_PKG}_CONFIG_PATH")
    if env_path:
        return Path(env_path)
    return _first_existing(_CONFIG_FILENAME) or Path(_PACKAGE_DIR, _CONFIG_FILENAME)


def config_local_path() -> Path | None:
    env_path: str | None = os.getenv(f"{_PKG}_CONFIG_LOCAL_PATH")
    if env_path:
        return Path(env_path)
    return _first_existing(_CONFIG_LOCAL_FILENAME)


def _load_yaml(path: Path | None) -> Dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        return YAML().load(path)
    except Exception as e:
        logger.opt(exception=e).exception(f"Error loading config file: {path}")
        return None


@lru_cache(maxsize=1)
def get_effective_config() -> Dict[str, Any]:
    base: Dict[str, Any] | None = _load_yaml(config_path())
    local: Dict[str, Any] | None = _load_yaml(config_local_path())
    if base is None:
        return {} if local is None else local
    if local is None:
        return base
    return cast(Dict[str, Any], helper.update_deep(base, local))


class _EffectiveConfigSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: Any, field_name: str) -> Tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> Dict[str, Any]:
        return dict(get_effective_config())


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
        extra="ignore",  # ignore | forbid | allow
        protected_namespaces=(),
        env_nested_delimiter="__",
        # alias_generator=AliasGenerator(
        #     validation_alias=to_camel,
        #     serialization_alias=to_pascal,
        # )
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
        return init_settings, env_settings, _EffectiveConfigSource(settings_cls)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    logger.debug(f"EFFECTIVE CONFIGPATH: {config_path()}")
    logger.debug(f"EFFECTIVE CONFIGLOCALPATH: {config_local_path()}")
    return Settings()  # type: ignore[call-arg]


class _LazySettings:
    def __getattr__(self, name: str) -> Any:
        return getattr(get_settings(), name)


settings: Settings = cast(Settings, _LazySettings())


@lru_cache(maxsize=1)
def get_timezone() -> datetime.tzinfo:
    return pytz.timezone(get_settings().timezone)


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
    logger.info(json.dumps(get_settings().model_dump(by_alias=True), indent=4, sort_keys=False, default=str))


if __name__ == "__main__":
    from somestuff.corestuff import configure_logging

    configure_logging()
    log_settings()
    logger.info(json.dumps(get_effective_config(), indent=4, sort_keys=False, default=str))
    helper.get_loguru_logger_info()

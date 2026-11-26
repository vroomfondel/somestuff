"""TypedDict shapes of the JSON payloads returned by the Home Assistant REST and WebSocket APIs."""

from typing import TypedDict

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class Context(TypedDict, total=False):
    """Context of a state or event (REST /api/states, WS state payloads)."""

    id: str
    parent_id: str | None
    user_id: str | None


class State(TypedDict, total=False):
    """Entity state as returned by REST /api/states and WS get_states."""

    entity_id: str
    state: str
    attributes: dict[str, JsonValue]
    last_changed: str
    last_reported: str
    last_updated: str
    context: Context


class EntityRegistryEntry(TypedDict, total=False):
    """Entity registry entry as returned by WS config/entity_registry/list."""

    id: str
    entity_id: str
    unique_id: str
    platform: str
    config_entry_id: str | None
    device_id: str | None
    area_id: str | None
    name: str | None
    original_name: str | None
    icon: str | None
    disabled_by: str | None
    hidden_by: str | None
    entity_category: str | None
    has_entity_name: bool
    translation_key: str | None
    labels: list[str]
    categories: dict[str, str]
    created_at: float
    modified_at: float
    options: dict[str, dict[str, JsonValue]]


class DeviceRegistryEntry(TypedDict, total=False):
    """Device registry entry as returned by WS config/device_registry/list."""

    id: str
    name: str | None
    name_by_user: str | None
    manufacturer: str | None
    model: str | None
    model_id: str | None
    area_id: str | None
    disabled_by: str | None
    entry_type: str | None
    identifiers: list[list[str]]
    connections: list[list[str]]
    config_entries: list[str]
    primary_config_entry: str | None
    via_device_id: str | None
    sw_version: str | None
    hw_version: str | None
    serial_number: str | None
    configuration_url: str | None
    labels: list[str]
    created_at: float
    modified_at: float


class AreaRegistryEntry(TypedDict, total=False):
    """Area registry entry as returned by WS config/area_registry/list."""

    area_id: str
    name: str
    floor_id: str | None
    aliases: list[str]
    icon: str | None
    labels: list[str]
    picture: str | None
    temperature_entity_id: str | None
    humidity_entity_id: str | None
    created_at: float
    modified_at: float


class StatisticMeta(TypedDict, total=False):
    """Statistic metadata as returned by WS recorder/list_statistic_ids."""

    statistic_id: str
    name: str | None
    source: str
    has_sum: bool
    mean_type: int
    has_mean: bool
    statistics_unit_of_measurement: str | None
    display_unit_of_measurement: str | None
    unit_class: str | None

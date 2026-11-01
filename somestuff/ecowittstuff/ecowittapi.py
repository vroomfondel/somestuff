import datetime
import json
import uuid
from enum import IntEnum
from typing import Optional

import pydantic
import requests
from loguru import logger
from pydantic import BaseModel, field_validator, model_validator, validator
from pydantic_extra_types.mac_address import MacAddress

from somestuff.corestuff import configure_logging
from somestuff.corestuff.config import get_timezone, settings
from somestuff.corestuff.helper import get_pretty_dict_json_no_sort


# https://doc.ecowitt.net/web/#/apiv3en?page_id=17
class ResultType(IntEnum):
    SYSTEM_BUSY = -1
    SUCCESS = 0
    ILLEGAL_PARAMETER = 40000
    ILLEGAL_APPLICATION_KEY = 40010
    ILLEGAL_API_KEY = 40011
    ILLEGAL_MAC_IMEI_PARAMETER = 40012
    ILLEGAL_START_DATE = 40013
    ILLEGAL_END_DATE = 40014
    ILLEGAL_CYCLE_TYPE = 40015
    ILLEGAL_CALL_BACK = 40016
    MISSING_APPLICATION_KEY = 40017
    MISSING_API_KEY = 40018
    MISSING_MAC_PARAMETER = 40019
    MISSING_START_DATE = 40020
    MISSING_END_DATE = 40021
    ILLEGAL_VOUCHER_TYPE = 40022
    NEEDS_OTHER_SERVICE_SUPPORT = 43001
    MEDIA_FILE_OR_DATA_PACKET_NULL = 44001
    OVER_LIMIT_OR_ERROR = 45001
    NO_EXISTING_REQUEST = 46001
    PARSE_JSON_XML_CONTENTS_ERROR = 47001
    PRIVILEGE_PROBLEM = 48001


class MeasurementValue(BaseModel):
    time: float
    unit: str
    value: float
    time_as_datetime: datetime.datetime | None = None

    @model_validator(mode="after")
    def _val_time_to_datetime(self) -> "MeasurementValue":
        self.time_as_datetime = datetime.datetime.fromtimestamp(self.time, tz=get_timezone())
        return self

    # def time_as_datetime(self):
    #     return datetime.datetime.fromtimestamp(self.time, tz=get_timezone())


class OutdoorData(BaseModel):
    temperature: MeasurementValue
    feels_like: MeasurementValue
    app_temp: MeasurementValue
    dew_point: MeasurementValue
    vpd: MeasurementValue
    humidity: MeasurementValue


class IndoorData(BaseModel):
    temperature: MeasurementValue
    humidity: MeasurementValue
    dew_point: MeasurementValue
    feels_like: MeasurementValue
    app_tempin: MeasurementValue


class SolarAndUvi(BaseModel):
    solar: MeasurementValue
    uvi: MeasurementValue


class RainfallData(BaseModel):
    rain_rate: MeasurementValue
    daily: MeasurementValue
    event: MeasurementValue
    weekly: MeasurementValue
    monthly: MeasurementValue
    yearly: MeasurementValue


class WindData(BaseModel):
    wind_speed: MeasurementValue
    wind_gust: MeasurementValue
    wind_direction: MeasurementValue


class PressureData(BaseModel):
    relative: MeasurementValue
    absolute: MeasurementValue


class BatteryData(BaseModel):
    sensor_array: MeasurementValue


class WeatherData(BaseModel):
    outdoor: OutdoorData
    indoor: IndoorData
    solar_and_uvi: SolarAndUvi
    rainfall: RainfallData
    wind: WindData
    pressure: PressureData
    battery: BatteryData


class WeatherStationResponse(BaseModel):
    # code: int
    code: ResultType
    msg: str
    time: float
    data: WeatherData
    time_as_datetime: datetime.datetime | None = None

    @model_validator(mode="after")
    def _val_time_to_datetime(self) -> "WeatherStationResponse":
        self.time_as_datetime = datetime.datetime.fromtimestamp(self.time, tz=get_timezone())
        return self

    #
    # def time_as_datetime(self):
    #     return datetime.datetime.fromtimestamp(self.time, tz=get_timezone())


def get_realtime_data() -> WeatherStationResponse:
    base_url: pydantic.networks.HttpUrl = settings.ecowitt.realtime_url

    # https://doc.ecowitt.net/web/#/apiv3en?page_id=17
    params: dict = {
        "temp_unitid": 1,
        "pressure_unitid": 3,
        "wind_speed_unitid": 10,
        "rainfall_unitid": 12,
        "solar_irradiance_unitid": 14,
        "call_back": "all",
    }

    params["application_key"] = settings.ecowitt.application_key
    params["api_key"] = settings.ecowitt.api_key
    params["mac"] = settings.ecowitt.mac

    logger.info(f"Sende Request an {base_url} mit Parametern: {list(params.keys())}")

    response = requests.get(str(base_url), params=params)
    response.raise_for_status()

    # logger.debug(response.text)

    response_dict: dict = response.json()

    logger.debug(get_pretty_dict_json_no_sort(response_dict))

    rc: ResultType = ResultType(int(response_dict["code"]))
    if rc != ResultType.SUCCESS:
        raise Exception("Fehler beim Abrufen der Daten")

    weather_response: WeatherStationResponse = WeatherStationResponse(**response_dict)

    logger.debug(get_pretty_dict_json_no_sort(weather_response.model_dump()))

    logger.success(f"Daten erfolgreich abgerufen (Zeit: {weather_response.time_as_datetime})")

    return weather_response


def main() -> None:
    configure_logging()
    weather_response: WeatherStationResponse = get_realtime_data()

    # Auf die Daten zugreifen
    logger.info(
        f"Außentemperatur: {weather_response.data.outdoor.temperature.value} {weather_response.data.outdoor.temperature.unit}"
    )
    logger.info(
        f"Luftfeuchtigkeit: {weather_response.data.outdoor.humidity.value}{weather_response.data.outdoor.humidity.unit}"
    )
    logger.info(
        f"Luftdruck: {weather_response.data.pressure.relative.value} {weather_response.data.pressure.relative.unit}"
    )


if __name__ == "__main__":
    main()

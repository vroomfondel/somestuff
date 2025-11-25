import uuid
from enum import IntEnum

import pydantic

from Helper import get_pretty_dict_json_no_sort
from config import settings, TIMEZONE
from loguru import logger

import requests

import json

from pydantic import BaseModel, validator, field_validator, model_validator
from pydantic_extra_types.mac_address import MacAddress

from typing import Optional
import datetime

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
    time_as_datetime: datetime.datetime|None = None

    @model_validator(mode="after")
    def _val_time_to_datetime(self):
        self.time_as_datetime = datetime.datetime.fromtimestamp(self.time, tz=TIMEZONE)
        return self

    # def time_as_datetime(self):
    #     return datetime.datetime.fromtimestamp(self.time, tz=TIMEZONE)


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
    def _val_time_to_datetime(self):
        self.time_as_datetime = datetime.datetime.fromtimestamp(self.time, tz=TIMEZONE)
        return self
    #
    # def time_as_datetime(self):
    #     return datetime.datetime.fromtimestamp(self.time, tz=TIMEZONE)



def get_realtime_data():
    base_url: pydantic.networks.HttpUrl = settings.ecowitt.realtime_url

    # https://doc.ecowitt.net/web/#/apiv3en?page_id=17
    params: dict = {
        "temp_unitid": 1,
        "pressure_unitid": 3,
        "wind_speed_unitid": 10,
        "rainfall_unitid": 12,
        "solar_irradiance_unitid": 14,
        "call_back": "all"
    }

    params['application_key'] = settings.ecowitt.application_key
    params['api_key'] = settings.ecowitt.api_key
    params['mac'] = settings.ecowitt.mac

    logger.info(f"Sende Request an {base_url} mit Parametern: {list(params.keys())}")

    response = requests.get(str(base_url), params=params)
    response.raise_for_status()

    logger.debug(response.text)

    response_dict: dict = response.json()

    rc: ResultType = ResultType(int(response_dict["code"]))
    if rc != ResultType.SUCCESS:
        raise Exception("Fehler beim Abrufen der Daten")

    weather_response: WeatherStationResponse = WeatherStationResponse(**response_dict)

    logger.debug(get_pretty_dict_json_no_sort(weather_response.model_dump()))

    logger.success(f"Daten erfolgreich abgerufen (Zeit: {weather_response.time_as_datetime})")

    return weather_response

def main():
    weather_response: WeatherStationResponse = get_realtime_data()

    # Auf die Daten zugreifen
    logger.info(f"Außentemperatur: {weather_response.data.outdoor.temperature.value} {weather_response.data.outdoor.temperature.unit}")
    logger.info(f"Luftfeuchtigkeit: {weather_response.data.outdoor.humidity.value}{weather_response.data.outdoor.humidity.unit}")
    logger.info(f"Luftdruck: {weather_response.data.pressure.relative.value} {weather_response.data.pressure.relative.unit}")

if __name__ == "__main__":
    main()

    # /home/thiess/pythondev_workspace/somestuff/.venv/bin/python /home/thiess/pythondev_workspace/somestuff/ecowitt/ecowittapi.py
    # 14:30:45+0100 | INFO | config::None:<module>:52 - EFFECTIVE CONFIGPATH: /home/thiess/pythondev_workspace/somestuff/config.yaml
    # 14:30:45+0100 | INFO | config::None:<module>:53 - EFFECTIVE CONFIGLOCALPATH: /home/thiess/pythondev_workspace/somestuff/config.local.yaml
    # 14:30:45+0100 | DEBUG | config::None:<module>:182 - TEMPLATEDIRPATH: /home/thiess/pythondev_workspace/somestuff/templates
    # 14:30:45+0100 | DEBUG | config::None:<module>:185 - TIMEZONE: Europe/Berlin
    # 14:30:45+0100 | INFO | ecowittapi::None:get_realtime_data:151 - Sende Request an https://api.ecowitt.net/api/v3/device/real_time mit Parametern: ['temp_unitid', 'pressure_unitid', 'wind_speed_unitid', 'rainfall_unitid', 'solar_irradiance_unitid', 'call_back', 'application_key', 'api_key', 'mac']
    # 14:30:46+0100 | DEBUG | ecowittapi::None:get_realtime_data:156 - {"code":0,"msg":"success","time":"1764077446","data":{"outdoor":{"temperature":{"time":"1764077442","unit":"℃","value":"4.7"},"feels_like":{"time":"1764077442","unit":"℃","value":"4.7"},"app_temp":{"time":"1764077442","unit":"℃","value":"3.0"},"dew_point":{"time":"1764077442","unit":"℃","value":"3.4"},"vpd":{"time":"1764077442","unit":"inHg","value":"0.023"},"humidity":{"time":"1764077442","unit":"%","value":"91"}},"indoor":{"temperature":{"time":"1764077442","unit":"℃","value":"22.0"},"humidity":{"time":"1764077442","unit":"%","value":"38"},"dew_point":{"time":"1764077442","unit":"℃","value":"7.1"},"feels_like":{"time":"1764077442","unit":"℃","value":"22.0"},"app_tempin":{"time":"1764077442","unit":"℃","value":"21.3"}},"solar_and_uvi":{"solar":{"time":"1764077442","unit":"lx","value":"4029.1"},"uvi":{"time":"1764077442","unit":"","value":"0"}},"rainfall":{"rain_rate":{"time":"1764077442","unit":"mm\/hr","value":"0.0"},"daily":{"time":"1764077442","unit":"mm","value":"6.6"},"event":{"time":"1764077442","unit":"mm","value":"0.0"},"weekly":{"time":"1764077442","unit":"mm","value":"6.6"},"monthly":{"time":"1764077442","unit":"mm","value":"85.1"},"yearly":{"time":"1764077442","unit":"mm","value":"600.4"}},"wind":{"wind_speed":{"time":"1764077442","unit":"BFT","value":"1"},"wind_gust":{"time":"1764077442","unit":"BFT","value":"1"},"wind_direction":{"time":"1764077442","unit":"º","value":"263"}},"pressure":{"relative":{"time":"1764077442","unit":"hPa","value":"1005.6"},"absolute":{"time":"1764077442","unit":"hPa","value":"1005.6"}},"battery":{"sensor_array":{"time":"1764077442","unit":"","value":"0"}}}}
    # 14:30:46+0100 | DEBUG | ecowittapi::None:get_realtime_data:166 - {
    #     "code": 0,
    #     "msg": "success",
    #     "time": 1764077446.0,
    #     "data": {
    #         "outdoor": {
    #             "temperature": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u2103",
    #                 "value": 4.7,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "feels_like": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u2103",
    #                 "value": 4.7,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "app_temp": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u2103",
    #                 "value": 3.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "dew_point": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u2103",
    #                 "value": 3.4,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "vpd": {
    #                 "time": 1764077442.0,
    #                 "unit": "inHg",
    #                 "value": 0.023,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "humidity": {
    #                 "time": 1764077442.0,
    #                 "unit": "%",
    #                 "value": 91.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             }
    #         },
    #         "indoor": {
    #             "temperature": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u2103",
    #                 "value": 22.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "humidity": {
    #                 "time": 1764077442.0,
    #                 "unit": "%",
    #                 "value": 38.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "dew_point": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u2103",
    #                 "value": 7.1,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "feels_like": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u2103",
    #                 "value": 22.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "app_tempin": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u2103",
    #                 "value": 21.3,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             }
    #         },
    #         "solar_and_uvi": {
    #             "solar": {
    #                 "time": 1764077442.0,
    #                 "unit": "lx",
    #                 "value": 4029.1,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "uvi": {
    #                 "time": 1764077442.0,
    #                 "unit": "",
    #                 "value": 0.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             }
    #         },
    #         "rainfall": {
    #             "rain_rate": {
    #                 "time": 1764077442.0,
    #                 "unit": "mm/hr",
    #                 "value": 0.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "daily": {
    #                 "time": 1764077442.0,
    #                 "unit": "mm",
    #                 "value": 6.6,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "event": {
    #                 "time": 1764077442.0,
    #                 "unit": "mm",
    #                 "value": 0.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "weekly": {
    #                 "time": 1764077442.0,
    #                 "unit": "mm",
    #                 "value": 6.6,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "monthly": {
    #                 "time": 1764077442.0,
    #                 "unit": "mm",
    #                 "value": 85.1,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "yearly": {
    #                 "time": 1764077442.0,
    #                 "unit": "mm",
    #                 "value": 600.4,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             }
    #         },
    #         "wind": {
    #             "wind_speed": {
    #                 "time": 1764077442.0,
    #                 "unit": "BFT",
    #                 "value": 1.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "wind_gust": {
    #                 "time": 1764077442.0,
    #                 "unit": "BFT",
    #                 "value": 1.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "wind_direction": {
    #                 "time": 1764077442.0,
    #                 "unit": "\u00ba",
    #                 "value": 263.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             }
    #         },
    #         "pressure": {
    #             "relative": {
    #                 "time": 1764077442.0,
    #                 "unit": "hPa",
    #                 "value": 1005.6,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             },
    #             "absolute": {
    #                 "time": 1764077442.0,
    #                 "unit": "hPa",
    #                 "value": 1005.6,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             }
    #         },
    #         "battery": {
    #             "sensor_array": {
    #                 "time": 1764077442.0,
    #                 "unit": "",
    #                 "value": 0.0,
    #                 "time_as_datetime": "2025-11-25 14:30:42+01:00"
    #             }
    #         }
    #     },
    #     "time_as_datetime": "2025-11-25 14:30:46+01:00"
    # }
    # 14:30:46+0100 | SUCCESS | ecowittapi::None:get_realtime_data:168 - Daten erfolgreich abgerufen (Zeit: 2025-11-25 14:30:46+01:00)
    # 14:30:46+0100 | INFO | ecowittapi::None:main:176 - Außentemperatur: 4.7 ℃
    # 14:30:46+0100 | INFO | ecowittapi::None:main:177 - Luftfeuchtigkeit: 91.0%
    # 14:30:46+0100 | INFO | ecowittapi::None:main:178 - Luftdruck: 1005.6 hPa
    #
    # Process finished with exit code 0
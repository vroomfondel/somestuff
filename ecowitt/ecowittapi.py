import uuid

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
    code: int
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

    response_dict: dict = response.json()

    # logger.debug(json.dumps(response_dict, indent=4))

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
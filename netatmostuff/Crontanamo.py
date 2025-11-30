import datetime
import json
import os
import sys
import time
from datetime import timedelta
from os.path import exists, expanduser
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz
import schedule

# https://github.com/dbader/schedule
from schedule import run_pending

import config
import Helper
from config import _EFFECTIVE_CONFIG as effconfig  # dirty.
from config import settings
from mqttstuff.mosquittomqttwrapper import MosquittoClientWrapper, MWMqttMessage

_tzberlin: datetime.tzinfo = pytz.timezone("Europe/Berlin")

from loguru import logger

os.environ["LOGURU_LEVEL"] = os.getenv("LOGURU_LEVEL", "DEBUG")  # standard is DEBUG
logger.remove()  # remove default-handler
# # https://buildmedia.readthedocs.org/media/pdf/loguru/latest/loguru.pdf
logger.add(sys.stderr, level=os.getenv("LOGURU_LEVEL"))  # type: ignore  # TRACE | DEBUG | INFO | WARN | ERROR |  FATAL

NOSENDMOSQUITTO: bool = False


def send_to_mosquitto(
    mqttclient: MosquittoClientWrapper,
    temp: Optional[float],
    pressure: Optional[float],
    absolute_pressure: Optional[float],
    rain: Optional[float],
    rain1h: Optional[float],
    rain24h: Optional[float],
    created_at_temp: Optional[datetime.datetime] = None,
    created_at_pressure: Optional[datetime.datetime] = None,
    created_at_rain: Optional[datetime.datetime] = None,
    created_at_rain1h: Optional[datetime.datetime] = None,
    created_at_rain24h: Optional[datetime.datetime] = None,
) -> None:
    assert isinstance(effconfig, dict)
    assert "mqtt_topics" in effconfig
    assert "mqtt_message_default_metadata" in effconfig

    # assert isinstance(effconfig["mqtt_topics"], dict)
    # assert "ecowitt" in effconfig["mqtt_topics"] and isinstance(effconfig["mqtt_topics"]["ecowitt"], dict)
    # assert "pressure" in effconfig["mqtt_topics"]["ecowitt"] and isinstance(effconfig["mqtt_topics"]["ecowitt"]["pressure"], dict)

    # msgs: List[Tuple[str, Union[int, float, str, Dict], Optional[datetime.datetime], Optional[Dict]]] = []
    msgs: List[MWMqttMessage] = []

    netatmo_topics: Dict[str, config.MqttTopic] = settings.mqtt_topics.root.get(
        "netatmo", {}
    )  # effconfig["mqtt_topics"]["netatmo"]
    # assert isinstance(netatmo_topics, dict)

    metadata: Dict[str, Any] = effconfig["mqtt_message_default_metadata"].copy()
    # NOPE: copy just to make sure not to change the original/have some memory problems after a while due to references
    # for that reason metadata is already/will be copied in MosquittoClientWrapper.publish or MosquittoClientWrapper.publish_multiple

    # msgs: List[Tuple[str, Union[int, float, str, Dict], Optional[datetime.datetime], Optional[Dict]]] = []
    topic: str | None

    logger.debug("Crontanamo::send_to_mosquitto::netatmo_topics:")
    logger.debug(Helper.get_pretty_dict_json_no_sort(netatmo_topics))

    logger.debug(f"Crontanamo::send_to_mosquitto::{temp=}")
    if temp is not None:
        assert "temperature" in netatmo_topics
        topic = netatmo_topics["temperature"].topic

        logger.debug(f"\t{topic=}")
        if topic is not None:
            msgs.append(
                MWMqttMessage(
                    topic=topic,
                    value=temp,
                    valuedt=created_at_temp,
                    retained=True,
                    metadata=metadata,
                    rettype="valuemsg",
                    qos=1,
                )
            )

    logger.debug(f"Crontanamo::send_to_mosquitto::{rain=}")
    if rain is not None:
        assert "rain" in netatmo_topics
        topic = netatmo_topics["rain"].topic

        logger.debug(f"\t{topic=}")
        if topic is not None:
            msgs.append(
                MWMqttMessage(
                    topic=topic,
                    value=rain,
                    valuedt=created_at_rain,
                    retained=True,
                    metadata=metadata,
                    rettype="valuemsg",
                    qos=1,
                )
            )

    logger.debug(f"Crontanamo::send_to_mosquitto::{rain1h=}")
    if rain1h is not None:
        assert "rain1h" in netatmo_topics
        topic = netatmo_topics["rain1h"].topic

        logger.debug(f"\t{topic=}")
        if topic is not None:
            msgs.append(
                MWMqttMessage(
                    topic=topic,
                    value=rain1h,
                    valuedt=created_at_rain1h,
                    retained=True,
                    metadata=metadata,
                    rettype="valuemsg",
                    qos=1,
                )
            )

    logger.debug(f"Crontanamo::send_to_mosquitto::{rain24h=}")
    if rain24h is not None:
        assert "rain24h" in netatmo_topics
        topic = netatmo_topics["rain24h"].topic

        logger.debug(f"\t{topic=}")
        if topic is not None:
            msgs.append(
                MWMqttMessage(
                    topic=topic,
                    value=rain24h,
                    valuedt=created_at_rain24h,
                    retained=True,
                    metadata=metadata,
                    rettype="valuemsg",
                    qos=1,
                )
            )

    logger.debug(f"Crontanamo::send_to_mosquitto::{pressure=} {absolute_pressure=}")
    if pressure is not None or absolute_pressure is not None:
        pd: Dict[str, float] = {}
        if pressure is not None:
            pd["pressure"] = pressure
        if absolute_pressure is not None:
            pd["absolute_pressure"] = absolute_pressure

        assert "pressure" in netatmo_topics
        topic = netatmo_topics["pressure"].topic

        logger.debug(f"\t{topic=}")
        if topic is not None:
            msgs.append(
                MWMqttMessage(
                    topic=topic,
                    value=pd,
                    valuedt=created_at_pressure,
                    retained=True,
                    metadata=metadata,
                    rettype="valuemsg",
                    qos=1,
                )
            )

    if NOSENDMOSQUITTO:
        logger.debug(f"{NOSENDMOSQUITTO=}")
        logger.debug(msgs)
        return

    send_results: List[bool] = mqttclient.publish_multiple(msgs)
    logger.debug(f"Crontanamo::send_to_mosquitto::send_results:")
    for i, res in enumerate(send_results):
        logger.debug(f"{msgs[i].topic} -> {res}")


def write_netatmo_credentials_to_shared_file() -> None:
    logger.debug("Crontanamo::write_netatmo_credentialsfileshared")

    if os.getenv("STORAGEPATH"):
        logger.debug("STORAGEPATH is in ENV (" + os.environ["STORAGEPATH"] + ")")

        credentials = expanduser("~/.netatmo.credentials")

        if exists(credentials):
            logger.debug(f"{credentials} EXISTS -> preparing copy")

            credentials2 = os.environ["STORAGEPATH"] + "/netatmo.credentials"

            with open(credentials, "r") as fin:
                netatmo_data = json.load(fin)

                # if "ACCESS_TOKEN" not in netatmo_data and os.getenv("_NETATMO_ACCESS_TOKEN"):
                #     netatmo_data["ACCESS_TOKEN"] = os.environ["_NETATMO_ACCESS_TOKEN"]

                with open(credentials2, "w") as f:
                    f.write(json.dumps(netatmo_data, indent=True))
    else:
        logger.debug("STORAGEPATH is NOT set")


def ensure_up2date_netatmo_credentialsfile() -> dict:
    logger.debug("Crontanamo::ensure_netatmo_credentialsfile")
    netatmo_data: dict = {}
    credentials = expanduser("~/.netatmo.credentials")
    if exists(credentials):
        logger.debug(f"{credentials} exists")

        with open(credentials, "r") as fin:
            netatmo_data = json.load(fin)

        if os.getenv("STORAGEPATH"):
            credentials2 = os.environ["STORAGEPATH"] + "/netatmo.credentials"

            if exists(credentials2):
                logger.debug(f"{credentials2} EXISTS -> checking if newer than {credentials}")

                crstat: int = int(os.stat(credentials).st_mtime)
                cr2stat: int = int(os.stat(credentials2).st_mtime)

                if cr2stat > crstat:
                    logger.debug(
                        f"{credentials2} ({datetime.datetime.fromtimestamp(cr2stat)}) NEWER THAN {credentials} ({datetime.datetime.fromtimestamp(crstat)}) -> OVERWRITING"
                    )

                    with open(credentials2, "r") as fin2:
                        netatmo_data = json.load(fin2)
                        # if "ACCESS_TOKEN" not in netatmo_data and os.getenv("_NETATMO_ACCESS_TOKEN"):
                        #     netatmo_data["ACCESS_TOKEN"] = os.environ["_NETATMO_ACCESS_TOKEN"]

                    with open(credentials, "w") as f:
                        f.write(json.dumps(netatmo_data, indent=True))
    else:
        logger.debug(f"{credentials} DOES NOT exist -> setting netatmo_data from config")
        netatmo_data["CLIENT_ID"] = os.getenv("_NETATMO_CLIENT_ID")
        netatmo_data["CLIENT_SECRET"] = os.getenv("_NETATMO_CLIENT_SECRET")
        netatmo_data["REFRESH_TOKEN"] = os.getenv("_NETATMO_REFRESH_TOKEN")

        logger.debug("netatmo_data from ENV:")
        logger.debug(json.dumps(netatmo_data, indent=True))

        if os.getenv("STORAGEPATH"):
            credentials2 = os.environ["STORAGEPATH"] + "/netatmo.credentials"

            if exists(credentials2):
                logger.debug(f"{credentials2} EXISTS -> setting to netatmo_data")
                with open(credentials2, "r") as fin2:
                    netatmo_data = json.load(fin2)
            else:
                logger.debug(f"{credentials2} DOES NOT EXIST")

        with open(credentials, "w") as f:
            f.write(json.dumps(netatmo_data, indent=True))

    logger.debug("actual netatmo_data now:")
    logger.debug(json.dumps(netatmo_data, indent=True))

    return netatmo_data


# for k in os.environ.keys():
#     logger.debug(f"ENV[{k}]: {os.getenv(k)}")

ensure_up2date_netatmo_credentialsfile()

import netatmostuff.lnetatmo as lnetatmo


def job(mqttclient: MosquittoClientWrapper) -> None:
    # Example: USERNAME and PASSWORD supposed to be defined by one of the previous methods

    ensure_up2date_netatmo_credentialsfile()

    personal_credfile: Path = Path(expanduser("~/.netatmo.credentials"))
    auth_data = lnetatmo.ClientAuth(credentialFile=personal_credfile)
    #     clientId=os.environ.get("NETATMO_CLIENT_ID"),
    #     clientSecret=os.environ.get("NETATMO_CLIENT_SECRET"),
    #     refreshToken=refreshToken,
    # )

    # # 2 : Get devices list
    weather_data: lnetatmo.WeatherStationData = lnetatmo.WeatherStationData(auth_data)
    logger.debug(f"{type(weather_data)=}")
    # weather_data.getMeasure()

    if weather_data is not None and weather_data.rawDataPostRequest is not None:
        logger.debug(f"** RAW DATA {type(weather_data.rawDataPostRequest)=} **")
        if isinstance(weather_data.rawDataPostRequest, bytes):
            logger.debug(
                Helper.get_pretty_dict_json_no_sort(json.loads(weather_data.rawDataPostRequest.decode("utf-8")))
            )
        elif isinstance(weather_data.rawDataPostRequest, str):
            logger.debug(Helper.get_pretty_dict_json_no_sort(json.loads(weather_data.rawDataPostRequest)))
        elif isinstance(weather_data.rawDataPostRequest, dict) or isinstance(weather_data.rawDataPostRequest, list):
            logger.debug(Helper.get_pretty_dict_json_no_sort(weather_data.rawDataPostRequest))

        logger.debug("/** RAW DATA **")

    station = weather_data.getStation()
    logger.debug(f"{type(station)=}")
    if station is not None:
        logger.debug(Helper.get_pretty_dict_json_no_sort(station))

    if not "dashboard_data" in station:
        logger.debug(f"NO DASHBOARD_DATA IN STATION!!!")
    else:
        logger.debug(f"DASHBOARD_DATA IN STATION!!!")
        logger.debug(Helper.get_pretty_dict_json_no_sort(station["dashboard_data"]))

    for hn, home in weather_data.homes.items():
        logger.debug(f"Home {hn}:")
        logger.debug(home)

    for sn, stationme in weather_data.stations.items():
        logger.debug(f"Station {sn}:")
        logger.debug(stationme)

    relative_pressure: float = station["dashboard_data"]["Pressure"]
    absolute_pressure: float = station["dashboard_data"]["AbsolutePressure"]
    pressure_t: int = station["dashboard_data"]["time_utc"]
    pressure_tempdt: datetime.datetime = datetime.datetime.fromtimestamp(pressure_t, tz=pytz.UTC).astimezone(_tzberlin)
    pressure_tempdt = pressure_tempdt.replace(second=0, microsecond=0)
    logger.debug(f"Absolute Pressure: {absolute_pressure} {pressure_t=} {pressure_tempdt=}")
    logger.debug(f"Relative Pressure: {relative_pressure} {pressure_t=} {pressure_tempdt=}")

    # print(f"{weather_data.default_station=} {type(weather_data.default_station_data)=} {weather_data.default_station_data=}")
    # print(f"{weather_data.homes=}")
    # print(f"{weather_data.modulesNamesList(station=station['station_name'])=}")

    aussen: Optional[dict] = None
    regen: Optional[dict] = None
    for n in weather_data.modulesNamesList(station=station["station_name"]):
        mod: dict = weather_data.moduleByName(n)
        logger.debug(f"{n} => {type(mod)=}")
        if not mod:
            logger.debug(f"EMPTY MOD (for {n}) !!!")
            continue

        logger.debug(Helper.get_pretty_dict_json_no_sort(mod))
        if n == settings.netatmo.outdoormodule.name or (
            mod is not None and mod["_id"] == str(settings.netatmo.outdoormodule.id)
        ):
            aussen = mod
        if n == settings.netatmo.rainmodule.name or (
            mod is not None and mod["_id"] == str(settings.netatmo.rainmodule.id)
        ):
            regen = mod

    if not aussen:
        raise Exception("AUSSEN MODULE NOT FOUND")

    if not regen:
        raise Exception("REGEN MODULE NOT FOUND")

    logger.debug("AUSSEN:")
    logger.debug(Helper.get_pretty_dict_json_no_sort(aussen))

    logger.debug("REGEN:")
    logger.debug(Helper.get_pretty_dict_json_no_sort(regen))

    if not "dashboard_data" in aussen:
        raise Exception("NO DASHBOARD DATA IN AUSSEN-MODULE FOUND")

    if not "dashboard_data" in regen:
        raise Exception("NO DASHBOARD DATA IN REGEN-MODULE FOUND")

    # try:
    #     logger.debug("MEASURES TEST...")
    #     bft: int = round((datetime.datetime.now()-timedelta(hours=1)).timestamp())
    #     measures: dict = weather_data.getMeasure(
    #         device_id=weather_data.default_station_data["_id"],
    #         real_time=True,
    #         scale="30min",
    #         #module_id=regen["_id"],
    #         mtype="pressure",  #"pressure,min_pressure,max_pressure",
    #         date_begin=bft,
    #         # limit="1"
    #     )
    #     logger.debug(f"MEASURES RESULT: {measures}")
    #     logger.debug(f"{type(measures)=} => {measures=}")
    #     logger.debug(Helper.getPrettyDictJSON(measures))
    #     for ts in measures["body"].keys():  # sollte nur einen geben
    #         logger.debug(f"{type(ts)=} => {ts}")
    #         dt_object = datetime.datetime.fromtimestamp(float(ts), tz=pytz.UTC).astimezone(_tzberlin)
    #         # dt_object = dt_object.replace(tzinfo=_tzberlin)
    #         values: List[float] = measures["body"].get(ts)
    #         logger.debug(f"Datetime: {dt_object} -> {values[0]}")
    #     logger.debug("/MEASURES TEST...")
    #
    #     # │ 2025-11-28 17:14:09.676 | DEBUG    | __main__:job:305 - {                                                                                                                                                                                           │
    #     # │     "body": {                                                                                                                                                                                                                                       │
    #     # │         "1764342849": [                                                                                                                                                                                                                             │
    #     # │             1014                                                                                                                                                                                                                                    │
    #     # │         ],                                                                                                                                                                                                                                          │
    #     # │         "1764344649": [                                                                                                                                                                                                                             │
    #     # │             1013.8                                                                                                                                                                                                                                  │
    #     # │         ]                                                                                                                                                                                                                                           │
    #     # │     },                                                                                                                                                                                                                                              │
    #     # │     "status": "ok",                                                                                                                                                                                                                                 │
    #     # │     "time_exec": 0.027267932891845703,                                                                                                                                                                                                              │
    #     # │     "time_server": 1764346449                                                                                                                                                                                                                       │
    #     # │ }
    # except Exception as exx:
    #     logger.opt(exception=exx).exception(exx)

    # try:
    #     logger.debug("MEASURES TEST FOR REGEN MODULE...")
    #     bft: int = round((datetime.datetime.now()-timedelta(hours=1)).timestamp())
    #     measures: dict = weather_data.getMeasure(
    #         device_id=weather_data.default_station_data["_id"],
    #         scale="1hour",
    #         module_id=regen["_id"],
    #         mtype="pressure,min_pressure,max_pressure",
    #         date_begin=bft,
    #         # limit="1"
    #     )
    #     logger.debug(f"{type(measures)=} => {measures=}")
    #     logger.debug(Helper.getPrettyDictJSON(measures))
    #     for ts in measures["body"].keys():  # sollte nur einen geben
    #         logger.debug(f"{type(ts)=} => {ts}")
    #         dt_object = datetime.datetime.fromtimestamp(float(ts), tz=pytz.UTC).astimezone(_tzberlin)
    #         # dt_object = dt_object.replace(tzinfo=_tzberlin)
    #         values: List[float] = measures["body"].get(ts)
    #         logger.debug(f"Datetime: {dt_object} -> {values[0]}")
    #     logger.debug("/MEASURES TEST FOR REGEN MODULE...")
    # except Exception as exx:
    #     logger.opt(exception=exx).exception(exx)

    before_one_hour = datetime.datetime.now(tz=_tzberlin) - timedelta(hours=1)  # midpoint of now -1h
    before_one_hour = before_one_hour.replace(minute=30, second=0, microsecond=0)
    logger.debug(f"{type(before_one_hour)=} {before_one_hour=}")

    before_24_hours = datetime.datetime.now(tz=_tzberlin) - timedelta(hours=12)  # midpoint of now -24h
    before_24_hours = before_24_hours.replace(minute=0, second=0, microsecond=0)
    logger.debug(f"{type(before_24_hours)=} {before_24_hours=}")

    cur_temp: float = aussen["dashboard_data"]["Temperature"]  # type: ignore
    aussen_t: int = aussen["dashboard_data"]["time_utc"]  # type: ignore
    aussen_tempdt: datetime.datetime = datetime.datetime.fromtimestamp(aussen_t, tz=pytz.UTC).astimezone(_tzberlin)
    aussen_tempdt = aussen_tempdt.replace(second=0, microsecond=0)
    logger.debug(f"Current Temperature: {cur_temp} {aussen_t=} {aussen_tempdt=}")

    rain: float = regen["dashboard_data"]["Rain"]  # type: ignore
    rain_sum_1: float = regen["dashboard_data"]["sum_rain_1"]  # type: ignore
    rain_sum_24: float = regen["dashboard_data"]["sum_rain_24"]  # type: ignore
    rain_t: int = regen["dashboard_data"]["time_utc"]  # type: ignore
    rain_tempdt: datetime.datetime = datetime.datetime.fromtimestamp(rain_t, tz=pytz.UTC).astimezone(_tzberlin)
    rain_tempdt = rain_tempdt.replace(second=0, microsecond=0)

    logger.debug(f"Current Rain: {rain=} {rain_sum_1=} {rain_sum_24=} {rain_t=} {rain_tempdt=}")

    send_to_mosquitto(
        mqttclient=mqttclient,
        temp=cur_temp,
        absolute_pressure=absolute_pressure,
        pressure=relative_pressure,
        rain=rain,
        rain1h=rain_sum_1,
        rain24h=rain_sum_24,
        created_at_temp=aussen_tempdt,
        created_at_pressure=pressure_tempdt,
        created_at_rain=rain_tempdt,
        created_at_rain1h=before_one_hour,
        created_at_rain24h=before_24_hours,
    )

    # measures: dict = weather_data.getMeasure(
    #     device_id=weather_data.default_station_data["_id"],
    #     scale="1hour",
    #     module_id=regen["_id"],
    #     mtype="sum_rain",
    #     date_begin=int(bft),
    #     limit="1"
    # )
    # logger.debug(f"{type(measures)=} => {measures=}")
    # logger.debug(Helper.getPrettyDictJSON(measures))
    # for ts in measures["body"].keys():  # sollte nur einen geben
    #     logger.debug(f"{type(ts)=} => {ts}")
    #     dt_object = datetime.datetime.fromtimestamp(float(ts), tz=pytz.UTC).astimezone(_tzberlin)
    #     # dt_object = dt_object.replace(tzinfo=_tzberlin)
    #     values: List[float] = measures["body"].get(ts)
    #     logger.debug(f"Datetime: {dt_object} -> {values[0]}")
    #
    #
    #     send_to_mosquitto(
    #         aussen["dashboard_data"]["Temperature"],
    #         values[0],
    #         created_at_temp=aussen_tempdt,
    #         created_at_rain=dt_object
    #     )


def exc_caught_job_loop(mqttclient: MosquittoClientWrapper, maxtries: int = 10) -> int:
    for i in range(0, maxtries):
        try:
            logger.debug(f"RUNNING EXCCAUGHT LOOP #{i+1}/{maxtries}")
            job(mqttclient=mqttclient)

            write_netatmo_credentials_to_shared_file()

            return 0
        except Exception as ex:
            # logger.exception(Helper.get_exception_tb_as_string(ex))
            # Helper.eprint(Helper.getExceptionTBAsString(ex))
            logger.opt(exception=ex).exception(ex)
            time.sleep(2)

    return 1


def run_test_netatmo() -> None:
    ensure_up2date_netatmo_credentialsfile()

    auth_data = lnetatmo.ClientAuth(credentialFile=Path(expanduser("~/.netatmo.credentials")))
    #     clientId=os.environ.get("NETATMO_CLIENT_ID"),
    #     clientSecret=os.environ.get("NETATMO_CLIENT_SECRET"),
    #     refreshToken=refreshToken,
    # )

    # # 2 : Get devices list
    weather_data: lnetatmo.WeatherStationData = lnetatmo.WeatherStationData(auth_data)
    station = weather_data.getStation()

    logger.debug(f"{type(station)=}")
    logger.debug(Helper.get_pretty_dict_json_no_sort(station))


def main() -> int:
    schedulexseconds: int = 300
    sleeptimexseconds: int = 10

    mqttclient: MosquittoClientWrapper = MosquittoClientWrapper(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        timeout_connect_seconds=10,
    )

    connected: bool = mqttclient.wait_for_connect_and_start_loop()  # starts loop in thread, so we can continue...
    logger.debug(f"mqttclient.is_connected()={mqttclient.is_connected()}")

    if not connected:
        logger.error("Failed to connect MQTT Client")
        return 123

    ret: int = exc_caught_job_loop(mqttclient=mqttclient, maxtries=10)

    if len(sys.argv) > 1 and not sys.argv[1] == "shootonce":
        if len(sys.argv) == 3:
            schedulexseconds = int(sys.argv[2])
            sleeptimexseconds = int(sys.argv[3])

        logger.info(f"{schedulexseconds=} {sleeptimexseconds=}")

        schedule.every(schedulexseconds).seconds.do(exc_caught_job_loop, mqttclient=mqttclient, maxtries=10)

        while True:
            run_pending()
            time.sleep(sleeptimexseconds)
    else:
        exit(ret)


if __name__ == "__main__":
    # logger.debug(f"{sys.argv=}")
    # sys.argv.append("loopme")
    main_ret: int = main()
    exit(main_ret)

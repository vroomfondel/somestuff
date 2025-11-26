import json
import logging
from os.path import expanduser, exists
from pathlib import Path

from config import settings
from config import _EFFECTIVE_CONFIG as effconfig  # dirty.

import os
from typing import Optional, List, Tuple, Union, Dict

import sys
from datetime import timedelta

import pytz
import schedule
from schedule import run_pending

import datetime, time

import Helper
from mqttstuff.mosquittomqttwrapper import MosquittoClientWrapper

_tzberlin: datetime.tzinfo = pytz.timezone("Europe/Berlin")

from loguru import logger

os.environ["LOGURU_LEVEL"] = os.getenv("LOGURU_LEVEL", "DEBUG")  # standard is DEBUG
logger.remove()  # remove default-handler
# # https://buildmedia.readthedocs.org/media/pdf/loguru/latest/loguru.pdf
logger.add(sys.stderr, level=os.getenv("LOGURU_LEVEL"))  # type: ignore  # TRACE | DEBUG | INFO | WARN | ERROR |  FATAL

nosendmosquitto: bool = False


def send_to_mosquitto(
    client: MosquittoClientWrapper,
    temp: Optional[float],
    rain: Optional[float],
    rain1h: Optional[float],
    rain24h: Optional[float],
    created_at_temp: Optional[datetime.datetime] = None,
    created_at_rain: Optional[datetime.datetime] = None,
    created_at_rain1h: Optional[datetime.datetime] = None,
    created_at_rain24h: Optional[datetime.datetime] = None,
) -> None:
    msgs: List[Tuple[str, Union[int, float, str, Dict], Optional[datetime.datetime], Optional[Dict]]] = []
    if temp is not None:
        msgs.append(
            (
                effconfig["mqtt"]["tempfeed"],  # type: ignore  # str
                temp,
                created_at_temp,
                effconfig["mqtt"]["DEFAULTMETADATA_RB12"],  # type: ignore  # str
            )
        )

    if rain is not None:
        msgs.append(
            (
                effconfig["mqtt"]["rainfeed"],  # type: ignore  # str
                rain,
                created_at_rain,
                effconfig["mqtt"]["DEFAULTMETADATA_RB12"],  # type: ignore  # str
            )
        )

    if rain1h is not None:
        msgs.append(
            (
                effconfig["mqtt"]["rainfeed1h"],  # type: ignore  # str
                rain1h,
                created_at_rain1h,
                effconfig["mqtt"]["DEFAULTMETADATA_RB12"],  # type: ignore  # str
            )
        )

    if rain24h is not None:
        msgs.append(
            (
                effconfig["mqtt"]["rainfeed24h"],  # type: ignore  # str
                rain24h,
                created_at_rain24h,
                effconfig["mqtt"]["DEFAULTMETADATA_RB12"],  # type: ignore  # str
            )
        )

    if nosendmosquitto:
        logger.debug(f"{nosendmosquitto=}")
        logging.debug(msgs)
        return

    # TODO HT20251126 adapt!
    client.publish_multiple(msgs)  # type: ignore  # check if msgs is list and correct type


def write_netatmo_credentials_to_shared_file() -> None:
    logger.debug("Crontanamo::write_netatmo_credentialsfileshared")

    if os.getenv("STORAGEPATH"):
        logger.debug("STORAGEPATH is in ENV (" + os.environ["STORAGEPATH"] + ")")

        CREDENTIALS = expanduser("~/.netatmo.credentials")

        if exists(CREDENTIALS):
            logger.debug(f"{CREDENTIALS} EXISTS -> preparing copy")

            CREDENTIALS2 = os.environ["STORAGEPATH"] + "/netatmo.credentials"

            with open(CREDENTIALS, "r") as fin:
                netatmo_data = json.load(fin)

                # if "ACCESS_TOKEN" not in netatmo_data and os.getenv("_NETATMO_ACCESS_TOKEN"):
                #     netatmo_data["ACCESS_TOKEN"] = os.environ["_NETATMO_ACCESS_TOKEN"]

                with open(CREDENTIALS2, "w") as f:
                    f.write(json.dumps(netatmo_data, indent=True))
    else:
        logger.debug("STORAGEPATH is NOT set")


def ensure_up2date_netatmo_credentialsfile() -> dict:
    logger.debug("Crontanamo::ensure_netatmo_credentialsfile")
    netatmo_data: dict = {}
    CREDENTIALS = expanduser("~/.netatmo.credentials")
    if exists(CREDENTIALS):
        logger.debug(f"{CREDENTIALS} exists")

        with open(CREDENTIALS, "r") as fin:
            netatmo_data = json.load(fin)

        if os.getenv("STORAGEPATH"):
            CREDENTIALS2 = os.environ["STORAGEPATH"] + "/netatmo.credentials"

            if exists(CREDENTIALS2):
                logger.debug(f"{CREDENTIALS2} EXISTS -> checking if newer than {CREDENTIALS}")

                crstat: int = os.stat(CREDENTIALS).st_mtime  # type: ignore
                cr2stat: int = os.stat(CREDENTIALS2).st_mtime  # type: ignore

                if cr2stat > crstat:
                    logger.debug(
                        f"{CREDENTIALS2} ({datetime.datetime.fromtimestamp(cr2stat)}) NEWER THAN {CREDENTIALS} ({datetime.datetime.fromtimestamp(crstat)}) -> OVERWRITING"
                    )

                    with open(CREDENTIALS2, "r") as fin2:
                        netatmo_data = json.load(fin2)
                        # if "ACCESS_TOKEN" not in netatmo_data and os.getenv("_NETATMO_ACCESS_TOKEN"):
                        #     netatmo_data["ACCESS_TOKEN"] = os.environ["_NETATMO_ACCESS_TOKEN"]

                    with open(CREDENTIALS, "w") as f:
                        f.write(json.dumps(netatmo_data, indent=True))
    else:
        logger.debug(f"{CREDENTIALS} DOES NOT exist -> setting netatmo_data from config")
        netatmo_data["CLIENT_ID"] = os.getenv("_NETATMO_CLIENT_ID")
        netatmo_data["CLIENT_SECRET"] = os.getenv("_NETATMO_CLIENT_SECRET")
        netatmo_data["REFRESH_TOKEN"] = os.getenv("_NETATMO_REFRESH_TOKEN")

        logger.debug("netatmo_data from ENV:")
        logger.debug(json.dumps(netatmo_data, indent=True))

        if os.getenv("STORAGEPATH"):
            CREDENTIALS2 = os.environ["STORAGEPATH"] + "/netatmo.credentials"

            if exists(CREDENTIALS2):
                logger.debug(f"{CREDENTIALS2} EXISTS -> setting to netatmo_data")
                with open(CREDENTIALS2, "r") as fin2:
                    netatmo_data = json.load(fin2)
            else:
                logger.debug(f"{CREDENTIALS2} DOES NOT EXIST")

        with open(CREDENTIALS, "w") as f:
            f.write(json.dumps(netatmo_data, indent=True))

    logger.debug("actual netatmo_data now:")
    logger.debug(json.dumps(netatmo_data, indent=True))

    return netatmo_data


# for k in os.environ.keys():
#     logger.debug(f"ENV[{k}]: {os.getenv(k)}")

ensure_up2date_netatmo_credentialsfile()

import lnetatmo  #  type: ignore


def job() -> None:
    # Example: USERNAME and PASSWORD supposed to be defined by one of the previous methods

    ensure_up2date_netatmo_credentialsfile()

    personal_credfile: Path = Path(expanduser("~/.netatmo.credentials"))
    authData = lnetatmo.ClientAuth(credentialFile=personal_credfile)
    #     clientId=os.environ.get("NETATMO_CLIENT_ID"),
    #     clientSecret=os.environ.get("NETATMO_CLIENT_SECRET"),
    #     refreshToken=refreshToken,
    # )

    # # 2 : Get devices list
    weatherData: lnetatmo.WeatherStationData = lnetatmo.WeatherStationData(authData)
    station = weatherData.getStation()

    # print(f"{type(station)=}")
    # Helper.printPrettyDictJSON(station)

    # print(f"{weatherData.default_station=} {type(weatherData.default_station_data)=} {weatherData.default_station_data=}")
    # print(f"{weatherData.homes=}")
    # print(f"{weatherData.modulesNamesList(station=station['station_name'])=}")

    aussen: Optional[dict] = None
    regenroep: Optional[dict] = None
    for n in weatherData.modulesNamesList(station=station["station_name"]):
        mod: dict = weatherData.moduleByName(n)
        logger.debug(f"{n} => {type(mod)=}")
        if not mod:
            logger.debug(f"EMPTY MOD (for {n}) !!!")
            continue

        logger.debug(Helper.get_pretty_dict_json_no_sort(mod))
        if n == "Außen" or mod["_id"] == "02:00:00:02:e7:f4":
            aussen = mod
        elif n == "RegenRoep12b":
            regenroep = mod

    if not aussen:
        raise Exception("AUSSEN MODULE NOT FOUND")

    if not regenroep:
        raise Exception("REGENROEP MODULE NOT FOUND")

    logger.debug("AUSSEN:")
    logger.debug(Helper.get_pretty_dict_json_no_sort(aussen))

    logger.debug("regenroep:")
    logger.debug(Helper.get_pretty_dict_json_no_sort(regenroep))

    if not "dashboard_data" in aussen:
        raise Exception("NO DASHBOARD DATA IN AUSSEN-MODULE FOUND")

    if not "dashboard_data" in regenroep:
        raise Exception("NO DASHBOARD DATA IN REGENROEP-MODULE FOUND")

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

    rain: float = regenroep["dashboard_data"]["Rain"]  # type: ignore
    rain_sum_1: float = regenroep["dashboard_data"]["sum_rain_1"]  # type: ignore
    rain_sum_24: float = regenroep["dashboard_data"]["sum_rain_24"]  # type: ignore
    rain_t: int = regenroep["dashboard_data"]["time_utc"]  # type: ignore
    rain_tempdt: datetime.datetime = datetime.datetime.fromtimestamp(rain_t, tz=pytz.UTC).astimezone(_tzberlin)
    rain_tempdt = rain_tempdt.replace(second=0, microsecond=0)

    logger.debug(f"Current Rain: {rain=} {rain_sum_1=} {rain_sum_24=} {rain_t=} {rain_tempdt=}")

    # TODO HT20251126 move to correct location and make proper init with topic-subscription setup!
    client: MosquittoClientWrapper = MosquittoClientWrapper(
        host=settings.mqtt.host, port=settings.mqtt.port, username=settings.mqtt.username, timeout_connect_seconds=10
    )

    send_to_mosquitto(
        client,
        temp=cur_temp,
        rain=rain,
        rain1h=rain_sum_1,
        rain24h=rain_sum_24,
        created_at_temp=aussen_tempdt,
        created_at_rain=rain_tempdt,
        created_at_rain1h=before_one_hour,
        created_at_rain24h=before_24_hours,
    )

    # measures: dict = weatherData.getMeasure(
    #     device_id=weatherData.default_station_data["_id"],
    #     scale="1hour",
    #     module_id=regenroep["_id"],
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


def exc_caught_job_loop(maxtries: int = 10) -> int:
    for i in range(0, maxtries):
        try:
            logger.debug(f"RUNNING EXCCAUGHT LOOP #{i+1}/{maxtries}")
            job()

            write_netatmo_credentials_to_shared_file()

            return 0
        except Exception as ex:
            logger.exception(Helper.get_exception_tb_as_string(ex))
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

    print(f"{type(station)=}")
    Helper.print_pretty_dict_json(station)


if __name__ == "__main__":
    # run_test_netatmo()
    # exit(123)

    schedulexseconds: int = 300
    sleeptimexseconds: int = 10

    ret: int = exc_caught_job_loop(10)

    if len(sys.argv) > 1 and not sys.argv[1] == "shootonce":
        if len(sys.argv) == 3:
            schedulexseconds = int(sys.argv[2])
            sleeptimexseconds = int(sys.argv[3])

        logger.info(f"{schedulexseconds=} {sleeptimexseconds=}")

        schedule.every(schedulexseconds).seconds.do(exc_caught_job_loop)

        while True:
            run_pending()
            time.sleep(sleeptimexseconds)
    else:
        exit(ret)


#     def run_day_schedule_loop(self):
#         schedule.every().hour.at(":01", tz=_tzberlin).do(self.run_logic_locked)
#
#         # schedule.every().day.at(f"{earlyhour:02}:00", tz=_tzberlin).do(
#         #     self.run_logic_locked,
#         #     earlyhour=earlyhour,
#         #     latesthour=latesthour,
#         #     tempmin=tempmin,
#         # )
#
#         for j in schedule.get_jobs():
#             self.logger.debug(f"{j=} {j.next_run=} {j.last_run=}")
#
#         sleeptimexseconds: int = 300
#         while True:
#             run_pending()
#             self.logger.debug(f"{threading.current_thread().name=}::start to sleep for {sleeptimexseconds}s")
#             time.sleep(sleeptimexseconds)
#
#     def runall(self):
#
#         schedulethread: threading.Thread = threading.Thread(
#             target=self.run_day_schedule_loop,
#             name="schedulethread",
#             daemon=True,
#         )
#         schedulethread.start()
#
#         self.setup_mqtt_with_triggers_start_loop_forever()

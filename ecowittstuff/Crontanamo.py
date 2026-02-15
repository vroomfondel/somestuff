import datetime
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pytz

# https://github.com/dbader/schedule
import schedule
from mqttstuff import MosquittoClientWrapper, MWMqttMessage
from schedule import run_pending

import Helper
from config import _EFFECTIVE_CONFIG as effconfig  # dirty.
from config import settings
from ecowittstuff.ecowittapi import WeatherStationResponse, get_realtime_data

_tzberlin: datetime.tzinfo = pytz.timezone("Europe/Berlin")

from loguru import logger

os.environ["LOGURU_LEVEL"] = os.getenv("LOGURU_LEVEL", "DEBUG")  # standard is DEBUG
logger.remove()  # remove default-handler
# # https://buildmedia.readthedocs.org/media/pdf/loguru/latest/loguru.pdf
logger.add(sys.stderr, level=os.getenv("LOGURU_LEVEL"))  # type: ignore  # TRACE | DEBUG | INFO | WARN | ERROR |  FATAL

NOSENDMOSQUITTO: bool = False


def send_to_mosquitto(
    mqttclient: MosquittoClientWrapper,
    wsr: WeatherStationResponse,
) -> None:
    assert isinstance(effconfig, dict)
    assert "mqtt_topics" in effconfig
    assert "mqtt_message_default_metadata" in effconfig

    # assert isinstance(effconfig["mqtt_topics"], dict)
    # assert "ecowitt" in effconfig["mqtt_topics"] and isinstance(effconfig["mqtt_topics"]["ecowitt"], dict)
    # assert "pressure" in effconfig["mqtt_topics"]["ecowitt"] and isinstance(effconfig["mqtt_topics"]["ecowitt"]["pressure"], dict)

    # msgs: List[Tuple[str, Union[int, float, str, Dict], Optional[datetime.datetime], Optional[Dict]]] = []
    msgs: List[MWMqttMessage] = []

    topic: str = effconfig["mqtt_topics"]["ecowitt"]["pressure"]["topic"]
    metadata: Dict[str, Any] = effconfig[
        "mqtt_message_default_metadata"
    ].copy()  # copy just to make sure not to change the original/have some memory problems after a while due to references

    msgs.append(
        MWMqttMessage(
            topic=topic,
            # value=wsr.data.pressure.absolute.value,
            value={"absolute": wsr.data.pressure.absolute.value, "relative": wsr.data.pressure.relative.value},
            valuedt=wsr.data.pressure.absolute.time_as_datetime,
            retained=False,
            metadata=metadata,
            rettype="valuemsg",  # Literal["json", "str", "int", "float", "valuemsg", "str_raw"] = "valuemsg"
        )
    )

    if NOSENDMOSQUITTO:
        logger.debug(f"{NOSENDMOSQUITTO=}")
        logger.debug(msgs)
        return

    mqttclient.publish_multiple(msgs)  # type: ignore  # check if msgs is list and correct type


def job(mqttclient: MosquittoClientWrapper) -> None:
    wsr: WeatherStationResponse = get_realtime_data()

    send_to_mosquitto(mqttclient, wsr=wsr)


def exc_caught_job_loop(mqttclient: MosquittoClientWrapper, maxtries: int = 10) -> int:
    for i in range(0, maxtries):
        try:
            logger.debug(f"RUNNING EXCCAUGHT LOOP #{i+1}/{maxtries}")
            job(mqttclient=mqttclient)

            return 0
        except Exception as ex:
            logger.opt(exception=ex).exception(ex)
            time.sleep(2)

    return 1


def main() -> int:
    schedulexseconds: int = 30  # 0
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
        return ret


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


if __name__ == "__main__":
    # logger.debug(f"{sys.argv=}")
    # sys.argv.append("loopme")
    main_ret: int = main()
    exit(main_ret)

import datetime
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# import pytz
from jinja2 import Template
from loguru import logger
from reputils import MailReport

import config
import Helper
from config import TIMEZONE, is_in_cluster, settings
from mqttstuff.mosquittomqttwrapper import MQTTLastDataReader, MWMqttMessage

# from netatmostuff.Crontanamo import write_netatmo_credentials_to_shared_file
# import netatmostuff.lnetatmo as lnetatmo


_templatedirpath: Path = Path(__file__).parent.resolve()

smtpip: str = os.getenv("SMTP_IP", str(settings.hydromail.smtpip))
mailfrom: str = os.getenv("MAILFROM", settings.hydromail.mailfrom)
mailreplyto: str = os.getenv("MAILREPLYTO", settings.hydromail.mailreplyto)
mailsubject_base: str = os.getenv("MAILSUBJECT_BASE", settings.hydromail.mailsubject_base)

mailrecipients_to: List[str] = [str(i) for i in settings.hydromail.mailrecipients_to]
if os.getenv("MAILTOS") is not None and os.getenv("MAILTOS") != "":
    mailrecipients_to = [i.strip() for i in os.getenv("MAILTOS").split(",")]  # type: ignore

mailrecipients_cc: List[str] = [str(i) for i in settings.hydromail.mailrecipients_cc]
if os.getenv("MAILCCS") is not None and os.getenv("MAILCCS") != "":
    mailrecipients_cc = [i.strip() for i in os.getenv("MAILCCS").split(",")]  # type: ignore


_sdfD_formatstring: str = "%d.%m.%Y"
_sdfDHM_formatstring: str = "%d.%m.%Y %H:%M"
_sdfE_formatstring: str = "%Y%m%d"

DISABLE_MAIL_SEND: bool = os.getenv("DISABLE_MAIL_SEND", "False") == "True"


def mail_stuff(
    netatmodata: dict,
    current_temp: float,
    rain_lasthour: float,
    rain_overall_today: float,
    rain_overall_yesterday: float,
    wasserbisoberkante: float | None,
    current_ma: float | None,
    wasserandma_dt: datetime.datetime | None,
    current_busvoltage: float | None,
) -> None:

    fp: Path = Path(_templatedirpath, "mailtemplate.html.j2")
    logger.debug(f"{fp.absolute()=}")

    with open(fp) as file_:
        template = Template(file_.read())

        serverinfo = MailReport.SMTPServerInfo(
            smtp_server=smtpip, smtp_port=25, useStartTLS=True, wantsdebug=False, ignoresslerrors=True
        )

        now: datetime.datetime = datetime.datetime.now(tz=TIMEZONE)
        sdd: str = now.strftime(_sdfD_formatstring)

        sendmail: MailReport.MRSendmail = MailReport.MRSendmail(
            serverinfo=serverinfo,
            returnpath=MailReport.EmailAddress.fromSTR(mailfrom),
            replyto=MailReport.EmailAddress.fromSTR(mailreplyto),
            subject=f"{mailsubject_base} :: {sdd}",
        )
        sendmail.tos = [MailReport.EmailAddress.fromSTR(k) for k in mailrecipients_to]

        # for to in mailrecipients_to:
        #     sendmail.addTo(MailReport.EmailAddress.fromSTR(to))

        if mailrecipients_cc is not None:
            sendmail.ccs = [MailReport.EmailAddress.fromSTR(k) for k in mailrecipients_cc]

        values: dict = {
            "wasserbisoberkante": wasserbisoberkante,
            "current_ma": current_ma,
            "current_busvoltage": current_busvoltage,
            "current_temp": current_temp,
            "rain_lasthour": rain_lasthour,
            "rain_overall_today": rain_overall_today,
            "rain_overall_yesterday": rain_overall_yesterday,
            "netatmodata": netatmodata,
            "wasserandma_dt": (
                None
                if wasserandma_dt is None
                else wasserandma_dt.astimezone(tz=TIMEZONE).strftime(_sdfDHM_formatstring)
            ),
        }
        mt_html: str = template.render(values)
        logger.debug(mt_html)

        if not DISABLE_MAIL_SEND:
            sendmail.send(html=mt_html)


def _get_latest_from_mqtt(
    topic: str, value_fieldname: str, created_at_fieldname: str = "orig_time", noisy: bool = False
) -> Tuple[float | None, datetime.datetime | None]:
    data_received: List[MWMqttMessage] | None = MQTTLastDataReader.get_most_recent_data_with_timeout(
        host=settings.mqtt.host,
        port=settings.mqtt.port,
        username=settings.mqtt.username,
        password=settings.mqtt.password,
        topics=topic,
        noisy=noisy,
        retained="yes",  # also get retained messages
        rettype="json",
        # rettype="valuemsg",
        # created_at_fieldname=created_at_fieldname  # uargs.
    )

    if data_received is None:
        raise Exception(f"No Data received from MQTT in {topic=}")

    logger.debug(f"Data received from MQTT on {topic=}:")
    data: Dict[str, float | str] = data_received[0].value  # type: ignore

    logger.debug(f"{type(data)=}")
    logger.debug(Helper.get_pretty_dict_json_no_sort(data, 4))

    retv: float | None = data.get(value_fieldname)  # type: ignore
    ret_dt: datetime.datetime | None = None
    ret_dt_s: str | None | float = data.get(created_at_fieldname)
    if ret_dt_s is not None and isinstance(ret_dt_s, str):
        ret_dt = datetime.datetime.fromisoformat(ret_dt_s).astimezone(tz=TIMEZONE)

    logger.debug(f"Value from MQTT topic '{topic}' is {retv} with created_at '{ret_dt_s}'")
    return retv, ret_dt


def get_current_waterlevel_and_busvoltage_and_ma(
    noisy: bool = False,
) -> Tuple[
    Tuple[float | None, datetime.datetime | None],
    Tuple[float | None, datetime.datetime | None],
    Tuple[float | None, datetime.datetime | None],
]:
    wasserstand_created_at: datetime.datetime | None
    wasserstand: float | None

    ma_created_at: datetime.datetime | None
    ma: float | None

    busvoltage_created_at: datetime.datetime | None
    busvoltage: float | None

    topic: str
    wasserstandsmesser_topics: Dict[str, config.MqttTopic] = settings.mqtt_topics.root.get("wasserstandsmesser", {})
    logger.debug(Helper.get_pretty_dict_json_no_sort(wasserstandsmesser_topics))

    assert "wasserstand" in wasserstandsmesser_topics
    topic = wasserstandsmesser_topics["wasserstand"].topic

    wasserstand, wasserstand_created_at = _get_latest_from_mqtt(
        topic=topic, value_fieldname="unteroberkante", created_at_fieldname="orig_time", noisy=noisy
    )

    assert "ma" in wasserstandsmesser_topics
    topic = wasserstandsmesser_topics["ma"].topic

    ma, ma_created_at = _get_latest_from_mqtt(
        topic=topic, value_fieldname="value", created_at_fieldname="created_at", noisy=noisy
    )

    assert "busvoltage" in wasserstandsmesser_topics
    topic = wasserstandsmesser_topics["busvoltage"].topic

    busvoltage, busvoltage_created_at = _get_latest_from_mqtt(
        topic=topic, value_fieldname="value", created_at_fieldname="created_at", noisy=noisy
    )

    return (wasserstand, wasserstand_created_at), (busvoltage, busvoltage_created_at), (ma, ma_created_at)


def read_netatmo() -> dict:
    import netatmostuff.lnetatmo as lnetatmo

    # Example: USERNAME and PASSWORD supposed to be defined by one of the previous methods
    ret: dict = {}

    auth_data = lnetatmo.ClientAuth()
    # auth_data = lnetatmo.ClientAuth(
    #     clientId=os.environ.get("NETATMO_CLIENT_ID"),
    #     clientSecret=os.environ.get("NETATMO_CLIENT_SECRET"),
    #     refreshToken=os.environ.get("NETATMO_REFRESH_TOKEN")
    # )

    weather_data: lnetatmo.WeatherStationData = lnetatmo.WeatherStationData(auth_data)
    logger.debug(f"{type(weather_data.default_station_data)=} {weather_data.default_station_data=}")
    logger.debug(f"{weather_data.homes=}")
    logger.debug(f"{weather_data.modulesNamesList()=}")

    aussen: Optional[dict] = None
    regen: Optional[dict] = None

    for n in weather_data.modulesNamesList():
        mod: dict = weather_data.moduleByName(n)
        logger.debug(f"{n} => {type(mod)=} {mod=}")

        logger.debug(Helper.get_pretty_dict_json_no_sort(mod))

        if n == settings.netatmo.outdoormodule.name or mod["_id"] == str(settings.netatmo.outdoormodule.id):
            aussen = mod
        if n == settings.netatmo.rainmodule.name or mod["_id"] == str(settings.netatmo.rainmodule.id):
            regen = mod

    logger.debug(f"Found Aussen-Module: {aussen=}")
    logger.debug(f"Found Regen-Module: {regen=}")
    current_temp: float = aussen["dashboard_data"]["Temperature"]  # type: ignore
    lasthourrain: float = regen["dashboard_data"]["sum_rain_1"]  # type: ignore
    logger.debug(f"Current Temperature: {current_temp}")
    logger.debug(f"Current Rain: {lasthourrain}")  # Rain | sum_rain_24

    ret["current_temp"] = current_temp
    ret["rain_lasthour"] = lasthourrain

    begin: datetime.datetime = datetime.datetime.now(TIMEZONE)
    begin = begin.replace(day=begin.day - 1, hour=0, minute=0, second=0, microsecond=0)
    logger.debug(f" {begin=} {begin}")

    end: datetime.datetime = datetime.datetime.now(TIMEZONE)
    end = end.replace(hour=end.hour - 1, minute=59, second=59, microsecond=999_999)
    logger.debug(f" {end=} {end}")

    now: datetime.datetime = datetime.datetime.now(TIMEZONE)
    now_sdf_d: str = now.strftime(_sdfD_formatstring)

    measures: dict = weather_data.getMeasure(
        device_id=weather_data.default_station_data["_id"],  # "70:ee:50:02:ed:4c",  # "Indoor" | homestationid!
        scale="1hour",  # Timeframe between two measurements {30min, 1hour, 3hours, 1day, 1week, 1month}
        mtype="sum_rain",
        module_id=regenroep["_id"],  # type: ignore
        date_begin=int(begin.timestamp()),
        date_end=int(end.timestamp()),
        limit=None,
        optimize=False,
        real_time=False,
    )
    logger.debug(f"{type(measures)=} {measures}")
    bms: dict = measures["body"]
    times: str  # timestamp as string

    ret_measures_today: list[dict] = []
    ret_measures_yesterday: list[dict] = []
    ret["measures_today"] = ret_measures_today
    ret["measures_yesterday"] = ret_measures_yesterday

    ret["rain_overall_yesterday"] = float(0)
    ret["rain_overall_today"] = float(0)

    logger.debug(f"{len(bms)=}")
    if len(bms) == 0:
        raise Exception("NO DATA RETURNED")

    for times in bms:
        measures_here: list[float | int] = bms[times]
        measures_here_0: float = measures_here[0]

        measure_date: datetime.datetime = datetime.datetime.fromtimestamp(float(times), TIMEZONE)
        # logger.debug(f"timestamp: {times}\tmeasure_date: {measure_date}\tmeasures_here: {measures_here}")

        measure_sdf_d: str = measure_date.strftime(_sdfD_formatstring)

        tgt: list[dict] = ret_measures_yesterday

        if now_sdf_d == measure_sdf_d:
            tgt = ret_measures_today
            ret["rain_overall_today"] += measures_here_0
        else:
            ret["rain_overall_yesterday"] += measures_here_0

        tgt.append(
            {
                "date": measure_sdf_d,
                "datetime": measure_date.strftime(_sdfDHM_formatstring),
                "time_millis": measure_date.timestamp(),  # begin auf die stunde ?!
                "rain": measures_here_0,
            }
        )

    return ret


def do_main_stuff() -> None:
    global DISABLE_MAIL_SEND

    if not is_in_cluster():
        DISABLE_MAIL_SEND = True

    try:
        netatmo_data: dict = read_netatmo()

        logger.debug("new getting current_waterlevel and stuff...")
        (wasserbisoberkante, wasserdt), (currentbusvoltage, currentbusvoltagedt), (currentma, currentmadt) = (
            get_current_waterlevel_and_busvoltage_and_ma(noisy=True)
        )

        if DISABLE_MAIL_SEND:
            logger.debug("NOT sending email... DISABLE_MAIL_SEND is activated...")
        else:
            logger.debug("now trying to send email...")
            mail_stuff(
                netatmo_data,
                netatmo_data["current_temp"],
                netatmo_data["rain_lasthour"],
                netatmo_data["rain_overall_today"],
                netatmo_data["rain_overall_yesterday"],
                wasserbisoberkante,
                currentma,
                currentmadt,
                currentbusvoltage,
            )
    except Exception as ex:
        logger.opt(exception=ex).exception(ex)
    finally:
        try:
            import netatmostuff.lnetatmo as lnetatmo
            from netatmostuff.Crontanamo import write_netatmo_credentials_to_shared_file

            write_netatmo_credentials_to_shared_file()
        except Exception as ex:
            logger.opt(exception=ex).exception(ex)


if __name__ == "__main__":
    # get_current_waterlevel_and_busvoltage_and_ma()
    # exit(0)

    do_main_stuff()

    # Timestamp of the first measure to retrieve (Local Unix Time in seconds).
    # By default, it will retrieve the oldest data available.

    # Temperature data (°C) = {temperature, min_temp, max_temp, date_min_temp, date_max_temp}
    # Humidity data (%) = {humidity, min_hum, max_hum, date_min_hum, date_max_hum}
    # CO2 data (ppm) = {co2, min_co2, max_co2, date_min_co2, date_max_co2}
    # Pressure data (bar) = {pressure, min_pressure, max_pressure, date_min_pressure, date_max_pressure}
    # Noise data (db) = {noise, min_noise, max_noise, date_min_noise, date_max_noise}
    # Rain data (mm) = {rain, min_rain, max_rain, sum_rain, date_min_rain, date_max_rain}
    # Wind data (km/h, °) = {windstrength, windangle, guststrength, gustangle, date_min_gust, date_max_gust}

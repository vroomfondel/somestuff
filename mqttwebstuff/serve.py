#!/usr/bin/env python3
"""Serve a live web view onto an MQTT stream.

Subscribes to the broker, maps every message through a mapper plugin (a plain
Python file, see :mod:`mqttwebstuff.plugin_api`) and pushes rendered updates to
all connected browsers via Server-Sent Events. Without ``--mapper`` a generic
fallback shows each ``--topics`` pattern's messages as JSON cards.

Everything is overridable per CLI option or ``MQTTWEB_*`` environment variable
(CLI wins), so the same container image can serve any stream/plugin in k3s.

Usage::

    python3 -m mqttwebstuff.serve --mapper mqttwebstuff/plugins/oepnv_view.py --mqtt-host broker.example.org
    python3 -m mqttwebstuff.serve --topics 'ecowitt/#' --mqtt-host broker.example.org   # generic JSON cards
    MQTTWEB_MAPPER=/config/oepnv_view.py MQTTWEB_MQTT_HOST=broker python3 -m mqttwebstuff.serve

The MQTT connection tolerates an unreachable broker at startup: paho keeps
retrying in the background while the web app is already up (an empty board and
``/healthz`` showing ``mqtt_connected: false``).
"""

import asyncio
import logging
from pathlib import Path

import typer
import uvicorn
from dotenv import load_dotenv
from mqttstuff import MosquittoClientWrapper
from mqttstuff.mosquittomqttwrapper import MWMqttMessage

from mqttwebstuff import configure_logging, print_banner
from mqttwebstuff.hub import ViewHub
from mqttwebstuff.plugin_api import LoadedPlugin, generic_plugin, load_plugin
from mqttwebstuff.webapp import build_environment, create_app

logger = logging.getLogger(__name__)

#: Optional env files (gitignored via ``*.local.*``), searched in the current
#: working directory AND next to this module. ``load_dotenv`` never overrides
#: variables that are already set, so loading the CWD file first yields the
#: precedence: real environment > ``$CWD/mqttweb.local.env`` > module-dir file.
CREDS_FILES = (Path.cwd() / "mqttweb.local.env", Path(__file__).parent / "mqttweb.local.env")
for _creds_file in CREDS_FILES:
    load_dotenv(_creds_file)

app = typer.Typer(add_completion=False, rich_markup_mode=None)


def _build_mqtt_client(
    plugin: LoadedPlugin,
    hub: ViewHub,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    tls: bool,
    tls_ca: str,
    tls_cert: str,
    tls_key: str,
    tls_insecure: bool,
) -> MosquittoClientWrapper:
    """Build the subscribing MQTT client and wire it into the hub.

    Args:
        plugin: Supplies the subscribe patterns.
        hub: Receives every message via its thread-safe :meth:`ViewHub.submit`.
        host: MQTT broker host.
        port: MQTT broker port.
        username: MQTT username (empty = anonymous).
        password: MQTT password.
        tls: Encrypt the connection with TLS.
        tls_ca: CA certificate path (empty = system CA store).
        tls_cert: Client certificate path (mutual TLS).
        tls_key: Client key path (mutual TLS).
        tls_insecure: Skip TLS certificate verification.

    Returns:
        The configured (not yet connected) wrapper.
    """
    client = MosquittoClientWrapper(
        host=host,
        port=port,
        username=username or None,
        password=password or None,
        topics=list(plugin.subscriptions),
        timeout_connect_seconds=15,
        tls=tls,
        tls_ca_certs=tls_ca or None,
        tls_certfile=tls_cert or None,
        tls_keyfile=tls_key or None,
        tls_insecure=tls_insecure,
    )

    def _on_message(msg: MWMqttMessage, userdata: object) -> None:
        # Runs on paho's network thread; hub.submit trampolines into the loop.
        if isinstance(msg.value, str):
            hub.submit(msg.topic, msg.value)

    # str_raw hands us the undecoded payload text; JSON detection lives in the hub.
    client.set_on_msg_callback(_on_message, rettype="str_raw")
    return client


@app.command()
def serve(
    listen_host: str = typer.Option("0.0.0.0", "--listen-host", envvar="MQTTWEB_LISTEN_HOST", help="bind address"),
    listen_port: int = typer.Option(8080, "--listen-port", envvar="MQTTWEB_LISTEN_PORT", help="bind port"),
    limit_concurrency: int = typer.Option(
        0,
        "--limit-concurrency",
        envvar="MQTTWEB_LIMIT_CONCURRENCY",
        help="reject new connections with 503 above N concurrent ones (0 = unlimited); every open browser tab"
        " holds one SSE stream, a page load briefly needs several slots — size generously (100+), not per-user",
    ),
    mapper: str = typer.Option(
        "",
        "--mapper",
        envvar="MQTTWEB_MAPPER",
        help="mapper plugin: .py file path or dotted module name ('' = generic JSON view)",
    ),
    topics: str = typer.Option(
        "#",
        "--topics",
        envvar="MQTTWEB_TOPICS",
        help="comma-separated subscribe patterns for the generic view (ignored with --mapper)",
    ),
    title: str = typer.Option("", "--title", envvar="MQTTWEB_TITLE", help="page title override ('' = plugin's TITLE)"),
    item_ttl: float = typer.Option(
        900.0,
        "--item-ttl",
        envvar="MQTTWEB_ITEM_TTL",
        help="generic view: seconds until an unrefreshed card disappears (0 = keep forever)",
    ),
    mqtt_host: str = typer.Option(
        "mosquitto.mosquitto.svc.cluster.local", "--mqtt-host", envvar="MQTTWEB_MQTT_HOST", help="MQTT broker host"
    ),
    mqtt_port: int = typer.Option(1883, "--mqtt-port", envvar="MQTTWEB_MQTT_PORT", help="MQTT broker port"),
    mqtt_username: str = typer.Option(
        "", "--mqtt-user", envvar="MQTTWEB_MQTT_USER", help="MQTT username ('' = anonymous)"
    ),
    mqtt_password: str = typer.Option("", "--mqtt-password", envvar="MQTTWEB_MQTT_PASSWORD", help="MQTT password"),
    mqtt_tls: bool = typer.Option(
        False,
        "--mqtt-tls",
        envvar="MQTTWEB_MQTT_TLS",
        help="encrypt the MQTT connection with TLS (brokers usually listen on 8883 then — set --mqtt-port)",
    ),
    mqtt_tls_ca: str = typer.Option(
        "", "--mqtt-tls-ca", envvar="MQTTWEB_MQTT_TLS_CA", help="CA certificate path ('' = system CA store)"
    ),
    mqtt_tls_cert: str = typer.Option(
        "", "--mqtt-tls-cert", envvar="MQTTWEB_MQTT_TLS_CERT", help="client certificate path (mutual TLS)"
    ),
    mqtt_tls_key: str = typer.Option(
        "", "--mqtt-tls-key", envvar="MQTTWEB_MQTT_TLS_KEY", help="client key path (mutual TLS)"
    ),
    mqtt_tls_insecure: bool = typer.Option(
        False,
        "--mqtt-tls-insecure",
        envvar="MQTTWEB_MQTT_TLS_INSECURE",
        help="skip TLS hostname verification (self-signed certs) — encrypted but MITM-able",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", envvar="MQTTWEB_VERBOSE", help="DEBUG logging"),
) -> None:
    """Serve the live MQTT board.

    \f
    Everything below the ``\\f`` is hidden from ``--help`` (click convention).

    Raises:
        typer.Exit: On an invalid mapper plugin (exit 1).
    """
    configure_logging(verbose=verbose)
    print_banner()

    try:
        if mapper:
            plugin = load_plugin(mapper)
        else:
            patterns = [t.strip() for t in topics.split(",") if t.strip()]
            plugin = generic_plugin(patterns, ttl=item_ttl if item_ttl > 0 else None)
    except ValueError as exc:
        logger.error(f"{exc}")
        raise typer.Exit(code=1)
    if title:
        plugin.title = title

    env = build_environment(plugin)
    hub = ViewHub(env, plugin)
    client = _build_mqtt_client(
        plugin,
        hub,
        host=mqtt_host,
        port=mqtt_port,
        username=mqtt_username,
        password=mqtt_password,
        tls=mqtt_tls,
        tls_ca=mqtt_tls_ca,
        tls_cert=mqtt_tls_cert,
        tls_key=mqtt_tls_key,
        tls_insecure=mqtt_tls_insecure,
    )

    async def _mqtt_start() -> None:
        logger.info(f"MQTT connecting to host={mqtt_host} port={mqtt_port} topics={list(plugin.subscriptions)}")

        def _connect() -> None:
            try:
                if client.wait_for_connect_and_start_loop():
                    logger.info(f"MQTT connected to host={mqtt_host} port={mqtt_port}")
                else:
                    logger.warning("MQTT not connected within timeout — paho keeps retrying in background")
            except Exception as exc:
                logger.warning(f"MQTT connect failed: {type(exc).__name__}: {exc} — serving without a broker")

        await asyncio.to_thread(_connect)

    async def _mqtt_stop() -> None:
        try:
            await asyncio.to_thread(client.disconnect)
        except Exception:
            logger.exception("MQTT disconnect failed")

    web = create_app(hub, env, is_connected=client.is_connected, on_startup=_mqtt_start, on_shutdown=_mqtt_stop)
    logger.info(f"serving on http://{listen_host}:{listen_port}/ (title={plugin.title!r})")
    # log_config=None keeps uvicorn on the root logger, which configure_logging
    # already intercepts into loguru. uvicorn handles SIGTERM/SIGINT itself.
    # timeout_graceful_shutdown: open SSE streams never end on their own, so
    # without a limit uvicorn would wait forever for connected browsers on
    # shutdown; force-closing them is safe (clients auto-reconnect, retry: 3000).
    uvicorn.run(
        web,
        host=listen_host,
        port=listen_port,
        log_config=None,
        access_log=verbose,
        timeout_graceful_shutdown=3,
        limit_concurrency=limit_concurrency if limit_concurrency > 0 else None,
    )


if __name__ == "__main__":
    app()

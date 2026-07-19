"""Tests for mqttwebstuff: plugin contract, hub cache/expiry/broadcast, web app."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mqttwebstuff.hub import ViewHub, anchor_slug, decode_payload
from mqttwebstuff.plugin_api import LoadedPlugin, MapResult, ViewEvent, generic_plugin, load_plugin
from mqttwebstuff.webapp import _sse_frame, build_environment, create_app


def _one(result: MapResult) -> ViewEvent:
    """Narrow a mapper result expected to be a single event."""
    assert isinstance(result, ViewEvent)
    return result


OEPNV_PLUGIN = Path(__file__).parents[1] / "mqttwebstuff" / "plugins" / "oepnv_view.py"

DEPARTURES_PAYLOAD = {
    "time": "2026-07-19T19:33:06",
    "line": "X95",
    "stop": "Ellerbek, Waldhofstraße",
    "direction": "S Hamburg Airport",
    "departures": [
        {
            "scheduled": "2026-07-19T20:32:00",
            "expected": "2026-07-19T20:32:00",
            "in_minutes": 58.9,
            "delay_s": None,
            "realtime": False,
            "stop_id": "540910",
        }
    ],
}

STATUS_PAYLOAD = {
    "time": "2026-07-19T19:33:06",
    "feed_timestamp": 1784482368,
    "feed_age_s": 18,
    "stale": False,
    "unchanged_cycles": 0,
    "any_realtime": True,
    "total_trip_updates": 4711,
    "lines_with_realtime": 3,
    "lines_total": 4,
}

LINE_PAYLOAD = {
    "line": "X95",
    "has_realtime": True,
    "updates": 5,
    "delay_values": 3,
    "delay_min_s": -30,
    "delay_max_s": 120,
    "delay_avg_s": 40.0,
    "static_trips": 12,
}


def test_anchor_slug() -> None:
    assert anchor_slug("Ellerbek, Waldhofstraße") == "ellerbek-waldhofstrasse"
    assert anchor_slug("nodered/hydrostatics/hydrostatic1") == "nodered-hydrostatics-hydrostatic1"
    assert anchor_slug("///") == "x"


def test_decode_payload() -> None:
    assert decode_payload(b'{"a": 1}') == {"a": 1}
    assert decode_payload('["x"]') == ["x"]
    assert decode_payload(b"{broken json") == "{broken json"
    assert decode_payload("plain text") == "plain text"


def test_load_plugin_rejects_missing_contract(tmp_path: Path) -> None:
    bad = tmp_path / "bad_plugin.py"
    bad.write_text("SUBSCRIPTIONS = []\n")
    with pytest.raises(ValueError):
        load_plugin(bad)
    with pytest.raises(ValueError):
        load_plugin(tmp_path / "does_not_exist.py")


def test_oepnv_plugin_contract() -> None:
    plugin = load_plugin(OEPNV_PLUGIN)
    assert plugin.subscriptions == ("oepnv/#",)
    assert list(plugin.panels) == ["departures", "status", "lines", "alerts"]
    assert plugin.template_dir is not None


def test_load_plugin_by_dotted_module_name() -> None:
    plugin = load_plugin("mqttwebstuff.plugins.oepnv_view")
    assert plugin.title == "ÖPNV Live"
    # Template dir anchors at the module's own directory, not the CWD.
    assert plugin.template_dir == OEPNV_PLUGIN.parent / "templates"
    with pytest.raises(ValueError):
        load_plugin("mqttwebstuff.does_not_exist")


def test_load_plugin_relative_template_dir_anchors_at_plugin(tmp_path: Path) -> None:
    (tmp_path / "tpl").mkdir()
    plugin_file = tmp_path / "rel_tpl_plugin.py"
    plugin_file.write_text(
        "TEMPLATE_DIR = 'tpl'\n" "SUBSCRIPTIONS = ['x/#']\n" "def map_message(topic, payload):\n" "    return None\n"
    )
    plugin = load_plugin(plugin_file)
    assert plugin.template_dir == tmp_path / "tpl"
    assert plugin.title == "rel_tpl_plugin"


def test_oepnv_plugin_mapping() -> None:
    plugin = load_plugin(OEPNV_PLUGIN)

    ev = _one(plugin.map_message("oepnv/departures/X95/Ellerbek_Waldhofstrasse/S_Hamburg_Airport", DEPARTURES_PAYLOAD))
    assert (ev.panel, ev.template) == ("departures", "oepnv_departures.html.j2")
    assert ev.key == "X95/Ellerbek_Waldhofstrasse/S_Hamburg_Airport"
    assert ev.sort.startswith("2026-07-19T20:32:00")

    # The aggregate document and non-dict payloads are filtered.
    assert plugin.map_message("oepnv/departures", {"departures": []}) is None
    assert plugin.map_message("oepnv/status", "not a dict") is None
    assert plugin.map_message("oepnv/unknown/topic", {}) is None

    status = _one(plugin.map_message("oepnv/status", STATUS_PAYLOAD))
    assert (status.panel, status.key) == ("status", "summary")

    line = _one(plugin.map_message("oepnv/lines/X95/status", LINE_PAYLOAD))
    assert (line.panel, line.key) == ("lines", "X95")

    alert = _one(plugin.map_message("oepnv/alerts", {"entity": "444730", "text": "Umleitung"}))
    assert alert.panel == "alerts"
    # Content-addressed key: the same alert re-published replaces itself.
    alert2 = _one(plugin.map_message("oepnv/alerts", {"entity": "444730", "text": "Umleitung"}))
    assert alert2.key == alert.key


def test_oepnv_plugin_base_topic_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OEPNV_MQTT_BASE_TOPIC", "home/oepnv")
    plugin = load_plugin(OEPNV_PLUGIN)  # file-based load re-executes the module
    assert plugin.subscriptions == ("home/oepnv/#",)
    status = _one(plugin.map_message("home/oepnv/status", STATUS_PAYLOAD))
    assert status.panel == "status"
    # Topics outside the configured base are dropped.
    assert plugin.map_message("oepnv/status", STATUS_PAYLOAD) is None


def test_generic_plugin_flat_maps_topic_root_to_panel() -> None:
    plugin = generic_plugin(["ecowitt/#"], ttl=None, hierarchical=False)
    ev = _one(plugin.map_message("ecowitt/sensor/temp", {"t": 21.5}))
    assert (ev.panel, ev.key, ev.ttl) == ("ecowitt", "ecowitt/sensor/temp", None)


def test_generic_plugin_hierarchical_emits_branches_and_leaf() -> None:
    plugin = generic_plugin(["nodered/#"], ttl=None)  # hierarchical is the default
    events = plugin.map_message("nodered/hydrostatics/hydrostatic1/busvoltage", 24.19)
    assert isinstance(events, list) and len(events) == 3

    branches, leaf = events[:-1], events[-1]
    assert [(b.data["label"], b.data["depth"], b.data["branch"]) for b in branches] == [
        ("hydrostatics", 0, True),
        ("hydrostatic1", 1, True),
    ]
    assert (leaf.data["label"], leaf.data["depth"], leaf.data["branch"]) == ("busvoltage", 2, False)
    assert leaf.data["value"] == 24.19
    # Branch keys end in "/" so they sort before their children and never
    # collide with a leaf published on the same path.
    assert branches[0].key == "nodered/hydrostatics/"
    assert all(e.panel == "nodered" for e in events)

    # Two-segment topic: a single leaf, no branch rows.
    events = plugin.map_message("nodered/status", "OK")
    assert isinstance(events, list) and len(events) == 1 and events[0].data["depth"] == 0


def test_hub_renders_hierarchical_tree_in_order() -> None:
    plugin = generic_plugin(["nodered/#"], ttl=None)
    hub = ViewHub(build_environment(plugin), plugin)
    hub.ingest("nodered/hydrostatics/hydrostatic1/busvoltage", 24.19)
    hub.ingest("nodered/hydrostatics/hydrostatic1/status", {"lat": 53.6})

    body = hub.render_panel_body("nodered")
    # Branch rows appear once each and before their leaves (match the label
    # spans — the raw segment strings also occur inside data-tree-id paths).
    label = '<span class="mqttweb-tree-label">{}</span>'.format
    assert body.count(label("hydrostatics/")) == 1 and body.count(label("hydrostatic1/")) == 1
    order = [body.index(label(s)) for s in ("hydrostatics/", "hydrostatic1/", "busvoltage", "status")]
    assert order == sorted(order)
    # Scalar inline, JSON collapsible with a stable tree id for state restore.
    assert "<code>24.19</code>" in body
    assert 'data-tree-id="nodered/hydrostatics/hydrostatic1/status"' in body
    # Rows are deep-linkable: stable anchor id plus a self-link on the label.
    assert 'id="t-nodered-hydrostatics-hydrostatic1-busvoltage"' in body
    assert 'href="#t-nodered-hydrostatics-hydrostatic1-busvoltage"' in body


def _oepnv_hub() -> ViewHub:
    plugin = load_plugin(OEPNV_PLUGIN)
    return ViewHub(build_environment(plugin), plugin)


def test_hub_ingest_renders_and_replaces() -> None:
    hub = _oepnv_hub()
    topic = "oepnv/departures/X95/Ellerbek_Waldhofstrasse/S_Hamburg_Airport"
    hub.ingest(topic, DEPARTURES_PAYLOAD)
    board = hub.render_board()
    assert "X95" in board and "S Hamburg Airport" in board
    assert "20:32" in board  # hhmm filter applied to `expected`

    # Same (panel, key) replaces instead of appending.
    hub.ingest(topic, {**DEPARTURES_PAYLOAD, "direction": "Bf. Pinneberg"})
    body = hub.render_panel_body("departures")
    assert body.count("<article") == 1
    assert "Bf. Pinneberg" in body and "S Hamburg Airport" not in body


def test_hub_groups_departures_by_stop() -> None:
    hub = _oepnv_hub()
    moordamm = {**DEPARTURES_PAYLOAD, "stop": "Ellerbek, Moordamm"}
    hub.ingest("oepnv/departures/X95/Ellerbek_Waldhofstrasse/S_Hamburg_Airport", DEPARTURES_PAYLOAD)
    hub.ingest("oepnv/departures/X95/Ellerbek_Moordamm/S_Hamburg_Airport", moordamm)
    hub.ingest("oepnv/departures/295/Ellerbek_Moordamm/Bf_Pinneberg", {**moordamm, "line": "295"})

    body = hub.render_panel_body("departures")
    # One bordered box per stop, stops alphabetical, both Moordamm cards in one box.
    assert body.count("mqttweb-group-box") == 2
    assert body.index("Ellerbek, Moordamm") < body.index("Ellerbek, Waldhofstraße")
    moordamm_section = body[body.index("Ellerbek, Moordamm") : body.index("Ellerbek, Waldhofstraße")]
    assert moordamm_section.count("<article") == 2

    # Each stop box is deep-linkable: stable anchor id, heading is a self-link.
    assert 'id="g-departures-ellerbek-moordamm"' in body
    assert 'href="#g-departures-ellerbek-moordamm"' in body

    # The departures panel is "plain": no enclosing card and no "Abfahrten" title.
    board = hub.render_board()
    assert "Abfahrten" not in board
    assert 'class="mqttweb-panel mqttweb-panel-plain" id="panel-departures"' in board


def test_hub_ttl_expiry_sweeps_items() -> None:
    hub = _oepnv_hub()
    hub.ingest("oepnv/status", STATUS_PAYLOAD)
    assert "Trip-Updates" in hub.render_panel_body("status")
    changed = hub.sweep_once(now=1e12)  # far beyond any monotonic ttl
    assert changed == ["status"]
    assert "keine Daten" in hub.render_panel_body("status")


def test_hub_broadcasts_panel_and_board_events() -> None:
    hub = _oepnv_hub()
    queue = hub.subscribe()

    hub.ingest("oepnv/status", STATUS_PAYLOAD)
    event, fragment = queue.get_nowait()
    assert event == "panel:status" and "Trip-Updates" in fragment

    # A crashing template/mapper must not kill the stream; unknown topic → no event.
    hub.ingest("oepnv/unknown", {"x": 1})
    assert queue.qsize() == 0

    # An undeclared panel repaints the whole board once.
    generic = generic_plugin(["#"], ttl=None)
    ghub = ViewHub(build_environment(generic), generic)
    gqueue = ghub.subscribe()
    ghub.ingest("brandnew/topic", {"hello": "world"})
    event, fragment = gqueue.get_nowait()
    assert event == "board" and "brandnew" in fragment

    hub.unsubscribe(queue)
    ghub.unsubscribe(gqueue)


def test_hub_mapper_exception_is_contained() -> None:
    def _boom(topic: str, payload: object) -> ViewEvent | None:
        raise RuntimeError("mapper crash")

    plugin = LoadedPlugin(subscriptions=("#",), map_message=_boom)
    hub = ViewHub(build_environment(plugin), plugin)
    hub.ingest("any/topic", {})  # must not raise
    assert hub.render_board() == ""


def test_webapp_index_health_and_stream_headers() -> None:
    hub = _oepnv_hub()
    hub.ingest("oepnv/status", STATUS_PAYLOAD)
    app = create_app(hub, build_environment(hub.plugin), is_connected=lambda: False)

    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "ÖPNV Live" in index.text
        assert "Trip-Updates" in index.text  # cache is server-rendered into the page
        assert 'sse-connect="/stream"' in index.text

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"ok": True, "mqtt_connected": False}

        assert client.get("/static/htmx.min.js").status_code == 200


def test_sse_frame_format() -> None:
    # /stream itself is an infinite generator (hangs TestClient on close), so
    # the wire format is tested at the unit level instead.
    assert _sse_frame("panel:status", "<p>x</p>") == "event: panel:status\ndata: <p>x</p>\n\n"
    assert _sse_frame("board", "<a>\n<b>") == "event: board\ndata: <a>\ndata: <b>\n\n"
    assert _sse_frame("board", "") == "event: board\ndata: \n\n"

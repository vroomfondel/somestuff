#!/usr/bin/python
# Shebang deliberately /usr/bin/python (Ansible convention, not /usr/bin/env python3):
# Ansible derives the module interpreter from this line and only replaces it via
# interpreter discovery. With `env python3` it is taken literally and the run fails with
# "module interpreter not found".
"""Ansible module: manage Uptime-Kuma monitors idempotently."""

DOCUMENTATION = r"""
---
module: uptimekuma_monitor
short_description: Manages monitors in Uptime Kuma 2.x idempotently
description:
  - Creates monitors, reconciles them and deletes them, via the Socket.IO API.
  - References go by name (I(parent), I(notifications)), not by ID -
    IDs differ per instance and would not be declarative.
  - The actual state is read fresh from the server (C(getMonitorList)), not from a
    cache - that is the prerequisite for reliable C(changed) reporting.
requirements:
  - python-socketio
notes:
  - "The module talks to Kuma over the network and needs C(python-socketio). It therefore
    belongs on the control node - C(delegate_to: localhost) or a play with
    C(hosts: localhost). Nothing needs to be installed on the target hosts."
  - "C(python-socketio) lives only in the .venv of this repo, not in the system Python. The
    run therefore needs C(ansible_python_interpreter) pointing at C(.venv/bin/python) -
    otherwise the import fails. Either via C(-e ansible_python_interpreter=...) or permanently
    for localhost in the inventory."
  - "Uptime Kuma allows duplicate monitor names. Since I(name) is the idempotency key here,
    the module fails on ambiguity instead of silently hitting the wrong one."
  - "In C(--check) against an empty instance a task fails whose I(parent) or
    I(notifications) only a prior task of the same run would create - in check mode the
    reference doesn't come into existence. That is the usual check_mode limit with dependent
    resources. C(uptimekuma/uptimekuma_apply.py) doesn't have this problem because it knows
    the entire desired state."
  - "The server applies I(weight) only on creation. A later change is reported as
    C(not_applicable) and does not affect C(changed)."
options:
  url:
    description: Base URL of the Uptime-Kuma instance.
    required: true
    type: str
  username:
    description:
      - Username.
      - The socket login accepts no API keys; those apply only to C(/metrics).
    required: true
    type: str
  password:
    description: Password.
    required: true
    type: str
  name:
    description:
      - Display name, serves as the idempotency key.
      - Mutually exclusive with I(monitors); exactly one of the two is required.
    type: str
  monitors:
    description:
      - Bulk mode - a list of monitors, each a dict with the same options this module
        takes for a single monitor.
      - "Exists because of the server side: Kuma rate-limits logins to 20 per minute
        (C(loginRateLimiter) in C(server/rate-limiter.js)), and every module invocation
        opens its own Socket.IO session. A C(loop) over more than ~20 monitors therefore
        runs into C(login: no response within 30s) - regardless of how fast the machine is.
        Bulk mode logs in ONCE and reconciles all entries in that session."
      - Entries are ordered by their I(parent) reference before writing, so a group is
        created before the monitors pointing at it.
    type: list
    elements: dict
  defaults:
    description:
      - Bulk mode only - values merged UNDER every entry, so an entry always wins.
      - Keeps fleet-wide identical settings (interval, timeout, ...) in one place instead
        of repeating them per monitor.
      - "Deliberately NOT applied to C(type=group): timing has no meaning for a group, and
        Kuma leaves whatever the UI form happened to hold there - merging would rewrite
        those values on the first run."
    type: dict
  state:
    description: Whether the monitor should exist.
    type: str
    choices: [present, absent]
    default: present
  type:
    description: Monitor type. Required on creation.
    type: str
  active:
    description:
      - Whether the monitor should run.
      - Runs server-side via pause/resume, not via the edit path.
    type: bool
  parent:
    description: Name of the parent group (a monitor with C(type=group)).
    type: str
  notifications:
    description: Names of the notification providers to link.
    type: list
    elements: str
  description:
    description: Free-text description.
    type: str
  interval:
    description: Check interval in seconds.
    type: int
  retry_interval:
    description: Wait time between retries in seconds.
    type: int
  resend_interval:
    description: Re-notify every X checks (0 = off).
    type: int
  max_retries:
    description: Retries before the monitor is considered DOWN.
    type: int
  timeout:
    description: Timeout of a check in seconds.
    type: int
  upside_down:
    description: Inverted logic - reachable counts as DOWN.
    type: bool
  url_target:
    description: Target URL for HTTP-like monitors (Kuma field C(url)).
    type: str
  hostname:
    description: Hostname for ping/port/mqtt/db monitors.
    type: str
  port:
    description: Port for port/mqtt/db monitors.
    type: int
  keyword:
    description: Keyword for C(keyword) monitors.
    type: str
  invert_keyword:
    description: The keyword must NOT be present.
    type: bool
  json_path:
    description: JSONata expression for C(json-query)/mqtt-json-query.
    type: str
  expected_value:
    description: Expected value of the JSON-path result.
    type: str
  mqtt_topic:
    description: MQTT topic.
    type: str
  mqtt_username:
    description: MQTT username.
    type: str
  mqtt_password:
    description: MQTT password.
    type: str
  mqtt_check_type:
    description: How the MQTT message is checked.
    type: str
    choices: [keyword, json-query, none]
  mqtt_success_message:
    description: Message that counts as success (with C(mqtt_check_type=keyword)).
    type: str
  database_connection_string:
    description: Connection string for DB monitors.
    type: str
  database_query:
    description: Query for DB monitors.
    type: str
  accepted_statuscodes:
    description: HTTP status codes that count as success.
    type: list
    elements: str
  extra:
    description:
      - Any further Kuma fields in original spelling (camelCase), e.g.
        C(jsonPathOperator) or C(packetSize).
      - Escape hatch for the roughly 100 fields not mapped individually here.
    type: dict
author:
  - Henning Thieß
"""

EXAMPLES = r"""
- name: Provision Uptime-Kuma monitors
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Create group
      uptimekuma_monitor:
        url: https://uptimekuma.heidk8.elasticc.io
        username: "{{ uptimekuma_admin_user }}"
        password: "{{ uptimekuma_admin_password }}"
        name: outdoormesh
        type: group

    - name: MQTT check in the group
      uptimekuma_monitor:
        url: https://uptimekuma.heidk8.elasticc.io
        username: "{{ uptimekuma_admin_user }}"
        password: "{{ uptimekuma_admin_password }}"
        name: husqvarna/automower/pongs
        type: mqtt
        parent: outdoormesh
        hostname: mosquitto.mosquitto.svc.cluster.local
        port: 1883
        mqtt_topic: husqvarna/automower/pongs
        mqtt_username: uptimekuma
        mqtt_password: "{{ mqtt_uptimekuma_password }}"
        mqtt_check_type: json-query
        json_path: "($millis()-$toMillis(created_at)) < 900000"
        expected_value: "true"
        notifications:
          - My Gotify Alarm (1)
        active: true

# From a play that runs against real hosts: delegate to the control node.
- name: Monitor for this host
  uptimekuma_monitor:
    url: https://uptimekuma.heidk8.elasticc.io
    username: "{{ uptimekuma_admin_user }}"
    password: "{{ uptimekuma_admin_password }}"
    name: "ping-{{ inventory_hostname }}"
    type: ping
    hostname: "{{ ansible_default_ipv4.address }}"
  delegate_to: localhost

- name: Remove monitor
  uptimekuma_monitor:
    url: https://uptimekuma.heidk8.elasticc.io
    username: "{{ uptimekuma_admin_user }}"
    password: "{{ uptimekuma_admin_password }}"
    name: old-check
    state: absent
"""

RETURN = r"""
monitor_id:
  description: ID of the created or found monitor.
  returned: when the monitor exists
  type: int
  sample: 42
created:
  description: Whether the monitor was newly created in this run.
  returned: always
  type: bool
  sample: false
diff:
  description: "Differences as C({field: {before, after}})."
  returned: on changes
  type: dict
  sample: {"interval": {"before": 60, "after": 300}}
results:
  description:
    - Per-monitor outcome in bulk mode, in the order they were written.
    - Each entry carries C(name), C(changed), C(created), C(monitor_id) and C(diff).
  returned: in bulk mode
  type: list
  elements: dict
  sample: [{"name": "outdoormesh", "changed": false, "created": false, "monitor_id": 44}]
not_applicable:
  description:
    - Differences in fields the server no longer applies after creation.
    - Deliberately not counted in C(changed), otherwise every run would report a change.
  returned: when such fields differ
  type: dict
  sample: {"weight": {"before": 2000, "after": 999}}
"""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.uptimekuma_client import KumaClient, KumaError

# Ansible option -> Kuma field name. Kuma mixes camelCase and snake_case; the module options
# stay snake_case throughout.
OPTION_TO_KUMA: dict[str, str] = {
    "name": "name",
    "type": "type",
    "active": "active",
    "parent": "parent",
    "notifications": "notifications",
    "description": "description",
    "interval": "interval",
    "retry_interval": "retryInterval",
    "resend_interval": "resendInterval",
    "max_retries": "maxretries",
    "timeout": "timeout",
    "upside_down": "upsideDown",
    "url_target": "url",
    "hostname": "hostname",
    "port": "port",
    "keyword": "keyword",
    "invert_keyword": "invertKeyword",
    "json_path": "jsonPath",
    "expected_value": "expectedValue",
    "mqtt_topic": "mqttTopic",
    "mqtt_username": "mqttUsername",
    "mqtt_password": "mqttPassword",
    "mqtt_check_type": "mqttCheckType",
    "mqtt_success_message": "mqttSuccessMessage",
    "database_connection_string": "databaseConnectionString",
    "database_query": "databaseQuery",
    "accepted_statuscodes": "accepted_statuscodes",
}

# On creation the server requires these fields. `conditions` is NOT NULL and does have a
# DB default, but an explicit NULL beats it - that is exactly what uptime-kuma-api fails on.
# When reconciling an existing monitor the defaults are NOT applied, otherwise the module
# would overwrite foreign settings unasked.
CREATE_DEFAULTS: dict[str, object] = {
    "conditions": [],
    "accepted_statuscodes": ["200-299"],
    "interval": 60,
    "retryInterval": 60,
    "resendInterval": 0,
    "maxretries": 0,
    "timeout": 48,
    "upsideDown": False,
    "expiryNotification": False,
    "ignoreTls": False,
    "notificationIDList": [],
}


def build_desired(params: dict[str, object]) -> dict[str, object]:
    """Translates the module parameters into a Kuma desired state.

    Only explicitly set options go in: an unspecified parameter means "don't care", not "set
    to None" - otherwise the module would flatten existing values.

    Args:
        params: The module parameters from AnsibleModule.

    Returns:
        The desired state in Kuma field names.
    """
    desired: dict[str, object] = {}
    for option, kuma_field in OPTION_TO_KUMA.items():
        value = params.get(option)
        if value is not None:
            desired[kuma_field] = value

    extra = params.get("extra")
    if isinstance(extra, dict):
        desired.update(extra)
    return desired


def params_type_missing(params: dict[str, object]) -> bool:
    """Whether the single-monitor path was called without ``type``.

    Args:
        params: The module parameters.

    Returns:
        True if ``type`` is required but absent.
    """
    return bool(params.get("name")) and params.get("state") == "present" and not params.get("type")


def order_by_parent(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """Sorts monitors so that a group comes before the monitors referencing it.

    ``parent`` is a NAME, and the server can only resolve it once the group exists. A
    parent that is not part of this batch is assumed to already exist server-side.

    Args:
        entries: The declared monitors.

    Returns:
        The same entries, reordered.

    Raises:
        KumaError: If the parent references form a cycle.
    """
    declared = {str(e.get("name")) for e in entries}
    pending = {str(e.get("name")): e for e in entries}
    ordered: list[dict[str, object]] = []
    emitted: set[str] = set()

    while pending:
        wave = [
            name
            for name, e in pending.items()
            if not e.get("parent") or str(e["parent"]) not in declared or str(e["parent"]) in emitted
        ]
        if not wave:
            raise KumaError("parent cycle among: " + ", ".join(sorted(pending)))
        for name in wave:
            ordered.append(pending.pop(name))
            emitted.add(name)
    return ordered


def upsert_many(
    client: KumaClient,
    entries: list[dict[str, object]],
    defaults: dict[str, object] | None,
    check_mode: bool,
) -> tuple[bool, list[dict[str, object]]]:
    """Reconciles a whole list of monitors within one already-open session.

    Args:
        client: A logged-in client.
        entries: The declared monitors, each with this module's option names.
        defaults: Values merged under every non-group entry; the entry wins.
        check_mode: If True, nothing is written.

    Returns:
        ``(changed, results)`` - whether anything changed, plus the per-monitor outcome.

    Raises:
        KumaError: If an entry has no ``name`` or ``type``.
    """
    changed = False
    results: list[dict[str, object]] = []

    for entry in order_by_parent(entries):
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise KumaError("every entry in 'monitors' needs a 'name'")
        if not entry.get("type"):
            raise KumaError(f"monitor {name!r} has no 'type'")

        # Groups stay unmerged on purpose - see the `defaults` option docs.
        merged = entry if entry.get("type") == "group" else {**(defaults or {}), **entry}
        desired = build_desired(merged)
        # Defaults only when creating - on an existing monitor they would overwrite
        # settings the caller never mentioned.
        if client.monitor_by_name(name) is None:
            desired = {**CREATE_DEFAULTS, **desired}

        result = client.upsert_monitor(desired, check_mode=check_mode)
        changed = changed or result["changed"]
        entry_result: dict[str, object] = {
            "name": name,
            "changed": result["changed"],
            "created": result["created"],
            "monitor_id": result["object_id"],
            "diff": result["diff"],
        }
        if result.get("not_applicable"):
            entry_result["not_applicable"] = result["not_applicable"]
        results.append(entry_result)

    return changed, results


def run_module() -> None:
    """Runs the module and terminates it with exit_json/fail_json."""
    module = AnsibleModule(
        argument_spec={
            "url": {"type": "str", "required": True},
            "username": {"type": "str", "required": True},
            "password": {"type": "str", "required": True, "no_log": True},
            "name": {"type": "str"},
            "monitors": {"type": "list", "elements": "dict"},
            "defaults": {"type": "dict"},
            "state": {"type": "str", "choices": ["present", "absent"], "default": "present"},
            "type": {"type": "str"},
            "active": {"type": "bool"},
            "parent": {"type": "str"},
            "notifications": {"type": "list", "elements": "str"},
            "description": {"type": "str"},
            "interval": {"type": "int"},
            "retry_interval": {"type": "int"},
            "resend_interval": {"type": "int"},
            "max_retries": {"type": "int"},
            "timeout": {"type": "int"},
            "upside_down": {"type": "bool"},
            "url_target": {"type": "str"},
            "hostname": {"type": "str"},
            "port": {"type": "int"},
            "keyword": {"type": "str"},
            "invert_keyword": {"type": "bool"},
            "json_path": {"type": "str"},
            "expected_value": {"type": "str"},
            "mqtt_topic": {"type": "str"},
            "mqtt_username": {"type": "str"},
            "mqtt_password": {"type": "str", "no_log": True},
            "mqtt_check_type": {"type": "str", "choices": ["keyword", "json-query", "none"]},
            "mqtt_success_message": {"type": "str"},
            "database_connection_string": {"type": "str", "no_log": True},
            "database_query": {"type": "str"},
            "accepted_statuscodes": {"type": "list", "elements": "str"},
            "extra": {"type": "dict"},
        },
        supports_check_mode=True,
        required_one_of=[("name", "monitors")],
        mutually_exclusive=[("name", "monitors")],
    )
    # `required_if` cannot express "only when `name` is set" - in bulk mode `type` sits on
    # the individual entry, so the check happens there (upsert_many).
    if params_type_missing(module.params):
        module.fail_json(msg="state=present with 'name' requires 'type'")

    params = module.params
    client = KumaClient(params["url"])

    try:
        client.connect()
        client.login(params["username"], params["password"])

        if params["monitors"] is not None:
            if params["state"] == "absent":
                module.fail_json(msg="state=absent is not supported in bulk mode")
            changed, results = upsert_many(client, params["monitors"], params["defaults"], module.check_mode)
            module.exit_json(changed=changed, results=results)

        if params["state"] == "absent":
            existing = client.monitor_by_name(params["name"])
            if existing is None:
                module.exit_json(changed=False, created=False, diff={})
            if not module.check_mode:
                client.delete_monitor(existing["id"])
            module.exit_json(changed=True, created=False, monitor_id=existing["id"], diff={})

        desired = build_desired(params)

        # Defaults only for the create case - when reconciling they would overwrite foreign
        # settings the caller didn't even mention.
        if client.monitor_by_name(params["name"]) is None:
            desired = {**CREATE_DEFAULTS, **desired}

        result = client.upsert_monitor(desired, check_mode=module.check_mode)

        module.exit_json(
            changed=result["changed"],
            created=result["created"],
            monitor_id=result["object_id"],
            diff=result["diff"],
            not_applicable=result.get("not_applicable", {}),
        )

    except KumaError as exc:
        module.fail_json(msg=str(exc))
    except Exception as exc:
        module.fail_json(msg=f"{type(exc).__name__}: {exc}")
    finally:
        client.close()


def main() -> None:
    """Entry point."""
    run_module()


if __name__ == "__main__":
    main()

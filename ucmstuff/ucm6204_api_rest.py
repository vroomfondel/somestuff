#!/usr/bin/env python3
"""Complete named-method wrapper for the Grandstream UCM6204 HTTPS API.

This is a sister module to :mod:`ucm6204_api`. It defines :class:`UCM6204Rest`,
a subclass of :class:`ucm6204_api.UCM6204` that adds a named, typed, documented
method for **every** action of the UCM6xxx HTTPS API Guide — trunks (SIP, analog,
digital, SLA), inbound/outbound routes, IVRs, queues (and agents), paging groups,
SIP accounts, users, extension/PIN groups, channel and call control, dialing and
transfers, plus the legacy ``cdrapi``/``recapi``/``pmsapi``/``queueapi``.

Nothing here adds capability over the base client's generic
:meth:`ucm6204_api.UCM6204.api_call` — every method is a thin, self-documenting
wrapper around it. The value is discoverability (exact action-name casing such as
``Hangup`` or ``MulticastPaging`` is encoded in the method names), IDE completion,
docstrings and consistent response unwrapping.

Two conventions:

* Every method returns the reply's ``response`` object (a :data:`JSONObject`) via
  :meth:`UCM6204Rest._call`, not the full ``{"status": ..., "response": ...}``
  envelope — the status is validated and raised on by the base client.
* ``list*`` methods that have a well-known item key (verified against the live
  device) unwrap it and return a ``list`` via :meth:`UCM6204Rest._list`; the key
  is named in each docstring. Others return the ``response`` object.

Most create/update/delete/get actions take entity-specific parameters that differ
per action; they are accepted via ``**params`` (all string-valued, as the API
expects) and the important ones are named in the docstrings.

Dependencies:
    Same as :mod:`ucm6204_api` (``requests``).

Example:
    >>> from ucmstuff.ucm6204_api_rest import UCM6204Rest
    >>> ucm = UCM6204Rest(host="ucm.example", api_user="cdrapi", api_password="…")
    >>> ucm.connect()
    >>> [t["trunk_name"] for t in ucm.list_voip_trunks()]      # doctest: +SKIP
    ['trunk1', 'trunk2', 'trunk3', …]
    >>> ucm.get_sip_trunk(trunk="1")["status"]                 # doctest: +SKIP
    'Registered'
"""

from ucmstuff.ucm6204_api import UCM6204, JSONObject, JSONValue, UCMAPIError

__all__ = ["UCM6204Rest", "UCMAPIError"]


class UCM6204Rest(UCM6204):
    """Full named-method HTTPS-API client for the UCM6204.

    Inherits challenge/response authentication, cookie handling, the weak-DH TLS
    workaround and the generic :meth:`~ucm6204_api.UCM6204.api_call` from
    :class:`ucm6204_api.UCM6204`, and exposes every documented API action as a
    typed, documented method.
    """

    # ── low-level helpers ────────────────────────────────────────────────────

    def _call(self, action: str, **params: str) -> JSONObject:
        """Run ``action`` and return its ``response`` object.

        Args:
            action: The API action name (exact casing).
            **params: Action parameters (string-valued).

        Returns:
            JSONObject: The ``response`` object of the reply (``{}`` if absent).

        Raises:
            UCMAPIError: If the reply ``status`` is non-zero.
        """
        response = self.api_call(action, **params).get("response")
        return response if isinstance(response, dict) else {}

    def _list(self, action: str, item_key: str, **params: str) -> list[JSONValue]:
        """Run a ``list*`` ``action`` and unwrap its item list.

        Args:
            action: The API action name.
            item_key: The key in ``response`` holding the item list (e.g.
                ``"voip_trunk"``).
            **params: Action parameters (e.g. ``page``, ``item_num``, ``options``).

        Returns:
            list[JSONValue]: The list of items (empty if the key is absent).
        """
        value = self._call(action, **params).get(item_key)
        return value if isinstance(value, list) else []

    # ── System ───────────────────────────────────────────────────────────────

    def apply_changes(self, **params: str) -> JSONObject:
        """Apply pending configuration changes (``applyChanges``)."""
        return self._call("applyChanges", **params)

    def play_prompt_by_org(self, **params: str) -> JSONObject:
        """Play/record a custom prompt (``playPromptByOrg``)."""
        return self._call("playPromptByOrg", **params)

    # ``getSystemStatus`` / ``getSystemGeneralStatus`` are on the base client.

    # ── SIP accounts / extensions ────────────────────────────────────────────

    def get_sip_account(self, **params: str) -> JSONObject:
        """Get a SIP account/extension (``getSIPAccount``). Param: ``extension``."""
        return self._call("getSIPAccount", **params)

    def update_sip_account(self, **params: str) -> JSONObject:
        """Update a SIP account/extension (``updateSIPAccount``). Param: ``extension``."""
        return self._call("updateSIPAccount", **params)

    def list_users(self, **params: str) -> list[JSONValue]:
        """List user-portal users (``listUser``). Item key: ``user_id``."""
        return self._list("listUser", "user_id", **params)

    def get_user(self, **params: str) -> JSONObject:
        """Get a user-portal user (``getUser``). Param: ``user_name``."""
        return self._call("getUser", **params)

    def update_user(self, **params: str) -> JSONObject:
        """Update a user-portal user (``updateUser``). Param: ``user_name``."""
        return self._call("updateUser", **params)

    def list_extension_groups(self, **params: str) -> list[JSONValue]:
        """List extension groups (``listExtensionGroup``). Item key: ``extension_group``."""
        return self._list("listExtensionGroup", "extension_group", **params)

    def list_pin_sets(self, **params: str) -> list[JSONValue]:
        """List PIN sets (``listPinSets``). Item key: ``pin_sets_id``."""
        return self._list("listPinSets", "pin_sets_id", **params)

    # ── VoIP / SIP trunks ────────────────────────────────────────────────────

    def list_voip_trunks(self, **params: str) -> list[JSONValue]:
        """List VoIP (SIP) trunks (``listVoIPTrunk``). Item key: ``voip_trunk``.

        Common params: ``page``, ``item_num``, ``options``.
        """
        return self._list("listVoIPTrunk", "voip_trunk", **params)

    def get_sip_trunk(self, **params: str) -> JSONObject:
        """Get a SIP trunk incl. live status (``getSIPTrunk``). Param: ``trunk`` (index).

        The response ``trunk`` object includes ``status`` (Registered /
        Unregistered / Rejected …) and ``out_of_service``.
        """
        response = self._call("getSIPTrunk", **params)
        inner = response.get("trunk")
        return inner if isinstance(inner, dict) else response

    def add_sip_trunk(self, **params: str) -> JSONObject:
        """Create a SIP trunk (``addSIPTrunk``)."""
        return self._call("addSIPTrunk", **params)

    def update_sip_trunk(self, **params: str) -> JSONObject:
        """Update a SIP trunk (``updateSIPTrunk``). Param: ``trunk`` (index)."""
        return self._call("updateSIPTrunk", **params)

    def delete_sip_trunk(self, **params: str) -> JSONObject:
        """Delete a SIP trunk (``deleteSIPTrunk``). Param: ``trunk`` (index)."""
        return self._call("deleteSIPTrunk", **params)

    # ── Analog trunks ────────────────────────────────────────────────────────

    def list_analog_trunks(self, **params: str) -> list[JSONValue]:
        """List analog (FXO) trunks (``listAnalogTrunk``). Item key: ``analogtrunk``."""
        return self._list("listAnalogTrunk", "analogtrunk", **params)

    def get_analog_trunk(self, **params: str) -> JSONObject:
        """Get an analog trunk (``getAnalogTrunk``). Param: ``trunk`` (index)."""
        return self._call("getAnalogTrunk", **params)

    def add_analog_trunk(self, **params: str) -> JSONObject:
        """Create an analog trunk (``addAnalogTrunk``)."""
        return self._call("addAnalogTrunk", **params)

    def update_analog_trunk(self, **params: str) -> JSONObject:
        """Update an analog trunk (``updateAnalogTrunk``). Param: ``trunk`` (index)."""
        return self._call("updateAnalogTrunk", **params)

    def delete_analog_trunk(self, **params: str) -> JSONObject:
        """Delete an analog trunk (``deleteAnalogTrunk``). Param: ``trunk`` (index)."""
        return self._call("deleteAnalogTrunk", **params)

    # ── Digital trunks ───────────────────────────────────────────────────────

    def list_digital_trunks(self, **params: str) -> list[JSONValue]:
        """List digital (E1/T1/BRI) trunks (``listDigitalTrunk``). Item key: ``digital_trunks``."""
        return self._list("listDigitalTrunk", "digital_trunks", **params)

    def get_digital_trunk(self, **params: str) -> JSONObject:
        """Get a digital trunk (``getDigitalTrunk``). Param: ``trunk`` (index)."""
        return self._call("getDigitalTrunk", **params)

    def add_digital_trunk(self, **params: str) -> JSONObject:
        """Create a digital trunk (``addDigitalTrunk``)."""
        return self._call("addDigitalTrunk", **params)

    def update_digital_trunk(self, **params: str) -> JSONObject:
        """Update a digital trunk (``updateDigitalTrunk``). Param: ``trunk`` (index)."""
        return self._call("updateDigitalTrunk", **params)

    def delete_digital_trunk(self, **params: str) -> JSONObject:
        """Delete a digital trunk (``deleteDigitalTrunk``). Param: ``trunk`` (index)."""
        return self._call("deleteDigitalTrunk", **params)

    # ── SLA trunks ───────────────────────────────────────────────────────────

    def add_sla_trunk(self, **params: str) -> JSONObject:
        """Create an SLA (Shared Line Appearance) trunk (``addSLATrunk``)."""
        return self._call("addSLATrunk", **params)

    def update_sla_trunk(self, **params: str) -> JSONObject:
        """Update an SLA trunk (``updateSLATrunk``)."""
        return self._call("updateSLATrunk", **params)

    # ── Inbound routes ───────────────────────────────────────────────────────

    def list_inbound_routes(self, **params: str) -> list[JSONValue]:
        """List inbound routes (``listInboundRoute``). Item key: ``inbound_route``."""
        return self._list("listInboundRoute", "inbound_route", **params)

    def get_inbound_route(self, **params: str) -> JSONObject:
        """Get an inbound route (``getInboundRoute``). Param: ``inbound_route`` (id)."""
        return self._call("getInboundRoute", **params)

    def add_inbound_route(self, **params: str) -> JSONObject:
        """Create an inbound route (``addInboundRoute``)."""
        return self._call("addInboundRoute", **params)

    def update_inbound_route(self, **params: str) -> JSONObject:
        """Update an inbound route (``updateInboundRoute``). Param: ``inbound_route`` (id)."""
        return self._call("updateInboundRoute", **params)

    def delete_inbound_route(self, **params: str) -> JSONObject:
        """Delete an inbound route (``deleteInboundRoute``). Param: ``inbound_route`` (id)."""
        return self._call("deleteInboundRoute", **params)

    # ── Outbound routes ──────────────────────────────────────────────────────

    def list_outbound_routes(self, **params: str) -> list[JSONValue]:
        """List outbound routes (``listOutboundRoute``). Item key: ``outbound_route``."""
        return self._list("listOutboundRoute", "outbound_route", **params)

    def get_outbound_route(self, **params: str) -> JSONObject:
        """Get an outbound route (``getOutboundRoute``). Param: ``outbound_route`` (id)."""
        return self._call("getOutboundRoute", **params)

    def add_outbound_route(self, **params: str) -> JSONObject:
        """Create an outbound route (``addOutboundRoute``)."""
        return self._call("addOutboundRoute", **params)

    def update_outbound_route(self, **params: str) -> JSONObject:
        """Update an outbound route (``updateOutboundRoute``). Param: ``outbound_route`` (id)."""
        return self._call("updateOutboundRoute", **params)

    def delete_outbound_route(self, **params: str) -> JSONObject:
        """Delete an outbound route (``deleteOutboundRoute``). Param: ``outbound_route`` (id)."""
        return self._call("deleteOutboundRoute", **params)

    # ── IVR ──────────────────────────────────────────────────────────────────

    def list_ivrs(self, **params: str) -> list[JSONValue]:
        """List IVRs (``listIVR``). Item key: ``ivr``."""
        return self._list("listIVR", "ivr", **params)

    def get_ivr(self, **params: str) -> JSONObject:
        """Get an IVR (``getIVR``). Param: ``ivr`` (id)."""
        return self._call("getIVR", **params)

    def add_ivr(self, **params: str) -> JSONObject:
        """Create an IVR (``addIVR``)."""
        return self._call("addIVR", **params)

    def update_ivr(self, **params: str) -> JSONObject:
        """Update an IVR (``updateIVR``). Param: ``ivr`` (id)."""
        return self._call("updateIVR", **params)

    def delete_ivr(self, **params: str) -> JSONObject:
        """Delete an IVR (``deleteIVR``). Param: ``ivr`` (id)."""
        return self._call("deleteIVR", **params)

    # ── Queues ───────────────────────────────────────────────────────────────

    def list_queues(self, **params: str) -> list[JSONValue]:
        """List call queues (``listQueue``). Item key: ``queue``."""
        return self._list("listQueue", "queue", **params)

    def get_queue(self, **params: str) -> JSONObject:
        """Get a call queue (``getQueue``). Param: ``queue`` (extension)."""
        return self._call("getQueue", **params)

    def add_queue(self, **params: str) -> JSONObject:
        """Create a call queue (``addQueue``)."""
        return self._call("addQueue", **params)

    def update_queue(self, **params: str) -> JSONObject:
        """Update a call queue (``updateQueue``). Param: ``queue`` (extension)."""
        return self._call("updateQueue", **params)

    def delete_queue(self, **params: str) -> JSONObject:
        """Delete a call queue (``deleteQueue``). Param: ``queue`` (extension)."""
        return self._call("deleteQueue", **params)

    def login_logoff_queue_agent(self, **params: str) -> JSONObject:
        """Log a dynamic agent in/out of a queue (``loginLogoffQueueAgent``)."""
        return self._call("loginLogoffQueueAgent", **params)

    def pause_unpause_queue_agent(self, **params: str) -> JSONObject:
        """Pause/unpause a queue agent (``pauseUnpauseQueueAgent``)."""
        return self._call("pauseUnpauseQueueAgent", **params)

    # ── Paging / intercom groups ─────────────────────────────────────────────

    def list_paging_groups(self, **params: str) -> list[JSONValue]:
        """List paging/intercom groups (``listPaginggroup``). Item key: ``paginggroup``."""
        return self._list("listPaginggroup", "paginggroup", **params)

    def get_paging_group(self, **params: str) -> JSONObject:
        """Get a paging/intercom group (``getPaginggroup``). Param: ``paginggroup`` (id)."""
        return self._call("getPaginggroup", **params)

    def add_paging_group(self, **params: str) -> JSONObject:
        """Create a paging/intercom group (``addPaginggroup``)."""
        return self._call("addPaginggroup", **params)

    def update_paging_group(self, **params: str) -> JSONObject:
        """Update a paging/intercom group (``updatePaginggroup``). Param: ``paginggroup`` (id)."""
        return self._call("updatePaginggroup", **params)

    def delete_paging_group(self, **params: str) -> JSONObject:
        """Delete a paging/intercom group (``deletePaginggroup``). Param: ``paginggroup`` (id)."""
        return self._call("deletePaginggroup", **params)

    def multicast_paging(self, **params: str) -> JSONObject:
        """Start a multicast paging session (``MulticastPaging``)."""
        return self._call("MulticastPaging", **params)

    def multicast_paging_hangup(self, **params: str) -> JSONObject:
        """Stop a multicast paging session (``MulticastPagingHangup``)."""
        return self._call("MulticastPagingHangup", **params)

    # ── Channels & call control ──────────────────────────────────────────────
    #
    # ``listBridgedChannels`` / ``listUnBridgedChannels`` / ``Hangup`` /
    # ``mute`` / ``unmute`` / ``acceptCall`` / ``refuseCall`` are on the base
    # client. The additions below complete channel control.

    def hold(self, **params: str) -> JSONObject:
        """Put a channel on hold (``hold``). Param: ``channel``."""
        return self._call("hold", **params)

    def unhold(self, **params: str) -> JSONObject:
        """Resume a held channel (``unhold``). Param: ``channel``."""
        return self._call("unhold", **params)

    def callbarge(self, channel: str, exten: str, barge_exten: str, mode: str = "") -> JSONObject:
        """Barge into an active call — listen/whisper/barge (``callbarge``).

        Args:
            channel: The channel to monitor.
            exten: The extension that monitors the call.
            barge_exten: Permission extension, ``"<exten>@1"`` (ask) or
                ``"<exten>@0"`` (don't ask).
            mode: ``""`` listen, ``"W"`` whisper, ``"B"`` barge.

        Returns:
            JSONObject: The ``callbarge`` response.
        """
        return self._call("callbarge", channel=channel, exten=exten, **{"barge-exten": barge_exten, "mode": mode})

    # ── Dialing (3rd-party call origination) ─────────────────────────────────
    #
    # ``dialExtension`` is on the base client.

    def dial_outbound(self, **params: str) -> JSONObject:
        """Originate an outbound call (``dialOutbound``)."""
        return self._call("dialOutbound", **params)

    def dial_outbound_two(self, **params: str) -> JSONObject:
        """Originate a call between two external numbers (``dialOutboundTwo``)."""
        return self._call("dialOutboundTwo", **params)

    def dial_ivr(self, **params: str) -> JSONObject:
        """Dial an extension into an IVR (``dialIVR``)."""
        return self._call("dialIVR", **params)

    def dial_ivr_outbound(self, **params: str) -> JSONObject:
        """Dial an outbound number into an IVR (``dialIVROutbound``)."""
        return self._call("dialIVROutbound", **params)

    def dial_queue(self, **params: str) -> JSONObject:
        """Dial an extension into a queue (``dialQueue``)."""
        return self._call("dialQueue", **params)

    def dial_ringgroup(self, **params: str) -> JSONObject:
        """Dial an extension into a ring group (``dialRinggroup``)."""
        return self._call("dialRinggroup", **params)

    # ── Transfer ─────────────────────────────────────────────────────────────

    def call_transfer(self, **params: str) -> JSONObject:
        """Transfer a call (``callTransfer``). Params: ``channel``, ``exten``."""
        return self._call("callTransfer", **params)

    def transfer_number_inbound(self, **params: str) -> JSONObject:
        """Transfer an inbound call to a number (``transferNumberInbound``)."""
        return self._call("transferNumberInbound", **params)

    def transfer_number_outbound(self, **params: str) -> JSONObject:
        """Transfer an outbound call to a number (``transferNumberOutbound``)."""
        return self._call("transferNumberOutbound", **params)

    # ── Legacy sub-APIs ──────────────────────────────────────────────────────
    #
    # ``cdrapi`` is on the base client as :meth:`~ucm6204_api.UCM6204.get_cdr`.

    def recapi(self, **params: str) -> JSONObject:
        """Recording API — download/list recordings (``recapi``). Param: ``filename``."""
        return self._call("recapi", **params)

    def pmsapi(self, **params: str) -> JSONObject:
        """PMS (hospitality) API (``pmsapi``)."""
        return self._call("pmsapi", **params)

    def queueapi(self, **params: str) -> JSONObject:
        """Queue statistics API (``queueapi``)."""
        return self._call("queueapi", **params)

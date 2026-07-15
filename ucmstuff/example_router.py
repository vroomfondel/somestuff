#!/usr/bin/env python3
"""Coordinated example: monitor UCM calls over WebSocket and control them via the API.

This wires the two coordinated clients together:

* :class:`~ucmstuff.ucm6204_api.UCMEventClient` — receives real-time call events
  over the WebSocket (authenticated with a *web* user).
* :class:`~ucmstuff.ucm6204_api.UCM6204` — the HTTPS-API control client
  (authenticated with the *API* user), used to act on those events.

A :class:`~ucmstuff.ucm6204_api.TrunkCallRouter` filters incoming calls to the
configured trunk(s) and hands each one to ``on_incoming``, where the caller-based
branching lives. Configuration is read from environment variables — see
``ucm.env.example``.

Run (from the repo root so ``ucmstuff`` is importable)::

    set -a; . ucmstuff/ucm.env.local; set +a   # your filled-in copy
    python3 -m ucmstuff.example_router
"""

import logging
import os
import sys

from ucmstuff.ucm6204_api import (
    IncomingCall,
    TrunkCallRouter,
    UCM6204,
    UCMAPIError,
    UCMEventClient,
)


def main() -> None:
    """Connect both clients and route incoming trunk calls to ``on_incoming``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    host = os.environ["UCM_HOST"]
    port = int(os.environ.get("UCM_PORT", "8089"))
    trunks = [t for t in os.environ.get("UCM_TRUNKS", "").split() if t]

    # Control client (HTTPS API, API user) — used to act on calls.
    api = UCM6204(
        host=host, port=port, api_user=os.environ["UCM_API_USER"], api_password=os.environ["UCM_API_PASSWORD"]
    )
    api.connect()

    # Event client (WebSocket, web user) — receives the call events.
    events = UCMEventClient(
        host=host, ws_port=port, web_user=os.environ["UCM_WEB_USER"], web_password=os.environ["UCM_WEB_PASSWORD"]
    )

    def on_incoming(call: IncomingCall) -> None:
        """Branch by caller — replace the demo rules with your own logic."""
        logging.info("incoming %s call from %r <%s> on %s", call.trunk, call.name, call.number, call.channel)
        # --- your caller-based branching goes here, e.g.: ---
        # try:
        #     if call.number in {"+491234567"}:
        #         api.accept_call(call.channel)
        #     elif call.name.upper().startswith("SPAM"):
        #         api.refuse_call(call.channel)
        # except UCMAPIError as exc:
        #     logging.warning("control action failed: %s", exc)

    if trunks:
        events.add_event_handler(TrunkCallRouter(trunks, on_call=on_incoming).handle)
    else:
        logging.warning("UCM_TRUNKS is empty — monitoring only, no call routing")

    logging.info("monitoring %s:%d, trunks=%s", host, port, trunks or "-")
    events.run(block=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)

"""Process-wide singletons, initialised in main.lifespan()."""
from __future__ import annotations


class AppState:
    ctl = None         # CallController
    wg = None          # WireGuardManager
    transcoder = None  # TranscoderService
    sip = None         # SipGateway | None
    started = 0.0


state = AppState()

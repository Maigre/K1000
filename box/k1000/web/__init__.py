# SPDX-License-Identifier: GPL-3.0-or-later
"""K1000 phone surface: the box's own PWA (HTTPS + websocket) and the captive portal.

D-007, docs/06-phone.md. `server.PhoneServer` is the only entry point the service needs.
"""
from .server import PhoneBridge, PhoneScreenInfo, PhoneServer, PhoneSettings

__all__ = ["PhoneBridge", "PhoneScreenInfo", "PhoneServer", "PhoneSettings"]

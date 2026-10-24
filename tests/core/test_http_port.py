"""One process per port (GAP-117): a second Controller must not share 10994."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler

import pytest

from core.protocol.http_server import OwnPortServer


def test_a_second_server_on_the_same_port_fails() -> None:
    """On Windows the standard server's SO_REUSEADDR let two Controllers take 10994
    (two quick double-clicks); the second must fail at once, on every system."""
    first = OwnPortServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        with pytest.raises(OSError):
            OwnPortServer(("127.0.0.1", first.server_address[1]), BaseHTTPRequestHandler)
    finally:
        first.server_close()

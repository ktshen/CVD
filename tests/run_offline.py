from __future__ import annotations

import ipaddress
import logging
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch
import urllib.parse
import urllib.request

import aiohttp


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    blocked: list[str] = []
    original_resolve = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_urlopen = urllib.request.urlopen
    original_request = aiohttp.ClientSession._request

    def require_local(host) -> None:
        if host is None or host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        blocked.append(str(host))
        raise AssertionError("External networking is disabled during offline tests")

    def resolve(host, *args, **kwargs):
        require_local(host)
        return original_resolve(host, *args, **kwargs)

    def connect(sock, address):
        if isinstance(address, tuple):
            require_local(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if isinstance(address, tuple):
            require_local(address[0])
        return original_connect_ex(sock, address)

    def urlopen(url, *args, **kwargs):
        require_local(urllib.parse.urlsplit(getattr(url, "full_url", url)).hostname)
        return original_urlopen(url, *args, **kwargs)

    async def request(session, method, url, *args, **kwargs):
        require_local(urllib.parse.urlsplit(str(url)).hostname)
        return await original_request(session, method, url, *args, **kwargs)

    with (
        patch.object(socket, "getaddrinfo", resolve),
        patch.object(socket.socket, "connect", connect),
        patch.object(socket.socket, "connect_ex", connect_ex),
        patch.object(urllib.request, "urlopen", urlopen),
        patch.object(aiohttp.ClientSession, "_request", request),
    ):
        previous_logging_level = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            suite = unittest.defaultTestLoader.discover(str(root / "tests"))
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        finally:
            logging.disable(previous_logging_level)
    print(f"External network attempts: {len(blocked)}")
    return 0 if result.wasSuccessful() and not blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
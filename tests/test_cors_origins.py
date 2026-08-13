"""CORS origin policy.

Opening the console from another device on the LAN (`vite --host`) changes the
browser's Origin to the dev machine's DHCP address, which an exact allowlist
cannot anticipate. The failure mode is silent -- the preflight is rejected, the
real request is never sent, and commands simply appear to do nothing -- so the
policy is worth pinning.
"""
import re

from api.app import _PRIVATE_LAN_ORIGIN_REGEX

MATCH = re.compile(_PRIVATE_LAN_ORIGIN_REGEX).fullmatch


def test_allows_private_lan_origins_on_any_port():
    assert MATCH("http://10.0.10.117:5175")
    assert MATCH("http://192.168.1.20:5175")
    assert MATCH("http://172.16.4.4:3000")
    assert MATCH("http://172.31.255.255:8080")


def test_allows_mdns_hostnames():
    assert MATCH("http://roverhub.local:5175")


def test_rejects_public_origins():
    """The point of the private-range restriction: a hostile page on the
    internet must not be able to drive the robot through this API."""
    assert not MATCH("http://evil.example.com:5175")
    assert not MATCH("http://93.184.216.34:5175")
    assert not MATCH("https://evil.example.com")


def test_rejects_addresses_just_outside_the_private_ranges():
    # 172.15 and 172.32 bracket the 172.16-172.31 private block.
    assert not MATCH("http://172.15.0.1:5175")
    assert not MATCH("http://172.32.0.1:5175")
    assert not MATCH("http://11.0.0.1:5175")


def test_rejects_origin_with_a_path_or_trailing_content():
    """A regex anchored loosely would let `http://10.0.0.1:80.evil.com` through."""
    assert not MATCH("http://10.0.10.117:5175/evil")
    assert not MATCH("http://10.0.10.117:5175.evil.com")

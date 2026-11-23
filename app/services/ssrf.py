"""Shared hostname-resolving SSRF guard (argon S3).

Both the AI-provider ``base_url`` check (settings) and the automation
``input.http`` fetcher resolve the URL host with :func:`socket.getaddrinfo`
and require EVERY resolved address to be acceptable: loopback, link-local,
multicast and reserved ranges are rejected by default. IPv4-mapped IPv6
addresses (``::ffff:1.2.3.4``) are unwrapped before the verdict so the
mapped form cannot smuggle a non-public target past the check.
Unresolvable hosts fail closed (there is nothing public to confirm).

Carve-outs (AI provider URLs only — typed in by the account owner in
settings, where local Ollama/LM Studio and LAN mirrors are flagship
flows): ``allow_loopback`` permits loopback, ``allow_private`` additionally
permits private LAN ranges (RFC1918 10/8, 172.16/12, 192.168/16, CGNAT
100.64/10 for Tailscale, ULA fc00::/7). Link-local (incl. the
169.254.169.254 cloud-metadata address), multicast, unspecified and
reserved addresses stay blocked even with both carve-outs. Automation
``input.http`` enforces the strict form (no carve-out, no allowlist yet).
"""

import ipaddress
import socket
from urllib.parse import urlparse

_PRIVATE_LAN_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / Tailscale
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
)


def _is_private_lan(ip):
    """True when *ip* is a private LAN address allowed for AI URLs."""
    try:
        return any(ip in net for net in _PRIVATE_LAN_NETS
                   if ip.version == net.version)
    except (TypeError, ValueError):
        return False


def _unwrapped(ip):
    """Unwrap IPv4-mapped IPv6 (``::ffff:127.0.0.1`` -> ``127.0.0.1``)."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def host_block_reason(host, *, allow_loopback=False, allow_private=False):
    """Refusal reason for *host*, or None when it resolves exclusively to
    acceptable addresses (public always; loopback iff *allow_loopback*;
    private LAN iff *allow_private*). Link-local, multicast, unspecified
    and reserved addresses are always refused."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return (f"Could not resolve host {host!r}; "
                "only public hosts are allowed.")
    addrs = [info[4][0] for info in infos]
    if not addrs:
        return (f"Could not resolve host {host!r}; "
                "only public hosts are allowed.")
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return (f"Could not resolve host {host!r}; "
                    "only public hosts are allowed.")
        ip = _unwrapped(ip)
        if ip.is_loopback:
            if allow_loopback:
                continue
            return ("That host resolves to a non-public address; "
                    "only public hosts are allowed.")
        # Always dangerous, even for owner-typed AI URLs: cloud metadata
        # (169.254.169.254), multicast, 0.0.0.0 and reserved ranges.
        if (ip.is_link_local or ip.is_unspecified or ip.is_multicast
                or ip.is_reserved):
            return ("That host resolves to a non-public address; "
                    "only public hosts are allowed.")
        if ip.is_global:
            continue
        if allow_private and _is_private_lan(ip):
            continue
        return ("That host resolves to a non-public address; "
                "only public hosts are allowed.")
    return None


def url_block_reason(url, *, allow_loopback=False, allow_private=False):
    """Refusal reason for *url* (bad scheme/host or non-public DNS), or
    None when the URL may proceed."""
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return "Invalid URL."
    if parsed.scheme not in ("http", "https"):
        return "URL must start with http:// or https://."
    host = (parsed.hostname or "").lower()
    if not host:
        return "URL must include a host."
    return host_block_reason(host, allow_loopback=allow_loopback,
                             allow_private=allow_private)


def is_public_url(url, *, allow_loopback=False, allow_private=False):
    """True when *url* is http(s) and resolves exclusively to acceptable IPs
    (public always; loopback iff *allow_loopback*; private LAN iff
    *allow_private*)."""
    return url_block_reason(url, allow_loopback=allow_loopback,
                            allow_private=allow_private) is None

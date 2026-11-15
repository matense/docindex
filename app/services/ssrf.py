"""Shared hostname-resolving SSRF guard (argon S3).

Both the AI-provider ``base_url`` check (settings) and the automation
``input.http`` fetcher resolve the URL host with :func:`socket.getaddrinfo`
and require EVERY resolved address to be public: loopback, link-local,
private, multicast and reserved ranges are all rejected. IPv4-mapped IPv6
addresses (``::ffff:1.2.3.4``) are unwrapped before the verdict so the
mapped form cannot smuggle a non-public target past the check.
Unresolvable hosts fail closed (there is nothing public to confirm).

The only carve-out is ``allow_loopback``: AI provider URLs are typed in by
the account owner in settings — local Ollama/LM Studio is a flagship flow —
so the settings routes explicitly opt in to loopback. Automation
``input.http`` enforces the strict form (no carve-out, no allowlist yet).
"""

import ipaddress
import socket
from urllib.parse import urlparse


def _unwrapped(ip):
    """Unwrap IPv4-mapped IPv6 (``::ffff:127.0.0.1`` -> ``127.0.0.1``)."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def host_block_reason(host, *, allow_loopback=False):
    """Refusal reason for *host*, or None when it resolves exclusively to
    public addresses (loopback additionally iff *allow_loopback*)."""
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
        if ip.is_global:
            continue
        if allow_loopback and ip.is_loopback:
            continue
        return ("That host resolves to a non-public address; "
                "only public hosts are allowed.")
    return None


def url_block_reason(url, *, allow_loopback=False):
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
    return host_block_reason(host, allow_loopback=allow_loopback)


def is_public_url(url, *, allow_loopback=False):
    """True when *url* is http(s) and resolves exclusively to public IPs
    (loopback additionally iff *allow_loopback*)."""
    return url_block_reason(url, allow_loopback=allow_loopback) is None

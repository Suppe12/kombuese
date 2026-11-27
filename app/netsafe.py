"""Fetching URLs on behalf of users and AI runs without reaching into the local network (SSRF).

Every host name is resolved first; loopback, private, link-local and other non-public addresses are refused, also after
redirects (followed by hand, at most 5). KOMBUESE_ALLOW_PRIVATE_URLS=true lifts the check, e.g. for a recipe site in
your own network.

DNS rebinding (security review 2026-11-25): the connection itself goes to an address checked in the same step
(_connect below). Before, check() resolved the name and urllib resolved it a second time to connect, so a DNS server
answering with a public address first and 127.0.0.1 or 192.168.x.x a moment later led into the local network.
With an HTTP(S) proxy in the environment the proxy resolves the names; then only check() applies, as before.
"""
import http.client
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

import config


class Blocked(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _addresses(host, port):
    """Resolve host; refuse it if any of its addresses is not a public one. → getaddrinfo entries"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Blocked(f"host not found: {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise Blocked(f"address in a local network is not allowed: {host}")
    return infos


def _connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs):
    """socket.create_connection for http.client, but only to the addresses resolved and checked right here."""
    if config.ALLOW_PRIVATE_URLS:
        return socket.create_connection(address, timeout, source_address)
    host, port = address
    err = None
    for family, _, proto, _, sockaddr in _addresses(host, port):
        s = None
        try:
            s = socket.socket(family, socket.SOCK_STREAM, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                s.settimeout(timeout)
            if source_address:
                s.bind(source_address)
            s.connect(sockaddr)
            return s
        except OSError as e:
            err = e
            if s is not None:
                s.close()
    raise err or OSError(f"cannot connect to {host}")


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._create_connection = _connect


class _HTTPSConnection(http.client.HTTPSConnection):  # TLS and the certificate check stay with the host name
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._create_connection = _connect


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


_opener = (urllib.request.build_opener(_NoRedirect) if urllib.request.getproxies()
           else urllib.request.build_opener(_NoRedirect, _HTTPHandler, _HTTPSHandler))


def check(url):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Blocked("only http(s) links")
    if config.ALLOW_PRIVATE_URLS:
        return
    _addresses(u.hostname, u.port or (443 if u.scheme == "https" else 80))


def open_url(url, headers=None, timeout=20, max_bytes=6 * 1024 * 1024, data=None, method=None):
    """→ (bytes, content_type, charset, final_url). Raises Blocked, urllib.error.HTTPError/URLError."""
    for _ in range(6):
        check(url)
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            r = _opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                if e.code == 303:
                    data, method = None, "GET"
                continue
            raise
        with r:
            return r.read(max_bytes), r.headers.get_content_type(), r.headers.get_content_charset(), url
    raise Blocked("too many redirects")

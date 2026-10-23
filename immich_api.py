"""Small Immich client. No permanent-delete or empty-trash operation exists."""

import ipaddress
import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request

from inventory import asset_id


class ApiError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_key(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError(
                "API key must be a regular private file with mode 600 or 400"
            )
        if info.st_uid != os.getuid():
            raise ValueError("API key file must belong to the current user")
        key = source.read(4097).strip()
    if (
        not key
        or len(key) > 4096
        or any(c.isspace() or not c.isascii() or not c.isprintable() for c in key)
    ):
        raise ValueError("invalid API key file")
    return key


class ImmichAPI:
    def __init__(self, url, key, timeout=30):
        parsed = urllib.parse.urlsplit(url)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "API URL must have a host and no credentials, query or fragment"
            )
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError("use HTTPS, or HTTP on loopback through an SSH tunnel")
        if parsed.path.rstrip("/") != "/api":
            raise ValueError("API URL must end in /api")
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.url + path,
            body,
            {
                "x-api-key": self.key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(20 * 1024 * 1024 + 1)
                if len(data) > 20 * 1024 * 1024:
                    raise ApiError("API response exceeds size limit")
                return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            # Do not expose server error bodies, headers or credentials in logs.
            raise ApiError(
                f"Immich API {method} failed with HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            raise ApiError(
                f"Immich API {method} failed; check connection and journal"
            ) from None

    def preflight(self, owner, for_restore=False):
        identity = self.request("GET", "/users/me")
        if identity.get("id") != owner:
            raise ApiError("API key belongs to a different owner")
        permissions = set(self.request("GET", "/api-keys/me")["permissions"])
        required = {"asset.read", "asset.delete", "user.read", "userConfig.read"}
        if "all" not in permissions and not required.issubset(permissions):
            raise ApiError(
                "key requires asset.read, asset.delete, user.read and userConfig.read"
            )
        trash = self.request("GET", "/config")["trash"]
        if not for_restore and (
            trash.get("enabled") is not True
            or type(trash.get("days")) is not int
            or trash["days"] < 7
        ):
            raise ApiError("trash must be enabled with at least 7 days retention")
        return trash

    def get_asset(self, identifier):
        return self.request("GET", "/assets/" + asset_id(identifier))

    def trash(self, ids):
        if not ids:
            raise ValueError("empty trash request")
        return self.request(
            "DELETE", "/assets", {"ids": [asset_id(i) for i in ids], "force": False}
        )

    def restore(self, ids):
        if not ids:
            raise ValueError("empty restore request")
        return self.request(
            "POST", "/trash/restore/assets", {"ids": [asset_id(i) for i in ids]}
        )

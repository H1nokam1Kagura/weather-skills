"""Minimal Kong native-Anthropic client for hosts without the Kong kit (e.g. a GitHub runner).

The relay's KongRoles worker (``tools/workflow/kong_roles.WORKER_SOURCE``) imports a module named
``kong_client`` from ``<kit_root>/scripts`` and uses exactly: ``KongConfig.from_env``,
``anthropic_client``, ``check_body_size``, ``create_message``, ``describe_error``, ``text_of``
and ``is_mock_url``. This file provides those names with the same contracts as the kit's
``scripts/kong_client.py``, so the worker source, the receipt, the redaction and the UNKNOWN
semantics are byte-for-byte the KongRoles ones. Point KongRoles at it with
``kit_root=relay/runner_kit`` and ``kit_python=<an interpreter with the pinned anthropic SDK>``
(``relay/runner_kit/requirements.txt``).

Differences from the kit, all narrowing:
- the key comes ONLY from the process environment (``KONG_ANTHROPIC_KEY``); no ``.env`` is read;
- the route is fixed: ``https://ai-kong-gateway.bmgf.io`` + ``/v2/anthropic`` (native route).
  ``KONG_GATEWAY_URL`` may only redirect to a loopback ``http`` mock, which accepts only the
  kit's test keys, so a real key can never be sent anywhere but the gateway;
- the SDK's debug logging is disabled before import (``ANTHROPIC_LOG`` is dropped and the
  ``anthropic``/``httpx`` loggers are held at WARNING), so no library echoes request headers;
- no Unified route, no backoff helper (KongRoles never retries).
"""

import ipaddress
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_GATEWAY = "https://ai-kong-gateway.bmgf.io"
ANTHROPIC_PATH = "/v2/anthropic"  # native route; the SDK appends /v1/messages
KEY_HEADER = "x-kong-key"
PLACEHOLDER_KEY = "kong-managed"  # ignored by Kong on the messages route (kit CODE)
MOCK_KEYS = ("test-key", "test-key-b")
# Largest request body observed passing (kit KNOWN_GOOD_BODY_BYTES).
KNOWN_GOOD_BODY_BYTES = 97_600
SECRET_NAME_PREFIXES = ("KONG_", "ANTHROPIC_", "OPENAI_")
SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET")


def is_mock_url(url):
    host = (urlsplit(url).hostname or "").lower()
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _known_secrets(secrets=()):
    known = {str(v) for v in secrets if v}
    known.update(
        v
        for k, v in os.environ.items()
        if v and k.startswith(SECRET_NAME_PREFIXES) and any(s in k for s in SECRET_NAME_MARKERS)
    )
    return known


def sanitize(value, secrets=()):
    """Redact every known secret value (and URL credentials/key query params) from diagnostics."""
    known = _known_secrets(secrets)
    if isinstance(value, dict):
        return {sanitize(str(k), known): sanitize(v, known) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, known) for v in value]
    if isinstance(value, str):
        for secret in sorted(known, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
        value = re.sub(
            r"(?i)([?&](?:key|api_key|token|secret|password)=)[^&\s]+", r"\1[REDACTED]", value
        )
    return value


@dataclass(frozen=True)
class KongConfig:
    gateway_url: str
    anthropic_key: str | None
    max_retries: int
    timeout: float
    anthropic_path: str = ANTHROPIC_PATH
    key_header: str = KEY_HEADER
    placeholder_key: str = PLACEHOLDER_KEY

    def __post_init__(self):
        url = urlsplit(self.gateway_url)
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("gateway must be an origin without credentials, path or query")
        mock = is_mock_url(self.gateway_url)
        if self.gateway_url != DEFAULT_GATEWAY and not (mock and url.scheme == "http"):
            raise ValueError("only the production gateway or a loopback http mock is allowed")
        if mock and self.anthropic_key not in (None, *MOCK_KEYS):
            raise ValueError("loopback mocks accept only test keys; remove real credentials")
        if not 0 <= self.max_retries <= 5:
            raise ValueError("SDK retries must be 0..5")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("timeout must be finite, >0 and <=300 seconds")

    @classmethod
    def from_env(cls):
        env = os.environ.get
        return cls(
            gateway_url=env("KONG_GATEWAY_URL", DEFAULT_GATEWAY).rstrip("/"),
            anthropic_key=env("KONG_ANTHROPIC_KEY") or None,
            max_retries=int(env("KONG_SDK_MAX_RETRIES", "0")),
            timeout=float(env("KONG_TIMEOUT_SECONDS", "60")),
        )

    @property
    def anthropic_base_url(self):
        return self.gateway_url + self.anthropic_path

    @property
    def unified_key(self):  # the worker passes (anthropic_key, unified_key) to describe_error
        return None

    def __repr__(self):  # never show the key
        key = "<set>" if self.anthropic_key else "<unset>"
        return f"KongConfig(gateway_url={self.gateway_url!r}, anthropic_key={key})"


def _quiet_sdk_logging():
    os.environ.pop("ANTHROPIC_LOG", None)
    for name in ("anthropic", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def anthropic_client(cfg=None):
    """Official Anthropic SDK pointed at the native route, key in ``x-kong-key``."""
    _quiet_sdk_logging()
    import anthropic

    cfg = cfg or KongConfig.from_env()
    if not cfg.anthropic_key:
        raise RuntimeError("KONG_ANTHROPIC_KEY is not set")
    return anthropic.Anthropic(
        api_key=cfg.placeholder_key,
        base_url=cfg.anthropic_base_url,
        default_headers={cfg.key_header: cfg.anthropic_key},
        max_retries=cfg.max_retries,
        timeout=cfg.timeout,
    )


def text_of(message):
    """First text block of an Anthropic message (content[0] may be a thinking block)."""
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def _get(headers, name):
    if headers is None:
        return None
    try:
        return headers.get(name)
    except Exception:
        return None


def rate_info(headers):
    return {
        "via": _get(headers, "via"),
        "request_id": _get(headers, "x-kong-request-id") or _get(headers, "request-id"),
        "retry_after": _get(headers, "retry-after"),
    }


def layer_hint(status, via, body=""):
    """Best-effort guess at which layer produced an error (same table as the kit)."""
    if status is None:
        return "no response received (network, DNS, TLS, or timeout)"
    if not via and not body.strip():
        return "empty body and no via header: likely an edge layer in front of Kong"
    if via and not body.strip():
        return "empty body but Kong answered: likely a gateway or plugin defect"
    if '"type":"error"' in body.replace(" ", ""):
        return "provider-shaped error: the request reached the upstream provider"
    if "no Route matched" in body:
        return "Kong has no route for this path: check the path prefix"
    if status == 401 and via:
        return "Kong or an upstream rejected the credential"
    if status == 429:
        return "rate limited"
    return "see the Kong limits-and-errors reference"


def describe_error(err, secrets=()):
    """Structured, key-free description of an SDK or HTTP error."""
    response = getattr(err, "response", None)
    headers = getattr(response, "headers", None)
    status = getattr(err, "status_code", None) or getattr(response, "status_code", None)
    body = ""
    try:
        body = response.text if response is not None else ""
    except Exception:
        pass
    info = rate_info(headers)
    return sanitize(
        {
            "status": status,
            "message": sanitize(str(getattr(err, "message", None) or err), secrets)[:300],
            "via": info.get("via"),
            "request_id": info.get("request_id"),
            "hint": layer_hint(status, info.get("via"), body),
        },
        secrets,
    )


def check_body_size(payload, limit=KNOWN_GOOD_BODY_BYTES):
    size = len(json.dumps(payload).encode("utf-8"))
    if size > limit:
        raise ValueError(f"request body is {size:,} bytes; the known-good maximum is {limit:,}")
    return size


def create_message(client, **kwargs):
    """messages.create returning (message, routing info from response headers)."""
    raw = client.messages.with_raw_response.create(**kwargs)
    return raw.parse(), rate_info(raw.headers)

"""Relay tokens and room-change publishing for the QueUp relay on verilog (see P2P.md)."""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request

PUBLISH_TIMEOUT = 1.5      # seconds per attempt; a slow relay must never hold up a write for long
FAILURES_BEFORE_BACKOFF = 3
BACKOFF_SECONDS = 10
TOKEN_TTL = 6 * 3600
_down_until = 0.0
_failures = 0


def config(private):
    """(relay url, shared secret) from <private>/relay_url and relay_secret, or None when the relay isn't set up."""
    try:
        url = open(private + "relay_url").read().strip()
        secret = open(private + "relay_secret").read().strip()
    except OSError:
        return None
    return (url, secret) if url and secret else None


def tree_name(private):
    """queup, queup-dev, ...: rooms of different deployments never mix on the relay."""
    return os.path.basename(private.rstrip("/"))


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def mint_token(private, room, user, owner):
    """{url, token} letting this user's tab join this room on the relay, or None if the relay isn't set up."""
    cfg = config(private)
    if not cfg:
        return None
    url, secret = cfg
    claims = {"t": tree_name(private), "r": room, "u": user, "o": bool(owner), "e": int(time.time() + TOKEN_TTL)}
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = _b64url(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return {"url": url, "token": body + "." + sig}


def publish_endpoint(private, url):
    """<private>/relay_publish_url when browsers reach the relay through a proxy (e.g. port 443), else derived from url."""
    try:
        endpoint = open(private + "relay_publish_url").read().strip()
    except OSError:
        endpoint = ""
    return endpoint or url.replace("wss://", "https://", 1).replace("ws://", "http://", 1).rsplit("/", 1)[0] + "/publish"


def publish(private, room, data):
    """Send a room's current state (or {} once deleted) to the relay, retrying once; tabs re-check the server if one is lost."""
    global _down_until, _failures
    cfg = config(private)
    if not cfg or time.time() < _down_until:
        return False
    url, secret = cfg
    endpoint = publish_endpoint(private, url)
    body = json.dumps({"tree": tree_name(private), "room": room, "data": data}, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), (ts + "." + body).encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(endpoint, data=body.encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Relay-Ts": ts, "X-Relay-Sig": sig})
    for attempt in (1, 2):
        start = time.time()
        try:
            urllib.request.urlopen(req, timeout=PUBLISH_TIMEOUT).read()
            _failures = 0
            return True
        except Exception as e:
            sys.stderr.write("relay publish to %s failed (room %s, attempt %d, %.2fs): %r\n" % (endpoint, room, attempt, time.time() - start, e))
    _failures += 1
    if _failures >= FAILURES_BEFORE_BACKOFF:
        # don't make every write wait on a relay that keeps failing
        _down_until = time.time() + BACKOFF_SECONDS
        _failures = 0
    return False

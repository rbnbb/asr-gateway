"""Signed, expiring browser cookies; API bearer credentials remain independent."""
import hashlib
import hmac
import secrets
import time
from http.cookies import SimpleCookie, CookieError

COOKIE = "__Host-asr_session"


def signing_key(config):
    # Rotating either API or browser credentials invalidates existing sessions.
    material = "\0".join((config.api_key, config.browser_username, config.browser_password))
    return hashlib.sha256(material.encode()).hexdigest()


def issue(key, lifetime):
    payload = str(int(time.time()) + lifetime) + "." + secrets.token_hex(16)
    signature = hmac.new(key.encode(), ("browser-session-v1:" + payload).encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def valid(header, key):
    try:
        cookies = SimpleCookie()
        cookies.load(header)
        value = cookies[COOKIE].value
        expires, nonce, signature = value.split(".")
        if int(expires) <= time.time() or len(nonce) != 32:
            return False
        payload = expires + "." + nonce
        expected = hmac.new(key.encode(), ("browser-session-v1:" + payload).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (CookieError, KeyError, ValueError, TypeError):
        return False


def cookie_header(value, lifetime):
    return f"{COOKIE}={value}; Path=/; Max-Age={lifetime}; HttpOnly; Secure; SameSite=Strict"

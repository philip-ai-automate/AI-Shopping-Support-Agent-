"""
data_crypto.py — encryption for buyer details we keep (7 Nov 2026, Shopify 7A item 9).

One data key, used only for this. It lives in /etc/phixtra/data.key (root
only, mode 600) and a copy is kept in the owner's password manager. It is
NOT the portal login key (PORTAL_SECRET_KEY), so changing that never makes
stored details unreadable.

Stored values look like "v1:<Fernet token>". Anything without the "v1:"
prefix is old plain text and is returned as it is, so readers work before,
during and after the one-off conversion.

The same file is in ai-backend/, api-key-manager/ and whatsapp-gateway/ — keep them identical.
"""
import base64
import hashlib
import hmac
import os

KEY_FILE = os.getenv("PHIXTRA_DATA_KEY_FILE", "/etc/phixtra/data.key")
PREFIX = "v1:"
_f = None


def _raw() -> bytes:
    with open(KEY_FILE, "rb") as fh:
        raw = fh.read().strip()
    key = base64.urlsafe_b64decode(raw)
    if len(key) != 32:
        raise ValueError("data key must be 32 bytes")
    return key


def _fernet():
    global _f
    if _f is None:
        from cryptography.fernet import Fernet
        k = _raw()
        # Separate sub-keys for encryption and fingerprints.
        _f = Fernet(base64.urlsafe_b64encode(hmac.new(k, b"phixtra-enc-v1", hashlib.sha256).digest()))
    return _f


def enc(s):
    """Encrypt a text value. None/'' stay as they are. Raises if the key is missing
    (never store plain text by accident)."""
    if s is None or s == "":
        return s
    s = str(s)
    if s.startswith(PREFIX):
        # Already encrypted? Only if it really opens — a visitor can type
        # text that happens to start with "v1:" (review 10 Nov 2026).
        try:
            _fernet().decrypt(s[len(PREFIX):].encode())
            return s
        except Exception:
            pass
    return PREFIX + _fernet().encrypt(s.encode()).decode()


def dec(s):
    """Decrypt a stored value. Plain (old) values come back unchanged; a value
    that can't be decrypted comes back as ''."""
    if not s or not isinstance(s, str) or not s.startswith(PREFIX):
        return s
    try:
        return _fernet().decrypt(s[len(PREFIX):].encode()).decode()
    except Exception:
        return ""


def ready() -> bool:
    """True when the data key loads and works. Services call this at start
    so a missing key shows as a loud error, not as silently lost messages."""
    try:
        return dec(enc("phixtra-key-check")) == "phixtra-key-check"
    except Exception as e:
        print(f"❌❌ DATA KEY NOT USABLE ({KEY_FILE}): {e} — messages cannot be saved", flush=True)
        return False


def is_enc(s) -> bool:
    return isinstance(s, str) and s.startswith(PREFIX)


def fingerprint(kind: str, value) -> str:
    """Keyed fingerprint of a value (e.g. a phone number in the access log), so
    an exact value can be searched for without being stored. '' for empty."""
    if value is None or value == "":
        return ""
    k = hmac.new(_raw(), b"phixtra-fp-v1", hashlib.sha256).digest()
    return hmac.new(k, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()

"""Optional passphrase authentication between peers.

When a passphrase is set, every peer-to-peer request carries an HMAC token bound to the
method, path, a timestamp and a single-use nonce. The server answers with a proof (an HMAC of
that token), so the client also learns whether the server knows the passphrase. Peers that
fail either check are ignored. The passphrase itself never crosses the network.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Mapping

TOKEN_HEADER = "X-LanShare-Auth"
PROOF_HEADER = "X-LanShare-Proof"
MAX_CLOCK_SKEW = 300  # seconds
_KDF_SALT = b"lanshare/v1/peer-auth"
_KDF_ITERATIONS = 200_000
_TOKEN_RE = re.compile(r"^v1\.([0-9a-f]{32})\.(\d{1,12})\.([0-9a-f]{24})\.([0-9a-f]{64})$")

# Reasons reported back to a rejected client and shown in its UI.
REASONS = {
    "missing": "this peer requires a passphrase",
    "malformed": "invalid authentication token",
    "bad_signature": "passphrase does not match",
    "clock_skew": "the two computers' clocks differ by more than 5 minutes",
    "replay": "authentication token was reused",
    "no_proof": "peer does not use the same passphrase",
}


class AuthError(Exception):
    def __init__(self, reason: str):
        super().__init__(REASONS.get(reason, reason))
        self.reason = reason


def derive_key(passphrase: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), _KDF_SALT, _KDF_ITERATIONS)


class Authenticator:
    def __init__(self, passphrase: str | None, peer_id: str):
        self.peer_id = peer_id
        self.enabled = bool(passphrase)
        self._key = derive_key(passphrase) if passphrase else b""
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _mac(self, *fields: str) -> str:
        return hmac.new(self._key, "\n".join(fields).encode("utf-8"), hashlib.sha256).hexdigest()

    # ----- client side ------------------------------------------------------------------

    def sign_request(self, method: str, path: str) -> dict[str, str]:
        """Headers to send with a request to another peer (empty when auth is off)."""
        if not self.enabled:
            return {}
        ts = str(int(time.time()))
        nonce = secrets.token_hex(12)
        sig = self._mac("req", method.upper(), path, ts, nonce, self.peer_id)
        return {TOKEN_HEADER: f"v1.{self.peer_id}.{ts}.{nonce}.{sig}"}

    def verify_response(self, request_headers: Mapping[str, str], response_headers: Mapping[str, str]) -> bool:
        """True if the responding peer proved it knows the passphrase (always True when off)."""
        if not self.enabled:
            return True
        token = request_headers.get(TOKEN_HEADER)
        proof = response_headers.get(PROOF_HEADER)
        if not token or not proof:
            return False
        return hmac.compare_digest(self.proof_for(token), proof)

    # ----- server side ------------------------------------------------------------------

    def verify_request(self, method: str, path: str, token: str | None) -> str:
        """Check a request token. Returns the caller's peer ID or raises AuthError."""
        if not token:
            raise AuthError("missing")
        m = _TOKEN_RE.match(token)
        if not m:
            raise AuthError("malformed")
        peer_id, ts, nonce, sig = m.groups()
        expected = self._mac("req", method.upper(), path, ts, nonce, peer_id)
        if not hmac.compare_digest(expected, sig):
            raise AuthError("bad_signature")
        now = time.time()
        if abs(now - int(ts)) > MAX_CLOCK_SKEW:
            raise AuthError("clock_skew")
        with self._lock:
            if nonce in self._seen:
                raise AuthError("replay")
            if len(self._seen) > 10_000:
                self._seen = {n: exp for n, exp in self._seen.items() if exp > now}
            self._seen[nonce] = now + 2 * MAX_CLOCK_SKEW
        return peer_id

    def proof_for(self, token: str) -> str:
        return self._mac("resp", token)

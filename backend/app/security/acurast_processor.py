"""Verification primitives for Acurast Processor management check-ins.

Acurast Android processors sign the JSON check-in body with a P-256 key. The
signature contains compact ``r || s || recovery-id`` bytes. Recovery is used to
derive the processor's SS58 address, so FARMOS never stores a processor private
or public key.
"""

from __future__ import annotations

import hashlib

from ecdsa import BadSignatureError, NIST256p, VerifyingKey
from ecdsa.util import sigdecode_string

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_encode(value: bytes) -> str:
    integer = int.from_bytes(value, "big")
    encoded = ""
    while integer:
        integer, remainder = divmod(integer, 58)
        encoded = _BASE58_ALPHABET[remainder] + encoded
    leading_zeroes = len(value) - len(value.lstrip(b"\0"))
    return "1" * leading_zeroes + (encoded or "1")


def _ss58_address_from_compressed_p256_key(compressed_key: bytes) -> str:
    """Derive Acurast's network-42 SS58 address from a compressed P-256 key."""
    public_key_hash = hashlib.blake2b(compressed_key, digest_size=32).digest()
    body = bytes([42]) + public_key_hash
    checksum = hashlib.blake2b(b"SS58PRE" + body, digest_size=64).digest()[:2]
    return _base58_encode(body + checksum)


def verify_acurast_processor_signature(
    address: str,
    raw_body: bytes,
    signature_hex: str,
) -> bool:
    """Return whether an official Acurast Android check-in signature is valid.

    The official backend hashes short payloads with SHA-256, and hashes long
    payloads with BLAKE2b-256 before SHA-256. It then recovers a P-256 public key
    from the compact signature and compares the derived SS58 address.
    """
    try:
        signature = bytes.fromhex(signature_hex)
        if len(signature) != 65:
            return False
        payload = (
            hashlib.blake2b(raw_body, digest_size=32).digest()
            if len(raw_body) > 256
            else raw_body
        )
        digest = hashlib.sha256(payload).digest()
        candidates = VerifyingKey.from_public_key_recovery_with_digest(
            signature[:64],
            digest,
            curve=NIST256p,
            sigdecode=sigdecode_string,
        )
    except (BadSignatureError, ValueError):
        return False

    normalized_address = address.strip()
    return any(
        _ss58_address_from_compressed_p256_key(
            candidate.to_string(encoding="compressed")
        )
        == normalized_address
        for candidate in candidates
    )

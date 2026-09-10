"""Credential hardening helpers.

Passwords are never persisted in plaintext. We keep only a salted, memory-hard
hash (for the login/cache check), and when a plaintext password must transit the
Celery/Redis broker on its way to the scraper it is encrypted with a shared key.
"""

import hashlib
import hmac
import os
import secrets

# scrypt cost parameters. n must be a power of two.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

_fernet = None


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    # OpenSSL rejects the call unless maxmem covers the working set (~128*n*r*p).
    maxmem = 128 * n * r * p + (1 << 20)
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=maxmem
    )


def hash_password(password: str) -> str:
    """Return a self-describing salted scrypt hash: scrypt$n$r$p$salt$hash."""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time verify against a scrypt hash or a legacy unsalted sha256 hex."""
    if not stored:
        return False

    if "$" not in stored:
        # Legacy format: bare sha256 hex digest written by older versions.
        legacy = hashlib.sha256(password.encode()).hexdigest()
        return hmac.compare_digest(legacy, stored)

    try:
        scheme, n_s, r_s, p_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = _scrypt(
            password,
            bytes.fromhex(salt_hex),
            int(n_s),
            int(r_s),
            int(p_s),
            len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError):
        return False

    return hmac.compare_digest(derived.hex(), hash_hex)


def needs_rehash(stored: str | None) -> bool:
    """True when a verified password should be re-stored in the current format."""
    return not stored or not stored.startswith("scrypt$")


def write_secret_file(path: str, content: str) -> None:
    """Write a secret to disk owner-readable only (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _get_fernet():
    global _fernet
    if _fernet is None:
        key = os.getenv("CRED_ENC_KEY")
        if not key:
            raise RuntimeError(
                "CRED_ENC_KEY is not set. Generate one with "
                "`python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\"` and set the same value "
                "on both the API and the Celery worker."
            )
        from cryptography.fernet import Fernet

        _fernet = Fernet(key.encode())
    return _fernet


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for transit through the broker. Never stored at rest."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()

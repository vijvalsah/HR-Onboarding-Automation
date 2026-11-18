"""Password hashing using bcrypt directly.

We call the `bcrypt` library rather than passlib: passlib is unmaintained and
incompatible with modern bcrypt releases. bcrypt hashes are salted and slow by
design, which is what makes stored passwords resistant to brute force.

bcrypt rejects inputs longer than 72 bytes, so we truncate defensively — the
first 72 bytes still provide ample entropy for real passwords.
"""

import bcrypt

_MAX_BCRYPT_BYTES = 72


def _to_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_MAX_BCRYPT_BYTES]


def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bytes(password), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed stored hash — treat as a failed match rather than raising.
        return False

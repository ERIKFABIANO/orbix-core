import hashlib
import hmac
import secrets


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_bytes(value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).digest()


def sha256_hex(value: str | bytes) -> str:
    return sha256_bytes(value).hex()


def keyed_hash(key: bytes, *parts: str) -> str:
    """HMAC-SHA256 em hex. Usado para não guardar e-mail nem código em claro no Redis."""
    return hmac.new(key, "\x1f".join(parts).encode(), hashlib.sha256).hexdigest()


def numeric_code(digits: int = 6) -> str:
    return f"{secrets.randbelow(10**digits):0{digits}d}"


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())

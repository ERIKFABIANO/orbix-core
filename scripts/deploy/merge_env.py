#!/usr/bin/env python3
"""Roda NA DROPLET. Instala os arquivos de ambiente novos preservando REDIS_PASSWORD e SECRET_KEY."""

import os
import re
import secrets
from pathlib import Path

BASE = Path("/opt/orbix")
NEW = BASE / "incoming"


def existing(file: str, key: str) -> str | None:
    path = BASE / file
    if not path.exists():
        return None
    match = re.search(rf"^{key}=(.+)$", path.read_text(), flags=re.M)
    return match.group(1).strip() if match else None


redis_password = existing("compose.env", "REDIS_PASSWORD") or secrets.token_urlsafe(36)
secret_key = existing("api.env", "SECRET_KEY") or secrets.token_urlsafe(48)
kept = {
    "REDIS_PASSWORD": "reaproveitada" if existing("compose.env", "REDIS_PASSWORD") else "nova",
    "SECRET_KEY": "reaproveitada" if existing("api.env", "SECRET_KEY") else "nova",
}

for name in ("api.env", "worker.env", "compose.env"):
    text = (NEW / name).read_text()
    text = text.replace("{{REDIS_PASSWORD}}", redis_password).replace("{{SECRET_KEY}}", secret_key)
    assert "{{" not in text, name
    target = BASE / name
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(target, 0o600)
    (NEW / name).unlink()

for name in ("docker-compose.yml", "Caddyfile"):
    (BASE / name).write_text((NEW / name).read_text())
    (NEW / name).unlink()
print("ambiente instalado:", kept)

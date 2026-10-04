"""Sobe um Postgres local igual ao schema do Supabase, para os testes de integração.

Uso:
    uv run python scripts/testdb.py up      # cria, carrega o schema e aplica as migrações
    uv run python scripts/testdb.py down    # remove o container

Requer Docker. O banco fica em 127.0.0.1:54329 e não guarda nada entre execuções.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "orbix-testdb"
PORT = "54329"
ADMIN_URL = f"postgresql+asyncpg://postgres:test@127.0.0.1:{PORT}/postgres"


def run(*args: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, input=stdin, capture_output=True, check=check)


def psql(sql_file: Path) -> None:
    result = run(
        "docker",
        "exec",
        "-i",
        NAME,
        "psql",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        stdin=sql_file.read_bytes(),
        check=False,
    )
    if result.returncode != 0:
        sys.exit(f"falha ao carregar {sql_file.name}:\n{result.stderr.decode(errors='replace')[-800:]}")


def down() -> None:
    run("docker", "rm", "-f", NAME, check=False)


def up() -> None:
    down()
    run(
        "docker",
        "run",
        "-d",
        "--name",
        NAME,
        "-e",
        "POSTGRES_PASSWORD=test",
        "-p",
        f"127.0.0.1:{PORT}:5432",
        "--tmpfs",
        "/var/lib/postgresql/data",
        "postgres:17-alpine",
    )
    for _ in range(60):
        ready = run("docker", "exec", NAME, "pg_isready", "-U", "postgres", check=False)
        # o entrypoint reinicia o servidor uma vez; esperar a segunda subida
        logs = run("docker", "logs", NAME, check=False)
        if ready.returncode == 0 and (logs.stdout + logs.stderr).count(b"ready to accept connections") >= 2:
            break
        time.sleep(0.5)
    else:
        sys.exit("Postgres de teste não ficou pronto")

    psql(ROOT / "tests" / "db" / "bootstrap.sql")
    psql(ROOT / "tests" / "db" / "schema_base.sql")
    psql(ROOT / "tests" / "db" / "after_schema.sql")

    env = {**os.environ, "MIGRATION_DATABASE_URL": ADMIN_URL, "MIGRATION_DB_SSL": "false"}
    for command in (["stamp", "0002"], ["upgrade", "head"]):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *command], cwd=ROOT, env=env, capture_output=True, check=False
        )
        if result.returncode != 0:
            sys.exit(f"alembic {' '.join(command)} falhou:\n{result.stderr.decode(errors='replace')[-1500:]}")
    print(f"banco de teste pronto em 127.0.0.1:{PORT}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "up"
    {"up": up, "down": down}.get(action, up)()

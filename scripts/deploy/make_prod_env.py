"""Gera api.env, worker.env e compose.env de produção a partir do .env local. Não imprime valores.

REDIS_PASSWORD e SECRET_KEY ficam como marcadores: o script merge_env.py, na droplet,
reaproveita os que já existem lá (ou gera novos), para não trocar a cada deploy.
"""

import sys
from pathlib import Path

src = Path(__file__).resolve().parents[2] / ".env"
out = Path(sys.argv[1])
image = sys.argv[2]
out.mkdir(parents=True, exist_ok=True)

v = dict(
    l.split("=", 1)
    for l in src.read_text(encoding="utf-8").splitlines()
    if "=" in l and not l.lstrip().startswith("#")
)
v = {k: val.strip() for k, val in v.items()}
DOMAIN = "api-declare.orbixlab.com.br"
APP = "https://declare.orbixlab.com.br"


def db(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


common = {
    "ENV": "prod",
    "DB_SSL": "true",
    "REDIS_URL": "redis://:{{REDIS_PASSWORD}}@redis:6379/0",
    "SECRET_KEY": "{{SECRET_KEY}}",
    "SOLANA_MEMO_RPC_URL": v.get("SOLANA_MEMO_RPC_URL", "https://api.devnet.solana.com"),
    "SOLANA_MEMO_CLUSTER": v.get("SOLANA_MEMO_CLUSTER", "devnet"),
}
api = {
    **common,
    "DATABASE_URL": db(v["DATABASE_URL"]),
    "APP_URL": APP,
    "API_URL": f"https://{DOMAIN}",
    # localhost:3000/3100: o Filipe (e os testes locais do front) rodando contra a API de produção.
    "CORS_ORIGINS": f'["{APP}","http://localhost:3000","http://localhost:3100"]',
    # 127.0.0.1: healthcheck interno do container
    "ALLOWED_HOSTS": f'["{DOMAIN}","127.0.0.1"]',
    "RESEND_API_KEY": v.get("RESEND_API_KEY", ""),
    "EMAIL_FROM": v.get("EMAIL_FROM", ""),
    "GOOGLE_CLIENT_ID": v.get("GOOGLE_CLIENT_ID", ""),
    "GOOGLE_CLIENT_SECRET": v.get("GOOGLE_CLIENT_SECRET", ""),
    "GITHUB_CLIENT_ID": v.get("GITHUB_CLIENT_ID", ""),
    "GITHUB_CLIENT_SECRET": v.get("GITHUB_CLIENT_SECRET", ""),
    "OPENAI_API_KEY": v.get("OPENAI_API_KEY", ""),
    "LLM_MODEL_AGENT": v.get("LLM_MODEL_AGENT", "gpt-5.4"),
    "R2_ENDPOINT": v.get("R2_ENDPOINT", ""),
    "R2_ACCESS_KEY_ID": v.get("R2_ACCESS_KEY_ID", ""),
    "R2_SECRET_ACCESS_KEY": v.get("R2_SECRET_ACCESS_KEY", ""),
    "R2_BUCKET": v.get("R2_BUCKET", "orbixdeclare"),
    "UNKNOWN_COST_POLICY": v.get("UNKNOWN_COST_POLICY", "zero"),
}
worker = {
    **common,
    "DATABASE_URL": db(v["WORKER_DATABASE_URL"]),
    "HELIUS_API_KEY": v.get("HELIUS_API_KEY", ""),
    "COINGECKO_API_KEY": v.get("COINGECKO_API_KEY", ""),
    "SOLANA_MEMO_SECRET_KEY": v.get("SOLANA_MEMO_SECRET_KEY", ""),
}
compose = {"ORBIX_IMAGE": image, "API_DOMAIN": DOMAIN, "REDIS_PASSWORD": "{{REDIS_PASSWORD}}"}


def dump(name: str, data: dict[str, str]) -> None:
    body = "".join(f"{k}={val}\n" for k, val in data.items() if val != "")
    (out / name).write_text(body, encoding="utf-8", newline="\n")


dump("api.env", api)
dump("worker.env", worker)
dump("compose.env", compose)

api_text, worker_text = (out / "api.env").read_text(), (out / "worker.env").read_text()
assert "orbix_api." in api["DATABASE_URL"] and "orbix_worker." in worker["DATABASE_URL"]
# privilégio mínimo: cada container só recebe o que usa
assert "SOLANA_MEMO_SECRET_KEY" not in api_text and "HELIUS_API_KEY" not in api_text
assert (
    "OPENAI_API_KEY" not in worker_text
    and "RESEND_API_KEY" not in worker_text
    and "R2_SECRET" not in worker_text
)
assert "postgres." + v["SUPABASE_PROJECT_REF"] + ":" not in api_text + worker_text  # senha de admin fica fora
print("api.env:", sorted(k for k, val in api.items() if val != ""))
print("worker.env:", sorted(k for k, val in worker.items() if val != ""))

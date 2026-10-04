# Deploy manual para a droplet: build local, envio por SSH e subida dos containers.
# Uso (na raiz do repositório):  .\scripts\deploy\deploy.ps1 -HostIp <ip da droplet>
#
# Antes: rodar os testes, e carregar a chave no agente (ssh-add $env:USERPROFILE\.ssh\orbix_deploy).
# O IP da droplet não fica no repositório; passe por parâmetro.
# Migrações de banco NÃO são aplicadas aqui: rode `uv run alembic upgrade head` antes, se houver.
param(
    [Parameter(Mandatory = $true)][string]$HostIp,
    [string]$User = "erik",
    [string]$Key = "$env:USERPROFILE\.ssh\orbix_deploy"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$stage = Join-Path ([IO.Path]::GetTempPath()) ("orbix-deploy-" + [guid]::NewGuid().ToString("N"))
$tag = "orbix-api:" + (Get-Date -Format "yyyyMMdd-HHmm")
$target = "$User@$HostIp"

try {
    Set-Location $root
    docker build -q -t $tag . | Out-Null
    uv run python scripts\deploy\make_prod_env.py $stage $tag
    Copy-Item infra\docker-compose.yml, infra\Caddyfile, scripts\deploy\merge_env.py, scripts\deploy\remote.sh $stage
    docker save -o (Join-Path $stage "orbix-api.tar") $tag

    ssh -o BatchMode=yes -i $Key $target "mkdir -p /opt/orbix/incoming; chmod 700 /opt/orbix/incoming"
    scp -q -o BatchMode=yes -i $Key (Join-Path $stage "*") "${target}:/opt/orbix/incoming/"
    # arquivos saem do Windows com CRLF; o servidor precisa de LF
    ssh -o BatchMode=yes -i $Key $target "sed -i 's/\r$//' /opt/orbix/incoming/*.sh /opt/orbix/incoming/*.py /opt/orbix/incoming/*.env /opt/orbix/incoming/Caddyfile /opt/orbix/incoming/docker-compose.yml; cp /opt/orbix/incoming/remote.sh /tmp/orbix-remote.sh; bash /tmp/orbix-remote.sh; unlink /tmp/orbix-remote.sh"
}
finally {
    # a pasta temporária tem os arquivos de ambiente: apagar sempre
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
}
Write-Host "Deploy de $tag concluído. Confira com: uv run python scripts\smoke_prod.py <carteira Solana pública>"

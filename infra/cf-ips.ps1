# Restringe 80/443 da droplet aos IPs da Cloudflare e mostra a linha trusted_proxies do Caddyfile.
# Rodar DEPOIS que o domínio estiver com proxy laranja ligado na Cloudflare.
# Uso: .\infra\cf-ips.ps1 [-SshIp 1.2.3.4]
param(
    [string]$FirewallName = "orbix-api-fw",
    [string]$SshIp = (Invoke-RestMethod -Uri https://api.ipify.org -TimeoutSec 10)
)
$ErrorActionPreference = "Stop"

$v4 = (Invoke-RestMethod -Uri https://www.cloudflare.com/ips-v4 -TimeoutSec 10).Trim() -split "\s+"
$v6 = (Invoke-RestMethod -Uri https://www.cloudflare.com/ips-v6 -TimeoutSec 10).Trim() -split "\s+"
$all = $v4 + $v6
if ($v4.Count -lt 10) { throw "lista de IPs da Cloudflare veio incompleta: abortando" }

$addresses = ($all | ForEach-Object { "address:$_" }) -join ","
$inbound = @(
    "protocol:tcp,ports:22,address:$SshIp/32",
    "protocol:tcp,ports:80,$addresses",
    "protocol:tcp,ports:443,$addresses"
) -join " "
$outbound = "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0 protocol:udp,ports:all,address:0.0.0.0/0,address:::/0 protocol:icmp,address:0.0.0.0/0,address:::/0"

$fw = doctl compute firewall list --format ID,Name --no-header | Where-Object { $_ -match "\s$FirewallName$" }
if (-not $fw) { throw "firewall $FirewallName não encontrado" }
$fwId = ($fw -split "\s+")[0]

doctl compute firewall update $fwId --name $FirewallName --tag-names orbix-api `
    --inbound-rules $inbound --outbound-rules $outbound --format Name,Status

Write-Host "`nLinha para o Caddyfile (bloco servers):"
Write-Host ("trusted_proxies static " + ($all -join " "))

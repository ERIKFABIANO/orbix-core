# Kubernetes

Manifests equivalentes ao `infra/docker-compose.yml`. **Não estão em uso**: a produção roda hoje em Docker Compose numa droplet de 1 GB. Os arquivos foram validados contra o schema do Kubernetes (kubeconform), mas nunca aplicados num cluster de verdade.

## O que precisa antes

| Requisito | Motivo |
|---|---|
| Nó com 2 GB de RAM ou mais | O próprio Kubernetes (mesmo o k3s) usa de 500 MB a 1 GB. Na droplet atual não sobra memória para a aplicação |
| Imagem num registry | O cluster puxa a imagem; hoje ela é enviada por SSH. Usar o Container Registry da DigitalOcean |
| CNI com NetworkPolicy | O k3s e o DOKS já aplicam as políticas de rede deste diretório |

## O que os manifests garantem

- Namespace com Pod Security `restricted`: sem root, sem escalada de privilégio, `seccomp` padrão.
- Containers com sistema de arquivos somente leitura e sem capabilities.
- Rede negada por padrão. O Redis só aceita a API e o worker. A API só aceita o controlador de entrada. API e worker só saem para o Redis, DNS, portas 443, 5432 e 6543.
- A chave da carteira de memo e a da Helius existem só no Secret do worker.
- Nenhum pod monta o token da conta de serviço do cluster.

## Subir

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Namespace
metadata: { name: orbix }
EOF

# segredos a partir dos mesmos arquivos usados no compose (nunca commitar)
kubectl -n orbix create secret generic orbix-api    --from-env-file=api.env
kubectl -n orbix create secret generic orbix-worker --from-env-file=worker.env
kubectl -n orbix create secret generic orbix-redis  --from-literal=REDIS_PASSWORD="$(grep ^REDIS_PASSWORD= compose.env | cut -d= -f2-)"
kubectl -n orbix create secret tls orbix-origin-tls --cert=origin.pem --key=origin.key

cd infra/k8s
kustomize edit set image orbix-api=registry.digitalocean.com/<registry>/orbix-api:<tag>
kubectl apply -k .
kubectl -n orbix rollout status deploy/api
```

## Diferenças em relação ao compose

- O Caddy sai: a entrada é o Ingress do cluster. Os Authenticated Origin Pulls da Cloudflare (exigir o certificado de cliente dela) precisam ser configurados no controlador de entrada. No Traefik isso é um `TLSOption` com `clientAuth`.
- O IP real do visitante chega pelo cabeçalho `CF-Connecting-IP`. O controlador precisa confiar nos IPs da Cloudflare e repassar em `X-Forwarded-For`, senão o rate limit por IP enxerga só o IP do proxy.
- O firewall da DigitalOcean continua valendo no nó: portas 80 e 443 só para os IPs da Cloudflare.

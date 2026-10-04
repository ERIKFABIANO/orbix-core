"""Teste de ponta a ponta contra a API em produção, com conta descartável (apagada no fim)."""

import hashlib
import sys
import time

import httpx
from solders.keypair import Keypair

sys.stdout.reconfigure(encoding="utf-8")
API = "https://api-declare.orbixlab.com.br"
SOLANA_PUBLIC_WALLET = sys.argv[1]
HL_WALLET = sys.argv[2] if len(sys.argv) > 2 else None
ORIGIN = {"Origin": "https://declare.orbixlab.com.br", "Accept-Language": "pt-BR"}

ok_count = fail_count = 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global ok_count, fail_count
    if condition:
        ok_count += 1
    else:
        fail_count += 1
    print(f"  [{'ok' if condition else 'FALHOU'}] {label} {detail if detail != '' else ''}")


with httpx.Client(base_url=API, timeout=60, headers=ORIGIN) as c:
    print("== básico ==")
    r = c.get("/health")
    check("health", r.status_code == 200 and r.json() == {"status": "ok"})
    providers = c.get("/api/auth/providers").json()
    check("providers", providers.get("wallet") is True, providers)
    check("rota protegida sem token", c.get("/api/me").status_code == 401)

    print("== login com carteira ==")
    kp = Keypair()
    address = str(kp.pubkey())
    nonce = c.get("/api/auth/nonce", params={"address": address})
    check("nonce", nonce.status_code == 200)
    message = nonce.json()["message"]
    check(
        "mensagem SIWS com o domínio do front",
        message.startswith("declare.orbixlab.com.br wants you to sign in"),
    )
    session = c.post(
        "/api/auth/verify",
        json={"address": address, "message": message, "signature": str(kp.sign_message(message.encode()))},
    )
    check("verify", session.status_code == 200, session.text[:150] if session.status_code != 200 else "")
    token = session.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    check(
        "CORS liberado para o front",
        session.headers.get("access-control-allow-origin") == "https://declare.orbixlab.com.br",
    )
    replay = c.post(
        "/api/auth/verify",
        json={"address": address, "message": message, "signature": str(kp.sign_message(message.encode()))},
    )
    check("replay do nonce recusado", replay.status_code == 401)
    me = c.get("/api/me", headers=auth).json()
    check(
        "me",
        me["address"] == address and me["loginMethods"] == ["wallet"] and me["onboarded"] is False,
        {k: me[k] for k in ("plan", "agentQuestionsLeft", "hasWallets")},
    )

    try:
        print("== carteiras e sincronização ==")
        added = c.post(
            "/api/wallets",
            json={"network": "solana", "address": SOLANA_PUBLIC_WALLET, "label": "Teste"},
            headers=auth,
        )
        check(
            "adicionar carteira Solana pública",
            added.status_code == 200,
            added.text[:150] if added.status_code != 200 else added.json()["status"],
        )
        if HL_WALLET:
            hl = c.post("/api/wallets", json={"network": "hyperliquid", "address": HL_WALLET}, headers=auth)
            check(
                "adicionar carteira Hyperliquid",
                hl.status_code == 200,
                hl.text[:150] if hl.status_code != 200 else "",
            )
        started = c.post("/api/ingest", headers=auth)
        check(
            "iniciar sincronização",
            started.status_code == 200 and started.json()["state"] == "running",
            started.json().get("state"),
        )
        t0 = time.time()
        status = started.json()
        while time.time() - t0 < 240 and status["state"] == "running":
            time.sleep(3)
            status = c.get("/api/ingest/status", headers=auth).json()
        print(f"     estado final: {status['state']} em {time.time() - t0:.0f}s | lidas: {status['read']}")
        for w in status["wallets"]:
            print(f"     {w['network']:<12} {w['state']:<8} lidas={w['read']} erro={w.get('error')}")
        print("     passos:", {s["key"]: s["state"] for s in status["steps"]})
        check("sincronização concluída", status["state"] == "done")
        check("onboarded depois da sincronização", c.get("/api/me", headers=auth).json()["onboarded"] is True)

        print("== painel, eventos e relatórios ==")
        dash = c.get("/api/dashboard", headers=auth).json()
        check(
            "dashboard",
            "volumeBrl" in dash,
            {
                k: dash.get(k)
                for k in (
                    "month",
                    "volumeBrl",
                    "disposals",
                    "capitalGainBrl",
                    "estimatedTaxBrl",
                    "missingPrices",
                )
            },
        )
        reports = c.get("/api/reports", headers=auth).json()
        check(
            "lista de relatórios",
            isinstance(reports, list) and len(reports) > 0,
            [(r["month"], r["status"], r["events"]) for r in reports[:4]],
        )
        closed = next(
            (r["month"] for r in reports if r["month"] < time.strftime("%Y-%m") and r["events"] > 0), None
        )
        month = closed or dash["month"]
        events = c.get("/api/events", params={"month": month}, headers=auth).json()
        check(
            f"eventos de {month}",
            isinstance(events, list) and len(events) > 0,
            f"{len(events)} eventos; ex.: {events[0]['asset'][:30]} valor={events[0]['valueBrl']}"
            if events
            else "",
        )
        detail = c.get(f"/api/report/{month}", headers=auth).json()
        check("detalhe do relatório (rascunho)", detail.get("status") == "draft", detail.get("totals"))
        csv_link = c.get(f"/api/report/{month}/csv", headers=auth).json()
        check(
            "CSV de rascunho embutido",
            csv_link.get("url", "").startswith("data:text/csv"),
            csv_link.get("filename"),
        )

        print("== agente (OpenAI) ==")
        reply = c.post(
            "/api/agent",
            json={"message": "Resuma em duas frases o resultado deste mês.", "month": month},
            headers=auth,
        )
        check(
            "agente respondeu", reply.status_code == 200, reply.text[:200] if reply.status_code != 200 else ""
        )
        if reply.status_code == 200:
            body = reply.json()
            text_block = body["message"]["blocks"][0]["text"]
            print("     resposta:", text_block[:260].replace("\n", " "))
            print(
                "     blocos:",
                [b["type"] for b in body["message"]["blocks"]],
                "| perguntas restantes:",
                body["questionsLeft"],
            )

        print("== finalizar relatório (R2) ==")
        if closed:
            missing = (
                detail
                and c.get("/api/dashboard", params={"month": month}, headers=auth).json()["missingPrices"]
            )
            fin = c.post(f"/api/report/{month}/decripto", headers=auth)
            if fin.status_code == 409:
                print("     recusado:", fin.json()["error"]["code"], "-", fin.json()["error"]["message"])
                check(
                    "recusa coerente (eventos sem preço)",
                    fin.json()["error"]["code"] in ("missing_prices", "nothing_to_report")
                    and (missing or 0) >= 0,
                )
            else:
                check(
                    "finalização",
                    fin.status_code == 200,
                    fin.text[:200] if fin.status_code != 200 else fin.json()["filename"],
                )
                if fin.status_code == 200:
                    dec = httpx.get(fin.json()["url"], timeout=30)
                    check(
                        "download do DeCripto pelo link assinado do R2",
                        dec.status_code == 200 and b"ORBIX DECLARE" in dec.content,
                        f"{len(dec.content)} bytes",
                    )
                    csv2 = c.get(f"/api/report/{month}/csv", headers=auth).json()
                    got = httpx.get(csv2["url"], timeout=30)
                    last = got.content.decode().splitlines()[-1]
                    check(
                        "CSV final no R2 com linha de verificação",
                        got.status_code == 200
                        and last.startswith("verificacao,")
                        and len(last.split(",")[1]) == 64,
                    )
                    print("     sha256 do CSV baixado:", hashlib.sha256(got.content).hexdigest()[:16], "…")
                    check(
                        "link do R2 sem assinatura é recusado",
                        httpx.get(csv2["url"].split("?")[0], timeout=30).status_code in (400, 401, 403),
                    )
                    final = c.get(f"/api/report/{month}", headers=auth).json()
                    check(
                        "relatório ficou final",
                        final["status"] == "final",
                        f"atestação: {final['attestation']}",
                    )
        else:
            print("     sem mês fechado com eventos nesta carteira; finalização não testada")
    finally:
        print("== limpeza ==")
        deleted = c.delete("/api/me", headers=auth)
        check("conta de teste apagada", deleted.status_code == 204)
        check("token não vale mais", c.get("/api/me", headers=auth).status_code == 401)

print(f"\nRESULTADO: {ok_count} ok, {fail_count} falhas")

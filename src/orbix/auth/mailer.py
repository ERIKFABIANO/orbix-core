"""Envio do código de login por e-mail (Resend) e o template da mensagem."""

from html import escape
from typing import Protocol

import httpx
import structlog

from orbix.config import Settings
from orbix.errors import AppError
from orbix.i18n import Locale

log = structlog.get_logger()

RESEND_URL = "https://api.resend.com/emails"


class Mailer(Protocol):
    async def send_login_code(self, to: str, code: str, locale: Locale) -> None: ...


TEXTS: dict[Locale, dict[str, str]] = {
    "pt": {
        "subject": "{code} é o seu código do Orbix Declare",
        "preheader": "Use este código para entrar. Ele vale por 10 minutos.",
        "title": "Seu código de acesso",
        "intro": "Digite este código na tela de login para entrar no Orbix Declare.",
        "validity": "O código vale por 10 minutos e só funciona uma vez.",
        "ignore": "Se você não pediu este código, ignore este e-mail. Ninguém entra na sua conta sem ele.",
        "never": "O Orbix Declare nunca pede sua chave privada, sua frase de recuperação nem assinatura de transação.",
        "footer": "Orbix Lab · declare.orbixlab.com.br",
    },
    "en": {
        "subject": "{code} is your Orbix Declare code",
        "preheader": "Use this code to sign in. It is valid for 10 minutes.",
        "title": "Your access code",
        "intro": "Enter this code on the sign-in screen to access Orbix Declare.",
        "validity": "The code is valid for 10 minutes and works only once.",
        "ignore": "If you didn't request this code, ignore this email. No one can access your account without it.",
        "never": "Orbix Declare never asks for your private key, recovery phrase or a transaction signature.",
        "footer": "Orbix Lab · declare.orbixlab.com.br",
    },
}


def render_login_code(code: str, locale: Locale) -> tuple[str, str, str]:
    """(assunto, html, texto). Layout em tabela com estilos inline, como os clientes de e-mail exigem."""
    t = TEXTS[locale]
    spaced = f"{code[:3]} {code[3:]}"
    subject = t["subject"].format(code=code)
    text = "\n\n".join([t["title"], spaced, t["intro"], t["validity"], t["ignore"], t["never"], t["footer"]])
    html = f"""<!doctype html>
<html lang="{"en" if locale == "en" else "pt-BR"}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{escape(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#f4f3f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#17171a;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(t["preheader"])}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f3f8;">
<tr><td align="center" style="padding:32px 16px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border-radius:16px;border:1px solid #e4e2ee;">
    <tr><td style="padding:28px 32px 0 32px;font-size:15px;font-weight:700;letter-spacing:-0.01em;color:#17171a;">
      orbix<span style="color:#6b3cff;">.</span> declare
    </td></tr>
    <tr><td style="padding:24px 32px 0 32px;">
      <h1 style="margin:0;font-size:22px;line-height:1.25;letter-spacing:-0.02em;color:#17171a;">{escape(t["title"])}</h1>
      <p style="margin:12px 0 0 0;font-size:15px;line-height:1.6;color:#4a4a55;">{escape(t["intro"])}</p>
    </td></tr>
    <tr><td align="center" style="padding:24px 32px 0 32px;">
      <div style="display:inline-block;padding:16px 28px;border-radius:12px;background:#f1edff;border:1px solid #d9cfff;font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;font-size:34px;font-weight:700;letter-spacing:0.12em;color:#2a1a66;">{escape(spaced)}</div>
    </td></tr>
    <tr><td style="padding:20px 32px 0 32px;">
      <p style="margin:0;font-size:14px;line-height:1.6;color:#4a4a55;">{escape(t["validity"])}</p>
      <p style="margin:12px 0 0 0;font-size:14px;line-height:1.6;color:#4a4a55;">{escape(t["ignore"])}</p>
    </td></tr>
    <tr><td style="padding:20px 32px 0 32px;">
      <div style="padding:12px 14px;border-radius:10px;background:#f7f7f5;border-left:3px solid #6b3cff;font-size:13px;line-height:1.55;color:#4a4a55;">{escape(t["never"])}</div>
    </td></tr>
    <tr><td style="padding:24px 32px 28px 32px;font-size:12px;color:#8a8a96;">{escape(t["footer"])}</td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""
    return subject, html, text


class ResendMailer:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def send_login_code(self, to: str, code: str, locale: Locale) -> None:
        key = self._settings.resend_api_key
        if key is None:
            if self._settings.is_prod:
                raise AppError("email_unavailable", 503)
            # só fora de produção: sem chave do Resend, o código vai para o log do servidor
            log.warning("codigo de login (dev, sem Resend)", code=code)
            return
        subject, html, text = render_login_code(code, locale)
        try:
            response = await self._client.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                json={
                    "from": self._settings.email_from,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                    "text": text,
                },
                timeout=10,
            )
        except httpx.HTTPError:
            raise AppError("email_unavailable", 503) from None
        if response.status_code >= 400:
            log.error("resend recusou o envio", status=response.status_code)
            raise AppError("email_unavailable", 503)

"""Idioma da resposta (pt ou en) e catálogo de textos mostrados ao usuário."""

from contextvars import ContextVar
from typing import Literal

Locale = Literal["pt", "en"]

_locale: ContextVar[Locale] = ContextVar("locale", default="pt")


def locale_from_header(value: str | None) -> Locale:
    first = (value or "").split(",")[0].strip().lower()
    return "en" if first.startswith("en") else "pt"


def set_locale(locale: Locale) -> None:
    _locale.set(locale)


def get_locale() -> Locale:
    return _locale.get()


def tr(pt: str, en: str) -> str:
    return en if get_locale() == "en" else pt


# code -> (português, inglês). `{nome}` é preenchido com os parâmetros do erro.
ERRORS: dict[str, tuple[str, str]] = {
    "unauthorized": ("Sua sessão expirou. Entre de novo.", "Your session expired. Sign in again."),
    "not_found": ("Não encontramos o que você procura.", "We couldn't find what you're looking for."),
    "invalid_request": (
        "Algum dado enviado está incorreto. Confira e tente de novo.",
        "Some of the data sent is incorrect. Check it and try again.",
    ),
    "rate_limited": (
        "Muitas tentativas. Aguarde {seconds} segundos e tente de novo.",
        "Too many attempts. Wait {seconds} seconds and try again.",
    ),
    "internal": (
        "Algo deu errado do nosso lado. Tente de novo em instantes.",
        "Something went wrong on our side. Try again in a moment.",
    ),
    # login com carteira
    "invalid_address": (
        "Endereço inválido. Confira e tente de novo.",
        "Invalid address. Check it and try again.",
    ),
    "nonce_expired": (
        "O pedido de login expirou. Tente entrar de novo.",
        "The sign-in request expired. Try signing in again.",
    ),
    "invalid_signature": (
        "Não conseguimos confirmar a assinatura da carteira. Tente de novo.",
        "We couldn't confirm the wallet signature. Try again.",
    ),
    # login por e-mail
    "invalid_email": ("E-mail inválido.", "Invalid email."),
    "invalid_code": ("Código incorreto. Confira e tente de novo.", "Wrong code. Check it and try again."),
    "code_expired": (
        "Este código venceu ou já foi usado. Peça um novo.",
        "This code expired or was already used. Request a new one.",
    ),
    "too_many_attempts": (
        "Muitas tentativas erradas. Peça um novo código.",
        "Too many wrong attempts. Request a new code.",
    ),
    "email_unavailable": (
        "Não conseguimos enviar o e-mail agora. Tente de novo em instantes.",
        "We couldn't send the email right now. Try again in a moment.",
    ),
    # login social
    "provider_unavailable": (
        "Esta forma de login não está disponível no momento.",
        "This sign-in method is not available right now.",
    ),
    "oauth_failed": (
        "O login não foi concluído. Tente de novo.",
        "The sign-in was not completed. Try again.",
    ),
    "oauth_state": (
        "A tentativa de login expirou. Tente de novo.",
        "The sign-in attempt expired. Try again.",
    ),
    "email_not_verified": (
        "Sua conta nesse serviço não tem e-mail confirmado. Confirme o e-mail lá e tente de novo.",
        "Your account on that service has no confirmed email. Confirm it there and try again.",
    ),
    "identity_in_use": (
        "Esta forma de login já pertence a outra conta.",
        "This sign-in method already belongs to another account.",
    ),
    # carteiras
    "duplicate_wallet": ("Esta carteira já está conectada.", "This wallet is already connected."),
    "plan_limit": (
        "O plano grátis permite até {limit} carteiras. Remova uma ou mude de plano.",
        "The free plan allows up to {limit} wallets. Remove one or upgrade.",
    ),
    "login_wallet": (
        "A carteira de login não pode ser removida.",
        "The sign-in wallet can't be removed.",
    ),
    "sync_in_progress": (
        "Esta carteira está sendo lida agora. Aguarde terminar.",
        "This wallet is being read right now. Wait for it to finish.",
    ),
    # eventos e relatórios
    "invalid_month": ("Mês inválido. Use o formato AAAA-MM.", "Invalid month. Use the YYYY-MM format."),
    "invalid_price": ("Informe um preço maior que zero.", "Enter a price greater than zero."),
    "review_incomplete": (
        "Para registrar a revisão, informe o motivo, a evidência e confirme.",
        "To record the review, enter the reason, the evidence and confirm.",
    ),
    "month_open": (
        "Este mês ainda não terminou. O relatório só pode ser finalizado depois do último dia.",
        "This month isn't over yet. The report can only be finalized after its last day.",
    ),
    "missing_prices": (
        "Há {count} evento(s) sem preço neste mês. Informe o preço antes de finalizar.",
        "There are {count} event(s) without a price this month. Enter the price before finalizing.",
    ),
    "report_not_final": (
        "Este relatório ainda não foi finalizado. Finalize antes de gerar uma nova versão.",
        "This report has not been finalized yet. Finalize it before generating a new version.",
    ),
    "attestation_pending": (
        "O registro da versão atual na Solana ainda não foi confirmado. Tente de novo em instantes.",
        "The current version is still being recorded on Solana. Try again in a moment.",
    ),
    "report_up_to_date": (
        "Os números do mês não mudaram desde a finalização. Não há o que atualizar.",
        "This month's numbers have not changed since the report was finalized. Nothing to update.",
    ),
    "nothing_to_report": (
        "Não há alienações neste mês para declarar.",
        "There are no disposals to report this month.",
    ),
    "storage_unavailable": (
        "Não conseguimos guardar o arquivo agora. Tente de novo em instantes.",
        "We couldn't store the file right now. Try again in a moment.",
    ),
    # agente
    "quota": (
        "Você usou todas as perguntas deste mês. A cota renova no dia 1º.",
        "You've used all of this month's questions. The quota renews on the 1st.",
    ),
    "agent_unavailable": (
        "O agente está indisponível no momento. Tente de novo em instantes.",
        "The agent is unavailable right now. Try again in a moment.",
    ),
}

# erros guardados em wallets.sync_error (código) e traduzidos na resposta
SYNC_ERRORS: dict[str, tuple[str, str]] = {
    "source_unavailable": (
        "A fonte de dados não respondeu. Tente sincronizar de novo.",
        "The data source didn't respond. Try syncing again.",
    ),
    "source_rejected": (
        "A fonte de dados recusou a leitura desta carteira.",
        "The data source refused to read this wallet.",
    ),
    "not_configured": (
        "A leitura desta rede ainda não está configurada no servidor.",
        "Reading this network is not configured on the server yet.",
    ),
    "unexpected": (
        "A leitura falhou por um erro inesperado. Tente de novo.",
        "The read failed with an unexpected error. Try again.",
    ),
}


def error_message(code: str, **params: object) -> str:
    pt, en = ERRORS.get(code, ERRORS["internal"])
    try:
        return (en if get_locale() == "en" else pt).format(**params)
    except (KeyError, IndexError):
        return en if get_locale() == "en" else pt


def sync_error_message(code: str | None) -> str | None:
    if not code:
        return None
    pt, en = SYNC_ERRORS.get(code, SYNC_ERRORS["unexpected"])
    return en if get_locale() == "en" else pt

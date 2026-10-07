"""Formatos de entrada e saída da API. Espelham `src/lib/api/types.ts` do front."""

import datetime as dt
import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from pydantic.alias_generators import to_camel
from solders.pubkey import Pubkey

Network = Literal["solana", "hyperliquid"]
LoginMethod = Literal["wallet", "email", "google", "github"]
Plan = Literal["free", "pro", "accountant"]
WalletStatus = Literal["synced", "syncing", "error", "empty", "pending"]
StepState = Literal["done", "running", "pending", "error"]
EventType = Literal["swap", "perp", "funding", "transfer"]
ReportStatus = Literal["draft", "final"]

HYPERLIQUID_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def is_solana_address(value: str) -> bool:
    # não checar a curva: endereços de programa (PDA) são válidos e ficam fora dela
    if not 32 <= len(value) <= 44:
        return False
    try:
        return str(Pubkey.from_string(value)) == value
    except Exception:
        return False


def is_hyperliquid_address(value: str) -> bool:
    return HYPERLIQUID_RE.fullmatch(value) is not None


def _solana_address(value: str) -> str:
    if not is_solana_address(value):
        raise ValueError("endereço Solana inválido")
    return value


SolanaAddress = Annotated[str, StringConstraints(max_length=44), AfterValidator(_solana_address)]


class In(BaseModel):
    """Entrada: campos desconhecidos são recusados."""

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, alias_generator=to_camel, populate_by_name=True
    )


class Out(BaseModel):
    """Saída: serializada em camelCase."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ── autenticação ──────────────────────────────────────────────────────────


class NonceOut(Out):
    message: str
    nonce: str
    expires_at: datetime


class VerifyIn(In):
    address: Annotated[str, StringConstraints(max_length=44)]
    message: Annotated[str, StringConstraints(max_length=2000)]
    signature: Annotated[str, StringConstraints(max_length=128)]


class EmailStartIn(In):
    email: EmailStr


class EmailStartOut(Out):
    sent: bool = True
    expires_at: datetime
    resend_after: int


class EmailVerifyIn(In):
    email: EmailStr
    code: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class OAuthStartOut(Out):
    url: str


class ExchangeIn(In):
    code: Annotated[str, StringConstraints(min_length=20, max_length=200)]


class UserOut(Out):
    id: str
    address: str | None
    email: str | None
    display_name: str | None
    login_methods: list[LoginMethod]
    has_wallets: bool
    plan: Plan
    agent_questions_left: int
    onboarded: bool


class SessionOut(Out):
    token: str
    expires_at: datetime
    user: UserOut


# ── carteiras ─────────────────────────────────────────────────────────────


class WalletIn(In):
    network: Network
    address: Annotated[str, StringConstraints(max_length=64)]
    label: Annotated[str, StringConstraints(max_length=40)] | None = None


class WalletOut(Out):
    id: str
    network: Network
    address: str
    label: str
    is_login: bool
    verified_at: datetime | None
    last_sync_at: datetime | None
    status: WalletStatus
    error: str | None = None


# ── sincronização ─────────────────────────────────────────────────────────


class SyncWalletOut(Out):
    wallet_id: str
    network: Network
    address: str
    read: int
    total: int | None
    state: StepState
    detail: str | None = None
    error: str | None = None


class SyncStepOut(Out):
    key: str
    label: str
    state: StepState


class SyncStatusOut(Out):
    state: Literal["idle", "running", "done", "error"]
    since: str
    read: int
    estimated: int | None
    wallets: list[SyncWalletOut]
    steps: list[SyncStepOut]


# ── painel e eventos ──────────────────────────────────────────────────────


class DashboardOut(Out):
    month: str
    updated_at: datetime
    volume_brl: float
    disposals: int
    capital_gain_brl: float
    gain_change_pct: float | None
    estimated_tax_brl: float
    exemption_limit_brl: float
    # null enquanto houver evento sem preço: o front não deduz isenção sozinho
    exemption_status: Literal["exempt", "taxable"] | None = None
    missing_prices: int


class WalletRefOut(Out):
    address: str
    label: str | None


class EventReviewOut(Out):
    # "price": preço corrigido; "cost": custo de aquisição informado
    kind: Literal["price", "cost"] = "price"
    reason: str
    evidence: str
    previous_price_brl: float | None
    new_price_brl: float
    previous_cost_brl: float | None = None
    new_cost_brl: float | None = None
    created_at: datetime


class TaxEventOut(Out):
    id: str
    date: datetime
    network: Network
    type: EventType
    asset: str
    quantity: float
    quantity_asset: str
    value_brl: float | None
    price_source: Literal["auto", "manual"] | None
    tx_hash: str
    explorer_url: str
    # detalhes para a revisão do evento (null quando não se aplica)
    wallet: WalletRefOut | None = None
    protocol: str | None = None
    unit_price_brl: float | None = None
    price_provider: str | None = None
    ptax: float | None = None
    # o campo `date` acima sombreia o tipo: usar o nome qualificado
    ptax_date: dt.date | None = None
    price_observed_at: datetime | None = None
    rule_version: str | None = None
    fees_brl: float | None = None
    cost_brl: float | None = None
    gain_brl: float | None = None
    # quantos fills da corretora formam o evento (uma ordem pode executar em vários)
    fill_count: int = 1
    pending_reasons: list[str] = []
    review_history: list[EventReviewOut] = []


class ManualCostIn(In):
    # custo total de aquisição da quantidade vendida, em reais
    cost_brl: float = Field(ge=0, lt=1e12)
    reason: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    evidence: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    confirmed: Literal[True]


class ManualPriceIn(In):
    unit_price_brl: float = Field(gt=0, lt=1e12)
    # revisão auditável: motivo e evidência ficam registrados em event_reviews
    reason: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None
    evidence: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    confirmed: Literal[True] | None = None


# ── relatórios ────────────────────────────────────────────────────────────


class ReportSummaryOut(Out):
    month: str
    status: ReportStatus
    events: int
    total_brl: float
    updated_at: datetime


class ReportRowOut(Out):
    id: str
    date: datetime
    type: EventType
    asset: str
    quantity: float
    ptax: float
    value_brl: float
    cost_brl: float
    gain_brl: float
    manual_price: bool = False
    # o ativo vendido entrou antes do histórico lido; o custo foi tratado como zero
    cost_unknown: bool = False
    # o custo de aquisição desta venda foi informado pelo usuário
    cost_manual: bool = False


class ReportTotalsOut(Out):
    disposed_brl: float
    cost_brl: float
    gain_brl: float
    tax_brl: float


class AttestationOut(Out):
    hash: str
    tx_signature: str
    slot: int
    registered_at: datetime
    public_id: str


class CoverageOut(Out):
    state: Literal["complete", "partial", "unknown"]
    imported_from: date | None
    imported_through: date | None
    imported_events: int | None


class ReportReviewItemOut(Out):
    id: str
    kind: Literal["acquisition_cost", "classification"]
    label: str


class ReportReviewOut(Out):
    engine_version: str | None
    coverage: CoverageOut
    limitations: list[str]
    pending_reasons: list[str]
    unsupported_operations: list[str]
    review_items: list[ReportReviewItemOut]
    # true só quando o arquivo DeCripto seguir o leiaute oficial e não houver pendência
    decripto_ready: bool


class ReportDetailOut(Out):
    month: str
    status: ReportStatus
    totals: ReportTotalsOut
    rows: list[ReportRowOut]
    attestation: AttestationOut | None
    review: ReportReviewOut | None = None


class DownloadLinkOut(Out):
    url: str
    filename: str
    expires_at: datetime


class PublicVerificationOut(Out):
    public_id: str
    description: str
    month: str
    hash: str
    tx_signature: str
    slot: int
    registered_at: datetime
    valid: bool


# ── agente ────────────────────────────────────────────────────────────────


class AgentIn(In):
    message: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    month: Annotated[str, StringConstraints(pattern=MONTH_RE.pattern)] | None = None
    conversation_id: Annotated[str, StringConstraints(max_length=64)] | None = None


class AgentMessageOut(Out):
    id: str
    role: Literal["user", "assistant"]
    blocks: list[dict[str, Any]]
    created_at: datetime


class AgentSourceTxOut(Out):
    title: str
    signature: str
    date: datetime
    slot: int


class AgentContextOut(Out):
    month: str
    status: ReportStatus
    volume_brl: float
    gain_brl: float
    tax_brl: float
    tax_rate_pct: float
    source_tx: AgentSourceTxOut | None


class AgentReplyOut(Out):
    conversation_id: str
    message: AgentMessageOut
    context: AgentContextOut | None
    suggestions: list[str]
    questions_left: int

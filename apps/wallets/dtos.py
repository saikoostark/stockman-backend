from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FundingCommand:
    currency: str
    amount: str
    reason: str = ""
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class ReversalCommand:
    journal_id: str
    reason: str
    request_id: str = ""


@dataclass(frozen=True, slots=True)
class WalletDTO:
    currency: str
    available: str
    reserved: str


@dataclass(frozen=True, slots=True)
class EntryDTO:
    account: str
    amount: str


@dataclass(frozen=True, slots=True)
class JournalDTO:
    id: str
    currency: str
    kind: str
    reference: str
    reversal_of: str | None
    created_at: str
    posted_at: str | None
    reason: str
    entries: tuple[EntryDTO, ...]


@dataclass(frozen=True, slots=True)
class PostingLine:
    account: str
    units: int


@dataclass(frozen=True, slots=True)
class PostingCommand:
    kind: str
    lines: tuple[PostingLine, ...]
    reference: str = ""
    reason: str = ""
    reversal_of: str | None = None
    request_id: str = ""

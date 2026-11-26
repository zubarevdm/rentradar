"""Revision of the saved search card and the durable claim on its enrichment."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import blake2s
from json import dumps

from .orm import ListingRow


@dataclass(frozen=True)
class DetailClaim:
    external_id: str
    revision: str
    attempts: int
    until: datetime


def detail_revision(row: ListingRow) -> str:
    values = (
        row.url, row.price, row.description, row.photos, row.deposit_rub,
        row.commission_rub, row.commission_pct, row.deposit_due_rub, row.deposit_installments,
        row.upfront_payments, row.utilities_rub, row.utilities_included, row.meters_included,
    )
    return blake2s(dumps(values, sort_keys=True).encode(), digest_size=16).hexdigest()

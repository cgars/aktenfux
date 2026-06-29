"""Pydantic schemas for LLM responses and sidecar JSON documents."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# Maximum characters for the auto-filled short description / summary_short.
# Also used by llm.py as the last-resort fallback length.
DESCRIPTION_SHORT_MAX_CHARS = 120

# ---------------------------------------------------------------------------
# Coercion helpers shared across models
# ---------------------------------------------------------------------------

# Strings a German (or other) LLM might use to express "no value".
_NONE_LIKE_VALUES: frozenset[str] = frozenset({
    "", "null", "none", "n/a", "na", "-", "—", "unbekannt",
    "keine", "keine angabe", "nicht angegeben", "nicht bekannt",
    "unknown", "not available",
})

# Valid DocumentType literals (kept in sync with the Literal below).
_VALID_DOCUMENT_TYPES: frozenset[str] = frozenset({
    "Invoice", "Contract", "Notice", "Policy", "BankStatement",
    "Letter", "Receipt", "Manual", "Other",
})

# German / common aliases → canonical English DocumentType.
_DOCUMENT_TYPE_ALIASES: dict[str, str] = {
    "rechnung": "Invoice",
    "faktura": "Invoice",
    "vertrag": "Contract",
    "rahmenvertrag": "Contract",
    "dienstleistungsvertrag": "Contract",
    "bescheid": "Notice",
    "schreiben": "Letter",
    "brief": "Letter",
    "mitteilung": "Letter",
    "police": "Policy",
    "versicherungspolice": "Policy",
    "kontoauszug": "BankStatement",
    "kontoübersicht": "BankStatement",
    "quittung": "Receipt",
    "kassenbon": "Receipt",
    "kassenzettel": "Receipt",
    "beleg": "Receipt",
    "anleitung": "Manual",
    "handbuch": "Manual",
    "bedienungsanleitung": "Manual",
    "gebrauchsanweisung": "Manual",
    "sonstiges": "Other",
    "anderes": "Other",
}


# ---------------------------------------------------------------------------
# Document type and category enums
# ---------------------------------------------------------------------------

DocumentType = Literal[
    "Invoice",
    "Contract",
    "Notice",
    "Policy",
    "BankStatement",
    "Letter",
    "Receipt",
    "Manual",
    "Other",
]


# ---------------------------------------------------------------------------
# Document integrity / multi-document scan assessment
# ---------------------------------------------------------------------------


RecommendedIntegrityAction = Literal["none", "run_split_detection", "manual_review"]


class DocumentIntegrity(BaseModel):
    """Assessment of whether a PDF may contain multiple logical documents."""

    possible_multi_document_scan: bool
    suspected_document_count: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    recommended_action: RecommendedIntegrityAction


def default_document_integrity() -> DocumentIntegrity:
    """Return the default single-document integrity assessment for sidecars."""

    return DocumentIntegrity(
        possible_multi_document_scan=False,
        suspected_document_count=1,
        confidence=0.0,
        reason="No document integrity assessment was provided.",
        recommended_action="none",
    )


# ---------------------------------------------------------------------------
# Amount sub-model
# ---------------------------------------------------------------------------


class Amount(BaseModel):
    label: str
    amount: float
    currency: str = "EUR"

    @field_validator("amount", mode="before")
    @classmethod
    def parse_german_number(cls, v: Any) -> Any:
        """Accept German-style number strings such as '1.500,00' or '500,99'.

        German convention: dot = thousands separator, comma = decimal separator.
        When both separators appear the dot is treated as the thousands separator
        and the comma as the decimal separator (e.g. "1.500,00" → 1500.0).
        When only a comma appears it is treated as the decimal separator.
        Currency symbols and surrounding whitespace are stripped before parsing.
        Note: when both separators are present this logic always assumes German
        format; English format "1,500.00" yields the same numeric result (1500.0).
        """
        if not isinstance(v, str):
            return v
        # Strip currency symbols, letters, and surrounding whitespace.
        # Hyphen at the start of the character class ensures it is treated as a
        # literal rather than a range indicator.
        cleaned = re.sub(r"[^-\d,.]", "", v.strip())
        if not cleaned:
            return 0.0
        # German format: "1.500,00" – dot is thousands separator, comma is decimal.
        if "," in cleaned and "." in cleaned:
            # Decide format by last separator: 1.500,00 (de) vs 1,500.00 (en)
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            # Comma as decimal separator (e.g. "500,00").
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0


# ---------------------------------------------------------------------------
# Entities sub-model
# ---------------------------------------------------------------------------


class Entities(BaseModel):
    people: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    contract_numbers: list[str] = Field(default_factory=list)
    invoice_numbers: list[str] = Field(default_factory=list)
    customer_numbers: list[str] = Field(default_factory=list)

    @field_validator(
        "people", "organizations", "addresses",
        "contract_numbers", "invoice_numbers", "customer_numbers",
        mode="before",
    )
    @classmethod
    def coerce_entity_list(cls, v: Any) -> Any:
        """Accept a comma/newline-separated string in place of a list."""
        if isinstance(v, str):
            if not v.strip():
                return []
            return [item.strip() for item in re.split(r"[,\n]", v) if item.strip()]
        if not isinstance(v, list):
            return []
        return v


# ---------------------------------------------------------------------------
# LLM analysis result (what the LLM returns)
# ---------------------------------------------------------------------------


class DocumentAnalysis(BaseModel):
    """Validated LLM response for a single document."""

    document_date: str | None = None
    correspondent: str | None = None
    document_type: DocumentType = "Other"
    topic: str = ""
    category: str = "Other"
    tags: list[str] = Field(default_factory=list)

    summary_short: str = ""
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)

    action_required: bool = False
    action_summary: str | None = None
    deadline: str | None = None

    amounts: list[Amount] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)

    suggested_folder: str = ""
    suggested_filename: str = ""
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    document_integrity: DocumentIntegrity

    @field_validator("document_type", mode="before")
    @classmethod
    def coerce_document_type(cls, v: Any) -> Any:
        """Map German/unknown type names to valid DocumentType; fall back to 'Other'."""
        if not isinstance(v, str):
            return "Other"
        if v in _VALID_DOCUMENT_TYPES:
            return v
        v_lower = v.lower().strip()
        # Accept valid types regardless of case (e.g. "invoice" → "Invoice").
        for canonical in _VALID_DOCUMENT_TYPES:
            if canonical.lower() == v_lower:
                return canonical
        # Try German / alias mapping.
        mapped = _DOCUMENT_TYPE_ALIASES.get(v_lower)
        if mapped:
            return mapped
        return "Other"

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, v: Any) -> Any:
        """Normalize a percentage confidence value (e.g. 85) to a fraction (0.85)."""
        if isinstance(v, (int, float)) and v > 1.0:
            normalized = v / 100.0
            # Clamp in case the percentage itself is out of range.
            return min(max(normalized, 0.0), 1.0)
        return v

    @field_validator("document_date", "deadline", mode="before")
    @classmethod
    def empty_string_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v.lower().strip() in _NONE_LIKE_VALUES:
            return None
        return v

    @field_validator("tags", "key_points", mode="before")
    @classmethod
    def coerce_to_string_list(cls, v: Any) -> Any:
        """Accept a comma/newline-separated string in place of a list."""
        if isinstance(v, str):
            if not v.strip():
                return []
            return [item.strip() for item in re.split(r"[,\n]", v) if item.strip()]
        if not isinstance(v, list):
            return []
        return v

    @field_validator("amounts", mode="before")
    @classmethod
    def coerce_amounts_list(cls, v: Any) -> Any:
        """Silently drop amount entries that are not dict-like, rather than failing."""
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, (dict, Amount))]

    @field_validator("key_points")
    @classmethod
    def limit_key_points(cls, v: list[str]) -> list[str]:
        return v[:5]

    @model_validator(mode="after")
    def action_summary_requires_action_required(self) -> "DocumentAnalysis":
        if not self.action_required:
            self.action_summary = None
        return self

    @model_validator(mode="after")
    def ensure_summary_short(self) -> "DocumentAnalysis":
        """Fill summary_short from summary when the LLM leaves it blank."""
        if not self.summary_short and self.summary:
            self.summary_short = self.summary[:DESCRIPTION_SHORT_MAX_CHARS].rstrip()
        return self


# ---------------------------------------------------------------------------
# Sidecar document (stored as JSON next to the PDF)
# ---------------------------------------------------------------------------


DocumentStatus = Literal["review", "approved", "rejected", "error", "dry_run"]


class SidecarDocument(BaseModel):
    """The full sidecar JSON record stored alongside each PDF."""

    id: str
    original_path: str
    current_path: str
    sha256: str

    # Classification from LLM
    document_date: str | None = None
    correspondent: str | None = None
    document_type: DocumentType = "Other"
    category: str = "Other"
    topic: str = ""
    tags: list[str] = Field(default_factory=list)

    # Summaries
    summary_short: str = ""
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)

    # Actions
    action_required: bool = False
    action_summary: str | None = None
    deadline: str | None = None

    # Financials
    amounts: list[Amount] = Field(default_factory=list)
    entities: Entities = Field(default_factory=Entities)

    # Archive suggestions
    suggested_folder: str = ""
    suggested_filename: str = ""
    confidence: float = 0.0
    document_integrity: DocumentIntegrity = Field(default_factory=default_document_integrity)

    # Processing metadata
    model: str = ""
    processed_at: str = Field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    status: DocumentStatus = "review"
    warnings: list[str] = Field(default_factory=list)
    approved_at: str | None = None
    error_message: str | None = None

    @classmethod
    def from_analysis(
        cls,
        analysis: DocumentAnalysis,
        *,
        doc_id: str,
        original_path: str,
        current_path: str,
        sha256: str,
        model: str,
        warnings: list[str] | None = None,
    ) -> "SidecarDocument":
        return cls(
            id=doc_id,
            original_path=original_path,
            current_path=current_path,
            sha256=sha256,
            document_date=analysis.document_date,
            correspondent=analysis.correspondent,
            document_type=analysis.document_type,
            category=analysis.category,
            topic=analysis.topic,
            tags=analysis.tags,
            summary_short=analysis.summary_short,
            summary=analysis.summary,
            key_points=analysis.key_points,
            action_required=analysis.action_required,
            action_summary=analysis.action_summary,
            deadline=analysis.deadline,
            amounts=analysis.amounts,
            entities=analysis.entities,
            suggested_folder=analysis.suggested_folder,
            suggested_filename=analysis.suggested_filename,
            confidence=analysis.confidence,
            document_integrity=analysis.document_integrity,
            model=model,
            warnings=warnings or [],
        )

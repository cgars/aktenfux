"""Pydantic schemas for LLM responses and sidecar JSON documents."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
# Amount sub-model
# ---------------------------------------------------------------------------


class Amount(BaseModel):
    label: str
    amount: float
    currency: str = "EUR"


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

    @field_validator("key_points")
    @classmethod
    def limit_key_points(cls, v: list[str]) -> list[str]:
        return v[:5]

    @field_validator("document_date", "deadline", mode="before")
    @classmethod
    def empty_string_to_none(cls, v: Any) -> Any:
        if v == "" or v == "null":
            return None
        return v

    @model_validator(mode="after")
    def action_summary_requires_action_required(self) -> "DocumentAnalysis":
        if not self.action_required:
            self.action_summary = None
        return self


# ---------------------------------------------------------------------------
# Sidecar document (stored as JSON next to the PDF)
# ---------------------------------------------------------------------------


DocumentStatus = Literal["review", "approved", "rejected", "error"]


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
            model=model,
            warnings=warnings or [],
        )

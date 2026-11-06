#!/usr/bin/env python3
"""Create an OCR-readable synthetic multi-document PDF for end-to-end UAT.

The script writes only one PDF into an existing Aktenfux ``_Inbox``. It does
not create or modify a sidecar, configuration, or lifecycle state. Aktenfux
must discover and process the document through its normal CLI workflow.
"""
from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import BinaryIO, Sequence

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas


DEFAULT_FILENAME = "synthetic-multi-document.pdf"
EXPECTED_SPLIT_BEFORE_PAGE = 4


def _wrapped_lines(text: str, width: int = 78) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=False)


def _draw_page(
    pdf: Canvas,
    *,
    physical_page: int,
    issuer: str,
    issuer_details: str,
    title: str,
    reference_label: str,
    reference: str,
    logical_page: int,
    body: Sequence[str],
) -> None:
    page_width, page_height = A4
    accent = HexColor("#1f4e78") if physical_page <= 3 else HexColor("#2f6b3c")

    pdf.setFillColor(accent)
    pdf.rect(0, page_height - 92, page_width, 92, fill=1, stroke=0)
    pdf.setFillColor(white)
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(48, page_height - 42, issuer)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(48, page_height - 61, issuer_details)
    pdf.drawRightString(
        page_width - 48,
        page_height - 42,
        f"Seite {logical_page} von 3",
    )

    y = page_height - 132
    pdf.setFillColor(HexColor("#17202a"))
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(48, y, title)
    y -= 38

    pdf.setStrokeColor(HexColor("#b8c2cc"))
    pdf.setFillColor(HexColor("#f4f6f7"))
    pdf.roundRect(48, y - 46, page_width - 96, 58, 6, fill=1, stroke=1)
    pdf.setFillColor(HexColor("#17202a"))
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(62, y - 7, reference_label)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(180, y - 7, reference)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(62, y - 27, "Physische PDF-Seite")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(180, y - 27, f"{physical_page} von 6")
    y -= 82

    pdf.setFont("Helvetica", 11)
    for paragraph in body:
        if paragraph.startswith("## "):
            y -= 10
            pdf.setFont("Helvetica-Bold", 13)
            pdf.drawString(48, y, paragraph[3:])
            pdf.setFont("Helvetica", 11)
            y -= 24
            continue
        for line in _wrapped_lines(paragraph):
            pdf.drawString(48, y, line)
            y -= 16
        y -= 10

    pdf.setStrokeColor(accent)
    pdf.line(48, 48, page_width - 48, 48)
    pdf.setFillColor(HexColor("#566573"))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(48, 34, "Synthetisches Aktenfux E2E-UAT-Dokument - keine echten Daten")
    pdf.drawRightString(page_width - 48, 34, f"PDF-Seite {physical_page}/6")
    pdf.showPage()


def _write_pdf(stream: BinaryIO) -> None:
    pdf = Canvas(
        stream,
        pagesize=A4,
        pageCompression=1,
        invariant=1,
    )
    pdf.setTitle("Aktenfux synthetic multi-document E2E UAT")
    pdf.setAuthor("Aktenfux UAT fixture generator")
    pdf.setSubject("Two unrelated logical documents concatenated into one PDF")

    invoice_pages: tuple[tuple[str, ...], ...] = (
        (
            "Rechnungsempfaenger: Erika Beispiel, Testweg 12, 50667 Koeln",
            "Rechnungsdatum: 2026-10-15 | Kundennummer: K-10001",
            "## Stromlieferung September 2026",
            "Wir berechnen fuer den Testzeitraum eine rein synthetische Stromlieferung. Diese Rechnung gehoert ausschliesslich zur Nordlicht Energie GmbH.",
            "Rechnungsbetrag: 123,45 EUR | Faellig am: 2026-11-15",
        ),
        (
            "Zaehlernummer: Z-471100 | Abrechnungszeitraum: 2026-09-01 bis 2026-09-30",
            "Verbrauch: 321 kWh | Arbeitspreis: 0,31 EUR/kWh",
            "## Zahlungsinformationen",
            "Bitte ueberweisen Sie ausschliesslich unter Angabe der Rechnungsnummer RE-2026-4711 auf das Testkonto der Nordlicht Energie GmbH.",
            "IBAN: DE00 0000 0000 4711 0000 00 | BIC: TESTDE00XXX",
        ),
        (
            "Zusammenfassung: Rechnungsbetrag 123,45 EUR, faellig am 2026-11-15.",
            "Rueckfragen zur Rechnung beantwortet die Nordlicht Energie GmbH unter rechnung@example.invalid.",
            "## ENDE DER RECHNUNG",
            "Mit freundlichen Gruessen",
            "Nordlicht Energie GmbH - Buchhaltung",
        ),
    )
    for index, body in enumerate(invoice_pages, start=1):
        _draw_page(
            pdf,
            physical_page=index,
            issuer="NORDLICHT ENERGIE GMBH",
            issuer_details="Hafenstrasse 10 | 20457 Hamburg | rechnung@example.invalid",
            title="RECHNUNG",
            reference_label="Rechnungsnummer",
            reference="RE-2026-4711",
            logical_page=index,
            body=body,
        )

    contract_pages: tuple[tuple[str, ...], ...] = (
        (
            "Vertragspartner: Max Mustermann, Waldallee 8, 79100 Freiburg",
            "Vertragsdatum: 2026-11-01 | Kundennummer: P-77881",
            "## Gegenstand des Vertrages",
            "Die Waldweg Mobilitaet AG vermietet ein synthetisches Lastenrad fuer einen unabhaengigen Testvertrag. Dieser Vertrag steht in keinem Zusammenhang mit einer Energierechnung.",
            "Fahrzeug-ID: RAD-9007 | Beginn: 2026-12-01 | Laufzeit: 12 Monate",
        ),
        (
            "Monatliche Testmiete: 49,00 EUR | Kaution: 150,00 EUR",
            "Vertragskonto: VK-9007-2026 | SEPA-Mandat: M-77881",
            "## Pflichten der Vertragsparteien",
            "Die Vermieterin stellt das Lastenrad bereit. Der Mieter meldet Schaeden unverzueglich und verwendet das Fahrzeug nur fuer private Testzwecke.",
            "Zahlungsempfaenger ist ausschliesslich die Waldweg Mobilitaet AG.",
        ),
        (
            "Der Vertrag kann mit einer Frist von vier Wochen zum Laufzeitende gekuendigt werden.",
            "Ansprechpartnerin: Paula Probe, vertrag@example.invalid",
            "## UNTERSCHRIFTEN",
            "Waldweg Mobilitaet AG: ____________________",
            "Max Mustermann: ____________________",
            "ENDE DES MIETVERTRAGS MV-2026-9007",
        ),
    )
    for logical_page, body in enumerate(contract_pages, start=1):
        _draw_page(
            pdf,
            physical_page=logical_page + 3,
            issuer="WALDWEG MOBILITAET AG",
            issuer_details="Radweg 22 | 79100 Freiburg | vertrag@example.invalid",
            title="MIETVERTRAG LASTENRAD",
            reference_label="Vertragsnummer",
            reference="MV-2026-9007",
            logical_page=logical_page,
            body=body,
        )

    pdf.save()


def create_uat_pdf(output_path: Path) -> Path:
    """Create the six-page UAT PDF without overwriting an existing file."""
    output_path = output_path.expanduser()
    if output_path.suffix.lower() != ".pdf":
        raise ValueError("UAT output must use the .pdf extension.")
    if not output_path.parent.is_dir():
        raise FileNotFoundError(f"Output directory does not exist: {output_path.parent}")

    try:
        stream = output_path.open("xb")
    except FileExistsError:
        raise FileExistsError(f"Refusing to overwrite existing file: {output_path}") from None

    try:
        with stream:
            _write_pdf(stream)
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    return output_path


def _validated_inbox(workspace: Path) -> Path:
    root = workspace.expanduser().resolve()
    config = root / "config.yaml"
    inbox = root / "_Inbox"
    if not config.is_file() or config.is_symlink():
        raise ValueError(f"No regular config.yaml found in workspace: {root}")
    if not inbox.is_dir() or inbox.is_symlink():
        raise ValueError(f"No regular _Inbox directory found in workspace: {root}")
    if inbox.resolve().parent != root:
        raise ValueError("_Inbox must be a direct child of the workspace.")
    return inbox


def _validated_filename(value: str) -> str:
    candidate = Path(value)
    if candidate.name != value or candidate.suffix.lower() != ".pdf":
        raise ValueError("Filename must be one simple .pdf filename.")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a six-page OCR-readable PDF containing two unrelated logical "
            "documents and place it in an initialized Aktenfux _Inbox."
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Workspace previously created with 'afu init --target'.",
    )
    parser.add_argument(
        "--filename",
        default=DEFAULT_FILENAME,
        help=f"Simple destination filename (default: {DEFAULT_FILENAME}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        inbox = _validated_inbox(args.workspace)
        filename = _validated_filename(args.filename)
        output = create_uat_pdf(inbox / filename)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    print(f"Created synthetic E2E UAT PDF: {output}")
    print("Logical documents: 2; physical pages: 6")
    print(f"Expected split boundary: before page {EXPECTED_SPLIT_BEFORE_PAGE}")
    print("No sidecar was created; continue with 'afu scan --no-dry-run'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

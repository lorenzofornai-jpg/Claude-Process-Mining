"""Controllo rapido della copertura: esempi secondo il sistema sorgente dichiarato e domande di business
collegate agli obiettivi. Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.connectors.base import ColumnSchema, TableSchema  # noqa: E402
from app.i18n import render  # noqa: E402
from app.services import coverage  # noqa: E402

QUESTIONS = """Analizzare 3 use cases:
1. Touchless collection (fatture senza interventi manuali successivi alla creazione)
2. Payment terms mismatch (termini di pagamento in fattura diversi da contratti e ordini)
3. Days Sales Outstanding (tempo tra la creazione della fattura e la ricezione del pagamento)"""

TABLES = [TableSchema(name="invoices", row_count=10, columns=[
    ColumnSchema(name="invoice_id", inferred_type="string"), ColumnSchema(name="created_at", inferred_type="date"),
    ColumnSchema(name="paid_at", inferred_type="date"), ColumnSchema(name="created_by", inferred_type="string")])]


def test_examples_follow_the_declared_system_and_questions_are_linked():
    objectives = ["Tempi di attraversamento e colli di bottiglia", "Rilavorazioni e modifiche (prezzi, quantità, date, blocchi)",
                  "Puntualità di pagamenti o incassi"]
    crm = coverage.quick_coverage({"objectives": objectives, "key_questions": QUESTIONS,
                                   "systems": [{"name": "Salesforce"}]}, TABLES)
    texts = " ".join(render("it", m["hint"]) for r in crm["objectives"] for m in r["missing"] + r["useful_missing"])
    assert "SAP" not in texts and "audit trail" in texts
    links = {r["objective"]: [q["n"] for q in r["questions"]] for r in crm["objectives"]}
    assert links == {objectives[0]: [3], objectives[1]: [1], objectives[2]: [3]}
    assert [q["short"] for q in crm["unanswered"]] == ["Payment terms mismatch"]

    sap = coverage.quick_coverage({"objectives": objectives, "systems": [{"name": "SAP S/4HANA"}]}, TABLES)
    assert any("CDHDR" in render("it", m["hint"]) for r in sap["objectives"] for m in r["missing"] + r["useful_missing"])

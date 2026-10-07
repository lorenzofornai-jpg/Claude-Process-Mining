"""Profilo dei dati: colonna attivita' contro motivo, tabelle che ripetono un'altra, date previste con i
giorni da sommare. Dati inventati e generici (non SAP), perche' le regole devono valere per qualunque
sistema sorgente. Uso: python -m pytest backend/tests
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.i18n import render  # noqa: E402
from app.services.profiling import compact_for_mapping, profile_tables  # noqa: E402


def _log_rows():
    rows = []
    for i in range(10):
        rows.append({"log_id": f"L{2*i}", "ticket_id": f"T{i}", "event_name": "Hold placed", "event_date": f"2026-03-{i+1:02d}",
                     "user_name": ["anna", "marco"][i % 2], "hold_reason": "Customer complaint", "amount": f"{100+i}.50"})
        rows.append({"log_id": f"L{2*i+1}", "ticket_id": f"T{i}", "event_name": "Hold released", "event_date": f"2026-03-{i+11:02d}",
                     "user_name": ["anna", "marco"][i % 2], "hold_reason": "Solved", "amount": f"{100+i}.50"})
    return rows


def _tickets():
    return [{"ticket_id": f"T{i}", "opened": f"2026-02-{i+1:02d}", "priority": ["high", "low"][i % 2]} for i in range(10)]


def test_event_name_is_activity_and_reason_is_attribute():
    p = profile_tables({"ticket_log": _log_rows(), "tickets": _tickets()},
                       {"ticket_log": ["event_date"], "tickets": ["opened"]})
    log = next(t for t in p["tables"] if t["name"] == "ticket_log")
    # «event_name» e' l'attivita' anche se contiene «name»; «user_name» resta un utente
    assert [a["column"] for a in log["activity_columns"]] == ["event_name"]
    # il motivo, che qui va di pari passo con l'attivita', non la sostituisce: e' un attributo
    assert [r["column"] for r in log["reason_columns"]] == ["hold_reason"]
    cm = compact_for_mapping(p)
    assert cm["reason_columns"]["ticket_log"]["hold_reason"] == ["Customer complaint", "Solved"]


def test_filtered_export_is_a_copy_but_header_and_items_are_not():
    orders = [{"order_id": f"O{i}", "customer": f"C{i % 3}", "created": f"2026-01-{i+1:02d}",
               "closed": f"2026-02-{i+1:02d}" if i % 2 else "", "total": f"{50 + i * 7}.25", "region": ["N", "S"][i % 2]}
              for i in range(12)]
    closed_orders = [dict(o) for o in orders if o["closed"]]
    items = [{"order_id": f"O{i}", "item": str(k), "customer": f"C{i % 3}", "qty": str(k + i), "product": f"P{k}"}
             for i in range(12) for k in range(2)]
    p = profile_tables({"orders": orders, "closed_orders": closed_orders, "order_items": items},
                       {"orders": ["created", "closed"], "closed_orders": ["created", "closed"]})
    assert [(c["table"], c["of"], c["exclude"]) for c in p["copies"]] == [("closed_orders", "orders", True)]
    # un solo avviso per la tabella che ripete l'altra
    assert [render("it", i["title"]) for i in p["issues"] if i["table"] == "closed_orders"] == [
        "Ripete la tabella orders: tutte le sue 6 righe sono già lì"]


def test_copy_with_own_columns_stays_in_mapping():
    orders = [{"order_id": f"O{i}", "created": f"2026-01-{i+1:02d}", "total": f"{50 + i}.5", "status": ["A", "B"][i % 2]}
              for i in range(8)]
    extended = [dict(o, paid_on=f"2026-03-{i+1:02d}") for i, o in enumerate(orders)]
    p = profile_tables({"orders": orders, "orders_paid": extended}, {"orders": ["created"], "orders_paid": ["created", "paid_on"]})
    assert [(c["table"], c["new"], c["exclude"]) for c in p["copies"]] == [("orders_paid", ["paid_on"], False)]


def test_planned_date_with_days_column_and_objective():
    invoices = [{"invoice_id": f"I{i}", "issued": f"2026-01-{i+1:02d}", "due_base": f"2026-01-{i+1:02d}",
                 "payment_days": str([30, 60][i % 2]), "amount": f"{10 + i}.0"} for i in range(10)]
    p = profile_tables({"invoices": invoices}, {"invoices": ["issued", "due_base"]},
                       planned={"invoices": {"due_base": "il nome indica una data prevista o di scadenza"}},
                       due_objectives=["Puntualità di pagamenti o incassi"])
    issue = next(i for i in p["issues"] if "due_base" in render("it", i["title"]))
    action = render("en", issue["action"])
    assert "payment_days" in action and "Timeliness" in action
    assert "payment_days" in compact_for_mapping(p)["planned_dates"]["invoices"]["due_base"]

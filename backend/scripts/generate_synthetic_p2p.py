"""Genera un dataset sintetico Purchase-to-Pay per testare il Modulo 1.

Simula 4 tabelle come se fossero un export da un sistema sorgente generico:
purchase_orders (header), po_lines, goods_receipts, invoices.

Include alcune anomalie deliberate (timestamp mancanti, una fattura
antecedente alla creazione del PO) per verificare che il Data Quality
Engine le rilevi davvero, invece di testare solo il "percorso felice".
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic_p2p"
OUT_DIR.mkdir(parents=True, exist_ok=True)

VENDORS = [
    ("V-100", "Acme Supplies GmbH"),
    ("V-101", "Nordic Components AB"),
    ("V-102", "Iberia Packaging SL"),
    ("V-103", "Alpine Logistics SRL"),
]
MATERIALS = ["Steel Sheet", "Bearing Kit", "Control Valve", "Cable Harness", "Gasket Set"]
BUYERS = ["m.rossi", "l.bianchi", "a.moretti"]
CURRENCY = "EUR"

BASE_DATE = datetime(2026, 1, 5)

purchase_orders = []
po_lines = []
goods_receipts = []
invoices = []

n_pos = 30
gr_counter = 1
inv_counter = 1

for i in range(1, n_pos + 1):
    po_number = f"PO-{4500000 + i}"
    vendor_id, vendor_name = random.choice(VENDORS)
    created_date = BASE_DATE + timedelta(days=random.randint(0, 40))
    created_by = random.choice(BUYERS)
    n_lines = random.randint(1, 3)

    line_total = 0.0
    for line_no in range(1, n_lines + 1):
        qty = random.randint(1, 50)
        unit_price = round(random.uniform(15, 500), 2)
        line_amount = round(qty * unit_price, 2)
        line_total += line_amount
        po_lines.append(
            {
                "po_number": po_number,
                "po_line_no": line_no,
                "material": random.choice(MATERIALS),
                "quantity": qty,
                "unit_price": unit_price,
                "line_amount": line_amount,
                "plant": random.choice(["P100", "P200"]),
            }
        )

        # ~85% delle righe riceve una merce (le altre restano "aperte": normale in un P2P reale)
        if random.random() < 0.85:
            gr_date = created_date + timedelta(days=random.randint(2, 15))
            goods_receipts.append(
                {
                    "gr_number": f"GR-{9000000 + gr_counter}",
                    "po_number": po_number,
                    "po_line_no": line_no,
                    # 2 righe su ~90 con data mancante: anomalia deliberata per il DQ Engine
                    "gr_date": "" if gr_counter in (7, 41) else gr_date.strftime("%Y-%m-%d"),
                    "quantity_received": qty if random.random() > 0.1 else max(qty - random.randint(1, 3), 0),
                    "posted_by": random.choice(BUYERS),
                }
            )
            gr_counter += 1

    purchase_orders.append(
        {
            "po_number": po_number,
            "vendor_id": vendor_id,
            "vendor_name": vendor_name,
            "created_date": created_date.strftime("%Y-%m-%d"),
            "created_by": created_by,
            "po_status": random.choice(["Released", "Released", "Blocked"]),
            "currency": CURRENCY,
            "total_amount": round(line_total, 2),
        }
    )

    # ~80% dei PO riceve fattura (le 2 PO delle anomalie deliberate la ricevono sempre,
    # altrimenti l'anomalia non verrebbe mai generata)
    if i in (12, 27) or random.random() < 0.8:
        invoice_date = created_date + timedelta(days=random.randint(5, 25))
        # anomalia deliberata su 2 fatture: data fattura precedente alla creazione del PO
        if i in (12, 27):
            invoice_date = created_date - timedelta(days=3)
        paid = random.random() < 0.7
        payment_date = invoice_date + timedelta(days=random.randint(3, 20)) if paid else None

        invoices.append(
            {
                "invoice_number": f"INV-{7000000 + inv_counter}",
                "po_number": po_number,
                "invoice_date": invoice_date.strftime("%Y-%m-%d"),
                "invoice_amount": round(line_total * random.uniform(0.98, 1.0), 2),
                "currency": CURRENCY,
                "posted_by": random.choice(BUYERS),
                "payment_status": "Paid" if paid else "Open",
                "payment_date": payment_date.strftime("%Y-%m-%d") if payment_date else "",
            }
        )
        inv_counter += 1


def _write(name: str, rows: list[dict]) -> None:
    path = OUT_DIR / name
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"scritto {path} ({len(rows)} righe)")


if __name__ == "__main__":
    _write("purchase_orders.csv", purchase_orders)
    _write("po_lines.csv", po_lines)
    _write("goods_receipts.csv", goods_receipts)
    _write("invoices.csv", invoices)

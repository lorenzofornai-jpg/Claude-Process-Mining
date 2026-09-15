"""Genera un dataset P2P con nomi di tabelle/campi SAP realistici, per
stressare il Modulo 1 su volume e complessita' (non piu' i nomi "amichevoli"
di generate_synthetic_p2p.py).

7 tabelle, esattamente quelle che un consulente SAP estrarrebbe per un
progetto di process mining su Purchase-to-Pay:

- LFA1  (anagrafica fornitori)
- EKKO  (testata ordine d'acquisto)
- EKPO  (righe ordine d'acquisto)
- EKBE  (storico ordine: qui solo i movimenti di entrata merce, VGABE='1')
- RBKP  (testata fattura fornitore)
- RSEG  (righe fattura, collegate all'ordine)
- BSAK  (partite fornitore compensate: il "pagamento")

~100 ordini, quantita' di righe a cascata realistiche (centinaia di righe
per tabella in totale). Include le stesse categorie di anomalie deliberate
di generate_synthetic_p2p.py (timestamp mancanti, fatture antecedenti alla
creazione dell'ordine, fatture bloccate mai pagate) per verificare che il
Data Quality Engine le rilevi anche su questo schema.
"""
from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(7)

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "sap_p2p_sample"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MANDT = "100"
BUKRS = "1000"
BASE_DATE = datetime(2026, 1, 5)

VENDOR_NAMES = [
    ("Acme Supplies GmbH", "DE", "Muenchen"), ("Nordic Components AB", "SE", "Goeteborg"),
    ("Iberia Packaging SL", "ES", "Madrid"), ("Alpine Logistics SRL", "IT", "Torino"),
    ("Rhein Metall Teile AG", "DE", "Koeln"), ("Atlantic Fasteners Ltd", "GB", "Leeds"),
    ("Baltic Steel OU", "EE", "Tallinn"), ("Danube Electronics KFT", "HU", "Budapest"),
    ("Pyrenees Hydraulics SA", "FR", "Toulouse"), ("Carpathian Tools SRL", "RO", "Cluj"),
    ("Nordsee Maritime GmbH", "DE", "Hamburg"), ("Iberian Valves SL", "ES", "Bilbao"),
    ("Lombardia Meccanica SPA", "IT", "Milano"), ("Flanders Coating NV", "BE", "Antwerpen"),
    ("Helvetia Precision AG", "CH", "Zuerich"),
]
MATERIALS = [
    ("Steel Sheet 2mm", "ROH1"), ("Bearing Kit 608ZZ", "ROH2"), ("Control Valve DN50", "ROH3"),
    ("Cable Harness 3m", "ROH1"), ("Gasket Set Viton", "ROH2"), ("Hydraulic Hose 1/2in", "ROH3"),
    ("Servo Motor 24V", "ROH1"), ("Pressure Sensor 0-10bar", "ROH2"), ("Aluminium Profile 40x40", "ROH1"),
    ("PLC Module CPU1214", "ROH2"),
]
BUYERS = ["M.ROSSI", "L.BIANCHI", "A.MORETTI"]
EKORG_LIST = ["1000", "1010"]
EKGRP_LIST = ["001", "002", "003"]
BSART = "NB"
CURRENCY = "EUR"
TAX_CODES = ["V1", "V2"]

lfa1, ekko, ekpo, ekbe, rbkp, rseg, bsak = [], [], [], [], [], [], []

for i, (name, land, ort) in enumerate(VENDOR_NAMES, start=1):
    lfa1.append({
        "MANDT": MANDT, "LIFNR": f"{100000 + i}", "NAME1": name, "LAND1": land,
        "ORT01": ort, "STRAS": f"Industriestrasse {i}", "KTOKK": "KRED",
    })

n_pos = 100
gr_doc_counter = 5000000001
inv_doc_counter = 5100000001
clearing_counter = 1400000001

for i in range(1, n_pos + 1):
    ebeln = f"{4500000000 + i}"
    lifnr = lfa1[random.randint(0, len(lfa1) - 1)]["LIFNR"]
    aedat = BASE_DATE + timedelta(days=random.randint(0, 60))
    ekgrp = random.choice(EKGRP_LIST)

    ekko.append({
        "MANDT": MANDT, "EBELN": ebeln, "BUKRS": BUKRS, "BSART": BSART, "LIFNR": lifnr,
        "EKORG": random.choice(EKORG_LIST), "EKGRP": ekgrp,
        "AEDAT": aedat.strftime("%Y%m%d"), "ERNAM": random.choice(BUYERS),
        "WAERS": CURRENCY, "ZTERM": random.choice(["0001", "0002", "0003"]),
        "FRGKE": random.choice(["R", "R", "R", "B"]),  # R=rilasciato, B=bloccato
    })

    n_lines = random.randint(1, 4)
    po_line_total = 0.0
    line_infos = []

    for line_no in range(1, n_lines + 1):
        ebelp = f"{line_no * 10:05d}"
        mat_name, matkl = MATERIALS[random.randint(0, len(MATERIALS) - 1)]
        matnr = f"{random.randint(100000, 999999):018d}"
        menge = random.randint(5, 200)
        netpr = round(random.uniform(8, 450), 2)
        netwr = round(menge * netpr, 2)
        po_line_total += netwr

        ekpo.append({
            "MANDT": MANDT, "EBELN": ebeln, "EBELP": ebelp, "MATNR": matnr, "TXZ01": mat_name,
            "WERKS": random.choice(["P100", "P200"]), "MATKL": matkl,
            "MENGE": menge, "MEINS": "PC", "NETPR": netpr, "PEINH": 1, "NETWR": netwr,
            "LOEKZ": "",
        })
        line_infos.append({"ebelp": ebelp, "menge": menge, "netwr": netwr})

        # ~85% delle righe riceve merce (le altre restano aperte: normale in un P2P reale)
        if random.random() < 0.85:
            gr_date = aedat + timedelta(days=random.randint(2, 15))
            gr_doc_counter += 1
            zeile_menge = menge if random.random() > 0.1 else max(menge - random.randint(1, 5), 0)
            ekbe.append({
                "MANDT": MANDT, "EBELN": ebeln, "EBELP": ebelp, "ZEILE": "0001", "VGABE": "1",
                "GJAHR": "2026", "BELNR": str(gr_doc_counter),
                # 2 righe con BUDAT mancante: anomalia deliberata per il DQ Engine
                "BUDAT": "" if gr_doc_counter in (5000000008, 5000000042) else gr_date.strftime("%Y%m%d"),
                "MENGE": zeile_menge, "DMBTR": round(zeile_menge * netpr, 2),
                "BEWTP": "E", "ERNAM": random.choice(BUYERS),
            })

    # ~80% degli ordini riceve fattura (a volte su piu' righe, a volte parziale)
    if random.random() < 0.8:
        invoice_date = aedat + timedelta(days=random.randint(5, 25))
        # anomalia deliberata su 2 ordini: fattura con data antecedente alla creazione del PO
        if i in (12, 77):
            invoice_date = aedat - timedelta(days=3)
        blocked = random.random() < 0.1  # fattura bloccata per pagamento (ZLSPR)

        inv_doc_counter += 1
        belnr = str(inv_doc_counter)
        gross = round(po_line_total * random.uniform(0.97, 1.0), 2)

        rbkp.append({
            "MANDT": MANDT, "BELNR": belnr, "GJAHR": "2026", "BUKRS": BUKRS, "LIFNR": lifnr,
            "BLDAT": invoice_date.strftime("%Y%m%d"),
            "BUDAT": (invoice_date + timedelta(days=1)).strftime("%Y%m%d"),
            "WAERS": CURRENCY, "RMWWR": gross, "ERNAM": random.choice(BUYERS),
            "ZLSPR": "R" if blocked else "",
        })

        buzei = 0
        for li in line_infos:
            if random.random() < 0.9:  # non tutte le righe finiscono sempre in fattura insieme
                buzei += 1
                rseg.append({
                    "MANDT": MANDT, "BELNR": belnr, "GJAHR": "2026", "BUZEI": f"{buzei:03d}",
                    "EBELN": ebeln, "EBELP": li["ebelp"],
                    "MATNR": "", "MENGE": li["menge"],
                    "WRBTR": round(li["netwr"] * random.uniform(0.98, 1.0), 2),
                    "MWSKZ": random.choice(TAX_CODES),
                })

        # pagata solo se non bloccata, e solo il ~75% delle non bloccate
        if not blocked and random.random() < 0.75:
            clearing_counter += 1
            pay_date = invoice_date + timedelta(days=random.randint(5, 30))
            bsak.append({
                "MANDT": MANDT, "BUKRS": BUKRS, "LIFNR": lifnr, "BELNR": belnr, "GJAHR": "2026",
                "AUGDT": pay_date.strftime("%Y%m%d"), "AUGBL": str(clearing_counter),
                "DMBTR": gross,
            })


def _write(name: str, rows: list[dict]) -> None:
    path = OUT_DIR / name
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"scritto {path} ({len(rows)} righe)")


if __name__ == "__main__":
    _write("LFA1.csv", lfa1)
    _write("EKKO.csv", ekko)
    _write("EKPO.csv", ekpo)
    _write("EKBE.csv", ekbe)
    _write("RBKP.csv", rbkp)
    _write("RSEG.csv", rseg)
    _write("BSAK.csv", bsak)
    print(f"\ntotale righe: {len(lfa1) + len(ekko) + len(ekpo) + len(ekbe) + len(rbkp) + len(rseg) + len(bsak)}")

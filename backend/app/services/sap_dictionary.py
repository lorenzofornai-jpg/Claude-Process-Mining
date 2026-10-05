"""Dizionario delle tabelle SAP standard per il mapping deterministico.

Il significato di tabelle e campi SAP standard e' documentato e stabile: per
queste tabelle il mapping verso OCEL non richiede l'AI. Il dizionario dice,
per ogni tabella, quale oggetto definisce (con la chiave), quali date sono
attivita' di processo, quali campi sono utili come dimensioni di analisi e
quali collegamenti esistono verso oggetti definiti da altre tabelle.

Si applica solo cio' che e' presente nei dati caricati: una colonna assente
viene saltata; un collegamento verso un oggetto che nel dataset non c'e'
viene tolto a valle (vedi deterministic.finalize). I campi non elencati
(tecnici o ridondanti) non vengono mappati.

Vincoli del motore di trasformazione rispettati qui:
- un evento si collega a un oggetto se la riga dell'evento contiene tutte le
  colonne chiave di quell'oggetto con lo stesso nome (in SAP i nomi dei campi
  sono condivisi tra tabelle: EBELN, EBELP, LIFNR, KUNNR...);
- oppure tramite una tabella "ponte" (joins): la riga dell'evento si unisce
  alle righe della tabella ponte sulla colonna indicata e si collegano gli
  oggetti costruiti da quelle righe (es. testata ordine -> posizioni).
- i nomi degli eventi sono unici nell'intero dataset.

Per aggiungere tabelle (o processi) basta aggiungere voci qui.
"""
from __future__ import annotations

# Ogni voce:
#   label: descrizione della tabella
#   object: (tipo oggetto, [colonne chiave]) oppure None (tabella di soli eventi);
#       al posto della lista si possono dare chiavi alternative ([[...], [...]]):
#       vale la prima presente nei dati (export senza posizione, es. senza BUZEI)
#   object_attributes: {colonna: descrizione}
#   events: [(nome evento, [colonne data in ordine di preferenza], {colonna: descrizione attributo evento})]
#   relations: [(evento di questa tabella, tipo oggetto collegato, qualifier)]
#       collegamento diretto: le colonne chiave dell'oggetto sono nella riga
#   joins: [(evento di un'altra tabella, tipo oggetto collegato, colonna di join, qualifier)]
#       la tabella fa da ponte: per ogni evento si cercano qui le righe con lo
#       stesso valore della colonna di join e si collegano gli oggetti di quelle righe
#   activity (facoltativo): l'attivita' di ogni riga si legge da una colonna "tipo"
#       {"event": evento di questa tabella, "column": colonna, "values": {codice: attivita'},
#        "covered_by": {codice: evento di un'altra tabella}}: un codice "coperto" da
#       un'altra tabella presente nel dataset si esclude, per non contare due volte
#       lo stesso fatto (es. la fattura da EKBE e da RBKP)
SAP_TABLES: dict[str, dict] = {
    # ------------------------------------------------------------ Purchase-to-Pay
    "EBAN": {
        "label": "Richieste d'acquisto (posizioni)",
        "object": ("Purchase Requisition Item", ["BANFN", "BNFPO"]),
        "object_attributes": {
            "MATNR": "materiale", "MATKL": "gruppo merci", "WERKS": "stabilimento",
            "EKGRP": "gruppo acquisti", "MENGE": "quantità", "PREIS": "prezzo di valutazione",
            "AFNAM": "richiedente", "BSART": "tipo documento", "KNTTP": "tipo imputazione",
        },
        "events": [
            ("Create Purchase Requisition", ["BADAT", "ERDAT"], {"ERNAM": "utente"}),
            ("Release Purchase Requisition", ["FRGDT"], {}),
        ],
        "relations": [], "joins": [],
    },
    "EKKO": {
        "label": "Ordini d'acquisto (testata)",
        "object": ("Purchase Order", ["EBELN"]),
        "object_attributes": {
            "BUKRS": "società", "BSART": "tipo ordine", "EKORG": "organizzazione acquisti",
            "EKGRP": "gruppo acquisti", "LIFNR": "fornitore", "ZTERM": "condizioni di pagamento",
            "WAERS": "valuta", "INCO1": "incoterms", "FRGKE": "indicatore di rilascio",
        },
        "events": [("Create Purchase Order", ["AEDAT", "BEDAT"], {"ERNAM": "utente"})],
        "relations": [("Create Purchase Order", "Vendor", "from vendor")],
        "joins": [],
    },
    "EKPO": {
        "label": "Ordini d'acquisto (posizioni)",
        "object": ("Purchase Order Item", ["EBELN", "EBELP"]),
        "object_attributes": {
            "MATNR": "materiale", "MATKL": "gruppo merci", "TXZ01": "testo breve", "WERKS": "stabilimento",
            "MENGE": "quantità ordinata", "MEINS": "unità di misura", "NETPR": "prezzo netto",
            "NETWR": "valore netto", "KNTTP": "tipo imputazione", "PSTYP": "categoria posizione",
            "WEBRE": "verifica fattura su entrata merci", "BANFN": "richiesta d'acquisto",
            "LOEKZ": "indicatore di cancellazione",
        },
        "events": [],
        "relations": [],
        "joins": [("Create Purchase Order", "Purchase Order Item", "EBELN", "creates")],
    },
    "EKBE": {
        "label": "Storico ordine d'acquisto (entrate merci e fatture)",
        "object": None,
        "object_attributes": {},
        "events": [("Post Purchase Order History", ["BUDAT", "CPUDT"], {
            "VGABE": "tipo operazione (1 entrata merci, 2 fattura)", "BEWTP": "categoria storico",
            "BWART": "tipo movimento", "MENGE": "quantità", "DMBTR": "importo", "BELNR": "documento",
            "ERNAM": "utente",
        })],
        "relations": [
            ("Post Purchase Order History", "Purchase Order", "for order"),
            ("Post Purchase Order History", "Purchase Order Item", "for item"),
        ],
        "joins": [],
        "activity": {
            "event": "Post Purchase Order History", "column": "VGABE",
            "values": {
                "1": "Post Goods Receipt", "2": "Post Invoice Receipt", "3": "Post Subsequent Debit/Credit",
                "4": "Post Down Payment", "6": "Post Goods Issue for Stock Transfer",
                "8": "Post Delivery for Stock Transfer", "9": "Post Service Entry Sheet",
            },
            "covered_by": {"2": "Post Supplier Invoice"},
        },
    },
    "MSEG": {
        "label": "Movimenti merci (posizioni)",
        "object": None,
        "object_attributes": {},
        "events": [("Post Goods Movement", ["BUDAT_MKPF", "CPUDT_MKPF"], {
            "BWART": "tipo movimento", "MENGE": "quantità", "DMBTR": "importo", "WERKS": "stabilimento",
            "LGORT": "magazzino", "MBLNR": "documento materiale", "USNAM_MKPF": "utente",
        })],
        "relations": [
            ("Post Goods Movement", "Purchase Order", "for order"),
            ("Post Goods Movement", "Purchase Order Item", "receives against"),
        ],
        "joins": [],
    },
    "RBKP": {
        "label": "Fatture fornitore (testata)",
        "object": ("Supplier Invoice", ["BELNR", "GJAHR"]),
        "object_attributes": {
            "BUKRS": "società", "BLART": "tipo documento", "LIFNR": "fornitore", "RMWWR": "importo lordo",
            "WAERS": "valuta", "ZTERM": "condizioni di pagamento", "ZFBDT": "data base per la scadenza",
            "RBSTAT": "stato fattura", "XBLNR": "riferimento fornitore", "BLDAT": "data documento del fornitore",
            "ZLSPR": "blocco pagamento",
        },
        "events": [
            ("Enter Supplier Invoice", ["CPUDT"], {"USNAM": "utente"}),
            ("Post Supplier Invoice", ["BUDAT"], {"ERNAM": "utente"}),
        ],
        "relations": [
            ("Enter Supplier Invoice", "Vendor", "from vendor"),
            ("Post Supplier Invoice", "Vendor", "from vendor"),
        ],
        "joins": [],
    },
    "RSEG": {
        "label": "Fatture fornitore (posizioni)",
        "object": None,
        "object_attributes": {},
        "events": [],
        "relations": [],
        "joins": [
            ("Post Supplier Invoice", "Purchase Order Item", "BELNR", "invoices"),
            ("Post Supplier Invoice", "Purchase Order", "BELNR", "for order"),
        ],
    },
    "LFA1": {
        "label": "Anagrafica fornitori",
        "object": ("Vendor", ["LIFNR"]),
        "object_attributes": {
            "NAME1": "nome", "LAND1": "paese", "ORT01": "città", "REGIO": "regione",
            "KTOKK": "gruppo conti", "BRSCH": "settore",
        },
        "events": [], "relations": [], "joins": [],
    },
    # ------------------------------------------------------------ Accounts Payable
    "BSIK": {
        "label": "Partite aperte fornitori",
        "object": ("Vendor Open Item", [["BUKRS", "BELNR", "GJAHR", "BUZEI"], ["BUKRS", "BELNR", "GJAHR"]]),
        "object_attributes": {
            "LIFNR": "fornitore", "BLART": "tipo documento", "DMBTR": "importo", "WAERS": "valuta",
            "ZTERM": "condizioni di pagamento", "ZFBDT": "data base per la scadenza",
            "ZBD1T": "giorni di pagamento", "ZLSCH": "metodo di pagamento", "ZLSPR": "blocco pagamento",
        },
        "events": [("Post Vendor Open Item", ["BUDAT", "CPUDT"], {})],
        "relations": [("Post Vendor Open Item", "Vendor", "from vendor")],
        "joins": [],
    },
    "BSAK": {
        "label": "Partite pareggiate fornitori",
        "object": ("Vendor Cleared Item", [["BUKRS", "BELNR", "GJAHR", "BUZEI"], ["BUKRS", "BELNR", "GJAHR"]]),
        "object_attributes": {
            "LIFNR": "fornitore", "BLART": "tipo documento", "DMBTR": "importo", "WAERS": "valuta",
            "ZTERM": "condizioni di pagamento", "ZFBDT": "data base per la scadenza",
            "ZBD1T": "giorni di pagamento", "ZLSCH": "metodo di pagamento", "AUGBL": "documento di pareggio",
        },
        "events": [
            ("Post Vendor Item", ["BUDAT", "CPUDT"], {}),
            ("Clear Vendor Item", ["AUGDT"], {}),
        ],
        "relations": [
            ("Post Vendor Item", "Vendor", "from vendor"),
            ("Clear Vendor Item", "Vendor", "pays vendor"),
        ],
        "joins": [],
    },
    # ------------------------------------------------------------ Accounts Receivable
    "KNA1": {
        "label": "Anagrafica clienti",
        "object": ("Customer", ["KUNNR"]),
        "object_attributes": {
            "NAME1": "nome", "LAND1": "paese", "ORT01": "città", "REGIO": "regione",
            "KTOKD": "gruppo conti", "BRSCH": "settore",
        },
        "events": [], "relations": [], "joins": [],
    },
    "BSID": {
        "label": "Partite aperte clienti",
        "object": ("Customer Open Item", [["BUKRS", "BELNR", "GJAHR", "BUZEI"], ["BUKRS", "BELNR", "GJAHR"]]),
        "object_attributes": {
            "KUNNR": "cliente", "BLART": "tipo documento", "DMBTR": "importo", "WAERS": "valuta",
            "ZTERM": "condizioni di pagamento", "ZFBDT": "data base per la scadenza",
            "ZBD1T": "giorni di pagamento", "MANST": "livello di sollecito", "ZLSCH": "metodo di pagamento",
        },
        "events": [
            ("Post Customer Open Item", ["BUDAT", "CPUDT"], {}),
            ("Send Dunning Notice (open item)", ["MADAT"], {"MANST": "livello di sollecito"}),
        ],
        "relations": [
            ("Post Customer Open Item", "Customer", "for customer"),
            ("Send Dunning Notice (open item)", "Customer", "to customer"),
        ],
        "joins": [],
    },
    "BSAD": {
        "label": "Partite pareggiate clienti",
        "object": ("Customer Cleared Item", [["BUKRS", "BELNR", "GJAHR", "BUZEI"], ["BUKRS", "BELNR", "GJAHR"]]),
        "object_attributes": {
            "KUNNR": "cliente", "BLART": "tipo documento", "DMBTR": "importo", "WAERS": "valuta",
            "ZTERM": "condizioni di pagamento", "ZFBDT": "data base per la scadenza",
            "ZBD1T": "giorni di pagamento", "MANST": "livello di sollecito", "AUGBL": "documento di pareggio",
        },
        "events": [
            ("Post Customer Item", ["BUDAT", "CPUDT"], {}),
            ("Send Dunning Notice", ["MADAT"], {"MANST": "livello di sollecito"}),
            ("Clear Customer Item", ["AUGDT"], {}),
        ],
        "relations": [
            ("Post Customer Item", "Customer", "for customer"),
            ("Send Dunning Notice", "Customer", "to customer"),
            ("Clear Customer Item", "Customer", "paid by customer"),
        ],
        "joins": [],
    },
    # ------------------------------------------------------------ Order-to-Cash
    "VBAK": {
        "label": "Ordini di vendita (testata)",
        "object": ("Sales Order", ["VBELN"]),
        "object_attributes": {
            "AUART": "tipo ordine", "VKORG": "organizzazione vendite", "VTWEG": "canale distributivo",
            "SPART": "settore merceologico", "VKBUR": "ufficio vendite", "KUNNR": "cliente",
            "NETWR": "valore netto", "WAERK": "valuta",
        },
        "events": [("Create Sales Order", ["ERDAT", "AUDAT"], {"ERNAM": "utente"})],
        "relations": [("Create Sales Order", "Customer", "for customer")],
        "joins": [],
    },
    "VBAP": {
        "label": "Ordini di vendita (posizioni)",
        "object": ("Sales Order Item", ["VBELN", "POSNR"]),
        "object_attributes": {
            "MATNR": "materiale", "MATKL": "gruppo merci", "WERKS": "stabilimento",
            "KWMENG": "quantità", "NETWR": "valore netto", "PSTYV": "categoria posizione",
            "ABGRU": "motivo di rifiuto",
        },
        "events": [],
        "relations": [],
        "joins": [("Create Sales Order", "Sales Order Item", "VBELN", "creates")],
    },
    "LIKP": {
        "label": "Consegne in uscita (testata)",
        "object": ("Outbound Delivery", ["VBELN"]),
        "object_attributes": {
            "LFART": "tipo consegna", "VSTEL": "punto di spedizione", "ROUTE": "itinerario",
            "KUNNR": "destinatario merci",
        },
        "events": [
            ("Create Outbound Delivery", ["ERDAT"], {"ERNAM": "utente"}),
            ("Post Goods Issue", ["WADAT_IST"], {}),
        ],
        "relations": [
            ("Create Outbound Delivery", "Customer", "ship to"),
            ("Post Goods Issue", "Customer", "ship to"),
        ],
        "joins": [],
    },
    "VBRK": {
        "label": "Documenti di fatturazione (testata)",
        "object": ("Billing Document", ["VBELN"]),
        "object_attributes": {
            "FKART": "tipo fattura", "VKORG": "organizzazione vendite", "NETWR": "valore netto",
            "WAERK": "valuta", "KUNAG": "committente", "KUNRG": "pagatore", "ZTERM": "condizioni di pagamento",
        },
        "events": [
            ("Create Billing Document", ["ERDAT"], {"ERNAM": "utente"}),
            ("Post Billing Document", ["FKDAT"], {}),
        ],
        "relations": [],
        "joins": [],
    },
}


def lookup(table_name: str, columns: list[str]) -> tuple[str, dict] | None:
    """Voce del dizionario per una tabella caricata. Il nome del file puo'
    avere suffissi (es. "EKKO_2024", "ekko-export"): conta la parte iniziale.
    La voce si usa solo se la tabella contiene le colonne chiave dell'oggetto
    (o, per le tabelle di soli eventi, almeno una data prevista)."""
    upper = table_name.upper()
    cols = {c.upper() for c in columns}
    for code, entry in SAP_TABLES.items():
        if upper == code or (upper.startswith(code) and not upper[len(code)].isalnum()):
            obj = entry["object"]
            if obj and object_keys(obj[1], cols) is None:
                return None
            if not obj and not any(set(d) & cols for _, d, _ in entry["events"]) and not entry["joins"]:
                return None
            return code, entry
    return None


def object_keys(keys: list, columns: set[str]) -> list[str] | None:
    """Colonne chiave da usare (in maiuscolo): la prima alternativa presente nei dati."""
    alternatives = keys if keys and isinstance(keys[0], list) else [keys]
    return next((k for k in alternatives if set(k) <= columns), None)

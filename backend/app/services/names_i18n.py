"""Nomi di oggetti, eventi e attivita' nella lingua dell'utente.

I nomi proposti da Claude seguono gia' la lingua (prompt del mapping). Questo modulo traduce i nomi che arrivano
senza AI: dizionario SAP standard, modello P2P di esempio, catalogo appreso in un'altra lingua. Funziona nei due
versi (inglese -> italiano e italiano -> inglese); i nomi che non conosce restano come sono (un nome scelto da un
utente non si cambia).
"""
from __future__ import annotations

# inglese -> italiano (nomi di business; attivita' con verbo + oggetto, come «Imposta blocco sollecito»)
EN_IT = {
    # oggetti
    "Billing Document": "Documento di fatturazione",
    "Customer": "Cliente",
    "Customer Cleared Item": "Partita cliente pareggiata",
    "Customer Open Item": "Partita cliente aperta",
    "Outbound Delivery": "Consegna in uscita",
    "Purchase Order": "Ordine d'acquisto",
    "Purchase Order Item": "Posizione ordine d'acquisto",
    "Purchase Requisition Item": "Posizione richiesta d'acquisto",
    "Sales Order": "Ordine di vendita",
    "Sales Order Item": "Posizione ordine di vendita",
    "Supplier Invoice": "Fattura fornitore",
    "Vendor": "Fornitore",
    "Vendor Cleared Item": "Partita fornitore pareggiata",
    "Vendor Open Item": "Partita fornitore aperta",
    "PurchaseOrder": "Ordine d'acquisto",
    "POLine": "Posizione ordine d'acquisto",
    "GoodsReceipt": "Entrata merci",
    "Invoice": "Fattura",
    # eventi e attivita'
    "Clear Customer Item": "Pareggia partita cliente",
    "Clear Vendor Item": "Pareggia partita fornitore",
    "Create Billing Document": "Crea documento di fatturazione",
    "Create Outbound Delivery": "Crea consegna in uscita",
    "Create Purchase Order": "Crea ordine d'acquisto",
    "Create Purchase Requisition": "Crea richiesta d'acquisto",
    "Create Sales Order": "Crea ordine di vendita",
    "Enter Supplier Invoice": "Inserisci fattura fornitore",
    "Post Billing Document": "Registra documento di fatturazione",
    "Post Customer Item": "Registra partita cliente",
    "Post Customer Open Item": "Registra partita cliente aperta",
    "Post Delivery for Stock Transfer": "Registra consegna per trasferimento",
    "Post Down Payment": "Registra acconto",
    "Post Goods Issue": "Registra uscita merci",
    "Post Goods Issue for Stock Transfer": "Registra uscita merci per trasferimento",
    "Post Goods Movement": "Registra movimento merci",
    "Post Goods Receipt": "Registra entrata merci",
    "Post Invoice": "Registra fattura",
    "Post Invoice Receipt": "Registra ricevimento fattura",
    "Post Purchase Order History": "Registra storico ordine d'acquisto",
    "Post Service Entry Sheet": "Registra accettazione servizi",
    "Post Subsequent Debit/Credit": "Registra addebito/accredito successivo",
    "Post Supplier Invoice": "Registra fattura fornitore",
    "Post Vendor Item": "Registra partita fornitore",
    "Post Vendor Open Item": "Registra partita fornitore aperta",
    "Release Purchase Requisition": "Rilascia richiesta d'acquisto",
    "Send Dunning Notice": "Invia sollecito",
    "Send Dunning Notice (open item)": "Invia sollecito (partita aperta)",
}
IT_EN = {}
for _en, _it in EN_IT.items():
    IT_EN.setdefault(_it, _en)   # «Ordine d'acquisto» -> «Purchase Order» (non «PurchaseOrder»)


def name(value: str | None, lang: str) -> str | None:
    if not value:
        return value
    table = EN_IT if lang == "it" else IT_EN
    if value in table:
        return table[value]
    # nomi generici del mapping senza AI («Bkpf event» = evento della tabella BKPF)
    if lang == "it" and value.endswith(" event") and " " not in value[:-6]:
        return f"Evento {value[:-6]}"
    if lang == "en" and value.startswith("Evento ") and " " not in value[7:]:
        return f"{value[7:]} event"
    return value


def localize_proposals(proposals: list, lang: str) -> list:
    """Traduce sul posto i nomi conosciuti delle proposte (oggetti, eventi, oggetti collegati, nomi per valore
    delle colonne attivita' e di divisione). Ritorna la stessa lista."""
    if lang not in ("it", "en"):
        return proposals
    for p in proposals:
        for f in ("object_type", "event_type", "related_object_type"):
            setattr(p, f, name(getattr(p, f), lang))
        if p.ocel_element in ("event_type.activity", "object_type.split") and isinstance(p.activity_values, dict):
            p.activity_values = {k: name(v, lang) for k, v in p.activity_values.items()}
    return proposals


def localize_rows(rows: list[dict], lang: str) -> list[dict]:
    """Come localize_proposals, per righe salvate come dizionari."""
    if lang not in ("it", "en"):
        return rows
    for r in rows:
        for f in ("object_type", "event_type", "related_object_type"):
            r[f] = name(r.get(f), lang)
        if r.get("ocel_element") in ("event_type.activity", "object_type.split") and isinstance(r.get("activity_values"), dict):
            r["activity_values"] = {k: name(v, lang) for k, v in r["activity_values"].items()}
    return rows


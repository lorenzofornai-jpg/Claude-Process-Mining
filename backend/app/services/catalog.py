"""System Table Catalog: libreria di tabelle note per sistema+processo.

In produzione questa libreria si arricchisce nel tempo (approvazione umana
dei pattern ricorrenti). Qui la seediamo con la voce "GenericFile + P2P"
che descrive esattamente le 4 tabelle del dataset sintetico, cosi' l'AI
Mapping Service puo' mostrare sia il percorso "riconosco il pattern da
template" (alta confidence) sia il percorso "tabella non nota, deduco da
euristiche generiche" (confidence piu' bassa) per tabelle non presenti qui.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CatalogHint:
    table_name: str
    object_type: str | None
    event_type: str | None
    timestamp_column: str | None
    rationale: str


GENERIC_FILE_P2P_CATALOG: dict[str, CatalogHint] = {
    "purchase_orders": CatalogHint(
        table_name="purchase_orders",
        object_type="PurchaseOrder",
        event_type="Create Purchase Order",
        timestamp_column="created_date",
        rationale=(
            "Nome tabella coincide con il pattern noto 'purchase_orders' nel "
            "template GenericFile+P2P: tabella header ordini d'acquisto."
        ),
    ),
    "po_lines": CatalogHint(
        table_name="po_lines",
        object_type="POLine",
        event_type=None,
        timestamp_column=None,
        rationale=(
            "Nome tabella coincide con il pattern noto 'po_lines': righe ordine, "
            "figlie di purchase_orders. Nessun evento proprio: creata insieme "
            "all'ordine header."
        ),
    ),
    "goods_receipts": CatalogHint(
        table_name="goods_receipts",
        object_type="GoodsReceipt",
        event_type="Post Goods Receipt",
        timestamp_column="gr_date",
        rationale=(
            "Nome tabella coincide con il pattern noto 'goods_receipts': "
            "movimenti di ricevimento merce collegati a una riga ordine."
        ),
    ),
    "invoices": CatalogHint(
        table_name="invoices",
        object_type="Invoice",
        event_type="Post Invoice",
        timestamp_column="invoice_date",
        rationale=(
            "Nome tabella coincide con il pattern noto 'invoices': fatture "
            "collegate a un ordine d'acquisto."
        ),
    ),
}

TEMPLATE_ID = "genericfile_p2p_v1"
LEARNED_TEMPLATE_ID = "catalog:learned"


def lookup(table_name: str) -> CatalogHint | None:
    return GENERIC_FILE_P2P_CATALOG.get(table_name)


def dynamic_lookup(table_name: str) -> tuple[CatalogHint, list[dict]] | None:
    """Catalogo "vivo", alimentato dall'utente: cerca un mapping gia' confermato
    in una struttura che l'utente ha esplicitamente aggiunto al catalogo
    (IngestionConfig.in_catalog=True, pulsante "Aggiungi al catalogo" nel
    registro strutture) per una tabella con questo stesso nome esatto.

    Ritorna (hint, rules) nello stesso formato del catalogo statico sopra, cosi'
    HeuristicAIMapper puo' trattarlo allo stesso modo con _from_template(); usato
    anche da ClaudeAIMapper per dare a Claude un pattern di riferimento gia'
    validato da un umano, come una "wiki" di mapping precedenti.

    Semplificazione: match solo per nome tabella esatto, non per struttura
    colonne - stessa assunzione gia' fatta dal catalogo statico sopra. Se piu'
    strutture in catalogo hanno usato lo stesso nome tabella, vince la piu'
    recente.
    """
    from app.db import SessionLocal
    from app.models import FieldMapping, IngestionConfig

    db = SessionLocal()
    try:
        rows = (
            db.query(FieldMapping)
            .join(IngestionConfig, FieldMapping.ingestion_config_id == IngestionConfig.id)
            .filter(
                IngestionConfig.in_catalog.is_(True),
                FieldMapping.source_table == table_name,
                FieldMapping.status.in_(("confirmed", "overridden")),
            )
            .order_by(IngestionConfig.created_at.desc())
            .all()
        )
        if not rows:
            return None

        # tiene solo le righe della struttura piu' recente tra quelle trovate,
        # per non mischiare pattern di due strutture diverse con lo stesso nome tabella
        most_recent_config_id = rows[0].ingestion_config_id
        rows = [r for r in rows if r.ingestion_config_id == most_recent_config_id]

        rules = [
            dict(
                col=r.source_column,
                el=r.ocel_element,
                object_type=r.object_type,
                event_type=r.event_type,
                qualifier=r.qualifier,
                related_object_type=r.related_object_type,
                # confidence fissa e alta: e' un pattern confermato da un umano in una
                # struttura promossa, non una proposta AI grezza (il campo r.confidence
                # e' quello della proposta originale, spesso pre-correzione).
                conf=0.90,
                rationale=(r.rationale or "").strip()
                or "Pattern di mapping riutilizzato dal catalogo (confermato in una struttura precedente).",
            )
            for r in rows
        ]
        object_type = next((r.object_type for r in rows if r.object_type), None)
        event_type = next((r.event_type for r in rows if r.event_type), None)
        timestamp_column = next(
            (r.source_column for r in rows if r.ocel_element == "event_type.timestamp"), None
        )
        hint = CatalogHint(
            table_name=table_name,
            object_type=object_type,
            event_type=event_type,
            timestamp_column=timestamp_column,
            rationale=(
                f"Pattern riutilizzato dal catalogo: una tabella chiamata '{table_name}' e' gia' "
                "stata mappata e confermata in una struttura precedente."
            ),
        )
        return hint, rules
    finally:
        db.close()

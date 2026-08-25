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


def lookup(table_name: str) -> CatalogHint | None:
    return GENERIC_FILE_P2P_CATALOG.get(table_name)

"""AI Mapping Service.

Interfaccia comune (AIMapper) pensata per essere implementata da un vero
LLM in futuro (es. ClaudeAIMapper che chiama l'API Anthropic con lo schema
sorgente + il Process Context Profile + il System Table Catalog come
contesto). In questo prototipo usiamo HeuristicAIMapper: stessa interfaccia,
stesso formato di output (confidence + rationale + based_on_template), ma
la "proposta" viene da regole invece che da una chiamata LLM. Il resto
della pipeline (review UI, transformation engine) non sa e non deve sapere
quale implementazione sta usando.

Due percorsi dimostrati deliberatamente:
1. Tabella riconosciuta nel System Table Catalog (template GenericFile+P2P)
   -> proposte ad alta confidence, motivate dal pattern noto.
2. Tabella/colonna non riconosciuta -> euristica generica a bassa
   confidence, che nella UI di review finisce sotto soglia di auto-accept
   e richiede quindi una decisione esplicita dell'utente (lo stesso
   meccanismo che nel disegno concettuale chiamavamo "domanda mirata").
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel

from app.config import ANTHROPIC_MODEL
from app.connectors.base import TableSchema
from app.services import catalog

ID_LIKE_PATTERN = re.compile(r"(_number|_no|_id)$", re.IGNORECASE)


@dataclass
class MappingProposal:
    source_table: str
    source_column: str | None
    ocel_element: str  # object_type.key | object_type.attribute | event_type.timestamp
    # | event_type.attribute | e2o_relationship
    object_type: str | None
    event_type: str | None
    attribute_name: str | None
    qualifier: str | None
    related_object_type: str | None
    confidence: float
    rationale: str
    based_on_template: str | None


class AIMapper(ABC):
    @abstractmethod
    def propose_mapping(self, tables: list[TableSchema], context_profile: dict) -> list[MappingProposal]:
        ...


# ---------------------------------------------------------------------------
# Template GenericFile+P2P: la "competenza" gia' validata su questo pattern
# di tabelle. E' l'equivalente, per il mock, di cio' che un vero LLM
# ricostruirebbe da system_table_catalog + few-shot su mapping precedenti.
# ---------------------------------------------------------------------------

_TEMPLATE_RULES: dict[str, list[dict]] = {
    "purchase_orders": [
        dict(col="po_number", el="object_type.key", object_type="PurchaseOrder", conf=0.97,
             rationale="Valori univoci, nome colonna coerente con pattern chiave (*_number): chiave naturale di PurchaseOrder."),
        dict(col="vendor_id", el="object_type.attribute", object_type="PurchaseOrder", conf=0.80,
             rationale="Identificativo anagrafica fornitore, stabile per l'intero ordine: attributo dell'oggetto."),
        dict(col="vendor_name", el="object_type.attribute", object_type="PurchaseOrder", conf=0.80,
             rationale="Descrizione anagrafica collegata al fornitore, stabile per l'ordine."),
        dict(col="created_date", el="event_type.timestamp", event_type="Create Purchase Order", conf=0.95,
             rationale="Colonna data associata alla creazione del record header ordine."),
        dict(col="created_by", el="event_type.attribute", event_type="Create Purchase Order", conf=0.78,
             rationale="Utente che ha eseguito la transazione: tipicamente attributo dell'evento, non dell'oggetto."),
        dict(col="po_status", el="object_type.attribute", object_type="PurchaseOrder", conf=0.68,
             rationale="Possibile attributo variabile nel tempo (Released/Blocked); in questo prototipo trattato come snapshot, non come storia di cambiamenti."),
        dict(col="currency", el="object_type.attribute", object_type="PurchaseOrder", conf=0.82,
             rationale="Valuta dell'ordine, stabile per l'intero documento."),
        dict(col="total_amount", el="event_type.attribute", event_type="Create Purchase Order", conf=0.72,
             rationale="Importo calcolato al momento della creazione: attributo dell'evento di creazione."),
    ],
    "po_lines": [
        dict(col="po_number", el="object_type.key", object_type="POLine", conf=0.85,
             rationale="Componente della chiave composita di POLine, insieme a po_line_no."),
        dict(col="po_number", el="e2o_relationship", event_type="Create Purchase Order",
             related_object_type="POLine", qualifier="creates", conf=0.88,
             rationale="Le righe condividono po_number con l'header ordine: create contestualmente all'evento di creazione ordine."),
        dict(col="po_line_no", el="object_type.key", object_type="POLine", conf=0.85,
             rationale="Componente della chiave composita di POLine, insieme a po_number."),
        dict(col="material", el="object_type.attribute", object_type="POLine", conf=0.80,
             rationale="Descrizione materiale ordinato, attributo stabile della riga."),
        dict(col="quantity", el="object_type.attribute", object_type="POLine", conf=0.75,
             rationale="Quantita' ordinata: valore stabile della riga, non di un singolo evento."),
        dict(col="unit_price", el="object_type.attribute", object_type="POLine", conf=0.75,
             rationale="Prezzo unitario pattuito: attributo stabile della riga."),
        dict(col="line_amount", el="object_type.attribute", object_type="POLine", conf=0.72,
             rationale="Importo di riga derivato da quantita' x prezzo: attributo della riga."),
        dict(col="plant", el="object_type.attribute", object_type="POLine", conf=0.80,
             rationale="Stabilimento di destinazione, attributo stabile della riga."),
    ],
    "goods_receipts": [
        dict(col="gr_number", el="object_type.key", object_type="GoodsReceipt", conf=0.96,
             rationale="Valori univoci, nome colonna coerente con pattern chiave: chiave naturale di GoodsReceipt."),
        dict(col="po_number", el="e2o_relationship", event_type="Post Goods Receipt",
             related_object_type="PurchaseOrder", qualifier="for order", conf=0.84,
             rationale="Presente anche come chiave in purchase_orders: collega il ricevimento all'ordine."),
        dict(col="po_line_no", el="e2o_relationship", event_type="Post Goods Receipt",
             related_object_type="POLine", qualifier="receives against", conf=0.87,
             rationale="po_number + po_line_no combinati corrispondono alla chiave composita di POLine: il ricevimento e' evaso contro quella riga."),
        dict(col="gr_date", el="event_type.timestamp", event_type="Post Goods Receipt", conf=0.93,
             rationale="Colonna data associata alla registrazione del ricevimento merce."),
        dict(col="quantity_received", el="event_type.attribute", event_type="Post Goods Receipt", conf=0.74,
             rationale="Quantita' effettivamente ricevuta in questa transazione: attributo dell'evento."),
        dict(col="posted_by", el="event_type.attribute", event_type="Post Goods Receipt", conf=0.76,
             rationale="Utente che ha registrato il movimento: attributo dell'evento."),
    ],
    "invoices": [
        dict(col="invoice_number", el="object_type.key", object_type="Invoice", conf=0.96,
             rationale="Valori univoci, nome colonna coerente con pattern chiave: chiave naturale di Invoice."),
        dict(col="po_number", el="e2o_relationship", event_type="Post Invoice",
             related_object_type="PurchaseOrder", qualifier="for order", conf=0.85,
             rationale="Presente anche come chiave in purchase_orders: collega la fattura all'ordine."),
        dict(col="po_number", el="e2o_relationship", event_type="Post Payment",
             related_object_type="PurchaseOrder", qualifier="for order", conf=0.58,
             rationale="Stessa relazione dell'evento Post Invoice, ma dipende dalla conferma dell'evento Post Payment (vedi payment_date)."),
        dict(col="invoice_date", el="event_type.timestamp", event_type="Post Invoice", conf=0.91,
             rationale="Colonna data associata alla registrazione della fattura."),
        dict(col="invoice_amount", el="event_type.attribute", event_type="Post Invoice", conf=0.74,
             rationale="Importo fatturato in questa transazione: attributo dell'evento."),
        dict(col="currency", el="object_type.attribute", object_type="Invoice", conf=0.80,
             rationale="Valuta della fattura, stabile per il documento."),
        dict(col="posted_by", el="event_type.attribute", event_type="Post Invoice", conf=0.75,
             rationale="Utente che ha registrato la fattura: attributo dell'evento."),
        dict(col="payment_status", el="object_type.attribute", object_type="Invoice", conf=0.60,
             rationale="Ambiguo: 'Paid' potrebbe indicare un evento distinto di pagamento invece di un semplice attributo statico. Verificare insieme a payment_date."),
        dict(col="payment_date", el="event_type.timestamp", event_type="Post Payment", conf=0.58,
             rationale="La tabella contiene due colonne data plausibili come timestamp evento (invoice_date, payment_date). "
                        "Ho proposto due event type distinti (Post Invoice, Post Payment) invece di uno solo con stato: "
                        "confermare o correggere se nel processo reale il pagamento non e' un evento tracciato separatamente."),
    ],
}


class HeuristicAIMapper(AIMapper):
    """Mock dell'AI Mapping Service: stessa interfaccia di un futuro ClaudeAIMapper."""

    def propose_mapping(self, tables: list[TableSchema], context_profile: dict) -> list[MappingProposal]:
        proposals: list[MappingProposal] = []
        for table in tables:
            hint = catalog.lookup(table.name)
            if hint and table.name in _TEMPLATE_RULES:
                proposals.extend(self._from_template(table))
            else:
                proposals.extend(self._generic_fallback(table))
        return proposals

    def _from_template(self, table: TableSchema) -> list[MappingProposal]:
        out = []
        for rule in _TEMPLATE_RULES[table.name]:
            out.append(
                MappingProposal(
                    source_table=table.name,
                    source_column=rule["col"],
                    ocel_element=rule["el"],
                    object_type=rule.get("object_type"),
                    event_type=rule.get("event_type"),
                    attribute_name=rule["col"] if "attribute" in rule["el"] else None,
                    qualifier=rule.get("qualifier"),
                    related_object_type=rule.get("related_object_type"),
                    confidence=rule["conf"],
                    rationale=rule["rationale"],
                    based_on_template=catalog.TEMPLATE_ID,
                )
            )
        return out

    def _generic_fallback(self, table: TableSchema) -> list[MappingProposal]:
        """Euristica generica per tabelle non presenti nel catalogo.

        Piu' cauta: chiavi/timestamp riconosciuti per pattern di nome con
        confidence media, tutto il resto proposto a bassa confidence perche'
        senza un template di riferimento l'AI non ha basi solide per
        distinguere attributi oggetto/evento.
        """
        out = []
        object_type_guess = "".join(part.capitalize() for part in table.name.rstrip("s").split("_"))
        key_cols = [c for c in table.columns if ID_LIKE_PATTERN.search(c.name) and c.distinct_ratio > 0.95]
        date_cols = [c for c in table.columns if c.inferred_type == "date"]

        for col in table.columns:
            if col in key_cols:
                out.append(MappingProposal(
                    source_table=table.name, source_column=col.name, ocel_element="object_type.key",
                    object_type=object_type_guess, event_type=None, attribute_name=None, qualifier=None,
                    related_object_type=None, confidence=0.72,
                    rationale=f"Nome colonna coerente con pattern chiave e valori pressoche' univoci ({col.distinct_ratio:.0%}), ma tabella non presente nel catalogo: verificare.",
                    based_on_template=None,
                ))
            elif col in date_cols and not out_has_timestamp(out, table.name):
                out.append(MappingProposal(
                    source_table=table.name, source_column=col.name, ocel_element="event_type.timestamp",
                    object_type=None, event_type=f"{object_type_guess} event", attribute_name=None,
                    qualifier=None, related_object_type=None, confidence=0.55,
                    rationale="Colonna di tipo data ma tabella non riconosciuta: proposto come timestamp evento, da confermare.",
                    based_on_template=None,
                ))
            else:
                out.append(MappingProposal(
                    source_table=table.name, source_column=col.name, ocel_element="object_type.attribute",
                    object_type=object_type_guess, event_type=None, attribute_name=col.name, qualifier=None,
                    related_object_type=None, confidence=0.40,
                    rationale="Tabella non presente nel catalogo: nessun pattern noto per classificare questa colonna come attributo oggetto o evento. Richiede revisione manuale.",
                    based_on_template=None,
                ))
        return out


def out_has_timestamp(proposals: list[MappingProposal], table_name: str) -> bool:
    return any(p.source_table == table_name and p.ocel_element == "event_type.timestamp" for p in proposals)


# ---------------------------------------------------------------------------
# ClaudeAIMapper: implementazione reale con una chiamata LLM, stessa
# interfaccia di HeuristicAIMapper. A differenza del mock non ha nessuna
# conoscenza precodificata delle tabelle P2P: ragiona da zero su nomi
# tabella/colonna, tipi inferiti, valori di esempio e Process Context
# Profile - lo stesso materiale che avrebbe un revisore umano.
# ---------------------------------------------------------------------------

class LLMFieldMapping(BaseModel):
    source_table: str
    source_column: str
    ocel_element: Literal[
        "object_type.key", "object_type.attribute",
        "event_type.timestamp", "event_type.attribute", "e2o_relationship",
    ]
    object_type: Optional[str] = None
    event_type: Optional[str] = None
    attribute_name: Optional[str] = None
    qualifier: Optional[str] = None
    related_object_type: Optional[str] = None
    confidence: float
    rationale: str


class LLMMappingResponse(BaseModel):
    proposals: list[LLMFieldMapping]


_MAPPING_SYSTEM_PROMPT = """\
Sei l'AI Mapping Service di una piattaforma enterprise di process mining.
Il tuo compito e' proporre, per OGNI colonna di OGNI tabella sorgente ricevuta, un mapping verso un
log OCEL 2.0 (object-centric event log). Non hai accesso a documentazione esterna: ragiona solo dai
nomi di tabella/colonna, dai tipi inferiti, dai valori di esempio, dalle statistiche (null_ratio,
distinct_ratio) e dal contesto di processo fornito.

Per ciascuna colonna scegli una o piu' di queste categorie (ocel_element) - piu' di una riga per la
stessa colonna e' corretto quando contribuisce a piu' aspetti del modello (es. componente di chiave
composita E relazione verso un altro oggetto):

- "object_type.key": la colonna (da sola o in combinazione con altre dello stesso tipo oggetto)
  identifica univocamente un'istanza di un tipo di oggetto di business (es. numero ordine).
- "object_type.attribute": descrive un attributo stabile dell'oggetto (es. nome fornitore).
- "event_type.timestamp": la colonna e' la data/ora di un evento di processo. Una tabella
  transazionale puo' avere piu' timestamp plausibili: se cosi', proponi event type distinti,
  ciascuno con la propria confidence, invece di sceglierne uno a caso.
- "event_type.attribute": descrive un attributo specifico dell'occorrenza dell'evento (es. utente
  che ha eseguito la transazione, importo di quella transazione).
- "e2o_relationship": la colonna collega l'evento (generato dalla riga corrente) a un oggetto di un
  ALTRO tipo (es. una fattura che referenzia l'ordine d'acquisto). In questo caso valorizza anche
  event_type, related_object_type e un qualifier breve in inglese (es. "for order",
  "receives against").

Regole:
- Sii onesto sulla confidence (0.0-1.0): alta (>0.85) solo se il pattern e' inequivocabile, media
  (0.6-0.85) se plausibile ma con alternative ragionevoli, bassa (<0.6) se ambiguo o se stai
  indovinando - in questi casi la rationale deve spiegare l'ambiguita' come faresti a un revisore
  umano che deve decidere se accettare o correggere.
- Non inventare colonne o tabelle che non ti sono state fornite.
- rationale sempre in italiano, una frase sola, concreta (cita nomi di colonna/valori quando aiuta).
- Se una tabella e' puramente anagrafica/di supporto senza una data plausibile, non forzare un
  event_type.timestamp: assegna solo object_type.key/attribute a quella tabella.
"""


class ClaudeAIMapper(AIMapper):
    """Implementazione reale via Anthropic API (stessa interfaccia del mock)."""

    def __init__(self, model: str | None = None):
        import anthropic  # import locale: il pacchetto non deve essere richiesto se non si usa questa classe

        self._client = anthropic.Anthropic()
        self._model = model or ANTHROPIC_MODEL

    def propose_mapping(self, tables: list[TableSchema], context_profile: dict) -> list[MappingProposal]:
        payload = {
            "process_context": context_profile,
            "tables": [
                {
                    "name": t.name,
                    "row_count": t.row_count,
                    "columns": [
                        {
                            "name": c.name,
                            "inferred_type": c.inferred_type,
                            "sample_values": c.sample_values,
                            "null_ratio": c.null_ratio,
                            "distinct_ratio": c.distinct_ratio,
                        }
                        for c in t.columns
                    ],
                }
                for t in tables
            ],
        }

        response = self._client.messages.parse(
            model=self._model,
            max_tokens=16000,
            system=_MAPPING_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    "Schema delle tabelle sorgente e contesto di processo:\n\n"
                    + json.dumps(payload, indent=2, ensure_ascii=False)
                ),
            }],
            output_format=LLMMappingResponse,
        )

        parsed = response.parsed_output
        return [
            MappingProposal(
                source_table=p.source_table,
                source_column=p.source_column,
                ocel_element=p.ocel_element,
                object_type=p.object_type,
                event_type=p.event_type,
                attribute_name=p.attribute_name,
                qualifier=p.qualifier,
                related_object_type=p.related_object_type,
                confidence=p.confidence,
                rationale=p.rationale,
                based_on_template=None,
            )
            for p in parsed.proposals
        ]

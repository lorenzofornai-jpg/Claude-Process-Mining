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
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel

from app.config import AI_MAPPING_BUDGET_USD, AI_MAPPING_EFFORT, ANTHROPIC_MODEL
from app.connectors.base import TableSchema
from app.i18n import msg, render
from app.services import catalog, deterministic_mapping, documents

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
    # solo per event_type.activity: {valore della colonna: nome attivita'} ("" = escluso);
    # None = i valori della colonna sono gia' nomi di attivita'
    activity_values: dict[str, str] | None = None


class AIMapper(ABC):
    @abstractmethod
    def propose_mapping(
        self,
        tables: list[TableSchema],
        context_profile: dict,
        table_descriptions: dict[str, str] | None = None,
    ) -> list[MappingProposal]:
        """table_descriptions: note libere opzionali dell'utente per tabella
        (nome tabella -> testo), raccolte nel passo 'Descrivi le tabelle' prima
        della chiamata. Solo ClaudeAIMapper le usa davvero (le passa nel
        payload perche' il LLM ci ragioni sopra); HeuristicAIMapper le ignora,
        dato che non fa reasoning testuale libero."""
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


def proposals_from_rules(table: TableSchema, rules: list[dict], based_on_template: str) -> list[MappingProposal]:
    return [
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
            based_on_template=based_on_template,
            activity_values=rule.get("activity_values"),
        )
        for rule in rules
    ]


class HeuristicAIMapper(AIMapper):
    """Mock dell'AI Mapping Service: stessa interfaccia di un futuro ClaudeAIMapper."""

    def propose_mapping(
        self,
        tables: list[TableSchema],
        context_profile: dict,
        table_descriptions: dict[str, str] | None = None,
    ) -> list[MappingProposal]:
        proposals: list[MappingProposal] = []
        for table in tables:
            known = deterministic_mapping.rules_for(table, _TEMPLATE_RULES)
            if known:
                proposals.extend(proposals_from_rules(table, *known))
            else:
                activity_cols = ((context_profile or {}).get("data_profile") or {}).get("activity_columns", {})
                proposals.extend(self._generic_fallback(table, activity_cols.get(table.name, {})))
        return deterministic_mapping.finalize(proposals, tables)

    def _generic_fallback(self, table: TableSchema, activity_cols: dict[str, list[str]] | None = None) -> list[MappingProposal]:
        """Euristica generica per tabelle non presenti nel catalogo.

        Piu' cauta: chiavi/timestamp riconosciuti per pattern di nome con
        confidence media, tutto il resto proposto a bassa confidence perche'
        senza un template di riferimento l'AI non ha basi solide per
        distinguere attributi oggetto/evento.
        """
        out = []
        object_type_guess = "".join(part.capitalize() for part in table.name.rstrip("s").split("_"))
        # La quasi-unicita' dei valori e' il segnale primario di chiave: funziona
        # anche su convenzioni di naming senza suffissi inglesi (es. campi SAP
        # come EBELN, LIFNR, BELNR). Il pattern sul nome resta solo un booster
        # di confidence quando concorda, non un requisito per essere candidato.
        # Gli importi (float) sono esclusi a priori: quasi mai una chiave naturale,
        # spesso quasi-unici per caso (es. DMBTR, NETWR). Se nessuna colonna ha un
        # nome coerente con un pattern chiave, si prende solo la piu' vicina
        # all'unicita' totale invece di comporre una chiave fragile con piu'
        # colonne quasi-uniche per caso (es. nome+citta'+via di un'anagrafe).
        key_candidates = [c for c in table.columns
                          if c.distinct_ratio > 0.95 and c.inferred_type not in ("float", "date", "time")]
        name_matched_candidates = [c for c in key_candidates if ID_LIKE_PATTERN.search(c.name)]
        if name_matched_candidates:
            key_cols = name_matched_candidates
        elif key_candidates:
            key_cols = [max(key_candidates, key=lambda c: c.distinct_ratio)]
        else:
            key_cols = []
        date_cols = [c for c in table.columns if c.inferred_type == "date"]
        # come data dell'evento si preferisce quella che ha anche l'ora (es. CPUDT + CPUTM)
        first_date = next((c for c in date_cols if getattr(c, "time_column", None)), date_cols[0] if date_cols else None)
        columns = sorted(table.columns, key=lambda c: c is not first_date)

        for col in columns:
            if col in key_cols:
                name_matches = bool(ID_LIKE_PATTERN.search(col.name))
                confidence = 0.72 if name_matches else 0.58
                rationale = (
                    msg("Nome colonna coerente con pattern chiave e valori pressoché univoci ({p}), ma tabella non presente "
                        "nel catalogo: verificare.", p=f"{col.distinct_ratio:.0%}")
                    if name_matches else
                    msg("Valori pressoché univoci ({p}) suggeriscono una chiave, anche se il nome colonna non segue un "
                        "pattern noto: tabella non presente nel catalogo, verificare.", p=f"{col.distinct_ratio:.0%}")
                )
                out.append(MappingProposal(
                    source_table=table.name, source_column=col.name, ocel_element="object_type.key",
                    object_type=object_type_guess, event_type=None, attribute_name=None, qualifier=None,
                    related_object_type=None, confidence=confidence,
                    rationale=rationale,
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
        # colonna attivita' rilevata dal profilo: i suoi valori diventano le attivita' dell'evento
        timestamp = next((p for p in out if p.ocel_element == "event_type.timestamp"), None)
        if timestamp and activity_cols:
            col, values = next(iter(activity_cols.items()))
            out = [p for p in out if not (p.source_column == col and p.ocel_element == "object_type.attribute")]
            out.append(MappingProposal(
                source_table=table.name, source_column=col, ocel_element="event_type.activity",
                object_type=None, event_type=timestamp.event_type, attribute_name=None, qualifier=None,
                related_object_type=None, confidence=0.55,
                rationale=msg("Il profilo dei dati indica che {c} distingue operazioni diverse ({n} valori): ogni valore "
                              "diventa un'attività. Dai un nome leggibile ai codici o escludi quelli non di processo.",
                              c=col, n=len(values)),
                based_on_template=None, activity_values={v: v for v in values},
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

# Output compatto: una chiamata per tabella, chiavi corte e solo le colonne utili.
# Le chiavi lunghe ripetute per ogni colonna (source_table, ocel_element, ...)
# costavano da sole piu' token dei contenuti.
_ELEMENT = {
    "key": "object_type.key",
    "obj_attr": "object_type.attribute",
    "timestamp": "event_type.timestamp",
    "activity": "event_type.activity",
    "evt_attr": "event_type.attribute",
    "relation": "e2o_relationship",
}


class LLMActivityValue(BaseModel):
    v: str                                     # valore della colonna
    act: str                                   # nome attivita' ("" = non e' un'attivita' di processo)


class LLMColumnMapping(BaseModel):
    col: str                                   # colonna sorgente
    el: Literal["key", "obj_attr", "timestamp", "activity", "evt_attr", "relation"]
    obj: Optional[str] = None                  # object_type
    evt: Optional[str] = None                  # event_type
    attr: Optional[str] = None                 # attribute_name
    q: Optional[str] = None                    # qualifier
    rel: Optional[str] = None                  # related_object_type
    values: Optional[list[LLMActivityValue]] = None  # solo per "activity"
    conf: float
    why: str = ""                              # rationale (vuota se confidence alta)


class LLMTableMapping(BaseModel):
    columns: list[LLMColumnMapping]


class LLMSkeletonObject(BaseModel):
    name: str
    table: str
    key_columns: list[str]


class LLMSkeletonEvent(BaseModel):
    name: str
    table: str
    timestamp_column: str


class LLMSkeleton(BaseModel):
    object_types: list[LLMSkeletonObject]
    event_types: list[LLMSkeletonEvent]


_SKELETON_SYSTEM_PROMPT = """\
Sei l'AI Mapping Service di una piattaforma di process mining. Ricevi le tabelle sorgente di un
processo (nomi, colonne, tipi, eventuale nota dell'utente) e il contesto di processo (assessment,
attivita' BPMN, data_profile con chiavi candidate, colonne data e relazioni misurate). Definisci SOLO
lo scheletro del modello OCEL 2.0 comune a tutto il dataset, che verra' poi usato tabella per tabella.
"document_excerpts", se presente, contiene per tabella brani dei documenti del cliente (data dictionary,
manuali, procedure) che la citano: usali per capire cosa rappresenta ogni tabella.
- object_types: i tipi di oggetto di business (nome in inglese, leggibile, es. "Purchase Order"),
  la tabella che li definisce e le colonne chiave (preferisci le candidate_keys misurate);
- event_types: le attivita' di processo (nome riconoscibile, allineato al BPMN se presente), con la
  tabella e la colonna data/ora che le genera. Una tabella puo' generare piu' eventi. Se il
  data_profile indica per la tabella una colonna attivita' (activity_columns: tipo movimento, azione,
  stato, causale), quella data genera UN solo evento "contenitore" (nome generico, es. "Order History"):
  le singole attivita' verranno lette dai valori della colonna, tabella per tabella. Le colonne in
  reason_columns (motivo o causale di un'operazione) non distinguono eventi.
- Una tabella in copy_of ripete le righe di un'altra tabella (indice, vista, estrazione filtrata): non
  definisce object_types ne' event_types; oggetti ed eventi vengono dalla tabella indicata in "of".
"already_defined_model", se presente, e' il modello gia' definito dalle altre tabelle del dataset
(riconosciute senza AI): non ripeterlo, ma riusa ESATTAMENTE quei nomi quando una tabella si riferisce
agli stessi oggetti. Le tabelle anagrafiche senza date plausibili definiscono oggetti ma non eventi.
Le colonne "date(planned)" sono date previste o di scadenza (es. SAP ZFBDT, consegna prevista): non sono
fatti avvenuti e non generano eventi. Le colonne di tipo "time" contengono solo l'ora (es. SAP CPUTM, ERZET): non sono date di eventi, vengono
unite in automatico alla loro colonna data; a parita' di significato preferisci come data quella che ha
l'ora. Copri tutte le tabelle ricevute. Non inventare colonne. Risposta breve: niente spiegazioni.
"""

_MAPPING_SYSTEM_PROMPT = """\
Sei l'AI Mapping Service di una piattaforma enterprise di process mining. Ricevi UNA tabella sorgente
e proponi il mapping delle sue colonne verso un log OCEL 2.0 (object-centric event log). Ragiona dai
nomi di tabella/colonna, dai tipi, dai valori di esempio, dalle statistiche (null_ratio,
distinct_ratio) e dal contesto di processo. La nota utente sulla tabella ("user_description"), se
presente, e' affidabile e prioritaria rispetto all'inferenza dai soli nomi (utile su nomi opachi come
quelli SAP). "document_excerpts", se presente, sono brani dei documenti del cliente (data dictionary,
manuali, procedure; "doc" = file di provenienza) che citano questa tabella, le sue colonne o i suoi codici:
affidabili come la nota utente per il significato di colonne, codici e stati, e per i nomi delle attivita'.

Contesto ("process_context"):
- "assessment" (obiettivi, oggetto principale, eventi di inizio/fine, granularita' dei timestamp,
  campi personalizzati) e "bpmn_activities": orientano oggetti, chiavi e nomi degli eventi (usa i nomi
  delle attivita' BPMN quando una data corrisponde a un'attivita');
- "data_profile": evidenze MISURATE per questa tabella: candidate_keys (migliori object_type.key),
  date_columns, relationships (colonna -> chiave di un'altra tabella: candidate e2o_relationship),
  activity_columns (colonna -> tutti i suoi valori distinti: probabile colonna che dice quale
  operazione e' registrata in ogni riga), reason_columns (colonna -> valori: motivo o causale
  dell'operazione), planned_dates (date previste, con i giorni da sommare se ci sono), copy_of (la tabella
  ripete le righe di un'altra).
"already_defined_model" e' il modello comune dell'intero dataset: riusa ESATTAMENTE quei nomi per
oggetti ed eventi. "known_pattern", se presente, e' un mapping gia' validato da un umano per una
tabella con lo stesso nome: seguilo (confidence alta) salvo evidenze contrarie.

Elemento ("el") per ogni colonna mappata; piu' righe per la stessa colonna se serve:
- "key": identifica un oggetto di business (obj = tipo oggetto);
- "obj_attr": attributo stabile dell'oggetto (obj, attr);
- "timestamp": data/ora di un evento di processo (evt; obj = oggetto a cui l'evento si riferisce,
  di norma quello definito dalla tabella). Piu' date plausibili = eventi distinti;
- "evt_attr": attributo della singola occorrenza dell'evento (evt, attr), es. utente, importo;
- "relation": la colonna collega l'evento della riga a un oggetto di un ALTRO tipo
  (evt, rel = tipo oggetto collegato, q = qualifier breve in inglese, es. "for order");
- "activity": la colonna dice QUALE operazione e' avvenuta nella riga (tipo movimento, azione, stato,
  causale): evt = lo stesso evento del "timestamp" della tabella, values = un elemento per OGNI valore
  in activity_columns, {v: valore, act: nome attivita' leggibile in inglese, allineato al BPMN se
  presente}; act "" per i valori che non sono attivita' di processo. Se i valori sono gia' nomi di
  attivita' leggibili, values con act = v. Usalo solo se la colonna e' in activity_columns o il suo
  significato e' inequivocabile.

Regole per contenere costi e lavoro di revisione:
- Mappa SOLO le colonne utili al process mining: chiavi, date di processo, collegamenti, e gli
  attributi utili come dimensioni di analisi (categorie, organizzazione, area, paese, controparte,
  utente, stato, importi, quantita'). OMETTI le colonne tecniche o ridondanti (mandante, contatori,
  flag interni, testi duplicati, date di aggiornamento tecnico): una colonna omessa viene ignorata.
- conf (0.0-1.0) onesta: >0.85 solo se inequivocabile, 0.6-0.85 plausibile, <0.6 ambigua.
- why: nella lingua indicata da process_context.language (it = italiano, en = inglese), massimo 12
  parole, solo se conf <= 0.85 (spiega il dubbio al revisore);
  stringa vuota se conf > 0.85.
- Ometti i campi non pertinenti all'elemento scelto. Non inventare colonne.
- Tabella anagrafica senza date plausibili: niente "timestamp".
- Colonne con "planned" (date previste o di scadenza: scadenza di pagamento, consegna prevista, data base
  per la scadenza come SAP ZFBDT): NON sono fatti avvenuti, mai "timestamp"; mappale come attributo
  (obj_attr dell'oggetto, o evt_attr dell'evento a cui si riferiscono): servono a misurare ritardi.
- Colonne in reason_columns (motivo di blocco, rifiuto, rettifica...): "evt_attr" dell'evento della riga
  (o "obj_attr" se la tabella non genera eventi), MAI "activity": dicono perche', non quale operazione.
  Se i loro valori parlano di contestazioni o dispute, chiama l'attributo in modo che si capisca.
- Tabella con copy_of: ripete le righe della tabella "of", che gia' genera oggetti ed eventi. Niente "key"
  di nuovi oggetti, niente "timestamp": mappa solo le colonne in new_columns, come "obj_attr" dell'oggetto
  della tabella "of" (stesso nome di tipo oggetto). Se new_columns e' vuoto, non mappare nulla.
- Colonne di tipo "time" (solo ora, es. CPUTM, ERZET): mai "timestamp", non mapparle; vengono unite in
  automatico alla colonna data indicata in "with_time". A parita' di significato scegli come "timestamp"
  la data che ha "with_time" (es. CPUDT con CPUTM invece di BUDAT, che e' solo un giorno).
"""

# Prezzi in USD per milione di token (input, output). Modello sconosciuto: si usa
# il listino piu' alto tra quelli noti, cosi' il tetto di spesa resta prudente.
_PRICES_USD_PER_MTOK = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_FALLBACK_PRICE = (5.0, 25.0)
_CHARS_PER_TOKEN = 3.0          # stima prudente per JSON con testo italiano
_MIN_OUTPUT_PER_COLUMN = 25     # sotto questa soglia il mapping non sarebbe affidabile
_SKELETON_MAX_TOKENS = 6000


class MappingBudgetError(RuntimeError):
    """Il dataset e' troppo grande per il tetto di spesa del mapping AI (message: messaggio da tradurre)."""

    def __init__(self, message: dict):
        super().__init__(message.get("m", ""))
        self.message = message


def _tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN) + 50


def _profile_for_table(profile: dict | None, table: str) -> dict | None:
    """Solo la parte del profilo misurato che riguarda questa tabella."""
    if not profile:
        return None
    return {
        "candidate_keys": {k: v for k, v in profile.get("candidate_keys", {}).items() if k == table},
        "date_columns": {k: v for k, v in profile.get("date_columns", {}).items() if k == table},
        "relationships": [r for r in profile.get("relationships", [])
                          if r.startswith(f"{table}.") or f"-> {table}." in r],
        "activity_columns": profile.get("activity_columns", {}).get(table, {}),
        "reason_columns": profile.get("reason_columns", {}).get(table, {}),
        "planned_dates": profile.get("planned_dates", {}).get(table, {}),
        "copy_of": profile.get("copy_of", {}).get(table),
    }


class ClaudeAIMapper(AIMapper):
    """Implementazione reale via Anthropic API (stessa interfaccia del mock).

    Metodo misto: le tabelle riconosciute con certezza (catalogo, dizionario
    SAP standard) sono mappate senza AI; Claude riceve solo le altre, con il
    modello gia' definito come vocabolario.

    Costo sotto controllo: prima di chiamare Claude si stima l'input di ogni
    chiamata e si assegna a ciascuna un limite di output (max_tokens) tale che
    il costo massimo possibile stia nel tetto AI_MAPPING_BUDGET_USD. Se non
    tutte le tabelle ci stanno, le piu' grandi restano escluse (budget_skipped)
    e si possono rigenerare dalla revisione; se non ci sta nulla e nulla e'
    stato riconosciuto, si ferma prima di spendere (MappingBudgetError)."""

    PARALLEL_CALLS = 6

    def __init__(self, model: str | None = None):
        import anthropic  # import locale: il pacchetto non deve essere richiesto se non si usa questa classe

        self._client = anthropic.Anthropic()
        self._model = model or ANTHROPIC_MODEL
        self._price_in, self._price_out = _PRICES_USD_PER_MTOK.get(self._model, _FALLBACK_PRICE)
        self._lock = threading.Lock()
        self.spent_usd = 0.0
        self.last_missing_tables: list[str] = []
        self.budget_skipped: list[str] = []
        self.known_tables: list[str] = []

    @property
    def budget_usd(self) -> float:
        return AI_MAPPING_BUDGET_USD

    @staticmethod
    def _known_pattern_for(table_name: str, current_columns: list[str]) -> list[dict] | None:
        """Pattern gia' confermato in catalogo per una tabella con lo stesso nome
        (None se le colonne non si sovrappongono a sufficienza)."""
        dynamic = catalog.dynamic_lookup(table_name, current_columns)
        if dynamic is None:
            return None
        _, rules = dynamic
        return [
            {"column": r["col"], "ocel_element": r["el"], "object_type": r.get("object_type"),
             "event_type": r.get("event_type"), "qualifier": r.get("qualifier"),
             "related_object_type": r.get("related_object_type"),
             **({"activity_values": r["activity_values"]} if r.get("activity_values") else {})}
            for r in rules
        ]

    # ---- costi -------------------------------------------------------------
    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self._price_in + tokens_out * self._price_out) / 1_000_000

    def _record(self, usage) -> None:
        tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25 \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
        with self._lock:
            self.spent_usd += self._cost(int(tokens_in), usage.output_tokens or 0)

    def _stream(self, system: str, content: str, max_tokens: int, output_format):
        with self._client.messages.stream(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": AI_MAPPING_EFFORT},
            output_format=output_format,
        ) as stream:
            response = stream.get_final_message()
        self._record(response.usage)
        print(
            f"ClaudeAIMapper: stop_reason={response.stop_reason}, token in/out="
            f"{response.usage.input_tokens}/{response.usage.output_tokens} (max {max_tokens}), "
            f"spesa finora $ {self.spent_usd:.3f}"
        )
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            raise RuntimeError(f"risposta non completa (stop_reason={response.stop_reason})")
        return response.parsed_output

    # ---- payload -----------------------------------------------------------
    def _table_content(self, t: TableSchema, context_profile: dict, descriptions: dict[str, str],
                       vocabulary: dict | None) -> str:
        context = {k: v for k, v in context_profile.items() if k not in ("data_profile", "document_excerpts")}
        context["data_profile"] = _profile_for_table(context_profile.get("data_profile"), t.name)
        excerpts = (context_profile.get("document_excerpts") or {}).get(t.name)
        payload = {
            "process_context": context,
            "already_defined_model": vocabulary,
            "table": {
                "name": t.name,
                "row_count": t.row_count,
                "user_description": descriptions.get(t.name),
                **({"document_excerpts": excerpts} if excerpts else {}),
                "known_pattern": self._known_pattern_for(t.name, [c.name for c in t.columns]),
                "columns": [
                    {"name": c.name, "type": c.inferred_type, "samples": c.sample_values[:3],
                     "null_ratio": c.null_ratio, "distinct_ratio": c.distinct_ratio,
                     **({"with_time": c.time_column} if getattr(c, "time_column", None) else {}),
                     **({"planned": render("it", c.planned_reason)} if getattr(c, "planned_reason", None) else {})}
                    for c in t.columns
                ],
            },
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _skeleton_content(tables: list[TableSchema], context_profile: dict, descriptions: dict[str, str],
                          vocabulary: dict | None = None) -> str:
        context = {k: v for k, v in context_profile.items() if k != "document_excerpts"}
        names = {t.name for t in tables}
        excerpts = {k: v for k, v in (context_profile.get("document_excerpts") or {}).items() if k in names}
        if excerpts:
            context["document_excerpts"] = documents.for_skeleton(excerpts)
        payload = {
            "process_context": context,
            "already_defined_model": vocabulary,
            "tables": [
                {"name": t.name, "rows": t.row_count, "user_description": descriptions.get(t.name),
                 "columns": [f"{c.name}:{c.inferred_type}" + ("(planned)" if getattr(c, "planned_reason", None) else "")
                             for c in t.columns]}
                for t in tables
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # ---- mapping -----------------------------------------------------------
    def _plan(self, unknown: list[TableSchema], context_profile: dict, descriptions: dict[str, str],
              vocabulary: dict | None, use_skeleton: bool) -> tuple[dict, dict, list[TableSchema]] | None:
        """Limiti di output per tabella che garantiscono il tetto di spesa.
        Se non ci stanno tutte, esclude le tabelle piu' grandi finche' ci stanno."""
        candidates = sorted(unknown, key=lambda t: len(t.columns))
        vocab_tokens = 1500 if use_skeleton else _tokens(json.dumps(vocabulary or {}))
        while candidates:
            skeleton = use_skeleton and len(candidates) > 1
            sk_cost = self._cost(_tokens(_SKELETON_SYSTEM_PROMPT + self._skeleton_content(
                candidates, context_profile, descriptions, vocabulary)), _SKELETON_MAX_TOKENS) if skeleton else 0.0
            table_in = {
                t.name: _tokens(_MAPPING_SYSTEM_PROMPT + self._table_content(t, context_profile, descriptions, None))
                + vocab_tokens
                for t in candidates
            }
            out_tokens = (self.budget_usd - sk_cost - self._cost(sum(table_in.values()), 0)) / self._price_out * 1_000_000
            total_cols = sum(len(t.columns) for t in candidates) or 1
            per_col = out_tokens / total_cols
            if per_col >= _MIN_OUTPUT_PER_COLUMN:
                max_out = {t.name: max(1024, min(32000, int(per_col * len(t.columns)))) for t in candidates}
                print(f"ClaudeAIMapper: piano di spesa ≤ $ {self.budget_usd:.2f}, "
                      f"~{int(per_col)} token di output per colonna su {len(candidates)} tabelle")
                return table_in, max_out, candidates
            candidates = candidates[:-1]  # esclude la tabella piu' grande
        return None

    def propose_mapping(
        self,
        tables: list[TableSchema],
        context_profile: dict,
        table_descriptions: dict[str, str] | None = None,
        vocabulary: dict | None = None,
        progress=None,
    ) -> list[MappingProposal]:
        """1. mapping deterministico delle tabelle riconosciute (nessun costo);
        2. modello comune per le altre (chiamata breve), partendo da quanto gia' definito;
        3. una chiamata per tabella non riconosciuta, in parallelo, entro il tetto di spesa.
        Le tabelle non riuscite si ritentano solo se il budget residuo lo consente."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        descriptions = table_descriptions or {}
        mappable = [t for t in tables if t.columns]
        report = progress or (lambda info: None)

        # --- 1. deterministico
        proposals: list[MappingProposal] = []
        unknown: list[TableSchema] = []
        for t in mappable:
            known = deterministic_mapping.rules_for(t, _TEMPLATE_RULES)
            if known:
                proposals.extend(proposals_from_rules(t, *known))
                self.known_tables.append(t.name)
            else:
                unknown.append(t)
        print(f"ClaudeAIMapper: {len(self.known_tables)} tabelle riconosciute senza AI, {len(unknown)} a Claude")
        if proposals and vocabulary is None:
            vocabulary = _vocabulary_from(proposals)

        state = {"phase": "model" if unknown else "tables", "total": len(mappable), "done": list(self.known_tables),
                 "known": list(self.known_tables), "running": [], "budget_usd": self.budget_usd, "spent_usd": 0.0}
        report(dict(state))

        if unknown:
            # --- piano di spesa, prima di chiamare Claude
            use_skeleton = len(unknown) > 1
            plan = self._plan(unknown, context_profile, descriptions, vocabulary, use_skeleton)
            if plan is None:
                self.budget_skipped = [t.name for t in unknown]
                if not proposals:
                    total_cols = sum(len(t.columns) for t in unknown)
                    raise MappingBudgetError(msg(
                        "Con {t} tabelle e {c} colonne da mappare con l'AI il costo supererebbe il limite di $ {b} per "
                        "elaborazione. Carica meno tabelle (o solo le colonne che servono) e riprova; il limite si cambia "
                        "con AI_MAPPING_BUDGET_USD nel file .env.", t=len(unknown), c=total_cols, b=f"{self.budget_usd:.2f}"))
                unknown = []
            else:
                table_in, max_out, unknown_in_budget = plan
                self.budget_skipped = [t.name for t in unknown if t not in unknown_in_budget]
                unknown = unknown_in_budget

        if unknown and len(unknown) > 1:
            try:
                sk = self._stream(_SKELETON_SYSTEM_PROMPT,
                                  self._skeleton_content(unknown, context_profile, descriptions, vocabulary),
                                  _SKELETON_MAX_TOKENS, LLMSkeleton)
                merged = dict(vocabulary or {})
                objects = dict(merged.get("object_types", {}))
                objects.update({o.name: {"table": o.table, "key_columns": o.key_columns} for o in sk.object_types})
                events = list(merged.get("event_types", []))
                events += [{"name": e.name, "table": e.table, "timestamp_column": e.timestamp_column}
                           for e in sk.event_types]
                vocabulary = {"object_types": objects, "event_types": events}
            except Exception as exc:
                print(f"ClaudeAIMapper: modello comune non riuscito ({exc!r}), procedo senza.")

        def one(t: TableSchema) -> list[MappingProposal]:
            parsed = self._stream(_MAPPING_SYSTEM_PROMPT, self._table_content(t, context_profile, descriptions, vocabulary),
                                  max_out[t.name], LLMTableMapping)
            known_cols = {c.name for c in t.columns}
            return [
                MappingProposal(
                    source_table=t.name, source_column=m.col, ocel_element=_ELEMENT[m.el],
                    object_type=m.obj, event_type=m.evt, attribute_name=m.attr, qualifier=m.q,
                    related_object_type=m.rel, confidence=m.conf,
                    rationale=m.why or "Mapping evidente da nome e valori della colonna.",
                    based_on_template=None,
                    activity_values=({x.v: x.act for x in m.values} or None) if m.el == "activity" and m.values else None,
                )
                for m in parsed.columns if m.col in known_cols
            ]

        pending = list(unknown)
        for attempt in (1, 2):
            skipped_now: list[TableSchema] = []
            if attempt == 2 and pending:
                # secondo tentativo solo se il budget residuo copre il caso peggiore
                reserve = self.budget_usd - self.spent_usd
                affordable = []
                for t in pending:
                    worst = self._cost(table_in[t.name], max_out[t.name])
                    if worst <= reserve:
                        affordable.append(t)
                        reserve -= worst
                    else:
                        skipped_now.append(t)
                pending = affordable
            if not pending:
                pending = skipped_now
                break
            state.update(phase="tables" if attempt == 1 else "retry", running=[t.name for t in pending],
                         spent_usd=round(self.spent_usd, 3))
            report(dict(state))
            failed: list[TableSchema] = []
            with ThreadPoolExecutor(max_workers=min(self.PARALLEL_CALLS, len(pending))) as pool:
                futures = {pool.submit(one, t): t for t in pending}
                for fut in as_completed(futures):
                    t = futures[fut]
                    try:
                        extra = fut.result()
                    except Exception as exc:
                        print(f"ClaudeAIMapper: tabella {t.name} non riuscita al tentativo {attempt} ({exc!r}).")
                        extra = []
                    state["running"] = [n for n in state["running"] if n != t.name]
                    if extra:
                        proposals.extend(extra)
                        state["done"] = state["done"] + [t.name]
                    else:
                        failed.append(t)
                    state["spent_usd"] = round(self.spent_usd, 3)
                    report(dict(state))
            pending = failed + skipped_now
        self.last_missing_tables = [t.name for t in pending] + self.budget_skipped
        print(f"ClaudeAIMapper: mapping completato, spesa $ {self.spent_usd:.3f} (limite $ {self.budget_usd:.2f})")
        if not proposals:
            raise RuntimeError("Claude non ha prodotto nessuna proposta di mapping")
        proposals = deterministic_mapping.finalize(proposals, mappable)
        order = {t.name: i for i, t in enumerate(mappable)}
        proposals.sort(key=lambda p: order.get(p.source_table, len(order)))
        return proposals


def _vocabulary_from(proposals: list[MappingProposal]) -> dict:
    """Modello gia' definito (oggetti con tabella e chiave, eventi con tabella e data)."""
    objects: dict[str, dict] = {}
    events: list[dict] = []
    for p in proposals:
        if p.ocel_element == "object_type.key" and p.object_type:
            o = objects.setdefault(p.object_type, {"table": p.source_table, "key_columns": []})
            o["key_columns"].append(p.source_column)
        elif p.ocel_element == "event_type.timestamp" and p.event_type:
            events.append({"name": p.event_type, "table": p.source_table, "timestamp_column": p.source_column})
    return {"object_types": objects, "event_types": events}

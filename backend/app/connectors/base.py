"""Contratto comune a tutti i connettori (architettura a plugin).

Ogni sistema sorgente (SAP, Salesforce, ServiceNow, File...) implementa
questa stessa interfaccia. L'Orchestrator, l'AI Mapping Service e il
Transformation Engine dipendono solo da questo contratto, mai da un
connettore specifico: aggiungere un nuovo sistema significa scrivere un
nuovo modulo che implementa Connector, senza toccare il resto della
pipeline di ingestion.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ColumnSchema:
    name: str
    inferred_type: str  # "string" | "integer" | "float" | "date" | "boolean"
    sample_values: list[str] = field(default_factory=list)
    null_ratio: float = 0.0
    distinct_ratio: float = 0.0  # 1.0 = tutti i valori distinti (candidato chiave)


@dataclass
class TableSchema:
    name: str
    row_count: int
    columns: list[ColumnSchema]


class Connector(ABC):
    """Contratto di un connettore verso un sistema sorgente."""

    plugin_id: str = "base"

    @abstractmethod
    def discover_schema(self) -> list[TableSchema]:
        """Ritorna lo schema delle tabelle disponibili (senza leggere tutti i dati)."""

    @abstractmethod
    def extract_sample(self, table_name: str, limit: int = 20) -> list[dict]:
        """Ritorna un campione di righe per la tabella indicata."""

    @abstractmethod
    def extract_full(self, table_name: str, watermark: str | None = None) -> list[dict]:
        """Ritorna tutte le righe (o le righe successive al watermark, se incremental)."""

    def capabilities(self) -> dict:
        return {"sample": True, "full": True, "incremental": False}

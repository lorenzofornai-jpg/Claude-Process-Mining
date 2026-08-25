"""Connettore baseline: upload/lettura di file CSV/TXT.

E' trattato come un connettore a tutti gli effetti (stesso contratto degli
altri), non come caso speciale: questo e' cio' che permette, in futuro, di
gestire uno "snapshot" ricaricato come nuovo watermark con la stessa logica
usata per un refresh incrementale da sistema.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.connectors.base import ColumnSchema, Connector, TableSchema


def _infer_column_type(series: pd.Series) -> str:
    non_null = series.dropna()
    if non_null.empty:
        return "string"

    if pd.to_numeric(non_null, errors="coerce").notna().all():
        as_num = pd.to_numeric(non_null, errors="coerce")
        return "integer" if (as_num == as_num.astype("Int64", errors="ignore")).all() else "float"

    parsed_dates = pd.to_datetime(non_null, errors="coerce", format=None)
    if parsed_dates.notna().mean() > 0.9:
        return "date"

    lowered = non_null.astype(str).str.lower()
    if lowered.isin(["true", "false", "yes", "no"]).all():
        return "boolean"

    return "string"


class FileConnector(Connector):
    plugin_id = "file_connector"

    def __init__(self, file_paths: list[Path]):
        self._files = {p.stem: p for p in file_paths}
        self._cache: dict[str, pd.DataFrame] = {}

    def _load(self, table_name: str) -> pd.DataFrame:
        if table_name not in self._cache:
            path = self._files[table_name]
            self._cache[table_name] = pd.read_csv(path, dtype=str, keep_default_na=True)
        return self._cache[table_name]

    def discover_schema(self) -> list[TableSchema]:
        schemas = []
        for table_name in self._files:
            df = self._load(table_name)
            columns = []
            for col in df.columns:
                series = df[col]
                columns.append(
                    ColumnSchema(
                        name=col,
                        inferred_type=_infer_column_type(series),
                        sample_values=series.dropna().astype(str).unique().tolist()[:5],
                        null_ratio=round(series.isna().mean(), 3),
                        distinct_ratio=round(series.nunique(dropna=True) / max(len(series), 1), 3),
                    )
                )
            schemas.append(TableSchema(name=table_name, row_count=len(df), columns=columns))
        return schemas

    def extract_sample(self, table_name: str, limit: int = 20) -> list[dict]:
        df = self._load(table_name)
        return df.head(limit).to_dict(orient="records")

    def extract_full(self, table_name: str, watermark: str | None = None) -> list[dict]:
        df = self._load(table_name)
        return df.to_dict(orient="records")

    def capabilities(self) -> dict:
        return {"sample": True, "full": True, "incremental": False}

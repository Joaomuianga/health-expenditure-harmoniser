"""Source adapters: read a country file (csv / excel / json) into a list of (locator, payload) pairs.

Adapters only *read* - they never interpret. Interpretation (field mapping, parsing) is in harmonise.py and is
driven by the country YAML. A new file format = one new function here; a new country in a known format = YAML only.
"""
from __future__ import annotations
import csv, json
from dataclasses import dataclass, field
from pathlib import Path
import pandas as pd
from .config import sha256_file


@dataclass
class SourceData:
    path: Path
    sha256: str
    fmt: str
    records: list[tuple[str, dict]] = field(default_factory=list)      # (source_locator, payload)
    control: tuple[str, dict] | None = None                             # footer/total row, if any
    metadata: dict = field(default_factory=dict)
    coa_labels: dict[str, str] = field(default_factory=dict)            # account_code -> label, when the source ships a CoA


def _read_csv(path: Path, src: dict) -> SourceData:
    sd = SourceData(path, sha256_file(path), "csv")
    with open(path, newline="", encoding=src.get("encoding", "utf-8")) as f:
        rd = csv.DictReader(f, delimiter=src.get("delimiter", ","))
        for i, row in enumerate(rd, start=2):                            # line 1 is the header
            sd.records.append((f"line {i}", dict(row)))
    return sd


def _read_excel(path: Path, src: dict) -> SourceData:
    sd = SourceData(path, sha256_file(path), "excel")
    hdr = src["header_row"] - 1
    raw = pd.read_excel(path, sheet_name=src["sheet"], header=None, dtype=str, keep_default_na=False)
    sd.metadata["preamble"] = [str(v) for v in raw.iloc[:hdr, 0].tolist() if str(v).strip()]
    cols = [str(c).strip() for c in raw.iloc[hdr].tolist()]
    ct = src.get("control_total")
    for i in range(hdr + 1, len(raw)):
        row = dict(zip(cols, [str(v) for v in raw.iloc[i].tolist()]))
        locator = f"{src['sheet']}!row {i + 1}"
        if ct and row.get(ct["id_column"], "").strip() == ct["marker"]:
            sd.control = (locator, row)
        elif any(v.strip() for v in row.values()):
            sd.records.append((locator, row))
    if src.get("coa_sheet"):
        coa = pd.read_excel(path, sheet_name=src["coa_sheet"], dtype=str, keep_default_na=False)
        sd.coa_labels = {str(a).strip(): str(b).strip() for a, b in zip(coa.iloc[:, 0], coa.iloc[:, 1])}
    return sd


def _read_json(path: Path, src: dict) -> SourceData:
    sd = SourceData(path, sha256_file(path), "json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    sd.metadata = doc.get(src.get("metadata_path", "metadata"), {}) or {}
    for i, rec in enumerate(doc[src["records_path"]]):
        sd.records.append((f"$.{src['records_path']}[{i}]", rec))
    return sd


_READERS = {"csv": _read_csv, "excel": _read_excel, "json": _read_json}


def read_source(cfg: dict, raw_dir: Path) -> SourceData:
    src = cfg["source"]
    path = Path(raw_dir) / src["file"]
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - put the assessment files in {raw_dir}")
    return _READERS[src["format"]](path, src)

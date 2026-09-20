from __future__ import annotations
import hashlib, os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DEFAULT_RAW_DIR = ROOT / "data" / "raw"
DEFAULT_DB = Path(os.environ.get("HXH_DB", ROOT / "db" / "health_expenditure.sqlite"))


def load_country_configs() -> list[dict]:
    """One YAML per country. Adding a country = adding a file here (+ CoA rows), not editing code."""
    return [yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted((CONFIG_DIR / "countries").glob("*.yaml"))]


def config_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(CONFIG_DIR.rglob("*")):
        if p.is_file():
            h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()[:12]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

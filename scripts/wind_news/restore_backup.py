"""Validate a consistent DuckDB backup and copy it to a NEW isolated file.

Never restores over an operating database; a service switch is a separate step.
Usage: python scripts/wind_news/restore_backup.py SNAPSHOT --sha256 HASH --output NEW_DB
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil


def restore(snapshot, expected_hash, output):
    import duckdb
    source = Path(snapshot).absolute()
    target = Path(output).absolute()
    if source.is_symlink() or not source.is_file():
        raise ValueError("backup_must_be_regular_file")
    source = source.resolve(strict=True)
    if target.exists() or target.is_symlink():
        raise ValueError("restore_destination_must_be_new")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    if checksum != expected_hash.lower():
        raise ValueError("backup_hash_mismatch")
    with duckdb.connect(str(source), read_only=True) as db:
        tables = {row[0] for row in db.execute("SHOW TABLES").fetchall()}
        if not {"issues", "articles", "jobs", "deliveries"}.issubset(tables):
            raise ValueError("not_a_wind_news_database")
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("issues", "articles", "jobs", "deliveries")}
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting a destination created during validation.
    with source.open("rb") as src, target.open("xb") as dest:
        shutil.copyfileobj(src, dest)
    if hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
        raise ValueError("restored_hash_mismatch")
    return {"sha256": checksum, "counts": counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = restore(args.snapshot, args.sha256, args.output)
    except (ValueError, OSError) as exc:
        # Do not expose rows, runtime paths, or SQL/connection details.
        parser.exit(1, "Restore refused; verify backup hash, destination and service state.\n")
    print("Isolated restore verified:", result["counts"])


if __name__ == "__main__":
    main()

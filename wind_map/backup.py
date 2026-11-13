"""Consistent SQLite backup plus only the immutable originals referenced by that backup."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
from .core import WindError, atomic_write, canonical, utc_now

ASSET = re.compile(r"^originals/[a-f0-9]{2}/[a-f0-9]{64}$")


def new_directory(path):
    path = Path(path).absolute()
    if path.exists() or any(p.is_symlink() for p in [path, *path.parents]):
        raise WindError("Backup/restore destination must be new and must not use symlinks")
    path.mkdir(parents=True)
    return path


def documents_in(db_path):
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise WindError("Backup database integrity failed")
        if db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != "1":
            raise WindError("Unsupported backup schema")
        return [json.loads(r[0]) for r in db.execute("SELECT data FROM documents")]


def checked_asset(root, doc):
    asset = doc["asset"].replace("\\", "/")
    if not ASSET.fullmatch(asset) or asset != "originals/" + doc["sha256"][:2] + "/" + doc["sha256"]:
        raise WindError("Invalid original asset reference")
    path = root / asset
    if any(p.is_symlink() for p in [path, *path.parents]) or not path.resolve().is_relative_to(root.resolve()):
        raise WindError("Unsafe original asset path")
    if hashlib.sha256(path.read_bytes()).hexdigest() != doc["sha256"]:
        raise WindError("Original asset integrity failed")
    return path, asset


def backup_bundle(store, destination):
    root = new_directory(destination)
    database = root / "wind-map.sqlite3"
    store.backup(database)
    docs = documents_in(database)
    files = {"wind-map.sqlite3": hashlib.sha256(database.read_bytes()).hexdigest()}
    for doc in docs:
        source, asset = checked_asset(store.root, doc)
        target = root / asset
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
        files[asset] = doc["sha256"]
    manifest = {"schema_version": 1, "created_at": utc_now(), "files": files}
    atomic_write(root / "complete.json", canonical(manifest))
    verify_bundle(root)
    return {"path": str(root), "documents": len(docs), "files": len(files)}


def verify_bundle(root):
    root = Path(root).absolute()
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise WindError("Unsafe backup directory")
    manifest = json.loads((root / "complete.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
        raise WindError("Incomplete backup bundle")
    for relative, digest in manifest["files"].items():
        if relative != "wind-map.sqlite3" and not ASSET.fullmatch(relative):
            raise WindError("Unexpected backup file")
        path = root / relative
        if any(p.is_symlink() for p in [path, *path.parents]) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise WindError("Backup hash mismatch")
    docs = documents_in(root / "wind-map.sqlite3")
    expected = {"wind-map.sqlite3"}
    for doc in docs:
        _, asset = checked_asset(root, doc); expected.add(asset)
    if set(manifest["files"]) != expected:
        raise WindError("Backup originals do not reconcile")
    return manifest


def restore_bundle(source, destination):
    source = Path(source).absolute()
    manifest = verify_bundle(source)
    target = new_directory(destination)
    # No active runtime is overwritten. The caller selects this restored runtime separately.
    for relative in manifest["files"]:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, path)
    documents_in(target / "wind-map.sqlite3")
    for doc in documents_in(target / "wind-map.sqlite3"):
        checked_asset(target, doc)
    atomic_write(target / "restored-from.json", canonical({"source": str(source), "restored_at": utc_now()}))
    return {"runtime": str(target), "status": "restored"}
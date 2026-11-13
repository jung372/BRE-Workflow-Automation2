"""Validate complete immutable versions before changing a pointer or pushing a dedicated clone."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
from .core import WindError, atomic_write, canonical
from .snapshot import PREFIX, write_snapshot
from news.locks import file_lock

ID = re.compile(r"^[a-f0-9]{24}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
RELATIVE = re.compile(r"^(?:regions|index|manifest)\.json$|^projects/[A-Za-z0-9_-]{1,100}\.json$")


def load_checked(root, relative, digest=None):
    root = Path(root).absolute()
    target = root / relative
    if any(p.is_symlink() for p in [target, *target.parents]) or not target.resolve().is_relative_to(root.resolve()):
        raise WindError("Unsafe public artifact path")
    raw = target.read_bytes()
    if len(raw) > 30_000_000:
        raise WindError("Oversized public artifact")
    if digest and (not SHA.fullmatch(digest) or hashlib.sha256(raw).hexdigest() != digest):
        raise WindError("Public artifact hash mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise WindError("Unsupported public schema")
    return value


def validate_descriptor(root, descriptor, *, allow_preview=False):
    if not isinstance(descriptor, dict) or not ID.fullmatch(descriptor.get("snapshot_id", "")):
        raise WindError("Invalid snapshot descriptor")
    ident = descriptor["snapshot_id"]
    base = PREFIX + "versions/" + ident + "/"
    if descriptor.get("manifest") != base + "manifest.json":
        raise WindError("Manifest escapes snapshot")
    manifest = load_checked(root, descriptor["manifest"], descriptor.get("sha256"))
    if manifest.get("snapshot_id") != ident or type(manifest.get("preview")) is not bool:
        raise WindError("Invalid manifest identity")
    if manifest["preview"] and not allow_preview:
        raise WindError("Preview is not a publication")
    paths = manifest.get("files", {})
    if not isinstance(paths, dict) or not {"regions.json", "index.json"} <= paths.keys():
        raise WindError("Missing public snapshot files")
    values = {}
    for path, digest in paths.items():
        if not RELATIVE.fullmatch(path) or path == "manifest.json":
            raise WindError("Unexpected public artifact")
        value = load_checked(root, base + path, digest)
        if value.get("snapshot_id") != ident:
            raise WindError("Mixed snapshot versions")
        values[path] = value
    projects = values["index.json"].get("projects")
    if not isinstance(projects, list) or len({p["project_id"] for p in projects}) != len(projects):
        raise WindError("Invalid project index")
    expected = {f'projects/{p["project_id"]}.json' for p in projects} | {"regions.json", "index.json"}
    if expected != paths.keys():
        raise WindError("Project details do not reconcile with index")
    for p in projects:
        detail = values[f'projects/{p["project_id"]}.json']
        if detail.get("project_id") != p["project_id"]:
            raise WindError("Wrong project detail")
        sources = {s["id"] for s in detail["sources"]}
        for permit in detail["permits"]:
            if not set(permit["evidence_ids"]) <= sources:
                raise WindError("Missing public permit evidence")
        for fact in detail["milestones"]:
            if fact["evidence_id"] not in sources:
                raise WindError("Missing public milestone evidence")
    regions = values["regions.json"]
    if regions.get("preview") != manifest["preview"] or regions.get("as_of") != manifest["as_of"]:
        raise WindError("Mismatched publication metadata")
    if set(regions.get("totals", {})) != {"all", "valid", "review", "approved_pending", "cancelled"}:
        raise WindError("Missing capacity scopes")
    for scope, rows in regions["totals"].items():
        by_id = {r["region_id"]: r for r in rows}
        if "KR" not in by_id or len(by_id) != len(rows):
            raise WindError("Invalid regional totals")
        for ident, row in by_id.items():
            for key in ("capacity_w", "unallocated_w", "permit_count"):
                if type(row[key]) is not int or row[key] < 0:
                    raise WindError("Invalid exact total")
            children = [r for r in rows if r["parent_id"] == ident]
            if children or ident == "KR":
                if row["capacity_w"] != row["unallocated_w"] + sum(r["capacity_w"] for r in children):
                    raise WindError("Regional totals do not reconcile")
            if row["permit_count"] != len(set(row["permit_ids"])):
                raise WindError("Permit count does not reconcile")
    return manifest


def validate_tree(root, *, allow_preview=False):
    pointer = load_checked(root, PREFIX + "current.json")
    validate_descriptor(root, pointer.get("current"), allow_preview=allow_preview)
    if pointer.get("previous"):
        validate_descriptor(root, pointer["previous"], allow_preview=allow_preview)
    return pointer


def rollback(root):
    pointer = load_checked(root, PREFIX + "current.json")
    if not pointer.get("previous"):
        raise WindError("No verified previous version")
    validate_descriptor(root, pointer["previous"])
    old = pointer.get("current")
    try:
        validate_descriptor(root, old)
    except (WindError, OSError, ValueError):
        old = None
    pointer["current"], pointer["previous"] = pointer["previous"], old
    atomic_write(Path(root) / PREFIX / "current.json", canonical(pointer))
    return pointer


def git(root, *args, check=True):
    result = subprocess.run(["git", "-C", str(root), "-c", "core.autocrlf=false", "-c", "core.eol=lf", *args], text=True, capture_output=True, timeout=120)
    if check and result.returncode:
        # Git output may contain remote credentials. Do not echo it.
        raise WindError("Git operation failed: " + args[0])
    return result


def publish(snapshot, clone, *, expected_remote, branch="main", attempts=3):
    """Only a clean, dedicated marked clone. Rebase concurrent non-wind publications, never force."""
    supplied = Path(clone).absolute()
    if any(p.is_symlink() for p in [supplied, *supplied.parents]):
        raise WindError("Unsafe publication clone path")
    root = supplied.resolve()
    marker = root / ".git" / "bre-wind-map-publisher"
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != expected_remote:
        raise WindError("Dedicated publication clone marker is required")
    if git(root, "remote", "get-url", "origin").stdout.strip() != expected_remote:
        raise WindError("Unexpected publication remote")
    if not re.fullmatch(r"[A-Za-z0-9/_-]+", branch) or not 1 <= attempts <= 5:
        raise WindError("Invalid publication arguments")
    with file_lock(root / ".git" / "bre-publication.lock", timeout=30):
        if git(root, "status", "--porcelain").stdout.strip():
            raise WindError("Publication clone must be clean")
        if git(root, "branch", "--show-current").stdout.strip() != branch:
            raise WindError("Publication clone is on the wrong branch")
        git(root, "fetch", "origin", branch)
        git(root, "merge", "--ff-only", "origin/" + branch)
        # Unpushed local commits could include unrelated files.
        if git(root, "rev-list", "--count", "origin/" + branch + "..HEAD").stdout.strip() != "0":
            raise WindError("Publication clone contains unpushed commits")
        write_snapshot(snapshot, root)
        validate_tree(root)
        changed = git(root, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
        if any(not line[3:].replace("\\", "/").startswith(PREFIX) for line in changed):
            raise WindError("Publication changes escaped wind-map subtree")
        git(root, "add", "--", PREFIX)
        if git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return {"status": "unchanged", "snapshot_id": snapshot["snapshot_id"]}
        git(root, "commit", "-m", "Publish wind map " + snapshot["snapshot_id"])
        for attempt in range(attempts):
            if git(root, "push", "origin", "HEAD:" + branch, check=False).returncode == 0:
                return {"status": "pushed", "snapshot_id": snapshot["snapshot_id"]}
            if attempt + 1 == attempts:
                break
            git(root, "fetch", "origin", branch)
            result = git(root, "rebase", "origin/" + branch, check=False)
            if result.returncode:
                git(root, "rebase", "--abort", check=False)
                raise WindError("Concurrent publication conflict; local commit retained")
            validate_tree(root)
        raise WindError("Push retry limit reached; local commit retained")

def verify_remote(base_url, *, expected_snapshot=None, client=None):
    """Read-only post-publication verification against actual served bytes."""
    from tempfile import TemporaryDirectory
    from urllib.parse import urlsplit
    from .core import safe_url
    from .sources import SourceClient
    base_url = safe_url(base_url).rstrip("/") + "/"
    client = client or SourceClient([urlsplit(base_url).hostname], interval=0.05)
    pointer_raw = client.fetch(base_url + PREFIX + "current.json")["content"]
    pointer = json.loads(pointer_raw)
    descriptor = pointer.get("current")
    if not isinstance(descriptor, dict) or not ID.fullmatch(descriptor.get("snapshot_id","")):
        raise WindError("Remote snapshot descriptor is invalid")
    ident = descriptor["snapshot_id"]
    if expected_snapshot and ident != expected_snapshot:
        raise WindError("Pages has not served the expected snapshot yet")
    base = PREFIX + "versions/" + ident + "/"
    if descriptor.get("manifest") != base + "manifest.json":
        raise WindError("Remote manifest path is invalid")
    manifest_raw = client.fetch(base_url + descriptor["manifest"])["content"]
    if hashlib.sha256(manifest_raw).hexdigest() != descriptor.get("sha256"):
        raise WindError("Remote manifest hash mismatch")
    manifest = json.loads(manifest_raw)
    with TemporaryDirectory(prefix="bre-wind-verify-") as temporary:
        root = Path(temporary)
        atomic_write(root / descriptor["manifest"], manifest_raw)
        for relative, digest in manifest.get("files",{}).items():
            if not RELATIVE.fullmatch(relative) or relative == "manifest.json":
                raise WindError("Unexpected remote artifact")
            raw = client.fetch(base_url + base + relative)["content"]
            if hashlib.sha256(raw).hexdigest() != digest:
                raise WindError("Remote artifact hash mismatch")
            atomic_write(root / base / relative, raw)
        validate_descriptor(root, descriptor)
    return {"status":"verified","snapshot_id":ident,"files":len(manifest["files"])+1}

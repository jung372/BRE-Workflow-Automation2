"""Immutable public snapshots and a narrowly scoped, independent Git publisher."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import date
from pathlib import Path

from .collector import safe_public_url

PREFIX = "data/wind-news/"
ISSUE_FIELDS = {"schema_version", "issue_id", "issue_date", "revision", "timezone", "window_start", "window_end",
                "published_at", "content_status", "headline_summary", "correction_reason", "corrected_at"}
ITEM_FIELDS = {"headline", "title", "summary", "companies", "project_name", "region", "wind_type", "primary_category",
               "contract_stage", "tags", "source_name", "source_url", "source_published_at", "event_id",
               "representative_article_id", "evidence_scope", "late_arrival", "timestamp_basis",
               "amount", "currency", "capacity", "capacity_unit", "event_date"}
COUNT_FIELDS = {"fetched", "eligible_articles", "merged_duplicates", "published_topics", "held", "deferred"}


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _public_issue(issue):
    if issue.get("approved") is not True or issue.get("state") not in ("READY", "COMMITTED", "WEB_VERIFIED"):
        raise ValueError("UNAPPROVED_ISSUE")
    issue_date = issue["issue_date"]
    if date.fromisoformat(issue_date).isoformat() != issue_date:
        raise ValueError("INVALID_ISSUE_DATE")
    if not isinstance(issue.get("revision"), int) or isinstance(issue["revision"], bool) or issue["revision"] < 1:
        raise ValueError("INVALID_REVISION")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", issue["issue_id"]):
        raise ValueError("INVALID_ISSUE_ID")
    public = {key: issue[key] for key in ISSUE_FIELDS if key in issue}
    public["schema_version"] = 1
    public["counts"] = {key: value for key, value in issue.get("counts", {}).items() if key in COUNT_FIELDS}
    public["coverage"] = {key: issue.get("coverage", {})[key]
                          for key in ("expected_sources", "successful_sources", "partial") if key in issue.get("coverage", {})}
    public["items"] = []
    for item in issue.get("items", []):
        if not isinstance(item.get("summary"), str) or not isinstance(item.get("companies", []), list):
            raise ValueError("INVALID_PUBLIC_ITEM")
        safe_public_url(item["source_url"], [__import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(item["source_url"]).hostname])
        public["items"].append({key: item[key] for key in ITEM_FIELDS if key in item})
    if len(public["items"]) > 15:
        raise ValueError("ISSUE_SELECTION_LIMIT")
    if public["counts"].get("published_topics", len(public["items"])) != len(public["items"]):
        raise ValueError("ITEM_COUNT_MISMATCH")
    return public


def build_snapshot(issues, output_dir):
    """Generate only from approved DB snapshots; output_dir is the site root.

    Index and search show the newest revision for each day. Older revisions remain
    immutable archive files; an old-date backfill never replaces a newer latest.
    """
    root = Path(output_dir).resolve()
    issues = list(issues)
    active_keys = {(i["issue_date"], i["revision"]) for i in issues if i.get("state") in ("READY", "COMMITTED")}
    active_months = {key[0][:7] for key in active_keys}
    all_issues = sorted((_public_issue(i) for i in issues), key=lambda i: (i["issue_date"], i["revision"]))
    files, descriptors, seen = {}, [], set()
    for issue in all_issues:
        key = (issue["issue_date"], issue["revision"])
        if key in seen:
            raise ValueError("DUPLICATE_ISSUE_REVISION")
        seen.add(key)
        day = issue["issue_date"]
        path = f"{PREFIX}issues/{day[:4]}/{day[5:7]}/{day}.r{issue['revision']}.json"
        content = _bytes(issue)
        files[path] = content
        descriptors.append({"issue_id": issue["issue_id"], "issue_date": day, "revision": issue["revision"],
            "path": path, "sha256": _hash(content), "published_at": issue.get("published_at"),
            "content_status": issue.get("content_status"), "counts": issue["counts"]})
    by_day = {}
    for issue, descriptor in zip(all_issues, descriptors):
        by_day[issue["issue_date"]] = (issue, descriptor)
    current = [by_day[day] for day in sorted(by_day, reverse=True)]
    latest = current[0][1] if current else None
    index = {"schema_version": 1, "issues": [pair[1] for pair in current]}
    files[PREFIX + "index.json"] = _bytes(index)
    files[PREFIX + "latest.json"] = _bytes({"schema_version": 1, "latest": latest})
    snapshot_id = _hash(_bytes(index))[:24]
    months = {}
    for issue, descriptor in current:
        month = issue["issue_date"][:7]
        months.setdefault(month, []).extend(dict(item, issue_id=issue["issue_id"], issue_date=issue["issue_date"],
            revision=issue["revision"], issue_path=descriptor["path"]) for item in issue["items"])
        # Zero-item months still have a shard, preserving the range of published days.
    shards = []
    for month, items in sorted(months.items(), reverse=True):
        shard = _bytes({"schema_version": 1, "month": month, "items": items})
        # Content-addressed per-month suffix means unchanged past months are reused.
        path = f"{PREFIX}search/{month[:4]}/{month}.{_hash(shard)[:24]}.json"
        files[path] = shard
        shards.append({"month": month, "path": path, "sha256": _hash(shard), "count": len(items)})
    manifest = {"schema_version": 1, "generated_at": max((i.get("published_at") or "" for i in all_issues), default=None),
                "snapshot_id": snapshot_id, "months": shards}
    files[PREFIX + "search/manifest.json"] = _bytes(manifest)
    # Validate all immutable conflicts and resolved paths before any file writes.
    changed_paths = []
    for path, content in files.items():
        target = root / path
        if not target.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in [target, *target.parents] if parent != root.parent):
            raise ValueError("UNSAFE_SNAPSHOT_PATH")
        if target.exists() and ("/issues/" in path or ("/search/" in path and not path.endswith("manifest.json"))):
            stored = target.read_bytes()
            # An existing checkout created outside this publisher may use global
            # Windows autocrlf. Accept only the exact LF-to-CRLF transformation,
            # then restore canonical bytes; all actual content changes still fail.
            if stored != content and stored != content.replace(b"\n", b"\r\n"):
                raise ValueError("IMMUTABLE_SNAPSHOT_CONFLICT")
        if not target.exists() or target.read_bytes() != content:
            changed_paths.append(path)
    for path, content in files.items():
        target = root / path
        if target.exists() and target.read_bytes() == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_bytes(content)
        os.replace(temp, target)
    return {"snapshot_id": snapshot_id, "descriptor": latest, "latest": latest, "manifest": manifest,
            "files": {path: _hash(content) for path, content in files.items()},
            "changed_paths": changed_paths,
            "verify_paths": [PREFIX + "index.json", PREFIX + "latest.json", PREFIX + "search/manifest.json"] +
                            sorted(set(([latest["path"]] if latest else []) +
                                       [d["path"] for d in descriptors if (d["issue_date"], d["revision"]) in active_keys] +
                                       [s["path"] for s in shards if s["month"] in active_months] +
                                       [path for path in changed_paths if "/issues/" in path or ("/search/" in path and not path.endswith("manifest.json"))]))}


def verify_pages(snapshot, base_url, transport=None, *, timeout_seconds=60, max_files=10000):
    """Retrieve the actual immutable files and all manifests within a deadline."""
    from urllib.parse import urlsplit
    host = urlsplit(base_url).hostname
    base = safe_public_url(base_url, [host], resolve=transport is None).rstrip("/") + "/"
    paths = snapshot["verify_paths"]
    if len(paths) > max_files:
        return {"verified": False, "error_code": "PAGES_VERIFY_LIMIT"}
    if transport is None:
        import requests
        transport = requests.Session()
        transport.trust_env = False
    deadline = time.monotonic() + min(120, max(1, timeout_seconds))
    for path in paths:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"verified": False, "error_code": "PAGES_VERIFY_TIMEOUT"}
        try:
            response = transport.get(base + path, timeout=min(10, remaining), allow_redirects=False,
                                     headers={"Cache-Control": "no-cache"})
            if response.status_code != 200 or _hash(response.content) != snapshot["files"][path]:
                return {"verified": False, "error_code": "PAGES_HASH_MISMATCH"}
            json.loads(response.content)
        except Exception:
            return {"verified": False, "error_code": "PAGES_REQUEST_ERROR"}
    return {"verified": True}


class Publisher:
    def __init__(self, config, transport=None, git_runner=None):
        self.config = config.get("publisher", config)
        self.transport = transport
        self.git_runner = git_runner

    def _git(self, clone, *args, check=True):
        if self.git_runner:
            return self.git_runner(clone, list(args), check=check)
        result = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True,
                                timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if check and result.returncode:
            raise RuntimeError("GIT_OPERATION_FAILED")
        return result

    def _git_restore_bytes(self, clone, *args):
        # Published SHA-256 covers the literal canonical bytes. Windows global
        # core.autocrlf must not rewrite immutable JSON during recovery/sync.
        return self._git(clone, "-c", "core.autocrlf=false", "-c", "core.eol=lf", *args)

    def _recover_dirty_news(self, clone, *, recover=True):
        """Recover interrupted generation only inside the dedicated news tree."""
        status = self._git(clone, "status", "--porcelain", "-z", "--untracked-files=all").stdout
        entries, parts, index = [], status.split("\0"), 0
        while index < len(parts):
            entry = parts[index]
            index += 1
            if not entry:
                continue
            code, path = entry[:2], entry[3:]
            entries.append((code, path))
            if "R" in code or "C" in code:
                if index >= len(parts) or not parts[index]:
                    raise ValueError("INVALID_GIT_STATUS")
                entries.append((code, parts[index]))
                index += 1
        dirty = [(code, path) for code, path in entries if path != ".wind-news-publish-clone"]
        if any(not path.startswith(PREFIX) for _, path in dirty):
            raise ValueError("PUBLISH_CLONE_DIRTY")
        news_root = (clone / PREFIX).resolve()
        if not news_root.is_relative_to(clone):
            raise ValueError("UNSAFE_SNAPSHOT_PATH")
        # Verify every deletion target before restoring or deleting any artifact.
        untracked = []
        for code, path in dirty:
            target = clone / path
            if not target.resolve().is_relative_to(news_root) or any(p.is_symlink() for p in [target, *target.parents] if p.is_relative_to(clone)):
                raise ValueError("UNSAFE_SNAPSHOT_PATH")
            if code == "??":
                if not target.is_file():
                    raise ValueError("UNSAFE_SNAPSHOT_PATH")
                untracked.append(target)
        if not recover:
            return
        if any(code != "??" for code, _ in dirty):
            tracked = self._git(clone, "ls-tree", "-r", "--name-only", "HEAD", "--", PREFIX).stdout
            if tracked:
                # One bounded pathspec restores tracked news files only. Unrelated
                # dirty changes were rejected before this reversible recovery.
                self._git_restore_bytes(clone, "restore", "--source=HEAD", "--staged", "--worktree", "--", PREFIX)
            else:
                # A first publication can crash after add but before commit.
                self._git(clone, "reset", "HEAD", "--", PREFIX)
                # Files newly staged in that interrupted publication are untracked
                # after reset and are regenerated from approved DB input below.
                for code, path in dirty:
                    target = clone / path
                    if code != "??" and target.is_file():
                        untracked.append(target)
        for target in set(untracked):
            target.unlink()

    def _stage_paths(self, clone, paths):
        """Avoid Windows' command-line limit even for multi-year rebuilds."""
        batch, length = [], 0
        for path in paths:
            if not path.startswith(PREFIX):
                raise ValueError("PUBLISH_PATH_OUTSIDE_ALLOWLIST")
            size = len(path) + 3
            if size > 12000:
                raise ValueError("PUBLISH_PATH_TOO_LONG")
            if batch and (len(batch) >= 100 or length + size > 12000):
                self._git(clone, "add", "--", *batch)
                batch, length = [], 0
            batch.append(path)
            length += size
        if batch:
            self._git(clone, "add", "--", *batch)

    def publish(self, issues):
        cfg = self.config
        if not cfg.get("enabled", False):
            raise ValueError("PUBLISHER_DISABLED")
        clone = Path(cfg["publish_clone"]).resolve()
        remote, branch = cfg.get("remote", "origin"), cfg.get("branch", "main")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", branch) or branch.startswith(("-", "/")) or ".." in branch:
            raise ValueError("INVALID_PUBLISH_BRANCH")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", remote) or remote.startswith("-"):
            raise ValueError("INVALID_REMOTE")
        # This must be a dedicated disposable publish checkout, never a code release.
        if not (clone / ".wind-news-publish-clone").is_file():
            raise ValueError("PUBLISH_CLONE_MARKER_REQUIRED")
        self._recover_dirty_news(clone, recover=False)
        if self._git(clone, "branch", "--show-current").stdout.strip() != branch:
            raise ValueError("PUBLISH_BRANCH_MISMATCH")
        self._git(clone, "fetch", remote, branch)
        local = self._git(clone, "rev-parse", "HEAD").stdout.strip()
        remote_head = self._git(clone, "rev-parse", f"{remote}/{branch}").stdout.strip()
        sync_action = None
        if local != remote_head:
            ahead = self._git(clone, "rev-list", f"{remote_head}..HEAD", "--max-count=11").stdout.splitlines()
            if ahead:
                # A crash after commit or a failed push is safely resumable only
                # when every pending commit is ours and touches only news data.
                if len(ahead) > 10:
                    raise ValueError("PUBLISH_CLONE_LOCAL_COMMITS")
                for pending in ahead:
                    subject = self._git(clone, "show", "-s", "--format=%s", pending).stdout.strip()
                    touched = self._git(clone, "diff-tree", "--no-commit-id", "--name-only", "-r", pending).stdout.splitlines()
                    parents = self._git(clone, "show", "-s", "--format=%P", pending).stdout.split()
                    if not subject.startswith("Publish wind news snapshot ") or not touched or len(parents) != 1 or any(not p.startswith(PREFIX) for p in touched):
                        raise ValueError("PUBLISH_CLONE_LOCAL_COMMITS")
                ancestor = self._git(clone, "merge-base", "--is-ancestor", remote_head, "HEAD", check=False)
                if ancestor.returncode:
                    sync_action = "reset"
            else:
                sync_action = "merge"
        self._recover_dirty_news(clone)
        if sync_action == "reset":
            self._git_restore_bytes(clone, "reset", "--hard", f"{remote}/{branch}")
        elif sync_action == "merge":
            self._git_restore_bytes(clone, "merge", "--ff-only", f"{remote}/{branch}")
        incoming = list(issues)
        for attempt in range(min(3, max(1, int(cfg.get("max_push_attempts", 3))))):
            # Retain remote published archives when an older DB replay/backfill runs.
            merged = {(i["issue_date"], i["revision"]): i for i in incoming}
            archive = clone / PREFIX / "issues"
            if archive.exists():
                for path in archive.rglob("*.json"):
                    public = json.loads(path.read_text(encoding="utf-8"))
                    key = (public["issue_date"], public["revision"])
                    merged.setdefault(key, dict(public, approved=True, state="WEB_VERIFIED"))
            snapshot = build_snapshot(merged.values(), clone)
            self._stage_paths(clone, sorted(snapshot["changed_paths"]))
            staged = self._git(clone, "diff", "--cached", "--name-only").stdout.splitlines()
            if any(not path.startswith(PREFIX) for path in staged):
                raise ValueError("PUBLISH_PATH_OUTSIDE_ALLOWLIST")
            if staged:
                self._git(clone, "commit", "-m", "Publish wind news snapshot " + snapshot["snapshot_id"])
            commit = self._git(clone, "rev-parse", "HEAD").stdout.strip()
            pushed = self._git(clone, "push", remote, f"HEAD:refs/heads/{branch}", check=False)
            if pushed.returncode == 0:
                result = {"state": "COMMITTED", "commit_sha": commit, "snapshot_id": snapshot["snapshot_id"],
                          "snapshot": snapshot, "descriptor": snapshot["descriptor"]}
                if cfg.get("pages_base_url"):
                    verification = verify_pages(snapshot, cfg["pages_base_url"], self.transport,
                                                timeout_seconds=cfg.get("verify_timeout_seconds", 60))
                    result["verification"] = verification
                    if verification["verified"]:
                        result["state"] = "WEB_VERIFIED"
                return result
            if attempt == min(3, max(1, int(cfg.get("max_push_attempts", 3)))) - 1:
                raise RuntimeError("GIT_PUSH_RETRY_EXHAUSTED")
            self._git(clone, "fetch", remote, branch)
            # Reset only our clean dedicated clone after saving DB snapshots in memory.
            # Rebuild all news artifacts on the new remote; notices remain untouched.
            self._git_restore_bytes(clone, "reset", "--hard", f"{remote}/{branch}")
        raise RuntimeError("GIT_PUSH_RETRY_EXHAUSTED")

    def reconcile(self, issues):
        return self.publish(issues)


def publish(issues, config, **kwargs):
    return Publisher(config, **kwargs).publish(issues)

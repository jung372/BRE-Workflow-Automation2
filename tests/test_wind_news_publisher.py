import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from news.publisher import Publisher, build_snapshot, verify_pages


def issue(day="2026-09-30", revision=1):
    return {"schema_version": 1, "issue_id": "wind-" + day, "issue_date": day, "revision": revision,
        "approved": True, "state": "READY", "published_at": day + "T08:00:00+09:00", "content_status": "normal",
        "counts": {"published_topics": 1, "held": 0}, "coverage": {"expected_sources": ["example"], "partial": False},
        "headline_summary": ["국내 해상풍력 소식"], "api_key": "not-public",
        "items": [{"headline": "국내 해상풍력 소식", "summary": "100MW 공급 MOU 체결.", "companies": ["대한풍력"],
            "source_url": "https://news.example.com/1", "source_name": "Example", "event_id": "event-" + day,
            "representative_article_id": "article-1", "evidence_scope": "description", "timestamp_basis": "naver_pubDate",
            "text": "full copyrighted body", "credentials": "not-public"}]}


class FileResponse:
    status_code = 200

    def __init__(self, content):
        self.content = content


class SiteTransport:
    def __init__(self, root, corrupt=None):
        self.root, self.corrupt, self.calls = Path(root), corrupt, []

    def get(self, url, **kwargs):
        path = url.split("https://pages.example.com/site/", 1)[1]
        self.calls.append((path, kwargs))
        return FileResponse(b"{}" if path == self.corrupt else (self.root / path).read_bytes())


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_newest_revision_search_and_archive_are_consistent(self):
        first, second, old = issue(), issue(revision=2), issue("2024-02-01")
        second["items"][0]["summary"] = "정정된 근거 요약."
        snapshot = build_snapshot([first, second, old], self.root)
        self.assertEqual(snapshot["latest"]["revision"], 2)
        index = json.loads((self.root / "data/wind-news/index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["issues"]), 2)
        self.assertTrue((self.root / "data/wind-news/issues/2026/09/2026-09-30.r1.json").is_file())
        month = snapshot["manifest"]["months"][0]
        shard = json.loads((self.root / month["path"]).read_text(encoding="utf-8"))
        self.assertEqual(shard["items"][0]["revision"], 2)
        self.assertEqual(shard["items"][0]["headline"], "국내 해상풍력 소식")
        self.assertEqual(shard["items"][0]["timestamp_basis"], "naver_pubDate")
        self.assertNotIn("text", shard["items"][0])
        self.assertNotIn("credentials", shard["items"][0])
        self.assertNotIn("api_key", (self.root / index["issues"][0]["path"]).read_text(encoding="utf-8"))
        for path, expected in snapshot["files"].items():
            self.assertEqual(hashlib.sha256((self.root / path).read_bytes()).hexdigest(), expected)

    def test_replay_is_deterministic_and_old_month_reused(self):
        old = issue("2024-02-01")
        old["state"] = "WEB_VERIFIED"
        first = build_snapshot([old, issue()], self.root)
        second = build_snapshot([old, issue(), issue("2026-10-01")], self.root)
        first_old = next(m for m in first["manifest"]["months"] if m["month"] == "2024-02")
        second_old = next(m for m in second["manifest"]["months"] if m["month"] == "2024-02")
        self.assertEqual(first_old, second_old)
        self.assertNotIn(first_old["path"], second["verify_paths"])
        replay = build_snapshot([old, issue(), issue("2026-10-01")], self.root)
        self.assertEqual(second["files"], replay["files"])
        self.assertEqual(replay["changed_paths"], [])

    def test_immutable_conflict_and_unapproved_input_do_not_change_manifest(self):
        current = issue()
        snapshot = build_snapshot([current], self.root)
        previous = (self.root / "data/wind-news/latest.json").read_bytes()
        changed = copy.deepcopy(current)
        changed["items"][0]["summary"] = "changed without revision"
        with self.assertRaisesRegex(ValueError, "IMMUTABLE"):
            build_snapshot([changed], self.root)
        self.assertEqual((self.root / "data/wind-news/latest.json").read_bytes(), previous)
        for state in ("DRAFT", "REVIEW_REQUIRED"):
            with self.assertRaisesRegex(ValueError, "UNAPPROVED"):
                build_snapshot([dict(current, state=state)], self.root)
        with self.assertRaisesRegex(ValueError, "UNAPPROVED"):
            build_snapshot([dict(current, approved=False)], self.root)
        self.assertTrue(verify_pages(snapshot, "https://pages.example.com/site", SiteTransport(self.root))["verified"])

    def test_actual_pages_manifest_and_month_hash_required(self):
        snapshot = build_snapshot([issue()], self.root)
        for path in ["data/wind-news/search/manifest.json", snapshot["manifest"]["months"][0]["path"], snapshot["latest"]["path"]]:
            with self.subTest(path=path):
                result = verify_pages(snapshot, "https://pages.example.com/site", SiteTransport(self.root, path))
                self.assertFalse(result["verified"])

    def test_large_history_staging_batches_only_requested_changed_files(self):
        calls = []
        def runner(clone, args, check=True):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        publisher = Publisher({}, git_runner=runner)
        paths = [f"data/wind-news/issues/2026/09/2026-09-30.r{i}.json" for i in range(2500)]
        publisher._stage_paths(self.root, paths)
        self.assertGreater(len(calls), 1)
        self.assertEqual([path for args in calls for path in args[2:]], paths)
        for args in calls:
            self.assertEqual(args[:2], ["add", "--"])
            self.assertLessEqual(len(args[2:]), 100)
            self.assertLessEqual(sum(len(p) + 3 for p in args[2:]), 12000)

    def test_publish_does_not_stage_unchanged_multi_year_history(self):
        calls, changed = [], ["data/wind-news/latest.json", "data/wind-news/search/manifest.json"]
        def runner(clone, args, check=True):
            calls.append(args)
            output = ""
            if args[:2] == ["branch", "--show-current"]:
                output = "main\n"
            elif args[0] == "rev-parse":
                output = "a" * 40 + "\n"
            elif args[:3] == ["diff", "--cached", "--name-only"]:
                output = "\n".join(changed)
            return subprocess.CompletedProcess(args, 0, output, "")
        (self.root / ".wind-news-publish-clone").touch()
        snapshot = {"snapshot_id": "snapshot", "descriptor": None, "changed_paths": changed,
                    "files": {f"data/wind-news/issues/2020/01/2020-01-01.r{i}.json": "hash" for i in range(2500)}}
        with patch("news.publisher.build_snapshot", return_value=snapshot):
            Publisher({"enabled": True, "publish_clone": str(self.root)}, git_runner=runner).publish([])
        self.assertEqual([p for args in calls if args[0] == "add" for p in args[2:]], changed)

    def test_windows_checkout_line_endings_preserve_immutable_canonical_hashes(self):
        first = build_snapshot([issue()], self.root)
        archive = self.root / first["latest"]["path"]
        canonical_bytes = archive.read_bytes()
        archive.write_bytes(canonical_bytes.replace(b"\n", b"\r\n"))
        replay = build_snapshot([issue()], self.root)
        self.assertEqual(replay["files"], first["files"])
        self.assertEqual(archive.read_bytes(), canonical_bytes)


class GitPublisherTests(unittest.TestCase):
    """Only isolated local repositories and a bare filesystem remote are used."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote, self.clone = self.root / "remote.git", self.root / "publish"
        self.git(self.root, "init", "--bare", str(self.remote))
        self.git(self.root, "clone", str(self.remote), str(self.clone))
        self.git(self.clone, "config", "user.email", "offline-test@example.invalid")
        self.git(self.clone, "config", "user.name", "Offline Test")
        self.git(self.clone, "checkout", "-b", "main")
        (self.clone / "index.html").write_text("existing notice site", encoding="utf-8")
        self.git(self.clone, "add", "index.html")
        self.git(self.clone, "commit", "-m", "Initial site")
        self.git(self.clone, "push", "origin", "main")
        (self.clone / ".wind-news-publish-clone").touch()
        self.config = {"enabled": True, "publish_clone": str(self.clone), "branch": "main", "max_push_attempts": 1}

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def git(clone, *args, check=True):
        return subprocess.run(["git", "-C", str(clone), *args], check=check, capture_output=True, text=True, timeout=20)

    def test_isolated_git_publish_and_old_backfill_preserve_site_and_latest(self):
        result = Publisher(self.config).publish([issue()])
        self.assertEqual(result["state"], "COMMITTED")
        self.assertEqual((self.clone / "index.html").read_text(), "existing notice site")
        next_result = Publisher(self.config).publish([issue("2024-02-01")])
        self.assertEqual(next_result["descriptor"]["issue_date"], "2026-09-30")
        changed = self.git(self.clone, "show", "--format=", "--name-only", "HEAD").stdout.splitlines()
        self.assertTrue(all(path.startswith("data/wind-news/") for path in changed if path))

    def test_dirty_unrelated_file_and_unknown_local_commit_are_rejected(self):
        (self.clone / "index.html").write_text("unrelated dirty user change")
        with self.assertRaisesRegex(ValueError, "DIRTY"):
            Publisher(self.config).publish([issue()])
        self.git(self.clone, "add", "index.html")
        self.git(self.clone, "commit", "-m", "User change")
        with self.assertRaisesRegex(ValueError, "LOCAL_COMMITS"):
            Publisher(self.config).publish([issue()])

    def test_failed_push_after_commit_resumes_without_duplicate_revision(self):
        def fail_push(clone, args, check=True):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 1, "", "offline simulated")
            return self.git(clone, *args, check=check)
        with self.assertRaisesRegex(RuntimeError, "RETRY_EXHAUSTED"):
            Publisher(self.config, git_runner=fail_push).publish([issue()])
        committed = self.git(self.clone, "rev-parse", "HEAD").stdout.strip()
        result = Publisher(self.config).publish([issue()])
        self.assertEqual(result["commit_sha"], committed)
        self.assertEqual(self.git(self.clone, "rev-list", "--count", "HEAD").stdout.strip(), "2")

    def test_remote_notice_commit_race_is_reapplied_without_force(self):
        peer = self.root / "notice-peer"
        self.git(self.root, "clone", "--branch", "main", str(self.remote), str(peer))
        self.git(peer, "config", "user.email", "offline-test@example.invalid")
        self.git(peer, "config", "user.name", "Offline Test")
        raced = []
        def race(clone, args, check=True):
            if args[0] == "push" and not raced:
                raced.append(True)
                (peer / "notice.json").write_text('{"notice":"preserved"}')
                self.git(peer, "add", "notice.json")
                self.git(peer, "commit", "-m", "Notice update")
                self.git(peer, "push", "origin", "main")
            return self.git(clone, *args, check=check)
        cfg = dict(self.config, max_push_attempts=3)
        result = Publisher(cfg, git_runner=race).publish([issue()])
        self.assertEqual(result["state"], "COMMITTED")
        self.assertTrue((self.clone / "notice.json").exists())
        self.assertEqual(self.git(self.clone, "rev-list", "--count", "HEAD").stdout.strip(), "3")

    def test_partial_first_build_and_staging_crash_recover_from_db(self):
        orphan = self.clone / "data/wind-news/issues/2025/01/2025-01-01.r1.json"
        orphan.parent.mkdir(parents=True)
        orphan.write_text('{"partial":"not approved"}')
        temp = self.clone / "data/wind-news/latest.json.tmp"
        temp.write_text("unfinished write")
        self.git(self.clone, "add", "--", "data/wind-news/issues/2025/01/2025-01-01.r1.json")
        result = Publisher(self.config).publish([issue()])
        self.assertEqual(result["state"], "COMMITTED")
        self.assertFalse(orphan.exists())
        self.assertFalse(temp.exists())
        self.assertEqual(result["descriptor"]["issue_date"], "2026-09-30")

    def test_partial_revision_build_restores_archives_and_rebuilds(self):
        Publisher(self.config).publish([issue()])
        previous_archive = self.clone / "data/wind-news/issues/2026/09/2026-09-30.r1.json"
        committed_bytes = previous_archive.read_bytes()
        previous_archive.write_text("incomplete corrupt write")
        latest = self.clone / "data/wind-news/latest.json"
        latest.write_text("incomplete manifest")
        self.git(self.clone, "add", "--", "data/wind-news/latest.json")
        orphan = self.clone / "data/wind-news/search/2026/incomplete.json.tmp"
        orphan.write_text("unfinished shard")
        result = Publisher(self.config).publish([issue(revision=2)])
        self.assertEqual(result["descriptor"]["revision"], 2)
        self.assertEqual(previous_archive.read_bytes(), committed_bytes)
        self.assertFalse(orphan.exists())

    def test_unrelated_dirt_prevents_recovery_of_dirty_news(self):
        path = self.clone / "data/wind-news/latest.json.tmp"
        path.parent.mkdir(parents=True)
        path.write_text("partial but untouched on rejection")
        (self.clone / "index.html").write_text("user dirty change")
        with self.assertRaisesRegex(ValueError, "DIRTY"):
            Publisher(self.config).publish([issue()])
        self.assertEqual(path.read_text(), "partial but untouched on rejection")
        self.assertEqual((self.clone / "index.html").read_text(), "user dirty change")


if __name__ == "__main__":
    unittest.main()

"""Update only the two existing news schedules using n8n's CLI.

Run on the Linux server as its Docker operator. Credential objects are never
exported. Existing workflow backups stay private on the server, not in Git.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time

N8N = "n8n-personal-n8n-1"
NAMES = ("BRE-WIND-03-Publish", "BRE-WIND-04-Deliver")


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("BRIEFING_OPERATOR_COMMAND_FAILED")
    return result.stdout


def active_ids():
    output = command(["docker", "exec", N8N, "n8n", "list:workflow", "--active=true", "--onlyId"])
    return {s.strip() for s in output.splitlines() if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", s.strip())}


def exported(workflow_id, directory, label):
    remote = "/tmp/bre-briefing-" + workflow_id + ".json"
    command(["docker", "exec", N8N, "n8n", "export:workflow", "--id=" + workflow_id, "--output=" + remote])
    target = directory / (label + ".json")
    command(["docker", "cp", N8N + ":" + remote, str(target)])
    command(["docker", "exec", N8N, "rm", "-f", remote])
    rows = json.loads(target.read_text())
    if len(rows) != 1 or rows[0]["id"] != workflow_id:
        raise RuntimeError("BRIEFING_WORKFLOW_NOT_FOUND")
    return rows[0]


def updated(old, template):
    if old["name"] != template["name"] or old["name"] not in NAMES:
        raise ValueError("BRIEFING_WORKFLOW_NAME_MISMATCH")
    flow = {k: copy.deepcopy(old[k]) for k in ("id", "name", "nodes", "connections", "settings", "tags") if k in old}
    flow["active"] = False
    replacements = {"Schedule"} if old["name"] == NAMES[0] else {"Schedule", "Submit", "Check delivery"}
    parameters = {n["name"]: n["parameters"] for n in template["nodes"]}
    found = set()
    for node in flow["nodes"]:
        if node["name"] in replacements:
            # Keep the actual HTTP endpoint/auth binding and every other setting.
            if node["name"] == "Submit":
                node["parameters"]["jsonBody"] = parameters["Submit"]["jsonBody"]
            else:
                node["parameters"] = copy.deepcopy(parameters[node["name"]])
            found.add(node["name"])
    if found != replacements:
        raise ValueError("BRIEFING_WORKFLOW_NODES_MISSING")
    flow.setdefault("settings", {})["timezone"] = "Asia/Seoul"
    return flow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--teams-configured", choices=("true", "false"), required=True)
    args = parser.parse_args()
    links = json.loads((args.root / "settings/n8n-links.json").read_text())["workflows"]
    ids = [links[name] for name in NAMES]
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", i) for i in ids):
        raise ValueError("BRIEFING_WORKFLOW_IDS_INVALID")
    backup = args.root / "operator" / ("briefing-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
    backup.mkdir(parents=True, mode=0o700)
    before_active = active_ids()
    if ids[0] not in before_active:
        raise RuntimeError("EXISTING_NEWS_PUBLISH_NOT_ACTIVE")
    old = [exported(i, backup, "before-" + name) for i, name in zip(ids, NAMES)]
    templates = [json.loads((args.source / "automation/n8n" / (name + ".json")).read_text()) for name in NAMES]
    flows = [updated(previous, template) for previous, template in zip(old, templates)]
    target = backup / "update.json"
    target.write_text(json.dumps(flows, ensure_ascii=False))
    remote = "/tmp/bre-briefing-update.json"
    command(["docker", "cp", str(target), N8N + ":" + remote])
    command(["docker", "exec", "--user", "root", N8N, "chown", "node:node", remote])
    try:
        command(["docker", "exec", N8N, "n8n", "import:workflow", "--input=" + remote])
        expected_active = before_active - {ids[1]}
        if args.teams_configured == "true":
            expected_active.add(ids[1])
        for workflow_id in ids:
            if workflow_id in expected_active:
                command(["docker", "exec", N8N, "n8n", "publish:workflow", "--id=" + workflow_id])
        if active_ids() != expected_active:
            raise RuntimeError("BRIEFING_ACTIVE_WORKFLOW_SET_MISMATCH")
        command(["docker", "restart", N8N])
        for _ in range(30):
            health = command(["docker", "inspect", "--format", "{{.State.Health.Status}}", N8N]).strip()
            if health == "healthy":
                break
            time.sleep(2)
        if health != "healthy":
            raise RuntimeError("N8N_HEALTH_FAILED")
        for workflow_id, name, desired in zip(ids, NAMES, flows):
            current = exported(workflow_id, backup, "after-" + name)
            for field in ("nodes", "connections", "settings"):
                if current[field] != desired[field]:
                    raise RuntimeError("BRIEFING_WORKFLOW_CONTENT_MISMATCH")
            if bool(current.get("activeVersionId") or current.get("active")) != (workflow_id in expected_active):
                raise RuntimeError("BRIEFING_PUBLICATION_STATE_MISMATCH")
        if active_ids() != expected_active:
            raise RuntimeError("BRIEFING_ACTIVE_WORKFLOW_SET_MISMATCH")
        print("BRIEFING_SCHEDULES_VERIFIED publish=07:50 delivery=08:00-09:00 timezone=Asia/Seoul")
        print("OTHER_ACTIVE_WORKFLOWS_PRESERVED=" + str(len(before_active - set(ids))))
        print("TEAMS_SCHEDULE_ACTIVE=" + args.teams_configured)
    except Exception:
        # Restore both prior drafts and their exact published versions. Version
        # history survives import; leave every unrelated workflow untouched.
        rollback = backup / "rollback.json"
        rollback.write_text(json.dumps(old, ensure_ascii=False))
        command(["docker", "cp", str(rollback), N8N + ":" + remote])
        command(["docker", "exec", "--user", "root", N8N, "chown", "node:node", remote])
        command(["docker", "exec", N8N, "n8n", "import:workflow", "--input=" + remote])
        for previous in old:
            if previous["id"] in before_active:
                publish = ["docker", "exec", N8N, "n8n", "publish:workflow", "--id=" + previous["id"]]
                if previous.get("activeVersionId"):
                    publish.append("--versionId=" + previous["activeVersionId"])
                command(publish)
        command(["docker", "restart", N8N])
        print("BRIEFING_WORKFLOWS_ROLLED_BACK")
        raise
    finally:
        command(["docker", "exec", N8N, "rm", "-f", remote])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print CLI stderr, exported workflows, or environment values.
        code = str(exc)
        raise SystemExit(code if re.fullmatch(r"[A-Z_]{1,100}", code) else "BRIEFING_OPERATOR_FAILED")

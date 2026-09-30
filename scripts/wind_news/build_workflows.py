"""Generate credential-free, inactive n8n workflows for the news HTTP service.

Re-run after editing service URL: python scripts/wind_news/build_workflows.py
No network calls, credential access, import, or workflow activation occurs here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[2]


def node(name, kind, params, x, y=0, version=1):
    return {"id": str(uuid5(NAMESPACE_URL, "bre-wind/" + name)), "name": name,
            "type": "n8n-nodes-base." + kind, "typeVersion": version,
            "position": [x, y], "parameters": params}


def http(name, method, path, body=None, key=False, x=440):
    params = {"method": method, "url": "={{ $('Configure').first().json.service_url + " + path + " }}",
              "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
              "options": {"timeout": 35000, "redirect": {"redirect": {"followRedirects": False}}}}
    if key:
        params.update(sendHeaders=True, headerParameters={"parameters": [
            {"name": "Idempotency-Key", "value": "={{ $workflow.name + ':' + $execution.id }}"}]})
    if body is not None:
        params.update(sendBody=True, specifyBody="json", jsonBody=body)
    result = node(name, "httpRequest", params, x, version=4.2)
    # The ID is intentionally absent. Importer must bind its local Header Auth.
    result["credentials"] = {"httpHeaderAuth": {"name": "BRE Wind Service"}}
    return result


def workflow(number, label, endpoint, body, crons, service_url, *, async_job=True, publish=False, error=False):
    name = f"BRE-WIND-{number}-{label}"
    nodes, connections = [], {}

    def edge(source, target, branch=0):
        main = connections.setdefault(source, {"main": []})["main"]
        while len(main) <= branch:
            main.append([])
        main[branch].append({"node": target, "type": "main", "index": 0})

    if error:
        nodes.append(node("Error trigger", "errorTrigger", {}, 0))
        edge("Error trigger", "Configure")
    else:
        nodes.append(node("Schedule", "scheduleTrigger", {"rule": {"interval": [
            {"field": "cronExpression", "expression": c} for c in crons]}}, 0, version=1.2))
        nodes.append(node("Manual test", "manualTrigger", {}, 0, 180))
        edge("Schedule", "Configure")
        edge("Manual test", "Configure")
    code = ("const issue_date = $now.setZone('Asia/Seoul').toFormat('yyyy-MM-dd');\n"
            f"return [{{json: {{service_url: {json.dumps(service_url)}, issue_date, "
            "issue_id: 'wind-' + issue_date, deadline: Date.now() + 1100000}}}];")
    nodes.append(node("Configure", "code", {"jsCode": code}, 220, version=2))
    if publish:
        nodes.append(http("Read issue", "GET", "'/v1/issues/' + $json.issue_id", x=440))
        edge("Configure", "Read issue")
        nodes.append(node("Require approval", "code", {"jsCode":
            "if (!['READY','COMMITTED','WEB_VERIFIED'].includes($json.state) || !$json.approval_hash) "
            "throw new Error('issue_not_ready_or_unapproved');\nreturn $input.all();"}, 650, version=2))
        edge("Read issue", "Require approval")
        edge("Require approval", "Submit")
    else:
        edge("Configure", "Submit")
    nodes.append(http("Submit", "POST", endpoint, body, key=async_job, x=880 if publish else 440))
    if async_job:
        nodes.append(node("Wait for job", "wait", {"resume": "timeInterval", "amount": 5, "unit": "seconds"}, 1080, version=1.1))
        edge("Submit", "Wait for job")
        nodes.append(http("Poll job", "GET", "'/v1/jobs/' + $('Submit').first().json.job_id", x=1280))
        edge("Wait for job", "Poll job")
        nodes.append(node("Check outcome", "code", {"jsCode":
            "if ($json.status === 'FAILED') throw new Error('news_job_failed:' + ($json.error_code || 'unknown'));\n"
            "if (!['QUEUED','RUNNING','SUCCEEDED'].includes($json.status)) throw new Error('invalid_job_status');\n"
            "if (Date.now() > $('Configure').first().json.deadline) throw new Error('news_job_deadline');\n"
            "return $input.all();"}, 1480, version=2))
        edge("Poll job", "Check outcome")
        nodes.append(node("Completed?", "if", {"conditions": {"options": {
            "caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{"id": "job-done", "leftValue": "={{ $json.status }}", "rightValue": "SUCCEEDED",
                            "operator": {"type": "string", "operation": "equals"}}], "combinator": "and"},
            "options": {}}, 1700, version=2.2))
        edge("Check outcome", "Completed?")
        edge("Completed?", "Wait for job", 1)
    else:
        nodes.append(node("Check delivery", "code", {"jsCode":
            "if (['FAILED','UNKNOWN'].includes($json.status)) throw new Error('delivery_' + $json.status.toLowerCase());\n"
            "return $input.all();"}, 680, version=2))
        edge("Submit", "Check delivery")
    nodes.append(node("Setup instructions", "stickyNote", {"content":
        "## Import inactive\nBind every HTTP node to Header Auth `BRE Wind Service`: "
        "Authorization = Bearer <service token>. Edit Configure.service_url. "
        "No credentials are included. Shadow review is the default. "
        "Set Error workflow to imported BRE-WIND-05A-Errors. "
        "Read docs/wind-news-operations.md before enabling schedules.",
        "height": 240, "width": 600}, 160, -300))
    return {"name": name, "active": False, "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "timezone": "Asia/Seoul", "executionTimeout": 1200,
                         "saveDataErrorExecution": "none", "saveDataSuccessExecution": "none",
                         "saveManualExecutions": False}, "pinData": {}, "tags": []}


def build(service_url="http://news-service:8090"):
    config_body = "={{ JSON.stringify({issue_date: $('Configure').first().json.issue_date}) }}"
    definitions = [
        ("01", "Collect", "'/v1/collect'", config_body, ["5 * * * *", "30 7 * * *"], {}),
        ("02", "Prepare", "'/v1/issues/prepare'", config_body, ["40 7 * * *"], {}),
        ("03", "Publish", "'/v1/issues/' + $('Configure').first().json.issue_id + '/publish'",
         "={{ JSON.stringify({revision: $json.revision, approval_hash: $json.approval_hash}) }}",
         ["0 8 * * *"], {"publish": True}),
        ("04", "Deliver", "'/v1/deliveries/send'",
         "={{ JSON.stringify({issue_date: $('Configure').first().json.issue_date, channel_id: 'wind-news'}) }}",
         ["*/5 8-23 * * *"], {"async_job": False}),
        ("05A", "Errors", "'/v1/reconcile'", '{"trigger":"workflow_error"}', [], {"error": True}),
        ("05B", "Reconcile", "'/v1/reconcile'", config_body, ["*/5 * * * *"], {}),
        ("06", "Backup", "'/v1/backup'", "{}", ["30 2 * * *"], {}),
    ]
    return [workflow(*args, service_url, **options) for *args, options in definitions]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-url", default="http://news-service:8090")
    parser.add_argument("--output", type=Path, default=ROOT / "automation/n8n")
    args = parser.parse_args()
    parsed = urlsplit(args.service_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("service URL must be a credential-free HTTP(S) base URL")
    args.output.mkdir(parents=True, exist_ok=True)
    for item in build(args.service_url.rstrip("/")):
        (args.output / (item["name"] + ".json")).write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Generated 7 inactive workflows; no network calls made.")


if __name__ == "__main__":
    main()

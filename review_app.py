#!/usr/bin/env python3
"""
Bellhaven — review app (Step 3)

A small local web app where a reviewer sees each proposed change with its
supporting evidence and can Approve or Reject it.

  * Approve  -> the change is written to the CRM through the API.
  * Reject   -> nothing is written. Ever. The decision is only recorded.
  * Nothing writes without an explicit approval click.

Decisions are persisted in state/decisions.json (keyed by the stable
proposal_id from matcher.py). Decided proposals are never re-proposed:
matcher.py skips any proposal_id that already has a decision, and this
app disables the buttons on decided items, so an approval can never be
applied twice (safe re-runs / idempotency).

CHOW proposals: approving creates the NEW account under Bellhaven first,
then sets ONLY chow_current_account on the old account to the new id.
The old account's parent and all other fields are left untouched.

Safety check: before any write, the app re-reads the account from the CRM
and verifies it still matches the snapshot the proposal was built from.
If the CRM has changed since the proposal was generated, the write is
blocked and the item is flagged stale instead of writing blind.

Usage:
  python3 review_app.py [--port 8765] [--dry-run]
  --dry-run   rehearse safely: approvals are recorded in
              state/decisions_dryrun.json and NO CRM write calls are made.

Stdlib only — no pip installs.
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_DIR = ROOT / "state"

# ------------------------------------------------------------------ config
def load_env() -> dict:
    env = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("BELLHAVEN_")})
    return env

ENV = load_env()
API_BASE = ENV.get("BELLHAVEN_API_BASE", "https://analyst-assessment-production.up.railway.app/api/v1")
TOKEN = ENV.get("BELLHAVEN_TOKEN", "")
BELLHAVEN_PARENT_ID = "0015QAPLGS3FVYEEEM"

DRY_RUN = False  # set in main()


def decisions_path() -> Path:
    return STATE_DIR / ("decisions_dryrun.json" if DRY_RUN else "decisions.json")


def load_decisions() -> dict:
    p = decisions_path()
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_decisions(decisions: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = decisions_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(decisions, indent=2) + "\n")
    tmp.replace(decisions_path())


# ------------------------------------------------------------------ CRM API
def api(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read() or b"{}")


def api_get_account(account_id: str) -> dict:
    _, d = api("GET", f"/accounts/{account_id}")
    return d


# ------------------------------------------------------------------ apply
class StaleProposal(Exception):
    pass


def check_fresh(proposal: dict) -> dict | None:
    """Re-read the target account; block if it no longer matches the
    snapshot the proposal was built from."""
    acct = proposal.get("account")
    if not acct:
        return None
    current = api_get_account(acct["account_id"])
    for field in ("name", "parent_id", "billing_street", "billing_city",
                  "billing_state", "billing_zip", "status"):
        snap = acct.get(field if field != "parent_id" else "parent_name")
        # snapshot stores parent_name, current stores both — compare name only for parent
        if field == "parent_id":
            if (current.get("parent_name") or "") != (acct.get("parent_name") or ""):
                raise StaleProposal(
                    f'parent changed since proposal: was "{acct.get("parent_name")}", '
                    f'now "{current.get("parent_name")}"')
            continue
        if field in acct and str(current.get(field, "")) != str(acct.get(field, "")):
            raise StaleProposal(
                f'{field} changed since proposal: was "{acct.get(field)}", '
                f'now "{current.get(field)}"')
    return current


def apply_proposal(proposal: dict) -> dict:
    """Perform the CRM write(s) for an approved proposal. Returns a result
    record. In dry-run mode, records what WOULD be sent and calls nothing."""
    ptype = proposal["type"]
    changes = proposal.get("proposed_changes") or {}

    if ptype in ("update_account", "mark_duplicate", "flag_orphan"):
        account_id = proposal["account"]["account_id"]
        if DRY_RUN:
            return {"action": f"PATCH /accounts/{account_id}", "body": changes,
                    "dry_run": True}
        status, resp = api("PATCH", f"/accounts/{account_id}", changes)
        return {"action": f"PATCH /accounts/{account_id}", "status": status,
                "response_name": resp.get("name"), "response_status": resp.get("status")}

    if ptype == "create_account":
        if DRY_RUN:
            return {"action": "POST /accounts", "body": changes, "dry_run": True}
        status, resp = api("POST", "/accounts", changes)
        return {"action": "POST /accounts", "status": status,
                "created_account_id": resp.get("account_id"),
                "response_name": resp.get("name")}

    if ptype == "chow_create_new":
        plan = proposal["chow_plan"]
        old_id = plan["leave_old_account_untouched"]
        new_body = plan["create_new_account"]
        if DRY_RUN:
            return {"action": "POST /accounts (new) then PATCH old chow_current_account",
                    "body": new_body, "dry_run": True}
        status, resp = api("POST", "/accounts", new_body)
        new_id = resp.get("account_id")
        if not new_id:
            raise RuntimeError(f"CHOW create did not return an account_id (status {status})")
        # Only now touch the old account, and ONLY the chow field.
        pstatus, presp = api("PATCH", f"/accounts/{old_id}",
                             {"chow_current_account": new_id})
        return {"action": "POST /accounts + PATCH old chow_current_account",
                "status": status, "created_account_id": new_id,
                "chow_patch_status": pstatus,
                "old_account_chow_current": presp.get("chow_current_account")}

    raise ValueError(f"Unknown proposal type {ptype}")


# ------------------------------------------------------------------ page
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bellhaven Sync — Review Queue</title>
<style>
  body { font-family: Verdana, sans-serif; background:#FAF7F0; color:#2B2B26; margin:0; }
  header { background:#2E5D50; color:#fff; padding:16px 24px; }
  header h1 { font-family: Georgia, serif; font-weight:normal; margin:0; font-size:22px; }
  header p { margin:4px 0 0; font-size:12px; opacity:.85; }
  .wrap { max-width: 980px; margin: 0 auto; padding: 20px 16px 60px; }
  .counts { font-size: 13px; margin: 10px 0 18px; }
  .card { background:#fff; border:1px solid #E4DFD2; border-radius:4px; padding:16px 18px; margin-bottom:14px; }
  .card.decided { opacity:.62; }
  .badges span { display:inline-block; font-size:10.5px; border-radius:3px; padding:2px 8px; margin-right:6px; background:#EFE8D2; color:#6d5716; }
  .badges .conf-medium { background:#F6D9B8; color:#7a4a12; }
  .badges .chow { background:#2E5D50; color:#fff; }
  .badges .done { background:#2E5D50; color:#fff; }
  .badges .rej { background:#8a2f2b; color:#fff; }
  h3 { font-family: Georgia, serif; font-weight:normal; margin:8px 0 4px; color:#2E5D50; }
  .cols { display:grid; grid-template-columns:1fr 1fr; gap:14px; font-size:12.5px; }
  .cols h4 { margin:6px 0 4px; font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:#8a8574; }
  .cols p { margin:2px 0; }
  ul.ev { font-size:12.5px; margin:8px 0; padding-left:18px; }
  table.ch { font-size:12.5px; border-collapse:collapse; margin:6px 0; }
  table.ch td, table.ch th { border:1px solid #E4DFD2; padding:4px 8px; text-align:left; }
  .btns { margin-top:10px; }
  button { font-family: Verdana, sans-serif; font-size:13px; border:0; border-radius:3px; padding:8px 16px; cursor:pointer; margin-right:8px; }
  .approve { background:#2E5D50; color:#fff; }
  .reject { background:#fff; color:#8a2f2b; border:1px solid #8a2f2b; }
  button:disabled { opacity:.5; cursor:default; }
  .result { font-size:12.5px; margin-top:8px; }
  .dryrun { background:#C9A227; color:#2B2B26; padding:8px 24px; font-size:12.5px; }
</style></head><body>
<header><h1>Bellhaven Sync — Review Queue</h1>
<p>Approve writes the change to the CRM through the API. Reject writes nothing. Decided items are never re-proposed.</p></header>
__DRYRUN__
<div class="wrap"><div class="counts" id="counts"></div><div id="queue"></div></div>
<script>
let proposals = [], decisions = {};
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load() {
  const r = await fetch('/api/queue'); const d = await r.json();
  proposals = d.proposals; decisions = d.decisions;
  render();
}
function addr(a) { return a ? esc(a.billing_street) + ', ' + esc(a.billing_city) + ', ' + esc(a.billing_state) + ' ' + esc(a.billing_zip) : '—'; }
function render() {
  const pending = proposals.filter(p => !decisions[p.proposal_id]).length;
  document.getElementById('counts').textContent =
    proposals.length + ' proposals · ' + pending + ' pending · ' + (proposals.length - pending) + ' decided';
  document.getElementById('queue').innerHTML = proposals.map(p => {
    const dec = decisions[p.proposal_id];
    const site = p.site, acct = p.account;
    let changes = '';
    const rows = Object.entries(p.proposed_changes || {}).map(([k,v]) =>
      '<tr><th>' + esc(k) + '</th><td>' + esc(v) + '</td></tr>').join('');
    if (rows) changes = '<table class="ch">' + rows + '</table>';
    if (p.chow_plan) changes += '<p><b>CHOW plan:</b> leave old account untouched, create new account under Bellhaven, then set chow_current_account on the old account to the new id.</p>';
    return '<div class="card' + (dec ? ' decided' : '') + '" id="card-' + p.proposal_id + '">' +
      '<div class="badges"><span>' + esc(p.type) + '</span><span>' + esc(p.classification) + '</span>' +
      '<span class="conf-' + esc(p.confidence) + '">confidence: ' + esc(p.confidence) + '</span>' +
      (p.chow ? '<span class="chow">CHOW — SOP applies</span>' : '') +
      (dec ? '<span class="' + (dec.decision === 'approved' ? 'done' : 'rej') + '">' + esc(dec.decision) + '</span>' : '') + '</div>' +
      '<h3>' + esc(site ? site.name : (acct ? acct.name : '')) + '</h3>' +
      '<div class="cols"><div><h4>Website (source of truth)</h4>' +
      (site ? '<p><b>' + esc(site.name) + '</b></p><p>' + esc(site.address) + ', ' + esc(site.city) + ', ' + esc(site.state) + ' ' + esc(site.zip) + '</p><p>' + esc((site.care_offerings || []).join('; ')) + '</p>' : '<p>— not on website —</p>') +
      '</div><div><h4>CRM account</h4>' +
      (acct ? '<p><b>' + esc(acct.name) + '</b></p><p>' + addr(acct) + '</p><p>Parent: ' + esc(acct.parent_name || '(none)') + '</p>' : '<p>— no account —</p>') +
      '</div></div>' +
      '<ul class="ev">' + (p.evidence || []).map(e => '<li>' + esc(e) + '</li>').join('') + '</ul>' +
      changes +
      (dec ? '<div class="result">' + esc(dec.decision) + ' at ' + esc(dec.decided_at) +
             (dec.apply_result ? ' · ' + esc(JSON.stringify(dec.apply_result)) : '') +
             (dec.error ? ' · ERROR: ' + esc(dec.error) : '') + '</div>'
           : '<div class="btns"><button class="approve" onclick="decide(\\'' + p.proposal_id + '\\',\\'approved\\')">Approve</button>' +
             '<button class="reject" onclick="decide(\\'' + p.proposal_id + '\\',\\'rejected\\')">Reject</button></div>') +
      '</div>';
  }).join('');
}
async function decide(id, decision) {
  const card = document.getElementById('card-' + id);
  card.querySelectorAll('button').forEach(b => b.disabled = true);
  const r = await fetch('/api/decide', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({proposal_id: id, decision})});
  const d = await r.json();
  if (!r.ok) alert('Error: ' + (d.error || r.status));
  load();
}
load();
</script></body></html>"""


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep console clean
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = PAGE.replace("__DRYRUN__",
                                '<div class="dryrun">DRY-RUN MODE — approvals are rehearsed only, no CRM writes.</div>'
                                if DRY_RUN else "")
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/queue":
            proposals = json.loads((DATA / "proposals.json").read_text())
            self._json(200, {"proposals": proposals, "decisions": load_decisions(),
                             "dry_run": DRY_RUN})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/decide":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        proposal_id = payload.get("proposal_id")
        decision = payload.get("decision")
        if decision not in ("approved", "rejected"):
            return self._json(400, {"error": "decision must be approved or rejected"})

        proposals = {p["proposal_id"]: p
                     for p in json.loads((DATA / "proposals.json").read_text())}
        proposal = proposals.get(proposal_id)
        if not proposal:
            return self._json(404, {"error": "unknown proposal_id"})

        decisions = load_decisions()
        if proposal_id in decisions:
            return self._json(409, {"error": "already decided — re-applying is blocked",
                                    "decision": decisions[proposal_id]})

        record = {"decision": decision,
                  "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "dry_run": DRY_RUN}
        if decision == "approved":
            try:
                check_fresh(proposal)
                record["apply_result"] = apply_proposal(proposal)
                record["applied"] = not DRY_RUN
            except StaleProposal as e:
                record["error"] = f"STALE — write blocked: {e}"
                record["applied"] = False
            except Exception as e:  # API failure — record it, do not pretend success
                record["error"] = f"API error: {e}"
                record["applied"] = False
        else:
            record["applied"] = False  # rejected => no API call was made at all

        decisions[proposal_id] = record
        save_decisions(decisions)
        code = 200 if "error" not in record else 502
        return self._json(code, {"ok": "error" not in record, "record": record})


def main() -> int:
    global DRY_RUN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    DRY_RUN = args.dry_run
    if not TOKEN:
        print("BELLHAVEN_TOKEN not set (.env or environment)", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    mode = "DRY-RUN (no CRM writes)" if DRY_RUN else "LIVE (approvals write to CRM)"
    print(f"Bellhaven review app: http://127.0.0.1:{args.port}  —  {mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

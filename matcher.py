#!/usr/bin/env python3
"""
Bellhaven — matching logic (Step 2)

Links each website location (data/bellhaven_locations.json, from scraper.py)
to the right CRM account and classifies it:

  confident_match     same facility, already correct — no change proposed
  match_needs_fix     same facility, but something is wrong (outdated name,
                      wrong parent, wrong zip/street, ...) — fix proposed
  no_crm_account      website location with no CRM account — create proposed
  orphan              CRM account under the Bellhaven parent that no longer
                      appears on the website — Needs Review proposed
  duplicate           extra CRM account for a facility that already has a
                      surviving account — duplicate_of + Inactive proposed

READ-ONLY: this script only GETs from the CRM API. It writes proposals to
data/proposals.json / .csv for human review. Nothing is written to the CRM
until a reviewer approves a proposal in the review app (Step 3).

Matching rules (evidence-based, in order of strength):
  * A name match ALONE is never enough — the CRM contains a different
    "Amberly Manor" in Colorado Springs, and "Bellhaven of New Carlisle"
    (OH) must not match the website's Carlisle, PA. Every match needs
    geographic corroboration: same zip, or same city + street, or same
    street address.
  * Strong corroborators: exact street address (after abbreviation
    normalisation), exact zip, exact phone (website vs CRM), and the
    website Administrator appearing as a contact on the CRM account.
  * When several CRM accounts claim one website location, one survivor is
    chosen by: phone match > administrator contact > exact name >
    already under Bellhaven parent > has contacts > has revenue.
    The rest are proposed as duplicates of the survivor.

CHOW SOP: if a matched account must change parent AND lifetime_revenue > 0
AND outstanding_ar > 0, the proposal is flagged CHOW — do NOT re-parent.
Instead: create a new account under Bellhaven and set chow_current_account
on the old account to the new account's id (applied at approval time).
"""

import csv
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# ---------------------------------------------------------------- config
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
SITE_BASE = "https://analyst-assessment-production.up.railway.app"

CARE_MAP = {  # website offering -> CRM care_type (primary offering decides)
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
}

# ---------------------------------------------------------------- helpers
STREET_EXP = {
    "st": "street", "ave": "avenue", "av": "avenue", "rd": "road",
    "ln": "lane", "dr": "drive", "blvd": "boulevard", "ct": "court",
    "pk": "pike", "hwy": "highway", "w": "west", "e": "east",
    "n": "north", "s": "south", "nw": "northwest", "ne": "northeast",
    "sw": "southwest", "se": "southeast",
}
NAME_EXP = {"rehab": "rehabilitation", "&": "and", "ctr": "center",
            "centre": "center", "healthcare": "health care"}


def norm_street(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(STREET_EXP.get(t, t) for t in s.split())


def street_core(s: str) -> tuple[str, str]:
    """(house_number, core street name without suffix/directionals)."""
    toks = norm_street(s).split()
    house = toks[0] if toks and toks[0].isdigit() else ""
    core = [t for t in toks[1:] if t not in
            ("street", "avenue", "road", "lane", "drive", "boulevard",
             "court", "pike", "highway", "west", "east", "north", "south",
             "northwest", "northeast", "southwest", "southeast")]
    return house, " ".join(core)


def norm_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(NAME_EXP.get(t, t) for t in s.split() if t != "the")


def name_tokens(s: str) -> set[str]:
    stop = {"bellhaven", "of", "at", "and", "the"}
    return {t for t in norm_name(s).split() if t not in stop}


def api_get(path: str):
    req = urllib.request.Request(API_BASE + path,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_all(endpoint: str) -> list[dict]:
    out, page = [], 1
    while True:
        d = api_get(f"/{endpoint}?page={page}&page_size=50")
        out.extend(d["data"])
        if len(out) >= d["total"]:
            return out
        page += 1


def fetch_site_admin_phone(source_url: str) -> tuple[str, str]:
    req = urllib.request.Request(source_url, headers={"User-Agent": "bellhaven-sync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        page_html = r.read().decode("utf-8", errors="replace")
    admin = re.search(r"<dt>Administrator</dt><dd>(.*?)</dd>", page_html, re.S)
    phone = re.search(r"<dt>Phone</dt><dd>(.*?)</dd>", page_html, re.S)
    return (admin.group(1).strip() if admin else "",
            phone.group(1).strip() if phone else "")


def pid(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- scoring
def score_pair(site: dict, acct: dict, site_phone: str,
               admin_names: set[str], site_admin: str) -> tuple[int, list[str], bool]:
    """Return (score, evidence, eligible).

    Eligible requires real corroboration — ZIP/city alone is NOT enough
    (a different facility can share a town and ZIP: e.g. Maplewood Senior
    Care Center, 431 Main St, is not Bellhaven of Maplewood, 210 Orchard Ln).
    A pair is eligible only if at least one holds:
      * street address matches (house number + street core), or
      * phone matches the website exactly, or
      * the website administrator is a contact on the account, or
      * ZIP + city match AND name-token overlap >= 0.5 (rebrand pattern;
        any street conflict is then flagged for the reviewer).
    """
    ev: list[str] = []
    score = 0

    zip_ok = acct["billing_zip"] == site["zip"]
    city_ok = acct["billing_city"].lower() == site["city"].lower()
    if zip_ok:
        score += 35; ev.append(f'ZIP matches exactly ({site["zip"]})')
    if city_ok:
        score += 15; ev.append(f'City matches ({site["city"]})')
    if acct["billing_state"] == site["state"]:
        score += 5

    a_house, a_core = street_core(acct["billing_street"])
    s_house, s_core = street_core(site["address"])
    street_exact = bool(a_house and a_house == s_house and a_core and a_core == s_core)
    if street_exact:
        score += 40; ev.append(f'Street address matches ({site["address"]})')
    elif a_core and s_core and a_core == s_core and city_ok:
        score += 20; ev.append("Street name + city match (house number/format differs)")

    j = 0.0
    if norm_name(acct["name"]) == norm_name(site["name"]):
        score += 25; ev.append("Name matches website name"); j = 1.0
    else:
        at, st = name_tokens(acct["name"]), name_tokens(site["name"])
        if at and st:
            j = len(at & st) / len(at | st)
            if j >= 0.4:
                score += round(15 * j)
                ev.append(f'Name is similar (CRM: "{acct["name"]}")')

    phone_ok = bool(site_phone and acct.get("phone") == site_phone)
    if phone_ok:
        score += 30; ev.append(f'Phone matches website exactly ({site_phone})')
    admin_ok = bool(site_admin and site_admin in admin_names)
    if admin_ok:
        score += 20; ev.append(f'Website administrator "{site_admin}" is a contact on this account')

    eligible = street_exact or phone_ok or admin_ok or (zip_ok and city_ok and j >= 0.5)
    return score, ev, eligible


# ---------------------------------------------------------------- main
def main() -> int:
    if not TOKEN:
        print("BELLHAVEN_TOKEN not set (.env or environment)", file=sys.stderr)
        return 1

    sites = json.loads((DATA / "bellhaven_locations.json").read_text())
    accounts = fetch_all("accounts")
    contacts = fetch_all("contacts")

    parents = {a["account_id"]: a["name"] for a in accounts
               if "(Parent Account)" in a["name"]}
    bellhaven_parent_id = next(i for i, n in parents.items() if n.startswith("Bellhaven"))
    # Inactive accounts and accounts already marked as duplicates are not
    # live facilities — never match or flag them again (safe re-runs).
    # CHOW predecessor accounts (chow_current_account set) are settled by
    # the CHOW SOP: they stay untouched under their old parent and must
    # never be re-proposed as duplicates of their successor accounts.
    facilities = [a for a in accounts if a["account_id"] not in parents
                  and a["status"] != "Inactive"
                  and not a.get("duplicate_of_account")
                  and not a.get("chow_current_account")]

    admin_by_acct: dict[str, set[str]] = {}
    contacts_by_acct: dict[str, list] = {}
    for c in contacts:
        contacts_by_acct.setdefault(c["account_id"], []).append(c)
        if c["title"] == "Administrator":
            admin_by_acct.setdefault(c["account_id"], set()).add(c["name"])

    print(f"{len(sites)} website locations, {len(facilities)} CRM facility accounts, "
          f"Bellhaven parent = {bellhaven_parent_id}")

    proposals: list[dict] = []
    matches: list[dict] = []
    used_as_survivor: set[str] = set()
    used_as_duplicate: set[str] = set()

    for site in sites:
        admin, phone = fetch_site_admin_phone(site["source_url"])
        scored = []
        for acct in facilities:
            s, ev, geo = score_pair(site, acct, phone,
                                    admin_by_acct.get(acct["account_id"], set()), admin)
            if geo and s >= 55:
                scored.append((s, acct, ev))
        scored.sort(key=lambda x: -x[0])

        if not scored:
            proposals.append({
                "proposal_id": pid("create", site["slug"]),
                "type": "create_account",
                "classification": "no_crm_account",
                "confidence": "high",
                "site": site,
                "account": None,
                "evidence": [f'No CRM account corroborated by address/ZIP/city for '
                             f'{site["address"]}, {site["city"]}, {site["state"]} {site["zip"]}',
                             "Name-only lookalikes were rejected (geographic corroboration required)"],
                "proposed_changes": {
                    "name": site["name"],
                    "parent_id": bellhaven_parent_id,
                    "billing_street": site["address"],
                    "billing_city": site["city"],
                    "billing_state": site["state"],
                    "billing_zip": site["zip"],
                    "care_type": CARE_MAP.get(site["care_offerings"][0], ""),
                    "status": "Active",
                    "phone": phone,
                },
                "chow": False,
                "status": "pending",
            })
            matches.append({"site": site["name"], "classification": "no_crm_account",
                            "account": None})
            continue

        # survivor tie-break within the cluster
        def survivor_key(item):
            s, acct, ev = item
            return (acct.get("phone") == phone and phone != "",
                    admin in admin_by_acct.get(acct["account_id"], set()),
                    norm_name(acct["name"]) == norm_name(site["name"]),
                    acct["parent_id"] == bellhaven_parent_id,
                    len(contacts_by_acct.get(acct["account_id"], [])),
                    acct["lifetime_revenue"], s)
        scored.sort(key=survivor_key, reverse=True)
        _, surv, sev = scored[0]
        used_as_survivor.add(surv["account_id"])

        # Ambiguous cluster: several accounts claim this location and NO
        # evidence (phone / admin contact / exact name / Bellhaven parent)
        # distinguishes the survivor — the reviewer must confirm the choice.
        ambiguous = False
        if len(scored) > 1:
            top_keys = [survivor_key(it)[:5] for it in scored]
            ambiguous = all(k == top_keys[0] for k in top_keys)
        if ambiguous:
            sev = sev + ["AMBIGUOUS CLUSTER: multiple CRM accounts at this address "
                         "with no phone/contact/name evidence distinguishing them — "
                         "survivor choice needs reviewer confirmation"]

        # duplicates in this cluster
        for _, dup, dev in scored[1:]:
            used_as_duplicate.add(dup["account_id"])
            if ambiguous:
                dev = dev + ["AMBIGUOUS CLUSTER: survivor choice needs reviewer confirmation "
                             "(no distinguishing phone/contact/name evidence)"]
            proposals.append({
                "proposal_id": pid("duplicate", dup["account_id"], surv["account_id"]),
                "type": "mark_duplicate",
                "classification": "duplicate",
                "confidence": "medium" if ambiguous else "high",
                "site": site,
                "account": {"account_id": dup["account_id"], "name": dup["name"],
                            "parent_name": dup["parent_name"],
                            "billing_street": dup["billing_street"],
                            "billing_city": dup["billing_city"],
                            "billing_state": dup["billing_state"],
                            "billing_zip": dup["billing_zip"]},
                "evidence": dev + [f'Same facility as surviving account "{surv["name"]}" '
                                    f'({surv["account_id"]}) — one website location, '
                                    f'{len(scored)} CRM accounts'],
                "proposed_changes": {"duplicate_of_account": surv["account_id"],
                                     "status": "Inactive",
                                     "note": f'Duplicate of {surv["name"]} ({surv["account_id"]}). '
                                             f'Same facility as website location {site["name"]}. '
                                             f'Marked Inactive per duplicate convention.'},
                "chow": False,
                "status": "pending",
            })

        # diff survivor vs website truth
        changes: dict[str, str] = {}
        notes: list[str] = []
        if surv["name"].strip() != site["name"].strip():
            changes["name"] = site["name"]
            kind = "outdated name" if norm_name(surv["name"]) != norm_name(site["name"]) \
                else "name spelling/format differs from website"
            notes.append(f'Name differs: CRM "{surv["name"]}" vs website "{site["name"]}" ({kind})')
        if norm_street(surv["billing_street"]) != norm_street(site["address"]):
            changes["billing_street"] = site["address"]
            notes.append(f'Street differs: CRM "{surv["billing_street"]}" vs website "{site["address"]}"')
        if surv["billing_city"].lower() != site["city"].lower():
            changes["billing_city"] = site["city"]; notes.append("City differs")
        if surv["billing_state"] != site["state"]:
            changes["billing_state"] = site["state"]; notes.append("State differs")
        if surv["billing_zip"] != site["zip"]:
            changes["billing_zip"] = site["zip"]
            notes.append(f'ZIP differs: CRM {surv["billing_zip"]} vs website {site["zip"]}')
        want_care = CARE_MAP.get(site["care_offerings"][0], "")
        if want_care and surv["care_type"] != want_care:
            changes["care_type"] = want_care
            notes.append(f'Care type differs: CRM "{surv["care_type"]}" vs website "{site["care_offerings"][0]}"')
        if len(site["care_offerings"]) > 1:
            notes.append(f'Website lists additional offerings {site["care_offerings"][1:]}; '
                         f'CRM care_type is single-value, primary kept')

        chow = False
        if surv["parent_id"] != bellhaven_parent_id:
            if surv["lifetime_revenue"] > 0 and surv["outstanding_ar"] > 0:
                chow = True
                notes.append(f'CHOW: parent change needed but lifetime_revenue='
                             f'{surv["lifetime_revenue"]} and outstanding_ar='
                             f'{surv["outstanding_ar"]} — must NOT re-parent directly')
            else:
                changes["parent_id"] = bellhaven_parent_id
                notes.append(f'Wrong parent: CRM parent is "{surv["parent_name"] or "(none)"}", '
                             f'should be Bellhaven Senior Living (revenue={surv["lifetime_revenue"]}, '
                             f'AR={surv["outstanding_ar"]} — direct re-parent allowed)')

        if changes or chow:
            proposals.append({
                "proposal_id": pid("chow" if chow else "fix", surv["account_id"], site["slug"]),
                "type": "chow_create_new" if chow else "update_account",
                "classification": "match_needs_fix",
                "confidence": "medium" if ambiguous or (
                    "billing_street" in changes
                    and not any("Phone matches" in e or "administrator" in e for e in sev)
                ) else "high",
                "site": site,
                "account": {"account_id": surv["account_id"], "name": surv["name"],
                            "parent_name": surv["parent_name"],
                            "billing_street": surv["billing_street"],
                            "billing_city": surv["billing_city"],
                            "billing_state": surv["billing_state"],
                            "billing_zip": surv["billing_zip"],
                            "lifetime_revenue": surv["lifetime_revenue"],
                            "outstanding_ar": surv["outstanding_ar"]},
                "evidence": sev + notes,
                "proposed_changes": changes,
                "chow": chow,
                "chow_plan": ({"leave_old_account_untouched": surv["account_id"],
                               "create_new_account": {
                                   "name": site["name"], "parent_id": bellhaven_parent_id,
                                   "billing_street": site["address"],
                                   "billing_city": site["city"],
                                   "billing_state": site["state"],
                                   "billing_zip": site["zip"],
                                   "care_type": want_care, "status": "Active",
                                   "phone": phone},
                               "set_on_old_account": {"chow_current_account": "<new account id>"}}
                              if chow else None),
                "status": "pending",
            })
            matches.append({"site": site["name"], "classification": "match_needs_fix",
                            "account": surv["name"]})
        else:
            matches.append({"site": site["name"], "classification": "confident_match",
                            "account": surv["name"]})

    # orphans: Bellhaven-parent facilities never used above
    for acct in facilities:
        if (acct["parent_id"] == bellhaven_parent_id
                and acct["account_id"] not in used_as_survivor
                and acct["account_id"] not in used_as_duplicate):
            proposals.append({
                "proposal_id": pid("orphan", acct["account_id"]),
                "type": "flag_orphan",
                "classification": "orphan_bellhaven_parent",
                "confidence": "high",
                "site": None,
                "account": {"account_id": acct["account_id"], "name": acct["name"],
                            "parent_name": acct["parent_name"],
                            "billing_street": acct["billing_street"],
                            "billing_city": acct["billing_city"],
                            "billing_state": acct["billing_state"],
                            "billing_zip": acct["billing_zip"],
                            "lifetime_revenue": acct["lifetime_revenue"],
                            "outstanding_ar": acct["outstanding_ar"]},
                "evidence": ["Under Bellhaven parent in CRM but no matching location "
                             "on the Bellhaven website (all 35 locations checked)"],
                "proposed_changes": {
                    "status": "Needs Review",
                    "note": "Under Bellhaven parent but not listed on the Bellhaven "
                            "website as of this run — may have been sold, closed, or "
                            "rebranded. Flagged Needs Review per CRM conventions."},
                "chow": False,
                "status": "pending",
            })
            matches.append({"site": None, "classification": "orphan_bellhaven_parent",
                            "account": acct["name"]})

    # Re-run safety: never re-propose an item that was already decided in
    # the review app (state/decisions.json is keyed by stable proposal_id).
    decisions_file = ROOT / "state" / "decisions.json"
    if decisions_file.exists():
        decided = set(json.loads(decisions_file.read_text()).keys())
        before = len(proposals)
        proposals = [p for p in proposals if p["proposal_id"] not in decided]
        if before != len(proposals):
            print(f"Skipped {before - len(proposals)} already-decided proposals")

    (DATA / "proposals.json").write_text(json.dumps(proposals, indent=2) + "\n")
    with (DATA / "proposals.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["proposal_id", "type", "classification", "confidence", "chow",
                    "site_name", "account_name", "account_id", "proposed_changes", "evidence"])
        for p in proposals:
            w.writerow([p["proposal_id"], p["type"], p["classification"], p["confidence"],
                        p["chow"], p["site"]["name"] if p["site"] else "",
                        p["account"]["name"] if p["account"] else "",
                        p["account"]["account_id"] if p["account"] else "",
                        json.dumps(p["proposed_changes"]),
                        " | ".join(p["evidence"])])
    (DATA / "match_report.json").write_text(json.dumps(matches, indent=2) + "\n")

    from collections import Counter
    print("\nClassification counts (website locations):")
    site_counts = Counter(m["classification"] for m in matches if m["site"])
    for k, v in sorted(site_counts.items()):
        print(f"  {k}: {v}")
    print(f"Proposals total: {len(proposals)} "
          f"({Counter(p['type'] for p in proposals)})")
    print("Wrote data/proposals.json, data/proposals.csv, data/match_report.json")
    print("NO CRM writes were made — all proposals are pending review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

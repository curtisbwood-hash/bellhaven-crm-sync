# Bellhaven CRM Sync

Reconciles the Bellhaven Senior Living CRM sandbox against the public
website (the source of truth for communities) and produces human-reviewed
proposals for every discrepancy. Nothing is written to the CRM without a
reviewer's approval.

## Pipeline

1. **Scraper** (`scraper.py`) — pulls every Bellhaven community (name,
   address, city, state, ZIP, care offerings) from the website. It scans
   the homepage as well as all directory pages: the directory lists 34
   locations, but a 35th (Bellhaven Meadows of Findlay) is only linked
   from the homepage. Output: `data/bellhaven_locations.csv/.json`.
2. **Matcher** (`matcher.py`) — classifies each website location against
   CRM accounts. A name match alone is never sufficient; a match needs
   geographic corroboration (same street address, exact phone match, the
   website's Administrator appearing as a CRM contact, or ZIP + city +
   strong name overlap). Emits proposals: updates, duplicates (loser gets
   `duplicate_of_account` + Inactive), creates, CHOW cases (per the CHOW
   SOP: revenue history + outstanding AR means never re-parent — create
   under the correct parent and set `chow_current_account` on the old
   account), and orphans (flagged Needs Review). Output:
   `data/proposals.json/.csv`, `data/match_report.json`.
3. **Review app** (`review_app.py`) — local web UI listing each proposal
   with its supporting evidence. Approve applies the change through the
   CRM API; reject writes nothing. Decisions persist in
   `state/decisions.json`; a freshness re-read blocks stale writes, and
   decided proposals are never re-applied or re-proposed, so daily
   re-runs are safe.

## Run it

```bash
cp .env.example .env   # fill in BELLHAVEN_TOKEN
python3 scraper.py
python3 matcher.py
python3 review_app.py --port 8765   # review queue at http://127.0.0.1:8765
```

## Daily schedule

`.github/workflows/daily-sync.yml` runs the scraper + matcher daily and
uploads fresh proposals as a build artifact. `run_daily.sh` is the local
cron equivalent. Neither writes to the CRM — review is always human.

## This run

29 proposals were generated, reviewed one by one, and all 29 approved
changes were applied and verified in the CRM (`state/decisions.json`).
A full re-run afterward classifies all 35 website locations as confident
matches and generates zero new proposals
(`data/rerun_match_report.json`, `data/rerun_proposals.json`).

See `WRITEUP.md` for the approach, judgment calls, and AI-use notes.

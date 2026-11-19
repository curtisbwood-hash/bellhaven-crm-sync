# Bellhaven CRM Reconciliation — Writeup

**Time spent:** ~2 hours (reading the brief and CRM conventions, building, and reviewing all 29 proposals one by one before anything was applied).

## Matching approach

The scraper pulls every community from the Bellhaven website — not just the directory pages. The directory lists 34 locations, but the homepage's "New this year" notice links to a 35th (Bellhaven Meadows of Findlay) that has its own live page, so the scraper scans the homepage plus all directory pages and dedupes to the full 35.

The matcher scores each website location against CRM accounts, but a name match alone is never enough — every match needs geographic corroboration: the same street address, an exact phone match, the website's named Administrator appearing as a contact on the CRM account, or ZIP + city + strong name overlap. Each location lands in one bucket: confident match, match needing a fix, no CRM account yet, or a CRM account under the Bellhaven parent that is no longer on the website (flagged Needs Review — never deleted; the API has no delete, and flagging preserves the history). Proposals go to a local review app that shows the evidence side by side; nothing writes to the CRM until a reviewer approves, and approved changes are applied through the API and verified. Decisions are stored, so daily re-runs never re-propose settled items — verified by re-running the full pipeline after the review: all 35 locations classify as confident matches and zero new proposals are generated.

## Judgment calls

Duplicates are handled with the CRM's own convention: the losing copy gets `duplicate_of_account` set to the survivor and is marked Inactive. The survivor is the copy with corroborating evidence (e.g., the Owosso copy whose phone matches the website and holds the website's Administrator as a contact). One cluster had no distinguishing evidence at all — three Kettering accounts at one address, all with $0 revenue/AR and no contacts — so the survivor was picked, renamed, and re-parented, and the other two marked as its duplicates; with no money or contacts attached, the choice carries no financial risk. Parent changes follow the CHOW SOP strictly: when an account has revenue history and outstanding AR (Marietta: $51,250 / $3,800; Tiffin: $84,000 / $12,400), it is left untouched, a new account is created under Bellhaven, and `chow_current_account` on the old account points to the new one. Accounts with AR of $0 were re-parented directly. One medium-confidence match (Union Square — name/city/ZIP match but a conflicting street address) was reviewed twice before approval; the website is the source of truth, so its address won.

## AI use

I used an AI assistant (Muse) throughout as a pair programmer and analyst: drafting the scraper, matcher, and review app; cross-checking the scrape with a second independent parser (35/35 records, zero mismatches); and preparing each proposal's evidence packet. Every one of the 29 proposals was reviewed and approved or rejected by me individually before it touched the CRM, and each applied change was verified in the CRM afterward. The AI caught real issues — a missing 35th location, a ZIP transposition in Portsmouth (45626 vs 45662) — and I caught one of its: a create proposal missing the facility's phone number, rejected and corrected before approval.

## What I'd build next

Contact reconciliation (the same treatment for the 67 CRM contacts: dedupe, stale administrators, missing contacts for newly created accounts), a confidence-scored auto-approval tier for the highest-confidence name fixes with human review reserved for medium/low confidence, and a run-over-run diff report so a reviewer sees only what changed since yesterday. I'd also wire the GitHub Actions schedule (config included) to open the review queue with a notification when new proposals appear.

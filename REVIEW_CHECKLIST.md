# Bellhaven Review Checklist — use for EVERY proposal

Work each proposal top to bottom. Only Approve writes to the CRM (through
the API). Reject writes nothing. Once decided, an item never comes back.

## 1. Identify what you're looking at
- [ ] What type is it? Update / Create / Duplicate / Orphan / CHOW
- [ ] What classification? Confident fix, needs-fix, no-account, orphan
- [ ] Confidence flag: **High** = proceed. **Medium** = STOP — discuss first
      (Kettering and Union Square are the two mediums in this queue)

## 2. Check the evidence (website = source of truth)
- [ ] Name: website name vs CRM name — is the CRM name outdated/different?
- [ ] Street address: do they match? (Ln vs Lane, St vs Street = same.
      A different street or a PO Box = a real difference)
- [ ] City, state, ZIP: all three match? (Portsmouth rule: a transposed ZIP
      like 45626 vs 45662 is a CRM error to fix, not a different facility)
- [ ] Care offerings: website offerings vs CRM care type
      (Assisted Living = Assisted Living · Memory Support = Memory Care ·
      Short-Term Rehabilitation & Nursing = Skilled Nursing)
- [ ] Corroboration: is this match backed by more than the name?
      Same street address, same ZIP, exact phone match, or the website
      Administrator showing as a contact on the CRM account.
      **Name alone is never enough** (the Colorado "Amberly Manor" trap)

## 3. Check the parent
- [ ] Is the account under Bellhaven Senior Living?
- [ ] If not, a parent fix is needed — then run the CHOW check (step 4)
- [ ] If it has no parent at all, same thing: it needs Bellhaven as parent

## 4. CHOW check — MANDATORY before any parent change (the SOP)
- [ ] Look at `lifetime_revenue` AND `outstanding_ar`
- [ ] BOTH greater than zero? → **CHOW route, no exceptions:**
      do NOT re-parent the old account. Leave it exactly as it is,
      create a NEW account under Bellhaven, and set
      `chow_current_account` on the OLD account to the new account's ID
- [ ] Revenue is $0 OR AR is $0? → Direct re-parent is allowed

## 5. Type-specific checks
**If it's a Duplicate:**
- [ ] Same street address as the surviving account? (Same town/ZIP alone
      is NOT enough — different street = different facility, reject it)
- [ ] Is the survivor the right keeper? Best evidence wins: phone match,
      administrator contact, exact Bellhaven name, already under Bellhaven
- [ ] Loser gets: `duplicate_of_account` = survivor's ID + status Inactive
      + a note explaining why. There is no merge/delete — this is the way

**If it's a Create (no CRM account):**
- [ ] You searched/confirmed no account exists for that address/ZIP —
      the matcher rejected name-only lookalikes, spot-check one if unsure
- [ ] New account fields match the website exactly, parent = Bellhaven

**If it's an Orphan (under Bellhaven, not on the website):**
- [ ] You confirmed it's not among the 35 website locations
- [ ] Action is status = Needs Review + explanatory note.
      Never delete, never inactivate on suspicion alone

## 6. Decide
- [ ] Evidence supports the change and the right rule was applied → **Approve**
- [ ] Evidence is thin, conflicting, or the wrong account was picked → **Reject**
- [ ] Medium confidence or anything you can't explain in one sentence
      → pause and discuss before deciding

## 7. Verify after approving
- [ ] The app reports the write result (no error, no STALE block)
- [ ] Spot-check the changed account in the CRM browser — the change landed
      (new name / parent / ZIP / status, or the new CHOW account exists and
      the old account points to it)
- [ ] Move to the next proposal — decided items won't reappear on re-runs

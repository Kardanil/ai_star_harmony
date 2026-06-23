# HRStuff — HR CV screening & ATS automation

A toolkit that drives a **logged-in Chrome** (over the Chrome DevTools Protocol) to do two jobs:

1. **Screen candidates already in the Harmony ATS** (app.harmonyats.org): download résumés, judge them by reading, and move them through the hiring funnel (advance / reject / backlog).
2. **Import hh.ru applicants into Harmony** via the *HarmonyHunter* browser extension, and triage them on hh (move "Все" → "Подумать").

Everything is plain Python in `src/` that attaches to one Chrome instance via a remote-debugging port. There are no API keys — the scripts automate the real web UIs you are logged into.

---

## Setup — run Chrome on a debugging port

Every script attaches to a Chrome you launch with remote debugging enabled, using a dedicated profile (`chrome-profile/`) so logins persist across runs:

```bash
# from the repo root
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9347 \
  --user-data-dir="$PWD/chrome-profile" \
  --no-first-run --no-default-browser-check \
  https://app.harmonyats.org/
```

If Chrome is already running on that port (or won't start because the profile is locked):

```bash
pkill -f "remote-debugging-port=9347"
rm -f chrome-profile/Singleton*
# then relaunch with the command above
```

In that Chrome window, sign in to whatever the task needs:

- **app.harmonyats.org** — the ATS. Needed for screening and all funnel moves.
- **hh.ru** (employer account, host `spb.hh.ru`) — needed only for the hh→Harmony import.
- **HarmonyHunter extension** — click its toolbar icon and log in (workspace `https://app.harmonyats.org` + your email/password). Needed only for the import; `upload_hh_to_harmony.py --preflight-only` verifies it.

The default debug port is **9347**; every script accepts `--port`. Run all scripts **from the repo root** (e.g. `python3 src/process.py …`) — they import each other by module name.

Python deps: `requests`, `websocket-client`.

---

## Repo layout

- **`src/`** — the scripts (below).
- **`data/`** — downloaded per-vacancy data (résumé text files, `index.tsv`, `decisions.tsv`, logs). **Candidate PII — do not share.**
- **`docs/`** — input CSVs (e.g. the "Все вакансии" sheet mapping CRM vacancies → hh postings).
- **`chrome-profile/`** — the Chrome debugging profile = your logged-in sessions. **Never commit or share — it contains credentials/cookies.**
- **`old_algo/`** — archived earlier code (superseded scripts).

---

## The driver model — `src/harmony.py`

The shared abstraction the Harmony scripts build on:

- **Identity = `openId`** — a UUID that appears in the URL once a candidate card is opened. Decisions are keyed by `openId`, **never by display name** (names drop patronymics on cards and are not unique).
- **`wait_dom(cond_js)`** — waits until a DOM predicate is true (MutationObserver + CDP `awaitPromise`); **no fixed `sleep`s** in the action path. (SPA in-pane updates don't change `document.readyState`, so we key off DOM content.)
- **`move(vacancy, target)`** — moves the open candidate to a funnel stage (Сменить этап → pick vacancy → pick stage → [reason if Rejection] → Сохранить) and **verifies** the source-stage count dropped.

---

## Pipeline A — screen candidates already in Harmony

1. **List vacancies + funnel counts:** `python3 src/list_vacancies.py` → `data/vacancies.tsv`.
2. **Download a vacancy's incoming résumés to disk:**
   `python3 src/download_harmony_candidates.py --url "<vacancy URL with ?stageId=…>" --out data/<vac>`
   Writes each résumé as a text file plus `index.tsv` (seq → name, openId, detail_url). Resumable (`--reindex`, `--limit`, `--delay`).
3. **Judge — READ the files yourself** and decide per candidate (see Rules). Write `data/<vac>/decisions.tsv` with columns `seq`, `action` (`advance` | `reject` | `backlog`), optional `reason`.
4. **Apply the decisions:**
   `python3 src/process.py --base data/<vac> --vacancy "<CRM vacancy name>"`
   One pass over the stage: advances → "Первичный отбор AI", rejects → "Rejection" (with reason), backlogs → "Бэклог резюме". Undecided cards are **skipped by default** (not rejected). Useful flags: `--dry-run` (preview from the file, no browser), `--only <seqs>`, `--reject-all` (reject everyone in the stage), `--source-stage`/`--stage-id`.

---

## Pipeline B — import hh.ru applicants into Harmony

`python3 src/upload_hh_to_harmony.py --vacancy "<CRM vacancy name>"`

Reads the vacancy's hh postings from the CSV in `docs/`, walks each posting's "Все" responses, and per candidate:
1. opens the résumé in a **new tab**;
2. drives the HarmonyHunter popup to save the candidate onto the CRM vacancy (stage "Входящий поток");
3. on success, moves them on hh from "Все" → **"Подумать"** (Пригласить → Подумать → Изменить статус — this also **sends the candidate the templated message**).

Flags: `--preflight-only` (check Chrome + hh login + extension login, then exit), `--limit N` (small test), `--no-consider` (upload only), `--no-message` (move without messaging), `--dry`.

Notes / known limits:
- hh's candidate list shows **~52 per posting** at a time; moving processed candidates out of "Все" lets the next batch load (how we page past the cap).
- A failed "Подумать" move just leaves the candidate in "Все" (logged) — uploads are unaffected.

---

## Rules (IMPORTANT)

- **ALWAYS download CVs to disk** when screening, unless explicitly told otherwise. `src/download_harmony_candidates.py` saves each résumé as text under `data/<vacancy>/`. Do not keep them only in memory.
- **Do NOT write keyword-based scoring scripts for ranking.** Download the CVs, then **READ each file yourself** (READ — not grep/search/keyword-count) and judge quality on actual content (experience, employers, education, relevance to the role). Fanning out subagents that each read full CVs is fine; keyword heuristics are not.
- **Do NOT click by coordinates (x/y) — drive the UI through the DOM** (`element.click()` + selectors, waiting on DOM predicates), for as long as that works. This applies to Harmony **and** hh.ru (hh's React controls accept DOM clicks — that was verified end-to-end). Coordinate / CDP `Input` mouse clicks are brittle (stale coords when the list re-renders, buttons below the fold, focus races) and were the cause of the flaky "Подумать" move — only fall back to them if a specific control genuinely cannot be driven via the DOM.

---

## Notes

- Default Chrome debug port **9347** (`--port` everywhere). hh employer host is `spb.hh.ru`.
- Harmony "Входящий поток" stage id is global; other stage ids are per-vacancy (read from the funnel sidebar `<a href ...stageId=...>`).
- Bulk funnel moves verify by the **stage-count change**, not a per-candidate status readback (which can race).

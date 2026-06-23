#!/usr/bin/env python3
"""Upload hh.ru applicants into the Harmony CRM via the HarmonyHunter browser
extension, AND triage them on hh by moving each processed candidate from "Все"
into the "Подумать" status.

Per candidate (live top-of-list loop, see process_vacancy):
  1. take the top card in hh's "Все" list  ->  its resume hash
  2. open that resume in a NEW tab; drive the HarmonyHunter popup to save the
     candidate onto the matching CRM vacancy (source=hh, stage=Входящий поток);
     close the resume tab
  3. ONLY if the upload succeeded: on the list, click the card's "Пригласить" ->
     "Подумать" -> "Изменить статус" (sends the templated message by default).
     This removes the candidate from "Все" (verified: "Все" count drops by 1).
  4. repeat; when the rendered list empties, reload to backfill the next batch
     from the larger "Все" pool (this is how we page past hh's ~52/posting cap).

How the extension is driven (no clickable toolbar icon over CDP):
  popup.html reads chrome.tabs.query({active,currentWindow}) and scrapes that tab.
  So we open the resume in a tab, keep it ACTIVE, open popup.html as a background
  target, then pick vacancy+stage and click "ВЗЯТЬ НА ВАКАНСИЮ".

hh ignores synthetic JS clicks on its React controls — the "Пригласить"/"Подумать"/
"Изменить статус" path is driven with CDP Input.dispatchMouseEvent (trusted clicks).

Prereqs: Chrome on --port with the HarmonyHunter extension, logged into BOTH
hh.ru (employer) AND app.harmonyats.org. Run preflight() first (the script does).

Examples:
  python3 src/upload_hh_to_harmony.py --vacancy "ВТБ / Applied ML / Signal Processing Engineer" --preflight-only
  python3 src/upload_hh_to_harmony.py --vacancy "ВТБ / ..." --limit 2          # small live test
  python3 src/upload_hh_to_harmony.py --vacancy "ВТБ / ..."                     # full run
  python3 src/upload_hh_to_harmony.py --vacancy "ВТБ / ..." --no-consider --dry # old behavior: upload only, dry
"""
import argparse
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests
import websocket

EXT_ID = "imgboigaaajoljpnnilbpdkejdlodgfo"          # HarmonyHunter
POPUP = f"chrome-extension://{EXT_ID}/popup.html"
HH = "https://spb.hh.ru"
RESP_URL = HH + "/employer/vacancyresponses?vacancyId={vid}&hhtmFrom=vacancy"
RESUME_URL = HH + "/resume/{h}?vacancyId={vid}&hhtmFrom=employer_candidates"
DEFAULT_CSV = "docs/Все вакансии - Актуальные вакансии.csv"
DEFAULT_STAGE = "Входящий поток"
CONSIDER_ITEM = '[data-qa="change-topic-menu-item__consider"]'   # "Подумать" funnel-status item
STATUS_DIALOG_RE = "Изменить статус резюме"
COMMIT_BUTTON = "Изменить статус"

HARMONY_VAC_ID = {  # only for optional count verification
    "Kaspi / Инженер по разработке AI-агентов (LLM) /": "019e6a51-78a4-7545-be88-a49ddff98a64",
    "Kaspi / ML-инженер по обучению LLM": "019e6a50-9bb0-7507-bfb7-ced312a035d3",
    "КРИТ / RAG / LLM инженер": "019e6a4f-8ea2-7136-a571-5d952c93f6bc",
    "ВТБ / Applied ML / Signal Processing Engineer": "019e2b52-29bb-7002-a381-46f8953f82e6",
    "СБЕР / Data Science / ML Аналитик ERP": "019e2b07-c805-7354-bd59-f2f29c32698a",
}


# ----------------------------------------------------------------------------- CDP
class CDP:
    """Browser-level CDP over one websocket, using flat sessions (sessionId)."""

    def __init__(self, port):
        self.port = port
        url = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=5).json()["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(url, timeout=35, suppress_origin=True, max_size=None)
        self.n = 0

    def cmd(self, method, params=None, sid=None, timeout=30):
        self.n += 1
        mid = self.n
        msg = {"id": mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.settimeout(timeout + 1)
        self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                break
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result", {})
        raise TimeoutError(method)

    def targets(self):
        return self.cmd("Target.getTargets")["targetInfos"]

    def attach(self, target_id):
        return self.cmd("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]

    def evalp(self, sid, expr, timeout=20):
        r = self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True},
                     sid=sid, timeout=timeout)
        return r.get("result", {}).get("value")

    def navigate(self, sid, target_id, url, settle=4.0):
        self.cmd("Page.navigate", {"url": url}, sid=sid)
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.evalp(sid, "document.readyState==='complete' && !!document.body"):
                break
            time.sleep(0.4)
        time.sleep(settle)
        self.cmd("Target.activateTarget", {"targetId": target_id})

    def click_xy(self, sid, x, y):
        """Trusted mouse click (hh's React ignores JS .click())."""
        for ty in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": ty, "x": x, "y": y}
            if ty != "mouseMoved":
                p.update(button="left", clickCount=1)
            self.cmd("Input.dispatchMouseEvent", p, sid=sid)
            time.sleep(0.05)

    def center(self, sid, finder_js, timeout=8.0):
        """Return the viewport-center {x,y} of the element returned by finder_js
        (a JS expression yielding an element), once it is visible; else None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.evalp(sid, "(()=>{const e=(%s);if(!e)return null;e.scrollIntoView({block:'center'});"
                                "const r=e.getBoundingClientRect();return (r.width>0&&r.height>0)?{x:r.x+r.width/2,y:r.y+r.height/2}:null})()" % finder_js)
            if r:
                return r
            time.sleep(0.3)
        return None


def js(s):
    return json.dumps(s, ensure_ascii=False)


def new_tab(port, url):
    endpoint = f"http://127.0.0.1:{port}/json/new?{quote(url, safe='')}"
    resp = requests.put(endpoint, timeout=5)
    if resp.status_code >= 400:
        resp = requests.get(endpoint, timeout=5)
    return resp.json()["targetId"]


# --------------------------------------------------------------------------- CSV
def parse_csv(path):
    rows = list(csv.reader(open(path, encoding="utf-8")))
    out, cur = [], None
    for r in rows[1:]:
        status, hhname, link, crm, _desc = (r + [""] * 5)[:5]
        link = link.strip()
        if crm.strip():
            cur = {"crm": crm.strip(), "hh_name": hhname.strip(), "links": []}
            out.append(cur)
        if link and cur is not None:
            cur["links"].append(link)
    for p in out:
        ids, seen = [], set()
        for l in p["links"]:
            m = re.search(r"/vacancy/(\d+)", l)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                ids.append(m.group(1))
        p["hh_ids"] = ids
    return out


def resolve_vacancy(positions, query):
    q = query.strip().lower()
    for p in positions:
        if p["crm"].lower() == q:
            return p
    for p in positions:
        if q in p["crm"].lower() or q in p["hh_name"].lower():
            return p
    raise SystemExit(f"Vacancy not found in CSV: {query!r}\nAvailable:\n  " +
                     "\n  ".join(p["crm"] for p in positions))


# ----------------------------------------------------------------------- preflight
def preflight(port, want_vacancy=None):
    """Check Chrome+port, a usable page tab, hh.ru login, and the extension.
    Returns (cdp, list_tid, list_sid). Raises SystemExit with a clear reason."""
    # 1. Chrome reachable on the debug port
    try:
        requests.get(f"http://127.0.0.1:{port}/json/version", timeout=5).raise_for_status()
    except Exception as e:
        raise SystemExit(f"PREFLIGHT FAIL: Chrome not reachable on debug port {port} ({e}).\n"
                         f"Launch it: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' "
                         f"--remote-debugging-port={port} --user-data-dir=.../chrome-profile ...")
    cdp = CDP(port)

    # 2. a page target (create one on hh.ru if none)
    page = next((t for t in cdp.targets() if t["type"] == "page" and (t.get("url") or "").startswith("http")), None)
    if not page:
        new_tab(port, HH + "/employer")
        time.sleep(3)
        page = next((t for t in cdp.targets() if t["type"] == "page" and (t.get("url") or "").startswith("http")), None)
    if not page:
        raise SystemExit("PREFLIGHT FAIL: no usable page tab and could not open one.")
    tid = page["targetId"]
    sid = cdp.attach(tid)
    cdp.cmd("Page.enable", sid=sid)
    cdp.cmd("Runtime.enable", sid=sid)

    # 3. hh.ru logged in (employer)
    cdp.navigate(sid, tid, HH + "/employer", settle=4)
    url = cdp.evalp(sid, "location.href") or ""
    body = (cdp.evalp(sid, "(document.body.innerText||'').slice(0,400)") or "")
    if "/account/login" in url or "/auth" in url or "Войти" in body[:120]:
        raise SystemExit("PREFLIGHT FAIL: hh.ru is not logged in (employer). Log in manually in the Chrome window, then retry.")

    # 4. HarmonyHunter extension present AND logged in — open the popup and read it.
    #    Logged-out popup shows "Вход в HarmonyHunter" + a password field and NO
    #    vacancy combobox, so every upload would fail with "candidate not loaded".
    try:
        pop = cdp.cmd("Target.createTarget", {"url": POPUP, "background": True})["targetId"]
    except Exception as e:
        raise SystemExit(f"PREFLIGHT FAIL: HarmonyHunter extension ({EXT_ID}) not installed/enabled ({e}).")
    psid = cdp.attach(pop)
    cdp.cmd("Runtime.enable", sid=psid)
    body = ""
    for _ in range(10):
        body = cdp.evalp(psid, "((document.body && document.body.innerText) || '')") or ""
        if body.strip():
            break
        time.sleep(0.5)
    logged_out = bool(cdp.evalp(psid, r"""(()=>{const b=document.body.innerText||'';return /Вход в HarmonyHunter/i.test(b) || document.querySelectorAll('input[type=password]').length>0})()"""))
    try:
        cdp.cmd("Target.closeTarget", {"targetId": pop})
    except Exception:
        pass
    if not body.strip() and not any(EXT_ID in (t.get("url") or "") for t in cdp.targets()):
        raise SystemExit(f"PREFLIGHT FAIL: HarmonyHunter extension ({EXT_ID}) not found/enabled in this Chrome profile.")
    if logged_out:
        raise SystemExit("PREFLIGHT FAIL: HarmonyHunter extension is NOT logged in.\n"
                         "Open the extension popup in the Chrome window and sign in "
                         "(workspace URL https://app.harmonyats.org + email + password), then retry.")

    print(f"PREFLIGHT OK: Chrome:{port} · page tab · hh.ru logged in · extension present & logged in", flush=True)
    return cdp, tid, sid


# ------------------------------------------------------------------- hh list reads
def list_hashes(cdp, sid):
    return cdp.evalp(sid, r"""(()=>{const s=[];const seen=new Set();for(const a of document.querySelectorAll('a[href*="/resume/"]')){const m=(a.getAttribute('href')||'').match(/\/resume\/([0-9a-f]+)/);if(m&&!seen.has(m[1])){seen.add(m[1]);s.push(m[1]);}}return s})()""") or []


def all_count(cdp, sid):
    return cdp.evalp(sid, r"""(()=>{const m=(document.body.innerText||'').match(/Все\s+(\d+)/);return m?+m[1]:null})()""")


# ----------------------------------------------------------------- extension save
def upload_via_extension(cdp, list_tid, resume_url, vacancy, stage=DEFAULT_STAGE, dry=False):
    """Open the resume in a NEW tab, drive the HarmonyHunter popup to save the
    candidate onto `vacancy` at `stage`, then close the resume + popup + success
    tabs (leaving the list tab untouched). Returns a result dict."""
    res = {"vacancy": vacancy, "hash": (re.search(r"/resume/([0-9a-f]+)", resume_url) or [None, ""])[1]}
    before = {t["targetId"] for t in cdp.targets()}
    rtid = cdp.cmd("Target.createTarget", {"url": resume_url})["targetId"]
    rsid = cdp.attach(rtid)
    cdp.cmd("Page.enable", sid=rsid); cdp.cmd("Runtime.enable", sid=rsid)
    # let the resume fully load (hh generates the PDF lazily; rushing -> "Uploaded file not found")
    deadline = time.time() + 25
    while time.time() < deadline:
        if cdp.evalp(rsid, "document.readyState==='complete' && !!document.body"):
            break
        time.sleep(0.4)
    time.sleep(6)
    res["resume_name"] = cdp.evalp(rsid, "((document.querySelector('[data-qa=\"resume-personal-name\"]')||{}).innerText||'').trim()") or ""
    cdp.cmd("Target.activateTarget", {"targetId": rtid})

    pop = cdp.cmd("Target.createTarget", {"url": POPUP, "background": True})["targetId"]
    cdp.cmd("Target.activateTarget", {"targetId": rtid})  # popup scrapes the ACTIVE tab = resume
    psid = cdp.attach(pop)

    def ev(expr):
        return cdp.evalp(psid, expr)

    def open_cb(prefix):
        for _ in range(10):
            if ev("(()=>{const P=%s;const c=[...document.querySelectorAll('[role=combobox]')].find(x=>(x.innerText||'').trim().replace(/\\s+/g,' ').startsWith(P));if(c){c.scrollIntoView({block:'center'});c.click();return 1}return null})()" % js(prefix)):
                return True
            time.sleep(0.6)
        return False

    def click_opt(text):
        return ev("(()=>{const T=%s;const o=[...document.querySelectorAll('[role=option]')].find(e=>(e.innerText||'').trim().replace(/\\s+/g,' ')===T);if(!o)return 0;o.scrollIntoView({block:'center'});o.click();return 1})()" % js(text))

    def save_disabled():
        return ev("(()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim().replace(/\\s+/g,' ')==='ВЗЯТЬ НА ВАКАНСИЮ');return b?!!(b.disabled||b.getAttribute('aria-disabled')==='true'):true})()")

    try:
        loaded = False
        for _ in range(30):
            if ev("[...document.querySelectorAll('[role=combobox]')].some(c=>/ваканс/i.test(c.innerText||''))"):
                loaded = True
                break
            time.sleep(0.7)
        if not loaded:
            res["result"] = "failed: candidate not loaded in popup"
            return res
        open_cb("Выберите вакансию"); time.sleep(1.0)
        if not click_opt(vacancy):
            res["result"] = "failed: vacancy option not found"
            return res
        time.sleep(0.8)
        if save_disabled():
            open_cb("Выберите этап"); time.sleep(0.8); click_opt(stage); time.sleep(0.6)
        if save_disabled():
            res["result"] = "failed: save button stays disabled"
            return res
        if dry:
            res["result"] = "dry-run (vacancy+stage set, not saved)"
            return res
        ev("(()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim().replace(/\\s+/g,' ')==='ВЗЯТЬ НА ВАКАНСИЮ'&&!x.disabled);if(b)b.click();return 1})()")
        log = ""
        for _ in range(15):
            log = ev("(document.body.innerText||'').replace(/\\s+/g,' ').trim()") or ""
            if re.search(r"(not found|не найден|Error:|ошибк)", log, re.I):
                res["result"] = "error"; res["log_tail"] = log[-160:]
                return res
            time.sleep(0.8)
        res["result"] = "saved"; res["log_tail"] = log[-160:]
        return res
    finally:
        for t in cdp.targets():
            u = t.get("url", "")
            if t["targetId"] == pop or t["targetId"] == rtid or (t["targetId"] not in before and "harmonyats.org" in u):
                try:
                    cdp.cmd("Target.closeTarget", {"targetId": t["targetId"]})
                except Exception:
                    pass
        cdp.cmd("Target.activateTarget", {"targetId": list_tid})


# ----------------------------------------------------------- hh "Подумать" move
def _esc(cdp, sid):
    cdp.evalp(sid, "for(let i=0;i<2;i++)document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));1")


def _dialog_open(cdp, sid):
    return bool(cdp.evalp(sid, "/%s/.test(document.body.innerText||'')" % STATUS_DIALOG_RE))


def move_to_consider(cdp, sid, target_hash, send_message=True):
    """On the list tab, move the card for `target_hash` to status "Подумать":
    Пригласить -> Подумать -> [keep/uncheck message] -> Изменить статус.
    Verifies the card leaves the list. Returns 'moved' | 'failed: <why>'.

    Hardened against post-tab-churn click races: the list tab was just
    reactivated, so the first click can be swallowed by a focus event and the
    React dropdown/dialog can lag. Each step retries and is verified."""
    invite_finder = (r"""(()=>{const a=[...document.querySelectorAll('a[href*="/resume/"]')].find(a=>(a.getAttribute('href')||'').includes(%s));"""
                     r"""if(!a)return null;let c=a;for(let i=0;i<8&&c;i++){const b=[...c.querySelectorAll('button')].find(x=>/Пригласить/.test(x.innerText||''));if(b)return b;c=c.parentElement;}return null})()""") % js(target_hash)
    time.sleep(0.8)  # let the reactivated list tab settle

    # 1+2. open the "Пригласить" dropdown and reveal "Подумать" — retry (a stale
    #      menu or a swallowed first click is the usual cause of "not visible").
    consider = None
    for _ in range(4):
        _esc(cdp, sid); time.sleep(0.3)
        invite = cdp.center(sid, invite_finder, timeout=5)
        if not invite:
            return "failed: invite button not found"
        cdp.click_xy(sid, invite["x"], invite["y"])
        consider = cdp.center(sid, "document.querySelector(%s)" % js(CONSIDER_ITEM), timeout=3.5)
        if consider:
            break
    if not consider:
        return "failed: 'Подумать' menu item not visible"
    cdp.click_xy(sid, consider["x"], consider["y"])

    # 3. wait for the "Изменить статус резюме" dialog
    ok = False
    for _ in range(16):
        if _dialog_open(cdp, sid):
            ok = True
            break
        time.sleep(0.4)
    if not ok:
        return "failed: status dialog did not open"

    # optionally uncheck "Отправить сообщение"
    if not send_message:
        cb = cdp.center(sid, "[...document.querySelectorAll('input[type=checkbox]')].find(x=>x.checked && x.getBoundingClientRect().width>0)", timeout=2)
        if cb:
            cdp.click_xy(sid, cb["x"], cb["y"]); time.sleep(0.4)

    # 4. commit — retry until the dialog actually closes (a missed click leaves
    #    it open and nothing commits, which looked like "card still in list").
    committed = False
    for _ in range(3):
        commit = cdp.center(sid, "[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim().replace(/\\s+/g,' ')===%s && x.getBoundingClientRect().width>0)" % js(COMMIT_BUTTON), timeout=4)
        if not commit:
            break
        cdp.click_xy(sid, commit["x"], commit["y"])
        for _ in range(12):
            time.sleep(0.4)
            if not _dialog_open(cdp, sid):
                committed = True
                break
        if committed:
            break
    if not committed:
        return "failed: commit did not close the status dialog"

    # 5. verify the card left the list (in-place removal; give it room)
    for _ in range(24):
        time.sleep(0.5)
        if target_hash not in list_hashes(cdp, sid):
            return "moved"
    return "failed: card still in list after commit"


# ----------------------------------------------------------------------- main loop
def process_vacancy(cdp, list_tid, list_sid, vac, stage, limit, send_message,
                    do_consider, dry, scope):
    """Live top-of-list processing across all hh postings for the CRM vacancy."""
    results = []
    skip = set()      # hashes we gave up on (upload failed) — leave them in "Все"
    done = set()      # hashes uploaded (and moved, if do_consider)
    for vid in vac["hh_ids"]:
        cdp.navigate(list_sid, list_tid, RESP_URL.format(vid=vid), settle=6)
        print(f"\n=== posting {vid} | Все={all_count(cdp, list_sid)} ===", flush=True)
        empty_reloads = 0
        while True:
            if limit and len(done) >= limit:
                print(f"  reached --limit {limit}", flush=True)
                return results
            todo = [h for h in list_hashes(cdp, list_sid) if h not in skip and h not in done]
            if not todo:
                # backfill from the larger pool (this is how we page past ~52/posting)
                ac = all_count(cdp, list_sid)
                if ac is not None and ac <= len(skip):
                    print(f"  posting {vid} drained (Все={ac}, all skipped/done)", flush=True)
                    break
                empty_reloads += 1
                if empty_reloads > 3:
                    print(f"  posting {vid}: no new cards after {empty_reloads} reloads, moving on", flush=True)
                    break
                cdp.navigate(list_sid, list_tid, RESP_URL.format(vid=vid), settle=5)
                continue
            empty_reloads = 0
            h = todo[0]
            rec = {"hash": h, "vid": vid}
            up = upload_via_extension(cdp, list_tid, RESUME_URL.format(h=h, vid=vid),
                                      vac["crm"], stage=stage, dry=dry)
            rec.update(up)
            if up.get("result") in ("saved", "dry-run (vacancy+stage set, not saved)"):
                done.add(h)
                if do_consider and not dry:
                    mv = move_to_consider(cdp, list_sid, h, send_message=send_message)
                    rec["consider"] = mv
                    if mv != "moved":
                        # move failed but upload ok — don't loop on it
                        skip.add(h)
                else:
                    rec["consider"] = "skipped (--no-consider/dry)"
                    skip.add(h)  # so the loop advances (we didn't remove it from Все)
            else:
                skip.add(h)
                rec["consider"] = "not attempted (upload failed)"
            results.append(rec)
            print(f"  [{len(done)} done] {rec.get('resume_name') or h[:12]}: upload={up.get('result')} | consider={rec.get('consider')}", flush=True)
            time.sleep(1.0)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9347)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--vacancy", required=True, help="CRM vacancy name (or unique substring) from the CSV")
    ap.add_argument("--scope", choices=["all", "suitable"], default="all")
    ap.add_argument("--stage", default=DEFAULT_STAGE)
    ap.add_argument("--limit", type=int, default=0, help="cap candidates processed (0 = no cap)")
    ap.add_argument("--no-consider", action="store_true", help="upload only; do NOT move to Подумать")
    ap.add_argument("--no-message", action="store_true", help="when moving to Подумать, uncheck 'Отправить сообщение'")
    ap.add_argument("--dry", action="store_true", help="extension selects vacancy+stage but never saves; no Подумать move")
    ap.add_argument("--preflight-only", action="store_true", help="run the preflight checks and exit")
    ap.add_argument("--out", default="data/uploads")
    args = ap.parse_args()

    positions = parse_csv(args.csv)
    vac = resolve_vacancy(positions, args.vacancy)
    slug = re.sub(r"[^\w]+", "_", vac["crm"]).strip("_")[:60]
    outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)

    cdp, list_tid, list_sid = preflight(args.port, vac["crm"])
    if args.preflight_only:
        return 0
    print(f"Vacancy: {vac['crm']}  |  {len(vac['hh_ids'])} hh postings  |  consider={not args.no_consider}  message={not args.no_message}  dry={args.dry}", flush=True)

    results = process_vacancy(cdp, list_tid, list_sid, vac, args.stage, args.limit,
                              send_message=not args.no_message, do_consider=not args.no_consider,
                              dry=args.dry, scope=args.scope)

    # safety sweep: close any leftover popup / success tabs
    for t in cdp.targets():
        u = t.get("url", "")
        if POPUP in u or "harmonyats.org/candidates?openId" in u:
            try:
                cdp.cmd("Target.closeTarget", {"targetId": t["targetId"]})
            except Exception:
                pass

    log_tsv = outdir / f"{slug}.upload_log.tsv"
    cols = ["hash", "vid", "resume_name", "vacancy", "result", "consider", "log_tail"]
    with log_tsv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t", extrasaction="ignore")
        w.writeheader(); w.writerows(results)
    saved = sum(1 for r in results if r.get("result") == "saved")
    moved = sum(1 for r in results if r.get("consider") == "moved")
    failed = [r for r in results if str(r.get("result", "")).startswith(("failed", "error"))]
    print(f"\nDONE: {saved} uploaded, {moved} moved to Подумать, {len(failed)} failed, {len(results)} total. Log: {log_tsv}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

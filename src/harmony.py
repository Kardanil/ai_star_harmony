#!/usr/bin/env python3
"""Harmony ATS driver — the explicit model the task scripts are built on.

Three ideas, made first-class:

  IDENTITY  The stable id of a candidate is `openId` (a UUID). It lives in the
            URL as `?openId=<uuid>` and appears only AFTER a card is opened.
            Display names are first+last only (no patronymic) and not unique —
            never key decisions on them. `index.tsv`'s detail_url carries the
            openId for every downloaded candidate.

  SCREENS   S1 vacancies list · S2 funnel/stage view (cards + live per-stage
            counts) · S3 candidate detail (CV, status <h2>, "Сменить этап") ·
            S4 stage popover · S5 reason dropdown (reject only) · S6 Сохранить.

  ACTION    move(target[, reason]) = S3→S4→[S5]→S6, then VERIFY the source
            stage's count dropped by one. No count change ⇒ the move didn't
            take. Verification is also the pacing signal — we never touch the
            next card until the count actually moved.

No fixed `time.sleep()` in the action path: every wait is `wait_dom(cond_js)`,
a MutationObserver + `awaitPromise` that resolves the instant a DOM predicate
holds (or fails fast at timeout). SPA in-pane updates don't change
`document.readyState`, so DOM-content predicates — not readyState — are what we
key off for card clicks / popovers / stage moves.
"""
import json
import re
import time
import unicodedata

from cdp_tool import CDP, eval_js, page_ws

BASE = "https://app.harmonyats.org"
INCOMING_STAGE_ID = "019e15e2-e3e0-7fa7-9ed1-87a018083e97"  # global "Входящий поток" stage
DEFAULT_REJECT_REASON = "Мы отказали: AI отсек по резюме"
STAGE_BUTTON = "Сменить этап"
SAVE_BUTTON = "Сохранить"

STAGES = [
    "Входящий поток", "Первичный отбор AI", "Повторный отбор СТО",
    "Приглашен на интервью с СТО", "Интервью проведено", "Отправлен заказчику",
    "Приглашен на интервью с заказчиком", "Интервью с заказчиком назначено",
    "Интервью с заказчиком проведено", "Оффер сделан", "Оформление",
    "Вышел на работу", "Бэклог", "Бэклог резюме", "Черный список", "Rejection",
]

# A candidate card in the S2 list: a <button type=button> in the left column
# (80<=x<=480), wide, with the tailwind `text-left` class.
CARD_FILTER = (
    "el => { const r=el.getBoundingClientRect(); const cls=String(el.className||''); "
    "const t=(el.innerText||'').trim(); return t && r.x>=80 && r.x<=480 && r.width>200 "
    "&& cls.includes('text-left'); }"
)

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def js(value):
    return json.dumps(value, ensure_ascii=False)


def normalize_name(value):
    n = unicodedata.normalize("NFKC", value or "").replace("ё", "е").replace("Ё", "Е")
    return " ".join(n.lower().split())


def names_match(expected, actual):
    """True if `actual` (e.g. a card's first+last) is the same person as
    `expected` (e.g. an index full name with patronymic). Subset match: every
    token of the shorter name appears in the longer."""
    e, a = normalize_name(expected), normalize_name(actual)
    if not e or not a:
        return False
    if e == a:
        return True
    et, at = set(e.split()), set(a.split())
    short, long = (at, et) if len(at) <= len(et) else (et, at)
    return len(short) >= 2 and short.issubset(long)


def open_id_from_url(url):
    m = re.search(r"openId=([0-9a-f-]+)", url or "")
    return m.group(1) if m else ""


def _js_re_escape(s):
    """Escape JS-RegExp metacharacters but leave spaces literal (re.escape would
    emit `\\ ` for a space, which is only leniently accepted by `new RegExp`)."""
    return re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", s)


class Harmony:
    def __init__(self, port=9347):
        self.cdp = CDP(page_ws(port))
        self.cdp.call("Page.enable")
        self.cdp.call("Runtime.enable")

    def close(self):
        try:
            self.cdp.close()
        except Exception:
            pass

    # ---- low-level eval ----
    def eval(self, expr, await_promise=False):
        return eval_js(self.cdp, expr, await_promise=await_promise)

    def truthy(self, cond_js):
        """Evaluate a JS boolean expression, treating an in-page exception as
        False (eval_js otherwise returns the error's description string, which is
        truthy — so a throwing predicate would read as success)."""
        return bool(self.eval("(() => { try { return !!(" + cond_js + "); } catch(e) { return false; } })()"))

    def url(self):
        return self.eval("location.href") or ""

    def dismiss(self):
        """Close any open popover/dialog (Escape twice) so the next action starts
        from a clean S2/S3 state. Safe to call when nothing is open."""
        self.eval("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}))")
        self.eval("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}))")

    # ---- the wait primitive: no fixed sleep, resolves on a DOM predicate ----
    def wait_dom(self, cond_js, timeout=10.0):
        """Block until `cond_js` (a JS boolean expression) is truthy, via a
        MutationObserver inside the page. Returns True if satisfied, False on
        timeout. Resolves within ~1 frame of the condition becoming true."""
        expr = (
            "new Promise((resolve)=>{"
            "const ok=()=>{try{return !!(" + cond_js + ");}catch(e){return false;}};"
            "if(ok())return resolve(true);"
            "const obs=new MutationObserver(()=>{if(ok()){obs.disconnect();clearTimeout(t);resolve(true);}});"
            "obs.observe(document.documentElement,{childList:true,subtree:true,attributes:true,characterData:true});"
            "const t=setTimeout(()=>{obs.disconnect();resolve(false);}," + str(int(timeout * 1000)) + ");"
            "})"
        )
        # Call Runtime.evaluate directly so the websocket recv timeout outlives
        # the in-page promise timeout (eval_js would use CDP's default 10s).
        result = self.cdp.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True, "userGesture": True},
            timeout=timeout + 5,
        )
        return bool(result.get("result", {}).get("value"))

    # ---- S-navigation ----
    def goto(self, url, timeout=20):
        self.cdp.call("Page.navigate", {"url": url})
        return self.wait_dom("document.readyState==='complete' && !!document.body", timeout)

    def goto_funnel(self, vacancy_id, stage_id=INCOMING_STAGE_ID, timeout=20):
        self.goto(f"{BASE}/vacancies/{vacancy_id}?stageId={stage_id}", timeout)
        # sidebar with stage names rendered
        return self.wait_dom("document.body.innerText.includes('Входящий поток')", timeout)

    # ---- S1: vacancies list ----
    def goto_vacancies(self, timeout=20):
        self.goto(f"{BASE}/search?m=vacancies", timeout)
        return self.wait_dom("document.body.innerText.includes('Все вакансии') || /\\/ /.test(document.body.innerText)", timeout)

    # ---- S2: funnel reads ----
    def stage_counts(self):
        """The funnel sidebar's live per-stage counts -> {stage: int}. A stage
        with no number rendered next to it is 0."""
        txt = self.eval("(document.body.innerText||'')") or ""
        out = {}
        for s in STAGES + ["Все"]:
            m = re.search(re.escape(s) + r"\s*\n\s*(\d+)(?:\s|$)", txt)
            out[s] = int(m.group(1)) if m else 0
        return out

    def stage_count(self, stage):
        return self.stage_counts().get(stage, 0)

    def list_cards(self):
        """Candidate cards in the current stage list -> [{index, name, lines}]."""
        rows = self.eval(
            "(() => Array.from(document.querySelectorAll('button[type=\"button\"]')).filter(" + CARD_FILTER + ")"
            ".map((el,i)=>({i, lines:(el.innerText||'').split(/\\n+/).map(s=>s.trim()).filter(Boolean)})))()"
        ) or []
        cards = []
        for r in rows:
            cards.append({"index": r["i"], "name": _card_name(r["lines"]), "lines": r["lines"]})
        return cards

    def open_card(self, index, expected_name=None, timeout=12):
        """Click the card at `index`, wait for the EXPECTED candidate's detail
        (S3) to render, and return that candidate's openId. Returns '' if the
        click was a no-op or the right candidate never rendered — so a stale
        openId is never returned and never applied to the wrong person."""
        clicked = self.truthy(
            "(() => { const els=Array.from(document.querySelectorAll('button[type=\"button\"]')).filter(" + CARD_FILTER + ");"
            "const el=els[" + str(index) + "]; if(!el) return false; el.scrollIntoView({block:'center'}); el.click(); return true; })()"
        )
        if not clicked:
            return ""
        has_detail = "!!document.querySelector('h1') && Array.from(document.querySelectorAll('button')).some(b=>(b.innerText||'').trim()===" + js(STAGE_BUTTON) + ")"
        if expected_name:
            # Wait for the h1 to be the expected person — whole-token subset match
            # (NOT substring includes(), which can confirm the wrong name), so we
            # don't read a stale/previous detail that happens to satisfy has_detail.
            tokens = normalize_name(expected_name).split()
            cond = ("(()=>{const h=document.querySelector('h1'); if(!h) return false;"
                    "const hw=(h.innerText||'').toLowerCase().normalize('NFKC').replace(/ё/g,'е').split(/\\s+/).filter(Boolean);"
                    "return " + js(tokens) + ".every(t=>hw.includes(t));})() && " + has_detail)
        else:
            cond = has_detail
        if not self.wait_dom(cond, timeout):
            return ""
        return open_id_from_url(self.url())

    # ---- S3: detail reads ----
    def detail_name(self):
        return self.eval("(()=>{const h=document.querySelector('h1');return h?h.innerText.trim().replace(/\\s+/g,' '):null;})()")

    def detail_status(self):
        return self.eval(
            "(() => { const known=" + js(STAGES) + ";"
            "const els=Array.from(document.querySelectorAll('h2')).map(e=>({t:(e.innerText||'').trim().replace(/\\s+/g,' '),r:e.getBoundingClientRect()}))"
            ".filter(o=>known.includes(o.t)&&o.r.width>0&&o.r.height>0); return els.length?els[0].t:null; })()"
        )

    # ---- the action: move current candidate to `target` ----
    def move(self, vacancy, target, source_stage="Входящий поток", reason=None, timeout=12):
        """Move the candidate whose detail (S3) is open to `target`. Verifies the
        move persisted (source-stage count strictly dropped, or the detail now
        shows the target). Returns 'moved' | 'failed: <why>'. On any failure the
        popover/dialog is dismissed so the next card starts from a clean state."""
        reason = reason or DEFAULT_REJECT_REASON
        before = self.stage_count(source_stage)
        result = self._do_move(vacancy, target, source_stage, before, reason, timeout)
        if result != "moved":
            self.dismiss()
        return result

    def _do_move(self, vacancy, target, source_stage, before, reason, timeout):
        # S3 -> open the "Сменить этап" popover (S4)
        if not self._click_button(STAGE_BUTTON):
            return "failed: 'Сменить этап' not found"
        if not self.wait_dom(self._vacancy_present_js(vacancy), timeout):
            return "failed: popover did not open"

        # S4 -> expand the vacancy, scroll, wait for the target stage row
        if not self._ensure_stage_visible(vacancy, target, timeout):
            return f"failed: stage '{target}' not visible in panel"
        if not self._click_stage_panel(target):
            return f"failed: stage '{target}' click"

        # S5 -> Rejection requires a reason
        if target == "Rejection":
            if not self.wait_dom(self._reason_or_save_ready_js(reason), timeout):
                return "failed: reason UI not ready"
            if not self._reason_selected(reason):
                self._open_reason_dropdown()
                self.wait_dom(self._reason_option_present_js(reason), timeout)
                self._select_reason_option(reason)
                if not self.wait_dom("(" + self._reason_selected_js(reason) + ")", timeout):
                    return f"failed: reason '{reason}' not selected"

        # S6 -> save, then VERIFY persistence
        if not self.wait_dom(self._save_enabled_js(), timeout):
            return "failed: save button not enabled"
        if not self._click_button(SAVE_BUTTON):
            return "failed: save click"
        return "moved" if self._verify_moved(source_stage, before, target, timeout) else \
            "failed: not verified (count did not drop)"

    def _verify_moved(self, source_stage, before, target, timeout):
        """True iff the move persisted. Primary signal: the source-stage badge
        renders a count strictly below `before` — the badge MUST be present
        (m!==null), so a transient re-render where it is momentarily absent is
        NOT mistaken for success. Fallback (source count unreadable, e.g.
        before==0): the open candidate's detail now shows the target stage."""
        if before >= 1:
            pat = _js_re_escape(source_stage) + r"\s*\n\s*(\d+)"
            if self.wait_dom(
                "(() => { const m=(document.body.innerText||'').match(new RegExp(" + js(pat) + "));"
                " return m!==null && parseInt(m[1],10) <= " + str(before - 1) + "; })()",
                timeout,
            ):
                return True
        return self.wait_dom(
            "(() => { const known=" + js(STAGES) + ";"
            " return Array.from(document.querySelectorAll('h2')).some(e=>{const t=(e.innerText||'').trim().replace(/\\s+/g,' ');"
            " return t===" + js(target) + " && known.includes(t);}); })()",
            min(timeout, 5),
        )

    # ---------- internal JS builders / clickers ----------
    def _click_button(self, text):
        return self.truthy(
            "(() => { const t=" + js(text) + "; const b=Array.from(document.querySelectorAll('button'))"
            ".find(e=>(e.innerText||e.textContent||'').trim().replace(/\\s+/g,' ')===t && !e.disabled);"
            "if(!b)return false; b.scrollIntoView({block:'center',inline:'center'}); b.click(); return true; })()"
        )

    def _vacancy_present_js(self, vacancy):
        return ("Array.from(document.querySelectorAll('button')).some(e=>{const t=(e.innerText||'').trim().replace(/\\s+/g,' ');"
                "return " + js(vacancy) + "? t.startsWith(" + js(vacancy) + ") : (t.includes(' / ')&&t.length<70);})")

    def _expand_vacancy(self, vacancy):
        # Prefer an EXACT name match; only fall back to startsWith when there is
        # no exact button (picking the shortest prefix match could expand a
        # different vacancy that shares a name prefix).
        return self.truthy(
            "(() => { const v=" + js(vacancy) + ";"
            "const b=Array.from(document.querySelectorAll('button'));"
            "const norm=e=>(e.innerText||'').trim().replace(/\\s+/g,' ');"
            "let cand=b.filter(e=>norm(e)===v);"
            "if(!cand.length) cand=b.filter(e=>{const t=norm(e); return v? t.startsWith(v):(t.includes(' / ')&&t.length<70);});"
            "cand.sort((x,y)=>(x.innerText||'').length-(y.innerText||'').length); if(!cand.length)return false;"
            "cand[0].scrollIntoView({block:'center'}); cand[0].click(); return true; })()"
        )

    def _any_stage_in_panel_js(self):
        # A sentinel stage that is present whenever a vacancy's stage list is
        # expanded — used to tell "accordion open" from "collapsed".
        return self._stage_in_panel_js("Первичный отбор AI")

    def _stage_in_panel_js(self, target):
        return ("Array.from(document.querySelectorAll('div,button,li,span')).some(e=>{const t=(e.innerText||'').trim().replace(/\\s+/g,' ');"
                "const r=e.getBoundingClientRect(); return t===" + js(target) + " && r.x<520 && r.width>0 && r.height>0;})")

    def _scroll_stage_list(self):
        self.eval(
            "(() => { const a=Array.from(document.querySelectorAll('div,span,li,button')).find(e=>(e.innerText||'').trim()==='Первичный отбор AI' && e.getBoundingClientRect().x<520 && e.getBoundingClientRect().width>0);"
            "if(!a)return false; let p=a.parentElement; while(p){const o=getComputedStyle(p).overflowY; if((o==='auto'||o==='scroll')&&p.scrollHeight>p.clientHeight+20){p.scrollTop=p.scrollHeight;return true;} p=p.parentElement;} return false; })()"
        )

    def _ensure_stage_visible(self, vacancy, target, timeout):
        # Already visible (accordion open and scrolled into view)?
        if self.truthy(self._stage_in_panel_js(target)):
            return True
        for _ in range(2):
            # Only (re)expand when collapsed — clicking an already-open vacancy
            # toggles its stage list shut.
            if not self.truthy(self._any_stage_in_panel_js()):
                self._expand_vacancy(vacancy)
            deadline = time.time() + timeout
            while time.time() < deadline:
                self._scroll_stage_list()
                if self.truthy(self._stage_in_panel_js(target)):
                    return True
                self.wait_dom(self._stage_in_panel_js(target), 0.8)
            # full miss: collapse so the next pass re-expands cleanly
            if not self.truthy(self._stage_in_panel_js(target)):
                self._expand_vacancy(vacancy)
        return self.truthy(self._stage_in_panel_js(target))

    def _click_stage_panel(self, target):
        return self.truthy(
            "(() => { const els=Array.from(document.querySelectorAll('div,button,li,span'))"
            ".map(el=>{const r=el.getBoundingClientRect();return {el,t:(el.innerText||'').trim().replace(/\\s+/g,' '),x:r.x,w:r.width,h:r.height,kids:el.childElementCount};})"
            ".filter(x=>x.t===" + js(target) + " && x.x<520 && x.w>0 && x.h>0); els.sort((a,b)=>a.kids-b.kids);"
            "if(!els.length)return false; els[0].el.scrollIntoView({block:'center'}); els[0].el.click(); return true; })()"
        )

    def _reason_option_present_js(self, reason):
        return ("Array.from(document.querySelectorAll('[role=option],li,div,button,span')).some(e=>(e.innerText||'').trim().replace(/\\s+/g,' ')===" + js(reason) + " && e.getBoundingClientRect().width>0)")

    def _save_enabled_js(self):
        return ("Array.from(document.querySelectorAll('button')).some(b=>(b.innerText||'').trim().replace(/\\s+/g,' ')===" + js(SAVE_BUTTON) + " && !b.disabled)")

    def _reason_or_save_ready_js(self, reason):
        return "(" + self._reason_selected_js(reason) + ") || (" + self._save_enabled_js() + ") || Array.from(document.querySelectorAll('button,[role=combobox]')).some(e=>/Выберите причин/i.test(e.innerText||e.textContent||''))"

    def _open_reason_dropdown(self):
        return self.truthy(
            "(() => { const c=Array.from(document.querySelectorAll('button,[role=combobox],[role=button]'))"
            ".map(el=>({el,t:(el.innerText||el.textContent||el.placeholder||'').trim().replace(/\\s+/g,' '),r:el.getBoundingClientRect()}))"
            ".filter(o=>/Выберите причин/i.test(o.t)&&o.r.width>0&&o.r.height>0); c.sort((a,b)=>a.r.width-b.r.width);"
            "if(!c.length)return false; c[0].el.scrollIntoView({block:'center'}); c[0].el.click(); return true; })()"
        )

    def _select_reason_option(self, reason):
        return self.truthy(
            "(() => { const o=Array.from(document.querySelectorAll('[role=option],li,div,button,span'))"
            ".map(el=>({el,t:(el.innerText||'').trim().replace(/\\s+/g,' '),r:el.getBoundingClientRect()}))"
            ".filter(x=>x.t===" + js(reason) + " && x.r.width>0 && x.r.height>0); o.sort((a,b)=>a.r.width-b.r.width);"
            "if(!o.length)return false; o[0].el.click(); return true; })()"
        )

    def _reason_selected_js(self, reason):
        return "Array.from(document.querySelectorAll('[role=dialog] *')).some(el=>(el.innerText||'').trim().replace(/\\s+/g,' ').includes(" + js(reason) + "))"

    def _reason_selected(self, reason):
        return self.truthy(self._reason_selected_js(reason))


def _card_name(lines):
    for ln in lines:
        if len(ln) <= 4 and re.fullmatch(r"[A-ZА-ЯЁ]{1,4}", ln):
            continue  # avatar initials
        return ln
    return lines[0] if lines else ""

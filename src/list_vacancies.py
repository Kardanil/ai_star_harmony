#!/usr/bin/env python3
"""Part 1 of the processing pipeline: list Harmony vacancies with per-stage candidate
counts (the "how many candidates are where" overview).

Drives the open Chrome (remote-debug --port, logged into app.harmonyats.org): goes to
the "Все вакансии" view, clicks each vacancy, and reads its funnel stage counts +
vacancy id (needed to build the Входящий-поток download URL).

  python3 src/list_vacancies.py [--port 9347] [--out data/vacancies.tsv]
"""
import argparse
import csv
import json
import re
import time
from pathlib import Path

from cdp_tool import CDP, eval_js, page_ws

LIST_URL = "https://app.harmonyats.org/search?m=vacancies"
INCOMING_STAGE_ID = "019e15e2-e3e0-7fa7-9ed1-87a018083e97"  # global "Входящий поток" stage
STAGES = [
    "Всего", "Входящий поток", "Первичный отбор AI", "Повторный отбор СТО",
    "Приглашен на интервью с СТО", "Интервью проведено", "Отправлен заказчику",
    "Приглашен на интервью с заказчиком", "Интервью с заказчиком назначено",
    "Интервью с заказчиком проведено", "Оффер сделан", "Оформление", "Вышел на работу",
    "Бэклог", "Rejection",
]


def js(v):
    return json.dumps(v, ensure_ascii=False)


def vacancy_names(cdp):
    """Distinct vacancy names in the left list of the 'Все вакансии' view."""
    return eval_js(cdp, """
(() => {
  const seen=new Set(); const out=[];
  document.querySelectorAll('button,a,div').forEach(e => {
    const t=(e.innerText||e.textContent||'').trim().replace(/\\s+/g,' ').replace(/\\s*—\\s*$/,'');
    const r=e.getBoundingClientRect();
    if ((/ \\/ /.test(t) || t==='Backlog') && t.length<70 && !/Нанято|Открыт|Приостанов|Закрыт/.test(t) && r.x<470 && r.width>0 && r.height>0 && !seen.has(t)) {
      seen.add(t); out.push(t);
    }
  });
  return out;
})()
""") or []


def click_vacancy(cdp, name):
    return bool(eval_js(cdp, "(() => {const n=%s;const els=[...document.querySelectorAll('button,a,div')].filter(e=>{const t=(e.innerText||e.textContent||'').trim().replace(/\\s+/g,' ').replace(/\\s*—\\s*$/,'');const r=e.getBoundingClientRect();return t===n && r.x<470 && r.width>0 && r.height>0;});els.sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length);if(els[0]){els[0].scrollIntoView({block:'center'});els[0].click();return true}return false})()" % js(name)))


def current_vacancy_id(cdp):
    url = eval_js(cdp, "location.href") or ""
    m = re.search(r"openId=([0-9a-f-]+)", url)
    return m.group(1) if m else ""


def read_stage_counts(cdp):
    """Parse the 'Сколько кандидатов было на каждом этапе' panel into {stage: count}."""
    seg = eval_js(cdp, "(() => {const t=document.body.innerText||'';const i=t.indexOf('каждом этапе');return i>=0?t.slice(i,i+1000):'';})()") or ""
    out = {}
    for s in STAGES:
        m = re.search(re.escape(s) + r"\s+(\d+)", seg)
        if m:
            out[s] = int(m.group(1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9347)
    ap.add_argument("--out", default="data/vacancies.tsv")
    ap.add_argument("--include-backlog", action="store_true")
    args = ap.parse_args()

    cdp = CDP(page_ws(args.port))
    cdp.call("Page.enable"); cdp.call("Runtime.enable")
    cdp.call("Page.navigate", {"url": LIST_URL}); time.sleep(4)

    names = vacancy_names(cdp)
    if not args.include_backlog:
        names = [n for n in names if n != "Backlog"]
    print(f"Found {len(names)} vacancies", flush=True)

    rows = []
    for name in names:
        if not click_vacancy(cdp, name):
            print(f"  ! could not click: {name}", flush=True)
            continue
        time.sleep(2.5)
        vid = current_vacancy_id(cdp)
        counts = read_stage_counts(cdp)
        incoming = counts.get("Входящий поток", "")
        url = f"https://app.harmonyats.org/vacancies/{vid}?stageId={INCOMING_STAGE_ID}" if vid else ""
        rows.append({"vacancy": name, "id": vid, "incoming": incoming,
                     "counts": counts, "incoming_url": url})
        print(f"  {name}  | id={vid} | Входящий поток={incoming} | total={counts.get('Всего','?')}", flush=True)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    cols = ["vacancy", "id", "Всего", "Входящий поток", "Первичный отбор AI", "Rejection", "incoming_url"]
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t"); w.writerow(cols)
        for r in rows:
            w.writerow([r["vacancy"], r["id"], r["counts"].get("Всего", ""),
                        r["counts"].get("Входящий поток", ""), r["counts"].get("Первичный отбор AI", ""),
                        r["counts"].get("Rejection", ""), r["incoming_url"]])
    cdp.close()
    print(f"\nSaved {len(rows)} vacancies -> {args.out}", flush=True)


if __name__ == "__main__":
    main()

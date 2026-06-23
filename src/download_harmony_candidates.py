#!/usr/bin/env python3
import argparse
import json
import re
import time
from pathlib import Path

from cdp_tool import CDP, eval_js, page_ws


INCOMING_URL = "https://app.harmonyats.org/candidates?stageId=019e15e2-e3e0-7fa7-9ed1-87a018083e97"


def safe_name(value):
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^\w.\-а-яА-ЯёЁ]+", "_", value)
    value = value.strip("._-")
    return value[:90] or "candidate"


def field(text, label):
    match = re.search(rf"{re.escape(label)}:\n([^\n]+)", text)
    return match.group(1).strip() if match else ""


def parse_name(text):
    match = re.search(r"(?:^|\n)Отправить\n(.+?)\nЖелаемая позиция:", text, re.S)
    if not match:
        match = re.search(r"(?:^|\n)Отправить\n(.+?)\n(?:Желаемая должность|Локации:|Email:|Метки:)", text, re.S)
    if not match:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[0] if lines else ""
    lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
    useful = []
    for line in lines:
        if len(line) <= 4 and re.fullmatch(r"[A-ZА-ЯЁA-Z]{1,4}", line):
            continue
        useful.append(line)
    return useful[0] if useful else ""


def parse_status(text):
    match = re.search(r"(?:^|\n)Статус\n([^\n]+)", text)
    return match.group(1).strip() if match else ""


def parse_vacancy(text, links):
    marker = re.search(r"(?:^|\n)Метки:\nДобавить\n(.+?)\nСтатус", text, re.S)
    if not marker:
        marker = re.search(r"(?:^|\n)Метки:\n(.+?)\nСтатус", text, re.S)
    if marker:
        lines = [line.strip() for line in marker.group(1).splitlines() if line.strip() and line.strip() != "Добавить"]
        if lines:
            return lines[-1]
    skipped = {
        "Взять на вакансию",
        "Редактировать",
        "Удалить",
        "Отправить",
        "Добавить",
        "Предыдущий слайд",
        "Следующий слайд",
        "hh.ru",
        "Добавить резюме",
        "Дубликаты",
        "Распаршенное",
        "Описание",
        "URL источника",
        "Распечатать",
        "Оригинал",
    }
    for link in links:
        label = link.get("text", "").strip()
        if label and label not in skipped and not label.startswith("+") and "@" not in label:
            return label
    return ""


def candidate_buttons_js():
    return """
(() => Array.from(document.querySelectorAll('button[type="button"]'))
  .map((el, index) => {
    const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
    const lines = (el.innerText || el.textContent || '').split(/\\n+/).map(x => x.trim()).filter(Boolean);
    const r = el.getBoundingClientRect();
    const cls = String(el.className || '');
    return {index, text, lines, x: r.x, y: r.y, w: r.width, h: r.height, cls};
  })
  .filter(x => x.text && x.x >= 90 && x.x <= 450 && x.w > 200 && x.cls.includes('text-left'))
)()
"""


def page_range_js():
    return """
(() => {
  const nodes = Array.from(document.querySelectorAll('div'));
  for (const el of nodes) {
    const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
    const r = el.getBoundingClientRect();
    if (/^\\d+\\s*-\\s*\\d+\\s+из\\s+\\d+\\s+\\d+\\s*\\/\\s*\\d+$/.test(text) && r.x >= 100 && r.x <= 450 && r.width > 0 && text.length < 80) {
      const m = text.match(/(\\d+)\\s*-\\s*(\\d+)\\s+из\\s+(\\d+)/);
      const p = text.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
      return {text, start: Number(m[1]), end: Number(m[2]), total: Number(m[3]), page: p ? Number(p[1]) : null, pages: p ? Number(p[2]) : null};
    }
  }
  return null;
})()
"""


def click_candidate_js(idx):
    return f"""
(() => {{
  const els = Array.from(document.querySelectorAll('button[type="button"]'))
    .filter((el) => {{
      const text = (el.innerText || el.textContent || '').trim();
      const r = el.getBoundingClientRect();
      const cls = String(el.className || '');
      return text && r.x >= 90 && r.x <= 450 && r.width > 200 && cls.includes('text-left');
    }});
  const el = els[{idx}];
  if (!el) return {{clicked: false, count: els.length}};
  const lines = (el.innerText || el.textContent || '').split(/\\n+/).map(x => x.trim()).filter(Boolean);
  el.scrollIntoView({{block: 'center', inline: 'center'}});
  el.click();
  return {{clicked: true, count: els.length, text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' '), lines}};
}})()
"""


def candidate_center_js(idx):
    return f"""
(() => {{
  const els = Array.from(document.querySelectorAll('button[type="button"]'))
    .filter((el) => {{
      const text = (el.innerText || el.textContent || '').trim();
      const r = el.getBoundingClientRect();
      const cls = String(el.className || '');
      return text && r.x >= 90 && r.x <= 450 && r.width > 200 && cls.includes('text-left');
    }});
  const el = els[{idx}];
  if (!el) return {{found: false, count: els.length}};
  el.scrollIntoView({{block: 'center', inline: 'center'}});
  const r = el.getBoundingClientRect();
  const lines = (el.innerText || el.textContent || '').split(/\\n+/).map(x => x.trim()).filter(Boolean);
  return {{found: true, count: els.length, text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' '), lines, x: r.x + r.width / 2, y: r.y + r.height / 2}};
}})()
"""


def click_candidate_by_name_js(expected):
    expected_json = json.dumps(expected, ensure_ascii=False)
    return f"""
(() => {{
  const expected = {expected_json}.toLowerCase();
  const els = Array.from(document.querySelectorAll('button[type="button"]'))
    .filter((el) => {{
      const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
      const r = el.getBoundingClientRect();
      const cls = String(el.className || '');
      return text && r.x >= 90 && r.x <= 450 && r.width > 200 && cls.includes('text-left');
    }});
  const el = els.find((x) => (x.innerText || x.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase().includes(expected));
  if (!el) return {{clicked: false, expected, count: els.length, texts: els.map(x => (x.innerText || x.textContent || '').trim().replace(/\\s+/g, ' ')).slice(0, 30)}};
  el.scrollIntoView({{block: 'center', inline: 'center'}});
  el.click();
  return {{clicked: true, expected, text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' '), count: els.length}};
}})()
"""


def click_parsed_tab_js():
    return click_tab_js("Распаршенное")


def click_tab_js(label):
    label_json = json.dumps(label, ensure_ascii=False)
    return f"""
(() => {{
  const label = {label_json};
  const tabs = Array.from(document.querySelectorAll('button,[role="tab"]'));
  const tab = tabs.find((el) => (el.innerText || el.textContent || '').trim().includes(label));
  if (!tab) return false;
  tab.click();
  return true;
}})()
"""


def detail_js():
    return """
(() => {
  const containers = Array.from(document.querySelectorAll('div'))
    .map((el) => {
      const text = (el.innerText || el.textContent || '').trim();
      const r = el.getBoundingClientRect();
      return {el, text, r};
    })
    .filter(x => x.r.x >= 430 && x.r.width > 450 && (x.text.includes('Желаемая позиция:') || x.text.includes('Сохранено ') || x.text.includes('Опыт работы') || x.text.includes('Профессиональный опыт')));
  containers.sort((a, b) => {
    const aCv = a.text.includes('Основная информация') || a.text.includes('Профессиональный опыт');
    const bCv = b.text.includes('Основная информация') || b.text.includes('Профессиональный опыт');
    if (aCv !== bCv) return aCv ? -1 : 1;
    return b.text.length - a.text.length;
  });
  const picked = containers[0];
  if (!picked) return null;
  const links = Array.from(picked.el.querySelectorAll('a,button[role="tab"],button'))
    .map((el) => ({text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' '), href: el.href || ''}))
    .filter(x => x.text);
  return {text: picked.text, links};
})()
"""


def collect_tab_texts(cdp, delay):
    result = []
    for label in ["Распаршенное", "Анализ", "Описание", "Оригинал", "hh.ru"]:
        clicked = eval_js(cdp, click_tab_js(label), await_promise=True)
        if clicked:
            time.sleep(max(0.25, delay / 2))
            detail = wait_for_detail(cdp, timeout=5)
            text = detail.get("text", "") if detail else ""
            if text:
                result.append({"label": label, "text": text, "links": detail.get("links", []) if detail else []})
    if not result:
        detail = wait_for_detail(cdp, timeout=5)
        if detail and detail.get("text"):
            result.append({"label": "visible", "text": detail["text"], "links": detail.get("links", [])})
    return result


def pick_best_cv(tab_texts):
    if not tab_texts:
        return {"label": "", "text": "", "links": []}

    def score(item):
        text = item.get("text", "")
        cv_markers = [
            "Профессиональный опыт",
            "Опыт работы",
            "Основная информация",
            "Навыки",
            "Образование",
            "Желаемая должность",
            "Желаемая позиция",
        ]
        marker_score = sum(1000 for marker in cv_markers if marker in text)
        return marker_score + len(text)

    return max(tab_texts, key=score)


def wait_for_detail(cdp, timeout=10):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = eval_js(cdp, detail_js(), await_promise=True)
        if last and last.get("text") and (
            "Желаемая позиция:" in last["text"]
            or "Сохранено " in last["text"]
            or "Опыт работы" in last["text"]
            or "Профессиональный опыт" in last["text"]
        ):
            return last
        time.sleep(0.4)
    return last


def summary_name(item):
    lines = item.get("lines") or []
    if not lines:
        lines = re.split(r"\s{2,}", item.get("text", ""))
    if not lines:
        return ""
    first = lines[0].strip()
    if len(first) <= 4 and re.fullmatch(r"[A-ZА-ЯЁ]{1,4}", first):
        return lines[1].strip() if len(lines) > 1 else ""
    return first


def name_matches(expected, actual):
    if not expected or not actual:
        return True
    expected_parts = [p.lower() for p in re.split(r"\s+", expected) if len(p) > 1]
    actual_lower = actual.lower()
    if len(expected_parts) >= 2:
        return all(part in actual_lower for part in expected_parts[:2])
    return expected_parts[0] in actual_lower if expected_parts else True


def click_and_wait_candidate(cdp, idx, item, delay):
    expected = summary_name(item)
    clicked = None
    for attempt in range(5):
        clicked = eval_js(cdp, click_candidate_by_name_js(expected), await_promise=True)
        if not clicked or not clicked.get("clicked"):
            clicked = eval_js(cdp, click_candidate_js(idx), await_promise=True)
        if not clicked or not clicked.get("clicked", True):
            time.sleep(delay)
            continue
        time.sleep(delay + attempt * 0.5)
        detail = wait_for_detail(cdp, timeout=8)
        actual_name = parse_name(detail.get("text", "")) if detail else ""
        if name_matches(expected, actual_name):
            return clicked, detail
    return None, None  # give up on this flaky candidate; caller logs + skips (no abort)


def next_page_js():
    return """
(() => {
  const blocks = Array.from(document.querySelectorAll('div'))
    .map((el) => {
      const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
      const r = el.getBoundingClientRect();
      return {el, text, r};
    })
    .filter(x => /\\d+\\s*-\\s*\\d+\\s+из\\s+\\d+/.test(x.text) && x.r.x >= 100 && x.r.x <= 450 && x.el.querySelectorAll('button').length >= 2);
  blocks.sort((a, b) => a.r.width - b.r.width);
  const block = blocks[0];
  if (!block) return {clicked: false, reason: 'pagination not found'};
  const buttons = Array.from(block.el.querySelectorAll('button'));
  const next = buttons[buttons.length - 1];
  if (!next) return {clicked: false, reason: 'next button not found', text: block.text};
  if (next.disabled || next.getAttribute('aria-disabled') === 'true') return {clicked: false, done: true, text: block.text};
  next.click();
  return {clicked: true, text: block.text};
})()
"""


def navigate_to(cdp, url):
    cdp.call("Page.navigate", {"url": url})
    time.sleep(4)


def write_candidate(out_dir, seq, page, index_on_page, summary, detail, current_url):
    text = detail["text"] or ""
    links = detail.get("links") or []
    name = parse_name(text)
    position = field(text, "Желаемая позиция")
    company = field(text, "Компания")
    phone = field(text, "Номер телефона") or field(text, "phone")
    email = field(text, "Email") or field(text, "email")
    telegram = field(text, "Telegram") or field(text, "telegram")
    vacancy = parse_vacancy(text, links)
    status = parse_status(text) or "Входящий поток"

    filename = f"{seq:04d}_{safe_name(name or summary or 'candidate')}.txt"
    path = out_dir / filename
    header = {
        "seq": seq,
        "page": page,
        "index_on_page": index_on_page,
        "name": name,
        "position": position,
        "company": company,
        "vacancy": vacancy,
        "status": status,
        "phone": phone,
        "email": email,
        "telegram": telegram,
        "detail_url": current_url,
        "list_summary": summary,
        "links": links,
    }
    body = [
        json.dumps(header, ensure_ascii=False, indent=2),
        "",
        "===== PARSED CV TEXT =====",
        text,
        "",
    ]
    path.write_text("\n".join(body), encoding="utf-8")
    header["file"] = str(path)
    return header


def read_saved_header(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("{"):
        return None
    raw = text.split("\n\n===== ", 1)[0]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    data["file"] = str(path)
    seq_match = re.match(r"^(\d+)_", path.name)
    if seq_match:
        data["seq"] = int(seq_match.group(1))
    return data


def write_indexes(out_dir, rows):
    index_jsonl = out_dir / "index.jsonl"
    index_tsv = out_dir / "index.tsv"
    with index_jsonl.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    columns = ["seq", "page", "index_on_page", "name", "position", "company", "vacancy", "status", "phone", "email", "telegram", "detail_url", "file"]
    with index_tsv.open("w", encoding="utf-8") as f:
        f.write("\t".join(columns) + "\n")
        for row in rows:
            f.write("\t".join(str(row.get(col, "")).replace("\t", " ").replace("\n", " ") for col in columns) + "\n")
    return index_tsv, index_jsonl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9347)
    parser.add_argument("--url", default=INCOMING_URL)
    parser.add_argument("--out", default="data/incoming_candidates")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--reindex", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.reindex:
        rows = []
        for path in sorted(out_dir.glob("*.txt")):
            row = read_saved_header(path)
            if row:
                rows.append(row)
        index_tsv, _ = write_indexes(out_dir, rows)
        print(f"Reindexed {len(rows)} candidates in {out_dir}", flush=True)
        print(f"Index: {index_tsv}", flush=True)
        return

    cdp = CDP(page_ws(args.port))
    rows = []
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        navigate_to(cdp, args.url)

        # resume: skip candidates already on disk, continue seq from the max saved
        saved_names = []
        seq = 0
        for p in sorted(out_dir.glob("*.txt")):
            hdr = read_saved_header(p)
            if hdr:
                saved_names.append(hdr.get("name") or hdr.get("list_summary") or "")
                seq = max(seq, hdr.get("seq", 0))
        if saved_names:
            print(f"Resume: {len(saved_names)} already on disk; continuing from seq {seq}", flush=True)
        new_count = 0
        skipped = 0
        while True:
            page_info = eval_js(cdp, page_range_js(), await_promise=True) or {}
            page = page_info.get("page") or 1
            buttons = eval_js(cdp, candidate_buttons_js(), await_promise=True) or []
            if not buttons:
                raise RuntimeError("No candidate buttons found on the current page")

            print(f"Page {page}: {len(buttons)} candidates visible, range {page_info.get('text', '')}", flush=True)
            for idx, item in enumerate(buttons):
                if args.limit and new_count >= args.limit:
                    break
                name = summary_name(item)
                if name and any(name_matches(name, sn) for sn in saved_names):
                    continue  # already downloaded this candidate
                clicked, detail = click_and_wait_candidate(cdp, idx, item, args.delay)
                if clicked is None:
                    skipped += 1
                    print(f"  SKIP page {page} idx {idx + 1}: '{name}' (detail did not match)", flush=True)
                    continue
                eval_js(cdp, click_parsed_tab_js(), await_promise=True)
                time.sleep(max(0.2, args.delay / 2))

                detail = wait_for_detail(cdp, timeout=8) or detail
                current_url = eval_js(cdp, "location.href", await_promise=True)
                if not detail or not detail.get("text"):
                    skipped += 1
                    print(f"  SKIP page {page} idx {idx + 1}: '{name}' (no parsed detail)", flush=True)
                    continue

                seq += 1
                new_count += 1
                row = write_candidate(out_dir, seq, page, idx + 1, item["text"], detail, current_url)
                saved_names.append(row.get("name") or name)
                rows.append(row)
                print(f"{seq:04d}: {row.get('name') or item['text']} | {row.get('vacancy')}", flush=True)

            if args.limit and new_count >= args.limit:
                break
            before = eval_js(cdp, page_range_js(), await_promise=True) or {}
            moved = eval_js(cdp, next_page_js(), await_promise=True)
            if not moved or not moved.get("clicked"):
                print(f"Pagination finished: {moved}", flush=True)
                break
            deadline = time.time() + 10
            while time.time() < deadline:
                time.sleep(0.5)
                after = eval_js(cdp, page_range_js(), await_promise=True) or {}
                if after.get("text") and after.get("text") != before.get("text"):
                    break
            time.sleep(args.delay)

    finally:
        cdp.close()

    # rebuild the index from ALL files on disk (so resume runs keep a complete index)
    all_rows = []
    for p in sorted(out_dir.glob("*.txt")):
        hdr = read_saved_header(p)
        if hdr:
            all_rows.append(hdr)
    index_tsv, _ = write_indexes(out_dir, all_rows)

    print(f"Saved {len(rows)} new ({skipped} skipped this run); {len(all_rows)} total in {out_dir}", flush=True)
    print(f"Index: {index_tsv}", flush=True)


if __name__ == "__main__":
    main()

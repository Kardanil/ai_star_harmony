#!/usr/bin/env python3
import argparse
import json
import sys
import time
from urllib.parse import quote

import requests
import websocket


PORT = 9347


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        self.next_id = 1

    def call(self, method, params=None, timeout=10):
        msg_id = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        # Track the socket recv timeout to the requested per-call deadline; the
        # connection was opened with a fixed 10s recv timeout, which would
        # otherwise raise WebSocketTimeoutException (NOT TimeoutError) and abort
        # any call whose `timeout` exceeds 10s (e.g. a long-running awaitPromise).
        self.ws.settimeout(timeout + 1)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result", {})
        raise TimeoutError(method)

    def close(self):
        self.ws.close()


def page_ws(port=PORT):
    pages = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
    for page in pages:
        if page.get("type") == "page":
            return page["webSocketDebuggerUrl"]
    raise RuntimeError("No page target found")


def new_tab(url, port=PORT):
    # Newer Chrome requires PUT for /json/new (GET returns 405); fall back to GET
    # for older builds.
    encoded = quote(url, safe="")
    endpoint = f"http://127.0.0.1:{port}/json/new?{encoded}"
    resp = requests.put(endpoint, timeout=5)
    if resp.status_code >= 400:
        resp = requests.get(endpoint, timeout=5)
    return resp.json()["webSocketDebuggerUrl"]


def eval_js(cdp, expr, await_promise=False):
    result = cdp.call(
        "Runtime.evaluate",
        {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
            "userGesture": True,
        },
    )
    value = result.get("result", {})
    if "value" in value:
        return value["value"]
    if "description" in value:
        return value["description"]
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["new", "nav", "text", "eval", "click", "buttons", "url"])
    parser.add_argument("arg", nargs="?")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if args.cmd == "new":
        if not args.arg:
            raise SystemExit("new requires URL")
        ws_url = new_tab(args.arg, args.port)
    else:
        ws_url = page_ws(args.port)

    cdp = CDP(ws_url)
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        if args.cmd == "nav":
            cdp.call("Page.navigate", {"url": args.arg})
            time.sleep(4)
            print(eval_js(cdp, "location.href"))
        elif args.cmd == "new":
            time.sleep(4)
            print(eval_js(cdp, "location.href"))
        elif args.cmd == "url":
            print(eval_js(cdp, "location.href"))
        elif args.cmd == "text":
            text = eval_js(cdp, "document.body ? document.body.innerText : ''")
            print(text or "")
        elif args.cmd == "eval":
            print(json.dumps(eval_js(cdp, args.arg, await_promise=True), ensure_ascii=False, indent=2))
        elif args.cmd == "buttons":
            js = """
(() => Array.from(document.querySelectorAll('button,a,[role="button"],[role="tab"],[role="menuitem"]'))
  .map((el, i) => ({i, tag: el.tagName, role: el.getAttribute('role'), text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 160)}))
  .filter(x => x.text))()
"""
            print(json.dumps(eval_js(cdp, js), ensure_ascii=False, indent=2))
        elif args.cmd == "click":
            needle = json.dumps(args.arg, ensure_ascii=False)
            js = f"""
(() => {{
  const needle = {needle}.toLowerCase();
  const els = Array.from(document.querySelectorAll('button,a,[role="button"],[role="tab"],[role="menuitem"],div,span'));
  const scored = els.map((el) => {{
    const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
    if (!text || !text.toLowerCase().includes(needle)) return null;
    const clickable = el.closest('button,a,[role="button"],[role="tab"],[role="menuitem"]') || el;
    const r = clickable.getBoundingClientRect();
    return {{text: text.slice(0, 220), tag: clickable.tagName, role: clickable.getAttribute('role'), x: r.x, y: r.y, w: r.width, h: r.height, el: clickable}};
  }}).filter(Boolean).filter(x => x.w > 0 && x.h > 0);
  scored.sort((a,b) => (a.text.length - b.text.length) || (a.y - b.y));
  if (!scored.length) return {{clicked: false, reason: 'not found'}};
  const item = scored[0];
  item.el.scrollIntoView({{block: 'center', inline: 'center'}});
  item.el.click();
  return {{clicked: true, text: item.text, tag: item.tag, role: item.role, x: item.x, y: item.y, w: item.w, h: item.h}};
}})()
"""
            print(json.dumps(eval_js(cdp, js), ensure_ascii=False, indent=2))
            time.sleep(2)
    finally:
        cdp.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

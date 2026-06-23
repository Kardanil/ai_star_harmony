#!/usr/bin/env python3
"""Triage one stage of a Harmony vacancy in a single pass — the one script that
replaces process_candidates.py / reject_remaining.py / classify_remaining.py /
finish_stragglers.py.

Model (see harmony.py): decisions are keyed by **openId**, never by name. We
walk the stage's cards; for each we open it (revealing its openId), look up the
decision, and `move()` it — which verifies by the source-stage count dropping.
Because we wait for that drop before taking the next card, there is no
double-processing and no straggler pass: one clean sweep.

Decisions come from `<base>/decisions.tsv` (cols: seq, action[, reason]) joined
to `<base>/index.tsv` (seq -> detail_url -> openId). Actions:
  advance -> Первичный отбор AI   reject -> Rejection   backlog -> Бэклог резюме

A card whose openId has NO decision is, by default, SKIPPED (left in place) —
not rejected. Pass `--default-action reject` (or `--reject-all`) to act on the
undecided too. This avoids silently rejecting a new/unmatched applicant.

  # apply a decisions.tsv (undecided cards are left alone)
  python3 src/process.py --base data/severstal_go --vacancy "Северсталь / Backend-инженер (Go)"
  # only a subset of seqs
  python3 src/process.py --base data/<x> --vacancy "<name>" --only 4 21 60
  # reject everyone in the stage (explicit)
  python3 src/process.py --vacancy "<name>" --vacancy-id <uuid> --reject-all
  # preview the plan from the decisions file (no browser)
  python3 src/process.py --base data/<x> --vacancy "<name>" --dry-run
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from harmony import Harmony, INCOMING_STAGE_ID, DEFAULT_REJECT_REASON, open_id_from_url

TARGET_BY_ACTION = {
    "advance": "Первичный отбор AI",
    "reject": "Rejection",
    "backlog": "Бэклог резюме",
}


def load_decisions(base, only=None):
    """-> ({openId: {action, reason, seq, name}}, vacancy_id). `only` (a set of
    seq strings) restricts which decisions are loaded."""
    base = Path(base)
    with (base / "index.tsv").open(newline="", encoding="utf-8") as f:
        index = {r["seq"]: r for r in csv.DictReader(f, delimiter="\t")}
    by_open = {}
    vacancy_id = ""
    # derive vacancy id from any index row (all share the same vacancy)
    for r in index.values():
        m = re.search(r"/vacancies/([0-9a-f-]+)", r.get("detail_url", ""))
        if m:
            vacancy_id = m.group(1)
            break
    dpath = base / "decisions.tsv"
    if not dpath.exists():
        return by_open, vacancy_id
    with dpath.open(newline="", encoding="utf-8") as f:
        for d in csv.DictReader(f, delimiter="\t"):
            action = (d.get("action") or "").strip().lower()
            if action not in TARGET_BY_ACTION:
                continue
            if only and d["seq"] not in only:
                continue
            idx = index.get(d["seq"])
            if not idx:
                continue
            oid = open_id_from_url(idx["detail_url"])
            if oid:
                by_open[oid] = {"action": action, "reason": (d.get("reason") or "").strip(),
                                "seq": d["seq"], "name": idx.get("name", "")}
    return by_open, vacancy_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9347)
    ap.add_argument("--base", help="vacancy folder with index.tsv + decisions.tsv")
    ap.add_argument("--vacancy", required=True, help="CRM vacancy name (to expand in the 'Сменить этап' popover)")
    ap.add_argument("--vacancy-id", help="vacancy UUID (else derived from index.tsv)")
    ap.add_argument("--stage-id", default=INCOMING_STAGE_ID, help="source stage id (default Входящий поток)")
    ap.add_argument("--source-stage", default="Входящий поток", help="source stage name (for count verification)")
    ap.add_argument("--reason", default=DEFAULT_REJECT_REASON)
    ap.add_argument("--only", nargs="*", help="restrict to these seq ids (others are skipped)")
    ap.add_argument("--reject-all", action="store_true", help="reject every card in the stage; ignore decisions")
    ap.add_argument("--default-action", choices=["skip"] + list(TARGET_BY_ACTION), default="skip",
                    help="action for a card whose openId has no decision (default: skip, i.e. leave it)")
    ap.add_argument("--dry-run", action="store_true", help="print the planned actions from the decisions file; no browser")
    ap.add_argument("--max", type=int, default=1000)
    args = ap.parse_args()

    decisions, derived_vid = ({}, "")
    only = set(args.only) if args.only else None
    if args.base and not args.reject_all:
        decisions, derived_vid = load_decisions(args.base, only)
        print(f"decisions: {len(decisions)} (by openId)" + (f", only={sorted(only)}" if only else ""), flush=True)
    vacancy_id = args.vacancy_id or derived_vid
    if not vacancy_id:
        print("ERROR: need --vacancy-id (or a --base whose index.tsv has it)", file=sys.stderr)
        return 2

    # --- dry run: preview from the decisions file, no browser ---
    if args.dry_run:
        if args.reject_all:
            print("DRY RUN: would REJECT every card in the stage", flush=True)
            return 0
        plan = defaultdict(int)
        for oid, d in decisions.items():
            print(json.dumps({"seq": d["seq"], "name": d["name"], "openId": oid,
                              "action": d["action"], "target": TARGET_BY_ACTION[d["action"]]}, ensure_ascii=False), flush=True)
            plan[d["action"]] += 1
        print(f"DRY RUN plan: {dict(plan)} | undecided cards -> {args.default_action}", flush=True)
        return 0

    def decide(oid):
        if args.reject_all:
            return "reject", args.reason
        d = decisions.get(oid)
        if d:
            return d["action"], (d["reason"] or args.reason)
        return args.default_action, args.reason  # default: 'skip'

    h = Harmony(args.port)
    counts = {"moved": 0, "failed": 0, "skipped": 0}
    by_action = defaultdict(int)
    fails = defaultdict(int)
    skip = set()        # openIds we are done with (acted, skipped, or gave up on)
    no_progress = 0
    end = None
    try:
        h.goto_funnel(vacancy_id, args.stage_id)
        start = h.stage_count(args.source_stage)
        print(f"{args.source_stage}: {start} present", flush=True)

        for _ in range(args.max):
            h.dismiss()  # clear any leftover popover/dialog from a prior failure
            cards = h.list_cards()
            if not cards:
                print("LIST EMPTY — done", flush=True)
                break

            # open the first card whose openId we haven't finished with
            picked = None
            for c in cards:
                oid = h.open_card(c["index"], expected_name=c["name"])
                if not oid or oid in skip:
                    continue
                picked = (c, oid)
                break
            if picked is None:
                no_progress += 1
                if no_progress >= 3:
                    print(json.dumps({"done": "only skipped/unopenable cards remain", "skipped": len(skip)}, ensure_ascii=False), flush=True)
                    break
                continue
            no_progress = 0

            c, oid = picked
            action, reason = decide(oid)
            name = h.detail_name() or c["name"]

            if action == "skip":
                print(json.dumps({"name": name, "openId": oid, "result": "skipped (no decision)"}, ensure_ascii=False), flush=True)
                skip.add(oid)
                counts["skipped"] += 1
                continue

            target = TARGET_BY_ACTION[action]
            if h.detail_status() == target:  # already there (e.g. a re-run) — nothing to do
                print(json.dumps({"name": name, "openId": oid, "target": target, "result": "already_target"}, ensure_ascii=False), flush=True)
                skip.add(oid)
                counts["skipped"] += 1
                continue
            res = h.move(args.vacancy, target, source_stage=args.source_stage, reason=reason)
            print(json.dumps({"name": name, "openId": oid, "action": action, "target": target, "result": res}, ensure_ascii=False), flush=True)
            if res == "moved":
                skip.add(oid)
                counts["moved"] += 1
                by_action[action] += 1
            else:
                fails[oid] += 1
                counts["failed"] += 1
                if fails[oid] >= 2:
                    skip.add(oid)  # give up after two attempts; don't loop forever
    finally:
        try:
            end = h.stage_count(args.source_stage)
        except Exception:
            pass
        h.close()

    print(f"DONE moved={counts['moved']} ({dict(by_action)}) skipped={counts['skipped']} failed={counts['failed']} | {args.source_stage} now={end}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

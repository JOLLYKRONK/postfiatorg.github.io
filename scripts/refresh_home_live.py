#!/usr/bin/env python3
"""Refresh the homepage's build-time data snapshots from the live public APIs.

Writes:
  static/benchmarks/live-testnet-validator-stats.json
      Validator summary computed from the live VHS API with the same rules the
      homepage JS uses (revoked validators excluded, 24h agreement >= 0.999,
      30d agreement >= 0.99, ledger = max current_index). This file is both
      the JS fallback payload and the source for the statically rendered
      validator card.
  data/task_feed_snapshot.json
      The most recent public Task Node feed items, rendered statically into
      the homepage at build time and live-refreshed by JS in the browser.
  static/llms.txt, static/llms-full.txt, content/about.md
      The "Current public proof points" block in each, rewritten in place from
      the same snapshot. These are what assistants quote, so they must not be
      able to drift behind the homepage card.
  static/postfiat-project.json
      current_status.last_updated_utc and live_validator_snapshot.

Run before a deploy (or on a schedule) to keep the no-JS view current:
  python3 scripts/refresh_home_live.py

Report drift without writing anything, for CI:
  python3 scripts/refresh_home_live.py --check

Check the renderers with no network at all:
  python3 scripts/refresh_home_live.py --self-test
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import pathlib
import re
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[1]
VHS_URL = "https://vhs.testnet.postfiat.org/v1/network/validators/test"
FEED_URL = "https://pftasks-api.fly.dev/activity/public-feed?limit=24"
STATS_PATH = REPO / "static" / "benchmarks" / "live-testnet-validator-stats.json"
FEED_PATH = REPO / "data" / "task_feed_snapshot.json"
FEED_ITEMS = 8
PROJECT_JSON_PATH = REPO / "static" / "postfiat-project.json"
EXPLORER_URL = "https://explorer.testnet.postfiat.org/network/validators"
PROOF_POINTS_HEADING = "## Current public proof points"
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

SOURCE_BULLET = (
    "Source: Post Fiat validator history service snapshot at {source_url} "
    "(`count: {validator_count}`, {validator_count} validator entries), "
    "retrieved {as_of_date}. Same snapshot rendered in the testnet explorer "
    "at {explorer_url}"
)

# Bullet templates per surface, keyed by repo-relative path. The wording
# differs per page and is kept verbatim so a refresh changes numbers only.
TEXT_SURFACES: dict[str, tuple[str, ...]] = {
    "static/llms.txt": (
        "{validator_count} validators listed in the latest public VHS snapshot",
        "{publishing_domain_count} publishing domains visible on the network",
        "{verified_domain_count} verified domains in the latest public VHS snapshot",
        "{strong_24h} of {validator_count} show 99.9%+ agreement over 24 hours",
        "{strong_30d} of {validator_count} show 99%+ agreement over 30 days",
        SOURCE_BULLET,
    ),
    "static/llms-full.txt": (
        "{validator_count} validators listed in the latest public VHS snapshot",
        "{publishing_domain_count} publishing domains visible on the network",
        "{verified_domain_count} verified domains in the latest public VHS snapshot",
        "{strong_24h} of {validator_count} show 99.9%+ agreement over 24 hours",
        "{strong_30d} of {validator_count} show 99%+ agreement over 30 days",
        SOURCE_BULLET,
    ),
    "content/about.md": (
        "{validator_count} validators in the latest public VHS snapshot",
        "{publishing_domain_count} publishing domains visible on the network",
        "{verified_domain_count} verified domains in the latest public VHS snapshot",
        "{strong_24h} of {validator_count} above 99.9% agreement over 24 hours",
        "{strong_30d} of {validator_count} above 99% agreement over 30 days",
        SOURCE_BULLET,
    ),
}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def agreement_score(validator: dict, key: str) -> float:
    try:
        return float((validator.get(key) or {}).get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def build_validator_stats(payload: dict, now_iso: str) -> dict:
    validators = [v for v in payload.get("validators", []) if not v.get("revoked")]
    total = len(validators)
    strong24 = sum(1 for v in validators if agreement_score(v, "agreement_24h") >= 0.999)
    strong30 = sum(1 for v in validators if agreement_score(v, "agreement_30day") >= 0.99)

    def mean(key: str) -> float:
        scores = [agreement_score(v, key) for v in validators]
        return sum(scores) / len(scores) if scores else 0.0

    return {
        "generated_at": now_iso,
        "source_name": "Live VHS snapshot",
        "source_url": VHS_URL,
        "validator_count": total,
        "publishing_domain_count": sum(
            1 for v in validators if str(v.get("domain") or "").strip()
        ),
        "verified_domain_count": sum(1 for v in validators if v.get("domain_verified")),
        "latest_ledger_index": max(
            (int(v.get("current_index") or 0) for v in validators), default=0
        ),
        "agreement_24h": {
            "threshold": 0.999,
            "threshold_label": "99.9%+",
            "count": strong24,
            "ratio": (strong24 / total) if total else 0.0,
            "mean_score": mean("agreement_24h"),
        },
        "agreement_30day": {
            "threshold": 0.99,
            "threshold_label": "99%+",
            "count": strong30,
            "ratio": (strong30 / total) if total else 0.0,
            "mean_score": mean("agreement_30day"),
        },
    }


def display_time(iso: str) -> str:
    # Always include the year: undated or year-less timestamps next to a
    # dated whitepaper read as inconsistent to careful reviewers.
    try:
        stamp = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "UTC"
    return stamp.strftime("%b %-d, %Y, %H:%M UTC")


def diversify_by_actor(raw_items: list, limit: int, per_actor: int = 2) -> list:
    """Prefer a mix of contributors over a single node's burst of activity."""
    picked: list = []
    counts: dict[str, int] = {}
    deferred: list = []
    for item in raw_items:
        actor = str(item.get("actor") or "node")
        if counts.get(actor, 0) < per_actor:
            picked.append(item)
            counts[actor] = counts.get(actor, 0) + 1
        else:
            deferred.append(item)
        if len(picked) >= limit:
            return picked
    return (picked + deferred)[:limit]


def build_feed_snapshot(payload: dict, now_iso: str) -> dict:
    items = []
    for item in diversify_by_actor(payload.get("items", []), FEED_ITEMS):
        title = (item.get("title") or item.get("summary") or "Task Node update").strip()
        summary = (item.get("summary") or "").strip()
        if title.endswith("...") and summary:
            title = summary
        items.append(
            {
                "category": str(item.get("category") or item.get("type") or "network")
                .replace("_", " ")
                .strip(),
                "timestamp": item.get("timestamp") or "",
                "display_time": display_time(item.get("timestamp") or ""),
                "title": title[:132],
                "summary": summary[:150],
                "actor": (item.get("actor") or "node").strip(),
                "tickers": [str(t).lstrip("$") for t in (item.get("tickers") or [])][:4],
                "links": [
                    {
                        "label": (link.get("label") or "PFTL proof").strip(),
                        "url": link.get("url") or "",
                    }
                    for link in (item.get("links") or [])
                    if str(link.get("url") or "").startswith("https://")
                ][:2],
            }
        )
    return {
        "generated_at": now_iso,
        "generated_at_display": display_time(now_iso),
        "source": FEED_URL,
        "items": items,
    }


def as_of_date(now_iso: str) -> str:
    # Month names are spelled out rather than run through strftime so the
    # output does not depend on the machine's locale.
    stamp = dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    return f"{MONTH_NAMES[stamp.month - 1]} {stamp.day}, {stamp.year}"


def proof_point_facts(stats: dict) -> dict:
    return {
        "as_of_date": as_of_date(stats["generated_at"]),
        "validator_count": stats["validator_count"],
        "publishing_domain_count": stats["publishing_domain_count"],
        "verified_domain_count": stats["verified_domain_count"],
        "strong_24h": stats["agreement_24h"]["count"],
        "strong_30d": stats["agreement_30day"]["count"],
        "source_url": stats["source_url"],
        "explorer_url": EXPLORER_URL,
    }


def render_proof_points(stats: dict, templates: tuple[str, ...]) -> str:
    facts = proof_point_facts(stats)
    bullets = "\n".join(f"- {tpl.format(**facts)}" for tpl in templates)
    return (
        f"{PROOF_POINTS_HEADING}\n\n"
        f"As of {facts['as_of_date']}:\n\n"
        f"{bullets}\n"
    )


def replace_proof_points(text: str, block: str, path: pathlib.Path) -> str:
    """Swap the proof-points section, leaving every other section untouched."""
    try:
        start = text.index(PROOF_POINTS_HEADING)
        end = text.index("\n## ", start + len(PROOF_POINTS_HEADING))
    except ValueError as exc:
        raise SystemExit(
            f"{path.relative_to(REPO)}: no '{PROOF_POINTS_HEADING}' section "
            "to refresh; restore the heading or drop the file from TEXT_SURFACES"
        ) from exc
    return text[:start] + block + text[end:]


def refresh_project_json(current_text: str, stats: dict) -> str:
    payload = json.loads(current_text)
    status = payload["current_status"]
    snapshot = status["live_validator_snapshot"]
    # That field has always been second-precision; the card payload keeps
    # milliseconds, so trim rather than restyle the published summary.
    status["last_updated_utc"] = stats["generated_at"].split(".")[0].removesuffix("Z") + "Z"
    snapshot["source_url"] = stats["source_url"]
    snapshot["validator_count"] = stats["validator_count"]
    snapshot["publishing_domain_count"] = stats["publishing_domain_count"]
    snapshot["verified_domain_count"] = stats["verified_domain_count"]
    snapshot["latest_ledger_index"] = stats["latest_ledger_index"]
    snapshot["agreement_24h"]["count"] = stats["agreement_24h"]["count"]
    snapshot["agreement_30day"]["count"] = stats["agreement_30day"]["count"]
    return json.dumps(payload, indent=2) + "\n"


def build_text_targets(stats: dict) -> list[tuple[pathlib.Path, str]]:
    targets: list[tuple[pathlib.Path, str]] = []
    for rel, templates in TEXT_SURFACES.items():
        path = REPO / rel
        current = path.read_text(encoding="utf-8")
        block = render_proof_points(stats, templates)
        targets.append((path, replace_proof_points(current, block, path)))
    targets.append((PROJECT_JSON_PATH, refresh_project_json(
        PROJECT_JSON_PATH.read_text(encoding="utf-8"), stats
    )))
    return targets


def atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


VOLATILE_KEYS = frozenset(
    {
        "generated_at",
        "generated_at_display",
        "last_updated_utc",
        "latest_ledger_index",
        "ratio",
        "mean_score",
    }
)
DATED_RETRIEVE = re.compile(r"retrieved [A-Z][a-z]+ \d{1,2}, \d{4}")


def material_view(path: pathlib.Path, text: str):
    """Drop timestamps and ledger position so drift checks report fact changes.

    Without this, every run looks stale: the ledger index moves every few
    seconds and the retrieval date turns over at midnight.
    """
    if path.suffix == ".json":

        def strip(node):
            if isinstance(node, dict):
                return {
                    key: strip(value)
                    for key, value in node.items()
                    if key not in VOLATILE_KEYS
                }
            if isinstance(node, list):
                return [strip(value) for value in node]
            return node

        return strip(json.loads(text))
    return [
        line
        for line in (DATED_RETRIEVE.sub("retrieved <date>", raw) for raw in text.splitlines())
        if not line.startswith("As of ")
    ]


def self_test() -> int:
    payload = {
        "validators": [
            {
                "domain": "a.example",
                "domain_verified": True,
                "current_index": 10,
                "agreement_24h": {"score": "1.00000"},
                "agreement_30day": {"score": "0.99500"},
            },
            {
                "domain": "b.example",
                "domain_verified": False,
                "current_index": 12,
                "agreement_24h": {"score": "0.99000"},
                "agreement_30day": {"score": "0.90000"},
            },
            {
                "domain": "c.example",
                "domain_verified": True,
                "current_index": 11,
                "revoked": True,
                "agreement_24h": {"score": "1.00000"},
                "agreement_30day": {"score": "1.00000"},
            },
        ]
    }
    stats = build_validator_stats(payload, "2026-09-21T21:15:25.000Z")
    checks: list[tuple[str, bool]] = [
        ("revoked validators excluded", stats["validator_count"] == 2),
        ("publishing domains counted", stats["publishing_domain_count"] == 2),
        ("verified domains counted", stats["verified_domain_count"] == 1),
        ("24h agreement counted", stats["agreement_24h"]["count"] == 1),
        ("30d agreement counted", stats["agreement_30day"]["count"] == 1),
        ("ledger index is the max", stats["latest_ledger_index"] == 12),
        ("date renders without locale", as_of_date(stats["generated_at"]) == "September 21, 2026"),
    ]
    for rel, templates in TEXT_SURFACES.items():
        path = REPO / rel
        current = path.read_text(encoding="utf-8")
        block = render_proof_points(stats, templates)
        once = replace_proof_points(current, block, path)
        twice = replace_proof_points(once, block, path)
        start = current.index(PROOF_POINTS_HEADING)
        tail_of = lambda text: text[text.index("\n## ", start + len(PROOF_POINTS_HEADING)):]
        checks.append((f"{rel} rewrites the block", once != current))
        checks.append((f"{rel} rewrite is idempotent", once == twice))
        checks.append(
            (
                f"{rel} keeps the sections around it",
                once[:start] == current[:start] and tail_of(once) == tail_of(current),
            )
        )
        checks.append((f"{rel} carries the source URL", stats["source_url"] in once))
    project = refresh_project_json(PROJECT_JSON_PATH.read_text(encoding="utf-8"), stats)
    snapshot = json.loads(project)["current_status"]["live_validator_snapshot"]
    checks.append(("project json validator count", snapshot["validator_count"] == 2))
    checks.append(("project json 24h count", snapshot["agreement_24h"]["count"] == 1))

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    print(f"{len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh the committed live-fact snapshots from the public APIs."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any surface is behind the live snapshot; write nothing",
    )
    parser.add_argument(
        "--exact",
        action="store_true",
        help="with --check, compare bytes instead of ignoring timestamps and ledger position",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the diff for each surface, write nothing"
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="refresh the assistant/page text surfaces and the project JSON only",
    )
    parser.add_argument(
        "--skip-stats", action="store_true", help="leave the homepage card payload alone"
    )
    parser.add_argument(
        "--skip-feed", action="store_true", help="leave the Task Node feed snapshot alone"
    )
    parser.add_argument(
        "--self-test", action="store_true", help="exercise the renderers offline and exit"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return self_test()

    now_iso = (
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
    ).replace("+00:00", "Z")

    stats = build_validator_stats(fetch_json(VHS_URL), now_iso)
    # The feed carries no durable fact and changes on every run, so a fact
    # drift check does not fetch it at all.
    include_feed = not (args.text_only or args.skip_feed or (args.check and not args.exact))
    targets: list[tuple[pathlib.Path, str]] = []
    if not (args.text_only or args.skip_stats):
        targets.append((STATS_PATH, json.dumps(stats, indent=2) + "\n"))
    targets.extend(build_text_targets(stats))
    if include_feed:
        feed = build_feed_snapshot(fetch_json(FEED_URL), now_iso)
        targets.append((FEED_PATH, json.dumps(feed, indent=2) + "\n"))

    print(
        f"validator stats: {stats['validator_count']} validators, "
        f"{stats['publishing_domain_count']} domains, "
        f"ledger {stats['latest_ledger_index']:,} @ {stats['generated_at']}"
    )

    current_texts: dict[pathlib.Path, str | None] = {
        path: (path.read_text(encoding="utf-8") if path.exists() else None)
        for path, _ in targets
    }
    if args.check:
        stale: list[pathlib.Path] = []
        for path, text in targets:
            current = current_texts[path]
            if current is None:
                stale.append(path)
            elif args.exact:
                if current != text:
                    stale.append(path)
            elif material_view(path, current) != material_view(path, text):
                stale.append(path)
        if stale:
            print(f"{len(stale)} of {len(targets)} live-fact surfaces are stale:")
            for path in stale:
                print(f"  {path.relative_to(REPO)}")
            return 1
        print(f"all {len(targets)} live-fact surfaces are current")
        return 0

    drifted: dict[pathlib.Path, tuple[str | None, str]] = {
        path: (current_texts[path], text)
        for path, text in targets
        if current_texts[path] != text
    }

    for path, (current, text) in drifted.items():
        label = path.relative_to(REPO)
        if args.dry_run:
            if current is None:
                print(f"would create {label}")
                continue
            sys.stdout.write(
                "".join(
                    difflib.unified_diff(
                        current.splitlines(keepends=True),
                        text.splitlines(keepends=True),
                        fromfile=str(label),
                        tofile=str(label),
                    )
                )
            )
            continue
        atomic_write(path, text)
        detail = ""
        if path == FEED_PATH:
            detail = f" ({len(json.loads(text)['items'])} items)"
        print(f"refreshed {label}{detail}")

    if not drifted:
        print(f"all {len(targets)} live-fact surfaces are already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


#!/usr/bin/env python3
"""
Polymarket US V3.4 Backfill & Diagnostics Updater

Updates scanner_plus.py in place.
Creates a backup, validates syntax, and preserves state.

No API calls, webhook messages, or trading operations.
"""

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "scanner_plus.py"
BACKUP = ROOT / "scanner_plus_v33_backup.py"

def fail(message):
    print("UPGRADE STOPPED:", message)
    print("No scanner changes were written.")
    sys.exit(1)

def replace_once(source, old, new, label):
    count = source.count(old)
    if count != 1:
        fail(f"{label}: expected one matching section, found {count}")
    return source.replace(old, new, 1)

def upgrade():
    if not TARGET.is_file():
        fail("scanner_plus.py was not found in this folder")

    source = TARGET.read_text(encoding="utf-8")

    if "V34_BACKFILL_UPGRADE" in source:
        print("V3.4 upgrade is already installed.")
        return

    # 1. Add a durable overflow queue for discovered contracts.
    old = """    stats = Counter()
    # V3.2 skipped"""

    new = """    stats = Counter()

    # V34_BACKFILL_UPGRADE
    # Preserve resolved candidates discovered while the main queue is full.
    overflow = state.setdefault("v34_discovery_overflow", [])
    if not isinstance(overflow, list):
        raise ValueError("Invalid V3.4 discovery overflow state")

    # V3.2 skipped"""

    source = replace_once(source, old, new, "overflow initialization")

    # 2. Drain saved overflow candidates before requesting more markets.
    old = """    queue[:] = safe_queue[:V32_MAX_QUEUE]

    # Discover closed markets"""

    new = """    queue[:] = safe_queue[:V32_MAX_QUEUE]

    # Move saved discovery candidates into available queue slots.
    known = set(archive) | set(skipped)
    known.update(x["slug"] for x in queue)
    remaining_overflow = []

    for item in overflow:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        if slug in known:
            continue
        known.add(slug)
        if len(queue) < V32_MAX_QUEUE:
            queue.append(item)
            stats["overflow_requeued"] += 1
        else:
            remaining_overflow.append(item)

    overflow[:] = remaining_overflow
    stats["overflow_remaining"] = len(overflow)

    # Discover closed markets"""

    source = replace_once(source, old, new, "overflow recovery")

    # 3. Permit new discovery whenever room exists, but avoid
    # requesting another page until saved overflow is drained.
    old = """    can_list = len(queue) <= V32_MAX_QUEUE - 100"""

    new = """    can_list = len(queue) < V32_MAX_QUEUE and not overflow"""

    source = replace_once(source, old, new, "discovery scheduling")

    # 4. Record all discovered category labels.
    old = """            queued_slugs = {x['slug'] for x in queue}
            for m in listing:"""

    new = """            queued_slugs = {x['slug'] for x in queue}
            queued_slugs.update(
                x["slug"] for x in overflow
                if isinstance(x, dict) and isinstance(x.get("slug"), str)
            )
            listed_categories = Counter()
            for m in listing:"""

    source = replace_once(source, old, new, "discovery diagnostics")

    # 5. Replace queue insertion with lossless overflow handling.
    old = """                if len(queue) >= V32_MAX_QUEUE:
                    stats['queue_at_capacity'] += 1
                    break
                queue.append({'slug':slug,'category':base.category_of(m),
                              'event':event_group(m),
                              'startDate':m.get('startDate') or m.get('createdAt'),
                              'endDate':m.get('endDate'),
                              'question':clean_text(m.get('question'),120),
                              'attempts':0})
                queued_slugs.add(slug)
                stats['queued'] += 1"""

    new = """                category = base.category_of(m)
                listed_categories[category] += 1

                candidate = {
                    'slug': slug,
                    'category': category,
                    'event': event_group(m),
                    'startDate': m.get('startDate') or m.get('createdAt'),
                    'endDate': m.get('endDate'),
                    'question': clean_text(
                        m.get('question') or m.get('title'), 120
                    ),
                    'attempts': 0,
                }

                if len(queue) < V32_MAX_QUEUE:
                    queue.append(candidate)
                    stats['queued'] += 1
                else:
                    overflow.append(candidate)
                    stats['overflow_added'] += 1

                queued_slugs.add(slug)

            stats['listed_categories'] = dict(listed_categories)
            stats['overflow_remaining'] = len(overflow)"""

    source = replace_once(source, old, new, "lossless discovery")

    # 6. Prioritize fresh metadata-complete candidates.
    old = """        index,m = min(ready,key=lambda im:(
            archived_by_category[im[1].get('category','other')] +
            picked_by_category[im[1].get('category','other')], im[0]))"""

    new = """        index,m = min(ready,key=lambda im:(
            bool(im[1].get("needs_metadata")),
            archived_by_category[im[1].get('category','other')] +
            picked_by_category[im[1].get('category','other')],
            im[0]))"""

    if old in source:
        source = replace_once(source, old, new, "candidate priority")

    # 7. Count fresh candidates separately from metadata rechecks.
    old = """        stats['attempted'] += 1
        try:"""

    new = """        stats['attempted'] += 1
        if m.get("needs_metadata"):
            stats["attempted_recheck"] += 1
        else:
            stats["attempted_fresh"] += 1
        try:"""

    if old in source:
        source = replace_once(source, old, new, "attempt diagnostics")

    # 8. Preserve the queue diagnostics for Discord and Actions logs.
    old = """    stats['skipped_total'] = len(skipped)"""

    new = """    stats['skipped_total'] = len(skipped)
    stats['overflow_remaining'] = len(overflow)
    stats['queue_capacity'] = V32_MAX_QUEUE"""

    source = replace_once(source, old, new, "queue statistics")

    # 9. Add a compact status block without changing the old
    # digest header or statistical signal format.
    old = """    history_reasons = [(k.replace('history_', ''), v) for k, v in v31_stats.items()"""

    new = """    if "V34_BACKFILL_UPGRADE" and v31_stats:
        queue_cats = v31_stats.get("queue_categories", {})
        archive_cats = v31_stats.get("archive_categories", {})
        discovered = v31_stats.get("listed_categories", {})

        def category_text(data):
            if not isinstance(data, dict) or not data:
                return "none reported"
            return ", ".join(
                f"{k}={v}" for k, v in
                sorted(data.items(), key=lambda item: -item[1])[:4]
            )

        header.append(
            f"Backfill: fresh {v31_stats.get('attempted_fresh', 0)} | "
            f"rechecks {v31_stats.get('attempted_recheck', 0)} | "
            f"overflow {v31_stats.get('overflow_remaining', 0)}"
        )
        header.append(
            "Historical queue categories: " + category_text(queue_cats)
        )
        if discovered:
            header.append(
                "Latest catalog page: " + category_text(discovered)
            )

    history_reasons = [(k.replace('history_', ''), v) for k, v in v31_stats.items()"""

    source = replace_once(source, old, new, "Discord diagnostics")

    # Check syntax before touching the working scanner.
    try:
        compile(source, str(TARGET), "exec")
    except SyntaxError as exc:
        fail(f"generated Python failed syntax validation: {exc}")

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)

    temp = TARGET.with_suffix(".py.v34tmp")
    temp.write_text(source, encoding="utf-8")
    temp.replace(TARGET)

    print()
    print("POLYMARKET US V3.4 UPDATE INSTALLED")
    print("---------------------------------")
    print("Historical queue overflow protection: enabled")
    print("Category discovery diagnostics: enabled")
    print("Fresh candidate priority: enabled")
    print("Discord backfill diagnostics: enabled")
    print("Validation thresholds: unchanged")
    print("Existing state.json: untouched")
    print("Trading functionality: none added")
    print()
    print("Backup:", BACKUP.name)
    print("Scanner:", TARGET.name)
    print()
    print("Next: python3 test_v33.py")


if __name__ == "__main__":
    upgrade()

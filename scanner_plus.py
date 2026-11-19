#!/usr/bin/env python3
"""Quality-of-data upgrade for the existing read-only Polymarket US scanner.

This wrapper deliberately reuses market_alerts.py for state, model validation,
settlement retrieval, watchlist alerts and Discord delivery. It adds no trading.
Run with: python3 scanner_plus.py --max-pages 5 --max-bbo 25 --min-volume 50
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
import time
from urllib.parse import quote

import market_alerts as core


def market_name(m):
    """Prefer actual market question to the frequently abbreviated titleShort."""
    parts = []
    for key in ("question", "title", "subtitle", "titleShort"):
        value = m.get(key)
        if isinstance(value, str):
            value = " ".join(value.split())
            if value and value.casefold() not in {p.casefold() for p in parts}:
                parts.append(value)
    if not parts:
        return str(m.get("slug", "Unknown contract"))[:180]
    first = parts[0]
    # Market metadata can split the question between title and subtitle.
    if len(first) < 28 and len(parts) > 1:
        second = parts[1]
        if second.casefold() not in first.casefold():
            first += " — " + second
    return first[:180]


def reported_volume(m):
    """None means unavailable, NOT zero traded volume."""
    raw = m.get("volume24hr")
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


def volume_text(m):
    value = reported_volume(m)
    return "not reported" if value is None else f"{value:,.0f}"


def choose_candidates(markets, *, min_volume=50, max_spread=0.08, limit=40):
    """Allow unknown-volume contracts through to quote discovery; rotate sampling.

    A low or unknown-volume contract is explored but never represented as
    liquid without book evidence. Reported high-volume contracts come first.
    """
    groups = defaultdict(list)
    for m in markets:
        if not core.eligible_market(m):
            continue
        vol = reported_volume(m)
        # Low or zero reported volume does NOT block discovery. It only
        # deprioritizes the market: volume may be stale or incomplete.
        listing_quote = core.list_quote(m)
        if listing_quote is not None and listing_quote["spread"] > max_spread:
            continue
        groups[core.category_of(m)].append(m)
    if not groups:
        return []

    # Keep high reported-volume markets prioritized and sample others over time.
    # Rotation makes the collector gradually expand its historical sample set.
    bucket = int(time.time() // 21600)  # new window every six hours
    for category, rows in groups.items():
        known = [m for m in rows if reported_volume(m) is not None and reported_volume(m) >= min_volume]
        unknown = [m for m in rows if reported_volume(m) is None]
        low_volume = [m for m in rows if reported_volume(m) is not None and reported_volume(m) < min_volume]
        known.sort(key=lambda m: (-reported_volume(m), m["slug"]))
        low_volume.sort(key=lambda m: (-reported_volume(m), m["slug"]))
        unknown.sort(key=lambda m: m["slug"])
        if unknown:
            offset = (bucket * max(1, limit // max(1, len(groups)))) % len(unknown)
            unknown = unknown[offset:] + unknown[:offset]
        groups[category] = known + unknown + low_volume

    # Round-robin across categories to avoid sampling only one sector.
    ordered_categories = sorted(groups, key=lambda c: (-len(groups[c]), c))
    selected = []
    while len(selected) < limit:
        progress = False
        for category in ordered_categories:
            if groups[category] and len(selected) < limit:
                selected.append(groups[category].pop(0))
                progress = True
        if not progress:
            break
    return selected



def top_ask_quantity(slug, shown_ask):
    """Return actual quantity offered at the shown best ask, when obtainable."""
    try:
        data = core.request_json("/v1/markets/" + quote(slug, safe="") + "/book")
        book = data.get("marketData", {}) if isinstance(data, dict) else {}
        levels = book.get("offers", [])
        if not isinstance(levels, list):
            return None
        qty = 0.0
        matched = False
        for level in levels:
            if not isinstance(level, dict):
                continue
            px = core.price(level.get("px"))
            try:
                shares = float(level.get("qty"))
            except (ValueError, TypeError):
                continue
            if px is not None and abs(px - shown_ask) < 0.0001 and math.isfinite(shares) and shares >= 0:
                qty += shares
                matched = True
        return qty if matched else None
    except Exception:  # Degrade the *optional* liquidity detail; keep scanning.
        return None


def alert_text(slug, m, q, descriptions, *, watched=False, note=""):
    label = "WATCHLIST" if watched else "MARKET ALERT"
    lines = [f"**POLYMARKET US · {label}**", market_name(m),
             f"Market slug: `{slug}`", *[f"• {d}" for d in descriptions],
             f"YES bid ${q['bid']:.2f} | ask ${q['ask']:.2f} | spread ${q['spread']:.2f}",
             f"24h volume: {volume_text(m)}"]
    if note:
        lines.append(f"Note: {note[:160]}")
    lines.append("Research only; check full resolution rules, actual book depth and fees. NO TRADE PLACED.")
    return "\n".join(lines)


def market_digest(ranked, quoted, samples, models, *, timestamp):
    """A market research digest, never labeled profitable or liquid without proof."""
    all_rows = [m for m in ranked if m.get("slug") in quoted]
    # Volume first when available; then narrower spreads. Never rank by cheap ask.
    all_rows.sort(key=lambda m: (
        reported_volume(m) is not None,
        reported_volume(m) or 0,
        -quoted[m["slug"]]["spread"],
    ), reverse=True)
    verified = sum(s.get("result") in (0, 1) for s in samples.values())
    lines = [f"**POLYMARKET US · RESEARCH SCAN** ({timestamp})",
             f"Tracked {len(samples)} unique markets · verified outcomes {verified} · validated categories {len(models)}",
             "**Live quotes to research (NOT picks):**"]
    if not all_rows:
        lines.append("No eligible live quotes in this scan. Review API/filters.")
    for i, m in enumerate(all_rows[:5], 1):
        slug = m["slug"]
        q = quoted[slug]
        size = top_ask_quantity(slug, q["ask"])
        depth = (f"{size:,.0f} contracts offered at ask" if size is not None
                 else "ask-level quantity unavailable")
        name = market_name(m)
        lines.append(f"{i}. **{name[:120]}** ({core.category_of(m)})\n"
                     f"   YES bid ${q['bid']:.2f} | ask ${q['ask']:.2f} | spread ${q['spread']:.2f}\n"
                     f"   24h volume {volume_text(m)} | {depth}\n"
                     f"   `{slug}`")
    if not models:
        lines.append("No statistically validated category yet. These are NOT value picks.")
    else:
        lines.append("Validated categories exist; only separate validated edge alerts include model estimates.")
    lines.append("Read-only · No trades · Verify settlement, fees, and executable depth.")
    return "\n".join(lines)[:core.MAX_MESSAGE]


# Change only display/discovery functions; preserve the existing outcome-trained
# model and its original strict holdout validation without loosening thresholds.
core.market_name = market_name
core.choose_candidates = choose_candidates
core.alert_text = alert_text
core.market_digest = market_digest


if __name__ == "__main__":
    core.main()

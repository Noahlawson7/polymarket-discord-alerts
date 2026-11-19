#!/usr/bin/env python3
"""Read-only market-quality and longitudinal research upgrade for Polymarket US.

This file runs alongside market_alerts.py in the same GitHub repository.
It preserves the original model, settlement checks, Discord webhook and state.
No trade execution. Python standard library only.

GitHub Actions command example:
python3 scanner_upgrade.py --max-pages 5 --max-bbo 30 --min-volume 0 --max-spread 0.08 --report-now
"""
from __future__ import annotations

import math
import sys
from urllib.parse import quote
from urllib.error import HTTPError, URLError

import market_alerts as base

# Reuse existing settled-outcome model, alert detection and state helpers.
# Replace only the scan orchestration to avoid noisy NEW MARKET alerts.
_session = {"cursor": 0, "next_cursor": 1, "candidates": 0}


def maybe_nonnegative(value):
    """Number if present and valid, otherwise None (NOT zero)."""
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def market_name(m):
    """Prioritize the full market question, not its abbreviated UI title."""
    for key in ("question", "title"):
        value = m.get(key)
        if isinstance(value, str) and value.strip():
            main = " ".join(value.split())
            sub = m.get("subtitle")
            if key == "title" and isinstance(sub, str) and sub.strip():
                sub = " ".join(sub.split())
                if sub.lower() != main.lower():
                    main += " — " + sub
            return main[:240]
    return str(m.get("titleShort") or m.get("slug") or "Untitled market")[:240]


def fetch_bbo(slug):
    """Fetch real BBO, open interest and depth WITHOUT manufacturing values."""
    payload = base.request_json("/v1/markets/" + quote(slug, safe="") + "/bbo")
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("state") not in (None, "", "MARKET_STATE_OPEN"):
        return None
    bid = base.price(data.get("bestBid"))
    ask = base.price(data.get("bestAsk"))
    if bid is None or ask is None or not (0 < ask < 1 and 0 <= bid <= ask):
        return None
    return {
        "bid": bid,
        "ask": ask,
        "spread": round(ask - bid, 6),
        "open_interest": maybe_nonnegative(data.get("openInterest")),
        "ask_shares": maybe_nonnegative(data.get("askShares")),
        "bid_shares": maybe_nonnegative(data.get("bidShares")),
        "ask_depth": maybe_nonnegative(data.get("askDepth")),
        "bid_depth": maybe_nonnegative(data.get("bidDepth")),
    }


def fetch_book_metrics(slug, target_shares=10):
    """Validate executable YES cost using the official /book offers.

    Price is in dollars per share, qty is number of shares. This models a
    hypothetical sweep of visible resting offers only; not an order preview,
    and does not include fees or moving-book / execution risk.
    """
    payload = base.request_json("/v1/markets/" + quote(slug, safe="") + "/book")
    data = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or data.get("state") not in (None, "", "MARKET_STATE_OPEN"):
        return None
    raw_offers = data.get("offers")
    if not isinstance(raw_offers, list):
        return None
    offers = []
    for level in raw_offers:
        if not isinstance(level, dict):
            continue
        px = maybe_nonnegative(level.get("px"))
        qty = maybe_nonnegative(level.get("qty"))
        if px is not None and 0 < px < 1 and qty is not None and qty > 0:
            offers.append((px, qty))
    if not offers:
        return {"available": 0, "best_offer": None, "vwap": None,
                "fully_fillable": False, "levels": 0, "impact": None}
    offers.sort(key=lambda x: x[0])
    remaining = float(target_shares)
    filled, spent = 0., 0.
    for px, qty in offers:
        units = min(qty, remaining)
        filled += units
        spent += units * px
        remaining -= units
        if remaining <= 1e-9:
            break
    is_full = remaining <= 1e-9
    vwap = spent / filled if filled > 0 else None
    return {"available": sum(q for _, q in offers),
            "best_offer": offers[0][0], "vwap": vwap,
            "fully_fillable": is_full, "levels": len(offers),
            "impact": max(0, vwap - offers[0][0]) if vwap is not None else None,
            "filled": filled, "target": float(target_shares)}


def record_price(state, slug, q, *, timestamp):
    """Retain a small, capped time series (no fabricated historical quotes)."""
    all_history = state.setdefault("upgrade_price_history", {})
    history = all_history.setdefault(slug, [])
    history = [x for x in history if isinstance(x, dict)
               and maybe_nonnegative(x.get("t")) is not None
               and timestamp - float(x["t"]) <= 24 * 3600]
    if not history or timestamp - float(history[-1]["t"]) >= 270:
        history.append({"t": timestamp, "ask": q["ask"], "bid": q["bid"]})
    # Keep enough samples for genuine price movement checks without JSON bloat.
    all_history[slug] = history[-100:]
    if len(all_history) > 550:
        oldest = sorted(all_history, key=lambda k: all_history[k][-1].get("t", 0)
                        if all_history[k] else 0)
        for victim in oldest[:len(all_history) - 550]:
            del all_history[victim]


def observed_movement(state, slug, current_ask, *, timestamp):
    """Return change vs recorded ask at least 30 min old; not implied odds."""
    samples = state.get("upgrade_price_history", {}).get(slug, [])
    old = [x for x in samples if 1800 <= timestamp - float(x.get("t", timestamp)) <= 21600
           and maybe_nonnegative(x.get("ask")) is not None]
    if not old:
        return None
    # Prefer approximately one hour ago, with a transparent elapsed time.
    previous = min(old, key=lambda x: abs(timestamp - float(x["t"]) - 3600))
    elapsed = int((timestamp - float(previous["t"])) / 60)
    return {"delta": round(current_ask - float(previous["ask"]), 5), "minutes": elapsed}


def rotating_candidates(markets, *, min_volume=0, max_spread=0.08, limit=30):
    """Rotate through more than the first 10 markets across successive scans.

    Volume is used for prioritization *only* when the API supplies it. This
    is a research discovery strategy, not an edge/probability estimate.
    """
    groups = {}
    for m in markets:
        if not base.eligible_market(m):
            continue
        vol = maybe_nonnegative(m.get("volume24hr"))
        if min_volume > 0 and (vol is None or vol < min_volume):
            continue
        listed_quote = base.list_quote(m)
        if listed_quote is not None and listed_quote["spread"] > max_spread:
            continue
        groups.setdefault(base.category_of(m), []).append(m)
    if not groups:
        _session["candidates"] = 0
        return []

    def rank(m):
        vol = maybe_nonnegative(m.get("volume24hr"))
        q = base.list_quote(m)
        return (vol if vol is not None else -1, -(q["spread"] if q else 1))

    categories = sorted(groups)
    stride = max(1, (limit + len(categories) - 1) // len(categories))
    cursor = _session["cursor"]
    for category in categories:
        group = sorted(groups[category], key=rank, reverse=True)
        # Rotating offset gives each category new research candidates on the
        # next run instead of repeatedly sampling the same handful of symbols.
        offset = (cursor * stride) % len(group)
        groups[category] = group[offset:] + group[:offset]

    chosen = []
    while len(chosen) < limit and any(groups.values()):
        for category in categories:
            if len(chosen) >= limit:
                break
            if groups[category]:
                chosen.append(groups[category].pop(0))
    _session["candidates"] = len(chosen)
    _session["next_cursor"] = cursor + 1
    return chosen


def fmt(value, *, decimals=0):
    return "unavailable" if value is None else f"{value:,.{decimals}f}"


def digest_picks(selected, quoted, *, limit=5):
    """Prioritize available ask quantity while diversifying market categories."""
    valid = [m for m in selected if m["slug"] in quoted]
    def rank(m):
        q = quoted[m["slug"]]
        v = maybe_nonnegative(m.get("volume24hr"))
        return (q["ask_shares"] if q["ask_shares"] is not None else -1,
                v if v is not None else -1, -q["spread"])
    sorted_valid = sorted(valid, key=rank, reverse=True)
    top, categories = [], set()
    for m in sorted_valid:
        cat = base.category_of(m)
        if cat not in categories:
            top.append(m)
            categories.add(cat)
            if len(top) >= limit:
                return top
    for m in sorted_valid:
        if m not in top:
            top.append(m)
            if len(top) >= limit:
                break
    return top


def market_digest(ranked, quoted, samples, models, *, timestamp, book_details=None,
                  movement=None, diagnostics=None):
    """Bounded Discord digest: market detail + actual depth + honest training status."""
    book_details = book_details or {}
    movement = movement or {}
    diagnostics = diagnostics or {}
    settled = sum(s.get("result") in (0, 1) for s in samples.values())
    pending = sum(s.get("result") is None for s in samples.values())
    best = digest_picks(ranked, quoted)
    lines = [
        f"**POLYMARKET US · MARKET RESEARCH** ({timestamp})",
        f"BBO usable: {len(quoted)}/{_session['candidates']} checked "
        f"| samples: {len(samples)} ({settled} settled, {pending} pending) "
        f"| validated categories: {len(models)}",
        "**Research watchlist — NOT winning picks:**",
    ]
    if diagnostics:
        lines.append(f"Filters: {diagnostics.get('wide', 0)} wide spreads; "
                     f"{diagnostics.get('missing', 0)} invalid/unavailable quotes; "
                     f"{diagnostics.get('errors', 0)} request errors")
    tail = ("No model-backed pick unless historical holdout validation passes. "
            "Figures exclude fees; check market rules. Read-only, NO TRADES.")
    def format_row(i, m):
        q = quoted[m["slug"]]
        name = market_name(m).replace("`", "'").replace("@", "＠")
        if len(name) > 128:
            name = name[:127] + "…"
        v24 = maybe_nonnegative(m.get("volume24hr"))
        total = maybe_nonnegative(m.get("volume"))
        end = m.get("endDate")
        when = f" | ends {end[:10]}" if isinstance(end, str) and len(end) >= 10 else ""
        bl = book_details.get(m["slug"])
        details = [f"{i}. **{name}** [{base.category_of(m)}]{when}",
                   f"   YES ${q['bid']:.3f} bid / ${q['ask']:.3f} ask "
                   f"(spread ${q['spread']:.3f})",
                   f"   Ask shares at best: {fmt(q['ask_shares'])} "
                   f"| OI: {fmt(q['open_interest'])} "
                   f"| 24h vol: {fmt(v24)} "
                   f"| total vol: {fmt(total)}"]
        if bl is not None:
            if bl["fully_fillable"]:
                details.append(f"   10-share book VWAP: ${bl['vwap']:.3f} "
                               f"(visible slippage +${bl['impact']:.3f}/share, fees excluded)")
            else:
                details.append(f"   10-share book depth: insufficient "
                               f"(visible offers {fmt(bl['available'], decimals=1)} shares)")
        if m["slug"] in movement:
            mv = movement[m["slug"]]
            details.append(f"   Ask change vs {mv['minutes']}m ago: "
                           f"{mv['delta']:+.3f} (price, NOT a probability model)")
        return "\n".join(details)

    message = "\n".join(lines)
    displayed = 0
    for i, m in enumerate(best, 1):
        row = format_row(i, m)
        if len(message) + len(row) + len(tail) + 8 > base.MAX_MESSAGE:
            break
        message += "\n" + row
        displayed += 1
    if not best:
        message += "\nNo eligible quotes. Inspect filters and API errors in Actions."
    elif displayed < len(best):
        message += f"\n({len(best) - displayed} additional market(s) omitted to fit Discord)"
    message += "\n" + tail
    return message[:base.MAX_MESSAGE]


def alert_text(slug, m, q, descriptions, *, watched=False, note=""):
    label = "WATCHLIST" if watched else "BROAD SCAN"
    lines = [f"**POLYMARKET US · {label}**", market_name(m)[:220],
             f"Market: `{slug}`", *["• " + str(d) for d in descriptions],
             f"YES bid ${q['bid']:.3f} | ask ${q['ask']:.3f} | spread ${q['spread']:.3f}",
             f"Ask shares: {fmt(q.get('ask_shares'))} | open interest: {fmt(q.get('open_interest'))}"]
    if note:
        lines.append("Note: " + note[:160])
    lines.append("Read-only research. Verify rules, depth and fees. NO TRADE PLACED.")
    return "\n".join(lines)[:base.MAX_MESSAGE]


def scan_once(args):
    """Read-only scan; only signals and periodic digests reach Discord.

    Existing state.json and the original strictly gated statistical model are
    reused. New-market discovery by itself does NOT generate an alert.
    """
    state = base.load_state(args.state)
    watch = base.read_watchlist(args.watchlist)
    raw = base.list_markets(args.max_pages, args.page_size)
    markets = {m["slug"]: m for m in raw if base.eligible_market(m)}
    if not markets:
        raise RuntimeError("No active markets returned; preserving prior state")

    try:
        cursor = max(0, int(state.get("upgrade_cursor", 0)))
    except (ValueError, TypeError):
        cursor = 0
    _session.update({"cursor": cursor, "next_cursor": cursor + 1, "candidates": 0})
    selected = rotating_candidates(list(markets.values()), min_volume=args.min_volume,
                                   max_spread=args.max_spread, limit=args.max_bbo)
    slugs = list(dict.fromkeys([*watch, *(m["slug"] for m in selected)]))
    _session["candidates"] = len(slugs)

    samples = state.setdefault("training_samples", {})
    observations = state.setdefault("observations", {})
    settle_count = base.settle_matured_samples(samples, check_limit=15)
    models = {}
    for category in {s.get("category") for s in samples.values()}:
        fitted = base.model_summary(samples, category)
        if fitted:
            models[category] = fitted

    quoted, proposed_updates, notifications, errors = {}, {}, [], []
    diagnostics = {"missing": 0, "wide": 0, "errors": 0}
    movements = {}
    book_details = {}
    timestamp = base.now_ts()
    for slug in slugs:
        try:
            q = fetch_bbo(slug)
            if q is None:
                diagnostics["missing"] += 1
                continue
            if q["spread"] > args.max_spread:
                diagnostics["wide"] += 1
                continue
            previous_move = observed_movement(state, slug, q["ask"], timestamp=timestamp)
            if previous_move is not None:
                movements[slug] = previous_move
            quoted[slug] = q
            record_price(state, slug, q, timestamp=timestamp)
            m = markets.get(slug, {"slug": slug, "title": slug})
            if slug in markets:
                base.track_sample(samples, m, q)
            setting = watch.get(slug, {})
            # A user-supplied unvalidated fair probability must NOT masquerade
            # as the outcome-trained model. Personal price targets are fine.
            safe_settings = {"fair": None, "target": setting.get("target")}
            alerts, proposed = base.examine(
                slug, q, observations.get(slug, {}), safe_settings,
                move=args.move, min_edge=args.min_edge, max_spread=args.max_spread,
                theta=args.fee_theta, execution_buffer=args.buffer,
            )
            # Only raise positive-EV labels when the original out-of-sample
            # validation has passed for this category/price bucket.
            model = models.get(base.category_of(m))
            score = base.statistical_yes_edge(
                model, q["bid"], q["ask"], theta=args.fee_theta, buffer=args.buffer
            )
            value_active = bool(score and score["edge"] >= args.min_edge)
            if value_active and not proposed.get("stat_edge_active", False):
                alerts.append(
                    f"OUT-OF-SAMPLE-VALIDATED VALUE FLAG: estimated lower-bound "
                    f"net edge +${score['edge']:.3f}/contract; calibrated YES probability "
                    f"{score['estimate']:.1%}; lower bound {score['conservative']:.1%}; "
                    f"training-bin n={score['n']} / holdout Brier gain "
                    f"{score['validation']:.4f}. NOT a guaranteed profit."
                )
            proposed["stat_edge_active"] = value_active
            proposed_updates[slug] = proposed
            if alerts:
                notifications.append((slug, m, q, alerts, setting))
        except (ValueError, KeyError, TypeError, HTTPError, URLError,
                RuntimeError, OSError) as exc:
            diagnostics["errors"] += 1
            errors.append(f"{slug[:60]}: {type(exc).__name__}: {str(exc)[:70]}")

    sent = 0
    processed_alerts = set()
    for slug, m, q, alerts, setting in notifications[:args.max_alerts]:
        msg = alert_text(slug, m, q, alerts, watched=slug in watch,
                         note=setting.get("note", ""))
        try:
            if base.post_discord(msg):
                observations[slug] = proposed_updates[slug]
                processed_alerts.add(slug)
                sent += 1
            else:
                errors.append(f"{slug}: Discord declined alert")
        except (RuntimeError, ValueError, OSError) as exc:
            errors.append(f"{slug}: notification error {type(exc).__name__}")
    # Don't consume a signal until its Discord notification is accepted.
    signaled = {n[0] for n in notifications}
    for slug, proposed in proposed_updates.items():
        if slug not in signaled or slug in processed_alerts:
            observations[slug] = proposed

    digest_due = base.now_ts() - base.number(state.get("last_digest", 0)) >= args.digest_hours * 3600
    digest_failed = False
    if args.report_now or digest_due:
        candidate_markets = digest_picks(selected, quoted)
        for m in candidate_markets[:5]:
            try:
                info = fetch_book_metrics(m["slug"], target_shares=10)
                if info is not None:
                    book_details[m["slug"]] = info
            except (ValueError, KeyError, TypeError, HTTPError, URLError,
                    RuntimeError, OSError) as exc:
                errors.append(f"Book {m['slug'][:35]}: {type(exc).__name__}")
        digest = market_digest(selected, quoted, samples, models,
                               timestamp=base.utc_stamp(), book_details=book_details,
                               movement=movements, diagnostics=diagnostics)
        try:
            if base.post_discord(digest):
                state["last_digest"] = base.now_ts()
                sent += 1
            else:
                digest_failed = True
        except (RuntimeError, ValueError, OSError) as exc:
            errors.append(f"Digest failed: {type(exc).__name__}")
            digest_failed = True

    state["initialized"] = True
    state["seen_slugs"] = sorted(set(state.get("seen_slugs", [])).union(markets))
    state["upgrade_cursor"] = _session["next_cursor"]
    cutoff = base.now_ts() - 7 * 86400
    state["observations"] = {
        s: v for s, v in observations.items()
        if base.number(v.get("last_seen"), 0) >= cutoff
    }
    base.save_state(state, args.state)
    settled_total = sum(s.get("result") in (0, 1) for s in samples.values())
    print(
        f"{base.utc_stamp()} | Listed {len(markets)} | BBO valid {len(quoted)}/{len(slugs)} "
        f"| training samples {len(samples)} | settled total {settled_total} "
        f"| newly verified {settle_count} | model categories {len(models)} "
        f"| Discord messages sent {sent} | 10-share book checks {len(book_details)} "
        f"| no quote {diagnostics['missing']} | wide {diagnostics['wide']} "
        f"| errors {diagnostics['errors']} | scan rotation {cursor}->{state['upgrade_cursor']}" +
        (f" | errors: {'; '.join(errors[:5])}" if errors else ""), flush=True
    )
    if args.report_now and digest_failed:
        raise RuntimeError("Requested Discord digest failed; consult Actions log")
    return sent

def main(argv=None):
    base.market_name = market_name
    base.market_digest = market_digest
    base.alert_text = alert_text
    base.choose_candidates = rotating_candidates
    base.scan_once = scan_once
    return base.main(argv)


if __name__ == "__main__":
    sys.exit(main())

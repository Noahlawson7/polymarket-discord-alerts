#!/usr/bin/env python3
"""Polymarket US Scanner V3: identifiable contracts + outcome-backed research.

Drop-in replacement for scanner_plus.py, called by the existing GitHub Action.
Extends market_alerts.py WITHOUT changing its trained model, its strict holdout
requirements, state schema, webhook, or trade execution (none is provided).
Uses the public Polymarket US API. Python standard library only.

Example (your workflow command can remain the same):
python3 scanner_plus.py --max-pages 5 --max-bbo 25 --min-volume 0 --max-spread 0.08 --report-now
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


def clean_text(value, max_length=180):
    """Sanitize untrusted API labels for Discord and remove duplicate whitespace."""
    if not isinstance(value, str):
        return ""
    value = " ".join(value.replace("`", "'").replace("@", "＠").split())
    return value[:max_length]


def market_name(m):
    """Identify the *specific outcome*, not only the umbrella event title.

    Polymarket US markets may have generic event titles with the particular
    player/company in titleShort, subtitle, side/team metadata, or question.
    Displaying the market slug separately is mandatory if these fields are not
    sufficiently descriptive; never guess which player a price refers to.
    """
    primary = clean_text(m.get("question")) or clean_text(m.get("title")) or clean_text(m.get("name"))
    if not primary:
        primary = clean_text(m.get("titleShort")) or "Unlabeled contract"
    parts = [primary]
    for key in ("subtitle", "titleShort", "outcomeLabel", "marketLabel", "selectionName",
                "participantName", "participant", "title", "name"):
        detail = clean_text(m.get(key), 90)
        if detail and detail.casefold() not in primary.casefold() and                 not any(detail.casefold() == p.casefold() for p in parts):
            # Avoid repeating a second generic event title.
            if len(parts) < 3:
                parts.append(detail)
    sides = m.get("marketSides")
    if isinstance(sides, list):
        for side in sides:
            if not isinstance(side, dict) or side.get("long") is not True:
                continue
            team = side.get("team")
            candidates = [side.get("description"),
                          team.get("name") if isinstance(team, dict) else None]
            for raw in candidates:
                detail = clean_text(raw, 90)
                if detail and detail.casefold() not in ("yes", "no", "true", "false")                         and not any(detail.casefold() in p.casefold() for p in parts):
                    if len(parts) < 3:
                        parts.append(detail)
    return " — ".join(parts)[:260]


def enrich_market(m):
    """Optional lookup of exact market metadata for display only, no speculation.

    Failure must not interrupt quote scans or data collection. Called for a
    maximum of five shortlisted markets per digest.
    """
    slug = m.get("slug")
    if not isinstance(slug, str) or not slug:
        return m
    try:
        payload = base.request_json("/v1/market/slug/" + quote(slug, safe=""))
    except (HTTPError, URLError, OSError, ValueError, TypeError, RuntimeError):
        return m
    if not isinstance(payload, dict):
        return m
    detail = payload.get("market") or payload.get("data") or payload
    if not isinstance(detail, dict):
        return m
    if detail.get("slug") not in (None, slug):
        return m
    # The direct lookup is authoritative for specific labels, while the
    # listing remains a fallback for absent fields and category data.
    merged = dict(m)
    for key in ("question", "title", "titleShort", "subtitle", "name", "marketSides",
                "outcomeLabel", "marketLabel", "participantName", "selectionName", "endDate"):
        if detail.get(key):
            merged[key] = detail[key]
    return merged


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

    Volume is used for prioritization *only* when the API supplies it.
    Listed-market spreads are NEVER filtered here: they can be stale or
    incorrectly normalized. Live BBO spread is checked by scan_once().
    This is research discovery, not an edge/probability estimate.
    """
    groups = {}
    for m in markets:
        if not base.eligible_market(m):
            continue
        vol = maybe_nonnegative(m.get("volume24hr"))
        if min_volume > 0 and (vol is None or vol < min_volume):
            continue
        # Do not filter by quote fields in the market-list response.
        # Those fields may be stale, missing, or formatted differently from
        # the live BBO endpoint. In V2 this early spread check silently
        # removed every market, resulting in "BBO usable: 0/0".
        # The real spread restriction is enforced after fetch_bbo() below.
        groups.setdefault(base.category_of(m), []).append(m)
    if not groups:
        _session["candidates"] = 0
        return []

    def rank(m):
        vol = maybe_nonnegative(m.get("volume24hr"))
        # Never use listing-level bid/ask data to gate or rank candidates:
        # use actual BBO responses for all quote-quality decisions.
        return (vol if vol is not None else -1, str(m.get("slug") or ""))

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


def record_research_snapshot(state, slug, market, quote_data, *, timestamp):
    """Keep one immutable opening snapshot per unique slug and later daily prices.

    Repeated quotes on the SAME event are NOT independent outcome labels and
    MUST NOT be used as additional model training rows.
    """
    records = state.setdefault("v3_research_records", {})
    row = records.get(slug)
    if not isinstance(row, dict):
        row = {"first_seen": timestamp, "first_bid": quote_data["bid"],
               "first_ask": quote_data["ask"], "category": base.category_of(market),
               "contract": market_name(market)[:200],
               "end_date": clean_text(market.get("endDate"), 30), "daily": []}
        records[slug] = row
    row["last_seen"] = timestamp
    row["last_bid"] = quote_data["bid"]
    row["last_ask"] = quote_data["ask"]
    # Never overwrite first-seen price. Record at most one mark per UTC day.
    bucket = int(timestamp // 86400)
    daily = row.setdefault("daily", [])
    if not daily or daily[-1].get("day") != bucket:
        daily.append({"day": bucket, "bid": quote_data["bid"], "ask": quote_data["ask"]})
    row["daily"] = daily[-30:]
    # Bound the optional analysis metadata independently of settlement samples.
    if len(records) > 2000:
        victims = sorted(records, key=lambda key: base.number(records[key].get("last_seen"), 0))
        for key in victims[:len(records)-2000]:
            del records[key]


def sync_verified_outcomes(state, samples):
    """Attach verified resolutions to quote records, never derive from prices."""
    records = state.get("v3_research_records", {})
    for slug, sample in samples.items():
        if not isinstance(sample, dict) or slug not in records:
            continue
        result = sample.get("result")
        if result in (0, 1):
            records[slug]["verified_result"] = int(result)


def validated_signal(slug, market, q, models, args):
    """Only existing holdout-validated base model is allowed to estimate P(YES)."""
    model = models.get(base.category_of(market))
    if not model:
        return None
    result = base.statistical_yes_edge(model, q["bid"], q["ask"],
                                       theta=args.fee_theta, buffer=args.buffer)
    if not isinstance(result, dict):
        return None
    try:
        estimate, conservative = float(result["estimate"]), float(result["conservative"])
        edge, n = float(result["edge"]), int(result["n"])
        gain = float(result["validation"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(x) for x in (estimate, conservative, edge, gain)):
        return None
    if not (0 <= conservative <= estimate <= 1 and n > 0 and edge >= args.min_edge):
        return None
    return {"slug": slug, "market": market, "quote": q, "estimate": estimate,
            "conservative": conservative, "edge": edge, "n": n, "gain": gain}


def market_digest(ranked, quoted, samples, models, *, timestamp, book_details=None,
                  movement=None, diagnostics=None, validated=None, history_count=0):
    """V3 digest: exact contract IDs, history counts, honest model estimates."""
    book_details = book_details or {}
    movement = movement or {}
    diagnostics = diagnostics or {}
    validated = validated or []
    settled = sum(isinstance(s, dict) and s.get("result") in (0, 1) for s in samples.values())
    pending = sum(isinstance(s, dict) and s.get("result") is None for s in samples.values())
    top = digest_picks(ranked, quoted, limit=5)
    header = [
        f"**POLYMARKET US · V3 RESEARCH** ({timestamp})",
        f"Live quotes {len(quoted)}/{_session['candidates']} | unique samples "
        f"{len(samples)} ({settled} settled, {pending} pending) | long-term records {history_count}",
        f"Validated categories {len(models)} | bad quotes {diagnostics.get('missing',0)} "
        f"| wide spreads {diagnostics.get('wide',0)} | API errors {diagnostics.get('errors',0)}",
    ]
    if validated:
        header.append("**HOLDOUT-VALIDATED VALUE SIGNALS (NOT GUARANTEED):**")
        for signal in sorted(validated, key=lambda x:x["edge"], reverse=True)[:2]:
            label = market_name(signal['market'])[:105]
            header.append(
                f"• **{label}** | `{clean_text(signal['slug'], 90)}`\n"
                f"  YES ask ${signal['quote']['ask']:.3f} | calibrated P(YES) "
                f"{signal['estimate']:.1%} (lower {signal['conservative']:.1%})\n"
                f"  Estimated net edge +${signal['edge']:.3f}/share using configured fee/buffer "
                f"| bin n={signal['n']} | holdout Brier gain {signal['gain']:.4f}"
            )
    else:
        header.append("**Validated value signals:** none; no defensible P(YES) estimate yet.")
    header.append("**Live research watchlist (NOT picks):**")
    tail = ("Historical quotes are NOT independent resolved outcomes. Estimates only "
            "appear after holdout validation. Verify terms and real fees. READ-ONLY; NO TRADES.")
    message = "\n".join(header)
    if not top:
        message += "\nNo qualifying live markets this run."
    shown = 0
    for i, m in enumerate(top, 1):
        slug = m["slug"]
        q = quoted[slug]
        item = enrich_market(m)
        label = market_name(item)[:135]
        end = clean_text(item.get("endDate"), 10)
        bl = book_details.get(slug)
        detail = f"{i}. **{label}** [{base.category_of(m)}]"
        if end:
            detail += f" ends {end}"
        detail += f"\n   `{clean_text(slug, 110)}` | YES bid ${q['bid']:.3f} / ask ${q['ask']:.3f}"
        detail += f" | ask size {fmt(q.get('ask_shares'))}"
        if bl and bl.get("fully_fillable") and bl.get("vwap") is not None:
            detail += f" | 10-share book VWAP ${bl['vwap']:.3f}"
        elif bl is not None:
            detail += " | 10-share depth insufficient"
        else:
            detail += " | depth not checked"
        mv = movement.get(slug)
        if mv is not None:
            detail += f" | {mv['minutes']}m ask Δ {mv['delta']:+.3f}"
        if len(message) + len(detail) + len(tail) + 4 > base.MAX_MESSAGE:
            break
        message += "\n" + detail
        shown += 1
    if shown < len(top):
        note = f"\n({len(top)-shown} more research markets omitted to fit Discord)"
        if len(message) + len(note) + len(tail) + 3 <= base.MAX_MESSAGE:
            message += note
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
    print(f"Discovery: {len(raw)} listed entries; {len(markets)} eligible; "
          f"{len(selected)} selected; {len(watch)} watchlist; "
          f"{len(slugs)} queued for live BBO", flush=True)
    if not slugs:
        raise RuntimeError(
            "Found eligible markets but queued zero BBO checks; "
            "check market eligibility and selection limits"
        )

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
    validated = []
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
                record_research_snapshot(state, slug, m, q, timestamp=timestamp)
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
            score = validated_signal(slug, m, q, models, args)
            if score:
                validated.append(score)
            value_active = score is not None
            if value_active and not proposed.get("stat_edge_active", False):
                alerts.append(
                    f"OUT-OF-SAMPLE-VALIDATED VALUE FLAG: estimated lower-bound "
                    f"net edge +${score['edge']:.3f}/contract; calibrated YES probability "
                    f"{score['estimate']:.1%}; lower bound {score['conservative']:.1%}; "
                    f"training-bin n={score['n']} / holdout Brier gain "
                    f"{score['gain']:.4f}. NOT a guaranteed profit."
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
                               movement=movements, diagnostics=diagnostics,
                               validated=validated,
                               history_count=len(state.get("v3_research_records", {})))
        try:
            if base.post_discord(digest):
                state["last_digest"] = base.now_ts()
                sent += 1
            else:
                digest_failed = True
        except (RuntimeError, ValueError, OSError) as exc:
            errors.append(f"Digest failed: {type(exc).__name__}")
            digest_failed = True

    sync_verified_outcomes(state, samples)
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
        f"| errors {diagnostics['errors']} | scan rotation {cursor}->{state['upgrade_cursor']} "
        f"| V3 records {len(state.get('v3_research_records', {}))} | validated flags {len(validated)}" +
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


#!/usr/bin/env python3
"""Polymarket US Scanner V3.2: faster backfill + conservatively calibrated probabilities.

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
import os
import time
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
    """Backward-compatible API for tests and call sites."""
    q, reason = quote_with_reason(slug)
    return q


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


# ---------------- V3.2: VERIFIED HISTORICAL CALIBRATION ----------------
# Public US API supplies confirmed settlement and book-derived price history.
# One immutable pre-event quote per resolved contract, NOT every price tick.
from datetime import datetime, timezone
from collections import Counter
from urllib.parse import urlencode, urlparse

# Backward-compatible state keys: v31_archive, v31_queue, v31_offset, etc.
V31_MAX_ARCHIVE = 3000
V31_MIN_TRAIN = 100
V31_MIN_HOLDOUT = 40
V31_MIN_BIN_TRAIN = 25
V31_MIN_BIN_HOLDOUT = 12
V31_PRIOR_STRENGTH = 35.0
V31_HOLDOUT_GAIN = .003
V31_BIN_GAIN = .002
# ~99.67% one-sided normal quantile, conservative across 15 possible cells.
V32_MULTIPLE_TEST_Z = 2.72
V32_MAX_QUEUE = 350
V32_LIST_SECONDS = 900
V32_MAX_ATTEMPTS = 3
V32_DEFAULT_BACKFILL = 12


def env_int(name, default, low, high):
    try:
        value = int(os.getenv(name, str(default)))
        return min(high, max(low, value))
    except (TypeError, ValueError):
        return default



def iso_epoch(s):
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if d.tzinfo is None:
            return None
        return d.timestamp()
    except (ValueError, OverflowError):
        return None


def resolved_yes(payload):
    # Contract payout is authoritative; no inference from a price of $0.99.
    if not isinstance(payload, dict):
        return None
    p = maybe_nonnegative(payload.get('settlement'))
    if p is None:
        return None
    if abs(p - 1.0) < 1e-8:
        return 1
    if abs(p) < 1e-8:
        return 0
    # Voided, half-paid and nonbinary settlements excluded.
    return None


def is_resolved(m):
    return isinstance(m, dict) and m.get('status') == 'MARKET_STATUS_RESOLVED'


def event_group(m):
    for k in ('eventId', 'eventSlug'):
        v = m.get(k)
        if isinstance(v, (int, str)) and str(v).strip():
            return f'{k}:{v}'
    name = clean_text(m.get('title') or m.get('question'), 180).casefold()
    end = clean_text(m.get('endDate'), 10)
    # Treat alternatives sharing the same event question/deadline as ONE event.
    return 'q:' + (name or str(m.get('slug'))) + ':' + end


def historical_point(payload, m):
    """Safely choose an EARLY, historic book quote with documented provenance.

    End date is not guaranteed to equal actual resolution time; first quote
    close to listing start is deliberately used. If early-quote provenance is
    missing, skip instead of manufacturing look-ahead-free training data.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get('history'), list):
        return None
    start = iso_epoch(m.get('startDate') or m.get('createdAt'))
    end = iso_epoch(m.get('endDate'))
    if start is None or end is None or end - start < 3 * 86400:
        return None
    rows = []
    raw_history = payload['history']
    for pt in raw_history:
        if not isinstance(pt, dict):
            continue
        ts = maybe_nonnegative(pt.get('timestamp'))
        a = maybe_nonnegative(pt.get('longPrice'))
        short_ask = maybe_nonnegative(pt.get('shortPrice'))
        if ts is None or a is None or short_ask is None:
            continue
        b = 1.0 - short_ask
        if (start - 3600 <= ts <= min(start + 7 * 86400, end - 2 * 86400)
                and 0.02 <= b <= a <= 0.98 and a - b <= .10):
            rows.append((ts, b, a))
    if not rows:
        return None
    # Reject series with no history after the proposed snapshot: may be
    # snapshots taken AFTER early resolution despite a future 'endDate'.
    max_ts = max((maybe_nonnegative(x.get('timestamp')) or 0)
                 for x in raw_history if isinstance(x, dict))
    ts, b, a = min(rows, key=lambda r: r[0])
    if max_ts - ts < 86400:
        return None
    return {'t': int(ts), 'bid': round(b, 6), 'ask': round(a, 6),
            'p': round((a + b) / 2.0, 6), 'spread': round(a - b, 6)}


def backfill_historical(state, *, now, items_per_run=None):
    """Bounded, incremental historical import. Preserves every existing V3.1 key.

    A requested import is NOT guaranteed to produce a training record. Rates and
    data coverage depend on the exchange; skipped/failed cases are explicit.
    All API calls are read-only GETs using base.request_json().
    """
    if items_per_run is None:
        items_per_run = env_int('PM_BACKFILL_PER_RUN', V32_DEFAULT_BACKFILL, 1, 30)
    list_seconds = env_int('PM_BACKFILL_LIST_INTERVAL_SECONDS', V32_LIST_SECONDS, 300, 3600)
    archive = state.setdefault('v31_archive', {})
    queue = state.setdefault('v31_queue', [])
    skipped = state.setdefault('v31_failed', {})
    stats = Counter()
    # Remove malformed/duplicate queue records without losing valid entries.
    known = set(archive) | set(skipped)
    safe_queue = []
    for item in queue:
        if isinstance(item, dict) and isinstance(item.get('slug'), str):
            slug = item['slug']
            if slug and slug not in known:
                safe_queue.append(item)
                known.add(slug)
    queue[:] = safe_queue[:V32_MAX_QUEUE]

    # Throttle discovery if queue has fewer than 100 slots left. Otherwise an
    # offset advance could silently skip resolved contracts forever.
    can_list = len(queue) <= V32_MAX_QUEUE - 100
    if can_list and now - base.number(state.get('v31_last_list'), 0) >= list_seconds:
        offset = int(base.number(state.get('v31_offset'), 0))
        try:
            path = '/v1/markets?' + urlencode({'closed': 'true', 'limit': 100, 'offset': offset})
            body = base.request_json(path)
            listing = body.get('markets') if isinstance(body, dict) else None
            if not isinstance(listing, list):
                raise ValueError('Malformed closed market listing')
            stats['listed'] = len(listing)
            queued_slugs = {x['slug'] for x in queue}
            for m in listing:
                if not is_resolved(m):
                    stats['not_resolved'] += 1
                    continue
                slug = m.get('slug')
                if not isinstance(slug, str) or not slug:
                    continue
                if slug in archive or slug in skipped or slug in queued_slugs:
                    continue
                queue.append({'slug': slug, 'category': base.category_of(m),
                              'event': event_group(m),
                              'startDate': m.get('startDate') or m.get('createdAt'),
                              'endDate': m.get('endDate'),
                              'question': clean_text(m.get('question'), 120),
                              'attempts': 0})
                queued_slugs.add(slug)
                stats['queued'] += 1
            state['v31_offset'] = offset + len(listing) if listing else 0
            state['v31_last_list'] = now
        except (HTTPError, URLError, OSError, RuntimeError, ValueError, TypeError) as exc:
            stats['listing_errors'] += 1
            print(f'V3.2 historic listing error: {type(exc).__name__}: {str(exc)[:100]}', flush=True)
    elif not can_list:
        stats['discovery_paused_queue_full'] = 1

    # A transient 429/5xx should pause the importer, not mark a resolved
    # contract as NO or burn through retries during the same execution.
    if now < base.number(state.get('v32_backoff_until'), 0):
        stats['backoff_seconds'] = int(state['v32_backoff_until'] - now)
        stats['archived'] = len(archive)
        stats['remaining'] = len(queue)
        return dict(stats)

    processed = 0
    count = len(queue)  # Do not retry the same slug in this same scan.
    while queue and processed < items_per_run and count > 0:
        count -= 1
        m = queue.pop(0)
        if base.number(m.get('retry_after'), 0) > now:
            queue.append(m)
            stats['deferred'] += 1
            continue
        slug = m['slug']
        processed += 1
        stats['attempted'] += 1
        try:
            settle = base.request_json('/v1/markets/' + quote(slug, safe='') + '/settlement')
            result = resolved_yes(settle)
            if result is None:
                stats['nonbinary_or_pending'] += 1
                skipped[slug] = 'not_binary_settled'
                continue
            history_url = '/v1/price-history?' + urlencode({
                'symbol': slug, 'fixedInterval': 'INTERVAL_ALL', 'fidelity': 180})
            history = base.request_json(history_url)
            point = historical_point(history, m)
            if point is None:
                stats['no_safe_history'] += 1
                skipped[slug] = 'no_safe_early_quote'
                continue
            archive[slug] = {'category': m['category'], 'event': m['event'],
                             'p': point['p'], 'ask': point['ask'],
                             'bid': point['bid'], 'y': result,
                             't': point['t'], 'end': iso_epoch(m['endDate']),
                             'source': 'us_settlement_and_early_book_history'}
            stats['imported'] += 1
        except (HTTPError, URLError, OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            attempts = int(m.get('attempts', 0)) + 1
            status = getattr(exc, 'code', None)
            stats['record_errors'] += 1
            if (attempts >= V32_MAX_ATTEMPTS and
                (isinstance(exc, HTTPError) and status in (400, 401, 403, 404, 422) or
                 isinstance(exc, (ValueError, KeyError, TypeError)))):
                skipped[slug] = f'error_after_{attempts}_attempts'
                stats['permanent_error'] += 1
            else:
                m['attempts'] = attempts
                wait = min(3600, 60 * 2 ** min(attempts, 5))
                m['retry_after'] = now + wait
                queue.append(m)
                stats['deferred_retry'] += 1
            print(f'V3.2 backfill error {slug[:45]}: {type(exc).__name__}: '
                  f'{str(exc)[:90]}', flush=True)
            if status in (429, 500, 502, 503, 504) or isinstance(exc, (URLError, OSError)):
                state['v32_backoff_until'] = now + min(300, 30 * (2 ** min(attempts, 3)))
                stats['gateway_backoff'] += 1
                break
        # Fixed small gap avoids a burst at 25 requests/sec from one runner.
        # No writes/trading calls are made.
        time.sleep(0.08)
    if len(archive) > V31_MAX_ARCHIVE:
        victims = sorted(archive, key=lambda key: archive[key].get('end') or 0)
        for key in victims[:len(archive) - V31_MAX_ARCHIVE]:
            del archive[key]
    if len(skipped) > 3000:
        for key in list(skipped)[:len(skipped)-3000]:
            del skipped[key]
    stats['archived'] = len(archive)
    stats['remaining'] = len(queue)
    stats['skipped_total'] = len(skipped)
    return dict(stats)


def probability_bucket(p):
    return min(4, max(0, int(p * 5)))


def brier(p, y):
    return (p - y) ** 2


def horizon_bucket(seconds):
    if seconds <= 0:
        return None
    if seconds <= 7 * 86400:
        return 'week'
    if seconds <= 45 * 86400:
        return 'month'
    return 'long'


def calibration_cell(row):
    return (probability_bucket(row['p']), horizon_bucket(row['end']-row['t']))


def clamp_probability(p):
    return max(.001, min(.999, p))


def beta_residual_bin(train, bucket):
    """Bayesian calibration OFFSET, not one probability for a whole wide bin.

    Beta prior is centered on average historical midpoint with strength K.
    The posterior correction is applied to each live midpoint individually:
      predicted P(YES) = live midpoint + posterior historical YES-rate bias.
    This prevents cheap contracts from looking undervalued merely because
    they sit below the average price in the same 20-point probability band.
    """
    rows = [r for r in train if calibration_cell(r) == bucket]
    if len(rows) < V31_MIN_BIN_TRAIN:
        return None
    n = len(rows)
    market_mean = sum(r['p'] for r in rows) / n
    yes = sum(r['y'] for r in rows)
    alpha = .5 + V31_PRIOR_STRENGTH * market_mean + yes
    beta = .5 + V31_PRIOR_STRENGTH * (1 - market_mean) + n - yes
    posterior = alpha / (alpha + beta)
    variance = alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1))
    correction = posterior - market_mean
    # Conservative multiple-comparisons-aware lower bound for the correction.
    correction_lower = correction - V32_MULTIPLE_TEST_Z * math.sqrt(variance)
    return {'shift': correction, 'shift_lower': correction_lower,
            'n': n, 'market_mean': market_mean, 'posterior_yes_rate': posterior}


def paired_gain_stats(differences):
    """Holdout Brier improvement and a cautious lower confidence bound."""
    n = len(differences)
    if n < 2:
        return -1.0, -1.0
    avg = sum(differences) / n
    var = sum((x - avg) ** 2 for x in differences) / (n - 1)
    return avg, avg - 1.645 * math.sqrt(var / n)


def historical_models(archive):
    """Event-deduplicated chronological validation; category-specific cells.

    Only information known before holdout snapshot is used for training.
    Holdout is never used for fitting; a later test can still overfit when
    many cells are repeatedly examined, so a stringent bound is applied.
    """
    models, coverage = {}, {}
    cats = sorted({r.get('category') for r in archive.values() if isinstance(r, dict)
                   and isinstance(r.get('category'), str)})
    for cat in cats:
        rows = []
        for slug, r in archive.items():
            if not isinstance(r, dict) or r.get('category') != cat:
                continue
            try:
                p, t, end = float(r['p']), float(r['t']), float(r['end'])
                y = r['y']
            except (ValueError, KeyError, TypeError, OverflowError):
                continue
            if y not in (0, 1) or not (0 < p < 1 and t < end):
                continue
            rows.append({'slug': slug, 'p': p, 't': t, 'end': end, 'y': int(y),
                         'event': r.get('event') or slug})
        rows.sort(key=lambda r: (r['t'], r['slug']))
        independent, used_events = [], set()
        for r in rows:
            if r['event'] in used_events:
                continue
            used_events.add(r['event'])
            independent.append(r)
        rows = independent
        coverage[cat] = len(rows)
        if len(rows) < V31_MIN_TRAIN + V31_MIN_HOLDOUT:
            continue
        pivot = max(V31_MIN_TRAIN, int(len(rows) * .70))
        cut_time = rows[pivot]['t'] if pivot < len(rows) else None
        if cut_time is None:
            continue
        holdout = [r for r in rows if r['t'] >= cut_time]
        holdout_events = {r['event'] for r in holdout}
        train = [r for r in rows if r['end'] <= cut_time and
                 r['event'] not in holdout_events]
        if len(train) < V31_MIN_TRAIN or len(holdout) < V31_MIN_HOLDOUT:
            continue
        cells = [(b, h) for b in range(5) for h in ('week', 'month', 'long')]
        fitted = {c: beta_residual_bin(train, c) for c in cells}
        scored = [(r, fitted[calibration_cell(r)]) for r in holdout]
        scored = [(r, cal) for r, cal in scored if cal]
        if len(scored) < V31_MIN_HOLDOUT:
            continue
        all_diffs = [brier(r['p'], r['y']) -
                     brier(clamp_probability(r['p'] + cal['shift']), r['y'])
                     for r, cal in scored]
        gain, lower_gain = paired_gain_stats(all_diffs)
        if gain < V31_HOLDOUT_GAIN or lower_gain <= 0:
            continue
        qualifying = {}
        for bucket, cal in fitted.items():
            if cal is None:
                continue
            sub = [r for r, _ in scored if calibration_cell(r) == bucket]
            if len(sub) < V31_MIN_BIN_HOLDOUT:
                continue
            differences = [brier(r['p'], r['y']) -
                           brier(clamp_probability(r['p'] + cal['shift']), r['y'])
                           for r in sub]
            bin_gain, bin_gain_lower = paired_gain_stats(differences)
            if bin_gain < V31_BIN_GAIN or bin_gain_lower <= 0:
                continue
            # This is still only a conditional historical-calibration model,
            # not an independent fundamental forecast for a named contract.
            qualifying[bucket] = dict(cal, holdout_n=len(sub),
                                      bucket_brier_gain=round(bin_gain, 6))
        if qualifying:
            models[cat] = {'buckets': qualifying, 'train_n': len(train),
                           'holdout_n': len(scored), 'brier_gain': round(gain, 6),
                           'gain_lower': round(lower_gain, 6),
                           'brier_raw': round(sum(brier(r['p'],r['y']) for r,c in scored)/len(scored), 6),
                           'brier_calibrated': round(sum(brier(clamp_probability(r['p']+c['shift']),r['y']) for r,c in scored)/len(scored), 6)}
    return models, coverage


def v32_signal(slug, market, q, models, args, *, execution_price=None):
    """Positive YES research flag only after holdout and conservative fee-aware EV.

    Current midpoint is a MARKET input, corrected by verified HISTORICAL
    frequency bias, and is explicitly not a separate event-fundamental model.
    When book VWAP is supplied it replaces optimistic top-of-book ask.
    """
    cat = base.category_of(market)
    model = models.get(cat)
    if not model:
        return None
    mid = (q['bid'] + q['ask']) / 2
    end = iso_epoch(market.get('endDate'))
    if end is None:
        return None
    horizon = horizon_bucket(end - base.now_ts())
    if horizon is None:
        return None
    cell = (probability_bucket(mid), horizon)
    cal = model['buckets'].get(cell)
    if not cal:
        return None
    price = q['ask'] if execution_price is None else execution_price
    if not isinstance(price, (int, float)) or not q['ask'] - .02 <= price <= 1:
        return None
    estimated = clamp_probability(mid + cal['shift'])
    lower = clamp_probability(mid + cal['shift_lower'])
    theta = max(0.0, min(.25, base.number(args.fee_theta, .095)))
    fee = theta * price * (1 - price)
    buffer = max(0., base.number(args.buffer, 0))
    edge = estimated - price - fee - buffer
    lower_edge = lower - price - fee - buffer
    if lower_edge < args.min_edge:
        return None
    return {'slug': slug, 'market': market, 'quote': q, 'estimate': estimated,
            'lower': lower, 'edge': edge, 'lower_edge': lower_edge,
            'execution_price': price, 'approx_fee': fee,
            'correction': cal['shift'], 'bin_n': cal['n'],
            'holdout_n': cal['holdout_n'],
            'brier_gain': model['brier_gain'], 'bin_gain': cal['bucket_brier_gain']}


def quote_with_reason(slug):
    """Return quote/reason; missing quotes are NOT HTTP errors."""
    payload = base.request_json('/v1/markets/' + quote(slug,safe='') + '/bbo')
    data = payload.get('marketData') if isinstance(payload,dict) else None
    if not isinstance(data,dict):
        return None,'missing_marketData'
    if data.get('state') not in (None,'','MARKET_STATE_OPEN'):
        return None,'market_not_open'
    b = base.price(data.get('bestBid'))
    a = base.price(data.get('bestAsk'))
    if b is None and a is None:
        return None,'both_sides_missing'
    if b is None:
        return None,'bid_missing'
    if a is None:
        return None,'ask_missing'
    if not (0 < a < 1 and 0 <= b <= a):
        return None,'crossed_or_out_of_range'
    return {'bid':b,'ask':a,'spread':round(a-b,6),
            'open_interest':maybe_nonnegative(data.get('openInterest')),
            'ask_shares':maybe_nonnegative(data.get('askShares')),
            'bid_shares':maybe_nonnegative(data.get('bidShares')),
            'ask_depth':maybe_nonnegative(data.get('askDepth')),
            'bid_depth':maybe_nonnegative(data.get('bidDepth'))},None
# ---------------- END V3.2 ADDITIONS ----------------

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
                  movement=None, diagnostics=None, validated=None, history_count=0,
                  v31_stats=None, v31_coverage=None, quote_reasons=None):
    """V3 digest: exact contract IDs, history counts, honest model estimates."""
    book_details = book_details or {}
    movement = movement or {}
    diagnostics = diagnostics or {}
    validated = validated or []
    v31_stats = v31_stats or {}
    v31_coverage = v31_coverage or {}
    quote_reasons = quote_reasons or {}
    settled = sum(isinstance(s, dict) and s.get("result") in (0, 1) for s in samples.values())
    pending = sum(isinstance(s, dict) and s.get("result") is None for s in samples.values())
    top = digest_picks(ranked, quoted, limit=5)
    header = [
        f"**POLYMARKET US · V3.2 STATISTICAL RESEARCH** ({timestamp})",
        f"Live quotes {len(quoted)}/{_session['candidates']} | unique samples "
        f"{len(samples)} ({settled} settled, {pending} pending) | long-term records {history_count}",
        f"Validated categories {len(models)} | incomplete quotes {diagnostics.get('missing',0)} "
        f"| wide spreads {diagnostics.get('wide',0)} | API errors {diagnostics.get('errors',0)}",
        f"Historical archive {v31_stats.get('archived',0)} | "
        f"new {v31_stats.get('imported',0)}/{v31_stats.get('attempted',0)} attempted | "
        f"queued {v31_stats.get('remaining',0)} | skipped {v31_stats.get('skipped_total',0)}", 
    ]
    if v31_coverage:
        detail_coverage = ', '.join(
            f'{cat}={n}' for cat, n in sorted(v31_coverage.items(),
                key=lambda item: -item[1])[:4])
        header.append('Independent settled events by category: ' + detail_coverage)
    if quote_reasons:
        detail = ', '.join(f'{reason}={count}' for reason,count in sorted(
            quote_reasons.items(), key=lambda item: -item[1])[:4])
        header.append('Quote diagnostics: ' + detail)
    if validated:
        header.append('**HOLDOUT-TESTED CALIBRATION FLAGS (NOT GUARANTEED):**')
        for signal in sorted(validated,key=lambda x:x['lower_edge'],reverse=True)[:2]:
            header.append(
                f"• **{market_name(signal['market'])[:104]}** | "
                f"`{clean_text(signal['slug'], 85)}`\n"
                f"  10-share VWAP ${signal['execution_price']:.3f} | calibrated P(YES) "
                f"{signal['estimate']:.1%}, conservative bound {signal['lower']:.1%}\n"
                f"  Est. net EV/share (fee + buffer included) "
                f"+${signal['edge']:.3f}/share (conservative +${signal['lower_edge']:.3f}) "
                f"| training bin n={signal['bin_n']} / holdout n={signal['holdout_n']} "
                f"| holdout Brier gain {signal['brier_gain']:.4f}"
            )
    else:
        header.append('**Validated probability signals:** none. Historical calibration '
                      'has not passed chronological out-of-sample checks for these markets.')
    header.append("**Live research watchlist (NOT picks):**")
    tail = ("Bid/ask: exchange prices. Calibrated estimates correct historical "
            "market-price bias, NOT independent event forecasts. Fee and depth "
            "costs approximate. READ-ONLY; NO TRADES.")
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
        detail += f" (market pricing {q['bid']:.0%}–{q['ask']:.0%}; NOT a model estimate)"
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

    v31_stats = backfill_historical(state, now=base.now_ts())
    v31_models, v31_coverage = historical_models(state.get('v31_archive', {}))
    print('V3.2 backfill: ' + str(v31_stats) + '; coverage=' + str(v31_coverage)
          + '; validated categories=' + str(sorted(v31_models)),flush=True)
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
    quote_reasons = Counter()
    movements = {}
    book_details = {}
    validated = []
    timestamp = base.now_ts()
    for slug in slugs:
        try:
            q, reason = quote_with_reason(slug)
            if q is None:
                diagnostics["missing"] += 1
                quote_reasons[reason or 'unknown'] += 1
                continue
            if q["spread"] > args.max_spread:
                diagnostics["wide"] += 1
                quote_reasons['spread_above_limit'] += 1
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
            # Preliminary theoretical screen; confirm 10-share visible
            # execution depth before sending ANY value flag.
            score = None
            prelim = v32_signal(slug, m, q, v31_models, args)
            if prelim:
                depth = fetch_book_metrics(slug, target_shares=10)
                if depth is not None:
                    book_details[slug] = depth
                if depth and depth.get('fully_fillable') and depth.get('vwap') is not None:
                    score = v32_signal(slug, m, q, v31_models, args,
                                       execution_price=depth['vwap'])
                else:
                    quote_reasons['signal_book_insufficient'] += 1
            if score:
                validated.append(score)
            value_active = score is not None
            if value_active and not proposed.get("stat_edge_active", False):
                alerts.append(
                    f"HOLDOUT-TESTED CALIBRATION FLAG: conservative estimated "
                    f"net advantage +${score['lower_edge']:.3f}/contract; "
                    f"calibrated P(YES) {score['estimate']:.1%}; lower "
                    f"{score['lower']:.1%}; training bin n={score['bin_n']} "
                    f"and holdout n={score['holdout_n']}, Brier gain "
                    f"{score['brier_gain']:.4f}. NOT guaranteed."
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
                info = book_details.get(m["slug"])
                if info is None:
                    info = fetch_book_metrics(m["slug"], target_shares=10)
                if info is not None:
                    book_details[m["slug"]] = info
            except (ValueError, KeyError, TypeError, HTTPError, URLError,
                    RuntimeError, OSError) as exc:
                errors.append(f"Book {m['slug'][:35]}: {type(exc).__name__}")
        digest = market_digest(selected, quoted, samples, v31_models,
                               timestamp=base.utc_stamp(), book_details=book_details,
                               movement=movements, diagnostics=diagnostics,
                               validated=validated,
                               history_count=len(state.get("v3_research_records", {})),
                               v31_stats=v31_stats, v31_coverage=v31_coverage,
                               quote_reasons=quote_reasons)
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
        f"| V3 records {len(state.get('v3_research_records', {}))} | validated flags {len(validated)}"
        f" | V3.2 archived {len(state.get('v31_archive', {}))}"
        f" | V3.2 validated historical categories {len(v31_models)}"
        f" | quote diagnostic reasons {dict(quote_reasons)}" +
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


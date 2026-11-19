#!/usr/bin/env python3
"""Read-only Polymarket US cross-category statistical alert scanner.

Collects historical first-seen quotes and verified settlement outcomes, validates
a category/price-bucket calibration model out of sample, and flags only
conservative after-fee expected-value candidates. No trading API is used.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
API = "https://gateway.polymarket.us"
STATE = ROOT / "state.json"
WATCHLIST = ROOT / "watchlist.csv"
MAX_MESSAGE = 1850


def now_ts():
    return time.time()


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def price(v):
    if isinstance(v, dict):
        v = v.get("value")
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    return f if math.isfinite(f) and 0 <= f <= 1 else None


def number(v, default=0.0):
    try:
        f = float(v)
        return f if math.isfinite(f) and f >= 0 else default
    except (ValueError, TypeError):
        return default


def request_json(path, params=None):
    url = API + path
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "PersonalReadOnlyMarketAlerts/1.0", "Accept": "application/json"})
    with urlopen(req, timeout=18) as resp:
        return json.load(resp)


def list_markets(max_pages=10, page_size=100):
    """Fetch up to max_pages pages. This is bounded discovery, not a whole-exchange guarantee."""
    all_rows = []
    for page_idx in range(max_pages):
        payload = request_json("/v1/markets", {
            "limit": page_size,
            "offset": page_idx * page_size,
            "active": "true",
            "closed": "false",
            "archived": "false",
        })
        rows = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Market listing API returned an unexpected shape")
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
    return all_rows


def eligible_market(m):
    return bool(isinstance(m, dict) and m.get("slug") and
                m.get("active", True) is not False and m.get("closed", False) is not True and
                m.get("archived", False) is not True and m.get("hidden", False) is not True)


def list_quote(m):
    bid = price(m.get("bestBidQuote"))
    ask = price(m.get("bestAskQuote"))
    if bid is None or ask is None or not 0 < ask < 1 or not 0 <= bid <= ask:
        return None
    return {"bid": bid, "ask": ask, "spread": round(ask - bid, 4)}


def fetch_bbo(slug):
    payload = request_json("/v1/markets/" + quote(slug, safe="") + "/bbo")
    m = payload.get("marketData") if isinstance(payload, dict) else None
    if not isinstance(m, dict) or m.get("state") not in (None, "", "MARKET_STATE_OPEN"):
        return None
    bid, ask = price(m.get("bestBid")), price(m.get("bestAsk"))
    if bid is None or ask is None or not 0 < ask < 1 or not 0 <= bid <= ask:
        return None
    return {"bid": bid, "ask": ask, "spread": round(ask - bid, 4)}


def read_watchlist(path):
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        result = {}
        for row in reader:
            slug = (row.get("slug") or "").strip()
            if not slug or slug.startswith("#") or slug.startswith("REPLACE_"):
                continue
            def opt_float(key):
                raw = (row.get(key) or "").strip()
                if not raw:
                    return None
                value = float(raw)
                if not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"{slug}: {key} must be between 0 and 1")
                return value
            result[slug] = {"fair": opt_float("fair_probability"),
                            "target": opt_float("alert_ask_below"),
                            "note": (row.get("notes") or "").strip()[:160]}
        return result


def candidate_rank(m, q):
    """Trading activity/quote quality only; NEVER a probability estimate."""
    volume = number(m.get("volume24hr"), 0)
    spread_penalty = 120 * q["spread"] if q is not None else 25
    return math.log1p(volume) * 3 - spread_penalty


def choose_candidates(markets, *, min_volume=50, max_spread=0.08, limit=40):
    """Don't discard markets with missing listing quotes: fetch BBO separately."""
    ranked = []
    for m in markets:
        if not eligible_market(m):
            continue
        if number(m.get("volume24hr"), 0) < min_volume:
            continue
        q = list_quote(m)
        if q is not None and q["spread"] > max_spread:
            continue
        ranked.append((candidate_rank(m, q), m))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    # Diversify early quote checks rather than selecting exclusively one market type.
    grouped = {}
    for _, m in ranked:
        grouped.setdefault(category_of(m), []).append(m)
    selected = []
    categories = sorted(grouped, key=lambda category: candidate_rank(grouped[category][0], list_quote(grouped[category][0])), reverse=True)
    while len(selected) < limit and any(grouped.values()):
        for category in categories:
            if grouped[category] and len(selected) < limit:
                selected.append(grouped[category].pop(0))
    return selected


def net_edge(fair, ask, theta=0.095, execution_buffer=0.01):
    """Illustrative long-YES expected net benefit per contract, not a guarantee."""
    return fair - ask - theta * ask * (1 - ask) - execution_buffer


def examine(slug, quote_data, prior, setting, *, move=0.05, min_edge=0.05,
            max_spread=0.08, theta=0.095, execution_buffer=0.01):
    """Pure detection; state updates should be committed only on successful notifications.

    On new observations no movement alert is raised. Repeated value/target
    conditions are suppressed until the condition ceases and reappears.
    """
    prev = dict(prior)
    if quote_data is None or quote_data["spread"] > max_spread:
        return [], prev
    ask, bid = quote_data["ask"], quote_data["bid"]
    anchor = prev.get("anchor")
    alerts = []
    if anchor is None:
        prev["anchor"] = ask
    elif abs(ask - anchor) + 1e-9 >= move:
        diff = ask - anchor
        alerts.append(f"PRICE MOVE: YES ask {anchor:.2f} → {ask:.2f} ({diff:+.2f})")
        prev["anchor"] = ask

    fair = setting.get("fair")
    if fair is not None:
        edge = net_edge(fair, ask, theta, execution_buffer)
        active = edge + 1e-9 >= min_edge
        if active and not prev.get("edge_active", False):
            alerts.append(f"MODEL VALUE FLAG: estimated net +${edge:.3f}/contract; model YES probability {fair:.1%}")
        prev["edge_active"] = active

    target = setting.get("target")
    if target is not None:
        met = ask <= target
        if met and not prev.get("target_active", False):
            alerts.append(f"PRICE TARGET: YES ask {ask:.2f} ≤ your target {target:.2f}")
        prev["target_active"] = met
    prev["last_seen"] = now_ts()
    prev["last_ask"] = ask
    prev["last_bid"] = bid
    return alerts, prev


def validate_webhook(url):
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    return (parsed.scheme == "https" and parsed.hostname in ("discord.com", "discordapp.com")
            and len(parts) >= 5 and parts[:3] == ["", "api", "webhooks"]
            and bool(parts[3]) and bool(parts[4]) and parsed.username is None)


def load_webhook():
    env = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if env:
        if not validate_webhook(env):
            raise ValueError("DISCORD_WEBHOOK_URL isn't a valid Discord webhook URL")
        return env
    secret_file = ROOT / ".discord_webhook"
    if not secret_file.exists():
        raise ValueError("No Discord webhook configured. Run: python3 setup_discord.py")
    url = secret_file.read_text(encoding="utf-8").strip()
    if not validate_webhook(url):
        raise ValueError("Saved Discord webhook isn't valid. Run setup_discord.py again")
    return url


def post_discord(message, *, webhook=None, transport=None):
    url = webhook if webhook is not None else load_webhook()
    if not validate_webhook(url):
        raise ValueError("Invalid Discord webhook URL")
    payload = json.dumps({"content": message[:MAX_MESSAGE], "allowed_mentions": {"parse": []}}).encode("utf-8")
    request = Request(url, data=payload, method="POST", headers={"Content-Type": "application/json", "User-Agent": "PersonalReadOnlyMarketAlerts/1.0"})
    opener = transport or urlopen
    for attempt in range(2):
        try:
            with opener(request, timeout=18) as response:
                return 200 <= response.status < 300
        except HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                retry = number(exc.headers.get("Retry-After", "1"), 1)
                time.sleep(min(max(retry, 1), 10))
                continue
            # Avoid including URL or token in exception messages.
            raise RuntimeError(f"Discord returned HTTP {exc.code}") from None
        except URLError:
            raise RuntimeError("Could not reach Discord; check internet connection") from None
    return False


def load_state(path=STATE):
    if not path.exists():
        return {"observations": {}, "seen_slugs": [], "initialized": False, "last_digest": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("observations"), dict) or not isinstance(data.get("seen_slugs"), list):
        raise ValueError("Invalid state file; back it up and delete it to restart")
    return data


def save_state(data, path=STATE):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)


def market_name(m):
    return str(m.get("titleShort") or m.get("question") or m.get("title") or m.get("slug"))[:100]


def alert_text(slug, m, q, descriptions, *, watched=False, note=""):
    label = "WATCHLIST" if watched else "BROAD MARKET SCAN"
    text = f"**POLYMARKET US · {label}**\n{market_name(m)}\nMarket slug: `{slug}`\n" + "\n".join(f"• {d}" for d in descriptions)
    text += f"\nYES Bid {q['bid']:.2f} | Ask {q['ask']:.2f} | Spread {q['spread']:.2f}"
    if note:
        text += f"\nNote: {note}"
    text += "\nResearch only. Verify liquidity, resolution terms, fees, and eligibility. NO TRADE PLACED."
    return text


def digest_text(ranked, quoted, *, timestamp):
    lines = [f"**POLYMARKET US · TOP MARKETS TO RESEARCH** ({timestamp})",
             "Ranked by 24h volume and bid/ask spread — NOT expected returns or recommended bets."]
    for i, m in enumerate(ranked[:5], 1):
        slug = m["slug"]
        q = quoted.get(slug)
        if q is None:
            continue
        lines.append(f"{i}. {market_name(m)[:70]}\n   `{slug}` · YES ask {q['ask']:.2f} · spread {q['spread']:.2f} · 24h volume {number(m.get('volume24hr')):,.0f}")
    lines.append("Read-only scanner. No independent predictive edge inferred.")
    return "\n".join(lines)


def end_timestamp(m):
    raw = m.get("endDate")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def category_of(m):
    category = str(m.get("category") or "Uncategorized").strip().lower()
    return category[:45] or "uncategorized"


def bucket_for(p):
    return min(4, max(0, int(p * 5)))


def model_summary(samples, category):
    """Walk-forward-ish train/holdout validation of simple calibration corrections.

    Each unique contract contributes ONE observation taken before the event end,
    then the result is verified from the public settlement API. Conservative
    category-specific model is gated by a holdout Brier improvement. No label
    leakage from unresolved contracts.
    """
    rows = [v for v in samples.values() if v.get("category") == category and
            v.get("result") in (0, 1) and 0 < number(v.get("p")) < 1 and
            number(v.get("settled_at")) > 0]
    rows.sort(key=lambda v: (v["settled_at"], v.get("first_seen", 0)))
    if len(rows) < 150:
        return None
    cutoff = int(len(rows) * 0.8)
    train, test = rows[:cutoff], rows[cutoff:]
    if len(test) < 30:
        return None
    by_bin = {}
    for i in range(5):
        subset = [v for v in train if bucket_for(v["p"]) == i]
        if len(subset) < 25:
            continue
        residuals = [v["result"] - v["p"] for v in subset]
        n = len(residuals)
        avg = sum(residuals) / n
        # Treat grouped, correlated contract outcomes cautiously.
        variance = sum((x - avg) ** 2 for x in residuals) / max(1, n - 1)
        shrink = n / (n + 50.0)
        by_bin[i] = {
            "correction": shrink * avg,
            "se": max(0.025, math.sqrt(max(0, variance) / n)),
            "n": n,
        }
    if not by_bin:
        return None
    baseline_loss = 0.0
    adjusted_loss = 0.0
    eval_n = 0
    for row in test:
        b = by_bin.get(bucket_for(row["p"]))
        if b is None:
            continue
        p = row["p"]
        adjusted = min(0.99, max(0.01, p + b["correction"]))
        baseline_loss += (p - row["result"]) ** 2
        adjusted_loss += (adjusted - row["result"]) ** 2
        eval_n += 1
    if eval_n < 25:
        return None
    improvement = (baseline_loss - adjusted_loss) / eval_n
    # Don't promote a model merely because it can fit the training history.
    if improvement < 0.001:
        return None
    return {"bins": by_bin, "n_train": len(train), "n_test": eval_n,
            "brier_improvement": improvement}


def statistical_yes_edge(model, bid, ask, *, theta=0.095, buffer=0.01):
    """Lower-bound YES edge; no model -> no value alerts."""
    midpoint = (bid + ask) / 2
    b = model["bins"].get(bucket_for(midpoint)) if model else None
    if b is None:
        return None
    estimated_p = max(0, min(1, midpoint + b["correction"]))
    conservative_p = max(0, estimated_p - 1.64 * b["se"])
    edge = conservative_p - ask - theta * ask * (1 - ask) - buffer
    return {"estimate": estimated_p, "conservative": conservative_p,
            "edge": edge, "n": b["n"], "validation": model["brier_improvement"]}


def fetch_settlement(slug):
    data = request_json("/v1/markets/" + quote(slug, safe="") + "/settlement")
    outcome = data.get("settlement") if isinstance(data, dict) else None
    if isinstance(outcome, bool):
        return None
    try:
        outcome = float(outcome)
    except (TypeError, ValueError):
        return None
    return int(outcome) if outcome in (0, 1) else None


def settle_matured_samples(samples, settle_fn=fetch_settlement, *, check_limit=5):
    now = now_ts()
    due = sorted(((slug, s) for slug, s in samples.items()
                  if s.get("result") is None and 0 < number(s.get("end_ts")) <= now - 3600
                  and number(s.get("last_check")) <= now - 21600),
                 key=lambda x: x[1]["end_ts"])
    done = 0
    for slug, sample in due[:check_limit]:
        # Explicitly don't label a contract as lost just because it disappeared.
        sample["last_check"] = now
        try:
            outcome = settle_fn(slug)
        except (HTTPError, URLError, RuntimeError, ValueError, OSError):
            continue
        if outcome in (0, 1):
            sample["result"] = outcome
            sample["settled_at"] = now
            done += 1
    return done


def track_sample(samples, m, q):
    slug = m["slug"]
    end_ts = end_timestamp(m)
    now = now_ts()
    # Snapshot before conclusion; one sample per market avoids fake precision.
    if slug not in samples and q["spread"] <= 0.08 and end_ts is not None and end_ts - now >= 3600:
        midpoint = (q["bid"] + q["ask"]) / 2
        samples[slug] = {"category": category_of(m), "p": round(midpoint, 5),
                         "first_seen": now, "end_ts": end_ts, "result": None,
                         "last_check": 0, "settled_at": 0}


def market_digest(ranked, quoted, samples, models, *, timestamp):
    valid = [m for m in ranked if m["slug"] in quoted]
    resolved = sum(s.get("result") in (0, 1) for s in samples.values())
    lines = [f"**POLYMARKET US · CROSS-CATEGORY STAT SCAN** ({timestamp})",
             f"Tracked {len(samples)} unique markets · verified outcomes {resolved} · validated categories {len(models)}",
             "**Top 5 liquid markets to research (NOT winning picks):**"]
    for i, m in enumerate(valid[:5], 1):
        q = quoted[m["slug"]]
        lines.append(f"{i}. {market_name(m)[:66]} · {category_of(m)}\n"
                     f"   YES ask {q['ask']:.2f}, spread {q['spread']:.2f}, 24h volume {number(m.get('volume24hr')):,.0f}")
    if not valid:
        lines.append("No eligible live quotes. Check API response, quote depth, and filters.")
    if not models:
        lines.append("No category passed historical-sample and holdout-validation rules yet; no statistical pick is justified.")
    lines.append("Read-only · No trades placed · Fees, limits and settlement rules matter.")
    return "\n".join(lines)[:MAX_MESSAGE]


def scan_once(args, *, listing_fn=list_markets, quote_fn=fetch_bbo, notifier=post_discord):
    state = load_state(args.state)
    watch = read_watchlist(args.watchlist)
    markets_raw = listing_fn(args.max_pages, args.page_size)
    markets = {m["slug"]: m for m in markets_raw if eligible_market(m)}
    if not markets:
        raise RuntimeError("No active markets found; not updating baseline")
    ranked = choose_candidates(list(markets.values()), min_volume=args.min_volume,
                               max_spread=args.max_spread, limit=args.max_bbo)
    # Always include a specifically watched slug even if it is outside the list API page bound.
    slugs = list(dict.fromkeys(list(watch) + [m["slug"] for m in ranked]))
    quoted = {}
    updates = {}
    notifications = []
    errors = []
    old_observations = state.setdefault("observations", {})
    samples = state.setdefault("training_samples", {})
    settled_count = settle_matured_samples(samples, check_limit=5)
    validated_models = {}
    for category in {v.get("category") for v in samples.values()}:
        fitted = model_summary(samples, category)
        if fitted:
            validated_models[category] = fitted
    previous_seen = set(state.get("seen_slugs", []))
    initialized = bool(state.get("initialized", False))
    for slug in slugs:
        try:
            q = quote_fn(slug)
            if q is None:
                continue
            quoted[slug] = q
            m = markets.get(slug, {"slug": slug, "title": slug, "volume24hr": 0})
            setting = watch.get(slug, {})
            if slug in markets:
                track_sample(samples, m, q)
            # Manual "fair_probability" values are intentionally ignored for a
            # strictly validated, outcomes-trained statistical model.
            audited_setting = {"fair": None, "target": setting.get("target")}
            alerts, proposed = examine(slug, q, old_observations.get(slug, {}), audited_setting,
                                      move=args.move, min_edge=args.min_edge, max_spread=args.max_spread,
                                      theta=args.fee_theta, execution_buffer=args.buffer)
            model = validated_models.get(category_of(m))
            score = statistical_yes_edge(model, q["bid"], q["ask"],
                                         theta=args.fee_theta, buffer=args.buffer)
            if score and score["edge"] >= args.min_edge and not proposed.get("stat_edge_active", False):
                alerts.append(f"VALIDATED STATISTICAL YES EDGE: conservative estimated +${score['edge']:.3f}/contract "
                              f"after fees/buffer | YES p≈{score['estimate']:.1%} "
                              f"(lower bound {score['conservative']:.1%}) | historical bin n={score['n']}, "
                              f"holdout Brier gain {score['validation']:.4f}")
            proposed["stat_edge_active"] = bool(score and score["edge"] >= args.min_edge)
            # Listing discovery: only after an earlier successful scan; never infer value.
            if initialized and slug not in previous_seen and slug not in watch and \
               number(m.get("volume24hr"), 0) >= args.min_volume and q["spread"] <= args.max_spread:
                alerts.insert(0, "NEWLY OBSERVED MARKET: passed volume/spread filters; not a profit prediction")
            updates[slug] = proposed
            if alerts:
                notifications.append((slug, m, q, alerts, setting))
        except (ValueError, KeyError, TypeError, HTTPError, URLError, RuntimeError, OSError) as exc:
            errors.append(f"{slug}: {type(exc).__name__}")
    # Send a deliberately bounded number of notifications per cycle.
    sent = 0
    for slug, m, q, alerts, setting in notifications[:args.max_alerts]:
        msg = alert_text(slug, m, q, alerts, watched=(slug in watch), note=setting.get("note", ""))
        try:
            if notifier(msg):
                old_observations[slug] = updates[slug]
                sent += 1
            else:
                errors.append(f"{slug}: Discord declined alert")
        except (RuntimeError, ValueError, OSError) as exc:
            errors.append(f"{slug}: notification failed: {exc}")
    # For untriggered observations, commit immediately. If an alert fails, keep it armed.
    alert_slugs = {item[0] for item in notifications}
    for slug, proposed in updates.items():
        if slug not in alert_slugs:
            old_observations[slug] = proposed
    # If more market alerts than budget, keep their previous anchors so they retry later.
    digest_due = (now_ts() - number(state.get("last_digest", 0)) >= args.digest_hours * 3600)
    report_failed = False
    if args.report_now or digest_due:
        available = [m for m in ranked if m["slug"] in quoted]
        try:
            if notifier(market_digest(available, quoted, samples, validated_models, timestamp=utc_stamp())):
                state["last_digest"] = now_ts()
                sent += 1
            else:
                errors.append("Digest Discord delivery failed")
                report_failed = True
        except (RuntimeError, ValueError, OSError) as exc:
            errors.append(f"Digest notification failed: {exc}")
            report_failed = True
    state["initialized"] = True
    # Preserve prior entries as discovery listings can shift between pages.
    state["seen_slugs"] = sorted(previous_seen.union(markets))
    # Prune observations older than 7 days, with bounded storage for indefinite operation.
    cutoff = now_ts() - 7 * 86400
    state["observations"] = {s: v for s, v in old_observations.items() if number(v.get("last_seen"), 0) >= cutoff}
    save_state(state, args.state)
    print(f"{utc_stamp()} | Listed {len(markets)} | BBO valid {len(quoted)}/{len(slugs)} | verified settlements {settled_count} | model categories {len(validated_models)} | alerts delivered {sent}" +
          (f" | errors: {'; '.join(errors[:5])}" if errors else ""), flush=True)
    if args.report_now and report_failed:
        raise RuntimeError("Manual market report failed to reach Discord; see earlier scan log")
    return sent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Polymarket US read-only Discord alerts (NO TRADE EXECUTION)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--loop", action="store_true", help="Repeat every 300 seconds while computer remains on")
    group.add_argument("--test-discord", action="store_true", help="Send a single Discord connection test")
    group.add_argument("--demo", action="store_true", help="Run offline detector demonstration")
    parser.add_argument("--report-now", action="store_true", help="Send market research digest immediately, even if no model is validated")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--watchlist", type=Path, default=WATCHLIST)
    parser.add_argument("--max-pages", type=int, default=10, help="Market listing pages to scan; default 10")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-bbo", type=int, default=40, help="Number of promising quote-quality markets to check in detail")
    parser.add_argument("--min-volume", type=float, default=50, help="Minimum reported 24h volume for broad scan")
    parser.add_argument("--max-spread", type=float, default=0.08)
    parser.add_argument("--move", type=float, default=0.05)
    parser.add_argument("--min-edge", type=float, default=0.05)
    parser.add_argument("--fee-theta", type=float, default=0.095)
    parser.add_argument("--buffer", type=float, default=0.01)
    parser.add_argument("--max-alerts", type=int, default=5)
    parser.add_argument("--digest-hours", type=float, default=6)
    args = parser.parse_args(argv)
    if args.test_discord:
        if post_discord("Polymarket US scanner connected successfully. Test message only. No trades placed."):
            print("Discord test sent. Check your #polymarket-alerts channel.")
        else:
            raise RuntimeError("Discord did not confirm delivery")
        return
    if args.demo:
        for ask in (0.60, 0.59, 0.53, 0.46, 0.48):
            if ask == 0.60:
                st = {}
            a, st = examine("DEMO", {"bid": ask-0.02, "ask": ask, "spread": 0.02}, st, {"fair": 0.68, "target": 0.48})
            print(f"Synthetic {ask:.2f}: {a or 'no alert'}")
        return
    for key in ("max_pages", "page_size", "max_bbo", "max_alerts", "interval"):
        if getattr(args, key) <= 0:
            parser.error(f"--{key.replace('_','-')} must be positive")
    for key in ("min_volume", "max_spread", "move", "min_edge", "fee_theta", "buffer", "digest_hours"):
        x = getattr(args, key)
        if not math.isfinite(x) or x < 0:
            parser.error(f"--{key.replace('_','-')} must be nonnegative and finite")
    if args.loop and args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    load_webhook()  # Fail fast if user hasn't connected their private Discord webhook.
    if not args.loop:
        scan_once(args)
        return
    print(f"Read-only monitoring every {args.interval} seconds; Ctrl+C to stop. No trades can be placed.")
    try:
        while True:
            start = now_ts()
            try:
                scan_once(args)
            except (HTTPError, URLError, RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"Scan failed ({type(exc).__name__}); will retry next cycle", file=sys.stderr)
            time.sleep(max(0, args.interval - (now_ts() - start)))
    except KeyboardInterrupt:
        print("Stopped. No trades placed.")


if __name__ == "__main__":
    main()

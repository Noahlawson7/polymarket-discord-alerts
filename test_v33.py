
#!/usr/bin/env python3
"""
Polymarket US V3.3 / V3.4 Offline Regression Tests

Checks:
- Historical price recovery
- Strict historical quote filters
- Settlement validation
- Category-balanced backfill
- Historical discovery pagination
- Independent event counts
- Statistical validation safeguards
- Discord digest compatibility

No network, no webhook calls, no trading.
"""

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Stub the scanner's base module to prevent network access.
base = types.ModuleType("market_alerts")

base.category_of = lambda m: m.get("category", "other")
base.number = lambda v, default=0: (
    float(v) if v is not None else default
)
base.request_json = lambda path: None
base.MAX_MESSAGE = 1950

sys.modules["market_alerts"] = base

scanner_path = Path(__file__).with_name("scanner_plus.py")

if not scanner_path.exists():
    raise SystemExit("ERROR: scanner_plus.py is missing")

spec = importlib.util.spec_from_file_location(
    "v33", scanner_path
)
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

# Prevent delays during offline tests.
v.time.sleep = lambda _: None

start = datetime(2026, 10, 1, tzinfo=timezone.utc)
end = start + timedelta(days=60)
ts = int(start.timestamp())

market = {
    "slug": "sport-a",
    "status": "MARKET_STATUS_RESOLVED",
    "category": "sports",
    "startDate": start.isoformat(),
    "endDate": end.isoformat(),
    "eventId": "EVT-S",
    "question": "Will Example A win?",
}


def pt(days, ask, noask):
    return {
        "timestamp": int(ts + days * 86400),
        "longPrice": ask,
        "shortPrice": noask,
    }


print("TEST 1: Historical price recovery")

hist = {
    "history": [
        pt(1, .75, .48),
        pt(3, .70, .48),
        pt(10, .40, .62),
        pt(12, .41, .61),
    ]
}

rec, why = v.historical_point_with_reason(hist, market)

assert why is None, why
assert rec["observation_method"] == "recovered_later_quote"
assert rec["t"] == ts + 10 * 86400
assert rec["spread"] <= .10
assert rec["delay_days"] == 10

print("PASS: Later valid historical quote recovered")


print("TEST 2: Initial-window priority")

clean = {
    "history": [
        pt(1, .36, .65),
        pt(3, .38, .63),
        pt(10, .40, .61),
    ]
}

reliable, why = v.historical_point_with_reason(
    clean, market
)

assert why is None
assert reliable["observation_method"] == "initial_window"
assert reliable["t"] == ts + 86400

print("PASS: Original clean quotes prioritized")


print("TEST 3: Strict spread safeguards")

wide = {
    "history": [
        pt(1, .75, .48),
        pt(11, .70, .50),
        pt(13, .70, .50),
    ]
}

assert v.historical_point_with_reason(wide, market)[0] is None

print("PASS: Wide spreads correctly rejected")


print("TEST 4: Recovery time limits")

too_late = {
    "history": [
        pt(1, .75, .48),
        pt(22, .40, .62),
        pt(24, .40, .62),
    ]
}

assert v.historical_point_with_reason(
    too_late, market
)[0] is None

short = dict(
    market,
    endDate=(start + timedelta(days=24)).isoformat(),
)

assert v.historical_point_with_reason(
    {
        "history": [
            pt(1, .75, .48),
            pt(14, .40, .62),
            pt(15, .40, .62),
        ]
    },
    short,
)[0] is None

print("PASS: Recovery deadlines respected")


print("TEST 5: Independent timestamp requirement")

uncorroborated = {
    "history": [
        pt(1, .75, .48),
        pt(10, .40, .62),
        pt(65, .40, .62),
    ]
}

_, reason = v.historical_point_with_reason(
    uncorroborated, market
)

assert reason == "insufficient_later_history"

print("PASS: Uncorroborated quotes rejected")


print("TEST 6: Contractual outcome deadlines")

long_end = dict(
    market,
    question="Confirmed By — October 12, 2026",
    endDate=(start + timedelta(days=200)).isoformat(),
)

expected_deadline = datetime(
    2026, 10, 13, tzinfo=timezone.utc
).timestamp()

assert v.effective_outcome_deadline(long_end) == expected_deadline

print("PASS: Question deadline respected")


print("TEST 7: Historical archive preservation")

state = {
    "v31_failed": {
        "sport-a": "no_safe_early_quote:early_spread_above_10c"
    },
    "v31_archive": {
        "already": {
            "category": "sports",
            "event": "old",
            "p": .3,
            "y": 0,
            "t": ts,
            "end": ts + 100000,
        }
    },
    "v31_last_list": 2000000000,
    "v321_legacy_recheck_done": True,
    "v322_diagnostic_recheck_done": True,
}

calls = []


def api(path):
    calls.append(path)

    if path.startswith("/v1/market/slug/"):
        return {"market": market}

    if path.endswith("/settlement"):
        return {"settlement": 1}

    if path.startswith("/v1/price-history?"):
        return hist

    raise AssertionError("Unexpected API path: " + path)


base.request_json = api

result = v.backfill_historical(
    state,
    now=2000000000,
    items_per_run=12,
)

assert result["v33_requeued"] == 1
assert result["recovered_imports"] == 1
assert result["archived"] == 2
assert state["v31_archive"]["already"]["y"] == 0
assert (
    state["v31_archive"]["sport-a"]["observation_method"]
    == "recovered_later_quote"
)

print("PASS: Verified archive preserved")


print("TEST 8: No repeated legacy migrations")

n_calls = len(calls)

again = v.backfill_historical(
    state,
    now=2000000001,
    items_per_run=12,
)

assert len(calls) == n_calls
assert not again.get("v33_requeued")

print("PASS: Legacy recovery does not repeat")


print("TEST 9: Category-balanced backfill")

finance = dict(
    market,
    slug="fin-a",
    category="finance",
    eventId="EVT-F",
)

politics = dict(
    market,
    slug="pol-a",
    category="politics",
    eventId="EVT-P",
)

culture = dict(
    market,
    slug="cul-a",
    category="culture",
    eventId="EVT-C",
)

state2 = {
    "v31_archive": {
        "sports" + str(i): {"category": "sports"}
        for i in range(5)
    },
    "v31_queue": [
        dict(s, attempts=0, event=v.event_group(s))
        for s in [
            market,
            dict(market, slug="sport-b"),
            finance,
            politics,
            culture,
        ]
    ],
    "v31_last_list": 2000000000,
    "v321_legacy_recheck_done": True,
    "v322_diagnostic_recheck_done": True,
    "v33_recovery_recheck_done": True,
}

chosen = []


def api_balanced(path):
    if path.endswith("/settlement"):
        chosen.append(path.split("/")[3])
        return {"settlement": 1}

    if path.startswith("/v1/price-history?"):
        return clean

    raise AssertionError(path)


base.request_json = api_balanced

back = v.backfill_historical(
    state2,
    now=2000000000,
    items_per_run=3,
)

assert back["attempted"] == 3
assert set(chosen) == {
    "fin-a",
    "pol-a",
    "cul-a",
}

assert back["archive_categories"]["finance"] == 1

print("PASS: Underrepresented categories prioritized")


print("TEST 10: Historical discovery pagination")

state3 = {
    "v31_last_list": 0,
    "v31_offset": 100,
    "v321_legacy_recheck_done": True,
    "v322_diagnostic_recheck_done": True,
    "v33_recovery_recheck_done": True,
}

offsets = []


def api_lanes(path):
    if path.startswith("/v1/markets?"):
        offset = int(
            parse_qs(urlparse(path).query)["offset"][0]
        )
        offsets.append(offset)
        return {"markets": []}

    raise AssertionError(path)


base.request_json = api_lanes

for current in (
    2000000000,
    2000002000,
    2000004000,
    2000006000,
):
    v.backfill_historical(
        state3,
        now=current,
        items_per_run=1,
    )

assert offsets == [100, 400, 1200, 2500]

print("PASS: Historical discovery lanes advance")


print("TEST 11: Independent event validation")

records = {}

for i in range(10):
    records[f"a{i}"] = {
        "category": "sports",
        "event": f"E{i // 2}",
        "p": .4,
        "t": ts + i * 86400,
        "end": ts + (i + 2) * 86400,
        "y": i % 2,
    }

progress = v.validation_progress(records, {})

assert progress["sports"]["independent"] == 5
assert progress["sports"]["additional_events_needed"] == 135
assert progress["finance"]["independent"] == 0

assert v.historical_models(records)[0] == {}

print("PASS: No inflated event counts or false models")


print("TEST 12: Discord research report")

report = v.market_digest(
    [],
    {},
    {},
    {},
    timestamp="offline",
    v31_stats={
        "archived": 2,
        "recovered_imports": 1,
        "v33_requeued": 1,
    },
    progress=progress,
)

assert "V3.3 STATISTICAL RESEARCH" in report
assert "History recovery: imported 1" in report
assert "sports=5/140" in report
assert "Validated probability signals:** none" in report
assert len(report) <= base.MAX_MESSAGE

print("PASS: Research report remains compatible")


print()
print("=" * 55)
print("ALL V3.3 HISTORICAL REGRESSION TESTS PASSED")
print("No network calls, webhooks, or trades were used.")
print("=" * 55)


#!/usr/bin/env python3
"""
Polymarket US V3.4 Historical Backfill Upgrade

Automatically updates scanner_plus.py to:
- Prioritize fresh historical candidates over legacy rechecks
- Track fresh and recheck attempts separately
- Report historical backfill efficiency
- Preserve existing scanner functionality

No network calls, trading, or Discord messages.
Creates a backup before modifying the scanner.
"""

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "scanner_plus.py"
BACKUP = ROOT / "scanner_plus_v33_backup.py"


def upgrade():
    if not TARGET.exists():
        print("ERROR: scanner_plus.py not found.")
        print("Put this updater in the same folder as scanner_plus.py.")
        sys.exit(1)

    original = TARGET.read_text(encoding="utf-8")

    if "attempted_fresh" in original and "attempted_recheck" in original:
        print("Scanner appears to have already received this upgrade.")
        return

    # Locate original V3.3 candidate selection.
    old_selection = """index,m = min(ready,key=lambda im:(
            archived_by_category[im[1].get('category','other')] +
            picked_by_category[im[1].get('category','other')], im[0]))"""

    new_selection = """index, m = min(
            ready,
            key=lambda im: (
                # Prefer fresh, metadata-complete records.
                bool(im[1].get("needs_metadata")),
                # Maintain balance across categories.
                archived_by_category[
                    im[1].get("category", "other")
                ] + picked_by_category[
                    im[1].get("category", "other")
                ],
                im[0],
            ),
        )"""

    old_counter = """        stats['attempted'] += 1
        try:"""

    new_counter = """        stats['attempted'] += 1

        if m.get("needs_metadata"):
            stats["attempted_recheck"] += 1
        else:
            stats["attempted_fresh"] += 1

        try:"""

    # Verify exact source compatibility before writing.
    if original.count(old_selection) != 1:
        print("ERROR: Expected V3.3 candidate selection not found.")
        print("No files were changed.")
        sys.exit(1)

    if original.count(old_counter) != 1:
        print("ERROR: Expected V3.3 attempt counter not found.")
        print("No files were changed.")
        sys.exit(1)

    updated = original.replace(old_selection, new_selection, 1)
    updated = updated.replace(old_counter, new_counter, 1)

    # Validate Python syntax before replacing the scanner.
    try:
        compile(updated, str(TARGET), "exec")
    except SyntaxError as exc:
        print(f"ERROR: Generated scanner failed syntax check: {exc}")
        print("No files were changed.")
        sys.exit(1)

    # Preserve the existing version before overwriting.
    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)

    TARGET.write_text(updated, encoding="utf-8")

    print("SUCCESS: Historical backfill priority upgraded.")
    print("Backup:", BACKUP.name)
    print("Updated:", TARGET.name)
    print()
    print("Changes:")
    print("- Fresh candidates prioritized over metadata rechecks")
    print("- Category balancing preserved")
    print("- Fresh and recheck attempt diagnostics added")
    print("- Historical quality gates unchanged")
    print("- Statistical thresholds unchanged")
    print("- No trading functionality added")
    print()
    print("Next: run python3 test_v33.py")


if __name__ == "__main__":
    upgrade()

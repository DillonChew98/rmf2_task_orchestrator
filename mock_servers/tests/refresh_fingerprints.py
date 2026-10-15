"""Refresh the feature-definition fingerprints test_drift.py compares against.

Run this ONLY after checking that the fake drivers still satisfy the changed
definitions. Refreshing without looking is how the last mock drifted.

    python -m tests.refresh_fingerprints
"""

from __future__ import annotations

import json
from pathlib import Path

from mock_servers.harness.instrument_root import find_instrument_root

from .test_drift import EXPECTED, FEATURE_COUNTS, fdl_fingerprints


def main() -> None:
    EXPECTED.mkdir(parents=True, exist_ok=True)
    for instrument in sorted(FEATURE_COUNTS):
        root = find_instrument_root(instrument)
        fingerprints = fdl_fingerprints(root)
        target = EXPECTED / f"{instrument}_fdl.json"
        target.write_text(json.dumps(fingerprints, indent=2, sort_keys=True) + "\n")
        print(f"{instrument}: {len(fingerprints)} features -> {target.name}")


if __name__ == "__main__":
    main()

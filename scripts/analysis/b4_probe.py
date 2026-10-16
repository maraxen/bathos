"""Trivial child run for the B4 cut-over latency measurement.

Writes its own bathos run id (recovered from the basename of $BTH_RESULTS_PATH,
which `bth run` names `<run_id>.bth-results.json`) to the path given as argv[1],
so the parent instrument knows which run to look for. Does nothing else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    results_path = os.environ.get("BTH_RESULTS_PATH")
    if not results_path or len(sys.argv) != 2:
        print("b4_probe: needs BTH_RESULTS_PATH and one argument (id file)", file=sys.stderr)
        return 2
    run_id = Path(results_path).name.removesuffix(".bth-results.json")
    Path(sys.argv[1]).write_text(run_id + "\n")
    print(f"b4_probe run_id={run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

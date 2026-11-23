"""Time the search ranking helper against a running sidecar's /embed endpoint."""

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import meaning  # noqa: E402

# --- TUNABLES ---------------------------------------------------------------
DEFAULT_RUNS = 30
QUERY = "Riverton observatory evening tour schedule and tickets"
CANDIDATES = (
    "Riverton Observatory tickets\nBook a guided evening tour or a weekend stargazing session.",
    "City astronomy events\nA calendar of upcoming talks, night walks and telescope demonstrations.",
    "Riverton Observatory visitor hours\nEvening tours leave at seven on Fridays and Saturdays.",
    "Riverton Museum late nights\nThe museum stays open after sunset during its summer series.",
    "Driving to the observatory\nParking information and the route from downtown Riverton.",
    "Regional art fair\nAn indoor gallery and outdoor stalls are open all weekend.",
    "Riverton night sky guide\nHow to spot bright planets around the city this month.",
    "Observatory tour FAQ\nAdvance reservations are required and weather may affect access.",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sidecar_url", help="Sidecar base URL")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    meaning.SIDECAR_URL = args.sidecar_url.rstrip("/")
    elapsed = []
    misses = 0
    for _ in range(args.runs):
        start = time.perf_counter()
        scores = meaning.batch_similarity(QUERY, CANDIDATES)
        elapsed.append(time.perf_counter() - start)
        misses += scores is None
    elapsed.sort()
    p50 = elapsed[math.ceil(len(elapsed) * 0.50) - 1]
    p95 = elapsed[math.ceil(len(elapsed) * 0.95) - 1]
    print(f"{args.runs} runs, {1 + len(CANDIDATES)} texts per call, {misses} fallbacks")
    print(f"p50 {p50:.3f} s   p95 {p95:.3f} s")
    return int(misses != 0)


if __name__ == "__main__":
    raise SystemExit(main())

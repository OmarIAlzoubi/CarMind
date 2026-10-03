"""Offline demonstration adapter; all automotive work goes through CarMindApp."""

import argparse

from carmind.ownership_demo import main


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CarMind ownership MVP (offline scripted demonstration)")
    parser.add_argument("--demo", action="store_true", required=True,
                        help="Run the explicitly fake offline ownership journey")
    parser.parse_args()
    raise SystemExit(main())

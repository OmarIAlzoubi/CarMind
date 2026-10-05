"""Offline demonstration adapter; all automotive work goes through CarMindApp."""

import argparse
import sys

from carmind.ownership_demo import main


if __name__ == "__main__":
    if sys.argv[1:2] == ["manual"]:
        from carmind.manual_cli import main as manual_main
        raise SystemExit(manual_main(sys.argv[2:]))
    if sys.argv[1:2] == ["proactive"]:
        from carmind.proactive_cli import main as proactive_main
        raise SystemExit(proactive_main(sys.argv[2:]))
    parser = argparse.ArgumentParser(description="CarMind ownership MVP (offline scripted demonstration)")
    parser.add_argument("--demo", action="store_true", required=True,
                        help="Run the explicitly fake offline ownership journey")
    parser.parse_args()
    raise SystemExit(main())

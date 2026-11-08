"""CLI.

  python -m controller run    --sim-url http://127.0.0.1:8700 [--db state/controller.db]
  python -m controller why    nw_renewal --at "D3 14:00"        # why did it change, who approved, what next
  python -m controller heard  nw_renewal:p0004567 --day 3        # what a customer heard, and why it was live
  python -m controller view   [--wf ks_cancel] [--at "D5 12:00"] # supervisor view (one screen per workflow)
  python -m controller status                                    # loop health for on-call
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

DEFAULT_DB = os.environ.get("CONTROLLER_DB", "state/controller.db")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--sim-url", required=True)
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("--tick", type=int, default=None)
    w = sub.add_parser("why")
    w.add_argument("workflow")
    w.add_argument("--at", required=True, help='"D3 14:00" or a sim minute')
    w.add_argument("--db", default=DEFAULT_DB)
    h = sub.add_parser("heard")
    h.add_argument("person_or_lead")
    h.add_argument("--day", type=int, required=True)
    h.add_argument("--db", default=DEFAULT_DB)
    v = sub.add_parser("view")
    v.add_argument("--wf")
    v.add_argument("--at")
    v.add_argument("--db", default=DEFAULT_DB)
    s = sub.add_parser("status")
    s.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.cmd == "run":
        from .loop import run
        from .settings import SETTINGS
        run(a.sim_url, a.db, a.tick or SETTINGS.tick_minutes)
        return
    from .store.db import connect
    conn = connect(a.db)
    if a.cmd == "why":
        from .audit import why
        print(why(conn, a.workflow, a.at))
    elif a.cmd == "heard":
        from .audit import heard
        print(heard(conn, a.person_or_lead, a.day))
    elif a.cmd == "view":
        from .views import supervisor_view
        print(supervisor_view(conn, a.wf, a.at))
    elif a.cmd == "status":
        from .views import status
        print(status(conn))


if __name__ == "__main__":
    main()

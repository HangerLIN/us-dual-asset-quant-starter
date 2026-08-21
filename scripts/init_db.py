from __future__ import annotations

import argparse

from _bootstrap import ROOT  # noqa: F401
from _common import init_db, json_print

from platform_core.db import get_engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Create starter database tables.")
    parser.add_argument("--db-url", default=None, help="Override DATABASE_URL.")
    args = parser.parse_args()

    engine = get_engine(args.db_url)
    init_db(engine)
    json_print({"status": "ok", "database_url": str(engine.url.render_as_string(hide_password=True))})


if __name__ == "__main__":
    main()

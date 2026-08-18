from __future__ import annotations

import argparse
import logging
import time

from cvd.config import CLEANUP_INTERVAL_SECONDS, DB_PATH, RETENTION_DAYS
from cvd.database import cleanup_old_trades, connect, initialize


def clean_once() -> int:
    initialize(DB_PATH)
    connection = connect(DB_PATH)
    try:
        return cleanup_old_trades(connection, RETENTION_DAYS)
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete expired Binance aggregate trades")
    parser.add_argument("--once", action="store_true", help="clean once and exit")
    args = parser.parse_args()
    while True:
        deleted = clean_once()
        logging.info("Deleted %d trades older than %d days", deleted, RETENTION_DAYS)
        if args.once:
            return
        time.sleep(CLEANUP_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass
#!/usr/bin/env python3
"""Explicit standalone preparation worker. Disabled by default; never submits publications."""
from __future__ import annotations
import argparse
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from myUtils.repurpose.contracts import RepurposeError, require
from myUtils.repurpose.pipeline import run_one
from myUtils.repurpose.store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--rules-root', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    require(os.environ.get('SAU_TEACHING_REPURPOSE_ENABLED') == '1', 'repurpose_disabled')
    store = Store(args.db, args.root)
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        task = run_one(store, args.rules_root)
        if task:
            print(f"repurpose task={task['id']} state={task['state']}", flush=True)
        if args.once:
            return 0
        time.sleep(2)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RepurposeError as exc:
        print(exc.code, file=sys.stderr)
        raise SystemExit(2) from None

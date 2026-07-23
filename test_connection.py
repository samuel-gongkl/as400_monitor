"""Standalone connectivity tester for the AS400 monitor.

Run this right after installing the IBM i Access ODBC driver to confirm the
monitor can reach your IBM i partition. It checks, step by step:

  1. pyodbc is importable.
  2. The configured ODBC driver is registered on this machine.
  3. A connection can be opened with the credentials in .env.
  4. A trivial query (SELECT 1 FROM SYSIBM.SYSDUMMY1) succeeds.
  5. A sample of the real SYSTEM_STATUS metric can be read.

Usage:
    python test_connection.py

Reads the same .env / environment settings as the app. In mock mode it simply
reports that no live connection is configured.
"""
from __future__ import annotations

import sys

from config import Config


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def _ok(msg: str) -> None:
    print(f"{GREEN}[ OK ]{RESET} {msg}")


def _fail(msg: str) -> None:
    print(f"{RED}[FAIL]{RESET} {msg}")


def _info(msg: str) -> None:
    print(f"{YELLOW}[INFO]{RESET} {msg}")


def main() -> int:
    print("=" * 60)
    print(" AS400 / IBM i monitor - connection test")
    print("=" * 60)
    print(f"Mode   : {Config.MODE}")
    print(f"Driver : {Config.DRIVER}")
    print(f"Host   : {Config.HOST or '(not set)'}")
    print(f"User   : {Config.USER or '(not set)'}")
    print("-" * 60)

    if not Config.is_live():
        _info("AS400_MODE is not 'live' - nothing to connect to.")
        _info("Set AS400_MODE=live in .env to test a real connection.")
        return 0

    # Step 1: pyodbc available
    try:
        import pyodbc
    except ImportError:
        _fail("pyodbc is not installed. Run: pip install -r requirements.txt")
        return 1
    _ok(f"pyodbc {pyodbc.version} imported")

    # Step 2: driver registered
    drivers = list(pyodbc.drivers())
    if Config.DRIVER in drivers:
        _ok(f"ODBC driver '{Config.DRIVER}' is registered")
    else:
        _fail(f"ODBC driver '{Config.DRIVER}' NOT found")
        print(f"{DIM}     Installed drivers:{RESET}")
        for d in drivers:
            print(f"{DIM}       - {d}{RESET}")
        ibm = [d for d in drivers if "IBM i" in d or "iSeries" in d]
        if ibm:
            _info(f"Found IBM i-like driver(s): {ibm}")
            _info("Update AS400_DRIVER in .env to match one of the above.")
        else:
            _info("Install IBM i Access Client Solutions (ACS) with the "
                  "64-bit ODBC component, then re-run this test.")
        return 1

    # Step 3 & 4: connect + trivial query (reuse the app's db layer)
    from db import run_query, AS400Error, reset_connection
    reset_connection()
    try:
        run_query("SELECT 1 AS OK FROM SYSIBM.SYSDUMMY1")
        _ok(f"Connected to '{Config.HOST}' and ran test query")
    except AS400Error as exc:
        _fail(f"Connection/query failed: {exc}")
        _info("Check host/IP, user/password, firewall (ports 446/8471), "
              "and that the user profile is enabled.")
        return 1

    # Step 5: read a real metric
    try:
        rows = run_query(
            "SELECT AVERAGE_CPU_UTILIZATION, ACTIVE_JOBS_IN_SYSTEM "
            "FROM TABLE(QSYS2.SYSTEM_STATUS(RESET_STATISTICS => 'NO')) X"
        )
        if rows:
            r = rows[0]
            _ok(f"SYSTEM_STATUS read: CPU={r.get('AVERAGE_CPU_UTILIZATION')}%, "
                f"active jobs={r.get('ACTIVE_JOBS_IN_SYSTEM')}")
    except AS400Error as exc:
        _fail(f"Could read connection but not SYSTEM_STATUS: {exc}")
        _info("The user profile may lack authority on QSYS2 services.")
        return 1

    print("-" * 60)
    _ok("All checks passed - the dashboard should work in live mode.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

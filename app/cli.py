"""Small admin CLI.

  python -m app.cli gen-keys        # print FERNET_KEY and SESSION_SECRET
  python -m app.cli hash-password   # prompt for a password, print ADMIN_PASSWORD_HASH
  python -m app.cli init-db         # alembic upgrade head + seed default niches
  python -m app.cli dry-run-check   # run the dry-run end-to-end test suite
"""
from __future__ import annotations

import getpass
import secrets
import subprocess
import sys

from cryptography.fernet import Fernet


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "gen-keys":
        print(f"FERNET_KEY={Fernet.generate_key().decode()}")
        print(f"SESSION_SECRET={secrets.token_urlsafe(32)}")
    elif cmd == "hash-password":
        from app.security import hash_password
        pw = getpass.getpass("Password: ")
        if len(pw) < 10:
            print("Use at least 10 characters.", file=sys.stderr)
            return 1
        if pw != getpass.getpass("Repeat: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
        print(f"ADMIN_PASSWORD_HASH='{hash_password(pw)}'")
    elif cmd == "init-db":
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
        from sqlmodel import Session
        from app.db import get_engine
        from app.services.niches import seed_niches
        with Session(get_engine()) as s:
            n = seed_niches(s)
            s.commit()
        print(f"Database ready. Seeded {n} niches.")
    elif cmd == "dry-run-check":
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_e2e_dry_run.py"]).returncode
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Delete heavy intermediates of finished jobs. Keeps OCR cache + output artifacts.

PYTHONPATH=.deps:. python3 tools/prune.py
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app import db, storage
from app.settings import Settings

ACTIVE = ("running", "queued", "paused", "finalizing")


def main() -> int:
    settings = Settings()
    db.init_db(settings.resolved_database_url())
    s = db.get_session()
    try:
        keep = {j.id for j in s.execute(select(db.Job).where(db.Job.status.in_(ACTIVE))).scalars().all()}
    finally:
        s.close()
    before = storage.free_mb(settings.storage_dir)
    res = storage.prune_work(settings, keep)
    after = storage.free_mb(settings.storage_dir)
    print(
        f"removed {res['removed']} dirs, ~{res['freed_mb']} MB "
        f"(free {before} -> {after} MB; kept {len(keep)} active jobs)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

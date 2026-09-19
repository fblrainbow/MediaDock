"""MediaDock test package.

Stage-005: unit tests must never touch the real database file, so the
storage path is fixed to an in-memory SQLite database *before* any test
module imports `server`. Probes that need a real file pass an explicit path.
"""
import os

os.environ.setdefault("MEDIADOCK_DB", ":memory:")


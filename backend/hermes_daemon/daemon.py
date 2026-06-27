"""Compatibility wrapper for the Hermes daemon.

The implementation lives in ``backend.hermes.daemon``. This module remains so
existing local launch commands such as ``python -m backend.hermes_daemon.daemon``
continue to work.
"""

from backend.hermes.daemon import *  # noqa: F401,F403
from backend.hermes.daemon import main


if __name__ == "__main__":
    main()

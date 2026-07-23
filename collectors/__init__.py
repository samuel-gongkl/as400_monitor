"""Collector package.

Importing this package registers all built-in collectors. To add a new
monitor, create a module here and import it below.
"""
from collectors.base import (  # noqa: F401
    Collector,
    collect_all,
    all_collectors,
    get_collector,
    register,
)

# Import side-effect: each module registers its collector(s).
from collectors import system_status  # noqa: F401,E402
from collectors import memory  # noqa: F401,E402
from collectors import disk  # noqa: F401,E402
from collectors import subsystems  # noqa: F401,E402
from collectors import highest_cpu_jobs  # noqa: F401,E402
from collectors import messages  # noqa: F401,E402

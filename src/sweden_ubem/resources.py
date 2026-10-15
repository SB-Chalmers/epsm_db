"""Locate owned runtime files in a source checkout or an installed wheel."""

import sysconfig
from pathlib import Path


def resource_path(relative: str) -> Path:
    """Resolve packaged application resources, never external source datasets."""
    module = Path(__file__).resolve()
    checkout = module.parents[2]
    if module.parent == checkout / "src/sweden_ubem":
        return checkout / relative
    return Path(sysconfig.get_path("data")) / "share/epsm_db" / relative

"""Building-first access to Swedish building evidence."""

import logging as _stdlib_logging

from .enums import BuildingType
from .epc_heating import EPCHeatingTarget, heating_target_from_record
from .explore import UBEM, Building
from .records import BuildingRecord, Receipt, RecordCollection
from .stock import Stock, StockCell
from .version import SCHEMA_VERSION, VERSION

__all__ = [
    "SCHEMA_VERSION",
    "UBEM",
    "Building",
    "BuildingRecord",
    "BuildingType",
    "EPCHeatingTarget",
    "Receipt",
    "RecordCollection",
    "Stock",
    "StockCell",
    "heating_target_from_record",
]
__version__ = VERSION

# A library emits no output until its host selects handlers.

_stdlib_logging.getLogger("sweden_ubem").addHandler(_stdlib_logging.NullHandler())

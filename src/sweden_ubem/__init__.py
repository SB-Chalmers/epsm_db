"""Building-first access to Swedish building evidence."""

from .explore import Building, UBEM
from .version import SCHEMA_VERSION, VERSION
from .enums import BuildingType
from .epc_heating import EPCHeatingTarget, heating_target_from_record
from .records import BuildingRecord, RecordCollection, Receipt
from .stock import Stock, StockCell

__all__ = [
    "Building",
    "UBEM",
    "SCHEMA_VERSION",
    "BuildingType",
    "EPCHeatingTarget",
    "heating_target_from_record",
    "BuildingRecord",
    "RecordCollection",
    "Receipt",
    "Stock",
    "StockCell",
]
__version__ = VERSION

"""Building-first access to Swedish building evidence."""

from .explore import Building, UBEM
from .version import SCHEMA_VERSION, VERSION
from .enums import BuildingType
from .epc_heating import EPCHeatingTarget, heating_target_from_record
from .records import BuildingRecord, RecordCollection, Receipt

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
]
__version__ = VERSION

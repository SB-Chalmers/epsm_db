"""Evidence database API. The existing ingestion implementation remains sweden_ubem.

No connection is opened on import. Database, Stock and BuildingRecord refer to
the existing implementation classes, so a second stock/record schema is avoided.
"""

__version__ = "0.3.0"
__all__ = ["Database", "Stock", "StockCell", "BuildingRecord", "Receipt", "BuildingType"]


def __getattr__(name):
    if name == "Database":
        from sweden_ubem.explore import UBEM
        return UBEM
    if name in {"Stock", "StockCell"}:
        from sweden_ubem import stock
        return getattr(stock, name)
    if name in {"BuildingRecord", "Receipt"}:
        from sweden_ubem import records
        return getattr(records, name)
    if name == "BuildingType":
        from sweden_ubem.enums import BuildingType
        return BuildingType
    raise AttributeError(name)

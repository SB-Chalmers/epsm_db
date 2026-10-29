"""GIS evidence, lazy building stocks and explicit EPSM conversion.

Importing this package opens no connection and loads no modelling catalogue.
"""
__version__ = "0.5.0"
__all__ = ["Lake", "Database", "Stock", "StockCell", "BuildingRecord", "Receipt", "BuildingType",
           "PublicationPendingError", "RecordNotFoundError", "BoundaryUnavailableError"]


def __getattr__(name):
    if name == "Lake":
        from .lake import Lake
        return Lake
    if name == "Database":
        from .database import Database
        return Database
    if name in {"Stock", "StockCell"}:
        from . import stock
        return getattr(stock, name)
    if name in {"BuildingRecord", "Receipt"}:
        from . import records
        return getattr(records, name)
    if name == "BuildingType":
        from .enums import BuildingType
        return BuildingType
    if name in {"PublicationPendingError", "RecordNotFoundError", "BoundaryUnavailableError"}:
        from . import errors
        return getattr(errors, name)
    raise AttributeError(name)

"""Compatibility forwarding for the pre-harmonisation record adapter.

New code uses epsm.Building.from_record; model assembly is owned by EPSM.
"""
from epsm import legacy_records as _implementation

def __getattr__(name):
    return getattr(_implementation, name)

__all__ = [name for name in dir(_implementation) if not name.startswith("_")]

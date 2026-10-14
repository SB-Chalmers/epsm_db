"""Lazy stock exploration over a named database dataset and EPSM catalogue."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from .epc_heating import heating_target_from_record
from .database import _connect
from .io import fetch_records

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class Stock:
    """A database-backed building collection with a pinned archetype catalogue."""

    db: object
    catalogue: object = field(init=False)
    _count: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        from epsm_data.parametric import load_parametric_catalogue

        self.catalogue = load_parametric_catalogue()

    def __repr__(self):
        count = "lazy" if self._count is None else str(self._count)
        return (
            f"Stock(dataset={self.db.dataset_id!r}, "
            f"catalogue={self.catalogue.dataset_id!r}, buildings={count})"
        )

    def __len__(self):
        if self._count is None:
            with _connect(self.db.database_url) as connection:
                self._count = connection.execute(
                    "SELECT count(*) FROM mart.building_facts WHERE dataset_id=%s",
                    (self.db.dataset_id,),
                ).fetchone()[0]
        return self._count

    def record(self, building_id):
        """Fetch one full building record by UUID."""
        return self.db.building(building_id).record()

    def matrix(self, *, building_type=None):
        """Count buildings by catalogue category and register-year era."""
        return self.db.archetype_matrix(building_type=building_type)

    def cell(
        self, *, building_type, era, ventilation=None, climate_zone=None,
        renovation_state="existing",
    ):
        """Select records and declare optional model-catalogue dimensions."""
        return StockCell(
            self, building_type, era, ventilation, climate_zone, renovation_state
        )

    def plot_heating(self, *, building_type, era, ventilation=None, **plot_kwargs):
        """Plot EPC reported heating for a stock cell."""
        return self.cell(
            building_type=building_type, era=era, ventilation=ventilation
        ).plot_heating(**plot_kwargs)

    def plot_atemp(self, *, building_type, era, ventilation=None, **plot_kwargs):
        """Plot reported heated area for a stock cell."""
        return self.cell(
            building_type=building_type, era=era, ventilation=ventilation
        ).plot_atemp(**plot_kwargs)

    def iter_records(self, *, batch_size=100):
        """Stream all dataset building records in bounded UUID-ordered pages."""
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or not 1 <= batch_size <= 10000
        ):
            raise ValueError("batch_size must be between 1 and 10000")
        reader = fetch_records.using(self.db.database_url, dataset=self.db.dataset_id)
        cursor = None
        while True:
            page = reader.all(
                include_support=True,
                include_other_statuses=True,
                limit=batch_size,
                after_uuid=cursor,
            )
            yield from page
            if not page.receipt.truncated:
                break
            cursor = page.receipt.next_cursor

    def __iter__(self):
        return self.iter_records()


@dataclass(eq=False)
class StockCell:
    """A lazy selection whose records retain their source evidence and receipts."""

    stock: Stock
    building_type: str
    era: str
    ventilation: str | None = None
    climate_zone: str | None = None
    renovation_state: str = "existing"
    _members: pd.DataFrame | None = field(default=None, init=False, repr=False)
    _frame: pd.DataFrame | None = field(default=None, init=False, repr=False)
    _heating: pd.DataFrame | None = field(default=None, init=False, repr=False)
    _components: pd.DataFrame | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        dimensions = self.stock.catalogue.dimensions
        if self.building_type not in dimensions["categories"]:
            raise ValueError(f"Unknown EPSM building type: {self.building_type}")
        if self.era not in dimensions["eras"]:
            raise ValueError(f"Unknown EPSM era: {self.era}")
        if self.ventilation is not None and self.ventilation not in dimensions["ventilation"]:
            raise ValueError(f"Unknown EPSM ventilation system: {self.ventilation}")
        if self.climate_zone is not None and self.climate_zone not in dimensions["zones"]:
            raise ValueError(f"Unknown EPSM climate zone: {self.climate_zone}")
        if self.renovation_state not in dimensions["states"]:
            raise ValueError(f"Unknown EPSM renovation state: {self.renovation_state}")

    def __repr__(self):
        count = "lazy" if self._members is None else str(len(self._members))
        return (
            f"StockCell(dataset={self.stock.db.dataset_id!r}, "
            f"type={self.building_type!r}, era={self.era!r}, "
            f"ventilation={self.ventilation!r}, buildings={count})"
        )

    @property
    def archetype_slot(self):
        """Resolved catalogue slot when zone and ventilation are specified."""
        if self.climate_zone is None or self.ventilation is None:
            raise ValueError("Specify climate_zone and ventilation for a full archetype slot")
        from epsm_data.parametric import ArchetypeKey

        return self.stock.catalogue.get_slot(ArchetypeKey(
            self.building_type, self.era, self.climate_zone,
            self.renovation_state, self.ventilation,
        ))

    @property
    def members(self):
        """Building IDs and reported ventilation for this cell."""
        if self._members is None:
            rows = self.stock.db.ventilation_buildings(
                building_type=self.building_type, era=self.era
            )
            if self.ventilation is not None:
                rows = rows.loc[rows.ventilation_system.eq(self.ventilation)]
            self._members = rows.reset_index(drop=True)
            logger.debug("Selected %d buildings for %r", len(self._members), self)
        return self._members.copy()

    def __len__(self):
        return len(self.members)

    def ventilation_counts(self):
        """Count reported systems and unresolved evidence within this type/era cell."""
        labels = ["S", "F", "FT", "FTX", "No linked EPC", "Unresolved EPC ventilation"]
        counts = self.members.ventilation_system.value_counts()
        return pd.DataFrame(
            {"buildings": [int(counts.get(label, 0)) for label in labels]},
            index=pd.Index(labels, name="ventilation_system"),
        )

    def iter_records(self, *, batch_size=100):
        """Hydrate selected building records in batches, without loading the whole stock."""
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or not 1 <= batch_size <= 10000
        ):
            raise ValueError("batch_size must be between 1 and 10000")
        reader = fetch_records.using(self.stock.db.database_url, dataset=self.stock.db.dataset_id)
        ids = self.members.building_id.tolist()
        for offset in range(0, len(ids), batch_size):
            page = reader.from_uuids(ids[offset : offset + batch_size])
            if page.receipt.truncated or len(page) != len(ids[offset : offset + batch_size]):
                raise LookupError("Stock cell changed while fetching records; rerun the selection")
            logger.debug("Hydrated %d records for %r", len(page), self)
            yield from page

    def __iter__(self):
        return self.iter_records()

    def records(self):
        """Materialize this cell's records; use iteration for larger cells."""
        return tuple(self.iter_records())

    def frame(self):
        """Building properties; component heights appear only for single-part buildings."""
        if self._frame is not None:
            return self._frame.copy()
        ventilation = dict(zip(self.members.building_id.map(str), self.members.ventilation_system))
        rows = []
        for record in self:
            component = record.components[0] if len(record.components) == 1 else None
            matched = [
                height for height in record.heights
                if component is not None
                and height.get("component_id") == component.get("component_id")
                and height.get("input_geometry_hash") == component.get("geometry_hash")
                and height.get("height_status") == "determined"
            ]
            height = matched[0] if len(matched) == 1 else {}
            rows.append({
                "building_id": record.building_id,
                "building_type": record.type.value,
                "construction_year": record.facts.get("construction_year"),
                "ventilation_system": ventilation[record.building_id],
                "footprint_m2": record.geometry.area if record.geometry is not None else None,
                "atemp_m2": record.atemp_m2,
                "storeys": record.storeys,
                "basement_storeys": record.basement_storeys,
                "component_count": len(record.components),
                "height_m": height.get("representative_height_m"),
                "eave_height_m": height.get("eave_height_m"),
                "ridge_height_m": height.get("ridge_height_m"),
                "roof_type": height.get("roof_form"),
                "epc_certificate_count": record.facts.get("epc_certificate_count"),
                "status_verified": record.facts.get("status_verified"),
                "is_existing": record.facts.get("is_existing"),
            })
        self._frame = pd.DataFrame(rows)
        return self._frame.copy()

    def component_frame(self):
        """One row per geometry component, with its own matched height evidence."""
        if self._components is not None:
            return self._components.copy()
        rows = []
        for record in self:
            for component in record.components:
                matched = [
                    height for height in record.heights
                    if height.get("component_id") == component.get("component_id")
                    and height.get("input_geometry_hash") == component.get("geometry_hash")
                ]
                height = matched[0] if len(matched) == 1 else {}
                determined = height.get("height_status") == "determined"
                rows.append({
                    "building_id": record.building_id,
                    "component_id": component["component_id"],
                    "footprint_m2": component["geometry"].area if component.get("geometry") is not None else None,
                    "height_status": height.get("height_status") if height else "missing_or_ambiguous",
                    "height_m": height.get("representative_height_m") if determined else None,
                    "eave_height_m": height.get("eave_height_m") if determined else None,
                    "ridge_height_m": height.get("ridge_height_m") if determined else None,
                    "roof_type": height.get("roof_form") if determined else None,
                })
        self._components = pd.DataFrame(rows)
        return self._components.copy()

    def epc_heating(self):
        """One row per building with comparable reported heating or an exclusion."""
        if self._heating is not None:
            return self._heating.copy()
        rows = []
        for record in self:
            row = {"building_id": record.building_id, "reported_heating_kwh": None,
                   "reported_heating_kwh_m2": None, "atemp_m2": record.atemp_m2,
                   "carrier": None, "exclusion": None, "epc_regime": None,
                   "reported_heating_basis": None, "combined_heating_dhw_kwh": None,
                   "dhw_subtracted_kwh": None}
            try:
                target = heating_target_from_record(record)
            except ValueError as exc:
                row["exclusion"] = str(exc)
            else:
                row["carrier"] = target.carrier
                row["epc_regime"] = target.regime
                row["reported_heating_basis"] = target.reported_heating_basis
                row["combined_heating_dhw_kwh"] = target.combined_heating_dhw_kwh
                row["dhw_subtracted_kwh"] = target.dhw_subtracted_kwh
                row["reported_heating_kwh"] = target.reported_period_heating_kwh
                if target.reported_period_heating_kwh is None:
                    row["exclusion"] = ", ".join(target.exclusions)
                elif row["atemp_m2"] is not None and row["atemp_m2"] > 0:
                    row["reported_heating_kwh_m2"] = (
                        target.reported_period_heating_kwh / row["atemp_m2"]
                    )
            rows.append(row)
        self._heating = pd.DataFrame(rows)
        return self._heating.copy()

    def plot_heating(self, *, per_area=False, bins=20, ax=None):
        """Histogram of EPC reported delivered heating, optionally per Atemp."""
        import matplotlib.pyplot as plt

        values = self.epc_heating()
        column = "reported_heating_kwh_m2" if per_area else "reported_heating_kwh"
        values = values[column].dropna()
        if values.empty:
            raise ValueError("No comparable EPC reported heating in this stock cell")
        if not per_area:
            values = values / 1000
        if ax is None:
            _, ax = plt.subplots(figsize=(7, 4.5))
        ax.hist(values, bins=bins, color="#4477AA", edgecolor="white")
        median = values.median()
        ax.axvline(median, color="#BB5566", linestyle="--", linewidth=2,
                   label=f"Median {median:,.1f}")
        ax.set(
            title=(
                f"EPC reported heating {'per Atemp' if per_area else 'per building'} "
                f"· {self.building_type} · {self.era} (n={len(values)})"
            ),
            xlabel="kWh/m²·year" if per_area else "MWh per building",
            ylabel="Buildings",
        )
        ax.legend(frameon=False)
        ax.grid(axis="y", alpha=0.2)
        return ax

    def plot_atemp(self, *, bins=20, ax=None):
        """Histogram of positive EPC reported heated area (Atemp)."""
        import matplotlib.pyplot as plt

        values = self.frame()["atemp_m2"].dropna()
        values = values.loc[values > 0]
        if values.empty:
            raise ValueError("No positive reported Atemp in this stock cell")
        if ax is None:
            _, ax = plt.subplots(figsize=(7, 4.5))
        ax.hist(values, bins=bins, color="#44AA99", edgecolor="white")
        median = values.median()
        ax.axvline(median, color="#BB5566", linestyle="--", linewidth=2,
                   label=f"Median {median:,.0f} m²")
        ax.set(
            title=f"Reported Atemp · {self.building_type} · {self.era} (n={len(values)})",
            xlabel="Atemp (m²)", ylabel="Buildings",
        )
        ax.legend(frameon=False)
        ax.grid(axis="y", alpha=0.2)
        return ax

    def to_epsm(self, *, weather, **kwargs):
        """Assemble models with explicit weather; return models and all receipts."""
        from .epsm import EPSMBuilding

        overrides = dict(kwargs.pop("overrides", None) or {})
        declared = {"renovation_state": self.renovation_state}
        if self.climate_zone is not None:
            declared["climate_zone"] = self.climate_zone
        if self.ventilation is not None:
            declared["ventilation"] = self.ventilation
        for key, value in declared.items():
            if key in overrides and overrides[key] != value:
                raise ValueError(f"Override {key} conflicts with the stock cell")
        return EPSMBuilding.from_records(
            self.iter_records(), weather=weather, overrides={**declared, **overrides}, **kwargs
        )

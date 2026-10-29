"""Lazy geographic stocks, catalogue cells and explicit physical conversion."""

import hashlib
import json
import math
import warnings
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from functools import cached_property
from importlib.resources import files

import geopandas as gpd
import pandas as pd
from psycopg import sql
from shapely import from_wkb

from ._logging import call_with_context, operation
from .errors import BoundaryUnavailableError, RecordNotFoundError
from .records import Receipt

VENTILATION = ("S", "F", "FT", "FTX", "No linked EPC", "Unresolved EPC ventilation")
# Source types outside the authored catalogue keep their own cohort ("generic"); only
# their catalogue fabric selection borrows the matching multifamily representative.
GENERIC = "generic"
BUILDING_LAYERS = ("buildings", "component_heights", "properties", "addresses", "epc_links")


def _limit(value, name="limit", maximum=100000):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _municipality(value):
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit() and len(text) <= 4:
        return text.zfill(4)
    names = json.loads(files("epsm_db").joinpath("municipality_names.json").read_text())[
        "municipalities"
    ]
    matches = [code for code, name in names.items() if name.casefold() == text.casefold()]
    if len(matches) != 1:
        raise ValueError(
            "Choose a four-digit municipality code or an unambiguous municipality name"
        )
    return matches[0]


class Stock:
    """A lazy selection. repr performs no queries, catalogue loading or counting."""

    def __init__(
        self,
        db,
        *,
        municipality=None,
        bbox=None,
        area_type=None,
        area_code=None,
        boundary_version=None,
        _filters=None,
    ):
        self.db = db
        if _filters is not None:
            self._filters = dict(_filters)
            return
        if (area_type is None) != (area_code is None):
            raise ValueError("Supply area_type and area_code together")
        if boundary_version is not None and area_type is None:
            raise ValueError("boundary_version needs an explicit area selection")
        if bbox is not None:
            bbox = tuple(float(v) for v in bbox)
            if (
                len(bbox) != 4
                or not all(math.isfinite(v) for v in bbox)
                or bbox[0] >= bbox[2]
                or bbox[1] >= bbox[3]
            ):
                raise ValueError("bbox must be finite (xmin,ymin,xmax,ymax) in EPSG:3006")
        self._filters = {
            "municipality": _municipality(municipality),
            "bbox": bbox,
            "area_type": area_type,
            "area_code": str(area_code) if area_code is not None else None,
            "boundary_version": str(boundary_version) if boundary_version is not None else None,
        }

    def __repr__(self):
        from .records import _display_label

        return f"{type(self).__name__}(municipality={_display_label(self._filters.get('municipality'))}; lazy)"

    def describe(self):
        return {
            k: deepcopy(v)
            for k, v in self._filters.items()
            if v is not None and k != "radius_geometry"
        }

    @cached_property
    def catalogue(self):
        try:
            from epsm_data import Catalogue
        except ImportError as error:
            raise ImportError(
                "Catalogue operations require epsm_db[catalogue] with EPSM Data 0.7.0+."
            ) from error
        return call_with_context(Catalogue.load)

    def _where(self, connection):
        clauses, values = [sql.SQL("true")], []
        filters = self._filters
        if filters.get("municipality") is not None:
            clauses.append(sql.SQL("f.municipality_code=%s"))
            values.append(filters["municipality"])
        if filters.get("bbox") is not None:
            clauses.append(
                sql.SQL(
                    "b.geom && ST_MakeEnvelope(%s,%s,%s,%s,3006) AND ST_Intersects(b.geom,ST_MakeEnvelope(%s,%s,%s,%s,3006))"
                )
            )
            values.extend(filters["bbox"] * 2)
        if filters.get("area_type") is not None:
            query = "SELECT fid FROM gis.boundaries WHERE area_type=%s AND area_code=%s"
            parameters = [filters["area_type"], filters["area_code"]]
            if filters.get("boundary_version") is not None:
                query += " AND boundary_version=%s"
                parameters.append(filters["boundary_version"])
            found = connection.execute(query, parameters).fetchall()
            if len(found) != 1:
                raise BoundaryUnavailableError(
                    "Expected one boundary; choose its boundary_version or import the boundary first."
                )
            clauses.append(
                sql.SQL(
                    "EXISTS (SELECT 1 FROM gis.boundaries a WHERE a.fid=%s AND b.geom && a.geom AND ST_Intersects(b.geom,a.geom))"
                )
            )
            values.append(found[0]["fid"])
        if filters.get("building_type") is not None:
            if filters["building_type"] == "generic":
                clauses.append(
                    sql.SQL(
                        "f.building_type NOT IN ('single_family','multi_family','school','office')"
                    )
                )
            else:
                clauses.append(sql.SQL("f.building_type=%s"))
                values.append(filters["building_type"])
        if filters.get("era") is not None:
            start, end = self.catalogue.dimensions["eras"][filters["era"]]
            clauses.append(sql.SQL("f.construction_year>=%s"))
            values.append(start)
            if end is not None:
                clauses.append(sql.SQL("f.construction_year<=%s"))
                values.append(end)
        if filters.get("ventilation") is not None:
            clauses.append(sql.SQL("f.ventilation_system=%s"))
            values.append(filters["ventilation"])
        if filters.get("uuid") is not None:
            clauses.append(sql.SQL("f.building_id=%s"))
            values.append(str(filters["uuid"]))
        if filters.get("exclude") is not None:
            clauses.append(sql.SQL("f.building_id<>%s"))
            values.append(str(filters["exclude"]))
        if filters.get("radius_geometry") is not None:
            clauses.append(sql.SQL("ST_DWithin(b.geom,ST_GeomFromWKB(%s,3006),%s)"))
            values.extend([filters["radius_geometry"], filters["radius_m"]])
        return sql.SQL(" AND ").join(clauses), values

    def _selection_sql(self, connection):
        where, values = self._where(connection)
        return sql.SQL(
            "SELECT f.fid FROM gis.model_facts f JOIN gis.model_buildings b ON b.building_id=f.fid WHERE {}"
        ).format(where), values

    def __len__(self):
        with self.db._read() as (connection, _):
            selected, values = self._selection_sql(connection)
            return connection.execute(
                sql.SQL("SELECT count(*) AS n FROM ({}) selection").format(selected), values
            ).fetchone()["n"]

    def within_radius(self, geometry, *, radius_m=100, exclude=None):
        if not hasattr(geometry, "is_valid") or geometry.is_empty or not geometry.is_valid:
            raise ValueError("Supply a valid, nonempty EPSG:3006 Shapely geometry")
        if isinstance(radius_m, bool) or not math.isfinite(float(radius_m)) or float(radius_m) <= 0:
            raise ValueError("radius_m must be finite and positive")
        filters = dict(
            self._filters, radius_geometry=geometry.wkb, radius_m=float(radius_m), exclude=exclude
        )
        return Stock(self.db, _filters=filters)

    def cell(
        self, building_type, era, *, ventilation=None, climate_zone=None, renovation_state=None
    ):
        return StockCell(
            self,
            building_type,
            era,
            ventilation=ventilation,
            climate_zone=climate_zone,
            renovation_state=renovation_state,
        )

    @operation("database.stock.matrix", level=10)
    def matrix(self, *, building_type=None):
        catalogue = self.catalogue
        eras = dict(catalogue.dimensions["eras"])
        categories = list(dict.fromkeys((*catalogue.dimensions["categories"], GENERIC)))
        if building_type is not None:
            if building_type not in categories:
                raise ValueError("Unknown catalogue building type")
            categories = [building_type]
        with self.db._read() as (connection, _):
            where, values = self._where(connection)
            rows = connection.execute(
                sql.SQL("""SELECT f.building_type,f.construction_year,count(*) AS n
                FROM gis.model_facts f JOIN gis.model_buildings b ON b.building_id=f.fid
                WHERE {} GROUP BY f.building_type,f.construction_year""").format(where),
                values,
            )
            counts = Counter()
            for row in rows:
                category = (
                    row["building_type"]
                    if row["building_type"] in catalogue.dimensions["categories"]
                    else GENERIC
                )
                year = row["construction_year"]
                era = (
                    catalogue.era_for_year(year)
                    if year is not None and year > 0
                    else "Missing year"
                )
                counts[category, era] += row["n"]
        columns = [*eras, "Missing year"]
        frame = pd.DataFrame(
            [[counts[c, e] for e in columns] for c in categories],
            index=pd.Index(categories, name="building_type"),
            columns=columns,
        )
        frame["Total"] = frame.sum(axis=1)
        if building_type is None:
            frame.loc["TOTAL"] = frame.sum(axis=0)
        return frame

    def ventilation_counts(self):
        with self.db._read() as (connection, _):
            where, values = self._where(connection)
            rows = connection.execute(
                sql.SQL("""SELECT f.ventilation_system,count(*) AS n
                FROM gis.model_facts f JOIN gis.model_buildings b ON b.building_id=f.fid
                WHERE {} GROUP BY f.ventilation_system""").format(where),
                values,
            )
            counts = {r["ventilation_system"]: r["n"] for r in rows}
        return pd.DataFrame(
            {"buildings": [counts.get(v, 0) for v in VENTILATION]},
            index=pd.Index(VENTILATION, name="ventilation_system"),
        )

    def record(self, building_id):
        filters = dict(self._filters, uuid=str(building_id))
        records = Stock(self.db, _filters=filters).iter_records(batch_size=1)
        try:
            return next(records)
        except StopIteration as error:
            raise RecordNotFoundError(
                Receipt("selected stock", str(building_id), request=self.describe())
            ) from error
        finally:
            records.close()

    @operation("database.stock.read", level=10)
    def iter_records(self, *, batch_size=100):
        from .hydrate import hydrate

        _limit(batch_size, "batch_size", 10000)
        with self.db._read() as (connection, publication):
            where, values = self._where(connection)
            cursor = 0
            while True:
                query = sql.SQL("""SELECT f.*,b.attributes,b.purposes,b.house_number,b.object_version,
                    b.valid_from,b.registry_file_id,b.registry_source_record,ST_AsBinary(b.geom) AS geometry_wkb
                    FROM gis.model_facts f JOIN gis.model_buildings b ON b.building_id=f.fid
                    WHERE {} AND f.fid>%s ORDER BY f.fid LIMIT %s""").format(where)
                rows = connection.execute(query, [*values, cursor, batch_size]).fetchall()
                if not rows:
                    break
                yield from hydrate(
                    connection, rows, publication, self.describe(), self.db._lake.url
                )
                cursor = rows[-1]["fid"]

    def __iter__(self):
        return self.iter_records()

    def records(self):
        """Explicitly materialize all records; prefer iter_records for large selections."""
        return tuple(self.iter_records())

    def _frame(self, *, component=False, limit=1000, after=None):
        _limit(limit)
        if after is not None and (isinstance(after, bool) or not str(after).isdigit()):
            raise ValueError("after must be a nonnegative numeric cursor")
        with self.db._read() as (connection, publication):
            where, values = self._where(connection)
            if component:
                columns = """c.component_id AS fid,f.building_id,c.external_id AS component_external_id,
                    f.municipality_code,h.height_status,h.representative_height_m,h.eave_height_m,
                    h.ridge_height_m,h.roof_form,ST_AsBinary(c.geom) AS geometry_wkb"""
                joins = """JOIN gis.model_components c ON c.building_id=f.fid
                    LEFT JOIN gis.component_heights h ON h.fid=c.component_id"""
                key = "c.component_id"
            else:
                columns, joins, key = "f.*,ST_AsBinary(b.geom) AS geometry_wkb", "", "f.fid"
            query = sql.SQL(
                "SELECT "
                + columns
                + " FROM gis.model_facts f JOIN gis.model_buildings b ON b.building_id=f.fid "
                + joins
                + " WHERE {} AND "
                + key
                + ">%s ORDER BY "
                + key
                + " LIMIT %s"
            ).format(where)
            cursor = connection.execute(query, [*values, int(after or 0), limit + 1])
            names = [column.name for column in cursor.description]
            rows = cursor.fetchall()
            truncated = len(rows) > limit
            rows = rows[:limit]
            data = pd.DataFrame(rows, columns=names)
            geometry = [
                from_wkb(bytes(v)) if v is not None else None for v in data.pop("geometry_wkb")
            ]
            frame = gpd.GeoDataFrame(data, geometry=geometry, crs="EPSG:3006")
            receipt = Receipt(
                publication["dataset_id"],
                request=self.describe(),
                snapshot=publication,
                truncated=truncated,
                next_cursor=str(rows[-1]["fid"]) if truncated else None,
            )
            frame.attrs["receipt"] = receipt
            if truncated:
                warnings.warn(
                    f"Preview limited to {limit} rows. Use after={receipt.next_cursor}, iteration or extract for the complete selection.",
                    UserWarning,
                    stacklevel=3,
                )
            return frame

    @operation("database.stock.preview", level=10)
    def frame(self, *, limit=1000, after=None):
        return self._frame(limit=limit, after=after)

    @operation("database.stock.component_preview", level=10)
    def component_frame(self, *, limit=1000, after=None):
        return self._frame(component=True, limit=limit, after=after)

    def extract(self, output, *, layers=BUILDING_LAYERS, batch_rows=10000):
        filters = self._filters
        return self.db._lake.extract(
            output,
            layers=layers,
            batch_rows=batch_rows,
            _stock=self,
            municipality_code=filters.get("municipality"),
            bbox=filters.get("bbox"),
            area_type=filters.get("area_type"),
            area_code=filters.get("area_code"),
            boundary_version=filters.get("boundary_version"),
        )

    def epc_heating(self):
        """Explicit complete tabulation; exclusions preserve shared/uncertain certificate scope."""
        from .epc_heating import heating_target_from_record

        rows = []
        for record in self:
            target = heating_target_from_record(record) if record.single_building_epc else None
            amount = target.reported_period_heating_kwh if target is not None else None
            exclusions = (
                target.exclusions if target is not None else ("unresolved_certificate_scope",)
            )
            area = record.atemp_m2
            rows.append(
                {
                    "building_id": record.building_id,
                    "atemp_m2": area,
                    "reported_heating_kwh": amount,
                    "reported_heating_kwh_m2": amount / area
                    if amount is not None and area and area > 0
                    else None,
                    "exclusions": exclusions,
                }
            )
        return pd.DataFrame(
            rows,
            columns=[
                "building_id",
                "atemp_m2",
                "reported_heating_kwh",
                "reported_heating_kwh_m2",
                "exclusions",
            ],
        )

    def plot_heating(self, *, per_area=False, bins=20, ax=None):
        values = self.epc_heating()[
            "reported_heating_kwh_m2" if per_area else "reported_heating_kwh"
        ].dropna()
        if values.empty:
            raise ValueError("No comparable reported EPC heating in this selection")
        return values.plot.hist(bins=bins, ax=ax)

    def plot_atemp(self, *, bins=20, ax=None):
        values = pd.Series([r.atemp_m2 for r in self], dtype="float64").dropna()
        values = values[values > 0]
        if values.empty:
            raise ValueError("No positive reported Atemp in this selection")
        return values.plot.hist(bins=bins, ax=ax)

    def _archetype(self, supplied):
        if isinstance(supplied, str):
            raise ValueError(
                'archetype="cell" requires a StockCell; otherwise supply a Data Archetype'
            )
        return supplied

    @operation("database.stock.convert", level=10)
    def iter_epsm(self, *, archetype=None, calendar=None, batch_size=100, **options):
        forbidden = {"weather", "policy", "overrides", "preflight", "energyplus"} & options.keys()
        if forbidden:
            raise TypeError(
                "Supply physical Building options here; weather, engine and execution belong to Building.simulate()."
            )
        if calendar is not None and archetype is None:
            raise ValueError("calendar requires an explicitly selected archetype")
        selected = self._archetype(archetype)
        if selected is not None:
            from epsm_data import Archetype

            if not isinstance(selected, Archetype):
                raise TypeError('Supply a Data Archetype, or archetype="cell" on a StockCell')
        from epsm import Building

        records = self.iter_records(batch_size=batch_size)
        try:
            for record in records:
                receipt = deepcopy(record.receipt)
                receipt.building_id = record.building_id
                building = None
                try:
                    building = call_with_context(
                        Building.from_record,
                        record,
                        archetype=selected,
                        calendar=calendar,
                        **options,
                    )
                    readiness = building.readiness()
                    receipt.fields["model_readiness"] = {
                        "model_state": "ready" if readiness.ready else "incomplete",
                        "requirements": list(readiness.missing + readiness.unsupported),
                        "scope": "physical inputs only; engine/weather validation is separate",
                    }
                    receipt.identities["conversion_status"] = (
                        "converted" if readiness.ready else "draft"
                    )
                    if selected is not None:
                        receipt.assumptions.append(
                            "Explicit Data archetype applied; independent source evidence retained."
                        )
                        receipt.identities["archetype_id"] = selected.archetype_id
                    # No execution/preflight has happened, so do not claim simulation readiness.
                    receipt.ready_for_simulation = False
                except (ValueError, TypeError) as error:
                    building = None
                    receipt.errors.append(f"{type(error).__name__}: {error}")
                    receipt.identities["conversion_status"] = "failed"
                yield building, receipt
        finally:
            records.close()

    def to_epsm(self, **options):
        """Materialize conversion outcomes; iter_epsm preserves bounded memory."""
        buildings, receipts = [], []
        for building, receipt in self.iter_epsm(**options):
            if building is not None:
                buildings.append(building)
            receipts.append(receipt)
        return tuple(buildings), tuple(receipts)


class StockCell(Stock):
    """Type/era cohort with optional observed ventilation and declared catalogue state.

    ``generic`` selects source types outside the authored catalogue; their fabric
    assumption borrows the multifamily representative and records that donor.
    """

    def __init__(
        self,
        stock,
        building_type,
        era,
        *,
        ventilation=None,
        climate_zone=None,
        renovation_state=None,
    ):
        self.stock = stock
        self.building_type, self.era = building_type, era
        self.ventilation, self.climate_zone = ventilation, climate_zone
        catalogue = stock.catalogue
        dimensions = catalogue.dimensions
        if building_type not in (*dimensions["categories"], GENERIC):
            raise ValueError(f"Unknown catalogue building type: {building_type}")
        if era not in dimensions["eras"]:
            raise ValueError(f"Unknown catalogue era: {era}")
        if ventilation is not None and ventilation not in VENTILATION:
            raise ValueError(f"Unknown observed ventilation filter: {ventilation}")
        if climate_zone is not None and climate_zone not in dimensions["zones"]:
            raise ValueError(f"Unknown catalogue climate zone: {climate_zone}")
        if era == "SE.07":
            if renovation_state is not None:
                raise ValueError("SE.07 has no renovation states")
        elif renovation_state is None:
            renovation_state = "existing"
        if renovation_state is not None and renovation_state not in dimensions["states"]:
            raise ValueError(f"Unknown catalogue renovation state: {renovation_state}")
        self.renovation_state = renovation_state
        filters = dict(
            stock._filters,
            building_type=building_type,
            era=era,
            ventilation=ventilation,
            climate_zone=climate_zone,
            renovation_state=renovation_state,
        )
        super().__init__(stock.db, _filters=filters)
        self.__dict__["catalogue"] = catalogue

    def __repr__(self):
        from .records import _display_label

        return (
            f"StockCell(type={_display_label(self.building_type, 20)}, "
            f"era={_display_label(self.era, 12)}, "
            f"ventilation={_display_label(self.ventilation, 8)}; lazy)"
        )

    @cached_property
    def archetype(self):
        """Canonical physical definition; selecting it does not apply it to records."""
        records = self.catalogue.archetype_records
        candidates = [
            identifier
            for identifier in self.catalogue.ids(category=self.building_type)
            if records[identifier]["era"] == self.era
        ]
        if len(candidates) != 1:
            raise ValueError(
                "No unique canonical archetype for this cell; supply an explicit Data Archetype"
            )
        return call_with_context(self.catalogue.get, candidates[0], state=self.renovation_state)

    @property
    def archetype_slot(self):
        """Resolve the LoD2 fabric slot; the observed ventilation filter does not select it."""
        from epsm_data import ArchetypeKey

        if self.climate_zone is None:
            raise ValueError("Specify climate_zone for a full archetype slot")
        return self.catalogue.get_slot(
            ArchetypeKey(self.building_type, self.era, self.climate_zone, self.renovation_state)
        )

    def _assignment(self):
        """Declared catalogue assumption for a zone-complete cell; None for a partial cell."""
        if self.climate_zone is None:
            return None
        slot = self.archetype_slot
        catalogue_json = json.dumps(
            self.catalogue.resolved_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return {
            "kind": "assumption",
            "source": "stock_cell",
            "catalogue_dataset": self.catalogue.dataset_id,
            "catalogue_sha256": hashlib.sha256(catalogue_json.encode()).hexdigest(),
            "slot_id": slot.slot_id,
            **slot.key.to_dict(),
            "construction_years": list(self.catalogue.dimensions["eras"][self.era]),
            **({"selection": deepcopy(slot.selection)} if self.building_type == GENERIC else {}),
        }

    @operation("database.stock.read", level=10)
    def iter_records(self, *, batch_size=100):
        """Yield record copies that carry this cell's catalogue assumption when it is complete.

        No source observation or original record is modified; a partial cell (no
        climate zone) does not invent a catalogue selection.
        """
        assignment = self._assignment()
        for record in super().iter_records(batch_size=batch_size):
            if assignment is None:
                yield record
                continue
            assignments = deepcopy(record.archetype_assignments)
            if assignment not in assignments:
                assignments += (deepcopy(assignment),)
            yield replace(record, archetype_assignments=assignments)

    def _archetype(self, supplied):
        if isinstance(supplied, str):
            if supplied != "cell":
                raise ValueError('Use archetype="cell" or supply a Data Archetype')
            return self.archetype
        return supplied

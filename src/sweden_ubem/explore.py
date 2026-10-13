"""Read-only, building-first exploration of a named dataset."""

from __future__ import annotations
import os
from dataclasses import dataclass
import geopandas as gpd
import pandas as pd
from psycopg import sql
from .database import _connect


def _rows(url, query, params=()):
    with _connect(url) as c:
        cur = c.execute(query, params)
        return [dict(zip([f.name for f in cur.description], r)) for r in cur.fetchall()]


def _frame(url, query, params=(), geo=False):
    rows = _rows(url, query, params)
    if geo:
        geometry = gpd.GeoSeries.from_wkb([r.pop("geometry_wkb") for r in rows], crs=3006)
        return gpd.GeoDataFrame(rows, geometry=geometry, crs=3006)
    return pd.DataFrame(rows)


class UBEM:
    def __init__(self, database_url=None, *, dataset="uddevalla"):
        self.database_url = database_url or os.environ.get("UBEM_DATABASE_URL", "")
        if not self.database_url:
            raise ValueError("Set UBEM_DATABASE_URL or supply database_url")
        self.dataset_id = dataset
        if not _rows(
            self.database_url,
            "SELECT dataset_id FROM mart.datasets WHERE dataset_id=%s",
            (dataset,),
        ):
            raise LookupError(f"Dataset {dataset!r} does not exist")

    def epc_results(self):
        """Every parsed EPC subject, including unmatched subjects with no building."""
        return self._view("epc_subject_results")

    def epc_summary(self):
        return self._view("epc_diagnostic_summary")

    def __repr__(self):
        return f"UBEM(dataset={self.dataset_id!r})"

    def _view(self, name, *, building_id=None, geo=False, area_type=None):
        with _connect(self.database_url) as c:
            cols = [
                r[0]
                for r in c.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='mart' AND table_name=%s ORDER BY ordinal_position",
                    (name,),
                )
            ]
        if not cols:
            raise LookupError(name)
        selected = [
            sql.SQL("ST_AsBinary(geom) AS geometry_wkb")
            if x == "geom" and geo
            else sql.Identifier(x)
            for x in cols
        ]
        query = sql.SQL("SELECT {} FROM mart.{} WHERE dataset_id=%s").format(
            sql.SQL(",").join(selected), sql.Identifier(name)
        )
        params = [self.dataset_id]
        if building_id:
            query += sql.SQL(" AND building_id=%s")
            params.append(building_id)
        if area_type is not None:
            query += sql.SQL(" AND area_type=%s")
            params.append(area_type)
        return _frame(self.database_url, query, params, geo=geo)

    def buildings(
        self,
        *,
        purpose=None,
        category=None,
        scope=None,
        include_support=False,
        include_other_statuses=False,
    ):
        clauses = ["f.dataset_id=%s"]
        params = [self.dataset_id]
        if not include_support:
            clauses.append("f.selection_role='primary'")
        if not include_other_statuses:
            clauses.append("f.status_verified AND f.is_existing")
        if purpose or category:
            clauses.append(
                "EXISTS(SELECT 1 FROM mart.building_purposes p WHERE p.dataset_id=f.dataset_id AND p.building_id=f.building_id AND p.category=%s)"
            )
            params.append(purpose or category)
        if scope is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM mart.scope_membership m WHERE m.dataset_id=f.dataset_id AND m.building_id=f.building_id AND m.scope_id=%s AND m.inclusion_state='included')"
            )
            params.append(scope)
        return _frame(
            self.database_url,
            "SELECT f.dataset_id,f.building_id,f.municipality_code,f.selection_role,f.object_status,f.status_verified,f.is_existing,f.house_number,f.construction_year,f.main_building,f.geometry_status,f.linkage_eligible,f.component_count,f.determined_height_component_count,f.entrance_count,f.resolved_address_count,f.property_candidate_count,f.epc_certificate_count,f.rejected_epc_candidate_count,f.purposes,ST_AsBinary(f.geom) AS geometry_wkb FROM mart.building_facts f WHERE "
            + " AND ".join(clauses)
            + " ORDER BY f.building_id",
            params,
            geo=True,
        )

    def building(self, building_id):
        if not _rows(
            self.database_url,
            "SELECT building_id FROM mart.building_facts WHERE dataset_id=%s AND building_id=%s",
            (self.dataset_id, building_id),
        ):
            raise KeyError(building_id)
        return Building(self, str(building_id))

    def pick(self, *, multi_component=False, has_epc=False):
        query = "SELECT building_id FROM mart.building_facts WHERE dataset_id=%s AND selection_role='primary' AND status_verified AND is_existing"
        if multi_component:
            query += " AND component_count>1"
        if has_epc:
            query += " AND epc_certificate_count>0"
        selected = _rows(
            self.database_url, query + " ORDER BY building_id LIMIT 1", (self.dataset_id,)
        )
        if not selected:
            raise LookupError(
                "No building matches; inspect include_other_statuses=True if status mapping is unresolved"
            )
        return self.building(selected[0]["building_id"])

    def sources(self):
        return self._view("source_inventory")

    def quality(self):
        return self._view("quality_findings")

    def scopes(self):
        return self._view("scope_membership")

    def completeness(self):
        return self._view("scope_completeness")

    def areas(self, area_type=None):
        return self._view("context_areas", geo=True, area_type=area_type)

    def boundaries(self):
        return self.areas("municipality")

    def population_grid(self):
        return self.areas("population_grid_1km")

    def map(self, **filters):
        return self.buildings(**filters).plot()

    def context_map(self, indicator_id, *, period=None, dimensions=None):
        values = self._view("building_context")
        values = values.loc[values.indicator_id.eq(indicator_id)] if not values.empty else values
        if period is not None:
            values = values.loc[values.reference_period.eq(str(period))]
        if dimensions is not None:
            values = values.loc[values.dimensions.map(lambda x: x == dimensions)]
        if values.empty:
            raise LookupError("No matching context observations")
        values = values.drop_duplicates("observation_id")
        if values.duplicated(["area_type", "area_code"]).any():
            raise ValueError("Select one period and dimension set before mapping")
        return (
            self.areas()
            .merge(
                values[["area_type", "area_code", "normalized_value"]],
                on=["area_type", "area_code"],
            )
            .plot(column="normalized_value", legend=True)
        )

    def export(self, buildings, *, format="geopackage", output=None):
        from .exports import export_dataset

        return export_dataset(
            self.database_url,
            self.dataset_id,
            [str(x) for x in buildings.building_id],
            format=format,
            output=output,
        )


@dataclass(frozen=True)
class Building:
    db: UBEM
    building_id: str

    def record(self, **kwargs):
        """Fetch a typed evidence record through the read-only retrieval API."""
        from .io import fetch_record
        return fetch_record.from_uuid(self.building_id, database_url=self.db.database_url,
                                      dataset=self.db.dataset_id, **kwargs)

    def to_epsm(self, **kwargs):
        """Optional EPSM integration; source evidence remains unchanged."""
        return self.record().to_epsm(**kwargs)

    def _view(self, name, geo=False):
        return self.db._view(name, building_id=self.building_id, geo=geo)

    def components(self):
        return self._view("building_components", True)

    def facts(self):
        return self._view("building_facts", True)

    def purposes(self):
        return self._view("building_purposes")

    def heights(self):
        return self._view("component_heights")

    def addresses(self):
        return self._view("building_addresses", True)

    def properties(self):
        return self._view("building_properties")

    def epc_candidates(self):
        return self._view("epc_candidates")

    def epc(self):
        return self._view("building_epc")

    def areas(self):
        return self._view("building_areas")

    def context(self):
        return self._view("building_context")

    def context_summary(self):
        return self._view("building_context_summary")

    def completeness(self):
        return self._view("scope_completeness")

    def epc_certificates(self, *, latest=True):
        rows = _frame(
            self.db.database_url,
            "SELECT c.* FROM mart.epc_certificates c WHERE c.dataset_id=%s AND EXISTS(SELECT 1 FROM mart.building_epc r WHERE r.dataset_id=c.dataset_id AND r.certificate_id=c.certificate_id AND r.building_id=%s)",
            (self.db.dataset_id, self.building_id),
        )
        if rows.empty:
            return rows
        if latest:
            dated = rows.loc[rows.date_status.eq("verified") & rows.certificate_date.notna()]
            if not dated.empty:
                # Unknown dates remain visible; ties are not broken by ID.
                rows = rows.loc[
                    rows.certificate_date.eq(dated.certificate_date.max())
                    | rows.certificate_date.isna()
                    | ~rows.date_status.eq("verified")
                ]
        return rows

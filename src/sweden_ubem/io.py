"""Read-only PostGIS retrieval, with bounded selections and explicit ambiguity."""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from shapely import from_wkb, from_wkt
from shapely.geometry import Point, box, shape

from .enums import BuildingType
from .records import BuildingRecord, Receipt, RecordCollection


class RetrievalError(LookupError):
    pass


class RecordNotFoundError(RetrievalError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(f"No matching building in dataset {receipt.dataset_id!r}")


class AmbiguousRecordError(RetrievalError):
    def __init__(self, candidates):
        self.candidates = candidates
        self.receipt = candidates.receipt
        super().__init__("Multiple matching buildings; inspect .candidates or use fetch_records")


class BoundaryUnavailableError(RetrievalError):
    pass


@lru_cache(maxsize=1)
def _municipality_codes_by_name():
    catalogue = json.loads(files("sweden_ubem").joinpath("municipality_names.json").read_text())
    return {name.casefold(): code for code, name in catalogue["municipalities"].items()}


def _number(value, name, *, positive=False):
    if (
        isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
        or positive
        and float(value) == 0
    ):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return float(value)


def _epsg(value):
    text = str(value).upper().removeprefix("EPSG:")
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("Supply an EPSG code, e.g. 3006 or 'EPSG:4326'")
    return int(text)


def _geometry(value):
    if isinstance(value, dict):
        value = shape(value.get("geometry", value))
    elif isinstance(value, str):
        value = from_wkt(value)
    if not hasattr(value, "wkb") or value.is_empty or not value.is_valid or value.has_z:
        raise ValueError("Supply valid, nonempty 2-D Shapely geometry, GeoJSON or WKT")
    if not all(math.isfinite(x) for x in value.bounds):
        raise ValueError("Geometry coordinates must be finite")
    return value


def _programme(field):
    # Only a unique latest dated, single-building certificate may classify a use.
    return f"""EXISTS (SELECT 1 FROM mart.building_epc l
      JOIN mart.epc_certificates c USING(dataset_id,certificate_id)
      JOIN mart.epc_observations o USING(dataset_id,certificate_id)
      WHERE l.dataset_id=f.dataset_id AND l.building_id=f.building_id
      AND c.date_status='verified' AND c.certificate_date IS NOT NULL
      AND c.subject_count=1 AND c.loaded_building_count=1 AND c.matching_status='fully_matched'
      AND o.source_field='{field}' AND jsonb_array_length(o.raw_values)=1
      AND CASE WHEN o.raw_values->>0 ~ '^[0-9]+([.,][0-9]+)?$'
               THEN replace(o.raw_values->>0,',','.')::numeric BETWEEN 50 AND 100 ELSE false END
      AND NOT EXISTS (SELECT 1 FROM mart.building_epc l2
        JOIN mart.epc_certificates c2 USING(dataset_id,certificate_id)
        WHERE l2.dataset_id=l.dataset_id AND l2.building_id=l.building_id
        AND c2.certificate_id<>c.certificate_id AND
        (c2.certificate_date IS NULL OR c2.date_status<>'verified' OR c2.certificate_date>=c.certificate_date)))"""


def _purpose(condition):
    return (
        "EXISTS (SELECT 1 FROM mart.building_purposes p WHERE p.dataset_id=f.dataset_id AND p.building_id=f.building_id AND "
        + condition.replace("%", "%%")
        + ")"
    )


TYPE_SQL = (
    "CASE "
    + " ".join(
        [
            "WHEN " + _purpose("p.category='Komplementbyggnad'") + " THEN 'complementary'",
            "WHEN "
            + _purpose("p.detail ILIKE '%Småhus med flera lägenheter%'")
            + " THEN 'unknown'",
            "WHEN " + _purpose("p.detail ILIKE '%Flerfamiljshus%'") + " THEN 'multi_family'",
            "WHEN " + _purpose("p.detail ILIKE '%Småhus%'") + " THEN 'single_family'",
            "WHEN "
            + _purpose("p.detail ILIKE '%Skola%'")
            + " OR "
            + _programme("EgenAtempSkolor")
            + " THEN 'school'",
            "WHEN "
            + _purpose("p.detail ILIKE '%Kontor%'")
            + " OR ("
            + _purpose("p.category='Verksamhet'")
            + " AND "
            + _programme("EgenAtempKontor")
            + ") THEN 'office'",
            "WHEN " + _purpose("p.category='Industri'") + " THEN 'industrial'",
            "WHEN " + _purpose("p.category NOT IN ('Bostad','Verksamhet')") + " THEN 'other'",
        ]
    )
    + " ELSE 'unknown' END"
)


class RecordFetcher:
    """Configure with .using(), or supply connection/dataset on each request.

    Collection selectors return up to ``limit`` UUID-ordered records. A truncated
    receipt provides ``next_cursor`` for the next call's ``after_uuid`` argument.
    Every request uses a read-only repeatable-read transaction.
    """

    def __init__(self, database_url=None, dataset=None):
        self.database_url = database_url
        self.dataset = dataset

    def using(self, database_url=None, *, dataset=None):
        return type(self)(database_url or self.database_url, dataset or self.dataset)

    def from_uuid(self, building_id, **kwargs):
        kwargs.setdefault("include_support", True)
        kwargs.setdefault("include_other_statuses", True)
        return self._fetch(
            "f.building_id=%s::uuid",
            [str(UUID(str(building_id)))],
            request={"selector": "uuid", "uuid": str(building_id)},
            **kwargs,
        )

    def from_point(self, x, y, *, epsg=3006, **kwargs):
        if not all(math.isfinite(float(v)) for v in (x, y)):
            raise ValueError("Point coordinates must be finite")
        return self._spatial(
            Point(float(x), float(y)), epsg=epsg, predicate="covers_point", **kwargs
        )

    def within_boundary(self, polygon, *, epsg=3006, spatial_match="intersects", **kwargs):
        geom = _geometry(polygon)
        if geom.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Boundary must be Polygon or MultiPolygon")
        return self._spatial(geom, epsg=epsg, predicate=spatial_match, **kwargs)

    def from_footprint(self, footprint, *, epsg=3006, min_overlap_fraction=0.8, **kwargs):
        threshold = _number(min_overlap_fraction, "min_overlap_fraction", positive=True)
        if threshold > 1:
            raise ValueError("Overlap fraction must be <=1")
        geom = _geometry(footprint)
        if geom.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Footprint must be Polygon or MultiPolygon")
        return self._spatial(
            geom, epsg=epsg, predicate="footprint_match", fraction=threshold, **kwargs
        )

    def within_radius(self, geometry=None, *, x=None, y=None, radius_m, epsg=3006, **kwargs):
        if geometry is None:
            if x is None or y is None:
                raise ValueError("Supply geometry or x and y")
            geometry = Point(float(x), float(y))
        return self._spatial(
            _geometry(geometry),
            epsg=epsg,
            predicate="radius",
            radius_m=_number(radius_m, "radius_m", positive=True),
            **kwargs,
        )

    def nearest(self, x, y, *, max_distance_m, epsg=3006, **kwargs):
        """Return distance-ordered candidates; refuse a truncated search region."""
        requested_limit = kwargs.pop("limit", 1000)
        if (
            not isinstance(requested_limit, int)
            or isinstance(requested_limit, bool)
            or not 1 <= requested_limit <= 10000
        ):
            raise ValueError("limit must be an integer between 1 and 10000")
        if kwargs.get("after_uuid"):
            raise ValueError("nearest is distance-ordered; UUID pagination is not applicable")
        result = self.within_radius(
            x=x, y=y, radius_m=max_distance_m, epsg=epsg, limit=10000, **kwargs
        )
        if result.receipt.truncated:
            raise RetrievalError("Nearest search exceeds 10000 candidates; reduce max_distance_m")
        ordered = sorted(result.records, key=lambda r: (r.facts["query_distance_m"], r.building_id))
        result.receipt.request["selector"] = "nearest"
        result.receipt.request["candidate_count"] = len(ordered)
        result.receipt.request["nearest_limit"] = requested_limit
        if len(ordered) > requested_limit:
            result.receipt.quality.append(
                "Nearest result limited by distance; UUID continuation is not applicable"
            )
        return RecordCollection(tuple(ordered[:requested_limit]), result.receipt)

    def from_bbox(self, minx, miny, maxx, maxy, *, epsg=3006, **kwargs):
        if minx >= maxx or miny >= maxy:
            raise ValueError("Bounding box bounds must increase")
        return self._spatial(
            box(minx, miny, maxx, maxy), epsg=epsg, predicate="intersects", **kwargs
        )

    def from_address(
        self, address=None, *, street=None, number=None, locality=None, municipality=None, **kwargs
    ):
        if address is not None:
            match = re.fullmatch(r"\s*(.+?)\s+(\d+\s*[A-Za-z]?)\s*", address)
            if not match:
                raise ValueError(
                    "Supply street and number separately, or an address ending in its number"
                )
            street, number = match.groups()
        if not street or number is None:
            raise ValueError("Supply street and number")
        parts = [
            "a.dataset_id=f.dataset_id",
            "a.building_id=f.building_id",
            "a.resolved",
            "lower(btrim(a.street_name))=lower(btrim(%s))",
            "lower(replace(a.address_number,' ',''))=lower(replace(%s,' ',''))",
        ]
        params = [str(street), str(number)]
        if locality:
            parts.append("lower(btrim(a.locality))=lower(btrim(%s))")
            params.append(locality)
        condition = (
            "EXISTS (SELECT 1 FROM mart.building_addresses a WHERE " + " AND ".join(parts) + ")"
        )
        if municipality is not None:
            extra, values = self._municipality(municipality)
            condition += " AND " + extra
            params += values
        return self._fetch(
            condition,
            params,
            request={
                "selector": "address",
                "street": street,
                "number": number,
                "locality": locality,
                "municipality": municipality,
            },
            **kwargs,
        )

    def _municipality(self, value):
        text = str(value).strip()
        if text.isdigit():
            return "f.municipality_code=%s", [text.zfill(4)]
        name = re.sub(r"\s+kommun$", "", text, flags=re.IGNORECASE).strip().casefold()
        if code := _municipality_codes_by_name().get(name):
            return "f.municipality_code=%s", [code]
        return (
            """f.municipality_code IN (SELECT a.area_code FROM mart.context_areas a
          WHERE a.dataset_id=f.dataset_id AND a.area_type='municipality' AND lower(a.name)=lower(%s))""",
            [text],
        )

    def from_municipality(self, municipality, **kwargs):
        condition, params = self._municipality(municipality)
        return self._fetch(
            condition, params, request={"selector": "municipality", "value": municipality}, **kwargs
        )

    def from_regso(self, code, *, reference_period=None, **kwargs):
        return self._area("regso", code, reference_period, **kwargs)

    def from_deso(self, code, *, reference_period=None, **kwargs):
        return self._area("deso", code, reference_period, **kwargs)

    def from_property(self, designation=None, *, property_uuid=None, **kwargs):
        if (designation is None) == (property_uuid is None):
            raise ValueError("Supply designation or property_uuid")
        if property_uuid:
            condition = "p.property_unit_id=%s"
            params = [str(property_uuid)]
        else:
            from .linkage import normalize_designation

            condition = "p.property_unit_id IN (SELECT d.property_unit_id FROM mart.property_designations d WHERE d.dataset_id=f.dataset_id AND d.normalized_label=%s)"
            params = [normalize_designation(designation)]
        return self._fetch(
            "EXISTS (SELECT 1 FROM mart.building_properties p WHERE p.dataset_id=f.dataset_id AND p.building_id=f.building_id AND "
            + condition
            + ")",
            params,
            request={
                "selector": "property",
                "designation": designation,
                "property_uuid": property_uuid,
            },
            **kwargs,
        )

    def _area(self, area_type, code, period, **kwargs):
        return self._fetch(
            "true",
            [],
            request={"selector": area_type, "code": str(code), "reference_period": period},
            area=(area_type, str(code), period),
            **kwargs,
        )

    def _spatial(self, geometry, *, epsg, predicate, fraction=None, radius_m=None, **kwargs):
        expressions = {
            "intersects": "ST_Intersects(f.geom,q.geom)",
            "covered_by": "ST_CoveredBy(f.geom,q.geom)",
            "centroid_within": "ST_Covers(q.geom,ST_Centroid(f.geom))",
            "covers_point": "ST_Covers(f.geom,q.geom)",
            "radius": "ST_DWithin(f.geom,q.geom,%s)",
            "footprint_match": "ST_Intersects(f.geom,q.geom) AND ST_Area(ST_Intersection(f.geom,q.geom))/NULLIF(ST_Area(ST_Union(f.geom,q.geom)),0)>=%s",
        }
        if predicate not in expressions:
            raise ValueError(f"Unsupported spatial_match: {predicate}")
        extra = (
            [radius_m]
            if predicate == "radius"
            else [fraction]
            if predicate == "footprint_match"
            else []
        )
        return self._fetch(
            expressions[predicate],
            extra,
            request={
                "selector": predicate,
                "epsg": _epsg(epsg),
                "geometry_wkb": geometry.wkb_hex,
                "radius_m": radius_m,
                "min_overlap_fraction": fraction,
            },
            spatial=(geometry.wkb, _epsg(epsg)),
            **kwargs,
        )

    def _fetch(
        self,
        condition,
        params,
        *,
        request,
        database_url=None,
        dataset=None,
        min_footprint_area_m2=0,
        max_footprint_area_m2=None,
        include_type=None,
        has_epc=None,
        construction_year=None,
        include_support=False,
        include_other_statuses=False,
        exclude_uuid=None,
        limit=1000,
        after_uuid=None,
        spatial=None,
        area=None,
    ):
        url = database_url or self.database_url or os.environ.get("UBEM_DATABASE_URL")
        dataset = dataset or self.dataset or os.environ.get("UBEM_DATASET", "uddevalla")
        if not url:
            raise ValueError("Set UBEM_DATABASE_URL or supply database_url")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError("limit must be an integer between 1 and 10000")
        minimum = _number(min_footprint_area_m2, "min_footprint_area_m2")
        receipt = Receipt(dataset, request=deepcopy(request))
        with psycopg.connect(
            url,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on -c statement_timeout=60000",
        ) as c:
            c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            metadata = c.execute(
                "SELECT * FROM catalog.dataset WHERE dataset_id=%s", (dataset,)
            ).fetchone()
            if metadata is None:
                raise RetrievalError(
                    f"Dataset {dataset!r} is unavailable; select a processed dataset"
                )
            if metadata.get("dataset_kind") == "national_source":
                raise RetrievalError(
                    "Backing source datasets are not published building selections; select a processed dataset"
                )
            receipt.snapshot = dict(metadata)
            receipt.identities["transaction_read_only"] = c.execute(
                "SELECT current_setting('transaction_read_only') AS value"
            ).fetchone()["value"]
            columns = {
                r["column_name"]
                for r in c.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='mart' AND table_name='building_facts'"
                )
            }
            clauses = ["f.dataset_id=%s", condition]
            values = [dataset, *params]
            filters = {
                "min_footprint_area_m2": minimum,
                "include_support": include_support,
                "include_other_statuses": include_other_statuses,
                "limit": limit,
                "after_uuid": after_uuid,
            }
            if not include_support and "selection_role" in columns:
                clauses.append("f.selection_role='primary'")
            if not include_other_statuses:
                clauses.append("f.status_verified AND f.is_existing")
            if minimum:
                clauses.append("ST_Area(f.geom)>=%s")
                values.append(minimum)
            if max_footprint_area_m2 is not None:
                maximum = _number(max_footprint_area_m2, "max_footprint_area_m2")
                if maximum < minimum:
                    raise ValueError("Maximum footprint area is below minimum")
                clauses.append("ST_Area(f.geom)<=%s")
                values.append(maximum)
                filters["max_footprint_area_m2"] = maximum
            if include_type is not None:
                kinds = [BuildingType(x).value for x in include_type]
                if not kinds:
                    raise ValueError("include_type must not be empty")
                clauses.append(f"({TYPE_SQL})=ANY(%s)")
                values.append(kinds)
                filters["include_type"] = kinds
            if has_epc is not None:
                if not isinstance(has_epc, bool):
                    raise ValueError("has_epc must be bool")
                clauses.append(
                    "f.epc_certificate_count>0" if has_epc else "f.epc_certificate_count=0"
                )
                filters["has_epc"] = has_epc
            if construction_year is not None:
                lo, hi = construction_year
                if int(lo) > int(hi):
                    raise ValueError("Construction year bounds must increase")
                clauses.append("f.construction_year BETWEEN %s AND %s")
                values.extend([int(lo), int(hi)])
                filters["construction_year"] = [lo, hi]
            for name, value, op in [
                ("exclude_uuid", exclude_uuid, "<>"),
                ("after_uuid", after_uuid, ">"),
            ]:
                if value:
                    clauses.append(f"f.building_id{op}%s::uuid")
                    values.append(str(UUID(str(value))))
            prefix = ""
            join = ""
            select_distance = ""
            if area:
                kind, code, period = area
                boundary_sql = "SELECT area_type,area_code,name,reference_period,ST_AsBinary(geom) AS geometry_wkb FROM mart.context_areas WHERE dataset_id=%s AND area_type=%s AND area_code=%s"
                boundary_values = [dataset, kind, code]
                if period is not None:
                    boundary_sql += " AND reference_period=%s"
                    boundary_values.append(str(period))
                boundaries = c.execute(boundary_sql, boundary_values).fetchall()
                if not boundaries:
                    raise BoundaryUnavailableError(
                        f"{kind} boundary {code!r} unavailable in {dataset!r}; this is not a zero-building result"
                    )
                if len(boundaries) != 1:
                    raise BoundaryUnavailableError(
                        "Boundary is ambiguous; supply its reference_period"
                    )
                boundary = boundaries[0]
                receipt.request["boundary"] = {
                    k: v for k, v in boundary.items() if k != "geometry_wkb"
                }
                spatial = (bytes(boundary["geometry_wkb"]), 3006)
                clauses.append("ST_Intersects(f.geom,q.geom)")
            if spatial:
                prefix = "WITH q AS (SELECT ST_Transform(ST_GeomFromWKB(%s,%s),3006) AS geom) "
                join = " CROSS JOIN q"
                select_distance = ",ST_Distance(f.geom,q.geom) AS query_distance_m"
                values = [*spatial, *values]
            receipt.request["filters"] = filters
            query = (
                prefix
                + f"SELECT f.*,ST_AsBinary(f.geom) AS geometry_wkb,({TYPE_SQL}) AS building_type{select_distance} FROM mart.building_facts f{join} WHERE "
                + " AND ".join(clauses)
                + " ORDER BY f.building_id LIMIT %s"
            )
            facts = c.execute(query, [*values, limit + 1]).fetchall()
            receipt.truncated = len(facts) > limit
            facts = facts[:limit]
            if receipt.truncated:
                receipt.next_cursor = str(facts[-1]["building_id"])
            layers = c.execute(
                "SELECT * FROM catalog.dataset_layer WHERE dataset_id=%s", (dataset,)
            ).fetchall()
            incomplete = [x["layer"] for x in layers if x["loading_status"] != "loaded"]
            if incomplete:
                receipt.quality.append("Incomplete dataset layers: " + ", ".join(incomplete))
            records = self._hydrate(c, facts, receipt, url)
        return RecordCollection(tuple(records), receipt)

    def _hydrate(self, c, facts, selection_receipt, url):
        ids = [str(f["building_id"]) for f in facts]
        if not ids:
            return []
        dataset = selection_receipt.dataset_id
        grouped = {}
        for view in [
            "building_components",
            "component_heights",
            "building_purposes",
            "building_addresses",
            "building_properties",
            "quality_findings",
        ]:
            geometry = (
                ",ST_AsBinary(geom) AS geometry_wkb"
                if view in {"building_components", "building_addresses"}
                else ""
            )
            rows = c.execute(
                f"SELECT v.*{geometry} FROM mart.{view} v WHERE dataset_id=%s AND building_id=ANY(%s::uuid[])",
                (dataset, ids),
            ).fetchall()
            by_id = defaultdict(list)
            for row in rows:
                row.pop("geom", None)
                if "geometry_wkb" in row and row["geometry_wkb"] is not None:
                    row["geometry"] = from_wkb(bytes(row.pop("geometry_wkb")))
                by_id[str(row["building_id"])].append(row)
            grouped[view] = by_id
        links = c.execute(
            "SELECT DISTINCT building_id,certificate_id FROM mart.building_epc WHERE dataset_id=%s AND building_id=ANY(%s::uuid[])",
            (dataset, ids),
        ).fetchall()
        cert_ids = sorted({l["certificate_id"] for l in links})
        cert_tables = {}
        for view in ["epc_certificates", "epc_observations", "epc_subjects", "epc_subject_results"]:
            by_cert = defaultdict(list)
            if cert_ids:
                rows = c.execute(
                    f"SELECT * FROM mart.{view} WHERE dataset_id=%s AND certificate_id=ANY(%s)",
                    (dataset, cert_ids),
                ).fetchall()
                for row in rows:
                    by_cert[row["certificate_id"]].append(row)
            cert_tables[view] = by_cert
        building_certs = defaultdict(set)
        for link in links:
            building_certs[str(link["building_id"])].add(link["certificate_id"])
        result = []
        for f in facts:
            bid = str(f["building_id"])
            geom = (
                from_wkb(bytes(f.pop("geometry_wkb")))
                if f.get("geometry_wkb") is not None
                else None
            )
            f.pop("geom", None)
            kind = BuildingType(f.pop("building_type"))
            receipt = deepcopy(selection_receipt)
            receipt.building_id = bid
            receipt.truncated = False
            receipt.next_cursor = None
            certificates = sorted(building_certs[bid])
            tables = {
                k: tuple(row for cid in certificates for row in v[cid])
                for k, v in cert_tables.items()
            }
            record = BuildingRecord(
                dataset,
                bid,
                kind,
                geom,
                dict(f),
                components=tuple(grouped["building_components"][bid]),
                heights=tuple(grouped["component_heights"][bid]),
                purposes=tuple(grouped["building_purposes"][bid]),
                addresses=tuple(grouped["building_addresses"][bid]),
                properties=tuple(grouped["building_properties"][bid]),
                certificates=tables["epc_certificates"],
                observations=tables["epc_observations"],
                subjects=tables["epc_subjects"],
                epc_results=tables["epc_subject_results"],
                quality_findings=tuple(grouped["quality_findings"][bid]),
                receipt=receipt,
                _database_url=url,
            )
            receipt.fields["footprint"] = {
                "source_state": "available"
                if geom is not None and geom.is_valid
                else "missing_or_invalid",
                "origin": "derived",
                "value": geom.area if geom is not None else None,
                "unit": "m2",
            }
            receipt.fields["building_type"] = {
                "source_state": "inferred" if kind != BuildingType.UNKNOWN else "unknown",
                "origin": "LM purpose / scope-qualified EPC use",
                "value": kind.value,
            }
            receipt.fields["construction_year"] = {
                "source_state": "available" if f.get("construction_year") else "missing",
                "origin": "reported",
                "value": f.get("construction_year"),
            }
            for name, source, integer in [
                ("atemp_m2", "EgenAtemp", False),
                ("storeys", "EgenAntalPlan", True),
                ("basement_storeys", "EgenAntalKallarplan", True),
            ]:
                value, status = record.observation(source, integer=integer)
                if not record.single_building_epc:
                    status = "unresolved_certificate_scope"
                receipt.fields[name] = {
                    "source_state": status,
                    "origin": "reported",
                    "value": value,
                    "usable_for_building": record.single_building_epc,
                }
            receipt.fields["height_components"] = {
                "source_state": "available" if record.heights else "missing",
                "origin": "derived",
                "value": len(record.heights),
            }
            determined = sum(h.get("height_status") == "determined" for h in record.heights)
            receipt.fields["height_components"].update(
                source_state="available"
                if determined == len(record.components) and determined
                else "partial"
                if determined
                else "missing",
                determined_components=determined,
                expected_components=len(record.components),
            )
            receipt.quality.extend(str(flag) for flag in f.get("quality_flags", []))
            receipt.quality.extend(f"{q['layer']}: {q['code']}" for q in record.quality_findings)
            if record.certificates and not record.single_building_epc:
                receipt.quality.append(
                    "No unique latest single-building EPC; certificate quantities are not allocated"
                )
            if f.get("geometry_status") != "resolved":
                receipt.quality.append("Building footprint resolution is not resolved")
            if record.temporal_warnings:
                receipt.quality.append("EPC/building temporal conflict")
            for name, value in [
                ("atemp_m2", record.atemp_m2),
                ("basement_storeys", record.basement_storeys),
            ]:
                if value is not None and (value < 0 or name == "atemp_m2" and value == 0):
                    receipt.fields[name]["source_state"] = "suspect"
                    receipt.quality.append(f"Invalid reported {name}: {value}")
            if record.storeys is not None and (
                record.storeys <= 0
                or any(
                    h.get("eave_height_m") and not 2 <= h["eave_height_m"] / record.storeys <= 5
                    for h in record.heights
                )
            ):
                receipt.fields["storeys"]["source_state"] = "suspect"
                receipt.quality.append(
                    "Reported storeys fail provisional 2–5 m eave-height screening"
                )
            result.append(record)
        return result


class SingleRecordFetcher(RecordFetcher):
    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if name in {"from_uuid", "from_point", "from_address", "from_footprint", "from_property"}:
            return lambda *args, **kwargs: value(*args, **kwargs).one()
        return value


fetch_records = RecordFetcher()
fetch_record = SingleRecordFetcher()

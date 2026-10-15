"""Database-only processing and publication over shared national evidence."""

import json
from collections import defaultdict
from datetime import date
from uuid import UUID

import shapely
from psycopg.types.json import Jsonb

from sweden_ubem._logging import operation as logged_operation

from .components import GEOMETRY_POLICY_VERSION, assemble_building_snapshot, relation_candidates
from .context import CONTEXT_POLICY_VERSION, derive_area_relations
from .database import LAYERS, _connect, application_digest
from .epc_assessment import POLICY_VERSION, address_key, assess_epc, subject_addresses
from .hashing import content_hash
from .jobs import bulk_insert, rows, serial_worker, work
from .linkage import POLICY_VERSION as PROPERTY_POLICY
from .linkage import property_match_key


def release(url, source, assessment_date=None, event_sources=None):
    with _connect(url) as c:
        metadata = c.execute(
            "SELECT metadata FROM catalog.dataset WHERE dataset_id=%s AND dataset_kind='national_source'",
            (source,),
        ).fetchone()
        if not metadata:
            raise ValueError("Initialize national evidence before processing")
        inputs = c.execute(
            "SELECT layer,loading_status,source_version_id,input_signature FROM catalog.dataset_layer WHERE dataset_id=%s ORDER BY layer",
            (source,),
        ).fetchall()
        mappings = c.execute(
            "SELECT regime,source_field,owner,value_type,source_unit,normalized_unit,multiplier,review_status,authority FROM catalog.field_mapping ORDER BY regime,source_field"
        ).fetchall()
        available = [
            r[0]
            for r in c.execute(
                "SELECT DISTINCT source_version_id FROM evidence.epc_site_event WHERE dataset_id=%s ORDER BY source_version_id",
                (source,),
            )
        ]
        events = available if event_sources is None else sorted(event_sources)
        if set(events) - set(available):
            raise ValueError("Site-event sources must be imported before processing")
        policies = {
            "geometry": GEOMETRY_POLICY_VERSION,
            "property": PROPERTY_POLICY,
            "epc": POLICY_VERSION,
            "context": CONTEXT_POLICY_VERSION,
            "quality": "source-completeness-v1",
        }
        assessment = str(assessment_date or date.today())
        code = application_digest()
        mapping = content_hash(mappings)
        if metadata[0].get("field_mapping_digest") != mapping:
            raise ValueError("Source field mappings changed; initialize a fresh backing database")
        environment = {
            "postgres": c.execute("SHOW server_version").fetchone()[0],
            "postgis": c.execute("SELECT postgis_full_version()").fetchone()[0],
            "shapely": shapely.__version__,
            "geos": shapely.geos_version_string,
        }
        signature = content_hash(
            [source, metadata[0], inputs, mapping, policies, assessment, events, code, environment]
        )
        identity = UUID(signature[:32])
        c.execute(
            "INSERT INTO catalog.processing_release(processing_release_id,source_dataset_id,input_signature,policy_versions,code_digest,environment,field_mapping_digest,assessment_date,event_source_versions) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(input_signature) DO NOTHING",
            (
                identity,
                source,
                signature,
                Jsonb(policies),
                code,
                Jsonb(environment),
                mapping,
                assessment,
                events,
            ),
        )
        return identity


@logged_operation("database.partition.prepare")
def prepare_partition(url, source, processing, partition):
    signature = content_hash([source, processing, partition])
    with work(url, source, "geometry", partition, signature, processing=processing) as state:
        if state is not None:
            c = state["connection"]
            run = state["run_id"]
            grouped = defaultdict(list)
            for r in rows(
                c,
                "SELECT g.component_id,g.building_id::text,g.geometry_hash,ST_AsBinary(g.geom) geometry,g.representation_status FROM stock.geometry_component g JOIN stock.building b USING(dataset_id,building_id) WHERE b.dataset_id=%s AND b.processing_partition=%s ORDER BY g.building_id,g.component_id",
                (source, partition),
            ):
                r["geometry"] = shapely.from_wkb(bytes(r["geometry"]))
                grouped[r["building_id"]].append(r)
            codes = {
                str(b): v
                for b, v in c.execute(
                    "SELECT building_id,municipality_values FROM stock.building WHERE dataset_id=%s AND processing_partition=%s",
                    (source, partition),
                )
            }
            footprints = []
            relations = []
            findings = []
            for bid, components in grouped.items():
                snap = assemble_building_snapshot(bid, components)
                if len(codes[bid]) != 1:
                    snap.update(
                        linkage_geometry=None,
                        resolution_status="ambiguous",
                        quality_flags=sorted(
                            set(snap["quality_flags"]) | {"lm_municipality_conflict"}
                        ),
                    )
                    findings.append(
                        dict(
                            dataset_id=source,
                            processing_release_id=processing,
                            building_id=bid,
                            layer="geometry",
                            code="lm_municipality_conflict",
                            details={"source_municipality_values": codes[bid]},
                            run_id=run,
                        )
                    )
                if snap["linkage_geometry"] is None:
                    findings.append(
                        dict(
                            dataset_id=source,
                            processing_release_id=processing,
                            building_id=bid,
                            layer="epc_linkage",
                            code="geometry_unresolved",
                            details={"automatic_links_withheld": True},
                            run_id=run,
                        )
                    )
                footprints.append(
                    dict(
                        dataset_id=source,
                        processing_release_id=processing,
                        building_id=bid,
                        display_geom=snap["display_geometry"],
                        linkage_geom=snap["linkage_geometry"],
                        resolution_status=snap["resolution_status"],
                        quality_flags=snap["quality_flags"],
                        run_id=run,
                    )
                )
                if any(not r["geometry"].is_valid for r in components):
                    findings.append(
                        dict(
                            dataset_id=source,
                            processing_release_id=processing,
                            building_id=bid,
                            layer="geometry",
                            code="invalid_component_geometry",
                            details={"automatic_links_withheld": True},
                            run_id=run,
                        )
                    )
                else:
                    for r in relation_candidates(components):
                        relations.append(
                            dict(
                                dataset_id=source,
                                processing_release_id=processing,
                                left_component_id=r["component_a"],
                                right_component_id=r["component_b"],
                                relation_kind=r["relation_type"],
                                decision=r["decision_status"],
                                details=r,
                                run_id=run,
                            )
                        )
            for table, data in [
                ("stock.building_footprint", footprints),
                ("stock.component_relation", relations),
                ("quality.finding", findings),
            ]:
                state["rows"] += bulk_insert(c, table, data)
    with work(
        url, source, "property_linkage", partition, signature, processing=processing
    ) as state:
        if state is not None:
            c = state["connection"]
            result = c.execute(
                """INSERT INTO evidence.building_property(dataset_id,processing_release_id,building_id,property_unit_id,intersection_area_m2,overlap_fraction,rank,is_primary,run_id)
 WITH hits AS (SELECT f.dataset_id,f.building_id,p.property_unit_id,ST_Area(ST_Union(ST_Intersection(f.linkage_geom,p.geom))) area,ST_Area(f.linkage_geom) total,bool_or(ST_Covers(p.geom,ST_PointOnSurface(f.linkage_geom))) contains_point
 FROM stock.building_footprint f JOIN stock.building b USING(dataset_id,building_id) JOIN stock.property_area_part p ON p.dataset_id=f.dataset_id AND ST_IsValid(p.geom) AND ST_Intersects(f.linkage_geom,p.geom)
 WHERE f.dataset_id=%s AND f.processing_release_id=%s AND b.processing_partition=%s AND f.linkage_geom IS NOT NULL GROUP BY f.dataset_id,f.building_id,p.property_unit_id,f.linkage_geom),
 ranked AS (SELECT *,row_number() OVER(PARTITION BY building_id ORDER BY contains_point DESC,area DESC,property_unit_id) rank FROM hits WHERE area>0)
 SELECT dataset_id,%s,building_id,property_unit_id,area,area/total,rank,rank=1,%s FROM ranked ON CONFLICT DO NOTHING""",
                (source, processing, partition, processing, state["run_id"]),
            )
            state["rows"] = result.rowcount


def prepare_buildings(url, source, processing, ids):
    if not ids:
        return
    with _connect(url) as c:
        partitions = [
            r[0]
            for r in c.execute(
                "SELECT DISTINCT processing_partition FROM stock.building WHERE dataset_id=%s AND building_id=ANY(%s::uuid[]) ORDER BY processing_partition",
                (source, list(ids)),
            )
        ]
    for partition in partitions:
        prepare_partition(url, source, processing, partition)


def prepare_source_quality(url, source, processing, states):
    """Compute source diagnostics once per release; selections project local rows."""
    statements = {
        "register": """INSERT INTO quality.finding(dataset_id,processing_release_id,building_id,layer,code,details,run_id)
          SELECT %s,%s,b.building_id,'register','record_missing','{}',%s FROM stock.building b
          WHERE b.dataset_id=%s AND b.register_run_id IS NULL""",
        "addresses": """INSERT INTO quality.finding(dataset_id,processing_release_id,layer,code,details,run_id)
          SELECT %s,%s,'addresses','unresolved_address',jsonb_build_object('address_place_id',e.address_place_id),%s
          FROM stock.entrance e WHERE e.dataset_id=%s AND e.address_place_id IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM stock.address_place a WHERE (a.dataset_id,a.address_place_id)=(e.dataset_id,e.address_place_id))
          GROUP BY e.address_place_id""",
        "properties": """INSERT INTO quality.finding(dataset_id,processing_release_id,source_record_id,layer,code,details,run_id)
          SELECT %s,%s,r.record_id,'properties','invalid_property_record',jsonb_build_object('fid',r.source_record::bigint),%s
          FROM raw_lm.record r WHERE r.dataset_id=%s AND r.kind='property' AND (
            COALESCE(r.original_values->>'registerenhetsreferens','')='' OR COALESCE(r.original_values->>'objektidentitet','')=''
            OR r.original_geometry IS NULL OR ST_IsEmpty(ST_GeomFromWKB(r.original_geometry,3006)))""",
    }
    for layer, statement in statements.items():
        if states.get(layer) != "loaded":
            continue
        signature = content_hash([source, processing, layer, "source-completeness-v1"])
        with work(
            url, source, "quality:" + layer, "all", signature, processing=processing
        ) as state:
            if state is not None:
                state["rows"] = (
                    state["connection"]
                    .execute(statement, (source, processing, state["run_id"], source))
                    .rowcount
                )


def assess_certificate(url, source, processing, certificate):
    signature = content_hash([source, processing, certificate])
    with work(url, source, "epc_linkage", certificate, signature, processing=processing) as state:
        if state is None:
            return
        c = state["connection"]
        run = state["run_id"]
        subjects = rows(
            c,
            "SELECT s.subject_id epc_subject_id,s.certificate_id,e.formular_id,s.municipality_code,s.designation_normalized,s.house_number,s.original_values,e.certificate_date FROM evidence.epc_subject s JOIN evidence.epc_certificate e USING(dataset_id,certificate_id) WHERE s.dataset_id=%s AND s.certificate_id=%s",
            (source, certificate),
        )
        by_subject = {r["epc_subject_id"]: r for r in subjects}
        for r in subjects:
            r.update(
                property_match_key=property_match_key(
                    r["municipality_code"], r["designation_normalized"]
                ),
                source_rows=[],
                source_locators=[],
            )
        table = (
            "raw_epc.before_v260_row"
            if certificate.startswith("fore_v260:")
            else "raw_epc.after_v260_row"
        )
        with c.cursor(name="certificate_raw") as raw:
            raw.execute(
                f"SELECT record_id,source_file,source_record,source_sha256,original_values,subject_id FROM {table} WHERE dataset_id=%s AND certificate_id=%s ORDER BY source_file,source_record::bigint",
                (source, certificate),
            )
            for record, file, ordinal, digest, values, sid in raw:
                if sid not in by_subject:
                    continue
                by_subject[sid]["source_rows"].append(values)
                by_subject[sid]["source_locators"].append(
                    dict(table=table, record_id=record, file=file, record=ordinal, sha256=digest)
                )
        c.execute("CREATE TEMP TABLE policy_buildings(building_id uuid PRIMARY KEY) ON COMMIT DROP")
        c.execute(
            "INSERT INTO policy_buildings SELECT DISTINCT g.building_id FROM evidence.epc_subject s JOIN stock.property_designation d ON (d.dataset_id,d.municipality_code,d.normalized_label)=(s.dataset_id,s.municipality_code,s.designation_normalized) JOIN stock.property_area_part p ON (p.dataset_id,p.property_unit_id)=(d.dataset_id,d.property_unit_id) JOIN stock.geometry_component g ON g.dataset_id=p.dataset_id AND ST_IsValid(g.geom) AND ST_IsValid(p.geom) AND ST_Intersects(g.geom,p.geom) WHERE s.dataset_id=%s AND s.certificate_id=%s ON CONFLICT DO NOTHING",
            (source, certificate),
        )
        address_counts = {}
        exact_keys = set()
        for subject in subjects:
            for key in subject_addresses(subject):
                match = json.dumps(key[1:], ensure_ascii=False)
                base = json.dumps(key[1:3] + key[4:], ensure_ascii=False)
                base_query = "FROM stock.address_place a JOIN stock.entrance e USING(dataset_id,address_place_id) JOIN stock.building b USING(dataset_id,building_id) WHERE a.dataset_id=%s AND b.municipality_code=%s"
                exact = c.execute(
                    "SELECT count(DISTINCT e.building_id) " + base_query + " AND a.match_key=%s",
                    (source, subject["municipality_code"], match),
                ).fetchone()[0]
                if exact:
                    exact_keys.add(key)
                count = exact
                if not exact and not key[3]:
                    count = c.execute(
                        "SELECT count(DISTINCT e.building_id) "
                        + base_query
                        + " AND a.base_match_key=%s",
                        (source, subject["municipality_code"], base),
                    ).fetchone()[0]
                address_counts[key] = count
                c.execute(
                    "INSERT INTO policy_buildings SELECT DISTINCT e.building_id "
                    + base_query
                    + " AND (a.match_key=%s OR (%s AND a.base_match_key=%s)) ON CONFLICT DO NOTHING",
                    (source, subject["municipality_code"], match, not key[3], base),
                )
        for (partition,) in c.execute(
            "SELECT DISTINCT b.processing_partition FROM stock.building b JOIN policy_buildings p USING(building_id) WHERE b.dataset_id=%s ORDER BY b.processing_partition",
            (source,),
        ).fetchall():
            prepare_partition(url, source, processing, partition)
        shared = defaultdict(set)
        for other_table in ("raw_epc.before_v260_row", "raw_epc.after_v260_row"):
            with c.cursor(name="ambiguity_" + content_hash([certificate, other_table])[:12]) as cur:
                cur.execute(
                    f"SELECT s.municipality_code,s.designation_normalized,s.house_number,r.original_values FROM {other_table} r JOIN evidence.epc_subject s ON (s.dataset_id,s.subject_id)=(r.dataset_id,r.subject_id) WHERE s.dataset_id=%s AND (s.municipality_code,s.designation_normalized) IN (SELECT municipality_code,designation_normalized FROM evidence.epc_subject WHERE dataset_id=%s AND certificate_id=%s)",
                    (source, source, certificate),
                )
                for municipality, designation, house, values in cur:
                    key = address_key(municipality, values.get("IdAdr"), values.get("IdPostnr"))
                    if key:
                        shared[(property_match_key(municipality, designation), key)].add(house)
        assessment, event_versions = c.execute(
            "SELECT assessment_date,event_source_versions FROM catalog.processing_release WHERE processing_release_id=%s",
            (processing,),
        ).fetchone()
        events = rows(
            c,
            "SELECT event_id,municipality_code,property_match_key,address,postcode,event_kind,reported_date::text,date_basis,description,source_url,source_document_sha256,evidence_scope FROM evidence.epc_site_event WHERE dataset_id=%s AND source_version_id=ANY(%s)",
            (source, event_versions),
        )
        strict = {
            r[0]
            for r in c.execute(
                "SELECT DISTINCT s.subject_id FROM evidence.epc_subject s JOIN evidence.epc_certificate e USING(dataset_id,certificate_id) JOIN stock.property_designation d ON d.dataset_id=s.dataset_id AND d.municipality_code=s.municipality_code AND d.normalized_label=s.designation_normalized JOIN evidence.building_property p ON p.dataset_id=d.dataset_id AND p.processing_release_id=%s AND p.property_unit_id=d.property_unit_id JOIN stock.building b ON (b.dataset_id,b.building_id)=(p.dataset_id,p.building_id) WHERE s.dataset_id=%s AND s.certificate_id=%s AND b.house_number=s.house_number AND p.overlap_fraction>=0.5 AND (e.certificate_date IS NULL OR b.construction_year IS NULL OR b.construction_year<=extract(year from e.certificate_date)) AND (SELECT count(*) FROM evidence.building_property q WHERE (q.dataset_id,q.processing_release_id,q.building_id)=(p.dataset_id,p.processing_release_id,p.building_id) AND q.overlap_fraction>=0.5)=1",
                (processing, source, certificate),
            )
        }
        # Preserve complete graph checks while streaming arbitrarily large building
        # neighborhoods through bounded policy batches and database reductions.
        with c.cursor(name="policy_ids") as cur:
            cur.execute("SELECT building_id::text FROM policy_buildings ORDER BY building_id")
            from .jobs import batches

            for ids in batches((r[0] for r in cur), 2000):
                buildings = rows(
                    c,
                    "SELECT b.building_id::text,b.house_number,f.linkage_geom IS NOT NULL linkage_eligible,b.municipality_code,b.construction_year,b.is_existing,b.register_run_id::text,ST_Area(f.display_geom) footprint_area_m2,(SELECT count(*) FROM stock.geometry_component g WHERE (g.dataset_id,g.building_id)=(b.dataset_id,b.building_id)) component_count,COALESCE((SELECT jsonb_agg(jsonb_build_object('category',p.category,'detail',p.detail)) FROM stock.building_purpose p WHERE (p.dataset_id,p.building_id)=(b.dataset_id,b.building_id)),'[]'::jsonb) purposes FROM stock.building b JOIN stock.building_footprint f USING(dataset_id,building_id) WHERE b.dataset_id=%s AND f.processing_release_id=%s AND b.building_id=ANY(%s::uuid[])",
                    (source, processing, ids),
                )
                props = rows(
                    c,
                    "SELECT building_id::text,property_unit_id,overlap_fraction,is_primary is_primary_property FROM evidence.building_property WHERE dataset_id=%s AND processing_release_id=%s AND building_id=ANY(%s::uuid[])",
                    (source, processing, ids),
                )
                units = sorted({r["property_unit_id"] for r in props})
                designations = rows(
                    c,
                    "SELECT property_unit_id,municipality_code,normalized_label designation_normalized FROM stock.property_designation WHERE dataset_id=%s AND property_unit_id=ANY(%s)",
                    (source, units),
                )
                addresses = rows(
                    c,
                    "SELECT e.building_id::text,b.municipality_code,a.street_name,a.address_number,a.original_values,a.address_place_id,a.run_id::text FROM stock.entrance e JOIN stock.address_place a USING(dataset_id,address_place_id) JOIN stock.building b USING(dataset_id,building_id) WHERE e.dataset_id=%s AND e.building_id=ANY(%s::uuid[])",
                    (source, ids),
                )
                candidates, _ = assess_epc(
                    buildings,
                    props,
                    designations,
                    subjects,
                    addresses,
                    events=events,
                    assessment_date=str(assessment),
                    shared_address_houses=shared,
                    address_building_counts=address_counts,
                    exact_address_keys=exact_keys,
                    strict_accepted_subjects=strict,
                )
                state["rows"] += bulk_insert(
                    c,
                    "evidence.epc_candidate",
                    [
                        dict(
                            dataset_id=source,
                            processing_release_id=processing,
                            building_id=r["building_id"],
                            subject_id=r["epc_subject_id"],
                            property_unit_id=r["property_unit_id"],
                            decision=r["decision"],
                            evidence_tier=r["evidence_tier"],
                            reasons=r["reason_codes"],
                            details={
                                k: v for k, v in r["evidence"].items() if k != "source_locators"
                            },
                            run_id=run,
                        )
                        for r in candidates
                    ],
                )
        _, results = assess_epc(
            [],
            [],
            [],
            subjects,
            [],
            events=events,
            assessment_date=str(assessment),
            shared_address_houses=shared,
        )
        for result in results:
            result["reasons"] = [
                r for r in result["reasons"] if r not in ("no_candidate", "no_accepted_candidate")
            ]
            result["details"].pop("source_locators", None)
        c.execute(
            "CREATE TEMP TABLE policy_results ON COMMIT DROP AS SELECT * FROM evidence.epc_subject_result WITH NO DATA"
        )
        bulk_insert(
            c,
            "pg_temp.policy_results",
            [
                dict(dataset_id=source, processing_release_id=processing, **r, run_id=run)
                for r in results
            ],
        )
        c.execute(
            """UPDATE policy_results s SET
         outcome=CASE WHEN EXISTS(SELECT 1 FROM evidence.epc_candidate r WHERE (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,s.processing_release_id,s.subject_id) AND decision='accepted') THEN 'matched' WHEN EXISTS(SELECT 1 FROM evidence.epc_candidate r WHERE (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,s.processing_release_id,s.subject_id)) THEN 'unresolved_candidates' ELSE 'no_candidate' END
         WHERE s.dataset_id=%s AND s.processing_release_id=%s AND s.subject_id=ANY(%s)""",
            (source, processing, list(by_subject)),
        )
        c.execute(
            """UPDATE policy_results s SET
         match_methods=ARRAY(SELECT DISTINCT r.details->>'match_method' FROM evidence.epc_candidate r WHERE (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,s.processing_release_id,s.subject_id) AND decision='accepted' ORDER BY 1),
         reasons=ARRAY(SELECT DISTINCT reason FROM (SELECT unnest(s.reasons) reason UNION ALL SELECT unnest(r.reasons) FROM evidence.epc_candidate r WHERE (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,s.processing_release_id,s.subject_id) AND (s.outcome<>'matched' OR r.decision='accepted') UNION ALL SELECT CASE WHEN s.outcome='no_candidate' THEN 'no_candidate' ELSE 'no_accepted_candidate' END WHERE s.outcome<>'matched') q ORDER BY reason),
         details=jsonb_set(jsonb_set(s.details,'{accepted_building_ids}',COALESCE((SELECT jsonb_agg(id ORDER BY id) FROM (SELECT DISTINCT building_id::text id FROM evidence.epc_candidate r WHERE (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,s.processing_release_id,s.subject_id) AND decision='accepted') a),'[]'::jsonb)),'{candidate_building_ids}',COALESCE((SELECT jsonb_agg(id ORDER BY id) FROM (SELECT DISTINCT building_id::text id FROM evidence.epc_candidate r WHERE (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,s.processing_release_id,s.subject_id)) a),'[]'::jsonb))
         WHERE s.dataset_id=%s AND s.processing_release_id=%s AND s.subject_id=ANY(%s)""",
            (source, processing, list(by_subject)),
        )
        c.execute(
            "UPDATE policy_results SET temporal_status='construction_conflict' WHERE dataset_id=%s AND processing_release_id=%s AND subject_id=ANY(%s) AND outcome<>'matched' AND 'construction_after_certificate'=ANY(reasons)",
            (source, processing, list(by_subject)),
        )

        result_columns = "dataset_id,processing_release_id,subject_id,outcome,match_methods,reporting_scope,temporal_status,assessment_date,reasons,details,run_id"
        state["rows"] += c.execute(
            f"INSERT INTO evidence.epc_subject_result({result_columns}) SELECT {result_columns} FROM policy_results"
        ).rowcount


def selection_info(c, dataset):
    row = c.execute(
        "SELECT source_dataset_id,processing_release_id,municipality_code,sealed FROM catalog.dataset WHERE dataset_id=%s AND dataset_kind<>'national_source'",
        (dataset,),
    ).fetchone()
    if not row:
        raise LookupError(dataset)
    return row


def plan_dataset(url, dataset=None, municipality="1485", source="national"):
    with _connect(url) as c:
        if dataset:
            found = c.execute(
                "SELECT source_dataset_id,municipality_code FROM catalog.dataset WHERE dataset_id=%s",
                (dataset,),
            ).fetchone()
            if found and found[0]:
                source, municipality = found
        count = c.execute(
            "SELECT count(*) FROM stock.building WHERE dataset_id=%s AND (%s::text IS NULL OR municipality_values @> ARRAY[%s]::text[])",
            (source, municipality, municipality),
        ).fetchone()[0]
        certificates = c.execute(
            "SELECT count(DISTINCT certificate_id) FROM evidence.epc_subject WHERE dataset_id=%s AND (%s::text IS NULL OR municipality_code=%s)",
            (source, municipality, municipality),
        ).fetchone()[0]
        layers = dict(
            c.execute(
                "SELECT layer,loading_status FROM catalog.dataset_layer WHERE dataset_id=%s",
                (source,),
            )
        )
        return {
            "source": source,
            "municipality": municipality,
            "primary_buildings": count,
            "certificates": certificates,
            "source_layers": layers,
            "external_source_reads": 0,
        }


def derive_context(url, dataset):
    with _connect(url) as c:
        source, processing, municipality, _ = selection_info(c, dataset)
        partitions = [
            r[0]
            for r in c.execute(
                "SELECT DISTINCT b.processing_partition FROM scope.dataset_building m JOIN stock.building b ON (b.dataset_id,b.building_id)=(m.source_dataset_id,m.building_id) WHERE m.dataset_id=%s AND m.selection_role='primary' ORDER BY b.processing_partition",
                (dataset,),
            )
        ]
        signature = content_hash([source, processing, dataset, "context"])
    for partition in partitions:
        with work(
            url,
            source,
            "context_linkage",
            partition,
            signature,
            processing=processing,
            selection=dataset,
        ) as state:
            if state is None:
                continue
            c = state["connection"]
            buildings = rows(
                c,
                "SELECT b.building_id::text,ST_AsBinary(f.display_geom) geometry,f.resolution_status geometry_status,b.municipality_code FROM scope.dataset_building m JOIN stock.building b ON (b.dataset_id,b.building_id)=(m.source_dataset_id,m.building_id) JOIN stock.building_footprint f ON (f.dataset_id,f.processing_release_id,f.building_id)=(b.dataset_id,%s,b.building_id) WHERE m.dataset_id=%s AND m.selection_role='primary' AND b.processing_partition=%s",
                (processing, dataset, partition),
            )
            for b in buildings:
                b["geometry"] = shapely.from_wkb(bytes(b["geometry"]))
            ids = [b["building_id"] for b in buildings]
            areas = rows(
                c,
                "SELECT a.area_type,a.area_code,ST_AsBinary(a.geom) geometry,a.area_type area_role FROM mart.context_areas a WHERE a.dataset_id=%s AND EXISTS(SELECT 1 FROM stock.building_footprint f WHERE f.dataset_id=%s AND f.processing_release_id=%s AND f.building_id=ANY(%s::uuid[]) AND ST_IsValid(f.display_geom) AND ST_Intersects(a.geom,f.display_geom))",
                (dataset, source, processing, ids),
            )
            for a in areas:
                a["geometry"] = shapely.from_wkb(bytes(a["geometry"]))
            relationships = derive_area_relations(buildings, areas)
            state["rows"] = bulk_insert(
                c,
                "context.building_area",
                [
                    dict(
                        dataset_id=dataset,
                        source_dataset_id=source,
                        building_id=r["building_id"],
                        area_type=r["area_type"],
                        area_code=r["area_code"],
                        footprint_fraction=r["footprint_fraction"],
                        rank=r["rank"],
                        is_primary=r["is_primary"],
                        assignment_status=r["resolution_status"],
                        reasons=r["reason_codes"],
                        run_id=state["run_id"],
                    )
                    for r in relationships
                ],
            )


def refresh_coverage(url, dataset):
    with _connect(url) as c:
        c.execute("DELETE FROM scope.coverage WHERE dataset_id=%s", (dataset,))
        for layer, table in [
            ("geometry", "mart.building_components"),
            ("register", "mart.building_purposes"),
            ("addresses", "mart.building_addresses"),
            ("properties", "mart.building_properties"),
            ("epc", "mart.building_epc"),
        ]:
            c.execute(
                f"INSERT INTO scope.coverage SELECT b.dataset_id,b.building_id,%s,CASE WHEN count(x.building_id)>0 THEN 'available' ELSE 'missing' END,count(x.building_id),1,'{{}}' FROM scope.dataset_building b LEFT JOIN {table} x USING(dataset_id,building_id) WHERE b.dataset_id=%s GROUP BY b.dataset_id,b.building_id",
                (layer, dataset),
            )
        c.execute(
            "UPDATE scope.coverage c SET available_count=CASE WHEN b.register_run_id IS NULL THEN 0 ELSE 1 END,status=CASE WHEN b.register_run_id IS NULL THEN 'missing' ELSE 'available' END FROM scope.dataset_building m JOIN stock.building b ON (b.dataset_id,b.building_id)=(m.source_dataset_id,m.building_id) WHERE c.dataset_id=m.dataset_id AND c.building_id=m.building_id AND c.dataset_id=%s AND c.layer='register'",
            (dataset,),
        )
        c.execute(
            "INSERT INTO scope.coverage SELECT m.dataset_id,m.building_id,'heights',CASE WHEN bool_or(h.height_status='failed') THEN 'failed' WHEN bool_or(h.height_status='representation_blocked') THEN 'ambiguous' WHEN count(h.component_key) FILTER(WHERE h.height_status='determined')=0 THEN 'missing' ELSE 'available' END,count(h.component_key) FILTER(WHERE h.height_status='determined'),count(g.component_key),'{}' FROM scope.dataset_building m LEFT JOIN stock.component_data g ON (g.dataset_id,g.building_id)=(m.source_dataset_id,m.building_id) LEFT JOIN evidence.component_height_data h ON h.component_key=g.component_key WHERE m.dataset_id=%s GROUP BY m.dataset_id,m.building_id",
            (dataset,),
        )
        c.execute(
            "UPDATE scope.coverage c SET status='ambiguous',reasons=ARRAY['geometry_unresolved'] FROM mart.building_facts f WHERE c.dataset_id=f.dataset_id AND c.building_id=f.building_id AND c.dataset_id=%s AND c.layer='epc' AND NOT f.linkage_eligible",
            (dataset,),
        )
        c.execute(
            "UPDATE scope.coverage c SET available_count=x.n,required_count=x.required,status=CASE WHEN x.n=x.required AND x.required>0 THEN 'available' WHEN x.n>0 THEN 'ambiguous' ELSE 'missing' END FROM (SELECT dataset_id,building_id,count(*) FILTER(WHERE resolved)::integer n,count(*)::integer required FROM mart.building_addresses GROUP BY dataset_id,building_id) x WHERE c.dataset_id=x.dataset_id AND c.building_id=x.building_id AND c.dataset_id=%s AND c.layer='addresses'",
            (dataset,),
        )
        for layer, kind in [
            ("municipality", "municipality"),
            ("regso", "regso"),
            ("population", "population_grid_1km"),
            ("income", "regso"),
        ]:
            c.execute(
                "INSERT INTO scope.coverage SELECT b.dataset_id,b.building_id,%s,CASE WHEN b.selection_role='epc_support' THEN 'not_applicable' WHEN bool_or(a.assignment_status IN ('ambiguous','geometry_unresolved','mismatch')) THEN 'ambiguous' WHEN count(a.building_id)>0 THEN 'available' ELSE 'missing' END,count(a.building_id) FILTER(WHERE a.is_primary),1,'{}' FROM scope.dataset_building b LEFT JOIN context.building_area a ON a.dataset_id=b.dataset_id AND a.building_id=b.building_id AND a.area_type=%s WHERE b.dataset_id=%s GROUP BY b.dataset_id,b.building_id,b.selection_role",
                (layer, kind, dataset),
            )
        # Income availability requires an actual usable observation, not an area link.
        c.execute(
            "UPDATE scope.coverage c SET status='missing',available_count=0 WHERE c.dataset_id=%s AND c.layer='income' AND c.status='available' AND NOT EXISTS(SELECT 1 FROM mart.building_context x WHERE x.dataset_id=c.dataset_id AND x.building_id=c.building_id AND x.is_primary AND x.indicator_id LIKE 'scb:income:%%' AND x.normalized_value IS NOT NULL)",
            (dataset,),
        )
    from .scopes import evaluate_scopes

    evaluate_scopes(url, dataset)


@logged_operation("database.dataset.process")
def process_dataset(url, **kwargs):
    # A selection's membership and publication must not race another scheduler.
    with _connect(url) as lock:
        lock.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("selection:" + kwargs.get("dataset", "uddevalla"),),
        )
        with serial_worker(url):
            return _process_dataset(url, **kwargs)


def _process_dataset(
    url,
    *,
    dataset="uddevalla",
    municipality="1485",
    source="national",
    assessment_date=None,
    event_sources=None,
    layers=None,
):
    if municipality is not None and (not municipality.isdigit() or len(municipality) != 4):
        raise ValueError("Four-digit municipality required")
    processing = release(url, source, assessment_date, event_sources)
    requested = layers or list(LAYERS)
    if set(requested) - set(LAYERS):
        raise ValueError("Unknown processing layer")
    with _connect(url) as c:
        existing = c.execute(
            "SELECT source_dataset_id,processing_release_id,municipality_code,sealed FROM catalog.dataset WHERE dataset_id=%s",
            (dataset,),
        ).fetchone()
        if existing and existing[:3] != (source, processing, municipality):
            if existing[3]:
                raise ValueError("Published selection is immutable; use a new dataset name")
            c.execute("DELETE FROM context.building_area WHERE dataset_id=%s", (dataset,))
            c.execute("DELETE FROM scope.coverage WHERE dataset_id=%s", (dataset,))
            c.execute("DELETE FROM scope.membership WHERE dataset_id=%s", (dataset,))
            c.execute("DELETE FROM scope.dataset_building WHERE dataset_id=%s", (dataset,))
            c.execute("DELETE FROM scope.dataset_certificate WHERE dataset_id=%s", (dataset,))
            c.execute("DELETE FROM catalog.work_unit WHERE selection_dataset_id=%s", (dataset,))
            c.execute(
                "UPDATE catalog.dataset SET processing_release_id=%s WHERE dataset_id=%s",
                (processing, dataset),
            )
            c.execute(
                "UPDATE catalog.dataset_layer SET loading_status='stale' WHERE dataset_id=%s",
                (dataset,),
            )
        c.execute(
            "INSERT INTO catalog.dataset(dataset_id,dataset_kind,municipality_code,source_dataset_id,processing_release_id) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                dataset,
                "municipality_selection" if municipality else "national_selection",
                municipality,
                source,
                processing,
            ),
        )
        for layer in LAYERS:
            c.execute(
                "INSERT INTO catalog.dataset_layer(dataset_id,layer) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (dataset, layer),
            )
        if existing and existing[3]:
            return {
                "dataset": dataset,
                "layers": {layer: "already_loaded" for layer in requested},
                "external_source_reads": 0,
            }
        c.execute(
            "INSERT INTO scope.dataset_building SELECT %s,%s,building_id,'primary' FROM stock.building WHERE dataset_id=%s AND (%s::text IS NULL OR municipality_values @> ARRAY[%s]::text[]) ON CONFLICT DO NOTHING",
            (dataset, source, source, municipality, municipality),
        )
        c.execute(
            "INSERT INTO scope.dataset_certificate SELECT DISTINCT %s,%s,certificate_id,'declared_municipality' FROM evidence.epc_subject WHERE dataset_id=%s AND (%s::text IS NULL OR municipality_code=%s) ON CONFLICT DO NOTHING",
            (dataset, source, source, municipality, municipality),
        )
        states = dict(
            c.execute(
                "SELECT layer,loading_status FROM catalog.dataset_layer WHERE dataset_id=%s",
                (source,),
            )
        )
        partitions = [
            r[0]
            for r in c.execute(
                "SELECT DISTINCT b.processing_partition FROM scope.dataset_building m JOIN stock.building b ON (b.dataset_id,b.building_id)=(m.source_dataset_id,m.building_id) WHERE m.dataset_id=%s ORDER BY b.processing_partition",
                (dataset,),
            )
        ]
    if states.get("geometry") != "loaded":
        raise ValueError("National geometry import is incomplete")
    prepare_source_quality(url, source, processing, states)
    # Property computation is shared and cheap to schedule with the geometry unit.
    for partition in partitions:
        prepare_partition(url, source, processing, partition)
    if "epc_linkage" in requested and all(
        states.get(layer) == "loaded" for layer in ("register", "addresses", "properties", "epc")
    ):
        with _connect(url) as c:
            cur = c.cursor(name="owned_certificates")
            cur.execute(
                "SELECT certificate_id FROM scope.dataset_certificate WHERE dataset_id=%s ORDER BY certificate_id",
                (dataset,),
            )
            for (certificate,) in cur:
                assess_certificate(url, source, processing, certificate)
        with _connect(url) as c:
            c.execute(
                "INSERT INTO scope.dataset_building SELECT DISTINCT %s,%s,r.building_id,'epc_support' FROM evidence.epc_candidate r JOIN evidence.epc_subject s USING(dataset_id,subject_id) JOIN scope.dataset_certificate m ON (m.source_dataset_id,m.certificate_id)=(s.dataset_id,s.certificate_id) WHERE m.dataset_id=%s AND r.processing_release_id=%s ON CONFLICT DO NOTHING",
                (dataset, source, dataset, processing),
            )
    if "context_linkage" in requested:
        derive_context(url, dataset)
    result = {}
    with _connect(url) as c:
        for layer in requested:
            ready = states.get(layer) == "loaded" if layer in states else True
            if layer == "epc_linkage":
                ready = all(
                    states.get(dependency) == "loaded"
                    for dependency in ("register", "addresses", "properties", "epc")
                )
            if layer == "context_linkage":
                ready = all(
                    states.get(dependency) == "loaded"
                    for dependency in ("municipality", "regso", "population")
                )
            if layer in ("municipality", "regso", "income") and municipality:
                kind = "regso" if layer == "income" else layer
                ready = (
                    ready
                    and c.execute(
                        "SELECT count(*)>0 FROM mart.context_areas WHERE dataset_id=%s AND area_type=%s",
                        (dataset, kind),
                    ).fetchone()[0]
                )
            if layer in ("municipality", "regso", "income", "context_linkage") and not municipality:
                # One saved snapshot cannot declare all Sweden's context ready.
                ready = (
                    ready
                    and c.execute(
                        """WITH required AS (
                  SELECT DISTINCT unnest(b.municipality_values) AS code
                  FROM scope.dataset_building m JOIN stock.building b
                  ON (b.dataset_id,b.building_id)=(m.source_dataset_id,m.building_id)
                  WHERE m.dataset_id=%s AND m.selection_role='primary'
                ) SELECT NOT EXISTS(SELECT 1 FROM required r WHERE NOT EXISTS(
                  SELECT 1 FROM context.area a WHERE a.dataset_id=%s
                  AND a.area_type='municipality' AND a.area_code=r.code)
                  OR (%s<>'municipality' AND NOT EXISTS(SELECT 1 FROM context.area_relation ar
                    WHERE ar.dataset_id=%s AND ar.parent_type='municipality' AND ar.parent_code=r.code AND ar.child_type='regso'))
                  OR (%s='income' AND NOT EXISTS(SELECT 1 FROM context.area_relation ar JOIN context.observation o
                    ON (o.dataset_id,o.area_type,o.area_code)=(ar.dataset_id,ar.child_type,ar.child_code)
                    WHERE ar.dataset_id=%s AND ar.parent_type='municipality' AND ar.parent_code=r.code
                    AND o.indicator_id='scb:income:median_earned')))
                  """,
                        (dataset, source, layer, source, layer, source),
                    ).fetchone()[0]
                )
            status = "loaded" if ready else "not_loaded"
            result[layer] = status
            c.execute(
                "UPDATE catalog.dataset_layer SET loading_status=%s,input_signature=%s WHERE dataset_id=%s AND layer=%s",
                (status, content_hash([processing, layer]), dataset, layer),
            )
    refresh_coverage(url, dataset)
    from .database import validate_dataset

    validation = validate_dataset(url, dataset)
    if validation["valid"] and set(requested) == set(LAYERS):
        with _connect(url) as c:
            if c.execute(
                "SELECT sealed FROM catalog.dataset WHERE dataset_id=%s", (source,)
            ).fetchone()[0]:
                c.execute("UPDATE catalog.dataset SET sealed=true WHERE dataset_id=%s", (dataset,))
                if municipality:
                    c.execute(
                        "INSERT INTO catalog.active_dataset(municipality_code,dataset_id) VALUES(%s,%s) ON CONFLICT(municipality_code) DO UPDATE SET dataset_id=EXCLUDED.dataset_id",
                        (municipality, dataset),
                    )
    return {
        "dataset": dataset,
        "source": source,
        "processing_release": processing,
        "layers": result,
        "validation": validation,
        "external_source_reads": 0,
    }

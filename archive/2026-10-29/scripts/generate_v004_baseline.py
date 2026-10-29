"""Generate a fresh compact baseline while preserving historical migration bytes."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

COMPACT_CATALOG = """
CREATE TABLE catalog.source_file_data (
 occurrence_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source_version_id text NOT NULL REFERENCES catalog.source_version,
 logical_name text NOT NULL, asset_key bigint NOT NULL REFERENCES catalog.asset(asset_key),
 bundle_path text NOT NULL, display_path text NOT NULL, format text NOT NULL,
 metadata jsonb NOT NULL DEFAULT '{}',ready boolean NOT NULL DEFAULT false, UNIQUE(source_version_id,logical_name));
CREATE VIEW catalog.source_file AS SELECT f.source_version_id,f.logical_name,a.sha256
 FROM catalog.source_file_data f JOIN catalog.asset a USING(asset_key);
CREATE TABLE catalog.record_locator (
 record_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 dataset_id text NOT NULL REFERENCES catalog.dataset,
 occurrence_key bigint NOT NULL REFERENCES catalog.source_file_data,
 kind text NOT NULL, source_record text NOT NULL, source_member text,
 UNIQUE NULLS NOT DISTINCT(dataset_id,occurrence_key,kind,source_record,source_member));
CREATE TABLE raw_lm.property_diagnostic (
 record_key bigint PRIMARY KEY REFERENCES catalog.record_locator,
 property_part_id text, property_unit_id text, diagnostic_geometry bytea);
CREATE TABLE raw_scb.record_data (
 record_key bigint PRIMARY KEY REFERENCES catalog.record_locator,
 original_values jsonb NOT NULL, original_geometry bytea);
CREATE TABLE raw_epc.before_v260_data (
 record_key bigint PRIMARY KEY REFERENCES catalog.record_locator,
 certificate_id text,subject_id text,original_values jsonb NOT NULL);
CREATE TABLE raw_epc.after_v260_data (LIKE raw_epc.before_v260_data INCLUDING ALL);
ALTER TABLE raw_epc.after_v260_data ADD FOREIGN KEY(record_key) REFERENCES catalog.record_locator;
CREATE VIEW catalog.located_record AS
 SELECT l.dataset_id,l.record_key::text record_id,l.kind,f.source_version_id,a.sha256 source_sha256,
 f.display_path source_file,l.source_member,l.source_record,l.record_key,l.occurrence_key
 FROM catalog.record_locator l JOIN catalog.source_file_data f USING(occurrence_key)
 JOIN catalog.asset a USING(asset_key);
CREATE VIEW raw_epc.before_v260_row AS SELECT l.dataset_id,l.record_id,l.kind,l.source_version_id,
 l.source_sha256,l.source_file,l.source_member,l.source_record,r.original_values,
 NULL::bytea original_geometry,r.certificate_id,r.subject_id
 FROM catalog.located_record l JOIN raw_epc.before_v260_data r USING(record_key);
CREATE VIEW raw_epc.after_v260_row AS SELECT l.dataset_id,l.record_id,l.kind,l.source_version_id,
 l.source_sha256,l.source_file,l.source_member,l.source_record,r.original_values,
 NULL::bytea original_geometry,r.certificate_id,r.subject_id
 FROM catalog.located_record l JOIN raw_epc.after_v260_data r USING(record_key);
CREATE VIEW raw_scb.record AS SELECT l.dataset_id,l.record_id,l.kind,l.source_version_id,
 l.source_sha256,l.source_file,l.source_member,l.source_record,r.original_values,r.original_geometry
 FROM catalog.located_record l JOIN raw_scb.record_data r USING(record_key);
CREATE TABLE catalog.ingestion_progress (
 source_dataset_id text REFERENCES catalog.dataset,layer text,step text,
 state text NOT NULL CHECK(state IN ('running','complete','failed')),
 expected_rows bigint,error text,started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(source_dataset_id,layer,step));
CREATE TABLE catalog.asset_verification (
 asset_key bigint PRIMARY KEY REFERENCES catalog.asset(asset_key),
 verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),source_rows bigint NOT NULL CHECK(source_rows>=0));
"""

COMPONENTS = """
CREATE TABLE stock.component_data (
 component_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 dataset_id text NOT NULL,building_id uuid NOT NULL,geometry_hash bytea NOT NULL CHECK(octet_length(geometry_hash)=32),
 geom geometry(Geometry,3006) NOT NULL,outline_labels text[] NOT NULL,
 representation_status text NOT NULL,owner_tile_id text,source_fids bigint[] NOT NULL,municipality_values text[] NOT NULL DEFAULT '{}',
 revision_timestamp text,object_version integer,run_id uuid NOT NULL REFERENCES catalog.process_run,
 UNIQUE(dataset_id,building_id,geometry_hash),UNIQUE(dataset_id,component_key),FOREIGN KEY(dataset_id,building_id) REFERENCES stock.building,
 CHECK(NOT ST_IsEmpty(geom)));
CREATE VIEW stock.geometry_component AS SELECT dataset_id,
 building_id::text||':'||encode(geometry_hash,'hex') component_id,building_id,
 encode(geometry_hash,'hex') geometry_hash,geom,outline_labels,representation_status,owner_tile_id,
 source_fids,revision_timestamp,object_version,run_id FROM stock.component_data;
CREATE TABLE stock.component_lineage (
 component_key bigint REFERENCES stock.component_data,source_fid bigint,
 occurrence_key bigint NOT NULL REFERENCES catalog.source_file_data,disposition text NOT NULL,
 revision_ts text,object_version integer,outline text,municipality text,house_number text,
 PRIMARY KEY(component_key,source_fid));
CREATE VIEW stock.component_source_feature AS SELECT g.dataset_id,
 g.building_id::text||':'||encode(g.geometry_hash,'hex') component_id,l.source_fid,
 f.source_version_id,l.disposition,
 jsonb_build_object('component_id',g.building_id::text||':'||encode(g.geometry_hash,'hex'),
 'building_id',g.building_id::text,'source_fid',l.source_fid::text,'disposition',l.disposition,
 'revision_ts',l.revision_ts,'object_version',l.object_version::text,'outline',l.outline,
 'municipality',l.municipality,'house_number',l.house_number) original_values
 FROM stock.component_lineage l JOIN stock.component_data g USING(component_key)
 JOIN catalog.source_file_data f USING(occurrence_key);
CREATE TABLE stock.component_relation_data (
 dataset_id text NOT NULL,processing_release_id uuid NOT NULL,
 left_component_key bigint,right_component_key bigint,
 relation_kind text NOT NULL,decision text NOT NULL,details jsonb NOT NULL,
 run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(processing_release_id,left_component_key,right_component_key),
 FOREIGN KEY(processing_release_id,dataset_id) REFERENCES catalog.processing_release(processing_release_id,source_dataset_id),
 FOREIGN KEY(dataset_id,left_component_key) REFERENCES stock.component_data(dataset_id,component_key),
 FOREIGN KEY(dataset_id,right_component_key) REFERENCES stock.component_data(dataset_id,component_key));
CREATE VIEW stock.component_relation AS SELECT a.dataset_id,r.processing_release_id,
 a.building_id::text||':'||encode(a.geometry_hash,'hex') left_component_id,
 b.building_id::text||':'||encode(b.geometry_hash,'hex') right_component_id,
 r.relation_kind,r.decision,r.details,r.run_id FROM stock.component_relation_data r
 JOIN stock.component_data a ON a.component_key=r.left_component_key
 JOIN stock.component_data b ON b.component_key=r.right_component_key;
"""

HEIGHTS = """
CREATE TABLE evidence.measurement_source (
 measurement_key bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source_version_id text NOT NULL REFERENCES catalog.source_version,
 occurrence_key bigint NOT NULL REFERENCES catalog.source_file_data,
 evidence_root text,original_path text NOT NULL,
 UNIQUE NULLS NOT DISTINCT(source_version_id,evidence_root,original_path));
CREATE TABLE evidence.height_catalogue (
 component_key bigint PRIMARY KEY REFERENCES stock.component_data,
 record_key bigint NOT NULL UNIQUE REFERENCES catalog.record_locator,
 measurement_key bigint REFERENCES evidence.measurement_source,baseline_row_offset bigint,
 owner_tile_id text,geometry_status text,failure_reason text,processing_status text,processing_error text,
 extra_fields jsonb NOT NULL DEFAULT '{}');
CREATE TABLE evidence.component_height_data (
 component_key bigint PRIMARY KEY REFERENCES stock.component_data,
 height_status text NOT NULL,representative_height_m double precision,eave_height_m double precision,
 ridge_height_m double precision,roof_form text,details jsonb NOT NULL,
 evidence_asset_key bigint REFERENCES catalog.asset(asset_key),measurement_record_key bigint REFERENCES catalog.record_locator,
 run_id uuid NOT NULL REFERENCES catalog.process_run);
CREATE VIEW evidence.component_height AS SELECT g.dataset_id,
 g.building_id::text||':'||encode(g.geometry_hash,'hex') component_id,
 encode(g.geometry_hash,'hex') input_geometry_hash,h.height_status,h.representative_height_m,
 h.eave_height_m,h.ridge_height_m,h.roof_form,h.details,a.sha256 evidence_sha256,h.run_id
 FROM evidence.component_height_data h JOIN stock.component_data g USING(component_key)
 LEFT JOIN catalog.asset a ON a.asset_key=h.evidence_asset_key;
CREATE FUNCTION catalog.source_float(v double precision) RETURNS text LANGUAGE SQL IMMUTABLE AS $$
 SELECT CASE WHEN v IS NULL THEN NULL WHEN v=trunc(v) AND abs(v)<1e16 THEN v::text||'.0' ELSE v::text END $$;
CREATE VIEW evidence.height_source_facts AS
 SELECT c.component_key,c.record_key,g.building_id::text||':'||encode(g.geometry_hash,'hex') component_id,
 (SELECT jsonb_object_agg(key,value) FROM jsonb_each(jsonb_build_object(
 'building_id',g.building_id::text,'component_id',g.building_id::text||':'||encode(g.geometry_hash,'hex'),
 'input_geometry_hash',encode(g.geometry_hash,'hex'),'owner_tile_id',c.owner_tile_id,
 'geometry_status',c.geometry_status,'failure_reason',c.failure_reason,'processing_status',c.processing_status,
 'processing_error',c.processing_error,'height_status',h.height_status,
 'representative_height_m',catalog.source_float(h.representative_height_m),
 'eave_height_m',catalog.source_float(h.eave_height_m),'ridge_height_m',catalog.source_float(h.ridge_height_m),
 'roof_form',h.roof_form,'evidence_record_path',m.original_path,'evidence_record_sha256',a.sha256,
 'evidence_root',m.evidence_root,'baseline_row_offset',c.baseline_row_offset::text)||c.extra_fields)
 WHERE f.metadata->'columns' IS NULL OR f.metadata->'columns' ? key) original_values
 FROM evidence.height_catalogue c JOIN stock.component_data g USING(component_key)
 JOIN evidence.component_height_data h USING(component_key)
 JOIN catalog.record_locator l USING(record_key) JOIN catalog.source_file_data f ON f.occurrence_key=l.occurrence_key
 LEFT JOIN evidence.measurement_source m USING(measurement_key)
 LEFT JOIN catalog.source_file_data mf ON mf.occurrence_key=m.occurrence_key
 LEFT JOIN catalog.asset a ON a.asset_key=mf.asset_key;
CREATE VIEW raw_height.record AS
 SELECT l.dataset_id,l.record_id,l.kind,l.source_version_id,l.source_sha256,l.source_file,l.source_member,
 CASE WHEN l.kind='component_height' THEN s.component_id ELSE l.source_record END source_record,
 CASE WHEN l.kind='component_height' THEN s.original_values ELSE '{}'::jsonb END original_values,
 NULL::bytea original_geometry FROM catalog.located_record l LEFT JOIN evidence.height_source_facts s USING(record_key)
 WHERE l.kind IN ('component_height','measurement');
"""


def generate():
    migration = ROOT / "sql/migrations"
    text = (migration / "001_evidence.sql").read_text().replace("v0.0.3", "v0.0.4").replace("'0.0.3'", "'0.0.4'")
    text = text.replace("CREATE TABLE catalog.asset (sha256", "CREATE TABLE catalog.asset (asset_key bigint GENERATED ALWAYS AS IDENTITY UNIQUE,sha256")
    a = text.index("CREATE TABLE catalog.asset_location (")
    b = text.index("CREATE TABLE catalog.source_version", a)
    text = text[:a] + """
CREATE TABLE catalog.asset_location_data(asset_key bigint REFERENCES catalog.asset(asset_key),
 profile text NOT NULL,path text NOT NULL,mtime_ns bigint NOT NULL,PRIMARY KEY(asset_key,profile,path));
CREATE VIEW catalog.asset_location AS SELECT a.sha256,l.profile,l.path,l.mtime_ns
 FROM catalog.asset_location_data l JOIN catalog.asset a USING(asset_key);
""" + text[b:]
    a = text.index("CREATE TABLE catalog.source_file (")
    b = text.index("CREATE TABLE catalog.process_run", a)
    # Record/progress tables need process/catalog definitions, so insert them at raw section.
    text = text[:a] + text[b:]
    a = text.index("-- Each projection retains")
    b = text.index("CREATE TABLE stock.building", a)
    text = text[:a] + COMPACT_CATALOG + text[b:]
    a = text.index("CREATE TABLE stock.geometry_component")
    b = text.index("CREATE TABLE stock.building_footprint", a)
    text = text[:a] + COMPONENTS + text[b:]
    a = text.index("CREATE TABLE evidence.component_height")
    b = text.index("CREATE TABLE evidence.building_property", a)
    text = text[:a] + HEIGHTS + text[b:]
    a = text.index("CREATE TABLE evidence.epc_subject (")
    b = text.index("CREATE TABLE evidence.epc_observation", a)
    subjects = text[a:b].replace("evidence.epc_subject (", "evidence.epc_subject_data (")
    subjects = subjects.replace("original_values jsonb NOT NULL", "representative_record_key bigint NOT NULL REFERENCES catalog.record_locator")
    subjects += """
CREATE VIEW evidence.epc_subject AS SELECT s.dataset_id,s.subject_id,s.certificate_id,
 s.municipality_code,s.designation_normalized,s.house_number,r.original_values,s.run_id
 FROM evidence.epc_subject_data s JOIN (
 SELECT record_key,original_values FROM raw_epc.before_v260_data UNION ALL
 SELECT record_key,original_values FROM raw_epc.after_v260_data) r ON r.record_key=s.representative_record_key;
"""
    text = text[:a] + subjects + text[b:]
    text = text.replace("REFERENCES evidence.epc_subject", "REFERENCES evidence.epc_subject_data")
    a = text.index("CREATE FUNCTION catalog.check_raw_source()")
    text = text[:a] + """
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['catalog.asset','catalog.source_version',
 'catalog.record_locator','raw_scb.record_data','raw_epc.before_v260_data','raw_epc.after_v260_data',
 'evidence.epc_candidate','evidence.epc_subject_result','evidence.epc_site_event'] LOOP
 EXECUTE format('CREATE TRIGGER immutable_source BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION catalog.keep_source_record()',t);
 END LOOP;
END $$;
CREATE FUNCTION catalog.keep_source_occurrence() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='DELETE' OR (to_jsonb(NEW)-'ready') IS DISTINCT FROM (to_jsonb(OLD)-'ready') THEN
 RAISE EXCEPTION 'Registered source occurrence facts are immutable'; END IF; RETURN NEW;
END $$;
CREATE TRIGGER immutable_occurrence BEFORE UPDATE OR DELETE ON catalog.source_file_data
 FOR EACH ROW EXECUTE FUNCTION catalog.keep_source_occurrence();
CREATE VIEW raw_lm.record AS SELECT l.dataset_id,l.record_id,l.kind,l.source_version_id,
 l.source_sha256,l.source_file,l.source_member,l.source_record,
 CASE WHEN l.kind='property' THEN jsonb_build_object('objektidentitet',d.property_part_id,
 'registerenhetsreferens',d.property_unit_id) ELSE '{}'::jsonb END original_values,
 COALESCE(d.diagnostic_geometry,ST_AsBinary(p.geom)) original_geometry
 FROM catalog.located_record l LEFT JOIN raw_lm.property_diagnostic d USING(record_key)
 LEFT JOIN stock.property_area_part p ON p.dataset_id=l.dataset_id AND p.property_area_part_id=d.property_part_id
 WHERE l.kind IN ('geometry','building_feature','register','address','property');
"""
    text = text.replace('raw_label text, normalized_label text NOT NULL,','raw_label text, normalized_label text NOT NULL,representative_position bigint NOT NULL,')
    # Secondary indexes are built after loading; retain the lookup indexes required by ingestion.
    processing = re.findall(r"CREATE INDEX[^;]+;", text)
    text = re.sub(r"CREATE INDEX[^;]+;", "", text)
    for name in ("002_views.sql", "004_run_clock.sql", "006_epc_event_inventory.sql", "008_quality_projection.sql", "010_verified_atemp.sql"):
        text += "\n" + (migration / name).read_text()
    text += """
CREATE VIEW mart.source_record_locators AS SELECT l.record_key record_id,l.dataset_id source_dataset_id,
 l.kind,l.source_record,l.source_member,f.logical_name,f.source_version_id,f.bundle_path,f.format,f.metadata,
 a.sha256 asset_sha256 FROM catalog.record_locator l JOIN catalog.source_file_data f USING(occurrence_key)
 JOIN catalog.asset a USING(asset_key);
"""
    text = text.replace(
        "(SELECT count(*) FROM evidence.component_height h JOIN stock.geometry_component c USING(dataset_id,component_id) WHERE (c.dataset_id,c.building_id)=(b.dataset_id,b.building_id) AND h.height_status='determined')",
        "(SELECT count(*) FROM evidence.component_height_data h JOIN stock.component_data c USING(component_key) WHERE (c.dataset_id,c.building_id)=(b.dataset_id,b.building_id) AND h.height_status='determined')")
    # Public IDs are projections; source joins use compact keys throughout.
    start = text.index("CREATE VIEW mart.component_heights AS")
    end = text.index(";",start)+1
    text = text[:start] + """CREATE VIEW mart.component_heights AS
 SELECT m.dataset_id,g.building_id,g.building_id::text||':'||encode(g.geometry_hash,'hex') component_id,
 encode(g.geometry_hash,'hex') input_geometry_hash,h.height_status,h.representative_height_m,
 h.eave_height_m,h.ridge_height_m,h.roof_form,(s.original_values||h.details) details,a.sha256 evidence_sha256,h.run_id
 FROM scope.dataset_building m JOIN stock.component_data g ON (g.dataset_id,g.building_id)=(m.source_dataset_id,m.building_id)
 JOIN evidence.component_height_data h USING(component_key) LEFT JOIN evidence.height_source_facts s USING(component_key)
 LEFT JOIN catalog.asset a ON a.asset_key=h.evidence_asset_key;""" + text[end:]
    # Catalogue lookup is typed and indexed, independent of JSON payloads.
    text += """
CREATE INDEX component_building_lookup ON stock.component_data(dataset_id,building_id);
CREATE INDEX height_asset_locator ON evidence.height_catalogue(measurement_key,baseline_row_offset);
CREATE INDEX source_file_asset_lookup ON catalog.source_file_data(asset_key,occurrence_key);
CREATE INDEX measurement_occurrence_lookup ON evidence.measurement_source(occurrence_key,measurement_key);
CREATE INDEX record_kind_lookup ON catalog.record_locator(dataset_id,kind);
CREATE INDEX raw_epc_before_certificate ON raw_epc.before_v260_data(certificate_id,subject_id);
CREATE INDEX raw_epc_after_certificate ON raw_epc.after_v260_data(certificate_id,subject_id);
CREATE INDEX raw_epc_before_subject ON raw_epc.before_v260_data(subject_id,record_key);
CREATE INDEX raw_epc_after_subject ON raw_epc.after_v260_data(subject_id,record_key);
"""
    # Processing indexes target physical relations, never compatibility views.
    secondary = (migration / "003_lookup_indexes.sql").read_text()
    secondary = "\n".join(line for line in secondary.splitlines() if not any(name in line for name in (
        "registered_source_hash_lookup", "raw_epc_", "raw_lm_kind_lookup", "height_measurement_locator_lookup", "raw_height_component_lookup", "component_building_lookup")))
    secondary = secondary.replace("evidence.epc_subject(", "evidence.epc_subject_data(")
    processing = [line.replace("stock.geometry_component", "stock.component_data").replace("evidence.epc_subject(", "evidence.epc_subject_data(") for line in processing]
    directory = ROOT / "sql/baselines/0.0.4"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "001_compact.sql").write_text(text)
    (directory / "processing_indexes.sql").write_text("\n".join(processing) + "\n" + secondary + "\n")


if __name__ == "__main__":
    generate()

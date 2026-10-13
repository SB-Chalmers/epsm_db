-- Clean v0.0.4 baseline. No in-place migration from the previous prototype.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS raw_lm;
CREATE SCHEMA IF NOT EXISTS raw_epc;
CREATE SCHEMA IF NOT EXISTS raw_height;
CREATE SCHEMA IF NOT EXISTS raw_scb;
CREATE SCHEMA IF NOT EXISTS stock;
CREATE SCHEMA IF NOT EXISTS evidence;
CREATE SCHEMA IF NOT EXISTS context;
CREATE SCHEMA IF NOT EXISTS quality;
CREATE SCHEMA IF NOT EXISTS scope;
CREATE SCHEMA IF NOT EXISTS mart;

CREATE TABLE catalog.dataset (
 dataset_id text PRIMARY KEY, municipality_code text CHECK (municipality_code ~ '^[0-9]{4}$'),
 dataset_kind text NOT NULL DEFAULT 'national_source' CHECK(dataset_kind IN ('national_source','municipality_selection','national_selection')),
 source_dataset_id text REFERENCES catalog.dataset, processing_release_id uuid, sealed boolean NOT NULL DEFAULT false,
 metadata jsonb NOT NULL DEFAULT '{}',
 CHECK((dataset_kind='national_source' AND municipality_code IS NULL AND source_dataset_id IS NULL AND processing_release_id IS NULL) OR
       (dataset_kind='municipality_selection' AND municipality_code IS NOT NULL AND source_dataset_id IS NOT NULL AND processing_release_id IS NOT NULL) OR
       (dataset_kind='national_selection' AND municipality_code IS NULL AND source_dataset_id IS NOT NULL AND processing_release_id IS NOT NULL)),
 schema_version text NOT NULL DEFAULT '0.0.4', created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(dataset_id,municipality_code), UNIQUE(dataset_id,source_dataset_id), UNIQUE(dataset_id,source_dataset_id,processing_release_id)
);
CREATE TABLE catalog.processing_release (
 processing_release_id uuid PRIMARY KEY, source_dataset_id text NOT NULL REFERENCES catalog.dataset,
 input_signature text NOT NULL UNIQUE, policy_versions jsonb NOT NULL, code_digest text NOT NULL,
 environment jsonb NOT NULL, field_mapping_digest text NOT NULL, assessment_date date NOT NULL,
 event_source_versions text[] NOT NULL DEFAULT '{}',
 UNIQUE(processing_release_id,source_dataset_id));
ALTER TABLE catalog.dataset ADD FOREIGN KEY(processing_release_id,source_dataset_id)
 REFERENCES catalog.processing_release(processing_release_id,source_dataset_id);
CREATE FUNCTION catalog.check_dataset_parent() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.source_dataset_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM catalog.dataset d WHERE d.dataset_id=NEW.source_dataset_id AND d.dataset_kind='national_source') THEN
  RAISE EXCEPTION 'Selection parent must be a national source snapshot';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER dataset_parent BEFORE INSERT OR UPDATE ON catalog.dataset FOR EACH ROW EXECUTE FUNCTION catalog.check_dataset_parent();
CREATE TABLE catalog.active_dataset (municipality_code text PRIMARY KEY, dataset_id text NOT NULL UNIQUE,
 FOREIGN KEY(dataset_id,municipality_code) REFERENCES catalog.dataset(dataset_id,municipality_code));
CREATE TABLE catalog.asset (asset_key bigint GENERATED ALWAYS AS IDENTITY UNIQUE,sha256 text PRIMARY KEY CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 byte_size bigint NOT NULL CHECK(byte_size>=0));

CREATE TABLE catalog.asset_location_data(asset_key bigint REFERENCES catalog.asset(asset_key),
 profile text NOT NULL,path text NOT NULL,mtime_ns bigint NOT NULL,PRIMARY KEY(asset_key,profile,path));
CREATE VIEW catalog.asset_location AS SELECT a.sha256,l.profile,l.path,l.mtime_ns
 FROM catalog.asset_location_data l JOIN catalog.asset a USING(asset_key);
CREATE TABLE catalog.source_version (source_version_id text PRIMARY KEY, layer text NOT NULL,
 publisher text NOT NULL, contract text NOT NULL, publication_status text NOT NULL,
 metadata jsonb NOT NULL DEFAULT '{}');
CREATE TABLE catalog.process_run (run_id uuid PRIMARY KEY, dataset_id text NOT NULL REFERENCES catalog.dataset,
 layer text NOT NULL, processing_release_id uuid REFERENCES catalog.processing_release, policy_version text NOT NULL, code_version text NOT NULL,
 status text NOT NULL CHECK(status IN ('running','complete','failed')),
 input_versions jsonb NOT NULL, started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 error text);
CREATE TABLE catalog.dataset_layer (dataset_id text REFERENCES catalog.dataset, layer text NOT NULL,
 loading_status text NOT NULL DEFAULT 'not_loaded' CHECK(loading_status IN ('not_loaded','loaded','failed','stale')),
 source_version_id text REFERENCES catalog.source_version, run_id uuid REFERENCES catalog.process_run, input_signature text,
 PRIMARY KEY(dataset_id,layer));
CREATE TABLE catalog.work_unit (
 work_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 owner_dataset_id text NOT NULL REFERENCES catalog.dataset,
 processing_release_id uuid, selection_dataset_id text, stage text NOT NULL, partition_key text NOT NULL,
 input_signature text NOT NULL, state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','complete','failed','stale')),
 checkpoint jsonb NOT NULL DEFAULT '{}', rows_written bigint NOT NULL DEFAULT 0,
 generation bigint NOT NULL DEFAULT 0, run_id uuid REFERENCES catalog.process_run, error text,
 FOREIGN KEY(processing_release_id,owner_dataset_id) REFERENCES catalog.processing_release(processing_release_id,source_dataset_id),
 FOREIGN KEY(selection_dataset_id,owner_dataset_id,processing_release_id) REFERENCES catalog.dataset(dataset_id,source_dataset_id,processing_release_id),
 CHECK(stage NOT IN ('geometry','property_linkage','epc_linkage') OR selection_dataset_id IS NULL),
 CHECK(stage NOT LIKE 'import:%' OR (processing_release_id IS NULL AND selection_dataset_id IS NULL)),
 UNIQUE NULLS NOT DISTINCT(owner_dataset_id,processing_release_id,selection_dataset_id,stage,partition_key));
CREATE TABLE catalog.field_mapping (regime text, source_field text, owner text NOT NULL,
 value_type text NOT NULL CHECK(value_type IN ('number','integer','boolean','text','date')),
 source_unit text, normalized_unit text, multiplier numeric NOT NULL DEFAULT 1,
 review_status text NOT NULL CHECK(review_status IN ('verified','unverified')),
 authority text, PRIMARY KEY(regime,source_field),
 CHECK(review_status <> 'verified' OR authority IS NOT NULL));
INSERT INTO catalog.field_mapping(regime,source_field,owner,value_type,source_unit,normalized_unit,review_status,authority)
VALUES
 ('fore_v260','ExpertGodkannande','certificate','date',NULL,NULL,'verified','Boverket Mappningsfil före v2.6: DEKLARATION, Datum för godkännande'),
 ('efter_v260','ExpertGodkannande','certificate','date',NULL,NULL,'verified','Boverket Mappningsfil efter v2.6: DEKLARATION, Datum för godkännande'),
 ('fore_v260','EgenAtemp','certificate','number','m2','m2','verified','Boverket Mappningsfil före v2.6: DEKLARATION, Mätt värde Atemp, m2'),
 ('fore_v260','EgenNybyggAr','certificate','integer','year','year','verified','Boverket Mappningsfil före v2.6: DEKLARATION, Nybyggnadsår'),
 ('efter_v260','EgenNybyggAr','certificate','integer','year','year','verified','Boverket Mappningsfil efter v2.6: DEKLARATION, Nybyggnadsår'),
 ('efter_v260','EgenAtemp','certificate','number','m2','m2','unverified',NULL);


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
CREATE TABLE stock.building (dataset_id text REFERENCES catalog.dataset, building_id uuid,
 municipality_code text, municipality_values text[] NOT NULL DEFAULT '{}', processing_partition text,
 object_status text, status_verified boolean NOT NULL DEFAULT false, is_existing boolean,
 house_number integer, construction_year integer, main_building boolean,
 register_run_id uuid REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,building_id));
CREATE TABLE scope.dataset_building (
 dataset_id text, source_dataset_id text NOT NULL, building_id uuid,
 selection_role text NOT NULL CHECK(selection_role IN ('primary','epc_support')),
 PRIMARY KEY(dataset_id,building_id),
 FOREIGN KEY(dataset_id,source_dataset_id) REFERENCES catalog.dataset(dataset_id,source_dataset_id),
 FOREIGN KEY(source_dataset_id,building_id) REFERENCES stock.building(dataset_id,building_id));


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
CREATE TABLE stock.building_footprint (dataset_id text, processing_release_id uuid NOT NULL, building_id uuid,
 display_geom geometry(Geometry,3006) NOT NULL, linkage_geom geometry(Geometry,3006),
 resolution_status text NOT NULL, quality_flags text[] NOT NULL DEFAULT '{}',
 run_id uuid NOT NULL REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,processing_release_id,building_id),
 FOREIGN KEY(processing_release_id,dataset_id) REFERENCES catalog.processing_release(processing_release_id,source_dataset_id),
 FOREIGN KEY(dataset_id,building_id) REFERENCES stock.building,
 CHECK(resolution_status='resolved' OR linkage_geom IS NULL));

CREATE TABLE stock.building_purpose (dataset_id text, building_id uuid, ordinal integer,
 category text NOT NULL, detail text, is_main boolean, original_values jsonb NOT NULL,
 run_id uuid NOT NULL REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,building_id,ordinal),
 FOREIGN KEY(dataset_id,building_id) REFERENCES stock.building);
CREATE TABLE stock.entrance (dataset_id text, entrance_id text, building_id uuid NOT NULL,
 address_place_id text, original_values jsonb NOT NULL, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,entrance_id), FOREIGN KEY(dataset_id,building_id) REFERENCES stock.building);
CREATE TABLE stock.address_place (dataset_id text, address_place_id text, street_name text,
 address_number text, locality text, match_key text, base_match_key text, geom geometry(Point,3006), original_values jsonb NOT NULL,
 run_id uuid NOT NULL REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,address_place_id));
CREATE TABLE stock.property_unit (dataset_id text REFERENCES catalog.dataset, property_unit_id text,
 PRIMARY KEY(dataset_id,property_unit_id));
CREATE TABLE stock.property_area_part (dataset_id text, property_area_part_id text,
 property_unit_id text NOT NULL, geom geometry(Geometry,3006) NOT NULL,
 run_id uuid NOT NULL REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,property_area_part_id),
 FOREIGN KEY(dataset_id,property_unit_id) REFERENCES stock.property_unit);

CREATE TABLE stock.property_designation (dataset_id text, property_unit_id text,
 municipality_code text, raw_label text, normalized_label text NOT NULL,representative_position bigint NOT NULL,
 PRIMARY KEY(dataset_id,property_unit_id,municipality_code,normalized_label),
 FOREIGN KEY(dataset_id,property_unit_id) REFERENCES stock.property_unit);


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
CREATE TABLE evidence.building_property (dataset_id text, processing_release_id uuid NOT NULL, building_id uuid, property_unit_id text,
 intersection_area_m2 double precision NOT NULL CHECK(intersection_area_m2>0),
 overlap_fraction double precision NOT NULL CHECK(overlap_fraction>0 AND overlap_fraction<=1.000000001),
 rank integer NOT NULL, is_primary boolean NOT NULL, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,processing_release_id,building_id,property_unit_id),
 FOREIGN KEY(processing_release_id,dataset_id) REFERENCES catalog.processing_release(processing_release_id,source_dataset_id),
 FOREIGN KEY(dataset_id,building_id) REFERENCES stock.building,
 FOREIGN KEY(dataset_id,property_unit_id) REFERENCES stock.property_unit);
CREATE TABLE evidence.epc_certificate (dataset_id text REFERENCES catalog.dataset, certificate_id text,
 formular_id bigint NOT NULL, regime text NOT NULL CHECK(regime IN ('fore_v260','efter_v260')),
 certificate_date date, date_status text NOT NULL DEFAULT 'unverified',
 closure_status text NOT NULL DEFAULT 'unknown', run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,certificate_id));
CREATE TABLE scope.dataset_certificate (
 dataset_id text, source_dataset_id text NOT NULL, certificate_id text, inclusion_reason text NOT NULL,
 PRIMARY KEY(dataset_id,certificate_id),
 FOREIGN KEY(dataset_id,source_dataset_id) REFERENCES catalog.dataset(dataset_id,source_dataset_id),
 FOREIGN KEY(source_dataset_id,certificate_id) REFERENCES evidence.epc_certificate(dataset_id,certificate_id));
CREATE TABLE evidence.epc_subject_data (dataset_id text, subject_id text, certificate_id text NOT NULL,
 municipality_code text NOT NULL, designation_normalized text NOT NULL, house_number integer,
 representative_record_key bigint NOT NULL REFERENCES catalog.record_locator, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,subject_id),
 FOREIGN KEY(dataset_id,certificate_id) REFERENCES evidence.epc_certificate);

CREATE VIEW evidence.epc_subject AS SELECT s.dataset_id,s.subject_id,s.certificate_id,
 s.municipality_code,s.designation_normalized,s.house_number,r.original_values,s.run_id
 FROM evidence.epc_subject_data s JOIN (
 SELECT record_key,original_values FROM raw_epc.before_v260_data UNION ALL
 SELECT record_key,original_values FROM raw_epc.after_v260_data) r ON r.record_key=s.representative_record_key;
CREATE TABLE evidence.epc_observation (dataset_id text, observation_id text, certificate_id text NOT NULL,
 owner_kind text NOT NULL, owner_key text, source_field text NOT NULL, raw_values jsonb NOT NULL,
 normalized_value jsonb, source_unit text, normalized_unit text,
 value_status text NOT NULL, mapping_status text NOT NULL, source_locators jsonb NOT NULL,
 run_id uuid NOT NULL REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,observation_id),
 FOREIGN KEY(dataset_id,certificate_id) REFERENCES evidence.epc_certificate);
CREATE TABLE evidence.epc_candidate (dataset_id text, processing_release_id uuid NOT NULL, building_id uuid, subject_id text, property_unit_id text,
 decision text NOT NULL CHECK(decision IN ('accepted','rejected')), evidence_tier text,
 reasons text[] NOT NULL, details jsonb NOT NULL, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,processing_release_id,building_id,subject_id,property_unit_id),
 FOREIGN KEY(processing_release_id,dataset_id) REFERENCES catalog.processing_release(processing_release_id,source_dataset_id),
 FOREIGN KEY(dataset_id,building_id) REFERENCES stock.building,
 FOREIGN KEY(dataset_id,subject_id) REFERENCES evidence.epc_subject_data,
 FOREIGN KEY(dataset_id,property_unit_id) REFERENCES stock.property_unit);

-- Every parsed subject receives an outcome, including subjects with zero candidates.
CREATE TABLE evidence.epc_subject_result (
 dataset_id text, processing_release_id uuid NOT NULL, subject_id text, outcome text NOT NULL
 CHECK(outcome IN ('matched','unresolved_candidates','no_candidate')),
 match_methods text[] NOT NULL, reporting_scope text NOT NULL
 CHECK(reporting_scope IN ('declared_single_subject','declared_multiple_subjects')),
 temporal_status text NOT NULL CHECK(temporal_status IN
 ('unknown','site_change_warning','construction_conflict','certificate_after_assessment_date')),
 assessment_date date NOT NULL, reasons text[] NOT NULL, details jsonb NOT NULL,
 run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,processing_release_id,subject_id),
 FOREIGN KEY(processing_release_id,dataset_id) REFERENCES catalog.processing_release(processing_release_id,source_dataset_id),
 FOREIGN KEY(dataset_id,subject_id) REFERENCES evidence.epc_subject_data);
CREATE TABLE evidence.epc_site_event (
 dataset_id text REFERENCES catalog.dataset, event_id text, municipality_code text NOT NULL,
 property_match_key text, address text, postcode text, event_kind text NOT NULL,
 reported_date date NOT NULL, date_basis text NOT NULL, description text NOT NULL,
 source_url text NOT NULL, source_sha256 text NOT NULL REFERENCES catalog.asset,
 source_version_id text NOT NULL REFERENCES catalog.source_version,
 source_document_sha256 text REFERENCES catalog.asset,
 evidence_scope text NOT NULL CHECK(evidence_scope='site'),
 run_id uuid NOT NULL REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,event_id));

CREATE TABLE context.area (dataset_id text REFERENCES catalog.dataset, area_type text, area_code text,
 name text, reference_period text NOT NULL, geom geometry(Geometry,3006) NOT NULL,
 run_id uuid NOT NULL REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,area_type,area_code));

CREATE TABLE context.area_relation (dataset_id text, child_type text, child_code text,
 parent_type text, parent_code text, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,child_type,child_code,parent_type,parent_code),
 FOREIGN KEY(dataset_id,child_type,child_code) REFERENCES context.area,
 FOREIGN KEY(dataset_id,parent_type,parent_code) REFERENCES context.area);
CREATE TABLE context.observation (dataset_id text, observation_id text, area_type text NOT NULL,
 area_code text NOT NULL, indicator_id text NOT NULL, reference_period text NOT NULL,
 dimensions jsonb NOT NULL, raw_value text, normalized_value double precision,
 source_unit text NOT NULL, normalized_unit text NOT NULL, transformation text NOT NULL,
 value_status text NOT NULL, provenance jsonb NOT NULL, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,observation_id), FOREIGN KEY(dataset_id,area_type,area_code) REFERENCES context.area);
CREATE TABLE context.building_area (dataset_id text, source_dataset_id text NOT NULL, building_id uuid, area_type text, area_code text,
 footprint_fraction double precision NOT NULL, rank integer NOT NULL, is_primary boolean NOT NULL,
 assignment_status text NOT NULL, reasons text[] NOT NULL, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,building_id,area_type,area_code),
 FOREIGN KEY(dataset_id,building_id) REFERENCES scope.dataset_building,
 FOREIGN KEY(dataset_id,source_dataset_id) REFERENCES catalog.dataset(dataset_id,source_dataset_id),
 FOREIGN KEY(source_dataset_id,area_type,area_code) REFERENCES context.area);
CREATE UNIQUE INDEX unique_primary_area ON context.building_area(dataset_id,building_id,area_type) WHERE is_primary;
CREATE TABLE quality.finding (finding_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 dataset_id text NOT NULL REFERENCES catalog.dataset, building_id uuid, layer text NOT NULL,
 code text NOT NULL, details jsonb NOT NULL, processing_release_id uuid REFERENCES catalog.processing_release,
 source_record_id text, run_id uuid REFERENCES catalog.process_run);
CREATE TABLE scope.definition (dataset_id text REFERENCES catalog.dataset, scope_id text,
 rules jsonb NOT NULL, PRIMARY KEY(dataset_id,scope_id));
CREATE TABLE scope.membership (dataset_id text, scope_id text, building_id uuid,
 inclusion_state text NOT NULL, reasons text[] NOT NULL,
 PRIMARY KEY(dataset_id,scope_id,building_id), FOREIGN KEY(dataset_id,scope_id) REFERENCES scope.definition,
 FOREIGN KEY(dataset_id,building_id) REFERENCES scope.dataset_building);
CREATE TABLE scope.coverage (dataset_id text, building_id uuid, layer text,
 status text NOT NULL CHECK(status IN ('available','missing','failed','ambiguous','not_applicable')),
 available_count integer NOT NULL, required_count integer NOT NULL, reasons text[] NOT NULL,
 PRIMARY KEY(dataset_id,building_id,layer), FOREIGN KEY(dataset_id,building_id) REFERENCES scope.dataset_building);

REVOKE ALL ON SCHEMA catalog,raw_lm,raw_epc,raw_height,raw_scb,stock,evidence,context,quality,scope,mart FROM PUBLIC;

CREATE FUNCTION catalog.keep_source_record() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 RAISE EXCEPTION 'Original source records are append-only; rebuild the disposable application schema explicitly';
END $$;

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

-- Selection projections over shared national evidence.
CREATE VIEW mart.datasets AS
SELECT dataset_id,municipality_code,schema_version,created_at FROM catalog.dataset WHERE dataset_kind<>'national_source';

CREATE VIEW mart.building_purposes AS
SELECT m.dataset_id,x.building_id,x.ordinal,x.category,x.detail,x.is_main,x.original_values,x.run_id FROM scope.dataset_building m JOIN stock.building_purpose x ON x.dataset_id=m.source_dataset_id AND x.building_id=m.building_id;

CREATE VIEW mart.building_components AS
SELECT m.dataset_id,x.component_id,x.building_id,x.geometry_hash,x.geom,x.outline_labels,x.representation_status,x.owner_tile_id,x.source_fids,x.revision_timestamp,x.object_version,x.run_id FROM scope.dataset_building m JOIN stock.geometry_component x ON x.dataset_id=m.source_dataset_id AND x.building_id=m.building_id;

CREATE VIEW mart.component_heights AS
 SELECT m.dataset_id,g.building_id,g.building_id::text||':'||encode(g.geometry_hash,'hex') component_id,
 encode(g.geometry_hash,'hex') input_geometry_hash,h.height_status,h.representative_height_m,
 h.eave_height_m,h.ridge_height_m,h.roof_form,(s.original_values||h.details) details,a.sha256 evidence_sha256,h.run_id
 FROM scope.dataset_building m JOIN stock.component_data g ON (g.dataset_id,g.building_id)=(m.source_dataset_id,m.building_id)
 JOIN evidence.component_height_data h USING(component_key) LEFT JOIN evidence.height_source_facts s USING(component_key)
 LEFT JOIN catalog.asset a ON a.asset_key=h.evidence_asset_key;

CREATE VIEW mart.building_addresses AS
SELECT m.dataset_id,e.building_id,e.entrance_id,e.address_place_id,a.street_name,a.address_number,a.locality,a.geom,(a.address_place_id IS NOT NULL) AS resolved,e.original_values,e.run_id FROM scope.dataset_building m JOIN stock.entrance e ON (e.dataset_id,e.building_id)=(m.source_dataset_id,m.building_id) LEFT JOIN stock.address_place a ON (a.dataset_id,a.address_place_id)=(e.dataset_id,e.address_place_id);

CREATE VIEW mart.building_properties AS
SELECT m.dataset_id,p.building_id,p.property_unit_id,p.intersection_area_m2,p.overlap_fraction,p.rank,p.is_primary,p.run_id FROM scope.dataset_building m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN evidence.building_property p ON (p.dataset_id,p.processing_release_id,p.building_id)=(m.source_dataset_id,d.processing_release_id,m.building_id);

CREATE VIEW mart.epc_subjects AS
SELECT m.dataset_id,x.subject_id,x.certificate_id,x.municipality_code,x.designation_normalized,x.house_number,x.original_values,x.run_id FROM scope.dataset_certificate m JOIN evidence.epc_subject x ON (x.dataset_id,x.certificate_id)=(m.source_dataset_id,m.certificate_id);

CREATE VIEW mart.epc_observations AS
SELECT m.dataset_id,x.observation_id,x.certificate_id,x.owner_kind,x.owner_key,x.source_field,x.raw_values,x.normalized_value,x.source_unit,x.normalized_unit,x.value_status,x.mapping_status,COALESCE((SELECT jsonb_agg(jsonb_build_object('file',r.source_file,'record',r.source_record) ORDER BY r.source_file,r.source_record::bigint) FROM (SELECT source_file,source_record,original_values FROM raw_epc.before_v260_row WHERE dataset_id=x.dataset_id AND certificate_id=x.certificate_id UNION ALL SELECT source_file,source_record,original_values FROM raw_epc.after_v260_row WHERE dataset_id=x.dataset_id AND certificate_id=x.certificate_id) r WHERE COALESCE(r.original_values->>x.source_field,'')<>''),'[]'::jsonb) AS source_locators,x.run_id FROM scope.dataset_certificate m JOIN evidence.epc_observation x ON (x.dataset_id,x.certificate_id)=(m.source_dataset_id,m.certificate_id);

CREATE VIEW mart.epc_candidates AS
SELECT m.dataset_id,r.building_id,r.subject_id,s.certificate_id,r.property_unit_id,r.decision,r.evidence_tier,r.reasons,(r.details||jsonb_build_object('source_locators',COALESCE((SELECT jsonb_agg(jsonb_build_object('table',r.source_table,'record_id',r.record_id,'file',r.source_file,'record',r.source_record,'sha256',r.source_sha256) ORDER BY r.source_file,r.source_record::bigint) FROM (SELECT 'raw_epc.before_v260_row'::text source_table,record_id,source_file,source_record,source_sha256 FROM raw_epc.before_v260_row WHERE dataset_id=s.dataset_id AND certificate_id=s.certificate_id AND subject_id=s.subject_id UNION ALL SELECT 'raw_epc.after_v260_row',record_id,source_file,source_record,source_sha256 FROM raw_epc.after_v260_row WHERE dataset_id=s.dataset_id AND certificate_id=s.certificate_id AND subject_id=s.subject_id) r),'[]'::jsonb))) AS details,r.run_id FROM scope.dataset_certificate m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN evidence.epc_subject s ON (s.dataset_id,s.certificate_id)=(m.source_dataset_id,m.certificate_id) JOIN evidence.epc_candidate r ON (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,d.processing_release_id,s.subject_id);

CREATE VIEW mart.building_epc AS
SELECT r.dataset_id,r.building_id,r.subject_id,r.certificate_id,r.property_unit_id,r.decision,r.evidence_tier,r.reasons,r.details,r.run_id FROM mart.epc_candidates r JOIN scope.dataset_building m USING(dataset_id,building_id) WHERE r.decision='accepted';

CREATE VIEW mart.epc_raw_inventory AS
SELECT m.dataset_id,r.record_id,r.source_sha256,r.source_file,r.source_record,'fore_v260'::text AS regime FROM scope.dataset_certificate m JOIN raw_epc.before_v260_row r ON (r.dataset_id,r.certificate_id)=(m.source_dataset_id,m.certificate_id) UNION ALL SELECT m.dataset_id,r.record_id,r.source_sha256,r.source_file,r.source_record,'efter_v260'::text FROM scope.dataset_certificate m JOIN raw_epc.after_v260_row r ON (r.dataset_id,r.certificate_id)=(m.source_dataset_id,m.certificate_id);

CREATE VIEW mart.epc_subject_results AS
SELECT m.dataset_id,r.subject_id,s.certificate_id,s.municipality_code,s.designation_normalized,s.house_number,r.outcome,r.match_methods,r.reporting_scope,r.temporal_status,r.assessment_date,r.reasons,(r.details||jsonb_build_object('source_locators',COALESCE((SELECT jsonb_agg(jsonb_build_object('table',r.source_table,'record_id',r.record_id,'file',r.source_file,'record',r.source_record,'sha256',r.source_sha256) ORDER BY r.source_file,r.source_record::bigint) FROM (SELECT 'raw_epc.before_v260_row'::text source_table,record_id,source_file,source_record,source_sha256 FROM raw_epc.before_v260_row WHERE dataset_id=s.dataset_id AND certificate_id=s.certificate_id AND subject_id=s.subject_id UNION ALL SELECT 'raw_epc.after_v260_row',record_id,source_file,source_record,source_sha256 FROM raw_epc.after_v260_row WHERE dataset_id=s.dataset_id AND certificate_id=s.certificate_id AND subject_id=s.subject_id) r),'[]'::jsonb))) AS details,r.run_id FROM scope.dataset_certificate m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN evidence.epc_subject s ON (s.dataset_id,s.certificate_id)=(m.source_dataset_id,m.certificate_id) JOIN evidence.epc_subject_result r ON (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,d.processing_release_id,s.subject_id);

CREATE VIEW mart.epc_site_events AS
SELECT d.dataset_id,e.event_id,e.municipality_code,e.property_match_key,e.address,e.postcode,e.event_kind,e.reported_date,e.date_basis,e.description,e.source_url,e.source_sha256,e.source_version_id,e.source_document_sha256,e.evidence_scope,e.run_id FROM catalog.dataset d JOIN catalog.processing_release p USING(processing_release_id) JOIN evidence.epc_site_event e ON e.dataset_id=d.source_dataset_id AND e.source_version_id=ANY(p.event_source_versions) WHERE d.municipality_code IS NULL OR e.municipality_code=d.municipality_code;

CREATE VIEW mart.epc_diagnostic_summary AS
SELECT dataset_id,municipality_code,outcome,reporting_scope,temporal_status,count(*) AS subject_count,count(DISTINCT certificate_id) AS certificate_count FROM mart.epc_subject_results GROUP BY dataset_id,municipality_code,outcome,reporting_scope,temporal_status;

CREATE VIEW mart.epc_certificates AS
SELECT m.dataset_id,c.certificate_id,c.formular_id,c.regime,c.certificate_date,c.date_status,c.closure_status,c.run_id,
 (SELECT count(DISTINCT building_id) FROM mart.building_epc r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id)) AS loaded_building_count,
 (SELECT count(*) FROM evidence.epc_subject s WHERE (s.dataset_id,s.certificate_id)=(c.dataset_id,c.certificate_id)) AS subject_count,
 (SELECT count(*) FROM mart.epc_subject_results r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id) AND outcome='matched') AS matched_subject_count,
 CASE WHEN NOT EXISTS(SELECT 1 FROM evidence.epc_subject s WHERE (s.dataset_id,s.certificate_id)=(c.dataset_id,c.certificate_id)) THEN 'no_parsed_subjects'
 WHEN NOT EXISTS(SELECT 1 FROM mart.epc_subject_results r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id)) THEN 'not_assessed'
 WHEN NOT EXISTS(SELECT 1 FROM mart.epc_subject_results r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id) AND outcome='matched') THEN 'unmatched'
 WHEN EXISTS(SELECT 1 FROM mart.epc_subject_results r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id) AND outcome<>'matched') THEN 'partly_matched' ELSE 'fully_matched' END AS matching_status,
 'loaded_relations'::text AS cardinality_scope
 FROM scope.dataset_certificate m JOIN evidence.epc_certificate c ON (c.dataset_id,c.certificate_id)=(m.source_dataset_id,m.certificate_id);

CREATE VIEW mart.building_facts AS
SELECT m.dataset_id,b.building_id,b.municipality_code,m.selection_role,b.object_status,b.status_verified,b.is_existing,b.house_number,b.construction_year,b.main_building,
 f.display_geom AS geom,f.resolution_status AS geometry_status,(f.linkage_geom IS NOT NULL) AS linkage_eligible,f.quality_flags,ST_Area(f.display_geom) AS area_m2,
 (SELECT count(*) FROM stock.geometry_component c WHERE (c.dataset_id,c.building_id)=(b.dataset_id,b.building_id)) AS component_count,
 (SELECT count(*) FROM evidence.component_height_data h JOIN stock.component_data c USING(component_key) WHERE (c.dataset_id,c.building_id)=(b.dataset_id,b.building_id) AND h.height_status='determined') AS determined_height_component_count,
 (SELECT count(*) FROM stock.entrance e WHERE (e.dataset_id,e.building_id)=(b.dataset_id,b.building_id)) AS entrance_count,
 (SELECT count(*) FROM stock.entrance e JOIN stock.address_place a USING(dataset_id,address_place_id) WHERE (e.dataset_id,e.building_id)=(b.dataset_id,b.building_id)) AS resolved_address_count,
 (SELECT count(*) FROM mart.building_properties p WHERE (p.dataset_id,p.building_id)=(m.dataset_id,b.building_id)) AS property_candidate_count,
 (SELECT count(DISTINCT r.certificate_id) FROM mart.building_epc r WHERE (r.dataset_id,r.building_id)=(m.dataset_id,b.building_id)) AS epc_certificate_count,
 (SELECT count(*) FROM mart.epc_candidates r WHERE (r.dataset_id,r.building_id)=(m.dataset_id,b.building_id) AND decision='rejected') AS rejected_epc_candidate_count,
 COALESCE((SELECT array_agg(DISTINCT p.category ORDER BY p.category) FROM stock.building_purpose p WHERE (p.dataset_id,p.building_id)=(b.dataset_id,b.building_id)),'{}') AS purposes
 FROM scope.dataset_building m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN stock.building b ON (b.dataset_id,b.building_id)=(m.source_dataset_id,m.building_id)
 LEFT JOIN stock.building_footprint f ON (f.dataset_id,f.processing_release_id,f.building_id)=(b.dataset_id,d.processing_release_id,b.building_id);

CREATE VIEW mart.property_parts AS
WITH bounds AS (SELECT m.dataset_id,m.source_dataset_id,ST_SetSRID(ST_Extent(f.display_geom)::geometry,3006) geom FROM scope.dataset_building m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN stock.building_footprint f ON (f.dataset_id,f.processing_release_id,f.building_id)=(m.source_dataset_id,d.processing_release_id,m.building_id) GROUP BY m.dataset_id,m.source_dataset_id)
 SELECT x.dataset_id,p.property_area_part_id,p.property_unit_id,p.geom FROM bounds x JOIN stock.property_area_part p ON p.dataset_id=x.source_dataset_id AND ST_Intersects(p.geom,x.geom);

CREATE VIEW mart.property_units AS
SELECT DISTINCT dataset_id,property_unit_id FROM mart.property_parts;

CREATE VIEW mart.property_designations AS
SELECT m.dataset_id,p.property_unit_id,p.municipality_code,p.raw_label,p.normalized_label FROM mart.property_units m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN stock.property_designation p ON (p.dataset_id,p.property_unit_id)=(d.source_dataset_id,m.property_unit_id);

CREATE VIEW mart.context_areas AS
SELECT d.dataset_id,a.area_type,a.area_code,a.name,a.reference_period,a.geom,a.run_id FROM catalog.dataset d JOIN context.area a ON a.dataset_id=d.source_dataset_id WHERE d.dataset_kind='national_selection' OR
 (a.area_type='municipality' AND a.area_code=d.municipality_code) OR
 (a.area_type='regso' AND EXISTS(SELECT 1 FROM context.area_relation r WHERE (r.dataset_id,r.child_type,r.child_code)=(a.dataset_id,a.area_type,a.area_code) AND r.parent_type='municipality' AND r.parent_code=d.municipality_code)) OR
 (a.area_type='population_grid_1km' AND EXISTS(SELECT 1 FROM context.area m WHERE m.dataset_id=a.dataset_id AND m.area_type='municipality' AND m.area_code=d.municipality_code AND ST_Intersects(a.geom,m.geom)));

CREATE VIEW mart.context_observations AS
SELECT a.dataset_id,o.observation_id,o.area_type,o.area_code,o.indicator_id,o.reference_period,o.dimensions,o.raw_value,o.normalized_value,o.normalized_unit,o.value_status,o.provenance FROM mart.context_areas a JOIN catalog.dataset d ON d.dataset_id=a.dataset_id JOIN context.observation o ON (o.dataset_id,o.area_type,o.area_code)=(d.source_dataset_id,a.area_type,a.area_code);

CREATE VIEW mart.building_areas AS
SELECT dataset_id,building_id,area_type,area_code,footprint_fraction,rank,is_primary,assignment_status,reasons,run_id FROM context.building_area;

CREATE VIEW mart.building_context AS
SELECT r.dataset_id,r.building_id,r.area_type,r.area_code,r.is_primary,r.assignment_status,o.observation_id,o.indicator_id,o.reference_period,o.dimensions,o.raw_value,o.normalized_value,o.source_unit,o.normalized_unit,o.transformation,o.value_status,o.provenance,o.run_id FROM context.building_area r JOIN context.observation o ON (o.dataset_id,o.area_type,o.area_code)=(r.source_dataset_id,r.area_type,r.area_code);

CREATE VIEW mart.building_context_summary AS
SELECT b.dataset_id,b.building_id,
 (SELECT area_code FROM context.building_area r WHERE (r.dataset_id,r.building_id)=(b.dataset_id,b.building_id) AND area_type='municipality' AND is_primary) AS municipality,
 (SELECT area_code FROM context.building_area r WHERE (r.dataset_id,r.building_id)=(b.dataset_id,b.building_id) AND area_type='regso' AND is_primary) AS regso,
 (SELECT area_code FROM context.building_area r WHERE (r.dataset_id,r.building_id)=(b.dataset_id,b.building_id) AND area_type='population_grid_1km' AND is_primary) AS population_cell,
 (SELECT jsonb_agg(jsonb_build_object('indicator',c.indicator_id,'period',c.reference_period,'dimensions',c.dimensions,'value',c.normalized_value,'unit',c.normalized_unit)) FROM mart.building_context c WHERE (c.dataset_id,c.building_id)=(b.dataset_id,b.building_id) AND c.is_primary) AS primary_observations
 FROM scope.dataset_building b;

CREATE VIEW mart.scope_membership AS
SELECT dataset_id,scope_id,building_id,inclusion_state,reasons FROM scope.membership;

CREATE VIEW mart.scope_completeness AS
SELECT b.dataset_id,b.building_id,l.layer,l.loading_status,CASE WHEN l.loading_status='loaded' THEN COALESCE(c.status,'missing') ELSE NULL END AS evidence_status,COALESCE(c.available_count,0) AS available_count,COALESCE(c.required_count,0) AS required_count,COALESCE(c.reasons,'{}') AS reasons FROM scope.dataset_building b JOIN catalog.dataset_layer l USING(dataset_id) LEFT JOIN scope.coverage c ON (c.dataset_id,c.building_id,c.layer)=(b.dataset_id,b.building_id,l.layer);

CREATE VIEW mart.process_runs AS
SELECT r.run_id,d.dataset_id,r.layer,r.policy_version,r.code_version,r.status,r.input_versions,r.started_at,r.finished_at,r.error FROM catalog.dataset d JOIN catalog.process_run r ON (r.dataset_id=d.source_dataset_id AND (r.processing_release_id IS NULL OR r.processing_release_id=d.processing_release_id)) OR r.dataset_id=d.dataset_id WHERE d.dataset_kind<>'national_source';

CREATE VIEW mart.source_inventory AS
WITH layers AS (SELECT d.dataset_id,l.layer,l.loading_status,l.source_version_id,l.run_id FROM catalog.dataset d JOIN catalog.dataset_layer l ON l.dataset_id=d.source_dataset_id WHERE d.dataset_kind<>'national_source' UNION ALL SELECT DISTINCT d.dataset_id,'epc_site_events'::text,'loaded'::text,e.source_version_id,e.run_id FROM catalog.dataset d JOIN catalog.processing_release p USING(processing_release_id) JOIN evidence.epc_site_event e ON e.dataset_id=d.source_dataset_id AND e.source_version_id=ANY(p.event_source_versions))
 SELECT l.dataset_id,l.layer,l.loading_status,l.source_version_id,v.publisher,v.contract,v.publication_status,v.metadata,f.logical_name,f.sha256,a.byte_size,x.profile,x.path,x.mtime_ns,l.run_id FROM layers l LEFT JOIN catalog.source_version v USING(source_version_id) LEFT JOIN catalog.source_file f ON f.source_version_id=l.source_version_id AND (f.logical_name NOT LIKE 'measurement/%' OR EXISTS(SELECT 1 FROM mart.component_heights h WHERE h.dataset_id=l.dataset_id AND h.evidence_sha256=f.sha256)) LEFT JOIN catalog.asset a ON a.sha256=f.sha256 LEFT JOIN catalog.asset_location x ON x.sha256=f.sha256;

CREATE VIEW mart.quality_findings AS
SELECT q.finding_id,d.dataset_id,q.building_id,q.layer,q.code,q.details,q.run_id FROM catalog.dataset d JOIN quality.finding q ON q.dataset_id=d.source_dataset_id AND (q.processing_release_id IS NULL OR q.processing_release_id=d.processing_release_id) WHERE d.dataset_kind<>'national_source' AND (EXISTS(SELECT 1 FROM scope.dataset_building m WHERE m.dataset_id=d.dataset_id AND m.building_id=q.building_id) OR q.source_record_id IN (SELECT record_id FROM mart.epc_raw_inventory i WHERE i.dataset_id=d.dataset_id) OR (q.layer='addresses' AND EXISTS(SELECT 1 FROM mart.building_addresses a WHERE a.dataset_id=d.dataset_id AND a.address_place_id=q.details->>'address_place_id')));

DO $$ DECLARE n text; cols text; BEGIN
 FOR n IN SELECT table_name FROM information_schema.views WHERE table_schema='mart' AND table_name<>'datasets' LOOP
 SELECT string_agg('v.'||quote_ident(column_name),',' ORDER BY ordinal_position) INTO cols FROM information_schema.columns WHERE table_schema='mart' AND table_name=n;
 EXECUTE format('CREATE VIEW mart.%I AS SELECT %s FROM mart.%I v JOIN catalog.active_dataset a USING(dataset_id)',n||'_current',cols,n);
 END LOOP;
END $$;

-- PostgreSQL now() is the transaction-start time. Capture actual completion time.
CREATE FUNCTION catalog.record_run_completion() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.status IN ('complete','failed') AND OLD.status='running' THEN
   NEW.finished_at := clock_timestamp();
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER actual_run_completion BEFORE UPDATE OF status ON catalog.process_run
 FOR EACH ROW EXECUTE FUNCTION catalog.record_run_completion();

-- Site-event source inventory is projected with selections in 002_views.sql.

-- Source diagnostics are shared per processing release, with municipal scope
-- determined from building/address membership and the selected footprint bounds.
CREATE OR REPLACE VIEW mart.quality_findings AS
WITH bounds AS (
 SELECT m.dataset_id,ST_SetSRID(ST_Extent(f.display_geom)::geometry,3006) geom
 FROM scope.dataset_building m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id
 JOIN stock.building_footprint f ON (f.dataset_id,f.processing_release_id,f.building_id)=
   (m.source_dataset_id,d.processing_release_id,m.building_id) GROUP BY m.dataset_id
)
SELECT q.finding_id,d.dataset_id,q.building_id,q.layer,q.code,
 CASE WHEN q.code='lm_municipality_conflict' AND d.municipality_code IS NOT NULL
   THEN q.details||jsonb_build_object('selected_municipality',d.municipality_code)
   ELSE q.details END AS details,q.run_id
FROM catalog.dataset d JOIN quality.finding q ON q.dataset_id=d.source_dataset_id
 AND (q.processing_release_id IS NULL OR q.processing_release_id=d.processing_release_id)
WHERE d.dataset_kind<>'national_source' AND (
 EXISTS(SELECT 1 FROM scope.dataset_building m WHERE m.dataset_id=d.dataset_id AND m.building_id=q.building_id)
 OR q.source_record_id IN (SELECT record_id FROM mart.epc_raw_inventory i WHERE i.dataset_id=d.dataset_id)
 OR (q.layer='addresses' AND EXISTS(SELECT 1 FROM mart.building_addresses a WHERE a.dataset_id=d.dataset_id AND a.address_place_id=q.details->>'address_place_id'))
 OR (q.layer='properties' AND EXISTS(SELECT 1 FROM raw_lm.record r JOIN bounds b ON b.dataset_id=d.dataset_id
   WHERE r.dataset_id=q.dataset_id AND r.record_id=q.source_record_id
   AND ST_Intersects(ST_GeomFromWKB(r.original_geometry,3006),b.geom)))
);

-- Official Boverket declaration form v2.8, page 3: Atemp excluding
-- Avarmgarage, m2. EgenByggnadsTyp is a distinct field; the mapping
-- workbook's EgenAtemp label "Byggnadstyp" is erroneous.
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM evidence.epc_observation)
 OR EXISTS(SELECT 1 FROM catalog.work_unit WHERE stage='import:epc') THEN
  RAISE EXCEPTION 'Verified Atemp mapping requires a fresh EPC import';
 END IF;
END $$;
UPDATE catalog.field_mapping SET review_status='verified',
 authority='Boverket Formulär version 2.8, page 3: EgenAtemp, Atemp (exklusive Avarmgarage), m2; separate EgenByggnadsTyp'
 WHERE regime='efter_v260' AND source_field='EgenAtemp';

CREATE VIEW mart.source_record_locators AS SELECT l.record_key record_id,l.dataset_id source_dataset_id,
 l.kind,l.source_record,l.source_member,f.logical_name,f.source_version_id,f.bundle_path,f.format,f.metadata,
 a.sha256 asset_sha256 FROM catalog.record_locator l JOIN catalog.source_file_data f USING(occurrence_key)
 JOIN catalog.asset a USING(asset_key);

CREATE INDEX component_building_lookup ON stock.component_data(dataset_id,building_id);
CREATE INDEX height_asset_locator ON evidence.height_catalogue(measurement_key,baseline_row_offset);
CREATE INDEX source_file_asset_lookup ON catalog.source_file_data(asset_key,occurrence_key);
CREATE INDEX measurement_occurrence_lookup ON evidence.measurement_source(occurrence_key,measurement_key);
CREATE INDEX record_kind_lookup ON catalog.record_locator(dataset_id,kind);
CREATE INDEX raw_epc_before_certificate ON raw_epc.before_v260_data(certificate_id,subject_id);
CREATE INDEX raw_epc_after_certificate ON raw_epc.after_v260_data(certificate_id,subject_id);
CREATE INDEX raw_epc_before_subject ON raw_epc.before_v260_data(subject_id,record_key);
CREATE INDEX raw_epc_after_subject ON raw_epc.after_v260_data(subject_id,record_key);

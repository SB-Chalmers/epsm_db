-- Clean v0.0.3 baseline. No in-place migration from the previous prototype.
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
 schema_version text NOT NULL DEFAULT '0.0.3', created_at timestamptz NOT NULL DEFAULT now(),
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
CREATE TABLE catalog.asset (sha256 text PRIMARY KEY CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 byte_size bigint NOT NULL CHECK(byte_size>=0));
CREATE TABLE catalog.asset_location (sha256 text REFERENCES catalog.asset, profile text NOT NULL,
 path text NOT NULL, mtime_ns bigint NOT NULL, PRIMARY KEY(sha256,profile,path));
CREATE TABLE catalog.source_version (source_version_id text PRIMARY KEY, layer text NOT NULL,
 publisher text NOT NULL, contract text NOT NULL, publication_status text NOT NULL,
 metadata jsonb NOT NULL DEFAULT '{}');
CREATE TABLE catalog.source_file (source_version_id text REFERENCES catalog.source_version,
 logical_name text NOT NULL, sha256 text NOT NULL REFERENCES catalog.asset,
 PRIMARY KEY(source_version_id,logical_name));
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

-- Each projection retains untouched geometry bytes and original textual values.
CREATE TABLE raw_lm.record (dataset_id text REFERENCES catalog.dataset, record_id text,
 kind text NOT NULL, source_version_id text NOT NULL REFERENCES catalog.source_version,
 source_sha256 text NOT NULL REFERENCES catalog.asset, source_file text NOT NULL,
 source_member text, source_record text NOT NULL, original_values jsonb NOT NULL,
 original_geometry bytea, PRIMARY KEY(dataset_id,record_id));
CREATE TABLE raw_height.record (LIKE raw_lm.record INCLUDING ALL);
CREATE TABLE raw_scb.record (LIKE raw_lm.record INCLUDING ALL);
CREATE TABLE raw_epc.before_v260_row (LIKE raw_lm.record INCLUDING ALL);
CREATE TABLE raw_epc.after_v260_row (LIKE raw_lm.record INCLUDING ALL);

ALTER TABLE raw_epc.before_v260_row ADD certificate_id text, ADD subject_id text;
ALTER TABLE raw_epc.after_v260_row ADD certificate_id text, ADD subject_id text;
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
CREATE INDEX building_municipality ON stock.building(dataset_id,municipality_code);
CREATE TABLE stock.geometry_component (dataset_id text, component_id text, building_id uuid NOT NULL,
 geometry_hash text NOT NULL CHECK(geometry_hash ~ '^[0-9a-f]{64}$'),
 geom geometry(Geometry,3006) NOT NULL, outline_labels text[] NOT NULL,
 representation_status text NOT NULL, owner_tile_id text, source_fids bigint[] NOT NULL,
 revision_timestamp text, object_version integer, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,component_id), UNIQUE(dataset_id,component_id,geometry_hash),
 FOREIGN KEY(dataset_id,building_id) REFERENCES stock.building,
 CHECK(component_id = building_id::text || ':' || geometry_hash), CHECK(NOT ST_IsEmpty(geom)));
CREATE INDEX component_geom ON stock.geometry_component USING gist(geom);
CREATE TABLE stock.component_source_feature (dataset_id text, component_id text, source_fid bigint,
 source_version_id text REFERENCES catalog.source_version, disposition text NOT NULL, original_values jsonb NOT NULL,
 PRIMARY KEY(dataset_id,component_id,source_fid),
 FOREIGN KEY(dataset_id,component_id) REFERENCES stock.geometry_component);
CREATE TABLE stock.component_relation (dataset_id text, processing_release_id uuid NOT NULL, left_component_id text, right_component_id text,
 relation_kind text NOT NULL, decision text NOT NULL, details jsonb NOT NULL,
 run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,processing_release_id,left_component_id,right_component_id),
 FOREIGN KEY(processing_release_id,dataset_id) REFERENCES catalog.processing_release(processing_release_id,source_dataset_id),
 FOREIGN KEY(dataset_id,left_component_id) REFERENCES stock.geometry_component,
 FOREIGN KEY(dataset_id,right_component_id) REFERENCES stock.geometry_component);
CREATE TABLE stock.building_footprint (dataset_id text, processing_release_id uuid NOT NULL, building_id uuid,
 display_geom geometry(Geometry,3006) NOT NULL, linkage_geom geometry(Geometry,3006),
 resolution_status text NOT NULL, quality_flags text[] NOT NULL DEFAULT '{}',
 run_id uuid NOT NULL REFERENCES catalog.process_run, PRIMARY KEY(dataset_id,processing_release_id,building_id),
 FOREIGN KEY(processing_release_id,dataset_id) REFERENCES catalog.processing_release(processing_release_id,source_dataset_id),
 FOREIGN KEY(dataset_id,building_id) REFERENCES stock.building,
 CHECK(resolution_status='resolved' OR linkage_geom IS NULL));
CREATE INDEX footprint_linkage ON stock.building_footprint USING gist(linkage_geom);
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
CREATE INDEX property_geom ON stock.property_area_part USING gist(geom);
CREATE TABLE stock.property_designation (dataset_id text, property_unit_id text,
 municipality_code text, raw_label text, normalized_label text NOT NULL,
 PRIMARY KEY(dataset_id,property_unit_id,municipality_code,normalized_label),
 FOREIGN KEY(dataset_id,property_unit_id) REFERENCES stock.property_unit);

CREATE TABLE evidence.component_height (dataset_id text, component_id text, input_geometry_hash text NOT NULL,
 height_status text NOT NULL, representative_height_m double precision, eave_height_m double precision,
 ridge_height_m double precision, roof_form text, details jsonb NOT NULL,
 evidence_sha256 text REFERENCES catalog.asset, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,component_id),
 FOREIGN KEY(dataset_id,component_id,input_geometry_hash)
 REFERENCES stock.geometry_component(dataset_id,component_id,geometry_hash));
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
CREATE TABLE evidence.epc_subject (dataset_id text, subject_id text, certificate_id text NOT NULL,
 municipality_code text NOT NULL, designation_normalized text NOT NULL, house_number integer,
 original_values jsonb NOT NULL, run_id uuid NOT NULL REFERENCES catalog.process_run,
 PRIMARY KEY(dataset_id,subject_id),
 FOREIGN KEY(dataset_id,certificate_id) REFERENCES evidence.epc_certificate);
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
 FOREIGN KEY(dataset_id,subject_id) REFERENCES evidence.epc_subject,
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
 FOREIGN KEY(dataset_id,subject_id) REFERENCES evidence.epc_subject);
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
CREATE INDEX context_geom ON context.area USING gist(geom);
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
CREATE FUNCTION catalog.check_raw_source() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM catalog.source_file f WHERE f.source_version_id=NEW.source_version_id AND f.sha256=NEW.source_sha256) THEN
   RAISE EXCEPTION 'Raw record source is not registered in its source version';
 END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['raw_lm.record','raw_height.record','raw_scb.record','raw_epc.before_v260_row','raw_epc.after_v260_row'] LOOP
 EXECUTE format('ALTER TABLE %s ADD FOREIGN KEY(dataset_id) REFERENCES catalog.dataset',t);
 EXECUTE format('CREATE TRIGGER immutable_source BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION catalog.keep_source_record()',t);
 EXECUTE format('CREATE TRIGGER registered_source BEFORE INSERT ON %s FOR EACH ROW EXECUTE FUNCTION catalog.check_raw_source()',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['catalog.asset','catalog.source_version','catalog.source_file','evidence.epc_candidate','evidence.epc_subject_result','evidence.epc_site_event'] LOOP
 EXECUTE format('CREATE TRIGGER immutable_source BEFORE UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION catalog.keep_source_record()',t);
 END LOOP;
END $$;

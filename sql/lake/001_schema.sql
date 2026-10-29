-- GIS-first data lake v1. A fresh database only; no legacy baseline migrations.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA lake;
CREATE SCHEMA gis;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA lake,gis FROM PUBLIC;
CREATE TABLE lake.schema_version(version integer PRIMARY KEY CHECK(version=1));
INSERT INTO lake.schema_version VALUES(1);
CREATE TABLE lake.source (
 source_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 name text NOT NULL UNIQUE, kind text NOT NULL, version text NOT NULL,
 options jsonb NOT NULL DEFAULT '{}', imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE lake.import_file (
 file_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source_id bigint NOT NULL REFERENCES lake.source, path text NOT NULL,
 byte_size bigint NOT NULL, mtime_ns bigint NOT NULL, checksum text,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','loading','complete','failed')),
 committed_rows bigint NOT NULL DEFAULT 0, error text,
 updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(source_id,path)
);
CREATE TABLE lake.publication (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 revision bigint NOT NULL DEFAULT 0, linked_revision bigint, published_revision bigint,
 linked_at timestamptz, published_at timestamptz,
 property_links_dirty boolean NOT NULL DEFAULT true, epc_links_dirty boolean NOT NULL DEFAULT true,
 minimum_overlap double precision NOT NULL DEFAULT 0.5
);
INSERT INTO lake.publication(singleton) VALUES(true);
CREATE TABLE lake.building (
 building_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 external_id text NOT NULL UNIQUE, municipality_code text, municipality_conflict boolean NOT NULL DEFAULT false, house_number integer,
 construction_year integer, object_status text, object_version integer, valid_from timestamptz, purposes jsonb NOT NULL DEFAULT '[]',
 attributes jsonb NOT NULL DEFAULT '{}', registry_file_id bigint REFERENCES lake.import_file, registry_source_record text,
 geom geometry(MultiPolygon,3006), geometry_valid boolean NOT NULL DEFAULT false,
 geometry_dirty boolean NOT NULL DEFAULT true
);
CREATE INDEX building_geom ON lake.building USING gist(geom);
CREATE INDEX building_municipality ON lake.building(municipality_code);
CREATE TABLE lake.component (
 component_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 external_id text NOT NULL UNIQUE, building_id bigint NOT NULL REFERENCES lake.building,
 geom geometry(MultiPolygon,3006) NOT NULL, geometry_valid boolean NOT NULL,
 file_id bigint NOT NULL REFERENCES lake.import_file, source_record text NOT NULL,
 attributes jsonb NOT NULL DEFAULT '{}', UNIQUE(file_id,source_record)
);
CREATE INDEX component_geom ON lake.component USING gist(geom);
CREATE INDEX component_building ON lake.component(building_id);
CREATE TABLE lake.property (
 property_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 external_id text NOT NULL UNIQUE, municipality_code text, designation text,
 designation_key text, geom geometry(MultiPolygon,3006),
 geometry_valid boolean NOT NULL DEFAULT false, geometry_dirty boolean NOT NULL DEFAULT true
);
CREATE INDEX property_geom ON lake.property USING gist(geom);
CREATE INDEX property_designation ON lake.property(municipality_code,designation_key);
CREATE TABLE lake.property_part (
 part_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 external_id text NOT NULL UNIQUE, property_id bigint REFERENCES lake.property,
 municipality_code text,designation text,designation_key text,
 geom geometry(MultiPolygon,3006) NOT NULL, file_id bigint NOT NULL REFERENCES lake.import_file,
 source_record text NOT NULL, attributes jsonb NOT NULL DEFAULT '{}', UNIQUE(file_id,source_record)
);
CREATE INDEX property_part_geom ON lake.property_part USING gist(geom);
CREATE INDEX property_part_property ON lake.property_part(property_id);
CREATE INDEX property_part_municipality ON lake.property_part(municipality_code);
CREATE INDEX property_part_designation ON lake.property_part(municipality_code,designation_key,property_id);
CREATE TABLE lake.address (
 address_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 external_id text NOT NULL UNIQUE, municipality_code text, street_name text,
 address_number text, locality text, geom geometry(Point,3006),
 object_version integer, valid_from timestamptz,
 file_id bigint NOT NULL REFERENCES lake.import_file, source_record text NOT NULL,
 attributes jsonb NOT NULL DEFAULT '{}', UNIQUE(file_id,source_record)
);
CREATE INDEX address_geom ON lake.address USING gist(geom);
CREATE TABLE lake.building_address (
 link_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 building_id bigint NOT NULL REFERENCES lake.building,
 address_external_id text NOT NULL, address_id bigint REFERENCES lake.address,
 method text NOT NULL DEFAULT 'source_reference',
 UNIQUE(building_id,address_external_id)
);
CREATE INDEX building_address_address ON lake.building_address(address_id);
CREATE TABLE lake.epc (
 epc_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 external_id text NOT NULL UNIQUE, regime text, certificate_date date,
 energy_class text, atemp_m2 double precision,
 attributes jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE lake.epc_record (
 record_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 epc_id bigint NOT NULL REFERENCES lake.epc, file_id bigint NOT NULL REFERENCES lake.import_file,
 source_record text NOT NULL, attributes jsonb NOT NULL, UNIQUE(file_id,source_record)
);
CREATE INDEX epc_record_certificate ON lake.epc_record(epc_id);
CREATE TABLE lake.epc_subject (
 subject_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 epc_id bigint NOT NULL REFERENCES lake.epc, municipality_code text,
 designation_key text, house_number integer, building_external_id text,
 UNIQUE NULLS NOT DISTINCT(epc_id,municipality_code,designation_key,house_number,building_external_id)
);
CREATE INDEX epc_subject_property ON lake.epc_subject(municipality_code,designation_key,house_number);
CREATE INDEX epc_subject_building ON lake.epc_subject(building_external_id);
CREATE TABLE lake.height (
 height_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 component_external_id text NOT NULL, component_id bigint REFERENCES lake.component,
 height_status text NOT NULL, representative_height_m double precision,
 eave_height_m double precision, ridge_height_m double precision, roof_form text,
 measured_at timestamptz, evidence_path text,
 file_id bigint NOT NULL REFERENCES lake.import_file, source_record text NOT NULL,
 attributes jsonb NOT NULL DEFAULT '{}', UNIQUE(file_id,source_record)
);
CREATE INDEX height_component ON lake.height(component_id,measured_at DESC NULLS LAST,height_id DESC);
CREATE INDEX height_source_component ON lake.height(component_external_id);
CREATE TABLE lake.building_property (
 link_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 building_id bigint NOT NULL REFERENCES lake.building,
 property_id bigint NOT NULL REFERENCES lake.property,
 overlap_fraction double precision NOT NULL,
 status text NOT NULL CHECK(status IN ('matched','candidate','ambiguous')),
 method text NOT NULL DEFAULT 'polygon_overlap', UNIQUE(building_id,property_id)
);
CREATE INDEX building_property_property ON lake.building_property(property_id);
CREATE TABLE lake.building_epc (
 link_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 building_id bigint NOT NULL REFERENCES lake.building,
 epc_id bigint NOT NULL REFERENCES lake.epc, subject_id bigint NOT NULL REFERENCES lake.epc_subject,
 status text NOT NULL CHECK(status IN ('matched','ambiguous')),
 method text NOT NULL, UNIQUE(building_id,subject_id)
);
CREATE INDEX building_epc_epc ON lake.building_epc(epc_id);
-- These views are an API over evidence, with no geometry hashing or policy-run joins.
CREATE VIEW lake.latest_height AS
 SELECT DISTINCT ON (component_id) * FROM lake.height WHERE component_id IS NOT NULL
 ORDER BY component_id,measured_at DESC NULLS LAST,height_id DESC;

-- Administrative/statistical geography is another domain in the same lake.
-- Boundary edition and observation period are deliberately separate identifiers.
CREATE TABLE lake.area (
 area_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 area_type text NOT NULL,external_id text NOT NULL,boundary_version text NOT NULL,
 name text,municipality_code text,
 parent_type text,parent_external_id text,parent_boundary_version text,
 parent_area_id bigint REFERENCES lake.area,
 geom geometry(MultiPolygon,3006) NOT NULL,geometry_valid boolean NOT NULL,
 file_id bigint NOT NULL REFERENCES lake.import_file,source_record text NOT NULL,
 attributes jsonb NOT NULL DEFAULT '{}',
 UNIQUE(area_type,external_id,boundary_version),UNIQUE(file_id,source_record)
);
CREATE INDEX area_geom ON lake.area USING gist(geom);
CREATE INDEX area_type_version ON lake.area(area_type,boundary_version);
CREATE INDEX area_parent ON lake.area(parent_area_id);
CREATE TABLE lake.statistic (
 observation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 area_type text NOT NULL,area_external_id text NOT NULL,boundary_version text NOT NULL,
 area_id bigint REFERENCES lake.area,
 indicator text NOT NULL,period text NOT NULL,unit text NOT NULL,
 value double precision,raw_value text,value_status text NOT NULL,
 dimensions jsonb NOT NULL DEFAULT '{}',source_field text NOT NULL,
 file_id bigint NOT NULL REFERENCES lake.import_file,source_record text NOT NULL,
 attributes jsonb NOT NULL DEFAULT '{}',
 UNIQUE(file_id,source_record,source_field,indicator,period,dimensions)
);
CREATE INDEX statistic_area_indicator ON lake.statistic(area_id,indicator,period);
CREATE INDEX statistic_source_area ON lake.statistic(area_type,area_external_id,boundary_version) WHERE area_id IS NULL;
CREATE INDEX statistic_indicator_period ON lake.statistic(indicator,period);
CREATE TABLE lake.building_area (
 link_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 building_id bigint NOT NULL REFERENCES lake.building,area_id bigint NOT NULL REFERENCES lake.area,
 status text NOT NULL CHECK(status IN ('matched','ambiguous')),
 method text NOT NULL DEFAULT 'representative_point',UNIQUE(building_id,area_id)
);
CREATE INDEX building_area_area ON lake.building_area(area_id);
ALTER TABLE lake.building ADD COLUMN label_point geometry(Point,3006);
CREATE INDEX building_label_point ON lake.building USING gist(label_point);
ALTER TABLE lake.publication ADD COLUMN area_links_dirty boolean NOT NULL DEFAULT true;
-- NVDB has themes and linear-referenced events; RLID alone is not a feature key.
CREATE TABLE lake.road (
 road_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 external_id text NOT NULL UNIQUE,source_layer text NOT NULL,source_member text,
 network_link_id text,start_measure double precision,end_measure double precision,
 geom geometry(Geometry,3006),geometry_valid boolean,
 file_id bigint NOT NULL REFERENCES lake.import_file,source_record text NOT NULL,
 attributes jsonb NOT NULL DEFAULT '{}',UNIQUE(file_id,source_record)
);
CREATE INDEX road_geom ON lake.road USING gist(geom);
CREATE INDEX road_network_link ON lake.road(network_link_id);
CREATE INDEX road_layer ON lake.road(source_layer);

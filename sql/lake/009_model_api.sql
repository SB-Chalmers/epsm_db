-- Additive model API over canonical evidence. No source rows or geometry hashes.
ALTER TABLE lake.schema_version DROP CONSTRAINT IF EXISTS schema_version_version_check;
ALTER TABLE lake.schema_version ADD CONSTRAINT schema_version_version_check CHECK(version IN (1,2));
ALTER TABLE lake.publication ADD COLUMN model_revision bigint;

-- A small reviewed mapping registry, copied from baseline 0.0.4 and its Atemp correction.
CREATE TABLE lake.model_field_mapping (
 regime text NOT NULL, source_field text NOT NULL, owner text NOT NULL,
 value_type text NOT NULL, source_unit text, normalized_unit text,
 multiplier double precision NOT NULL DEFAULT 1,
 review_status text NOT NULL, authority text, PRIMARY KEY(regime,source_field)
);
INSERT INTO lake.model_field_mapping(regime,source_field,owner,value_type,source_unit,normalized_unit,review_status,authority) VALUES
 ('fore_v260','ExpertGodkannande','certificate','date',NULL,NULL,'verified','Boverket Mappningsfil före v2.6: DEKLARATION, Datum för godkännande'),
 ('efter_v260','ExpertGodkannande','certificate','date',NULL,NULL,'verified','Boverket Mappningsfil efter v2.6: DEKLARATION, Datum för godkännande'),
 ('fore_v260','EgenAtemp','certificate','number','m2','m2','verified','Boverket Mappningsfil före v2.6: DEKLARATION, Mätt värde Atemp, m2'),
 ('fore_v260','EgenNybyggAr','certificate','integer','year','year','verified','Boverket Mappningsfil före v2.6: DEKLARATION, Nybyggnadsår'),
 ('efter_v260','EgenNybyggAr','certificate','integer','year','year','verified','Boverket Mappningsfil efter v2.6: DEKLARATION, Nybyggnadsår'),
 ('efter_v260','EgenAtemp','certificate','number','m2','m2','verified','Boverket Formulär version 2.8, page 3: Atemp excluding Avarmgarage; correction in baseline 0.0.4');

CREATE FUNCTION lake.model_date(value text) RETURNS date LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
 IF value !~ '^\d{4}-\d{2}-\d{2}([T ].*)?$' THEN RETURN NULL; END IF;
 PERFORM value::timestamp;
 RETURN left(value,10)::date;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN NULL;
END $$;
CREATE FUNCTION lake.model_number(value text) RETURNS double precision LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE parsed double precision;
BEGIN
 parsed := replace(replace(value,' ',''),',','.')::double precision;
 IF parsed IN ('NaN'::double precision,'Infinity'::double precision,'-Infinity'::double precision) THEN RETURN NULL; END IF;
 RETURN parsed;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN NULL;
END $$;

-- Cache only the fields needed for national classification and certificate selection.
CREATE MATERIALIZED VIEW gis.model_epc_fields AS
 SELECT r.epc_id,v.source_field,array_agg(DISTINCT r.attributes->>v.source_field ORDER BY r.attributes->>v.source_field) AS raw_values
 FROM lake.epc_record r CROSS JOIN (VALUES ('ExpertGodkannande'),('EgenAtempSkolor'),('EgenAtempKontor'),
   ('VentTypSjalvdrag'),('VentTypF'),('VentTypFmed'),('VentTypFT'),('VentTypFTX')) v(source_field)
 WHERE nullif(btrim(r.attributes->>v.source_field),'') IS NOT NULL
 GROUP BY r.epc_id,v.source_field WITH NO DATA;
CREATE UNIQUE INDEX model_epc_fields_key ON gis.model_epc_fields(epc_id,source_field);

CREATE MATERIALIZED VIEW gis.model_certificates AS
 WITH subjects AS (
  SELECT epc_id,count(*) AS subject_count FROM lake.epc_subject GROUP BY epc_id
 ), matches AS (
  SELECT epc_id,count(DISTINCT building_id) AS loaded_building_count,
   count(DISTINCT subject_id) FILTER(WHERE status='matched') AS matched_subject_count,
   bool_or(status<>'matched') AS ambiguous FROM lake.building_epc GROUP BY epc_id
 )
 SELECT e.epc_id AS fid,e.epc_id,e.external_id AS certificate_id,e.regime,
   CASE WHEN cardinality(d.raw_values)=1 THEN lake.model_date(d.raw_values[1]) END AS certificate_date,
   CASE WHEN cardinality(d.raw_values)=1 AND lake.model_date(d.raw_values[1]) IS NOT NULL
             AND m.review_status='verified' THEN 'verified' ELSE 'unverified' END AS date_status,
   COALESCE(s.subject_count,0) AS subject_count,COALESCE(l.loaded_building_count,0) AS loaded_building_count,
   CASE WHEN s.subject_count>0 AND l.matched_subject_count=s.subject_count AND NOT l.ambiguous
        THEN 'fully_matched' ELSE 'unresolved' END AS matching_status,
   e.energy_class,e.atemp_m2,e.attributes
 FROM lake.epc e LEFT JOIN subjects s USING(epc_id) LEFT JOIN matches l USING(epc_id)
 LEFT JOIN gis.model_epc_fields d ON d.epc_id=e.epc_id AND d.source_field='ExpertGodkannande'
 LEFT JOIN lake.model_field_mapping m ON m.regime=e.regime AND m.source_field='ExpertGodkannande'
 WITH NO DATA;
CREATE UNIQUE INDEX model_certificates_fid ON gis.model_certificates(fid);

CREATE MATERIALIZED VIEW gis.model_facts AS
 WITH candidates AS (
  SELECT DISTINCT l.building_id,c.epc_id,c.certificate_date,c.date_status
  FROM lake.building_epc l JOIN gis.model_certificates c USING(epc_id)
 ), dates AS (
  SELECT building_id,count(*) AS certificate_count,
   bool_and(certificate_date IS NOT NULL AND date_status='verified') AS dates_verified,
   max(certificate_date) AS latest FROM candidates GROUP BY building_id
 ), chosen AS (
  SELECT d.building_id,d.certificate_count,
   CASE WHEN d.dates_verified AND count(*) FILTER(WHERE c.certificate_date=d.latest)=1
    THEN max(c.epc_id) FILTER(WHERE c.certificate_date=d.latest) END AS selected_epc_id
  FROM dates d JOIN candidates c USING(building_id)
  GROUP BY d.building_id,d.certificate_count,d.dates_verified,d.latest
 ), ventilation AS (
  SELECT epc_id,
   array_agg(DISTINCT CASE source_field WHEN 'VentTypSjalvdrag' THEN 'S'
    WHEN 'VentTypF' THEN 'F' WHEN 'VentTypFmed' THEN 'F'
    WHEN 'VentTypFT' THEN 'FT' WHEN 'VentTypFTX' THEN 'FTX' END)
    FILTER(WHERE cardinality(raw_values)=1 AND lower(btrim(raw_values[1])) IN ('ja','true','1')) AS positive,
   bool_or(cardinality(raw_values)<>1 OR lower(btrim(raw_values[1])) NOT IN ('ja','true','1','nej','false','0')) AS invalid
  FROM gis.model_epc_fields WHERE source_field LIKE 'VentTyp%' GROUP BY epc_id
 ), programmes AS (
  SELECT epc_id,
   bool_or(source_field='EgenAtempSkolor' AND cardinality(raw_values)=1 AND lake.model_number(raw_values[1]) BETWEEN 50 AND 100) AS school,
   bool_or(source_field='EgenAtempKontor' AND cardinality(raw_values)=1 AND lake.model_number(raw_values[1]) BETWEEN 50 AND 100) AS office
  FROM gis.model_epc_fields WHERE source_field IN ('EgenAtempSkolor','EgenAtempKontor') GROUP BY epc_id
 ), facts AS (
 SELECT b.building_id AS fid,b.external_id AS building_id,b.municipality_code,b.construction_year,
  b.object_status,b.geometry_valid,
  COALESCE(ch.certificate_count,0) AS certificate_count,ch.selected_epc_id,
  COALESCE(c.subject_count=1 AND c.loaded_building_count=1 AND c.matching_status='fully_matched',false) AS single_building_epc,
  CASE WHEN ch.certificate_count IS NULL THEN 'No linked EPC'
       WHEN c.subject_count=1 AND c.loaded_building_count=1 AND c.matching_status='fully_matched'
        AND cardinality(v.positive)=1 AND NOT v.invalid THEN v.positive[1]
       ELSE 'Unresolved EPC ventilation' END AS ventilation_system,
  CASE
   WHEN p.complementary THEN 'complementary'
   WHEN p.multismall THEN 'unknown'
   WHEN p.multi_family THEN 'multi_family'
   WHEN p.single_family THEN 'single_family'
   WHEN p.school OR (c.subject_count=1 AND c.loaded_building_count=1 AND c.matching_status='fully_matched' AND pr.school) THEN 'school'
   WHEN p.office OR (p.business AND c.subject_count=1 AND c.loaded_building_count=1 AND c.matching_status='fully_matched' AND pr.office) THEN 'office'
   WHEN p.industrial THEN 'industrial'
   WHEN p.other THEN 'other' ELSE 'unknown' END AS building_type
 FROM lake.building b LEFT JOIN chosen ch ON ch.building_id=b.building_id
 LEFT JOIN gis.model_certificates c ON c.epc_id=ch.selected_epc_id
 LEFT JOIN ventilation v ON v.epc_id=c.epc_id LEFT JOIN programmes pr ON pr.epc_id=c.epc_id
 LEFT JOIN LATERAL (
  SELECT bool_or(x->>'category'='Komplementbyggnad') AS complementary,
   bool_or(x->>'detail' ILIKE '%Småhus med flera lägenheter%') AS multismall,
   bool_or(x->>'detail' ILIKE '%Flerfamiljshus%' OR x->>'detail' ILIKE '%Flerfamiljhus%') AS multi_family,
   bool_or(x->>'detail' ILIKE '%Småhus%') AS single_family,
   bool_or(x->>'detail' ILIKE '%Skola%') AS school,bool_or(x->>'detail' ILIKE '%Kontor%') AS office,
   bool_or(x->>'category'='Verksamhet') AS business,bool_or(x->>'category'='Industri') AS industrial,
   bool_or(x->>'category' NOT IN ('Bostad','Verksamhet')) AS other
  FROM jsonb_array_elements(b.purposes) x
 ) p ON true
 ) SELECT * FROM facts WITH NO DATA;
CREATE UNIQUE INDEX model_facts_fid ON gis.model_facts(fid);
CREATE UNIQUE INDEX model_facts_source_id ON gis.model_facts(building_id);
CREATE INDEX model_facts_municipality ON gis.model_facts(municipality_code);
CREATE INDEX model_facts_classification ON gis.model_facts(building_type,construction_year,ventilation_system);

-- Reader-safe evidence views. Detailed records are hydrated only for selected keys.
CREATE VIEW gis.model_buildings AS SELECT * FROM lake.building;
CREATE VIEW gis.model_components AS SELECT * FROM lake.component;
CREATE VIEW gis.model_properties AS SELECT * FROM lake.property;
CREATE VIEW gis.model_property_parts AS SELECT * FROM lake.property_part;
CREATE VIEW gis.model_property_links AS SELECT * FROM lake.building_property;
CREATE VIEW gis.model_addresses AS SELECT * FROM lake.address;
CREATE VIEW gis.model_epc_links AS SELECT * FROM lake.building_epc;
CREATE VIEW gis.model_epc_subjects AS SELECT * FROM lake.epc_subject;
CREATE VIEW gis.model_field_mapping AS SELECT * FROM lake.model_field_mapping;
CREATE OR REPLACE VIEW gis.publication_status AS
 SELECT revision AS source_revision,linked_revision,published_revision,linked_at,published_at,
  published_revision IS DISTINCT FROM revision AS refresh_needed,
  EXISTS(SELECT 1 FROM lake.import_file WHERE state<>'complete') AS incomplete_source_files,
  model_revision FROM lake.publication;
UPDATE lake.schema_version SET version=2;
-- Complete stock exports also retain selected registry buildings without footprints.
CREATE VIEW gis.model_export_buildings AS
 SELECT f.fid,f.building_id AS building_external_id,f.municipality_code,f.construction_year,
        f.building_type,f.ventilation_system,f.certificate_count,f.single_building_epc,
        b.geometry_valid,b.geom
 FROM gis.model_facts f JOIN lake.building b ON b.building_id=f.fid;

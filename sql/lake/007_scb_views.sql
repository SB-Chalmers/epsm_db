-- Live indexed views over SCB observations; no geometry per category/year.
CREATE INDEX IF NOT EXISTS statistic_indicator_pattern ON lake.statistic(indicator text_pattern_ops,period);
CREATE OR REPLACE VIEW gis.statistics AS
 SELECT observation_id AS fid,area_id,area_type,area_external_id AS area_code,
        boundary_version,indicator,period,unit,value,raw_value,value_status,
        dimensions,file_id,source_record,source_field,
        attributes->>'scb_table_id' AS scb_table_id,
        attributes->>'indicator_label' AS indicator_label,
        attributes->'dimension_labels' AS dimension_labels FROM lake.statistic;

CREATE OR REPLACE VIEW gis.area_statistics AS
 SELECT s.observation_id AS fid,s.area_id,s.area_type,s.area_external_id AS area_code,
        s.boundary_version,a.name,s.indicator,s.period,s.unit,s.value,s.raw_value,
        s.value_status,s.dimensions,a.geom,
        a.municipality_code,s.attributes->>'scb_table_id' AS scb_table_id,
        s.attributes->>'indicator_label' AS indicator_label,
        s.attributes->'dimension_labels' AS dimension_labels
 FROM lake.statistic s JOIN lake.area a USING(area_id);

CREATE OR REPLACE VIEW gis.scb_population AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB6574:%';
CREATE OR REPLACE VIEW gis.scb_households AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB6568:%';
CREATE OR REPLACE VIEW gis.scb_housing_tenure AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB6638:%';
CREATE OR REPLACE VIEW gis.scb_income AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB6679:%';
CREATE OR REPLACE VIEW gis.scb_employment AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB6680:%';
CREATE OR REPLACE VIEW gis.scb_education AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB6534:%';
CREATE OR REPLACE VIEW gis.scb_cars AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB6589:%';
CREATE OR REPLACE VIEW gis.scb_energy_use AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB3654:%';
CREATE OR REPLACE VIEW gis.scb_heat_production AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB3452:%';
CREATE OR REPLACE VIEW gis.scb_electricity_production AS SELECT * FROM gis.area_statistics WHERE indicator LIKE 'scb:TAB3451:%';

-- Numeric source keys first, typed stored geometry, and spatial indexes.
-- Publication refreshes snapshots together in one transaction. No joins run on pan.
CREATE MATERIALIZED VIEW gis.component_heights AS
 SELECT c.component_id AS fid,c.external_id AS component_external_id,
        c.building_id,b.external_id AS building_external_id,b.municipality_code,
        c.attributes->>'representation_status' AS representation_status,
        h.height_id,h.height_status,h.representative_height_m,h.eave_height_m,
        h.ridge_height_m,h.roof_form,h.measured_at,h.evidence_path,
        (h.height_id IS NOT NULL) AS height_record_available,c.geometry_valid,c.geom
 FROM lake.component c JOIN lake.building b USING(building_id)
 LEFT JOIN lake.latest_height h USING(component_id);
CREATE UNIQUE INDEX gis_component_heights_fid ON gis.component_heights(fid);
CREATE INDEX gis_component_heights_geom ON gis.component_heights USING gist(geom);
CREATE INDEX gis_component_heights_municipality ON gis.component_heights(municipality_code);
CREATE VIEW gis.components AS SELECT * FROM gis.component_heights;
CREATE MATERIALIZED VIEW gis.buildings AS
 SELECT b.building_id AS fid,b.external_id AS building_external_id,b.municipality_code,
        b.house_number,b.construction_year,b.object_status,b.purposes,b.municipality_conflict,b.geometry_valid,
        public.ST_Area(b.geom) AS footprint_area_m2,
        COALESCE(h.component_count,0) AS component_count,
        COALESCE(h.determined_height_count,0) AS determined_height_count,
        h.minimum_component_height_m,h.maximum_component_height_m,
        COALESCE(p.property_count,0) AS matched_property_count,
        COALESCE(a.address_count,0) AS address_count,
        COALESCE(e.epc_count,0) AS matched_epc_count,b.geom
 FROM lake.building b
 LEFT JOIN (SELECT c.building_id,count(*) AS component_count,
                   count(*) FILTER(WHERE h.height_status='determined') AS determined_height_count,
                   min(h.representative_height_m) FILTER(WHERE h.height_status='determined') AS minimum_component_height_m,
                   max(h.representative_height_m) FILTER(WHERE h.height_status='determined') AS maximum_component_height_m
            FROM lake.component c LEFT JOIN lake.latest_height h USING(component_id)
            GROUP BY c.building_id) h USING(building_id)
 LEFT JOIN (SELECT building_id,count(*) AS property_count FROM lake.building_property
            WHERE status='matched' GROUP BY building_id) p USING(building_id)
 LEFT JOIN (SELECT building_id,count(DISTINCT address_id) AS address_count FROM lake.building_address
            WHERE address_id IS NOT NULL GROUP BY building_id) a USING(building_id)
 LEFT JOIN (SELECT building_id,count(DISTINCT epc_id) AS epc_count FROM lake.building_epc
            WHERE status='matched' GROUP BY building_id) e USING(building_id)
 WHERE b.geom IS NOT NULL;
CREATE UNIQUE INDEX gis_buildings_fid ON gis.buildings(fid);
CREATE INDEX gis_buildings_geom ON gis.buildings USING gist(geom);
CREATE INDEX gis_buildings_municipality ON gis.buildings(municipality_code);
CREATE MATERIALIZED VIEW gis.properties AS
 SELECT property_id AS fid,external_id AS property_external_id,municipality_code,
        designation,geometry_valid,geom FROM lake.property WHERE geom IS NOT NULL;
CREATE UNIQUE INDEX gis_properties_fid ON gis.properties(fid);
CREATE INDEX gis_properties_geom ON gis.properties USING gist(geom);
CREATE INDEX gis_properties_municipality ON gis.properties(municipality_code);
CREATE MATERIALIZED VIEW gis.addresses AS
 SELECT address_id AS fid,external_id AS address_external_id,municipality_code,
        street_name,address_number,locality,geom FROM lake.address WHERE geom IS NOT NULL;
CREATE UNIQUE INDEX gis_addresses_fid ON gis.addresses(fid);
CREATE INDEX gis_addresses_geom ON gis.addresses USING gist(geom);
CREATE INDEX gis_addresses_municipality ON gis.addresses(municipality_code);
CREATE MATERIALIZED VIEW gis.epc_links AS
 SELECT l.link_id AS fid,l.building_id,b.external_id AS building_external_id,
        b.municipality_code,l.subject_id,l.status AS match_status,l.method,
        e.epc_id,e.external_id AS certificate_external_id,e.certificate_date,
        e.energy_class,e.atemp_m2,b.geom
 FROM lake.building_epc l JOIN lake.building b USING(building_id)
 JOIN lake.epc e USING(epc_id) WHERE b.geom IS NOT NULL;
CREATE UNIQUE INDEX gis_epc_links_fid ON gis.epc_links(fid);
CREATE INDEX gis_epc_links_geom ON gis.epc_links USING gist(geom);
CREATE INDEX gis_epc_links_municipality ON gis.epc_links(municipality_code);
CREATE MATERIALIZED VIEW gis.building_properties AS
 SELECT l.link_id AS fid,l.building_id,l.property_id,l.overlap_fraction,l.status,l.method,b.geom
 FROM lake.building_property l JOIN lake.building b USING(building_id) WHERE b.geom IS NOT NULL;
CREATE UNIQUE INDEX gis_building_properties_fid ON gis.building_properties(fid);
CREATE INDEX gis_building_properties_geom ON gis.building_properties USING gist(geom);
CREATE VIEW gis.building_addresses AS
 SELECT link_id AS fid,building_id,address_id,address_external_id,method FROM lake.building_address;
CREATE VIEW gis.epc_records AS SELECT record_id AS fid,epc_id,file_id,source_record,attributes FROM lake.epc_record;
CREATE VIEW gis.height_evidence AS
 SELECT height_id AS fid,component_id,component_external_id,height_status,representative_height_m,eave_height_m,
        ridge_height_m,roof_form,measured_at,evidence_path,file_id,source_record,attributes
 FROM lake.height;
CREATE VIEW gis.publication_status AS
 SELECT revision AS source_revision,linked_revision,published_revision,linked_at,published_at,
        published_revision IS DISTINCT FROM revision AS refresh_needed,
        EXISTS(SELECT 1 FROM lake.import_file WHERE state<>'complete') AS incomplete_source_files
 FROM lake.publication;
CREATE VIEW gis.sources AS
 SELECT f.file_id AS fid,s.name,s.kind,s.version,f.path,f.state,f.committed_rows,f.updated_at
 FROM lake.source s JOIN lake.import_file f USING(source_id);
CREATE VIEW gis.footprints AS SELECT * FROM gis.buildings;

-- One inspectable source-record interface; no second raw geometry copy.
CREATE VIEW gis.source_records AS
 SELECT 'building:'||building_id::text AS fid,'building'::text AS entity_type,
        building_id AS entity_id,registry_file_id AS file_id,registry_source_record AS source_record,attributes
 FROM lake.building WHERE registry_file_id IS NOT NULL
 UNION ALL
 SELECT 'component:'||component_id::text,'component',component_id,file_id,source_record,attributes FROM lake.component
 UNION ALL
 SELECT 'property_part:'||part_id::text,'property_part',part_id,file_id,source_record,attributes FROM lake.property_part
 UNION ALL
 SELECT 'address:'||address_id::text,'address',address_id,file_id,source_record,attributes FROM lake.address
 UNION ALL
 SELECT 'epc_record:'||record_id::text,'epc_record',record_id,file_id,source_record,attributes FROM lake.epc_record
 UNION ALL
 SELECT 'height:'||height_id::text,'height',height_id,file_id,source_record,attributes FROM lake.height;

CREATE VIEW gis.property_aliases AS
 SELECT part_id AS fid,property_id,municipality_code,designation,designation_key
 FROM lake.property_part;

CREATE MATERIALIZED VIEW gis.boundaries AS
 SELECT area_id AS fid,area_type,external_id AS area_code,boundary_version,name,
        municipality_code,parent_area_id,geometry_valid,geom FROM lake.area;
CREATE UNIQUE INDEX gis_boundaries_fid ON gis.boundaries(fid);
CREATE INDEX gis_boundaries_geom ON gis.boundaries USING gist(geom);
CREATE INDEX gis_boundaries_municipality ON gis.boundaries(municipality_code);
CREATE INDEX gis_boundaries_type_version ON gis.boundaries(area_type,boundary_version);
CREATE VIEW gis.statistics AS
 SELECT observation_id AS fid,area_id,area_type,area_external_id AS area_code,
        boundary_version,indicator,period,unit,value,raw_value,value_status,
        dimensions,file_id,source_record,source_field,
        attributes->>'scb_table_id' AS scb_table_id,
        attributes->>'indicator_label' AS indicator_label,
        attributes->'dimension_labels' AS dimension_labels FROM lake.statistic;
-- Filter indicator/period/dimensions before mapping. Indexed numeric joins avoid
-- copying boundary geometry for every year, category and statistical indicator.
CREATE VIEW gis.area_statistics AS
 SELECT s.observation_id AS fid,s.area_id,s.area_type,s.area_external_id AS area_code,
        s.boundary_version,a.name,s.indicator,s.period,s.unit,s.value,s.raw_value,
        s.value_status,s.dimensions,a.geom,
        a.municipality_code,s.attributes->>'scb_table_id' AS scb_table_id,
        s.attributes->>'indicator_label' AS indicator_label,
        s.attributes->'dimension_labels' AS dimension_labels
 FROM lake.statistic s JOIN lake.area a USING(area_id);
CREATE VIEW gis.building_areas AS
 SELECT l.link_id AS fid,l.building_id,l.area_id,a.area_type,a.external_id AS area_code,
        a.boundary_version,l.method,l.status FROM lake.building_area l JOIN lake.area a USING(area_id);
CREATE VIEW gis.building_statistics AS
 SELECT l.link_id::text||':'||s.observation_id::text AS fid,
        l.building_id,s.observation_id,s.area_id,s.area_type,s.area_external_id AS area_code,
        s.boundary_version,s.indicator,s.period,s.unit,s.value,s.value_status,s.dimensions,
        'area_context'::text AS interpretation
 FROM lake.building_area l JOIN lake.statistic s USING(area_id) WHERE l.status='matched';
CREATE MATERIALIZED VIEW gis.roads AS
 SELECT road_id AS fid,external_id,source_layer,network_link_id,start_measure,end_measure,
        geometry_valid,attributes,public.ST_Multi(geom)::geometry(MultiLineString,3006) AS geom
 FROM lake.road WHERE public.GeometryType(geom) IN ('LINESTRING','MULTILINESTRING');
CREATE UNIQUE INDEX gis_roads_fid ON gis.roads(fid);
CREATE INDEX gis_roads_geom ON gis.roads USING gist(geom);
CREATE INDEX gis_roads_layer ON gis.roads(source_layer);
CREATE MATERIALIZED VIEW gis.road_points AS
 SELECT road_id AS fid,external_id,source_layer,network_link_id,attributes,
        public.ST_Multi(geom)::geometry(MultiPoint,3006) AS geom
 FROM lake.road WHERE public.GeometryType(geom) IN ('POINT','MULTIPOINT');
CREATE UNIQUE INDEX gis_road_points_fid ON gis.road_points(fid);
CREATE INDEX gis_road_points_geom ON gis.road_points USING gist(geom);
CREATE MATERIALIZED VIEW gis.road_areas AS
 SELECT road_id AS fid,external_id,source_layer,network_link_id,attributes,
        public.ST_Multi(geom)::geometry(MultiPolygon,3006) AS geom
 FROM lake.road WHERE public.GeometryType(geom) IN ('POLYGON','MULTIPOLYGON');
CREATE UNIQUE INDEX gis_road_areas_fid ON gis.road_areas(fid);
CREATE INDEX gis_road_areas_geom ON gis.road_areas USING gist(geom);
CREATE VIEW gis.road_attributes AS
 SELECT road_id AS fid,external_id,network_link_id,source_layer,source_member,start_measure,
        end_measure,geometry_valid,file_id,source_record,attributes FROM lake.road;
CREATE VIEW gis.context_records AS
 SELECT 'area:'||area_id::text AS fid,'area'::text AS entity_type,area_id AS entity_id,
        file_id,source_record,attributes FROM lake.area
 UNION ALL
 SELECT 'statistic:'||observation_id::text,'statistic',observation_id,file_id,source_record,attributes FROM lake.statistic
 UNION ALL
 SELECT 'road:'||road_id::text,'road',road_id,file_id,source_record,attributes FROM lake.road;
-- Convenient QGIS layer names share the same indexed boundary snapshot.
CREATE VIEW gis.national_boundary AS SELECT * FROM gis.boundaries WHERE area_type='country';
CREATE VIEW gis.counties AS SELECT * FROM gis.boundaries WHERE area_type='county';
CREATE VIEW gis.municipalities AS SELECT * FROM gis.boundaries WHERE area_type='municipality';
CREATE VIEW gis.regso AS SELECT * FROM gis.boundaries WHERE area_type='regso';
CREATE VIEW gis.deso AS SELECT * FROM gis.boundaries WHERE area_type='deso';
CREATE VIEW gis.population_grids AS SELECT * FROM gis.boundaries WHERE area_type='population_grid_1km';

-- Raw cadastral parts include unresolved source property-unit references.
CREATE VIEW gis.property_parts AS
 SELECT part_id AS fid,external_id AS property_part_external_id,property_id,
        municipality_code,designation,
        CASE WHEN property_id IS NULL THEN 'missing_source_reference'
             ELSE 'source_reference' END AS property_reference_status,
        attributes->>'objekttyp' AS source_feature_type,attributes,geom
 FROM lake.property_part;

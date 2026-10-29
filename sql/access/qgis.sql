-- Administrative mapping projections, separate from immutable source migrations.
-- Run as the database owner after the standard mart views and ubem_readonly exist.
-- QGIS selects the first view attribute as its feature identifier. JSON array
-- encoding preserves both source key parts without delimiter/hash collisions.
-- Explicit 2D MultiPolygon/3006 typmods publish complete geometry metadata.
CREATE SCHEMA IF NOT EXISTS qgis;
REVOKE ALL ON SCHEMA qgis FROM PUBLIC;

CREATE OR REPLACE VIEW qgis.footprints AS
SELECT pg_catalog.jsonb_build_array(f.dataset_id,f.building_id)::text AS fid,
       f.dataset_id,f.building_id,f.municipality_code,f.selection_role,
       f.object_status,f.is_existing,f.construction_year,f.geometry_status,
       f.linkage_eligible,f.quality_flags,f.area_m2,
       public.ST_Multi(public.ST_CollectionExtract(public.ST_Force2D(f.geom),3))
         ::public.geometry(MultiPolygon,3006) AS geom
FROM mart.building_facts f
WHERE f.geom IS NOT NULL;

CREATE OR REPLACE VIEW qgis.component_heights AS
SELECT pg_catalog.jsonb_build_array(c.dataset_id,c.component_id)::text AS fid,
       c.dataset_id,c.component_id,c.building_id,c.geometry_hash,
       c.representation_status,
       (h.component_id IS NOT NULL) AS height_record_available,
       h.height_status,h.representative_height_m,h.eave_height_m,
       h.ridge_height_m,h.roof_form,
       public.ST_Multi(public.ST_CollectionExtract(public.ST_Force2D(c.geom),3))
         ::public.geometry(MultiPolygon,3006) AS geom
FROM mart.building_components c
LEFT JOIN mart.component_heights h
  ON h.dataset_id=c.dataset_id AND h.component_id=c.component_id
 AND h.input_geometry_hash=c.geometry_hash
WHERE c.geom IS NOT NULL;

COMMENT ON SCHEMA qgis IS
 'Read-only mapping views with stable feature identifiers and explicit EPSG:3006 polygon geometry. Dataset presence does not imply publication or acceptance.';
COMMENT ON VIEW qgis.footprints IS
 'Building footprints for QGIS. fid encodes (dataset_id,building_id). Polygon components are exposed in 2D; original geometry remains in mart/stock.';
COMMENT ON VIEW qgis.component_heights IS
 'Component polygons with available height attributes, joined on dataset,component and geometry hash. Missing height evidence remains NULL; it is not a zero height.';

REVOKE ALL ON qgis.footprints,qgis.component_heights FROM PUBLIC;
GRANT USAGE ON SCHEMA qgis TO ubem_readonly;
GRANT SELECT ON qgis.footprints,qgis.component_heights TO ubem_readonly;

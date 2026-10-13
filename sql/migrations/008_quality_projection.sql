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

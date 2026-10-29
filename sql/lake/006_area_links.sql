-- One indexed membership operation, once after geometry/boundary changes.
-- Keep every boundary edition; border ties are explicit. Statistics stay totals
-- for their area, never inferred household/building measurements.
CREATE TEMP TABLE area_candidates ON COMMIT DROP AS
WITH candidates AS (
 SELECT b.building_id,a.area_id,a.area_type,a.boundary_version
 FROM lake.building b JOIN lake.area a
 ON a.geom && b.label_point AND CASE WHEN a.geometry_valid
    THEN public.ST_Covers(a.geom,b.label_point) ELSE false END
 WHERE b.label_point IS NOT NULL
), ranked AS (
 SELECT *,count(*) OVER(PARTITION BY building_id,area_type,boundary_version) AS matches FROM candidates
)
SELECT building_id,area_id,CASE WHEN matches=1 THEN 'matched' ELSE 'ambiguous' END AS status FROM ranked;
INSERT INTO lake.building_area(building_id,area_id,status)
 SELECT building_id,area_id,status FROM area_candidates
 ON CONFLICT(building_id,area_id) DO UPDATE SET status=EXCLUDED.status;
DELETE FROM lake.building_area l WHERE NOT EXISTS
 (SELECT 1 FROM area_candidates c WHERE c.building_id=l.building_id AND c.area_id=l.area_id);
ANALYZE lake.building_area;

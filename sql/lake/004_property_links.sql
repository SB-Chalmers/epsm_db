-- These generated links are reproducible; manual/source references live separately.
CREATE TEMP TABLE property_candidates ON COMMIT DROP AS
WITH intersections AS (
 SELECT b.building_id,p.property_id,
        public.ST_Area(CASE WHEN b.geometry_valid AND p.geometry_valid
          AND public.ST_Area(b.geom)>0 AND b.geom && p.geom
          THEN public.ST_Intersection(b.geom,p.geom) END)
        /NULLIF(public.ST_Area(b.geom),0) AS fraction
 FROM lake.building b JOIN lake.property p ON b.geom && p.geom
 WHERE b.geometry_valid AND p.geometry_valid
), ranked AS (
 SELECT *,count(*) FILTER(WHERE fraction >= %(minimum_overlap)s) OVER(PARTITION BY building_id) AS eligible
 FROM intersections WHERE fraction>0
)
SELECT building_id,property_id,fraction AS overlap_fraction,
        CASE WHEN fraction<%(minimum_overlap)s THEN 'candidate'
             WHEN eligible=1 THEN 'matched' ELSE 'ambiguous' END AS status FROM ranked;

INSERT INTO lake.building_property(building_id,property_id,overlap_fraction,status)
 SELECT building_id,property_id,overlap_fraction,status FROM property_candidates
 ON CONFLICT(building_id,property_id) DO UPDATE
 SET overlap_fraction=EXCLUDED.overlap_fraction,status=EXCLUDED.status;
DELETE FROM lake.building_property l WHERE NOT EXISTS
 (SELECT 1 FROM property_candidates c WHERE c.building_id=l.building_id AND c.property_id=l.property_id);
ANALYZE lake.building_property;

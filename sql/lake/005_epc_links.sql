-- An explicit source building reference takes precedence over property matching.
CREATE TEMP TABLE epc_candidates ON COMMIT DROP AS
WITH candidates AS (
 SELECT s.subject_id,s.epc_id,b.building_id,'source_building_reference'::text AS method
 FROM lake.epc_subject s JOIN lake.building b ON b.external_id=s.building_external_id
 UNION ALL
 SELECT DISTINCT s.subject_id,s.epc_id,b.building_id,'property_and_house_number'::text
 FROM lake.epc_subject s
 JOIN lake.property_part p ON p.municipality_code=s.municipality_code AND p.designation_key=s.designation_key
 JOIN lake.building_property l ON l.property_id=p.property_id AND l.status='matched'
 JOIN lake.building b ON b.building_id=l.building_id AND b.house_number=s.house_number
                     AND b.municipality_code=s.municipality_code
 WHERE s.building_external_id IS NULL AND s.house_number IS NOT NULL
), ranked AS (
 SELECT *,count(*) OVER(PARTITION BY subject_id) AS candidate_count FROM candidates
)
SELECT building_id,epc_id,subject_id,
        CASE WHEN candidate_count=1 THEN 'matched' ELSE 'ambiguous' END AS status,method FROM ranked;

INSERT INTO lake.building_epc(building_id,epc_id,subject_id,status,method)
 SELECT building_id,epc_id,subject_id,status,method FROM epc_candidates
 ON CONFLICT(building_id,subject_id) DO UPDATE SET status=EXCLUDED.status,method=EXCLUDED.method;
DELETE FROM lake.building_epc l WHERE NOT EXISTS
 (SELECT 1 FROM epc_candidates c WHERE c.building_id=l.building_id AND c.subject_id=l.subject_id);
ANALYZE lake.building_epc;

-- Preserve cadastral polygons whose source has no legal property-unit reference.
-- Existing source-part IDs and provenance remain intact; no property is invented.
ALTER TABLE lake.property_part ALTER COLUMN property_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS property_part_municipality ON lake.property_part(municipality_code);
CREATE OR REPLACE VIEW gis.property_parts AS
 SELECT part_id AS fid,external_id AS property_part_external_id,property_id,
        municipality_code,designation,
        CASE WHEN property_id IS NULL THEN 'missing_source_reference'
             ELSE 'source_reference' END AS property_reference_status,
        attributes->>'objekttyp' AS source_feature_type,attributes,geom
 FROM lake.property_part;

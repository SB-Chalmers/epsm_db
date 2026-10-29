-- Keep set-based joins informed after large COPY imports.
ANALYZE lake.component;
ANALYZE lake.building;
ANALYZE lake.height;
ANALYZE lake.address;
ANALYZE lake.building_address;
ANALYZE lake.property;
ANALYZE lake.property_part;
ANALYZE lake.epc;
ANALYZE lake.epc_subject;
ANALYZE lake.area;
ANALYZE lake.statistic;

-- Reconcile references in sets; one spatial join per building/property batch,
-- never a national spatial scan for every certificate.
UPDATE lake.height h SET component_id=c.component_id FROM lake.component c
 WHERE h.component_external_id=c.external_id AND h.component_id IS DISTINCT FROM c.component_id;
UPDATE lake.building_address l SET address_id=a.address_id FROM lake.address a
 WHERE l.address_external_id=a.external_id AND l.address_id IS DISTINCT FROM a.address_id;
-- Dirty geometries are unions calculated once after import, then stored/indexed.
WITH shapes AS (
 SELECT c.building_id,public.ST_Multi(CASE WHEN bool_and(c.geometry_valid)
        THEN public.ST_UnaryUnion(public.ST_Collect(c.geom))
        ELSE public.ST_CollectionExtract(public.ST_Collect(c.geom),3) END)
        ::geometry(MultiPolygon,3006) AS geom
 FROM lake.component c JOIN lake.building b USING(building_id)
 WHERE b.geometry_dirty GROUP BY c.building_id
)
UPDATE lake.building b SET geom=s.geom,geometry_valid=public.ST_IsValid(s.geom),geometry_dirty=false,
 label_point=CASE WHEN public.ST_IsValid(s.geom) THEN public.ST_PointOnSurface(s.geom) END
 FROM shapes s WHERE s.building_id=b.building_id;
WITH shapes AS (
 SELECT p.property_id,public.ST_Multi(CASE WHEN bool_and(public.ST_IsValid(p.geom))
        THEN public.ST_UnaryUnion(public.ST_Collect(p.geom))
        ELSE public.ST_CollectionExtract(public.ST_Collect(p.geom),3) END)
        ::geometry(MultiPolygon,3006) AS geom
 FROM lake.property_part p JOIN lake.property u USING(property_id)
 WHERE u.geometry_dirty GROUP BY p.property_id
)
UPDATE lake.property p SET geom=s.geom,geometry_valid=public.ST_IsValid(s.geom),geometry_dirty=false
 FROM shapes s WHERE s.property_id=p.property_id;
ANALYZE lake.building;
ANALYZE lake.component;
ANALYZE lake.property;
ANALYZE lake.height;

UPDATE lake.area child SET parent_area_id=parent.area_id FROM lake.area parent
 WHERE child.parent_type=parent.area_type AND child.parent_external_id=parent.external_id
 AND child.parent_boundary_version=parent.boundary_version AND child.area_id<>parent.area_id
 AND child.parent_area_id IS DISTINCT FROM parent.area_id;
UPDATE lake.statistic s SET area_id=a.area_id FROM lake.area a
 WHERE s.area_type=a.area_type AND s.area_external_id=a.external_id
 AND s.boundary_version=a.boundary_version AND s.area_id IS NULL;
ANALYZE lake.area;
ANALYZE lake.statistic;

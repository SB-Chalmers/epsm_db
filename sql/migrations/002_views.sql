-- Selection projections over shared national evidence.
CREATE VIEW mart.datasets AS
SELECT dataset_id,municipality_code,schema_version,created_at FROM catalog.dataset WHERE dataset_kind<>'national_source';

CREATE VIEW mart.building_purposes AS
SELECT m.dataset_id,x.building_id,x.ordinal,x.category,x.detail,x.is_main,x.original_values,x.run_id FROM scope.dataset_building m JOIN stock.building_purpose x ON x.dataset_id=m.source_dataset_id AND x.building_id=m.building_id;

CREATE VIEW mart.building_components AS
SELECT m.dataset_id,x.component_id,x.building_id,x.geometry_hash,x.geom,x.outline_labels,x.representation_status,x.owner_tile_id,x.source_fids,x.revision_timestamp,x.object_version,x.run_id FROM scope.dataset_building m JOIN stock.geometry_component x ON x.dataset_id=m.source_dataset_id AND x.building_id=m.building_id;

CREATE VIEW mart.component_heights AS
SELECT m.dataset_id,g.building_id,h.component_id,h.input_geometry_hash,h.height_status,h.representative_height_m,h.eave_height_m,h.ridge_height_m,h.roof_form,(r.original_values||h.details) AS details,h.evidence_sha256,h.run_id FROM scope.dataset_building m JOIN stock.geometry_component g ON (g.dataset_id,g.building_id)=(m.source_dataset_id,m.building_id) JOIN evidence.component_height h ON (h.dataset_id,h.component_id)=(g.dataset_id,g.component_id) LEFT JOIN raw_height.record r ON r.dataset_id=h.dataset_id AND r.kind='component_height' AND r.source_record=h.component_id;

CREATE VIEW mart.building_addresses AS
SELECT m.dataset_id,e.building_id,e.entrance_id,e.address_place_id,a.street_name,a.address_number,a.locality,a.geom,(a.address_place_id IS NOT NULL) AS resolved,e.original_values,e.run_id FROM scope.dataset_building m JOIN stock.entrance e ON (e.dataset_id,e.building_id)=(m.source_dataset_id,m.building_id) LEFT JOIN stock.address_place a ON (a.dataset_id,a.address_place_id)=(e.dataset_id,e.address_place_id);

CREATE VIEW mart.building_properties AS
SELECT m.dataset_id,p.building_id,p.property_unit_id,p.intersection_area_m2,p.overlap_fraction,p.rank,p.is_primary,p.run_id FROM scope.dataset_building m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN evidence.building_property p ON (p.dataset_id,p.processing_release_id,p.building_id)=(m.source_dataset_id,d.processing_release_id,m.building_id);

CREATE VIEW mart.epc_subjects AS
SELECT m.dataset_id,x.subject_id,x.certificate_id,x.municipality_code,x.designation_normalized,x.house_number,x.original_values,x.run_id FROM scope.dataset_certificate m JOIN evidence.epc_subject x ON (x.dataset_id,x.certificate_id)=(m.source_dataset_id,m.certificate_id);

CREATE VIEW mart.epc_observations AS
SELECT m.dataset_id,x.observation_id,x.certificate_id,x.owner_kind,x.owner_key,x.source_field,x.raw_values,x.normalized_value,x.source_unit,x.normalized_unit,x.value_status,x.mapping_status,COALESCE((SELECT jsonb_agg(jsonb_build_object('file',r.source_file,'record',r.source_record) ORDER BY r.source_file,r.source_record::bigint) FROM (SELECT source_file,source_record,original_values FROM raw_epc.before_v260_row WHERE dataset_id=x.dataset_id AND certificate_id=x.certificate_id UNION ALL SELECT source_file,source_record,original_values FROM raw_epc.after_v260_row WHERE dataset_id=x.dataset_id AND certificate_id=x.certificate_id) r WHERE COALESCE(r.original_values->>x.source_field,'')<>''),'[]'::jsonb) AS source_locators,x.run_id FROM scope.dataset_certificate m JOIN evidence.epc_observation x ON (x.dataset_id,x.certificate_id)=(m.source_dataset_id,m.certificate_id);

CREATE VIEW mart.epc_candidates AS
SELECT m.dataset_id,r.building_id,r.subject_id,s.certificate_id,r.property_unit_id,r.decision,r.evidence_tier,r.reasons,(r.details||jsonb_build_object('source_locators',COALESCE((SELECT jsonb_agg(jsonb_build_object('table',r.source_table,'record_id',r.record_id,'file',r.source_file,'record',r.source_record,'sha256',r.source_sha256) ORDER BY r.source_file,r.source_record::bigint) FROM (SELECT 'raw_epc.before_v260_row'::text source_table,record_id,source_file,source_record,source_sha256 FROM raw_epc.before_v260_row WHERE dataset_id=s.dataset_id AND certificate_id=s.certificate_id AND subject_id=s.subject_id UNION ALL SELECT 'raw_epc.after_v260_row',record_id,source_file,source_record,source_sha256 FROM raw_epc.after_v260_row WHERE dataset_id=s.dataset_id AND certificate_id=s.certificate_id AND subject_id=s.subject_id) r),'[]'::jsonb))) AS details,r.run_id FROM scope.dataset_certificate m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN evidence.epc_subject s ON (s.dataset_id,s.certificate_id)=(m.source_dataset_id,m.certificate_id) JOIN evidence.epc_candidate r ON (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,d.processing_release_id,s.subject_id);

CREATE VIEW mart.building_epc AS
SELECT r.dataset_id,r.building_id,r.subject_id,r.certificate_id,r.property_unit_id,r.decision,r.evidence_tier,r.reasons,r.details,r.run_id FROM mart.epc_candidates r JOIN scope.dataset_building m USING(dataset_id,building_id) WHERE r.decision='accepted';

CREATE VIEW mart.epc_raw_inventory AS
SELECT m.dataset_id,r.record_id,r.source_sha256,r.source_file,r.source_record,'fore_v260'::text AS regime FROM scope.dataset_certificate m JOIN raw_epc.before_v260_row r ON (r.dataset_id,r.certificate_id)=(m.source_dataset_id,m.certificate_id) UNION ALL SELECT m.dataset_id,r.record_id,r.source_sha256,r.source_file,r.source_record,'efter_v260'::text FROM scope.dataset_certificate m JOIN raw_epc.after_v260_row r ON (r.dataset_id,r.certificate_id)=(m.source_dataset_id,m.certificate_id);

CREATE VIEW mart.epc_subject_results AS
SELECT m.dataset_id,r.subject_id,s.certificate_id,s.municipality_code,s.designation_normalized,s.house_number,r.outcome,r.match_methods,r.reporting_scope,r.temporal_status,r.assessment_date,r.reasons,(r.details||jsonb_build_object('source_locators',COALESCE((SELECT jsonb_agg(jsonb_build_object('table',r.source_table,'record_id',r.record_id,'file',r.source_file,'record',r.source_record,'sha256',r.source_sha256) ORDER BY r.source_file,r.source_record::bigint) FROM (SELECT 'raw_epc.before_v260_row'::text source_table,record_id,source_file,source_record,source_sha256 FROM raw_epc.before_v260_row WHERE dataset_id=s.dataset_id AND certificate_id=s.certificate_id AND subject_id=s.subject_id UNION ALL SELECT 'raw_epc.after_v260_row',record_id,source_file,source_record,source_sha256 FROM raw_epc.after_v260_row WHERE dataset_id=s.dataset_id AND certificate_id=s.certificate_id AND subject_id=s.subject_id) r),'[]'::jsonb))) AS details,r.run_id FROM scope.dataset_certificate m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN evidence.epc_subject s ON (s.dataset_id,s.certificate_id)=(m.source_dataset_id,m.certificate_id) JOIN evidence.epc_subject_result r ON (r.dataset_id,r.processing_release_id,r.subject_id)=(s.dataset_id,d.processing_release_id,s.subject_id);

CREATE VIEW mart.epc_site_events AS
SELECT d.dataset_id,e.event_id,e.municipality_code,e.property_match_key,e.address,e.postcode,e.event_kind,e.reported_date,e.date_basis,e.description,e.source_url,e.source_sha256,e.source_version_id,e.source_document_sha256,e.evidence_scope,e.run_id FROM catalog.dataset d JOIN catalog.processing_release p USING(processing_release_id) JOIN evidence.epc_site_event e ON e.dataset_id=d.source_dataset_id AND e.source_version_id=ANY(p.event_source_versions) WHERE d.municipality_code IS NULL OR e.municipality_code=d.municipality_code;

CREATE VIEW mart.epc_diagnostic_summary AS
SELECT dataset_id,municipality_code,outcome,reporting_scope,temporal_status,count(*) AS subject_count,count(DISTINCT certificate_id) AS certificate_count FROM mart.epc_subject_results GROUP BY dataset_id,municipality_code,outcome,reporting_scope,temporal_status;

CREATE VIEW mart.epc_certificates AS
SELECT m.dataset_id,c.certificate_id,c.formular_id,c.regime,c.certificate_date,c.date_status,c.closure_status,c.run_id,
 (SELECT count(DISTINCT building_id) FROM mart.building_epc r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id)) AS loaded_building_count,
 (SELECT count(*) FROM evidence.epc_subject s WHERE (s.dataset_id,s.certificate_id)=(c.dataset_id,c.certificate_id)) AS subject_count,
 (SELECT count(*) FROM mart.epc_subject_results r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id) AND outcome='matched') AS matched_subject_count,
 CASE WHEN NOT EXISTS(SELECT 1 FROM evidence.epc_subject s WHERE (s.dataset_id,s.certificate_id)=(c.dataset_id,c.certificate_id)) THEN 'no_parsed_subjects'
 WHEN NOT EXISTS(SELECT 1 FROM mart.epc_subject_results r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id)) THEN 'not_assessed'
 WHEN NOT EXISTS(SELECT 1 FROM mart.epc_subject_results r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id) AND outcome='matched') THEN 'unmatched'
 WHEN EXISTS(SELECT 1 FROM mart.epc_subject_results r WHERE (r.dataset_id,r.certificate_id)=(m.dataset_id,c.certificate_id) AND outcome<>'matched') THEN 'partly_matched' ELSE 'fully_matched' END AS matching_status,
 'loaded_relations'::text AS cardinality_scope
 FROM scope.dataset_certificate m JOIN evidence.epc_certificate c ON (c.dataset_id,c.certificate_id)=(m.source_dataset_id,m.certificate_id);

CREATE VIEW mart.building_facts AS
SELECT m.dataset_id,b.building_id,b.municipality_code,m.selection_role,b.object_status,b.status_verified,b.is_existing,b.house_number,b.construction_year,b.main_building,
 f.display_geom AS geom,f.resolution_status AS geometry_status,(f.linkage_geom IS NOT NULL) AS linkage_eligible,f.quality_flags,ST_Area(f.display_geom) AS area_m2,
 (SELECT count(*) FROM stock.geometry_component c WHERE (c.dataset_id,c.building_id)=(b.dataset_id,b.building_id)) AS component_count,
 (SELECT count(*) FROM evidence.component_height h JOIN stock.geometry_component c USING(dataset_id,component_id) WHERE (c.dataset_id,c.building_id)=(b.dataset_id,b.building_id) AND h.height_status='determined') AS determined_height_component_count,
 (SELECT count(*) FROM stock.entrance e WHERE (e.dataset_id,e.building_id)=(b.dataset_id,b.building_id)) AS entrance_count,
 (SELECT count(*) FROM stock.entrance e JOIN stock.address_place a USING(dataset_id,address_place_id) WHERE (e.dataset_id,e.building_id)=(b.dataset_id,b.building_id)) AS resolved_address_count,
 (SELECT count(*) FROM mart.building_properties p WHERE (p.dataset_id,p.building_id)=(m.dataset_id,b.building_id)) AS property_candidate_count,
 (SELECT count(DISTINCT r.certificate_id) FROM mart.building_epc r WHERE (r.dataset_id,r.building_id)=(m.dataset_id,b.building_id)) AS epc_certificate_count,
 (SELECT count(*) FROM mart.epc_candidates r WHERE (r.dataset_id,r.building_id)=(m.dataset_id,b.building_id) AND decision='rejected') AS rejected_epc_candidate_count,
 COALESCE((SELECT array_agg(DISTINCT p.category ORDER BY p.category) FROM stock.building_purpose p WHERE (p.dataset_id,p.building_id)=(b.dataset_id,b.building_id)),'{}') AS purposes
 FROM scope.dataset_building m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN stock.building b ON (b.dataset_id,b.building_id)=(m.source_dataset_id,m.building_id)
 LEFT JOIN stock.building_footprint f ON (f.dataset_id,f.processing_release_id,f.building_id)=(b.dataset_id,d.processing_release_id,b.building_id);

CREATE VIEW mart.property_parts AS
WITH bounds AS (SELECT m.dataset_id,m.source_dataset_id,ST_SetSRID(ST_Extent(f.display_geom)::geometry,3006) geom FROM scope.dataset_building m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN stock.building_footprint f ON (f.dataset_id,f.processing_release_id,f.building_id)=(m.source_dataset_id,d.processing_release_id,m.building_id) GROUP BY m.dataset_id,m.source_dataset_id)
 SELECT x.dataset_id,p.property_area_part_id,p.property_unit_id,p.geom FROM bounds x JOIN stock.property_area_part p ON p.dataset_id=x.source_dataset_id AND ST_Intersects(p.geom,x.geom);

CREATE VIEW mart.property_units AS
SELECT DISTINCT dataset_id,property_unit_id FROM mart.property_parts;

CREATE VIEW mart.property_designations AS
SELECT m.dataset_id,p.property_unit_id,p.municipality_code,p.raw_label,p.normalized_label FROM mart.property_units m JOIN catalog.dataset d ON d.dataset_id=m.dataset_id JOIN stock.property_designation p ON (p.dataset_id,p.property_unit_id)=(d.source_dataset_id,m.property_unit_id);

CREATE VIEW mart.context_areas AS
SELECT d.dataset_id,a.area_type,a.area_code,a.name,a.reference_period,a.geom,a.run_id FROM catalog.dataset d JOIN context.area a ON a.dataset_id=d.source_dataset_id WHERE d.dataset_kind='national_selection' OR
 (a.area_type='municipality' AND a.area_code=d.municipality_code) OR
 (a.area_type='regso' AND EXISTS(SELECT 1 FROM context.area_relation r WHERE (r.dataset_id,r.child_type,r.child_code)=(a.dataset_id,a.area_type,a.area_code) AND r.parent_type='municipality' AND r.parent_code=d.municipality_code)) OR
 (a.area_type='population_grid_1km' AND EXISTS(SELECT 1 FROM context.area m WHERE m.dataset_id=a.dataset_id AND m.area_type='municipality' AND m.area_code=d.municipality_code AND ST_Intersects(a.geom,m.geom)));

CREATE VIEW mart.context_observations AS
SELECT a.dataset_id,o.observation_id,o.area_type,o.area_code,o.indicator_id,o.reference_period,o.dimensions,o.raw_value,o.normalized_value,o.normalized_unit,o.value_status,o.provenance FROM mart.context_areas a JOIN catalog.dataset d ON d.dataset_id=a.dataset_id JOIN context.observation o ON (o.dataset_id,o.area_type,o.area_code)=(d.source_dataset_id,a.area_type,a.area_code);

CREATE VIEW mart.building_areas AS
SELECT dataset_id,building_id,area_type,area_code,footprint_fraction,rank,is_primary,assignment_status,reasons,run_id FROM context.building_area;

CREATE VIEW mart.building_context AS
SELECT r.dataset_id,r.building_id,r.area_type,r.area_code,r.is_primary,r.assignment_status,o.observation_id,o.indicator_id,o.reference_period,o.dimensions,o.raw_value,o.normalized_value,o.source_unit,o.normalized_unit,o.transformation,o.value_status,o.provenance,o.run_id FROM context.building_area r JOIN context.observation o ON (o.dataset_id,o.area_type,o.area_code)=(r.source_dataset_id,r.area_type,r.area_code);

CREATE VIEW mart.building_context_summary AS
SELECT b.dataset_id,b.building_id,
 (SELECT area_code FROM context.building_area r WHERE (r.dataset_id,r.building_id)=(b.dataset_id,b.building_id) AND area_type='municipality' AND is_primary) AS municipality,
 (SELECT area_code FROM context.building_area r WHERE (r.dataset_id,r.building_id)=(b.dataset_id,b.building_id) AND area_type='regso' AND is_primary) AS regso,
 (SELECT area_code FROM context.building_area r WHERE (r.dataset_id,r.building_id)=(b.dataset_id,b.building_id) AND area_type='population_grid_1km' AND is_primary) AS population_cell,
 (SELECT jsonb_agg(jsonb_build_object('indicator',c.indicator_id,'period',c.reference_period,'dimensions',c.dimensions,'value',c.normalized_value,'unit',c.normalized_unit)) FROM mart.building_context c WHERE (c.dataset_id,c.building_id)=(b.dataset_id,b.building_id) AND c.is_primary) AS primary_observations
 FROM scope.dataset_building b;

CREATE VIEW mart.scope_membership AS
SELECT dataset_id,scope_id,building_id,inclusion_state,reasons FROM scope.membership;

CREATE VIEW mart.scope_completeness AS
SELECT b.dataset_id,b.building_id,l.layer,l.loading_status,CASE WHEN l.loading_status='loaded' THEN COALESCE(c.status,'missing') ELSE NULL END AS evidence_status,COALESCE(c.available_count,0) AS available_count,COALESCE(c.required_count,0) AS required_count,COALESCE(c.reasons,'{}') AS reasons FROM scope.dataset_building b JOIN catalog.dataset_layer l USING(dataset_id) LEFT JOIN scope.coverage c ON (c.dataset_id,c.building_id,c.layer)=(b.dataset_id,b.building_id,l.layer);

CREATE VIEW mart.process_runs AS
SELECT r.run_id,d.dataset_id,r.layer,r.policy_version,r.code_version,r.status,r.input_versions,r.started_at,r.finished_at,r.error FROM catalog.dataset d JOIN catalog.process_run r ON (r.dataset_id=d.source_dataset_id AND (r.processing_release_id IS NULL OR r.processing_release_id=d.processing_release_id)) OR r.dataset_id=d.dataset_id WHERE d.dataset_kind<>'national_source';

CREATE VIEW mart.source_inventory AS
WITH layers AS (SELECT d.dataset_id,l.layer,l.loading_status,l.source_version_id,l.run_id FROM catalog.dataset d JOIN catalog.dataset_layer l ON l.dataset_id=d.source_dataset_id WHERE d.dataset_kind<>'national_source' UNION ALL SELECT DISTINCT d.dataset_id,'epc_site_events'::text,'loaded'::text,e.source_version_id,e.run_id FROM catalog.dataset d JOIN catalog.processing_release p USING(processing_release_id) JOIN evidence.epc_site_event e ON e.dataset_id=d.source_dataset_id AND e.source_version_id=ANY(p.event_source_versions))
 SELECT l.dataset_id,l.layer,l.loading_status,l.source_version_id,v.publisher,v.contract,v.publication_status,v.metadata,f.logical_name,f.sha256,a.byte_size,x.profile,x.path,x.mtime_ns,l.run_id FROM layers l LEFT JOIN catalog.source_version v USING(source_version_id) LEFT JOIN catalog.source_file f ON f.source_version_id=l.source_version_id AND (f.logical_name NOT LIKE 'measurement/%' OR EXISTS(SELECT 1 FROM mart.component_heights h WHERE h.dataset_id=l.dataset_id AND h.evidence_sha256=f.sha256)) LEFT JOIN catalog.asset a ON a.sha256=f.sha256 LEFT JOIN catalog.asset_location x ON x.sha256=f.sha256;

CREATE VIEW mart.quality_findings AS
SELECT q.finding_id,d.dataset_id,q.building_id,q.layer,q.code,q.details,q.run_id FROM catalog.dataset d JOIN quality.finding q ON q.dataset_id=d.source_dataset_id AND (q.processing_release_id IS NULL OR q.processing_release_id=d.processing_release_id) WHERE d.dataset_kind<>'national_source' AND (EXISTS(SELECT 1 FROM scope.dataset_building m WHERE m.dataset_id=d.dataset_id AND m.building_id=q.building_id) OR q.source_record_id IN (SELECT record_id FROM mart.epc_raw_inventory i WHERE i.dataset_id=d.dataset_id) OR (q.layer='addresses' AND EXISTS(SELECT 1 FROM mart.building_addresses a WHERE a.dataset_id=d.dataset_id AND a.address_place_id=q.details->>'address_place_id')));

DO $$ DECLARE n text; cols text; BEGIN
 FOR n IN SELECT table_name FROM information_schema.views WHERE table_schema='mart' AND table_name<>'datasets' LOOP
 SELECT string_agg('v.'||quote_ident(column_name),',' ORDER BY ordinal_position) INTO cols FROM information_schema.columns WHERE table_schema='mart' AND table_name=n;
 EXECUTE format('CREATE VIEW mart.%I AS SELECT %s FROM mart.%I v JOIN catalog.active_dataset a USING(dataset_id)',n||'_current',cols,n);
 END LOOP;
END $$;

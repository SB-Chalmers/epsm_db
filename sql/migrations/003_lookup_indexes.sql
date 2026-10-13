-- Ownership and relation lookups used by the building-first query interfaces.
CREATE INDEX component_building_lookup ON stock.geometry_component(dataset_id,building_id);
CREATE INDEX entrance_building_lookup ON stock.entrance(dataset_id,building_id);
CREATE INDEX property_unit_parts_lookup ON stock.property_area_part(dataset_id,property_unit_id);
CREATE INDEX certificate_subject_lookup ON evidence.epc_subject(dataset_id,certificate_id);
CREATE INDEX certificate_observation_lookup ON evidence.epc_observation(dataset_id,certificate_id);
CREATE INDEX context_observation_area_lookup ON context.observation(dataset_id,area_type,area_code);
CREATE INDEX quality_building_lookup ON quality.finding(dataset_id,building_id);
CREATE INDEX registered_source_hash_lookup ON catalog.source_file(source_version_id,sha256);

CREATE INDEX building_assertion_lookup ON stock.building USING gin(municipality_values);
CREATE INDEX building_partition_lookup ON stock.building(dataset_id,processing_partition,building_id);
CREATE INDEX raw_epc_before_certificate ON raw_epc.before_v260_row(dataset_id,certificate_id,subject_id);
CREATE INDEX raw_epc_after_certificate ON raw_epc.after_v260_row(dataset_id,certificate_id,subject_id);
CREATE INDEX raw_epc_before_subject ON raw_epc.before_v260_row(dataset_id,subject_id);
CREATE INDEX raw_epc_after_subject ON raw_epc.after_v260_row(dataset_id,subject_id);
CREATE INDEX raw_lm_kind_lookup ON raw_lm.record(dataset_id,kind);
CREATE INDEX address_match_lookup ON stock.address_place(dataset_id,match_key);
CREATE INDEX address_base_lookup ON stock.address_place(dataset_id,base_match_key);
CREATE INDEX entrance_address_lookup ON stock.entrance(dataset_id,address_place_id,building_id);
CREATE INDEX designation_match_lookup ON stock.property_designation(dataset_id,municipality_code,normalized_label);
CREATE INDEX subject_property_lookup ON evidence.epc_subject(dataset_id,municipality_code,designation_normalized);
CREATE INDEX work_state_lookup ON catalog.work_unit(owner_dataset_id,stage,state);
CREATE INDEX height_measurement_locator_lookup ON raw_height.record(dataset_id,(original_values->>'evidence_record_path'),(original_values->>'evidence_root')) WHERE kind='component_height';
CREATE INDEX raw_height_component_lookup ON raw_height.record(dataset_id,source_record) WHERE kind='component_height';

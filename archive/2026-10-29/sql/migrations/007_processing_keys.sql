-- Query reverse relations through compact, versioned internal keys.
CREATE INDEX property_building_lookup ON evidence.building_property(dataset_id,processing_release_id,property_unit_id,building_id);
DROP INDEX evidence.epc_subject_candidate_lookup;
CREATE INDEX epc_subject_candidate_lookup ON evidence.epc_candidate(dataset_id,processing_release_id,subject_id,decision);

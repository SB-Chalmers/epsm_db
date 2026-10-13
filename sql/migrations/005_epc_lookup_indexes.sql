-- Certificate views and validation look up relations by subject, not by building.
CREATE INDEX epc_subject_candidate_lookup ON evidence.epc_candidate(dataset_id,subject_id,decision);

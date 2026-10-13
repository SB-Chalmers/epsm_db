-- component_id is already constrained to building UUID || ':' || geometry_hash.
-- Its primary key therefore identifies the hash, without a second wide unique
-- index repeating both the component ID and its 64-character suffix.
ALTER TABLE evidence.component_height
 DROP CONSTRAINT component_height_dataset_id_component_id_input_geometry_ha_fkey;
ALTER TABLE evidence.component_height
 ADD CONSTRAINT component_height_component_fkey
 FOREIGN KEY(dataset_id,component_id)
 REFERENCES stock.geometry_component(dataset_id,component_id),
 ADD CONSTRAINT component_height_geometry_hash_check
 CHECK(input_geometry_hash = split_part(component_id,':',2));
ALTER TABLE stock.geometry_component
 DROP CONSTRAINT geometry_component_dataset_id_component_id_geometry_hash_key;

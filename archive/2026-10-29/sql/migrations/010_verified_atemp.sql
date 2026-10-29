-- Official Boverket declaration form v2.8, page 3: Atemp excluding
-- Avarmgarage, m2. EgenByggnadsTyp is a distinct field; the mapping
-- workbook's EgenAtemp label "Byggnadstyp" is erroneous.
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM evidence.epc_observation)
 OR EXISTS(SELECT 1 FROM catalog.work_unit WHERE stage='import:epc') THEN
  RAISE EXCEPTION 'Verified Atemp mapping requires a fresh EPC import';
 END IF;
END $$;
UPDATE catalog.field_mapping SET review_status='verified',
 authority='Boverket Formulär version 2.8, page 3: EgenAtemp, Atemp (exklusive Avarmgarage), m2; separate EgenByggnadsTyp'
 WHERE regime='efter_v260' AND source_field='EgenAtemp';

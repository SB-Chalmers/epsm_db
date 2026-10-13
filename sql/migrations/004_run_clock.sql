-- PostgreSQL now() is the transaction-start time. Capture actual completion time.
CREATE FUNCTION catalog.record_run_completion() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.status IN ('complete','failed') AND OLD.status='running' THEN
   NEW.finished_at := clock_timestamp();
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER actual_run_completion BEFORE UPDATE OF status ON catalog.process_run
 FOR EACH ROW EXECUTE FUNCTION catalog.record_run_completion();

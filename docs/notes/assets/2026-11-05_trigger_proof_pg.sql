\set ON_ERROR_STOP 1
CREATE TABLE packets(packet_id TEXT PRIMARY KEY, prompt_text TEXT, status TEXT NOT NULL DEFAULT 'draft',
 version INTEGER NOT NULL DEFAULT 1, fencing_token INTEGER NOT NULL DEFAULT 0, active_run_id TEXT,
 last_producer TEXT NOT NULL DEFAULT 'user_sql');
CREATE TABLE raw_revision_outbox(outbox_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tx_id TEXT NOT NULL, packet_id TEXT NOT NULL,
 field_name TEXT NOT NULL, old_val TEXT, new_val TEXT, producer TEXT NOT NULL, captured_at timestamptz DEFAULT now());
CREATE FUNCTION fn_packets_bump_version() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
 IF (OLD.prompt_text IS DISTINCT FROM NEW.prompt_text OR OLD.status IS DISTINCT FROM NEW.status) THEN
  IF OLD.version = NEW.version THEN NEW.version := OLD.version + 1; NEW.last_producer := 'user_sql'; END IF;
 END IF; RETURN NEW; END; $$;
CREATE TRIGGER trg_packets_bump_version BEFORE UPDATE OF prompt_text, status ON packets FOR EACH ROW EXECUTE FUNCTION fn_packets_bump_version();
CREATE FUNCTION fn_packets_audit_outbox() RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE v_tx_id TEXT := pg_current_xact_id()::text;
BEGIN
 IF OLD.prompt_text IS DISTINCT FROM NEW.prompt_text THEN INSERT INTO raw_revision_outbox (tx_id, packet_id, field_name, old_val, new_val, producer) VALUES (v_tx_id, NEW.packet_id, 'prompt_text', OLD.prompt_text, NEW.prompt_text, NEW.last_producer); END IF;
 IF OLD.status IS DISTINCT FROM NEW.status THEN INSERT INTO raw_revision_outbox (tx_id, packet_id, field_name, old_val, new_val, producer) VALUES (v_tx_id, NEW.packet_id, 'status', OLD.status, NEW.status, NEW.last_producer); END IF;
 RETURN NULL; END; $$;
CREATE TRIGGER trg_packets_audit_outbox AFTER UPDATE OF prompt_text, status ON packets FOR EACH ROW
 WHEN (OLD.prompt_text IS DISTINCT FROM NEW.prompt_text OR OLD.status IS DISTINCT FROM NEW.status) EXECUTE FUNCTION fn_packets_audit_outbox();
INSERT INTO packets (packet_id, prompt_text, status, version, fencing_token) VALUES ('pkt-1','Initial prompt','draft',1,100);
UPDATE packets SET prompt_text='Initial prompt', status='draft' WHERE packet_id='pkt-1';
SELECT 'T1 exp v1 0', version, (SELECT count(*) FROM raw_revision_outbox) FROM packets;
UPDATE packets SET fencing_token=101, active_run_id='run-42', version=version+1 WHERE packet_id='pkt-1' AND version=1;
SELECT 'T2 exp v2 0', version, (SELECT count(*) FROM raw_revision_outbox) FROM packets;
UPDATE packets SET prompt_text=NULL, status='needs_review' WHERE packet_id='pkt-1';
SELECT 'T3 exp v3 2/1', version, (SELECT count(*) FROM raw_revision_outbox),(SELECT count(DISTINCT tx_id) FROM raw_revision_outbox) FROM packets;
UPDATE packets SET prompt_text='Revised', last_producer='kernel_worker', version=version+1 WHERE packet_id='pkt-1' AND version=3;
SELECT 'T4 exp v4 3', version, (SELECT count(*) FROM raw_revision_outbox) FROM packets;
UPDATE packets SET status='ready' WHERE packet_id='pkt-1';
SELECT 'T5 exp v5 user_sql', version, (SELECT producer FROM raw_revision_outbox ORDER BY outbox_id DESC LIMIT 1) FROM packets;
-- T6: semantic edit that sets version to the SAME value explicitly (version=version) must still bump
UPDATE packets SET status='hold', version=version WHERE packet_id='pkt-1';
SELECT 'T6 exp v6', version FROM packets;

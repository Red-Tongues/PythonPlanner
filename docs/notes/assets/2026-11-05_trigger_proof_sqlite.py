import sqlite3
c=sqlite3.connect(':memory:'); c.isolation_level=None
c.executescript("""
PRAGMA recursive_triggers = ON;
CREATE TABLE packets(packet_id TEXT PRIMARY KEY, prompt_text TEXT, status TEXT NOT NULL DEFAULT 'draft',
 version INTEGER NOT NULL DEFAULT 1, fencing_token INTEGER NOT NULL DEFAULT 0, active_run_id TEXT,
 last_producer TEXT NOT NULL DEFAULT 'user_sql');
CREATE TABLE raw_revision_outbox(outbox_id INTEGER PRIMARY KEY, tx_id TEXT NOT NULL, packet_id TEXT NOT NULL,
 field_name TEXT NOT NULL, old_val TEXT, new_val TEXT, producer TEXT NOT NULL, captured_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TRIGGER trg_packets_bump_version AFTER UPDATE OF prompt_text, status ON packets FOR EACH ROW
WHEN (OLD.prompt_text IS NOT NEW.prompt_text OR OLD.status IS NOT NEW.status) AND OLD.version = NEW.version
BEGIN UPDATE packets SET version = OLD.version + 1, last_producer='user_sql' WHERE packet_id=NEW.packet_id; END;
CREATE TRIGGER trg_packets_audit_outbox AFTER UPDATE OF prompt_text, status ON packets FOR EACH ROW
WHEN (OLD.prompt_text IS NOT NEW.prompt_text OR OLD.status IS NOT NEW.status)
BEGIN
INSERT INTO raw_revision_outbox (tx_id, packet_id, field_name, old_val, new_val, producer)
WITH ctx AS (SELECT lower(hex(randomblob(8))) AS tx)
SELECT ctx.tx, NEW.packet_id, 'prompt_text', OLD.prompt_text, NEW.prompt_text, CASE WHEN OLD.version = NEW.version THEN 'user_sql' ELSE NEW.last_producer END FROM ctx WHERE OLD.prompt_text IS NOT NEW.prompt_text
UNION ALL
SELECT ctx.tx, NEW.packet_id, 'status', OLD.status, NEW.status, CASE WHEN OLD.version = NEW.version THEN 'user_sql' ELSE NEW.last_producer END FROM ctx WHERE OLD.status IS NOT NEW.status;
END;
INSERT INTO packets (packet_id, prompt_text, status, version, fencing_token) VALUES ('pkt-1','Initial prompt','draft',1,100);
""")
q=lambda s:c.execute(s).fetchall()
def st(l): print(l, q("select version,last_producer from packets"), q("select count(*),count(distinct tx_id) from raw_revision_outbox"))
c.execute("UPDATE packets SET prompt_text='Initial prompt', status='draft' WHERE packet_id='pkt-1'"); st('T1 noop  exp v1 0')
c.execute("UPDATE packets SET fencing_token=101, active_run_id='run-42', version=version+1 WHERE packet_id='pkt-1' AND version=1"); st('T2 cas   exp v2 0')
c.execute("UPDATE packets SET prompt_text=NULL, status='needs_review' WHERE packet_id='pkt-1'"); st('T3 cfo   exp v3 2/1')
c.execute("UPDATE packets SET prompt_text='Revised', last_producer='kernel_worker', version=version+1 WHERE packet_id='pkt-1' AND version=3"); st('T4 kern  exp v4 3')
c.execute("UPDATE packets SET status='ready' WHERE packet_id='pkt-1'"); st('T5 cfo-after-kernel exp v5')
print('T5 producer logged:', q("select producer from raw_revision_outbox order by outbox_id desc limit 1"))
print('sqlite', sqlite3.sqlite_version)

-- Administrative install only: deliberately separate from immutable source baselines.
-- __SERVICE_ROLE__ is replaced with a validated SQL identifier by the installer.
CREATE SCHEMA IF NOT EXISTS ubem_access;
REVOKE ALL ON SCHEMA ubem_access FROM PUBLIC;
CREATE TABLE IF NOT EXISTS ubem_access.invitation (
 cid text PRIMARY KEY CHECK (cid ~ '^[a-z][a-z0-9]{1,30}$'),
 role_oid oid NOT NULL,
 token_hash text,
 expires_at timestamptz NOT NULL,
 status text NOT NULL CHECK (status IN ('pending','active','disabled')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 activated_at timestamptz
);
REVOKE ALL ON ubem_access.invitation FROM PUBLIC;
CREATE OR REPLACE FUNCTION ubem_access.activate(p_cid text,p_token_hash text,p_verifier text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE invitation ubem_access.invitation%ROWTYPE;
BEGIN
 IF p_cid IS NULL OR p_cid !~ '^[a-z][a-z0-9]{1,30}$' OR
    p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' OR
    p_verifier IS NULL OR length(p_verifier)>1024 OR
    p_verifier !~ '^SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*:[A-Za-z0-9+/]+=*$' THEN
   RAISE EXCEPTION 'Invalid or expired invitation' USING ERRCODE='28000';
 END IF;
 SELECT * INTO invitation FROM ubem_access.invitation WHERE cid=p_cid FOR UPDATE;
 IF NOT FOUND OR invitation.status<>'pending' OR invitation.token_hash IS DISTINCT FROM p_token_hash
    OR invitation.expires_at<=clock_timestamp() THEN
   RAISE EXCEPTION 'Invalid or expired invitation' USING ERRCODE='28000';
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE oid=invitation.role_oid AND rolname=p_cid
    AND NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
    AND NOT rolreplication AND NOT rolbypassrls AND pg_has_role(oid,'ubem_readonly','MEMBER')
    AND NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=invitation.role_oid
      AND roleid<>(SELECT oid FROM pg_roles WHERE rolname='ubem_readonly'))) THEN
   RAISE EXCEPTION 'Account needs administrator review' USING ERRCODE='28000';
 END IF;
 -- Only the invited, still-disabled account can be changed; plaintext never enters SQL.
 EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L',p_cid,p_verifier);
 UPDATE ubem_access.invitation SET status='active',token_hash=NULL,activated_at=clock_timestamp()
   WHERE cid=p_cid;
 RETURN p_cid;
END;
$$;
REVOKE ALL ON FUNCTION ubem_access.activate(text,text,text) FROM PUBLIC;
GRANT USAGE ON SCHEMA ubem_access TO __SERVICE_ROLE__;
GRANT EXECUTE ON FUNCTION ubem_access.activate(text,text,text) TO __SERVICE_ROLE__;

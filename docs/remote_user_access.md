> This guide describes the existing deployed schema 0.0.4 onboarding service.
> For the new GIS lake, see [GIS-first access and rollout](gis_first_lake.md).
> The existing private hostname/port can serve another database in the same
> PostgreSQL instance; grant reader access and select that database in QGIS.

# CID registration and database access over Tailscale

Configure once, then run:

```sh
sudo python3 /home/csd-lab/epsm_db/scripts/manage_db_access.py
```

On the first run, missing configuration starts the guided setup automatically.
Use the absolute command above from any working directory.

Enter a Chalmers CID such as `ssanjay`. The script creates the PostgreSQL account
`ssanjay` and emails `ssanjay@chalmers.se`. The recipient connects to Tailscale,
accepts the laptop share if necessary, opens a one-time HTTPS link, and chooses a
password. The account activates and the user receives the host, port, database,
username, and QGIS/Python instructions. The chosen password is never emailed.

Participants use ordinary PostgreSQL connections through Tailscale. They do not
need Linux accounts or SSH keys. IT involvement, campus DNS changes, and a paid
public tunnel are not required for this setup.

## One-time setup

The laptop already has Tailscale, MagicDNS, and the stable name:

```text
csd-lab-precision-7540.tailb64966.ts.net
```

The installer discovers the actual node identity/name with `tailscale status`,
so it does not ask the administrator to invent or maintain hostnames.

1. Decide how users receive Tailscale access. For people outside your existing
   tailnet, share **only this laptop** from the Tailscale Machines page. To keep
   daily registration to one CID, create a fresh **reusable device-share link**:
   select this laptop → Share → Copy invite link → Reusable link. Paste it into
   the setup prompt, which hides the link like a password. Every invitation email
   will include that link. Leave it blank only if recipients already have access.

   The recipient installs Tailscale and signs into their own account before
   accepting the share. Their Tailscale login can differ from their Chalmers CID;
   the database account is always the CID registered by the administrator.

2. Review Tailscale access controls. Share recipients need only TCP **443** for
   password setup and TCP **55433** for PostgreSQL. Preserve your existing admin
   and SSH access. New grants are additive: a broad existing allow-everything rule
   can also grant access, so adding a narrow rule does not override broader ones.
   A policy fragment for external share recipients is:

   ```json
   {
     "grants": [
       {
         "src": ["autogroup:shared"],
         "dst": ["100.117.53.118"],
         "ip": ["tcp:443", "tcp:55433"]
       }
     ]
   }
   ```

   This is a fragment to merge/review with your existing policy, not a complete
   replacement. Tailnet members who are not share recipients need their own
   equivalent grant. The script does not overwrite the tailnet policy.

3. On the database laptop, run:

   ```sh
   sudo python3 /home/csd-lab/epsm_db/scripts/manage_db_access.py setup
   ```

   Enter your sudo password in your own terminal. The script asks for the optional
   device-share link, target database (default `sweden_ubem_v004_baseline`), email
   sender, and administrator email. For this deployment the latter two can both
   be `ssanjay@chalmers.se`, provided Chalmers SMTP accepts that sender.

   It displays the private endpoints and asks you to type `setup` before installing.
   Tailscale HTTPS certificates must be enabled. If the CLI prints an enablement
   URL, open it while signed into the tailnet administrator account and enable
   HTTPS, then continue/retry setup as directed. First certificate issuance can take
   a minute; setup retries transient readiness failures for approximately two minutes.
   This is a Tailscale account setting,
   not a Chalmers IT request. This host currently has no HTTPS certificate domain
   enabled, so expect that one-time step.

4. Verify from a second authorized Tailscale device before inviting a cohort.
   On the host the local readiness command is:

   ```sh
   sudo python3 /home/csd-lab/epsm_db/scripts/manage_db_access.py check
   ```

   The installer/check verifies its node identity, private Serve configuration,
   absence of public Funnel on these ports, the HTTPS health endpoint, and a real
   PostgreSQL SCRAM/SASL authentication greeting. Passing on the host alone does
   not prove a recipient's sharing/policy settings permit access.

## What setup installs

A root-owned Flask/Gunicorn application under `/opt/ubem-enrollment` runs as the
non-login **service account** `ubem-enrollment`, listening on `127.0.0.1:18080`.
This is the only Linux account used for enrollment; participants get database
roles only. Runtime configuration lives in `/etc/ubem-access`.

Tailscale Serve publishes two private routes:

```sh
tailscale serve --bg --yes --https=443 http://127.0.0.1:18080
tailscale serve --bg --yes --tcp=55433 tcp://127.0.0.1:55433
```

The setup script executes these after installation. It refuses to overwrite a
conflicting service on either port or publish through an existing public Funnel
route. It saves the prior Serve configuration privately for reference and leaves
unrelated routes intact. It does not restart PostgreSQL or change Docker's port
bindings. The existing database stays on the loopback address.

The password page uses HTTPS with Tailscale-managed certificates. Database traffic
is encrypted/authenticated by Tailscale's network layer; **PostgreSQL SSL mode is
`disable`** for this connection. That setting is specific to the private Tailscale
hostname and must not be reused for a public/Wi-Fi PostgreSQL address. This avoids
restarting the active database or managing an additional database certificate.
PostgreSQL still requires the user's own SCRAM-authenticated database password.
The readiness probe explicitly rejects an endpoint that trusts clients without
password authentication.

The config/email helpers refuse the Tailscale SSL setting if the database hostname
no longer matches the configured node identity. Registration checks that the node
and private routes are still correct. Keep Tailscale connected for QGIS/Python.

No public service is deployed by this script. Root installation still requires the
administrator's sudo password, and Tailscale policy/sharing/HTTPS settings remain
under the administrator's own Tailscale account.

## Registering users

Run the command with no arguments and enter the CID, or specify it directly:

```sh
sudo python3 /home/csd-lab/epsm_db/scripts/manage_db_access.py register ssanjay
```

The PostgreSQL role starts with `NOLOGIN` and no password. The email contains
Tailscale installation/sharing instructions plus a random one-use password setup
link valid for **24 hours**. Only its SHA-256 digest is stored in PostgreSQL.
Possession of the registered Chalmers mailbox/link authorizes password choice.
Protect the invitation; do not paste it into chat, logs, or screenshots.

The page accepts 12–256 characters with matching confirmation. libpq prepares a
SCRAM verifier before the database call, so plaintext never enters SQL. Activation
locks the invitation record, verifies expiry, role identity and restricted state,
sets the verifier, enables login, and consumes the invitation in one transaction.
A used/expired link cannot reset an active account, even with simultaneous requests.

The confirmation page shows the connection fields even if confirmation email
submission fails. The email contains a `getpass`/`psycopg.connect` example and QGIS
fields. The chosen password is not emailed, displayed again, or persisted by the
application. PostgreSQL stores only its SCRAM verifier.

For the current host, the connection fields are:

| Field | Value |
|---|---|
| Host | `csd-lab-precision-7540.tailb64966.ts.net` |
| Port | `55433` |
| Database | `sweden_ubem_v004_baseline` |
| Username | The registered CID |
| Password | The user's chosen password |
| PostgreSQL SSL mode | Disable; Tailscale encrypts the connection |

Use the full MagicDNS name, especially for shared devices. The `epsm_db` package
is installed separately in the user's Python environment; DB registration does
not grant access to the private GitHub repository.

## QGIS mapping layers

Refresh the PostgreSQL connection in the Browser panel, then expand **qgis**.
Drag **footprints** onto the map for building polygons, or **component_heights**
for component polygons with height attributes already joined. The geometry column
is `geom`, explicitly declared as 2D MultiPolygon in EPSG:3006 (SWEREF 99 TM).
No manual geometry type, CRS or feature-ID selection is needed when adding these
views with the normal QGIS defaults.

The first attribute, `fid`, encodes both dataset and building/component identity
as a JSON array string. It is stable and unique by the underlying source keys,
unlike the repeated `dataset_id` or an unstable row-number identifier. QGIS uses
this first attribute automatically for a view. `component_heights` retains
components without height evidence, with NULL height values and
`height_record_available=false`. Height values are attributes in metres; the map
geometry is 2D. Polygonal parts are projected for mapping; original geometries
remain available in `mart`/`stock`.

The views are live projections over the existing `mart` views. They do not copy
national geometry, seal datasets, or imply that processing/acceptance is complete.
They share the datasets already exposed by `mart`, rather than the currently
empty `_current` views. Readers inherit access through `ubem_readonly`.

New onboarding installations create them automatically. To add/update them on an
existing configured database without reinstalling enrollment:

```bash
sudo python3 /home/csd-lab/epsm_db/scripts/manage_db_access.py mapping
```

For another installation, run `sql/access/qgis.sql` as the database owner in a
transaction after creating the standard `mart` views and `ubem_readonly` group.
Existing unavailable QGIS layers retain their old settings; remove them and add
these new layers after refreshing the connection.

References: [QGIS view identifiers](https://docs.qgis.org/3.44/en/docs/user_manual/managing_data_source/opening_data.html),
[PostGIS geometry metadata](https://postgis.net/docs/using_postgis_dbmanagement.html#geometry_columns).

## Share-link lifecycle and revocation

Tailscale reusable device-share links expire after 30 days and support up to 1,000
acceptances. Paste a **freshly created** link during setup. The script uses a
conservative 29-day stored deadline and stops issuing invitations when less than
24 hours remain. If the link is already old when pasted, it can expire earlier;
there is no API token configured to query the true server-side expiry/use count.
Rerun setup with a fresh link before expiry or when Tailscale reports it exhausted.
Reusing the same link during setup does not extend its stored deadline.

The reusable link is a bearer invitation to the laptop, so treat it as a secret
and restrict shared-user ports as above. It does not contain a database password
or activate any database role. Revoke leaked links/shares in the Tailscale console.
Do not distribute a shared Tailscale login or an auth key for user devices.

Disabling a PostgreSQL account does not revoke its Tailscale share. Remove that
share separately when the person should no longer reach the laptop at all.
Tailscale device key expiry also needs normal server administration; this host's
current device key expires on 22 March 2027 unless its expiry setting changes.

## Password reset and disabling

Renew an expired password link or reset a managed user:

```sh
sudo python3 /home/csd-lab/epsm_db/scripts/manage_db_access.py reset ssanjay
```

After confirmation, existing database sessions are terminated and the account is
disabled until the user chooses a new password through a fresh emailed link. The
script checks the recorded PostgreSQL role OID before touching an existing role.
It does not take over an arbitrary existing PostgreSQL account.

Disable database access:

```sh
sudo python3 /home/csd-lab/epsm_db/scripts/manage_db_access.py disable ssanjay
```

The role/state are retained for audit; login, password, and invitation are disabled
and existing database sessions terminated. A later administrator-authorized reset
can reactivate the same identity. Revoke Tailscale access separately if needed.

## Mail and access boundaries

The already-configured local Postfix submits mail through Chalmers SMTP. The mail
setup helper defaults to `smtp.chalmers.se:587`, with verified STARTTLS before
credential entry. The sender must be authorized by those SMTP credentials.

```sh
sudo postqueue -p
sudo journalctl -u postfix --since '10 min ago'
sudo tail -n 40 /var/log/mail.log
```

Queue acceptance is reported as queued, not delivered. Look for the intended
recipient and `status=sent` in mail logs; relay acceptance is not a guarantee of
inbox placement. Failed submissions remain private `.eml` drafts under
`/etc/ubem-access/outbox` (invitations) or
`/var/lib/ubem-enrollment/outbox` (confirmations). Those files can contain secret
invitation/share links and should not be pasted into logs or chats.

New participant accounts allow up to 20 simultaneous PostgreSQL connections. QGIS
keeps multiple connections open for browsing and loading layers; the previous
limit of three could prevent layers from opening. Existing accounts retain their
limit until an administrator changes it with `ALTER ROLE "cid" CONNECTION LIMIT 20`.

Participants inherit `ubem_readonly` plus the two catalog metadata table reads the
Python API requires. They can read all granted `mart` datasets in the target DB;
this is not municipality-specific or per-database network isolation. No writes,
role administration, owner, Docker, or Linux privileges are granted. PostgreSQL
roles are cluster-wide, and default PUBLIC CONNECT/TEMP permissions may permit
connections/temporary objects in other databases without private-table access.

The web service has no root, sudo, Docker, account-management, or invitation-table
read privileges. It can call only a constrained owner-controlled activation
function with a fixed safe search path. That function validates the token, expiry,
account OID, and nonprivileged role state/membership. Browser tokens use URL
fragments (not access-log paths), then a JSON POST. Responses disable caching and
referrers and apply host/origin/CSP restrictions. Do not enable request-body or
email-body logging. Keep the application and dependencies maintained.

Source import completion does not establish acceptance/publication of a processed
dataset. Check dataset readiness before treating the contents as analysis-ready.

## Verification and current status

The focused tests include disposable PostgreSQL login/read-only enforcement,
expired/reused/racing links, resets, service privilege restrictions, invitation
validation, mail failure handling, Tailscale identity/Serve/Funnel checks, and
rejection of trust authentication. Test databases and synthetic roles are removed.

The administrator has installed the backend and enabled private Tailscale Serve.
On 15 October 2026, the agent verified HTTP 200 from the trusted HTTPS health
endpoint and a SCRAM/SASL authentication greeting through the private database port.
Initial setup's short timeout occurred while the first certificate was being
issued; the certificate is now present, and setup has been updated to retry that
transient condition. No database restart or setup reinstall is needed for that error.

A real invitation and QGIS/Python connection from a second authorized device remain
the final end-to-end checks. No real onboarding email has been sent by the tests.

References: [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve),
[device sharing](https://tailscale.com/docs/features/sharing).

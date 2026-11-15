# Runbook: provisioning

1. `make validate` must pass. It checks, in order: config schema, secrets, local Tailscale state, DNS (each FQDN must resolve to a Tailscale address), `tailscale ping`, SSH, sudo, node Tailscale state, peer DNS from each node, OS/arch.
2. `make provision`. Safe to re-run; all tasks are idempotent.
3. On "datadir that this lab did not create" (or a `halab` cluster the lab did not create): the node already has that database. Decide whether the data matters. Adopt with `ARGS="-e <engine>_adopt_existing=true --limit <node>"` (`mysql_adopt_existing`, `mariadb_adopt_existing` or `postgres_adopt_existing`) or use a clean server.
4. On the exporter's up metric `!= 1` (MySQL/MariaDB: `mysql_up`, PostgreSQL: `pg_up`): exporter credentials mismatch. Re-run `make configure-exporters` after fixing `db_exporter_password`.
5. `make monitoring`, then `make metrics-check`. All targets must be `up`.

## Rolling a server config change

```bash
make provision ARGS="--limit db-02 --diff"   # replicas first
make provision ARGS="--limit db-01 --diff"   # primary last
```

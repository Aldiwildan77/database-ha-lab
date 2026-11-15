# Runbook: provisioning

1. `make validate` must pass. It checks, in order: config schema, secrets, local Tailscale state, DNS (each FQDN must resolve to a Tailscale address), `tailscale ping`, SSH, sudo, node Tailscale state, peer DNS from each node, OS/arch.
2. `make provision`. Safe to re-run; all tasks are idempotent.
3. On "datadir that this lab did not create": the node already has MySQL. Decide whether the data matters. Adopt with `ARGS="-e mysql_adopt_existing=true --limit <node>"` or use a clean server.
4. On `mysql_up != 1`: exporter credentials mismatch. Re-run `make configure-exporters` after fixing `mysql_exporter_password`.
5. `make monitoring`, then `make metrics-check`. All targets must be `up`.

## Rolling a my.cnf change

```bash
make provision ARGS="--limit mysql-02 --diff"   # replicas first
make provision ARGS="--limit mysql-01 --diff"   # primary last
```

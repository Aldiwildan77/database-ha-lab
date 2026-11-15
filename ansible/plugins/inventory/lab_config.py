# -*- coding: utf-8 -*-
"""Inventory plugin that builds the Ansible inventory from the lab's
user-facing configuration files (config/inventory.yml, config/ha.yml,
config/monitoring.yml).

It is the single place where user configuration is parsed and validated, so
every Ansible command fails early with a clear list of problems instead of
failing halfway through a play.
"""

from __future__ import annotations

DOCUMENTATION = r"""
name: lab_config
short_description: Build inventory from mysql-ha-lab config/*.yml
description:
  - Reads config/inventory.yml, config/ha.yml and config/monitoring.yml,
    validates them and exposes nodes as hosts in the C(mysql_nodes) group.
options:
  plugin:
    description: Name of the plugin.
    required: true
    choices: ['lab_config']
  config_dir:
    description: Directory holding the lab configuration, relative to this inventory source.
    default: ../../config
"""

import ipaddress
import os
import re

from ansible.errors import AnsibleParserError
from ansible.plugins.inventory import BaseInventoryPlugin

HA_PROVIDERS = {
    # provider: (minimum nodes, maximum nodes or None, phase status)
    "async-replication": (2, None),
    "orchestrator": (3, None),
    "semi-sync": (2, None),
    "innodb-cluster": (3, 9),
    "percona-xtradb-cluster": (3, None),
}
NODE_ROLES = {"primary", "replica"}
MYSQL_VERSIONS = {"8.0", "8.4"}
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
FQDN_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}\.?$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def is_tailscale_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip in TAILSCALE_V4 or ip in TAILSCALE_V6


def group_safe(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value)


class InventoryModule(BaseInventoryPlugin):
    NAME = "lab_config"

    def verify_file(self, path):
        return super().verify_file(path) and os.path.basename(path).startswith("lab_config")

    def _load(self, path, required=True):
        if not os.path.isfile(path):
            if required:
                raise AnsibleParserError(
                    f"lab_config: missing {path}. Run `make init` and fill it in."
                )
            return {}
        data = self.loader.load_from_file(path, cache="none")
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise AnsibleParserError(f"lab_config: {path} must contain a YAML mapping")
        return data

    def parse(self, inventory, loader, path, cache=False):
        super().parse(inventory, loader, path, cache)
        cfg = self._read_config_data(path)  # noqa: F841 (populates self.get_option)
        config_dir = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(path)), self.get_option("config_dir"))
        )
        lab_root = os.path.dirname(config_dir)

        inv = self._load(os.path.join(config_dir, "inventory.yml"))
        ha = self._load(os.path.join(config_dir, "ha.yml"))
        mon = self._load(os.path.join(config_dir, "monitoring.yml"), required=False)
        bench = self._load(os.path.join(config_dir, "benchmark.yml"), required=False)

        errors: list[str] = []
        warnings: list[str] = []
        nodes = self._validate(inv, ha, mon, errors, warnings)
        if errors:
            msg = "\n  - ".join(errors)
            raise AnsibleParserError(f"lab_config: configuration is invalid:\n  - {msg}")
        for w in warnings:
            self.display.warning(f"lab_config: {w}")

        env = inv.get("environment") or {}
        ssh = inv.get("ssh") or {}
        mysql = ha.get("mysql") or {}
        ha_mon = ha.get("monitoring") or {}

        self.inventory.add_group("mysql_nodes")
        all_vars = {
            "lab_root": lab_root,
            "lab_config_dir": config_dir,
            "lab_environment": env.get("name", "lab"),
            "lab_network": env.get("network", "tailscale"),
            "ha_provider": ha["ha_provider"],
            "mysql_version": str(mysql.get("version", "8.4")),
            "mysql_port": int(mysql.get("port", 3306)),
            "mysql_config": mysql.get("config") or {},
            "monitoring_enabled": bool(ha_mon.get("enabled", True)),
            "mysql_exporter_port": int(ha_mon.get("mysql_exporter_port", 9104)),
            "node_exporter_port": int(ha_mon.get("node_exporter_port", 9100)),
            "lab_collector_port": int(ha_mon.get("lab_collector_port", 9106)),
            "lab_monitoring": mon,
            "lab_ha": ha,
            "lab_benchmark": bench,
            "lab_node_count": len(nodes),
        }
        for k, v in all_vars.items():
            self.inventory.set_variable("mysql_nodes", k, v)
        # Also expose to implicit localhost through the "all" group.
        for k, v in all_vars.items():
            self.inventory.set_variable("all", k, v)

        for idx, node in enumerate(nodes, start=1):
            name = node["name"]
            node_ssh = {**ssh, **(node.get("ssh") or {})}
            self.inventory.add_host(name, group="mysql_nodes")
            for grp in (
                f"dc_{group_safe(node['datacenter'])}",
                f"role_{node['role']}",
            ):
                self.inventory.add_group(grp)
                self.inventory.add_child("mysql_nodes", grp)
                self.inventory.add_host(name, group=grp)

            hostvars = {
                "ansible_host": node["fqdn"].rstrip("."),
                "ansible_user": node_ssh.get("user"),
                "ansible_port": int(node_ssh.get("port", 22)),
                "node_name": name,
                "node_fqdn": node["fqdn"].rstrip("."),
                "node_role": node["role"],
                "node_datacenter": node["datacenter"],
                "node_server_id": int(node.get("server_id", idx)),
                "node_tailscale_ip": node.get("tailscale_ip", ""),
                "mysql_node_config": node.get("mysql") or {},
            }
            if node_ssh.get("private_key"):
                hostvars["ansible_ssh_private_key_file"] = os.path.expanduser(node_ssh["private_key"])
            if node_ssh.get("become_password_env"):
                hostvars["ansible_become_password"] = os.environ.get(node_ssh["become_password_env"], "")
            for k, v in hostvars.items():
                self.inventory.set_variable(name, k, v)

    # ------------------------------------------------------------------ #
    def _validate(self, inv, ha, mon, errors, warnings):
        env = inv.get("environment")
        if not isinstance(env, dict) or not env.get("name"):
            errors.append("inventory.yml: environment.name is required")
        elif not LABEL_RE.match(str(env["name"])):
            errors.append("inventory.yml: environment.name must match [a-z0-9][a-z0-9_-]*")
        if isinstance(env, dict) and env.get("network", "tailscale") != "tailscale":
            errors.append("inventory.yml: environment.network must be 'tailscale' (the only supported network)")

        ssh = inv.get("ssh")
        if not isinstance(ssh, dict) or not ssh.get("user"):
            errors.append("inventory.yml: ssh.user is required")
        else:
            key = ssh.get("private_key")
            if key and not os.path.isfile(os.path.expanduser(key)):
                errors.append(f"inventory.yml: ssh.private_key '{key}' does not exist on this machine")
            if not isinstance(ssh.get("port", 22), int):
                errors.append("inventory.yml: ssh.port must be an integer")

        nodes = inv.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            errors.append("inventory.yml: nodes must be a non-empty list")
            nodes = []

        names, fqdns, ids, ips = set(), set(), set(), set()
        for i, node in enumerate(nodes):
            where = f"inventory.yml: nodes[{i}]"
            if not isinstance(node, dict):
                errors.append(f"{where} must be a mapping")
                continue
            for field in ("name", "fqdn", "role", "datacenter"):
                if not node.get(field):
                    errors.append(f"{where}.{field} is required")
            name = str(node.get("name", ""))
            where = f"inventory.yml: node '{name or i}'"
            if name:
                if not NAME_RE.match(name):
                    errors.append(f"{where}: name must be lowercase alphanumeric/hyphen (DNS label)")
                if name in names:
                    errors.append(f"{where}: duplicate node name")
                names.add(name)
            fqdn = str(node.get("fqdn", "")).rstrip(".").lower()
            if fqdn:
                if not FQDN_RE.match(fqdn):
                    errors.append(f"{where}: fqdn '{fqdn}' is not a valid fully qualified domain name")
                elif fqdn.endswith("example.ts.net"):
                    errors.append(f"{where}: fqdn '{fqdn}' is still the example value; set your MagicDNS name")
                if fqdn in fqdns:
                    errors.append(f"{where}: duplicate fqdn '{fqdn}'")
                fqdns.add(fqdn)
            role = node.get("role")
            if role and role not in NODE_ROLES:
                errors.append(f"{where}: role '{role}' must be one of {sorted(NODE_ROLES)}")
            dc = str(node.get("datacenter", ""))
            if dc and not LABEL_RE.match(dc):
                errors.append(f"{where}: datacenter '{dc}' must match [a-z0-9][a-z0-9_-]*")
            sid = node.get("server_id", i + 1)
            if not isinstance(sid, int) or not (1 <= sid <= 4294967295):
                errors.append(f"{where}: server_id must be an integer in 1..4294967295")
            elif sid in ids:
                errors.append(f"{where}: duplicate server_id {sid}")
            else:
                ids.add(sid)
            tip = node.get("tailscale_ip")
            if tip:
                if not is_tailscale_ip(str(tip)):
                    errors.append(f"{where}: tailscale_ip '{tip}' is not in 100.64.0.0/10 or fd7a:115c:a1e0::/48")
                if tip in ips:
                    errors.append(f"{where}: duplicate tailscale_ip '{tip}'")
                ips.add(tip)
            if node.get("mysql") is not None and not isinstance(node.get("mysql"), dict):
                errors.append(f"{where}: mysql overrides must be a mapping of my.cnf options")
            if node.get("ssh") is not None and not isinstance(node.get("ssh"), dict):
                errors.append(f"{where}: ssh overrides must be a mapping")

        provider = ha.get("ha_provider")
        if provider not in HA_PROVIDERS:
            errors.append(f"ha.yml: ha_provider '{provider}' must be one of {sorted(HA_PROVIDERS)}")
        else:
            lo, hi = HA_PROVIDERS[provider]
            n = len(nodes)
            if n < lo:
                errors.append(f"ha.yml: {provider} needs at least {lo} nodes, inventory has {n}")
            if hi and n > hi:
                errors.append(f"ha.yml: {provider} supports at most {hi} nodes, inventory has {n}")
            primaries = [x.get("name") for x in nodes if isinstance(x, dict) and x.get("role") == "primary"]
            if len(primaries) != 1:
                errors.append(
                    f"inventory.yml: exactly one node must have role 'primary' "
                    f"(initial primary / bootstrap node), found {len(primaries)}"
                )
            if provider in ("innodb-cluster", "percona-xtradb-cluster", "orchestrator") and n % 2 == 0:
                warnings.append(f"{provider} with an even node count ({n}) does not improve quorum tolerance")

        mysql = ha.get("mysql") or {}
        if str(mysql.get("version", "8.4")) not in MYSQL_VERSIONS:
            errors.append(f"ha.yml: mysql.version must be one of {sorted(MYSQL_VERSIONS)}")
        ports = {}
        for key, default in (("port", 3306),):
            ports[f"mysql.{key}"] = mysql.get(key, default)
        hm = ha.get("monitoring") or {}
        ports["monitoring.mysql_exporter_port"] = hm.get("mysql_exporter_port", 9104)
        ports["monitoring.node_exporter_port"] = hm.get("node_exporter_port", 9100)
        ports["monitoring.lab_collector_port"] = hm.get("lab_collector_port", 9106)
        for k, v in ports.items():
            if not isinstance(v, int) or not (1 <= v <= 65535):
                errors.append(f"ha.yml: {k} must be an integer port")
        if len(set(ports.values())) != len(ports):
            errors.append(f"ha.yml: ports must be distinct: {ports}")

        if mon:
            mode = mon.get("scrape_address", "ip")
            if mode not in ("ip", "fqdn"):
                errors.append("monitoring.yml: scrape_address must be 'ip' or 'fqdn'")
            fw = mon.get("firewall") or {}
            if fw and not fw.get("manage", True):
                warnings.append(
                    "monitoring.yml: firewall.manage is false; MySQL and exporters listen on all "
                    "interfaces, so you must restrict them yourself"
                )
            for cidr in mon.get("allowed_sources") or []:
                try:
                    ipaddress.ip_network(str(cidr), strict=False)
                except ValueError:
                    errors.append(f"monitoring.yml: allowed_sources entry '{cidr}' is not a CIDR")
        return [n for n in nodes if isinstance(n, dict)]

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
    validates them and exposes nodes as hosts in the C(db_nodes) group (alias C(mysql_nodes)).
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

DEFAULT_ENGINE = "mysql"
# Core ports. Engine ports (database, engine exporter, HA components) come from ansible/engines/<engine>/engine.yml.
# Lab defaults deliberately differ from each product's standard port so the lab never collides with an
# existing installation on the same host.
CORE_PORTS = {
    "node_exporter": 19100,
    "lab_collector": 19106,
    "iperf": 15201,
}
NODE_ROLES = {"primary", "replica"}
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*\.?$")
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
TAILSCALE_CIDRS = [str(TAILSCALE_V4), str(TAILSCALE_V6)]
# type: (default interface, default cidrs, encrypted by default)
NETWORK_TYPES = {
    "tailscale": ("tailscale0", TAILSCALE_CIDRS, True),
    "vpn": (None, None, True),        # WireGuard/OpenVPN/IPsec managed outside the lab
    "private": (None, None, False),   # plain internal network: TLS is enforced by the lab
}


def is_tailscale_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip in TAILSCALE_V4 or ip in TAILSCALE_V6


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def in_cidrs(value: str, cidrs) -> bool:
    ip = ipaddress.ip_address(value)
    return any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs)


def mysql_host_pattern(cidr: str):
    """IPv4 CIDR -> MySQL 'net/mask' host pattern (works on 8.0 and 8.4). IPv6 has no netmask form."""
    net = ipaddress.ip_network(cidr, strict=False)
    if net.version != 4:
        return None
    return str(net.network_address) if net.prefixlen == 32 else f"{net.network_address}/{net.netmask}"


def _dig(data: dict, dotted: str):
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def resolve_ports(ha: dict, mon: dict, engine: dict) -> dict:
    """Effective lab ports: config value or default (core ports plus the engine's own)."""
    hm = ha.get("monitoring") or {}
    ports = {
        "node_exporter": hm.get("node_exporter_port", CORE_PORTS["node_exporter"]),
        "lab_collector": hm.get("lab_collector_port", CORE_PORTS["lab_collector"]),
        "iperf": (mon or {}).get("iperf_port", CORE_PORTS["iperf"]),
    }
    for name, spec in (engine.get("ports") or {}).items():
        value = _dig(ha, spec["config"]) if spec.get("config") else None
        ports[name] = spec["default"] if value is None else value
    return ports


def port_value(token: str, ports: dict) -> int:
    """Engine descriptors name ports ('mysql', 'router_rw+1'); resolve against the effective lab ports."""
    name, _, plus = str(token).partition("+")
    return int(ports[name]) + (int(plus) if plus else 0)


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
        engines = self._engines(lab_root)
        engine_name = ha.get("db_engine", DEFAULT_ENGINE)
        engine = engines.get(engine_name)
        networks = self._networks(inv, errors)
        nodes = self._validate(inv, ha, mon, networks, engines, engine_name, errors, warnings)
        if errors:
            msg = "\n  - ".join(errors)
            raise AnsibleParserError(f"lab_config: configuration is invalid:\n  - {msg}")
        for w in warnings:
            self.display.warning(f"lab_config: {w}")

        env = inv.get("environment") or {}
        ssh = inv.get("ssh") or {}
        mysql = ha.get("mysql") or {}
        engine_cfg = ha.get(engine["config_section"]) or {}
        ha_mon = ha.get("monitoring") or {}

        used = {n["_network"] for n in nodes}
        node_cidrs = sorted({c for name in used for c in networks[name]["cidrs"]})
        extra_sources = [str(c) for c in (mon.get("allowed_sources") or [])]
        client_cidrs = sorted(set(node_cidrs) | set(extra_sources))
        client_hosts = sorted({h for h in (mysql_host_pattern(c) for c in client_cidrs) if h})

        provider = ha["ha_provider"]
        strategy = engine["ha"][provider]
        router = (ha.get("innodb_cluster") or {}).get("router") or {}
        ports = {k: int(v) for k, v in resolve_ports(ha, mon, engine).items()}
        mysql_port = ports[engine["primary_port"]]
        mesh_ports = [port_value(t, ports) for t in strategy["mesh_ports"]]
        client_ports = [port_value(t, ports) for t in strategy["client_ports"]]
        optional = strategy.get("optional_client_ports")
        if optional:
            section = ha.get(provider.replace("-", "_")) or {}
            if (section.get(optional["feature"]) or {}).get("enabled", True):
                client_ports += [port_value(t, ports) for t in optional["ports"]]
        ha_endpoint = {}
        spec = strategy.get("ha_endpoint")
        if spec:
            section = ha.get(provider.replace("-", "_")) or {}
            if not spec.get("feature") or (section.get(spec["feature"]) or {}).get("enabled", True):
                ha_endpoint = {"port": port_value(spec["port"], ports), "target": spec["target"], "mode": spec["mode"]}
        monitoring_ports = [ports["node_exporter"], ports[engine["exporter_port"]], ports["lab_collector"]]

        self.inventory.add_group("db_nodes")
        # Older name kept as an alias so existing ad-hoc commands (-l mysql_nodes) still work.
        self.inventory.add_group("mysql_nodes")
        self.inventory.add_child("mysql_nodes", "db_nodes")
        all_vars = {
            "lab_root": lab_root,
            "lab_config_dir": config_dir,
            "lab_environment": env.get("name", "lab"),
            "lab_networks": {k: v for k, v in networks.items() if k in used},
            "lab_uses_tailscale": any(networks[n]["type"] == "tailscale" for n in used),
            "lab_mixed_networks": len(used) > 1,
            "lab_unencrypted_networks": sorted(n for n in used if not networks[n]["encrypted"]),
            "lab_allowed_sources": client_cidrs,
            "db_engine": engine_name,
            "lab_engine_dir": engine["_dir"],
            "lab_engine_tasks": os.path.join(engine["_dir"], "tasks"),
            "lab_engine": engine,
            "lab_ha_strategy": strategy,
            "lab_ha_endpoint": ha_endpoint,
            "lab_ha_min_nodes": strategy["min_nodes"],
            "lab_mesh_tcp_ports": mesh_ports,
            "lab_mesh_udp_ports": [],
            "lab_client_tcp_ports": client_ports,
            "lab_monitoring_tcp_ports": monitoring_ports if bool(ha_mon.get("enabled", True)) else [],
            "mysql_client_hosts": client_hosts,
            "ha_provider": ha["ha_provider"],
            "db_version": str(engine_cfg.get("version", engine["default_version"])),
            "mysql_version": str(engine_cfg.get("version", engine["default_version"])),
            "mysql_port": mysql_port,
            "lab_ports": ports,
            "orchestrator_http_port": ports.get("orchestrator_http"),
            "orchestrator_raft_port": ports.get("orchestrator_raft"),
            "router_rw_port": ports.get("router_rw"),
            "pxc_sst_port": ports.get("pxc_sst"),
            "pxc_gcomm_port": ports.get("pxc_gcomm"),
            "pxc_ist_port": ports.get("pxc_ist"),
            "lab_iperf_port": ports["iperf"],
            "mysql_config": mysql.get("config") or {},
            "db_config": engine_cfg.get("config") or {},
            "monitoring_enabled": bool(ha_mon.get("enabled", True)),
            "mysql_exporter_port": ports.get("mysql_exporter"),
            "db_port": mysql_port,
            "db_exporter_port": ports[engine["exporter_port"]],
            "db_exporter_role": engine["exporter"]["role"],
            "db_exporter_name": engine["exporter"]["name"],
            "db_exporter_up_metric": engine["exporter"]["up_metric"],
            "db_collector_module": engine["collector"]["module"],
            "node_exporter_port": ports["node_exporter"],
            "lab_collector_port": ports["lab_collector"],
            "lab_monitoring": mon,
            "lab_ha": ha,
            "lab_benchmark": bench,
            "lab_node_count": len(nodes),
        }
        for k, v in all_vars.items():
            self.inventory.set_variable("db_nodes", k, v)
        # Also expose to implicit localhost through the "all" group.
        for k, v in all_vars.items():
            self.inventory.set_variable("all", k, v)

        for idx, node in enumerate(nodes, start=1):
            name = node["name"]
            node_ssh = {**ssh, **(node.get("ssh") or {})}
            self.inventory.add_host(name, group="db_nodes")
            for grp in (
                f"dc_{group_safe(node['datacenter'])}",
                f"role_{node['role']}",
                f"net_{group_safe(node['_network'])}",
            ):
                self.inventory.add_group(grp)
                self.inventory.add_child("db_nodes", grp)
                self.inventory.add_host(name, group=grp)

            net = networks[node["_network"]]
            address = node["_address"]
            hostvars = {
                "ansible_host": address,
                "ansible_user": node_ssh.get("user"),
                "ansible_port": int(node_ssh.get("port", 22)),
                "node_name": name,
                "node_fqdn": address,
                "node_address": address,
                "node_network": node["_network"],
                "node_network_type": net["type"],
                "node_interface": net["interface"],
                "node_network_cidrs": net["cidrs"],
                "node_network_encrypted": net["encrypted"],
                "node_role": node["role"],
                "node_datacenter": node["datacenter"],
                "node_server_id": int(node.get("server_id", idx)),
                "node_tailscale_ip": node.get("tailscale_ip", ""),
                "mysql_node_config": node.get("mysql") or {},
                "db_node_config": node.get(engine["config_section"]) or {},
            }
            if node_ssh.get("private_key"):
                hostvars["ansible_ssh_private_key_file"] = os.path.expanduser(node_ssh["private_key"])
            if node_ssh.get("become_password_env"):
                hostvars["ansible_become_password"] = os.environ.get(node_ssh["become_password_env"], "")
            for k, v in hostvars.items():
                self.inventory.set_variable(name, k, v)

    # ------------------------------------------------------------------ #
    def _engines(self, lab_root):
        """Engine descriptors from ansible/engines/<engine>/engine.yml, keyed by name."""
        edir = os.path.join(lab_root, "ansible", "engines")
        out = {}
        if not os.path.isdir(edir):
            raise AnsibleParserError(f"lab_config: engine descriptors missing: {edir}")
        for sub in sorted(os.listdir(edir)):
            fname = os.path.join(sub, "engine.yml")
            if not os.path.isfile(os.path.join(edir, fname)):
                continue
            data = self.loader.load_from_file(os.path.join(edir, fname), cache="none")
            if not isinstance(data, dict) or not data.get("name") or not isinstance(data.get("ha"), dict):
                raise AnsibleParserError(f"lab_config: {fname} is not a valid engine descriptor (name and ha required)")
            self._check_descriptor(fname, data)
            if data["name"] != sub:
                raise AnsibleParserError(f"lab_config: {fname}: name '{data['name']}' must match its folder '{sub}'")
            data["_dir"] = os.path.join(edir, sub)
            out[data["name"]] = data
        return out

    @staticmethod
    def _check_descriptor(fname, data):
        """Fail early on descriptor typos: unknown port names, missing keys."""
        known = set((data.get("ports") or {}))
        for key in ("exporter", "collector"):
            if not data.get(key):
                raise AnsibleParserError(f"lab_config: {fname}: {key} is required")
        for key in ("module", "run_as", "connect"):
            if not (data["collector"] or {}).get(key):
                raise AnsibleParserError(f"lab_config: {fname}: collector.{key} is required")
        for key in ("role", "name", "up_metric"):
            if not (data["exporter"] or {}).get(key):
                raise AnsibleParserError(f"lab_config: {fname}: exporter.{key} is required")
        for key in ("primary_port", "exporter_port"):
            if data.get(key) not in known:
                raise AnsibleParserError(f"lab_config: {fname}: {key} '{data.get(key)}' is not in ports")
        for strat, spec in data["ha"].items():
            for key in ("family", "failover", "min_nodes", "mesh_ports", "client_ports", "provision", "deploy"):
                if key not in spec:
                    raise AnsibleParserError(f"lab_config: {fname}: ha.{strat}.{key} is required")
            tokens = list(spec["mesh_ports"]) + list(spec["client_ports"]) + list(
                (spec.get("optional_client_ports") or {}).get("ports", []))
            for token in tokens:
                if str(token).partition("+")[0] not in known and str(token).partition("+")[0] not in CORE_PORTS:
                    raise AnsibleParserError(f"lab_config: {fname}: ha.{strat} uses unknown port '{token}'")

    def _networks(self, inv, errors):
        raw = inv.get("networks")
        if raw is None:
            raw = {"tailscale": {"type": "tailscale"}}
        if not isinstance(raw, dict) or not raw:
            errors.append("inventory.yml: networks must be a mapping of name -> {type, interface, cidrs}")
            return {}
        out = {}
        for name, cfg in raw.items():
            where = f"inventory.yml: networks.{name}"
            cfg = cfg or {}
            if not LABEL_RE.match(str(name)):
                errors.append(f"{where}: name must match [a-z0-9][a-z0-9_-]*")
            ntype = cfg.get("type")
            if ntype not in NETWORK_TYPES:
                errors.append(f"{where}.type must be one of {sorted(NETWORK_TYPES)}")
                continue
            d_iface, d_cidrs, d_enc = NETWORK_TYPES[ntype]
            iface = cfg.get("interface", d_iface)
            cidrs = [str(c) for c in (cfg.get("cidrs") or d_cidrs or [])]
            if not iface:
                errors.append(f"{where}.interface is required for type {ntype} (e.g. eth1, wg0)")
            if not cidrs:
                errors.append(f"{where}.cidrs is required for type {ntype} (the subnet(s) the nodes use)")
            for c in cidrs:
                try:
                    ipaddress.ip_network(c, strict=False)
                except ValueError:
                    errors.append(f"{where}: cidr '{c}' is invalid")
            enc = cfg.get("encrypted", d_enc)
            if not isinstance(enc, bool):
                errors.append(f"{where}.encrypted must be true or false")
            out[str(name)] = {"type": ntype, "interface": iface, "cidrs": cidrs, "encrypted": bool(enc)}
        return out

    def _validate(self, inv, ha, mon, networks, engines, engine_name, errors, warnings):
        env = inv.get("environment")
        if not isinstance(env, dict) or not env.get("name"):
            errors.append("inventory.yml: environment.name is required")
        elif not LABEL_RE.match(str(env["name"])):
            errors.append("inventory.yml: environment.name must match [a-z0-9][a-z0-9_-]*")
        env = env if isinstance(env, dict) else {}
        default_net = env.get("network")
        if default_net is None and len(networks) == 1:
            default_net = next(iter(networks))
        if default_net is not None and networks and default_net not in networks:
            errors.append(f"inventory.yml: environment.network '{default_net}' is not defined in networks "
                          f"({sorted(networks)})")

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
            for field in ("name", "role", "datacenter"):
                if not node.get(field):
                    errors.append(f"{where}.{field} is required")
            if not (node.get("address") or node.get("fqdn")):
                errors.append(f"{where}.address is required (hostname, MagicDNS name or IP; 'fqdn' also accepted)")
            name = str(node.get("name", ""))
            where = f"inventory.yml: node '{name or i}'"
            if name:
                if not NAME_RE.match(name):
                    errors.append(f"{where}: name must be lowercase alphanumeric/hyphen (DNS label)")
                if name in names:
                    errors.append(f"{where}: duplicate node name")
                names.add(name)
            net_name = str(node.get("network") or default_net or "")
            net = networks.get(net_name)
            if not net_name:
                errors.append(f"{where}: network is required when several networks are defined")
            elif net is None:
                errors.append(f"{where}: network '{net_name}' is not defined in networks ({sorted(networks)})")
            node["_network"] = net_name
            fqdn = str(node.get("address") or node.get("fqdn") or "").rstrip(".").lower()
            node["_address"] = fqdn
            if fqdn:
                if not (is_ip(fqdn) or HOST_RE.match(fqdn)):
                    errors.append(f"{where}: address '{fqdn}' is not a valid hostname or IP address")
                elif fqdn.endswith("example.ts.net") or fqdn.endswith(".example.internal"):
                    errors.append(f"{where}: address '{fqdn}' is still the example value")
                elif net and is_ip(fqdn) and not in_cidrs(fqdn, net["cidrs"]):
                    errors.append(f"{where}: address {fqdn} is outside network '{net_name}' cidrs {net['cidrs']}")
                elif net and net["type"] == "tailscale" and is_ip(fqdn) and not is_tailscale_ip(fqdn):
                    errors.append(f"{where}: address {fqdn} is not a Tailscale address")
                if fqdn in fqdns:
                    errors.append(f"{where}: duplicate address '{fqdn}'")
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
            if tip and net and net["type"] != "tailscale":
                errors.append(f"{where}: tailscale_ip is only valid on a tailscale network")
            if tip:
                if not is_tailscale_ip(str(tip)):
                    errors.append(f"{where}: tailscale_ip '{tip}' is not in 100.64.0.0/10 or fd7a:115c:a1e0::/48")
                if tip in ips:
                    errors.append(f"{where}: duplicate tailscale_ip '{tip}'")
                ips.add(tip)
            section = (engines.get(engine_name) or {}).get("config_section", "mysql")
            for key in {"mysql", section}:
                if node.get(key) is not None and not isinstance(node.get(key), dict):
                    errors.append(f"{where}: {key} overrides must be a mapping of server options")
            if node.get("ssh") is not None and not isinstance(node.get("ssh"), dict):
                errors.append(f"{where}: ssh overrides must be a mapping")

        engine = engines.get(engine_name)
        provider = ha.get("ha_provider")
        if engine is None:
            errors.append(f"ha.yml: db_engine '{engine_name}' must be one of {sorted(engines)}")
        elif engine.get("status") == "planned":
            errors.append(f"ha.yml: db_engine '{engine_name}' is planned: its descriptor defines the strategies and "
                          f"ports, but the roles are not implemented yet. Ready engines: "
                          f"{sorted(n for n, e in engines.items() if e.get('status') != 'planned')}")
        elif provider not in engine["ha"]:
            errors.append(f"ha.yml: ha_provider '{provider}' is not supported by {engine_name}; "
                          f"choose one of {sorted(s for s, v in engine['ha'].items() if v.get('status') != 'planned')}")
        elif engine["ha"][provider].get("status") == "planned":
            errors.append(f"ha.yml: ha_provider '{provider}' is planned for {engine_name}: its descriptor defines it, "
                          f"but its roles are not implemented yet. Implemented: "
                          f"{sorted(s for s, v in engine['ha'].items() if v.get('status') != 'planned')}")
        else:
            strategy = engine["ha"][provider]
            lo, hi = strategy["min_nodes"], strategy.get("max_nodes")
            n = len(nodes)
            if n < lo:
                warnings.append(f"{provider} needs at least {lo} nodes, inventory has {n}: provisioning, monitoring "
                                f"and direct benchmarks work, make ha will refuse")
            if hi and n > hi:
                errors.append(f"ha.yml: {provider} supports at most {hi} nodes, inventory has {n}")
            primaries = [x.get("name") for x in nodes if isinstance(x, dict) and x.get("role") == "primary"]
            if len(primaries) != 1:
                errors.append(
                    f"inventory.yml: exactly one node must have role 'primary' "
                    f"(initial primary / bootstrap node), found {len(primaries)}"
                )
            if strategy.get("even_nodes_warning") and n % 2 == 0:
                warnings.append(f"{provider} with an even node count ({n}) does not improve quorum tolerance")

        used = {n.get("_network") for n in nodes if isinstance(n, dict) and n.get("_network") in networks}
        if len(used) > 1:
            warnings.append(
                f"nodes span several networks ({sorted(used)}); every node must reach every other node and this "
                "machine must reach every node (routing, VPN or Tailscale subnet router). make validate checks it."
            )
        plain = sorted(n for n in used if not networks[n]["encrypted"])
        repl = (ha.get("replication") or {})
        if plain and repl.get("ssl") is False:
            errors.append(f"ha.yml: replication.ssl cannot be false while nodes use unencrypted networks {plain}")
        pxc = ha.get("percona_xtradb_cluster") or {}
        if plain and ha.get("ha_provider") == "percona-xtradb-cluster" and pxc.get("encrypt_cluster_traffic") is False:
            errors.append(f"ha.yml: percona_xtradb_cluster.encrypt_cluster_traffic cannot be false on unencrypted "
                          f"networks {plain}")

        mysql = ha.get("mysql") or {}
        ratio = mysql.get("buffer_pool_ratio", 0.5)
        if not isinstance(ratio, (int, float)) or not (0.05 <= float(ratio) <= 0.9):
            errors.append("ha.yml: mysql.buffer_pool_ratio must be a number between 0.05 and 0.9")
        versions = (engine or {}).get("versions") or []
        section = (engine or {}).get("config_section", "mysql")
        if versions and str((ha.get(section) or {}).get("version", engine["default_version"])) not in versions:
            errors.append(f"ha.yml: {section}.version must be one of {versions}")
        ports = resolve_ports(ha, mon, engine or {})
        for k, v in ports.items():
            if not isinstance(v, int) or isinstance(v, bool) or not (1024 <= v <= 65534):
                errors.append(f"port '{k}' must be an integer between 1024 and 65534 (got {v!r})")
        spans = {k: int(((engine or {}).get("ports") or {}).get(k, {}).get("span", 1)) for k in ports}
        used_ports = [v + i for k, v in ports.items() if isinstance(v, int) for i in range(spans[k])]
        if len(set(used_ports)) != len(used_ports):
            errors.append(f"lab ports must be distinct (a span port reserves the next port too): {ports}")

        if mon:
            mode = mon.get("scrape_address", "ip")
            if mode not in ("ip", "fqdn"):
                errors.append("monitoring.yml: scrape_address must be 'ip' or 'fqdn'")
            fw = mon.get("firewall") or {}
            if fw.get("backend", "nftables") not in ("nftables", "ufw", "none"):
                errors.append("monitoring.yml: firewall.backend must be nftables, ufw or none")
            if fw and (not fw.get("manage", True) or fw.get("backend") == "none"):
                warnings.append(
                    "monitoring.yml: the lab does not manage the firewall; the database and exporters listen on all "
                    "interfaces, so restrict them with your own ACLs (make mesh-check still verifies reachability)"
                )
            for cidr in mon.get("allowed_sources") or []:
                try:
                    ipaddress.ip_network(str(cidr), strict=False)
                except ValueError:
                    errors.append(f"monitoring.yml: allowed_sources entry '{cidr}' is not a CIDR")
        return [n for n in nodes if isinstance(n, dict)]

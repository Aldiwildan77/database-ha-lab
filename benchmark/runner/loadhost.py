"""Runs JMeter on another machine (benchmark.yml load_host: <ssh target>), e.g. inside the database DC,
so client latency is not dominated by the operator's network. The JTL streams back over SSH, so the
live exporter, reports and ledger checks work unchanged."""

import os
import shlex
import subprocess

SSH = ["ssh", "-o", "BatchMode=yes"]   # run.py adds -i <load_host_key> when set


def base(host):
    return subprocess.run(SSH + [host, "echo $HOME"], capture_output=True, text=True, check=True).stdout.strip() + "/halab-jmeter"


def sync(host, root, b):
    subprocess.run(SSH + [host, "command -v java >/dev/null || (sudo apt-get update -qq && "
                    "sudo apt-get install -y -qq openjdk-17-jre-headless >/dev/null)"], check=True)
    rsh = ["-e", shlex.join(SSH)]
    subprocess.run(["rsync", "-a", "--delete", "--copy-links", *rsh, os.path.join(root, "local", "tools", "jmeter") + "/",
                    f"{host}:{b}/jmeter/"], check=True)
    subprocess.run(["rsync", "-a", "--delete", *rsh, "--exclude", "results", os.path.join(root, "jmeter") + "/",
                    f"{host}:{b}/lab/"], check=True)


def put_secret(host, path, b):
    subprocess.run(SSH + [host, f"umask 077; mkdir -p {b}/run && cat > {b}/run/secret.properties"],
                   stdin=open(path), check=True)


def remap(cmd, root, run_dir, b):
    """Local JMeter command -> the same command on the load host."""
    out = []
    for x in cmd:
        x = x.replace(os.path.join(run_dir, "secret.properties"), f"{b}/run/secret.properties")
        x = x.replace(run_dir, f"{b}/run")
        x = x.replace(os.path.join(root, "local", "tools", "jmeter"), f"{b}/jmeter")
        x = x.replace(os.path.join(root, "jmeter"), f"{b}/lab")
        out.append(x)
    return out


def run(host, cmd, root, run_dir, b, **kw):
    return subprocess.Popen(SSH + [host, f"cd {b}/run && {shlex.join(remap(cmd, root, run_dir, b))}"], **kw)


def follow(host, remote_jtl, local_jtl):
    """Streams the remote JTL into the local file while JMeter writes it."""
    return subprocess.Popen(SSH + [host, f"touch {remote_jtl}; tail -n +1 -F {remote_jtl}"],
                            stdout=open(local_jtl, "w"), stderr=subprocess.DEVNULL)


def fetch(host, remote, local):
    subprocess.run(["scp", "-q", *SSH[1:], f"{host}:{remote}", local], check=False)


def stop(host):
    subprocess.run(SSH + [host, "pkill -INT -f 'ApacheJMeter.jar' || true"], check=False)


def cleanup(host, b):
    subprocess.run(SSH + [host, f"rm -rf {b}/run"], check=False)

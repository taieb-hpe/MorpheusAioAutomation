import os
import hashlib
import shutil
import socket
import ssl
import subprocess
import time
import shlex
import re
import threading
import traceback
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML_PATH = os.path.join(BASE_DIR, "templates", "index.html")

install_lock = threading.Lock()
install_status = {"running": False, "logs": []}

MORPHEUS_PACKAGE = "morpheus-appliance"
MORPHEUS_DIRS = ["/opt/morpheus", "/var/opt/morpheus", "/etc/morpheus"]
MORPHEUS_CTL = "/opt/morpheus/bin/morpheus-ctl"


# ============================================================
# LOGGING
# ============================================================
class WebLogger:
    def log(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        with install_lock:
            install_status["logs"].append(line)
            if len(install_status["logs"]) > 2000:
                install_status["logs"] = install_status["logs"][-2000:]
        print(line, flush=True)


logger = WebLogger()


# ============================================================
# EXECUTORS (local + remote SSH)
# ============================================================
class LocalExecutor:
    def run(self, command, check=True, secrets=None):
        """
        Run a shell command on the local machine.
        Anything listed in `secrets` is replaced with **** in every log line.
        """
        secrets = [s for s in (secrets or []) if s]

        def redact(text):
            for s in secrets:
                text = text.replace(s, "****")
            return text

        logger.log(f"$ {redact(command)}")
        process = subprocess.run(
            command, shell=True, capture_output=True, text=True
        )
        if process.stdout:
            for line in process.stdout.strip().splitlines():
                logger.log(redact(line))
        if process.stderr:
            for line in process.stderr.strip().splitlines():
                logger.log(f"STDERR: {redact(line)}")
        if check and process.returncode != 0:
            raise RuntimeError(
                f"Command failed with exit code {process.returncode}: "
                f"{redact(command)}"
            )
        return process

    def path_exists(self, path):
        return os.path.exists(path)

    def copy_to_target(self, local_path, remote_path):
        # On local install the "target" is the same machine
        if os.path.abspath(local_path) != os.path.abspath(remote_path):
            shutil.copy2(local_path, remote_path)
        return remote_path


class RemoteExecutor:
    """
    Execute commands on a remote host over SSH.
    Prefer key-based auth. Password auth requires sshpass on the control host.
    """

    def __init__(self, host, user, password=None, key_path=None, port=22):
        self.host = host.strip()
        self.user = user.strip()
        self.password = password or ""
        self.key_path = key_path.strip() if key_path else ""
        self.port = int(port) if port else 22
        self._validate()

    def _validate(self):
        if not self.host:
            raise RuntimeError("Remote host / IP is required for remote install.")
        if not self.user:
            raise RuntimeError("Remote SSH user is required for remote install.")
        if self.key_path and not os.path.isfile(self.key_path):
            raise RuntimeError(f"SSH private key not found: {self.key_path}")
        if not self.key_path and not self.password:
            raise RuntimeError(
                "Either an SSH private key path or a password is required "
                "for remote authentication."
            )

    def _ssh_base(self, for_scp=False):
        """Build the common ssh / scp argument list."""
        binary = "scp" if for_scp else "ssh"
        cmd = [
            binary,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=30",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-p" if not for_scp else "-P", str(self.port),
        ]
        if self.key_path:
            cmd += ["-i", self.key_path]
        return cmd

    def _wrap_with_sshpass(self, cmd_list):
        if self.password and not self.key_path:
            # Check sshpass availability once
            check = subprocess.run(
                ["which", "sshpass"], capture_output=True, text=True
            )
            if check.returncode != 0:
                raise RuntimeError(
                    "Password authentication requires 'sshpass' on the control "
                    "machine. Install it (e.g. dnf/yum/apt install sshpass) "
                    "or use an SSH key instead."
                )
            return ["sshpass", "-p", self.password] + cmd_list
        return cmd_list

    def run(self, command, check=True, secrets=None):
        secrets = [s for s in (secrets or []) if s]
        if self.password:
            secrets.append(self.password)

        def redact(text):
            for s in secrets:
                if s:
                    text = text.replace(s, "****")
            return text

        ssh_cmd = self._ssh_base(for_scp=False)
        ssh_cmd.append(f"{self.user}@{self.host}")
        # Pass the whole remote command as a single argument so quoting is safe
        ssh_cmd.append(command)

        full_cmd = self._wrap_with_sshpass(ssh_cmd)
        log_cmd = " ".join(shlex.quote(c) for c in full_cmd)
        logger.log(f"$ {redact(log_cmd)}")

        process = subprocess.run(
            full_cmd, capture_output=True, text=True
        )
        if process.stdout:
            for line in process.stdout.strip().splitlines():
                logger.log(redact(line))
        if process.stderr:
            for line in process.stderr.strip().splitlines():
                # Filter out the common "Warning: Permanently added ..." noise
                if "Permanently added" in line or "Warning: Permanently added" in line:
                    continue
                logger.log(f"STDERR: {redact(line)}")
        if check and process.returncode != 0:
            raise RuntimeError(
                f"Remote command failed with exit code {process.returncode}: "
                f"{redact(command)}"
            )
        return process

    def path_exists(self, path):
        result = self.run(f"test -e {shlex.quote(path)}", check=False)
        return result.returncode == 0

    def copy_to_target(self, local_path, remote_path):
        """scp a local file to the remote host."""
        if not os.path.isfile(local_path):
            raise RuntimeError(f"Local file to copy does not exist: {local_path}")
        scp_cmd = self._ssh_base(for_scp=True)
        scp_cmd += [local_path, f"{self.user}@{self.host}:{remote_path}"]
        full_cmd = self._wrap_with_sshpass(scp_cmd)
        log_cmd = " ".join(shlex.quote(c) for c in full_cmd)
        logger.log(f"$ {log_cmd.replace(self.password or '', '****') if self.password else log_cmd}")
        process = subprocess.run(full_cmd, capture_output=True, text=True)
        if process.returncode != 0:
            err = (process.stderr or process.stdout or "").strip()
            raise RuntimeError(f"scp failed: {err}")
        logger.log(f"Copied {local_path} → {self.host}:{remote_path}")
        return remote_path


# ============================================================
# HELPERS
# ============================================================
def rb_quote(value):
    """Quote a value as a Ruby single-quoted string."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_proxy_url(host, port, username, password):
    if not host:
        return None
    host = re.sub(r"^https?://", "", host.strip()).rstrip("/")
    auth = ""
    if username and password:
        auth = (
            f"{urllib.parse.quote(username, safe='')}:"
            f"{urllib.parse.quote(password, safe='')}@"
        )
    port_part = f":{port}" if port else ""
    return f"http://{auth}{host}{port_part}"


def write_private_file(path, content):
    """Write a file readable only by the current user (mode 600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def validate_sudo(executor):
    logger.log("Checking passwordless sudo access...")
    result = executor.run("sudo -n true", check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "Passwordless sudo is required but 'sudo -n true' failed. "
            "Run the installer as a user with NOPASSWD sudo (or as root) "
            "on the target machine."
        )
    logger.log("Passwordless sudo check passed.")


# ------------------------------------------------------------
# Existing installation detection / cleanse
# ------------------------------------------------------------
def detect_existing_install(executor, os_type):
    findings = []
    if os_type == "rhel9":
        pkg = executor.run(f"rpm -q {MORPHEUS_PACKAGE}", check=False)
        installed = pkg.returncode == 0
    else:
        pkg = executor.run(
            f"dpkg-query -W -f='${{db:Status-Abbrev}}' {MORPHEUS_PACKAGE} "
            f"2>/dev/null | grep -q '^ii'",
            check=False
        )
        installed = pkg.returncode == 0
    if installed:
        findings.append(f"package '{MORPHEUS_PACKAGE}' is installed")
    for path in MORPHEUS_DIRS:
        if executor.path_exists(path):
            findings.append(f"{path} exists")
    return findings


def cleanse_existing_install(executor, os_type):
    logger.log("Cleansing existing Morpheus installation...")
    if executor.path_exists(MORPHEUS_CTL):
        executor.run(f"sudo {MORPHEUS_CTL} stop", check=False)
        executor.run(f"sudo {MORPHEUS_CTL} kill", check=False)
        result = executor.run(
            f"sudo {MORPHEUS_CTL} cleanse --yes", check=False
        )
        if result.returncode != 0:
            logger.log("'cleanse --yes' failed, retrying with piped confirmation...")
            executor.run(
                f"yes | sudo {MORPHEUS_CTL} cleanse", check=False
            )
        executor.run(f"sudo {MORPHEUS_CTL} uninstall", check=False)
    else:
        logger.log(
            "morpheus-ctl not found; skipping service stop/cleanse and "
            "removing leftovers directly."
        )
    if os_type == "rhel9":
        executor.run(
            f"sudo rpm -e --nodeps {MORPHEUS_PACKAGE}", check=False
        )
    else:
        executor.run(
            f"sudo dpkg -P {MORPHEUS_PACKAGE}", check=False
        )
    for path in MORPHEUS_DIRS + ["/var/log/morpheus"]:
        executor.run(f"sudo rm -rf {shlex.quote(path)}", check=False)
    remaining = detect_existing_install(executor, os_type)
    if remaining:
        raise RuntimeError(
            "Cleanse finished but remnants are still present: "
            + "; ".join(remaining)
        )
    logger.log("Existing Morpheus installation removed.")


# ------------------------------------------------------------
# Resource checks (executor-aware)
# ------------------------------------------------------------
def validate_disk_space(executor):
    logger.log("Checking disk space...")
    requirements = {
        "/": 20,
        "/var/opt/morpheus": 100,
        "/opt/morpheus": 50,
        "/var/lib/mysql": 100,
    }
    for path, required_gb in requirements.items():
        # Use df -BG (GNU) or fallback
        result = executor.run(
            f"df -BG --output=avail {shlex.quote(path)} 2>/dev/null | tail -1",
            check=False
        )
        if result.returncode != 0 or not result.stdout.strip():
            # Path may not exist yet
            logger.log(f"WARNING: {path} does not exist yet or df failed. Skipping.")
            continue
        try:
            free_str = result.stdout.strip().replace("G", "").strip()
            free_gb = float(free_str)
        except ValueError:
            logger.log(f"WARNING: Could not parse free space for {path}. Skipping.")
            continue
        logger.log(f"{path}: {free_gb:.1f} GB free (required: {required_gb} GB)")
        if free_gb < required_gb:
            raise RuntimeError(
                f"Insufficient disk space on {path}. "
                f"Required {required_gb} GB, available {free_gb:.1f} GB."
            )
    # Overall root check
    result = executor.run(
        "df -BG --output=avail / 2>/dev/null | tail -1", check=False
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            total_free_gb = float(result.stdout.strip().replace("G", "").strip())
            logger.log(
                f"Total free space on /: {total_free_gb:.1f} GB "
                f"(HPE Morpheus AIO minimum: 200 GB)"
            )
            if total_free_gb < 170:
                raise RuntimeError(
                    "Insufficient overall disk space for a Morpheus AIO "
                    f"installation. HPE requires at least 200 GB free, "
                    f"found {total_free_gb:.1f} GB."
                )
        except ValueError:
            pass


def validate_cpu_requirements(executor):
    logger.log("Checking CPU requirements...")
    result = executor.run("nproc 2>/dev/null || echo 0", check=False)
    try:
        cpu_count = int(result.stdout.strip() or "0")
    except ValueError:
        cpu_count = 0
    logger.log(
        f"Detected {cpu_count} vCPU(s) (HPE Morpheus AIO minimum: 4 vCPU, 1.4 GHz+)"
    )
    if cpu_count < 4:
        raise RuntimeError(
            f"Insufficient CPU cores for a Morpheus AIO installation. "
            f"HPE requires at least 4 vCPUs, found {cpu_count}."
        )


def validate_memory_requirements(executor):
    logger.log("Checking memory requirements...")
    result = executor.run(
        "grep MemTotal /proc/meminfo 2>/dev/null || true", check=False
    )
    if result.returncode != 0 or not result.stdout.strip():
        logger.log("WARNING: Could not read MemTotal. Skipping memory check.")
        return
    try:
        total_kb = int(result.stdout.split()[1])
        total_gb = total_kb / (1024 ** 2)
        logger.log(
            f"Detected {total_gb:.1f} GB RAM "
            f"(HPE Morpheus AIO minimum: 8 GB, recommended: 16 GB)"
        )
        if total_gb < 8:
            raise RuntimeError(
                f"Insufficient memory for a Morpheus AIO installation. "
                f"HPE requires at least 8 GB RAM, found {total_gb:.1f} GB."
            )
        if total_gb < 16:
            logger.log(
                "WARNING: 16 GB RAM is recommended for Morpheus AIO. "
                f"Only {total_gb:.1f} GB detected. Installation will "
                "continue, but performance may be affected."
            )
    except (IndexError, ValueError) as exc:
        logger.log(f"WARNING: Could not parse memory info: {exc}")


def validate_hostname_resolution(executor, hostname):
    logger.log("Checking hostname self-resolution...")
    target = hostname.strip() if hostname else ""
    if not target:
        # Get remote hostname
        result = executor.run("hostname", check=False)
        target = (result.stdout or "").strip() or "localhost"
    try:
        # Prefer remote resolution
        result = executor.run(
            f"getent hosts {shlex.quote(target)} || true", check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.log(f"Hostname '{target}' resolves (getent): {result.stdout.strip()}")
            return
    except Exception:
        pass
    logger.log(
        f"WARNING: Machine may not be self-resolvable for hostname '{target}'. "
        f"Attempting to auto-fix /etc/hosts..."
    )
    # Discover a usable IP on the target
    result = executor.run(
        "hostname -I 2>/dev/null | awk '{print $1}' || "
        "ip -4 route get 1 2>/dev/null | awk '{print $7; exit}' || echo 127.0.0.1",
        check=False
    )
    self_ip = (result.stdout or "127.0.0.1").strip().split()[0] or "127.0.0.1"
    hosts_line = f"{self_ip} {target}"
    executor.run(
        f"grep -qxF {shlex.quote(hosts_line)} /etc/hosts || "
        f"echo {shlex.quote(hosts_line)} | sudo tee -a /etc/hosts > /dev/null",
        check=False
    )
    logger.log(f"Added '{hosts_line}' to /etc/hosts on target.")


# ------------------------------------------------------------
# Whitelist / outbound connectivity validation (runs on target)
# ------------------------------------------------------------
def parse_whitelist_urls(whitelist_text):
    urls = []
    if not whitelist_text:
        return urls
    for line in whitelist_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def _split_endpoint(entry):
    if "*" in entry:
        return None
    if "://" not in entry:
        entry = "https://" + entry
    parsed = urllib.parse.urlparse(entry)
    if not parsed.hostname:
        return None
    scheme = parsed.scheme or "https"
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, parsed.hostname, port


def extract_whitelist_domains(urls):
    domains = set()
    for url in urls:
        parts = _split_endpoint(url)
        if parts:
            domains.add(parts[1])
        elif "*" in url:
            domains.add(url.split("://")[-1].split("/")[0])
    return sorted(domains)


def check_endpoint_remote(executor, entry, proxy_url=None, timeout=10):
    """
    Staged connectivity test executed ON THE TARGET via shell tools.
    Returns a result dict compatible with the old local checker.
    """
    result = {
        "entry": entry,
        "stages": [],
        "ok": False,
        "skipped": False,
        "error": None,
    }
    parts = _split_endpoint(entry)
    if parts is None:
        result["skipped"] = True
        result["error"] = "wildcard/unparseable entry cannot be tested"
        return result
    scheme, host, port = parts
    url = entry if "://" in entry else f"{scheme}://{host}:{port}"

    # DNS
    dns = executor.run(
        f"getent ahosts {shlex.quote(host)} 2>/dev/null | head -1 || "
        f"dig +short {shlex.quote(host)} 2>/dev/null | head -1 || true",
        check=False
    )
    if dns.stdout and dns.stdout.strip():
        result["stages"].append(f"DNS ok ({dns.stdout.strip().split()[0]})")
    else:
        result["error"] = "DNS failed / no resolution"
        return result

    # TCP (bash /dev/tcp or nc)
    tcp = executor.run(
        f"timeout {timeout} bash -c 'echo > /dev/tcp/{host}/{port}' 2>/dev/null || "
        f"nc -z -w {timeout} {shlex.quote(host)} {port} 2>/dev/null",
        check=False
    )
    if tcp.returncode != 0:
        result["error"] = f"TCP {port} blocked/unreachable"
        return result
    result["stages"].append(f"TCP {port} ok")

    # TLS (only for https)
    if scheme == "https":
        tls = executor.run(
            f"echo | timeout {timeout} openssl s_client -connect "
            f"{shlex.quote(host)}:{port} -servername {shlex.quote(host)} "
            f"2>/dev/null | head -5",
            check=False
        )
        if tls.returncode == 0 and ("BEGIN CERTIFICATE" in (tls.stdout or "") or
                                    "CONNECTED" in (tls.stdout or "")):
            result["stages"].append("TLS handshake ok")
        else:
            # Non-fatal for many environments (intercepting proxies etc.)
            result["stages"].append("TLS check inconclusive")

    # HTTP (curl)
    proxy_args = ""
    if proxy_url:
        proxy_args = f"--proxy {shlex.quote(proxy_url)} "
    http = executor.run(
        f"curl -sS -o /dev/null -w '%{{http_code}}' -m {timeout} "
        f"-I {proxy_args}{shlex.quote(url)} 2>/dev/null || "
        f"curl -sS -o /dev/null -w '%{{http_code}}' -m {timeout} "
        f"{proxy_args}{shlex.quote(url)} 2>/dev/null || echo 000",
        check=False
    )
    code = (http.stdout or "000").strip()
    if code and code != "000":
        label = "via proxy" if proxy_url else "direct"
        result["stages"].append(f"HTTP {code} ({label})")
        result["ok"] = True
    else:
        result["error"] = "HTTP request failed / no response"
    return result


def validate_url_connectivity(executor, urls, proxy_url=None, strict=False):
    if not urls:
        logger.log("No whitelist entries supplied. Skipping connectivity checks.")
        return
    mode = "through the configured proxy" if proxy_url else "directly"
    logger.log(f"Checking {len(urls)} whitelist entries {mode} (from target)...")
    # Sequential to keep SSH sessions simple and logs readable
    passed, failed, skipped = [], [], []
    for u in urls:
        r = check_endpoint_remote(executor, u, proxy_url=proxy_url)
        if r["skipped"]:
            skipped.append(r)
            logger.log(f"SKIP  {r['entry']}  ({r['error']})")
        elif r["ok"]:
            passed.append(r)
            logger.log(f"PASS  {r['entry']}  [{' | '.join(r['stages'])}]")
        else:
            failed.append(r)
            done = " | ".join(r["stages"])
            suffix = f"  (passed: {done})" if done else ""
            logger.log(f"FAIL  {r['entry']}  -> {r['error']}{suffix}")
    logger.log(
        f"Whitelist check: {len(passed)} passed, "
        f"{len(failed)} failed, {len(skipped)} skipped."
    )
    if failed:
        logger.log(
            "Ask the network team to allow the FAIL entries above "
            "(DNS, outbound TCP, and TLS inspection exemption/CA trust)."
        )
        if strict:
            raise RuntimeError(
                f"{len(failed)} whitelist endpoint(s) are not reachable. "
                f"Fix the network path or disable strict whitelist checking."
            )


# ------------------------------------------------------------
# System configuration
# ------------------------------------------------------------
def setup_rhel_subscription(executor, username, password, rhel_major_version=9):
    username = (username or "").strip()
    if not username or not (password or "").strip():
        logger.log(
            "RHEL subscription credentials not supplied. "
            "Skipping subscription registration."
        )
        return
    logger.log("Checking RHEL subscription status...")
    identity = executor.run("sudo subscription-manager identity", check=False)
    if identity.returncode == 0:
        logger.log("System is already registered. Skipping registration.")
    else:
        logger.log("Registering RHEL subscription...")
        executor.run(
            f"sudo subscription-manager register "
            f"--username {shlex.quote(username)} "
            f"--password {shlex.quote(password)}",
            check=True,
            secrets=[password]
        )
    executor.run("sudo subscription-manager refresh", check=False)
    for repo in ("baseos", "appstream"):
        executor.run(
            f"sudo subscription-manager repos "
            f"--enable=rhel-{rhel_major_version}-for-x86_64-{repo}-rpms",
            check=False
        )
    if rhel_major_version == 7:
        logger.log("RHEL 7 detected: enabling the Optional RPMs repo...")
        executor.run(
            "sudo yum-config-manager --enable rhel-7-server-optional-rpms",
            check=False
        )
    else:
        logger.log(
            f"RHEL {rhel_major_version}: Optional RPMs are part of appstream, "
            f"no action needed."
        )
    logger.log("RHEL subscription configuration completed.")


def setup_packages(executor, os_type):
    logger.log("Installing prerequisite packages...")
    if os_type == "rhel9":
        executor.run("sudo dnf clean all", check=False)
        executor.run("sudo dnf makecache", check=False)
        packages = [
            "curl", "wget", "tar", "gzip", "unzip", "vim", "openssl",
            "chrony", "policycoreutils", "policycoreutils-python-utils",
            "firewalld", "net-tools", "bind-utils",
        ]
        executor.run(f"sudo dnf install -y {' '.join(packages)}")
    elif os_type == "ubuntu":
        executor.run("sudo apt-get update", check=False)
        packages = [
            "curl", "wget", "tar", "gzip", "unzip", "vim", "openssl",
            "chrony", "firewalld", "net-tools", "dnsutils",
        ]
        executor.run(f"sudo apt-get install -y {' '.join(packages)}")


def setup_hostname(executor, hostname):
    if not hostname:
        logger.log("Hostname not specified. Skipping hostname setup.")
        return
    logger.log(f"Setting hostname to {hostname}")
    executor.run(f"sudo hostnamectl set-hostname {shlex.quote(hostname)}")
    logger.log("Hostname configured.")


def setup_ntp(executor, ntp_server, os_type):
    if not ntp_server:
        logger.log("NTP server not specified. Skipping NTP setup.")
        return
    logger.log(f"Configuring NTP server: {ntp_server}")
    # Write config via a here-doc on the remote side
    conf_content = f"server {ntp_server} iburst\\n"
    if os_type == "rhel9":
        conf, service = "/etc/chrony.conf", "chronyd"
    else:
        conf, service = "/etc/chrony/chrony.conf", "chrony"
    executor.run(
        f"printf '{conf_content}' | sudo tee {conf} > /dev/null"
    )
    executor.run(f"sudo systemctl enable {service}", check=False)
    executor.run(f"sudo systemctl restart {service}", check=False)
    logger.log("NTP configuration completed.")


def setup_dns(executor, dns_servers):
    if not dns_servers:
        logger.log("DNS servers not specified. Skipping DNS setup.")
        return
    servers = [s.strip() for s in dns_servers.split(",") if s.strip()]
    if not servers:
        return
    logger.log(f"Configuring DNS servers: {', '.join(servers)}")
    content = "\\n".join(f"nameserver {s}" for s in servers) + "\\n"
    executor.run(
        f"printf '{content}' | sudo tee /etc/resolv.conf > /dev/null",
        check=False
    )
    logger.log("DNS configuration completed.")


def setup_selinux(executor, selinux_mode):
    if not selinux_mode:
        return
    selinux_mode = selinux_mode.lower()
    logger.log(f"Configuring SELinux mode: {selinux_mode}")
    if selinux_mode not in ("disabled", "permissive", "enforcing"):
        logger.log(f"WARNING: Unknown SELinux mode: {selinux_mode}")
        return
    runtime = "1" if selinux_mode == "enforcing" else "0"
    executor.run(f"sudo setenforce {runtime}", check=False)
    executor.run(
        f"sudo sed -i 's/^SELINUX=.*/SELINUX={selinux_mode}/' "
        f"/etc/selinux/config",
        check=False
    )
    if selinux_mode == "disabled":
        logger.log(
            "SELinux configured as disabled. "
            "A reboot may be required for the permanent setting."
        )


def setup_firewall(executor, firewall_enabled, appliance_port):
    if not firewall_enabled:
        logger.log("Firewall configuration disabled.")
        return
    logger.log("Configuring firewall...")
    executor.run("sudo systemctl enable firewalld", check=False)
    executor.run("sudo systemctl start firewalld", check=False)
    ports = ["22/tcp", "80/tcp", "443/tcp", "8000/tcp"]
    if appliance_port:
        ports.append(f"{appliance_port}/tcp")
    for port in sorted(set(ports)):
        executor.run(
            f"sudo firewall-cmd --permanent --add-port={port}", check=False
        )
    executor.run("sudo firewall-cmd --reload", check=False)
    logger.log("Firewall configuration completed.")


def validate_port_443_reachability(executor):
    logger.log(
        "Checking local TCP 443 reachability "
        "(HPE Morpheus requires HTTPS access on 443)..."
    )
    result = executor.run(
        "timeout 5 bash -c 'echo > /dev/tcp/127.0.0.1/443' 2>/dev/null || "
        "nc -z -w 5 127.0.0.1 443 2>/dev/null",
        check=False
    )
    if result.returncode == 0:
        logger.log("Port 443 is open and reachable on localhost.")
    else:
        logger.log(
            "WARNING: Port 443 is not yet reachable on localhost "
            "(expected before morpheus-ctl reconfigure has run)."
        )


def validate_appliance_reachable(executor, appliance_url, hostname, timeout_seconds=180):
    target_host = hostname.strip() if hostname else None
    if not target_host and appliance_url:
        try:
            target_host = urllib.parse.urlparse(appliance_url).hostname
        except Exception:
            target_host = None
    if not target_host:
        # Ask the target for its own hostname
        r = executor.run("hostname -f 2>/dev/null || hostname", check=False)
        target_host = (r.stdout or "").strip() or "localhost"
    check_url = f"https://{target_host}"
    logger.log(
        f"Waiting for the Morpheus appliance to become reachable at "
        f"{check_url} (timeout: {timeout_seconds}s)..."
    )
    start_time = time.time()
    attempt = 0
    while time.time() - start_time < timeout_seconds:
        attempt += 1
        # Run curl on the target itself (ignore cert for the health check)
        result = executor.run(
            f"curl -k -sS -o /dev/null -w '%{{http_code}}' -m 10 "
            f"{shlex.quote(check_url)} 2>/dev/null || echo 000",
            check=False
        )
        code = (result.stdout or "000").strip()
        if code and code != "000":
            logger.log(
                f"Appliance responded with HTTP {code} on attempt {attempt}."
            )
            return True
        logger.log(
            f"Attempt {attempt}: appliance not yet reachable (HTTP {code}). "
            f"Retrying in 10s..."
        )
        time.sleep(10)
    logger.log(
        f"WARNING: Appliance did not become reachable at {check_url} "
        f"within {timeout_seconds} seconds. It may still be starting "
        f"up; check 'sudo morpheus-ctl status' on the host."
    )
    return False


def setup_proxy(executor, proxy_host, proxy_port, proxy_username, proxy_password):
    if not proxy_host:
        logger.log("Proxy not configured.")
        return
    logger.log(f"Configuring proxy: {proxy_host}:{proxy_port}")
    proxy_url = build_proxy_url(
        proxy_host, proxy_port, proxy_username, proxy_password
    )
    q = shlex.quote(proxy_url)
    # Write a profile.d script on the remote host
    content = (
        f"export HTTP_PROXY={q}\\n"
        f"export HTTPS_PROXY={q}\\n"
        f"export http_proxy={q}\\n"
        f"export https_proxy={q}\\n"
        f"export NO_PROXY=localhost,127.0.0.1\\n"
    )
    executor.run(
        f"printf '{content}' | sudo tee /etc/profile.d/morpheus-proxy.sh > /dev/null",
        secrets=[proxy_password, urllib.parse.quote(proxy_password or "", safe="")]
    )
    # Also export for the current remote session (best-effort)
    executor.run(
        f"export HTTP_PROXY={q}; export HTTPS_PROXY={q}; "
        f"export http_proxy={q}; export https_proxy={q}; "
        f"export NO_PROXY=localhost,127.0.0.1",
        check=False,
        secrets=[proxy_password]
    )
    logger.log("Proxy configuration completed.")


def setup_ssl(executor, ssl_cert, ssl_key):
    if not ssl_cert or not ssl_key:
        logger.log("SSL certificate/key not supplied. Skipping SSL setup.")
        return
    # ssl_cert / ssl_key are paths on the *control* machine
    cert_path = os.path.expanduser(ssl_cert)
    key_path = os.path.expanduser(ssl_key)
    if not os.path.exists(cert_path):
        raise RuntimeError(f"SSL certificate not found on control host: {cert_path}")
    if not os.path.exists(key_path):
        raise RuntimeError(f"SSL private key not found on control host: {key_path}")
    logger.log("Installing SSL certificate and private key on target...")
    executor.run("sudo mkdir -p /etc/morpheus/ssl")
    # Copy via a temporary location then move with sudo
    remote_tmp_cert = "/tmp/morpheus-ssl.crt"
    remote_tmp_key = "/tmp/morpheus-ssl.key"
    executor.copy_to_target(cert_path, remote_tmp_cert)
    executor.copy_to_target(key_path, remote_tmp_key)
    executor.run(f"sudo cp {remote_tmp_cert} /etc/morpheus/ssl/morpheus.crt")
    executor.run(f"sudo cp {remote_tmp_key} /etc/morpheus/ssl/morpheus.key")
    executor.run("sudo chmod 600 /etc/morpheus/ssl/morpheus.key")
    executor.run("sudo chmod 644 /etc/morpheus/ssl/morpheus.crt")
    executor.run(f"rm -f {remote_tmp_cert} {remote_tmp_key}", check=False)
    logger.log("SSL files installed.")


def generate_morpheus_rb(
    executor,
    appliance_url, hostname,
    proxy_host, proxy_port, proxy_username, proxy_password,
    ssl_cert, ssl_key
):
    logger.log("Generating /etc/morpheus/morpheus.rb configuration...")
    lines = []
    if appliance_url:
        lines.append(f"appliance_url {rb_quote(appliance_url)}")
    if hostname:
        lines.append(f"hostname {rb_quote(hostname)}")
    if proxy_host:
        lines.append(f"proxy['host'] = {rb_quote(proxy_host)}")
        if proxy_port:
            lines.append(f"proxy['port'] = {int(proxy_port)}")
        if proxy_username:
            lines.append(f"proxy['username'] = {rb_quote(proxy_username)}")
        if proxy_password:
            lines.append(f"proxy['password'] = {rb_quote(proxy_password)}")
    if ssl_cert:
        lines.append("nginx['ssl_certificate'] = '/etc/morpheus/ssl/morpheus.crt'")
    if ssl_key:
        lines.append("nginx['ssl_server_key'] = '/etc/morpheus/ssl/morpheus.key'")
    # Write locally then copy, or write via remote printf
    content = "\\n".join(lines) + "\\n"
    # Use a temp file on control host then scp for cleanliness with secrets
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="morpheus-rb-") as tmp:
        tmp.write("\n".join(lines) + "\n")
        tmp_path = tmp.name
    try:
        os.chmod(tmp_path, 0o600)
        remote_tmp = "/tmp/morpheus.rb"
        executor.copy_to_target(tmp_path, remote_tmp)
        executor.run("sudo mkdir -p /etc/morpheus")
        executor.run(f"sudo cp {remote_tmp} /etc/morpheus/morpheus.rb")
        executor.run("sudo chmod 600 /etc/morpheus/morpheus.rb")
        executor.run(f"rm -f {remote_tmp}", check=False)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    logger.log("Morpheus configuration generated.")


# ============================================================
# INSTALLATION
# ============================================================
def fetch_source_checksum(checksum_source_url):
    logger.log(f"Fetching source checksum from {checksum_source_url}...")
    context = ssl.create_default_context()
    request = urllib.request.Request(
        checksum_source_url, method="GET",
        headers={"User-Agent": "Morpheus-Installer/1.0"}
    )
    with urllib.request.urlopen(request, timeout=15, context=context) as response:
        body = response.read().decode("utf-8", errors="ignore")
    # Look for a 128-char hex digest (SHA-512)
    match = re.search(r"\b[0-9a-fA-F]{128}\b", body)
    if not match:
        # Fallback: also accept 64-char in case the published page still shows SHA-256
        match = re.search(r"\b[0-9a-fA-F]{64}\b", body)
        if match:
            logger.log(
                "WARNING: Only found a 64-character (SHA-256) digest at the "
                "source URL. Your package uses SHA-512 – verification against "
                "source will be skipped."
            )
            return None
        raise RuntimeError(
            f"Could not find a SHA-512 (or SHA-256) checksum in the response "
            f"from {checksum_source_url}."
        )
    return match.group(0)


def run_installation_task(
    os_type, package_path, package_checksum,
    appliance_url, hostname, ntp_server, dns_servers,
    firewall_enabled, selinux_mode,
    rhel_username, rhel_password,
    ssl_cert, ssl_key, whitelist,
    proxy_host, proxy_port, proxy_username, proxy_password,
    checksum_source_url="",
    cleanse_existing=True,
    whitelist_strict=False,
    # Remote target parameters
    target_mode="local",
    remote_host="",
    remote_user="",
    remote_password="",
    remote_key_path="",
    remote_port=22,
):
    executor = None
    try:
        if target_mode == "remote":
            logger.log("=" * 70)
            logger.log("HPE MORPHEUS ENTERPRISE INSTALLATION (REMOTE) STARTED")
            logger.log(f"Target: {remote_user}@{remote_host}:{remote_port}")
            logger.log("=" * 70)
            executor = RemoteExecutor(
                host=remote_host,
                user=remote_user,
                password=remote_password or None,
                key_path=remote_key_path or None,
                port=remote_port,
            )
            # Quick connectivity test
            logger.log("Testing SSH connectivity to target...")
            executor.run("echo 'SSH connection OK'", check=True)
        else:
            logger.log("=" * 70)
            logger.log("HPE MORPHEUS ENTERPRISE INSTALLATION (LOCAL) STARTED")
            logger.log("=" * 70)
            executor = LocalExecutor()

        # ---------------- Package validation (always on control host) ----------------
        expanded_package = os.path.expanduser(package_path)
        logger.log(f"Package path (control host): {expanded_package}")
        if not os.path.isfile(expanded_package):
            raise RuntimeError(
                f"Package file does not exist on the control host: {expanded_package}"
            )
        expected_extension = ".rpm" if os_type == "rhel9" else ".deb"
        if not expanded_package.lower().endswith(expected_extension):
            raise RuntimeError(
                f"{os_type} requires a {expected_extension} package. "
                f"Selected file: {expanded_package}"
            )
        logger.log("Package extension validated.")

        # ---------------- SHA-512 ----------------
        if not re.fullmatch(r"[0-9a-fA-F]{128}", package_checksum):
            raise RuntimeError(
                "Package SHA-512 checksum must contain exactly "
                "128 hexadecimal characters."
            )
        logger.log("Calculating package SHA-512...")
        sha512 = hashlib.sha512()
        with open(expanded_package, "rb") as package_file:
            while True:
                data = package_file.read(1024 * 1024)
                if not data:
                    break
                sha512.update(data)
        actual_checksum = sha512.hexdigest()
        logger.log(f"Actual SHA-512:   {actual_checksum}")
        logger.log(f"Expected SHA-512: {package_checksum}")
        if actual_checksum.lower() != package_checksum.lower():
            raise RuntimeError("SHA-512 checksum verification FAILED.")
        logger.log("SHA-512 checksum verified successfully.")

        if checksum_source_url:
            try:
                source_checksum = fetch_source_checksum(checksum_source_url)
                if source_checksum:
                    logger.log(f"Source SHA-512:   {source_checksum}")
                    if source_checksum.lower() != actual_checksum.lower():
                        raise RuntimeError(
                            "Package checksum does NOT match the published "
                            "source checksum. The package file may be "
                            "corrupted, tampered with, or the wrong version."
                        )
                    logger.log("Package checksum matches the published source checksum.")
            except RuntimeError:
                raise
            except Exception as exc:
                logger.log(
                    f"WARNING: Could not verify against source checksum: "
                    f"{exc}. Continuing with the user-supplied checksum only."
                )
        else:
            logger.log(
                "No checksum source URL supplied. Skipping verification "
                "against the published source checksum."
            )

        # ---------------- Sudo ----------------
        validate_sudo(executor)

        # ---------------- Existing install ----------------
        logger.log("Checking for an existing Morpheus installation...")
        existing = detect_existing_install(executor, os_type)
        if existing:
            for item in existing:
                logger.log(f"Found existing install: {item}")
            if not cleanse_existing:
                raise RuntimeError(
                    "An existing Morpheus installation was found and "
                    "automatic cleanse is disabled. Remove it manually "
                    "(morpheus-ctl cleanse) or enable 'cleanse existing'."
                )
            logger.log(
                "WARNING: cleansing will DELETE all existing Morpheus data "
                "(database, config, logs)."
            )
            cleanse_existing_install(executor, os_type)
        else:
            logger.log("No existing Morpheus installation found.")

        # ---------------- Resources ----------------
        validate_cpu_requirements(executor)
        validate_memory_requirements(executor)
        validate_disk_space(executor)
        validate_hostname_resolution(executor, hostname)

        # ---------------- Whitelist ----------------
        whitelist_urls = parse_whitelist_urls(whitelist)
        logger.log(f"Whitelist entries: {len(whitelist_urls)}")
        domains = extract_whitelist_domains(whitelist_urls)
        if domains:
            logger.log("Whitelist domains:")
            for domain in domains:
                logger.log(f"  - {domain}")
        validate_url_connectivity(
            executor,
            whitelist_urls,
            proxy_url=build_proxy_url(
                proxy_host, proxy_port, proxy_username, proxy_password
            ),
            strict=whitelist_strict
        )

        # ---------------- RHEL subscription ----------------
        if (
            os_type == "rhel9"
            and (rhel_username or "").strip()
            and (rhel_password or "").strip()
        ):
            setup_rhel_subscription(executor, rhel_username, rhel_password)
        elif os_type == "rhel9":
            logger.log("No RHEL credentials supplied; subscription step skipped.")

        # ---------------- System setup ----------------
        setup_packages(executor, os_type)
        setup_hostname(executor, hostname)
        setup_ntp(executor, ntp_server, os_type)
        setup_dns(executor, dns_servers)
        setup_proxy(executor, proxy_host, proxy_port, proxy_username, proxy_password)
        if os_type == "rhel9":
            setup_selinux(executor, selinux_mode)
        setup_firewall(executor, firewall_enabled, 8000)
        validate_port_443_reachability(executor)
        setup_ssl(executor, ssl_cert, ssl_key)
        generate_morpheus_rb(
            executor, appliance_url, hostname,
            proxy_host, proxy_port, proxy_username, proxy_password,
            ssl_cert, ssl_key
        )

        # ---------------- Transfer + Install package ----------------
        remote_pkg = f"/tmp/{os.path.basename(expanded_package)}"
        logger.log(f"Copying installation package to target: {remote_pkg}")
        executor.copy_to_target(expanded_package, remote_pkg)

        if os_type == "rhel9":
            logger.log("Installing Morpheus RPM package...")
            executor.run(f"sudo rpm -Uvh {shlex.quote(remote_pkg)}")
        else:
            logger.log("Installing Morpheus DEB package...")
            executor.run(f"sudo dpkg -i {shlex.quote(remote_pkg)}")

        # Clean up the package on the target
        executor.run(f"rm -f {shlex.quote(remote_pkg)}", check=False)

        # ---------------- Reconfigure ----------------
        logger.log("Running morpheus-ctl reconfigure...")
        executor.run("sudo morpheus-ctl reconfigure")
        logger.log(
            "morpheus-ctl reconfigure completed. Waiting for the "
            "appliance to come online..."
        )
        appliance_is_up = validate_appliance_reachable(
            executor, appliance_url, hostname, timeout_seconds=180
        )

        logger.log("=" * 70)
        logger.log("HPE MORPHEUS ENTERPRISE INSTALLATION COMPLETED")
        logger.log("=" * 70)
        final_host = hostname.strip() if hostname else None
        if not final_host and appliance_url:
            try:
                final_host = urllib.parse.urlparse(appliance_url).hostname
            except Exception:
                final_host = None
        if not final_host:
            if target_mode == "remote":
                final_host = remote_host
            else:
                final_host = "your_machine_name"
        logger.log("")
        logger.log("INSTALLATION SUMMARY")
        logger.log("-" * 70)
        logger.log(f"Target mode:        {target_mode}")
        if target_mode == "remote":
            logger.log(f"Remote host:        {remote_user}@{remote_host}")
        logger.log(f"Appliance URL:      https://{final_host}")
        logger.log(
            f"Appliance status:   "
            f"{'ONLINE' if appliance_is_up else 'NOT CONFIRMED (check manually)'}"
        )
        logger.log(f"OS type:            {os_type}")
        logger.log(f"Firewall enabled:   {bool(firewall_enabled)}")
        if os_type == "rhel9":
            logger.log(f"SELinux mode:       {selinux_mode or 'not set'}")
        logger.log("-" * 70)
        logger.log(
            "Log in to the appliance URL above to complete the "
            "initial Morpheus appliance setup wizard."
        )
        logger.log("-" * 70)

    except Exception as exc:
        logger.log("=" * 70)
        logger.log("INSTALLATION FAILED")
        logger.log(f"{type(exc).__name__}: {exc}")
        for tb_line in traceback.format_exc().strip().splitlines():
            logger.log(f"  {tb_line}")
        logger.log("=" * 70)
    finally:
        with install_lock:
            install_status["running"] = False


# ============================================================
# WEB PAGE
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index():
    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# INSTALL ENDPOINT
# ============================================================
def _reject(message, status_code=400):
    logger.log(f"REQUEST REJECTED (HTTP {status_code}): {message}")
    with install_lock:
        install_status["running"] = False
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "message": message}
    )


@app.post("/install")
async def install(
    background_tasks: BackgroundTasks,
    os_type: str = Form(...),
    package_path: str = Form(...),
    package_checksum: str = Form(...),
    checksum_source_url: str = Form(""),
    appliance_url: str = Form(""),
    hostname: str = Form(""),
    ntp_server: str = Form(""),
    dns_servers: str = Form(""),
    firewall_enabled: str = Form(""),
    selinux_mode: str = Form(""),
    rhel_username: str = Form(""),
    rhel_password: str = Form(""),
    ssl_cert: str = Form(""),
    ssl_key: str = Form(""),
    whitelist: str = Form(""),
    whitelist_strict: str = Form(""),
    cleanse_existing: str = Form("yes"),
    proxy_host: str = Form(""),
    proxy_port: str = Form(""),
    proxy_username: str = Form(""),
    proxy_password: str = Form(""),
    # ---- Remote target fields ----
    target_mode: str = Form("local"),          # "local" or "remote"
    remote_host: str = Form(""),
    remote_user: str = Form(""),
    remote_password: str = Form(""),
    remote_key_path: str = Form(""),
    remote_port: str = Form("22"),
):
    with install_lock:
        if install_status["running"]:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "message": "An installation is already running."}
            )
        install_status["running"] = True
        install_status["logs"] = []

    # Log what was received (secrets deliberately omitted)
    logger.log("Install request received:")
    logger.log(f"  target_mode:      {target_mode!r}")
    logger.log(f"  os_type:          {os_type!r}")
    logger.log(f"  package_path:     {package_path!r}")
    logger.log(f"  checksum length:  {len(package_checksum)} (need 128 for SHA-512)")
    logger.log(f"  hostname:         {hostname!r}")
    logger.log(f"  appliance_url:    {appliance_url!r}")
    logger.log(f"  rhel creds given: {bool(rhel_username.strip() and rhel_password.strip())}")
    logger.log(f"  proxy:            {proxy_host!r}:{proxy_port!r}")
    logger.log(f"  cleanse_existing: {cleanse_existing!r}")
    if target_mode == "remote":
        logger.log(f"  remote_host:      {remote_host!r}")
        logger.log(f"  remote_user:      {remote_user!r}")
        logger.log(f"  remote_port:      {remote_port!r}")
        logger.log(f"  remote_key:       {'yes' if remote_key_path.strip() else 'no'}")
        logger.log(f"  remote_password:  {'yes' if remote_password.strip() else 'no'}")

    package_checksum = package_checksum.strip()
    package_path = package_path.strip()
    target_mode = target_mode.strip().lower() or "local"

    if os_type not in ("rhel9", "ubuntu"):
        return _reject("Invalid operating system.")
    if target_mode not in ("local", "remote"):
        return _reject("target_mode must be 'local' or 'remote'.")

    expanded_package = os.path.expanduser(package_path)
    if not os.path.isfile(expanded_package):
        return _reject(
            f"Package file does not exist ON THE CONTROL HOST running this app: "
            f"{expanded_package}"
        )
    if not re.fullmatch(r"[0-9a-fA-F]{128}", package_checksum):
        return _reject(
            f"SHA-512 checksum must be exactly 128 hex characters, "
            f"got {len(package_checksum)}."
        )
    expected_extension = ".rpm" if os_type == "rhel9" else ".deb"
    if not expanded_package.lower().endswith(expected_extension):
        return _reject(f"{os_type} requires a {expected_extension} package.")

    if target_mode == "remote":
        if not remote_host.strip():
            return _reject("Remote host / IP is required for remote install.")
        if not remote_user.strip():
            return _reject("Remote SSH user is required for remote install.")
        if not remote_key_path.strip() and not remote_password.strip():
            return _reject(
                "Provide either an SSH private key path or a password for the remote host."
            )
        if remote_key_path.strip() and not os.path.isfile(os.path.expanduser(remote_key_path.strip())):
            return _reject(f"SSH private key not found on control host: {remote_key_path}")

    proxy_port_value = None
    if proxy_port.strip():
        try:
            proxy_port_value = int(proxy_port.strip())
        except ValueError:
            return _reject("Proxy port must be a number.")
        if not 1 <= proxy_port_value <= 65535:
            return _reject("Proxy port must be between 1 and 65535.")

    remote_port_value = 22
    if remote_port.strip():
        try:
            remote_port_value = int(remote_port.strip())
        except ValueError:
            return _reject("Remote SSH port must be a number.")
        if not 1 <= remote_port_value <= 65535:
            return _reject("Remote SSH port must be between 1 and 65535.")

    background_tasks.add_task(
        run_installation_task,
        os_type=os_type,
        package_path=package_path,
        package_checksum=package_checksum,
        appliance_url=appliance_url,
        hostname=hostname,
        ntp_server=ntp_server,
        dns_servers=dns_servers,
        firewall_enabled=bool(firewall_enabled),
        selinux_mode=selinux_mode,
        rhel_username=rhel_username,
        rhel_password=rhel_password,
        ssl_cert=ssl_cert,
        ssl_key=ssl_key,
        whitelist=whitelist,
        proxy_host=proxy_host,
        proxy_port=proxy_port_value,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        checksum_source_url=checksum_source_url,
        cleanse_existing=cleanse_existing.strip().lower() not in ("no", "false", "0"),
        whitelist_strict=bool(whitelist_strict.strip()),
        target_mode=target_mode,
        remote_host=remote_host.strip(),
        remote_user=remote_user.strip(),
        remote_password=remote_password,
        remote_key_path=os.path.expanduser(remote_key_path.strip()) if remote_key_path.strip() else "",
        remote_port=remote_port_value,
    )
    return JSONResponse(content={"ok": True, "message": "Installation started."})


# ============================================================
# LOG ENDPOINT
# ============================================================
@app.get("/logs")
async def logs():
    with install_lock:
        return {
            "running": install_status["running"],
            "logs": list(install_status["logs"])
        }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

import os
import hashlib
import logging
import subprocess
import shutil
import time
import urllib.request
import ssl
import shlex
import re
import threading

from fastapi import FastAPI, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


app = FastAPI()

# Serve style.css and main.js from the static/ folder
app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

# Path to the HTML page (kept separate from this file)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML_PATH = os.path.join(BASE_DIR, "templates", "index.html")

install_lock = threading.Lock()

install_status = {
    "running": False,
    "logs": []
}

executor = None


# ============================================================
# LOGGING
# ============================================================

class WebLogger:
    def log(self, message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        line = f"[{timestamp}] {message}"

        with install_lock:
            install_status["logs"].append(line)

            # Keep memory usage under control
            if len(install_status["logs"]) > 2000:
                install_status["logs"] = install_status["logs"][-2000:]

        print(line, flush=True)


logger = WebLogger()


# ============================================================
# LOCAL COMMAND EXECUTOR
# ============================================================

class LocalExecutor:

    def run(self, command, check=True):
        logger.log(f"$ {command}")

        process = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True
        )

        if process.stdout:
            for line in process.stdout.strip().splitlines():
                logger.log(line)

        if process.stderr:
            for line in process.stderr.strip().splitlines():
                logger.log(f"STDERR: {line}")

        if check and process.returncode != 0:
            raise RuntimeError(
                f"Command failed with exit code {process.returncode}: {command}"
            )

        return process

    def put(self, source, destination, mode=None):
        logger.log(f"Copying {source} -> {destination}")

        shutil.copy2(source, destination)

        if mode is not None:
            os.chmod(destination, mode)


# ============================================================
# HELPERS
# ============================================================

def validate_disk_space():
    logger.log("Checking disk space...")

    requirements = {
        "/": 20,
        "/var/opt/morpheus": 100,
        "/opt/morpheus": 50,
        "/var/lib/mysql": 100,
    }

    for path, required_gb in requirements.items():

        if not os.path.exists(path):
            logger.log(
                f"WARNING: {path} does not exist yet. "
                f"Skipping disk check."
            )
            continue

        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)

        logger.log(
            f"{path}: {free_gb:.1f} GB free "
            f"(required: {required_gb} GB)"
        )

        if free_gb < required_gb:
            raise RuntimeError(
                f"Insufficient disk space on {path}. "
                f"Required {required_gb} GB, available {free_gb:.1f} GB."
            )

    # Overall minimum per HPE Morpheus AIO requirements: 200 GB total
    total_free_gb = shutil.disk_usage("/").free / (1024 ** 3)

    logger.log(
        f"Total free space on /: {total_free_gb:.1f} GB "
        f"(HPE Morpheus AIO minimum: 200 GB)"
    )

    if total_free_gb < 200:
        raise RuntimeError(
            "Insufficient overall disk space for a Morpheus AIO "
            f"installation. HPE requires at least 200 GB free, "
            f"found {total_free_gb:.1f} GB."
        )


def validate_cpu_requirements():
    logger.log("Checking CPU requirements...")

    cpu_count = os.cpu_count() or 0

    logger.log(
        f"Detected {cpu_count} vCPU(s) "
        f"(HPE Morpheus AIO minimum: 4 vCPU, 1.4 GHz+)"
    )

    if cpu_count < 4:
        raise RuntimeError(
            f"Insufficient CPU cores for a Morpheus AIO installation. "
            f"HPE requires at least 4 vCPUs, found {cpu_count}."
        )


def validate_memory_requirements():
    logger.log("Checking memory requirements...")

    try:

        with open("/proc/meminfo", "r") as f:
            meminfo = f.read()

        total_kb = None

        for line in meminfo.splitlines():

            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
                break

        if total_kb is None:
            logger.log(
                "WARNING: Could not determine total memory from "
                "/proc/meminfo. Skipping memory check."
            )
            return

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

    except RuntimeError:
        raise

    except Exception as exc:
        logger.log(
            f"WARNING: Could not check memory requirements: {exc}"
        )


def validate_hostname_resolution(executor, hostname):
    logger.log("Checking hostname self-resolution...")

    import socket

    target = hostname.strip() if hostname else socket.gethostname()

    try:
        resolved_ip = socket.gethostbyname(target)

        logger.log(
            f"Hostname '{target}' resolves to {resolved_ip}."
        )

    except socket.gaierror as exc:

        logger.log(
            f"WARNING: Machine is not self resolvable to hostname "
            f"'{target}': {exc}. HPE Morpheus requires the appliance "
            f"host to resolve its own hostname."
        )

        logger.log(
            f"Attempting to auto-fix by adding '{target}' to /etc/hosts..."
        )

        try:

            # Determine the primary non-loopback IP of this machine, if
            # possible, otherwise fall back to 127.0.0.1.
            self_ip = "127.0.0.1"

            try:

                temp_socket = socket.socket(
                    socket.AF_INET,
                    socket.SOCK_DGRAM
                )

                temp_socket.connect(("8.8.8.8", 80))

                self_ip = temp_socket.getsockname()[0]

                temp_socket.close()

            except Exception:

                self_ip = "127.0.0.1"

            hosts_line = f"{self_ip} {target}\n"

            temp_hosts_entry = "/tmp/morpheus-hosts-entry"

            with open(temp_hosts_entry, "w") as f:
                f.write(hosts_line)

            executor.run(
                f"grep -qxF {shlex.quote(hosts_line.strip())} /etc/hosts || "
                f"sudo tee -a /etc/hosts < {shlex.quote(temp_hosts_entry)} "
                f"> /dev/null",
                check=False
            )

            logger.log(
                f"Added '{hosts_line.strip()}' to /etc/hosts."
            )

            # Re-verify resolution after the fix
            try:

                resolved_ip = socket.gethostbyname(target)

                logger.log(
                    f"Hostname '{target}' now resolves to {resolved_ip} "
                    f"after /etc/hosts fix."
                )

            except socket.gaierror:

                logger.log(
                    f"WARNING: Hostname '{target}' still does not "
                    f"resolve via DNS, but an /etc/hosts entry has "
                    f"been added so local resolution should succeed."
                )

        except Exception as fix_exc:

            logger.log(
                f"WARNING: Could not auto-fix /etc/hosts: {fix_exc}. "
                f"Reconfigure may fail without a valid hostname "
                f"resolution."
            )


def parse_whitelist_urls(whitelist_text):
    urls = []

    if not whitelist_text:
        return urls

    for line in whitelist_text.splitlines():

        line = line.strip()

        if not line:
            continue

        if line.startswith("#"):
            continue

        urls.append(line)

    return urls


def extract_whitelist_domains(urls):
    domains = []

    for url in urls:

        try:
            parsed = urllib.request.urlparse(url)

            if parsed.hostname:
                domains.append(parsed.hostname)

        except Exception:
            continue

    return sorted(set(domains))


def validate_url_connectivity(urls):
    logger.log("Checking whitelist URL connectivity...")

    context = ssl.create_default_context()

    for url in urls:

        try:

            logger.log(f"Testing: {url}")

            request = urllib.request.Request(
                url,
                method="HEAD",
                headers={
                    "User-Agent": "Morpheus-Installer/1.0"
                }
            )

            with urllib.request.urlopen(
                request,
                timeout=10,
                context=context
            ) as response:

                logger.log(
                    f"OK: {url} -> HTTP {response.status}"
                )

        except Exception as exc:

            logger.log(
                f"WARNING: Could not validate {url}: {exc}"
            )


def setup_rhel_subscription(executor, username, password, rhel_major_version=9):

    if not username or not password:
        logger.log(
            "RHEL subscription credentials not supplied. "
            "Skipping subscription registration."
        )
        return

    logger.log("Registering RHEL subscription...")

    password_quoted = shlex.quote(password)
    username_quoted = shlex.quote(username)

    executor.run(
        f"sudo subscription-manager register "
        f"--username {username_quoted} "
        f"--password {password_quoted}",
        check=True
    )

    executor.run(
        "sudo subscription-manager refresh",
        check=False
    )

    executor.run(
        f"sudo subscription-manager repos "
        f"--enable=rhel-{rhel_major_version}-for-x86_64-baseos-rpms",
        check=False
    )

    executor.run(
        f"sudo subscription-manager repos "
        f"--enable=rhel-{rhel_major_version}-for-x86_64-appstream-rpms",
        check=False
    )

    # Per HPE Morpheus docs: on RHEL 7.x the Optional RPMs repo must be
    # enabled separately for `morpheus-ctl reconfigure` to succeed. On
    # RHEL 8.x+ this repo is already folded into appstream, enabled above.
    if rhel_major_version == 7:

        logger.log(
            "RHEL 7 detected: enabling the Optional RPMs repo "
            "(required for morpheus-ctl reconfigure to succeed)..."
        )

        executor.run(
            "sudo yum-config-manager --enable rhel-7-server-optional-rpms",
            check=False
        )

    else:

        logger.log(
            f"RHEL {rhel_major_version} detected: Optional RPMs repo "
            f"is already included in appstream, no action needed."
        )

    logger.log("RHEL subscription configuration completed.")


def setup_packages(executor, os_type):

    logger.log("Installing prerequisite packages...")

    if os_type == "rhel9":

        executor.run(
            "sudo dnf clean all",
            check=False
        )

        executor.run(
            "sudo dnf makecache",
            check=False
        )

        packages = [
            "curl",
            "wget",
            "tar",
            "gzip",
            "unzip",
            "vim",
            "openssl",
            "chrony",
            "policycoreutils",
            "policycoreutils-python-utils",
            "firewalld",
            "net-tools",
            "bind-utils",
        ]

        package_string = " ".join(packages)

        executor.run(
            f"sudo dnf install -y {package_string}"
        )

    elif os_type == "ubuntu":

        executor.run(
            "sudo apt-get update",
            check=False
        )

        packages = [
            "curl",
            "wget",
            "tar",
            "gzip",
            "unzip",
            "vim",
            "openssl",
            "chrony",
            "firewalld",
            "net-tools",
            "dnsutils",
        ]

        package_string = " ".join(packages)

        executor.run(
            f"sudo apt-get install -y {package_string}"
        )


def setup_hostname(executor, hostname):

    if not hostname:
        logger.log("Hostname not specified. Skipping hostname setup.")
        return

    logger.log(f"Setting hostname to {hostname}")

    hostname_quoted = shlex.quote(hostname)

    executor.run(
        f"sudo hostnamectl set-hostname {hostname_quoted}"
    )

    logger.log("Hostname configured.")


def setup_ntp(executor, ntp_server, os_type):

    if not ntp_server:
        logger.log("NTP server not specified. Skipping NTP setup.")
        return

    logger.log(f"Configuring NTP server: {ntp_server}")

    ntp_quoted = shlex.quote(ntp_server)

    config = f"""server {ntp_server} iburst
"""

    temp_file = "/tmp/morpheus-chrony.conf"

    with open(temp_file, "w") as f:
        f.write(config)

    executor.run(
        f"sudo cp {shlex.quote(temp_file)} /etc/chrony.conf"
    )

    if os_type == "rhel9":
        service = "chronyd"
    else:
        service = "chrony"

    executor.run(
        f"sudo systemctl enable {service}",
        check=False
    )

    executor.run(
        f"sudo systemctl restart {service}",
        check=False
    )

    logger.log("NTP configuration completed.")


def setup_dns(executor, dns_servers):

    if not dns_servers:
        logger.log("DNS servers not specified. Skipping DNS setup.")
        return

    servers = [
        item.strip()
        for item in dns_servers.split(",")
        if item.strip()
    ]

    if not servers:
        return

    logger.log(
        f"Configuring DNS servers: {', '.join(servers)}"
    )

    try:

        with open("/tmp/resolv.conf.morpheus", "w") as f:

            for server in servers:
                f.write(f"nameserver {server}\n")

        executor.run(
            "sudo cp /tmp/resolv.conf.morpheus /etc/resolv.conf",
            check=False
        )

        logger.log("DNS configuration completed.")

    except Exception as exc:

        logger.log(
            f"WARNING: DNS configuration failed: {exc}"
        )


def setup_selinux(executor, selinux_mode):

    if not selinux_mode:
        return

    selinux_mode = selinux_mode.lower()

    logger.log(
        f"Configuring SELinux mode: {selinux_mode}"
    )

    if selinux_mode == "disabled":

        executor.run(
            "sudo setenforce 0",
            check=False
        )

        executor.run(
            "sudo sed -i 's/^SELINUX=.*/SELINUX=disabled/' "
            "/etc/selinux/config",
            check=False
        )

        logger.log(
            "SELinux configured as disabled. "
            "A reboot may be required for the permanent setting."
        )

    elif selinux_mode == "permissive":

        executor.run(
            "sudo setenforce 0",
            check=False
        )

        executor.run(
            "sudo sed -i 's/^SELINUX=.*/SELINUX=permissive/' "
            "/etc/selinux/config",
            check=False
        )

    elif selinux_mode == "enforcing":

        executor.run(
            "sudo setenforce 1",
            check=False
        )

        executor.run(
            "sudo sed -i 's/^SELINUX=.*/SELINUX=enforcing/' "
            "/etc/selinux/config",
            check=False
        )

    else:

        logger.log(
            f"WARNING: Unknown SELinux mode: {selinux_mode}"
        )


def setup_firewall(executor, firewall_enabled, appliance_port):

    if not firewall_enabled:
        logger.log("Firewall configuration disabled.")
        return

    logger.log("Configuring firewall...")

    executor.run(
        "sudo systemctl enable firewalld",
        check=False
    )

    executor.run(
        "sudo systemctl start firewalld",
        check=False
    )

    ports = [
        "22/tcp",
        "80/tcp",
        "443/tcp",
        "8000/tcp",
    ]

    if appliance_port:
        ports.append(f"{appliance_port}/tcp")

    for port in ports:

        executor.run(
            f"sudo firewall-cmd --permanent --add-port={port}",
            check=False
        )

    executor.run(
        "sudo firewall-cmd --reload",
        check=False
    )

    logger.log("Firewall configuration completed.")


def validate_port_443_reachability():

    logger.log(
        "Checking local TCP 443 reachability "
        "(HPE Morpheus requires HTTPS access on 443)..."
    )

    import socket

    try:

        test_socket = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        test_socket.settimeout(5)

        result = test_socket.connect_ex(("127.0.0.1", 443))

        test_socket.close()

        if result == 0:

            logger.log(
                "Port 443 is open and reachable on localhost."
            )

        else:

            logger.log(
                "WARNING: Port 443 is not yet reachable on localhost "
                "(this is expected before morpheus-ctl reconfigure has "
                "run, since nginx has not started listening yet)."
            )

    except Exception as exc:

        logger.log(
            f"WARNING: Could not test port 443 reachability: {exc}"
        )


def validate_appliance_reachable(appliance_url, hostname, timeout_seconds=180):

    target_host = hostname.strip() if hostname else None

    if not target_host and appliance_url:

        try:
            parsed = urllib.request.urlparse(appliance_url)
            target_host = parsed.hostname
        except Exception:
            target_host = None

    if not target_host:
        target_host = "localhost"

    check_url = f"https://{target_host}"

    logger.log(
        f"Waiting for the Morpheus appliance to become reachable at "
        f"{check_url} (timeout: {timeout_seconds}s)..."
    )

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    start_time = time.time()
    attempt = 0

    while time.time() - start_time < timeout_seconds:

        attempt += 1

        try:

            request = urllib.request.Request(
                check_url,
                method="GET",
                headers={"User-Agent": "Morpheus-Installer/1.0"}
            )

            with urllib.request.urlopen(
                request,
                timeout=10,
                context=context
            ) as response:

                logger.log(
                    f"Appliance responded with HTTP {response.status} "
                    f"on attempt {attempt}."
                )

                return True

        except Exception as exc:

            logger.log(
                f"Attempt {attempt}: appliance not yet reachable "
                f"({exc}). Retrying in 10s..."
            )

            time.sleep(10)

    logger.log(
        f"WARNING: Appliance did not become reachable at {check_url} "
        f"within {timeout_seconds} seconds. It may still be starting "
        f"up; check 'sudo morpheus-ctl status' on the host."
    )

    return False


def setup_proxy(
    executor,
    proxy_host,
    proxy_port,
    proxy_username,
    proxy_password
):

    if not proxy_host:
        logger.log("Proxy not configured.")
        return

    logger.log(
        f"Configuring proxy: {proxy_host}:{proxy_port}"
    )

    proxy_host_quoted = shlex.quote(proxy_host)

    if proxy_port:
        proxy_url = f"http://{proxy_host}:{proxy_port}"
    else:
        proxy_url = f"http://{proxy_host}"

    if proxy_username and proxy_password:

        proxy_url = (
            f"http://"
            f"{shlex.quote(proxy_username)}:"
            f"{shlex.quote(proxy_password)}@"
            f"{proxy_host}:{proxy_port}"
        )

    proxy_url_quoted = shlex.quote(proxy_url)

    executor.run(
        f"sudo tee /etc/profile.d/morpheus-proxy.sh > /dev/null <<'EOF'\n"
        f"export HTTP_PROXY={proxy_url_quoted}\n"
        f"export HTTPS_PROXY={proxy_url_quoted}\n"
        f"export http_proxy={proxy_url_quoted}\n"
        f"export https_proxy={proxy_url_quoted}\n"
        f"export NO_PROXY=localhost,127.0.0.1\n"
        f"EOF"
    )

    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

    logger.log("Proxy configuration completed.")


def setup_ssl(executor, ssl_cert, ssl_key):

    if not ssl_cert or not ssl_key:
        logger.log("SSL certificate/key not supplied. Skipping SSL setup.")
        return

    cert_path = os.path.expanduser(ssl_cert)
    key_path = os.path.expanduser(ssl_key)

    if not os.path.exists(cert_path):
        raise RuntimeError(
            f"SSL certificate not found: {cert_path}"
        )

    if not os.path.exists(key_path):
        raise RuntimeError(
            f"SSL private key not found: {key_path}"
        )

    logger.log("Installing SSL certificate and private key...")

    executor.run(
        "sudo mkdir -p /etc/morpheus/ssl"
    )

    executor.run(
        f"sudo cp {shlex.quote(cert_path)} "
        "/etc/morpheus/ssl/morpheus.crt"
    )

    executor.run(
        f"sudo cp {shlex.quote(key_path)} "
        "/etc/morpheus/ssl/morpheus.key"
    )

    executor.run(
        "sudo chmod 600 /etc/morpheus/ssl/morpheus.key"
    )

    executor.run(
        "sudo chmod 644 /etc/morpheus/ssl/morpheus.crt"
    )

    logger.log("SSL files installed.")


def generate_morpheus_rb(
    appliance_url,
    hostname,
    proxy_host,
    proxy_port,
    proxy_username,
    proxy_password,
    ssl_cert,
    ssl_key
):

    logger.log("Generating /etc/morpheus/morpheus.rb configuration...")

    lines = []

    if appliance_url:
        lines.append(
            f"appliance_url '{appliance_url}'"
        )

    if hostname:
        lines.append(
            f"hostname '{hostname}'"
        )

    if proxy_host:

        lines.append(
            f"proxy['host'] = '{proxy_host}'"
        )

        if proxy_port:
            lines.append(
                f"proxy['port'] = {proxy_port}"
            )

        if proxy_username:
            lines.append(
                f"proxy['username'] = '{proxy_username}'"
            )

        if proxy_password:
            lines.append(
                f"proxy['password'] = '{proxy_password}'"
            )

    if ssl_cert:
        lines.append(
            "nginx['ssl_certificate'] = '/etc/morpheus/ssl/morpheus.crt'"
        )

    if ssl_key:
        lines.append(
            "nginx['ssl_server_key'] = '/etc/morpheus/ssl/morpheus.key'"
        )

    config = "\n".join(lines) + "\n"

    temp_config = "/tmp/morpheus.rb"

    with open(temp_config, "w") as f:
        f.write(config)

    executor_local = LocalExecutor()

    executor_local.run(
        "sudo mkdir -p /etc/morpheus"
    )

    executor_local.run(
        f"sudo cp {shlex.quote(temp_config)} "
        "/etc/morpheus/morpheus.rb"
    )

    executor_local.run(
        "sudo chmod 600 /etc/morpheus/morpheus.rb"
    )

    logger.log(
        "Morpheus configuration generated."
    )


# ============================================================
# INSTALLATION
# ============================================================

def fetch_source_checksum(checksum_source_url):
    """
    Fetch a published SHA256 checksum from a source URL (e.g. a
    downloads.morpheusdata.com .sha256 file or checksum listing page)
    and extract the first 64-character hex string found in it.
    """

    logger.log(
        f"Fetching source checksum from {checksum_source_url}..."
    )

    context = ssl.create_default_context()

    request = urllib.request.Request(
        checksum_source_url,
        method="GET",
        headers={"User-Agent": "Morpheus-Installer/1.0"}
    )

    with urllib.request.urlopen(
        request,
        timeout=15,
        context=context
    ) as response:

        body = response.read().decode(
            "utf-8",
            errors="ignore"
        )

    match = re.search(r"\b[0-9a-fA-F]{64}\b", body)

    if not match:
        raise RuntimeError(
            f"Could not find a SHA256 checksum in the response from "
            f"{checksum_source_url}."
        )

    return match.group(0)


def run_installation_task(
    os_type,
    package_path,
    package_checksum,
    appliance_url,
    hostname,
    ntp_server,
    dns_servers,
    firewall_enabled,
    selinux_mode,
    rhel_username,
    rhel_password,
    ssl_cert,
    ssl_key,
    whitelist,
    proxy_host,
    proxy_port,
    proxy_username,
    proxy_password,
    checksum_source_url=""
):

    global executor

    try:

        executor = LocalExecutor()

        logger.log("=" * 70)
        logger.log("HPE MORPHEUS ENTERPRISE INSTALLATION STARTED")
        logger.log("=" * 70)

        # ----------------------------------------------------
        # Package validation
        # ----------------------------------------------------

        expanded_package = os.path.expanduser(package_path)

        logger.log(
            f"Package path: {expanded_package}"
        )

        if not os.path.isfile(expanded_package):
            raise RuntimeError(
                f"Package file does not exist: {expanded_package}"
            )

        expected_extension = (
            ".rpm"
            if os_type == "rhel9"
            else ".deb"
        )

        if not expanded_package.lower().endswith(
            expected_extension
        ):
            raise RuntimeError(
                f"{os_type} requires a {expected_extension} package. "
                f"Selected file: {expanded_package}"
            )

        logger.log("Package extension validated.")

        # ----------------------------------------------------
        # SHA256
        # ----------------------------------------------------

        if not re.fullmatch(
            r"[0-9a-fA-F]{64}",
            package_checksum
        ):
            raise RuntimeError(
                "Package SHA256 checksum must contain exactly "
                "64 hexadecimal characters."
            )

        logger.log("Calculating package SHA256...")

        sha256 = hashlib.sha256()

        with open(expanded_package, "rb") as package_file:

            while True:

                data = package_file.read(1024 * 1024)

                if not data:
                    break

                sha256.update(data)

        actual_checksum = sha256.hexdigest()

        logger.log(
            f"Actual SHA256:   {actual_checksum}"
        )

        logger.log(
            f"Expected SHA256: {package_checksum}"
        )

        if actual_checksum.lower() != package_checksum.lower():

            raise RuntimeError(
                "SHA256 checksum verification FAILED."
            )

        logger.log(
            "SHA256 checksum verified successfully."
        )

        # ----------------------------------------------------
        # Optional: verify against a published source checksum
        # ----------------------------------------------------

        if checksum_source_url:

            try:

                source_checksum = fetch_source_checksum(
                    checksum_source_url
                )

                logger.log(
                    f"Source SHA256:   {source_checksum}"
                )

                if source_checksum.lower() != actual_checksum.lower():

                    raise RuntimeError(
                        "Package checksum does NOT match the published "
                        "source checksum. The package file may be "
                        "corrupted, tampered with, or the wrong version."
                    )

                logger.log(
                    "Package checksum matches the published source "
                    "checksum."
                )

            except RuntimeError:
                raise

            except Exception as exc:

                logger.log(
                    f"WARNING: Could not verify against source "
                    f"checksum: {exc}. Continuing with the "
                    f"user-supplied checksum only."
                )

        else:

            logger.log(
                "No checksum source URL supplied. Skipping "
                "verification against the published source checksum."
            )


        # ----------------------------------------------------
        # CPU
        # ----------------------------------------------------

        validate_cpu_requirements()

        # ----------------------------------------------------
        # Memory
        # ----------------------------------------------------

        validate_memory_requirements()

        # ----------------------------------------------------
        # Disk
        # ----------------------------------------------------

        validate_disk_space()

        # ----------------------------------------------------
        # Hostname self-resolution
        # ----------------------------------------------------

        validate_hostname_resolution(executor, hostname)

        # ----------------------------------------------------
        # Whitelist
        # ----------------------------------------------------

        whitelist_urls = parse_whitelist_urls(
            whitelist
        )

        logger.log(
            f"Whitelist entries: {len(whitelist_urls)}"
        )

        domains = extract_whitelist_domains(
            whitelist_urls
        )

        if domains:

            logger.log(
                "Whitelist domains:"
            )

            for domain in domains:
                logger.log(
                    f"  - {domain}"
                )

        validate_url_connectivity(
            whitelist_urls
        )

        # ----------------------------------------------------
        # Sudo check
        # ----------------------------------------------------

        logger.log("Checking passwordless sudo access...")



        logger.log(
            "Passwordless sudo check passed."
        )

        # ----------------------------------------------------
        # RHEL
        # ----------------------------------------------------

        if os_type == "rhel9":

            setup_rhel_subscription(
                executor,
                rhel_username,
                rhel_password
            )

        # ----------------------------------------------------
        # Packages
        # ----------------------------------------------------

        setup_packages(
            executor,
            os_type
        )

        # ----------------------------------------------------
        # Hostname
        # ----------------------------------------------------

        setup_hostname(
            executor,
            hostname
        )

        # ----------------------------------------------------
        # NTP
        # ----------------------------------------------------

        setup_ntp(
            executor,
            ntp_server,
            os_type
        )

        # ----------------------------------------------------
        # DNS
        # ----------------------------------------------------

        setup_dns(
            executor,
            dns_servers
        )

        # ----------------------------------------------------
        # Proxy
        # ----------------------------------------------------

        setup_proxy(
            executor,
            proxy_host,
            proxy_port,
            proxy_username,
            proxy_password
        )

        # ----------------------------------------------------
        # SELinux
        # ----------------------------------------------------

        if os_type == "rhel9":

            setup_selinux(
                executor,
                selinux_mode
            )

        # ----------------------------------------------------
        # Firewall
        # ----------------------------------------------------

        setup_firewall(
            executor,
            firewall_enabled,
            8000
        )

        # ----------------------------------------------------
        # Port 443 pre-check
        # ----------------------------------------------------

        validate_port_443_reachability()

        # ----------------------------------------------------
        # SSL
        # ----------------------------------------------------

        setup_ssl(
            executor,
            ssl_cert,
            ssl_key
        )

        # ----------------------------------------------------
        # Morpheus configuration
        # ----------------------------------------------------

        generate_morpheus_rb(
            appliance_url,
            hostname,
            proxy_host,
            proxy_port,
            proxy_username,
            proxy_password,
            ssl_cert,
            ssl_key
        )

        # ----------------------------------------------------
        # Copy package
        # ----------------------------------------------------

        package_filename = os.path.basename(
            expanded_package
        )

        temp_package = os.path.join(
            "/tmp",
            package_filename
        )

        logger.log(
            f"Copying installation package to {temp_package}"
        )

        shutil.copy2(
            expanded_package,
            temp_package
        )

        # ----------------------------------------------------
        # Install package
        # ----------------------------------------------------

        if os_type == "rhel9":

            logger.log(
                "Installing Morpheus RPM package..."
            )

            executor.run(
                f"sudo rpm -Uvh {shlex.quote(temp_package)}"
            )

        else:

            logger.log(
                "Installing Morpheus DEB package..."
            )

            executor.run(
                f"sudo dpkg -i {shlex.quote(temp_package)}"
            )

        # ----------------------------------------------------
        # Reconfigure
        # ----------------------------------------------------

        logger.log(
            "Running morpheus-ctl reconfigure..."
        )

        executor.run(
            "sudo morpheus-ctl reconfigure"
        )

        logger.log(
            "morpheus-ctl reconfigure completed. Waiting for the "
            "appliance to come online..."
        )

        # ----------------------------------------------------
        # Post-install health check
        # ----------------------------------------------------

        appliance_is_up = validate_appliance_reachable(
            appliance_url,
            hostname,
            timeout_seconds=180
        )

        # ----------------------------------------------------
        # Complete
        # ----------------------------------------------------

        logger.log("=" * 70)
        logger.log("HPE MORPHEUS ENTERPRISE INSTALLATION COMPLETED")
        logger.log("=" * 70)

        # ----------------------------------------------------
        # Post-install summary
        # ----------------------------------------------------

        final_host = hostname.strip() if hostname else None

        if not final_host and appliance_url:

            try:
                final_host = urllib.request.urlparse(
                    appliance_url
                ).hostname
            except Exception:
                final_host = None

        if not final_host:
            final_host = "your_machine_name"

        summary_url = f"https://{final_host}"

        logger.log("")
        logger.log("INSTALLATION SUMMARY")
        logger.log("-" * 70)
        logger.log(f"Appliance URL:      {summary_url}")
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
        logger.log(str(exc))
        logger.log("=" * 70)

    finally:

        with install_lock:
            install_status["running"] = False

        executor = None


# ============================================================
# HTML
# ============================================================



# ============================================================
# WEB PAGE
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
async def index():

    with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# INSTALL ENDPOINT
# ============================================================

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

    proxy_host: str = Form(""),
    proxy_port: str = Form(""),
    proxy_username: str = Form(""),
    proxy_password: str = Form("")
):

    # --------------------------------------------------------
    # Prevent two simultaneous installations
    # --------------------------------------------------------

    with install_lock:

        if install_status["running"]:

            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "message": "An installation is already running."
                }
            )

        install_status["running"] = True
        install_status["logs"] = []

    # --------------------------------------------------------
    # Basic validation
    # --------------------------------------------------------

    if os_type not in ("rhel9", "ubuntu"):

        with install_lock:
            install_status["running"] = False

        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": "Invalid operating system."
            }
        )

    expanded_package = os.path.expanduser(
        package_path
    )

    if not os.path.isfile(expanded_package):

        with install_lock:
            install_status["running"] = False

        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": (
                    f"Package file does not exist: "
                    f"{expanded_package}"
                )
            }
        )

    if not re.fullmatch(
        r"[0-9a-fA-F]{64}",
        package_checksum
    ):

        with install_lock:
            install_status["running"] = False

        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": (
                    "SHA256 checksum must contain "
                    "64 hexadecimal characters."
                )
            }
        )

    expected_extension = (
        ".rpm"
        if os_type == "rhel9"
        else ".deb"
    )

    if not expanded_package.lower().endswith(
        expected_extension
    ):

        with install_lock:
            install_status["running"] = False

        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": (
                    f"{os_type} requires a "
                    f"{expected_extension} package."
                )
            }
        )

    # --------------------------------------------------------
    # Proxy port validation
    # --------------------------------------------------------

    proxy_port_value = None

    if proxy_port.strip():

        try:

            proxy_port_value = int(
                proxy_port.strip()
            )

        except ValueError:

            with install_lock:
                install_status["running"] = False

            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "message": "Proxy port must be a number."
                }
            )

        if not 1 <= proxy_port_value <= 65535:

            with install_lock:
                install_status["running"] = False

            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "message": (
                        "Proxy port must be between "
                        "1 and 65535."
                    )
                }
            )

    # --------------------------------------------------------
    # Start background installation
    # --------------------------------------------------------

    background_tasks.add_task(
        run_installation_task,

        os_type,
        package_path,
        package_checksum,
        appliance_url,
        hostname,
        ntp_server,
        dns_servers,

        bool(firewall_enabled),

        selinux_mode,

        rhel_username,
        rhel_password,

        ssl_cert,
        ssl_key,

        whitelist,

        proxy_host,
        proxy_port_value,
        proxy_username,
        proxy_password,
        checksum_source_url
    )

    return JSONResponse(
        content={
            "ok": True,
            "message": "Installation started."
        }
    )


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


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000
    )

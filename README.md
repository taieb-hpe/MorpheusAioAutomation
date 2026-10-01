# HPE Morpheus Enterprise — AIO Installer

A local web app that automates a single-node (AIO) install of HPE Morpheus
Enterprise on **RHEL 9** or **Ubuntu/Debian**.

You can install:

- **Locally** — on the same machine that runs this web app, or  
- **Remotely** — over SSH on a target host (IP + user + SSH key or password)

Fill out the form in your browser, click install, and watch the logs stream live.

---

## Folder structure

```
your-project/
├── app.py
├── README.md
├── templates/
│   └── index.html
└── static/
    ├── style.css
    └── main.js
```

Keep this layout exactly as-is — `app.py` reads `templates/index.html` and
serves `static/` as relative paths, so it must be run from the
`your-project/` root.

---

## 1. What to have ready before you start

- [ ] **The Morpheus Appliance package file**
  - `.rpm` for RHEL 9
  - `.deb` for Ubuntu/Debian
  - Download it from your Morpheus/HPE downloads portal and note the full
    path where you saved it (e.g. `/home/morpheusauto/HPE_Morpheus_Enterprise_Appliance.rpm`)
  - The package path is always on the **control host** (the machine running
    this web app). For remote installs it is copied to the target over SCP.

- [ ] **The package’s SHA-512 checksum**
  - Exactly **128 hexadecimal characters**
  - Usually published next to the download link
  - Optional: a URL to the published checksum file, if you want the script
    to auto-verify it against the source

- [ ] **For remote (SSH) install only**
  - Target host IP or hostname
  - SSH user with **passwordless sudo** on the target
  - Prefer an **SSH private key** path on the control host  
    (e.g. `/root/.ssh/id_rsa`)
  - Or an SSH password (requires `sshpass` installed on the control host)

- [ ] **SSL certificate + private key** (optional, only if you’re not using
  the appliance’s self-signed cert)
  - A `.crt` (or `.pem`) certificate file
  - A `.key` private key file  
  - Paths are on the **control host**; files are copied to the target when needed

- [ ] **RHEL subscription credentials** (RHEL only, optional if the system
  is already registered)

- [ ] **Network details** you’ll be asked for: hostname, NTP server, DNS
  servers, proxy (if any)

- [ ] A target machine or VM meeting HPE’s AIO minimums: **4+ vCPU, 8GB+
  RAM (16GB recommended), 200GB+ free disk**

---

## 2. Install Python dependencies

From inside `your-project/`:

```bash
# Create a virtual environment (only needed once)
python3 -m venv venv

# Activate it
source venv/bin/activate

# Install dependencies (only needed once, or when they change)
pip install fastapi uvicorn python-multipart
```

> Re-activate the venv (`source venv/bin/activate`) every time you open a
> new terminal to work on this. You only need to reinstall packages if you
> add new ones later.

### Extra tools for remote install (control host only)

```bash
# SSH client is usually already present
# For password-based SSH (optional — key auth is preferred):
# RHEL / Rocky / Alma:
sudo dnf install -y sshpass
# Ubuntu / Debian:
sudo apt-get install -y sshpass
```

---

## 3. Run the app

```bash
python app.py
```

You should see output similar to:

```
Uvicorn running on http://0.0.0.0:8000
```

If the firewall is active on the control host, allow port 8000:

```bash
# RHEL / firewalld
sudo firewall-cmd --add-port=8000/tcp --permanent
sudo firewall-cmd --reload

# Ubuntu (ufw example)
sudo ufw allow 8000/tcp
```

---

## 4. Open it in your browser

```
http://localhost:8000
```

(or `http://<control-host-ip>:8000` if running on a remote machine).

---

## 5. Fill out the form and install

### Installation Target

1. **Install On**
   - **Local (this machine)** — install on the host running this app  
   - **Remote (SSH)** — install on another host over SSH  
2. For remote installs, fill in:
   - **Target Host / IP**
   - **SSH Port** (default 22)
   - **SSH User**
   - **SSH Private Key Path** (preferred — path on the control host)
   - **SSH Password** (fallback only; needs `sshpass` on the control host)

### Package & OS

3. Pick the **Operating System** (RHEL 9 or Ubuntu/Debian).
4. Enter the **Package Path** (on the control host) and the **SHA-512
   Checksum** (128 hex characters). Optionally add the **Source Checksum
   URL** to auto-verify against the published checksum.

### Appliance & network

5. Set the **Appliance URL** and **Hostname**.
6. Fill in **NTP**, **DNS**, **firewall**, **SELinux** (RHEL), **RHEL
   subscription**, **SSL cert/key**, and **proxy** as needed — leave
   anything blank that doesn’t apply.

### Whitelist (outbound connectivity check)

7. The **Whitelist** box lists URLs/hosts the target must be able to reach
   (Morpheus downloads, image registries, RHEL/MySQL repos, etc.).

   - Checks run **from the target machine** (local or remote), so they
     reflect the real network path the appliance will use.
   - Each entry is tested in stages: DNS → TCP → TLS (if HTTPS) → HTTP.
   - Any HTTP response (including 401/403/404) counts as reachable — the
     goal is network path validation, not application success.
   - Lines starting with `#` are comments; wildcards cannot be tested and
     are skipped.
   - **Strict whitelist** (checkbox): if enabled, the install aborts when
     any entry fails. If disabled, failures are logged as warnings and the
     install continues.

### Options & start

8. Optionally keep **Automatically cleanse any existing Morpheus
   installation** checked (destructive — removes previous data).
9. Click **Start Morpheus Installation**.
10. Watch the **Installation Logs** panel — it streams live and shows every
    check, command, and output.
11. When you see `HPE MORPHEUS ENTERPRISE INSTALLATION COMPLETED` and the
    summary block, open the **Appliance URL** shown there to finish the
    Morpheus setup wizard.

---

## Notes

- **Privileges**
  - On the **target** (local or remote): the effective user needs
    **passwordless sudo** (`sudo -n true` must succeed). Most steps
    (`subscription-manager`, `dnf`/`apt`, `firewall-cmd`, `morpheus-ctl`,
    etc.) require root.
  - On the **control host** for remote installs: the user running the app
    needs read access to the package, SSL files, and the SSH private key.

- **Remote install flow**
  - SSH connectivity is tested first.
  - Package and SSL files are copied to the target with `scp`.
  - Resource checks, package install, reconfigure, whitelist tests, and
    the final health check all run **on the target**.

- Only **one installation** can run at a time; a second start while one is
  in progress is rejected (HTTP 409).

- If something fails, the logs show `INSTALLATION FAILED` with the error.
  Fix the underlying issue and re-submit the form to retry.

- After a successful install, complete the initial appliance setup wizard
  in the browser at the Appliance URL from the log summary.

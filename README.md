# HPE Morpheus Enterprise — AIO Installer

A local web app that automates a single-node (AIO) install of HPE Morpheus
Enterprise on RHEL 9 or Ubuntu/Debian. Fill out a form in your browser,
click install, and watch the logs stream live.

---

## Folder structure

```
your-project/
├── app.py
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
- [ ] **The package's SHA256 checksum**
  - Usually published next to the download link
  - Optional: a URL to the published checksum file, if you want the script
    to auto-verify it against the source
- [ ] **SSL certificate + private key** (optional, only if you're not using
  the appliance's self-signed cert)
  - A `.crt` (or `.pem`) certificate file
  - A `.key` private key file
- [ ] **RHEL subscription credentials** (RHEL only, optional if the system
  is already registered)
- [ ] **Network details** you'll be asked for: hostname, NTP server, DNS
  servers, proxy (if any)
- [ ] A target machine or VM meeting HPE's AIO minimums: **4+ vCPU, 8GB+
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

---

## 3. Run the app

```bash
python app.py
```

You should see output similar to:

```
Uvicorn running on http://0.0.0.0:8000
when installing the tool on rhel allow the 8000 port
firewall-cmd --add-port=8000/tcp --permanent
```

---

## 4. Open it in your browser

Go to:

```
http://localhost:8000
```

(or `http://<server-ip>:8000` if running on a remote machine).

---

## 5. Fill out the form and install

1. Pick the **Operating System** (RHEL 9 or Ubuntu/Debian).
2. Enter the **Package Path** and **SHA256 Checksum** for the file you
   downloaded in Step 1. Optionally add the **Source Checksum URL** to
   auto-verify against the published checksum.
3. Set the **Appliance URL** and **Hostname**.
4. Fill in **NTP/DNS**, **firewall**, **SELinux**, **RHEL subscription**,
   **SSL cert/key**, **proxy**, and **whitelist** sections as needed —
   leave anything blank that doesn't apply to your environment.
5. Click **Start Morpheus Installation**.
6. Watch the **Installation Logs** panel — it streams live and will show
   every check, command, and its output as the install progresses.
7. When you see `HPE MORPHEUS ENTERPRISE INSTALLATION COMPLETED` and the
   summary block at the bottom of the logs, open the **Appliance URL**
   shown there to complete the Morpheus setup wizard.

---

## Notes

- The app must be run with `sudo`-capable privileges on the target host,
  since most installation steps (`subscription-manager`, `dnf`/`apt`,
  `firewall-cmd`, `morpheus-ctl`, etc.) require root.
- Only one installation can run at a time; starting a second one while
  one is in progress will be rejected.
- If something fails partway through, the logs panel will show
  `INSTALLATION FAILED` with the error — fix the underlying issue and
  re-submit the form to retry.

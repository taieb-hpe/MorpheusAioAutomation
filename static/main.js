const form = document.getElementById("installForm");
const installButton = document.getElementById("installButton");
const logBox = document.getElementById("logBox");
const statusBox = document.getElementById("status");

let polling = false;
let pollTimer = null;


function toggleOSFields() {

    const osType =
        document.getElementById("os_type").value;

    const rhelBlock =
        document.getElementById("rhelBlock");

    const selinuxBlock =
        document.getElementById("selinuxBlock");

    const packagePath =
        document.getElementById("package_path");

    if (osType === "rhel9") {

        rhelBlock.classList.remove("hidden");
        selinuxBlock.classList.remove("hidden");

        if (
            packagePath.value.endsWith(".deb") ||
            packagePath.value.trim() === ""
        ) {

            packagePath.value =
                "/home/morpheusauto/HPE_Morpheus_Enterprise_Appliance.rpm";
        }

    } else {

        rhelBlock.classList.add("hidden");
        selinuxBlock.classList.add("hidden");

        if (
            packagePath.value.endsWith(".rpm") ||
            packagePath.value.trim() === ""
        ) {

            packagePath.value =
                "/home/morpheusauto/HPE_Morpheus_Enterprise_Appliance.deb";
        }
    }
}


document
    .getElementById("os_type")
    .addEventListener(
        "change",
        toggleOSFields
    );


async function pollLogs() {

    if (!polling) {
        return;
    }

    try {

        const response = await fetch(
            "/logs",
            {
                method: "GET",
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw new Error(
                "HTTP " + response.status
            );
        }

        const data = await response.json();

        logBox.textContent =
            data.logs.join("\n");

        logBox.scrollTop =
            logBox.scrollHeight;

        if (data.running) {

            statusBox.textContent =
                "Installation is running...";

            installButton.disabled = true;

        } else {

            polling = false;

            installButton.disabled = false;

            statusBox.textContent =
                "Installation finished. Check the logs above.";

            return;
        }

    } catch (error) {

        logBox.textContent +=
            "\nERROR: Unable to retrieve logs: " +
            error.message;

    }

    if (polling) {

        pollTimer = setTimeout(
            pollLogs,
            2000
        );
    }
}


form.addEventListener(
    "submit",
    async function(event) {

        // Prevent normal browser form submission.
        event.preventDefault();

        if (polling) {
            return;
        }

        // Client-side validation for remote mode
        const targetMode = document.getElementById("target_mode").value;
        if (targetMode === "remote") {
            const host = document.getElementById("remote_host").value.trim();
            const user = document.getElementById("remote_user").value.trim();
            const key  = document.getElementById("remote_key_path").value.trim();
            const pass = document.getElementById("remote_password").value;
            if (!host) {
                statusBox.textContent = "Remote host / IP is required.";
                return;
            }
            if (!user) {
                statusBox.textContent = "SSH user is required for remote install.";
                return;
            }
            if (!key && !pass) {
                statusBox.textContent =
                    "Provide either an SSH private key path or a password.";
                return;
            }
        }

        // SHA-512 length check
        const checksum = document.getElementById("package_checksum").value.trim();
        if (checksum && checksum.length !== 128) {
            statusBox.textContent =
                "SHA-512 checksum must be exactly 128 hexadecimal characters " +
                "(got " + checksum.length + ").";
            return;
        }

        installButton.disabled = true;

        statusBox.textContent =
            "Starting installation...";

        logBox.textContent =
            "Submitting installation request...";

        const formData =
            new FormData(form);

        try {

            const response = await fetch(
                "/install",
                {
                    method: "POST",
                    body: formData,
                    headers: {
                        "Accept": "application/json"
                    },
                    cache: "no-store"
                }
            );

            const data =
                await response.json();

            if (!response.ok) {

                throw new Error(
                    data.message ||
                    data.detail ||
                    "Installation request failed."
                );
            }

            statusBox.textContent =
                data.message ||
                "Installation started.";

            polling = true;

            pollLogs();

        } catch (error) {

            installButton.disabled = false;

            polling = false;

            statusBox.textContent =
                "Installation could not be started.";

            logBox.textContent =
                "ERROR: " + error.message;
        }
    }
);


// Initialise UI state
toggleOSFields();

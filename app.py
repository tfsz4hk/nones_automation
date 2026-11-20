
from flask import Flask, render_template, request, redirect, url_for, flash
from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)
import ipaddress
import json
import os
import platform
import re
import subprocess
import tempfile

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "local-development-key-change-this"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "devices.json")

PING_TIMEOUT = 3
SSH_TIMEOUT = 10

SUPPORTED_DEVICES = {
    "linux": "Linux",
    "cisco_ios": "Cisco IOS"
}

ALLOWED_COMMANDS = {
    "linux": [
        "hostname",
        "ip addr",
        "ip route",
        "uptime",
        "df -h",
        "free -h",
        "whoami",
        "uname -a"
    ],

    "cisco_ios": [
        "show version",
        "show ip interface brief",
        "show interfaces",
        "show ip route",
        "show running-config",
        "show ip protocols",
        "show vlan brief"
    ]
}

def load_devices():
    """Load devices from JSON storage."""

    if not os.path.exists(DATA_FILE):
        return []

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            devices = json.load(file)

        if not isinstance(devices, list):
            raise ValueError("Invalid device database format.")

        for device in devices:
            if not isinstance(device, dict):
                raise ValueError("Invalid device record.")

            if not all(
                isinstance(device.get(field), str)
                for field in ("name", "host", "device_type")
            ):
                raise ValueError("Invalid device information.")

            if device["device_type"] not in SUPPORTED_DEVICES:
                raise ValueError("Unsupported device type in database.")

        return devices

    except json.JSONDecodeError as exc:
        raise ValueError("Device database contains invalid JSON.") from exc


def save_devices(devices):
    """Save devices safely using atomic file replacement."""

    temp_file = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=BASE_DIR,
            delete=False,
            suffix=".tmp"
        ) as file:

            json.dump(devices, file, indent=4)
            file.flush()
            os.fsync(file.fileno())
            temp_file = file.name

        os.replace(temp_file, DATA_FILE)

    finally:
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)

def valid_host(host):
    """Validate an IP address or hostname."""

    if not host or len(host) > 253:
        return False

    host = host.strip()

    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass

    hostname_pattern = (
        r"^(?=.{1,253}$)"
        r"(?:[a-zA-Z0-9]"
        r"(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
        r"(?:\.(?:[a-zA-Z0-9]"
        r"(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?))*$"
    )

    return bool(re.fullmatch(hostname_pattern, host))

def ping_host(host):
    """Check whether a network device responds to ping."""

    is_windows = platform.system().lower() == "windows"

    if is_windows:
        command = [
            "ping", "-n", "1", "-w", "1500", host
        ]
    else:
        command = [
            "ping", "-c", "1", "-W", "2", host
        ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PING_TIMEOUT,
            check=False
        )

        return result.returncode == 0

    except (subprocess.TimeoutExpired, OSError):
        return False


def get_device_status(devices):
    """Calculate online and offline device counts."""

    online = 0

    for device in devices:
        device["online"] = ping_host(device["host"])

        if device["online"]:
            online += 1

    offline = len(devices) - online

    return online, offline

@app.route("/")
def index():

    try:
        devices = load_devices()

    except (ValueError, OSError):
        app.logger.exception("Unable to load device database.")
        flash("Unable to read device inventory.", "error")
        devices = []

    online, offline = get_device_status(devices)

    return render_template(
        "index.html",
        devices=devices,
        online=online,
        offline=offline
    )


@app.route("/devices/add", methods=["POST"])
def add_device():

    name = request.form.get("name", "").strip()
    host = request.form.get("host", "").strip()
    device_type = request.form.get(
        "device_type", "linux"
    ).strip()

    if not name or len(name) > 100:
        flash("Please enter a valid device name.", "error")
        return redirect(url_for("index"))

    if not valid_host(host):
        flash("Please enter a valid IP address or hostname.", "error")
        return redirect(url_for("index"))

    if device_type not in SUPPORTED_DEVICES:
        flash("Unsupported device type.", "error")
        return redirect(url_for("index"))

    try:
        devices = load_devices()

        if any(
            device["host"].lower() == host.lower()
            for device in devices
        ):
            flash("This device already exists.", "error")
            return redirect(url_for("index"))

        devices.append({
            "name": name,
            "host": host,
            "device_type": device_type
        })

        save_devices(devices)

        flash(f"{name} added successfully!", "success")

    except (ValueError, OSError):
        app.logger.exception("Failed to add device.")
        flash("Unable to save the new device.", "error")

    return redirect(url_for("index"))

@app.route(
    "/devices/delete/<int:device_id>",
    methods=["POST"]
)
def delete_device(device_id):

    try:
        devices = load_devices()

        if not 0 <= device_id < len(devices):
            flash("Device not found.", "error")
            return redirect(url_for("index"))

        removed = devices.pop(device_id)

        save_devices(devices)

        flash(
            f"{removed['name']} was removed successfully.",
            "success"
        )

    except (ValueError, OSError):
        app.logger.exception("Failed to delete device.")
        flash("Unable to delete device.", "error")

    return redirect(url_for("index"))

@app.route(
    "/devices/<int:device_id>/command",
    methods=["GET", "POST"]
)
def command(device_id):

    try:
        devices = load_devices()

    except (ValueError, OSError):
        app.logger.exception("Unable to load devices.")
        flash("Unable to read device inventory.", "error")
        return redirect(url_for("index"))

    if not 0 <= device_id < len(devices):
        flash("Device not found.", "error")
        return redirect(url_for("index"))

    device = devices[device_id]

    output = None
    error = None

    commands = ALLOWED_COMMANDS.get(
        device["device_type"], []
    )

    if request.method == "POST":

        selected = request.form.get("command", "")

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        enable_secret = request.form.get("enable_secret", "")

        if selected not in commands:
            error = "This command is not permitted."

        elif not username or not password:
            error = "Username and password are required."

        else:

            params = {
                "device_type": device["device_type"],
                "host": device["host"],
                "username": username,
                "password": password,
                "timeout": SSH_TIMEOUT,
                "conn_timeout": SSH_TIMEOUT,
                "auth_timeout": SSH_TIMEOUT,
                "banner_timeout": SSH_TIMEOUT,
                "fast_cli": False
            }

            if device["device_type"] == "cisco_ios":
                params["secret"] = enable_secret

            try:

                with ConnectHandler(**params) as connection:

            
                    if (
                        device["device_type"] == "cisco_ios"
                        and enable_secret
                    ):
                        connection.enable()

                    output = connection.send_command(
                        selected,
                        read_timeout=SSH_TIMEOUT
                    )

                    if not output:
                        output = "Command executed successfully. No output returned."

            except NetmikoAuthenticationException:
                error = "Authentication failed. Check your username and password."

            except NetmikoTimeoutException:
                error = "Connection timed out. Check the device IP and SSH service."

            except (OSError, ValueError):
                app.logger.exception("SSH connection error.")
                error = "Unable to connect to the device."

            except Exception:
                app.logger.exception("Unexpected command execution error.")
                error = "An unexpected error occurred while executing the command."

    return render_template(
        "command.html",
        device=device,
        commands=commands,
        output=output,
        error=error
    )

if __name__ == "__main__":

    print("=" * 45)
    print("       NETPILOT NETWORK AUTOMATION")
    print("=" * 45)
    print("Dashboard: http://127.0.0.1:5000")
    print("Environment: Local VirtualBox Lab")
    print("Status: Starting server...")
    print("=" * 45)

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )

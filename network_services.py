import os
import platform
import socket
import subprocess

import core_config as config
from camera import DEFAULT_IP


def ipconfig_wifi_ipv4():
    if platform.system().lower() != "windows":
        return None

    try:
        result = subprocess.run(
            ["ipconfig"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except Exception:
        return None

    current_adapter = ""
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if line and not line.startswith(" ") and line.endswith(":"):
            current_adapter = line.strip(":")
            continue

        if "Wireless LAN adapter Wi-Fi" not in current_adapter:
            continue

        if "IPv4" in line and ":" in line:
            return line.split(":", 1)[1].strip()

    return None


def fallback_local_ipv4():
    target = os.getenv("CAMERA_IP", DEFAULT_IP) or "8.8.8.8"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None
    finally:
        sock.close()


def windows_firewall_port_status(port):
    if platform.system().lower() != "windows":
        return {"status": "unknown", "message": "Verificacao automatica de firewall disponivel apenas no Windows."}

    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=Camera UI 8080"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "message": "Nao foi possivel verificar o firewall em tempo habil."}
    except Exception:
        return {"status": "unknown", "message": "Nao foi possivel consultar o firewall."}

    if result.returncode != 0:
        return {"status": "not_found", "message": f"Nao encontrei regra inbound TCP permitindo a porta {port}."}

    output = result.stdout.lower()
    expected_port = str(port)
    if "allow" in output and "in" in output and ("tcp" in output or "any" in output) and expected_port in output:
        return {"status": "allowed", "message": f"Existe regra inbound TCP permitindo a porta {port}."}
    return {"status": "not_found", "message": f"Nao encontrei regra inbound TCP permitindo a porta {port}."}


def network_status():
    ipv4 = ipconfig_wifi_ipv4() or fallback_local_ipv4()
    port = config.APP_PORT
    return {
        "host": config.APP_HOST,
        "port": port,
        "ipv4": ipv4,
        "url": f"http://{ipv4}:{port}" if ipv4 else None,
        "firewall": windows_firewall_port_status(port),
    }

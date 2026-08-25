#!/usr/bin/env python3
import base64
import hashlib
import hmac
import http.client
import json
import os
import ipaddress
import shlex
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


CONTAINER = os.environ.get("AWG_CONTAINER", "amnezia-awg2")
INTERFACE = os.environ.get("AWG_INTERFACE", "awg0")
SOCKET_PATH = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "peers.json"
TRAFFIC_FILE = DATA_DIR / "traffic.json"
ONLINE_SECONDS = int(os.environ.get("ONLINE_SECONDS", "180"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
ADMIN_USER = os.environ.get("PANEL_USER", "admin")
ADMIN_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
TLS_CERT = os.environ.get("TLS_CERT", "")
TLS_KEY = os.environ.get("TLS_KEY", "")
VPN_ENDPOINT = os.environ.get("VPN_ENDPOINT", "SERVER_IP:41824")
VPN_NETWORK = os.environ.get("VPN_NETWORK", "10.8.1.0/24")
VPN_DNS = os.environ.get("VPN_DNS", "1.1.1.1")
CLIENT_ALLOWED_IPS = os.environ.get("CLIENT_ALLOWED_IPS", "0.0.0.0/0, ::/0")
CLIENT_MTU = os.environ.get("CLIENT_MTU", "1420")
CLIENT_KEEPALIVE = os.environ.get("CLIENT_KEEPALIVE", "25")
ROUTING_DIR = DATA_DIR / "routing"
ROUTING_FILE = ROUTING_DIR / "roscomvpn-amnezia.json"
ROUTING_META_FILE = ROUTING_DIR / "roscomvpn-amnezia.meta.json"
ROUTING_REFRESH_SECONDS = int(os.environ.get("ROSCOMVPN_REFRESH_SECONDS", "86400"))
ROSCOMVPN_INCLUDE_DIRECT = os.environ.get("ROSCOMVPN_INCLUDE_DIRECT", "false").lower() in ("1", "true", "yes")
ROSCOMVPN_GEOIP_SOURCES = {
    "direct": os.environ.get(
        "ROSCOMVPN_DIRECT_URL",
        "https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geoip/release/text/direct.txt",
    ),
    "whitelist": os.environ.get(
        "ROSCOMVPN_WHITELIST_URL",
        "https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geoip/release/text/whitelist.txt",
    ),
    "private": os.environ.get(
        "ROSCOMVPN_PRIVATE_URL",
        "https://cdn.jsdelivr.net/gh/hydraponique/roscomvpn-geoip/release/text/private.txt",
    ),
}
ROSCOMVPN_GEOIP_CATEGORIES = (("direct",) if ROSCOMVPN_INCLUDE_DIRECT else ()) + ("whitelist", "private")
ROUTING_MODE = "full" if ROSCOMVPN_INCLUDE_DIRECT else "compatible"
MAX_REQUEST_BYTES = 16 * 1024
AUTH_FAILURE_LIMIT = int(os.environ.get("AUTH_FAILURE_LIMIT", "5"))
AUTH_FAILURE_WINDOW = int(os.environ.get("AUTH_FAILURE_WINDOW", "600"))
AUTH_BLOCK_SECONDS = int(os.environ.get("AUTH_BLOCK_SECONDS", "900"))
REQUEST_RATE_LIMIT = int(os.environ.get("REQUEST_RATE_LIMIT", "180"))
REQUEST_RATE_WINDOW = int(os.environ.get("REQUEST_RATE_WINDOW", "60"))
AUTH_LOG_FILE = DATA_DIR / "auth.log"

STATE_LOCK = threading.Lock()
SAMPLE_LOCK = threading.Lock()
LAST_SAMPLE = {}
ROUTING_LOCK = threading.Lock()
AUTH_LOCK = threading.Lock()
AUTH_STATE = {}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, unix_socket_path):
        super().__init__("localhost")
        self.unix_socket_path = unix_socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.unix_socket_path)


def docker_request(method, path, body=None):
    payload = None
    headers = {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn = UnixHTTPConnection(SOCKET_PATH)
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    if response.status >= 300:
        raise RuntimeError(f"Docker API {method} {path} failed: {response.status} {data.decode('utf-8', 'replace')}")
    return data


def docker_exec(command):
    create = {
        "AttachStdout": True,
        "AttachStderr": True,
        "Tty": False,
        "Cmd": ["sh", "-lc", command],
    }
    data = docker_request("POST", f"/containers/{CONTAINER}/exec", create)
    exec_id = json.loads(data.decode("utf-8"))["Id"]
    output = docker_request("POST", f"/exec/{exec_id}/start", {"Detach": False, "Tty": False})
    inspect = docker_request("GET", f"/exec/{exec_id}/json")
    exit_code = json.loads(inspect.decode("utf-8")).get("ExitCode")
    text = demux_docker_output(output).decode("utf-8", "replace")
    if exit_code:
        raise RuntimeError(text.strip() or f"command exited with {exit_code}")
    return text


def demux_docker_output(output):
    if len(output) < 8:
        return output
    chunks = []
    pos = 0
    while pos + 8 <= len(output):
        stream_type = output[pos]
        size = int.from_bytes(output[pos + 4 : pos + 8], "big")
        next_pos = pos + 8 + size
        if stream_type not in (1, 2) or size < 0 or next_pos > len(output):
            return output
        chunks.append(output[pos + 8 : next_pos])
        pos = next_pos
    if pos != len(output):
        return output
    return b"".join(chunks)


def load_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA_DIR, 0o700)
    if not STATE_FILE.exists():
        return {"names": {}, "disabled": {}, "saved": {}, "traffic": {}, "clients": {}, "order": []}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except json.JSONDecodeError:
        state = {"names": {}, "disabled": {}, "saved": {}, "traffic": {}, "clients": {}, "order": []}
    state.setdefault("names", {})
    state.setdefault("disabled", {})
    state.setdefault("saved", {})
    state.setdefault("traffic", {})
    state.setdefault("clients", {})
    state.setdefault("order", [])
    return state


def save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA_DIR, 0o700)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)
    os.chmod(STATE_FILE, 0o600)


def load_routing_meta():
    if not ROUTING_META_FILE.exists():
        return {}
    try:
        with ROUTING_META_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def routing_status():
    meta = load_routing_meta()
    generated_at = int(meta.get("generatedAt", 0) or 0)
    return {
        "available": ROUTING_FILE.exists(),
        "generatedAt": generated_at or None,
        "count": int(meta.get("count", 0) or 0),
        "stale": not generated_at or meta.get("mode") != ROUTING_MODE or time.time() - generated_at >= ROUTING_REFRESH_SECONDS,
        "sources": meta.get("sources", [ROSCOMVPN_GEOIP_SOURCES[name] for name in ROSCOMVPN_GEOIP_CATEGORIES]),
        "mode": meta.get("mode", ROUTING_MODE),
    }


def traffic_status():
    if not TRAFFIC_FILE.exists():
        return {"available": False, "error": "Traffic data is not ready yet"}
    try:
        with TRAFFIC_FILE.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "error": str(exc)}
    updated_at = int(payload.get("updatedAt", 0) or 0)
    payload["available"] = True
    payload["stale"] = not updated_at or time.time() - updated_at > 15 * 60
    return payload


def fetch_cidr_list(url):
    request = urllib.request.Request(url, headers={"User-Agent": "AWG-Panel-RoscomVPN/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError(f"Routing source is too large: {url}")

    cidrs = []
    for line in raw.decode("utf-8").splitlines():
        item = line.split("#", 1)[0].strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid CIDR in {url}: {item}") from exc
        if isinstance(network, ipaddress.IPv4Network):
            cidrs.append(str(network))
    if not cidrs:
        raise ValueError(f"Routing source has no IPv4 CIDRs: {url}")
    return cidrs


def refresh_routing():
    # Amnezia's import format is an array of {hostname, ip}; CIDRs go into hostname.
    # Amnezia rejects the 42k+ entry full list. whitelist + private is the compatible
    # default; direct can be included explicitly for clients that support it.
    with ROUTING_LOCK:
        seen = set()
        cidrs = []
        for category in ROSCOMVPN_GEOIP_CATEGORIES:
            url = ROSCOMVPN_GEOIP_SOURCES[category]
            for cidr in fetch_cidr_list(url):
                if cidr not in seen:
                    seen.add(cidr)
                    cidrs.append(cidr)

        payload = [{"hostname": cidr, "ip": ""} for cidr in cidrs]
        metadata = {
            "generatedAt": int(time.time()),
            "count": len(payload),
            "sources": [ROSCOMVPN_GEOIP_SOURCES[name] for name in ROSCOMVPN_GEOIP_CATEGORIES],
            "mode": ROUTING_MODE,
        }
        ROUTING_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(ROUTING_DIR, 0o700)
        for path, data in ((ROUTING_FILE, payload), (ROUTING_META_FILE, metadata)):
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                os.chmod(tmp, 0o600)
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            tmp.replace(path)
            os.chmod(path, 0o600)
        return routing_status()


def routing_updater():
    while True:
        try:
            if routing_status()["stale"]:
                status = refresh_routing()
                print(f"RoscomVPN routing updated: {status['count']} CIDRs", flush=True)
        except Exception as exc:
            print(f"RoscomVPN routing update failed: {exc}", flush=True)
        time.sleep(max(60, ROUTING_REFRESH_SECONDS))


def key_id(public_key):
    return hashlib.sha256(public_key.encode("utf-8")).hexdigest()[:10]


def parse_dump(dump):
    interface = None
    peers = []
    now = int(time.time())
    for raw in dump.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) > 10:
            if parts[0] == INTERFACE:
                interface = {"name": parts[0], "publicKey": parts[2], "listenPort": parts[3]}
            else:
                interface = {"name": INTERFACE, "publicKey": parts[1], "listenPort": parts[2]}
            continue
        if len(parts) >= 9 and parts[0] == INTERFACE:
            public_key, psk, endpoint, allowed_ips, handshake_s, rx_s, tx_s, keepalive = parts[1:9]
        elif len(parts) >= 8:
            public_key, psk, endpoint, allowed_ips, handshake_s, rx_s, tx_s, keepalive = parts[:8]
        else:
            continue
        handshake = int(handshake_s) if handshake_s.isdigit() else 0
        rx = int(rx_s) if rx_s.isdigit() else 0
        tx = int(tx_s) if tx_s.isdigit() else 0
        peers.append(
            {
                "publicKey": public_key,
                "presharedKey": psk,
                "endpoint": None if endpoint in ("(none)", "(null)") else endpoint,
                "allowedIps": allowed_ips,
                "latestHandshake": handshake,
                "rxBytes": rx,
                "txBytes": tx,
                "persistentKeepalive": keepalive,
                "online": bool(handshake and now - handshake <= ONLINE_SECONDS),
                "secondsSinceHandshake": now - handshake if handshake else None,
            }
        )
    return interface, peers


def parse_interface_dump(dump):
    for raw in dump.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) > 10:
            if parts[0] == INTERFACE:
                parts = parts[1:]
            interface = {
                "privateKey": parts[0],
                "publicKey": parts[1],
                "listenPort": parts[2],
                "jc": parts[3],
                "jmin": parts[4],
                "jmax": parts[5],
                "s1": parts[6],
                "s2": parts[7],
                "s3": parts[8],
                "s4": parts[9],
                "h1": parts[10],
                "h2": parts[11],
                "h3": parts[12],
                "h4": parts[13],
            }
            # AWG 2.x dumps stop after H1-H4 (or I1-I5). AWG 3.x appends
            # protocol parameters after the five optional signature packets.
            awg3_fields = {
                "headerProtectionKey": 19,
                "contentPaddingAddition": 20,
                "rekeyAfterTime": 21,
                "rekeyTimeout": 22,
                "rejectAfterTime": 23,
                "keepaliveTimeout": 24,
                "maxHandshakeAttempts": 25,
                "randomTrailers": 26,
                "disableCookies": 27,
            }
            for key, index in awg3_fields.items():
                value = parts[index] if index < len(parts) else ""
                if value not in ("", "(null)", "(none)"):
                    interface[key] = value
            interface["protocolVersion"] = "3" if interface.get("headerProtectionKey") not in (None, "(off)", "off") else "2"
            return interface
    raise ValueError("Interface dump is empty")


def get_interface_config():
    return parse_interface_dump(docker_exec(f"awg show {shlex.quote(INTERFACE)} dump"))


def human_peer_name(state, peer):
    public_key = peer["publicKey"]
    return state["names"].get(public_key) or peer.get("allowedIps") or key_id(public_key)


def sample_rates(peers):
    now = time.time()
    enriched = []
    with SAMPLE_LOCK:
        for peer in peers:
            public_key = peer["publicKey"]
            prev = LAST_SAMPLE.get(public_key)
            rx_rate = 0
            tx_rate = 0
            if prev:
                elapsed = max(1, now - prev["ts"])
                rx_rate = max(0, (peer["rxBytes"] - prev["rx"]) / elapsed)
                tx_rate = max(0, (peer["txBytes"] - prev["tx"]) / elapsed)
            LAST_SAMPLE[public_key] = {"ts": now, "rx": peer["rxBytes"], "tx": peer["txBytes"]}
            peer["rxRate"] = rx_rate
            peer["txRate"] = tx_rate
            enriched.append(peer)
    return enriched


def apply_traffic_totals(state, peer):
    public_key = peer["publicKey"]
    raw_rx = peer["rxBytes"]
    raw_tx = peer["txBytes"]
    traffic = state["traffic"].setdefault(
        public_key,
        {"rxOffset": 0, "txOffset": 0, "lastRawRx": raw_rx, "lastRawTx": raw_tx},
    )
    rx_offset = int(traffic.get("rxOffset", 0))
    tx_offset = int(traffic.get("txOffset", 0))
    last_raw_rx = int(traffic.get("lastRawRx", 0))
    last_raw_tx = int(traffic.get("lastRawTx", 0))

    if raw_rx < last_raw_rx:
        rx_offset += last_raw_rx
    if raw_tx < last_raw_tx:
        tx_offset += last_raw_tx

    traffic["rxOffset"] = rx_offset
    traffic["txOffset"] = tx_offset
    traffic["lastRawRx"] = raw_rx
    traffic["lastRawTx"] = raw_tx
    peer["rxBytes"] = rx_offset + raw_rx
    peer["txBytes"] = tx_offset + raw_tx


def sync_peer_order(state, peers):
    current_keys = [peer["publicKey"] for peer in peers]
    current_set = set(current_keys)
    existing_order = [public_key for public_key in state.get("order", []) if public_key in current_set]

    if existing_order:
        missing = [public_key for public_key in current_keys if public_key not in existing_order]
        order = existing_order + missing
    else:
        order = [
            peer["publicKey"]
            for peer in sorted(
                peers,
                key=lambda p: (not p.get("enabled", True), not p.get("online", False), p.get("name", "")),
            )
        ]

    state["order"] = order
    return {public_key: index for index, public_key in enumerate(order)}


def get_status():
    dump = docker_exec(f"awg show {shlex.quote(INTERFACE)} dump")
    interface, active_peers = parse_dump(dump)
    device = parse_interface_dump(dump)
    if interface:
        interface["protocolVersion"] = device["protocolVersion"]
    active_keys = {peer["publicKey"] for peer in active_peers}

    with STATE_LOCK:
        state = load_state()
        peers = []
        for peer in active_peers:
            public_key = peer["publicKey"]
            apply_traffic_totals(state, peer)
            state["saved"][public_key] = {
                "publicKey": public_key,
                "presharedKey": peer["presharedKey"],
                "endpoint": peer["endpoint"],
                "allowedIps": peer["allowedIps"],
                "persistentKeepalive": peer["persistentKeepalive"],
            }
            peer["id"] = key_id(public_key)
            peer["name"] = human_peer_name(state, peer)
            peer["enabled"] = not state["disabled"].get(public_key, False)
            peer["hasConfig"] = public_key in state["clients"]
            peer.pop("presharedKey", None)
            peers.append(peer)

        for public_key, saved in state["saved"].items():
            if public_key in active_keys:
                continue
            traffic = state["traffic"].get(public_key, {})
            rx_total = int(traffic.get("rxOffset", 0)) + int(traffic.get("lastRawRx", 0))
            tx_total = int(traffic.get("txOffset", 0)) + int(traffic.get("lastRawTx", 0))
            peer = {
                "publicKey": public_key,
                "id": key_id(public_key),
                "name": state["names"].get(public_key) or saved.get("allowedIps") or key_id(public_key),
                "enabled": False,
                "online": False,
                "endpoint": saved.get("endpoint"),
                "allowedIps": saved.get("allowedIps"),
                "latestHandshake": 0,
                "secondsSinceHandshake": None,
                "rxBytes": rx_total,
                "txBytes": tx_total,
                "rxRate": 0,
                "txRate": 0,
                "persistentKeepalive": saved.get("persistentKeepalive", "off"),
                "hasConfig": public_key in state["clients"],
            }
            peers.append(peer)
        peer_order = sync_peer_order(state, peers)
        save_state(state)

    peers = sample_rates(peers)
    peers.sort(key=lambda p: (peer_order.get(p["publicKey"], len(peer_order)), p.get("name", "")))
    return {
        "container": CONTAINER,
        "interface": interface or {"name": INTERFACE},
        "onlineSeconds": ONLINE_SECONDS,
        "peers": peers,
        "updatedAt": int(time.time()),
    }


def find_peer(public_key):
    status = get_status()
    for peer in status["peers"]:
        if peer["publicKey"] == public_key:
            return peer
    return None


def reconcile_disabled(public_key):
    peer = find_peer(public_key)
    if peer and peer.get("enabled") and peer.get("publicKey"):
        docker_exec(f"awg set {shlex.quote(INTERFACE)} peer {shlex.quote(public_key)} remove")


def disable_peer(public_key):
    status = get_status()
    peer = next((item for item in status["peers"] if item["publicKey"] == public_key), None)
    if not peer:
        raise ValueError("Peer not found")
    with STATE_LOCK:
        state = load_state()
        if peer.get("allowedIps"):
            state["saved"][public_key] = {
                "publicKey": public_key,
                "presharedKey": peer.get("presharedKey") or state["saved"].get(public_key, {}).get("presharedKey"),
                "endpoint": peer.get("endpoint"),
                "allowedIps": peer.get("allowedIps"),
                "persistentKeepalive": peer.get("persistentKeepalive", "off"),
            }
        state["disabled"][public_key] = True
        save_state(state)
    if peer.get("enabled", True):
        docker_exec(f"awg set {shlex.quote(INTERFACE)} peer {shlex.quote(public_key)} remove")


def enable_peer(public_key):
    with STATE_LOCK:
        state = load_state()
        saved = state["saved"].get(public_key)
        if not saved or not saved.get("allowedIps"):
            raise ValueError("Saved peer config is missing; cannot re-enable")
        state["disabled"][public_key] = False
        save_state(state)

    commands = []
    psk = saved.get("presharedKey")
    if psk and psk not in ("(none)", "(null)"):
        commands.append("tmp=$(mktemp)")
        commands.append(f"printf %s {shlex.quote(psk)} > \"$tmp\"")
        psk_arg = " preshared-key \"$tmp\""
    else:
        psk_arg = ""
    command = (
        f"awg set {shlex.quote(INTERFACE)} peer {shlex.quote(public_key)}"
        f"{psk_arg} allowed-ips {shlex.quote(saved['allowedIps'])}"
    )
    keepalive = saved.get("persistentKeepalive")
    if keepalive and keepalive not in ("off", "(none)", "(null)"):
        command += f" persistent-keepalive {shlex.quote(keepalive)}"
    commands.append(command)
    if psk_arg:
        commands.append("rm -f \"$tmp\"")
    docker_exec(" && ".join(commands))


def delete_peer(public_key):
    with STATE_LOCK:
        state = load_state()
        known = any(public_key in state.get(section, {}) for section in ("names", "disabled", "saved", "traffic", "clients"))

    remove_error = None
    try:
        docker_exec(f"awg set {shlex.quote(INTERFACE)} peer {shlex.quote(public_key)} remove")
    except Exception as exc:
        remove_error = exc

    with STATE_LOCK:
        state = load_state()
        removed = False
        for section in ("names", "disabled", "saved", "traffic", "clients"):
            if public_key in state.get(section, {}):
                state[section].pop(public_key, None)
                removed = True
        if public_key in state.get("order", []):
            state["order"] = [item for item in state["order"] if item != public_key]
            removed = True
        save_state(state)
    with SAMPLE_LOCK:
        LAST_SAMPLE.pop(public_key, None)
    if remove_error and not known and not removed:
        raise ValueError(str(remove_error) or "Peer not found")
    if not known and not removed:
        raise ValueError("Peer not found")


def used_peer_ips(state, active_peers):
    used = set()
    for peer in active_peers:
        allowed_ips = peer.get("allowedIps") or ""
        for item in allowed_ips.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError:
                continue
            if isinstance(network.network_address, ipaddress.IPv4Address):
                used.add(network.network_address)
    for saved in state.get("saved", {}).values():
        allowed_ips = saved.get("allowedIps") or ""
        for item in allowed_ips.split(","):
            try:
                network = ipaddress.ip_network(item.strip(), strict=False)
            except ValueError:
                continue
            if isinstance(network.network_address, ipaddress.IPv4Address):
                used.add(network.network_address)
    return used


def next_peer_ip(state, active_peers):
    network = ipaddress.ip_network(VPN_NETWORK, strict=False)
    used = used_peer_ips(state, active_peers)
    for host in network.hosts():
        if host not in used:
            return host
    raise ValueError(f"No free IPs left in {VPN_NETWORK}")


def keygen():
    output = docker_exec(
        "private=$(awg genkey) && "
        "public=$(printf %s \"$private\" | awg pubkey) && "
        "psk=$(awg genpsk) && "
        "printf 'private=%s\\npublic=%s\\npsk=%s\\n' \"$private\" \"$public\" \"$psk\""
    )
    result = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key] = value.strip()
    if not result.get("private") or not result.get("public") or not result.get("psk"):
        raise RuntimeError("Failed to generate peer keys")
    return result


def build_client_config(name, address, private_key, preshared_key, server):
    lines = [
        "[Interface]",
        f"# Name = {name}",
        f"PrivateKey = {private_key}",
        f"Address = {address}/32",
    ]
    if VPN_DNS:
        lines.append(f"DNS = {VPN_DNS}")
    if CLIENT_MTU:
        lines.append(f"MTU = {CLIENT_MTU}")
    lines.extend(
        [
            f"Jc = {server['jc']}",
            f"Jmin = {server['jmin']}",
            f"Jmax = {server['jmax']}",
            f"S1 = {server['s1']}",
            f"S2 = {server['s2']}",
            f"S3 = {server['s3']}",
            f"S4 = {server['s4']}",
            f"H1 = {server['h1']}",
            f"H2 = {server['h2']}",
            f"H3 = {server['h3']}",
            f"H4 = {server['h4']}",
        ]
    )
    # HeaderProtectionKey is mandatory on both ends when AWG 3.x enables it.
    # Other AWG 3.x/3.1 device settings are mirrored from the live interface.
    # AWG 2.x has no such dump fields and therefore keeps the old output.
    awg3_config_fields = (
        ("HeaderProtectionKey", "headerProtectionKey"),
        ("ContentPaddingAddition", "contentPaddingAddition"),
        ("RekeyAfterTime", "rekeyAfterTime"),
        ("RekeyTimeout", "rekeyTimeout"),
        ("RejectAfterTime", "rejectAfterTime"),
        ("KeepaliveTimeout", "keepaliveTimeout"),
        ("MaxHandshakeAttempts", "maxHandshakeAttempts"),
        ("RandomTrailers", "randomTrailers"),
        ("DisableCookies", "disableCookies"),
    )
    for config_name, server_name in awg3_config_fields:
        value = server.get(server_name)
        if value not in (None, "", "(null)", "(none)", "(off)"):
            lines.append(f"{config_name} = {value}")
    lines.extend(
        [
            "",
            "[Peer]",
            f"PublicKey = {server['publicKey']}",
            f"PresharedKey = {preshared_key}",
            f"AllowedIPs = {CLIENT_ALLOWED_IPS}",
            f"Endpoint = {VPN_ENDPOINT}",
        ]
    )
    if CLIENT_KEEPALIVE:
        lines.append(f"PersistentKeepalive = {CLIENT_KEEPALIVE}")
    return "\n".join(lines) + "\n"


def add_peer(name):
    cleaned = " ".join((name or "").strip().split())
    if not cleaned:
        raise ValueError("Peer name is required")
    if len(cleaned) > 80:
        raise ValueError("Name is too long")

    dump = docker_exec(f"awg show {shlex.quote(INTERFACE)} dump")
    _, active_peers = parse_dump(dump)
    server = parse_interface_dump(dump)
    keys = keygen()

    with STATE_LOCK:
        state = load_state()
        address = next_peer_ip(state, active_peers)
        allowed_ip = f"{address}/32"

    command = (
        "tmp=$(mktemp) && "
        f"printf %s {shlex.quote(keys['psk'])} > \"$tmp\" && "
        f"awg set {shlex.quote(INTERFACE)} peer {shlex.quote(keys['public'])} "
        f"preshared-key \"$tmp\" allowed-ips {shlex.quote(allowed_ip)} && "
        "rm -f \"$tmp\""
    )
    docker_exec(command)

    client_config = build_client_config(cleaned, str(address), keys["private"], keys["psk"], server)
    with STATE_LOCK:
        state = load_state()
        state["names"][keys["public"]] = cleaned
        state["disabled"][keys["public"]] = False
        state["saved"][keys["public"]] = {
            "publicKey": keys["public"],
            "presharedKey": keys["psk"],
            "endpoint": None,
            "allowedIps": allowed_ip,
            "persistentKeepalive": "off",
        }
        state["traffic"][keys["public"]] = {"rxOffset": 0, "txOffset": 0, "lastRawRx": 0, "lastRawTx": 0}
        state["clients"][keys["public"]] = {
            "name": cleaned,
            "address": allowed_ip,
            "privateKey": keys["private"],
            "publicKey": keys["public"],
            "presharedKey": keys["psk"],
            "config": client_config,
            "createdAt": int(time.time()),
        }
        state["order"] = [item for item in state.get("order", []) if item != keys["public"]]
        state["order"].append(keys["public"])
        save_state(state)
    return keys["public"]


def get_client_config(public_key):
    with STATE_LOCK:
        state = load_state()
        client = state["clients"].get(public_key)
        if not client:
            raise ValueError("Client config is available only for peers created in this panel")
        client = dict(client)

    # Rebuild exports from the live interface instead of trusting the snapshot
    # saved when the peer was created. This migrates existing AWG 2.x exports
    # after a server upgrade to AWG 3.x and follows endpoint/parameter changes.
    private_key = client.get("privateKey")
    preshared_key = client.get("presharedKey")
    address = str(client.get("address", "")).split("/", 1)[0]
    if private_key and preshared_key and address:
        server = get_interface_config()
        client["config"] = build_client_config(
            client.get("name", "peer"),
            address,
            private_key,
            preshared_key,
            server,
        )
    return client


def vpn_key_payload(config):
    encoded = base64.urlsafe_b64encode(config.encode("utf-8")).rstrip(b"=").decode("ascii")
    return f"vpn://{encoded}"


def amnezia_qr_payloads(config):
    data = config.encode("utf-8")
    chunk_size = 850
    chunks = [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]
    if len(chunks) > 255:
        raise ValueError("Config is too large for Amnezia QR")
    payloads = []
    for index, chunk in enumerate(chunks):
        # QDataStream-compatible layout used by Amnezia:
        # qint16 magic, quint8 chunksCount, quint8 chunkId, QByteArray(length + data).
        envelope = struct.pack(">hBBI", 1984, len(chunks), index, len(chunk)) + chunk
        payloads.append(base64.urlsafe_b64encode(envelope).rstrip(b"=").decode("ascii"))
    return payloads


def render_qr_svg(data):
    result = subprocess.run(
        ["qrencode", "-l", "L", "-t", "SVG", "-o", "-"],
        input=data.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace") or "qrencode failed")
    return result.stdout


def render_qr_png(data):
    result = subprocess.run(
        [
            "qrencode",
            "-l",
            "L",
            "-t",
            "PNG",
            "-s",
            "8",
            "-m",
            "4",
            "--foreground=000000",
            "--background=FFFFFFFF",
            "-o",
            "-",
        ],
        input=data.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace") or "qrencode failed")
    return result.stdout


def update_name(public_key, name):
    cleaned = " ".join((name or "").strip().split())
    if len(cleaned) > 80:
        raise ValueError("Name is too long")
    with STATE_LOCK:
        state = load_state()
        if cleaned:
            state["names"][public_key] = cleaned
        else:
            state["names"].pop(public_key, None)
        save_state(state)


def check_auth(header):
    if not ADMIN_PASSWORD:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    user, sep, password = decoded.partition(":")
    if not sep:
        return False
    return hmac.compare_digest(user, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASSWORD)


def audit_log(event, client, **fields):
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    line = f"{timestamp} {event} client={client}"
    if details:
        line += f" {details}"
    with AUTH_LOCK:
        with AUTH_LOG_FILE.open("a", encoding="utf-8") as log:
            os.chmod(AUTH_LOG_FILE, 0o600)
            log.write(line + "\n")


def auth_state(client):
    with AUTH_LOCK:
        if client not in AUTH_STATE and len(AUTH_STATE) >= 4096:
            oldest = min(AUTH_STATE, key=lambda key: AUTH_STATE[key].get("last_seen", 0))
            AUTH_STATE.pop(oldest, None)
        state = AUTH_STATE.setdefault(
            client,
            {
                "failures": [],
                "blocked_until": 0,
                "requests": [],
                "last_success_log": 0,
                "last_rate_log": 0,
                "last_seen": 0,
            },
        )
        state["last_seen"] = time.time()
        return state


def request_allowed(client):
    now = time.time()
    state = auth_state(client)
    with AUTH_LOCK:
        state["last_seen"] = now
        state["requests"] = [stamp for stamp in state["requests"] if now - stamp < REQUEST_RATE_WINDOW]
        if len(state["requests"]) >= REQUEST_RATE_LIMIT:
            return False
        state["requests"].append(now)
        return True


def auth_blocked(client):
    now = time.time()
    state = auth_state(client)
    with AUTH_LOCK:
        return max(0, int(state["blocked_until"] - now))


def record_rate_limit(client):
    now = time.time()
    state = auth_state(client)
    should_log = False
    with AUTH_LOCK:
        if now - state["last_rate_log"] >= REQUEST_RATE_WINDOW:
            state["last_rate_log"] = now
            should_log = True
    if should_log:
        audit_log("RATE_LIMIT", client)


def record_auth_failure(client):
    now = time.time()
    state = auth_state(client)
    with AUTH_LOCK:
        state["failures"] = [stamp for stamp in state["failures"] if now - stamp < AUTH_FAILURE_WINDOW]
        state["failures"].append(now)
        failures = len(state["failures"])
        if failures >= AUTH_FAILURE_LIMIT:
            state["blocked_until"] = now + AUTH_BLOCK_SECONDS
            state["failures"] = []
    audit_log("AUTH_FAILURE", client, failures=failures)
    return failures


def record_auth_success(client):
    now = time.time()
    state = auth_state(client)
    should_log = False
    with AUTH_LOCK:
        state["failures"] = []
        state["blocked_until"] = 0
        if now - state["last_success_log"] >= AUTH_FAILURE_WINDOW:
            state["last_success_log"] = now
            should_log = True
    if should_log:
        audit_log("AUTH_SUCCESS", client)


class Handler(BaseHTTPRequestHandler):
    server_version = "AWGPanel"
    sys_version = ""

    def version_string(self):
        return self.server_version

    def client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        candidate = forwarded or self.client_address[0]
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return self.client_address[0]

    def begin_request(self):
        client = self.client_ip()
        if request_allowed(client):
            return True
        record_rate_limit(client)
        self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many requests"}, {"Retry-After": "60"})
        return False

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
            "img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'",
        )
        super().end_headers()

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def require_auth(self):
        client = self.client_ip()
        retry_after = auth_blocked(client)
        if retry_after:
            self.send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"error": "Too many failed login attempts"},
                {"Retry-After": str(retry_after)},
            )
            return False
        authorization = self.headers.get("Authorization")
        if check_auth(authorization):
            record_auth_success(client)
            return True
        if authorization:
            record_auth_failure(client)
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="AWG Panel"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def send_json(self, status, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, status, content_type, data, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if size <= 0:
            return {}
        if size > MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large")
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_GET(self):
        if not self.begin_request():
            return
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            try:
                self.send_json(HTTPStatus.OK, get_status())
            except Exception as exc:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        if parsed.path == "/api/routing/status":
            self.send_json(HTTPStatus.OK, routing_status())
            return
        if parsed.path == "/api/traffic":
            self.send_json(HTTPStatus.OK, traffic_status())
            return
        if parsed.path == "/api/routing/roscomvpn-amnezia.json":
            try:
                if routing_status()["stale"]:
                    refresh_routing()
                self.send_bytes(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    ROUTING_FILE.read_bytes(),
                    {"Content-Disposition": 'attachment; filename="roscomvpn-amnezia.json"'},
                )
            except Exception as exc:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "peers"] and parts[3] in ("config", "qr.svg", "qr.png"):
            try:
                client = get_client_config(parts[2])
                if parts[3] == "config":
                    qr_payloads = amnezia_qr_payloads(client["config"])
                    self.send_json(
                        HTTPStatus.OK,
                        {
                            "name": client["name"],
                            "address": client["address"],
                            "publicKey": client["publicKey"],
                            "config": client["config"],
                            "vpnKey": vpn_key_payload(client["config"]),
                            "qrChunksCount": len(qr_payloads),
                        },
                    )
                else:
                    query = parse_qs(parsed.query)
                    fmt = query.get("format", ["native"])[0]
                    if fmt == "amnezia":
                        payloads = amnezia_qr_payloads(client["config"])
                        chunk = int(query.get("chunk", ["0"])[0])
                        payload = payloads[chunk % len(payloads)]
                    elif fmt == "vpnkey":
                        payload = vpn_key_payload(client["config"])
                    else:
                        payload = client["config"]
                    if parts[3] == "qr.png":
                        self.send_bytes(HTTPStatus.OK, "image/png", render_qr_png(payload))
                    else:
                        self.send_bytes(HTTPStatus.OK, "image/svg+xml", render_qr_svg(payload))
            except ValueError as exc:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except Exception as exc:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        path = "/index.html" if parsed.path == "/" else parsed.path
        static_root = Path(__file__).resolve().parent / "static"
        target = (static_root / path.lstrip("/")).resolve()
        if not target.is_relative_to(static_root.resolve()) or not target.exists() or target.is_dir():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = "text/html; charset=utf-8"
        if target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.begin_request():
            return
        if not self.require_auth():
            return
        fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        origin = self.headers.get("Origin")
        if fetch_site == "cross-site" or (origin and not self.valid_origin(origin)):
            audit_log("CSRF_REJECT", self.client_ip())
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Cross-site request rejected"})
            return
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        try:
            body = self.read_json()
            if len(parts) == 2 and parts == ["api", "peers"]:
                public_key = add_peer(body.get("name", ""))
                self.send_json(HTTPStatus.CREATED, {"publicKey": public_key, "status": get_status()})
                return
            if len(parts) == 3 and parts == ["api", "routing", "refresh"]:
                self.send_json(HTTPStatus.OK, refresh_routing())
                return
            if len(parts) == 4 and parts[:2] == ["api", "peers"]:
                public_key = parts[2]
                action = parts[3]
                if action == "name":
                    update_name(public_key, body.get("name", ""))
                elif action == "disable":
                    disable_peer(public_key)
                elif action == "enable":
                    enable_peer(public_key)
                elif action == "delete":
                    delete_peer(public_key)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self.send_json(HTTPStatus.OK, get_status())
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def valid_origin(self, origin):
        parsed = urlparse(origin)
        host = self.headers.get("Host", "").lower()
        scheme = self.headers.get("X-Forwarded-Proto", "https").split(",", 1)[0].strip().lower()
        return parsed.scheme.lower() == scheme and parsed.netloc.lower() == host


def main():
    if not ADMIN_PASSWORD:
        raise RuntimeError("PANEL_PASSWORD must be set")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=routing_updater, name="roscomvpn-routing", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    scheme = "http"
    if TLS_CERT and TLS_KEY:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(TLS_CERT, TLS_KEY)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"AWG panel listening on {scheme}://{HOST}:{PORT}, container={CONTAINER}, interface={INTERFACE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

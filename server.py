#!/usr/bin/env python3
"""Local VPN client. Loads a Clash subscription and runs mihomo on 127.0.0.1."""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
RUNTIME = ROOT / "runtime"
BIN = ROOT / "bin" / "mihomo"
CONFIG_PATH = ROOT / "config.json"
SUBSCRIPTION_PATH = RUNTIME / "subscription.yaml"
CLASH_PATH = RUNTIME / "config.yaml"
SECRET_PATH = RUNTIME / "secret"
SESSION_PATH = RUNTIME / "session.json"
PROXY_FLAG = RUNTIME / "system-proxy.json"
LOG_PATH = RUNTIME / "mihomo.log"
MMDB_PATH = RUNTIME / "country.mmdb"

UI_PORT = 8787
MIXED_PORT = 7890
CTL_PORT = 9090
MIHOMO_VERSION = "v1.19.32"
ACCOUNT_PREFIXES = ("剩余流量", "距离下次", "套餐到期", "官网")

app_lock = threading.RLock()
core_proc = None
core_error = ""
catalog = {"nodes": [], "routes": [], "account": [], "group": "", "updatedAt": ""}
delays = {}
session = {"selected": "", "systemProxy": True}
subscription_warning = ""


def redact(text: str) -> str:
    secret = ""
    if SECRET_PATH.exists():
        secret = SECRET_PATH.read_text(encoding="utf-8").strip()
    if secret:
        text = text.replace(secret, "•••")
    return re.sub(r"token=[^&\s'\"<>]+", "token=•••", text)


def read_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"subscription": ""}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def write_config(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


def load_session() -> None:
    global session
    if SESSION_PATH.exists():
        try:
            saved = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
            session.update(saved)
        except (OSError, json.JSONDecodeError):
            pass


def save_session() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(session), encoding="utf-8")


def secret() -> str:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        value = SECRET_PATH.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(16)
    SECRET_PATH.write_text(value, encoding="utf-8")
    os.chmod(SECRET_PATH, 0o600)
    return value


def yaml_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def parse_flow_body(body: str) -> dict:
    fields = {}
    i = 0
    n = len(body)

    def skip_ws() -> None:
        nonlocal i
        while i < n and body[i] in " \t\r\n":
            i += 1

    while i < n:
        skip_ws()
        if i >= n:
            break
        if body[i] == ",":
            i += 1
            continue
        key_start = i
        while i < n and body[i] not in ":\n":
            i += 1
        key = body[key_start:i].strip().strip("'\"")
        if i < n and body[i] == ":":
            i += 1
        skip_ws()
        if i >= n:
            fields[key] = ""
            break
        if body[i] in "{[":
            start = i
            brace = 0
            bracket = 0
            quote = None
            while i < n:
                ch = body[i]
                if quote:
                    if ch == "\\" and quote == '"':
                        i += 2
                        continue
                    if quote == "'" and ch == "'" and i + 1 < n and body[i + 1] == "'":
                        i += 2
                        continue
                    if ch == quote:
                        quote = None
                    i += 1
                    continue
                if ch in "'\"":
                    quote = ch
                elif ch == "{":
                    brace += 1
                elif ch == "}":
                    brace -= 1
                elif ch == "[":
                    bracket += 1
                elif ch == "]":
                    bracket -= 1
                i += 1
                if brace == 0 and bracket == 0 and quote is None:
                    break
            fields[key] = body[start:i].strip()
            continue
        if body[i] in "'\"":
            quote = body[i]
            i += 1
            chars = []
            while i < n:
                ch = body[i]
                if quote == "'" and ch == "'" and i + 1 < n and body[i + 1] == "'":
                    chars.append("'")
                    i += 2
                    continue
                if ch == quote:
                    i += 1
                    break
                chars.append(ch)
                i += 1
            fields[key] = "".join(chars)
            continue
        start = i
        while i < n and body[i] != ",":
            i += 1
        fields[key] = body[start:i].strip().strip("'\"")
    return fields


def iter_maps(text: str):
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        i += 1
        start = i
        brace = 1
        bracket = 0
        quote = None
        while i < n and (brace or bracket):
            ch = text[i]
            if quote:
                if ch == "\\" and quote == '"':
                    i += 2
                    continue
                if quote == "'" and ch == "'" and i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                if ch == quote:
                    quote = None
                i += 1
                continue
            if ch in "'\"":
                quote = ch
            elif ch == "{":
                brace += 1
            elif ch == "}":
                brace -= 1
            elif ch == "[":
                bracket += 1
            elif ch == "]":
                bracket -= 1
            i += 1
        yield text[start : i - 1 if i <= n else n]


def slice_section(text: str, start_key: str, end_keys: tuple[str, ...]) -> str:
    match = re.search(rf"(?m)^{re.escape(start_key)}:\s*$", text)
    if not match:
        return ""
    rest = text[match.end() :]
    end = len(rest)
    for key in end_keys:
        found = re.search(rf"(?m)^{re.escape(key)}:\s*$", rest)
        if found:
            end = min(end, found.start())
    return rest[:end]


def parse_name_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return []
    inner = raw[1:-1]
    items = []
    chars = []
    quote = None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote:
            if quote == "'" and ch == "'" and i + 1 < len(inner) and inner[i + 1] == "'":
                chars.append("'")
                i += 2
                continue
            if ch == quote:
                quote = None
                i += 1
                continue
            chars.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == ",":
            item = "".join(chars).strip()
            if item:
                items.append(item)
            chars = []
            i += 1
            continue
        chars.append(ch)
        i += 1
    tail = "".join(chars).strip()
    if tail:
        items.append(tail)
    return items


def is_account(name: str) -> bool:
    return name.startswith(ACCOUNT_PREFIXES)


def region_of(name: str) -> str:
    match = re.match(r"\[([^\]]+)\]", name)
    return match.group(1) if match else ""


def build_catalog(text: str) -> dict:
    proxies = {}
    for body in iter_maps(slice_section(text, "proxies", ("proxy-groups", "rules"))):
        fields = parse_flow_body(body)
        name = fields.get("name", "")
        if name:
            proxies[name] = fields.get("type", "")

    groups = {}
    for body in iter_maps(slice_section(text, "proxy-groups", ("rules",))):
        fields = parse_flow_body(body)
        name = fields.get("name", "")
        if not name:
            continue
        groups[name] = {
            "type": fields.get("type", "select"),
            "proxies": parse_name_list(fields.get("proxies", "")),
        }

    group_name = ""
    match = re.search(r"(?m)^-\s*'?MATCH,([^,'\s]+)", text)
    if match and match.group(1) in groups:
        group_name = match.group(1)
    elif groups:
        selects = [name for name, group in groups.items() if group["type"] == "select"]
        group_name = selects[0] if selects else next(iter(groups))

    menu = groups.get(group_name, {}).get("proxies", list(proxies))
    nodes = []
    routes = []
    account = []
    seen = set()
    for name in menu:
        if name in seen:
            continue
        seen.add(name)
        if is_account(name):
            account.append(name)
            continue
        if name in groups and name != group_name:
            routes.append({"name": name, "type": groups[name]["type"], "region": "", "role": "route"})
            continue
        if name in proxies:
            nodes.append({
                "name": name,
                "type": proxies[name],
                "region": region_of(name),
                "role": "node",
            })
    if not account:
        account = [name for name in proxies if is_account(name)]
    return {
        "nodes": nodes,
        "routes": routes,
        "account": account,
        "group": group_name or "Proxy",
        "updatedAt": datetime.now().strftime("%H:%M"),
    }


def harden(text: str, api_secret: str) -> str:
    drop = {"tun", "ebpf", "iptables", "listeners"}
    lines = text.replace("\r\n", "\n").split("\n")
    kept = []
    skipping = False
    replacements = {
        "mixed-port": f"mixed-port: {MIXED_PORT}",
        "allow-lan": "allow-lan: false",
        "bind-address": "bind-address: 127.0.0.1",
        "external-controller": f"external-controller: 127.0.0.1:{CTL_PORT}",
        "secret": f"secret: {api_secret}",
    }
    found = set()
    for line in lines:
        if line and not line.startswith(" ") and not line.startswith("\t") and not line.startswith("#") and not line.startswith("-"):
            key = line.split(":", 1)[0].strip()
            if key in drop:
                skipping = True
                continue
            skipping = False
            if key in replacements:
                kept.append(replacements[key])
                found.add(key)
                continue
        elif skipping:
            continue
        kept.append(line)
    if "secret" not in found:
        inserted = []
        placed = False
        for line in kept:
            inserted.append(line)
            if not placed and line.startswith("external-controller:"):
                inserted.append(replacements["secret"])
                placed = True
        kept = inserted
    for key in ("mixed-port", "allow-lan", "bind-address", "external-controller"):
        if key not in found:
            kept.insert(0, replacements[key])
    return "\n".join(kept).rstrip() + "\n"


def shares_to_clash(text: str) -> str:
    proxies = []
    names = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "://" not in line:
            continue
        node = parse_share(line)
        if not node:
            continue
        proxies.append(node)
        if not is_account(node["name"]):
            names.append(node["name"])
    if not proxies:
        return ""
    body = [
        f"mixed-port: {MIXED_PORT}",
        "allow-lan: false",
        "bind-address: 127.0.0.1",
        "mode: rule",
        "log-level: info",
        f"external-controller: 127.0.0.1:{CTL_PORT}",
        "proxies:",
    ]
    for node in proxies:
        body.append(emit_proxy(node))
    quoted = ", ".join(yaml_quote(name) for name in names)
    body.append("proxy-groups:")
    body.append(f"- {{ name: Proxy, type: select, proxies: [{quoted}] }}")
    body.append("rules:")
    body.append("- MATCH,Proxy")
    return "\n".join(body) + "\n"


def emit_proxy(node: dict) -> str:
    parts = [
        f"name: {yaml_quote(node['name'])}",
        f"type: {node['type']}",
        f"server: {yaml_quote(node['server'])}",
        f"port: {int(node['port'])}",
    ]
    if node.get("password"):
        parts.append(f"password: {yaml_quote(node['password'])}")
    if node.get("uuid"):
        parts.append(f"uuid: {yaml_quote(node['uuid'])}")
    parts.append("udp: true")
    for key in ("sni", "network", "flow", "servername", "client-fingerprint"):
        if node.get(key):
            parts.append(f"{key}: {yaml_quote(node[key])}")
    if "tls" in node:
        parts.append("tls: " + ("true" if node["tls"] else "false"))
    if node.get("skip-cert-verify"):
        parts.append("skip-cert-verify: true")
    if node.get("ws-opts"):
        ws = node["ws-opts"]
        host = yaml_quote(ws.get("headers", {}).get("Host", ""))
        parts.append(f"ws-opts: {{ path: {yaml_quote(ws.get('path', '/'))}, headers: {{ Host: {host} }} }}")
    if node.get("reality-opts"):
        reality = node["reality-opts"]
        parts.append(
            "reality-opts: { public-key: "
            + yaml_quote(reality.get("public-key", ""))
            + ", short-id: "
            + yaml_quote(reality.get("short-id", ""))
            + " }"
        )
    return "- { " + ", ".join(parts) + " }"


def parse_share(line: str) -> dict | None:
    scheme, rest = line.split("://", 1)
    name = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        name = urllib.parse.unquote(frag)
    parsed = urllib.parse.urlsplit(scheme + "://" + rest)
    query = urllib.parse.parse_qs(parsed.query)

    def q(key: str, default: str = "") -> str:
        return urllib.parse.unquote(query.get(key, [default])[0])

    host = parsed.hostname or ""
    port = parsed.port or 0
    user = urllib.parse.unquote(parsed.username or "")
    label = name or f"{host}:{port}"
    if scheme in ("hysteria2", "hy2"):
        return {
            "name": label,
            "type": "hysteria2",
            "server": host,
            "port": port,
            "password": user,
            "sni": q("sni", host),
            "skip-cert-verify": q("insecure") in ("1", "true"),
        }
    if scheme == "anytls":
        return {
            "name": label,
            "type": "anytls",
            "server": host,
            "port": port,
            "password": user,
            "sni": q("sni", host),
            "skip-cert-verify": q("insecure") in ("1", "true"),
        }
    if scheme != "vless":
        return None
    security = q("security", "none")
    network = q("type", "tcp")
    node = {
        "name": label,
        "type": "vless",
        "server": host,
        "port": port,
        "uuid": user,
        "network": network,
        "tls": security in ("tls", "reality"),
        "servername": q("sni") or q("servername") or host,
        "client-fingerprint": q("fp", "chrome"),
    }
    if q("flow"):
        node["flow"] = q("flow")
    if network == "ws":
        node["ws-opts"] = {"path": urllib.parse.unquote(q("path", "/")), "headers": {"Host": q("host") or node["servername"]}}
    if security == "reality":
        node["reality-opts"] = {"public-key": q("pbk"), "short-id": q("sid")}
    return node


def normalize_subscription(raw: str) -> str:
    text = raw.lstrip("\ufeff").strip()
    if re.search(r"(?m)^proxies:\s*$", text):
        return text if text.endswith("\n") else text + "\n"
    compact = "".join(text.split())
    decoded = ""
    try:
        padded = compact + "=" * ((4 - len(compact) % 4) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        decoded = ""
    if re.search(r"(?m)^proxies:\s*$", decoded):
        return decoded if decoded.endswith("\n") else decoded + "\n"
    built = shares_to_clash(decoded or text)
    if built:
        return built
    raise RuntimeError("Subscription did not include a node list.")


def fetch_subscription(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "clash-verge/1.3.8"})
    with urllib.request.urlopen(request, timeout=40) as response:
        raw = response.read().decode("utf-8", "replace")
    return normalize_subscription(raw)


def remember_catalog(text: str) -> None:
    global catalog
    catalog = build_catalog(text)
    names = [item["name"] for item in catalog["routes"] + catalog["nodes"]]
    if session.get("selected") not in names:
        session["selected"] = names[0] if names else ""
        save_session()


def write_runtime_config(text: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    SUBSCRIPTION_PATH.write_text(text, encoding="utf-8")
    os.chmod(SUBSCRIPTION_PATH, 0o600)
    CLASH_PATH.write_text(harden(text, secret()), encoding="utf-8")
    os.chmod(CLASH_PATH, 0o600)
    remember_catalog(text)


def ensure_binary() -> None:
    if BIN.exists():
        return
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        asset = f"mihomo-darwin-arm64-{MIHOMO_VERSION}.gz"
    elif machine in ("x86_64", "amd64"):
        asset = f"mihomo-darwin-amd64-{MIHOMO_VERSION}.gz"
    else:
        raise RuntimeError(f"No mihomo build for {machine}.")
    url = f"https://github.com/MetaCubeX/mihomo/releases/download/{MIHOMO_VERSION}/{asset}"
    BIN.parent.mkdir(parents=True, exist_ok=True)
    archive = BIN.with_suffix(".gz")
    urllib.request.urlretrieve(url, archive)
    import gzip
    import shutil

    with gzip.open(archive, "rb") as src, open(BIN, "wb") as dst:
        shutil.copyfileobj(src, dst)
    archive.unlink(missing_ok=True)
    os.chmod(BIN, 0o755)


def ensure_mmdb() -> None:
    if MMDB_PATH.exists() and MMDB_PATH.stat().st_size > 1000:
        return
    RUNTIME.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/country.mmdb"
    try:
        urllib.request.urlretrieve(url, MMDB_PATH)
    except OSError:
        return


def api(method: str, path: str, body: dict | None = None, timeout: float = 8):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{CTL_PORT}{path}",
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + secret(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return response.status, json.loads(payload) if payload else {}
    except urllib.error.URLError as error:
        return 0, {"message": str(getattr(error, "reason", error))}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("message") or detail
        except json.JSONDecodeError:
            message = detail or error.reason
        return error.code, {"message": message}


def core_alive() -> bool:
    return core_proc is not None and core_proc.poll() is None


def tail_log() -> str:
    if not LOG_PATH.exists():
        return ""
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(lines[-12:])
    return redact(text)[-500:]


def stop_core() -> None:
    global core_proc
    proc = core_proc
    core_proc = None
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def start_core() -> None:
    global core_proc, core_error
    if core_alive():
        return
    if not CLASH_PATH.exists():
        raise RuntimeError("No subscription loaded yet.")
    ensure_binary()
    ensure_mmdb()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    log = open(LOG_PATH, "wb")
    core_proc = subprocess.Popen(
        [str(BIN), "-d", str(RUNTIME), "-f", str(CLASH_PATH), "-secret", secret()],
        cwd=str(RUNTIME),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(40):
        if core_proc.poll() is not None:
            core_error = tail_log() or "The VPN core exited."
            raise RuntimeError(core_error)
        status, _payload = api("GET", "/version", timeout=1)
        if status == 200:
            core_error = ""
            apply_selection()
            return
        time.sleep(0.25)
    core_error = tail_log() or "The VPN core did not open its controller."
    raise RuntimeError(core_error)


def apply_selection() -> None:
    name = session.get("selected") or ""
    group = catalog.get("group") or ""
    if not name or not group or not core_alive():
        return
    status, payload = api("PUT", "/proxies/" + urllib.parse.quote(group, safe=""), {"name": name})
    if status >= 400:
        raise RuntimeError(payload.get("message") or "Could not select that node.")


def selectable_names() -> list[str]:
    return [item["name"] for item in catalog.get("routes", []) + catalog.get("nodes", [])]


def select_name(name: str) -> None:
    if name not in selectable_names():
        raise RuntimeError("That node is not in the current subscription.")
    session["selected"] = name
    save_session()
    if core_alive():
        apply_selection()


def chrome_app() -> Path | None:
    for path in (Path("/Applications/Google Chrome.app"), Path.home() / "Applications/Google Chrome.app"):
        if path.exists():
            return path
    return None


def open_chrome() -> dict:
    if chrome_app() is None:
        raise RuntimeError("Google Chrome is not installed. Point any app at 127.0.0.1:%s." % MIXED_PORT)
    if not session.get("selected"):
        raise RuntimeError("Pick a node first.")
    start_core()
    apply_selection()
    session["connected"] = True
    save_session()
    profile = RUNTIME / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            "open", "-na", "Google Chrome", "--args",
            "--user-data-dir=%s" % profile,
            "--proxy-server=http://127.0.0.1:%s" % MIXED_PORT,
            "--proxy-bypass-list=localhost;127.0.0.1",
            "--disable-quic",
            "--no-first-run",
            "--no-default-browser-check",
            "https://www.google.com/",
        ],
        start_new_session=True,
    )
    payload = state_payload()
    payload["browser"] = "Google Chrome"
    return payload


def reload_subscription(url: str | None = None) -> None:
    global subscription_warning
    data = read_config()
    if url is not None:
        if not url.startswith("https://"):
            raise RuntimeError("The subscription URL has to start with https://")
        data["subscription"] = url
        write_config(data)
    target = data.get("subscription", "").strip()
    if not target:
        raise RuntimeError("Add a subscription URL first.")
    try:
        text = fetch_subscription(target)
        subscription_warning = ""
    except Exception as error:
        if SUBSCRIPTION_PATH.exists():
            text = SUBSCRIPTION_PATH.read_text(encoding="utf-8")
            subscription_warning = "Using the last saved node list. " + redact(str(error))
        else:
            raise RuntimeError(redact(str(error))) from error
    write_runtime_config(text)
    if core_alive():
        stop_core()
        start_core()


def test_delay(names: list[str]) -> dict:
    if not core_alive():
        start_core()
    result = {}

    def one(name: str) -> None:
        query = urllib.parse.urlencode({"timeout": "4000", "url": "https://www.gstatic.com/generate_204"})
        path = "/proxies/" + urllib.parse.quote(name, safe="") + "/delay?" + query
        status, payload = api("GET", path, timeout=6)
        if status == 200 and isinstance(payload.get("delay"), int):
            result[name] = payload["delay"]
        else:
            result[name] = -1

    threads = []
    for name in names[:12]:
        if name not in selectable_names():
            continue
        thread = threading.Thread(target=one, args=(name,))
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    delays.update(result)
    return result


def public_nodes(items: list[dict]) -> list[dict]:
    copied = []
    for item in items:
        row = dict(item)
        if item["name"] in delays:
            row["delay"] = delays[item["name"]]
        copied.append(row)
    return copied


def state_payload() -> dict:
    return {
        "connected": bool(session.get("connected")) and core_alive(),
        "core": "running" if core_alive() else "error" if core_error else "stopped",
        "coreError": "" if core_alive() else core_error,
        "endpoint": f"127.0.0.1:{MIXED_PORT}",
        "selected": session.get("selected", ""),
        "warning": subscription_warning,
        "proxyError": "",
        "group": catalog.get("group", ""),
        "browserReady": chrome_app() is not None,
    }


def connect(name: str) -> dict:
    global core_error
    select_name(name)
    try:
        start_core()
    except RuntimeError as error:
        core_error = redact(str(error))
        session["connected"] = False
        save_session()
        payload = state_payload()
        payload["proxyError"] = core_error
        return payload
    session["connected"] = True
    save_session()
    return state_payload()


def disconnect() -> dict:
    session["connected"] = False
    save_session()
    return state_payload()


def cleanup() -> None:
    stop_core()


class Handler(BaseHTTPRequestHandler):
    server_version = "local-vpn"

    def log_message(self, fmt: str, *args) -> None:
        return

    def send_json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise RuntimeError("Request is too large.")
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/state":
                self.send_json(state_payload())
                return
            if path == "/api/nodes":
                self.send_json({
                    "nodes": public_nodes(catalog.get("nodes", [])),
                    "routes": public_nodes(catalog.get("routes", [])),
                    "account": catalog.get("account", []),
                    "group": catalog.get("group", ""),
                    "updatedAt": catalog.get("updatedAt", ""),
                })
                return
            if path == "/api/settings":
                self.send_json({"subscription": read_config().get("subscription", "")})
                return
            self.serve_static(path)
        except Exception as error:
            self.send_json({"error": redact(str(error))}, 500)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self.read_json()
            with app_lock:
                if path == "/api/refresh":
                    reload_subscription()
                    self.send_json(state_payload())
                elif path == "/api/subscription":
                    reload_subscription(body.get("url", "").strip())
                    self.send_json(state_payload())
                elif path == "/api/select":
                    select_name(body.get("name", ""))
                    self.send_json(state_payload())
                elif path == "/api/connect":
                    self.send_json(connect(body.get("name", "")))
                elif path == "/api/disconnect":
                    self.send_json(disconnect())
                elif path == "/api/browser":
                    self.send_json(open_chrome())
                elif path == "/api/delay":
                    names = body.get("names") or []
                    if not isinstance(names, list):
                        raise RuntimeError("Expected a list of node names.")
                    self.send_json({"delays": test_delay([str(name) for name in names])})
                else:
                    self.send_json({"error": "Unknown request."}, 404)
        except Exception as error:
            self.send_json({"error": redact(str(error))}, 400)

    def serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / rel).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except ValueError:
            self.send_error(404)
            return
        if not target.is_file():
            self.send_error(404)
            return
        kind = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Local VPN client")
    parser.add_argument("--port", type=int, default=UI_PORT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    load_session()
    PROXY_FLAG.unlink(missing_ok=True)
    try:
        reload_subscription()
        start_core()
    except Exception as error:
        global core_error
        core_error = redact(str(error))
        print(core_error)
    if port_open(args.port):
        raise SystemExit(f"http://127.0.0.1:{args.port} is already in use.")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True

    def handle_stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    url = f"http://127.0.0.1:{args.port}"
    print(f"VPN ready at {url}", flush=True)
    print(f"Local proxy at 127.0.0.1:{MIXED_PORT}", flush=True)
    if not args.no_open:
        subprocess.Popen(["open", url])
    try:
        server.serve_forever()
    finally:
        cleanup()
        server.server_close()


if __name__ == "__main__":
    main()


import json
import os
import urllib.parse

from api.utils.logging import get_logger

XRAY = "/usr/local/etc/xray/config.json"
HOST = os.getenv("VLESS_HOST", "vpn-gpt.store")

logger = get_logger("link")

def _read_cfg():
    logger.debug("Reading Xray configuration for link composition from %s", XRAY)
    with open(XRAY, "r", encoding="utf-8") as f:
        return json.load(f)

def compose_vless_link(uid: str, username: str = "user"):
    cfg = _read_cfg()
    inb = (cfg.get("inbounds") or [])[0]
    port = inb.get("port", 443)
    stream = inb.get("streamSettings", {}) or {}
    net = stream.get("network", "tcp")
    sec = (stream.get("security") or "").lower()

    params = {"encryption": "none"}

    # network specifics
    if net == "ws":
        ws = stream.get("wsSettings", {}) or {}
        path = ws.get("path", "/")
        params.update({"type": "ws", "path": path})
        host_hdr = (ws.get("headers") or {}).get("Host")
        if host_hdr:
            params["host"] = host_hdr
    elif net == "grpc":
        gs = stream.get("grpcSettings", {}) or {}
        svc = gs.get("serviceName", "grpc")
        params.update({"type": "grpc", "serviceName": svc, "mode": "gun"})
    else:
        params["type"] = "tcp"

    # security specifics
    if sec == "tls":
        params["security"] = "tls"
        ts = stream.get("tlsSettings", {}) or {}
        sni = ts.get("serverName") or HOST
        params["sni"] = sni
        alpn = ts.get("alpn")
        if alpn:
            params["alpn"] = ",".join(alpn)
    elif sec == "reality":
        params["security"] = "reality"
        rs = stream.get("realitySettings", {}) or {}
        params["pbk"] = rs.get("publicKey", "")
        sid = (rs.get("shortIds") or [""])[0]
        if sid:
            params["sid"] = sid
        sni = (rs.get("serverNames") or [HOST])[0]
        params["sni"] = sni
        params["fp"] = rs.get("fingerprint", "chrome")
        spx = rs.get("spiderX", "")
        if spx:
            params["spx"] = spx
        # для VLESS REALITY чаще всего нужен flow
        params["flow"] = "xtls-rprx-vision"

    query = urllib.parse.urlencode(params, doseq=False, safe="/,")
    link = f"vless://{uid}@{HOST}:{port}?{query}#{urllib.parse.quote(username)}"
    logger.info(
        "Composed VLESS link for user", extra={"username": username, "uuid": uid, "port": port}
    )
    return link

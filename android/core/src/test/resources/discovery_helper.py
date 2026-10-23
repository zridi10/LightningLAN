"""Test helper: announces a Kotlin test peer with python-zeroconf, the way the desktop app does, and
reports the LightningLAN peers it finds as JSON lines on stdout. Runs until stdin is closed.

    python discovery_helper.py <peer_id> <name> <port> <auth 0|1> <version> <interface address>
"""

import json
import socket
import sys
import threading

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

SERVICE_TYPE = "_lightninglan._tcp.local."


def main() -> None:
    peer_id, name, port, auth, version, interface = sys.argv[1:7]
    zc = Zeroconf(interfaces=[interface], ip_version=IPVersion.V4Only)
    info = ServiceInfo(
        SERVICE_TYPE,
        f"LightningLAN-{peer_id}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(interface)],
        port=int(port),
        properties={"id": peer_id, "name": name, "v": version, "auth": auth},
        server=f"lightninglan-{peer_id[:12]}.local.",
    )
    lock = threading.Lock()

    def emit(message: dict) -> None:
        with lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def resolve(zeroconf: Zeroconf, service_type: str, service_name: str) -> None:
        found = zeroconf.get_service_info(service_type, service_name, 3000)
        if found is None:
            return
        txt = {k.decode(): (v.decode() if v is not None else "") for k, v in found.properties.items()}
        emit({"found": service_name, "txt": txt, "addresses": found.parsed_addresses(IPVersion.V4Only), "port": found.port})

    def on_change(zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            emit({"lost": name})
            return
        threading.Thread(target=resolve, args=(zeroconf, service_type, name), daemon=True).start()

    zc.register_service(info)
    browser = ServiceBrowser(zc, SERVICE_TYPE, handlers=[on_change])
    emit({"registered": True})
    try:
        sys.stdin.read()
    finally:
        browser.cancel()
        zc.unregister_service(info)
        zc.close()


if __name__ == "__main__":
    main()

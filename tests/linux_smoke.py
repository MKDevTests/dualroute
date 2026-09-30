"""Linux integration: real host sockets, shared Docker namespace and VPN counters."""
import json
import socket
import subprocess
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


http = build_opener(ProxyHandler({}))


def api(path):
    started = time.monotonic()
    with http.open("http://127.0.0.1:19080" + path, timeout=2) as response:
        result = json.load(response)
    assert time.monotonic() - started < 2, "HTTP request blocked on collection"
    return result


def eventually(predicate, timeout=70):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, AssertionError, KeyError):
            pass
        time.sleep(1)
    raise AssertionError("Timed out waiting for integration result")


def echo(connection):
    with connection:
        while connection.recv(65536):
            connection.sendall(b"response" * 100)


server = socket.socket()
server.bind(("0.0.0.0", 0))
server.listen()
port = server.getsockname()[1]


def accept():
    while True:
        connection, _ = server.accept()
        threading.Thread(target=echo, args=(connection,), daemon=True).start()


threading.Thread(target=accept, daemon=True).start()
names = ["dualroute-smoke", "dualroute-smoke-client", "dualroute-smoke-vpn"]
try:
    docker("tag", "dualroute:test", "dualroute-smoke/gluetun:local")
    mock_server = '''import http.server,json,subprocess
subprocess.run(["ip","link","add","wg0","type","dummy"],check=True)
subprocess.run(["ip","link","set","wg0","up"],check=True)
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers()
  self.wfile.write(json.dumps({"status":"running","public_ip":"203.0.113.20"}).encode())
http.server.HTTPServer(("0.0.0.0",8000),Handler).serve_forever()
'''
    docker("run", "-d", "--name", names[2], "--cap-add", "NET_ADMIN", "--health-cmd", "curl -fsS http://127.0.0.1:8000/v1/vpn/status", "--health-interval", "1s", "--health-start-period", "1s",
           "-e", 'HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE={"auth":"none"}', "--entrypoint", "python", "dualroute-smoke/gluetun:local", "-u", "-c", mock_server)
    docker("run", "-d", "--name", names[0], "--network", "host", "--cap-add", "SYS_PTRACE", "--cap-add", "SYS_ADMIN", "--cap-add", "NET_ADMIN", "--cap-add", "NET_RAW",
           "--security-opt", "no-new-privileges:true", "-v", "/proc:/host/proc:ro", "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
           "-e", "DUALROUTE_PORT=19080", "dualroute:test")
    eventually(lambda: api("/api/health")["version"] == "0.4.0", timeout=30)
    eventually(lambda: api("/api/traffic/flows").get("accounting_enabled"))
    vpn = eventually(lambda: next((item for item in api("/api/snapshot")["vpns"] if item["status"] == "connected"), None))
    assert vpn["observable"] and vpn["interfaces"] == ["wg0"], vpn
    gateway = json.loads(docker("inspect", names[2]))[0]["NetworkSettings"]["Networks"]["bridge"]["Gateway"]
    client = f'''import socket,time
s=socket.create_connection(({gateway!r},{port}))
while True:
 s.sendall(b"traffic"*200);s.recv(65536);time.sleep(.5)
'''
    docker("run", "-d", "--name", names[1], "--network", "container:" + names[2], "--entrypoint", "python", "dualroute:test", "-u", "-c", client)
    host_client = socket.create_connection(("127.0.0.1", port))
    host_client.sendall(b"host-local-test")
    host_client.recv(65536)
    def resolved_flow():
        result = api("/api/traffic/flows?limit=2000")
        assert result.get("host_processes_visible"), result
        return next((flow for flow in result["items"] if flow.get("scope") == "vpn" and flow.get("application") == names[1] and flow.get("rx_bps") is not None and flow.get("process") == "python"), None)
    flow = eventually(resolved_flow)
    assert flow["rx_bps"] > 0 and flow["tx_bps"] > 0, flow
    snapshot = api("/api/snapshot?include_app_stats=true")
    observed = next(item for item in snapshot["vpns"] if item["id"] == vpn["id"])
    assert any(app["name"] == names[1] for app in observed["apps"]), observed
    assert observed["metric"]["rx_bps"] is not None, observed
    assert any(item.get("local_only") and item.get("process") for item in api("/api/traffic/flows?limit=2000")["items"]), "Host socket/process attribution failed"
    print("Linux smoke passed: fast HTTP, Gluetun health, tunnel rates, shared-namespace application and host process attribution")
except Exception:
    for name in names:
        subprocess.run(["docker", "logs", "--tail", "50", name], check=False)
    raise
finally:
    host_client = locals().get("host_client")
    if host_client:
        host_client.close()
    for name in names:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

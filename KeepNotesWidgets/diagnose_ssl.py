"""KeepNotes Widgets - SSL / "Could not reach Google" diagnostic.

Run via diagnose-ssl.bat (double-click). Writes ssl-report-<COMPUTERNAME>.txt next to this file.
Contains no passwords, tokens or cookies.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOSTS = ["android.clients.google.com", "www.google.com", "keep.googleapis.com"]
TIMEOUT = 15


def run(cmd, timeout=60):
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (cp.stdout + cp.stderr).strip()
    except Exception as exc:  # noqa: BLE001
        return f"<failed: {type(exc).__name__}: {exc}>"


def decode_der(der: bytes) -> dict:
    """Decode a DER certificate with CPython's internal helper (no extra packages)."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(pem)
        return ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]
    finally:
        os.unlink(path)


def name_str(rdns) -> str:
    return ", ".join(f"{k}={v}" for rdn in rdns for (k, v) in rdn)


# --------------------------------------------------------------------------
# Probe: runs inside each candidate interpreter, prints JSON
# --------------------------------------------------------------------------
def probe() -> dict:
    out: dict = {"executable": sys.executable, "version": sys.version,
                 "openssl": ssl.OPENSSL_VERSION, "prefix": sys.prefix,
                 "base_prefix": sys.base_prefix, "packages": {}, "tests": {}}
    from importlib.metadata import version, PackageNotFoundError
    for pkg in ["certifi", "requests", "urllib3", "gpsoauth", "gkeepapi", "PySide6-Essentials",
                "keyring", "pycryptodomex", "truststore"]:
        try:
            out["packages"][pkg] = version(pkg)
        except PackageNotFoundError:
            out["packages"][pkg] = "NOT INSTALLED"
        except Exception as exc:  # noqa: BLE001
            out["packages"][pkg] = f"error {type(exc).__name__}"
    try:
        import certifi
        out["certifi_where"] = certifi.where()
        out["certifi_exists"] = os.path.exists(certifi.where())
    except Exception as exc:  # noqa: BLE001
        out["certifi_where"] = f"<{type(exc).__name__}: {exc}>"
    out["env_ssl"] = {k: os.environ.get(k) for k in
                      ["SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                       "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY"]
                      if os.environ.get(k)}
    try:
        import urllib.request
        out["system_proxies"] = urllib.request.getproxies()
    except Exception as exc:  # noqa: BLE001
        out["system_proxies"] = f"<{type(exc).__name__}>"

    tests = out["tests"]

    def t(name, fn):
        try:
            tests[name] = "OK - " + str(fn())
        except Exception:  # noqa: BLE001
            tests[name] = "FAIL\n" + traceback.format_exc()

    # 1) plain ssl with Windows certificate store only (this is what gpsoauth does)
    def winstore(host):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_default_certs()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                return ss.version()

    # 2) plain ssl with certifi bundle only (this is what requests/gkeepapi does)
    def certifi_only(host):
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        with socket.create_connection((host, 443), timeout=TIMEOUT) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                return ss.version()

    for h in HOSTS:
        t(f"tls[windows-store] {h}", lambda h=h: winstore(h))
        t(f"tls[certifi] {h}", lambda h=h: certifi_only(h))

    # 3) requests GET (certifi)
    def req(url):
        import requests
        r = requests.get(url, timeout=TIMEOUT)
        return f"HTTP {r.status_code}"
    t("requests GET https://www.google.com", lambda: req("https://www.google.com"))
    t("requests GET https://android.clients.google.com", lambda: req("https://android.clients.google.com"))

    # 4) exactly the gpsoauth sign-in transport (AuthHTTPAdapter), harmless GET, no credentials
    def gps():
        import requests
        import gpsoauth
        s = requests.session()
        s.mount(gpsoauth.AUTH_URL, gpsoauth.AuthHTTPAdapter())
        r = s.get(gpsoauth.AUTH_URL, timeout=TIMEOUT)
        return f"HTTP {r.status_code} (any HTTP status = TLS works)"
    t("gpsoauth transport GET android.clients.google.com/auth", gps)

    # 5) same transport with the KeepNotes fix applied (if keep_sync is importable)
    def gps_fixed():
        sys.path.insert(0, str(HERE))
        import keep_sync  # noqa: F401  (applies the trust fix on import, if present)
        import requests
        import gpsoauth
        applied = getattr(keep_sync, "_TRUST_FIX", "not present in this keep_sync.py")
        s = requests.session()
        s.mount(gpsoauth.AUTH_URL, gpsoauth.AuthHTTPAdapter())
        r = s.get(gpsoauth.AUTH_URL, timeout=TIMEOUT)
        return f"HTTP {r.status_code} (trust fix: {applied})"
    t("gpsoauth transport WITH KeepNotes trust fix", gps_fixed)
    return out


# --------------------------------------------------------------------------
# Main report (runs once)
# --------------------------------------------------------------------------
def presented_cert(host: str) -> str:
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, 443), timeout=TIMEOUT) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                der = ss.getpeercert(binary_form=True)
        c = decode_der(der)
        return (f"subject: {name_str(c.get('subject', ()))}\n"
                f"    issuer : {name_str(c.get('issuer', ()))}\n"
                f"    valid  : {c.get('notBefore')} -> {c.get('notAfter')}")
    except Exception as exc:  # noqa: BLE001
        return f"<could not fetch: {type(exc).__name__}: {exc}>"


def clock_check() -> str:
    import http.client
    lines = []
    for host in ["www.google.com", "www.microsoft.com"]:
        try:
            c = http.client.HTTPConnection(host, 80, timeout=TIMEOUT)
            c.request("HEAD", "/")
            d = c.getresponse().getheader("Date")
            c.close()
            server = email.utils.parsedate_to_datetime(d)
            local = dt.datetime.now(dt.timezone.utc)
            diff = (local - server).total_seconds()
            flag = "OK" if abs(diff) < 300 else "WRONG CLOCK (>5 min off) - fix Windows date/time!"
            lines.append(f"{host}: server Date={d} | local UTC={local:%a, %d %b %Y %H:%M:%S} | "
                         f"diff={diff:+.0f}s -> {flag}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{host}: <failed: {type(exc).__name__}: {exc}>")
    return "\n".join(lines)


def shortcuts() -> str:
    ps = r"""
$sh = New-Object -ComObject WScript.Shell
$dirs = @("$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
          "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup",
          [Environment]::GetFolderPath('Desktop'))
foreach ($d in $dirs) { if (Test-Path $d) {
  Get-ChildItem $d -Filter *.lnk -ErrorAction SilentlyContinue | Where-Object Name -match 'keep' | ForEach-Object {
    $l = $sh.CreateShortcut($_.FullName)
    $ok = if (Test-Path $l.TargetPath) {'exists'} else {'MISSING'}
    "$($_.FullName)`n    -> $($l.TargetPath) [$ok] $($l.Arguments)"
  } } }
"""
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=120) or "(no KeepNotes shortcuts found)"


def candidates() -> list[str]:
    c: list[str] = []
    venv = HERE / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        c.append(str(venv))
    c.append(sys.executable)
    r = run(["py", "-3", "-c", "import sys;print(sys.executable)"])
    if r and os.path.exists(r.splitlines()[-1].strip()):
        c.append(r.splitlines()[-1].strip())
    w = shutil.which("python")
    if w and "WindowsApps" not in w:
        c.append(w)
    for p in [r"C:\Python314\python.exe"]:
        if os.path.exists(p):
            c.append(p)
    seen, res = set(), []
    for p in c:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


def main() -> None:
    comp = os.environ.get("COMPUTERNAME", socket.gethostname())
    report = HERE / f"ssl-report-{comp}.txt"
    L: list[str] = []
    add = L.append
    add("KeepNotes Widgets - SSL diagnostic report")
    add(f"Generated : {dt.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z (UTC%z)}")
    add(f"COMPUTERNAME: {comp}   user: {os.environ.get('USERNAME')}")
    add(f"Folder    : {HERE}")
    add("")
    add("== System clock vs HTTP Date header ==")
    add(clock_check())
    add("")
    add("== Certificate presented to this PC (detects antivirus/proxy HTTPS interception) ==")
    add("   Expected issuer for Google: O=Google Trust Services (CN=WR2/WE2/...)")
    for h in HOSTS:
        add(f"  {h}\n    {presented_cert(h)}")
    add("")
    add("== Proxy settings ==")
    add("WinHTTP: " + run(["netsh", "winhttp", "show", "proxy"]).replace("\n", " ").strip())
    add("WinINet (user): " + run(["reg", "query",
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyEnable"]).replace("\n", " "))
    add("")
    add("== Windows root store: Google-related roots ==")
    add(run(["powershell", "-NoProfile", "-Command",
             "Get-ChildItem Cert:\\LocalMachine\\Root,Cert:\\CurrentUser\\Root | "
             "Where-Object Subject -match 'GTS Root|GlobalSign' | "
             "ForEach-Object { $_.PSParentPath.Split(':')[-1] + ' | ' + $_.Subject + ' | until ' + $_.NotAfter.ToString('yyyy-MM-dd') }"]) or "(none found!)")
    add("")
    add("== KeepNotes shortcuts ==")
    add(shortcuts())
    add("")
    venv = HERE / ".venv"
    add("== Synced .venv ==")
    add(f".venv exists: {venv.exists()}")
    cfg = venv / "pyvenv.cfg"
    if cfg.exists():
        add("pyvenv.cfg:\n    " + cfg.read_text(errors="replace").strip().replace("\n", "\n    "))
        home = [l.split("=", 1)[1].strip() for l in cfg.read_text(errors="replace").splitlines() if l.startswith("home")]
        if home:
            add(f"base Python of .venv ({home[0]}) exists on this PC: {os.path.exists(os.path.join(home[0], 'python.exe'))}")
    add("")
    for py in candidates():
        add("=" * 78)
        add(f"== Interpreter: {py}")
        add("=" * 78)
        raw = run([py, str(Path(__file__).resolve()), "--probe"], timeout=240)
        try:
            data = json.loads(raw.splitlines()[-1]) if raw else None
            if not isinstance(data, dict):
                raise ValueError
        except Exception:  # noqa: BLE001
            add("Could not run this interpreter / probe output:\n" + raw)
            add("")
            continue
        add(f"version : {data['version']}")
        add(f"openssl : {data['openssl']}")
        add(f"prefix  : {data['prefix']}  (base {data['base_prefix']})")
        add("packages: " + ", ".join(f"{k}={v}" for k, v in data["packages"].items()))
        add(f"certifi : {data.get('certifi_where')} exists={data.get('certifi_exists')}")
        add(f"SSL/proxy env vars: {data.get('env_ssl') or 'none'}")
        add(f"system proxies: {data.get('system_proxies') or 'none'}")
        for name, res in data["tests"].items():
            add(f"-- {name}: {res.rstrip()}")
        add("")
    add("== Summary of FAIL lines ==")
    fails = [l for l in "\n".join(L).splitlines()
             if ": FAIL" in l or "WRONG CLOCK" in l or "[MISSING]" in l or "exists on this PC: False" in l]
    add("\n".join(fails) if fails else "No failures detected.")
    text = "\n".join(L) + "\n"
    report.write_text(text, encoding="utf-8")
    print(text)
    print(f"Report written to: {report}")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        print(json.dumps(probe()))
    else:
        main()

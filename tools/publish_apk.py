"""Publish a built APK to the NAS: a share folder and the web server.

Default publishes the sanitized release build. Use --apk/--name for a local
build that has this deployment's address baked in:

  python tools/publish_apk.py
  python tools/publish_apk.py --apk private/DGX-Chat-1.0.0-nas.apk \
                              --name DGX-Chat-1.0.0-nas.apk
"""
import argparse
import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nas_ssh  # noqa: E402

REPO = os.path.normpath(os.path.join(HERE, ".."))
# Where to drop the APK on the NAS. Override with APK_SHARE_DIR for your own
# share layout; the web copy is served by the backend at /release/.
SHARE_DIR = os.environ.get("APK_SHARE_DIR", "/volume1/StorageSpaceV1D2/APK")
WEB_DIR = os.environ.get("APK_WEB_DIR", "/volume1/docker/dgxchat/web/release")
BASE_URL = "http://%s:%s" % (nas_ssh.HOST, os.environ.get("APK_WEB_PORT", "8439"))

README_TEMPLATE = """DGX Chat - Android client
===========================

{name}
  sha256 {digest}
  {size} bytes

Install
-------
1. On the phone, open {url}/release/{name} in a browser and download
   (Android cannot browse SMB shares without extra software, so HTTP is easier).
2. Allow "install unknown apps" and install.
3. On first launch {address_hint}

Notes
-----
* {default_note}
* If an older build is already installed with a different signing key,
  uninstall it first.
* Verify the download:
    certutil -hashfile {name} SHA256     (Windows)
    sha256sum {name}                     (Linux/macOS)
"""


def upload(path, data, mode="644"):
    rc, out, err = nas_ssh.exec_stdin("cat > %s && chmod %s %s" % (path, mode, path), data)
    if rc != 0:
        print("  FAILED %s: %s" % (path, err[:300]))
    return rc == 0


def ensure(path):
    rc, out, err = nas_ssh.run("mkdir -p %s && echo ok" % path)
    return "ok" in out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", default=os.path.join(REPO, "release", "DGX-Chat-1.0.0.apk"))
    ap.add_argument("--name", default="DGX-Chat-1.0.0.apk")
    ap.add_argument("--personal", action="store_true",
                    help="the build already knows this server's address")
    a = ap.parse_args()

    if not os.path.exists(a.apk):
        sys.exit("APK not found: %s" % a.apk)
    blob = open(a.apk, "rb").read()
    digest = hashlib.sha256(blob).hexdigest()
    print("local APK: %s\n  %d bytes  sha256=%s" % (a.apk, len(blob), digest))

    if a.personal:
        address_hint = "the server address is already filled in - just sign in."
        default_note = "This build is pre-configured for this server. Do not publish it."
    else:
        address_hint = "enter your server address (%s) and sign in." % BASE_URL
        default_note = "The APK contains NO server address; you type it yourself."
    readme = README_TEMPLATE.format(name=a.name, digest=digest, size=len(blob),
                                    url=BASE_URL, address_hint=address_hint,
                                    default_note=default_note)

    for target in (SHARE_DIR, WEB_DIR):
        print("\n--- %s ---" % target)
        if not ensure(target):
            print("  cannot create directory")
            continue
        upload(target + "/" + a.name, blob)
        upload(target + "/" + a.name + ".sha256", ("%s  %s\n" % (digest, a.name)).encode())
        upload(target + "/README-%s.txt" % a.name.replace(".apk", ""), readme.encode("utf-8"))

    script = (
        "for f in %s/%s %s/%s; do "
        "  if [ -f \"$f\" ]; then "
        "    echo \"$(stat -c '%%s' \"$f\") bytes  $(sha256sum \"$f\" | cut -d' ' -f1)  $f\"; "
        "  else echo \"MISSING $f\"; fi; "
        "done" % (SHARE_DIR, a.name, WEB_DIR, a.name)
    )
    rc, out, err = nas_ssh.run(script)
    print("\n--- verify remote copies ---")
    print(out)
    if err.strip():
        print("stderr:", err[:300])
    print("download: %s/release/%s" % (BASE_URL, a.name))


if __name__ == "__main__":
    main()

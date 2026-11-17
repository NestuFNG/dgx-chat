"""Publish a built APK to the NAS: a share folder and the web server.

Default publishes the sanitized release build. Use --apk/--name for a local
build that has this deployment's address baked in:

  python tools/publish_apk.py
  python tools/publish_apk.py --apk private/DGX-Chat-1.0.0-nas.apk \
                              --name DGX-Chat-1.0.0-nas.apk
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

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
    ap.add_argument("--apk", default=None,
                    help="default: release/DGX-Chat-<version>.apk")
    ap.add_argument("--name", default=None, help="default: file name of --apk")
    ap.add_argument("--personal", action="store_true",
                    help="the build already knows this server's address")
    ap.add_argument("--all", action="store_true",
                    help="publish every variant that exists locally, then the update manifest")
    ap.add_argument("--manifest", action="store_true",
                    help="only (re)write release/latest.json for the in-app updater")
    a = ap.parse_args()

    if a.manifest:
        publish_manifest()
        return

    if a.all:
        for _name, (local, env_var, remote) in VARIANTS.items():
            if not os.path.exists(local):
                continue
            if env_var and not os.environ.get(env_var):
                print("skip %s (set %s to publish it)" % (_name, env_var))
                continue
            push_one(local, remote, personal=(_name != "generic"))
        publish_manifest()
        return

    apk = a.apk or os.path.join(REPO, "release", "DGX-Chat-%s.apk" % VERSION)
    push_one(apk, a.name or os.path.basename(apk), a.personal)


def push_one(apk_path, name, personal):
    if not os.path.exists(apk_path):
        sys.exit("APK not found: %s" % apk_path)
    blob = open(apk_path, "rb").read()
    digest = hashlib.sha256(blob).hexdigest()
    print("local APK: %s\n  %d bytes  sha256=%s" % (apk_path, len(blob), digest))

    if personal:
        address_hint = "the server address is already filled in - just sign in."
        default_note = "This build is pre-configured for this server. Do not publish it."
    else:
        address_hint = "enter your server address (%s) and sign in." % BASE_URL
        default_note = "The APK contains NO server address; you type it yourself."
    readme = README_TEMPLATE.format(name=name, digest=digest, size=len(blob),
                                    url=BASE_URL, address_hint=address_hint,
                                    default_note=default_note)

    for target in (SHARE_DIR, WEB_DIR):
        print("\n--- %s ---" % target)
        if not ensure(target):
            print("  cannot create directory")
            continue
        upload(target + "/" + name, blob)
        upload(target + "/" + name + ".sha256", ("%s  %s\n" % (digest, name)).encode())
        upload(target + "/README-%s.txt" % name.replace(".apk", ""), readme.encode("utf-8"))

    script = (
        "for f in %s/%s %s/%s; do "
        "  if [ -f \"$f\" ]; then "
        "    echo \"$(stat -c '%%s' \"$f\") bytes  $(sha256sum \"$f\" | cut -d' ' -f1)  $f\"; "
        "  else echo \"MISSING $f\"; fi; "
        "done" % (SHARE_DIR, name, WEB_DIR, name)
    )
    rc, out, err = nas_ssh.run(script)
    print("\n--- verify remote copies ---")
    print(out)
    if err.strip():
        print("stderr:", err[:300])
    print("download: %s/release/%s" % (BASE_URL, name))


def app_version():
    """Version string baked into the client bundle (app/package.json)."""
    path = os.path.join(REPO, "app", "package.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


# variant name -> (local file, origin env var, remote name)
# File names follow app/package.json so a new release never overwrites the
# build that is already installed.
VERSION = app_version()
VARIANTS = {
    "generic": (os.path.join(REPO, "release", "DGX-Chat-%s.apk" % VERSION), None,
                "DGX-Chat-%s.apk" % VERSION),
    "lan": (os.path.join(REPO, "private", "DGX-Chat-%s-nas.apk" % VERSION), "APK_LAN_ORIGIN",
            "DGX-Chat-%s-nas.apk" % VERSION),
    "domain": (os.path.join(REPO, "private", "DGX-Chat-%s-domain.apk" % VERSION), "APK_DOMAIN_ORIGIN",
               "DGX-Chat-%s-domain.apk" % VERSION),
}


def build_manifest():
    """Describe every variant that exists locally, for the in-app updater."""
    variants = []
    for name, (local, env_var, remote) in VARIANTS.items():
        if not os.path.exists(local):
            continue
        with open(local, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        origin = os.environ.get(env_var) if env_var else None
        if env_var and not origin:
            continue  # a pre-configured variant without a documented origin is useless
        variants.append({
            "name": name,
            "origin": origin,
            "url": "/release/" + remote,
            "sha256": digest,
            "size": os.path.getsize(local),
        })
    return {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "latest": {
            "version": app_version(),
            "versionCode": int(os.environ.get("APK_VERSION_CODE", "0") or 0),
            "notes": os.environ.get("APK_RELEASE_NOTES", ""),
        },
        "variants": variants,
    }


def publish_manifest():
    manifest = build_manifest()
    payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    for target in (SHARE_DIR, WEB_DIR):
        if ensure(target):
            upload(target + "/latest.json", payload)
    print("\n--- update manifest ---")
    print(payload.decode("utf-8"))
    return manifest


if __name__ == "__main__":
    main()

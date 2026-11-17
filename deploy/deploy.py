"""Deploy the DGX Chat backend to a remote host (NAS / home server) over SSH.

  python deploy.py sync     # upload server/ + write .env, no container actions
  python deploy.py up       # build + (re)start the container
  python deploy.py logs     # tail logs
  python deploy.py status   # container + health check
  python deploy.py down     # stop container
  python deploy.py all      # sync + up + status

SSH credentials come from tools/nas.env or the environment (see
tools/nas.env.example). The backend configuration is read from
deploy/.env.deploy (never committed) or the environment; deploy/.env.deploy.example
lists every key. Nothing sensitive has a built-in default.

Remote layout defaults to /volume1/docker/dgxchat (Synology-style); override it
with the DGXCHAT_REMOTE_ROOT environment variable.
"""
import io
import os
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.normpath(os.path.join(HERE, "..", "tools"))
REMOTE_ROOT = os.environ.get("DGXCHAT_REMOTE_ROOT", "/volume1/docker/dgxchat").rstrip("/")
REMOTE_APP = REMOTE_ROOT + "/app"
REMOTE_DATA = REMOTE_ROOT + "/data"
# The NAS SFTP server is chrooted to /volume1, so SFTP paths omit that prefix.
SFTP_PREFIX = "/volume1"
SERVER_DIR = os.path.normpath(os.path.join(HERE, "..", "server"))
DIST_DIR = os.path.normpath(os.path.join(HERE, "..", "app", "dist"))
REMOTE_WEB = REMOTE_ROOT + "/web"
EXCLUDE = {".venv", "__pycache__", ".pytest_cache", ".uv", "node_modules", ".git", ".mypy_cache",
           "data", "artifacts"}

# Generic, non-secret defaults only. Anything real (API keys, private gateway
# URLs, model ids) belongs in deploy/.env.deploy or the environment.
DEFAULT_ENV = {
    "DGX_BASE_URL": "http://localhost:8041/v1",
    "DGX_API_KEY": "",
    "SEARCH_MODE": "dgx",
    "TTS_PROVIDER": "deepseek",
    "ASR_PROVIDER": "deepseek",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "DATA_DIR": "/data",
    "HOST": "0.0.0.0",
    "PORT": "8439",
    "CORS_ALLOW_ORIGINS": "*",
    "LOG_LEVEL": "info",
}

_nas_ssh = None


def nas_ssh_module():
    """Import tools/nas_ssh.py lazily so the config loader works without paramiko."""
    global _nas_ssh
    if _nas_ssh is None:
        if TOOLS not in sys.path:
            sys.path.insert(0, TOOLS)
        import nas_ssh  # noqa: F401
        _nas_ssh = nas_ssh
    return _nas_ssh


class _LazySSH(object):
    """Proxy that imports tools/nas_ssh on first attribute access.

    Keeps the existing ``nas_ssh.run(...)`` call sites unchanged while letting
    ``load_env()`` be imported (e.g. for a dry run) without paramiko installed.
    """

    def __getattr__(self, name):
        return getattr(nas_ssh_module(), name)


nas_ssh = _LazySSH()


def load_env():
    """Read deploy/.env.deploy, then let the real environment win."""
    env = dict(DEFAULT_ENV)
    path = os.path.join(HERE, ".env.deploy")
    if os.path.exists(path):
        with io.open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in list(env):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def make_tarball():
    buf = io.BytesIO()
    count = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(SERVER_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, SERVER_DIR)
                if rel.endswith(".pyc") or rel.startswith("."):
                    continue
                tar.add(full, arcname=rel)
                count += 1
    buf.seek(0)
    return buf, count


def sftp_put_bytes(data, remote):
    client = nas_ssh.connect()
    try:
        sftp = client.open_sftp()
        with sftp.file(remote, "wb") as fh:
            fh.write(data)
        sftp.close()
    finally:
        client.close()


def make_dist_tarball():
    """Package app/dist as the browser-served web UI."""
    buf = io.BytesIO()
    count = 0
    if not os.path.isdir(DIST_DIR):
        return None, 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(DIST_DIR):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, DIST_DIR)
                tar.add(full, arcname=rel)
                count += 1
    buf.seek(0)
    return buf, count


def ensure_remote_dirs():
    """Create the remote tree with sudo (the NAS docker root is root-owned).

    app/ and web/ are handed to the SSH user so sftp can write into them;
    data/ is left to root because the container chowns it to its own uid.
    """
    script = (
        "set -e\n"
        "mkdir -p {root} {app} {data} {web}\n"
        "chown {user} {root} {app} {web} 2>/dev/null || true\n"
        "chmod 755 {root} {app} {web}\n"
        "ls -ld {root} {app} {data} {web}\n"
    ).format(root=REMOTE_ROOT, app=REMOTE_APP, data=REMOTE_DATA, web=REMOTE_WEB,
             user=nas_ssh.USER)
    rc, out, err = nas_ssh.run_sudo(script)
    print(out.strip())
    if err.strip():
        print("stderr:", err[:1000])
    return rc == 0


def do_sync():
    env = load_env()
    if not ensure_remote_dirs():
        print("ERROR: could not prepare remote directories")
        return False
    buf, count = make_tarball()
    print("packed %d server files (%.1f KB)" % (count, len(buf.getvalue()) / 1024.0))
    web_buf, web_count = make_dist_tarball()
    if web_buf is not None:
        print("packed %d web files (%.1f KB)" % (web_count, len(web_buf.getvalue()) / 1024.0))
    else:
        print("WARNING: app/dist missing - the browser UI will not be served")
    client = nas_ssh.connect()
    stage = REMOTE_ROOT
    try:
        sftp_ok = False
        try:
            sftp = client.open_sftp()
            sftp.listdir("/docker/dgxchat")
            sftp_ok = True
            sftp.close()
        except Exception:
            sftp_ok = False
    finally:
        client.close()

    if not sftp_ok:
        # This NAS's SFTP is chrooted to a different volume, so stream the
        # archives over SSH instead (we own app/ and web/, no sudo needed).
        print("sftp not usable for this path - streaming over ssh")
        rc1, o1, e1 = nas_ssh.exec_stdin(
            "rm -rf %s/* ; tar -xzf - -C %s" % (REMOTE_APP, REMOTE_APP), buf.getvalue())
        if rc1 != 0:
            print("server extract failed:", e1[:800])
            return False
        if web_buf is not None:
            rc2, o2, e2 = nas_ssh.exec_stdin(
                "rm -rf %s/* ; tar -xzf - -C %s" % (REMOTE_WEB, REMOTE_WEB), web_buf.getvalue())
            if rc2 != 0:
                print("web extract failed:", e2[:800])
                return False
        env_text = "\n".join("%s=%s" % (k, v) for k, v in sorted(env.items())) + "\n"
        rc3, o3, e3 = nas_ssh.exec_stdin(
            "cat > %s/.env" % REMOTE_APP, env_text.encode("utf-8"))
        if rc3 != 0:
            print("env write failed:", e3[:800])
            return False
        rc, out, err = nas_ssh.run("ls %s && echo '--- web ---' && ls %s | head"
                                   % (REMOTE_APP, REMOTE_WEB))
        print(out)
        if err.strip():
            print("stderr:", err[:800])
        return rc == 0

    try:
        sftp = client.open_sftp()
        with sftp.file(stage + "/_upload_server.tar.gz", "wb") as fh:
            fh.write(buf.getvalue())
        if web_buf is not None:
            with sftp.file(stage + "/_upload_web.tar.gz", "wb") as fh:
                fh.write(web_buf.getvalue())
        env_text = "\n".join("%s=%s" % (k, v) for k, v in sorted(env.items())) + "\n"
        with sftp.file(stage + "/_upload.env", "wb") as fh:
            fh.write(env_text.encode("utf-8"))
        sftp.close()
    finally:
        client.close()
    script = (
        "set -e\n"
        "cd {root}\n"
        "rm -rf {app}/*\n"
        "tar -xzf _upload_server.tar.gz -C {app}\n"
        "cp _upload.env {app}/.env\n"
        "if [ -f _upload_web.tar.gz ]; then rm -rf {web}/*; "
        "tar -xzf _upload_web.tar.gz -C {web}; fi\n"
        "rm -f _upload_server.tar.gz _upload_web.tar.gz _upload.env\n"
        "ls {app}\n"
    ).format(root=REMOTE_ROOT, app=REMOTE_APP, web=REMOTE_WEB)
    rc, out, err = nas_ssh.run_sudo(script)
    print(out)
    if err.strip():
        print("stderr:", err[:2000])
    return rc == 0


def do_up():
    script = (
        "set -e\n"
        "cd {app}\n"
        "docker compose down --remove-orphans 2>/dev/null || true\n"
        "docker compose up -d --build\n"
        "sleep 3\n"
        "docker compose ps\n"
    ).format(app=REMOTE_APP)
    rc, out, err = nas_ssh.run_sudo(script)
    print(out)
    if err.strip():
        print("stderr:", err[:3000])
    return rc == 0


def do_status():
    script = (
        "cd {app} 2>/dev/null && docker compose ps 2>&1 | head -20\n"
        "echo '--- health ---'\n"
        "curl -s -m 8 http://127.0.0.1:8439/health; echo\n"
        "echo '--- models ---'\n"
        "curl -s -m 8 -o /dev/null -w '%{{http_code}}\\n' http://127.0.0.1:8439/models\n"
    ).format(app=REMOTE_APP)
    rc, out, err = nas_ssh.run_sudo(script)
    print(out)
    if err.strip():
        print("stderr:", err[:2000])
    return rc == 0


def do_logs():
    rc, out, err = nas_ssh.run_sudo("cd %s && docker compose logs --tail=120" % REMOTE_APP)
    print(out)
    if err.strip():
        print("stderr:", err[:2000])


def do_down():
    rc, out, err = nas_ssh.run_sudo("cd %s && docker compose down" % REMOTE_APP)
    print(out)
    return rc == 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "all"
    if action == "sync":
        sys.exit(0 if do_sync() else 1)
    if action == "up":
        sys.exit(0 if do_up() else 1)
    if action == "status":
        do_status()
    elif action == "logs":
        do_logs()
    elif action == "down":
        do_down()
    elif action == "all":
        if not do_sync():
            sys.exit(1)
        ok = do_up()
        time.sleep(2)
        do_status()
        sys.exit(0 if ok else 1)
    else:
        sys.exit("unknown action " + action)

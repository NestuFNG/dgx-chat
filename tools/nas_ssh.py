"""SSH helper for the machine that hosts the DGX Chat container (a NAS or any
Linux box with docker).

Connection details are NEVER stored in this file. They are read from the process
environment, or from an optional git-ignored ``tools/nas.env`` file:

    NAS_HOST=your-server.local      # required
    NAS_USER=your-user              # required
    NAS_PASS=your-password          # required unless NAS_KEY_FILE is set
    NAS_KEY_FILE=~/.ssh/id_ed25519  # optional: public-key auth instead
    NAS_PORT=22                     # optional
    NAS_SUDO=1                      # 1 = the remote user needs sudo (default 1)

Start from the template:

    cp tools/nas.env.example tools/nas.env    # then edit it

Usage:
  python nas_ssh.py run "<command>"
  python nas_ssh.py bash <script-file>       # run a local bash script remotely
  python nas_ssh.py put <local> <remote>     # sftp upload
  python nas_ssh.py get <remote> <local>     # sftp download
"""
import os
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.environ.get("NAS_ENV_FILE") or os.path.join(HERE, "nas.env")


def _load_env_file(path):
    """Populate os.environ from a KEY=VALUE file.

    A variable that is already set to a non-empty value wins; empty variables
    count as unset so an exported-but-empty NAS_HOST cannot shadow the file.
    """
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or os.environ.get(key, "").strip():
                continue
            os.environ[key] = value


_load_env_file(ENV_FILE)

HOST = os.environ.get("NAS_HOST", "").strip()
USER = os.environ.get("NAS_USER", "").strip()
PASS = os.environ.get("NAS_PASS", "")
PORT = int(os.environ.get("NAS_PORT", "22") or 22)
KEY_FILE = os.environ.get("NAS_KEY_FILE", "").strip()
SUDO = os.environ.get("NAS_SUDO", "1") == "1"


def _require(name, value, hint):
    if not value:
        sys.exit(
            "nas_ssh: %s is not set.\n"
            "Set it in the environment, or copy tools/nas.env.example to\n"
            "tools/nas.env (git-ignored) and fill it in. %s" % (name, hint)
        )
    return value


def validate():
    """Fail with a readable message when the connection settings are missing."""
    _require("NAS_HOST", HOST, "Example: NAS_HOST=192.0.2.10 or NAS_HOST=nas.lan")
    _require("NAS_USER", USER, "Example: NAS_USER=deploy")
    if not KEY_FILE:
        _require("NAS_PASS", PASS,
                 "Or set NAS_KEY_FILE to an SSH private key instead of a password.")
    if SUDO and not PASS:
        sys.exit("nas_ssh: NAS_SUDO=1 needs NAS_PASS (sudo prompts for a password). "
                 "Set NAS_SUDO=0 if the remote user is root or has passwordless sudo.")


def connect():
    validate()
    import paramiko  # imported lazily so config-only use needs no dependency

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(
        hostname=HOST,
        port=PORT,
        username=USER,
        timeout=25,
        allow_agent=False,
        look_for_keys=False,
    )
    if KEY_FILE:
        kwargs["key_filename"] = os.path.expanduser(KEY_FILE)
    else:
        kwargs["password"] = PASS
    client.connect(**kwargs)
    return client


def _exec(client, cmd, timeout=1800):
    chan = client.get_transport().open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    if SUDO:
        chan.sendall((PASS + "\n").encode())
    out = b""
    err = b""
    while True:
        if chan.recv_ready():
            out += chan.recv(65536)
        if chan.recv_stderr_ready():
            err += chan.recv_stderr(65536)
        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
    rc = chan.recv_exit_status()
    while chan.recv_ready():
        out += chan.recv(65536)
    while chan.recv_stderr_ready():
        err += chan.recv_stderr(65536)
    return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def run(cmd, timeout=1800):
    client = connect()
    try:
        return _exec(client, cmd, timeout)
    finally:
        client.close()


def run_sudo(cmd, timeout=1800):
    """Run with sudo -S, password fed over the same channel."""
    return run("sudo -S -p '' bash -lc " + shquote(cmd), timeout)


def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def put_temp(text, remote):
    client = connect()
    try:
        sftp = client.open_sftp()
        with sftp.file(remote, "w") as fh:
            fh.write(text)
        sftp.chmod(remote, stat.S_IRWXU)
        sftp.close()
    finally:
        client.close()


def exec_stdin(cmd, data, timeout=1800):
    """Run cmd over SSH and feed data to its stdin."""
    client = connect()
    try:
        chan = client.get_transport().open_session()
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        chan.sendall(data)
        chan.shutdown_write()
        out = b""
        err = b""
        while True:
            if chan.recv_ready():
                out += chan.recv(65536)
            if chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
        rc = chan.recv_exit_status()
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    finally:
        client.close()


def put(local, remote):
    client = connect()
    try:
        sftp = client.open_sftp()
        sftp.put(local, remote)
        sftp.close()
    finally:
        client.close()


def get(remote, local):
    client = connect()
    try:
        sftp = client.open_sftp()
        sftp.get(remote, local)
        sftp.close()
    finally:
        client.close()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    if action == "run":
        rc, out, err = run(sys.argv[2])
    elif action == "bash":
        with open(sys.argv[2], "r", encoding="utf-8") as fh:
            script = fh.read()
        sftp_remote = "/tmp/_dsh_%d.sh" % os.getpid()
        put_temp(script, sftp_remote)
        rc, out, err = run("bash " + sftp_remote + "; rm -f " + sftp_remote)
    elif action == "put":
        put(sys.argv[2], sys.argv[3])
        sys.exit(0)
    elif action == "get":
        get(sys.argv[2], sys.argv[3])
        sys.exit(0)
    elif action == "check":
        rc, out, err = run("uname -a; docker --version; docker compose version")
    else:
        sys.exit("unknown action")
    sys.stdout.write(out)
    if err:
        sys.stderr.write("--- STDERR ---\n" + err)
    sys.stdout.write("\n[rc=%d]\n" % rc)

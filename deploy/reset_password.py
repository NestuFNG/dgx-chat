"""Emergency password reset for DGX Chat (run from this workstation).

Use when nobody can log in any more — the admin password is lost, or the only
admin account is locked out. Writes a new scrypt hash straight into the
container's SQLite database and revokes every token for that user.

  python deploy\\reset_password.py --user admin --password 'new-password'
  python deploy\\reset_password.py --user admin              # generate a random one
  python deploy\\reset_password.py --list                   # show accounts

The hash format is self-describing (scrypt$N$r$p$salt$digest), so it is computed
locally with hashlib and no application code has to run inside the container.
"""
import argparse
import base64
import hashlib
import hmac
import os
import secrets
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import nas_ssh  # noqa: E402

CONTAINER = "dgxchat"
DB_PATH = "/data/dgxchat.db"
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


def hash_password(password, n=SCRYPT_N):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P,
        dklen=32, maxmem=96 * 1024 * 1024,
    )
    return "scrypt$%d$%d$%d$%s$%s" % (n, SCRYPT_R, SCRYPT_P, salt.hex(), digest.hex())


def run_python_in_container(script):
    """Run a python script inside the container without shell-quoting problems."""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    cmd = (
        'docker exec %s python -c "import base64;exec(base64.b64decode(\'%s\').decode())"'
        % (CONTAINER, encoded)
    )
    return nas_ssh.run_sudo(cmd)


def list_users():
    script = (
        "import sqlite3\n"
        "c=sqlite3.connect('%s')\n"
        "for row in c.execute('SELECT username, role, disabled FROM users ORDER BY created_at'):\n"
        "    print('%%-24s %%s%%s' %% (row[0], row[1], ' (disabled)' if row[2] else ''))\n"
        "c.close()\n" % DB_PATH
    )
    rc, out, err = run_python_in_container(script)
    print(out or err)
    return rc == 0


def reset(username, password):
    key = username.strip().lower()
    stamp = int(time.time() * 1000)
    new_hash = hash_password(password)
    script = (
        "import sqlite3,sys\n"
        "c=sqlite3.connect('%s')\n"
        "cur=c.execute('UPDATE users SET password_hash=?, updated_at=? WHERE username_key=?',\n"
        "               (%r, %d, %r))\n"
        "if cur.rowcount != 1:\n"
        "    print('user not found:', %r); sys.exit(2)\n"
        "c.execute('UPDATE tokens SET revoked=1 WHERE user_id=(SELECT id FROM users WHERE username_key=?)', (%r,))\n"
        "c.commit()\n"
        "n=c.execute('SELECT COUNT(*) FROM tokens WHERE user_id=(SELECT id FROM users WHERE username_key=?) AND revoked=0', (%r,)).fetchone()[0]\n"
        "print('ok; active tokens left:', n)\n"
        "c.close()\n" % (DB_PATH, new_hash, stamp, key, key, key, key)
    )
    rc, out, err = run_python_in_container(script)
    if rc != 0:
        print("FAILED:", out.strip(), err.strip()[:500])
        return False
    print("password updated for %r" % username)
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list accounts")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=None)
    a = ap.parse_args()
    if a.list:
        sys.exit(0 if list_users() else 1)
    pw = a.password
    generated = False
    if not pw:
        pw = secrets.token_urlsafe(12)
        generated = True
    if len(pw) < 6:
        sys.exit("password must be at least 6 characters")
    ok = reset(a.user, pw)
    if ok and generated:
        print("NEW PASSWORD: %s" % pw)
        print("(change it after logging in: /admin -> 我的账号 -> 修改密码)")
    sys.exit(0 if ok else 1)

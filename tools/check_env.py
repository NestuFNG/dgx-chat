import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nas_ssh

CMD = (
    "docker exec dgxchat env > /tmp/_dgxenv.txt 2>/dev/null; "
    "grep -E 'DGX_BASE_URL|DGX_THINKING|SEARCH_MODE|REGISTRATION_MODE|DGXCHAT_DOCS|TTS_PROVIDER|ASR_PROVIDER|DAILY_' /tmp/_dgxenv.txt; "
    "rm -f /tmp/_dgxenv.txt"
)
rc, out, err = nas_ssh.run_sudo(CMD)
print(out)
if err.strip():
    print("stderr:", err[:600])
print("[rc=%d]" % rc)

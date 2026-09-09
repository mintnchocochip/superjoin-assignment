"""One command to run everything:

    python run.py

Installs what is missing, opens the page, serves it. Nothing else to set up -
the model is the Claude Code CLI on this machine unless an API key says otherwise.
"""

import subprocess
import sys
import threading
import webbrowser

PORT = 8000
URL = f"http://127.0.0.1:{PORT}"

if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                   check=True)
    print(f"\n  Fact Knowledge Layer -> {URL}   (ctrl-c to stop)\n")
    threading.Timer(1.5, webbrowser.open, [URL]).start()
    subprocess.run([sys.executable, "-m", "uvicorn", "app:app", "--port", str(PORT)])

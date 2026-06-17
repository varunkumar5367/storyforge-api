import subprocess
import re
import time

def test_tunnel():
    print("Starting cloudflared...")
    cmd = ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8000"]
    # Run cloudflared, capture stderr since it prints diagnostics there
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    
    url = None
    start_time = time.time()
    # Read stderr line by line
    while time.time() - start_time < 30:
        line = proc.stderr.readline()
        if not line:
            break
        print("CF Log:", line.strip())
        match = re.search(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com", line)
        if match:
            url = match.group(0)
            print(f"\nFOUND TUNNEL URL: {url}\n")
            break
            
    proc.terminate()
    proc.wait()
    print("cloudflared stopped.")

if __name__ == "__main__":
    test_tunnel()

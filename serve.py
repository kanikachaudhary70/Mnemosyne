import socket
import uvicorn
import sys
from pathlib import Path

# Add project roots to sys.path so it works from any directory
root_dir = Path(__file__).resolve().parent
search_paths = [
    root_dir,
    root_dir / "mnemosyne-main",
    root_dir / "mnemosyne-main" / "Mnemosyne-main",
    root_dir / "Mnemosyne-main",
    root_dir.parent / "mnemosyne-main" / "Mnemosyne-main",
]
for p in search_paths:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

def run():
    port = 8085
    # Create dual-stack socket to support both IPv6 (::1, localhost) and IPv4 (127.0.0.1) on Windows
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    except Exception:
        pass
    sock.bind(('::', port))
    sock.listen(128)
    
    print(f"Mnemosyne server running at http://localhost:{port}/ (dual-stack IPv4/IPv6)")
    
    config = uvicorn.Config(
        "api.index:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=False
    )
    server = uvicorn.Server(config)
    server.run(sockets=[sock])

if __name__ == "__main__":
    run()

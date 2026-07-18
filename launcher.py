"""Desktop entry point for Soft Clipper.

Starts the FastAPI/uvicorn server locally and opens the app in the default
browser. This is the script PyInstaller freezes into the .exe.
"""
import socket
import sys
import threading
import time
import webbrowser


def _free_port(preferred: int = 8501) -> int:
    for port in (preferred, 8502, 8503, 8600, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    return preferred


def main():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"

    # importing the app also runs its frozen-aware path setup
    import uvicorn
    from backend.main import app

    def open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    print("=" * 52)
    print("  Soft Clipper")
    print(f"  Open in your browser:  {url}")
    print("  (Keep this window open while using the app.)")
    print("=" * 52)

    threading.Thread(target=open_browser, daemon=True).start()

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

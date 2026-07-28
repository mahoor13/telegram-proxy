import os
import signal
import sys
import urllib.request


def main():
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:5000/health",
            timeout=3,
        ) as response:
            if response.status == 200:
                return 0
    except Exception:
        pass

    # Docker Compose does not restart an unhealthy container while PID 1 is
    # still alive. Terminating Gunicorn lets restart: unless-stopped recover a
    # process whose master is alive but whose worker cannot serve requests.
    try:
        os.kill(1, signal.SIGTERM)
    except ProcessLookupError:
        pass

    return 1


if __name__ == "__main__":
    sys.exit(main())

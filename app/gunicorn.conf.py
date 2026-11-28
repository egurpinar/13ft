from os import environ

# https://docs.gunicorn.org/en/stable/settings.html#settings
_host = environ.get("HOST", "127.0.0.1")
if ":" in _host and not _host.startswith("["):
    _host = f"[{_host}]"  # IPv6 literal
bind = f"{_host}:{environ.get('PORT', 5000)}"

# One process, many threads: fetch jobs and their progress streams live in
# process memory, and a progress stream holds its thread while it is open.
workers = 1
worker_class = "gthread"
threads = int(environ.get("THREADS", 8))
timeout = 120

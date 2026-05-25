#!/usr/bin/env python3
"""PVE Backup App - Standalone Docker application entry point."""

import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("PVE_BACKUP_HOST", "0.0.0.0")
    port = int(os.environ.get("PVE_BACKUP_PORT", "5000"))
    debug = os.environ.get("PVE_BACKUP_DEBUG", "false").lower() == "true"

    # Use waitress in production for Windows compatibility
    use_waitress = os.environ.get("PVE_BACKUP_USE_WAITRESS", "true").lower() == "true"

    if use_waitress and not debug:
        from waitress import serve
        print(f"Starting PVE Backup App on {host}:{port} (waitress)")
        serve(app, host=host, port=port)
    else:
        print(f"Starting PVE Backup App on {host}:{port} (flask dev)")
        app.run(host=host, port=port, debug=debug)

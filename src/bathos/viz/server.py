"""FastAPI local visualization server for `bth view`."""
from __future__ import annotations

import threading
import webbrowser
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from bathos.schema import Run
from bathos.campaigns import Campaign
from bathos.viz.html import render_html_report


def create_app(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    total_run_count: int | None = None,
) -> FastAPI:
    """Create a FastAPI app that serves a pre-rendered HTML report.

    Args:
        runs: List of Run objects to display
        campaigns: Optional list of Campaign objects to display
        total_run_count: Optional total run count (defaults to len(runs))

    Returns:
        FastAPI app with a GET / endpoint returning the HTML report
    """
    # Pre-render the HTML report
    html_content = render_html_report(
        runs, campaigns=campaigns or [], total_run_count=total_run_count
    )

    # Create FastAPI app
    app = FastAPI(title="bathos dashboard")

    @app.get("/", response_class=HTMLResponse)
    def get_report() -> str:
        """Return the pre-rendered HTML report."""
        return html_content

    return app


def run_server(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    total_run_count: int | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
) -> None:
    """Run the visualization server with optional browser auto-open.

    Args:
        runs: List of Run objects to display
        campaigns: Optional list of Campaign objects to display
        total_run_count: Optional total run count (defaults to len(runs))
        host: Host to bind to (default: 127.0.0.1)
        port: Port to bind to (default: 8080)
        open_browser: Whether to automatically open the browser (default: True)

    Raises:
        OSError: If the port is already in use
    """
    # Create the app
    app = create_app(runs, campaigns=campaigns, total_run_count=total_run_count)

    # Start browser in daemon thread if requested
    if open_browser:

        def open_browser_later() -> None:
            """Sleep then open the browser."""
            import time

            time.sleep(1)
            webbrowser.open(f"http://{host}:{port}")

        thread = threading.Thread(target=open_browser_later, daemon=True)
        thread.start()

    # Start uvicorn server
    try:
        import uvicorn

        uvicorn.run(app, host=host, port=port, log_level="info")
    except OSError as e:
        if "Address already in use" in str(e):
            raise OSError(
                f"Port {port} is already in use. Try: bth view --port {port + 1}"
            ) from e
        raise

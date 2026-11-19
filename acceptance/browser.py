"""Select the native headless test browser."""

import os


def chromium(playwright):
    channel = os.getenv("CUEPORCH_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "")
    return playwright.chromium.launch(
        headless=True, **({"channel": channel} if channel else {})
    )

"""Validate live backend URLs without printing credentials supplied by mistake."""

import argparse
from urllib.parse import urlsplit


def backend_base_url(value: str) -> str:
    try:
        if any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise ValueError("whitespace or control character")
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("an http or https scheme and a host are required")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("credentials are not allowed in the URL")
        if parsed.netloc.endswith(":"):
            raise ValueError("empty port")
        _ = parsed.port  # validates decimal format and range
    except ValueError:
        raise argparse.ArgumentTypeError(
            "invalid backend URL: use http or https, a host and a numeric port; "
            "example: http://127.0.0.1:8000"
        ) from None
    return value


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "invalid backend URL" in message:
            self.exit(2, "FATAL: --base-url is not a valid URL; "
                         "example: --base-url http://127.0.0.1:8000\n")
        super().error(message)

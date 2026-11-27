from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


_GOOGLE_HOSTS = frozenset({"google.com", "www.google.com"})
_GOOGLE_TRACKING_PARAMETERS = frozenset({"srsltid"})


class InvalidDestinationUrl(ValueError):
    pass


def clean_destination_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise InvalidDestinationUrl("destination URL is malformed") from error
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise InvalidDestinationUrl("destination URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidDestinationUrl("destination URL must not contain credentials")

    hostname = hostname.lower()
    # The page script resolves Google's redirect wrappers, so any Google URL
    # here, wrapper or navigation, means the observation broke its contract.
    if hostname in _GOOGLE_HOSTS:
        raise InvalidDestinationUrl("Google navigation is not an external destination")

    # Split the raw query on `&` only, drop tracking parameters, and keep the
    # rest byte for byte: decoding and re-encoding it corrupted non-UTF-8
    # values such as GBK and rewrote `?flag`, `;` and unescaped `/`.
    retained_query = "&".join(
        parameter
        for parameter in parsed.query.split("&")
        if parameter.partition("=")[0].lower() not in _GOOGLE_TRACKING_PARAMETERS
    )
    fragment = parsed.fragment.split(":~:", 1)[0]
    default_port = (parsed.scheme.lower() == "https" and port == 443) or (
        parsed.scheme.lower() == "http" and port == 80
    )
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if port is None or default_port else f"{host}:{port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            retained_query,
            fragment,
        )
    )

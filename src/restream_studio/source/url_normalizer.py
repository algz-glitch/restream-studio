import re
from ipaddress import ip_address
from urllib.parse import parse_qsl, unquote, urlsplit

_ALLOWED_HOSTS = {"live.douyin.com", "v.douyin.com"}
_LIVE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHORT_CODE_RE = re.compile(r"^[A-Za-z0-9]+$")
_DESTINATION_QUERY_KEYS = {
    "callback",
    "continue",
    "dest",
    "destination",
    "next",
    "redirect",
    "redirect_uri",
    "return",
    "return_to",
    "target",
    "url",
}
_DESTINATION_KEY_PARTS = ("redirect", "target", "url", "destination")
_HOST_LIKE_VALUE_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}(?:[/:].*)?$"
)


class DouyinUrlValidationError(ValueError):
    """Raised when a Douyin URL is outside the supported safe URL forms."""


def normalize_douyin_url(raw: str) -> str:
    """Validate and canonicalize a supported Douyin URL."""
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise DouyinUrlValidationError("URL must not contain control characters")

    candidate = raw.strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise DouyinUrlValidationError("URL structure is invalid") from exc

    if parsed.scheme.lower() != "https":
        raise DouyinUrlValidationError("URL must use HTTPS")
    if parsed.password is not None:
        raise DouyinUrlValidationError("URL must not contain a password")
    if parsed.username is not None:
        raise DouyinUrlValidationError("URL must not contain userinfo")

    try:
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise DouyinUrlValidationError("URL port is invalid") from exc
    if port not in (None, 443):
        raise DouyinUrlValidationError("URL must not use a non-default port")
    if hostname is None or hostname.lower() not in _ALLOWED_HOSTS:
        raise DouyinUrlValidationError("URL host is not supported")
    host = hostname.lower()

    if parsed.fragment:
        raise DouyinUrlValidationError("URL fragment is not supported")

    decoded_path = _fully_unquote(parsed.path)
    if any(ord(character) < 32 or ord(character) == 127 for character in decoded_path):
        raise DouyinUrlValidationError("URL path must not contain control characters")
    if "\\" in decoded_path:
        raise DouyinUrlValidationError("URL path must not contain a backslash")
    if decoded_path.count("/") > parsed.path.count("/"):
        raise DouyinUrlValidationError("URL path must not contain a decoded slash")
    if decoded_path.count("/") > (2 if decoded_path.endswith("/") else 1):
        raise DouyinUrlValidationError("URL path must contain exactly one segment")

    segment = decoded_path.removeprefix("/").removesuffix("/")
    if segment in {".", ".."}:
        raise DouyinUrlValidationError("URL path must not contain dot segments")
    if "/" in segment:
        raise DouyinUrlValidationError("URL path must not contain a decoded slash")
    if not segment:
        label = "room" if host == "live.douyin.com" else "code"
        raise DouyinUrlValidationError(f"URL {label} segment must not be empty")
    segment_pattern = _LIVE_SEGMENT_RE if host == "live.douyin.com" else _SHORT_CODE_RE
    if segment_pattern.fullmatch(segment) is None:
        raise DouyinUrlValidationError("URL segment contains unsupported characters")

    _validate_query(parsed.query)
    suffix = "/" if host == "v.douyin.com" else ""
    return f"https://{host}/{segment}{suffix}"


def _fully_unquote(value: str) -> str:
    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            return decoded
        decoded = next_value
    return decoded


def _validate_query(query: str) -> None:
    if not query:
        return
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError as exc:
        raise DouyinUrlValidationError("URL query is malformed") from exc

    for key, value in pairs:
        normalized_key = key.casefold().replace("-", "_")
        if normalized_key in _DESTINATION_QUERY_KEYS or any(
            part in normalized_key for part in _DESTINATION_KEY_PARTS
        ):
            raise DouyinUrlValidationError("URL query contains a destination parameter")
        decoded_value = _fully_unquote(value).strip()
        lowered_value = decoded_value.casefold()
        if _is_destination_value(decoded_value, lowered_value):
            raise DouyinUrlValidationError("URL query contains an external target")
        if any(ord(character) < 32 or ord(character) == 127 for character in decoded_value):
            raise DouyinUrlValidationError("URL query contains control characters")


def _is_destination_value(value: str, lowered_value: str) -> bool:
    if lowered_value.startswith(("http://", "https://", "//")):
        return True
    if _HOST_LIKE_VALUE_RE.fullmatch(value):
        return True
    try:
        ip_address(value)
    except ValueError:
        return False
    return True

"""Recipe cover images: DuckDuckGo search and upload to Mealie."""
from __future__ import annotations

import ipaddress
import os
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests

from mealie_client import MealieClient
from .errors import ImageDownloadError, ImageSearchError

MIN_AUTO_IMAGE_BYTES = 20_000  # auto-pick skips tiny/placeholder images
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
DOWNLOAD_SECONDS = 30  # for the whole download, redirects included
_CONNECT_SECONDS, _READ_SECONDS = 5, 10
_DOWNLOAD_HEADERS = {"User-Agent": "Mozilla/5.0"}


def _ingredient_text(ingredient) -> str:
    if isinstance(ingredient, dict):
        food = ingredient.get("food")
        return food.get("name", "") if isinstance(food, dict) else str(food or "")
    return str(ingredient)


def image_query(recipe_name: str, ingredients: list | None = None) -> str:
    """Search phrase: the recipe name plus a keyword from each of the first two ingredients."""
    hints = []
    for ingredient in (ingredients or [])[:2]:
        words = _ingredient_text(ingredient).split(",")[0].split()
        if words:
            hints.append(words[-1])
    hint = " " + " ".join(hints) if hints else ""
    return f"{recipe_name}{hint} food recipe"


def search_image_candidates(
    recipe_name: str,
    ingredients: list | None = None,
    max_results: int = 10,
) -> list[dict]:
    """Search DuckDuckGo for cover images without downloading or uploading anything.

    `ingredients` may be strings or Mealie ingredient dicts. Returns
    [{url, thumbnail, title, source, width, height}]; thumbnail may be None.
    Raises ImageSearchError if the search itself fails.
    """
    from ddgs import DDGS

    try:
        results = DDGS().images(image_query(recipe_name, ingredients), max_results=max_results)
    except Exception as e:
        raise ImageSearchError(f"Image search failed: {e}") from e

    candidates = []
    for result in results or []:
        url = result.get("image") or ""
        if not url.startswith("http"):
            continue
        candidates.append({
            "url": url,
            "thumbnail": result.get("thumbnail") or None,
            "title": result.get("title") or "",
            "source": result.get("url") or result.get("source") or "",
            "width": result.get("width"),
            "height": result.get("height"),
        })
    return candidates


def _private_hosts_allowed() -> bool:
    """Development/test escape hatch only: lets images be fetched from
    loopback and private addresses (the dev fake Mealie runs on 127.0.0.1)."""
    return os.environ.get("ALLOW_PRIVATE_IMAGE_HOSTS", "").strip().lower() in ("1", "true", "yes")


def _host_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as e:
        raise ImageDownloadError(f"The image host {host} could not be found.") from e
    return list(dict.fromkeys(info[4][0] for info in infos))


def _is_internal(address: str) -> bool:
    """True for anything that is not an ordinary public internet address."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])  # drop an IPv6 zone id
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if mapped is not None:
            return _is_internal(str(mapped))
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
            or ip.is_unspecified or ip.is_reserved or not ip.is_global)


def _check_destination(url: str) -> None:
    """Refuse anything but http(s) to a public address. This app sits inside a
    home network; an image address must not be a way to make it fetch from
    that network (or from the machine itself) on someone's behalf."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ImageDownloadError("That is not a valid web address.") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ImageDownloadError("Image addresses must start with http:// or https://.")
    if _private_hosts_allowed():
        return
    port = port or (443 if parts.scheme.lower() == "https" else 80)
    if any(_is_internal(address) for address in _host_addresses(host, port)):
        raise ImageDownloadError(
            f"{host} is an address on a private or local network, so the image was not fetched. "
            "Use an image that is on the public internet.")


def _peer_is_internal(response) -> bool:
    """Best effort: the address actually connected to (guards against a host
    name that resolves differently between the check and the request). Not
    meaningful - and skipped - when the request went through a proxy."""
    try:
        if requests.utils.get_environ_proxies(response.url):
            return False
        sock = response.raw.connection.sock
        return _is_internal(sock.getpeername()[0])
    except Exception:
        return False


def _download_image(url: str, default_content_type: str = "") -> tuple[bytes, str]:
    """Fetch an image as (bytes, content type). Raises ImageDownloadError with
    a message that is safe to show: only public http(s) addresses, at most
    MAX_REDIRECTS redirects (each re-checked), MAX_IMAGE_BYTES and
    DOWNLOAD_SECONDS in total."""
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    for _ in range(MAX_REDIRECTS + 1):
        _check_destination(url)
        try:
            response = requests.get(url, allow_redirects=False, stream=True, headers=_DOWNLOAD_HEADERS,
                                    timeout=(_CONNECT_SECONDS, _READ_SECONDS))
        except Exception as e:
            raise ImageDownloadError("The image could not be downloaded: the site did not answer.") from e
        try:
            if not _private_hosts_allowed() and _peer_is_internal(response):
                raise ImageDownloadError("That address leads to a private or local network, so the image was not fetched.")
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise ImageDownloadError("The image could not be downloaded: the site sent an empty redirect.")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise ImageDownloadError(f"The image could not be downloaded: the site answered {response.status_code}.")
            content_type = response.headers.get("content-type") or default_content_type
            if not content_type.startswith("image/"):
                raise ImageDownloadError("That address is not an image.")
            too_big = ImageDownloadError(f"That image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                raise too_big
            body = bytearray()
            try:
                for chunk in response.iter_content(64 * 1024):
                    body += chunk
                    if len(body) > MAX_IMAGE_BYTES:
                        raise too_big
                    if time.monotonic() > deadline:
                        raise ImageDownloadError("The image took too long to download.")
            except ImageDownloadError:
                raise
            except Exception as e:
                raise ImageDownloadError("The image could not be downloaded: the connection was interrupted.") from e
            return bytes(body), content_type
        finally:
            response.close()
        if time.monotonic() > deadline:  # pragma: no cover - reached only via `continue`
            break
    raise ImageDownloadError("The image could not be downloaded: too many redirects.")


def set_image_from_url(client: MealieClient, slug: str, url: str) -> bool:
    """Download `url` and make it the recipe's cover image.

    Raises ImageDownloadError (with a message for the user) if the address is
    refused or the download fails; returns False if Mealie refuses the upload.
    """
    image = _download_image(url, default_content_type="image/jpeg")
    if image is None:
        return False
    try:
        client.upload_recipe_image(slug, *image)
    except Exception:
        return False
    return True


def auto_set_image(
    client: MealieClient,
    slug: str,
    recipe_name: str,
    ingredients: list | None = None,
) -> bool:
    """Search and upload the first usable result (a real image of at least 20 KB).

    Returns False if the search fails or nothing usable is found.
    """
    try:
        candidates = search_image_candidates(recipe_name, ingredients)
    except Exception:
        return False
    for candidate in candidates:
        try:
            image = _download_image(candidate["url"])
        except ImageDownloadError:
            continue
        if image is None or len(image[0]) < MIN_AUTO_IMAGE_BYTES:
            continue
        try:
            client.upload_recipe_image(slug, *image)
        except Exception:
            continue
        return True
    return False

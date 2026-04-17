from __future__ import annotations

import argparse
import hashlib
import logging
import mimetypes
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import urllib3
import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OfflinePageCloner/1.0"
)

TRACKING_PATTERNS = (
    "google-analytics",
    "googletagmanager",
    "gtag/js",
    "doubleclick.net",
    "connect.facebook.net",
    "facebook.com/tr",
    "hotjar",
    "segment.com",
    "mixpanel",
    "clarity.ms",
    "fullstory",
    "matomo",
)

ONLINE_ONLY_DOMAINS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
)

# Lazy-load attribute names used by common JS libraries
LAZY_SRC_ATTRS = (
    "data-src",
    "data-lazy",
    "data-lazy-src",
    "data-original",
    "data-bg",
    "data-background",
    "data-srcset",
)

CSS_URL_PATTERN = re.compile(r"url\((?P<quote>['\"]?)(?P<url>.*?)(?P=quote)\)", re.IGNORECASE)
CSS_IMPORT_PATTERN = re.compile(
    r"@import\s+(?:url\((?P<quote1>['\"]?)(?P<url1>.*?)(?P=quote1)\)|(?P<quote2>['\"])(?P<url2>.*?)(?P=quote2))",
    re.IGNORECASE,
)


@dataclass
class AssetRecord:
    source_url: str
    local_name: str
    content_type: str = ""


class WebpageCloner:
    def __init__(
        self,
        url: str,
        output_dir: str | Path,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = 20,
    ) -> None:
        self.url = self._normalize_url(url)
        self.output_dir = Path(output_dir).resolve()
        self.assets_dir = self.output_dir / "assets"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.asset_map: Dict[str, AssetRecord] = {}
        self.local_name_map: Dict[str, str] = {}
        self.processed_css_assets: Set[str] = set()
        # Maps failed/CDN-only URL -> human-readable reason
        self.cdn_log: Dict[str, str] = {}

        # Retry adapter: 3 retries with backoff for transient server errors
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def clone(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)

        response = self._request(self.url)
        soup = BeautifulSoup(response.text, "html.parser")

        self._remove_tracking_scripts(soup)
        self._rewrite_stylesheets(soup)
        self._rewrite_scripts(soup)
        self._rewrite_images(soup)
        self._rewrite_lazy_images(soup)
        self._rewrite_media_sources(soup)
        self._rewrite_meta_assets(soup)
        self._rewrite_style_tags(soup)
        self._rewrite_inline_styles(soup)
        self._rewrite_iframes(soup)
        self._deactivate_links_and_forms(soup)
        self._rewrite_base_tag(soup)

        self._write_cdn_log()
        output_file = self.output_dir / "index.html"
        output_file.write_text(str(soup), encoding="utf-8")
        return output_file

    def _rewrite_stylesheets(self, soup: BeautifulSoup) -> None:
        for link in soup.find_all("link"):
            href = link.get("href")
            rel = {value.lower() for value in link.get("rel", [])}
            if not href:
                continue

            absolute_url = self._absolute_url(href)
            if not absolute_url:
                continue

            # Only localize .css files, fallback to CDN if download fails
            if "stylesheet" in rel or self._looks_like_stylesheet(absolute_url):
                # Google Fonts and similar: if download fails, keep CDN link
                if any(domain in absolute_url for domain in ONLINE_ONLY_DOMAINS):
                    link["href"] = absolute_url
                    self._log_cdn_url(absolute_url, reason="[CDN-only] Google Fonts or similar")
                    continue
                record = self._download_asset(absolute_url, preferred_ext=".css")
                if not record:
                    link["href"] = absolute_url
                    self._log_cdn_url(absolute_url, reason="[Failed Download] CSS fallback")
                    continue
                self._localize_css_asset(record, absolute_url)
                link["href"] = f"assets/{record.local_name}"
            elif rel & {"icon", "shortcut icon", "apple-touch-icon", "mask-icon"}:
                record = self._download_asset(absolute_url)
                if record:
                    link["href"] = f"assets/{record.local_name}"
                else:
                    link["href"] = absolute_url
                    self._log_cdn_url(absolute_url, reason="[Failed Download] Icon fallback")
            elif rel & {"preload", "prefetch", "modulepreload"}:
                record = self._download_asset(absolute_url)
                if record:
                    link["href"] = f"assets/{record.local_name}"
                else:
                    link["href"] = absolute_url
                    self._log_cdn_url(absolute_url, reason="[Failed Download] Preload fallback")
            elif self._looks_like_asset_link(absolute_url):
                record = self._download_asset(absolute_url)
                if record:
                    link["href"] = f"assets/{record.local_name}"
                else:
                    link["href"] = absolute_url
                    self._log_cdn_url(absolute_url, reason="[Failed Download] Asset fallback")

    def _rewrite_scripts(self, soup: BeautifulSoup) -> None:
        for script in list(soup.find_all("script")):
            # Only remove if it's a tracker (handled in _remove_tracking_scripts)
            if self._is_tracking_script(script):
                continue

            src = script.get("src")
            if not src:
                continue

            absolute_url = self._absolute_url(src)
            if not absolute_url:
                continue

            record = self._download_asset(absolute_url, preferred_ext=".js")
            if record:
                script["src"] = f"assets/{record.local_name}"
            else:
                script["src"] = absolute_url
                self._log_cdn_url(absolute_url, reason="[Failed Download] JS fallback")

    def _rewrite_images(self, soup: BeautifulSoup) -> None:
        import json
        for img in soup.find_all("img"):
            # Find the real image URL from data-src, data-lazy, srcset, etc.
            real_url = None
            # 1. data-srcs (JSON)
            data_srcs = img.get("data-srcs")
            if data_srcs:
                try:
                    srcs_dict = json.loads(data_srcs)
                    if isinstance(srcs_dict, dict):
                        for k in srcs_dict:
                            if isinstance(k, str) and k.strip():
                                real_url = k
                                break
                except Exception:
                    pass
            # 2. data-src, data-lazy, data-original, data-bg, data-background
            if not real_url:
                for attr in LAZY_SRC_ATTRS:
                    if attr == "data-srcset":
                        continue
                    val = img.get(attr)
                    if val and not val.startswith("data:"):
                        real_url = val
                        break
            # 3. srcset
            if not real_url:
                real_url = self._select_best_srcset_candidate(img.get("srcset"))
            # 4. fallback to src
            if not real_url:
                real_url = img.get("src")

            # If src is a placeholder, always prioritize real_url
            src_val = img.get("src", "")
            if (src_val.startswith("data:image/svg+xml") or src_val.startswith("data:image/gif")) and real_url:
                pass  # already handled
            elif not real_url:
                real_url = src_val

            absolute_url = self._absolute_url(real_url)
            if absolute_url:
                record = self._download_asset(absolute_url)
                if record:
                    img["src"] = f"assets/{record.local_name}"
                else:
                    img["src"] = absolute_url  # CDN fallback
            # Remove lazy attributes
            for attr in ["data-srcs", "data-src", "srcset", "data-lazy-src", "data-lazy", "data-original", "data-bg", "data-background", "data-srcset"]:
                img.attrs.pop(attr, None)

        # Remove all <noscript> tags
        for noscript in soup.find_all("noscript"):
            noscript.decompose()

    def _rewrite_media_sources(self, soup: BeautifulSoup) -> None:
        source_attrs = {
            "source": "src",
            "video": "poster",
            "audio": "src",
        }
        for tag_name, attr_name in source_attrs.items():
            for tag in soup.find_all(tag_name):
                value = tag.get(attr_name)
                absolute_url = self._absolute_url(value)
                if not absolute_url:
                    continue
                record = self._download_asset(absolute_url)
                if record:
                    tag[attr_name] = f"assets/{record.local_name}"
                else:
                    tag[attr_name] = absolute_url  # CDN fallback

        for source in soup.find_all("source"):
            srcset = source.get("srcset")
            if not srcset:
                continue
            selected_source = self._select_best_srcset_candidate(srcset)
            absolute_url = self._absolute_url(selected_source)
            if not absolute_url:
                continue
            record = self._download_asset(absolute_url)
            if record:
                source["src"] = f"assets/{record.local_name}"
                source.attrs.pop("srcset", None)
            else:
                source["src"] = absolute_url  # CDN fallback
                source.attrs.pop("srcset", None)

    def _rewrite_inline_styles(self, soup: BeautifulSoup) -> None:
        for tag in soup.find_all(style=True):
            tag["style"] = self._rewrite_css_urls(tag["style"], base_url=self.url, html_context=True)

    def _rewrite_style_tags(self, soup: BeautifulSoup) -> None:
        for style_tag in soup.find_all("style"):
            css_text = style_tag.string or style_tag.get_text()
            if not css_text:
                continue
            style_tag.string = self._rewrite_css_text(css_text, base_url=self.url, html_context=True)

    def _rewrite_lazy_images(self, soup: BeautifulSoup) -> None:
        # This is now handled in _rewrite_images, so this is a no-op for compatibility
        pass

    def _rewrite_meta_assets(self, soup: BeautifulSoup) -> None:
        """Localize Open Graph / Twitter Card image URLs inside <meta> tags."""
        for meta in soup.find_all("meta"):
            if meta.get("property", "") in ("og:image", "og:image:secure_url", "twitter:image"):
                content = meta.get("content", "")
                absolute_url = self._absolute_url(content)
                if not absolute_url:
                    continue
                record = self._download_asset(absolute_url)
                if record:
                    meta["content"] = f"assets/{record.local_name}"

    def _rewrite_iframes(self, soup: BeautifulSoup) -> None:
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src")
            if src and not src.startswith(("http://", "https://", "//")):
                iframe["src"] = self._absolute_url(src)
            # Optionally handle srcdoc as well (leave as is or convert if needed)

    def _deactivate_links_and_forms(self, soup: BeautifulSoup) -> None:
        # Set <a> href to 'javascript:void(0)' only if it does not already have an event handler
        for anchor in soup.find_all("a"):
            # If the anchor has any JS event attribute, do NOT overwrite href
            has_event = any(attr for attr in anchor.attrs if attr.startswith("on"))
            if not has_event:
                anchor["href"] = "javascript:void(0)"
        # Prevent all form submissions, but do not remove inline JS
        for form in soup.find_all("form"):
            form["action"] = "javascript:void(0)"
            if "onsubmit" not in form.attrs:
                form["onsubmit"] = "return false;"

    def _rewrite_base_tag(self, soup: BeautifulSoup) -> None:
        for base_tag in soup.find_all("base"):
            base_tag.decompose()

    def _remove_tracking_scripts(self, soup: BeautifulSoup) -> None:
        # Only remove scripts that are clear external trackers (very conservative)
        for script in list(soup.find_all("script")):
            if self._is_tracking_script(script):
                script.decompose()

    def _is_tracking_script(self, script: Tag) -> bool:
        # Only match clear external trackers, never remove internal libraries
        src = (script.get("src") or "").lower()
        inline_text = script.get_text(" ", strip=True).lower()
        # Only match if src is an external tracker
        tracker_domains = [
            "google-analytics.com", "googletagmanager.com", "gtag/js", "doubleclick.net",
            "connect.facebook.net", "facebook.com/tr", "hotjar.com", "segment.com",
            "mixpanel.com", "clarity.ms", "fullstory.com", "matomo.js", "pixel.js", "pixel.track"
        ]
        # Only match if src is present and matches a tracker domain
        if src and any(domain in src for domain in tracker_domains):
            return True
        # Optionally, match inline scripts that are obvious trackers (rare)
        if not src and ("analytics" in inline_text or "gtag(" in inline_text or "fbq(" in inline_text):
            return True
        return False

    def _rewrite_css_text(self, css_text: str, base_url: str, html_context: bool = False) -> str:
        rewritten = self._rewrite_css_imports(css_text, base_url=base_url, html_context=html_context)
        return self._rewrite_css_urls(rewritten, base_url=base_url, html_context=html_context)

    def _rewrite_css_imports(self, css_text: str, base_url: str, html_context: bool = False) -> str:
        def replace(match: re.Match[str]) -> str:
            raw_url = (match.group("url1") or match.group("url2") or "").strip()
            if not raw_url or raw_url.startswith("data:"):
                return match.group(0)

            absolute_url = self._absolute_url(raw_url, base_url=base_url)
            if not absolute_url:
                return match.group(0)

            record = self._download_asset(absolute_url, preferred_ext=".css")
            if not record:
                self._log_cdn_url(absolute_url, reason="[Failed Download] CSS import fallback")
                return f"@import url('{absolute_url}')"  # CDN fallback

            self._localize_css_asset(record, absolute_url)
            rewritten_url = f"assets/{record.local_name}" if html_context else record.local_name
            return f"@import url('{rewritten_url}')"

        return CSS_IMPORT_PATTERN.sub(replace, css_text)

    def _rewrite_css_urls(self, css_text: str, base_url: str, html_context: bool = False) -> str:
        def replace(match: re.Match[str]) -> str:
            raw_url = match.group("url").strip()
            if not raw_url or raw_url.startswith("data:"):
                return match.group(0)

            absolute_url = self._absolute_url(raw_url, base_url=base_url)
            if not absolute_url:
                return match.group(0)

            # Font file detection
            font_exts = (".woff2", ".woff", ".ttf", ".otf", ".eot")
            preferred_ext = None
            for ext in font_exts:
                if raw_url.lower().endswith(ext):
                    preferred_ext = ext
                    break
            record = self._download_asset(absolute_url, preferred_ext=preferred_ext)
            if not record:
                self._log_cdn_url(absolute_url, reason="[Failed Download] CSS url() fallback")
                return f"url('{absolute_url}')"  # CDN fallback

            if preferred_ext and record.local_name.lower().endswith(preferred_ext):
                # Ensure fonts go to assets/fonts/
                rewritten = f"assets/{record.local_name}" if html_context else record.local_name
                return f"url('{rewritten}')"
            if record.local_name.lower().endswith(".css"):
                self._localize_css_asset(record, absolute_url)
            rewritten = f"assets/{record.local_name}" if html_context else record.local_name
            return f"url('{rewritten}')"

        return CSS_URL_PATTERN.sub(replace, css_text)

    def _localize_css_asset(self, record: AssetRecord, source_url: str) -> None:
        if record.local_name in self.processed_css_assets:
            return

        self.processed_css_assets.add(record.local_name)
        css_path = self.assets_dir / record.local_name
        css_text = css_path.read_text(encoding="utf-8", errors="ignore")
        rewritten_css = self._rewrite_css_text(css_text, base_url=source_url)
        css_path.write_text(rewritten_css, encoding="utf-8")

    def _download_asset(self, asset_url: str, preferred_ext: Optional[str] = None) -> Optional[AssetRecord]:
        # Use the full URL (with query strings) as the cache key so versioned
        # assets are not confused with one another.
        existing = self.asset_map.get(asset_url)
        if existing:
            return existing

        # Always load certain domains from the internet (e.g. Google Fonts).
        if self._is_online_only(asset_url):
            logging.info("Keeping online (CDN-only domain): %s", asset_url)
            self._log_cdn_url(asset_url, reason="CDN-only domain")
            return None

        try:
            # Use the full URL (including query strings) to avoid 400 errors on
            # versioned or signed asset URLs.
            response = self._request(asset_url, stream=True)
        except requests.exceptions.SSLError:
            # SSL handshake failure: retry once without certificate verification
            logging.warning("SSL error on %s — retrying without verification", asset_url)
            try:
                response = self.session.get(asset_url, timeout=self.timeout, stream=True, verify=False)
                response.raise_for_status()
            except requests.RequestException as exc:
                logging.warning("Download failed after SSL fallback (CDN fallback): %s — %s", asset_url, exc)
                self._log_cdn_url(asset_url, reason=f"SSL + fallback error: {exc}")
                return None
        except requests.exceptions.Timeout:
            logging.warning("Timeout downloading %s (CDN fallback)", asset_url)
            self._log_cdn_url(asset_url, reason="timeout")
            return None
        except requests.exceptions.ConnectionError as exc:
            logging.warning("Connection error for %s (CDN fallback): %s", asset_url, exc)
            self._log_cdn_url(asset_url, reason=f"connection error: {exc}")
            return None
        except requests.RequestException as exc:
            logging.warning("HTTP error for %s (CDN fallback): %s", asset_url, exc)
            self._log_cdn_url(asset_url, reason=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — intentional catch-all to keep script alive
            logging.warning("Unexpected error downloading %s (CDN fallback): %s", asset_url, exc)
            self._log_cdn_url(asset_url, reason=f"unexpected: {exc}")
            return None

        # Sanitize the filename: strip query strings, keep only path + extension.
        local_name = self._build_local_name(asset_url, response.headers.get("Content-Type", ""), preferred_ext)
        # Ensure subfolder exists
        subfolder = local_name.split("/", 1)[0]
        target_dir = self.assets_dir / subfolder
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.assets_dir / local_name
        with target_path.open("wb") as file_handle:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file_handle.write(chunk)

        record = AssetRecord(
            source_url=asset_url,
            local_name=local_name,
            content_type=response.headers.get("Content-Type", ""),
        )
        self.asset_map[asset_url] = record
        return record

    def _is_online_only(self, url: str) -> bool:
        netloc = urlsplit(url).netloc.lower()
        return any(domain in netloc for domain in ONLINE_ONLY_DOMAINS)

    def _log_cdn_url(self, url: str, reason: str = "download failed") -> None:
        # First writer wins; keeps the most specific reason
        if url not in self.cdn_log:
            self.cdn_log[url] = reason

    def _write_cdn_log(self) -> None:
        log_path = self.output_dir / "cdn_load.txt"
        with log_path.open("w", encoding="utf-8") as fh:
            fh.write("# Assets loaded from the internet (CDN fallback)\n")
            fh.write(f"# Generated by WebpageCloner — {len(self.cdn_log)} URL(s)\n\n")
            for url, reason in sorted(self.cdn_log.items()):
                fh.write(f"{url}\n    reason: {reason}\n\n")
        if self.cdn_log:
            logging.info(
                "CDN fallback log written to %s (%d URL(s) load from internet)",
                log_path,
                len(self.cdn_log),
            )

    def _build_local_name(self, asset_url: str, content_type: str, preferred_ext: Optional[str]) -> str:
        parsed = urlsplit(asset_url)
        original_name = Path(posixpath.basename(parsed.path)).name or "asset"
        stem = Path(original_name).stem or "asset"
        extension = Path(original_name).suffix.lower()
        if not extension:
            guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) if content_type else None
            extension = guessed or preferred_ext or ""
        elif preferred_ext and extension.lower() != preferred_ext.lower() and content_type.startswith("text/"):
            extension = preferred_ext
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-") or "asset"
        digest = hashlib.sha1(asset_url.encode("utf-8")).hexdigest()[:10]
        # Determine subfolder by extension/content type
        ext = extension.lower()
        if ext in [".js"]:
            subfolder = "js"
        elif ext in [".css"]:
            subfolder = "css"
        elif ext in [".woff", ".woff2", ".ttf", ".otf", ".eot"] or "font" in content_type:
            subfolder = "fonts"
        elif ext in [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico"] or "image" in content_type:
            subfolder = "img"
        else:
            # fallback: try to guess from content_type
            if "javascript" in content_type:
                subfolder = "js"
            elif "css" in content_type:
                subfolder = "css"
            elif "font" in content_type:
                subfolder = "fonts"
            elif "image" in content_type:
                subfolder = "img"
            else:
                subfolder = "misc"
        candidate = f"{subfolder}/{safe_stem}-{digest}{extension}"
        existing_url = self.local_name_map.get(candidate)
        if existing_url and existing_url != asset_url:
            candidate = f"{subfolder}/{safe_stem}-{digest}-dup{extension}"
        self.local_name_map[candidate] = asset_url
        return candidate

    def _select_best_srcset_candidate(self, srcset: Optional[str]) -> Optional[str]:
        if not srcset:
            return None

        best_candidate: Optional[Tuple[float, str]] = None
        for item in srcset.split(","):
            candidate = item.strip()
            if not candidate:
                continue

            parts = candidate.split()
            url = parts[0]
            descriptor = parts[1] if len(parts) > 1 else "1x"
            score = self._descriptor_score(descriptor)
            if best_candidate is None or score > best_candidate[0]:
                best_candidate = (score, url)

        return best_candidate[1] if best_candidate else None

    def _descriptor_score(self, descriptor: str) -> float:
        normalized = descriptor.strip().lower()
        if normalized.endswith("w"):
            try:
                return float(normalized[:-1])
            except ValueError:
                return 0.0
        if normalized.endswith("x"):
            try:
                return float(normalized[:-1]) * 1000
            except ValueError:
                return 0.0
        return 0.0

    def _looks_like_stylesheet(self, url: str) -> bool:
        return urlsplit(url).path.lower().endswith(".css")

    def _looks_like_asset_link(self, url: str) -> bool:
        asset_extensions = (".ico", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".woff", ".woff2", ".ttf")
        return urlsplit(url).path.lower().endswith(asset_extensions)

    def _absolute_url(self, value: Optional[str], base_url: Optional[str] = None) -> Optional[str]:
        if not value:
            return None

        value = value.strip()
        if not value or value.startswith(("data:", "javascript:", "mailto:", "tel:")):
            return None

        base = base_url or self.url
        # Keep query strings intact so versioned/signed URLs work correctly.
        return urljoin(base, value)

    def _normalize_url(self, value: str) -> str:
        parsed = urlparse(value.strip())
        if not parsed.scheme:
            return f"https://{value.strip()}"
        return value.strip()

    def _normalize_asset_url(self, value: str) -> str:
        parts = urlsplit(value)
        cleaned_path = parts.path or "/"
        return urlunsplit((parts.scheme, parts.netloc, cleaned_path, "", ""))

    def _request(self, url: str, stream: bool = False) -> requests.Response:
        response = self.session.get(url, timeout=self.timeout, stream=stream)
        response.raise_for_status()
        return response


# ---------------------------------------------------------------------------
# Root folder where all cloned projects are stored
# ---------------------------------------------------------------------------
CLONED_ROOT = Path("cloned")


def _try_reach(url: str, timeout: int = 10) -> bool:
    """Return True if a HEAD (or GET) request to *url* succeeds."""
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        return resp.status_code < 500
    except requests.RequestException:
        return False


def _resolve_url(raw: str, timeout: int = 10) -> Optional[str]:
    """
    Given raw user input (with or without a scheme) return a reachable URL.

    Resolution order:
      1. If scheme provided → use as-is (no extra probe).
      2. No scheme → try https:// first; if SSL/timeout/connection error → try http://.
      3. Return None if both fail.
    """
    raw = raw.strip()
    if not raw:
        return None

    has_scheme = raw.startswith(("http://", "https://"))

    if has_scheme:
        parsed = urlsplit(raw)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return raw
        return None

    # No scheme supplied — probe https first, then http
    for scheme in ("https", "http"):
        candidate = f"{scheme}://{raw}"
        parsed = urlsplit(candidate)
        if not parsed.netloc:
            continue
        print(f"  [~] Trying {candidate} …", end=" ", flush=True)
        if _try_reach(candidate, timeout=timeout):
            print("OK")
            return candidate
        print("failed")

    return None


def _prompt_url(timeout: int = 10) -> str:
    """Interactively prompt for a URL with smart auto-correction and retry."""
    while True:
        raw = input("\n  Enter the Website URL: ").strip()
        if not raw:
            print("  [!] URL cannot be empty. Please try again.")
            continue

        url = _resolve_url(raw, timeout=timeout)
        if url:
            return url

        print(
            "  [!] Could not reach the URL via https:// or http://.\n"
            "      Please check the address and try again."
        )


def _sanitize_project_name(name: str) -> str:
    """
    Convert a raw project name into a safe relative path.

    Rules:
      - Spaces → hyphens
      - Backslashes → forward slashes (treat as path separators)
      - Characters unsafe on Windows/Linux/macOS (except / and -) → hyphens
      - Collapse repeated slashes / leading-trailing slashes
    """
    # Normalize backslashes to forward slashes first
    name = name.replace("\\", "/")
    # Spaces → hyphens
    name = name.replace(" ", "-")
    # Sanitize each path segment individually
    segments = name.split("/")
    clean_segments: List[str] = []
    for seg in segments:
        seg = re.sub(r'[<>:"|?*]+', "-", seg).strip(". -")
        if seg:
            clean_segments.append(seg)
    return "/".join(clean_segments)


def _prompt_project_name() -> str:
    """Interactively prompt for a project/folder name with sanitization."""
    while True:
        name = input("  Enter Project Name (folder inside cloned/): ").strip()
        if not name:
            print("  [!] Project name cannot be empty. Please try again.\n")
            continue

        safe = _sanitize_project_name(name)
        if not safe:
            print("  [!] Project name contains only invalid characters. Please try again.\n")
            continue

        if safe != name:
            print(f"  [~] Project name adjusted to: {safe!r}")
        return safe


def _print_banner() -> None:
    print("=" * 60)
    print("        WebpageCloner — Offline Page Downloader")
    print("=" * 60)
    print()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone a webpage into an offline-ready template with localized assets."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="The page URL to clone (optional; prompted if omitted).",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help=(
            "Output sub-folder name inside cloned/ "
            "(optional; prompted if omitted). Supports nested paths, e.g. my_site/v1."
        ),
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="Custom User-Agent header for all requests.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP timeout in seconds.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)

    _print_banner()

    # --- Resolve URL ---
    if args.url:
        url = args.url
        print(f"  URL        : {url}")
    else:
        url = _prompt_url(timeout=args.timeout)

    # --- Resolve output directory (always inside cloned/) ---
    if args.output_dir:
        project_rel = _sanitize_project_name(args.output_dir)
        if not project_rel:
            print("  [!] Provided --output-dir is invalid after sanitization. Using 'project'.")
            project_rel = "project"
    else:
        project_rel = _prompt_project_name()

    output_dir = CLONED_ROOT / project_rel

    print()
    print(f"  Cloning  : {url}")
    print(f"  Output   : {output_dir.resolve()}")
    print("-" * 60)

    cloner = WebpageCloner(
        url=url,
        output_dir=output_dir,
        user_agent=args.user_agent,
        timeout=args.timeout,
    )
    output_file = cloner.clone()
    print()
    print("=" * 60)
    logging.info("Offline page written to %s", output_file)
    print("  cdn_load.txt saved to: %s", output_dir / "cdn_load.txt")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
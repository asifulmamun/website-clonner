from __future__ import annotations

import argparse
import hashlib
import logging
import mimetypes
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag


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

    def clone(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)

        response = self._request(self.url)
        soup = BeautifulSoup(response.text, "html.parser")

        self._remove_tracking_scripts(soup)
        self._rewrite_stylesheets(soup)
        self._rewrite_scripts(soup)
        self._rewrite_images(soup)
        self._rewrite_media_sources(soup)
        self._rewrite_style_tags(soup)
        self._rewrite_inline_styles(soup)
        self._rewrite_iframes(soup)
        self._deactivate_links_and_forms(soup)
        self._rewrite_base_tag(soup)

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

            if "stylesheet" in rel or self._looks_like_stylesheet(absolute_url):
                record = self._download_asset(absolute_url, preferred_ext=".css")
                if not record:
                    continue
                self._localize_css_asset(record, absolute_url)
                link["href"] = f"assets/{record.local_name}"
            elif self._looks_like_asset_link(absolute_url):
                record = self._download_asset(absolute_url)
                if record:
                    link["href"] = f"assets/{record.local_name}"

    def _rewrite_scripts(self, soup: BeautifulSoup) -> None:
        for script in list(soup.find_all("script")):
            if self._is_tracking_script(script):
                script.decompose()
                continue

            src = script.get("src")
            if not src:
                continue

            absolute_url = self._absolute_url(src)
            if not absolute_url:
                script.decompose()
                continue

            record = self._download_asset(absolute_url, preferred_ext=".js")
            if record:
                script["src"] = f"assets/{record.local_name}"
            else:
                script.decompose()

    def _rewrite_images(self, soup: BeautifulSoup) -> None:
        for img in soup.find_all("img"):
            selected_source = self._select_best_srcset_candidate(img.get("srcset")) or img.get("src")
            absolute_url = self._absolute_url(selected_source)
            if absolute_url:
                record = self._download_asset(absolute_url)
                if record:
                    img["src"] = f"assets/{record.local_name}"
            img.attrs.pop("srcset", None)

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

    def _rewrite_inline_styles(self, soup: BeautifulSoup) -> None:
        for tag in soup.find_all(style=True):
            tag["style"] = self._rewrite_css_urls(tag["style"], base_url=self.url, html_context=True)

    def _rewrite_style_tags(self, soup: BeautifulSoup) -> None:
        for style_tag in soup.find_all("style"):
            css_text = style_tag.string or style_tag.get_text()
            if not css_text:
                continue
            style_tag.string = self._rewrite_css_text(css_text, base_url=self.url, html_context=True)

    def _rewrite_iframes(self, soup: BeautifulSoup) -> None:
        for iframe in soup.find_all("iframe"):
            iframe["src"] = "about:blank"
            iframe.attrs.pop("srcdoc", None)

    def _deactivate_links_and_forms(self, soup: BeautifulSoup) -> None:
        for anchor in soup.find_all("a"):
            anchor["href"] = "javascript:void(0)"

        for form in soup.find_all("form"):
            form["action"] = "#"
            form["onsubmit"] = "return false;"

    def _rewrite_base_tag(self, soup: BeautifulSoup) -> None:
        for base_tag in soup.find_all("base"):
            base_tag.decompose()

    def _remove_tracking_scripts(self, soup: BeautifulSoup) -> None:
        for script in list(soup.find_all("script")):
            if self._is_tracking_script(script):
                script.decompose()

    def _is_tracking_script(self, script: Tag) -> bool:
        src = (script.get("src") or "").lower()
        inline_text = script.get_text(" ", strip=True).lower()
        haystack = f"{src} {inline_text}"
        return any(pattern in haystack for pattern in TRACKING_PATTERNS)

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
                return match.group(0)

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

            record = self._download_asset(absolute_url)
            if not record:
                return match.group(0)

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
        cleaned_url = self._normalize_asset_url(asset_url)
        existing = self.asset_map.get(cleaned_url)
        if existing:
            return existing

        try:
            response = self._request(cleaned_url, stream=True)
        except requests.RequestException as exc:
            logging.warning("Failed to download %s: %s", cleaned_url, exc)
            return None

        local_name = self._build_local_name(cleaned_url, response.headers.get("Content-Type", ""), preferred_ext)
        target_path = self.assets_dir / local_name
        with target_path.open("wb") as file_handle:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    file_handle.write(chunk)

        record = AssetRecord(
            source_url=cleaned_url,
            local_name=local_name,
            content_type=response.headers.get("Content-Type", ""),
        )
        self.asset_map[cleaned_url] = record
        return record

    def _build_local_name(self, asset_url: str, content_type: str, preferred_ext: Optional[str]) -> str:
        parsed = urlsplit(asset_url)
        original_name = Path(posixpath.basename(parsed.path)).name
        original_name = original_name or "asset"
        stem = Path(original_name).stem or "asset"
        extension = Path(original_name).suffix

        if not extension:
            guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) if content_type else None
            extension = guessed or preferred_ext or ""
        elif preferred_ext and extension.lower() != preferred_ext.lower() and content_type.startswith("text/"):
            extension = preferred_ext

        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-") or "asset"
        digest = hashlib.sha1(asset_url.encode("utf-8")).hexdigest()[:10]
        candidate = f"{safe_stem}-{digest}{extension}"

        existing_url = self.local_name_map.get(candidate)
        if existing_url and existing_url != asset_url:
            candidate = f"{safe_stem}-{digest}-dup{extension}"

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
        return self._normalize_asset_url(urljoin(base, value))

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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone a webpage into an offline-ready template with localized assets."
    )
    parser.add_argument("url", help="The page URL to clone.")
    parser.add_argument(
        "-o",
        "--output-dir",
        default="cloned_site",
        help="Directory where index.html and assets/ will be written.",
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

    cloner = WebpageCloner(
        url=args.url,
        output_dir=args.output_dir,
        user_agent=args.user_agent,
        timeout=args.timeout,
    )
    output_file = cloner.clone()
    logging.info("Offline page written to %s", output_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
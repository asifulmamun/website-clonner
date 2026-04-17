

"""
cloner.py – Fully Offline Web Cloner

1. Downloads ALL CSS, JS, images, and fonts to assets/ and rewrites HTML/CSS to use local paths.
2. Handles lazy-loaded images (data-srcs, data-src, src) and removes all lazy attrs.
3. Injects a MutationObserver script at end of <body> to kill all links after load.
4. Injects CSS to force image visibility.
5. Removes all CSP meta tags and <base> tags.
6. All asset URLs are converted to relative (./assets/filename).

Requirements: requests, beautifulsoup4
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)




# For image src fixup (priority: data-srcs > data-src > src)
IMG_ATTRS = ["data-srcs", "data-src", "src"]

FONT_EXTS = (".woff2", ".woff", ".ttf", ".otf", ".eot")


# Post-load link killer (MutationObserver, runs after all JS/CSS)
LINK_KILLER_JS = """
<script>
window.addEventListener('load', function() {
    function killLinks() {
        document.querySelectorAll('a').forEach(function(a) {
            a.href = 'javascript:void(0);';
            a.target = '_self';
        });
    }
    killLinks();
    new MutationObserver(killLinks).observe(document.body, {childList:true, subtree:true});
});
</script>
"""

# CSS to force-show lazy / faded images

IMAGE_FIX_CSS = """
<style>
img { opacity: 1 !important; visibility: visible !important; }
</style>
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitise_project_name(name: str) -> str:
    name = name.strip().replace(" ", "-")
    name = re.sub(r"[^\w\-]", "", name)
    return name or "project"


def _make_local_filename(url: str) -> str:
    parsed = urlparse(url)
    basename = os.path.basename(parsed.path) or "image"
    basename = re.sub(r"[?#].*", "", basename)
    if "." not in basename:
        basename += ".jpg"
    short_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{short_hash}-{basename}"


def _abs(base: str, url: str) -> str:
    """Resolve *url* against *base*; return '' for data:/javascript: URIs."""
    if not url or url.startswith(("data:", "javascript:", "#", "mailto:")):
        return ""
    if url.startswith("//"):
        url = "https:" + url
    resolved = urljoin(base, url)
    if resolved.startswith(("http://", "https://")):
        return resolved
    return ""


def _extract_first_url_from_json(raw: str) -> str:
    """Parse a JSON string (e.g. Business Insider data-srcs) and return the
    first URL value found."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if isinstance(obj, str):
        return obj
    items = obj.values() if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
    for val in items:
        if isinstance(val, str) and val.startswith(("http://", "https://", "//")):
            return ("https:" + val) if val.startswith("//") else val
    return ""


def _best_srcset_url(srcset_value: str) -> str:
    if not srcset_value:
        return ""
    candidates = []
    for part in srcset_value.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        url = tokens[0]
        width = 0
        if len(tokens) > 1:
            m = re.match(r"(\d+)", tokens[-1].lower())
            if m:
                width = int(m.group(1))
        candidates.append((url, width))
    if not candidates:
        return ""
    candidates.sort(key=lambda c: c[1], reverse=True)
    return candidates[0][0]


# ---------------------------------------------------------------------------
# Main Cloner
# ---------------------------------------------------------------------------





class PageCloner:
    def __init__(self, target_url: str, project_name: str):
        self.target_url = target_url
        self.base_url = f"{urlparse(target_url).scheme}://{urlparse(target_url).netloc}"
        self.project_name = _sanitise_project_name(project_name)
        self.project_dir = Path("cloned") / self.project_name
        self.assets_dir = self.project_dir / "assets"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._downloaded: dict[str, str] = {}  # url → local filename
        self._font_map: dict[str, str] = {}  # url → local filename

    def run(self):
        self._setup_dirs()
        print(f"[*] Fetching {self.target_url} ...")
        html = self._fetch(self.target_url)
        if html is None:
            print("[!] Failed to fetch the page. Aborting.")
            sys.exit(1)
        soup = BeautifulSoup(html, "html.parser")
        print("[*] Downloading and relinking CSS/JS ...")
        self._sync_assets(soup)
        print("[*] Downloading and relinking images ...")
        self._fix_images(soup)
        print("[*] Downloading and relinking fonts ...")
        self._fix_fonts(soup)
        print("[*] Removing CSP and <base> tags ...")
        self._remove_csp_meta(soup)
        self._remove_base_tag(soup)
        print("[*] Injecting CSS fix ...")
        self._inject_css(soup)
        print("[*] Injecting link-killer script ...")
        self._inject_link_killer(soup)
        print("[*] Writing output ...")
        self._write_html(soup)
        print(f"[+] Done!  →  {self.project_dir / 'index.html'}")

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _setup_dirs(self):
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _fetch(self, url: str) -> str | None:
        try:
            r = self.session.get(url, timeout=30, verify=False)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            print(f"    [!] Fetch error: {url} ({exc})")
            return None


    def _download_file(self, url: str) -> str | None:
        """Download *url* into assets/ as flat filename. Returns local filename or None."""
        if not url:
            return None
        local_name = os.path.basename(urlparse(url).path) or "asset"
        local_path = self.assets_dir / local_name
        if url in self._downloaded:
            return self._downloaded[url]
        try:
            r = self.session.get(url, timeout=30, stream=True, verify=False)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            self._downloaded[url] = local_name
            return local_name
        except Exception as exc:
            print(f"    [!] Download failed: {url} ({exc})")
            return None


    def _sync_assets(self, soup: BeautifulSoup):
        # Download and relink all CSS
        for link in soup.find_all("link", href=True):
            href = link["href"]
            abs_url = _abs(self.target_url, href)
            if not abs_url:
                continue
            local_name = self._download_file(abs_url)
            if local_name:
                link["href"] = f"assets/{local_name}"
                if link.get("rel") and "stylesheet" in link["rel"]:
                    css_path = self.assets_dir / local_name
                    self._rewrite_css_urls(css_path)
            else:
                link["href"] = abs_url
                self._log_cdn(abs_url, f'LINK (download failed)')
        # Download and relink all JS
        for script in soup.find_all("script", src=True):
            src = script["src"]
            abs_url = _abs(self.target_url, src)
            if not abs_url:
                continue
            local_name = self._download_file(abs_url)
            if local_name:
                script["src"] = f"assets/{local_name}"
            else:
                script["src"] = abs_url
                self._log_cdn(abs_url, 'JS (download failed)')
        # Download and relink all <source src> (video/audio)
        for source in soup.find_all("source", src=True):
            src = source["src"]
            abs_url = _abs(self.target_url, src)
            if not abs_url:
                continue
            local_name = self._download_file(abs_url)
            if local_name:
                source["src"] = f"assets/{local_name}"
            else:
                source["src"] = abs_url
                self._log_cdn(abs_url, 'SRC (download failed)')


    def _fix_images(self, soup: BeautifulSoup):
        for img in soup.find_all("img"):
            found = None
            for attr in IMG_ATTRS:
                raw = img.get(attr)
                if not raw or not isinstance(raw, str):
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                if attr == "data-srcs" and raw.startswith(("{", "[")):
                    parsed = _extract_first_url_from_json(raw)
                    if parsed:
                        found = parsed
                        break
                if raw.startswith(("http://", "https://", "//", "/")):
                    if raw.startswith("//"):
                        raw = "https:" + raw
                    elif raw.startswith("/"):
                        # Convert root-relative to absolute
                        raw = self.base_url + raw
                    found = raw
                    break
            if found:
                abs_url = _abs(self.target_url, found)
                local_name = self._download_file(abs_url)
                if local_name:
                    img["src"] = f"assets/{local_name}"
                else:
                    img["src"] = abs_url
                    self._log_cdn(abs_url, 'IMG (download failed)')
            # Remove all lazy attrs except data-src/data-srcs (for JS lazy-load)
            for attr in list(img.attrs.keys()):
                if attr == "src" or attr in ("data-src", "data-srcs"):
                    continue
                if attr in ("srcset", "data-srcset") or attr.startswith("data-"):
                    del img[attr]

    def _rewrite_css_urls(self, css_path: Path):
        if not css_path.exists():
            return
        css = css_path.read_text(encoding="utf-8", errors="ignore")
        urls = re.findall(r'url\(([^)]+)\)', css)
        for url in urls:
            url_clean = url.strip('"\'')
            if url_clean.lower().startswith('data:'):
                continue
            abs_url = _abs(self.target_url, url_clean)
            if not abs_url:
                continue
            local_name = self._download_file(abs_url)
            if local_name:
                css = css.replace(url, f'assets/{local_name}')
            else:
                self._log_cdn(abs_url, 'CSS-URL')
        css_path.write_text(css, encoding="utf-8")

    def _log_cdn(self, url: str, asset_type: str):
        log_path = self.project_dir / "cdn_load.txt"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{asset_type}] {url}\n")
    def _download_fonts_from_css(self, css_path: Path):
        if not css_path.exists():
            return
        css = css_path.read_text(encoding="utf-8", errors="ignore")
        font_urls = re.findall(r'url\(([^)]+)\)', css)
        for url in font_urls:
            url = url.strip('"\'')
            if not url.lower().endswith(FONT_EXTS):
                continue
            abs_url = _abs(self.target_url, url)
            if not abs_url:
                continue
            local_name = self._download_file(abs_url)
            if local_name:
                # Replace font URL in CSS
                css = css.replace(url, f"assets/{local_name}")
        css_path.write_text(css, encoding="utf-8")

    def _fix_fonts(self, soup: BeautifulSoup):
        # Also handle <link> tags for fonts (e.g., Google Fonts)
        for link in soup.find_all("link", href=True):
            href = link["href"]
            if any(href.lower().endswith(ext) for ext in FONT_EXTS):
                abs_url = _abs(self.target_url, href)
                if not abs_url:
                    continue
                local_name = self._download_file(abs_url)
                if local_name:
                    link["href"] = f"assets/{local_name}"

    # ------------------------------------------------------------------
    # Path Correction – make every relative URL absolute
    # ------------------------------------------------------------------

    def _absolutise_paths(self, soup: BeautifulSoup):
        # <link href="...">
        for tag in soup.find_all("link", href=True):
            resolved = _abs(self.target_url, tag["href"])
            if resolved:
                tag["href"] = resolved
        # <script src="...">
        for tag in soup.find_all("script", src=True):
            resolved = _abs(self.target_url, tag["src"])
            if resolved:
                tag["src"] = resolved
        # <video>, <audio>, <source>, <iframe>, <embed>, <object>
        for tag_name in ("video", "audio", "source", "iframe", "embed", "object"):
            for tag in soup.find_all(tag_name):
                for attr in ("src", "data-src", "poster"):
                    val = tag.get(attr)
                    if val:
                        resolved = _abs(self.target_url, val)
                        if resolved:
                            tag[attr] = resolved
                            if tag_name in ("video", "iframe"):
                                self.cdn_log.append(f"[{tag_name.upper()} CDN] {resolved}")
        # <meta content="..."> with URLs (og:image, etc.)
        for meta in soup.find_all("meta", attrs={"content": True}):
            content = meta["content"]
            if content.startswith("/") and not content.startswith("//"):
                resolved = _abs(self.target_url, content)
                if resolved:
                    meta["content"] = resolved

    # ------------------------------------------------------------------
    # Deep-Scan Images
    # ------------------------------------------------------------------

    def _resolve_img_url(self, img_tag) -> str:
        # Priority: data-srcs (JSON), then data-src, then src
        for attr in IMG_ATTRS:
            raw = img_tag.get(attr)
            if not raw or not isinstance(raw, str):
                continue
            raw = raw.strip()
            if not raw:
                continue
            # JSON (Business Insider data-srcs)
            if attr == "data-srcs" and raw.startswith(("{", "[")):
                parsed = _extract_first_url_from_json(raw)
                if parsed:
                    return _abs(self.target_url, parsed)
            # Plain URL
            if raw.startswith(("http://", "https://", "//", "/")):
                if raw.startswith("//"):
                    raw = "https:" + raw
                return _abs(self.target_url, raw)
        return ""


    # No image downloading in Asset Sync mode; handled by _fix_images

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------



    def _remove_csp_meta(self, soup: BeautifulSoup):
        for meta in soup.find_all("meta"):
            equiv = (meta.get("http-equiv") or "").lower()
            if equiv in (
                "content-security-policy",
                "content-security-policy-report-only",
                "x-content-security-policy",
            ):
                meta.decompose()

    def _remove_base_tag(self, soup: BeautifulSoup):
        for base in soup.find_all("base"):
            base.decompose()

    # ------------------------------------------------------------------
    # Injections
    # ------------------------------------------------------------------


    def _inject_css(self, soup: BeautifulSoup):
        head = soup.find("head")
        if not head:
            head = soup.new_tag("head")
            if soup.html:
                soup.html.insert(0, head)
            else:
                soup.insert(0, head)
        css = BeautifulSoup(IMAGE_FIX_CSS, "html.parser")
        head.append(css)

    def _inject_link_killer(self, soup: BeautifulSoup):
        body = soup.find("body")
        if not body:
            body = soup.new_tag("body")
            if soup.html:
                soup.html.append(body)
            else:
                soup.append(body)
        script = BeautifulSoup(LINK_KILLER_JS, "html.parser")
        body.append(script)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _write_html(self, soup: BeautifulSoup):
        out = self.project_dir / "index.html"
        out.write_text(str(soup), encoding="utf-8")

    def _write_cdn_log(self):
        log_path = self.project_dir / "cdn_load.txt"
        with open(log_path, "w", encoding="utf-8") as f:
            if self.cdn_log:
                f.write("\n".join(self.cdn_log) + "\n")
            else:
                f.write("(no CDN/failed entries)\n")
        print(f"[*] CDN log → {log_path}  ({len(self.cdn_log)} entries)")


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------


def main():
    print("=" * 60)
    print("  Web Page Cloner  –  Same-to-Same UI")
    print("=" * 60)

    target_url = input("\nTarget URL: ").strip()
    if not target_url:
        print("[!] No URL provided.")
        sys.exit(1)
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url

    project_name = input("Project Name: ").strip()
    if not project_name:
        print("[!] No project name provided.")
        sys.exit(1)

    cloner = PageCloner(target_url, project_name)
    cloner.run()


if __name__ == "__main__":
    main()

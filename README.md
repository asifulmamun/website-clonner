# WebpageCloner — Offline Page Downloader

Clone any public webpage into a fully self-contained, offline-ready HTML template with all assets localized.

---

## Overview

WebpageCloner provides two distinct ways to clone webpages into offline-ready templates. You can choose the approach that best suits your requirements:

1. **Python-Based Approach**: A CLI tool for Python users to clone webpages and manage assets.
2. **PHP-Based Approach**: A PHP script that supports both CLI and server-based execution.

Both approaches are independent, and you can use either based on your preference or project requirements.

---

## Index

- [Python Version](#python-version)
- [PHP Version](#php-version)

---

## Features

- Interactive prompts — no need to type URLs in the command line
- **All projects saved under a single `cloned/` root folder** for clean organisation
- **Smart URL Auto-Correction** — enter `example.com` and the script tries `https://` then `http://` automatically
- **Deep path support** — project name `my_site/v1` creates `cloned/my_site/v1/`
- **Space handling** — spaces in project names are converted to hyphens automatically
- Downloads CSS, JS, images, fonts, and media files locally
- Smart Hybrid Downloader: localizes assets when possible, falls back to CDN on failure
- Handles lazy-loaded images (`data-src`, `data-srcset`, etc.)
- Localizes Open Graph / Twitter Card meta images
- Retry logic with exponential backoff for transient errors
- SSL fallback for certificate issues
- Logs all CDN-fallback URLs with reasons to `cdn_load.txt`
- Removes tracking scripts (Google Analytics, Facebook Pixel, Hotjar, etc.)
- **Advanced Directory Management** — All projects are saved under a `cloned/` root folder, with support for nested paths and automatic sanitization of project names.

---

## Python Approach

### Install Dependencies

Ensure you have Python 3.10.6 installed. The script has been tested with pip 26.0.1, which comes with Python 3.10. You can install the required dependencies using:

```bash
pip install -r requirements.txt
```

### Python Version Compatibility

- **Python Version**: 3.10.6
- **Pip Version**: 26.0.1

---

## PHP Approach

### PHP Version Compatibility

The PHP script is compatible with the following versions:

- **Minimum PHP Version**: 7.4
- **Tested Up To**: 8.2

Ensure the required PHP extensions (e.g., `cURL`, `libxml`) are enabled.

### Updated PHP Approach

The PHP script now supports both CLI-based and URL-based execution modes. You can choose the mode that best fits your workflow.

#### CLI-Based Execution

1. Open a terminal and navigate to the script directory:
   ```bash
   cd C:/Users/Al Mamun/Desktop/app/scrapper
   ```
2. Run the script with the required parameters:
   ```bash
   php webpage_cloner.php --site_url="https://example.com" --project_name="my_project"
   ```
   - `--site_url`: The URL of the website to clone.
   - `--project_name`: The name of the output folder inside `cloned/`.

#### URL-Based Execution

1. Place the `webpage_cloner.php` file in your server's document root (e.g., `htdocs` for XAMPP or `www` for Nginx).
2. Access the script via a browser or HTTP client by passing the required parameters:
   ```
   http://localhost/webpage_cloner.php?site_url=https://example.com&project_name=my_project
   ```
   - `site_url`: The URL of the website to clone.
   - `project_name`: The name of the output folder inside `cloned/`.

#### Example Usage

- **CLI**:
  ```bash
  php webpage_cloner.php --site_url="https://example.com" --project_name="my_project"
  ```
- **URL**:
  ```
  http://localhost/webpage_cloner.php?site_url=https://example.com&project_name=my_project
  ```

### How to Run

1. Open a terminal and navigate to the script directory:
   ```bash
   cd C:/Users/Al Mamun/Desktop/app/scrapper
   ```
2. Run the script using PHP CLI:
   ```bash
   php webpage_cloner.php
   ```
3. Follow the prompts to enter the website URL and project name.

### Run via a Web Server

1. Place the `webpage_cloner.php` file in your server's document root (e.g., `htdocs` for XAMPP or `www` for Nginx).
2. Access the script via a browser or HTTP client by passing the required parameters:
   ```
   http://localhost/webpage_cloner.php?site_url=https://example.com&project_name=my_project
   ```
3. The `site_url` parameter specifies the website to clone, and `project_name` defines the output folder inside `cloned/`.

### Example URL

```
http://localhost/webpage_cloner.php?site_url=https://example.com&project_name=my_project
```

### Notes

- Ensure the server has write permissions to the `cloned/` directory.
- PHP extensions like `cURL` and `libxml` must be enabled.

## Both Approaches

This project provides two distinct ways to clone webpages into offline-ready templates. You can choose the approach that best suits your requirements:

1. **Python-Based Approach**: A CLI tool for Python users to clone webpages and manage assets.
2. **PHP-Based Approach**: A PHP script that supports both CLI and server-based execution.

Both approaches are independent, and you can use either based on your preference or project requirements.
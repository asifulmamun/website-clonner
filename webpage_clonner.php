<?php

// Webpage Cloner in PHP
// Converts a webpage into a self-contained offline-ready template

// Configuration
$cloned_root = __DIR__ . '/cloned';
$default_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OfflinePageCloner/1.0";

// Prompt for URL and Project Name
function prompt($message) {
    echo $message;
    return trim(fgets(STDIN));
}

function sanitize_project_name($name) {
    $name = str_replace('\\', '/', $name); // Normalize backslashes
    $name = str_replace(' ', '-', $name);   // Replace spaces with hyphens
    $name = preg_replace('/[<>:"|?*]+/', '-', $name); // Remove unsafe characters
    $name = trim($name, '.-/');            // Trim leading/trailing slashes or dots
    return $name;
}

function resolve_url($raw_url) {
    $raw_url = trim($raw_url);
    if (empty($raw_url)) return null;

    if (!preg_match('/^https?:\/\//', $raw_url)) {
        $raw_url = "https://" . $raw_url;
    }

    if (filter_var($raw_url, FILTER_VALIDATE_URL)) {
        $headers = @get_headers($raw_url);
        if ($headers && strpos($headers[0], '200') !== false) {
            return $raw_url;
        }
        // Fallback to http://
        $raw_url = preg_replace('/^https:\/\//', 'http://', $raw_url);
        $headers = @get_headers($raw_url);
        if ($headers && strpos($headers[0], '200') !== false) {
            return $raw_url;
        }
    }

    return null;
}

function create_directory($path) {
    if (!is_dir($path)) {
        mkdir($path, 0777, true);
    }
}

function download_asset($url, $save_path) {
    $ch = curl_init($url);
    $fp = fopen($save_path, 'wb');

    curl_setopt($ch, CURLOPT_FILE, $fp);
    curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
    curl_setopt($ch, CURLOPT_USERAGENT, $GLOBALS['default_user_agent']);
    curl_setopt($ch, CURLOPT_TIMEOUT, 30);

    $success = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);

    curl_close($ch);
    fclose($fp);

    if ($success && $http_code < 400) {
        return true;
    } else {
        unlink($save_path); // Remove partial file
        return false;
    }
}

function scrape_and_download_assets($dom, $base_url, $assets_dir, $cdn_log_path) {
    $tags = [
        'img' => 'src',
        'link' => 'href',
        'script' => 'src',
        'source' => 'src',
    ];

    $cdn_log = fopen($cdn_log_path, 'a');

    foreach ($tags as $tag => $attr) {
        foreach ($dom->getElementsByTagName($tag) as $element) {
            $url = $element->getAttribute($attr);
            if (!$url || strpos($url, 'data:') === 0) continue;

            $absolute_url = resolve_url($base_url . '/' . ltrim($url, '/'));
            if (!$absolute_url) continue;

            $parsed_url = parse_url($absolute_url);
            $filename = basename($parsed_url['path']);
            $local_path = "$assets_dir/$filename";

            if (download_asset($absolute_url, $local_path)) {
                $element->setAttribute($attr, "assets/$filename");
            } else {
                fwrite($cdn_log, "$absolute_url\n");
            }
        }
    }

    fclose($cdn_log);
}

function neutralize_html($dom) {
    // Neutralize all <a> tags
    foreach ($dom->getElementsByTagName('a') as $a) {
        $a->setAttribute('href', 'javascript:void(0)');
        $a->removeAttribute('target');
    }

    // Deactivate all <form> tags
    foreach ($dom->getElementsByTagName('form') as $form) {
        $form->setAttribute('action', '#');
        $form->setAttribute('onsubmit', 'return false;');
    }

    // Remove unwanted attributes from all elements
    $unwanted_attributes = ['onclick', 'onmouseover', 'onmousedown', 'onbeforeunload'];
    foreach ($dom->getElementsByTagName('*') as $element) {
        foreach ($unwanted_attributes as $attr) {
            if ($element->hasAttribute($attr)) {
                $element->removeAttribute($attr);
            }
        }
    }

    // Remove <script> tags with specific keywords in src or content
    foreach ($dom->getElementsByTagName('script') as $script) {
        $src = $script->getAttribute('src');
        $content = $script->textContent;
        if (
            stripos($src, 'analytics') !== false ||
            stripos($src, 'pixel') !== false ||
            stripos($src, 'tracking') !== false ||
            stripos($src, 'hotjar') !== false ||
            stripos($content, 'analytics') !== false ||
            stripos($content, 'pixel') !== false ||
            stripos($content, 'tracking') !== false ||
            stripos($content, 'hotjar') !== false
        ) {
            $script->parentNode->removeChild($script);
        }
    }

    // Remove <base> tag
    foreach ($dom->getElementsByTagName('base') as $base) {
        $base->parentNode->removeChild($base);
    }
}

// Main Logic
$site_url = prompt("Enter the Website URL: ");
$site_url = resolve_url($site_url);
if (!$site_url) {
    die("Invalid or unreachable URL.\n");
}

$project_name = prompt("Enter Project Name: ");
$project_name = sanitize_project_name($project_name);

$project_dir = "$cloned_root/$project_name";
$assets_dir = "$project_dir/assets";
$cdn_log_path = "$project_dir/cdn_load.txt";

create_directory($project_dir);
create_directory($assets_dir);

$html = file_get_contents($site_url);
$dom = new DOMDocument();
libxml_use_internal_errors(true);
$dom->loadHTML($html);
libxml_clear_errors();

// Scrape and download assets
scrape_and_download_assets($dom, $site_url, $assets_dir, $cdn_log_path);

// Neutralize interactive elements
neutralize_html($dom);

// Save modified HTML
file_put_contents("$project_dir/index.html", $dom->saveHTML());

// Deactivate links and forms
foreach ($dom->getElementsByTagName('a') as $a) {
    $a->setAttribute('href', 'javascript:void(0)');
}
foreach ($dom->getElementsByTagName('form') as $form) {
    $form->setAttribute('onsubmit', 'return false;');
}

echo "Cloning complete. Output saved to $project_dir\n";
?>
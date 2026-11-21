<?php

function cueporch_woo_origin(string $url): string {
    $p = preg_match('/[\s\\\\]/', $url) ? false : parse_url($url);
    if (!is_array($p) || empty($p['scheme']) || empty($p['host'])
        || isset($p['user']) || isset($p['pass']) || isset($p['fragment'])) {
        return '';
    }
    $scheme = strtolower($p['scheme']);
    $host = strtolower($p['host']);
    if (($scheme !== 'https' && $scheme !== 'http') || preg_match('/[^\x21-\x7e]/', $host)) {
        return '';
    }
    $isLocal = ($host === 'localhost' || $host === '127.0.0.1');
    $validHost = $isLocal
        || filter_var($host, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4) !== false
        || preg_match('/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.[a-z][a-z0-9-]*[a-z0-9]$/', $host);
    if (!$validHost || ($scheme === 'http' && !$isLocal)) {
        return '';
    }
    $port = $p['port'] ?? null;
    if ($port !== null && (!is_int($port) || $port < 1 || $port > 65535)) {
        return '';
    }
    $default = $scheme === 'https' ? 443 : 80;
    $origin = $scheme . '://' . $host;
    if ($port !== null && $port !== $default) {
        $origin .= ':' . $port;
    }
    return $origin;
}

function cueporch_woo_embed_url(string $value, string $allowed_origin): string
{
    if ($allowed_origin === '') {
        return '';
    }

    if (strlen($value) > 1024) {
        return '';
    }

    if (preg_match('/[\x00-\x20\x7F]/', $value)) {
        return '';
    }

    if (strpos($value, '\\') !== false) {
        return '';
    }

    if (strpos($value, '#') !== false) {
        return '';
    }

    $origin = cueporch_woo_origin($value);
    if ($origin === '' || $origin !== $allowed_origin) {
        return '';
    }

    $urlParts = parse_url($value);
    if ($urlParts === false) {
        return '';
    }

    $path = $urlParts['path'] ?? '';
    $query = $urlParts['query'] ?? '';

    if (!preg_match('#\A/(embed|watch)/work/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\z#', $path, $pathMatches)) {
        return '';
    }

    $canonicalPath = '/embed/work/' . $pathMatches[2];
    $canonicalQuery = '';

    if ($query !== '') {
        $queryPairs = explode('&', $query);
        $seenKeys = [];

        foreach ($queryPairs as $pair) {
            if ($pair === '') {
                return '';
            }

            if (!preg_match('#\A(?:key=[A-Za-z0-9_-]{1,128}|start=\d{1,6}(?:\.\d{1,3})?|(?:autoplay|muted|loop|api|dnt)=(?:0|1|true|false))\z#', $pair)) {
                return '';
            }

            $equalsPosition = strpos($pair, '=');
            if ($equalsPosition === false) {
                return '';
            }

            $key = substr($pair, 0, $equalsPosition);
            if (isset($seenKeys[$key])) {
                return '';
            }

            $seenKeys[$key] = true;
        }

        $canonicalQuery = '?' . $query;
    }

    return $origin . $canonicalPath . $canonicalQuery;
}

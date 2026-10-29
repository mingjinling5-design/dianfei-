<?php
namespace FoskyTech;

/** Import a single, complete balance request; never combine different sessions/rooms. */
class DingTalkCapture
{
    public static function isAllowedUrl(string $url): bool
    {
        $u = parse_url($url);
        if (!is_array($u)) return false;
        $host = strtolower($u['host'] ?? '');
        // Match DNS labels, never a substring such as xiaofubao.com.example.org.
        $domain = $host === 'xiaofubao.com' || substr($host, -strlen('.xiaofubao.com')) === '.xiaofubao.com';
        return ($u['scheme'] ?? '') === 'https' && $domain
            && !isset($u['user']) && !isset($u['pass']) && !isset($u['fragment'])
            && (!isset($u['port']) || $u['port'] === 443)
            && ($u['path'] ?? '') === '/app/electric/queryRoomSurplus';
    }

    public static function parse(string $text): array
    {
        $json = json_decode($text, true);
        $requests = [];
        if (isset($json['log']['entries'])) {
            $pending = false; $completed = false;
            foreach ($json['log']['entries'] as $entry) {
                if (($entry['request']['_status'] ?? '') === 'processing') $pending = true;
                if (($entry['response']['status'] ?? 0) > 0) $completed = true;
            }
            if ($pending && !$completed) throw new \RuntimeException('抓包尚未完成：HAR 中的请求仍为 processing，未记录有效响应。请在 Reqable 确认余额请求已完成、请求体和响应内容可见后，重新导出完整 HAR。');
            foreach ($json['log']['entries'] as $entry) {
                $r = $entry['request'];
                $headers = $r['headers'] ?? [];
                if (!array_filter($headers, function ($h) { return strcasecmp($h['name'], 'cookie') === 0; })) {
                    $cookies = array_map(function ($c) { return $c['name'].'='.$c['value']; }, $r['cookies'] ?? []);
                    if ($cookies) $headers[] = ['name'=>'Cookie', 'value'=>implode('; ', $cookies)];
                }
                $body = $r['postData']['text'] ?? '';
                if ($body === '' && !empty($r['postData']['params'])) {
                    $p = [];
                    foreach ($r['postData']['params'] as $v) $p[$v['name']] = $v['value'];
                    $body = http_build_query($p);
                }
                $requests[] = ['url'=>$r['url'], 'method'=>$r['method'], 'headers'=>$headers, 'body'=>$body];
            }
        } else {
            $parts = preg_split('/\r?\n\r?\n/', trim($text), 2);
            $lines = preg_split('/\r?\n/', $parts[0]);
            if (!preg_match('~^(GET|POST) (\S+) HTTP/[\d.]+$~', array_shift($lines), $m)) {
                throw new \RuntimeException('请提供 HAR 或完整原始 HTTP 请求文本；不支持加密 pcap / curl 命令。');
            }
            $headers = []; $host = '';
            foreach ($lines as $line) {
                $pair = explode(':', $line, 2);
                if (count($pair) !== 2) continue;
                $headers[] = ['name'=>trim($pair[0]), 'value'=>trim($pair[1])];
                if (strcasecmp(trim($pair[0]), 'host') === 0) $host = trim($pair[1]);
            }
            $requests[] = ['url'=>strpos($m[2], 'https://') === 0 ? $m[2] : 'https://'.$host.$m[2],
                'method'=>$m[1], 'headers'=>$headers, 'body'=>$parts[1] ?? ''];
        }
        $matches = [];
        foreach ($requests as $r) {
            $u = parse_url($r['url']);
            // Only the known read-only electricity endpoint is eligible for automatic replay.
            if (!self::isAllowedUrl($r['url'])) continue;
            // Some HAR exporters record the connection/SNI host in url, while HTTP/2
            // :authority names the actual virtual host. Replay that captured host.
            $authority = null;
            foreach ($r['headers'] as $h) {
                if (!in_array(strtolower($h['name']), [':authority', 'host'], true)) continue;
                $value = strtolower(trim($h['value']));
                if (!preg_match('/\A[a-z0-9.-]+(?::443)?\z/i', $value)) throw new \RuntimeException('抓包中的站点请求头无效。');
                $value = preg_replace('/:443$/', '', $value);
                if ($authority !== null && $authority !== $value) throw new \RuntimeException('抓包中的 Host 与 :authority 不一致，请重新导出原始请求。');
                $authority = $value;
            }
            if ($authority !== null && $authority !== strtolower($u['host'])) {
                $target = 'https://'.$authority.$u['path'].(isset($u['query']) ? '?'.$u['query'] : '');
                if (!self::isAllowedUrl($target)) throw new \RuntimeException('抓包中的实际站点不属于校付宝电费接口。');
                $r['capturedUrl'] = $r['url'];
                $r['url'] = $target;
                $u = parse_url($target);
            }
            if (!in_array($r['method'], ['GET','POST'], true)) continue;
            parse_str($u['query'] ?? '', $p);
            $b = json_decode($r['body'], true);
            if (!is_array($b)) parse_str($r['body'], $b);
            $p = array_merge($p, $b);
            if (($p['platform'] ?? '') !== 'DING_TALK_H5') continue;
            foreach (['areaId','buildingCode','floorCode','roomCode'] as $key) {
                if (!isset($p[$key]) || !is_scalar($p[$key]) || (string)$p[$key] === '') continue 2;
            }
            $r['room'] = array_intersect_key($p, array_flip(['areaId','buildingCode','floorCode','roomCode']));
            $safe = [];
            foreach ($r['headers'] as $h) {
                if (preg_match('/[\r\n]/', $h['name'].$h['value'])) throw new \RuntimeException('无效请求头');
                if (in_array(strtolower($h['name']), ['host','content-length','connection','accept-encoding']) || strpos($h['name'], ':') === 0) continue;
                $safe[] = $h;
            }
            $r['headers'] = $safe;
            $matches[] = $r;
        }
        if (!$matches) throw new \RuntimeException('未找到参数完整的 DING_TALK_H5 电费余额请求。请抓取成功查询余额的请求；其他域名或接口需按实际抓包适配。');
        $rooms = array_unique(array_map(function ($r) { return json_encode($r['room']); }, $matches));
        if (count($rooms) > 1) throw new \RuntimeException('抓包含多个宿舍，请仅导出目标宿舍的余额请求。');
        return end($matches);
    }

    public static function query(array $request): array
    {
        if (!self::isAllowedUrl($request['url'] ?? '')) throw new \RuntimeException('无效的校付宝电费接口地址。');
        $curl = curl_init($request['url']);
        $options = [CURLOPT_RETURNTRANSFER=>true, CURLOPT_CUSTOMREQUEST=>$request['method'],
            CURLOPT_HTTPHEADER=>array_map(function ($h) { return $h['name'].': '.$h['value']; }, $request['headers']),
            CURLOPT_CONNECTTIMEOUT=>10, CURLOPT_TIMEOUT=>30, CURLOPT_SSL_VERIFYHOST=>2,
            CURLOPT_SSL_VERIFYPEER=>true, CURLOPT_FOLLOWLOCATION=>false, CURLOPT_PROTOCOLS=>CURLPROTO_HTTPS,
            CURLOPT_ENCODING=>''];
        if ($request['method'] === 'POST') $options[CURLOPT_POSTFIELDS] = $request['body'];
        curl_setopt_array($curl, $options);
        $body = curl_exec($curl); $errno = curl_errno($curl); $status = curl_getinfo($curl, CURLINFO_HTTP_CODE);
        curl_close($curl);
        if ($errno) throw new \RuntimeException('网络或 TLS 验证失败，cURL 错误码 '.$errno.'；请运行证书检查。');
        if ($status !== 200) throw new \RuntimeException('接口 HTTP '.$status.'；认证失效时请重新抓包。');
        $data = self::decodeResponse($body);
        return ['time'=>date(DATE_ATOM), 'room'=>$request['room'], 'roomName'=>$data['data']['displayRoomName'],
            'balance'=>$data['data']['amount'], 'surplus'=>$data['data']['surplus'] ?? null,
            'isShowSurplus'=>$data['data']['isShowSurplus'] ?? null, 'isShowMoney'=>$data['data']['isShowMoney'] ?? null];
    }

    public static function decodeResponse(string $body): array
    {
        if (trim($body) === '') throw new \RuntimeException('电费服务器返回 HTTP 200，但响应内容为空，未获取到实时余额。可能是登录状态失效或服务端访问限制，请在钉钉重新成功查询后导出新抓包。');
        $data = json_decode($body, true);
        if (!is_array($data)) throw new \RuntimeException('电费服务器返回非 JSON 内容，未获取到余额，请检查网络或服务端访问限制。');
        $code = $data['statusCode'] ?? $data['code'] ?? null;
        if ((string)$code === '204') throw new \RuntimeException('登录已过期（statusCode=204，服务器提示“请重新登录”）。请在钉钉重新登录并成功查询电费，然后上传新抓包；旧抓包无法自动续期。');
        if (($data['success'] ?? false) !== true) {
            $codeText = is_scalar($code) ? (string)$code : '未提供';
            throw new \RuntimeException('电费接口拒绝请求（statusCode='.$codeText.'），未获取实时余额。请核对宿舍和登录状态。');
        }
        if (!isset($data['data']['amount'], $data['data']['displayRoomName'])) throw new \RuntimeException('余额响应格式变化，需按实际抓包适配。');
        return $data;
    }
}

<?php
require __DIR__.'/src/DingTalkCapture.php';
date_default_timezone_set('Asia/Shanghai');
$dir = __DIR__.'/local';
if (!is_dir($dir)) mkdir($dir, 0700, true);
try {
    $command = $argv[1] ?? '';
    if ($command === 'import') {
        $file = $argv[2] ?? '';
        if (!is_file($file)) throw new RuntimeException('用法：php electricity.php import 抓包文件.har');
        $request = FoskyTech\DingTalkCapture::parse(file_get_contents($file));
        if (file_put_contents($dir.'/request.json', json_encode($request, JSON_PRETTY_PRINT|JSON_UNESCAPED_UNICODE), LOCK_EX) === false) throw new RuntimeException('配置写入失败');
        echo "已导入 DING_TALK_H5 请求；宿舍参数：".json_encode($request['room'], JSON_UNESCAPED_UNICODE).PHP_EOL;
    } elseif ($command === 'query') {
        if (!is_file($dir.'/request.json')) throw new RuntimeException('尚未导入抓包。请先执行 import。');
        $lock = fopen($dir.'/query.lock', 'c');
        if (!flock($lock, LOCK_EX|LOCK_NB)) exit(0);
        $request = json_decode(file_get_contents($dir.'/request.json'), true);
        // Revalidate persisted requests before sending credentials.
        $request = FoskyTech\DingTalkCapture::parse(json_encode(['log'=>['entries'=>[['request'=>[
            'url'=>$request['url'], 'method'=>$request['method'], 'headers'=>$request['headers'], 'postData'=>['text'=>$request['body']]
        ]]]]]));
        $result = FoskyTech\DingTalkCapture::query($request);
        $encoded = json_encode($result, JSON_UNESCAPED_UNICODE|JSON_PRETTY_PRINT);
        file_put_contents($dir.'/latest.json', $encoded, LOCK_EX);
        file_put_contents($dir.'/status.json', json_encode(['time'=>date(DATE_ATOM), 'ok'=>true, 'message'=>'实时余额查询成功'], JSON_UNESCAPED_UNICODE), LOCK_EX);
        file_put_contents($dir.'/history.jsonl', json_encode($result, JSON_UNESCAPED_UNICODE).PHP_EOL, FILE_APPEND|LOCK_EX);
        echo $encoded.PHP_EOL;
    } elseif ($command === 'doctor') {
        echo 'PHP: '.PHP_VERSION.PHP_EOL.'ini: '.php_ini_loaded_file().PHP_EOL;
        foreach (['curl.cainfo','openssl.cafile'] as $key) {
            $path = ini_get($key);
            echo $key.': '.$path.' '.(is_readable($path) ? '可读取' : '未配置或不可读取').PHP_EOL;
        }
        $host = 'compus.xiaofubao.com';
        if (is_file($dir.'/request.json')) {
            $config = json_decode(file_get_contents($dir.'/request.json'), true);
            if (FoskyTech\DingTalkCapture::isAllowedUrl($config['url'] ?? '')) $host = parse_url($config['url'], PHP_URL_HOST);
        }
        echo 'TLS target: '.$host.PHP_EOL;
        $c = curl_init('https://'.$host.'/');
        curl_setopt_array($c, [CURLOPT_RETURNTRANSFER=>true, CURLOPT_TIMEOUT=>20, CURLOPT_SSL_VERIFYHOST=>2, CURLOPT_SSL_VERIFYPEER=>true]);
        curl_exec($c);
        if (curl_errno($c)) throw new RuntimeException('TLS/网络检查失败，cURL 错误码 '.curl_errno($c));
        echo '目标服务器 TLS 证书验证通过；HTTP '.curl_getinfo($c, CURLINFO_HTTP_CODE).PHP_EOL;
    } else {
        echo "php electricity.php import <capture.har|request.txt>\nphp electricity.php query\nphp electricity.php doctor\n";
    }
} catch (Throwable $e) {
    $message = date(DATE_ATOM).' '.$e->getMessage().PHP_EOL;
    file_put_contents($dir.'/errors.log', $message, FILE_APPEND|LOCK_EX);
    if (($command ?? '') === 'query') file_put_contents($dir.'/status.json', json_encode(['time'=>date(DATE_ATOM), 'ok'=>false, 'message'=>$e->getMessage()], JSON_UNESCAPED_UNICODE), LOCK_EX);
    fwrite(STDERR, $message); exit(1);
}

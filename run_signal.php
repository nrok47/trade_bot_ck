<?php
$dir    = __DIR__;
$python = $dir . '\\.venv\\Scripts\\python.exe';
$script = $dir . '\\signal_advisor.py';

if (!file_exists($python)) {
    // fallback: ใช้ python ใน PATH
    $python = 'python';
}

$cmd    = "cd /d \"$dir\" && \"$python\" \"$script\" --report 2>&1";
$output = shell_exec($cmd);

header('Content-Type: application/json; charset=utf-8');
if ($output === null) {
    echo json_encode(['ok' => false, 'msg' => 'shell_exec ล้มเหลว — ตรวจสอบ PHP safe_mode หรือ disable_functions']);
} else {
    echo json_encode(['ok' => true, 'output' => $output]);
}

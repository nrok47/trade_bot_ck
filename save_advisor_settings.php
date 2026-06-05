<?php
header('Content-Type: application/json; charset=utf-8');
$file = __DIR__ . '/advisor_settings.json';

$body = file_get_contents('php://input');
$data = json_decode($body, true);

if (!$data || !isset($data['symbols']) || !is_array($data['symbols'])) {
    echo json_encode(['ok' => false, 'msg' => 'ข้อมูลไม่ถูกต้อง']);
    exit;
}

// Sanitize symbols
$syms = array_values(array_filter(array_map(function($s){ return strtoupper(trim($s)); }, $data['symbols'])));
if (empty($syms)) {
    echo json_encode(['ok' => false, 'msg' => 'ต้องมีอย่างน้อย 1 เหรียญ']);
    exit;
}

$out = [
    'symbols'         => $syms,
    'score_threshold' => isset($data['score_threshold']) ? (float)$data['score_threshold'] : 3.0,
    'leverage'        => isset($data['leverage'])        ? (int)$data['leverage']          : 8,
    'tp_roe_pct'      => isset($data['tp_roe_pct'])      ? (float)$data['tp_roe_pct']      : 30.0,
    'sl_hard_roe_pct' => isset($data['sl_hard_roe_pct']) ? (float)$data['sl_hard_roe_pct'] : 15.0,
];

if (file_put_contents($file, json_encode($out, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE)) === false) {
    echo json_encode(['ok' => false, 'msg' => 'บันทึกไฟล์ไม่ได้ — ตรวจสอบ permission ของโฟลเดอร์']);
    exit;
}

echo json_encode(['ok' => true]);

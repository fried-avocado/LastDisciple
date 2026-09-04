#!/bin/bash
# 验证：appcenter-cli 回退逻辑 + 增强日志输出
cd /mnt/e/workspace/fn/FreePower/app/server

echo "===== 1. 语法检查 ====="
python3 -m py_compile server.py && echo "server.py OK"
python3 -m py_compile priv_helper.py && echo "priv_helper.py OK"

echo ""
echo "===== 2. 启动特权助手（root，测试用户=root） ====="
export TRIM_PKGVAR=/tmp/freepower_log_test
mkdir -p "$TRIM_PKGVAR"
SOCK="/tmp/freepower_log_test/priv_helper.sock"
rm -f "$SOCK" "$TRIM_PKGVAR/info.log"
python3 priv_helper.py "$SOCK" root > /tmp/log_helper_stdout.log 2>&1 &
HPID=$!
sleep 2
[ -S "$SOCK" ] || { echo "FAIL: socket not created"; cat /tmp/log_helper_stdout.log; kill $HPID; exit 1; }
echo "helper started, sock=$SOCK"

echo ""
echo "===== 3. 测试 get_apps（appcenter-cli 不存在 → 应回退到目录遍历） ====="
python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(10)
s.connect('$SOCK')
f = s.makefile('rwb')
req = {'op': 'get_apps', 'params': {'what': 'apps'}, 'id': 1}
f.write((json.dumps(req) + '\n').encode())
f.flush()
resp = json.loads(f.readline().decode())
print('  success:', resp.get('success'))
data = resp.get('data', {})
print('  source:', data.get('source'))
print('  count:', data.get('count'))
f.close(); s.close()
"

echo ""
echo "===== 4. 测试 get_apps containers ====="
python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(10)
s.connect('$SOCK')
f = s.makefile('rwb')
req = {'op': 'get_apps', 'params': {'what': 'containers'}, 'id': 2}
f.write((json.dumps(req) + '\n').encode())
f.flush()
resp = json.loads(f.readline().decode())
print('  success:', resp.get('success'))
data = resp.get('data', {})
print('  count:', data.get('count'))
f.close(); s.close()
"

echo ""
echo "===== 5. 测试 start app（appcenter-cli 不存在 → systemctl 回退 → 应失败但记录日志） ====="
python3 -c "
import socket, json
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(30)
s.connect('$SOCK')
f = s.makefile('rwb')
req = {'op': 'start', 'params': {'type': 'app', 'name': 'testapp_xyz'}, 'id': 3}
f.write((json.dumps(req) + '\n').encode())
f.flush()
resp = json.loads(f.readline().decode())
print('  success:', resp.get('success'))
print('  error:', (resp.get('error') or '')[:100])
f.close(); s.close()
"

echo ""
echo "===== 6. 停止 helper ====="
kill $HPID 2>/dev/null
wait $HPID 2>/dev/null || true

echo ""
echo "===== 7. helper 审计日志（info.log） ====="
cat "$TRIM_PKGVAR/info.log" 2>/dev/null

echo ""
echo "===== 8. 启动 server.py（无 helper）测试降级日志 ====="
pkill -f "python3 server.py" 2>/dev/null || true
sleep 1
rm -f /mnt/e/workspace/fn/FreePower/app/server/ping.log
env -u TRIM_PRIV_HELPER_SOCK python3 server.py > /tmp/log_server_stdout.log 2>&1 &
SPID=$!
sleep 3

echo "  --- GET /api/system-apps ---"
curl -s http://127.0.0.1:8180/api/system-apps > /dev/null

echo "  --- POST /api/app-control ---"
curl -s -X POST http://127.0.0.1:8180/api/app-control -H 'Content-Type: application/json' -d '{"action":"stop","type":"docker","name":"test"}' > /dev/null

echo ""
echo "  --- server ping.log 中的权限相关日志 ---"
grep '\[priv\]\|\[api\]' /mnt/e/workspace/fn/FreePower/app/server/ping.log 2>/dev/null | head -20

kill $SPID 2>/dev/null
wait $SPID 2>/dev/null || true

echo ""
echo "ALL LOG TESTS DONE"

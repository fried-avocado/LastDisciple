#!/usr/bin/env python3
"""
FreePower 业务后端服务

架构模式（遵循 serve.py）：
- 同时监听 TCP 端口和 Unix domain socket，兼容 fnOS 统一网关
- TCP 端口：供 checkport 健康检查与直连访问
- Unix socket：供 fnOS 网关（nginx）通过 gatewaySocket 代理流量

业务功能：
- Ping 网关监控（固定/自适应探测模式）
- 配置管理（基础/高级/IoT/通知）
- 静态文件服务
- RESTful API 路由

启动方式：
  # fnOS 部署模式（TCP + Unix socket）
  python web_server.py <tcp_port> <socket_path> [static_dir] [gateway_prefix]

  # 本地开发模式（仅 TCP，默认端口 8180）
  python web_server.py
"""
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote


# ========== 路径与常量 ==========

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # app/server/
APP_DIR = os.path.dirname(BASE_DIR)                             # app/
STATIC_DIR = os.path.join(APP_DIR, "ui")                        # app/ui/

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
ADVANCED_CONFIG_FILE = os.path.join(BASE_DIR, "advanced_config.json")
IOT_CONFIG_FILE = os.path.join(BASE_DIR, "iot_config.json")
NOTIFICATION_CONFIG_FILE = os.path.join(BASE_DIR, "notification_config.json")
LOG_FILE = os.path.join(BASE_DIR, "ping.log")

# 特权助手 Unix socket 路径（由 cmd/main 以 root 启动 helper 时注入环境变量）
PRIV_HELPER_SOCKET = os.environ.get('TRIM_PRIV_HELPER_SOCK', '')


# ========== 全局状态 ==========

last_status = {"connected": False, "gateway": "", "response_time": 0, "error": ""}
probing_state_info = {}
ping_history = []
ping_stats = {"min": 0, "avg": 0, "max": 0, "count": 0}
service_start_time = time.time()

# 外网探测状态（与内网独立管理，检测模式共用）
wan_last_status = {"connected": False, "host": "", "response_time": 0, "error": "", "enabled": False}
wan_probing_state_info = {}
wan_ping_history = []
wan_ping_stats = {"min": 0, "avg": 0, "max": 0, "count": 0, "loss_rate": 0}

# 日志写入锁：内网/外网线程并发写 ping.log 时防止行撕裂
_log_lock = threading.Lock()


def write_log(message):
    """追加一条带时间戳的日志到 ping.log。多线程写入需加锁保证行原子性。"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}\n"
    try:
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(log_line)
    except Exception:
        pass


# ========== 特权助手客户端（临时 root 权限：申请 → 使用 → 释放） ==========

class PrivilegeError(Exception):
    """特权助手不可用（连接失败/通信异常）时抛出，对应 503。"""


class PrivilegeOperationError(Exception):
    """特权助手可达但操作本身失败（如命令未找到/执行出错）时抛出。"""


class PrivilegeClient:
    """与 root 特权助手通信的客户端。

    遵循飞牛 OS 权限规范：Web 服务以包用户运行，通过 Unix socket 向
    root helper 临时申请特权操作。使用上下文管理器实现安全流程：
      with PrivilegeClient() as priv:   # 申请（连接）
          data = priv.execute('get_apps')  # 使用
      # 退出 with 块自动释放（关闭连接）

    全程日志记录：连接申请/释放、请求参数、返回状态码、错误详情，
    便于追踪权限相关问题的根本原因。
    """

    def __init__(self, socket_path=None, timeout=30):
        self.socket_path = socket_path or PRIV_HELPER_SOCKET
        self.timeout = timeout
        self._sock = None
        self._f = None
        self._id = 0

    def __enter__(self):
        # 申请：连接特权助手 socket
        if not self.socket_path or os.name == 'nt':
            run_user = os.environ.get('TRIM_RUN_USERNAME') or os.environ.get('TRIM_USERNAME') or os.environ.get('USER', 'unknown')
            write_log(f"[priv] ACQUIRE caller_user={run_user} socket='{self.socket_path}' "
                      f"status=SKIP reason={'not_on_fnos(os.name=nt)' if os.name == 'nt' else 'socket_path_empty'}")
            raise PrivilegeError('privilege helper unavailable (not on fnOS)')
        run_user = os.environ.get('TRIM_RUN_USERNAME') or os.environ.get('TRIM_USERNAME') or os.environ.get('USER', 'unknown')
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect(self.socket_path)
            self._f = self._sock.makefile('rwb')
            write_log(f"[priv] ACQUIRE caller_user={run_user} socket='{self.socket_path}' status=OK")
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as e:
            write_log(f"[priv] ACQUIRE caller_user={run_user} socket='{self.socket_path}' "
                      f"status=FAIL error={type(e).__name__}: {e}")
            raise PrivilegeError(f'cannot connect helper: {e}')
        return self

    def __exit__(self, *exc):
        # 释放：关闭连接
        try:
            if self._f:
                self._f.close()
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        write_log(f"[priv] RELEASE socket='{self.socket_path}' status=OK")
        return False

    def execute(self, op, params=None):
        """使用：向 helper 发送一个操作请求并返回 data 字段。"""
        if not self._f:
            raise PrivilegeError('not connected')
        self._id += 1
        req = {'op': op, 'params': params or {}, 'id': self._id}
        req_str = json.dumps(req, ensure_ascii=False)
        try:
            self._f.write((req_str + '\n').encode('utf-8'))
            self._f.flush()
            line = self._f.readline()
        except (BrokenPipeError, OSError) as e:
            write_log(f"[priv] EXECUTE op={op} params={params} req_id={self._id} "
                      f"status=FAIL error=communication_failed: {e}")
            raise PrivilegeError(f'communication failed: {e}')
        if not line:
            write_log(f"[priv] EXECUTE op={op} params={params} req_id={self._id} "
                      f"status=FAIL error=helper_closed_connection")
            raise PrivilegeError('helper closed connection')
        try:
            resp = json.loads(line.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            write_log(f"[priv] EXECUTE op={op} params={params} req_id={self._id} "
                      f"status=FAIL error=invalid_response: {e}")
            raise PrivilegeError(f'invalid helper response: {e}')
        if not resp.get('success'):
            # 助手可达但操作失败（命令未找到/执行出错）——与连接不可用区分
            err = resp.get('error', 'unknown helper error')
            write_log(f"[priv] EXECUTE op={op} params={params} req_id={self._id} "
                      f"status=OPERATION_FAILED error='{err}'")
            raise PrivilegeOperationError(err)
        write_log(f"[priv] EXECUTE op={op} params={params} req_id={self._id} status=OK")
        return resp.get('data')


# ========== 自适应探测算法 ==========

class ConservativeAdaptiveProbingAlgorithm:
    """保守型自适应探测算法。

    根据网络状况动态调整探测间隔：
    - 网络正常时逐步提升探测等级（间隔翻倍）
    - 网络异常时立即重置到 L0（1 秒间隔）
    """

    def __init__(self):
        self.rtt_threshold = 500
        self.min_count_per_level = 3
        self.max_interval = 1800

        self.levels = self._generate_levels()
        self.current_level = 0
        self.current_level_count = 0

        self.window_size = 20
        self.rtt_window = []
        self.loss_window = []
        self.consecutive_success = 0
        self.consecutive_loss = 0

        self.total_probes = 0
        self.level_changes = 0

    def _generate_levels(self):
        levels = []
        interval = 1
        count = 30

        while interval <= self.max_interval:
            levels.append({
                "interval": interval,
                "count": count,
                "name": f"L{len(levels)}"
            })
            interval *= 2
            count = max(self.min_count_per_level, count // 2)

        return levels

    def add_sample(self, rtt_ms, success):
        self.total_probes += 1

        if success:
            self.rtt_window.append(rtt_ms)
            self.loss_window.append(0)
            self.consecutive_success += 1
            self.consecutive_loss = 0
        else:
            self.loss_window.append(1)
            self.consecutive_loss += 1
            self.consecutive_success = 0

        while len(self.rtt_window) > self.window_size:
            self.rtt_window.pop(0)
        while len(self.loss_window) > self.window_size:
            self.loss_window.pop(0)

        if not success or rtt_ms > self.rtt_threshold:
            self._reset_to_level_0()
            return self.get_current_interval()

        self.current_level_count += 1

        if self.current_level_count >= self.levels[self.current_level]["count"]:
            if self.current_level < len(self.levels) - 1:
                self._promote_level()
            else:
                self.current_level_count = self.levels[self.current_level]["count"]

        return self.get_current_interval()

    def _reset_to_level_0(self):
        self.current_level = 0
        self.current_level_count = 0
        self.level_changes += 1

    def _promote_level(self):
        if self.current_level < len(self.levels) - 1:
            self.current_level += 1
            self.current_level_count = 0
            self.level_changes += 1

    def get_current_interval(self):
        return self.levels[self.current_level]["interval"]

    def get_state_info(self):
        if len(self.rtt_window) > 0:
            avg_rtt = sum(self.rtt_window) / len(self.rtt_window)
            min_rtt = min(self.rtt_window)
            max_rtt = max(self.rtt_window)
        else:
            avg_rtt = 0
            min_rtt = 0
            max_rtt = 0

        loss_count = sum(self.loss_window)
        loss_rate = loss_count / max(len(self.loss_window), 1)

        current_level_data = self.levels[self.current_level]
        progress = min(100.0, self.current_level_count / current_level_data["count"] * 100)

        remaining_in_level = max(0, current_level_data["count"] - self.current_level_count)

        return {
            "state": current_level_data["name"],
            "level": self.current_level,
            "current_interval": self.get_current_interval(),
            "level_probe_count": self.current_level_count,
            "level_total_count": current_level_data["count"],
            "level_progress_pct": round(progress, 1),
            "remaining_in_level": remaining_in_level,
            "total_levels": len(self.levels),
            "avg_rtt": int(avg_rtt),
            "min_rtt": int(min_rtt),
            "max_rtt": int(max_rtt),
            "loss_rate_pct": round(loss_rate * 100, 1),
            "consecutive_success": self.consecutive_success,
            "consecutive_loss": self.consecutive_loss,
            "total_probes": self.total_probes,
            "level_changes": self.level_changes,
            "rtt_threshold_ms": self.rtt_threshold,
            "is_max_level": self.current_level == len(self.levels) - 1
        }

    def reset(self, new_base_interval=None):
        self._reset_to_level_0()
        self.rtt_window = []
        self.loss_window = []
        self.consecutive_success = 0
        self.consecutive_loss = 0
        self.total_probes = 0
        self.level_changes = 0


# ========== Ping 监控线程 ==========

class PingThread(threading.Thread):
    """后台 Ping 监控线程，支持固定/自适应两种探测模式。"""

    def __init__(self):
        super().__init__()
        self.daemon = True
        self.running = True
        self.adaptive_algorithm = None
        self.current_mode = "fixed"
        self.last_base_interval = 30
        self._config_lock = threading.Lock()

    def _tcp_ping(self, host, port=80, timeout=3):
        """通过TCP连接判断主机是否可达（不需要ICMP原始套接字权限）。

        飞牛OS上非root用户可能没有ICMP权限，subprocess调用ping会失败。
        TCP连接成功或被拒绝(Connection refused)都说明主机网络可达，
        仅连接超时才判定为不可达。
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            start = time.time()
            try:
                sock.connect((host, port))
                elapsed_ms = int((time.time() - start) * 1000)
                sock.close()
                return True, f"TCP连接成功({elapsed_ms}ms)"
            except ConnectionRefusedError:
                elapsed_ms = int((time.time() - start) * 1000)
                sock.close()
                return True, f"主机可达(端口{port}拒绝,{elapsed_ms}ms)"
            except socket.timeout:
                sock.close()
                return False, "TCP超时"
            except OSError as e:
                sock.close()
                return False, f"TCP异常:{e}"
        except socket.gaierror as e:
            return False, f"DNS解析失败:{e}"
        except Exception as e:
            return False, str(e)

    def _probe(self, host):
        """执行一次 ICMP+TCP 探测，返回 (connected, error, response_time_ms)。

        内网/外网探测共用此方法，确保检测标准统一。
        """
        start_time = time.time()
        connected = False
        error = ""

        try:
            if os.name == 'nt':
                result = subprocess.run(
                    ["ping", "-n", "1", "-w", "3000", host],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
            else:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "3", host],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
            connected = result.returncode == 0
            if not connected:
                # ICMP ping失败，尝试TCP ping作为备选方案
                connected, tcp_detail = self._tcp_ping(host)
                if connected:
                    error = ""
                else:
                    icmp_err = result.stderr.strip() if result.stderr else ""
                    if not icmp_err:
                        stdout_lines = result.stdout.strip().split('\n') if result.stdout else []
                        icmp_err = stdout_lines[-1] if stdout_lines else "无输出"
                    else:
                        icmp_err = icmp_err.split('\n')[0]
                    error = f"Ping失败(ICMP:{icmp_err}; TCP:{tcp_detail})"
        except subprocess.TimeoutExpired:
            error = "Ping超时"
        except Exception as e:
            error = str(e)

        response_time = int((time.time() - start_time) * 1000)
        return connected, error, response_time

    def run(self):
        write_log("Ping监控服务已启动")
        while self.running:
            try:
                config = Config().load()
                gateway = config.gateway
                base_interval = max(1, config.interval)

                if not gateway:
                    gateway = "192.168.1.1"

                with self._config_lock:
                    probing_mode = config.probing_mode

                    if probing_mode != self.current_mode:
                        self.current_mode = probing_mode
                        if probing_mode == "adaptive":
                            self.adaptive_algorithm = ConservativeAdaptiveProbingAlgorithm()
                            levels = self.adaptive_algorithm.levels
                            max_level = len(levels) - 1
                            max_interval = levels[max_level]["interval"]
                            write_log(f"🔄 已切换到保守型自适应探测模式 - 初始间隔: 1秒, 最大间隔: {max_interval}秒")
                        else:
                            self.adaptive_algorithm = None
                            write_log(f"🔄 已切换到固定间隔探测模式 - 间隔: {base_interval}秒")

                connected, error, response_time = self._probe(gateway)

                with self._config_lock:
                    if self.current_mode == "adaptive" and self.adaptive_algorithm is not None:
                        current_interval = self.adaptive_algorithm.add_sample(response_time if connected else 5000, connected)
                        state_info = self.adaptive_algorithm.get_state_info()
                        interval = int(current_interval)

                        level = state_info["level"]
                        progress = state_info["level_progress_pct"]
                        remaining = state_info["remaining_in_level"]

                        global probing_state_info
                        probing_state_info = {
                            "mode": "adaptive",
                            "level": level,
                            "level_name": f"L{level}",
                            "current_interval": interval,
                            "level_probe_count": state_info["level_probe_count"],
                            "level_total_count": state_info["level_total_count"],
                            "level_progress_pct": progress,
                            "remaining_in_level": remaining,
                            "total_levels": state_info["total_levels"],
                            "consecutive_success": state_info["consecutive_success"],
                            "consecutive_loss": state_info["consecutive_loss"],
                            "is_max_level": state_info["is_max_level"],
                            "total_probes": state_info["total_probes"],
                            "level_changes": state_info["level_changes"]
                        }

                        if connected:
                            log_msg = (f"✅ Ping {gateway} 成功 - 响应时间: {response_time}ms - "
                                     f"等级: L{level} - 间隔: {interval}秒 - "
                                     f"进度: {progress:.0f}% - 本等级剩余: {remaining}次")
                        else:
                            log_msg = (f"❌ Ping {gateway} 失败 - {error} - "
                                     f"已重置为初始等级 L0 - 间隔: 1秒 - 30次探测")
                        write_log(log_msg)
                    else:
                        interval = base_interval
                        probing_state_info = {
                            "mode": "fixed",
                            "current_interval": interval
                        }
                        if connected:
                            write_log(f"✅ Ping {gateway} 成功 - 响应时间: {response_time}ms - 固定间隔: {base_interval}秒")
                        else:
                            write_log(f"❌ Ping {gateway} 失败 - {error} - 固定间隔: {base_interval}秒")

                global last_status, ping_history, ping_stats
                last_status = {
                    "connected": connected,
                    "gateway": gateway,
                    "response_time": response_time,
                    "error": error
                }

                if connected:
                    ping_history.append(response_time)
                    if len(ping_history) > 10:
                        ping_history.pop(0)
                    ping_stats = {
                        "count": len(ping_history),
                        "min": min(ping_history) if ping_history else 0,
                        "max": max(ping_history) if ping_history else 0,
                        "avg": sum(ping_history) // len(ping_history) if ping_history else 0
                    }

                time.sleep(max(1, interval))
            except Exception as e:
                write_log(f"⚠️ Ping线程异常: {str(e)}")
                time.sleep(5)


class WanPingThread(threading.Thread):
    """外网 Ping 监控线程，与内网 PingThread 共用 _probe 探测逻辑和 probing_mode 配置。

    通过 wan_ping_enabled 配置开关控制启停。未启用时空转等待，启用后开始探测 wanHost。
    """

    def __init__(self):
        super().__init__()
        self.daemon = True
        self.running = True
        self.adaptive_algorithm = None
        self.current_mode = "fixed"
        self.last_base_interval = 30
        self._config_lock = threading.Lock()
        self._total_probes = 0
        self._failed_probes = 0
        self._host_idx = 0          # 地址池轮流索引（round-robin）
        self._pool_size = 0

    def _tcp_ping(self, host, port=80, timeout=3):
        """TCP 连接探测（与 PingThread._tcp_ping 逻辑一致）。"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            start = time.time()
            try:
                sock.connect((host, port))
                elapsed_ms = int((time.time() - start) * 1000)
                sock.close()
                return True, f"TCP连接成功({elapsed_ms}ms)"
            except ConnectionRefusedError:
                elapsed_ms = int((time.time() - start) * 1000)
                sock.close()
                return True, f"主机可达(端口{port}拒绝,{elapsed_ms}ms)"
            except socket.timeout:
                sock.close()
                return False, "TCP超时"
            except OSError as e:
                sock.close()
                return False, f"TCP异常:{e}"
        except socket.gaierror as e:
            return False, f"DNS解析失败:{e}"
        except Exception as e:
            return False, str(e)

    def _probe(self, host):
        """执行一次 ICMP+TCP 探测，返回 (connected, error, response_time_ms)。"""
        start_time = time.time()
        connected = False
        error = ""
        try:
            if os.name == 'nt':
                result = subprocess.run(
                    ["ping", "-n", "1", "-w", "3000", host],
                    capture_output=True, text=True, timeout=5)
            else:
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "3", host],
                    capture_output=True, text=True, timeout=5)
            connected = result.returncode == 0
            if not connected:
                connected, tcp_detail = self._tcp_ping(host)
                if connected:
                    error = ""
                else:
                    icmp_err = result.stderr.strip() if result.stderr else ""
                    if not icmp_err:
                        stdout_lines = result.stdout.strip().split('\n') if result.stdout else []
                        icmp_err = stdout_lines[-1] if stdout_lines else "无输出"
                    else:
                        icmp_err = icmp_err.split('\n')[0]
                    error = f"Ping失败(ICMP:{icmp_err}; TCP:{tcp_detail})"
        except subprocess.TimeoutExpired:
            error = "Ping超时"
        except Exception as e:
            error = str(e)
        response_time = int((time.time() - start_time) * 1000)
        return connected, error, response_time

    def run(self):
        global wan_last_status, wan_ping_history, wan_ping_stats, wan_probing_state_info
        write_log("外网Ping监控线程已启动（等待启用）")
        while self.running:
            try:
                config = Config().load()
                enabled = config.wan_ping_enabled
                hosts = [h for h in config.wan_hosts if h]

                # 未启用或地址池为空：更新状态后空转等待
                if not enabled or not hosts:
                    wan_last_status = {
                        "connected": False, "host": "", "hosts": hosts,
                        "current_host": "", "response_time": 0,
                        "error": "", "enabled": bool(enabled)
                    }
                    wan_probing_state_info = {"mode": config.probing_mode, "enabled": False}
                    time.sleep(5)
                    continue

                # 地址池变化时重置轮询索引，保证从第一个地址开始
                if len(hosts) != self._pool_size:
                    self._pool_size = len(hosts)
                    self._host_idx = 0

                # 轮流选取当前探测地址（round-robin），避免对单一网址请求过频
                wan_host = hosts[self._host_idx % len(hosts)]
                self._host_idx += 1

                base_interval = max(1, config.interval)

                with self._config_lock:
                    probing_mode = config.probing_mode
                    if probing_mode != self.current_mode:
                        self.current_mode = probing_mode
                        if probing_mode == "adaptive":
                            self.adaptive_algorithm = ConservativeAdaptiveProbingAlgorithm()
                            write_log("🔄 外网探测：已切换到自适应模式")
                        else:
                            self.adaptive_algorithm = None
                            write_log(f"🔄 外网探测：已切换到固定间隔模式 - 间隔: {base_interval}秒")

                connected, error, response_time = self._probe(wan_host)
                self._total_probes += 1
                if not connected:
                    self._failed_probes += 1
                loss_rate = round(self._failed_probes / self._total_probes * 100, 1) if self._total_probes else 0

                with self._config_lock:
                    if self.current_mode == "adaptive" and self.adaptive_algorithm is not None:
                        current_interval = self.adaptive_algorithm.add_sample(response_time if connected else 5000, connected)
                        state_info = self.adaptive_algorithm.get_state_info()
                        interval = int(current_interval)
                        wan_probing_state_info = {
                            "mode": "adaptive",
                            "enabled": True,
                            "level": state_info["level"],
                            "current_interval": interval,
                            "level_progress_pct": state_info["level_progress_pct"],
                            "remaining_in_level": state_info["remaining_in_level"],
                            "consecutive_success": state_info["consecutive_success"],
                            "consecutive_loss": state_info["consecutive_loss"],
                            "total_probes": state_info["total_probes"],
                        }
                    else:
                        interval = base_interval
                        wan_probing_state_info = {"mode": "fixed", "enabled": True, "current_interval": interval}

                    if connected:
                        cycle_pos = self._host_idx % len(hosts)
                        write_log(f"🌐 WAN Ping {wan_host} 成功 - {response_time}ms - 间隔: {interval}秒（轮询 {cycle_pos + 1}/{len(hosts)}）")
                    else:
                        write_log(f"🌐 WAN Ping {wan_host} 失败 - {error} - 间隔: {interval}秒（轮询 {cycle_pos + 1}/{len(hosts)}）")

                wan_last_status = {
                    "connected": connected,
                    "host": wan_host,
                    "hosts": hosts,
                    "current_host": wan_host,
                    "response_time": response_time,
                    "error": error,
                    "enabled": True
                }

                if connected:
                    wan_ping_history.append(response_time)
                    if len(wan_ping_history) > 10:
                        wan_ping_history.pop(0)
                else:
                    # 失败时也记录一条，前端用 0 表示失败
                    wan_ping_history.append(0)
                    if len(wan_ping_history) > 10:
                        wan_ping_history.pop(0)

                success_times = [t for t in wan_ping_history if t > 0]
                wan_ping_stats = {
                    "count": len(wan_ping_history),
                    "min": min(success_times) if success_times else 0,
                    "max": max(success_times) if success_times else 0,
                    "avg": sum(success_times) // len(success_times) if success_times else 0,
                    "loss_rate": loss_rate
                }

                time.sleep(max(1, interval))
            except Exception as e:
                write_log(f"⚠️ 外网Ping线程异常: {str(e)}")
                time.sleep(5)


# ========== 配置类 ==========

class Config:
    """基础配置（探测间隔、网关地址、防抖、调试模式、探测模式）。

    网关地址拆分为：
      - lan_gateway：内网网关（未手动设置时自动获取局域网默认网关）
      - wan_host：外网地址（下拉菜单+输入框，预置国内知名网站，可自定义）
    """

    def __init__(self):
        self.interval = 10
        self.gateway = "192.168.1.99"     # 兼容旧字段：内网网关（lan_gateway 别名）
        self.lan_gateway = ""
        self.wan_host = ""
        self.wan_hosts = []                # 外网轮询地址池（用户勾选的多个地址）
        self.wan_ping_enabled = False      # 外网多线程 ping 探测开关
        self.debounce_time = 10
        self.debug = True
        self.probing_mode = "fixed"

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.interval = data.get('interval', 10)
                    self.gateway = data.get('gateway', "192.168.1.99")
                    self.lan_gateway = data.get('lanGateway', data.get('lan_gateway', ''))
                    self.wan_host = data.get('wanHost', data.get('wan_host', ''))
                    self.wan_ping_enabled = data.get('wanPingEnabled', data.get('wan_ping_enabled', False))
                    self.debounce_time = data.get('debounceTime', 10)
                    self.debug = data.get('debug', True)
                    self.probing_mode = data.get('probingMode', "fixed")
                    # 外网轮询地址池：优先 wanHosts 数组；缺失时回退到单个 wanHost
                    raw_hosts = data.get('wanHosts', data.get('wan_hosts', []))
                    if isinstance(raw_hosts, list):
                        self.wan_hosts = [h for h in raw_hosts if h and isinstance(h, str)]
                    else:
                        self.wan_hosts = []
                    if not self.wan_hosts and self.wan_host:
                        self.wan_hosts = [self.wan_host]
                    # 兼容：旧配置仅有 gateway 时，作为内网网关
                    if not self.lan_gateway and self.gateway:
                        self.lan_gateway = self.gateway
            except Exception as e:
                print(f"Error loading config: {e}")
        return self

    def save(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'interval': self.interval,
                    'gateway': self.gateway,
                    'lanGateway': self.lan_gateway,
                    'wanHost': self.wan_host,
                    'wanHosts': self.wan_hosts,
                    'wanPingEnabled': self.wan_ping_enabled,
                    'debounceTime': self.debounce_time,
                    'debug': self.debug,
                    'probingMode': self.probing_mode
                }, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving config: {e}")
            return False


def detect_default_gateway():
    """自动获取当前局域网默认网关地址。

    优先使用 `ip route`（Linux/fnOS），失败时回退到 `route -n` / netstat。
    返回网关 IP 字符串；无法获取时返回空字符串。
    """
    candidates = []
    try:
        # Linux: ip route show default
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                # 形如: default via 192.168.1.1 dev eth0 proto dhcp
                parts = line.split()
                if len(parts) >= 3 and parts[0] == 'default' and parts[1] == 'via':
                    candidates.append(parts[2])
    except Exception:
        pass

    if not candidates:
        try:
            # 回退: route -n
            result = subprocess.run(
                ["route", "-n"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == '0.0.0.0':
                        candidates.append(parts[1])
        except Exception:
            pass

    for c in candidates:
        c = c.strip()
        if c and c != '0.0.0.0':
            return c
    return ""


class AdvancedConfig:
    """高级配置（停止应用列表、Docker 容器列表、应用关闭/启动顺序）。"""

    def __init__(self):
        self.stop_apps = []
        self.docker_containers = []
        # 关闭顺序：[{name, type}]，type 为 'app' 或 'docker'
        self.shutdown_order = []
        # 启动顺序：默认为关闭顺序的逆序，但可由用户独立手动调整
        self.startup_order = []

    def load(self):
        if os.path.exists(ADVANCED_CONFIG_FILE):
            try:
                with open(ADVANCED_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.stop_apps = data.get('stopApps', [])
                    self.docker_containers = data.get('dockerContainers', [])
                    self.shutdown_order = data.get('shutdownOrder', [])
                    self.startup_order = data.get('startupOrder', [])
            except Exception as e:
                print(f"Error loading advanced config: {e}")
        return self

    def save(self):
        try:
            with open(ADVANCED_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'stopApps': self.stop_apps,
                    'dockerContainers': self.docker_containers,
                    'shutdownOrder': self.shutdown_order,
                    'startupOrder': self.startup_order
                }, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving advanced config: {e}")
            return False


class IotConfig:
    """IoT 平台配置（阿里云等）。"""

    def __init__(self):
        self.platform = "aliyun"
        self.device_name = ""
        self.product_key = ""
        self.device_secret = ""
        self.region = "cn-shanghai"
        self.enabled = False

    def load(self):
        if os.path.exists(IOT_CONFIG_FILE):
            try:
                with open(IOT_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.platform = data.get('platform', "aliyun")
                    self.device_name = data.get('deviceName', "")
                    self.product_key = data.get('productKey', "")
                    self.device_secret = data.get('deviceSecret', "")
                    self.region = data.get('region', "cn-shanghai")
                    self.enabled = data.get('enabled', False)
            except Exception as e:
                print(f"Error loading IoT config: {e}")
        return self

    def save(self):
        try:
            with open(IOT_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'platform': self.platform,
                    'deviceName': self.device_name,
                    'productKey': self.product_key,
                    'deviceSecret': self.device_secret,
                    'region': self.region,
                    'enabled': self.enabled
                }, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving IoT config: {e}")
            return False


class NotificationConfig:
    """通知渠道配置。"""

    def __init__(self):
        self.channels = []

    def load(self):
        if os.path.exists(NOTIFICATION_CONFIG_FILE):
            try:
                with open(NOTIFICATION_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.channels = data.get('channels', [])
            except Exception as e:
                print(f"Error loading notification config: {e}")
        return self

    def save(self):
        try:
            with open(NOTIFICATION_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'channels': self.channels
                }, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving notification config: {e}")
            return False


# ========== 网关兼容混入 ==========

class GatewayMixin:
    """fnOS 网关兼容能力，解决 Unix socket 下两个已知问题：

    1. address_string 崩溃：Unix socket 的 client_address 是空字符串，
       默认 self.client_address[0] 取首字符会抛 IndexError，
       导致 send_error/log_message 失败，网关收不到有效响应返回 502。
    2. 路径前缀：网关转发请求时路径带 gatewayPrefix（如 /app/FreePower），
       需要去掉前缀才能正确路由到业务 API 和静态文件。
    """

    gateway_prefix = "/app/FreePower"

    def address_string(self):
        """修复 Unix socket 下 client_address 为空字符串导致的 IndexError。"""
        try:
            if isinstance(self.client_address, str):
                return "unix"
            return self.client_address[0] or "unix"
        except (IndexError, TypeError):
            return "unix"

    def strip_gateway_prefix(self, path):
        """去掉网关前缀（如 /app/FreePower），返回业务路径。"""
        prefix = self.gateway_prefix
        if path.startswith(prefix):
            path = path[len(prefix):]
            if not path.startswith('/'):
                path = '/' + path
        return path


# ========== 请求处理器 ==========

class RequestHandler(GatewayMixin, BaseHTTPRequestHandler):
    """处理业务 API + 静态文件的 HTTP 请求，兼容 fnOS 网关。"""

    static_dir = STATIC_DIR

    MIME_TYPES = {
        '.html': 'text/html',
        '.htm': 'text/html',
        '.css': 'text/css',
        '.js': 'application/javascript',
        '.json': 'application/json',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.svg': 'image/svg+xml',
        '.ico': 'image/x-icon',
        '.webp': 'image/webp',
        '.txt': 'text/plain',
        '.md': 'text/markdown',
        '.pdf': 'application/pdf',
        '.zip': 'application/zip',
        '.gz': 'application/gzip',
        '.tar': 'application/x-tar',
        '.woff': 'font/woff',
        '.woff2': 'font/woff2',
        '.ttf': 'font/ttf',
        '.otf': 'font/otf',
        '.eot': 'application/vnd.ms-fontobject',
        '.xml': 'text/xml',
        '.csv': 'text/csv'
    }

    # --- 安全写入辅助（处理客户端断连） ---

    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, BrokenPipeError, socket.timeout, OSError):
            pass
        except Exception as e:
            print(f"Handle error: {e}")

    def safe_send_response(self, code):
        try:
            self.send_response(code)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def safe_send_header(self, keyword, value):
        try:
            self.send_header(keyword, value)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def safe_end_headers(self):
        try:
            self.end_headers()
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def safe_write(self, data):
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def send_json_response(self, status_code, data):
        """统一的 JSON 响应方法。"""
        try:
            self.safe_send_response(status_code)
            self.safe_send_header('Content-Type', 'application/json')
            self.safe_send_header('Access-Control-Allow-Origin', '*')
            self.safe_end_headers()
            self.safe_write(json.dumps(data).encode('utf-8'))
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    # --- CORS 预检 ---

    def do_OPTIONS(self):
        try:
            self.safe_send_response(200)
            self.safe_send_header('Access-Control-Allow-Origin', '*')
            self.safe_send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
            self.safe_send_header('Access-Control-Allow-Headers', 'Origin, Content-Type, Accept, Authorization')
            self.safe_send_header('Access-Control-Max-Age', '86400')
            self.safe_end_headers()
        except Exception:
            pass

    # --- 路由分发 ---

    def do_GET(self):
        parsed_url = urlparse(self.path)
        original_path = parsed_url.path
        # 去掉网关前缀（如 /app/FreePower），兼容直连和网关两种访问方式
        path = self.strip_gateway_prefix(original_path)

        # 根路径无尾部斜杠时301重定向，确保前端相对路径（./api/...）能正确解析。
        # 浏览器将无尾部斜杠的URL（如 /app/FreePower）视为"文件"，
        # 相对路径 ./api/config 会替换文件名变为 /app/api/config 而非 /app/FreePower/api/config。
        # 301 到 /app/FreePower/ 后浏览器将 /app/FreePower/ 视为"目录"，相对路径正确追加。
        if path == '/' and not original_path.endswith('/'):
            self.send_response(301)
            self.send_header('Location', original_path + '/')
            self.end_headers()
            return

        if path == '/health':
            self.handle_health()
        elif path == '/api/config':
            self.handle_get_config()
        elif path == '/api/advanced-config':
            self.handle_get_advanced_config()
        elif path == '/api/system-apps':
            self.handle_get_system_apps()
        elif path == '/api/docker-containers':
            self.handle_get_docker_containers_api()
        elif path == '/api/iot-config':
            self.handle_get_iot_config()
        elif path == '/api/notification-config':
            self.handle_get_notification_config()
        elif path == '/api/status':
            self.handle_get_status()
        elif path == '/api/wan-status':
            self.handle_get_wan_status()
        elif path == '/api/logs':
            self.handle_get_logs()
        else:
            self.serve_static_file(path)

    def do_POST(self):
        parsed_url = urlparse(self.path)
        path = self.strip_gateway_prefix(parsed_url.path)

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)

        if path == '/api/config':
            self.handle_post_config(post_data)
        elif path == '/api/advanced-config':
            self.handle_post_advanced_config(post_data)
        elif path == '/api/iot-config':
            self.handle_post_iot_config(post_data)
        elif path == '/api/notification-config':
            self.handle_post_notification_config(post_data)
        elif path == '/api/app-control':
            self.handle_app_control(post_data)
        elif path == '/api/execute-shutdown':
            self.handle_execute_shutdown(post_data)
        elif path == '/api/execute-startup':
            self.handle_execute_startup(post_data)
        else:
            self.send_error(404, "Not Found")

    # --- 静态文件服务 ---

    def serve_static_file(self, path):
        content = None
        content_type = None
        full_path = None
        status = 200
        error_msg = None

        try:
            decoded_path = unquote(path.lstrip('/'))
            if decoded_path == '':
                decoded_path = 'index.html'

            static_dir = os.path.normpath(self.static_dir)
            full_path = os.path.normpath(os.path.join(static_dir, decoded_path))

            if not full_path.startswith(static_dir):
                status = 403
                error_msg = "Forbidden"
            elif not os.path.exists(full_path):
                status = 404
                error_msg = "File not found"
            elif os.path.isdir(full_path):
                index_path = os.path.join(full_path, 'index.html')
                if os.path.exists(index_path):
                    full_path = index_path
                else:
                    status = 403
                    error_msg = "Directory listing not allowed"

            if status == 200:
                file_ext = os.path.splitext(full_path)[1].lower()
                content_type = self.MIME_TYPES.get(file_ext, 'application/octet-stream')

                with open(full_path, 'rb') as f:
                    content = f.read()
        except Exception as e:
            print(f"Error serving static file: {e}")
            status = 500
            error_msg = "Internal Server Error"

        try:
            if status == 200 and content is not None:
                self.safe_send_response(200)
                self.safe_send_header('Content-Type', content_type)
                self.safe_send_header('Access-Control-Allow-Origin', '*')
                self.safe_send_header('Content-Length', len(content))
                if file_ext in ('.html', '.htm'):
                    # HTML 不缓存，确保开发者/用户始终加载到最新页面，避免看到旧版界面
                    self.safe_send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                else:
                    # 其他静态资源配合前端版本号参数（?v=）刷新缓存
                    self.safe_send_header('Cache-Control', 'public, max-age=3600')
                self.safe_send_header('Last-Modified', time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(os.path.getmtime(full_path))))
                self.safe_end_headers()
                self.safe_write(content)
            else:
                self.send_error(status, error_msg)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    # --- API: 健康检查 ---

    def handle_health(self):
        try:
            self.safe_send_response(200)
            self.safe_send_header('Content-Type', 'application/json')
            self.safe_send_header('Access-Control-Allow-Origin', '*')
            self.safe_end_headers()
            self.safe_write(json.dumps({
                'success': True,
                'data': 'Service is healthy'
            }).encode('utf-8'))
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    # --- API: 基础配置 ---

    def handle_get_config(self):
        try:
            config = Config().load()
            self.safe_send_response(200)
            self.safe_send_header('Content-Type', 'application/json')
            self.safe_send_header('Access-Control-Allow-Origin', '*')
            self.safe_end_headers()
            self.safe_write(json.dumps({
                'success': True,
                'data': {
                    'interval': config.interval,
                    'gateway': config.gateway,
                    'lanGateway': config.lan_gateway,
                    'wanHost': config.wan_host,
                    'wanHosts': config.wan_hosts,
                    'wanPingEnabled': config.wan_ping_enabled,
                    'defaultGateway': detect_default_gateway(),
                    'debounceTime': config.debounce_time,
                    'debug': config.debug,
                    'probingMode': config.probing_mode
                }
            }).encode('utf-8'))
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def handle_post_config(self, post_data):
        try:
            data = json.loads(post_data)
            config = Config()
            config.interval = data.get('interval', 10)
            config.gateway = data.get('gateway', "192.168.1.99")
            config.lan_gateway = data.get('lanGateway', data.get('lan_gateway', ''))
            config.wan_host = data.get('wanHost', data.get('wan_host', ''))
            config.wan_ping_enabled = data.get('wanPingEnabled', data.get('wan_ping_enabled', False))
            config.debounce_time = data.get('debounceTime', 10)
            config.debug = data.get('debug', True)
            config.probing_mode = data.get('probingMode', 'fixed')

            # 外网轮询地址池：优先 wanHosts 数组；缺失时回退到单个 wanHost
            raw_hosts = data.get('wanHosts', data.get('wan_hosts', []))
            if isinstance(raw_hosts, list):
                config.wan_hosts = [h for h in raw_hosts if h and isinstance(h, str)]
            else:
                config.wan_hosts = []
            if not config.wan_hosts and config.wan_host:
                config.wan_hosts = [config.wan_host]

            max_interval_seconds = 24 * 3600
            if config.interval <= 0 or config.interval > max_interval_seconds:
                self.send_json_response(400, {
                    'success': False,
                    'error': f'Interval must be between 1 and {max_interval_seconds} seconds (24 hours)'
                })
                return

            # 内网网关：未手动设置时自动获取局域网默认网关
            if not config.lan_gateway:
                config.lan_gateway = detect_default_gateway()
            if not config.lan_gateway and not config.gateway:
                self.send_json_response(400, {
                    'success': False,
                    'error': 'LAN gateway cannot be empty'
                })
                return
            # 同步 gateway 兼容字段
            if config.gateway != config.lan_gateway:
                config.gateway = config.lan_gateway or config.gateway

            if config.debounce_time <= 0:
                self.send_json_response(400, {
                    'success': False,
                    'error': 'DebounceTime must be greater than 0'
                })
                return

            if config.probing_mode not in ['fixed', 'adaptive']:
                self.send_json_response(400, {
                    'success': False,
                    'error': 'Invalid probing mode'
                })
                return

            if config.save():
                # 立即同步外网探测启停状态，避免前端轮询读到旧值导致按钮状态回跳
                global wan_last_status
                wan_last_status["enabled"] = bool(config.wan_ping_enabled)
                wan_last_status["hosts"] = list(config.wan_hosts)
                wan_last_status["current_host"] = config.wan_hosts[0] if config.wan_hosts else ""
                if not config.wan_ping_enabled:
                    wan_last_status["host"] = ""
                self.send_json_response(200, {
                    'success': True,
                    'data': {
                        'interval': config.interval,
                        'gateway': config.gateway,
                        'lanGateway': config.lan_gateway,
                        'wanHost': config.wan_host,
                        'wanHosts': config.wan_hosts,
                        'wanPingEnabled': config.wan_ping_enabled,
                        'debounceTime': config.debounce_time,
                        'debug': config.debug,
                        'probingMode': config.probing_mode
                    }
                })
            else:
                self.send_json_response(500, {
                    'success': False,
                    'error': 'Failed to save config'
                })
        except Exception as e:
            self.send_json_response(400, {
                'success': False,
                'error': 'Invalid request data'
            })

    # --- API: 高级配置 ---

    def handle_get_advanced_config(self):
        config = AdvancedConfig().load()
        self.send_json_response(200, {
            'success': True,
            'data': {
                'stopApps': config.stop_apps,
                'dockerContainers': config.docker_containers,
                'shutdownOrder': config.shutdown_order,
                'startupOrder': config.startup_order
            }
        })

    def handle_post_advanced_config(self, post_data):
        try:
            data = json.loads(post_data)
            config = AdvancedConfig()
            config.stop_apps = data.get('stopApps', [])
            config.docker_containers = data.get('dockerContainers', [])
            config.shutdown_order = data.get('shutdownOrder', [])
            config.startup_order = data.get('startupOrder', [])

            if config.save():
                self.send_json_response(200, {
                    'success': True,
                    'data': {
                        'stopApps': config.stop_apps,
                        'dockerContainers': config.docker_containers,
                        'shutdownOrder': config.shutdown_order,
                        'startupOrder': config.startup_order
                    }
                })
            else:
                self.send_json_response(500, {
                    'success': False,
                    'error': 'Failed to save config'
                })
        except Exception as e:
            self.send_json_response(400, {
                'success': False,
                'error': 'Invalid request data'
            })

    # --- API: IoT 配置 ---

    def handle_get_iot_config(self):
        config = IotConfig().load()
        self.send_json_response(200, {
            'success': True,
            'data': {
                'platform': config.platform,
                'deviceName': config.device_name,
                'productKey': config.product_key,
                'deviceSecret': config.device_secret,
                'region': config.region,
                'enabled': config.enabled
            }
        })

    def handle_post_iot_config(self, post_data):
        try:
            data = json.loads(post_data)
            config = IotConfig()
            config.platform = data.get('platform', "aliyun")
            config.device_name = data.get('deviceName', "")
            config.product_key = data.get('productKey', "")
            config.device_secret = data.get('deviceSecret', "")
            config.region = data.get('region', "cn-shanghai")
            config.enabled = data.get('enabled', False)

            if config.save():
                self.send_json_response(200, {
                    'success': True,
                    'data': {
                        'platform': config.platform,
                        'deviceName': config.device_name,
                        'productKey': config.product_key,
                        'deviceSecret': config.device_secret,
                        'region': config.region,
                        'enabled': config.enabled
                    }
                })
            else:
                self.send_json_response(500, {
                    'success': False,
                    'error': 'Failed to save config'
                })
        except Exception as e:
            self.send_json_response(400, {
                'success': False,
                'error': 'Invalid request data'
            })

    # --- API: 飞牛OS 已安装应用列表 ---

    def handle_get_system_apps(self):
        """从飞牛OS系统中获取已安装的应用列表（通过 root 特权助手）。

        遵循飞牛 OS 权限规范：Web 服务以包用户运行，读取应用列表需要 root
        权限，因此通过 PrivilegeClient 向 root helper 临时申请（helper 内部
        使用 appcenter-cli list 命令获取）。无 helper（本地开发/非 fnOS）时
        返回空列表 + fallback 提示。
        """
        if not PRIV_HELPER_SOCKET:
            write_log("[api] GET /api/system-apps → 200 fallback (privilege helper not configured)")
            self.send_json_response(200, {
                'success': True,
                'data': {
                    'apps': [],
                    'count': 0,
                    'source': 'fallback',
                    'hint': 'privilege helper not configured (not on fnOS)'
                }
            })
            return
        try:
            with PrivilegeClient() as priv:
                data = priv.execute('get_apps', {'what': 'apps'})
        except PrivilegeError as e:
            write_log(f"[api] GET /api/system-apps → 503 privilege_error: {e}")
            self.send_json_response(503, {
                'success': False,
                'error': f'root privilege helper unavailable: {e}'
            })
            return
        apps = data.get('apps', []) if isinstance(data, dict) else []
        source = data.get('source', 'apps_dir') if isinstance(data, dict) else 'apps_dir'
        excluded = data.get('excluded', []) if isinstance(data, dict) else []
        write_log(f"[api] GET /api/system-apps → 200 source={source} count={len(apps)} excluded={len(excluded)}")
        self.send_json_response(200, {
            'success': True,
            'data': {
                'apps': apps,
                'source': source,
                'count': len(apps),
                'excluded': list(excluded)
            }
        })

    # --- API: Docker 容器列表（通过 root 特权助手） ---

    def handle_get_docker_containers_api(self):
        """通过 root 特权助手获取系统中的容器列表（含运行/停止两种状态）。

        包用户无权访问 docker 守护进程，因此通过 PrivilegeClient 向 root
        helper 申请。无 helper 时返回空列表 + fallback 提示。
        """
        if not PRIV_HELPER_SOCKET:
            write_log("[api] GET /api/docker-containers → 200 fallback (privilege helper not configured)")
            self.send_json_response(200, {
                'success': True,
                'data': {
                    'containers': [],
                    'count': 0,
                    'source': 'fallback',
                    'hint': 'privilege helper not configured (not on fnOS)'
                }
            })
            return
        try:
            with PrivilegeClient() as priv:
                data = priv.execute('get_apps', {'what': 'containers'})
        except PrivilegeError as e:
            write_log(f"[api] GET /api/docker-containers → 503 privilege_error: {e}")
            self.send_json_response(503, {
                'success': False,
                'error': f'root privilege helper unavailable: {e}'
            })
            return
        containers = data.get('containers', []) if isinstance(data, dict) else []
        write_log(f"[api] GET /api/docker-containers → 200 count={len(containers)}")
        self.send_json_response(200, {
            'success': True,
            'data': {
                'containers': containers,
                'count': len(containers)
            }
        })

    # --- API: 单目标启停（通过 root 特权助手） ---

    def handle_app_control(self, post_data):
        """启动/停止单个应用或容器。body: {action, type, name}"""
        try:
            data = json.loads(post_data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            write_log("[api] POST /api/app-control → 400 invalid_json")
            self.send_json_response(400, {'success': False, 'error': 'invalid json body'})
            return
        action = data.get('action')
        t = data.get('type')
        name = (data.get('name') or '').strip()
        if action not in ('start', 'stop') or t not in ('app', 'docker') or not name:
            write_log(f"[api] POST /api/app-control → 400 invalid_params action={action} type={t} name={name!r}")
            self.send_json_response(400, {'success': False, 'error': 'invalid request: need action, type, name'})
            return
        try:
            with PrivilegeClient() as priv:
                result = priv.execute(action, {'type': t, 'name': name})
        except PrivilegeError as e:
            write_log(f"[api] POST /api/app-control → 503 action={action} type={t} name={name} privilege_error: {e}")
            self.send_json_response(503, {'success': False, 'error': f'root privilege helper unavailable: {e}'})
            return
        except PrivilegeOperationError as e:
            write_log(f"[api] POST /api/app-control → 200 success=false action={action} type={t} name={name} error: {e}")
            self.send_json_response(200, {'success': False, 'error': str(e)})
            return
        write_log(f"[api] POST /api/app-control → 200 success=true action={action} type={t} name={name}")
        self.send_json_response(200, {'success': True, 'data': result})

    # --- API: 批量执行关闭顺序（通过 root 特权助手） ---

    def handle_execute_shutdown(self, post_data):
        """按 shutdownOrder 顺序停止全部配置项，聚合每项结果。"""
        cfg = AdvancedConfig().load()
        order = cfg.shutdown_order or []
        results = []
        if not PRIV_HELPER_SOCKET:
            write_log("[api] POST /api/execute-shutdown → 503 (privilege helper not configured)")
            self.send_json_response(503, {'success': False, 'error': 'root privilege helper unavailable (not on fnOS)'})
            return
        write_log(f"[api] POST /api/execute-shutdown items={len(order)}")
        try:
            with PrivilegeClient() as priv:
                for item in order:
                    name = item.get('name', '')
                    t = item.get('type', 'app')
                    if not name:
                        continue
                    try:
                        r = priv.execute('stop', {'type': t, 'name': name})
                        results.append({'name': name, 'type': t, 'success': True, 'message': r.get('message', '')})
                    except PrivilegeOperationError as e:
                        results.append({'name': name, 'type': t, 'success': False, 'error': str(e)})
        except PrivilegeError as e:
            write_log(f"[api] POST /api/execute-shutdown → 503 privilege_error: {e}")
            self.send_json_response(503, {'success': False, 'error': f'root privilege helper unavailable: {e}'})
            return
        ok_count = sum(1 for r in results if r.get('success'))
        write_log(f"[api] POST /api/execute-shutdown → 200 total={len(results)} ok={ok_count} failed={len(results) - ok_count}")
        self.send_json_response(200, {'success': True, 'data': {'results': results}})

    # --- API: 批量执行启动顺序（通过 root 特权助手） ---

    def handle_execute_startup(self, post_data):
        """按 startupOrder 顺序启动全部配置项，聚合每项结果。"""
        cfg = AdvancedConfig().load()
        order = cfg.startup_order or []
        results = []
        if not PRIV_HELPER_SOCKET:
            write_log("[api] POST /api/execute-startup → 503 (privilege helper not configured)")
            self.send_json_response(503, {'success': False, 'error': 'root privilege helper unavailable (not on fnOS)'})
            return
        write_log(f"[api] POST /api/execute-startup items={len(order)}")
        try:
            with PrivilegeClient() as priv:
                for item in order:
                    name = item.get('name', '')
                    t = item.get('type', 'app')
                    if not name:
                        continue
                    try:
                        r = priv.execute('start', {'type': t, 'name': name})
                        results.append({'name': name, 'type': t, 'success': True, 'message': r.get('message', '')})
                    except PrivilegeOperationError as e:
                        results.append({'name': name, 'type': t, 'success': False, 'error': str(e)})
        except PrivilegeError as e:
            write_log(f"[api] POST /api/execute-startup → 503 privilege_error: {e}")
            self.send_json_response(503, {'success': False, 'error': f'root privilege helper unavailable: {e}'})
            return
        ok_count = sum(1 for r in results if r.get('success'))
        write_log(f"[api] POST /api/execute-startup → 200 total={len(results)} ok={ok_count} failed={len(results) - ok_count}")
        self.send_json_response(200, {'success': True, 'data': {'results': results}})

    # --- API: 通知配置 ---

    def handle_get_notification_config(self):
        config = NotificationConfig().load()
        self.send_json_response(200, {
            'success': True,
            'data': {
                'channels': config.channels
            }
        })

    def handle_post_notification_config(self, post_data):
        try:
            data = json.loads(post_data)
            config = NotificationConfig()
            config.channels = data.get('channels', [])

            if config.save():
                self.send_json_response(200, {
                    'success': True,
                    'data': {
                        'channels': config.channels
                    }
                })
            else:
                self.send_json_response(500, {
                    'success': False,
                    'error': 'Failed to save config'
                })
        except Exception as e:
            self.send_json_response(400, {
                'success': False,
                'error': 'Invalid request data'
            })

    # --- API: 状态查询 ---

    def handle_get_status(self):
        global last_status, ping_stats, probing_state_info, service_start_time
        if not last_status["gateway"]:
            config = Config().load()
            last_status["gateway"] = config.gateway if config.gateway else "192.168.1.1"

        current_time = time.time()
        uptime_seconds = int(current_time - service_start_time)

        self.send_json_response(200, {
            'success': True,
            'data': {
                "connected": last_status["connected"],
                "gateway": last_status["gateway"],
                "response_time": last_status["response_time"],
                "error": last_status["error"],
                "stats": ping_stats,
                "probing": probing_state_info,
                "uptime": uptime_seconds
            }
        })

    # --- API: 外网状态查询 ---

    def handle_get_wan_status(self):
        global wan_last_status, wan_ping_stats, wan_probing_state_info, wan_ping_history
        self.send_json_response(200, {
            'success': True,
            'data': {
                "connected": wan_last_status.get("connected", False),
                "host": wan_last_status.get("host", ""),
                "hosts": wan_last_status.get("hosts", []),
                "current_host": wan_last_status.get("current_host", wan_last_status.get("host", "")),
                "response_time": wan_last_status.get("response_time", 0),
                "error": wan_last_status.get("error", ""),
                "enabled": wan_last_status.get("enabled", False),
                "stats": wan_ping_stats,
                "probing": wan_probing_state_info,
                "history": list(wan_ping_history)
            }
        })

    # --- API: 日志查询 ---

    def handle_get_logs(self):
        log_content = ""

        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                lines = [line.rstrip('\n') for line in lines if line.strip()]
                lines.reverse()
                lines = lines[:50]
                log_content = '\n'.join(lines)
            except Exception as e:
                log_content = f"读取日志失败: {str(e)}"
        else:
            log_content = "暂无日志记录"

        try:
            self.safe_send_response(200)
            self.safe_send_header('Content-Type', 'text/plain; charset=utf-8')
            self.safe_send_header('Access-Control-Allow-Origin', '*')
            self.safe_end_headers()
            self.safe_write(log_content.encode('utf-8'))
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass


# ========== Unix socket 服务器 ==========

if os.name != 'nt':
    class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        """多线程 Unix socket 服务器，供 fnOS 网关代理流量。

        Unix domain socket 是 POSIX 特性，Windows 上不可用。
        在 Windows 本地开发时 ThreadingUnixHTTPServer 为 None，仅使用 TCP。
        """

        daemon_threads = True
        allow_reuse_address = True

        def server_bind(self):
            socketserver.UnixStreamServer.server_bind(self)
            # 允许 nginx 等其他用户访问 socket
            os.chmod(self.server_address, 0o666)
else:
    ThreadingUnixHTTPServer = None


# ========== 启动入口 ==========

def main():
    """服务启动入口，支持 fnOS 部署和本地开发两种模式。

    fnOS 部署模式（参数顺序与 serve.py 一致）：
        python server.py <directory> <tcp_port> <socket_path> [gateway_prefix]
    本地开发模式：
        python server.py
    """
    if len(sys.argv) >= 4:
        # fnOS 部署模式：TCP + Unix socket 双监听
        # 参数顺序：<directory> <tcp_port> <socket_path> [gateway_prefix]
        static_dir = sys.argv[1]
        tcp_port = int(sys.argv[2])
        socket_path = sys.argv[3]
        gateway_prefix = sys.argv[4] if len(sys.argv) > 4 else "/app/FreePower"
    else:
        # 本地开发模式：仅 TCP，默认端口 8180
        tcp_port = int(os.environ.get('PORT', '8180'))
        socket_path = None
        static_dir = STATIC_DIR
        gateway_prefix = "/app/FreePower"

    # 应用配置到 Handler 类属性
    RequestHandler.gateway_prefix = gateway_prefix
    RequestHandler.static_dir = static_dir

    # 启动 Ping 监控线程
    ping_thread = PingThread()
    ping_thread.start()

    # 启动外网 Ping 监控线程（独立线程，与内网并行探测）
    wan_ping_thread = WanPingThread()
    wan_ping_thread.start()

    # 清理旧 socket 文件
    if socket_path and os.path.exists(socket_path):
        os.unlink(socket_path)

    # TCP 服务器（健康检查 + 直连访问）
    tcp_server = ThreadingHTTPServer(("0.0.0.0", tcp_port), RequestHandler)
    tcp_server.daemon_threads = True
    tcp_thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    tcp_thread.start()
    print(f"[serve] TCP listening on 0.0.0.0:{tcp_port}", flush=True)
    print(f"[serve] static_dir={static_dir}", flush=True)
    print(f"[serve] gateway_prefix={gateway_prefix}", flush=True)

    # Unix socket 服务器（fnOS 网关代理，仅 POSIX 平台）
    unix_server = None
    if socket_path and ThreadingUnixHTTPServer is not None:
        unix_server = ThreadingUnixHTTPServer(socket_path, RequestHandler)
        unix_thread = threading.Thread(target=unix_server.serve_forever, daemon=True)
        unix_thread.start()
        print(f"[serve] Unix socket listening on {socket_path}", flush=True)

    print("[serve] Ping monitor thread started", flush=True)
    print("[serve] WAN ping monitor thread started", flush=True)
    print("按 Ctrl+C 停止服务", flush=True)

    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("\n正在停止服务...", flush=True)
    finally:
        ping_thread.running = False
        wan_ping_thread.running = False
        tcp_server.shutdown()
        if unix_server:
            unix_server.shutdown()
            if os.path.exists(socket_path):
                os.unlink(socket_path)


if __name__ == '__main__':
    main()

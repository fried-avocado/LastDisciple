#!/usr/bin/env python3
"""
LastDisciple 业务后端服务

架构模式（遵循 serve.py）：
- 同时监听 TCP 端口和 Unix domain socket，兼容 fnOS 统一网关
- TCP 端口：供 checkport 健康检查与直连访问
- Unix socket：供 fnOS 网关（nginx）通过 gatewaySocket 代理流量

业务功能：
- Ping 网关监控（固定/自适应探测模式）
- 配置管理（基础/高级）
- 静态文件服务
- RESTful API 路由

启动方式：
  # fnOS 部署模式（TCP + Unix socket）
  python web_server.py <tcp_port> <socket_path> [static_dir] [gateway_prefix]

  # 本地开发模式（仅 TCP，默认端口 8180）
  python web_server.py
"""
import json

import math
import io
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote


# ========== 路径与常量 ==========

BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # app/server/
APP_DIR = os.path.dirname(BASE_DIR)                             # app/
STATIC_DIR = os.path.join(APP_DIR, "ui")                        # app/ui/

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
ADVANCED_CONFIG_FILE = os.path.join(BASE_DIR, "advanced_config.json")
LOG_FILE = os.path.join(BASE_DIR, "ping.log")

THEMES_DIR = os.path.join(STATIC_DIR, "themes")                 # app/ui/themes/
THEME_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
MAX_THEME_UPLOAD_BYTES = 10 * 1024 * 1024                       # 10MB
MAX_THEME_CSS_BYTES = 2 * 1024 * 1024                           # 单个 theme.css 上限

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


# ========== 主题包辅助函数 ==========

def is_valid_theme_id(theme_id):
    """主题 ID：小写字母/数字开头，仅含小写字母数字下划线连字符，防路径穿越。"""
    return isinstance(theme_id, str) and bool(THEME_ID_RE.fullmatch(theme_id))


def theme_dir(theme_id):
    return os.path.join(THEMES_DIR, theme_id)


def read_theme_meta(theme_id):
    """读取主题包元数据 theme.json；不存在或解析失败返回 None。"""
    meta_path = os.path.join(theme_dir(theme_id), "theme.json")
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def validate_theme_meta(meta):
    """校验 theme.json 结构，返回错误信息或 None。"""
    if not isinstance(meta, dict):
        return "theme.json must be a JSON object"
    if not is_valid_theme_id(meta.get("id")):
        return "theme.json: invalid or missing 'id' (lowercase letters/digits/-/_)"
    names = meta.get("name")
    if not isinstance(names, dict) or not any(
        isinstance(v, str) and v.strip() for v in names.values()
    ):
        return "theme.json: 'name' must be an i18n map like {\"en\": \"...\", \"zh-CN\": \"...\"}"
    return None


def install_theme_files(theme_id, files):
    """将 {文件名: bytes} 原子写入主题目录（先写临时目录再替换）。"""
    os.makedirs(THEMES_DIR, exist_ok=True)
    tmp_dir = os.path.join(THEMES_DIR, f".tmp-{os.getpid()}-{theme_id}")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir)
    try:
        for name, content in files.items():
            with open(os.path.join(tmp_dir, name), "wb") as f:
                f.write(content)
        target = theme_dir(theme_id)
        if os.path.exists(target):
            shutil.rmtree(target)
        os.replace(tmp_dir, target)
    except Exception:
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def extract_theme_packages_from_zip(data):
    """从 zip 字节流提取主题包。

    支持两种布局：
      - 单主题包：根目录 theme.json + theme.css
      - 备份包：<id>/theme.json + <id>/theme.css（多主题）
    返回 [(theme_id_hint, {文件名: bytes})]；布局非法抛 ValueError。
    """
    packages = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        single = {}
        multi = {}
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts_all = [p for p in name.split("/") if p]
            if name.startswith("/") or any(p == ".." for p in parts_all):
                raise ValueError(f"unsafe entry in archive: {info.filename}")
            parts = parts_all
            if len(parts) == 1 and parts[0] in ("theme.json", "theme.css"):
                single[parts[0]] = zf.read(info)
            elif (
                len(parts) == 2
                and parts[1] in ("theme.json", "theme.css")
                and is_valid_theme_id(parts[0])
            ):
                multi.setdefault(parts[0], {})[parts[1]] = zf.read(info)
            # 其他文件（如 README/预览图）忽略
        if single:
            packages.append(("", single))
        for folder, files in multi.items():
            packages.append((folder, files))
    if not packages:
        raise ValueError("archive contains no theme.json/theme.css")
    return packages


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


# ========== 关闭序列执行（手动/自动共用） ==========

def execute_shutdown_sequence(power_off=False, deadline=None):
    """按 shutdownOrder 顺序停止全部配置项，可选在末尾关闭系统。

    power_off=True 时在全部项处理完毕后执行系统关机（断线自动关机闭环）。
    deadline 为 time.monotonic() 绝对时刻：到点后剩余项全部标记跳过，
    但 power_off=True 时仍照常执行系统关机（总超时直接关机语义）。
    每次停止均记录耗时到 stop_stats.json（平均/最近/次数）。
    返回逐项结果列表（含可选的 system-poweroff 项）。
    """
    cfg = AdvancedConfig().load()
    order = cfg.shutdown_order or []
    results = []
    skipped_count = 0
    write_log(f"[exec] shutdown_sequence → items={len(order)} power_off={power_off} "
              f"timeout={'none' if deadline is None else 'set'}")
    if not PRIV_HELPER_SOCKET:
        write_log("[exec] shutdown_sequence → privilege helper not configured")
        items = [i for i in order if i.get('name')]
        if power_off:
            items.append({'name': 'system-poweroff', 'type': 'system'})
        return [{'name': i.get('name', ''), 'type': i.get('type', 'app'),
                 'success': False, 'error': 'root privilege helper unavailable (not on fnOS)'}
                for i in items]
    try:
        with PrivilegeClient() as priv:
            for item in order:
                name = item.get('name', '')
                t = item.get('type', 'app')
                if not name:
                    continue
                # 总超时到点：跳过剩余项（仍会执行关机）
                if deadline is not None and time.monotonic() >= deadline:
                    results.append({'name': name, 'type': t, 'success': False, 'skipped': True,
                                    'error': '关闭总超时已到，跳过'})
                    skipped_count += 1
                    continue
                t0 = time.monotonic()
                try:
                    r = priv.execute('stop', {'type': t, 'name': name})
                    duration = time.monotonic() - t0
                    record_stop_time(name, t, duration)
                    write_log(f"[exec] stop {t}:{name} ok in {duration:.2f}s")
                    results.append({'name': name, 'type': t, 'success': True,
                                    'message': r.get('message', ''), 'duration': round(duration, 2)})
                except PrivilegeOperationError as e:
                    duration = time.monotonic() - t0
                    record_stop_time(name, t, duration)
                    results.append({'name': name, 'type': t, 'success': False,
                                    'error': str(e), 'duration': round(duration, 2)})
            if skipped_count:
                write_log(f"[exec] shutdown_sequence → {skipped_count} item(s) skipped by timeout")
            if power_off:
                try:
                    r = priv.execute('poweroff', {})
                    results.append({'name': 'system-poweroff', 'type': 'system', 'success': True,
                                    'message': r.get('message', '')})
                    write_log("[exec] shutdown_sequence → system poweroff initiated")
                except PrivilegeOperationError as e:
                    results.append({'name': 'system-poweroff', 'type': 'system', 'success': False, 'error': str(e)})
                    write_log(f"[exec] shutdown_sequence → poweroff failed: {e}")
    except PrivilegeError as e:
        write_log(f"[exec] shutdown_sequence → privilege_error: {e}")
        items = [i for i in order if i.get('name')]
        if power_off:
            items.append({'name': 'system-poweroff', 'type': 'system'})
        return [{'name': i.get('name', ''), 'type': i.get('type', 'app'),
                 'success': False, 'error': f'root privilege helper unavailable: {e}'}
                for i in items]
    return results


# ========== 应用停止耗时统计 ==========

STOP_STATS_FILE = os.path.join(BASE_DIR, 'stop_stats.json')

def _load_stop_stats():
    try:
        with open(STOP_STATS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def _save_stop_stats(stats):
    try:
        with open(STOP_STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def record_stop_time(name, item_type, duration):
    """记录单次应用停止耗时（秒），维护次数/累计/最近/平均。"""
    try:
        stats = _load_stop_stats()
        key = f"{item_type}:{name}"
        entry = stats.get(key, {'count': 0, 'total': 0.0, 'last': 0.0, 'avg': 0.0})
        entry['count'] = int(entry.get('count', 0)) + 1
        entry['total'] = round(float(entry.get('total', 0.0)) + duration, 3)
        entry['last'] = round(duration, 3)
        entry['avg'] = round(entry['total'] / entry['count'], 3)
        stats[key] = entry
        _save_stop_stats(stats)
    except Exception:
        pass


# ========== 断线自动关机状态机 ==========

def execute_startup_sequence(only_names=None):
    """按 startupOrder 顺序启动全部配置项，可选仅启动指定名称的项。

    only_names 非空时仅处理名称在集合内的项（开机失败重试场景）。
    返回逐项结果列表。
    """
    cfg = AdvancedConfig().load()
    order = cfg.startup_order or []
    results = []
    write_log(f"[exec] startup_sequence → items={len(order)} only={len(only_names) if only_names else 'all'}")
    if not PRIV_HELPER_SOCKET:
        write_log("[exec] startup_sequence → privilege helper not configured")
        return [{'name': i.get('name', ''), 'type': i.get('type', 'app'),
                 'success': False, 'error': 'root privilege helper unavailable (not on fnOS)'}
                for i in order if i.get('name') and (only_names is None or i.get('name') in only_names)]
    try:
        with PrivilegeClient() as priv:
            for item in order:
                name = item.get('name', '')
                t = item.get('type', 'app')
                if not name or (only_names is not None and name not in only_names):
                    continue
                try:
                    r = priv.execute('start', {'type': t, 'name': name})
                    results.append({'name': name, 'type': t, 'success': True, 'message': r.get('message', '')})
                except PrivilegeOperationError as e:
                    results.append({'name': name, 'type': t, 'success': False, 'error': str(e)})
    except PrivilegeError as e:
        write_log(f"[exec] startup_sequence → privilege_error: {e}")
        return [{'name': i.get('name', ''), 'type': i.get('type', 'app'),
                 'success': False, 'error': f'root privilege helper unavailable: {e}'}
                for i in order if i.get('name') and (only_names is None or i.get('name') in only_names)]
    return results


class AutoShutdownController:
    """断线自动关机闭环控制器。

    状态迁移：
      disabled  : 功能未启用，不计数不触发
      armed     : 监控中，等待断线判定
      triggered : 已触发关机序列；等待网络恢复确认后重新武装，防止重复触发

    判定：所选触发源连续失败 ≥ failure_threshold → 触发；
    恢复：触发后相关源连续成功 ≥ recovery_threshold → 重新武装。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.state = "pending"  # 初始待武装：需先探测成功才进入 armed，防止开机即误关机
        self.trigger_source = None
        self.triggered_at = None
        self.last_reason = ""
        self.last_results = None
        self._lan_fail = 0
        self._wan_fail = 0
        self._recover_ok = 0

    @staticmethod
    def _relevant(source, target):
        return target == "any" or source == target

    def snapshot(self):
        with self._lock:
            return {
                "state": self.state,
                "triggerSource": self.trigger_source,
                "triggeredAt": self.triggered_at,
                "lastReason": self.last_reason,
                "lastResults": self.last_results,
                "consecutiveLoss": {"lan": self._lan_fail, "wan": self._wan_fail}
            }

    def on_probe(self, source, success):
        try:
            config = Config().load()
        except Exception:
            return
        if not config.auto_shutdown_enabled:
            with self._lock:
                if self.state != "disabled":
                    write_log("⛔ 自动关机：功能已关闭，控制器复位")
                self.state = "disabled"
            return

        target = config.auto_shutdown_target if config.auto_shutdown_target in ("lan", "wan", "any") else "any"
        fail_threshold = max(1, int(config.auto_shutdown_failure_threshold or 1))
        recover_threshold = max(1, int(config.auto_shutdown_recovery_threshold or 1))

        triggered = False
        with self._lock:
            if self.state == "disabled":
                # 功能从关闭切换为启用，进入待武装状态（等待首次探测成功确认）
                self.state = "pending"
                write_log("🟡 自动关机：已启用，待武装（等待首次探测成功）")
            if self.state == "pending":
                # 待武装：相关源探测成功后才进入 armed，网络未确认连通前不触发关机
                if success and self._relevant(source, target):
                    self.state = "armed"
                    self._lan_fail = 0
                    self._wan_fail = 0
                    write_log(f"🟢 自动关机：{source.upper()} 探测成功，进入武装监控状态")
                return
            if self.state == "triggered":
                # 等待恢复确认
                if success and self._relevant(source, target):
                    self._recover_ok += 1
                    if self._recover_ok >= recover_threshold:
                        write_log(f"🔁 自动关机：网络恢复已确认（连续成功 {self._recover_ok} 次），重新武装")
                        self._recover_ok = 0
                        self._lan_fail = 0
                        self._wan_fail = 0
                        self.state = "armed"
                else:
                    self._recover_ok = 0
                return

            # armed：更新失败计数
            if source == "lan":
                self._lan_fail = 0 if success else self._lan_fail + 1
                fail = self._lan_fail
            else:
                self._wan_fail = 0 if success else self._wan_fail + 1
                fail = self._wan_fail

            if not self._relevant(source, target):
                return
            if success or fail < fail_threshold:
                return

            # 判定断线 → 触发
            self.state = "triggered"
            self.trigger_source = source
            self.triggered_at = time.strftime('%Y-%m-%d %H:%M:%S')
            self.last_reason = f"{source.upper()} 连续失败 {fail} 次（阈值 {fail_threshold}）"
            self.last_results = None
            self._recover_ok = 0
            triggered = True
            write_log(f"⚡ 自动关机触发：{self.last_reason}，开始执行关闭序列")

        # 锁外以工作线程执行序列，避免阻塞探测循环
        if triggered:
            threading.Thread(target=self._execute, daemon=True).start()

    def _execute(self):
        try:
            try:
                cfg = Config().load()
                power_off = bool(getattr(cfg, 'auto_shutdown_power_off', True))
                timeout = int(getattr(cfg, 'shutdown_timeout_seconds', 0) or 0)
            except Exception:
                power_off = True
                timeout = 0
            deadline = (time.monotonic() + timeout) if timeout > 0 else None
            results = execute_shutdown_sequence(power_off=power_off, deadline=deadline)
            ok = sum(1 for r in results if r.get('success'))
            skipped = sum(1 for r in results if r.get('skipped'))
            self.last_results = {"total": len(results), "ok": ok, "failed": len(results) - ok,
                                 "skippedByTimeout": skipped}
            write_log(f"⚡ 自动关机序列执行完成：共 {len(results)} 项，成功 {ok}，"
                      f"失败 {len(results) - ok}（其中超时跳过 {skipped}）")
        except Exception as e:
            self.last_results = {"error": str(e)}
            write_log(f"⚡ 自动关机序列执行异常: {e}")


auto_shutdown = AutoShutdownController()


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
                auto_shutdown.on_probe('lan', connected)

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
                auto_shutdown.on_probe('wan', connected)
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
    """基础配置（探测间隔、网关地址、探测模式）。

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
        self.probing_mode = "fixed"        # 探测模式: fixed / adaptive
        self.auto_shutdown_enabled = False        # 断线自动关机开关
        self.auto_shutdown_target = "any"         # 触发源: lan / wan / any
        self.auto_shutdown_failure_threshold = 5  # 连续失败判定阈值（次）
        self.auto_shutdown_recovery_threshold = 3 # 恢复确认阈值（次）
        self.auto_shutdown_power_off = True       # 自动触发时在关闭序列末尾执行系统关机
        self.shutdown_timeout_seconds = 0         # 关闭序列总超时（秒，0=不限制，超时后跳过剩余项直接关机）
        self.shutdown_timeout_buffer_ratio = 0.10 # 测得耗时之上的富裕比率（测试按钮建议值 = 总和 × (1 + 比率)）
        self.theme_package = "cyberpunk"   # 当前主题包 ID（config.json 缺失时的默认值；此前仅在 load() 读到文件时赋值，全新安装访问该属性会抛 AttributeError）

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
                    self.probing_mode = data.get('probingMode', "fixed")
                    self.auto_shutdown_enabled = data.get('autoShutdownEnabled', False)
                    self.auto_shutdown_target = data.get('autoShutdownTarget', "any")
                    self.auto_shutdown_failure_threshold = data.get('autoShutdownFailureThreshold', 5)
                    self.auto_shutdown_recovery_threshold = data.get('autoShutdownRecoveryThreshold', 3)
                    self.auto_shutdown_power_off = bool(data.get('autoShutdownPowerOff', True))
                    self.shutdown_timeout_seconds = data.get('shutdownTimeoutSeconds', 0)
                    self.shutdown_timeout_buffer_ratio = data.get('shutdownTimeoutBufferRatio', 0.10)
                    self.theme_package = data.get('themePackage', "cyberpunk")
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
                    'autoShutdownEnabled': self.auto_shutdown_enabled,
                    'autoShutdownTarget': self.auto_shutdown_target,
                    'autoShutdownFailureThreshold': self.auto_shutdown_failure_threshold,
                    'autoShutdownRecoveryThreshold': self.auto_shutdown_recovery_threshold,
                    'autoShutdownPowerOff': self.auto_shutdown_power_off,
                    'shutdownTimeoutSeconds': self.shutdown_timeout_seconds,
                    'shutdownTimeoutBufferRatio': self.shutdown_timeout_buffer_ratio,
                    'probingMode': self.probing_mode,
                    'themePackage': self.theme_package
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
    """高级配置（应用关闭/启动顺序）。

    历史上的 stopApps/dockerContainers 扁平列表已移除，
    统一由 shutdownOrder/startupOrder 表达（type 为 'app' 或 'docker'）。
    """

    def __init__(self):
        # 关闭顺序：[{name, type}]，type 为 'app' 或 'docker'
        self.shutdown_order = []
        # 启动顺序：默认为关闭顺序的逆序，但可由用户独立手动调整
        self.startup_order = []

    def load(self):
        if os.path.exists(ADVANCED_CONFIG_FILE):
            try:
                with open(ADVANCED_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.shutdown_order = data.get('shutdownOrder', [])
                    self.startup_order = data.get('startupOrder', [])
            except Exception as e:
                print(f"Error loading advanced config: {e}")
        return self

    def save(self):
        try:
            with open(ADVANCED_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'shutdownOrder': self.shutdown_order,
                    'startupOrder': self.startup_order
                }, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving advanced config: {e}")
            return False


# ========== 网关兼容混入 ==========

class GatewayMixin:
    """fnOS 网关兼容能力，解决 Unix socket 下两个已知问题：

    1. address_string 崩溃：Unix socket 的 client_address 是空字符串，
       默认 self.client_address[0] 取首字符会抛 IndexError，
       导致 send_error/log_message 失败，网关收不到有效响应返回 502。
    2. 路径前缀：网关转发请求时路径带 gatewayPrefix（如 /app/LastDisciple），
       需要去掉前缀才能正确路由到业务 API 和静态文件。
    """

    gateway_prefix = "/app/LastDisciple"

    def address_string(self):
        """修复 Unix socket 下 client_address 为空字符串导致的 IndexError。"""
        try:
            if isinstance(self.client_address, str):
                return "unix"
            return self.client_address[0] or "unix"
        except (IndexError, TypeError):
            return "unix"

    def strip_gateway_prefix(self, path):
        """去掉网关前缀（如 /app/LastDisciple），返回业务路径。"""
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
        # 去掉网关前缀（如 /app/LastDisciple），兼容直连和网关两种访问方式
        path = self.strip_gateway_prefix(original_path)

        # 根路径无尾部斜杠时301重定向，确保前端相对路径（./api/...）能正确解析。
        # 浏览器将无尾部斜杠的URL（如 /app/LastDisciple）视为"文件"，
        # 相对路径 ./api/config 会替换文件名变为 /app/api/config 而非 /app/LastDisciple/api/config。
        # 301 到 /app/LastDisciple/ 后浏览器将 /app/LastDisciple/ 视为"目录"，相对路径正确追加。
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
        elif path == '/api/status':
            self.handle_get_status()
        elif path == '/api/stop-stats':
            self.handle_get_stop_stats()
        elif path == '/api/wan-status':
            self.handle_get_wan_status()
        elif path == '/api/logs':
            self.handle_get_logs()
        elif path == '/api/themes':
            self.handle_list_themes()
        elif path == '/api/themes/backup':
            self.handle_backup_themes()
        else:
            m = re.fullmatch(r'/api/themes/([a-z0-9][a-z0-9_-]{0,63})/download', path)
            if m:
                self.handle_download_theme(m.group(1))
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
        elif path == '/api/app-control':
            self.handle_app_control(post_data)
        elif path == '/api/execute-shutdown':
            self.handle_execute_shutdown(post_data)
        elif path == '/api/execute-startup':
            self.handle_execute_startup(post_data)
        elif path == '/api/test-shutdown-timing':
            self.handle_test_shutdown_timing(post_data)
        elif path == '/api/test-stop-item':
            self.handle_test_stop_item(post_data)
        elif path == '/api/themes/upload':
            self.handle_upload_theme(post_data)
        elif path == '/api/themes/active':
            self.handle_set_active_theme(post_data)
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

    # --- API: 主题包管理 ---

    def handle_list_themes(self):
        try:
            active = Config().load().theme_package
            themes = []
            if os.path.isdir(THEMES_DIR):
                for entry in sorted(os.listdir(THEMES_DIR)):
                    if not is_valid_theme_id(entry):
                        continue
                    meta = read_theme_meta(entry)
                    if not isinstance(meta, dict):
                        write_log(f"[themes] 跳过无效主题目录: {entry}")
                        continue
                    themes.append({
                        'id': meta.get('id', entry),
                        'name': meta.get('name', {}),
                        'version': meta.get('version', ''),
                        'author': meta.get('author', ''),
                        'description': meta.get('description', {}),
                        'active': entry == active
                    })
            self.send_json_response(200, {'success': True, 'data': {'themes': themes, 'active': active}})
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            write_log(f"[themes] GET /api/themes 异常: {e}")
            self.send_json_response(500, {'success': False, 'error': str(e)})

    def handle_set_active_theme(self, post_data):
        try:
            data = json.loads(post_data)
            theme_id = data.get('id')
            if not is_valid_theme_id(theme_id):
                self.send_json_response(400, {'success': False, 'error': 'invalid theme id'})
                return
            if not os.path.isfile(os.path.join(theme_dir(theme_id), 'theme.json')):
                self.send_json_response(404, {'success': False, 'error': f'theme not found: {theme_id}'})
                return
            config = Config().load()
            config.theme_package = theme_id
            if not config.save():
                self.send_json_response(500, {'success': False, 'error': 'failed to save config'})
                return
            write_log(f"[themes] 激活主题包: {theme_id}")
            self.send_json_response(200, {'success': True, 'data': {'active': theme_id}})
        except json.JSONDecodeError:
            self.send_json_response(400, {'success': False, 'error': 'invalid json body'})
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            write_log(f"[themes] POST /api/themes/active 异常: {e}")
            self.send_json_response(500, {'success': False, 'error': str(e)})

    def handle_upload_theme(self, post_data):
        try:
            if len(post_data) > MAX_THEME_UPLOAD_BYTES:
                self.send_json_response(413, {'success': False, 'error': 'theme package too large (max 10MB)'})
                return
            if not post_data.startswith(b'PK'):
                self.send_json_response(400, {'success': False, 'error': 'only .zip theme packages are supported'})
                return

            imported = []
            try:
                packages = extract_theme_packages_from_zip(post_data)
            except (zipfile.BadZipFile, ValueError) as e:
                self.send_json_response(400, {'success': False, 'error': f'invalid theme package: {e}'})
                return

            for hint_id, files in packages:
                if 'theme.json' not in files or 'theme.css' not in files:
                    self.send_json_response(400, {'success': False, 'error': 'package must contain theme.json and theme.css'})
                    return
                if len(files['theme.css']) == 0 or len(files['theme.css']) > MAX_THEME_CSS_BYTES:
                    self.send_json_response(400, {'success': False, 'error': 'theme.css is empty or too large'})
                    return
                try:
                    meta = json.loads(files['theme.json'].decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    self.send_json_response(400, {'success': False, 'error': f'invalid theme.json: {e}'})
                    return
                err = validate_theme_meta(meta)
                if err:
                    self.send_json_response(400, {'success': False, 'error': err})
                    return
                theme_id = hint_id or meta['id']
                # 备份包布局下以 theme.json 的 id 为准（目录名仅作提示）
                if not is_valid_theme_id(theme_id):
                    self.send_json_response(400, {'success': False, 'error': f'invalid theme id: {theme_id}'})
                    return
                try:
                    install_theme_files(theme_id, files)
                except Exception as e:
                    self.send_json_response(500, {'success': False, 'error': f'failed to install theme: {e}'})
                    return
                imported.append(theme_id)
                write_log(f"[themes] 导入主题包: {theme_id} ({len(files['theme.css'])} bytes css)")

            self.send_json_response(200, {'success': True, 'data': {'imported': imported}})
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            write_log(f"[themes] POST /api/themes/upload 异常: {e}")
            self.send_json_response(500, {'success': False, 'error': str(e)})

    def _send_zip_response(self, buf, filename):
        content = buf.getvalue()
        self.safe_send_response(200)
        self.safe_send_header('Content-Type', 'application/zip')
        self.safe_send_header('Access-Control-Allow-Origin', '*')
        self.safe_send_header('Content-Length', len(content))
        self.safe_send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.safe_send_header('Cache-Control', 'no-cache')
        self.safe_end_headers()
        self.safe_write(content)

    def handle_download_theme(self, theme_id):
        try:
            if not is_valid_theme_id(theme_id) or not os.path.isdir(theme_dir(theme_id)):
                self.send_json_response(404, {'success': False, 'error': f'theme not found: {theme_id}'})
                return
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(theme_dir(theme_id)):
                    for fname in sorted(files):
                        fpath = os.path.join(root, fname)
                        arcname = fname  # 单主题包：文件置于 zip 根目录
                        zf.write(fpath, arcname)
            meta = read_theme_meta(theme_id) or {}
            version = meta.get('version', '')
            filename = f"{theme_id}{('-' + str(version)) if version else ''}.zip"
            write_log(f"[themes] 下载主题包: {theme_id}")
            self._send_zip_response(buf, filename)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            write_log(f"[themes] GET /api/themes/{theme_id}/download 异常: {e}")
            self.send_json_response(500, {'success': False, 'error': str(e)})

    def handle_backup_themes(self):
        try:
            if not os.path.isdir(THEMES_DIR):
                self.send_json_response(200, {'success': False, 'error': 'no themes installed'})
                return
            buf = io.BytesIO()
            count = 0
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for entry in sorted(os.listdir(THEMES_DIR)):
                    tdir = theme_dir(entry)
                    if not is_valid_theme_id(entry) or not os.path.isdir(tdir):
                        continue
                    for root, _, files in os.walk(tdir):
                        for fname in sorted(files):
                            fpath = os.path.join(root, fname)
                            arcname = f"{entry}/{os.path.relpath(fpath, tdir).replace(os.sep, '/')}"
                            zf.write(fpath, arcname)
                    count += 1
            if count == 0:
                self.send_json_response(200, {'success': False, 'error': 'no themes installed'})
                return
            filename = f"LastDisciple-themes-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
            write_log(f"[themes] 备份主题包: {count} 个")
            self._send_zip_response(buf, filename)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            write_log(f"[themes] GET /api/themes/backup 异常: {e}")
            self.send_json_response(500, {'success': False, 'error': str(e)})

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
                    'probingMode': config.probing_mode,
                    'autoShutdownEnabled': config.auto_shutdown_enabled,
                    'autoShutdownTarget': config.auto_shutdown_target,
                    'autoShutdownFailureThreshold': config.auto_shutdown_failure_threshold,
                    'autoShutdownRecoveryThreshold': config.auto_shutdown_recovery_threshold,
                    'autoShutdownPowerOff': config.auto_shutdown_power_off,
                    'shutdownTimeoutSeconds': config.shutdown_timeout_seconds,
                    'shutdownTimeoutBufferRatio': config.shutdown_timeout_buffer_ratio,
                    'themePackage': config.theme_package
                }
            }).encode('utf-8'))
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def handle_post_config(self, post_data):
        try:
            data = json.loads(post_data)
            # 先加载现有配置：请求体未携带的字段（如 themePackage）保持原值不被重置
            config = Config().load()
            config.interval = data.get('interval', config.interval)
            config.gateway = data.get('gateway', config.gateway)
            config.lan_gateway = data.get('lanGateway', data.get('lan_gateway', config.lan_gateway))
            config.wan_host = data.get('wanHost', data.get('wan_host', config.wan_host))
            config.wan_ping_enabled = data.get('wanPingEnabled', data.get('wan_ping_enabled', config.wan_ping_enabled))
            config.probing_mode = data.get('probingMode', config.probing_mode)
            config.auto_shutdown_enabled = data.get('autoShutdownEnabled', config.auto_shutdown_enabled)
            config.auto_shutdown_target = data.get('autoShutdownTarget', config.auto_shutdown_target)
            config.auto_shutdown_failure_threshold = data.get('autoShutdownFailureThreshold', config.auto_shutdown_failure_threshold)
            config.auto_shutdown_recovery_threshold = data.get('autoShutdownRecoveryThreshold', config.auto_shutdown_recovery_threshold)
            config.auto_shutdown_power_off = bool(data.get('autoShutdownPowerOff', config.auto_shutdown_power_off))
            config.shutdown_timeout_seconds = int(data.get('shutdownTimeoutSeconds', config.shutdown_timeout_seconds) or 0)
            config.shutdown_timeout_buffer_ratio = float(data.get('shutdownTimeoutBufferRatio', config.shutdown_timeout_buffer_ratio) or 0)
            config.theme_package = data.get('themePackage', config.theme_package)

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

            if config.auto_shutdown_target not in ['lan', 'wan', 'any']:
                self.send_json_response(400, {
                    'success': False,
                    'error': 'AutoShutdownTarget must be one of: lan, wan, any'
                })
                return

            if config.auto_shutdown_failure_threshold < 1 or config.auto_shutdown_recovery_threshold < 1:
                self.send_json_response(400, {
                    'success': False,
                    'error': 'AutoShutdown thresholds must be greater than 0'
                })
                return

            if config.shutdown_timeout_seconds < 0:
                self.send_json_response(400, {
                    'success': False,
                    'error': 'shutdownTimeoutSeconds must be >= 0 (0 = no limit)'
                })
                return

            if not (0 <= config.shutdown_timeout_buffer_ratio <= 1):
                self.send_json_response(400, {
                    'success': False,
                    'error': 'shutdownTimeoutBufferRatio must be between 0 and 1'
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
                        'autoShutdownEnabled': config.auto_shutdown_enabled,
                        'autoShutdownTarget': config.auto_shutdown_target,
                        'autoShutdownFailureThreshold': config.auto_shutdown_failure_threshold,
                        'autoShutdownRecoveryThreshold': config.auto_shutdown_recovery_threshold,
                        'autoShutdownPowerOff': config.auto_shutdown_power_off,
                        'shutdownTimeoutSeconds': config.shutdown_timeout_seconds,
                        'shutdownTimeoutBufferRatio': config.shutdown_timeout_buffer_ratio,
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
                'shutdownOrder': config.shutdown_order,
                'startupOrder': config.startup_order
            }
        })

    def handle_post_advanced_config(self, post_data):
        try:
            data = json.loads(post_data)
            config = AdvancedConfig()
            config.shutdown_order = data.get('shutdownOrder', [])
            config.startup_order = data.get('startupOrder', [])

            if config.save():
                self.send_json_response(200, {
                    'success': True,
                    'data': {
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
        if not PRIV_HELPER_SOCKET:
            write_log("[api] POST /api/execute-shutdown → 503 (privilege helper not configured)")
            self.send_json_response(503, {'success': False, 'error': 'root privilege helper unavailable (not on fnOS)'})
            return
        write_log(f"[api] POST /api/execute-shutdown items={len(order)}")
        results = execute_shutdown_sequence()
        ok_count = sum(1 for r in results if r.get('success'))
        write_log(f"[api] POST /api/execute-shutdown → 200 total={len(results)} ok={ok_count} failed={len(results) - ok_count}")
        self.send_json_response(200, {'success': True, 'data': {'results': results}})

    # --- API: 批量执行启动顺序（通过 root 特权助手） ---

    def handle_execute_startup(self, post_data):
        """按 startupOrder 顺序启动全部配置项，聚合每项结果。"""
        if not PRIV_HELPER_SOCKET:
            write_log("[api] POST /api/execute-startup → 503 (privilege helper not configured)")
            self.send_json_response(503, {'success': False, 'error': 'root privilege helper unavailable (not on fnOS)'})
            return
        results = execute_startup_sequence()
        ok_count = sum(1 for r in results if r.get('success'))
        write_log(f"[api] POST /api/execute-startup → 200 total={len(results)} ok={ok_count} failed={len(results) - ok_count}")
        self.send_json_response(200, {'success': True, 'data': {'results': results}})

    def handle_get_stop_stats(self):
        """返回当前 shutdownOrder 各项的停止耗时统计与建议总超时。"""
        try:
            cfg = AdvancedConfig().load()
        except Exception:
            cfg = None
        order = (cfg.shutdown_order or []) if cfg else []
        ratio = 0.10
        try:
            ratio = float(Config().load().shutdown_timeout_buffer_ratio)
        except Exception:
            pass
        stats = _load_stop_stats()
        items = []
        total_avg = 0.0
        for it in (order or []):
            name, t = it.get('name', ''), it.get('type', 'app')
            if not name:
                continue
            entry = stats.get(f"{t}:{name}")
            row = {'name': name, 'type': t, 'count': 0, 'avg': None, 'last': None}
            if entry:
                row.update({'count': entry.get('count', 0), 'avg': entry.get('avg'),
                            'last': entry.get('last')})
                total_avg += float(entry.get('avg', 0.0))
            items.append(row)
        suggested = math.ceil(total_avg * (1.0 + ratio)) if items and total_avg > 0 else None
        self.send_json_response(200, {'success': True, 'data': {
            'items': items,
            'bufferRatio': ratio,
            'suggestedTimeoutSeconds': suggested
        }})

    def handle_test_stop_item(self, post_data):
        """实测单个配置项停止耗时：停止（计时）→ 记录统计 → 立即启动还原。

        供前端逐项驱动测试使用，每完成一项即可刷新进度展示。
        """
        if not PRIV_HELPER_SOCKET:
            self.send_json_response(503, {'success': False,
                                          'error': 'root privilege helper unavailable (not on fnOS)'})
            return
        try:
            data = json.loads(post_data) if post_data else {}
        except Exception:
            self.send_json_response(400, {'success': False, 'error': 'invalid JSON body'})
            return
        name = str(data.get('name') or '').strip()
        item_type = str(data.get('type') or 'app').strip()
        if not name:
            self.send_json_response(400, {'success': False, 'error': 'name is required'})
            return
        if item_type not in ('app', 'docker'):
            self.send_json_response(400, {'success': False, 'error': 'type must be app or docker'})
            return
        row = {'name': name, 'type': item_type, 'duration': None, 'restored': None, 'error': None}
        write_log(f"[test] item stop {item_type}:{name} begin")
        try:
            with PrivilegeClient() as priv:
                t0 = time.monotonic()
                try:
                    priv.execute('stop', {'type': item_type, 'name': name})
                    duration = time.monotonic() - t0
                    record_stop_time(name, item_type, duration)
                    row['duration'] = round(duration, 2)
                    write_log(f"[test] item stop {item_type}:{name} in {duration:.2f}s")
                except PrivilegeOperationError as e:
                    row['error'] = str(e)
                    write_log(f"[test] item stop {item_type}:{name} failed: {e}")
                if row['duration'] is not None:
                    try:
                        priv.execute('start', {'type': item_type, 'name': name})
                        row['restored'] = True
                    except PrivilegeOperationError as e:
                        row['restored'] = False
                        row['restoreError'] = str(e)
        except PrivilegeError as e:
            self.send_json_response(503, {'success': False, 'error': f'root privilege helper unavailable: {e}'})
            return
        write_log(f"[test] item {item_type}:{name} done dur={row['duration']} restored={row['restored']}")
        self.send_json_response(200, {'success': True, 'data': row})

    def handle_test_shutdown_timing(self, post_data):
        """逐项实测停止耗时：停止（计时）→ 立即启动回原状态。

        返回逐项耗时、建议总超时（Σ耗时 × (1 + 富裕比率)，精确到秒）。
        """
        if not PRIV_HELPER_SOCKET:
            self.send_json_response(503, {'success': False,
                                          'error': 'root privilege helper unavailable (not on fnOS)'})
            return
        try:
            cfg = AdvancedConfig().load()
        except Exception:
            cfg = None
        order = (cfg.shutdown_order or []) if cfg else []
        ratio = 0.10
        try:
            ratio = float(Config().load().shutdown_timeout_buffer_ratio)
        except Exception:
            pass
        results = []
        total = 0.0
        write_log(f"[api] POST /api/test-shutdown-timing → items={len(order)} ratio={ratio}")
        try:
            with PrivilegeClient() as priv:
                for item in order:
                    name, t = item.get('name', ''), item.get('type', 'app')
                    if not name:
                        continue
                    row = {'name': name, 'type': t, 'duration': None, 'restored': None, 'error': None}
                    t0 = time.monotonic()
                    try:
                        priv.execute('stop', {'type': t, 'name': name})
                        duration = time.monotonic() - t0
                        record_stop_time(name, t, duration)
                        row['duration'] = round(duration, 2)
                        total += duration
                        write_log(f"[test] stop {t}:{name} in {duration:.2f}s")
                    except PrivilegeOperationError as e:
                        row['error'] = str(e)
                        write_log(f"[test] stop {t}:{name} failed: {e}")
                        results.append(row)
                        continue
                    try:
                        priv.execute('start', {'type': t, 'name': name})
                        row['restored'] = True
                    except PrivilegeOperationError as e:
                        row['restored'] = False
                        row['restoreError'] = str(e)
                    results.append(row)
        except PrivilegeError as e:
            self.send_json_response(503, {'success': False, 'error': f'root privilege helper unavailable: {e}'})
            return
        suggested = math.ceil(total * (1.0 + ratio)) if results and total > 0 else None
        write_log(f"[api] POST /api/test-shutdown-timing → 200 total={total:.2f}s suggested={suggested}s")
        self.send_json_response(200, {'success': True, 'data': {
            'results': results,
            'bufferRatio': ratio,
            'totalSeconds': round(total, 2),
            'suggestedTimeoutSeconds': suggested
        }})

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
                "autoShutdown": auto_shutdown.snapshot(),
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

def _boot_start_task():
    """开机自动按 startupOrder 顺序启动应用（仅 fnOS 部署模式调用）。

    延迟 30 秒等待系统服务（docker/appcenter/特权助手）就绪；
    存在失败项时每 60 秒仅重试失败项，最多 3 轮。
    """
    try:
        time.sleep(30)
        failed = None
        for attempt in (1, 2, 3):
            results = execute_startup_sequence(only_names=failed)
            if not results:
                return
            failed_names = {r['name'] for r in results if not r.get('success')}
            ok = len(results) - len(failed_names)
            write_log(f"[boot] startup_sequence attempt {attempt}: ok={ok} failed={len(failed_names)}")
            if not failed_names:
                return
            failed = failed_names
            if attempt < 3:
                time.sleep(60)
    except Exception as e:
        write_log(f"[boot] startup_sequence error: {e}")


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
        gateway_prefix = sys.argv[4] if len(sys.argv) > 4 else "/app/LastDisciple"
    else:
        # 本地开发模式：仅 TCP，默认端口 8180
        tcp_port = int(os.environ.get('PORT', '8180'))
        socket_path = None
        static_dir = STATIC_DIR
        gateway_prefix = "/app/LastDisciple"

    # 应用配置到 Handler 类属性
    RequestHandler.gateway_prefix = gateway_prefix
    RequestHandler.static_dir = static_dir

    # 启动 Ping 监控线程
    ping_thread = PingThread()
    ping_thread.start()

    # 启动外网 Ping 监控线程（独立线程，与内网并行探测）
    wan_ping_thread = WanPingThread()
    wan_ping_thread.start()

    # 开机自动按 startupOrder 顺序启动应用（仅 fnOS 部署模式）
    if socket_path:
        threading.Thread(target=_boot_start_task, daemon=True).start()
        print("[serve] boot startup task scheduled (30s delay)", flush=True)

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

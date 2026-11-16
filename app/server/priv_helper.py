#!/usr/bin/env python3
"""LastDisciple 特权助手守护进程。

以 root 运行，通过 Unix domain socket 为 Web 服务器（包用户）提供受限的特权操作：
  - get_apps : 通过 appcenter-cli list 读取飞牛 OS 已安装应用列表 + Docker 容器列表
  - start    : 启动应用（appcenter-cli start / systemctl 回退）或 Docker 容器（docker start）
  - stop     : 停止应用（appcenter-cli stop / systemctl 回退）或 Docker 容器（docker stop）

安全模型（遵循飞牛 OS 权限规范）：
  1. socket 文件 0660 root:pkggroup —— 仅 root 与包用户组成员可连接
  2. SO_PEERCRED 校验调用方 UID 精确等于包用户 UID —— 双层防御
  3. op 白名单硬编码；未知操作拒绝
  4. name 正则 ^[A-Za-z0-9_.-]{1,128}$ 校验；type 枚举校验
  5. 所有 subprocess.run 使用列表参数、shell=False —— 杜绝 shell 注入
  6. 审计日志详尽记录每次请求（调用方 UID/用户名、权限校验过程、命令参数/返回码/stderr、耗时）

协议：行分隔 JSON，单行上限 64KB。
  请求: {"op":"get_apps","params":{"what":"apps"},"id":1}\\n
  响应: {"success":true,"data":{...},"id":1}\\n

启动方式（由 cmd/main 以 root 调用）:
  python3 priv_helper.py <socket_path> <pkg_username>
"""

import json
import os
import pwd
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime

# ========== 参数与常量 ==========

SOCKET_PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('TRIM_PRIV_HELPER_SOCK', '')
PKG_USERNAME = sys.argv[2] if len(sys.argv) > 2 else os.environ.get('TRIM_USERNAME', '')
PKGVAR = os.environ.get('TRIM_PKGVAR', '/tmp')
LOG_FILE = os.path.join(PKGVAR, 'info.log')

# op 白名单
OP_WHITELIST = {'get_apps', 'start', 'stop', 'poweroff'}

# 名称校验：仅允许字母、数字、下划线、点、短横线，长度 1-128
NAME_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,128}$')

# Docker 二进制定位候选
DOCKER_CANDIDATES = ('docker', '/usr/bin/docker', '/usr/local/bin/docker')

# appcenter-cli 二进制定位候选
APPCENTER_CANDIDATES = ('appcenter-cli', '/usr/bin/appcenter-cli', '/usr/local/bin/appcenter-cli',
                        '/trim/bin/appcenter-cli', '/opt/trim/bin/appcenter-cli')

# 单行读取上限
MAX_LINE = 64 * 1024

# 连接/读取超时
CONN_TIMEOUT = 30


# ========== 审计日志 ==========

def audit(msg):
    """追加一行审计日志到 info.log。"""
    try:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - [priv] {msg}\n"
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception:
        pass


def uid_to_username(uid):
    """将 UID 解析为用户名，失败时返回 str(uid)。"""
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError, TypeError):
        return str(uid)


# ========== 调用方身份校验 ==========

def get_pkg_uid():
    """返回包用户的 UID，无法解析时返回 None。"""
    if not PKG_USERNAME:
        return None
    try:
        return pwd.getpwnam(PKG_USERNAME).pw_uid
    except KeyError:
        return None


def peer_uid(conn):
    """通过 SO_PEERCRED 获取 Unix socket 对端的 UID（Linux 专有）。"""
    try:
        cred = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('iII'))
        _pid, uid, _gid = struct.unpack('iII', cred)
        return uid
    except Exception:
        return None


# ========== 命令定位辅助 ==========

def find_docker_bin():
    """定位可用的 docker 可执行文件，返回路径或 None。"""
    for candidate in DOCKER_CANDIDATES:
        try:
            result = subprocess.run(
                [candidate, 'version', '--format', '{{.Server.Version}}'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def find_appcenter_cli():
    """定位可用的 appcenter-cli 可执行文件，返回路径或 None。

    使用 `appcenter-cli --help` 探测，返回码 0 即视为可用。
    """
    for candidate in APPCENTER_CANDIDATES:
        try:
            result = subprocess.run(
                [candidate, '--help'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _run_cmd(args, timeout=30):
    """统一的命令执行封装，返回 (returncode, stdout, stderr)。"""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout or '', result.stderr or ''
    except FileNotFoundError:
        return -1, '', f'{args[0]}: command not found'
    except subprocess.TimeoutExpired:
        return -2, '', f'{args[0]}: timed out after {timeout}s'


# ========== 特权操作处理器 ==========

class PrivHandler:
    """处理单个连接的特权操作分发。"""

    def __init__(self, caller_uid=None):
        self.caller_uid = caller_uid
        self.caller_label = f"uid={caller_uid}({uid_to_username(caller_uid)})" if caller_uid is not None else "uid=unknown"

    def handle(self, req):
        """分发 op，返回响应字典。"""
        op = req.get('op')
        params = req.get('params') or {}
        req_id = req.get('id')

        if op not in OP_WHITELIST:
            audit(f"REJECT caller={self.caller_label} op={op!r} reason=unknown_op")
            return {'success': False, 'error': f'unknown op: {op}', 'id': req_id}

        started = time.time()
        try:
            if op == 'get_apps':
                data = self.get_apps(params)
            elif op in ('start', 'stop'):
                data = self.control(params, op)
            elif op == 'poweroff':
                data = self.poweroff(params)
            else:
                return {'success': False, 'error': 'op not implemented', 'id': req_id}
            elapsed = int((time.time() - started) * 1000)
            audit(f"OK caller={self.caller_label} op={op} params={params} elapsed={elapsed}ms")
            return {'success': True, 'data': data, 'id': req_id}
        except ValueError as e:
            audit(f"REJECT caller={self.caller_label} op={op} params={params} reason=invalid_input: {e}")
            return {'success': False, 'error': f'invalid input: {e}', 'id': req_id}
        except subprocess.TimeoutExpired:
            audit(f"TIMEOUT caller={self.caller_label} op={op} params={params}")
            return {'success': False, 'error': 'command timed out', 'id': req_id}
        except Exception as e:
            audit(f"ERROR caller={self.caller_label} op={op} params={params} error={e}")
            return {'success': False, 'error': str(e), 'id': req_id}

    # --- get_apps: 读取应用列表 + 容器列表 ---

    def get_apps(self, params):
        """读取飞牛 OS 已安装应用列表与 Docker 容器列表（root 权限）。

        params.what: 'apps' | 'containers' | 'all'（默认 'all'）
        """
        what = (params.get('what') or 'all').lower()
        if what not in ('apps', 'containers', 'all'):
            what = 'all'

        result = {}
        if what in ('apps', 'all'):
            result['apps'], source = self._read_system_apps()
            result['source'] = source
            # 自动获取时应排除的应用（当前应用自身 + 依赖应用）
            # 必须转换为 list，避免 set 无法 JSON 序列化
            result['excluded'] = sorted(self._auto_fetch_excluded_names())
        if what in ('containers', 'all'):
            result['containers'] = self._read_docker_containers()

        if what == 'apps':
            result['count'] = len(result.get('apps', []))
        elif what == 'containers':
            result['count'] = len(result.get('containers', []))
        else:
            result['count'] = len(result.get('apps', []))
        return result

    def _auto_fetch_excluded_names(self):
        """自动获取（picker）时应排除的应用名（规范化集合）。

        排除规则：
          1. 当前应用自身（如 LastDisciple）
          2. 当前应用所依赖的应用（如 python 3.9 → python39）

        返回规范化名称集合，便于前端忽略大小写/空格差异匹配。
        """
        excluded = set()
        # 当前应用自身
        appname = os.environ.get('TRIM_APPNAME', '') or 'LastDisciple'
        excluded.add(self._norm_name(appname))
        # 依赖应用
        excluded.update(self._load_own_dependency_names())
        return excluded

    def _read_system_apps(self):
        """通过 appcenter-cli list 获取已安装应用列表（root 权限）。

        主路径：执行 `appcenter-cli list`，解析 Unicode 表格输出提取应用名+显示名。
        回退路径：appcenter-cli 不可用时，遍历 fnOS apps 目录读取 manifest。

        展示规则：
          1. 展示全部应用（含当前应用 LastDisciple 自身）
          2. 包含当前应用所依赖的所有应用（依赖应用由系统自动管理，需一并展示）
          3. 依赖匹配仅按应用名匹配（忽略版本号差异）

        返回 (apps_list, source_str)。
        """
        appcenter_bin = find_appcenter_cli()
        if appcenter_bin:
            audit(f"GET_APPS caller={self.caller_label} method=appcenter-cli bin={appcenter_bin}")
            rc, stdout, stderr = _run_cmd([appcenter_bin, 'list'], timeout=15)
            if rc == 0:
                parsed = self._parse_appcenter_list_output(stdout)
                # 展示全部应用，不排除 LastDisciple 自身
                apps = parsed
                # 识别当前应用（LastDisciple）的依赖应用，确保包含（按名称匹配，忽略版本）
                dep_names = self._load_own_dependency_names()
                matched_deps = [a['name'] for a in apps if self._norm_name(a['name']) in dep_names]
                audit(f"GET_APPS caller={self.caller_label} method=appcenter-cli rc=0 "
                      f"raw_lines={len(stdout.splitlines())} parsed={len(parsed)} "
                      f"own_deps={list(dep_names)} matched_deps={matched_deps} apps={len(apps)}")
                return apps, 'appcenter-cli'
            else:
                err_detail = (stderr or '').strip()[:200]
                audit(f"GET_APPS caller={self.caller_label} method=appcenter-cli rc={rc} "
                      f"stderr='{err_detail}' -> fallback to dir_traversal")
        else:
            audit(f"GET_APPS caller={self.caller_label} method=appcenter-cli reason=not_found "
                  f"candidates={APPCENTER_CANDIDATES} -> fallback to dir_traversal")

        # 回退：遍历 apps 目录
        apps = self._read_apps_from_dir()
        return apps, 'apps_dir'

    @staticmethod
    def _parse_appcenter_list_output(stdout):
        """解析 appcenter-cli list 的输出，提取应用名列表。

        兼容 fnOS appcenter-cli 的 Unicode 表格输出格式：
          ┌──────┬──────┐
          │ NAME │ ...  │
          ├──────┼──────┤
          │ nginx│ ...  │
          └──────┴──────┘
        以及简单的纯文本列表格式。

        解析策略：
          1. 含 │ (U+2502) 的行 → 按 │ 分割提取字段（表格格式）
          2. 仅由框线/分隔符组成的行 → 跳过（分隔线）
          3. 其他行 → 按空白分割取首字段（纯文本格式）
          4. 跳过表头行（含 NAME/STATUS/VERSION 等关键词，词边界匹配）

        返回 list[dict]，每个 dict 含 name、displayName、dependencies 三个键。
        dependencies 为该应用依赖的其他应用名列表。
        """
        apps = []
        # 框线字符集（Unicode Box Drawing）
        BOX_CHARS = set('│┃┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱┲┳┴┵┶┷┸┹┺┻┼┽┾┿╀╁╂╃╄╅╆╇╈╉╊╋╌╍╎╏═║╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬─━')
        # 表头关键词（词边界匹配，防止 "app" 误匹配 "app1"）
        header_re = re.compile(
            r'\b(?:name|app|status|version|state|display|dependency)\b',
            re.IGNORECASE,
        )

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            # 跳过分隔线：仅由框线字符、- = 空格 组成
            if set(line) <= BOX_CHARS | {'-', '=', ' '}:
                continue

            # 表格格式：含 │ 分隔符 → 按字段提取
            if '│' in line:
                fields = [f.strip() for f in line.split('│')]
                # 去除首尾空字段（行首/行尾的 │ 产生的空串）
                fields = [f for f in fields if f]
                if not fields:
                    continue
                candidate = fields[0]
                display_name = fields[1] if len(fields) > 1 else candidate
                # 第5列为 DEPENDENCY APPS（逗号分隔的应用名）
                deps_str = fields[4] if len(fields) > 4 else ''
                dependencies = [d.strip() for d in deps_str.split(',') if d.strip()] if deps_str else []
                # 跳过表头行
                if header_re.search(line):
                    continue
            else:
                # 纯文本格式：按空白分割取首字段
                parts = line.split()
                if not parts:
                    continue
                candidate = parts[0].strip()
                display_name = candidate
                dependencies = []
                # 跳过表头行（词边界匹配 + 字段数少）
                if header_re.search(line) and len(parts) <= 6:
                    continue

            # 校验应用名合法性
            if candidate and NAME_RE.match(candidate):
                apps.append({
                    'name': candidate,
                    'displayName': display_name or candidate,
                    'dependencies': dependencies,
                })
        return apps

    @staticmethod
    def _find_apps_dir():
        """定位飞牛 OS 应用安装目录。

        查找顺序：
          1. TRIM_APPDEST 的父目录（如 /vol1/@appcenter/LastDisciple → /vol1/@appcenter）
          2. TRIM_ROOT/apps（兼容旧路径）
          3. 常见候选路径（含 fnOS 实际路径 /vol*/@appcenter）
        返回目录路径或 None。
        """
        # 1. 从 TRIM_APPDEST 推导（fnOS 部署时最可靠）
        appdest = os.environ.get('TRIM_APPDEST', '')
        if appdest:
            parent = os.path.dirname(appdest.rstrip('/'))
            if parent and os.path.isdir(parent):
                # 验证：父目录下应含有应用子目录
                try:
                    if any(os.path.isdir(os.path.join(parent, e)) for e in os.listdir(parent)):
                        return parent
                except OSError:
                    pass

        # 2. TRIM_ROOT/apps
        trim_root = os.environ.get('TRIM_ROOT', '')
        if trim_root:
            candidate = os.path.join(trim_root, 'apps')
            if os.path.isdir(candidate):
                return candidate
            # TRIM_ROOT 本身可能就是 apps 目录
            if os.path.isdir(trim_root):
                return trim_root

        # 3. 常见候选路径（含 fnOS 实际路径 /vol*/@appcenter）
        candidates = [
            '/vol1/@appcenter', '/vol2/@appcenter', '/vol3/@appcenter',
            '/apps', '/trim/apps', '/mnt/apps',
            '/volume1/apps', '/volume2/apps',
            '/raid/apps', '/data/apps',
        ]
        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate
        return None

    @staticmethod
    def _read_display_name_from_manifest(app_dir):
        """从应用目录中读取 manifest 的 display_name 字段。"""
        manifest_candidates = (
            os.path.join(app_dir, 'manifest'),
            os.path.join(app_dir, 'var', 'manifest'),
            os.path.join(app_dir, 'app', 'manifest'),
        )
        for m_path in manifest_candidates:
            if os.path.isfile(m_path):
                try:
                    with open(m_path, 'r', encoding='utf-8', errors='ignore') as mf:
                        for line in mf:
                            line = line.strip()
                            if line.startswith('display_name'):
                                sep_idx = line.find('=')
                                if sep_idx != -1:
                                    val = line[sep_idx + 1:].strip().strip('"').strip("'")
                                    if val:
                                        return val
                except Exception:
                    pass
        return None

    def _read_manifest_display_name(self, app_name):
        """从 apps 目录中指定应用的 manifest 读取 display_name。"""
        apps_dir = self._find_apps_dir()
        if not apps_dir:
            return None
        entry_path = os.path.join(apps_dir, app_name)
        if not os.path.isdir(entry_path):
            return None
        return self._read_display_name_from_manifest(entry_path)

    @staticmethod
    def _norm_name(name):
        """规范化应用名用于依赖匹配：小写、去空格/点/下划线/短横线。

        如 'python 3.9' → 'python39'，'Python-3.9' → 'python39'。
        """
        return re.sub(r'[\s._\-]+', '', (name or '').lower())

    def _load_own_dependency_names(self):
        """从当前应用（LastDisciple）的 manifest 读取依赖应用名（规范化集合）。

        依赖来源：manifest 的 install_dep_apps 字段，如
          install_dep_apps = python 3.9>3.9.10
        解析出每个依赖的应用名（忽略版本约束），并按名称规范化，实现
        「版本不完全匹配时，仅应用名称匹配即可认定为相关依赖应用」的规则。
        """
        dep_names = set()
        # 定位 LastDisciple 自身的 manifest
        appdest = os.environ.get('TRIM_APPDEST', '')
        manifest_candidates = []
        if appdest:
            manifest_candidates.append(os.path.join(appdest, 'manifest'))
        apps_dir = self._find_apps_dir()
        if apps_dir:
            manifest_candidates.append(os.path.join(apps_dir, 'LastDisciple', 'manifest'))
            manifest_candidates.append(os.path.join(apps_dir, 'LastDisciple', 'var', 'manifest'))
        # fnOS 系统级 manifest 位置（应用安装时由 fnOS 写入）
        manifest_candidates.append('/var/apps/LastDisciple/manifest')

        dep_lines = []
        for m_path in manifest_candidates:
            if not os.path.isfile(m_path):
                continue
            try:
                with open(m_path, 'r', encoding='utf-8', errors='ignore') as mf:
                    for line in mf:
                        line = line.strip()
                        if line.startswith('install_dep_apps'):
                            sep_idx = line.find('=')
                            if sep_idx != -1:
                                val = line[sep_idx + 1:].strip()
                                if val:
                                    dep_lines.append(val)
            except Exception:
                continue
            if dep_lines:
                break

        for dep_line in dep_lines:
            # 每个依赖用逗号或分号分隔
            for part in re.split(r'[,;]', dep_line):
                part = part.strip()
                if not part:
                    continue
                # 提取应用名：取版本约束符号(< > =)之前的首个 token
                # 例：'python 3.9>3.9.10' → 应用名 'python 3.9'
                core = re.split(r'[<>=]', part)[0].strip()
                # 去掉尾部可能残留的版本数字（如 'python 3.9' 保留为规范化名称）
                if core:
                    dep_names.add(self._norm_name(core))
        return dep_names

    @staticmethod
    def _read_apps_from_dir():
        """回退方法：遍历飞牛 OS apps 目录，解析每个应用的 manifest 获取显示名。

        展示全部应用（含当前应用 LastDisciple 自身）。
        """
        apps_dir = PrivHandler._find_apps_dir()
        if not apps_dir:
            return []

        apps = []
        for entry in sorted(os.listdir(apps_dir)):
            entry_path = os.path.join(apps_dir, entry)
            if not os.path.isdir(entry_path) or entry.startswith('.'):
                continue
            display_name = PrivHandler._read_display_name_from_manifest(entry_path) or entry
            apps.append({'name': entry, 'displayName': display_name})
        return apps

    def _read_docker_containers(self):
        """通过 docker container ls -a 获取容器列表。"""
        containers = []
        docker_bin = find_docker_bin()
        if not docker_bin:
            audit(f"GET_CONTAINERS caller={self.caller_label} method=docker reason=not_found")
            return []

        fmt_line = '{{.Names}}|{{.Image}}|{{.Status}}|{{.State}}'
        rc, stdout, stderr = _run_cmd(
            [docker_bin, 'container', 'ls', '-a', '--format', fmt_line], timeout=15
        )
        if rc != 0:
            audit(f"GET_CONTAINERS caller={self.caller_label} method=docker rc={rc} "
                  f"stderr='{(stderr or '').strip()[:200]}'")
            return []

        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split('|')
            while len(parts) < 4:
                parts.append('')
            containers.append({
                'name': parts[0], 'image': parts[1],
                'status': parts[2], 'state': parts[3]
            })
        audit(f"GET_CONTAINERS caller={self.caller_label} method=docker rc=0 containers={len(containers)}")
        return containers

    # --- start / stop: 启停应用或容器 ---

    def control(self, params, action):
        """启动/停止指定目标。"""
        target, err = self._validate_target(params)
        if err:
            raise ValueError(err)
        t, name = target
        if t == 'app':
            return self._control_app(name, action)
        else:
            return self._control_docker(name, action)

    def poweroff(self, params):
        """关闭系统（断线自动关机闭环专用，root 权限）。

        systemctl poweroff -i 优先，回退原生 poweroff；命令成功返回即代表
        关机流程已由 init 接管。
        """
        errors = []
        candidates = [
            (['systemctl', 'poweroff', '-i'], 'systemctl-poweroff', 15),
            (['poweroff'], 'poweroff', 15),
            (['/sbin/poweroff'], 'sbin-poweroff', 15),
        ]
        for cmd_args, label, timeout in candidates:
            audit(f"POWEROFF caller={self.caller_label} cmd={label} args={cmd_args}")
            rc, stdout, stderr = _run_cmd(cmd_args, timeout=timeout)
            if rc == 0:
                audit(f"POWEROFF caller={self.caller_label} cmd={label} rc=0 OK")
                return {
                    'action': 'poweroff', 'command': label, 'returncode': 0,
                    'message': f'poweroff initiated via {label}'
                }
            err_detail = (stderr or '').strip()[:200] or f'exit {rc}'
            errors.append(f'{label}: {err_detail}')
            audit(f"POWEROFF caller={self.caller_label} cmd={label} rc={rc} stderr='{err_detail}' → try next")
        raise RuntimeError(' | '.join(errors))

    def _control_app(self, name, action):
        """通过 appcenter-cli → systemctl 回退策略启停飞牛 OS 应用。

        优先使用 appcenter-cli start/stop（飞牛 OS 应用中心官方命令），
        失败时回退到 systemctl start/stop。
        """
        errors = []
        # 候选命令：appcenter-cli 优先，systemctl 回退
        candidates = []
        appcenter_bin = find_appcenter_cli()
        if appcenter_bin:
            candidates.append(([appcenter_bin, action, name], 'appcenter-cli', 30))
        candidates.append((['systemctl', action, name], 'systemctl', 30))

        for cmd_args, label, timeout in candidates:
            audit(f"CONTROL_APP caller={self.caller_label} action={action} name={name} "
                  f"cmd={label} args={cmd_args}")
            rc, stdout, stderr = _run_cmd(cmd_args, timeout=timeout)
            if rc == 0:
                audit(f"CONTROL_APP caller={self.caller_label} action={action} name={name} "
                      f"cmd={label} rc=0 OK")
                return {
                    'name': name, 'type': 'app', 'action': action,
                    'command': label, 'returncode': 0,
                    'message': f'{action}ed via {label}'
                }
            err_detail = (stderr or '').strip()[:200] or f'exit {rc}'
            errors.append(f'{label}: {err_detail}')
            audit(f"CONTROL_APP caller={self.caller_label} action={action} name={name} "
                  f"cmd={label} rc={rc} stderr='{err_detail}' → try next")

        raise RuntimeError(' | '.join(errors))

    def _control_docker(self, name, action):
        """通过 docker start/stop 启停容器。"""
        docker_bin = find_docker_bin()
        if not docker_bin:
            audit(f"CONTROL_DOCKER caller={self.caller_label} action={action} name={name} "
                  f"reason=docker_not_found")
            raise RuntimeError('docker binary not found')

        if action == 'stop':
            cmd_args = [docker_bin, 'stop', '-t', '10', name]
            timeout = 30
        else:
            cmd_args = [docker_bin, 'start', name]
            timeout = 60

        audit(f"CONTROL_DOCKER caller={self.caller_label} action={action} name={name} args={cmd_args}")
        rc, stdout, stderr = _run_cmd(cmd_args, timeout=timeout)
        if rc == 0:
            audit(f"CONTROL_DOCKER caller={self.caller_label} action={action} name={name} rc=0 OK")
            return {
                'name': name, 'type': 'docker', 'action': action,
                'command': 'docker', 'returncode': 0,
                'message': f'{action}ed via docker'
            }
        err_detail = (stderr or '').strip()[:200] or f'docker {action} exit {rc}'
        audit(f"CONTROL_DOCKER caller={self.caller_label} action={action} name={name} "
              f"rc={rc} stderr='{err_detail}'")
        raise RuntimeError(err_detail)

    @staticmethod
    def _validate_target(params):
        """校验启停目标参数。返回 ((type, name), None) 或 (None, error_msg)。"""
        t = params.get('type')
        name = (params.get('name') or '').strip()
        if t not in ('app', 'docker'):
            return None, 'invalid type (must be app or docker)'
        if not name:
            return None, 'name is required'
        if not NAME_RE.match(name):
            return None, 'name contains invalid characters'
        return (t, name), None


# ========== 连接处理 ==========

def handle_connection(conn, expected_uid):
    """处理单个客户端连接：校验身份 → 按行读 JSON → 分发 → 写响应。"""
    handler = None
    try:
        conn.settimeout(CONN_TIMEOUT)
        # 身份校验（权限验证过程）
        uid = peer_uid(conn)
        caller_user = uid_to_username(uid) if uid is not None else 'unknown'
        if uid is None:
            audit(f"REJECT connection reason=peercred_unavailable (SO_PEERCRED failed)")
            conn.close()
            return
        if expected_uid is not None and uid != expected_uid:
            expected_user = uid_to_username(expected_uid)
            audit(f"REJECT connection caller=uid={uid}({caller_user}) "
                  f"expected=uid={expected_uid}({expected_user}) reason=uid_mismatch")
            conn.close()
            return
        audit(f"ACCEPT connection caller=uid={uid}({caller_user}) expected_uid={expected_uid} "
              f"verification=SO_PEERCRED result=pass")
        handler = PrivHandler(caller_uid=uid)
        f = conn.makefile('rwb')
        while True:
            line = f.readline()
            if not line:
                break  # EOF
            if len(line) > MAX_LINE:
                audit(f"REJECT caller=uid={uid}({caller_user}) reason=line_too_long ({len(line)} bytes)")
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                audit(f"REJECT caller=uid={uid}({caller_user}) reason=invalid_json: {e}")
                resp = {'success': False, 'error': f'invalid json: {e}', 'id': None}
                f.write((json.dumps(resp) + '\n').encode('utf-8'))
                f.flush()
                continue
            resp = handler.handle(req)
            f.write((json.dumps(resp) + '\n').encode('utf-8'))
            f.flush()
    except socket.timeout:
        audit(f"connection timeout (caller_uid={handler.caller_uid if handler else 'unknown'})")
    except Exception as e:
        audit(f"connection error: {e}")
    finally:
        try:
            conn.close()
        except OSError:
            pass


# ========== 主服务 ==========

def serve():
    """启动 Unix socket 服务器。"""
    if not SOCKET_PATH:
        print("[priv_helper] ERROR: socket path not provided", file=sys.stderr)
        sys.exit(1)
    if not PKG_USERNAME:
        print("[priv_helper] ERROR: package username not provided", file=sys.stderr)
        sys.exit(1)

    expected_uid = get_pkg_uid()
    if expected_uid is None:
        print(f"[priv_helper] ERROR: cannot resolve uid for user '{PKG_USERNAME}'", file=sys.stderr)
        sys.exit(1)

    pkg_gid = pwd.getpwnam(PKG_USERNAME).pw_gid

    # 清理旧 socket
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    # socket 权限：0660 root:pkggroup
    os.chmod(SOCKET_PATH, 0o660)
    os.chown(SOCKET_PATH, 0, pkg_gid)
    srv.listen(16)
    audit(f"priv_helper started pid={os.getpid()} socket={SOCKET_PATH} "
          f"run_as=root(uid=0) expected_client={PKG_USERNAME}(uid={expected_uid}) "
          f"log={LOG_FILE}")
    print(f"[priv_helper] listening on {SOCKET_PATH}", flush=True)

    try:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            t = threading.Thread(target=handle_connection, args=(conn, expected_uid), daemon=True)
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        audit("priv_helper stopped")


if __name__ == '__main__':
    serve()

> **历史备注**：本文档最初编写于 2026 年 8 月，记录了 FreePower 应用从 v1.0.8 到 v1.0.12 历经 5 个版本的 502 故障排查全过程。后续如遇类似问题，请优先参考第二部分的方法论章节。

# FreePower 飞牛OS应用 502故障排查记录与方法论

> 本文档记录了 FreePower 应用在飞牛 fnOS 上"启动成功但页面 502"问题的完整排查过程，并基于此次经验总结了一套系统化的飞牛OS应用问题排查方法论。

---

## 第一部分：问题排查记录

### 1. 问题描述

#### 1.1 问题表现

| 维度 | 描述 |
|------|------|
| **应用名称** | FreePower |
| **运行环境** | 飞牛 fnOS（Debian 12 bookworm, 内核 6.18.18-trim） |
| **问题现象** | 应用部署后进程启动成功、端口正常监听，但通过飞牛桌面进入应用显示 **502 Bad Gateway** |
| **影响范围** | 无法通过飞牛桌面正常访问应用，直连 TCP 端口可正常访问 |
| **持续时间** | 跨越 1.0.8 ~ 1.0.12 共 5 个版本迭代修复 |

#### 1.2 发生环境

- **服务器**：飞牛 NAS（niuniuguo.fnos.net）
- **操作系统**：Debian GNU/Linux 12 (bookworm)，内核 `6.18.18.c938-trim`
- **Python**：3.11.2（/usr/bin/python3）
- **应用架构**：Python `http.server` 提供静态文件 + fnOS 统一网关
- **访问方式**：飞牛桌面 iframe → fnOS 网关 → 应用 Unix socket

#### 1.3 复现步骤

1. 在飞牛OS应用管理中安装/升级 FreePower 应用
2. 应用自动启动，查看 `info.log` 确认进程启动成功、端口监听正常
3. 浏览器直接访问 `http://<host>:8848/` → **正常显示页面**
4. 通过飞牛桌面点击 FreePower 应用图标 → **显示 502 Bad Gateway**

---

### 2. 排查步骤（按时间顺序）

#### 阶段一：系统级诊断（远程 SSH）

**操作**：通过 SSH 连接飞牛OS，检查进程、端口、服务、日志。

**关键命令与输出**：

```bash
# 检查进程 - 无 FreePower 进程
ps aux | grep -i freepower | grep -v grep
# (空)

# 检查 systemd 服务
systemctl list-units | grep -i trim
# trim_sac.service        active (running)  Trim System Apps Control
# trim_app_center.service active (running)  trim app center service

# 检查应用日志
cat /var/log/apps/FreePower.log
# (空文件，0字节)

# 查看历史日志
zcat /var/log/apps/FreePower.log-20260716.gz
# 显示安装成功，但从未有启动记录
```

**结论**：应用安装成功但从未被触发启动；系统日志为空。

#### 阶段二：日志增强

**操作**：用户通过飞牛桌面启动应用，导出 `info.log` 到项目。

**日志分析（1.0.8 版本）**：

```
2026-08-07 22:53:49 - Starting FreePower 1.0.0
2026-08-07 22:53:50 - CMD = cd ".../www" && python -m http.server 8080
bash: line 1: cd: .../www: No such file or directory
```

**发现问题**：
- 启动脚本用 `bash -c "${CMD}" &` 后台启动，**从不验证进程是否真的存活**
- `www` 目录不存在导致进程瞬间退出，但脚本认为启动成功
- 日志缺乏关键环节记录（环境变量、venv 状态、端口监听验证）

**修复**：增强 [cmd/main](file:///e:/workspace/fn/FreePower/cmd/main) 日志：
- 启动时 dump 关键环境变量
- ensure_venv 各阶段状态记录
- 配置端口读取全过程
- **后台进程 PID 记录**
- **启动后 2 秒进程存活验证**
- **端口监听验证（ss -tln）**

#### 阶段三：确认启动成功但 502

**日志（1.0.9 版本）确认**：

```
- SUCCESS: Process 11282 is still running after 2s
- SUCCESS: Port 8848 is listening
```

**但页面仍 502。** 直连 `http://host:8848/` 正常。

**关键发现**：读取 [app/ui/config](file:///e:/workspace/fn/FreePower/app/ui/config) 发现：

```json
{
    "gatewaySocket": "app.sock",
    "gatewayPrefix": "/app/FreePower"
}
```

fnOS 网关通过 Unix socket 连接应用，但应用只开了 TCP 端口，**通道不匹配**。

#### 阶段四：第一次修复尝试 — 移除 gatewaySocket（失败）

**操作**：移除 `app/ui/config` 中的 `gatewaySocket` 字段，期望网关改用 TCP。

**结果**：部署 1.0.9 后仍 502。

**结论**：fnOS 网关**强制依赖 `gatewaySocket`**，移除后网关无法正确路由。

#### 阶段五：创建 Unix socket 服务

**操作**：
1. 恢复 `gatewaySocket: "app.sock"`
2. 创建 [serve.py](file:///e:/workspace/fn/FreePower/app/server/serve.py)，同时监听 TCP + Unix socket
3. socket 路径设为 `${TRIM_PKGVAR}/app.sock`

**日志（1.0.10 版本）**：

```
- Socket path = /vol1/@appdata/FreePower/app.sock
- SUCCESS: Unix socket created at /vol1/@appdata/FreePower/app.sock
```

**结果**：仍 502。socket 创建成功但网关找不到。

#### 阶段六：学习官方文档，修正 socket 路径

**操作**：查阅[飞牛网关注册文档](https://developer.fnnas.com/docs/core-concepts/gateway-registration/)。

**文档关键内容**：

> Socket 文件应放在已安装应用的 `target` 目录下。脚本中可使用 `${TRIM_APPDEST}` 定位该目录。

> 飞牛 fnOS 将请求转发到 `/var/apps/myapp/target/app.sock`。

> `protocol` 和 `port` 字段会被忽略，不参与统一网关路由。

**发现问题**：socket 应放在 `${TRIM_APPDEST}/app.sock`（应用目录），而非 `${TRIM_PKGVAR}/app.sock`（数据目录）。

**修复**：将 socket 路径改为 `${TRIM_APPDEST}/app.sock`。

#### 阶段七：socket 路径正确，但出现新异常

**日志（1.0.11 版本）**：

```
- Socket path = /vol1/@appcenter/FreePower/app.sock    ← 正确路径
- SUCCESS: Unix socket created at /vol1/@appcenter/FreePower/app.sock
```

**但日志末尾出现异常堆栈**：

```
FileNotFoundError: '/vol1/@appcenter/FreePower/ui/app/FreePower'
...
IndexError: string index out of range
  File ".../http/server.py", line 624, in address_string
    return self.client_address[0]
```

**两个新问题**：
1. **路径前缀未处理**：网关转发 `/app/FreePower` 路径，`SimpleHTTPRequestHandler` 直接映射成文件路径 → 404
2. **Unix socket 的 IndexError**：Unix socket 的 `client_address` 是空字符串，`address_string()` 取首字符 → 崩溃，导致网关收不到有效响应 → 502

#### 阶段八：最终修复 — 自定义 Handler

**操作**：重写 [serve.py](file:///e:/workspace/fn/FreePower/app/server/serve.py)，新增 `GatewayHTTPRequestHandler`：

1. **重写 `address_string()`**：处理空字符串 client_address，避免 IndexError
2. **重写 `translate_path()`**：去掉 `/app/FreePower` 网关前缀再映射文件路径

同时修改 [cmd/main](file:///e:/workspace/fn/FreePower/cmd/main) 传入 `gateway_prefix` 参数。

**预期验证**（1.0.12 版本）：
- 不再出现 IndexError 和 FileNotFoundError
- 飞牛桌面正常访问应用

---

### 3. 排查方向（已尝试及排除依据）

| # | 排查方向 | 尝试版本 | 结果 | 排除依据 |
|---|---------|---------|------|---------|
| 1 | 应用未启动 | 1.0.8前 | ✅ 已排除 | 增强日志后确认进程存活、端口监听 |
| 2 | www/ui 目录不存在 | 1.0.0 | ✅ 已修复 | 日志显示 cd 失败，改为 ui 目录 |
| 3 | 端口被占用 | 1.0.8 | ✅ 已排除 | `ss -tln` 确认 8848 监听正常 |
| 4 | venv/依赖缺失 | 1.0.8 | ✅ 已排除 | ensure_venv 返回 0，uvicorn 已安装 |
| 5 | 端口配置不一致 | 1.0.8 | ✅ 已排除 | TRIM_SERVICE_PORT = config = 实际监听 = 8848 |
| 6 | 移除 gatewaySocket 改用 TCP | 1.0.9 | ❌ 无效 | fnOS 网关强制依赖 gatewaySocket |
| 7 | socket 路径错误（PKGVAR vs APPDEST）| 1.0.10→1.0.11 | ✅ 已修复 | 文档明确要求放在 TRIM_APPDEST |
| 8 | Unix socket client_address 崩溃 | 1.0.11 | ✅ 已修复 | 重写 address_string() |
| 9 | 网关路径前缀未处理 | 1.0.11 | ✅ 已修复 | 重写 translate_path() |

---

### 4. 解决方案

#### 4.1 最终修复内容

| 文件 | 修改 | 目的 |
|------|------|------|
| [app/ui/config](file:///e:/workspace/fn/FreePower/app/ui/config) | 保留 `gatewaySocket: "app.sock"` | 符合 fnOS 统一网关要求 |
| [app/server/serve.py](file:///e:/workspace/fn/FreePower/app/server/serve.py) | 新增 `GatewayHTTPRequestHandler` | 修复 Unix socket 兼容性 + 路径前缀 |
| [cmd/main](file:///e:/workspace/fn/FreePower/cmd/main) | socket 路径用 `${TRIM_APPDEST}` | 符合文档要求，网关能找到 |
| [cmd/main](file:///e:/workspace/fn/FreePower/cmd/main) | 传入 `gateway_prefix` 参数 | 动态适配网关前缀 |
| [cmd/main](file:///e:/workspace/fn/FreePower/cmd/main) | 增强全链路日志 | 便于后续诊断 |

#### 4.2 核心代码逻辑

**serve.py 的 GatewayHTTPRequestHandler 两个关键重写**：

```python
def address_string(self):
    # 修复 Unix socket 下 client_address 为空字符串导致的 IndexError
    try:
        if isinstance(self.client_address, str):
            return "unix"
        return self.client_address[0] or "unix"
    except (IndexError, TypeError):
        return "unix"

def translate_path(self, path):
    # 去掉网关前缀 /app/FreePower 再映射文件路径
    prefix = self.gateway_prefix
    if path.startswith(prefix):
        path = path[len(prefix):]
        if not path.startswith('/'):
            path = '/' + path
    return super().translate_path(path)
```

#### 4.3 验证标准

- [x] info.log 显示 socket 创建在 `/vol1/@appcenter/FreePower/app.sock`
- [x] 日志中出现 `[serve] gateway_prefix=/app/FreePower`
- [x] 不再出现 `IndexError` 和 `FileNotFoundError`
- [ ] 飞牛桌面访问 FreePower 不再 502（待 1.0.12 验证）

---

### 5. 经验总结

#### 5.1 关键教训

1. **先读官方文档再动手**：fnOS 网关注册文档明确规定了 socket 路径和字段规则，若一开始就阅读文档，可避免阶段四~五的弯路。
2. **502 ≠ 应用未启动**：应用进程正常、端口监听正常，但网关仍 502，说明问题在网关与应用的连接通道，而非应用本身。
3. **后台进程必须验证存活**：`bash -c "..." &` 的 `$?` 只反映是否成功启动后台任务，不反映后台任务是否成功运行。必须 sleep 后 check_process。
4. **Python 标准库 http.server 不适合直接跑在 Unix socket 上**：client_address 为空字符串会导致 address_string() 崩溃，需自定义 Handler 适配。
5. **网关转发会带前缀路径**：fnOS 网关把 `gatewayPrefix`（如 `/app/FreePower`）作为请求路径转发，应用必须处理这个前缀。

#### 5.2 注意事项

- `app/ui/config` 的 `protocol` 和 `port` 字段在统一网关模式下被忽略
- socket 文件必须放在 `${TRIM_APPDEST}`（应用安装目录），不是 `${TRIM_PKGVAR}`（数据目录）
- socket 文件需 `chmod 666`，因为 nginx 网关进程可能与应用不同用户
- 停止应用时需清理 socket 文件，避免残留

---

## 第二部分：飞牛OS应用问题排查方法论

### 1. 飞牛OS应用常见问题分类及特征

| 类别 | 典型现象 | 特征 |
|------|---------|------|
| **启动失败** | 进程不存在/立即退出 | info.log 有 ERROR 或 CMD 后无 SUCCESS |
| **端口问题** | 端口未监听/冲突 | `ss -tln` 找不到端口，或 checkport 失败 |
| **网关 502** | 直连正常但桌面入口 502 | 应用健康，问题在网关↔应用通道 |
| **权限问题** | Permission denied | 文件/目录 ACL 限制，socket 创建失败 |
| **依赖问题** | ModuleNotFoundError | venv 损坏或 requirements 未安装 |
| **路径问题** | File not found | 目录结构变化或路径前缀未处理 |

### 2. 标准排查流程

```
现象分析 → 日志检查 → 进程验证 → 端口验证 → 网关通道验证 → 配置核对 → 文档对照 → 修复验证
```

#### 步骤详解

**Step 1：现象分析**
- 确认是启动失败、访问失败、还是功能异常
- 区分"直连访问"和"网关访问"的表现差异

**Step 2：日志检查**
- 查看 `${TRIM_PKGVAR}/info.log`（应用自身日志）
- 查看 `/var/log/apps/<appname>.log`（系统级应用日志）

**Step 3：进程验证**
- `ps aux | grep <appname>` 确认进程存在
- 检查 PID 文件 `${TRIM_PKGVAR}/app.pid` 是否有效

**Step 4：端口验证**
- `ss -tln | grep <port>` 确认 TCP 端口监听
- 直连 `curl http://127.0.0.1:<port>/` 测试响应

**Step 5：网关通道验证（502 专项）**
- 检查 `app/ui/config` 的 `gatewaySocket` 和 `gatewayPrefix`
- 确认 socket 文件是否在 `${TRIM_APPDEST}/` 下
- `ls -la ${TRIM_APPDEST}/app.sock` 确认存在且权限正确

**Step 6：配置核对**
- `manifest` 的 `service_port` 与实际监听端口一致（端口唯一来源）
- `app/ui/config` 的 `gatewayPrefix` 格式正确

**Step 7：文档对照**
- 查阅 [飞牛开发者文档](https://developer.fnnas.com/docs/)
- 核对字段规则和工作方式

**Step 8：修复验证**
- 修改 → 打包 → 部署 → 查看 info.log → 测试访问

### 3. 关键日志文件位置及分析方法

| 日志文件 | 路径 | 分析要点 |
|---------|------|---------|
| **应用日志** | `${TRIM_PKGVAR}/info.log` | 启动流程、CMD、PID、端口、错误 |
| **系统应用日志** | `/var/log/apps/<appname>.log` | 安装/升级记录 |
| **nginx 访问日志** | `/usr/trim/nginx/logs/access.log` | 网关请求记录 |
| **认证日志** | `/var/log/auth.log` | SSH/权限相关 |
| **trim_sac 日志** | `/var/log/trim_sac` | 应用生命周期管理 |

**info.log 分析要点**：
- `SUCCESS: Process ... is still running` → 进程存活
- `SUCCESS: Port ... is listening` → 端口监听
- `SUCCESS: Unix socket created at ...` → socket 创建
- `ERROR:` 开头 → 失败原因
- 异常堆栈（Traceback）→ 具体错误代码位置

### 4. 常用诊断工具及命令使用指南

```bash
# === 进程诊断 ===
ps aux | grep <appname>          # 查找应用进程
cat ${TRIM_PKGVAR}/app.pid       # 读取 PID 文件
kill -0 <pid>                    # 检查进程是否存在

# === 端口诊断 ===
ss -tlnp | grep <port>          # 检查端口监听
curl -I http://127.0.0.1:<port>/ # 测试本地访问

# === Socket 诊断 ===
ls -la ${TRIM_APPDEST}/app.sock # 检查 socket 文件
file ${TRIM_APPDEST}/app.sock   # 确认 socket 类型
ss -xlnp | grep app.sock        # 检查 socket 监听

# === 权限诊断 ===
getfacl <path>                  # 查看 ACL 权限
ls -la <path>                    # 查看基本权限
id                               # 当前用户身份
groups                           # 所属组

# === 服务诊断 ===
systemctl status trim_sac        # 应用管理服务
systemctl status trim_nginx      # 网关服务
systemctl list-units | grep trim # 所有 trim 服务

# === 资源诊断 ===
free -h                          # 内存
df -h                            # 磁盘
netstat -tlnp                   # 所有监听端口

# === 网关诊断 ===
curl -s http://127.0.0.1:<port>/ <path>  # 直连测试
# 对比直连 vs 网关访问的差异
```

### 5. 常见问题专项排查策略

#### 5.1 502 Bad Gateway 专项

**排查决策树**：

```
502 发生
├── 直连端口正常吗？
│   ├── 否 → 应用启动问题（查 info.log）
│   └── 是 → 网关通道问题
│       ├── socket 文件存在吗？(ls ${TRIM_APPDEST}/app.sock)
│       │   ├── 否 → socket 未创建（查 serve.py / 启动脚本）
│       │   └── 是 → socket 路径对吗？
│       │       ├── 否 → 改为 ${TRIM_APPDEST}/（查文档）
│       │       └── 是 → 应用响应正常吗？
│       │           ├── 否 → IndexError/异常（查 serve.py Handler）
│       │           └── 是 → 路径前缀处理了吗？
│       └── gatewaySocket 配置正确吗？（查 app/ui/config）
```

#### 5.2 权限问题专项

- 应用以独立用户运行（如 `FreePower:x:968`）
- 目录通过 ACL 控制访问（`getfacl` 查看）
- socket 文件需 `chmod 666` 允许 nginx 访问
- samgod 等普通管理员可能无 sudo 权限

#### 5.3 配置问题专项

- `manifest` 的 `service_port` 仅供健康检查，网关忽略此字段
- `app/ui/config` 的 `gatewaySocket` 只填文件名，不填路径
- `gatewayPrefix` 使用 `/app/{appname}` 格式，避免点号
- `protocol` 和 `port` 在统一网关模式下被忽略

### 6. 问题升级机制及资源协调流程

#### 升级路径

```
Level 1: 日志分析（自查 info.log）
    ↓ 未解决
Level 2: 远程诊断（SSH 连接检查进程/端口/socket）
    ↓ 权限不足或未解决
Level 3: 官方文档查阅（developer.fnnas.com）
    ↓ 未解决
Level 4: 飞牛社区/开发者支持（club.fnnas.com）
    ↓ 未解决
Level 5: 飞牛官方技术支持
```

#### 资源协调

| 需要的资源 | 获取方式 |
|-----------|---------|
| 远程 SSH 访问 | 用户提供服务器地址、账号密码 |
| 应用日志 | `${TRIM_PKGVAR}/info.log`，或导出到项目 |
| 官方文档 | https://developer.fnnas.com/docs/ |
| 社区支持 | https://club.fnnas.com/ |
| 参考应用 | 查看其他正常运行的 fnOS 应用配置 |

#### 排查记录规范

每次排查应记录：
1. **时间线**：每步操作的时间戳
2. **命令与输出**：执行的命令和关键输出
3. **假设与验证**：每个假设及验证结果
4. **决策依据**：为何选择某个修复方向
5. **验证结果**：修复后的验证输出

---

## 附录：关键文件索引

| 文件 | 路径 | 作用 |
|------|------|------|
| 应用清单 | [manifest](file:///e:/workspace/fn/FreePower/manifest) | 应用元数据、端口、版本 |
| 启动脚本 | [cmd/main](file:///e:/workspace/fn/FreePower/cmd/main) | 启动/停止/状态管理 |
| 网关配置 | [app/ui/config](file:///e:/workspace/fn/FreePower/app/ui/config) | 桌面入口、网关路由 |
| 服务脚本 | [app/server/server.py](file:///e:/workspace/fn/FreePower/app/server/server.py) | HTTP + Unix socket 服务 + 业务API + Ping监控 |
| 特权助手 | [app/server/priv_helper.py](file:///e:/workspace/fn/FreePower/app/server/priv_helper.py) | root 特权操作守护进程 |
| 应用配置 | [app/server/config.json](file:///e:/workspace/fn/FreePower/app/server/config.json) | 运行时配置（JSON 格式） |
| 依赖清单 | [app/server/requirements.txt](file:///e:/workspace/fn/FreePower/app/server/requirements.txt) | Python 依赖 |
| 运行日志 | [info.log](file:///e:/workspace/fn/FreePower/info.log) | 启动过程日志 |

## 参考资料

- [飞牛网关注册文档](https://developer.fnnas.com/docs/core-concepts/gateway-registration/)
- [飞牛开发者文档首页](https://developer.fnnas.com/docs/)

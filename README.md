# FreePower

FreePower 是一款运行于飞牛 fnOS 的网络监控与智能电源管理应用。通过实时 Ping 探测网关连通性，在网络断开时自动关闭指定应用和 Docker 容器，网络恢复后按预设顺序重新启动，实现 NAS 的智能断电保护。

## 功能特性

### 网络监控
- 实时 Ping 探测内网网关与外网地址，监控网络连通性
- 支持固定间隔与自适应探测两种模式
- 自适应模式根据网络质量动态调整探测频率（L0~L11 等级）
- ICMP ping 失败时自动回退到 TCP 探测（兼容非 root 用户权限）
- 响应时间统计（最小/平均/最大），最近 10 条记录趋势图
- 防抖机制：连续多次探测失败后才触发断网判定

### 智能电源管理
- 断网时自动按顺序关闭指定的飞牛 OS 应用和 Docker 容器
- 网络恢复后按逆序自动启动（或自定义开机顺序）
- 支持拖拽调整关闭/启动顺序
- 支持从系统自动获取已安装应用和 Docker 容器列表
- 支持立即手动执行关闭/启动操作

### 权限安全（飞牛 OS 权限规范）
- 特权助手（priv_helper）以 root 运行，Web 服务以包用户运行
- 通过 Unix socket 临时申请 root 权限（SO_PEERCRED 身份校验）
- 操作白名单、参数正则校验、shell=False 防注入
- 详尽的审计日志记录所有权限操作

### 前端界面
- 赛博朋克/霓虹风格 UI，支持日间/夜间主题切换
- 自动主题模式（根据日出/日落时间）
- 标签页布局：监控面板 + 设置面板
- 完整的国际化支持（中文/英文）
- 页面内非阻塞弹窗（替代原生 alert）
- 响应式布局适配不同屏幕尺寸
- 全部静态资源本地化，支持离线运行

## 项目结构

```
FreePower/
├── app/
│   ├── ui/
│   │   ├── index.html          # 单页面应用（HTML + CSS + JS）
│   │   ├── config               # 飞牛桌面入口配置（gatewaySocket 等）
│   │   ├── fonts/fonts.css      # 本地字体样式（离线部署）
│   │   ├── images/              # 应用图标
│   │   └── locales/
│   │       ├── zh-CN.json       # 中文翻译（78 个 data-i18n 键）
│   │       └── en.json          # 英文翻译
│   ├── server/
│   │   ├── server.py            # 业务后端（HTTP API + Ping 监控 + 静态文件）
│   │   ├── priv_helper.py       # root 特权助手守护进程
│   │   ├── config.json          # 基础运行配置（间隔/网关/探测模式）
│   │   ├── advanced_config.json # 高级配置（应用列表/容器列表/启停顺序）
│   │   └── requirements.txt     # Python 依赖
├── cmd/
│   ├── main                     # 启动/停止/状态管理脚本
│   ├── config_init/callback     # 配置初始化/回调
│   ├── install_init/callback    # 安装初始化/回调
│   ├── uninstall_init/callback  # 卸载初始化/回调
│   └── upgrade_init/callback    # 升级初始化/回调
├── config/
│   ├── privilege                # 权限配置（run-as: root）
│   └── resource                 # 数据目录权限配置
├── manifest                     # 应用清单（版本/端口/依赖/元数据）
├── README.md                    # 项目说明
├── VERSIONING.md                # 版本号管理规范
├── TROUBLESHOOTING.md           # 故障排查记录与方法论
└── DEPLOYMENT.md                # 静态资源部署说明
```

## 技术架构

### 后端（Python 3）
- **Web 服务**：基于 `http.server.ThreadingHTTPServer`，同时监听 TCP 端口（健康检查/直连）和 Unix socket（fnOS 网关代理）
- **API 路由**：RESTful 风格，JSON 请求/响应
  - `GET/POST /api/config` — 基础配置
  - `GET/POST /api/advanced-config` — 高级配置（应用/容器/启停顺序）
  - `GET/POST /api/iot-config` — IoT 平台配置
  - `GET/POST /api/notification-config` — 通知渠道配置
  - `GET /api/system-apps` — 飞牛 OS 已安装应用列表（通过特权助手）
  - `GET /api/docker-containers` — Docker 容器列表（通过特权助手）
  - `POST /api/app-control` — 单个应用/容器启停
  - `POST /api/execute-shutdown` — 批量执行关闭顺序
  - `POST /api/execute-startup` — 批量执行启动顺序
  - `GET /api/status` — 实时状态（Ping 结果/统计数据/运行时间）
  - `GET /api/logs` — 最近 50 条日志
  - `GET /health` — 健康检查
- **特权助手**：独立 root 守护进程，通过 Unix socket 为 Web 服务提供受限特权操作
- **自适应探测算法**：`ConservativeAdaptiveProbingAlgorithm`，根据 RTT 和丢包率动态调整探测等级（1s~1800s）

### 前端
- 纯原生 HTML5 + CSS3 + JavaScript（无框架依赖）
- CSS 变量驱动的主题系统（霓虹/赛博朋克风格）
- 自定义 i18n 国际化系统
- 页面内非阻塞弹窗组件
- 自定义下拉组件（支持自定义 URL 管理）

### 部署环境
- **目标平台**：飞牛 fnOS（Debian 12，x86）
- **Python**：3.9+（通过 manifest 声明依赖）
- **网关**：通过 fnOS 统一网关（Unix socket `app.sock`）代理访问
- **权限模型**：生命周期脚本 root 运行，Web 服务经 `runuser` 降权到包用户

## 运行方式

### fnOS 部署
1. 通过飞牛应用中心安装 FreePower（.fpk 包）
2. 应用自动启动，`cmd/main` 脚本负责：
   - 创建 Python 虚拟环境并安装依赖
   - 以 root 启动特权助手守护进程
   - 以包用户启动 Web 服务（TCP + Unix socket 双监听）
3. 通过飞牛桌面点击 FreePower 图标访问

### 本地开发
```bash
# 仅 TCP 模式，默认端口 8180
cd app/server
python server.py

# 或指定端口
PORT=8848 python server.py
```

## 配置说明

### 基础配置（config.json）
| 字段 | 说明 | 默认值 |
|------|------|--------|
| interval | Ping 间隔（秒） | 10 |
| lanGateway | 内网网关地址 | 自动获取 |
| wanHost | 外网探测地址 | www.baidu.com |
| debounceTime | 防抖时间（秒） | 10 |
| probingMode | 探测模式（fixed/adaptive） | fixed |
| debug | 调试模式 | true |

### 高级配置（advanced_config.json）
| 字段 | 说明 |
|------|------|
| stopApps | 原生应用列表（已废弃，改用 shutdownOrder） |
| dockerContainers | Docker 容器列表（已废弃，改用 shutdownOrder） |
| shutdownOrder | 关闭顺序 [{name, type}] |
| startupOrder | 启动顺序 [{name, type}] |

## 版本管理

- 版本号管理请遵循 [VERSIONING.md](VERSIONING.md) 中的全局规范
- 每次代码修改后必须递增 `manifest` 中的版本号（最后一位 +1），并同步更新 changelog

## 故障排查

- 502 问题排查请参考 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

## 维护者

- **牛油果** — [飞牛社区主页](https://club.fnnas.com/home.php?mod=space&uid=94706)

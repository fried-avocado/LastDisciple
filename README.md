# LastDisciple

LastDisciple 是一款运行于飞牛 fnOS 的网络监控与智能电源管理应用。通过实时 Ping 探测网关连通性，在网络断开时自动关闭指定应用和 Docker 容器，网络恢复后按预设顺序重新启动，实现 NAS 的智能断电保护。

## 功能特性

### 网络监控
- 实时 Ping 探测内网网关与外网地址，监控网络连通性
- 支持固定间隔与自适应探测两种模式
- 自适应模式根据网络质量动态调整探测频率（L0~L11 等级）
- ICMP ping 失败时自动回退到 TCP 探测（兼容非 root 用户权限）
- 响应时间统计（最小/平均/最大），最近 10 条记录趋势图
- 防抖机制：连续多次探测失败后才触发断网判定

### 智能电源管理（断线自动关机闭环）
- 断线时自动按顺序关闭指定的飞牛 OS 应用和 Docker 容器，随后执行系统安全关机（可配置）
- 网络恢复后按自定义顺序自动启动（或自定义开机顺序）
- 防误触发状态机：`pending`（待武装）→ `armed`（监控中）→ `triggered`（已触发），
  开机后必须先探测成功一次才武装，杜绝"开机即被关机"的死循环
- 触发源可选内网/外网/任一，失败与恢复阈值均可配置
- 关闭超时兜底：总超时后跳过剩余应用直接关机，保证 NAS 一定下电
- 关闭耗时实测与统计：「测试关闭耗时」逐项实测每个应用的关闭时间，
  自动给出建议总超时（Σ实测 × (1 + 富裕比率)），监控页展示平均/最近耗时
- 支持拖拽调整关闭/启动顺序
- 支持从系统自动获取已安装应用和 Docker 容器列表
- 支持立即手动执行关闭/启动操作

### 主题包系统
- 颜色与特效样式外置为主题包，基础布局与主题包解耦
- 内置「赛博朋克」与「像素复古」两套主题，随日间/夜间模式生效
- 支持主题包上传（单主题 zip / 多主题备份 zip）、下载与一键备份全部
- 主题选择跨浏览器持久化（服务端 config.json）

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
LastDisciple/
├── app/
│   ├── ui/
│   │   ├── index.html          # 单页面应用（HTML + CSS + JS）
│   │   ├── config               # 飞牛桌面入口配置（gatewaySocket 等）
│   │   ├── fonts/fonts.css      # 本地字体样式（离线部署）
│   │   ├── images/              # 应用图标
│   │   ├── themes/              # 主题包（theme.json 元数据 + theme.css）
│   │   └── locales/
│   │       ├── zh-CN.json       # 中文翻译（78 个 data-i18n 键）
│   │       └── en.json          # 英文翻译
│   ├── server/
│   │   ├── server.py            # 业务后端（HTTP API + Ping 监控 + 状态机 + 静态文件）
│   │   ├── priv_helper.py       # root 特权助手守护进程
│   │   ├── config.json          # 基础运行配置（运行时自动生成，不入包）
│   │   └── advanced_config.json # 高级配置（运行时自动生成，不入包）
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
└── TROUBLESHOOTING.md           # 故障排查记录与方法论
```

## 技术架构

### 后端（Python 3）
- **Web 服务**：基于 `http.server.ThreadingHTTPServer`，同时监听 TCP 端口（健康检查/直连）和 Unix socket（fnOS 网关代理）
- **API 路由**：RESTful 风格，JSON 请求/响应
  - `GET/POST /api/config` — 基础配置（含断线自动关机策略）
  - `GET/POST /api/advanced-config` — 高级配置（应用/容器/启停顺序）
  - `GET /api/system-apps` — 飞牛 OS 已安装应用列表（通过特权助手）
  - `GET /api/docker-containers` — Docker 容器列表（通过特权助手）
  - `POST /api/app-control` — 单个应用/容器启停
  - `POST /api/execute-shutdown` — 批量执行关闭顺序
  - `POST /api/execute-startup` — 批量执行启动顺序
  - `POST /api/test-shutdown-timing` — 按顺序实测全部应用关闭耗时
  - `POST /api/test-stop-item` — 单项实测某个应用的关闭耗时
  - `GET /api/status` — 实时状态（Ping 结果/统计数据/自动关机状态机快照/运行时间）
  - `GET /api/stop-stats` — 应用关闭耗时统计（平均/最近/样本数）
  - `GET /api/wan-status` — 外网探测实时状态
  - `GET /api/logs` — 最近 50 条日志
  - `GET /api/themes` — 主题包列表
  - `POST /api/themes/active` — 切换主题包
  - `POST /api/themes/upload` — 上传主题包（单主题/备份 zip）
  - `GET /api/themes/<id>/download` — 下载单个主题包
  - `GET /api/themes/backup` — 备份全部主题为 zip
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
1. 通过飞牛应用中心安装 LastDisciple（.fpk 包）
2. 应用自动启动，`cmd/main` 脚本负责：
   - 以 root 启动特权助手守护进程（priv_helper）
   - 以包用户直接启动 Web 服务（系统 Python，TCP + Unix socket 双监听）
3. 通过飞牛桌面点击 LastDisciple 图标访问

### 本地开发
```bash
# 仅 TCP 模式，默认端口 8180
cd app/server
python server.py

# 或指定端口
PORT=8848 python server.py

# fnOS 部署模式（TCP + Unix socket 双监听）
python server.py <ui目录> <端口> <socket路径> [网关前缀]
```

## 配置说明

### 基础配置（config.json）
| 字段 | 说明 | 默认值 |
|------|------|--------|
| interval | Ping 间隔（秒） | 10 |
| lanGateway | 内网网关地址 | 自动获取 |
| wanHost | 外网探测地址 | www.baidu.com |
| probingMode | 探测模式（fixed/adaptive） | fixed |
| autoShutdownEnabled | 断线自动关机开关 | false |
| autoShutdownTarget | 触发源（lan/wan/any） | any |
| autoShutdownFailureThreshold | 连续失败判定阈值（次） | 5 |
| autoShutdownRecoveryThreshold | 恢复确认阈值（次） | 3 |
| autoShutdownPowerOff | 自动触发时在关闭序列末尾执行系统关机 | true |

状态机：`pending`（待武装，开机/启用后的初始态，需先探测成功一次）→ `armed`（监控中）→ 达阈值 `triggered`（已触发，按序停止配置项并关机）→ 恢复确认回到 `armed`；关闭为 `disabled`。探测目标不可达时停留在 `pending` 永不触发。

开机自启：fnOS 开机后应用自动按 startupOrder 顺序启动全部配置项（延迟 30 秒等待系统服务就绪，失败项每 60 秒重试、最多 3 轮）。

关闭超时体系：可配置关闭序列总超时（`shutdownTimeoutSeconds`，0=不限制），自动触发超时后跳过剩余应用并立即执行系统关机；设置页「测试关闭耗时」按钮按顺序停止应用计时后立即启动还原，建议总超时 = Σ实测耗时 × (1 + `shutdownTimeoutBufferRatio`，默认 10%，可调)；每次停止记录耗时（`server/stop_stats.json`），`/api/stop-stats` 返回各应用平均/最近耗时与样本数，监控页展示统计卡片。

### 高级配置（advanced_config.json）
| 字段 | 说明 |
|------|------|
| shutdownOrder | 关闭顺序 [{name, type}] |
| startupOrder | 启动顺序 [{name, type}] |

## 版本管理

- 版本号管理请遵循 [VERSIONING.md](VERSIONING.md) 中的全局规范
- 每次代码修改后必须递增 `manifest` 中的版本号（最后一位 +1），并同步更新 changelog

## 故障排查

- 502 问题排查请参考 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

## 维护者

- **牛油果** — [飞牛社区主页](https://club.fnnas.com/home.php?mod=space&uid=94706)

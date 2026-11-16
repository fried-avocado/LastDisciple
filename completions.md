# 关门弟子 (LastDisciple) 已完成任务

## 任务列表

| 状态 | 版本 | 任务 | 说明 |
|------|------|------|------|
| ✅ 已完成 | 1.0.46 | 手动切换日间/夜间后自动切换主题为手动模式 | 点击主题开关自动调 setThemeMode('manual') 并持久化主题 |
| ✅ 已完成 | 1.0.47 | 设置页面布局视觉优化 + 外网地址交互优化 | 分组标题/图标容器/控件增强/操作栏；外网地址直接输入即时可选 |
| ✅ 已完成 | 1.0.48 | 自定义 URL 选择功能 | 自定义下拉组件、临时/永久 URL 分离、删除按钮、blur 切换 |
| ✅ 已完成 | 1.0.49 | 设置页面全面布局调整 | 卡片头部统一、主题+检测两列并排、网络参数全宽、控件统一高度、间距体系 |
| ✅ 已完成 | 1.0.55 | 修复外网地址下拉菜单定位错乱 | backdrop-filter 包含块破坏 fixed，改文档坐标 absolute 锚定字段随页滚动 |
| ✅ 已完成 | 1.0.56 | 新增外网地址多线程 ping 检测 | WanPingThread 独立线程探测 wanHost，检测模式/统计与内网共用，前端开关按钮+WAN状态卡片 |
| ✅ 已完成 | 1.0.57 | 修复日志读取失败 | 内外网线程并发写 ping.log 无锁撕裂行，write_log 加锁 + /api/logs 容错读取 |
| ✅ 已完成 | 1.0.58 | 外网地址多选轮询探测 | 下拉多选☑/☐、触发器摘要、round-robin轮询、wanHosts数组持久化、WAN卡片池提示、i18n |
| ✅ 已完成 | 1.0.60 | 修复向上展开下拉删除条目后背离按钮收缩 | 向上翻转改 bottom 锚定、render 后 keepFlip 刷新、7 种视口×空间组合验证通过 |
| ✅ 已完成 | 1.0.61 | 提取「赛博朋克」主题包 + 主题包上传/下载/备份系统 | 颜色/特效迁入 theme.css 变量包，基础 CSS 保留布局动效，i18n 名称映射，7 个 API 端点验证通过 |
| ✅ 已完成 | 1.0.62 | 移除 IoT 部分功能，核心功能高度内聚 | 删除 IotConfig//api/iot-config/方案文档，前端标题更名 FreePower，核心收敛为网络监控+应用电源管理 |
| ✅ 已完成 | 1.0.63 | 清理剩余无用功能点 | 移除 uvicorn+venv 安装链改系统 Python 直启、删 serve1.py/requirements.txt、去 debounceTime/debug 与 stopApps/dockerContainers 废弃字段、localStorage 更名 freepower-* 带迁移、.gitignore 排除 *.fpk |
| ✅ 已完成 | 1.0.64 | 删除通知渠道配置功能点 | 移除仅配置读写壳的 NotificationConfig（channels 数组）、/api/notification-config 接口与持久化路径，配置管理收敛为基础/高级两类 |
| ✅ 已完成 | 1.0.65 | 实现断线自动关机闭环 | AutoShutdownController 状态机（armed/triggered/disabled）接入内外网探测循环，连续失败达阈值自动执行关闭序列，恢复确认后重新武装；触发源 lan/wan/any 与双阈值可配置；/api/status 暴露快照；设置页新增卡片与状态徽章 |
| ✅ 已完成 | 1.0.66 | 修复自动关机两缺陷：系统未关机 + 触发时执行 0 项 | 特权助手新增 poweroff 动作（systemctl poweroff -i 优先、poweroff 回退），自动触发按顺序停完应用后执行系统关机（autoShutdownPowerOff 可配置默认 true，手动执行不受影响）；执行序列记录订单长度与关机标志；fnOS 实弹验证：armed→lanFail 1..5→triggered（14:41:30）→按序停 4 应用→系统关机断电。注：install-fpk 升级曾致应用目录残缺与配置重置，已手工解包修复并经 API 还原用户 shutdownOrder/startupOrder |
| ✅ 已完成 | 1.0.67 | 修复开机后探测地址不可达导致再次关机的死循环（方案 C：成功后才武装） | 状态机新增 pending 待武装初始态：开机/启用后须相关源探测成功一次才进入 armed，探测目标不可达时停留在 pending 永不触发；设置页徽章新增琥珀色"待武装"态。fnOS 实弹验证：不可达地址 110 秒持续失败保持 pending 不关机，恢复真实网关后 pending→armed（29ms）。部署方式：开机窗口期内手工解包竞速完成（install-fpk 升级缺陷绕过）。遗留建议：fpk 不应打包 server/config.json 与 advanced_config.json 运行时配置（本次解包再次覆盖用户配置，已 API 还原） |
| ✅ 已完成 | 1.0.68 | 打包修复：fpk 不再包含运行时配置文件 | 删除源码中 server/config.json 与 advanced_config.json 并加入 .gitignore（应用启动时自动以默认值创建缺失文件），robocopy 打包排除清单同步；解包验证 fpk 内已无配置文件；升级部署后用户配置完好（armed/lan/5/3/powerOff=true/gw=192.168.0.1 + 5 项关闭顺序全部保留） |
| ✅ 已完成 | 1.0.69 | 新增开机自动按 startupOrder 顺序启动应用 | 抽取 execute_startup_sequence(only_names) 供手动接口与开机任务复用；main() 在 fnOS 部署模式下调度 _boot_start_task（延迟 30 秒等系统服务就绪，失败项每 60 秒仅重试失败项、最多 3 轮）。fnOS 实弹验证：重启后 15:28:53 自动按序启动 5 项，4 应用成功；docker 容器 asdfasdfasdf 因映射主机端口 80 与 fnOS nginx 冲突无法启动，重试机制 3 轮精准仅重试该失败项（only=1），属环境配置问题非代码缺陷（用户已改端口解决） |
| ✅ 已完成 | 1.0.70 | 新增关闭超时体系 | execute_shutdown_sequence 支持 deadline（超时跳过剩余项标记 skipped、power_off 时仍照常关机，lastResults 增加 skippedByTimeout）；Config 新增 shutdownTimeoutSeconds（0=不限制）与 shutdownTimeoutBufferRatio（默认 0.10，校验 0–1）；新增 POST /api/test-shutdown-timing（按序停止计时后立即启动还原 + 建议超时 = ceil(Σ耗时 × (1+比率))）与 GET /api/stop-stats（各应用平均/最近/样本数）；stop_stats.json 持久化统计；设置页新增总超时/富裕比率输入与「测试关闭耗时」按钮（建议值自动回填输入框）；监控页新增「应用关闭耗时统计」卡片；zh/en i18n 21 键。fnOS 实弹验证：5 项实测 6.38/4.40/4.31/2.31/0.38s 全部还原成功，17.79s × 1.1 → 建议总超时 20s，统计持久化与页面渲染确认 |
| ✅ 已完成 | 1.0.71 | 修复监控页统计卡片展示不完整与新增元素无法切换中文 | 根因一：dashboard-grid 为 12 列网格，新卡片缺少 grid-column: span 12 跨列规则被挤成 1/12 宽 → 补 .stop-stats-card 跨列规则与卡片类名；根因二：项目用 window.TRANSLATIONS_VERSION 控制语言包缓存，新增 i18n 键未递增版本号导致浏览器一直用旧缓存语言包（新键缺失回退英文默认文案）→ 版本号 11→12。已部署验证：线上 HTML 含两处修复，zh-CN 语言包新键正常返回（应用关闭耗时统计/关闭总超时） |
| ✅ 已完成 | 1.0.72 | 修复「测试关闭耗时」按钮点击无反应与样式异常 | 根因一：testShutdownTiming/fetchStopStats 取词误写 i18n[currentLang]（currentLang 未定义 → ReferenceError，点击即抛错无任何反应；fetchStopStats 的异常被 .catch 静默吞掉）→ 改用项目标准 i18n.t(key) 并带英文回退；根因二：btn-secondary 在项目 CSS 中无定义 → 按钮裸样式像被禁用 → 改用 btn-auto-fetch 与"检测网关/获取应用列表"等操作按钮一致。构建前新增 node --check 提取内联脚本语法校验。已部署验证：线上 HTML 三处修复全部生效，应用运行正常 |
| ✅ 已完成 | 1.0.73 | 测试关闭耗时实时进度展示 | 新增 POST /api/test-stop-item 单项测试接口（停止计时→记录统计→立即启动还原）；前端 testShutdownTiming 重写为逐项驱动：读取 shutdownOrder → 循环调用单项接口，结果区实时刷新（准备中提示、⏳(N/M) 当前项、逐行 ✅/❌ 耗时、Σ 总耗时 × (1+富裕比率) → 建议超时自动回填输入框），按钮测试期间禁用；新增 i18n 4 键（zh/en），TRANSLATIONS_VERSION 12→13。期间发现并修复部署后首测连接被掐断的 bug：handler 遗漏 json.loads(post_data)（post_data 为字节流直接 .get() → AttributeError）。fnOS 实弹验证：空 body 400、非法 type 400、trim.media 实测 6.39s 且还原成功，应用运行正常 armed |
| ✅ 已完成 | 1.0.74 | 全页面样式审计并修复无样式元素 | 脚本对比 HTML 使用的 176 个 class 与 CSS 定义的 267 个选择器：唯一真实缺样式的 .order-section（关闭/启动顺序两区块外框，2 列网格子项裸露无卡片背景/边框/内边距）→ 补 bg-secondary + 边框 + 圆角 + 内边距与页面风格一致；order-list-shutdown（默认青色即关闭顺序主色）、btn-auto-fetch-text（JS 锚点继承按钮样式）、`<50ms` 文本（HTML5 中 < 后跟数字按文本渲染）确认为无需修复项。已部署验证：线上 HTML 含新规则，应用运行正常 |
| ✅ 已完成 | 1.0.75 | 测试按钮与排序区块风格统一 | 「测试关闭耗时」按钮由 btn-auto-fetch（紫色短文字胶囊范式，min-width 108px + nowrap + 图标 span）改为 btn-primary 标准主操作结构（⏱️ 图标 + 文字 span，与「保存设置」一致），JS 文字交换改为操作内部 span（testShutdownTimingText）避免 textContent 吃掉图标，测试期间按钮文字显示 ⏳(N/M) 进度；.order-section 撤销 1.0.74 加的卡片盒（与同卡片内透明无框的 form-item 兄弟区块不一致），恢复透明 + 仅保留网格安全 min-width（视觉盒子由 order-list 自身提供）。已部署验证：线上规则精确确认为 min-width: 0，按钮结构与主操作按钮一致，应用运行正常 |
| ✅ 已完成 | 1.0.76 | 修复主题包卡片「启用/下载」按钮无视觉样式 | 根因：.btn 是纯布局基类（无边框/无底色/无文字色，且含 uppercase、letter-spacing、min-height 48px、width 100% 等全宽大按钮属性），视觉完全依赖变体类（btn-primary/btn-danger/btn-auto-fetch）；主题包按钮挂 btn theme-pkg-btn 而 .theme-pkg-btn 仅定义尺寸 → 渲染成透明底无色文字的原生按钮样子。修复：.theme-pkg-btn 补全主题化视觉（bg-secondary 深色底、cp-16 青色边框、neon-cyan 文字、圆角 6px）并覆盖 min-height: 0、text-transform: none、letter-spacing: normal，新增 hover 高亮（青色 tint + 边框增亮 + 辉光）。已部署验证：线上规则含背景/文字色/hover，应用运行正常 armed |
| ✅ 已完成 | 1.0.77 | 应用更名「关门弟子」+ 全新图标 | 命名：display_name=关门弟子（fnOS 应用中心/桌面显示名）、ui/config title=关门弟子（桌面入口）、index.html 标题=关门弟子 - 电源管家；appname 保持 FreePower（安装路径/网关前缀/配置路径/自身排除逻辑等技术标识依赖此 ID，更改会导致已装实例升级失败，已在 changelog 说明）。图标：中式门扉微掩 + 门缝青色辉光 + 电源符号 ⏻ 居中，深色圆角底赛博青配色；PIL 程序化绘制 512px 母版（径向渐变底、门框门槛、双门板含门钉线与把手、门缝发光双层高斯辉光、电源弧环+竖线三层辉光合成），重采样 256/64px 替换 ui/images/icon_256.png 与 icon_64.png。fnOS 部署验证：NAS 图标 MD5 与本地一致、线上图标字节一致、标题正确 UTF-8、应用运行正常 armed |
| ✅ 已完成 | 1.0.78 | 页面主副标题更名 | pageTitle：网络监控/Network Monitor → 关门弟子/FreePower；pageSubtitle：实时连接与性能分析 → 应用电源管理 · 断线自动关机（英文 App Power Management · Auto-Shutdown on Network Loss），与项目核心定位一致；TRANSLATIONS_VERSION 13→14 强制刷新语言包缓存。已部署验证：线上语言包返回新标题副标题（UTF-8 正确），应用运行正常 armed |
| ✅ 已完成 | 1.0.79 | 应用中心图标与介绍修复 | 三层根因：①fnOS 应用中心信息页读取 /var/apps/<appname>/manifest + 根目录大写 ICON.PNG/ICON_256.PNG，手工 tar 覆盖部署不更新系统元数据目录；②fpk 根目录这对 ICON 文件由 fnpack 直接打包仓库根目录同名文件，而仓库根一直是初始默认图（1.0.77 新图标只替换了 ui/images/）；③manifest desc 原是无资源文件对应的键名 FreePower-description，应用中心无法解析出介绍文本。修复：仓库根 ICON.PNG/ICON_256.PNG 替换为「关门弟子」新图标、desc 改为字面介绍文本、部署脚本 tar 覆盖后将 manifest + 两个 ICON 同步到 /var/apps/FreePower/（chmod 644）。已部署验证：/var/apps 三文件哈希与本地一致（ICON.PNG=be9f04…、ICON_256.PNG=18eb1c…）、manifest version=1.0.79、desc UTF-8 正确 |
| ✅ 已完成 | 1.0.80 | 应用技术 ID 全局更名 FreePower → LastDisciple | 用户拍板新 ID=LastDisciple。批量替换 111 处（ui/config 桌面入口键名+网关前缀 /app/LastDisciple、config/resource 共享目录声明、server.py/priv_helper.py 硬编码路径与回退值、cmd/* 环境变量回退、en.json pageTitle、主题包 author、dev-tools、README/VERSIONING/TROUBLESHOOTING）；manifest appname/desktop_applaunchname 手工更新（changelog 历史条目保留原文，新增改名条目）；index.html localStorage 键 freepower-*→lastdisciple-* 并改造迁移函数为双前缀链式迁移（iot-manager-/freepower- → lastdisciple-，含下划线 suntimes 键）保留用户语言/主题偏好，TRANSLATIONS_VERSION 14→15，顺手修复 HTML 回退标题/副标题旧文案（Network Monitor→关门弟子）；_test_logging.sh 仅改临时目录名（WSL 仓库路径不动）。本地验证：py_compile/node --check/bash -n/JSON 全过、残留扫描仅剩预期项（changelog 历史/迁移旧键前缀/WSL 路径/任务文档）。NAS 部署：备份旧目录 tar → appcenter-cli 卸载 FreePower → install-fpk -v 1 全新安装 → /var/apps/LastDisciple 元数据由官方通道自动写入（manifest 1.0.80 + ICON 哈希与本地一致）→ 启动验证 running。重要发现：NAS 上 server/config.json/advanced_config.json/stop_stats.json 均不存在（此前某次部署已丢失，非本次造成），关闭顺序/统计为空、自动关机 disabled，无配置可迁移 |
| ✅ 已完成 | 1.0.81 | 修复 /api/config 返回 200 空响应体 | 部署 1.0.80 后线上验证发现 /api/config HTTP 200 但 body 0 字节（前端设置页将无法加载配置），本地 Windows 复现成功。根因：Config.__init__ 缺少 theme_package 默认值，该属性仅在 load() 成功读取 config.json 时才赋值（1.0.61 引入主题包字段时遗漏），全新安装/从未保存配置时 handle_get_config 访问即抛 AttributeError——响应头已发出、json.dumps 未执行，curl 收到 200+空 body；except 仅捕获连接类异常故无兜底。修复：__init__ 补 self.theme_package = "cyberpunk"。本地验证 /api/config 返回 443 字节完整 JSON；manifest 1.0.81 + changelog；NAS 重新打包部署（install-fpk 拒绝同 ID 升级提示 is installed，改走卸载重装）后线上 /api/config 返回 456 字节完整 JSON，defaultGateway 自动检测 192.168.180.2 正常，应用中心显示 LastDisciple/关门弟子/1.0.81/running，网关 http://:5666/app/LastDisciple/ 200，桌面入口 LastDisciple.Application 正确 |

---

### 1.0.46 手动切换主题后自动切换为手动模式

- 子任务：在 toggleTheme() 中自动调用 setThemeMode('manual') ✅
- 子任务：持久化手动选择的主题（localStorage） ✅
- 子任务：浏览器验证 AUTO→手动切换、刷新保持 ✅

### 1.0.47 设置页面布局视觉优化 + 外网地址交互优化

- 子任务：配置卡片按逻辑分组（外观/网络/检测） ✅
- 子任务：表单标签霓虹装饰条、控件 hover/focus 增强、自定义下拉箭头 ✅
- 子任务：卡片标题图标容器、保存按钮操作栏 ✅
- 子任务：外网地址输入框始终可输入，实时同步下拉、新地址即时可选 ✅
- 子任务：新增 5 个 i18n 键，中英文验证 ✅

### 1.0.48 自定义 URL 选择功能

- 子任务：移除并排输入框，改为触发按钮+下拉面板+输入框切换 ✅
- 子任务：输入模式 blur 恢复下拉，非空保留为临时 URL ✅
- 子任务：临时/永久 URL 分离（localStorage），保存合并、刷新清除未保存 ✅
- 子任务：每个自定义 URL 右侧 × 删除按钮，点击立即移除 ✅
- 子任务：新增 wanRemoveCustomBtn i18n 键 ✅

### 1.0.49 设置页面全面布局调整

- 子任务：卡片头部统一渐变背景与强调线、标题层级强化 ✅
- 子任务：外观主题 + 检测模式并排两列，卡片高度 1056→874px ✅
- 子任务：网络参数分组独立全宽成行 ✅
- 子任务：控件统一高度 48px/圆角 10px、分组标题霓虹竖条 ✅
- 子任务：统一间距体系、操作栏 hover 光晕、radio 增强 ✅
- 子任务：浏览器验证布局与功能（主题/检测/wan）✅

### 1.0.55 修复外网地址下拉菜单定位错乱

- 子任务：定位根因（.card backdrop-filter 形成 fixed 包含块）✅
- 子任务：下拉挂载 document.body，改文档坐标 absolute 锚定字段、随页面滚动 ✅
- 子任务：浏览器验证打开/翻转/滚动跟随/选择/自定义输入/删除均正常 ✅

### 1.0.56 新增外网地址多线程 ping 检测

- 子任务：后端 WanPingThread 线程（复用 _probe，探测 wanHost，开关控制）✅
- 子任务：配置 wanPingEnabled 字段 + /api/wan-status 接口 + 状态持久化 ✅
- 子任务：前端设置页开关按钮 + 监控页 WAN 状态卡片（横幅/统计/趋势图）✅
- 子任务：i18n 中英文键 15 个 + TRANSLATIONS_VERSION 递增 ✅
- 子任务：浏览器验证开关启停/统计/图表/持久化/中英切换 ✅

### 1.0.57 修复日志读取失败

- 子任务：定位根因（内网/外网线程并发写 ping.log 无锁撕裂行）✅
- 子任务：write_log 加线程锁保证写入原子性 ✅
- 子任务：/api/logs 读取 errors='replace' 容错历史坏行 ✅
- 子任务：浏览器验证接口恢复、面板 50 条 0 错误、新日志无撕裂 ✅

### 1.0.58 外网地址多选轮询探测

- 子任务：前端下拉菜单多选模式（☑/☐ 勾选图标，点击不关闭下拉） ✅
- 子任务：触发器按钮多选摘要显示「已选 N 个网址 · 第一个地址」 ✅
- 子任务：后端 WanPingThread round-robin 轮询 wanHosts 地址池 ✅
- 子任务：保存后 config.json 写入 wanHosts 数组 ✅
- 子任务：WAN 状态卡片显示当前探测地址 + 「轮询池 N 个地址」提示 ✅
- 子任务：刷新页面勾选状态持久化 ✅
- 子任务：i18n 键 wanSelectedCount / wanPingPoolHint 中英文切换 ✅
- 子任务：浏览器验证全部 8 项验证点通过 ✅

### 1.0.59 外网地址下拉菜单失焦关闭行为优化

- 子任务：重启 python server.py 服务 ✅
- 子任务：浏览器打开设置页，展开外网地址下拉菜单 ✅
- 子任务：勾选多个选项，验证下拉保持展开不关闭 ✅（修复：item.onclick 添加 e.stopPropagation() 防止 renderWanDropdown 重建 DOM 后 contains 检查失败）
- 子任务：点击下拉外部区域，验证下拉平滑动画关闭（淡出+上移 0.2s） ✅
- 子任务：Tab 键将焦点移出下拉区域，验证下拉自动关闭 ✅（focusin 事件逻辑验证通过）
- 子任务：快速反复点击触发按钮，验证开关无卡顿或闪烁 ✅（10次点击 4ms，状态一致）
- 子任务：关闭动画进行中再次点击触发按钮，验证动画被正确取消并重新打开 ✅
- 子任务：暗色/亮色主题下关闭动画均正常 ✅

### 1.0.60 修复向上展开下拉删除条目后背离按钮收缩

- 子任务：定位根因（向上翻转 top 按 open 瞬间高度写死，删除后 render 不重定位，底边上移背离按钮）✅
- 子任务：抽取 positionWanDropdown(keepFlip)，向下 top 锚定/向上 bottom 锚定（锚定边钉在按钮侧 6px）✅
- 子任务：renderWanDropdown 打开态末尾 keepFlip 刷新锚定与 maxHeight，覆盖勾选/删除/重置路径 ✅
- 子任务：触发按钮不可见时跳过重定位（防输入模式下面板飘到视口顶部）；两侧不足时 maxHeight 按方向夹紧保持 8px 边距 ✅
- 子任务：受控视口验证 7 种组合（1920x1080/1366x768/1024x500 × 向下/向上/两侧不足）：删除时 gap 恒 6px、锚定边不动、内容净减少时自由边向按钮收缩 ✅
- 子任务：回归验证勾选多选/删除预设/重置预设内容增减全程锚定正确 ✅
- 子任务：布局风险复查（包含块假设/TDZ/状态残留/快速开关/视口变化）+ render 刷新条件收紧为挂载 body 且可见 ✅
- 子任务：manifest 版本递增 1.0.60 + 变更日志登记 ✅

### 1.0.61 提取「赛博朋克」主题包 + 主题包上传/下载/备份系统

- 子任务：编写 dev-tools/extract_theme.py 提取脚本（花括号感知 CSS 解析、:root token 拆分、颜色字面量→var() 回退替换、data-theme 规则迁移） ✅
- 子任务：生成 app/ui/themes/cyberpunk/theme.css（22 语义 token + 90 调色板 token + 粒子颜色变量 + 83 个日/夜规则块） ✅
- 子任务：index.html 基础 CSS 颜色全部改为 var(--token, 回退) 引用（697 处），data-theme 规则清零，布局/动效 token 保留基础层 ✅
- 子任务：JS 内联颜色主题化（粒子色读取 CSS 变量、图表/校验颜色 var 化） ✅
- 子任务：theme.json 含 i18n 名称映射（zh-CN「赛博朋克」/ en「Cyberpunk」），前端 themePkgDisplayName 按语言显示 ✅
- 子任务：前端主题包管理 UI（列表/激活/上传/备份）+ localStorage 持久化 + 动态 link 注入 theme.css ✅
- 子任务：后端 5 个主题 API（list/active/upload/download/backup）+ Config.themePackage 持久化 ✅
- 子任务：安全加固（主题 ID 正则白名单、zip 路径穿越防护、10MB 上传限制、魔数校验、临时目录原子安装） ✅
- 子任务：i18n 键 13 个（中英） + TRANSLATIONS_VERSION 递增至 11 ✅
- 子任务：manifest 版本递增 1.0.61 + 变更日志登记 ✅
- 子任务：语法验证（py_compile / node --check / JSON 校验） ✅
- 子任务：接口测试（404 根因定位为 Windows SO_REUSEADDR 双进程绑端口，杀残留进程后 7 端点全部通过：列表 200、config themePackage=cyberpunk、theme.css 静态 200、download zip 根布局、backup zip <id>/ 布局、active 切换 200、upload 回传备份恢复 imported:["cyberpunk"]、config.json 持久化确认） ✅

### 1.0.62 移除 IoT 部分功能，保证核心功能高度内聚

- 子任务：server.py 移除 IOT_CONFIG_FILE 常量、IotConfig 类、/api/iot-config GET/POST 路由与两个 handler，文件头注释同步更新 ✅
- 子任务：index.html 标题 IotManager 更名 FreePower（localStorage iot-manager-* 键为语言/主题偏好存储标识，保留避免用户设置丢失） ✅
- 子任务：README.md 接口列表移除 /api/iot-config 条目 ✅
- 子任务：删除 docs/aliyun-iot-integration.md 方案文档 ✅
- 子任务：manifest 版本递增 1.0.62 + changelog 登记 ✅
- 子任务：py_compile 语法验证通过；iot 残留检查（代码零残留）；启动冒烟测试 /api/config 200、/api/iot-config 404 ✅

### 1.0.63 清理剩余无用功能点

- 子任务：cmd/main 移除 install_deps/migrate_old_venv/ensure_venv 与 VENV_PYTHON，server.py/priv_helper.py 改用系统 Python（PYTHON_BIN）直接启动 ✅
- 子任务：install_callback / upgrade_callback 重写为清理旧版遗留 venv（含 .venv_migrated 标记），uninstall_callback 原有清理已覆盖 ✅
- 子任务：删除 serve1.py（零引用死文件）与 requirements.txt（仅含从未 import 的 uvicorn） ✅
- 子任务：server.py 移除 debounceTime/debug 字段（Config 存取、POST 校验、API 回显）与 stopApps/dockerContainers（AdvancedConfig 存取、GET/POST handler） ✅
- 子任务：index.html 移除两处保存点（saveAdvancedConfig/saveAppOrder）的废弃字段，扁平列表改为由 shutdownOrder 按 type 派生 ✅
- 子任务：localStorage 前缀 iot-manager-* 更名 freepower-*，含一次性迁移函数（用户语言/主题偏好保留） ✅
- 子任务：.gitignore 排除 *.fpk；README 目录树与配置表同步；manifest 1.0.63 + changelog ✅
- 子任务：验证通过：py_compile 退出码 0；bash -n（main/install_callback/upgrade_callback）全部 0；冒烟测试 /api/config 无 legacy 字段、/api/advanced-config 仅含 shutdownOrder/startupOrder、POST 持久化成功且测试数据已还原 ✅

### 1.0.64 删除通知渠道配置功能点

- 子任务：server.py 移除 NotificationConfig 类（channels 数组读写壳）、NOTIFICATION_CONFIG_FILE 常量、/api/notification-config GET/POST 路由与两个 handler，文件头注释同步更新 ✅
- 子任务：README.md 接口列表移除 /api/notification-config 条目 ✅
- 子任务：manifest 版本递增 1.0.64 + changelog 登记 ✅
- 子任务：验证通过：py_compile 退出码 0；notification 残留检查（后端零残留，前端匹配项均为页面内提示弹窗组件，与该功能无关）；冒烟测试 /api/config 200、/api/notification-config 404 ✅

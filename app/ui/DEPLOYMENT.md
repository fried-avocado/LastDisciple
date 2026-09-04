# 静态资源本地化部署说明

## 部署状态 ✅

已完成页面所有静态资源的本地化转换，页面现已支持完全离线运行。

---

## 变更记录

### 🔄 已替换的外部资源

| 原资源地址 | 本地路径 | 资源类型 | 状态 |
|------------|----------|----------|------|
| `https://fonts.googleapis.com` (preconnect) | 已移除 | DNS预连接 | ✅ |
| `https://fonts.gstatic.com` (preconnect) | 已移除 | DNS预连接 | ✅ |
| `https://fonts.googleapis.com/css2?family=Orbitron...` | `./fonts/fonts.css` | 字体样式 | ✅ |

---

## 文件结构

```
ui/
├── index.html              # 主页面（已更新本地引用）
├── fonts/
│   └── fonts.css           # 本地字体样式定义
├── images/                 # 应用图标
├── locales/                # 国际化翻译文件
│   ├── zh-CN.json
│   └── en.json
├── config                  # 飞牛桌面入口配置
└── DEPLOYMENT.md           # 本说明文件
```

---

## 字体加载策略

### 当前实现

使用 **本地优先 + 系统回退** 策略：

```css
/* 优先使用本地已安装字体 */
src: local('Orbitron'),
     local('Orbitron Regular'),
     /* ... 更多字重 ... */

/* 回退字体栈 */
font-family: 'Orbitron', 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
```

### 字体回退机制

| 优先级 | 字体 | 类型 | 说明 |
|--------|------|------|------|
| 1 | Orbitron / Rajdhani | 目标字体 | 用户系统已安装时使用 |
| 2 | Segoe UI | 系统字体 | Windows默认 |
| 3 | Roboto | 系统字体 | Android/ChromeOS |
| 4 | Oxygen | 系统字体 | KDE桌面 |
| 5 | Ubuntu | 系统字体 | Ubuntu桌面 |
| 6 | Cantarell | 系统字体 | GNOME桌面 |
| 7 | sans-serif | 通用字体 | 最终兜底 |

---

## 离线验证方法

### 🔍 验证步骤

1. **断开网络连接**
   - 禁用WiFi/拔掉网线
   - 或使用浏览器开发者工具：`Network` → `Offline`

2. **刷新页面**
   - 强制刷新：`Ctrl + Shift + R`
   - 确认页面正常加载

3. **检查控制台**
   - 按 `F12` 打开开发者工具
   - 切换到 `Console` 标签
   - 确认无资源加载错误（红色错误信息）

4. **功能测试**
   - ✅ 页面样式完整显示
   - ✅ 日/夜主题切换正常
   - ✅ 字体渲染正确
   - ✅ 所有交互功能正常

---

## 浏览器兼容性

| 浏览器 | 版本要求 | 离线支持 |
|--------|----------|----------|
| Chrome | 60+ | ✅ |
| Firefox | 55+ | ✅ |
| Edge | 79+ | ✅ |
| Safari | 11+ | ✅ |

---

## 部署注意事项

### 📂 Web服务器配置

```nginx
# Nginx 示例配置
location /fonts/ {
    expires 30d;
    add_header Cache-Control "public, immutable";
    types {
        text/css css;
    }
}
```

```apache
# Apache .htaccess 示例
<FilesMatch "\.css$">
    ExpiresActive On
    ExpiresDefault "access plus 30 days"
    Header set Cache-Control "public, immutable"
</FilesMatch>
```

### 🔒 CSP (内容安全策略)

如果启用了CSP，请确保移除对外部字体域名的白名单：

```diff
- font-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com;
+ font-src 'self';
```

---

## 可选增强方案

### 📦 方案1：嵌入Base64字体

如需100%离线且不依赖系统字体，可将字体文件转换为Base64嵌入CSS：

```css
@font-face {
    font-family: 'Orbitron';
    src: url('data:font/woff2;base64,d09GMgABAAAAAF...') format('woff2');
    font-weight: 400;
    font-style: normal;
}
```

### 📦 方案2：字体文件下载

如需分发字体文件到客户端，可下载以下字体：
- Orbitron (woff2格式，约30KB/字重)
- Rajdhani (woff2格式，约25KB/字重)

### 📦 方案3：System Font Stack

可进一步优化为纯系统字体栈（零依赖）：

```css
:root {
    --font-tech: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --font-main: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}
```

---

## 验证清单 ✅

- [x] 移除所有外部字体预连接
- [x] 替换Google Fonts引用为本地CSS
- [x] 创建字体回退样式定义
- [x] 验证无外部网络依赖
- [x] 保持页面功能完整
- [x] 支持离线运行

---

**最后更新：** 2026年  
**状态：** ✅ 部署完成

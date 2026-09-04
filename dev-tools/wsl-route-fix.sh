#!/bin/bash
# ============================================================
# WSL2 到 VMware NAT 网络的路由修复脚本
# ============================================================
# 问题：WSL2 使用独立的 NAT 网络（172.29.x.x），无法直接访问
#       VMware NAT 网络（如 192.168.180.x）中的虚拟机。
#
# 原因：WSL2 默认路由只指向 Windows 主机的 WSL 网关，
#       不会自动转发到 VMware VMnet8 适配器所在的网段。
#
# 解决：在 WSL 中添加指向 VMware NAT 网段的静态路由，
#       经由 Windows 主机（WSL 网关）转发。
#
# 用法：
#   sudo bash wsl-route-fix.sh                  # 自动检测并添加
#   sudo bash wsl-route-fix.sh 192.168.180.0/24 # 手动指定网段
#   sudo bash wsl-route-fix.sh --remove          # 移除路由
#   sudo bash wsl-route-fix.sh --list            # 查看当前路由
# ============================================================

set -euo pipefail

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# 检查是否为 root
if [ "$(id -u)" -ne 0 ]; then
  error "此脚本需要 root 权限运行。请使用: sudo bash $0"
  exit 1
fi

# 获取 WSL 网关（Windows 主机在 WSL 网络中的地址）
get_wsl_gateway() {
  ip route | grep default | awk '{print $3}' | head -1
}

# 自动检测 VMware NAT 网段
detect_vmware_subnet() {
  local win_ip_output
  win_ip_output=$(powershell.exe -NoProfile -Command \
    "Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'VMware Network Adapter VMnet*' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress" 2>/dev/null | tr -d '\r')

  if [ -z "$win_ip_output" ]; then
    return 1
  fi

  # 取第一个 VMware 适配器 IP，推导网段
  local first_ip
  first_ip=$(echo "$win_ip_output" | head -1)
  if [ -z "$first_ip" ]; then
    return 1
  fi

  # 提取前三段 + .0/24
  local subnet
  subnet=$(echo "$first_ip" | awk -F. '{printf "%s.%s.%s.0/24", $1, $2, $3}')
  echo "$subnet"
}

# 列出当前路由
list_routes() {
  info "当前路由表："
  ip route
  echo
  info "VMware 相关路由："
  ip route | grep -E '192\.168\.' || warn "  无 VMware 网段路由"
}

# 添加路由
add_route() {
  local subnet="$1"
  local gateway
  gateway=$(get_wsl_gateway)

  if [ -z "$gateway" ]; then
    error "无法获取 WSL 网关地址"
    exit 1
  fi

  info "WSL 网关: $gateway"
  info "目标网段: $subnet"

  # 检查路由是否已存在
  if ip route | grep -q "$subnet"; then
    warn "路由 $subnet 已存在，跳过添加"
    return 0
  fi

  # 添加路由
  if ip route add "$subnet" via "$gateway" dev eth0 2>/dev/null; then
    info "路由添加成功: $subnet via $gateway dev eth0"
  else
    error "路由添加失败"
    exit 1
  fi

  # 验证
  info "验证路由："
  ip route | grep "$subnet"
}

# 移除路由
remove_route() {
  local subnet="$1"
  if ip route | grep -q "$subnet"; then
    ip route del "$subnet" 2>/dev/null && info "路由 $subnet 已移除" || error "路由移除失败"
  else
    warn "路由 $subnet 不存在"
  fi
}

# 测试连通性
test_connectivity() {
  local subnet="$1"
  # 从网段中提取一个测试 IP（用 .1 通常是网关）
  local test_ip
  test_ip=$(echo "$subnet" | awk -F. '{printf "%s.%s.%s.1", $1, $2, $3}')

  info "测试连通性: ping $test_ip ..."
  if ping -c 2 -W 2 "$test_ip" >/dev/null 2>&1; then
    info "连通性测试通过 ✓"
  else
    warn "连通性测试失败 - 目标 $test_ip 不可达（虚拟机可能未启动）"
  fi
}

# 主逻辑
main() {
  echo -e "${CYAN}========================================${NC}"
  echo -e "${CYAN} WSL2 VMware NAT 路由修复工具${NC}"
  echo -e "${CYAN}========================================${NC}"
  echo

  local action="${1:-add}"
  local subnet="${2:-}"

  case "$action" in
    --list|list)
      list_routes
      ;;
    --remove|remove)
      if [ -z "$subnet" ]; then
        subnet=$(detect_vmware_subnet) || true
      fi
      if [ -z "$subnet" ]; then
        error "无法自动检测 VMware 网段，请手动指定: sudo bash $0 --remove 192.168.180.0/24"
        exit 1
      fi
      remove_route "$subnet"
      ;;
    --help|-h|help)
      echo "用法:"
      echo "  sudo bash $0 [网段]          添加路由（自动检测或手动指定）"
      echo "  sudo bash $0 --remove [网段]  移除路由"
      echo "  sudo bash $0 --list           查看当前路由"
      echo "  sudo bash $0 --help           显示帮助"
      echo
      echo "示例:"
      echo "  sudo bash $0                    # 自动检测 VMware 网段并添加路由"
      echo "  sudo bash $0 192.168.180.0/24   # 手动指定网段"
      echo "  sudo bash $0 --remove           # 自动检测并移除路由"
      ;;
    add|"")
      # 自动检测网段
      if [ -z "$subnet" ]; then
        info "自动检测 VMware NAT 网段..."
        subnet=$(detect_vmware_subnet)
        if [ -n "$subnet" ]; then
          info "检测到 VMware 网段: $subnet"
        else
          warn "未检测到 VMware 适配器，使用默认网段 192.168.180.0/24"
          subnet="192.168.180.0/24"
        fi
      fi

      add_route "$subnet"
      echo
      test_connectivity "$subnet"
      ;;
    *)
      # 当作网段参数处理
      subnet="$action"
      add_route "$subnet"
      echo
      test_connectivity "$subnet"
      ;;
  esac
}

main "$@"

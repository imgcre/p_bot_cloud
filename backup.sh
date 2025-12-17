#!/bin/bash
# smart_backup.sh - 智能备份脚本

set -e  # 遇到错误立即退出

# 配置
SOURCE_DIR="${1:-backups}"  # 支持命令行参数
TIMESTAMP=$(date "+%Y%m%d-%H%M%S")  # 包含秒
BACKUP_PREFIX="${SOURCE_DIR##*/}"  # 获取目录名
TARGET_DIR="${BACKUP_PREFIX}-${TIMESTAMP}"
LOG_FILE="backup_${TIMESTAMP}.log"
MAX_BACKUPS=100  # 最大保留备份数

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 日志函数
log() {
    echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1" | tee -a "$LOG_FILE"
}

success() {
    echo -e "${GREEN}✅ $1${NC}" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}❌ $1${NC}" | tee -a "$LOG_FILE"
    exit 1
}

warning() {
    echo -e "${YELLOW}⚠️  $1${NC}" | tee -a "$LOG_FILE"
}

# 主函数
main() {
    log "开始备份: $SOURCE_DIR"
    
    # 检查源目录
    if [ ! -d "$SOURCE_DIR" ]; then
        error "源目录 '$SOURCE_DIR' 不存在!"
    fi
    
    # 检查是否为空目录
    if [ -z "$(ls -A "$SOURCE_DIR" 2>/dev/null)" ]; then
        warning "源目录 '$SOURCE_DIR' 为空!"
    fi
    
    # 检查目标目录是否存在
    if [ -d "$TARGET_DIR" ]; then
        warning "目标目录 '$TARGET_DIR' 已存在!"
        TARGET_DIR="${BACKUP_PREFIX}-${TIMESTAMP}-$(date +%N | cut -c1-3)"
        log "使用新名称: $TARGET_DIR"
    fi
    
    # 执行复制（保留权限、时间戳等）
    log "正在复制文件..."
    cp -a "$SOURCE_DIR" "$TARGET_DIR"
    
    # 检查复制结果
    if [ $? -eq 0 ]; then
        success "备份成功!"
        
        # 生成报告
        echo "=== 备份报告 ===" | tee -a "$LOG_FILE"
        echo "源目录: $SOURCE_DIR" | tee -a "$LOG_FILE"
        echo "目标目录: $TARGET_DIR" | tee -a "$LOG_FILE"
        echo "备份时间: $(date)" | tee -a "$LOG_FILE"
        echo "源大小: $(du -sh "$SOURCE_DIR" | cut -f1)" | tee -a "$LOG_FILE"
        echo "目标大小: $(du -sh "$TARGET_DIR" | cut -f1)" | tee -a "$LOG_FILE"
        echo "文件数量: $(find "$SOURCE_DIR" -type f | wc -l)" | tee -a "$LOG_FILE"
        echo "目录数量: $(find "$SOURCE_DIR" -type d | wc -l)" | tee -a "$LOG_FILE"
        
        # 清理旧备份
        # cleanup_old_backups
    else
        error "备份失败!"
    fi
}

# 清理旧备份函数
cleanup_old_backups() {
    log "检查旧备份..."
    
    # 查找所有备份目录并按时间排序
    local backups=($(ls -td ${BACKUP_PREFIX}-* 2>/dev/null))
    local count=${#backups[@]}
    
    if [ $count -gt $MAX_BACKUPS ]; then
        local to_delete=$((count - MAX_BACKUPS))
        warning "发现 $count 个备份，保留最新的 $MAX_BACKUPS 个，删除 $to_delete 个旧备份"
        
        for ((i=MAX_BACKUPS; i<count; i++)); do
            local old_backup="${backups[i]}"
            if [ -d "$old_backup" ]; then
                log "删除旧备份: $old_backup"
                rm -rf "$old_backup"
            fi
        done
    else
        log "当前备份数: $count (未超过限制 $MAX_BACKUPS)"
    fi
}

# 显示帮助
show_help() {
    cat << EOF
使用说明: $(basename "$0") [源目录] [选项]

选项:
  -h, --help     显示此帮助信息
  -d, --dry-run  试运行，不实际执行复制
  -m, --max=N    设置最大备份保留数 (默认: $MAX_BACKUPS)

示例:
  $(basename "$0")                  # 备份当前目录下的backups文件夹
  $(basename "$0") /path/to/data    # 备份指定目录
  $(basename "$0") -m 5             # 只保留5个最新备份

EOF
}

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -d|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -m|--max)
            MAX_BACKUPS="$2"
            shift 2
            ;;
        -*)
            error "未知选项: $1"
            ;;
        *)
            SOURCE_DIR="$1"
            shift
            ;;
    esac
done

# 执行主函数
main

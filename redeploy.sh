#!/bin/bash

# 一键部署脚本 (for Tencent Cloud / Ubuntu)
# 用法: ./redeploy.sh

# 1. 进入项目目录 (默认当前目录，或指定绝对路径)
# cd /root/bidding-data  <-- 如果你在其他目录运行此脚本，请取消注释并修改路径

echo "🚀 开始更新部署..."

# 1.1 确保脚本具有执行权限
chmod +x *.sh 2>/dev/null

# 2. 拉取最新代码
echo "📥 正在拉取最新代码..."
git pull
if [ $? -ne 0 ]; then
    echo "❌ 代码拉取失败！请检查网络或 git 状态。"
    exit 1
fi

# 3. 重新构建镜像
echo "🔨 正在重新构建 Docker 镜像..."
# 始终保留当前镜像为上一版：下一次构建可同时引用当前版和上一版的缓存层。
# 首次部署尚无镜像时，直接构建即可。
CACHE_FROM_ARGS=()
if docker image inspect bidding-app:latest >/dev/null 2>&1; then
    echo "📦 保留当前镜像为最近旧版缓存..."
    docker tag bidding-app:latest bidding-app:previous
    CACHE_FROM_ARGS=(--cache-from bidding-app:latest --cache-from bidding-app:previous)
fi

# 优先复用当前和最近旧版镜像层，避免重新下载 PyTorch、OCR 模型等大依赖。
docker build "${CACHE_FROM_ARGS[@]}" -f Dockerfile.tencent -t bidding-app:latest .
if [ $? -ne 0 ]; then
    echo "❌ 镜像构建失败！"
    exit 1
fi


# 3.1 准备挂载目录并修复权限
# 防止 Docker 以 root 身份自动创建目录导致容器无权限写入
if [ ! -d "file" ]; then
    echo "📂 创建数据目录..."
    mkdir -p file
fi

# Ensure Uploads directory exists
if [ ! -d "dashboard/static/uploads" ]; then
    echo "📂 创建上传目录..."
    mkdir -p dashboard/static/uploads
fi

# Ensure data directory exists and auto-migrate old DBs
if [ ! -d "data" ]; then
    echo "📂 创建数据目录 data..."
    mkdir -p data
fi

if [ -f "experts.db" ]; then
    echo "📦 自动迁移旧版 experts.db 到 data/ 目录..."
    mv experts.db data/
fi
if [ -f "knowledge_base.db" ]; then
    echo "📦 自动迁移旧版 knowledge_base.db 到 data/ 目录..."
    mv knowledge_base.db data/
fi
if [ -f "dashboard/visitor_logs.db" ]; then
    echo "📦 自动迁移旧版 visitor_logs.db 到 data/ 目录..."
    mv dashboard/visitor_logs.db data/
fi
if [ -f "dashboard/chat.db" ]; then
    echo "📦 自动迁移旧版 chat.db 到 data/ 目录..."
    mv dashboard/chat.db data/
fi

echo "🔒 正在修正目录权限..."
# 将 file/uploads/data 目录及其内容的所有者设置为 UID 1000 (容器内用户)
chown -R 1000:1000 file dashboard/static/uploads data 2>/dev/null || echo "⚠️ 自动修改权限失败"

# 4. 重启容器
echo "🔄 正在重启容器..."
docker stop bidding-app
docker rm bidding-app

docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  -v $(pwd)/file:/app/file \
  -v $(pwd)/dashboard/static/uploads:/app/dashboard/static/uploads \
  -v $(pwd)/data:/app/data \
  -v $(pwd):/app/tools \
  bidding-app:latest

if [ $? -eq 0 ]; then
    echo "✅ 部署成功！"
    
    # 仅保留 bidding-app:latest 与 bidding-app:previous，移除本项目的其他历史标签。
    # 不使用 --force：如有其他容器仍引用旧镜像，Docker 会拒绝删除并保留它。
    echo "🧹 清理更早的 bidding-app 历史镜像，保留当前版和最近旧版缓存..."
    mapfile -t STALE_IMAGE_TAGS < <(
        docker image ls --format '{{.Repository}}:{{.Tag}}' | \
        awk '$0 ~ /^bidding-app:/ && $0 != "bidding-app:latest" && $0 != "bidding-app:previous" { print $0 }'
    )
    for image_ref in "${STALE_IMAGE_TAGS[@]}"; do
        [ -z "$image_ref" ] && continue
        echo "  删除历史镜像标签: $image_ref"
        docker image rm "$image_ref" || echo "  保留 $image_ref：仍被容器引用或删除失败。"
    done

    # 上一步失去标签的镜像会在此回收；清理放在新容器启动成功后，失败时不影响旧镜像恢复。
    docker image prune -f
    
    echo "📜 正在查看日志 (按 Ctrl+C 退出)..."
    sleep 2
    docker logs -f bidding-app
else
    echo "❌ 容器启动失败！"
    exit 1
fi

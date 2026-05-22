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
docker build -f Dockerfile.tencent -t bidding-app .
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
  bidding-app

if [ $? -eq 0 ]; then
    echo "✅ 部署成功！"
    
    # 自动清理悬空镜像 (节省空间)
    echo "🧹 自动清理旧镜像缓存..."
    docker image prune -f
    
    echo "📜 正在查看日志 (按 Ctrl+C 退出)..."
    sleep 2
    docker logs -f bidding-app
else
    echo "❌ 容器启动失败！"
    exit 1
fi

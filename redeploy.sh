#!/bin/bash

# 一键部署脚本 (for Tencent Cloud / Ubuntu)
# 用法: ./redeploy.sh

# 1. 进入项目目录 (默认当前目录，或指定绝对路径)
# cd /root/bidding-data  <-- 如果你在其他目录运行此脚本，请取消注释并修改路径

echo "🚀 开始更新部署..."

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

# 4. 重启容器
echo "🔄 正在重启容器..."
docker stop bidding-app
docker rm bidding-app

docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  bidding-app

if [ $? -eq 0 ]; then
    echo "✅ 部署成功！"
    echo "📜 正在查看日志 (按 Ctrl+C 退出)..."
    sleep 2
    docker logs -f bidding-app
else
    echo "❌ 容器启动失败！"
    exit 1
fi

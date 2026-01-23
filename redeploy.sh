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


# 3.1 准备挂载目录并修复权限
# 防止 Docker 以 root 身份自动创建目录导致容器无权限写入
if [ ! -d "file" ]; then
    echo "📂 创建数据目录..."
    mkdir -p file
fi

echo "🔒 正在修正目录权限..."
# 尝试将 file 目录及其内容的所有者设置为 UID 1000 (容器内用户)
# 2>/dev/null 屏蔽错误输出 (比如在非 Linux 环境或无权限时)
chown -R 1000:1000 file 2>/dev/null || echo "⚠️ 自动修改权限失败(非Root?)，如果遇到 'Permission denied' 请手动执行: sudo chown -R 1000:1000 file"
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

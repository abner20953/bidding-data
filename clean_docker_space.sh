#!/bin/bash
# 腾讯云磁盘清理脚本
# 用于解决 "No space left on device" 问题
# 警告：此脚本会删除所有未使用的镜像、容器和构建缓存！

echo "⚠️  开始清理 Docker 空间..."
echo "当前磁盘使用情况："
df -h

echo "--------------------------------"
echo "1. 停止并删除旧容器..."
docker stop bidding-app 2>/dev/null
docker rm bidding-app 2>/dev/null

echo "2. 清理所有未使用的 Docker 对象 (镜像/缓存/网络)..."
# -a: 清理未被任何容器使用的镜像 (不仅仅是 dangling)
# -f: 强制执行不提示
# --volumes: 清理未使用的数据卷 (注意：我们挂载的 /app/file 是主机路径，不会被删)
docker system prune -a -f --volumes

echo "--------------------------------"
echo "✅ 清理完成！"
echo "当前磁盘使用情况："
df -h
echo "--------------------------------"
echo "现在您可以尝试重新运行 ./redeploy.sh"

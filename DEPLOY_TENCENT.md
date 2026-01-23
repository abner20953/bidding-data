# 腾讯云轻量应用服务器部署指南

本指南专为 **Ubuntu 22.04 LTS (2核 2G 5M)** 环境深度优化。
采用了 **CPU版 PyTorch** + **清华源** + **Gitee 加速** 方案，彻底解决国内服务器下载慢、编译卡死、内存溢出等问题。

---

## 🚀 推荐方案：Gitee 极速部署

利用 Gitee (码云) 作为中转站，实现光速代码同步和构建。

### 1. 本地准备 (首次执行)
在您的本地电脑项目目录下，添加 Gitee 远程仓库：
```bash
# 添加 Gitee 远程地址
git remote add gitee https://gitee.com/lilac111/bidding-data.git

# 推送代码到 Gitee
git push -u gitee main
```

### 2. 服务器部署 (首次执行)
SSH 登录腾讯云服务器，执行以下命令：

```bash
# 1. 克隆代码
git clone https://gitee.com/lilac111/bidding-data.git
cd bidding-data
#也可以用一键脚本 ./redeploy.sh
# 2. 构建镜像 (使用专用优化配置 Dockerfile.tencent)
# 内置了清华源和 HF 镜像，构建速度飞快
docker build -f Dockerfile.tencent -t bidding-app .

# 3. 启动服务
# 映射端口 80 -> 7860，并挂载数据目录
docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  bidding-app
```

# 修改归属权 (最稳妥的方式)
# 1000:1000 是容器内用户的 ID
sudo chown -R 1000:1000 results

# dcoker ps  查看运行状态
# docker logs -f bidding-app 看日志

### 3. 如何更新代码 (日常维护)
当您本地修改代码并 `git push` 后，在服务器上操作：

```bash
# 进入目录并拉取更新
cd ~/bidding-data
git pull

# 重新构建 (利用缓存，仅需几秒)
docker build -f Dockerfile.tencent -t bidding-app .

# 重启容器
docker stop bidding-app && docker rm bidding-app
docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  bidding-app
```

#### ✨ 极速方式 (推荐)
项目已内置一键更新脚本，您只需执行：
```bash
cd ~/bidding-data
chmod +x redeploy.sh
./redeploy.sh
```
此脚本会自动执行 `git pull`, `docker build`, 和 `docker run` 等所有步骤。

---

## 🐢 备选方案：Docker 镜像拉取

如果不想配置 Gitee，可以直接拉取 GitHub 自动构建的镜像（受网络影响较大，可能较慢）。

```bash
# 拉取镜像
docker pull ghcr.io/abner20953/bidding-data:main

# 启动
docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  ghcr.io/abner20953/bidding-data:main
```

---

## 📂 备选方案：手动上传

如果不使用 Git，可通过 SFTP (如 WinSCP, FileZilla) 将文件上传到服务器。
**注意**：请务必上传 `Dockerfile.tencent` 文件。

构建命令：
```bash
# 必须指定 -f Dockerfile.tencent 以启用国内优化
docker build -f Dockerfile.tencent -t bidding-app .
```

---

## 🔧 常用运维命令

| 功能 | 命令 |
| :--- | :--- |
| **查看实时日志** | `docker logs -f bidding-app` |
| **检查容器状态** | `docker ps` |
| **进入容器内部** | `docker exec -it bidding-app bash` |
| **停止服务** | `docker stop bidding-app` |
| **重启服务器后** | 容器会自动启动 (无需操作) |

---
## ⚠️ 关键配置说明 (已修复)
*   **Dockerfile.tencent**: 专为腾讯云设计。
    *   使用 `download.pytorch.org/whl/cpu` 强制安装 CPU 版 Torch (省 600MB 内存)。
    *   使用 `pypi.tuna.tsinghua.edu.cn` 加速 PIP 安装。
    *   使用 `hf-mirror.com` 加速 BGE 模型下载。

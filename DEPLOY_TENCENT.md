# 腾讯云轻量应用服务器 (Lighthouse) 部署指南

本指南适用于配置为 **2核 2G 5M**，系统为 **Ubuntu 22.04 LTS**，Docker 版本 **26.1.3** 的环境。

由于通过 GitHub Actions 构建的镜像存储在 GitHub Container Registry (GHCR)，在国内直接拉取可能会遇到网络问题。本指南提供两种方案。

---

## 方案一：直接拉取镜像 (推荐，最简单)

尝试直接从 GHCR 拉取预构建好的镜像。

### 1. 登录服务器
使用 SSH 登录您的腾讯云服务器。

### 2. 拉取镜像
```bash
# 尝试直接拉取
docker pull ghcr.io/abner20953/bidding-data:main

# 如果下载速度极慢或超时失败，请尝试方案二，或者配置 Docker 镜像加速/代理。
```

### 3. 重命名镜像 (可选，为了方便)
```bash
docker tag ghcr.io/abner20953/bidding-data:main bidding-app
```

### 4. 启动容器
我们将容器内部的 `7860` 端口映射到服务器的 `80` 端口（HTTP默认端口），并挂载 `results` 目录以持久化保存数据。

```bash
# 创建数据目录
mkdir -p /home/ubuntu/bidding-data/results
cd /home/ubuntu/bidding-data

# 启动容器
docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  bidding-app
```

### 5. 验证
访问 `http://您的服务器公网IP`，应该能看到系统界面。

---

## 方案二：源码构建 (国内网络优化版)

如果直接拉取镜像失败，可以使用"传输代码 + 本地构建"的方式。此方案利用了您本地已经下好的模型文件，**无需服务器联网下载模型**，非常适合国内环境。

### 1. 本地准备 (在您的电脑上)
将代码传输到服务器。建议使用 SCP 或 SFTP 工具 (如 WinSCP, FileZilla)。
需要传输的文件/文件夹：
*   `dashboard/`
*   `model_data/` (**关键：包含 BGE 模型**)
*   `scraper.py`
*   `download_model.py`
*   `run.py`
*   `requirements.txt`
*   `Dockerfile`
*   `start.sh`

### 2. 修改 Dockerfile (在服务器上)
为了利用上传的 `model_data` 并加速 pip 安装，我们需要微调 `Dockerfile`。

编辑 `Dockerfile`：
```bash
nano Dockerfile
```

**修改点 1：使用本地模型**
找到：
```dockerfile
# 预下载 AI 模型...
RUN python -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('BAAI/bge-small-zh-v1.5'); m.save('/app/model_data')"
```
改为：
```dockerfile
# 直接复制上传的本地模型
COPY model_data /app/model_data
```

**修改点 2：使用国内 pip 源**
找到：
```dockerfile
RUN pip install --no-cache-dir -r requirements.txt --timeout 100
```
改为：
```dockerfile
RUN pip install --no-cache-dir -r requirements.txt --timeout 100 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

保存并退出 (`Ctrl+O`, `Enter`, `Ctrl+X`)。

### 3. 构建镜像
```bash
# 构建镜像 (注意最后有个点)
docker build -t bidding-app .
```
*由于利用了本地模型缓存和清华源，构建速度应该很快。*

### 4. 启动容器
```bash
docker run -d \
  --name bidding-app \
  --restart always \
  -p 80:7860 \
  -v $(pwd)/results:/app/results \
  bidding-app
```

---

## 常用维护命令

**查看日志**
```bash
docker logs -f bidding-app
```

**停止服务**
```bash
docker stop bidding-app
```

**更新版本 (方案一)**
```bash
docker pull ghcr.io/abner20953/bidding-data:main
docker stop bidding-app
docker rm bidding-app
# 重新运行启动命令...
```

**更新版本 (方案二)**
1. 上传新代码。
2. 重新执行 `docker build`。
3. 停止并删除旧容器，启动新容器。

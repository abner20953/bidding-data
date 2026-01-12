# Serv00 部署指南

本文档详细说明如何将项目部署到 [Serv00](https://serv00.com/) 免费托管服务。

## 前提条件

1. 注册 Serv00 账号并获取 SSH 登录信息
2. 本地已安装 SSH 客户端（Windows 可用 PuTTY 或 PowerShell）
3. 项目已推送到 GitHub

## 部署步骤

### 1. SSH 连接到 Serv00

使用您收到的 SSH 凭据连接：

```bash
ssh 用户名@s数字.serv00.com
# 例如: ssh user123@s1.serv00.com
```

首次连接时会提示保存指纹，输入 `yes` 确认。

### 2. 克隆项目

```bash
cd ~/domains/您的域名/public_html
# 如果没有域名，可以使用默认目录
cd ~

git clone https://github.com/abner20953/bidding-data.git
cd bidding-data
```

### 3. 创建虚拟环境并安装依赖

Serv00 通常预装了 Python，但需要检查版本：

```bash
python3 --version
# 确保是 Python 3.8+

# 创建虚拟环境
python3 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 4. 配置应用端口

Serv00 不允许使用 1024 以下的端口。修改 `dashboard/app.py` 的最后一行：

```bash
nano dashboard/app.py
```

将 `app.run(debug=True, port=5000)` 修改为：

```python
import os
port = int(os.environ.get('PORT', 8000))
app.run(host='0.0.0.0', port=port, debug=False)
```

保存并退出（Ctrl+X, Y, Enter）。

### 5. 配置 Devil Panel（推荐）

登录 Serv00 的 Devil Panel 控制面板：

1. 访问 `https://panel数字.serv00.com`
2. 使用您的账号登录
3. 进入 **WWW websites** → **Add website**
4. 配置：
   - **Domain**: 选择您的域名或子域名
   - **Type**: Python
   - **Directory**: 指向项目目录

5. 进入 **Port management** 添加端口（例如 8000）

### 6. 使用 Gunicorn 运行（生产环境）

安装 Gunicorn：

```bash
pip install gunicorn
```

创建启动脚本 `start.sh`：

```bash
cat > start.sh << 'EOF'
#!/bin/bash
cd ~/bidding-data
source .venv/bin/activate
gunicorn -w 2 -b 0.0.0.0:8000 dashboard.app:app
EOF

chmod +x start.sh
```

### 7. 设置后台运行

使用 `screen` 或 `tmux` 保持服务运行：

```bash
# 使用 screen
screen -S bidding
./start.sh

# 分离 screen: Ctrl+A, 然后按 D
# 重新连接: screen -r bidding
```

或者创建 cron 任务自动启动：

```bash
crontab -e
```

添加以下行（每次重启后自动运行）：

```
@reboot cd ~/bidding-data && ./start.sh
```

### 8. 测试访问

在浏览器访问：

```
http://您的域名:8000
```

或者使用 Serv00 提供的默认域名。

## 高级配置

### 配置反向代理（可选）

如果想要通过标准 HTTP/HTTPS 端口访问，可以在 Devil Panel 中配置反向代理：

1. 进入 **WWW websites**
2. 编辑您的站点
3. 在 **Proxy** 设置中填入：
   - **Backend**: `http://localhost:8000`

### 日志管理

修改 `start.sh` 添加日志输出：

```bash
#!/bin/bash
cd ~/bidding-data
source .venv/bin/activate
gunicorn -w 2 -b 0.0.0.0:8000 \
  --access-logfile logs/access.log \
  --error-logfile logs/error.log \
  dashboard.app:app
```

创建日志目录：

```bash
mkdir -p logs
```

### 数据持久化

`results` 目录已经在 `.gitignore` 中，生成的 Excel 文件会保存在服务器上。定期备份：

```bash
# 添加到 cron
0 2 * * * tar -czf ~/backups/results-$(date +\%Y\%m\%d).tar.gz ~/bidding-data/results
```

## 常见问题

### Q: 依赖安装失败怎么办？

A: Serv00 限制了某些编译操作。如果 `sentence-transformers` 安装失败，请联系支持或使用预编译包。

### Q: 如何更新代码？

```bash
cd ~/bidding-data
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt  # 如有新依赖
# 重启服务
screen -r bidding  # 进入 screen
Ctrl+C  # 停止服务
./start.sh  # 重启
```

### Q: 服务意外停止怎么办？

检查日志文件并使用 `screen -r` 重新进入会话，或通过 cron 任务自动重启。

## 注意事项

⚠️ **重要**：
- Serv00 免费账户有资源限制，请勿频繁爬取，建议设置更长的延迟
- 定期登录以保持账户活跃
- AI 模型下载可能需要较长时间，首次运行请耐心等待

---

**祝部署顺利！** 如有问题，请参考 [Serv00 官方文档](https://wiki.serv00.com/)。

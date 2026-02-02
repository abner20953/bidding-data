# 数据同步指南 (Data Sync Guide)

本文档说明如何将腾讯云生产环境的数据导出到本地开发环境。

## 核心数据文件

项目中包含用户数据的只有以下两个部分：
1. **`knowledge_base.db`**: 存储所有文章、案例、评论和标签的 SQLite 数据库。
2. **`dashboard/static/uploads/`**: 存储所有上传的图片和附件。

---

## 方法一：使用打包脚本 (推荐)

我们提供了一个脚本 `data_export.sh` 来一次性打包所有数据。

### 1. 在服务器上打包
登录到腾讯云服务器，进入项目目录，运行：

```bash
# 赋予执行权限 (仅第一次需要)
chmod +x data_export.sh

# 运行打包
./data_export.sh
```

运行后，会生成一个类似 `data_backup_20260202.tar.gz` 的文件。

### 2. 下载到本地
在**本地电脑**的终端 (PowerShell 或 CMD) 中运行 `scp` 命令下载文件：

```powershell
# 格式: scp <用户名>@<IP>:<远程路径>/<文件名> <本地路径>
# 示例:
scp ubuntu@1.2.3.4:/home/ubuntu/bidding-data/data_backup_20260202.tar.gz ./
```

### 3. 解压覆盖
在本地项目根目录解压下载的压缩包：

```powershell
# 解压 (需要安装 tar 工具或使用 7-Zip)
tar -xzvf data_backup_20260202.tar.gz
```

> **注意**: 解压操作会覆盖本地现有的 `knowledge_base.db` 和 `uploads` 文件夹，请确保本地没有未备份的重要数据。

---

## 方法二：使用 SFTP 工具 (图形化)

如果您不习惯命令行，可以使用图形化工具：
- **WinSCP** (Windows 推荐)
- **FileZilla**

**步骤**:
1. 使用工具连接到服务器。
2. 导航到项目部署目录。
3. 下载 `knowledge_base.db` 文件。
4. 下载 `dashboard/static/uploads` 整个文件夹。
5. 将它们覆盖到本地项目的对应位置。

---

## 下一步：重启本地服务

数据覆盖后，建议重启本地服务以确保连接重置：

```bash
python run.py
```

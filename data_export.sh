#!/bin/bash

# data_export.sh
# 作用: 将生产环境的数据（数据库 + 上传文件）打包，方便导出到本地

OUTPUT_FILE="data_backup_$(date +%Y%m%d).tar.gz"

echo "📦 正在打包数据..."
echo "Following files will be compressed:"
echo " - knowledge_base.db (知识库数据)"
echo " - dashboard/static/uploads/ (图片附件)"

# 检查文件是否存在
if [ ! -f "knowledge_base.db" ]; then
    echo "⚠️  警告: knowledge_base.db 未找到！"
fi

if [ ! -d "dashboard/static/uploads" ]; then
    echo "⚠️  警告: dashboard/static/uploads 目录未找到！"
fi

# 使用 tar 打包 (排除无关文件)
# h: 追踪符号链接 (如果有)
tar -czvf "$OUTPUT_FILE" knowledge_base.db dashboard/static/uploads 2>/dev/null

if [ $? -eq 0 ]; then
    echo "✅ 打包成功！"
    echo "📄 文件名: $OUTPUT_FILE"
    echo ""
    echo "下载方法 (在本地终端运行):"
    echo "scp <用户名>@<服务器IP>:<项目路径>/$OUTPUT_FILE ./"
else
    echo "❌ 打包失败！"
fi

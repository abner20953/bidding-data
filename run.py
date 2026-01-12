import sys
import os

# 将当前目录和 libs 目录添加到 path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(current_dir)


try:
    from dashboard.app import app
    print("Dashboard app loaded successfully.")
    app.run(debug=True, port=5000)
except ImportError as e:
    print(f"Error importing dashboard: {e}")
    # 尝试直接运行
    os.system("python dashboard/app.py")

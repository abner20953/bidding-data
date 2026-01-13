
import os
from sentence_transformers import SentenceTransformer

def download_and_save_model():
    # 模型名称
    model_name = 'BAAI/bge-small-zh-v1.5'
    # 保存目标路径
    save_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'model_data')
    
    print(f"准备下载模型: {model_name}")
    print(f"目标保存路径: {save_path}")
    
    if os.path.exists(save_path):
        print(f"提示: 目录 {save_path} 已存在。")
        choice = input("是否覆盖重新下载? (y/n): ")
        if choice.lower() != 'y':
            print("已取消。")
            return

    try:
        print("开始下载... (这可能需要几分钟，取决于网速)")
        model = SentenceTransformer(model_name)
        model.save(save_path)
        print("✅ 模型下载并保存成功！")
        print(f"现在您可以离线运行应用程序了。")
    except Exception as e:
        print(f"❌ 下载失败: {e}")

if __name__ == "__main__":
    download_and_save_model()

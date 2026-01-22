import os
import docx
import pdfplumber
import subprocess

def extract_text(filepath):
    """
    Factory function to extract text based on file extension.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.docx':
        return extract_docx(filepath)
    elif ext == '.pdf':
        return extract_pdf(filepath)
    elif ext == '.doc':
        return extract_doc(filepath)
    else:
        raise ValueError(f"不支持的文件格式: {ext} (仅支持 .docx, .doc, .pdf)")

def extract_docx(filepath):
    """
    Extracts text from a .docx file.
    """
    doc = docx.Document(filepath)
    text = []
    for para in doc.paragraphs:
        if para.text.strip():
            text.append(para.text.strip())
    return "\n".join(text)

def extract_doc(filepath):
    """
    Extracts text from a .doc (legacy Word) file using antiword.
    REQUIRES: antiword installed on the system (run `apt-get install antiword`).
    """
    try:
        # Run antiword
        result = subprocess.run(
            ['antiword', filepath], 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True # Return string instead of bytes
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            # If antiword is not found
            if "No such file or directory" in str(result.stderr):
                 raise RuntimeError("服务器未安装 antiword 工具，无法解析 .doc 文件。")
            raise RuntimeError(f"antiword 解析失败: {error_msg}")
            
        return result.stdout.strip()
        
    except FileNotFoundError:
        raise RuntimeError("服务器未安装 antiword 工具，无法解析 .doc 文件。")
    except Exception as e:
        raise RuntimeError(f"解析 .doc 文件出错: {str(e)}")

def extract_pdf(filepath):
    """
    Extracts text from a .pdf file using pdfplumber (stream-friendly).
    """
    text = []
    # 使用 pdfplumber 打开并逐页读取
    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                # 将每一页的文本按行分割，避免大块文本堆积
                lines = page_text.split('\n')
                for line in lines:
                    if line.strip():
                        text.append(line.strip())
    return "\n".join(text)

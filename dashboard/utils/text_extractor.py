import os
import docx
import pdfplumber
import subprocess

def extract_content(filepath):
    """
    Factory function to extract text with page metadata.
    Returns: List[{"text": str, "page": int}]
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.docx':
        return extract_docx(filepath)
    elif ext == '.pdf':
        return extract_pdf(filepath)
    elif ext == '.doc':
        # legacy doc via antiword doesn't give page numbers easily, treat as single page
        return [{"text": extract_doc(filepath), "page": 1}]
    else:
        raise ValueError(f"不支持的文件格式: {ext} (仅支持 .docx, .doc, .pdf)")

def extract_text(filepath):
    """
    Legacy wrapper for string-only return (if used elsewhere).
    """
    content = extract_content(filepath)
    return "\n".join([item['text'] for item in content])

def extract_docx(filepath):
    """
    Extracts text from a .docx file.
    Note: Docx doesn't have fixed pages. We return paragraphs, but page is set to 1.
    Future improvement: Estimate page by char count?
    """
    doc = docx.Document(filepath)
    content = []
    # Just merge all text for now? 
    # Or return chunks? segment_paragraphs expects raw text usually.
    # But if we return list of paragraphs here, segment_paragraphs might be redundant?
    # No, segment_paragraphs merges broken lines. Docx paragraphs are already paragraphs.
    # So we can just return one big block or list of blocks.
    # For compatibility with segment_paragraphs logic which expects "\n" split lines:
    full_text = []
    for para in doc.paragraphs:
        if para.text.strip():
            full_text.append(para.text.strip())
    
    # Return as single 'page' for now, or split if too huge?
    return [{"text": "\n".join(full_text), "page": 1}]

def extract_doc(filepath):
    # antiword returns string
    # See previous implementation
    try:
        result = subprocess.run(
            ['antiword', filepath], 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True
        )
        if result.returncode != 0:
            if "No such file or directory" in str(result.stderr):
                 raise RuntimeError("系统未安装 antiword 工具。")
            raise RuntimeError(f"antiword 解析失败: {result.stderr.strip()}")
        return result.stdout.strip()
    except FileNotFoundError:
        if os.name == 'nt':
             raise RuntimeError("Windows 本地环境需手动安装 Antiword 或将文件转换为 .docx。\n服务器端(Docker)会自动安装支持。")
        raise RuntimeError("服务器未安装 antiword 工具，请运行 apt-get install antiword")
    except Exception as e:
        raise RuntimeError(f"解析 .doc 文件出错: {str(e)}")

def extract_pdf(filepath):
    """
    Extracts text from a .pdf file using pdfplumber.
    Returns: List[{"text": str, "page": int}]
    """
    content = []
    with pdfplumber.open(filepath) as pdf:
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text()
            if page_text:
                content.append({
                    "text": page_text, # Keep raw page text (with \n)
                    "page": i + 1
                })
    return content

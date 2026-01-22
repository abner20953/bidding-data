import os
import docx
import pdfplumber

def extract_text(filepath):
    """
    Factory function to extract text based on file extension.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.docx':
        return extract_docx(filepath)
    elif ext == '.pdf':
        return extract_pdf(filepath)
    else:
        raise ValueError(f"Unsupported file format: {ext}")

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

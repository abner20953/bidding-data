import os
import pdfplumber

def extract_metadata(filepath):
    """
    Extracts metadata from the PDF file (Author, Creator, etc.)
    Returns: Dict {"author": str, "last_modified_by": str, "creator": str, ...}
    """
    ext = os.path.splitext(filepath)[1].lower()
    metadata = {"author": "Unknown", "creator": "Unknown"}
    
    if ext != '.pdf':
        return metadata

    try:
        with pdfplumber.open(filepath) as pdf:
            # pdf.metadata is a dict
            raw_meta = pdf.metadata
            if raw_meta:
                metadata["author"] = raw_meta.get("Author", "Unknown")
                metadata["creator"] = raw_meta.get("Creator", "Unknown")
                metadata["producer"] = raw_meta.get("Producer", "Unknown")
    except Exception as e:
        print(f"Error extracting metadata from {filepath}: {e}")
        
    return metadata

def extract_content(filepath):
    """
    Factory function to extract text with page metadata.
    Returns: List[{"text": str, "page": int}]
    """
    ext = os.path.splitext(filepath)[1].lower()
    
    if ext == '.pdf':
        return extract_pdf(filepath)
    else:
        raise ValueError(f"不支持的文件格式: {ext} (仅支持 .pdf)")

def extract_text(filepath):
    """
    Legacy wrapper for string-only return (if used elsewhere).
    """
    content = extract_content(filepath)
    return "\n".join([item['text'] for item in content])

def extract_pdf(filepath):
    """
    Extracts text from a .pdf file using pdfplumber.
    Returns: List[{"text": str, "page": int}]
    """
    content = []
    try:
        with pdfplumber.open(filepath) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text and text.strip():
                    content.append({
                        "text": text.strip(),
                        "page": i + 1
                    })
    except Exception as e:
        print(f"Error reading PDF {filepath}: {e}")
        return []
        
    return content

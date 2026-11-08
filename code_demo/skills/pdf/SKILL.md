---
name: pdf
description: 处理 PDF 文件——提取文本、创建 PDF、合并文档。当用户要求读取 PDF、创建 PDF 或处理 PDF 文件时使用。
source: manual
locked: false
created_at: 2026-06-20
updated_at: 2026-07-06
usage_count: 2
---

# PDF 处理技能（PDF Processing Skill）

你现在具备 PDF 操作的专业能力。请使用 `bash` 工具执行以下命令，使用 `read_file` 工具读取输出结果。

## 读取 PDF

**选项 1：快速文本提取（推荐）**
```bash
# 使用 pdftotext（来自 poppler-utils）
pdftotext input.pdf -        # 输出到标准输出
pdftotext input.pdf output.txt  # 输出到文件

# 若未安装 pdftotext，可尝试：
python3 -c "
import fitz  # PyMuPDF
doc = fitz.open('input.pdf')
for page in doc:
    print(page.get_text())
"
```

**选项 2：逐页读取并获取元数据**
```python
import fitz  # pip install pymupdf

doc = fitz.open("input.pdf")
print(f"页数: {len(doc)}")
print(f"元数据: {doc.metadata}")

for i, page in enumerate(doc):
    text = page.get_text()
    print(f"--- 第 {i+1} 页 ---")
    print(text)
```

提取文本后，使用 `read_file` 读取输出文件进行后续分析。

## 创建 PDF

**选项 1：从 Markdown 生成（推荐）**
```bash
pandoc input.md -o output.pdf
pandoc input.md -o output.pdf --pdf-engine=xelatex -V geometry:margin=1in
```

先用 `write_file` 创建 Markdown 源文件，再用 `bash` 执行 pandoc 转换。

**选项 2：编程方式创建**
```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("output.pdf", pagesize=letter)
c.drawString(100, 750, "Hello, PDF!")
c.save()
```

**选项 3：从 HTML 生成**
```bash
wkhtmltopdf input.html output.pdf
```

## 合并 PDF

```python
import fitz

result = fitz.open()
for pdf_path in ["file1.pdf", "file2.pdf", "file3.pdf"]:
    doc = fitz.open(pdf_path)
    result.insert_pdf(doc)
result.save("merged.pdf")
```

## 拆分 PDF

```python
import fitz

doc = fitz.open("input.pdf")
for i in range(len(doc)):
    single = fitz.open()
    single.insert_pdf(doc, from_page=i, to_page=i)
    single.save(f"page_{i+1}.pdf")
```

## 核心库对照表

| 任务 | 库 | 安装方式 |
|------|---------|---------|
| 读取/写入/合并 | PyMuPDF | `pip install pymupdf` |
| 从零创建 PDF | ReportLab | `pip install reportlab` |
| HTML 转 PDF | pdfkit | `pip install pdfkit` + 安装 wkhtmltopdf |
| 文本提取 | pdftotext | `brew install poppler` / `apt install poppler-utils` |

## 最佳实践

1. **使用前先检查工具是否已安装**：`bash("which pdftotext")` 或 `bash("python3 -c 'import fitz'")`
2. **处理编码问题**——PDF 可能包含多种字符编码
3. **大文件处理**：逐页处理以避免内存溢出
4. **扫描版 PDF 需 OCR**：若文本提取结果为空，使用 `pytesseract` 进行光学字符识别
5. **输出到文件后用 `read_file` 读取**，比直接解析 stdout 更可靠

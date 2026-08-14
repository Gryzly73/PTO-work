#!/usr/bin/env python3
"""
PDF to Markdown Converter
Извлекает: текст, таблицы в Markdown, и изображения
"""

import sys
import os
from pathlib import Path
import pdfplumber
import fitz  # PyMuPDF для изображений

def clean_text(text):
    """Очищает текст от лишних пробелов"""
    if not text:
        return ""
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        line = ' '.join(line.split())
        if line.strip():
            cleaned.append(line)
    return '\n'.join(lines)

def extract_tables(page):
    """Извлекает таблицы в формате Markdown"""
    tables = []
    try:
        extracted = page.extract_tables()
        for table in extracted:
            if not table or len(table) < 2:
                continue
            
            clean_table = []
            for row in table:
                clean_row = [str(cell).strip() if cell else "" for cell in row]
                if any(clean_row):
                    clean_table.append(clean_row)
            
            if len(clean_table) < 2:
                continue
            
            headers = clean_table[0]
            data = clean_table[1:]
            
            md_table = "| " + " | ".join(headers) + " |\n"
            md_table += "| " + " | ".join(["---"] * len(headers)) + " |\n"
            
            for row in data:
                while len(row) < len(headers):
                    row.append("")
                md_table += "| " + " | ".join(row) + " |\n"
            
            tables.append(md_table)
    except Exception as e:
        print(f"  ⚠️ Ошибка таблицы: {e}")
    
    return tables

def extract_images_from_pdf(pdf_path, output_dir):
    """Извлекает все изображения из PDF и сохраняет в папку"""
    images_info = []
    try:
        doc = fitz.open(pdf_path)
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            image_list = page.get_images(full=True)
            
            for img_index, img in enumerate(image_list, 1):
                xref = img[0]
                try:
                    base_image = doc.extract_image(xref)
                    image_bytes = base_image["image"]
                    image_ext = base_image["ext"]
                    
                    img_filename = f"page_{page_num+1:02d}_img_{img_index:02d}.{image_ext}"
                    img_path = output_dir / img_filename
                    
                    with open(img_path, "wb") as f:
                        f.write(image_bytes)
                    
                    images_info.append({
                        "page": page_num + 1,
                        "filename": img_filename,
                        "index": img_index
                    })
                except Exception as e:
                    continue
        
        doc.close()
    except Exception as e:
        print(f"  ⚠️ Ошибка при извлечении изображений: {e}")
    
    return images_info

def process_pdf(pdf_path):
    """Основная функция"""
    print("=" * 60)
    print("📄 НАЧИНАЕМ ОБРАБОТКУ PDF")
    print("=" * 60)
    
    output_dir = Path("images")
    output_dir.mkdir(exist_ok=True)
    
    print("\n📸 Извлекаем изображения из PDF...")
    all_images = extract_images_from_pdf(pdf_path, output_dir)
    print(f"   Найдено и сохранено {len(all_images)} изображений")
    
    print("\n📝 Извлекаем текст и таблицы...")
    
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        print(f"\n📊 Всего страниц: {total_pages}\n")
        
        with open("drawing.md", "w", encoding="utf-8") as f:
            for page_num, page in enumerate(pdf.pages, start=1):
                print(f"📝 Обработка страницы {page_num}/{total_pages}...")
                
                f.write(f"## Страница {page_num}\n\n")
                
                text = page.extract_text()
                if text:
                    cleaned_text = clean_text(text)
                    f.write("### Текст\n\n")
                    f.write(cleaned_text)
                    f.write("\n\n")
                
                tables = extract_tables(page)
                if tables:
                    f.write("### Таблицы\n\n")
                    for idx, table_md in enumerate(tables, 1):
                        f.write(f"**Таблица {idx}:**\n\n")
                        f.write(table_md)
                        f.write("\n\n")
                
                page_images = [img for img in all_images if img["page"] == page_num]
                if page_images:
                    f.write("### Изображения\n\n")
                    for img in page_images:
                        f.write(f"![Изображение {img['index']} со страницы {img['page']}](images/{img['filename']})\n\n")
                        f.write(f"*Рисунок {img['index']} - страница {img['page']}*\n\n")
                
                f.write("\n---\n\n")
                print(f"  ✅ Страница {page_num} обработана")
    
    print("\n" + "=" * 60)
    print("✅ ГОТОВО!")
    print("=" * 60)
    print(f"\n📁 Результаты сохранены:")
    print(f"   - drawing.md (основной файл)")
    print(f"   - images/ (папка с {len(all_images)} изображениями)")
    print("\n📊 Что извлечено:")
    print("   ✅ Весь текст")
    print("   ✅ Все таблицы (в формате Markdown)")
    print(f"   ✅ {len(all_images)} изображений")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("❌ Укажите PDF файл:")
        print("  python pdf_to_markdown.py drawing.pdf")
        sys.exit(1)
    
    pdf_file = sys.argv[1]
    if not os.path.exists(pdf_file):
        print(f"❌ Файл '{pdf_file}' не найден")
        sys.exit(1)
    
    process_pdf(pdf_file)
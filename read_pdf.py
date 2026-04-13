import pdfplumber, sys

# Force UTF-8 output
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

path = "800 0031.02_BA_EN_Bedienungsanleitung i-class compact.pdf"
with pdfplumber.open(path) as pdf:
    print(f"Total pages: {len(pdf.pages)}\n")
    for i, page in enumerate(pdf.pages):
        text = page.extract_text()
        if text:
            print(f"{'='*60}")
            print(f"PAGE {i+1}")
            print(f"{'='*60}")
            print(text)
            print()

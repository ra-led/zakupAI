import os
from pathlib import Path

from docling.utils import model_downloader
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    AcceleratorOptions,
    PdfPipelineOptions,
    TableFormerMode,
    EasyOcrOptions,
    TesseractOcrOptions,
    OcrMacOptions,
)
from docling_core.types.doc.document import (
    TextItem, PictureItem, TableItem, ListItem)
from docling.document_converter import DocumentConverter, PdfFormatOption

MODELS_DIR = Path(os.getenv("DOCLING_MODELS_DIR", "./models"))
ALLOWED_FORMATS = ["pdf", "docx"]
OPTIONS = {
    "ocr_engine": {
        "label": "OCR engine",
        "type": "select",
        "options": [
            "easyocr",
            "tesseract",
            "mac",
        ],
    }
}

if not MODELS_DIR.exists():
    model_downloader.download_models(output_dir=MODELS_DIR, progress=True, force=False)


class PipelineStep:
    def process(self, data):
        raise NotImplementedError("Subclasses should implement this!")


class LayoutAnalysisPDF(PipelineStep):
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    FORMULA = "formula"
    LIST_ITEM = "list_item"
    PAGE_FOOTER = "page_footer"
    PAGE_HEADER = "page_header"
    PICTURE = "picture"
    SECTION_HEADER = "section_header"
    TABLE = "table"
    TEXT = "text"
    TITLE = "title"
    DOCUMENT_INDEX = "document_index"
    CODE = "code"
    CHECKBOX_SELECTED = "checkbox_selected"
    CHECKBOX_UNSELECTED = "checkbox_unselected"
    FORM = "form"
    KEY_VALUE_REGION = "key_value_region"

    def __init__(self, update_status, ocr_engine, page_range):
        self.update_status = update_status
        self.page_range = page_range
        pipeline_options = PdfPipelineOptions(
            do_table_structure=True,
        )
        pipeline_options.accelerator_options = AcceleratorOptions(
            num_threads=8, device=AcceleratorDevice.CPU,
        )
        pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
        pipeline_options.table_structure_options.do_cell_matching = True
        pipeline_options.generate_page_images = True
        pipeline_options.generate_picture_images = False
        if ocr_engine == "easyocr":
            pipeline_options.ocr_options = EasyOcrOptions(lang=["ru", "en"], download_enabled=True)
        elif ocr_engine == "tesseract":
            pipeline_options.ocr_options = TesseractOcrOptions(lang=["rus", "eng"])
        elif ocr_engine == "mac":
            pipeline_options.ocr_options = OcrMacOptions(lang=["ru-RU", "en-US"])
        self.doc_converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options,
                )
            }
        )

    def process(self, data):
        conv_result = self.doc_converter.convert(data, page_range=self.page_range)

        images_dir = Path("images")
        images_dir.mkdir(parents=True, exist_ok=True)

        picture_counter = 0
        table_counter = 0
        annotations = []
        idx = 1
        for item, level in conv_result.document.iterate_items():
            item_data = {
                "id": idx,
                "page": item.prov[0].page_no,
            }
            item_image = item.get_image(conv_result.document)
            if not all(item_image.size):
                continue
            if item.label in [self.PAGE_FOOTER, self.PAGE_HEADER]:
                continue
            if isinstance(item, TextItem):
                if item.label.lower() in [self.SECTION_HEADER, self.TITLE]:
                    tag = "h"
                    item_data["hash_count"] = item.level
                elif isinstance(item, ListItem):
                    tag = "li"
                else:
                    tag = "p"
                content = {
                    "text": item.text,
                    "type": tag,
                }

            elif isinstance(item, TableItem) and not isinstance(item, ListItem):
                table_counter += 1
                element_image_filename = images_dir / f"table-{table_counter}.png"

                html = item.export_to_html(doc=conv_result.document)
                content = {
                    "html": html,
                    "type": "table",
                    "src": str(element_image_filename),
                }
            else:
                continue
            item_data.update(content)
            annotations.append(item_data)
            idx += 1

        return annotations


class TextToMarkdownStep(PipelineStep):
    def __init__(self, update_status):
        self.update_status = update_status

    def process(self, annotations):
        markdown_lines = []
        for item in annotations:
            item_type = item["type"]
            if item_type == "h":
                header_level = item.get("hash_count", 2)
                if header_level:
                    prefix = "#" * header_level
                    markdown_lines.append(f"{prefix} {item.get('text', '')}")
                else:
                    markdown_lines.append(item.get("text", ""))
            elif item_type == "p":
                markdown_lines.append(item.get("text", ""))
            elif item_type == "li":
                try:
                    if not markdown_lines[-1]:
                        markdown_lines = markdown_lines[:-1]
                except IndexError:
                    pass
                markdown_lines.append(f"- {item.get('text', '')}")
            elif item_type == "img":
                src = item.get("src", "")
                markdown_lines.append(f"![Image]({src})")
            elif item_type == "table":
                html = item.get("html", "")
                src = item.get("src", "")
                markdown_lines.append(f"![Table]({src})")
                markdown_lines.append(html)
            markdown_lines.append("")

        markdown_text = "\n".join(markdown_lines)
        return markdown_text



def run_pipeline(file_path, update_status, options, page_range):
    data = file_path

    ocr_engine = options.get("ocr_engine", "tesseract")

    steps = [
        LayoutAnalysisPDF(update_status, ocr_engine, page_range=page_range),
        TextToMarkdownStep(update_status),
    ]

    for step in steps:
        data = step.process(data)

    return data

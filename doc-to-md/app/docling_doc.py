from pathlib import Path

from docling_core.types.doc.document import (
    TextItem, PictureItem, TableItem, ListItem)
from docling.document_converter import DocumentConverter

IMAGE_RESOLUTION_SCALE = 2.0
ALLOWED_FORMATS = ["docx", "pptx", "xlsx"]


class PipelineStep:
    def process(self, data):
        raise NotImplementedError("Subclasses should implement this!")


class LayoutAnalysisDocx(PipelineStep):
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

    def __init__(self, update_status):
        self.update_status = update_status
        self.doc_converter = DocumentConverter()

    def process(self, data):
        conv_result = self.doc_converter.convert(data)

        images_dir = Path("images")
        images_dir.mkdir(parents=True, exist_ok=True)

        picture_counter = 0
        annotations = []
        idx = 1
        for item, level in conv_result.document.iterate_items():
            item_data = {}
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
                html = item.export_to_html(doc=conv_result.document)
                content = {
                    "html": html,
                    "type": "table",
                }
            if isinstance(item, PictureItem):
                picture_counter += 1
                element_image_filename = images_dir / f"picture-{picture_counter}.png"
                content = {
                    "src": str(element_image_filename),
                    "type": "img",
                }
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
                header_level = item.get("hash_count", 1)
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
                markdown_lines.append(html)
            markdown_lines.append("")

        markdown_text = "\n".join(markdown_lines)
        return markdown_text



def run_pipeline(data, update_status):
    steps = [
        LayoutAnalysisDocx(update_status),
        TextToMarkdownStep(update_status),
    ]

    for step in steps:
        data = step.process(data)

    return data

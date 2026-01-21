import concurrent.futures

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

from .docling_pdf import run_pipeline as original_run_pipeline


def fast_run_pipeline(file_path: str, update_status, options, page_range):
    update_status("Fast conversion pipeline started.")
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.ocr_options = TesseractOcrOptions(lang=["rus", "eng"])
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.do_cell_matching = True

    doc_converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=pipeline_options,
                backend=PyPdfiumDocumentBackend,
            )
        }
    )

    conv_result = doc_converter.convert(file_path)
    return conv_result.document.export_to_markdown()


def run_pipeline(file_path: str, update_status, options, page_range):
    update_status("Starting original PDF conversion pipeline.")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(original_run_pipeline, file_path, update_status, options, page_range)
            return future.result(timeout=180)
    except concurrent.futures.TimeoutError:
        update_status("Original PDF conversion timed out. Falling back to fast conversion pipeline.")
        return fast_run_pipeline(file_path, update_status, options, page_range)

import concurrent.futures

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TesseractOcrOptions, EasyOcrOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

from .docling_pdf import run_pipeline as original_run_pipeline


def _run_fast_with_ocr(file_path: str, ocr_engine: str, update_status):
    update_status("Fast conversion pipeline started.")
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = ocr_engine != "none"
    if ocr_engine == "tesseract":
        pipeline_options.ocr_options = TesseractOcrOptions(lang=["rus", "eng"])
    elif ocr_engine == "easyocr":
        pipeline_options.ocr_options = EasyOcrOptions(lang=["ru", "en"], download_enabled=True)
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


def fast_run_pipeline(file_path: str, update_status, options, page_range):
    preferred_ocr = (options or {}).get("ocr_engine", "easyocr")
    # fallback chain for environments where tesserocr is missing
    engines = [preferred_ocr, "easyocr", "none"]
    tried = []
    for engine in engines:
        if engine in tried:
            continue
        tried.append(engine)
        try:
            update_status(f"Fast conversion: trying OCR engine '{engine}'.")
            return _run_fast_with_ocr(file_path, engine, update_status)
        except Exception as exc:  # noqa: BLE001
            update_status(f"Fast conversion failed with '{engine}': {exc}")
            continue
    raise RuntimeError("Fast PDF conversion failed for all OCR fallbacks (tesseract, easyocr, no-ocr).")


def run_pipeline(file_path: str, update_status, options, page_range):
    update_status("Starting original PDF conversion pipeline.")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(original_run_pipeline, file_path, update_status, options, page_range)
            return future.result(timeout=180)
    except concurrent.futures.TimeoutError:
        update_status("Original PDF conversion timed out. Falling back to fast conversion pipeline.")
        return fast_run_pipeline(file_path, update_status, options, page_range)
    except Exception as exc:  # noqa: BLE001
        update_status(f"Original PDF conversion failed ({exc}). Falling back to fast conversion pipeline.")
        return fast_run_pipeline(file_path, update_status, options, page_range)

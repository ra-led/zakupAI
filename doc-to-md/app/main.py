from pathlib import Path
import tempfile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .converter import ALLOWED_EXTENSIONS, convert_to_markdown

app = FastAPI(title="doc-to-md", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def healthcheck() -> dict:
    return {"status": "ok"}


@app.post("/convert")
async def convert_document(file: UploadFile = File(...)) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="File is required")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir) / file.filename
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        temp_path.write_bytes(content)

        try:
            markdown = convert_to_markdown(temp_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Conversion failed: {exc}") from exc

    markdown = markdown.strip()
    if not markdown:
        raise HTTPException(status_code=400, detail="Conversion produced empty markdown")

    return {
        "filename": file.filename,
        "markdown": markdown,
    }

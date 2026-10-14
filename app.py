import os
import shutil
import time
import uuid
import json
import asyncio
import traceback
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse

import step_to_glb
import combine_glb_meshes

app = FastAPI(title="STEP to GLB Converter")

# Enable CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).parent
# Store temporary conversion sessions in the OS temp directory so the project workspace stays clean
JOBS_DIR = Path(tempfile.gettempdir()) / "step_to_glb_converter_temp"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Legacy staging paths for backward compatibility with /convert
LEGACY_STEP_DIR = BASE_DIR / "STEP" / "PL" / "Female Wiggins"
LEGACY_GLB_DIR = BASE_DIR / "GLB" / "PL" / "Female Wiggins"


@dataclass
class JobFile:
    id: int
    filename: str
    relative_path: str
    original_size: int
    status: str = "queued"  # queued, tessellating, optimizing, ready, failed
    stage_text: str = "Queued"
    glb_name: Optional[str] = None
    glb_size: Optional[int] = None
    error: Optional[str] = None
    download_url: Optional[str] = None


@dataclass
class JobState:
    job_id: str
    folder_name: str
    files: List[JobFile]
    step_dir: Path
    glb_dir: Path
    zip_path: Optional[Path] = None
    subscribers: List[asyncio.Queue] = field(default_factory=list)
    events_history: List[dict] = field(default_factory=list)
    completed: bool = False
    created_at: float = field(default_factory=time.time)

    def emit_event(self, event_type: str, data: dict):
        event_obj = {"event": event_type, "data": data}
        self.events_history.append(event_obj)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event_obj)
            except Exception:
                pass


jobs: Dict[str, JobState] = {}


def cleanup_old_jobs(max_age_seconds: int = 7200):
    """Clean up job directories and in-memory states older than 2 hours."""
    now = time.time()
    expired = [jid for jid, j in jobs.items() if now - j.created_at > max_age_seconds]
    for jid in expired:
        job = jobs.pop(jid, None)
        if job:
            job_dir = JOBS_DIR / jid
            shutil.rmtree(job_dir, ignore_errors=True)


async def process_job_async(job_id: str):
    """Background task converting each STEP file one-by-one while streaming live progress."""
    job = jobs.get(job_id)
    if not job:
        return

    # Broadcast init event
    job.emit_event(
        "init",
        {
            "job_id": job.job_id,
            "folder_name": job.folder_name,
            "total": len(job.files),
            "files": [
                {
                    "id": f.id,
                    "filename": f.filename,
                    "relative_path": f.relative_path,
                    "original_size": f.original_size,
                    "status": f.status,
                    "stage_text": f.stage_text,
                }
                for f in job.files
            ],
        },
    )

    successful_count = 0
    failed_count = 0

    for i, file_item in enumerate(job.files):
        step_file_path = job.step_dir / file_item.relative_path
        glb_rel_path = Path(file_item.relative_path).with_suffix(".glb")
        glb_file_path = job.glb_dir / glb_rel_path
        glb_file_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. State: Tessellating (STEP -> GLB)
        file_item.status = "tessellating"
        file_item.stage_text = "Tessellating (STEP → GLB)"
        job.emit_event(
            "model_start",
            {
                "file_id": file_item.id,
                "filename": file_item.filename,
                "index": i + 1,
                "total": len(job.files),
                "stage": "tessellating",
                "stage_text": "Tessellating CAD geometry...",
            },
        )

        try:
            # Run OpenCASCADE tessellation in a separate thread so event loop stays responsive
            conversion_ok = await asyncio.to_thread(
                step_to_glb.step_to_glb, str(step_file_path), str(glb_file_path)
            )
        except Exception as e:
            conversion_ok = False
            file_item.error = str(e)

        if not conversion_ok or not glb_file_path.exists():
            file_item.status = "failed"
            file_item.stage_text = "Tessellation Failed"
            if not file_item.error:
                file_item.error = "OpenCASCADE could not read or tessellate STEP file."
            failed_count += 1
            job.emit_event(
                "model_error",
                {
                    "file_id": file_item.id,
                    "filename": file_item.filename,
                    "index": i + 1,
                    "total": len(job.files),
                    "error": file_item.error,
                },
            )
            continue

        # 2. State: Optimizing (Combining sub-meshes and cleaning names)
        file_item.status = "optimizing"
        file_item.stage_text = "Optimizing Meshes"
        job.emit_event(
            "model_optimizing",
            {
                "file_id": file_item.id,
                "filename": file_item.filename,
                "index": i + 1,
                "total": len(job.files),
                "stage": "optimizing",
                "stage_text": "Cleaning node names & combining component meshes...",
            },
        )

        try:
            # Run pygltflib mesh welding/combining in a worker thread
            await asyncio.to_thread(
                combine_glb_meshes.combine_glb_file, glb_file_path, False
            )
        except Exception as e:
            # If combining mesh fails, the raw GLB is still present, but we record warning
            print(f"Warning combining meshes for {glb_file_path.name}: {e}")

        # 3. State: Ready
        glb_size = glb_file_path.stat().st_size
        file_item.status = "ready"
        file_item.stage_text = "Ready"
        file_item.glb_name = glb_file_path.name
        file_item.glb_size = glb_size
        file_item.download_url = f"/api/jobs/{job.job_id}/download-model?file_id={file_item.id}"
        successful_count += 1

        job.emit_event(
            "model_complete",
            {
                "file_id": file_item.id,
                "filename": file_item.filename,
                "glb_name": file_item.glb_name,
                "index": i + 1,
                "total": len(job.files),
                "status": "ready",
                "glb_size": glb_size,
                "download_url": file_item.download_url,
            },
        )

    # 4. Final step: Package ZIP archive if any GLBs were converted
    converted_glbs = list(job.glb_dir.rglob("*.glb"))
    if converted_glbs:
        zip_archive_base = JOBS_DIR / job_id / job.folder_name
        zip_final = await asyncio.to_thread(
            shutil.make_archive,
            str(zip_archive_base),
            "zip",
            str(job.glb_dir)
        )
        job.zip_path = Path(zip_final)
        zip_download_url = f"/api/jobs/{job.job_id}/download-zip"
    else:
        zip_download_url = None

    job.completed = True
    job.emit_event(
        "job_complete",
        {
            "job_id": job.job_id,
            "folder_name": job.folder_name,
            "total": len(job.files),
            "successful": successful_count,
            "failed": failed_count,
            "zip_url": zip_download_url,
        },
    )


@app.get("/", response_class=FileResponse)
async def serve_ui():
    return FileResponse(BASE_DIR / "index.html")


@app.post("/api/jobs")
async def create_conversion_job(
    files: list[UploadFile] = File(...),
    folder_name: str = Form("converted_glbs"),
):
    cleanup_old_jobs()

    # Filter valid STEP files
    valid_uploads = []
    for f in files:
        fname = f.filename.lower()
        if fname.endswith(".step") or fname.endswith(".stp"):
            valid_uploads.append(f)

    if not valid_uploads:
        raise HTTPException(
            status_code=400, detail="No valid .step or .stp files uploaded."
        )

    job_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    step_dir = job_dir / "step"
    glb_dir = job_dir / "glb"
    step_dir.mkdir(parents=True, exist_ok=True)
    glb_dir.mkdir(parents=True, exist_ok=True)

    job_files: List[JobFile] = []

    for idx, upload in enumerate(valid_uploads):
        clean_rel_path = upload.filename.replace("\\", "/").lstrip("/")
        # Prevent directory traversal
        target_path = (step_dir / clean_rel_path).resolve()
        if not str(target_path).startswith(str(step_dir.resolve())):
            target_path = step_dir / Path(clean_rel_path).name

        target_path.parent.mkdir(parents=True, exist_ok=True)
        content = await upload.read()
        with open(target_path, "wb") as out_f:
            out_f.write(content)

        job_files.append(
            JobFile(
                id=idx,
                filename=Path(clean_rel_path).name,
                relative_path=str(target_path.relative_to(step_dir)),
                original_size=len(content),
            )
        )

    job_state = JobState(
        job_id=job_id,
        folder_name=folder_name or "converted_glbs",
        files=job_files,
        step_dir=step_dir,
        glb_dir=glb_dir,
    )
    jobs[job_id] = job_state

    # Start conversion in the background
    asyncio.create_task(process_job_async(job_id))

    return {
        "job_id": job_id,
        "folder_name": job_state.folder_name,
        "total_files": len(job_files),
        "files": [
            {
                "id": f.id,
                "filename": f.filename,
                "relative_path": f.relative_path,
                "size": f.original_size,
            }
            for f in job_files
        ],
    }


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(job_id: str, request: Request):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    async def event_generator():
        # 1. Replay historical events to catch up late joiners
        for ev in list(job.events_history):
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"

        if job.completed:
            return

        # 2. Subscribe to new live events
        sub_queue = asyncio.Queue()
        job.subscribers.append(sub_queue)

        try:
            while True:
                # Disconnect check
                if await request.is_disconnected():
                    break

                try:
                    ev = await asyncio.wait_for(sub_queue.get(), timeout=15.0)
                    yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"
                    if ev["event"] == "job_complete":
                        break
                except asyncio.TimeoutError:
                    # Keep-alive heartbeat comment
                    yield ": ping\n\n"
        finally:
            if sub_queue in job.subscribers:
                job.subscribers.remove(sub_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/jobs/{job_id}/download-model")
async def download_single_model(job_id: str, file_id: int):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    matching_file = next((f for f in job.files if f.id == file_id), None)
    if not matching_file or matching_file.status != "ready":
        raise HTTPException(status_code=404, detail="Model file not ready or not found.")

    glb_rel = Path(matching_file.relative_path).with_suffix(".glb")
    glb_path = job.glb_dir / glb_rel

    if not glb_path.exists():
        raise HTTPException(status_code=404, detail="GLB file does not exist.")

    return FileResponse(
        path=glb_path,
        media_type="model/gltf-binary",
        filename=glb_path.name,
    )


@app.get("/api/jobs/{job_id}/download-zip")
async def download_job_zip(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    if not job.zip_path or not job.zip_path.exists():
        raise HTTPException(status_code=404, detail="ZIP archive not generated yet.")

    return FileResponse(
        path=job.zip_path,
        media_type="application/zip",
        filename=f"{job.folder_name}.zip",
    )


# -------------------------------------------------------------
# Legacy Endpoint (Backward Compatibility)
# -------------------------------------------------------------
@app.post("/convert")
def convert_files_legacy(files: list[UploadFile] = File(...)):
    try:
        shutil.rmtree(LEGACY_STEP_DIR, ignore_errors=True)
        shutil.rmtree(LEGACY_GLB_DIR, ignore_errors=True)
        LEGACY_STEP_DIR.mkdir(parents=True, exist_ok=True)
        LEGACY_GLB_DIR.mkdir(parents=True, exist_ok=True)

        for file in files:
            file_name_lower = file.filename.lower()
            if file_name_lower.endswith(".step") or file_name_lower.endswith(".stp"):
                file_path = LEGACY_STEP_DIR / file.filename
                if not str(file_path.resolve()).startswith(str(LEGACY_STEP_DIR.resolve())):
                    continue
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, "wb") as f:
                    f.write(file.file.read())

        step_to_glb.batch_convert()
        combine_glb_meshes.run_combine()

        converted_files = list(LEGACY_GLB_DIR.rglob("*.glb"))
        if not converted_files:
            return JSONResponse(
                status_code=400,
                content={"error": "No files were successfully converted."},
            )

        zip_path = BASE_DIR / "converted_glbs.zip"
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", str(LEGACY_GLB_DIR))

        return FileResponse(
            path=zip_path,
            media_type="application/zip",
            filename="converted_glbs.zip",
        )
    except Exception as e:
        error_details = traceback.format_exc()
        return JSONResponse(
            status_code=500,
            content={"error": f"Conversion failed:\n{error_details}"},
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
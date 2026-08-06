from fastapi import APIRouter, HTTPException

import backend.db.db_client as db

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/")
async def list_jobs(limit: int = 20):
    return {"jobs": db.list_jobs(limit=limit)}


@router.get("/{job_id}")
async def get_job(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job

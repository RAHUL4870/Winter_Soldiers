from fastapi import APIRouter

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


@router.post("/ask")
async def ask():
    return {"status": "ok", "answer": "Copilot not yet implemented", "model_used": None}

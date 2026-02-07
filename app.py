from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from typing import Optional
import os
import logging
import time

from src.tasks import task_queue
from src.db import list_images, get_prompt, delete_image, get_image_by_id, reset_running_tasks_to_queued, cleanup_orphaned_records
from src.config import list_config_summaries, select_config, get_max_concurrent

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('app.log')
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Image Generation")


@app.middleware("http")
async def task_timing_middleware(request, call_next):
    if request.url.path != "/api/tasks":
        return await call_next(request)

    start_time = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start_time) * 1000
    response.headers["Server-Timing"] = f"task;dur={elapsed_ms:.2f}"
    logger.info(
        "Request completed: %s %s in %.2fms",
        request.method,
        request.url.path,
        elapsed_ms
    )
    return response


# Pydantic models
class TaskCreate(BaseModel):
    prompt: str = Field(..., min_length=1)
    n: int = Field(..., ge=1, le=10)
    config_name: Optional[str] = None
    video_config: Optional[dict] = None


class ImageToVideoRequest(BaseModel):
    prompt: str = ""
    video_config: Optional[dict] = None
    config_name: Optional[str] = None


class TaskResponse(BaseModel):
    task_id: str


class ImageItem(BaseModel):
    id: int
    filename: str
    url: str
    created_at: str


class ImagesResponse(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[ImageItem]


class PromptResponse(BaseModel):
    prompt: str


class ConfigSummary(BaseModel):
    name: str
    base_url: str = ""
    model: str = "grok-imagine-1.0"
    max_concurrent: Optional[int] = 2


class ConfigListResponse(BaseModel):
    configs: list[ConfigSummary]
    default: Optional[str] = None


# Startup event
@app.on_event("startup")
async def startup_event():
    """Start the task queue workers on startup"""
    logger.info("Starting AI Image Generation application")
    
    # Clean up orphaned database records
    cleanup_count = cleanup_orphaned_records()
    if cleanup_count:
        logger.info(f"Cleaned up {cleanup_count} orphaned image records from database")
    
    reset_count = reset_running_tasks_to_queued()
    if reset_count:
        logger.info(f"Reset {reset_count} running tasks to queued after restart")
    requeued = await task_queue.requeue_pending_tasks()
    if requeued:
        logger.info(f"Requeued {requeued} pending tasks")
    task_queue.set_max_concurrent(get_max_concurrent())
    task_queue.start_workers()
    logger.info(f"Task queue workers started (max_concurrent={task_queue.max_concurrent})")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the task queue workers on shutdown"""
    logger.info("Shutting down application")
    await task_queue.stop_workers()
    logger.info("Task queue workers stopped")


# Mount static directories
app.mount("/output", StaticFiles(directory="output"), name="output")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    """Serve the main HTML page"""
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/api/configs", response_model=ConfigListResponse)
async def get_configs():
    """List available backend API configs"""
    try:
        return list_config_summaries()
    except FileNotFoundError as e:
        logger.error(str(e))
        raise HTTPException(status_code=500, detail="Config file not found")
    except Exception as e:
        logger.error(f"Failed to load configs: {e}")
        raise HTTPException(status_code=500, detail="Failed to load configs")


# Task endpoints
@app.post("/api/tasks", response_model=TaskResponse)
async def create_task(task_data: TaskCreate):
    """Create a new image generation task"""
    request_start = time.perf_counter()
    try:
        config_start = time.perf_counter()
        config = select_config(task_data.config_name)
        config_elapsed = time.perf_counter() - config_start
    except FileNotFoundError as e:
        logger.error(str(e))
        raise HTTPException(status_code=500, detail="Config file not found")
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        raise HTTPException(status_code=500, detail="Failed to load config")

    if not config:
        raise HTTPException(status_code=400, detail="Config not found")

    settings = {
        "base_url": config.get("base_url", "").rstrip("/"),
        "api_key": config.get("api_key", ""),
        "model": config.get("model", "grok-imagine-1.0"),
        "proxy": config.get("proxy"),
        "max_concurrent": get_max_concurrent()
    }

    if not settings["base_url"] or not settings["api_key"]:
        raise HTTPException(status_code=500, detail="Config missing base_url or api_key")

    logger.info(
        "Creating task: prompt='%s...', n=%s, config='%s', config_lookup_ms=%.2f",
        task_data.prompt[:50],
        task_data.n,
        config.get("name", "unknown"),
        config_elapsed * 1000
    )
    try:
        queue_start = time.perf_counter()
        task_id = await task_queue.create_task(
            prompt=task_data.prompt,
            n=task_data.n,
            settings=settings,
            config_name=task_data.config_name,
            video_config=task_data.video_config
        )
        queue_elapsed = time.perf_counter() - queue_start
        total_elapsed = time.perf_counter() - request_start
        logger.info(
            "Task created successfully: %s (queue_ms=%.2f total_ms=%.2f)",
            task_id,
            queue_elapsed * 1000,
            total_elapsed * 1000
        )
        return TaskResponse(task_id=task_id)
    except Exception as e:
        logger.error(f"Failed to create task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tasks")
async def get_tasks():
    """Get all tasks"""
    return task_queue.list_tasks()


@app.websocket("/ws/tasks")
async def websocket_tasks(websocket: WebSocket):
    """WebSocket endpoint for real-time task updates"""
    await websocket.accept()
    task_queue.add_websocket_client(websocket)
    logger.info("WebSocket client connected")

    try:
        # Send initial task list
        tasks = task_queue.list_tasks()
        await websocket.send_json({
            "type": "initial_tasks",
            "tasks": tasks
        })

        # Keep connection alive and handle incoming messages
        while True:
            data = await websocket.receive_text()
            # Echo back or handle commands if needed
            logger.debug(f"Received WebSocket message: {data}")

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        task_queue.remove_websocket_client(websocket)


@app.post("/api/config/concurrent")
async def set_concurrent(max_concurrent: int = Body(..., ge=1, le=10)):
    """Set max concurrent tasks"""
    task_queue.set_max_concurrent(max_concurrent)
    return {"max_concurrent": task_queue.max_concurrent}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """Get task by ID"""
    task = task_queue.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


# Image endpoints
@app.get("/api/images", response_model=ImagesResponse)
async def get_images(page: int = 1, page_size: int = 16):
    """Get paginated list of images"""
    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(status_code=400, detail="Page size must be between 1 and 100")

    try:
        images, total = list_images(page, page_size)

        items = [
            ImageItem(
                id=img["id"],
                filename=img["filename"],
                url=f"/output/{img['filename']}",
                created_at=img["created_at"]
            )
            for img in images
        ]

        return ImagesResponse(
            page=page,
            page_size=page_size,
            total=total,
            items=items
        )
    except Exception as e:
        logger.error(f"Failed to list images: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve images")


@app.get("/api/images/{image_id}/prompt", response_model=PromptResponse)
async def get_image_prompt(image_id: int):
    """Get prompt for an image"""
    prompt = get_prompt(image_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Image not found")
    return PromptResponse(prompt=prompt)


@app.delete("/api/images/{image_id}")
async def delete_image_endpoint(image_id: int):
    """Delete an image and its database record"""
    logger.info(f"Deleting image: {image_id}")
    filename = delete_image(image_id)

    if not filename:
        logger.warning(f"Image not found: {image_id}")
        raise HTTPException(status_code=404, detail="Image not found")

    # Delete file if exists
    filepath = os.path.join("output", filename)

    # Security: Ensure the path is within output directory
    if not os.path.abspath(filepath).startswith(os.path.abspath("output")):
        logger.error(f"Invalid file path detected: {filepath}")
        raise HTTPException(status_code=400, detail="Invalid file path")

    if os.path.exists(filepath):
        try:
            os.remove(filepath)
            logger.info(f"File deleted successfully: {filename}")
        except Exception as e:
            # Log error but don't fail the request
            logger.error(f"Failed to delete file {filepath}: {e}")
    else:
        logger.warning(f"File not found on disk: {filename}")

    return {"message": "Image deleted successfully"}


@app.post("/api/images/cleanup")
async def cleanup_orphaned_images():
    """Manually clean up orphaned image records from database"""
    try:
        cleanup_count = cleanup_orphaned_records()
        logger.info(f"Manual cleanup: removed {cleanup_count} orphaned records")
        return {
            "message": f"Successfully cleaned up {cleanup_count} orphaned record(s)",
            "count": cleanup_count
        }
    except Exception as e:
        logger.error(f"Failed to cleanup orphaned records: {e}")
        raise HTTPException(status_code=500, detail="Failed to cleanup orphaned records")


@app.post("/api/images/{image_id}/to-video", response_model=TaskResponse)
async def image_to_video(image_id: int, request: ImageToVideoRequest):
    """Convert an image to video"""
    # 1. 获取图片信息
    image = get_image_by_id(image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    filename = image.get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail="Image filename not found")

    # 检查文件是否存在
    filepath = os.path.join("output", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Image file not found on disk")

    # 2. 读取配置 - 默认使用 grok-video 配置
    config_name = request.config_name or "grok-video"
    try:
        config = select_config(config_name)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        raise HTTPException(status_code=500, detail="Failed to load config")

    if not config:
        raise HTTPException(status_code=400, detail=f"Config '{config_name}' not found")

    settings = {
        "base_url": config.get("base_url", "").rstrip("/"),
        "api_key": config.get("api_key", ""),
        "model": config.get("model", "grok-imagine-1.0-video"),
        "proxy": config.get("proxy"),
        "max_concurrent": get_max_concurrent()
    }

    if not settings["base_url"] or not settings["api_key"]:
        raise HTTPException(status_code=500, detail="Config missing base_url or api_key")

    # 3. 设置视频配置
    video_config = request.video_config or {"duration": 5}
    prompt = request.prompt or "让图片动起来"

    logger.info(f"Creating image-to-video task: image_id={image_id}, filename={filename}")

    # 4. 创建视频生成任务
    try:
        task_id = await task_queue.create_task(
            prompt=prompt,
            n=1,
            settings=settings,
            config_name=config_name,
            video_config=video_config,
            source_image=filename
        )
        logger.info(f"Image-to-video task created: {task_id}")
        return TaskResponse(task_id=task_id)
    except Exception as e:
        logger.error(f"Failed to create image-to-video task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8989)

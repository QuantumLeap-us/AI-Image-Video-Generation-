import asyncio
import uuid
import json
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Set
from enum import Enum

from src.client import generate_images
from src.db import insert_image, insert_task, update_task_status, get_task_by_id, list_tasks as db_list_tasks, list_tasks_by_status

logger = logging.getLogger(__name__)


def _run_generate_images(settings: Dict, prompt: str, n: int, video_config: Optional[Dict] = None, source_image: str = None) -> List[str]:
    return asyncio.run(generate_images(settings, prompt, n, video_config, source_image))


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Task:
    def __init__(self, prompt: str, n: int, settings: Dict, task_id: Optional[str] = None,
                 created_at: Optional[str] = None, config_name: Optional[str] = None,
                 video_config: Optional[Dict] = None, source_image: Optional[str] = None):
        self.task_id = task_id or str(uuid.uuid4())
        self.status = TaskStatus.QUEUED
        self.prompt = prompt
        self.n = n
        self.settings = settings
        self.created_at = created_at or datetime.now().isoformat()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.results: List[str] = []
        self.error: Optional[str] = None
        self.config_name = config_name
        self.video_config = video_config
        self.source_image = source_image

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "prompt": self.prompt,
            "n": self.n,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "results": self.results,
            "error": self.error
        }


class TaskQueue:
    def __init__(self, max_concurrent: int = 2):
        self.tasks: Dict[str, Task] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self.max_concurrent = max_concurrent
        self.running_tasks: Set[str] = set()
        self.worker_tasks: List[asyncio.Task] = []
        self._broadcast_tasks: Set[asyncio.Task] = set()
        self._broadcast_lock = asyncio.Lock()
        self.is_running = False
        self.websocket_clients: Set = set()

    def set_max_concurrent(self, max_concurrent: int):
        """Update max concurrent tasks"""
        self.max_concurrent = max(1, max_concurrent)
        logger.info(f"Max concurrent tasks set to {self.max_concurrent}")

    def start_workers(self):
        """Start background workers"""
        if not self.is_running:
            self.is_running = True
            # Start multiple workers based on max_concurrent
            for i in range(self.max_concurrent):
                worker = asyncio.create_task(self._worker(i))
                self.worker_tasks.append(worker)
            logger.info(f"Started {self.max_concurrent} workers")

    async def _worker(self, worker_id: int):
        """Background worker that processes tasks"""
        logger.info(f"Worker {worker_id} started")
        while self.is_running:
            try:
                task_id = await self.queue.get()
                task = self.tasks.get(task_id)

                if not task:
                    self.queue.task_done()
                    continue

                # Add to running tasks
                self.running_tasks.add(task_id)

                # Update status to running
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now().isoformat()

                # Update database
                update_task_status(task.task_id, TaskStatus.RUNNING, started_at=task.started_at)

                # Broadcast status update
                self._schedule_broadcast(task)

                logger.info(f"Worker {worker_id} processing task {task_id}")

                try:
                    # Generate images or video
                    filenames = await asyncio.to_thread(
                        _run_generate_images,
                        task.settings,
                        task.prompt,
                        task.n,
                        task.video_config,
                        task.source_image
                    )

                    # Save to database
                    for filename in filenames:
                        insert_image(filename, task.prompt)

                    # Update task
                    task.results = filenames
                    task.status = TaskStatus.SUCCEEDED

                    logger.info(f"Worker {worker_id} completed task {task_id}")

                except Exception as e:
                    task.status = TaskStatus.FAILED
                    task.error = str(e)
                    logger.error(f"Worker {worker_id} failed task {task_id}: {e}")

                finally:
                    task.finished_at = datetime.now().isoformat()

                    # Update database
                    update_task_status(
                        task.task_id,
                        task.status,
                        finished_at=task.finished_at,
                        results=json.dumps(task.results) if task.results else None,
                        error=task.error
                    )

                    # Broadcast status update
                    self._schedule_broadcast(task)

                    # Remove from running tasks
                    self.running_tasks.discard(task_id)
                    self.queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")

        logger.info(f"Worker {worker_id} stopped")

    async def create_task(self, prompt: str, n: int, settings: Dict, config_name: Optional[str] = None, video_config: Optional[Dict] = None, source_image: Optional[str] = None) -> str:
        """Create a new task and add to queue"""
        start_time = time.perf_counter()
        task = Task(prompt, n, settings, config_name=config_name, video_config=video_config, source_image=source_image)
        self.tasks[task.task_id] = task

        # Save to database
        db_start = time.perf_counter()
        insert_task(task.task_id, TaskStatus.QUEUED, prompt, n, config_name=config_name)
        db_elapsed = time.perf_counter() - db_start

        # Add to queue
        queue_start = time.perf_counter()
        await self.queue.put(task.task_id)
        queue_elapsed = time.perf_counter() - queue_start

        # Broadcast new task
        broadcast_start = time.perf_counter()
        self._schedule_broadcast(task)
        broadcast_elapsed = time.perf_counter() - broadcast_start

        total_elapsed = time.perf_counter() - start_time
        logger.info(
            "Created task %s (db_ms=%.2f queue_ms=%.2f broadcast_ms=%.2f total_ms=%.2f)",
            task.task_id,
            db_elapsed * 1000,
            queue_elapsed * 1000,
            broadcast_elapsed * 1000,
            total_elapsed * 1000
        )

        return task.task_id

    async def requeue_pending_tasks(self) -> int:
        """Requeue tasks that were pending before restart"""
        queued_tasks = list_tasks_by_status(TaskStatus.QUEUED)
        if not queued_tasks:
            return 0

        from src.config import select_config, get_max_concurrent

        requeued = 0
        for task_data in queued_tasks:
            task_id = task_data.get("task_id")
            if not task_id or task_id in self.tasks:
                continue

            config_name = task_data.get("config_name")
            config = select_config(config_name)
            if not config:
                logger.error("Config not found for task %s, skipping requeue", task_id)
                continue

            settings = {
                "base_url": config.get("base_url", "").rstrip("/"),
                "api_key": config.get("api_key", ""),
                "model": config.get("model", "grok-imagine-1.0"),
                "proxy": config.get("proxy"),
                "max_concurrent": get_max_concurrent()
            }

            if not settings["base_url"] or not settings["api_key"]:
                logger.error("Config missing base_url or api_key for task %s", task_id)
                continue

            task = Task(
                prompt=task_data.get("prompt", ""),
                n=task_data.get("n", 1),
                settings=settings,
                task_id=task_id,
                created_at=task_data.get("created_at"),
                config_name=config_name
            )
            self.tasks[task.task_id] = task
            await self.queue.put(task.task_id)
            requeued += 1
            self._schedule_broadcast(task)

        if requeued:
            logger.info("Requeued %s pending tasks after restart", requeued)
        return requeued

    def get_task(self, task_id: str) -> Optional[Dict]:
        """Get task by ID (from memory or database)"""
        # Try memory first
        task = self.tasks.get(task_id)
        if task:
            return task.to_dict()

        # Fallback to database
        return get_task_by_id(task_id)

    def list_tasks(self, limit: int = 10) -> List[Dict]:
        """List recent tasks from database"""
        return db_list_tasks(limit)

    async def stop_workers(self):
        """Stop all background workers"""
        self.is_running = False
        for worker in self.worker_tasks:
            worker.cancel()

        # Wait for all workers to stop
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        self.worker_tasks.clear()
        for task in list(self._broadcast_tasks):
            task.cancel()
        self._broadcast_tasks.clear()
        logger.info("All workers stopped")

    def add_websocket_client(self, websocket):
        """Add a WebSocket client"""
        self.websocket_clients.add(websocket)
        logger.info(f"WebSocket client added, total: {len(self.websocket_clients)}")

    def remove_websocket_client(self, websocket):
        """Remove a WebSocket client"""
        self.websocket_clients.discard(websocket)
        logger.info(f"WebSocket client removed, total: {len(self.websocket_clients)}")

    def _schedule_broadcast(self, task: Task):
        if not self.websocket_clients:
            return
        broadcast_task = asyncio.create_task(self._broadcast_task_update(task))
        self._broadcast_tasks.add(broadcast_task)
        broadcast_task.add_done_callback(self._handle_broadcast_done)

    def _handle_broadcast_done(self, task: asyncio.Task):
        self._broadcast_tasks.discard(task)
        try:
            task.result()
        except Exception as e:
            logger.error(f"Broadcast task failed: {e}")

    async def _broadcast_task_update(self, task: Task):
        """Broadcast task update to all connected WebSocket clients"""
        if not self.websocket_clients:
            return

        message = json.dumps({
            "type": "task_update",
            "task": task.to_dict()
        })

        async with self._broadcast_lock:
            # Send to all clients
            disconnected = set()
            for client in self.websocket_clients:
                try:
                    await client.send_text(message)
                except Exception as e:
                    logger.error(f"Failed to send to WebSocket client: {e}")
                    disconnected.add(client)

            # Remove disconnected clients
            for client in disconnected:
                self.websocket_clients.discard(client)


# Global task queue instance
task_queue = TaskQueue(max_concurrent=2)

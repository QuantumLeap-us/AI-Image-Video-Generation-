import httpx
import base64
import os
import logging
from datetime import datetime
from typing import List, Dict, Optional
import uuid

# Setup logging
logger = logging.getLogger(__name__)


def get_image_as_data_url(filename: str) -> str:
    """将本地图片转为 Base64 Data URL"""
    filepath = os.path.join("output", filename)
    with open(filepath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = filename.split(".")[-1].lower()
    mime = "image/png" if ext == "png" else "image/jpeg"
    return f"data:{mime};base64,{b64}"


async def generate_images(settings: Dict, prompt: str, n: int, video_config: Optional[Dict] = None, source_image: str = None) -> List[str]:
    """
    Call external image/video generation API and save media files (with batching)
    source_image: 可选的源图片文件名，用于图生视频
    """
    # For video generation, don't use batching
    if video_config:
        logger.info(f"Generating video with prompt: {prompt[:50]}...")
        return await _generate_video(settings, prompt, video_config, source_image)
    
    # For images, use batching
    BATCH_SIZE = 2
    all_filenames = []
    
    remaining = n
    while remaining > 0:
        current_n = min(remaining, BATCH_SIZE)
        try:
            logger.info(f"Processing batch of {current_n} images (remaining: {remaining})")
            filenames = await _generate_batch(settings, prompt, current_n)
            all_filenames.extend(filenames)
            remaining -= current_n
        except Exception as e:
            logger.error(f"Batch generation failed: {e!r}")
            if not all_filenames:
                raise
            break
            
    return all_filenames


async def _generate_batch(settings: Dict, prompt: str, n: int) -> List[str]:
    """
    Internal function to process a single batch
    """
    base_url = settings["base_url"].rstrip("/")
    api_key = settings["api_key"]
    model = settings["model"]
    proxy = settings.get("proxy")

    # Use standard images/generations endpoint
    url = f"{base_url}/v1/images/generations"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # Standard image generation payload
    payload = {
        "model": model,
        "prompt": prompt,
        "n": n,
        "stream": False,
        "size": "1024x1024",
        "quality": "standard",
        "response_format": "b64_json"  # Try b64_json first
    }

    filenames = []

    # Configure proxy if provided
    client_kwargs = {"timeout": 300.0}
    if proxy:
        logger.info(f"Using proxy: {proxy}")
        client_kwargs["proxy"] = proxy

    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            logger.info(f"Generating {n} images with prompt: {prompt[:50]}...")
            response = await client.post(url, json=payload, headers=headers)

            # Check for API-level errors before raise_for_status
            if response.status_code != 200:
                try:
                    err_data = response.json()
                    err_msg = err_data.get("error", {}).get("message", response.text)
                except Exception:
                    err_msg = response.text
                logger.error(f"API error (HTTP {response.status_code}): {err_msg}")
                raise ValueError(f"API error (HTTP {response.status_code}): {err_msg}")

            data = response.json()

            # Check for error in 200 response (some APIs return errors with 200)
            if "error" in data:
                err_msg = data["error"].get("message", str(data["error"]))
                logger.error(f"API returned error: {err_msg}")
                raise ValueError(f"API error: {err_msg}")

            # Process response
            if "data" in data and len(data["data"]) > 0:
                if data["data"][0].get("b64_json"):
                    filenames = await _save_b64_images(data["data"], prompt)
                    logger.info(f"Successfully saved {len(filenames)} images (b64_json)")
                elif data["data"][0].get("url"):
                    filenames = await _save_url_images(data["data"], prompt, client)
                    logger.info(f"Successfully saved {len(filenames)} images (url)")
                else:
                    raise ValueError("Unknown response format")
            else:
                raise ValueError("No image data in response")

        except (KeyError, ValueError) as e:
            # Only fallback for format/parsing issues, not API errors
            if "API error" in str(e):
                raise
            logger.warning(f"b64_json format failed, trying url format: {e}")
            # Fallback to url format
            payload["response_format"] = "url"

            try:
                response = await client.post(url, json=payload, headers=headers)

                if response.status_code != 200:
                    try:
                        err_data = response.json()
                        err_msg = err_data.get("error", {}).get("message", response.text)
                    except Exception:
                        err_msg = response.text
                    raise ValueError(f"API error (HTTP {response.status_code}): {err_msg}")

                data = response.json()

                if "error" in data:
                    err_msg = data["error"].get("message", str(data["error"]))
                    raise ValueError(f"API error: {err_msg}")

                if "data" in data and len(data["data"]) > 0:
                    if "url" in data["data"][0]:
                        filenames = await _save_url_images(data["data"], prompt, client)
                        logger.info(f"Successfully saved {len(filenames)} images (url fallback)")
                    else:
                        raise ValueError("No valid image data in response")
                else:
                    raise ValueError("Empty response data")
            except Exception as fallback_error:
                logger.error(f"Both b64_json and url formats failed: {fallback_error}")
                raise

        except Exception as e:
            logger.error(f"Image generation failed: {e!r}")
            raise

    return filenames


async def _save_b64_images(data: List[Dict], prompt: str) -> List[str]:
    """Save images from base64 encoded data"""
    os.makedirs("output", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = str(uuid.uuid4())[:8]

    filenames = []

    for idx, item in enumerate(data, 1):
        try:
            b64_data = item["b64_json"]
            # Fix base64 padding if missing
            missing_padding = len(b64_data) % 4
            if missing_padding:
                b64_data += "=" * (4 - missing_padding)
            image_bytes = base64.b64decode(b64_data)

            # Sanitize filename to prevent path traversal
            filename = f"{timestamp}_{short_id}_{idx}.png"
            filepath = os.path.join("output", filename)

            # Ensure the path is within output directory
            if not os.path.abspath(filepath).startswith(os.path.abspath("output")):
                raise ValueError("Invalid file path")

            with open(filepath, "wb") as f:
                f.write(image_bytes)

            filenames.append(filename)
            logger.debug(f"Saved image: {filename}")

        except Exception as e:
            logger.error(f"Failed to save image {idx}: {e}")
            raise

    return filenames


async def _save_url_images(data: List[Dict], prompt: str, client: httpx.AsyncClient) -> List[str]:
    """Download and save images from URLs"""
    os.makedirs("output", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = str(uuid.uuid4())[:8]

    filenames = []

    for idx, item in enumerate(data, 1):
        try:
            image_url = item["url"]

            # Download image
            logger.info(f"Downloading image from: {image_url}")
            response = await client.get(image_url)
            response.raise_for_status()
            image_bytes = response.content

            # Determine file extension from content-type or URL
            content_type = response.headers.get("content-type", "")
            if "jpeg" in content_type or "jpg" in content_type or image_url.endswith(".jpg"):
                ext = "jpg"
            elif "png" in content_type or image_url.endswith(".png"):
                ext = "png"
            else:
                ext = "jpg"  # default

            # Sanitize filename to prevent path traversal
            filename = f"{timestamp}_{short_id}_{idx}.{ext}"
            filepath = os.path.join("output", filename)

            # Ensure the path is within output directory
            if not os.path.abspath(filepath).startswith(os.path.abspath("output")):
                raise ValueError("Invalid file path")

            with open(filepath, "wb") as f:
                f.write(image_bytes)

            filenames.append(filename)
            logger.debug(f"Saved image: {filename}")

        except Exception as e:
            logger.error(f"Failed to download/save image {idx}: {e!r}")
            raise

    return filenames


async def _generate_video(settings: Dict, prompt: str, video_config: Dict, source_image: str = None) -> List[str]:
    """
    Generate video using the video model with chat completions endpoint
    source_image: 可选的源图片文件名，用于图生视频
    """
    base_url = settings["base_url"].rstrip("/")
    api_key = settings["api_key"]
    model = settings["model"]
    proxy = settings.get("proxy")

    # Use chat completions endpoint for video generation
    url = f"{base_url}/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # Build message content
    if source_image:
        # Image-to-video: use image_url format
        data_url = get_image_as_data_url(source_image)
        content = [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": prompt if prompt else "Animate this image"}
        ]
    else:
        # Text-to-video: plain text
        content = prompt

    # Video generation payload
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": content
        }],
        "video_config": video_config
    }

    # Configure proxy if provided
    client_kwargs = {"timeout": 600.0}  # Video generation takes longer
    if proxy:
        logger.info(f"Using proxy: {proxy}")
        client_kwargs["proxy"] = proxy

    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            logger.info(f"Generating video with prompt: {prompt[:50]}...")
            logger.info(f"Video config: {video_config}")
            response = await client.post(url, json=payload, headers=headers)

            # Check for API-level errors
            if response.status_code != 200:
                try:
                    err_data = response.json()
                    err_msg = err_data.get("error", {}).get("message", response.text)
                except Exception:
                    err_msg = response.text
                raise ValueError(f"API error (HTTP {response.status_code}): {err_msg}")

            # Parse response, handle empty body
            response_text = response.text.strip()
            if not response_text:
                raise ValueError("API returned empty response body")

            data = response.json()

            # Check for error in 200 response
            if "error" in data:
                err_msg = data["error"].get("message", str(data["error"]))
                raise ValueError(f"API error: {err_msg}")

            # Process response - video URL should be in the response
            if "choices" in data and len(data["choices"]) > 0:
                choice = data["choices"][0]
                message = choice.get("message", {})
                content = message.get("content", "")
                
                # Try to extract video URL from content
                # The actual format depends on your API's response
                # This is a generic implementation
                video_url = None

                # Check if there's a direct video URL in the response
                if "url" in message:
                    video_url = message["url"]
                elif content and ("http" in content or "https" in content):
                    # Try to extract URL from content
                    import re
                    # Match URLs ending with common video extensions
                    urls = re.findall(r'https?://[^\s<>")\]]+\.(?:mp4|webm|mov|avi)', content)
                    if urls:
                        video_url = urls[0]
                    else:
                        # Fallback to general URL pattern but be more careful
                        urls = re.findall(r'https?://[^\s<>")\]]+', content)
                        if urls:
                            video_url = urls[0]
                
                if video_url:
                    filename = await _save_video_from_url(video_url, prompt, client)
                    logger.info(f"Successfully saved video: {filename}")
                    return [filename]
                else:
                    raise ValueError("No video URL found in response")
            else:
                raise ValueError("Invalid response format")

        except Exception as e:
            logger.error(f"Video generation failed: {e!r}")
            raise

    return []


async def _save_video_from_url(video_url: str, prompt: str, client: httpx.AsyncClient) -> str:
    """Download and save video from URL"""
    os.makedirs("output", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = str(uuid.uuid4())[:8]

    try:
        # Download video
        logger.info(f"Downloading video from: {video_url}")
        response = await client.get(video_url)
        response.raise_for_status()
        video_bytes = response.content

        # Determine file extension from content-type or URL
        content_type = response.headers.get("content-type", "")
        if "mp4" in content_type or video_url.endswith(".mp4"):
            ext = "mp4"
        elif "webm" in content_type or video_url.endswith(".webm"):
            ext = "webm"
        else:
            ext = "mp4"  # default

        # Sanitize filename to prevent path traversal
        filename = f"{timestamp}_{short_id}.{ext}"
        filepath = os.path.join("output", filename)

        # Ensure the path is within output directory
        if not os.path.abspath(filepath).startswith(os.path.abspath("output")):
            raise ValueError("Invalid file path")

        with open(filepath, "wb") as f:
            f.write(video_bytes)

        logger.info(f"Saved video: {filename} ({len(video_bytes)} bytes)")
        return filename

    except Exception as e:
        logger.error(f"Failed to download/save video: {e!r}")
        raise

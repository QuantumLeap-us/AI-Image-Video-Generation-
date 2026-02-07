import json
import os
from typing import Dict, List, Optional, Tuple


CONFIG_PATH = os.getenv("AIGPIC_CONFIG_PATH", os.path.join("data", "configs.json"))


def _load_config_data() -> Dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def list_configs() -> Tuple[List[Dict], Optional[str], int]:
    data = _load_config_data()
    configs = data.get("api_configs", [])
    default_name = data.get("default")
    max_concurrent = data.get("max_concurrent", 2)
    return configs, default_name, max_concurrent


def get_max_concurrent() -> int:
    _, _, max_concurrent = list_configs()
    try:
        max_concurrent = int(max_concurrent)
    except (TypeError, ValueError):
        max_concurrent = 2
    return max(1, min(10, max_concurrent))


def select_config(config_name: Optional[str]) -> Optional[Dict]:
    configs, default_name, _ = list_configs()

    if config_name:
        for config in configs:
            if config.get("name") == config_name:
                return config

    return configs[0] if configs else None


def list_config_summaries() -> Dict:
    configs, default_name, max_concurrent = list_configs()
    summaries = []

    for config in configs:
        name = config.get("name")
        if not name:
            continue
        summaries.append({
            "name": name,
            "base_url": config.get("base_url", ""),
            "model": config.get("model", "grok-imagine-1.0"),
            "max_concurrent": max_concurrent
        })

    default = None
    if summaries:
        default = summaries[0]["name"]

    return {"configs": summaries, "default": default}

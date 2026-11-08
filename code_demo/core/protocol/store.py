import threading
import time
from pathlib import Path
import json

from code_demo.core.config import REQUESTS_DIR


class RequestStore:
    """
    用于协议工作流的持久化请求记录。
    协议状态应能持续足够长时间，以便检查、恢复或协调。
    本存储机制在 .team/requests/ 目录下为每个 request_id 保存一个 JSON 文件。

    持久化协议请求记录。
    s17 不应退回到 s16 之前的内存式跟踪器。这些请求文件使自主队友能够稍后检查或恢复协议状态。
    """

    def __init__(self, base_dir: Path):
        self.dir = base_dir  # 请求持久化目录
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # 保护请求文件读写的线程锁

    def _path(self, request_id: str) -> Path:
        return self.dir / f"{request_id}.json"

    # 创建请求
    def create(self, record: dict) -> dict:
        request_id = record["request_id"]
        with self._lock:
            self._path(request_id).write_text(json.dumps(record, indent=2, ensure_ascii=False))
        return record

    # 获取请求
    def get(self, request_id: str) -> dict | None:
        path = self._path(request_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # 更新请求内容
    def update(self, request_id: str, **changes) -> dict | None:
        with self._lock:
            record = self.get(request_id)
            if not record:
                return None
            record.update(changes)
            record["updated_at"] = time.time()
            self._path(request_id).write_text(json.dumps(record, indent=2, ensure_ascii=False))
        return record


REQUEST_STORE = RequestStore(REQUESTS_DIR)

# 注意：_append_claim_event, _task_allows_role, is_claimable_task
# 已统一移至 teammate/functions.py，此处不再重复定义。

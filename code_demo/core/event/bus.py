import time
from pathlib import Path
import json

from code_demo.core.config import VALID_MSG_TYPES, INBOX_DIR


class EventBUS:
    def __init__(self, event_log_path: Path):
        self.path = event_log_path  # 事件日志文件路径
        self.path.parent.mkdir(parents = True, exist_ok = True)
        if not self.path.exists():
            self.path.write_text("")
    
    # 追加一条worktree 的事件
    def emit(self,event: str, task_id = None, wt_name=None, error = None, **extra):
        payload = {"event": event, "timestamp": time.time()}
        if task_id is not None:
            payload["task_id"] = task_id
        if wt_name:
            payload["worktree"] = wt_name
        if error:
            payload["error"] = error
        payload.update(extra)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    # 列出最近的事件（默认20条）
    def list_recent(self, limit: int = 20) -> str:
        n = max(1, min(int(limit or 20), 200))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        items = []
        for line in lines[-n:]:
            try:
                items.append(json.loads(line))
            except Exception:
                items.append({"event": "parse_error", "raw": line})
        try:
            return json.dumps(items, indent=2, ensure_ascii=False)
        except Exception as e:
            return f"Error: worktree json 格式有误{e}，检查json格式文件，并手动修改或删除"





# -- MessageBus：每位队友拥有独立的 JSONL 收件箱 --
class MessageBUS:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir  # 代理消息收件箱目录
        self.dir.mkdir(parents=True, exist_ok=True)

    # 发送消息给agent同伴
    def send(self, sender: str, to: str, content: str,
            msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: 无效的消息类型 '{msg_type}'。有效类型：{VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time()
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"发送 {msg_type} 至 {to}"
    
    # 阅读收件箱
    def read_inbox(self, name: str):
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []

        # 使用原子重命名避免竞态：读取和清空之间的窗口期
        temp_path = inbox_path.with_suffix(".reading")
        try:
            inbox_path.rename(temp_path)
        except FileNotFoundError:
            return []
        except OSError:
            # rename 失败时回退到原来的方式
            messages = []
            for line in inbox_path.read_text(encoding="utf-8").strip().splitlines():
                if line:
                    messages.append(json.loads(line))
            inbox_path.write_text("")
            return messages

        # 读取临时文件内容
        messages = []
        try:
            for line in temp_path.read_text(encoding="utf-8").strip().splitlines():
                if line:
                    messages.append(json.loads(line))
        finally:
            temp_path.unlink(missing_ok=True)

        return messages

    # 广播同步消息
    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"已向 {count} 个队友广播"
    

BUS = MessageBUS(INBOX_DIR)



# 相较于15章， 多了 RequestStore

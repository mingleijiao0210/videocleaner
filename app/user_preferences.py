"""保存在软件目录内的少量本地用户偏好。"""

from __future__ import annotations

import json
from pathlib import Path

from .logger import LOGGER
from .settings import project_root, writable_root


class UserPreferences:
    """读写不含账号信息的本地 JSON 设置。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            writable_root() / "settings" / "user_preferences.json"
        )

    def _read(self) -> dict[str, object]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            LOGGER.warning("本地设置文件无法读取，将使用默认目录: %s", self.path)
            return {}

    def last_input_directory(self, fallback: Path | None = None) -> Path:
        """返回仍然存在的上次视频目录，否则返回安全的默认目录。"""

        default = Path(fallback or project_root())
        value = self._read().get("last_input_directory")
        if isinstance(value, str) and value:
            candidate = Path(value)
            if candidate.is_dir():
                return candidate
        return default

    def set_last_input_directory(self, directory: Path) -> None:
        """以原子替换方式保存上次由用户选择的目录。"""

        candidate = Path(directory).resolve()
        if not candidate.is_dir():
            return
        data = self._read()
        data["last_input_directory"] = str(candidate)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
            LOGGER.info("已记住上次视频文件夹: %s", candidate)
        except OSError:
            temporary.unlink(missing_ok=True)
            LOGGER.exception("无法保存上次视频文件夹")

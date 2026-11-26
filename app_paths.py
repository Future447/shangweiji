"""
统一路径管理模块
确保源码运行和 EXE 运行使用相同的用户数据路径，避免数据写入临时目录。
支持从旧路径迁移合法的 ADC_raw 配置。
"""

import json
import math
import os
import shutil
import sys
from pathlib import Path


def _is_frozen() -> bool:
    """判断是否在 PyInstaller 打包环境中运行。"""
    return getattr(sys, 'frozen', False)


def _get_meipass() -> str:
    """获取 PyInstaller 临时解包目录，非打包环境返回空字符串。"""
    return getattr(sys, '_MEIPASS', '')


def get_config_dir() -> Path:
    """
    获取配置文件目录。
    优先使用 %LOCALAPPDATA%/涡流传感器采集系统/
    回退到 ~/.涡流传感器采集系统/
    """
    local_app = os.environ.get('LOCALAPPDATA')
    if local_app:
        config_dir = Path(local_app) / "涡流传感器采集系统"
    else:
        config_dir = Path.home() / ".涡流传感器采集系统"

    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_config_path() -> Path:
    """获取 config.json 的完整路径。"""
    return get_config_dir() / "config.json"


def get_default_csv_dir() -> Path:
    """
    获取默认 CSV 保存目录。
    优先使用 %USERPROFILE%/Documents/涡流传感器采集系统/data/
    回退到 ~/涡流传感器采集系统/data/
    """
    documents = None

    # Windows 文档目录
    user_profile = os.environ.get('USERPROFILE')
    if user_profile:
        documents = Path(user_profile) / "Documents"
        if not documents.is_dir():
            documents = None

    # 回退
    if documents is None:
        documents = Path.home() / "Documents"
        if not documents.is_dir():
            documents = Path.home()

    csv_dir = documents / "涡流传感器采集系统" / "data"
    csv_dir.mkdir(parents=True, exist_ok=True)
    return csv_dir


def _is_valid_adc_raw_config(data: dict) -> bool:
    """检查配置是否为合法的 ADC_raw 格式。"""
    if not isinstance(data, dict):
        return False
    if data.get("protocol") != "ADC_raw":
        return False
    if data.get("unit") != "ADC":
        return False
    bl = data.get("baseline")
    if bl is None:
        return False
    try:
        val = float(bl)
    except (ValueError, TypeError):
        return False
    if not math.isfinite(val):
        return False
    if val < 0 or val > 4095:
        return False
    return True


def _try_migrate_config(target_path: Path) -> bool:
    """
    尝试从旧路径迁移合法的 ADC_raw 配置。
    不删除旧文件，只复制。
    返回 True 表示迁移成功。
    """
    if target_path.is_file():
        return False  # 目标已存在，不迁移

    # 候选旧路径
    candidates = []

    # 1. 源码目录旁的 config.json
    src_dir = Path(__file__).resolve().parent
    candidates.append(src_dir / "config.json")

    # 2. EXE 所在目录旁的 config.json
    if _is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "config.json")

    for old_path in candidates:
        if not old_path.is_file():
            continue
        if old_path.resolve() == target_path.resolve():
            continue

        try:
            with open(old_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if not _is_valid_adc_raw_config(data):
            continue

        # 复制到目标（原子写入）
        try:
            tmp_path = target_path.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            shutil.move(str(tmp_path), str(target_path))
            return True
        except OSError:
            # 清理临时文件
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            continue

    return False


def ensure_paths() -> dict:
    """
    确保所有必要路径存在，尝试迁移旧配置。
    返回 {"config_path": Path, "csv_dir": Path, "migrated": bool, "error": str}
    """
    result = {"config_path": None, "csv_dir": None, "migrated": False, "error": ""}

    try:
        config_path = get_config_path()
        result["config_path"] = config_path
    except OSError as e:
        result["error"] = f"无法创建配置目录: {e}"
        return result

    try:
        csv_dir = get_default_csv_dir()
        result["csv_dir"] = csv_dir
    except OSError as e:
        result["error"] = f"无法创建 CSV 目录: {e}"
        return result

    # 尝试迁移
    try:
        result["migrated"] = _try_migrate_config(config_path)
    except Exception:
        pass

    return result

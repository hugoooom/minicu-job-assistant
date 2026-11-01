"""在打开浏览器前验证 OCR 推理依赖可以实际导入。"""

import importlib
import sys
from pathlib import Path


def check_ocr_runtime():
    try:
        runtime = importlib.import_module("onnxruntime")
        if not runtime.get_available_providers():
            raise RuntimeError("onnxruntime 没有可用的推理提供程序")
    except (ImportError, OSError, AttributeError, RuntimeError) as exc:
        requirements = Path(__file__).resolve().parent / "requirements.txt"
        raise RuntimeError(
            f"OCR 环境检查失败，尚未打开任务浏览器：{exc}\n"
            f"当前 Python：{sys.executable}\n"
            "请在 PowerShell 用这个 Python 安装完整依赖：\n"
            f'& "{sys.executable}" -m pip install -r "{requirements}"\n'
            "安装后验证：\n"
            f'& "{sys.executable}" -c "import onnxruntime; print(onnxruntime.__version__)"\n'
            "若出现 DLL load failed，请查看 README 的 Windows 依赖排查。"
        ) from exc

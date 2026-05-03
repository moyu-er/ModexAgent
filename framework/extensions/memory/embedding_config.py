"""嵌入函数配置 - 支持本地模型缓存"""

import os
from collections.abc import Callable
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent

# 模型缓存目录
DEFAULT_MODEL_CACHE_DIR = PROJECT_ROOT / ".cache" / "chroma_models"

# 默认模型名称
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"


def get_model_cache_dir() -> Path:
    """获取模型缓存目录"""
    cache_dir = os.environ.get("CHROMA_MODEL_CACHE_DIR")
    if cache_dir:
        return Path(cache_dir)
    return DEFAULT_MODEL_CACHE_DIR


def ensure_model_cache_dir() -> Path:
    """确保模型缓存目录存在"""
    cache_dir = get_model_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_sentence_transformer_ef(
    model_name: str = DEFAULT_MODEL_NAME,
    cache_dir: Path | None = None,
    device: str = "cpu",
):
    """
    获取SentenceTransformer嵌入函数。
    
    使用本地缓存,避免重复下载。
    
    Args:
        model_name: 模型名称
        cache_dir: 缓存目录,默认使用项目内缓存
        device: 运行设备(cpu/cuda)
    
    Returns:
        嵌入函数
    """
    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    except ImportError:
        raise ImportError(
            "chromadb is required. Install with: pip install chromadb"
        )

    if cache_dir is None:
        cache_dir = ensure_model_cache_dir()

    # 设置环境变量让sentence-transformers使用我们的缓存目录
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(cache_dir)

    return SentenceTransformerEmbeddingFunction(
        model_name=model_name,
        device=device,
    )


def get_onnx_mini_lm_ef(cache_dir: Path | None = None):
    """
    获取ONNX MiniLM嵌入函数(更快的CPU推理)。
    
    Args:
        cache_dir: 缓存目录
    
    Returns:
        嵌入函数
    """
    try:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
    except ImportError:
        raise ImportError(
            "onnxruntime is required. Install with: pip install onnxruntime"
        )

    if cache_dir is None:
        cache_dir = ensure_model_cache_dir()

    # ONNX模型会缓存到指定目录
    old_cache = os.environ.get("CHROMA_CACHE_DIR")
    os.environ["CHROMA_CACHE_DIR"] = str(cache_dir)

    try:
        ef = ONNXMiniLM_L6_V2()
    finally:
        if old_cache:
            os.environ["CHROMA_CACHE_DIR"] = old_cache
        else:
            del os.environ["CHROMA_CACHE_DIR"]

    return ef


def get_default_ef():
    """获取默认嵌入函数(优先使用本地缓存)"""
    cache_dir = ensure_model_cache_dir()

    # 检查是否已有下载的模型
    model_path = cache_dir / "sentence-transformers" / DEFAULT_MODEL_NAME

    if model_path.exists():
        # 使用本地模型
        return get_sentence_transformer_ef(
            model_name=str(model_path),
            cache_dir=cache_dir,
        )

    # 首次使用,会下载到缓存目录
    return get_sentence_transformer_ef(cache_dir=cache_dir)


class CachedEmbeddingFunction:
    """
    带缓存的嵌入函数包装器。
    
    自动管理模型下载和缓存,避免重复下载。
    """

    _instance = None
    _ef = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._ef is None:
            self._ef = get_default_ef()

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self._ef(input)


def get_cached_ef() -> Callable[[list[str]], list[list[float]]]:
    """
    获取全局缓存的嵌入函数实例。
    
    单例模式,确保整个应用使用同一个嵌入函数实例。
    """
    return CachedEmbeddingFunction()

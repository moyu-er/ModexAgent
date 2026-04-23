"""FAISS 向量记忆存储实现

基于 FAISS 的本地文件向量存储，零配置、单文件、无需外部服务。

依赖:
    pip install faiss-cpu  # 或 faiss-gpu (如果有GPU)
    pip install sentence-transformers  # 用于文本向量化

Example:
    store = FAISSMemoryStore(
        workspace=Path("./workspace"),
        agent_id="chart_agent",
        embedding_model="all-MiniLM-L6-v2"  # 轻量级模型
    )

    # 添加记忆
    await store.add(agent_id, MemoryEntry(
        id="mem_1",
        content="用户喜欢蓝色主题的图表",
        importance=0.8
    ))

    # 向量搜索
    results = await store.search(agent_id, "用户偏好", limit=3)
"""

import json
import logging
import pickle
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from framework.core.memory import MemoryEntry, MemoryStore

logger = logging.getLogger(__name__)


class FAISSMemoryStore(MemoryStore):
    """
    FAISS 向量记忆存储。

    使用 FAISS 进行高效的向量相似度搜索，数据持久化到本地文件。

    存储结构:
        workspace/memory/
        └── {agent_id}/
            └── vector/
                ├── index.faiss          # FAISS 索引文件
                └── metadata.pkl         # 元数据 (id, content, metadata等)

    Attributes:
        workspace: 工作区根目录
        agent_id: Agent标识
        embedding_model: 使用的嵌入模型名称
        dimension: 向量维度
        index_type: FAISS索引类型 ("Flat", "IVF", "HNSW")
    """

    def __init__(
        self,
        workspace: Path,
        agent_id: str,
        embedding_model: str = "all-MiniLM-L6-v2",
        dimension: int = 384,
        index_type: str = "Flat",
        create_dirs: bool = True,
        embedding_fn: Optional[Callable[[str], List[float]]] = None,
    ):
        """
        初始化 FAISS 记忆存储。

        Args:
            workspace: 工作区根目录
            agent_id: Agent标识
            embedding_model: SentenceTransformer 模型名称
            dimension: 向量维度 (默认384对应 all-MiniLM-L6-v2)
            index_type: FAISS索引类型 ("Flat"=精确, "IVF"=快速, "HNSW"=平衡)
            create_dirs: 是否自动创建目录
            embedding_fn: 可选的自定义embedding函数（用于测试）
        """
        self.workspace = Path(workspace)
        self.agent_id = agent_id
        self.embedding_model_name = embedding_model
        self.dimension = dimension
        self.index_type = index_type
        self._embedding_fn = embedding_fn

        # 存储路径
        self.vector_dir = self.workspace / "memory" / agent_id / "vector"
        self.index_file = self.vector_dir / "index.faiss"
        self.metadata_file = self.vector_dir / "metadata.pkl"

        # 延迟初始化
        self._index: Optional[Any] = None
        self._metadata: Dict[str, Dict[str, Any]] = {}
        self._embedding_model: Optional[Any] = None

        if create_dirs:
            self.vector_dir.mkdir(parents=True, exist_ok=True)

        # 尝试加载已有数据
        self._load_if_exists()

    def _get_embedding_model(self) -> Any:
        """延迟加载嵌入模型"""
        if self._embedding_model is None:
            try:
                from sentence_transformers import SentenceTransformer

                self._embedding_model = SentenceTransformer(self.embedding_model_name)
            except ImportError:
                raise ImportError(
                    "sentence-transformers is required for FAISSMemoryStore. "
                    "Install with: pip install sentence-transformers"
                )
        return self._embedding_model

    def _text_to_vector(self, text: str) -> List[float]:
        """将文本转换为向量"""
        # 如果提供了自定义embedding函数，使用它
        if self._embedding_fn is not None:
            return self._embedding_fn(text)

        model = self._get_embedding_model()
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()

    def _init_index(self) -> Any:
        """初始化 FAISS 索引"""
        try:
            import faiss
        except ImportError:
            raise ImportError(
                "faiss-cpu or faiss-gpu is required. Install with: pip install faiss-cpu"
            )

        if self.index_type == "Flat":
            # 精确搜索，适合小规模数据 (<10k)
            return faiss.IndexFlatIP(self.dimension)  # 内积相似度
        elif self.index_type == "IVF":
            # 倒排文件，适合中等规模 (10k-1M)
            quantizer = faiss.IndexFlatIP(self.dimension)
            return faiss.IndexIVFFlat(quantizer, self.dimension, 100)
        elif self.index_type == "HNSW":
            # 图索引，适合大规模数据，搜索快
            return faiss.IndexHNSWFlat(self.dimension, 32)
        else:
            return faiss.IndexFlatIP(self.dimension)

    def _load_if_exists(self) -> None:
        """如果存在，加载已有索引和元数据"""
        if self.index_file.exists():
            try:
                import faiss

                self._index = faiss.read_index(str(self.index_file))
            except Exception as e:
                logger.warning(f"Failed to load FAISS index: {e}")
                self._index = None

        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, "rb") as f:
                    self._metadata = pickle.load(f)
            except Exception as e:
                logger.warning(f"Failed to load metadata: {e}")
                self._metadata = {}

    def _save(self) -> None:
        """保存索引和元数据到磁盘"""
        if self._index is not None:
            try:
                import faiss

                faiss.write_index(self._index, str(self.index_file))
            except Exception as e:
                logger.warning(f"Failed to save FAISS index: {e}")

        try:
            with open(self.metadata_file, "wb") as f:
                pickle.dump(self._metadata, f)
        except Exception as e:
                logger.warning(f"Failed to save metadata: {e}")

    async def add(self, agent_id: str, entry: MemoryEntry) -> None:
        """
        添加记忆到向量存储。

        Args:
            agent_id: Agent标识
            entry: 记忆条目
        """
        try:
            import numpy as np
            import faiss
        except ImportError:
            raise ImportError("numpy and faiss are required.")

        # 初始化索引（如果还没有）
        if self._index is None:
            self._index = self._init_index()

        # 文本向量化
        vector = self._text_to_vector(entry.content)
        vector_np = np.array([vector], dtype=np.float32)

        # 归一化向量（用于余弦相似度）
        faiss.normalize_L2(vector_np)

        # 添加到 FAISS 索引
        self._index.add(vector_np)

        # 保存元数据
        idx = len(self._metadata)
        self._metadata[str(idx)] = {
            "id": entry.id,
            "content": entry.content,
            "source_session_id": entry.source_session_id,
            "metadata": entry.metadata,
            "created_at": entry.created_at.isoformat(),
            "importance": entry.importance,
            "agent_id": agent_id,
        }

        # 持久化
        self._save()

    async def search(self, agent_id: str, query: str, limit: int = 5) -> List[MemoryEntry]:
        """
        向量搜索相关记忆。

        Args:
            agent_id: Agent标识
            query: 查询文本
            limit: 返回结果数量限制

        Returns:
            相关记忆条目列表
        """
        if self._index is None or len(self._metadata) == 0:
            return []

        try:
            import numpy as np
            import faiss
        except ImportError:
            raise ImportError("numpy and faiss are required.")

        # 查询向量化
        query_vector = self._text_to_vector(query)
        query_np = np.array([query_vector], dtype=np.float32)

        # 归一化
        faiss.normalize_L2(query_np)

        # 搜索
        distances, indices = self._index.search(query_np, min(limit, len(self._metadata)))

        # 构建结果
        results = []
        for i, idx in enumerate(indices[0]):
            if idx < 0 or idx >= len(self._metadata):
                continue

            meta = self._metadata.get(str(idx))
            if meta:
                entry = MemoryEntry(
                    id=meta["id"],
                    content=meta["content"],
                    source_session_id=meta.get("source_session_id"),
                    metadata=meta.get("metadata", {}),
                    created_at=datetime.fromisoformat(meta["created_at"]),
                    importance=meta.get("importance", 1.0),
                )
                results.append(entry)

        return results

    async def get_recent(self, agent_id: str, limit: int = 10) -> List[MemoryEntry]:
        """
        获取最近的记忆。

        Args:
            agent_id: Agent标识
            limit: 返回数量限制

        Returns:
            最近添加的记忆列表
        """
        # 按索引顺序（添加顺序）获取
        sorted_indices = sorted(self._metadata.keys(), key=lambda k: int(k), reverse=True)[:limit]

        results = []
        for idx in sorted_indices:
            meta = self._metadata[idx]
            entry = MemoryEntry(
                id=meta["id"],
                content=meta["content"],
                source_session_id=meta.get("source_session_id"),
                metadata=meta.get("metadata", {}),
                created_at=datetime.fromisoformat(meta["created_at"]),
                importance=meta.get("importance", 1.0),
            )
            results.append(entry)

        return results

    async def delete(self, agent_id: str, memory_id: str) -> bool:
        """
        删除记忆。

        注意：FAISS 不支持直接删除，这里采用标记删除方式。

        Args:
            agent_id: Agent标识
            memory_id: 记忆ID

        Returns:
            是否删除成功
        """
        # 查找并标记为已删除
        for idx, meta in self._metadata.items():
            if meta["id"] == memory_id:
                meta["deleted"] = True
                meta["deleted_at"] = datetime.now().isoformat()
                self._save()
                return True

        return False

    def get_stats(self) -> Dict[str, Any]:
        """
        获取存储统计信息。

        Returns:
            统计信息字典
        """
        total = len(self._metadata)
        active = sum(1 for m in self._metadata.values() if not m.get("deleted", False))
        deleted = total - active

        return {
            "total_entries": total,
            "active_entries": active,
            "deleted_entries": deleted,
            "index_type": self.index_type,
            "dimension": self.dimension,
            "embedding_model": self.embedding_model_name,
        }

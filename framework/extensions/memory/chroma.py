"""ChromaDB向量记忆存储实现"""

import json
import hashlib
from datetime import datetime
from typing import List, Optional, Dict, Any
import numpy as np

from framework.core.memory import MemoryStore, MemoryEntry
from .embedding_config import get_cached_ef


class ChromaMemoryStore(MemoryStore):
    """
    基于ChromaDB的向量记忆存储。

    使用ChromaDB提供向量搜索能力,支持语义相似度检索。

    Example:
        store = ChromaMemoryStore(
            persist_directory="./chroma_db",
            embedding_function=openai_embedding,
        )

        # 添加记忆
        await store.add("agent_1", MemoryEntry(
            content="User likes Python",
            importance=0.8,
        ))

        # 语义搜索
        memories = await store.search("agent_1", "programming language", limit=5)
    """

    def __init__(
        self,
        persist_directory: Optional[str] = None,
        embedding_function=None,
        collection_name: str = "agent_memories",
        distance_func: str = "cosine",
    ):
        """
        初始化ChromaDB记忆存储。

        Args:
            persist_directory: 持久化目录,None则内存存储
            embedding_function: 嵌入函数,用于生成向量
            collection_name: 集合名称
            distance_func: 距离函数(cosine/l2/ip)
        """
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError:
            raise ImportError(
                "chromadb is required for ChromaMemoryStore. Install with: pip install chromadb"
            )

        self._persist_directory = persist_directory
        self._collection_name = collection_name
        self._distance_func = distance_func

        # 使用默认嵌入函数(如果未提供)
        if embedding_function is None:
            # 使用带缓存的嵌入函数,模型会保存到项目目录
            # 首次使用时会自动下载(~80MB),后续直接使用本地缓存
            self._embedding_function = get_cached_ef()
        else:
            self._embedding_function = embedding_function

        # 初始化ChromaDB客户端
        if persist_directory:
            self._client = chromadb.PersistentClient(
                path=persist_directory,
                settings=Settings(
                    anonymized_telemetry=False,
                ),
            )
        else:
            self._client = chromadb.EphemeralClient()

        # 缓存collection引用
        self._collections: Dict[str, Any] = {}

    def _get_collection(self, agent_id: str):
        """获取或创建agent的collection"""
        if agent_id not in self._collections:
            collection_name = f"{self._collection_name}_{agent_id}"

            # 尝试获取现有collection
            try:
                collection = self._client.get_collection(
                    name=collection_name,
                    embedding_function=self._embedding_function,
                )
            except Exception:
                # 创建新collection
                collection = self._client.create_collection(
                    name=collection_name,
                    embedding_function=self._embedding_function,
                    metadata={"hnsw:space": self._distance_func},
                )

            self._collections[agent_id] = collection

        return self._collections[agent_id]

    async def add(self, agent_id: str, entry: MemoryEntry) -> None:
        """
        添加记忆条目。

        Args:
            agent_id: Agent唯一标识
            entry: 记忆条目
        """
        collection = self._get_collection(agent_id)

        # 构建metadata - 使用MemoryEntry的字段名
        metadata = {
            "created_at": entry.created_at.isoformat(),
            "importance": entry.importance,
            "source_session_id": entry.source_session_id or "",
        }

        # 添加自定义metadata
        if entry.metadata:
            # 只保留简单类型
            for key, value in entry.metadata.items():
                if isinstance(value, (str, int, float, bool)):
                    metadata[key] = value

        # 添加到ChromaDB
        collection.add(
            ids=[entry.id],
            documents=[entry.content],
            metadatas=[metadata],
        )

    async def search(
        self,
        agent_id: str,
        query: str,
        limit: int = 5,
    ) -> List[MemoryEntry]:
        """
        语义搜索记忆。

        Args:
            agent_id: Agent唯一标识
            query: 查询文本
            limit: 返回结果数量

        Returns:
            记忆条目列表,按相似度排序
        """
        collection = self._get_collection(agent_id)

        # 查询ChromaDB
        results = collection.query(
            query_texts=[query],
            n_results=limit,
            include=["documents", "metadatas", "distances"],
        )

        memories = []

        if results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                memory_id = results["ids"][0][i] if results["ids"] else str(i)

                # 解析created_at
                created_at_str = metadata.get("created_at", "")
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                except (ValueError, TypeError):
                    created_at = datetime.now()

                entry = MemoryEntry(
                    id=memory_id,
                    content=doc,
                    created_at=created_at,
                    importance=metadata.get("importance", 0.5),
                    source_session_id=metadata.get("source_session_id") or None,
                    metadata={
                        k: v
                        for k, v in metadata.items()
                        if k not in ["created_at", "importance", "source_session_id"]
                    },
                )
                memories.append(entry)

        return memories

    async def get_recent(
        self,
        agent_id: str,
        limit: int = 10,
    ) -> List[MemoryEntry]:
        """
        获取最近添加的记忆。

        Args:
            agent_id: Agent唯一标识
            limit: 返回结果数量

        Returns:
            记忆条目列表,按时间倒序
        """
        collection = self._get_collection(agent_id)

        # 获取所有记忆(ChromaDB不支持直接按时间排序)
        # 这里获取最近添加的limit*2条,然后手动排序
        total_count = collection.count()
        if total_count == 0:
            return []

        # 获取所有条目
        results = collection.get(
            limit=min(total_count, limit * 2),
            include=["documents", "metadatas"],
        )

        memories = []

        if results["documents"]:
            for i, doc in enumerate(results["documents"]):
                metadata = results["metadatas"][i] if results["metadatas"] else {}
                memory_id = results["ids"][i] if results["ids"] else str(i)

                # 解析created_at
                created_at_str = metadata.get("created_at", "")
                try:
                    created_at = datetime.fromisoformat(created_at_str)
                except (ValueError, TypeError):
                    created_at = datetime.now()

                entry = MemoryEntry(
                    id=memory_id,
                    content=doc,
                    created_at=created_at,
                    importance=metadata.get("importance", 0.5),
                    source_session_id=metadata.get("source_session_id") or None,
                    metadata={
                        k: v
                        for k, v in metadata.items()
                        if k not in ["created_at", "importance", "source_session_id"]
                    },
                )
                memories.append(entry)

        # 按时间倒序排序
        memories.sort(key=lambda x: x.created_at, reverse=True)

        return memories[:limit]

    async def delete(
        self,
        agent_id: str,
        content_filter: Optional[str] = None,
    ) -> int:
        """
        删除记忆。

        Args:
            agent_id: Agent唯一标识
            content_filter: 内容过滤条件(包含此字符串的记忆将被删除)

        Returns:
            删除的记忆数量
        """
        collection = self._get_collection(agent_id)

        if content_filter:
            # 搜索匹配的记忆
            results = collection.query(
                query_texts=[content_filter],
                n_results=1000,
                include=[],
            )

            if results["ids"] and results["ids"][0]:
                ids_to_delete = results["ids"][0]
                collection.delete(ids=ids_to_delete)
                return len(ids_to_delete)
        else:
            # 删除所有记忆
            count = collection.count()
            all_ids = collection.get()["ids"]
            if all_ids:
                collection.delete(ids=all_ids)
            return count

        return 0

    async def clear_agent(self, agent_id: str) -> None:
        """
        清空指定Agent的所有记忆。

        Args:
            agent_id: Agent唯一标识
        """
        collection = self._get_collection(agent_id)
        # 删除collection中的所有记录
        # 使用where={"$and": []}来匹配所有记录(ChromaDB需要非空where条件)
        try:
            # 先获取所有ID
            all_ids = collection.get()["ids"]
            if all_ids:
                collection.delete(ids=all_ids)
        except Exception:
            # 如果获取失败,尝试删除collection并重建
            collection_name = f"{self._collection_name}_{agent_id}"
            try:
                self._client.delete_collection(collection_name)
            except Exception:
                pass
            # 从缓存中移除
            if agent_id in self._collections:
                del self._collections[agent_id]

    def get_stats(self, agent_id: str) -> Dict[str, Any]:
        """
        获取记忆统计信息。

        Args:
            agent_id: Agent唯一标识

        Returns:
            统计信息字典
        """
        collection = self._get_collection(agent_id)

        return {
            "total_memories": collection.count(),
            "agent_id": agent_id,
            "collection_name": f"{self._collection_name}_{agent_id}",
        }

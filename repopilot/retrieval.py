"""Qdrant-backed retrieval; OpenAI embeddings when configured, lexical fallback otherwise."""
import hashlib
import math
import os
import re

DIM = 1536
COLLECTION = "repopilot_context_v1"


def _terms(text: str):
    return re.findall(r"[a-z][a-z0-9_]{2,}", text.lower())


def _lexical_vector(text: str) -> list[float]:
    vector = [0.0] * DIM
    for term in _terms(text):
        digest = int.from_bytes(hashlib.blake2b(term.encode(), digest_size=8).digest(), "big")
        vector[digest % DIM] += 1 if (digest >> 11) & 1 else -1
    magnitude = math.sqrt(sum(x * x for x in vector)) or 1
    return [x / magnitude for x in vector]


class ContextIndex:
    def __init__(self, client=None, embeddings=None):
        from qdrant_client import QdrantClient
        self.client = client or QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
        self.embeddings = embeddings or ("openai" if os.getenv("OPENAI_API_KEY") else "lexical")
        self.collection = COLLECTION + ("_semantic" if self.embeddings == "openai" else "_lexical")
        from qdrant_client.models import Distance, VectorParams
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(self.collection, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))

    def embed(self, text):
        if self.embeddings == "openai":
            from openai import OpenAI
            result = OpenAI().embeddings.create(model=os.getenv("REPOPILOT_EMBED_MODEL", "text-embedding-3-small"),
                                                input=text[:24000], dimensions=DIM)
            return result.data[0].embedding
        return _lexical_vector(text)

    def add(self, identifier: str, text: str):
        from qdrant_client.models import PointStruct
        if not identifier or not text.strip():
            raise ValueError("id and text are required")
        point_id = hashlib.sha256(identifier.encode()).hexdigest()[:32]
        self.client.upsert(self.collection, points=[PointStruct(id=point_id, vector=self.embed(text),
                                                                 payload={"id": identifier, "text": text[:12000]})])

    def search(self, query: str, limit=3):
        points = self.client.query_points(collection_name=self.collection, query=self.embed(query[:12000]),
                                          limit=min(max(limit, 1), 5), with_payload=True).points
        return [{"id": p.payload["id"], "text": p.payload["text"][:3000], "score": p.score}
                for p in points if p.score > 0.05]

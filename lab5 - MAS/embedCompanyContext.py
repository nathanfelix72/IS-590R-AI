"""
Embed company_brief.md into a local Chroma collection for debate RAG.
Re-run after replacing the brief so the index stays in sync.
"""

from pathlib import Path

import chromadb
from chromadb.config import Settings
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()

script_dir = Path(__file__).parent
chroma_path = script_dir / "chroma"
brief_path = script_dir / "company_brief.md"
collection_name = "companyContext"

text = brief_path.read_text(encoding="utf-8")

splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
chunks = splitter.split_text(text)

sentence_transformer_ef = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="sentence-transformers/all-mpnet-base-v2"
)

client = chromadb.PersistentClient(
    path=str(chroma_path),
    settings=Settings(anonymized_telemetry=False),
)

# Replace the collection each run so a new brief is a single re-embed.
existing = {c.name for c in client.list_collections()}
if collection_name in existing:
    client.delete_collection(collection_name)

collection = client.create_collection(
    name=collection_name,
    embedding_function=sentence_transformer_ef,
)

collection.add(
    documents=chunks,
    metadatas=[{"source": brief_path.name, "chunk": i} for i in range(len(chunks))],
    ids=[f"company_chunk_{i}" for i in range(len(chunks))],
)

print(f"Embedded {len(chunks)} chunk(s) from {brief_path.name} into '{collection_name}'.")
print(f"Chroma path: {chroma_path}")

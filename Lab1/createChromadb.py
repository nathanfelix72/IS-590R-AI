from sentence_transformers import SentenceTransformer
import pandas as pd
import chromadb
from chromadb.config import Settings
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from chromadb import PersistentClient
from pathlib import Path
from dotenv import load_dotenv
load_dotenv() 

script_dir = Path(__file__).parent
chroma_path = script_dir / "chroma"
df = pd.read_excel(script_dir / "Jobs for Semantic Search.xlsx")

texts = df["Description"].tolist()
metadatas = df[["Job title", "Company", "Location", "Skills", "URL"]].copy()

metadatas = metadatas.to_dict(orient="records")

sbert_ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

client = chromadb.PersistentClient(
    path=chroma_path,
    settings=Settings(anonymized_telemetry=False)
)

collection = client.create_collection(
    name="jobs",
    embedding_function=sbert_ef
)

collection.add(
    documents=texts,
    metadatas=metadatas,
    ids=[f"job_{i}" for i in range(len(texts))]
)
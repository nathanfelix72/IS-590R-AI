import chromadb
from chromadb.config import Settings
import numpy as np
from pathlib import Path
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from dotenv import load_dotenv
load_dotenv()

script_dir = Path(__file__).parent
chroma_path = script_dir / "chroma"

# Initialize persistent ChromaDB client
client = chromadb.PersistentClient(
    path=chroma_path,
    settings=Settings(anonymized_telemetry=False)
)

sbert_ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")

# Load your collection
collection = client.get_collection(name="jobs", embedding_function=sbert_ef)

# CLI loop
print("🔍 Semantic Job Search CLI")
print("Type your query to find matching jobs. Type 'exit' to quit.\n")

while True:
    query = input("Enter your job-related query: ").strip()
    if query.lower() in ["exit", "quit", "stop"]:
        print("👋 Exiting. Thanks for searching!")
        break

    try:
        query_embedding = sbert_ef([query])[0]
        print(f"\nQuery embedding: {len(query_embedding)} dimensions")
        print("First 10 values:", np.round(query_embedding[:10], 3))

        results = collection.query(query_texts=[query], n_results=5)
        print("\nTop Matches:\n")
        for doc, meta, dist in zip(results["documents"][0], results["metadatas"][0], results['distances'][0]):
            print(f"📄 {doc[:250]}")
            print(f"🏷️  Title: {meta.get('Job title')}")
            print(f"🏢 Company: {meta.get('Company')}")
            print(f"📍 Location: {meta.get('Location')}")
            print(f"🛠️  Skills: {meta.get('Skills')}")
            print(f"🔗 URL: {meta.get('URL')}")
            similarity = 1 - dist
            print(f"🥇 Similarity ranking: {np.round(similarity, 2)}\n")
            print(">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
            print("")
    except Exception as e:
        print(f"⚠️ Error during query: {e}\n")
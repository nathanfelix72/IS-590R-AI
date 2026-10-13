"""
Lab 4 Extension — Script 5: Which findings are actually worth citing?

Scripts 3 and 4 say where a disclosure showed up: on the first validated
pass, or only after the keyword recheck. They do not say which of those
chunks is strong evidence and which is a thin or generic mention. This
script scores each detected chunk, plus the keyword hits both passes
called empty, and ranks the real findings.

The score is a subjective quality judgment. It is not calibrated
confidence and it is not ground truth. A keyword match is only context
for the judge.

Run script3KeywordGap.py and script4HybridRecovery.py first.
"""
import json
from pathlib import Path

from docAnalyzerCommon import PDF_DIR, matched_keywords
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

MODEL_NAME = "qwen2.5:3b"
# None scores every prioritized chunk. Set an int to cap the judge calls.
MAX_CHUNKS_TO_SCORE = None
TOP_N = 5

llm = ChatOllama(model=MODEL_NAME, temperature=0.0)


class DisclosureJudgment(BaseModel):
    """Subjective quality score for one chunk, not a verified extraction."""

    score: int = Field(ge=0, le=100, description="Usefulness as climate-disclosure evidence, from 0 to 100")
    quality: str = Field(description="Exactly one of: strong, partial, weak, none")
    explanation: str = Field(description="One or two sentences justifying the score")
    evidence_quote: str = Field(
        description="A short quote copied verbatim from the chunk, or an empty string",
    )


judge = llm.with_structured_output(DisclosureJudgment)


def locate_report(name: str) -> Path:
    path = PDF_DIR / name
    if not path.exists():
        raise SystemExit(f"{path} not found — run the earlier scripts first.")
    return path


def assess(row, extraction):
    prompt = f"""You are scoring one chunk of a Form 10-K for how useful it is as climate-related disclosure evidence.

Use the same categories as the extractor: climate risks, emissions, targets and goals, governance, financial-statement effects, and mitigation. Judge the chunk and the extracted items together. If the extraction is null or empty, judge the chunk text alone.

score: an integer from 0 to 100.
- 80-100: specific, material disclosure (a figure, a date, a named commitment, oversight, or a concrete action)
- 40-79: clearly related, but generic, partial, or missing the detail that would make it citable
- 1-39: only loosely related, or routine boilerplate
- 0: no climate disclosure

quality: exactly one of strong, partial, weak, none.
explanation: one or two sentences justifying the score.
evidence_quote: a short quote copied verbatim from the chunk that supports the score. If nothing in the chunk supports a disclosure, return an empty string. Never invent or paraphrase the quote.

Do not raise the score just because a keyword appears. A keyword match is incidental.

First-pass keyword hit: {row['keyword_hit']}
Matched keywords: {", ".join(row["matched_keywords"]) or "none"}
Detected / recovered structured extraction: {json.dumps(extraction, ensure_ascii=False)}

Original chunk text:
{row['chunk_text']}
"""
    result = judge.invoke(prompt)
    # The quote is model-proposed and should be validated before displaying.
    quote = result.evidence_quote.strip()
    if quote and quote not in row["chunk_text"]:
        quote = ""  # do not present an unverified quote as source text
    return {
        "score": result.score,
        "explanation": result.explanation,
        "quality": result.quality,
        "evidence_quote": quote,
    }


def run():
    gap_path = locate_report("script3_gap_report.json")
    recovery_path = locate_report("script4_recovery_report.json")
    gap = json.loads(gap_path.read_text(encoding="utf-8"))
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))

    def identity(row):
        return (str(row["file"]), row["chunk_index"])

    recovered = {identity(r): r for r in recovery["recovered"]}
    confirmed_empty = {identity(r) for r in recovery["confirmed_empty"]}
    failed = {identity(r) for r in recovery["failed"]}

    rows = []
    for row in gap["all_rows"]:
        key = identity(row)
        if key in recovered:
            category = "keyword_recovered"
            extraction = recovered[key]["recovered_extraction"]
        elif row["llm_found_something"]:
            category = "first_pass_found"
            extraction = row["extraction"]
        elif key in failed:
            category = "recheck_failed"
            extraction = None
        elif key in confirmed_empty:
            category = "keyword_confirmed_empty"
            extraction = None
        elif row["keyword_hit"]:
            category = "keyword_first_pass_empty_unchecked"
            extraction = None
        else:
            category = "no_disclosure_detected"
            extraction = None

        rows.append({
            "file": str(row["file"]),
            "chunk_index": row["chunk_index"],
            "keyword_hit": row["keyword_hit"],
            "matched_keywords": matched_keywords(row["chunk_text"]),
            "category": category,
            "extraction": extraction,
            "chunk_preview": row["chunk_text"][:300],
            "chunk_text": row["chunk_text"],
            "evaluation": None,
        })

    # Prioritize positive findings for ranking, then sample confirmed keyword noise.
    to_score = [r for r in rows if r["category"] in ("first_pass_found", "keyword_recovered")]
    to_score += [r for r in rows if r["category"] == "keyword_confirmed_empty"]
    if MAX_CHUNKS_TO_SCORE is not None:
        to_score = to_score[:MAX_CHUNKS_TO_SCORE]

    for count, row in enumerate(to_score, 1):
        print(f"Evaluating {count}/{len(to_score)}: {Path(row['file']).name}, chunk {row['chunk_index']}")
        try:
            row["evaluation"] = assess(row, row["extraction"])
        except Exception as exc:
            row["evaluation_error"] = str(exc)

    ranked = sorted(
        [r for r in rows if r["evaluation"] is not None and
         r["category"] in ("first_pass_found", "keyword_recovered")],
        key=lambda r: r["evaluation"]["score"], reverse=True
    )
    ranked_keys = {(r["file"], r["chunk_index"]) for r in ranked}
    unscored_findings = [
        r for r in rows if r["category"] in ("first_pass_found", "keyword_recovered")
        and (r["file"], r["chunk_index"]) not in ranked_keys
    ]

    print("\n=== TOP CLIMATE DISCLOSURE EVIDENCE ===")
    for rank, row in enumerate(ranked[:TOP_N], 1):
        e = row["evaluation"]
        print(f"\n#{rank} | Score {e['score']}/100 | {row['category']}")
        print(f"File: {row['file']} | Chunk {row['chunk_index']} | Keyword: {row['keyword_hit']}")
        print(f"Matched keywords: {', '.join(row['matched_keywords']) or 'none'}")
        print(f"Why: {e['explanation']}")
        print(f"Preview: {row['chunk_preview']}...")
        if e["evidence_quote"]:
            print(f"Evidence: {e['evidence_quote']}")

    print("\n=== KEYWORD MATCHES WITH NO RECOVERED DISCLOSURE ===")
    negatives = [r for r in rows if r["category"] == "keyword_confirmed_empty"]
    for row in negatives[:5]:
        e = row["evaluation"]
        print(f"\n{row['file']} | Chunk {row['chunk_index']}")
        print(f"Matched keywords: {', '.join(row['matched_keywords']) or 'none'}")
        print("Why: " + (e["explanation"] if e else "No disclosure after second pass; not separately evaluated"))
        print(f"Preview: {row['chunk_preview']}...")

    output = recovery_path.parent / "script5_ranked_report.json"
    output.write_text(json.dumps({
        "ranked_findings": ranked,
        "unscored_findings": unscored_findings,
        "all_rows": rows,
        "scored_count": sum(r["evaluation"] is not None for r in rows),
        "ranking_limit": MAX_CHUNKS_TO_SCORE,
        "note": "LLM scores are subjective quality judgments, not calibrated confidence or verified ground truth."
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved full results to {output}")
    if unscored_findings:
        print(f"Warning: {len(unscored_findings)} detected disclosures were not scored. Increase MAX_CHUNKS_TO_SCORE for a complete ranking.")


if __name__ == "__main__":
    run()

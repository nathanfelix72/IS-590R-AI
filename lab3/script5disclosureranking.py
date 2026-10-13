match is incidental.

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

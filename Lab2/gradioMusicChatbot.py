import html
import json
import re
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_ollama import ChatOllama

load_dotenv()

script_dir = Path(__file__).parent
chroma_path = script_dir / "chroma"

# Pitchfork stores compound labels such as "Jazz / Rock". Filters match any
# label that contains the requested genre as one of those parts.
BASE_GENRES = (
    "Electronic",
    "Experimental",
    "Folk/Country",
    "Global",
    "Jazz",
    "Metal",
    "Pop/R&B",
    "Rap",
    "Rock",
)
GENRE_VALUES = (
    "Rock",
    "Rap",
    "Electronic",
    "Pop/R&B",
    "Experimental",
    "Jazz",
    "Folk/Country",
    "Electronic / Rock",
    "Experimental / Rock",
    "Metal",
    "Pop/R&B / Rock",
    "Electronic / Pop/R&B",
    "Global",
    "Metal / Rock",
    "Electronic / Experimental / Rock",
    "Rock / Experimental",
    "Electronic / Experimental",
    "Experimental / Jazz",
    "Jazz / Experimental",
    "Jazz / Rock",
    "Jazz / Pop/R&B",
    "Electronic / Jazz",
    "Global / Rock",
    "Pop/R&B / Rap",
    "Pop/R&B / Folk/Country",
    "Electronic / Rap",
    "Experimental / Metal / Rock",
    "Folk/Country / Rock",
    "Rap / Rock",
    "Experimental / Metal",
    "Pop/R&B / Global",
    "Electronic / Folk/Country",
    "Rap / Jazz",
    "Jazz / Pop/R&B / Global",
    "Global / Pop/R&B",
    "Folk/Country / Pop/R&B",
    "Rock / Pop/R&B",
    "Electronic / Global",
    "Rock / Metal",
    "Rap / Experimental",
    "Rock / Electronic",
    "Rap / Pop/R&B",
    "Metal / Rap",
    "Pop/R&B / Electronic",
    "Experimental / Global",
    "Experimental / Jazz / Pop/R&B / Rock",
    "Electronic / Jazz / Rap",
    "Experimental / Rap",
    "Electronic / Global / Pop/R&B",
    "Rock / Folk/Country",
    "Rap / Electronic / Jazz",
    "Electronic / Global / Rock",
)
GENRE_ALIASES = {
    "hip hop": "Rap",
    "hip-hop": "Rap",
    "hiphop": "Rap",
    "rap": "Rap",
    "r&b": "Pop/R&B",
    "rnb": "Pop/R&B",
    "soul": "Pop/R&B",
    "pop": "Pop/R&B",
    "country": "Folk/Country",
    "folk": "Folk/Country",
    "indie": "Rock",
    "rock": "Rock",
    "metal": "Metal",
    "jazz": "Jazz",
    "electronic": "Electronic",
    "ambient": "Electronic",
    "techno": "Electronic",
    "edm": "Electronic",
    "experimental": "Experimental",
    "global": "Global",
    "world": "Global",
}

llm = ChatOllama(model="llama3", temperature=0.4, num_gpu=0)
filter_llm = ChatOllama(model="llama3", temperature=0, num_gpu=0, format="json")

vectorstore = Chroma(
    collection_name="musicReviews",
    persist_directory=chroma_path,
)

FILTER_PROMPT = """You convert a music question into search filters for a Pitchfork review database.

Canonical genres, use only these names: Electronic, Experimental, Folk/Country, Global, Jazz, Metal, Pop/R&B, Rap, Rock.

Rules:
- genres: canonical genres the user asked for. Leave empty when no genre is requested.
  Map hip hop to Rap, country or folk to Folk/Country, r&b, soul, or pop to Pop/R&B,
  indie or rock to Rock, ambient, techno, or electronic to Electronic.
- genre_match: "all" when every genre should apply together, as in "electronic rock".
  "any" when the genres are alternatives, as in "jazz or rap", or when there is only one.
- artist: only when the user wants albums by that artist. Leave null for "similar to" or "like" an artist.
- album: only when the user names a specific album. Otherwise null.
- min_score and max_score: numbers from 0 to 10, or null.
  "highly rated", "critically acclaimed", or "best" means min_score 8.
- year_min and year_max: album release years, or null.
  "the 1990s" means 1990 and 1999. "from 2015" means year_min 2015. "in 2023" means both 2023.
- standalone_question: one self-contained search question. Use the chat history to resolve follow-ups.

Return only JSON:
{"standalone_question": "", "genres": [], "genre_match": "any", "artist": null, "album": null, "min_score": null, "max_score": null, "year_min": null, "year_max": null}

Chat history:
{chat_history}

User question: {question}
"""

ANSWER_PROMPT = """You are a knowledgeable music recommendation assistant. Answer only from the review excerpts below.
If the excerpts do not support an answer, say so. Do not invent albums, artists, or scores.

When you recommend music, explain why using the reviews. Refer to specific albums.
If an artist, album, or song name contains an explicit word, blur that word with asterisks.

Active metadata filters: {filters}
{filter_note}

Review excerpts:
{context}

Chat history:
{chat_history}

User question: {question}

Return only JSON with this shape:
{"answer": "a short helpful reply", "recommendations": [{"artist": "", "album": "", "reason": "one sentence grounded in the review"}]}

recommendations must only name artist and album pairs that appear in the excerpts.
Include up to 4 recommendations when the user wants suggestions or when specific albums answer the question.
Use an empty recommendations list for greetings or questions that no excerpt can answer.
Copy artist and album names exactly as they appear in the excerpts.
"""

chat_history = []


def fill_prompt(template, **kwargs):
    text = template
    for key, value in kwargs.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def parse_json(text):
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    return json.loads(raw)


def blank_to_none(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none", "n/a"}:
        return None
    return value


def canonical_genre(value):
    text = str(value).strip()
    for genre in BASE_GENRES:
        if text.lower() == genre.lower():
            return genre
    return GENRE_ALIASES.get(text.lower())


def coerce_number(value, low, high, as_int=False):
    value = blank_to_none(value)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < low or number > high:
        return None
    if as_int:
        return int(number)
    return number


def normalize_filters(data, question):
    data = data if isinstance(data, dict) else {}
    genres = []
    for genre in data.get("genres") or []:
        canonical = canonical_genre(genre)
        if canonical and canonical not in genres:
            genres.append(canonical)
    genre_match = str(data.get("genre_match") or "any").lower()
    if genre_match not in {"all", "any"}:
        genre_match = "any"
    year_min = coerce_number(data.get("year_min"), 1950, 2030, as_int=True)
    year_max = coerce_number(data.get("year_max"), 1950, 2030, as_int=True)
    if year_min is not None and year_max is not None and year_min > year_max:
        year_min, year_max = year_max, year_min
    min_score = coerce_number(data.get("min_score"), 0, 10)
    max_score = coerce_number(data.get("max_score"), 0, 10)
    if min_score is not None and max_score is not None and min_score > max_score:
        min_score, max_score = max_score, min_score
    artist = blank_to_none(data.get("artist"))
    album = blank_to_none(data.get("album"))
    standalone = blank_to_none(data.get("standalone_question")) or question
    return {
        "standalone_question": str(standalone).strip(),
        "genres": genres,
        "genre_match": genre_match,
        "artist": str(artist).strip() if artist else None,
        "album": str(album).strip() if album else None,
        "min_score": min_score,
        "max_score": max_score,
        "year_min": year_min,
        "year_max": year_max,
    }


def genre_values_for(label):
    matches = []
    for value in GENRE_VALUES:
        parts = [part.strip() for part in value.split(" / ")]
        if label in parts and value not in matches:
            matches.append(value)
    return matches


def chroma_where(filters, include_artist=True, include_album=True):
    clauses = []
    genre_groups = [genre_values_for(genre) for genre in filters["genres"]]
    genre_groups = [group for group in genre_groups if group]
    if genre_groups:
        if filters["genre_match"] == "all" and len(genre_groups) > 1:
            for group in genre_groups:
                clauses.append({"genre": {"$in": group}})
        else:
            union = []
            for group in genre_groups:
                for value in group:
                    if value not in union:
                        union.append(value)
            clauses.append({"genre": {"$in": union}})
    if include_artist and filters["artist"]:
        clauses.append({"artist": filters["artist"]})
    if include_album and filters["album"]:
        clauses.append({"album": filters["album"]})
    if filters["min_score"] is not None:
        clauses.append({"score": {"$gte": filters["min_score"]}})
    if filters["max_score"] is not None:
        clauses.append({"score": {"$lte": filters["max_score"]}})
    if filters["year_min"] is not None:
        clauses.append({"year": {"$gte": filters["year_min"]}})
    if filters["year_max"] is not None:
        clauses.append({"year": {"$lte": filters["year_max"]}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def describe_filters(filters, include_artist=True, include_album=True):
    parts = []
    if filters["genres"]:
        joiner = " + " if filters["genre_match"] == "all" else " or "
        parts.append(joiner.join(filters["genres"]))
    if include_artist and filters["artist"]:
        parts.append(f"artist: {filters['artist']}")
    if include_album and filters["album"]:
        parts.append(f"album: {filters['album']}")
    if filters["min_score"] is not None and filters["max_score"] is not None:
        parts.append(f"score {filters['min_score']:g}–{filters['max_score']:g}")
    elif filters["min_score"] is not None:
        parts.append(f"score ≥ {filters['min_score']:g}")
    elif filters["max_score"] is not None:
        parts.append(f"score ≤ {filters['max_score']:g}")
    if filters["year_min"] is not None and filters["year_max"] is not None:
        if filters["year_min"] == filters["year_max"]:
            parts.append(str(filters["year_min"]))
        else:
            parts.append(f"{filters['year_min']}–{filters['year_max']}")
    elif filters["year_min"] is not None:
        parts.append(f"from {filters['year_min']}")
    elif filters["year_max"] is not None:
        parts.append(f"through {filters['year_max']}")
    return parts


def history_text(pairs):
    if not pairs:
        return "(none)"
    lines = []
    for question, answer in pairs[-6:]:
        lines.append(f"User: {question}\nAssistant: {answer[:500]}")
    return "\n".join(lines)


def adjust_filters(filters, question):
    """Keep similarity requests from collapsing to an exact artist match."""
    text = f"{question} {filters['standalone_question']}".lower()
    similarity_cues = (
        "similar to",
        "reminds me of",
        "in the vein of",
        "along the lines of",
        "albums like",
        "artists like",
        "sounds like",
        "in the style of",
    )
    if filters["artist"] and any(cue in text for cue in similarity_cues):
        filters["artist"] = None
    if filters["max_score"] is not None and filters["max_score"] >= 10:
        filters["max_score"] = None
    return filters


def extract_filters(question):
    prompt = fill_prompt(
        FILTER_PROMPT,
        chat_history=history_text(chat_history),
        question=question,
    )
    try:
        raw = filter_llm.invoke(prompt).content
        filters = normalize_filters(parse_json(raw), question)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        filters = normalize_filters({}, question)
    return adjust_filters(filters, question)


def _filtered_search(question, filters, include_artist, include_album):
    where = chroma_where(
        filters, include_artist=include_artist, include_album=include_album
    )
    if where is None:
        return [], None
    return vectorstore.similarity_search(question, k=15, filter=where), where


def search_reviews(question, filters):
    notes = []
    include_artist = bool(filters["artist"])
    include_album = bool(filters["album"])
    try:
        docs, where = _filtered_search(question, filters, include_artist, include_album)
    except Exception:
        docs, where = [], None
        notes.append("The metadata filter could not be applied, so this search used review text only.")

    if not docs and where is not None and include_artist:
        include_artist = False
        try:
            docs, where = _filtered_search(question, filters, False, include_album)
        except Exception:
            docs, where = [], None
        if docs:
            notes.append(
                f"No exact artist match for {filters['artist']}, so that filter was dropped."
            )
    if not docs and include_album and (where is not None or filters["artist"]):
        include_album = False
        try:
            docs, where = _filtered_search(question, filters, include_artist, False)
        except Exception:
            docs, where = [], None
        if docs:
            notes.append(
                f"No exact album match for {filters['album']}, so that filter was dropped."
            )
    if not docs:
        docs = vectorstore.similarity_search(question, k=15)
        if where is not None or filters["genres"] or filters["min_score"] is not None or filters["year_min"] or filters["year_max"] or filters["artist"] or filters["album"]:
            if not notes:
                notes.append("No reviews matched those filters, so this search used review text only.")
        where = None

    applied = []
    if where is not None:
        applied = describe_filters(
            filters, include_artist=include_artist, include_album=include_album
        )
    return docs, applied, " ".join(notes)


def select_excerpts(docs, max_albums=8, per_album=2):
    """Keep at most two chunks per album so one review does not crowd out the rest."""
    counts = {}
    albums = set()
    selected = []
    for doc in docs:
        key = (doc.metadata.get("artist") or "", doc.metadata.get("album") or "")
        if counts.get(key, 0) >= per_album:
            continue
        if key not in albums and len(albums) >= max_albums:
            continue
        counts[key] = counts.get(key, 0) + 1
        albums.add(key)
        selected.append(doc)
    return selected


def format_context(docs):
    blocks = []
    for index, doc in enumerate(docs, 1):
        meta = doc.metadata
        score = format_score(meta.get("score"))
        year = format_year(meta.get("year"))
        blocks.append(
            f"[{index}] {meta.get('artist')} — {meta.get('album')} "
            f"({year}, score {score}, {meta.get('genre') or 'unknown genre'})\n"
            f"{doc.page_content}"
        )
    return "\n\n".join(blocks) if blocks else "(no matching reviews)"


def format_score(score):
    try:
        return f"{float(score):.1f}"
    except (TypeError, ValueError):
        return "—"


def format_year(year):
    try:
        return str(int(float(year)))
    except (TypeError, ValueError):
        return "—"


def norm_name(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def grounded_recommendations(raw_recs, docs):
    by_pair = {}
    by_album = {}
    for doc in docs:
        meta = doc.metadata
        pair = (norm_name(meta.get("artist")), norm_name(meta.get("album")))
        by_pair[pair] = meta
        by_album.setdefault(norm_name(meta.get("album")), []).append(meta)
    cleaned = []
    seen = set()
    for rec in raw_recs or []:
        if not isinstance(rec, dict):
            continue
        artist = rec.get("artist") or ""
        album = rec.get("album") or ""
        meta = by_pair.get((norm_name(artist), norm_name(album)))
        if meta is None:
            candidates = by_album.get(norm_name(album), [])
            for candidate in candidates:
                if norm_name(candidate.get("artist")) == norm_name(artist):
                    meta = candidate
                    break
            if meta is None and len(candidates) == 1:
                meta = candidates[0]
        if meta is None:
            continue
        key = (meta.get("artist"), meta.get("album"))
        if key in seen or not key[1]:
            continue
        seen.add(key)
        reason = str(rec.get("reason") or "").strip()
        cleaned.append(
            {
                "artist": meta.get("artist") or "Unknown artist",
                "album": meta.get("album") or "Unknown album",
                "score": meta.get("score"),
                "year": meta.get("year"),
                "genre": meta.get("genre") or "",
                "reason": reason or "Mentioned in the matching Pitchfork reviews.",
            }
        )
        if len(cleaned) >= 4:
            break
    return cleaned


def fallback_recommendations(docs):
    recs = []
    seen = set()
    for doc in docs:
        meta = doc.metadata
        key = (meta.get("artist"), meta.get("album"))
        if key in seen or not key[1]:
            continue
        seen.add(key)
        snippet = " ".join(doc.page_content.split())
        if len(snippet) > 180:
            snippet = snippet[:177].rstrip() + "..."
        recs.append(
            {
                "artist": meta.get("artist") or "Unknown artist",
                "album": meta.get("album") or "Unknown album",
                "score": meta.get("score"),
                "year": meta.get("year"),
                "genre": meta.get("genre") or "",
                "reason": snippet or "Retrieved from the matching Pitchfork reviews.",
            }
        )
        if len(recs) >= 4:
            break
    return recs


def wants_recommendation_cards(question, filters):
    text = question.lower()
    cues = (
        "recommend",
        "suggestion",
        "similar",
        "album for",
        "what should",
        "best",
        "highly rated",
        "critically",
        "in the mood",
    )
    if any(cue in text for cue in cues):
        return True
    return bool(filters["genres"] or filters["min_score"] is not None or filters["year_min"] or filters["year_max"])


def render_filters(applied, note):
    if not applied and not note:
        return "<div style='color:#52525b;padding:4px 0;'>No metadata filters on the latest question.</div>"
    chips = "".join(
        "<span style='display:inline-block;margin:0 6px 6px 0;padding:4px 10px;"
        "border-radius:999px;background:#eef2ff;color:#312e81;font-size:13px;'>"
        f"{html.escape(part)}</span>"
        for part in applied
    )
    note_html = (
        f"<div style='color:#52525b;font-size:13px;margin-top:4px;'>{html.escape(note)}</div>"
        if note
        else ""
    )
    return chips + note_html


def render_cards(recommendations):
    if not recommendations:
        return (
            "<div style='color:#52525b;padding:8px 0;'>"
            "Recommendation cards from the latest reply will show up here."
            "</div>"
        )
    blocks = []
    for rec in recommendations:
        meta_bits = [format_year(rec.get("year"))]
        if rec.get("genre"):
            meta_bits.append(rec["genre"])
        meta_bits.append(f"score {format_score(rec.get('score'))}")
        blocks.append(
            "<div style='border:1px solid #e4e4e7;border-radius:12px;padding:14px 16px;"
            "margin-bottom:10px;background:#ffffff;'>"
            f"<div style='font-size:16px;font-weight:650;color:#18181b;'>{html.escape(rec['album'])}</div>"
            f"<div style='color:#3f3f46;margin-top:2px;'>{html.escape(rec['artist'])}</div>"
            "<div style='margin-top:8px;font-size:13px;color:#52525b;'>"
            f"{html.escape(' · '.join(meta_bits))}</div>"
            f"<div style='margin-top:8px;line-height:1.45;color:#27272a;'>{html.escape(rec['reason'])}</div>"
            "</div>"
        )
    return "".join(blocks)


def answer_question(question, docs, applied, note, filters):
    prompt = fill_prompt(
        ANSWER_PROMPT,
        filters=", ".join(applied) if applied else "(none)",
        filter_note=note or "The excerpts already respect any filters listed above.",
        context=format_context(docs),
        chat_history=history_text(chat_history),
        question=question,
    )
    try:
        data = parse_json(llm.invoke(prompt).content)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        data = {}
    answer = ""
    raw_recs = []
    if isinstance(data, dict):
        answer = str(data.get("answer") or "").strip()
        raw_recs = data.get("recommendations") or []
    recommendations = grounded_recommendations(raw_recs, docs)
    if not recommendations and docs and wants_recommendation_cards(question, filters):
        recommendations = fallback_recommendations(docs)
    if not answer:
        if recommendations:
            names = ", ".join(f"{rec['artist']} — {rec['album']}" for rec in recommendations)
            answer = f"Here are albums from the matching reviews: {names}."
        else:
            answer = "I couldn't find enough support in the reviews to answer that."
    if recommendations:
        lines = []
        for rec in recommendations:
            lines.append(
                f"- **{rec['album']}** — {rec['artist']} "
                f"({format_year(rec['year'])}, score {format_score(rec['score'])}, {rec['genre']})\n"
                f"  {rec['reason']}"
            )
        answer = f"{answer}\n\n" + "\n".join(lines)
    if applied:
        answer = f"**Filters:** {', '.join(applied)}\n\n{answer}"
    if note:
        answer = f"{answer}\n\n_{note}_"
    return answer, recommendations


def get_response(message, history):
    history = history or []
    if not message or not str(message).strip():
        return history, render_filters([], ""), render_cards([])

    filters = extract_filters(message)
    search_question = filters["standalone_question"] or message
    docs, applied, note = search_reviews(search_question, filters)
    excerpts = select_excerpts(docs)
    answer, recommendations = answer_question(message, excerpts, applied, note, filters)

    chat_history.append((message, answer))
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": answer})
    return history, render_filters(applied, note), render_cards(recommendations)


def clear_history():
    global chat_history
    chat_history = []
    return None, render_filters([], ""), render_cards([])


with gr.Blocks(title="Music Recommendation Chatbot") as demo:
    gr.Markdown(
        """
    # 🎵 Music Recommendation Chatbot
    Ask for albums by genre, year, or Pitchfork score. Recommendations show up as cards
    grounded in the retrieved reviews.
    """
    )

    with gr.Row():
        with gr.Column(scale=4):
            chatbot = gr.Chatbot(height=500, avatar_images=(None, "🎧"), label="Chat")
            with gr.Row():
                msg = gr.Textbox(
                    placeholder="Ask about an album, artist, or request recommendations...",
                    show_label=False,
                    scale=4,
                    container=False,
                )
                submit_btn = gr.Button("Send 🎤", scale=1, variant="primary")
            with gr.Row():
                clear_btn = gr.Button("Clear Chat 🗑️")

        with gr.Column(scale=2):
            gr.Markdown("### Filters on this question")
            filter_panel = gr.HTML(render_filters([], ""))
            gr.Markdown("### Recommendations")
            card_panel = gr.HTML(render_cards([]))
            gr.Markdown(
                """
            ### 💡 Try asking:
            - "Recommend jazz albums from the 1990s scored 8 or higher"
            - "Highly rated electronic albums from 2020"
            - "What are some good metal albums?"
            - "Tell me about albums with great guitar work"
            """
            )

    msg.submit(
        get_response,
        inputs=[msg, chatbot],
        outputs=[chatbot, filter_panel, card_panel],
    ).then(lambda: "", outputs=msg)

    submit_btn.click(
        get_response,
        inputs=[msg, chatbot],
        outputs=[chatbot, filter_panel, card_panel],
    ).then(lambda: "", outputs=msg)

    clear_btn.click(clear_history, outputs=[chatbot, filter_panel, card_panel])

    gr.Examples(
        examples=[
            "Recommend jazz albums from the 1990s scored 8 or higher",
            "What are some highly rated electronic albums from 2020?",
            "Suggest metal albums with a score above 8",
            "Tell me about albums with great guitar work",
            "I'm in the mood for melancholic indie folk",
        ],
        inputs=msg,
        label="Example Questions",
    )

if __name__ == "__main__":
    demo.launch(
        theme=gr.themes.Soft(),
        share=False,
        server_name="127.0.0.1",
        server_port=7860,
    )

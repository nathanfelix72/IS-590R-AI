"""
Lab: Debate Pattern -- Build vs. Buy (with live web search + company RAG)
IS 590R -- AI and Agentic Systems
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load before LangChain / LangSmith imports. LangSmith caches LANGSMITH_* on
# first read, so dotenv must win before any tracer checks the environment.
script_dir = Path(__file__).resolve().parent
load_dotenv(script_dir / ".env")

from typing_extensions import TypedDict
from langgraph.graph import END, START, StateGraph
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_tavily import TavilySearch
from langsmith import traceable

chroma_path = script_dir / "chroma"

model = init_chat_model("ollama:qwen2.5:7b")
web_search = TavilySearch(max_results=5, topic="general")

embeddings = HuggingFaceEmbeddings(
    model_name="sentence-transformers/all-mpnet-base-v2"
)
vectorstore = Chroma(
    collection_name="companyContext",
    persist_directory=str(chroma_path),
    embedding_function=embeddings,
)
retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

CASE = """
Decision: build a custom CRM in-house, or buy/license a SaaS CRM.
BUILD: ~$400K/year for 3 years (two engineers + infrastructure); exact fit
       to our sales process.
BUY:   ~$150K/year for a SaaS CRM; covers ~80% of requirements out of the
       box; the remaining 20% needs manual workaround processes.
"""

COMPANY_CONTEXT_INSTRUCTION = (
    "When arguing about process fit, integrations, IT constraints, budget, "
    "or internal costs, prefer facts from the retrieved company context over "
    "guesses. Cite those company facts explicitly when you use them."
)

build_advocate = create_agent(
    model,
    tools=[web_search],
    system_prompt=(
        "You are an advocate arguing FOR building a custom CRM in-house. "
        f"{COMPANY_CONTEXT_INSTRUCTION} "
        "Use the web_search tool when it would strengthen your case with "
        "current pricing, vendor comparisons, or case studies -- don't "
        "just guess at numbers. Make the strongest honest case you can in "
        "3-4 sentences, citing what you found."
    ),
)

buy_advocate = create_agent(
    model,
    tools=[web_search],
    system_prompt=(
        "You are an advocate arguing FOR buying a SaaS CRM instead of "
        "building in-house. "
        f"{COMPANY_CONTEXT_INSTRUCTION} "
        "Use the web_search tool when it would strengthen your case with "
        "current SaaS CRM pricing or feature "
        "comparisons -- don't just guess at numbers. Make the strongest "
        "honest case you can in 3-4 sentences, directly rebutting the "
        "BUILD side's latest argument, citing what you found."
    ),
)


def _extract_urls(raw_content) -> list[str]:
    """
    Best-effort extraction of result URLs from a Tavily tool result.
    Tavily's output shape can be a JSON string or an already-parsed
    dict/list depending on version -- deliberately defensive.
    """
    data = raw_content
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return []

    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        return []

    return [r.get("url") for r in results if isinstance(r, dict) and r.get("url")]


def _describe_tool_usage(label: str, messages) -> None:
    """
    Prints every tool call's name + arguments (the actual search query
    sent) and, when parseable, every URL the tool returned. This is the
    only reliable way to confirm a tool fired -- the agent's final
    argument text alone can look authoritative with zero search behind
    it.
    """
    saw_any_call = False
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            saw_any_call = True
            print(f"[{label}] called {call.get('name')} with args: {call.get('args')}")

        if getattr(message, "type", None) == "tool":
            urls = _extract_urls(message.content)
            if urls:
                print(f"[{label}] tool returned {len(urls)} result(s):")
                for url in urls:
                    print(f"    - {url}")
            else:
                preview = str(message.content)[:200]
                print(f"[{label}] tool returned (no URLs parsed out; raw preview): {preview}")

    if not saw_any_call:
        print(f"[{label}] made NO tool calls -- this argument is from the model's own knowledge only.")


@traceable(name="retrieve_company_context", run_type="retriever")
def _retrieve_company_context(query: str, label: str) -> str:
    """Always retrieve top-k company chunks and format them for prompt injection."""
    docs = retriever.invoke(query)
    if not docs:
        print(f"[{label}] RAG retrieved 0 company chunks.")
        return "(No company context retrieved — run embedCompanyContext.py first.)"

    print(f"[{label}] RAG retrieved {len(docs)} company chunk(s):")
    for i, doc in enumerate(docs, 1):
        preview = " ".join(doc.page_content.split())[:120]
        print(f"    [{i}] {preview}...")

    blocks = []
    for i, doc in enumerate(docs, 1):
        blocks.append(f"[{i}] {doc.page_content.strip()}")
    return "\n\n".join(blocks)


def _with_company_context(label: str, query: str, body: str) -> str:
    context = _retrieve_company_context(query, label)
    return (
        f"Company context (retrieved):\n{context}\n\n"
        f"{body}"
    )


class DebateState(TypedDict):
    case: str
    position_a: str
    position_b: str
    round: int
    max_rounds: int
    resolution: str


def agent_position_a(state: DebateState) -> dict:
    if state["round"] == 0:
        body = f"Case:\n{state['case']}\n\nMake your opening argument."
        query = state["case"]
    else:
        body = (
            f"Case:\n{state['case']}\n\n"
            f"The BUY side just argued:\n{state['position_b']}\n\n"
            "Rebut their argument and strengthen your case."
        )
        query = f"{state['case']}\n{state['position_b']}"

    user_content = _with_company_context("build_advocate", query, body)
    result = build_advocate.invoke(
        {"messages": [{"role": "user", "content": user_content}]}
    )
    _describe_tool_usage("build_advocate", result["messages"])
    return {"position_a": result["messages"][-1].text, "round": state["round"] + 1}


def agent_position_b(state: DebateState) -> dict:
    body = (
        f"Case:\n{state['case']}\n\n"
        f"The BUILD side just argued:\n{state['position_a']}\n\n"
        "Rebut their argument and strengthen your case."
    )
    query = f"{state['case']}\n{state['position_a']}"
    user_content = _with_company_context("buy_advocate", query, body)

    result = buy_advocate.invoke(
        {"messages": [{"role": "user", "content": user_content}]}
    )
    _describe_tool_usage("buy_advocate", result["messages"])
    return {"position_b": result["messages"][-1].text}


def moderator(state: DebateState) -> dict:
    body = (
        f"Case:\n{state['case']}\n\n"
        f"BUILD side's final argument:\n{state['position_a']}\n\n"
        f"BUY side's final argument:\n{state['position_b']}"
    )
    query = (
        f"{state['case']}\n{state['position_a']}\n{state['position_b']}"
    )
    user_content = _with_company_context("moderator", query, body)

    response = model.invoke([
        {"role": "system", "content": (
            "You are a neutral moderator. Read both sides' final "
            "arguments and recommend BUILD or BUY. Prefer retrieved "
            "company facts when weighing process fit, integrations, IT "
            "constraints, and budget. Name the single most important "
            "trade-off explicitly, and state the recommendation in the "
            "first sentence."
        )},
        {"role": "user", "content": user_content},
    ])
    return {"resolution": response.text}


def continue_debate(state: DebateState) -> str:
    if state["round"] < state["max_rounds"]:
        return "agent_position_a"
    return "moderator"


graph = StateGraph(DebateState)
graph.add_node("agent_position_a", agent_position_a)
graph.add_node("agent_position_b", agent_position_b)
graph.add_node("moderator", moderator)
graph.add_edge(START, "agent_position_a")
graph.add_edge("agent_position_a", "agent_position_b")
graph.add_conditional_edges(
    "agent_position_b", continue_debate, ["agent_position_a", "moderator"]
)
graph.add_edge("moderator", END)
debate_graph = graph.compile()
debate_graph.get_graph().draw_mermaid_png(output_file_path="debate_workflow.png")


@traceable(name="run_debate")
def run_debate(max_rounds: int = 2) -> dict:
    return debate_graph.invoke({
        "case": CASE,
        "position_a": "",
        "position_b": "",
        "round": 0,
        "max_rounds": max_rounds,
        "resolution": "",
    })


if __name__ == "__main__":
    tracing = os.getenv("LANGSMITH_TRACING", "").lower() in {"1", "true", "yes"}
    project = os.getenv("LANGSMITH_PROJECT", "(default)")
    print(f"LangSmith tracing={'on' if tracing else 'OFF'} project={project}")

    result = run_debate(max_rounds=2)

    print("\n--- Final BUILD argument ---")
    print(result["position_a"])
    print("\n--- Final BUY argument ---")
    print(result["position_b"])
    print("\n--- Moderator's resolution ---")
    print(result["resolution"])

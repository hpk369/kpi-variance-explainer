"""KPI Variance Explainer: the public Space (Gradio).

The page draws the intro, the 4 suggested questions, the answer and the "Show your work" panel. Everything else
(input checks, rate limits, the live agent call and the cached fallback) is in core.py.
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import gradio as gr

import core

NOTICE = "_Northlake Bank is fictional. Every client, deal and figure in this demo is synthetic._"
INTRO = (
    "Ask why a KPI moved. The agent answers only through six governed Databricks SQL functions, explains terms from "
    "the bank's glossary, and checks every figure against the tool results before you see it. A wrong figure is sent "
    "back to be rewritten; after two tries the commentary is withheld."
)
CSS = """
.answer {min-height:120px}
"""
WORKING = ("_Working: calling the governed tools, retrieving definitions and checking the numbers "
           "(usually 10–40 seconds)…_")


def ask(question: str, used: int):
    """Generator: shows a working message first, then the answer."""
    if not (question or "").strip():
        yield core.MSG["empty"], "", used, core.remaining_text(used)
        return
    yield WORKING, "", used, core.remaining_text(used)
    r = core.respond(question, used)
    print(f"[ask] mode={r.mode} used={r.used} chars={len(question)} {r.detail}", flush=True)
    yield r.answer, r.work, r.used, core.remaining_text(r.used)


with gr.Blocks(title="KPI Variance Explainer") as demo:
    gr.Markdown("# KPI Variance Explainer")
    gr.Markdown(INTRO + "\n\n" + NOTICE)
    used = gr.State(0)
    with gr.Row():
        buttons = [gr.Button(q, size="sm") for q in core.SUGGESTED]
    box = gr.Textbox(label="Your question", placeholder="e.g. Why did Industrials revenue miss plan in Q3?",
                     max_length=core.MAX_CHARS, lines=1, max_lines=3, submit_btn="Ask")
    left = gr.Markdown(core.remaining_text(0))
    answer = gr.Markdown(elem_classes="answer")
    with gr.Accordion("Show your work", open=False):
        work = gr.Markdown()

    outputs = [answer, work, used, left]
    # api_visibility="private": no public API, so the per-visit limit can't be skipped by calling the endpoint directly
    run = dict(concurrency_limit=2, concurrency_id="agent", api_visibility="private")
    box.submit(ask, [box, used], outputs, **run)
    for b, q in zip(buttons, core.SUGGESTED):
        b.click(lambda q=q: q, None, box, queue=False, api_visibility="private").then(ask, [box, used], outputs, **run)

    gr.Markdown("Code and evaluation scorecard: "
                "[github.com/hpk369/kpi-variance-explainer](https://github.com/hpk369/kpi-variance-explainer)")

if __name__ == "__main__":
    demo.queue(max_size=20).launch(ssr_mode=False, css=CSS)

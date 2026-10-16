"""Curated example workflows shown on the Automation index page.

Each entry is a ready-made graph the user can instantiate with one click
and then adjust in the editor. Node positions (x/y) lay the chain out
left-to-right.
"""

EXAMPLES = [
    {
        "key": "translate-file",
        "name": "Translate a file",
        "icon": "fa-language",
        "description": "Right-click any file in a drive → Run workflow. "
                       "The file is translated and saved next to your "
                       "documents.",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "input.trigger", "x": 60, "y": 120,
                 "config": {}},
                {"id": "n2", "type": "ai.prompt", "x": 340, "y": 120,
                 "config": {
                     "prompt": "Translate the input into English. Keep the "
                               "original structure and formatting.",
                     "system": "You are a professional translator. Answer "
                               "only with the translated text, no "
                               "commentary."}},
                {"id": "n3", "type": "output.save_file", "x": 640, "y": 120,
                 "config": {"name": "translated.txt", "drive_id": "",
                            "folder": ""}},
            ],
            "edges": [
                {"from": "n1", "from_port": "out",
                 "to": "n2", "to_port": "in"},
                {"from": "n2", "from_port": "out",
                 "to": "n3", "to_port": "in"},
            ],
        },
    },
    {
        "key": "summarize-to-notebook",
        "name": "Summarize a file into a notebook",
        "icon": "fa-book-medical",
        "description": "Triggered from a file: an AI summary is appended "
                       "to a notebook you pick (or a new one).",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "input.trigger", "x": 60, "y": 120,
                 "config": {}},
                {"id": "n2", "type": "ai.prompt", "x": 340, "y": 120,
                 "config": {
                     "prompt": "Summarize the input in clear bullet "
                               "points, with a one-line takeaway at the "
                               "end.",
                     "system": ""}},
                {"id": "n3", "type": "notebook.output", "x": 640, "y": 120,
                 "config": {"notebook_id": "", "mode": "append",
                            "name": "Summaries", "drive_id": ""}},
            ],
            "edges": [
                {"from": "n1", "from_port": "out",
                 "to": "n2", "to_port": "in"},
                {"from": "n2", "from_port": "out",
                 "to": "n3", "to_port": "in"},
            ],
        },
    },
    {
        "key": "research-digest",
        "name": "Research digest (scheduled)",
        "icon": "fa-newspaper",
        "description": "Searches your drives for a topic, reads the "
                       "matches and writes a digest notebook. Enable the "
                       "schedule in the editor to run it periodically.",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "docindex.search_files",
                 "x": 60, "y": 120,
                 "config": {"query": "report", "limit": 10}},
                {"id": "n2", "type": "transform.read_files",
                 "x": 340, "y": 120, "config": {"max_chars": 6000}},
                {"id": "n3", "type": "ai.prompt", "x": 620, "y": 120,
                 "config": {
                     "prompt": "These files matched the topic. Write a "
                               "digest: what each file is about and the "
                               "key points across all of them.",
                     "system": "You are an analyst writing concise, "
                               "well-structured digests in markdown."}},
                {"id": "n4", "type": "notebook.output", "x": 900, "y": 120,
                 "config": {"notebook_id": "", "mode": "append",
                            "name": "Digest", "drive_id": ""}},
            ],
            "edges": [
                {"from": "n1", "from_port": "out",
                 "to": "n2", "to_port": "in"},
                {"from": "n2", "from_port": "out",
                 "to": "n3", "to_port": "in"},
                {"from": "n3", "from_port": "out",
                 "to": "n4", "to_port": "in"},
            ],
        },
    },
    {
        "key": "filter-spreadsheet",
        "name": "Filter a spreadsheet (no AI)",
        "icon": "fa-filter",
        "description": "Reads an Excel/CSV file, keeps the rows that "
                       "match a condition and saves the result as CSV.",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "input.file", "x": 60, "y": 120,
                 "config": {"file_id": ""}},
                {"id": "n2", "type": "transform.table_filter",
                 "x": 340, "y": 120,
                 "config": {"conditions": [
                     {"column": "", "op": "contains", "value": ""}]}},
                {"id": "n3", "type": "output.save_file", "x": 640, "y": 120,
                 "config": {"name": "filtered.csv", "drive_id": "",
                            "folder": ""}},
            ],
            "edges": [
                {"from": "n1", "from_port": "out",
                 "to": "n2", "to_port": "in"},
                {"from": "n2", "from_port": "out",
                 "to": "n3", "to_port": "in"},
            ],
        },
    },
    {
        "key": "caption-images",
        "name": "Caption an image with AI",
        "icon": "fa-image",
        "description": "Triggered from an image file: the AI describes it "
                       "in detail and the caption is saved as a text file "
                       "next to your documents.",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "input.trigger", "x": 60, "y": 120,
                 "config": {}},
                {"id": "n2", "type": "ai.caption_image", "x": 340, "y": 120,
                 "config": {
                     "file_id": "",
                     "instruction": "Describe this image in detail: objects, "
                                    "people, scenes, colors and any visible "
                                    "text."}},
                {"id": "n3", "type": "output.save_file", "x": 640, "y": 120,
                 "config": {"name": "caption.txt", "drive_id": "",
                            "folder": ""}},
            ],
            "edges": [
                {"from": "n1", "from_port": "out",
                 "to": "n2", "to_port": "in"},
                {"from": "n2", "from_port": "out",
                 "to": "n3", "to_port": "in"},
            ],
        },
    },
    {
        "key": "ask-and-report",
        "name": "Ask a question, get a report",
        "icon": "fa-circle-question",
        "description": "Uses run inputs: every run asks you a question "
                       "about a spreadsheet, the AI answers from the data "
                       "and the report is saved as a file. Great for "
                       "recurring analysis.",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "input.param", "x": 60, "y": 40,
                 "config": {
                     "param": "question",
                     "label": "What do you want to know about the data?",
                     "kind": "longtext", "ask": "yes", "default": ""}},
                {"id": "n2", "type": "input.file", "x": 60, "y": 260,
                 "config": {"file_id": ""}},
                {"id": "n3", "type": "ai.prompt", "x": 400, "y": 140,
                 "config": {
                     "prompt": "The user asked: {{ n1 }}\n\nHere is the "
                               "data as a table:\n{{ n2 }}\n\nAnswer the "
                               "question from the data, with a short "
                               "explanation of how you got there.",
                     "system": "You are a data analyst. Ground every claim "
                               "in the provided table."}},
                {"id": "n4", "type": "output.save_file", "x": 700, "y": 140,
                 "config": {"name": "report.md", "drive_id": "",
                            "folder": ""}},
            ],
            "edges": [
                # n1/n2 plug into the prompt through the {{ n1 }}/{{ n2 }}
                # template refs — those references already order the run,
                # so no wires are needed for them.
                {"from": "n3", "from_port": "out",
                 "to": "n4", "to_port": "in"},
            ],
        },
    },
    {
        "key": "json-to-spreadsheet",
        "name": "JSON to spreadsheet (no AI)",
        "icon": "fa-table",
        "description": "Reads a JSON file, turns it into a table and saves "
                       "it as CSV — the simplest way to handle JSON data.",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "input.file", "x": 60, "y": 120,
                 "config": {"file_id": ""}},
                {"id": "n2", "type": "transform.json_to_table",
                 "x": 340, "y": 120, "config": {}},
                {"id": "n3", "type": "output.save_file", "x": 640, "y": 120,
                 "config": {"name": "data.csv", "drive_id": "",
                            "folder": ""}},
            ],
            "edges": [
                {"from": "n1", "from_port": "out",
                 "to": "n2", "to_port": "in"},
                {"from": "n2", "from_port": "out",
                 "to": "n3", "to_port": "in"},
            ],
        },
    },
    {
        "key": "branch-on-value",
        "name": "Branch on a value (condition)",
        "icon": "fa-code-fork",
        "description": "Asks a question at run time and saves to a different "
                       "file depending on the answer — the simplest "
                       "condition + merge example.",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "input.param", "x": 60, "y": 120,
                 "config": {"param": "topic", "label": "Which topic?",
                            "kind": "text", "ask": "yes", "default": ""}},
                {"id": "n2", "type": "logic.condition", "x": 340, "y": 120,
                 "config": {"left": "{{ n1 }}", "op": "contains",
                            "right": "urgent"}},
                {"id": "n3", "type": "transform.template", "x": 620, "y": 40,
                 "config": {"template": "URGENT: {{ n1 }}"}},
                {"id": "n4", "type": "transform.template", "x": 620, "y": 220,
                 "config": {"template": "Later: {{ n1 }}"}},
                {"id": "n5", "type": "logic.merge", "x": 880, "y": 120,
                 "config": {}},
                {"id": "n6", "type": "output.save_file", "x": 1140, "y": 120,
                 "config": {"name": "result.txt", "drive_id": "",
                            "folder": ""}},
            ],
            "edges": [
                {"from": "n2", "from_port": "true",
                 "to": "n3", "to_port": "in"},
                {"from": "n2", "from_port": "false",
                 "to": "n4", "to_port": "in"},
                {"from": "n3", "from_port": "out",
                 "to": "n5", "to_port": "in1"},
                {"from": "n4", "from_port": "out",
                 "to": "n5", "to_port": "in2"},
                {"from": "n5", "from_port": "out",
                 "to": "n6", "to_port": "in"},
            ],
        },
    },
    {
        "key": "invoice-to-table",
        "name": "Invoice PDF to spreadsheet (AI)",
        "icon": "fa-file-invoice",
        "description": "Reads a file, extracts invoice fields as JSON with "
                       "the AI, turns them into a table and saves CSV.",
        "graph": {
            "nodes": [
                {"id": "n1", "type": "input.file", "x": 60, "y": 120,
                 "config": {"file_id": ""}},
                {"id": "n2", "type": "ai.extract_json", "x": 340, "y": 120,
                 "config": {
                     "prompt": "Extract the invoice number, date and total.",
                     "schema": '{"number": "...", "date": "...", "total": 0}',
                     "system": ""}},
                {"id": "n3", "type": "transform.json_to_table",
                 "x": 620, "y": 120, "config": {}},
                {"id": "n4", "type": "output.save_table", "x": 900, "y": 120,
                 "config": {"name": "invoices.csv", "format": "csv",
                            "drive_id": "", "folder": ""}},
            ],
            "edges": [
                {"from": "n1", "from_port": "out",
                 "to": "n2", "to_port": "in"},
                {"from": "n2", "from_port": "out",
                 "to": "n3", "to_port": "in"},
                {"from": "n3", "from_port": "out",
                 "to": "n4", "to_port": "in"},
            ],
        },
    },
]


def by_key(key):
    return next((e for e in EXAMPLES if e["key"] == key), None)

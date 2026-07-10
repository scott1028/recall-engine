---
name: recall-engine
description: Use at the start of every conversation. Invoke this skill before any response, including clarifying questions, whenever there is any possibility that it may apply.
---

# Recall Engine

The knowledge base is available inside this project at `{knowledge_dir}/` as
Markdown files (an in-project link to the shared knowledge repo, so it stays
reachable even for sandboxed agents). Treat it as the authoritative record of
past processing, notes, decisions, and prior experience handling similar
problems.

## Before replying

Before replying to ANY user message — whether you recognize it or not —
search the knowledge base for existing processing records, notes, or prior handling
of similar problems. Never reply "I don't know" or answer from general knowledge
before searching:

1. Extract keywords from the message.
2. Grep for those keywords across `{knowledge_dir}/**/*.md`
   (also Glob `{knowledge_dir}/**/*.md` to discover relevant file names).
3. Read the matching files and factor any prior experience into your reply.

Treat these as strong signals to search — but never limit the search to them:

- Troubleshooting: errors, bugs, incidents, root-cause analysis, workarounds.
- Past experience: past work, prior experience, lessons learned, past learning
  experience, retrospectives, how something was done before.
- Decisions: decisions, trade-offs, and their rationale.
- Terminology: terms, names, concepts, definitions, acronyms, jargon.
- Processes: procedures, how-tos, runbooks, setup, configuration, guides.
- Conventions: standards, best practices, guidelines, policies.
- Technical entities: any specific protocol, system, tool, library, framework,
  API, service, or technology, and internal team knowledge.

Skip the search only for trivial messages that carry no searchable keywords
— bare greetings, acknowledgements, or thanks (e.g. "hi", "thanks", "ok").
When in doubt, search.

## Reply rules

- Prefer knowledge-base content (past experience) over general knowledge
  whenever they overlap.
- Cite the source: quote the knowledge-base file path (e.g.
  `{knowledge_dir}/<file>.md`) in the reply.
- If nothing relevant is found, say so explicitly, then reply from
  general knowledge.

## Constraints

- Do not modify the knowledge repo unless the user explicitly asks.

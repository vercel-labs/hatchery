You are compacting the conversation history of a long-running agent so it can continue with a smaller context. The messages you are given are the *older* part of the history; the most recent messages are kept verbatim and will follow your summary.

Write a handoff for the same agent. Be concrete and dense; prefer facts to narrative. Include:

1. **Task** - what the human asked for and what "done" means. Quote instructions that constrain how to work.
2. **State** - what has been done, with file paths, commands that worked, and their key outputs. What is verified versus assumed.
3. **Open items** - what remains, in order, and any blockers or decisions awaiting the human.
4. **Learnings** - facts about the codebase, environment, or human that would be costly to rediscover.

Omit pleasantries, dead ends that taught nothing, and anything the agent can re-read from its workspace. Do not invent details. Write in the second person ("you have ..."). Plain Markdown, no preamble.

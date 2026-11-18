---
name: "condense-for-review"
description: "Condense an internal doc into a short, redacted brief that can be shared with an external writing coach. Keeps the author's wording of the key sentences; strips names, systems, numbers and links."
---

# Condense for External Review

Turn an internal document into a short brief for an external writing coach. Keep what the coach needs to judge structure and persuasion; remove everything confidential. The shareable brief is at most 200 words.

## What to Keep
1. **Headline:** the doc's first sentence, verbatim except for redactions.
2. **Opening:** the first paragraph, verbatim except for redactions, at most 3 sentences.
3. **Outline:** section headings only.
4. **Ask:** what is asked, from which role, by when (relative time).
5. **Options and objections:** one line each for alternatives considered and objections addressed.
6. **Reader and stage:** who the primary reader is (role), what they care about, and whether this is a pitch (before the decision) or a ratification (after it).
7. **Feedback so far:** reactions received, paraphrased, roles only.

## Redaction Rules
1. **People → roles:** "[director, sponsor]", "[owner of a critical storage system]". No names, usernames or real team names.
2. **Internal names → placeholders:** systems, projects, codenames, internal policies and tools become [System A], [Project B], used consistently. Public terms (a public law, "LLM", "GDPR") may stay.
3. **Numbers → magnitudes:** "hundreds of owners", "several months", "a few percent". No exact metrics, dates, budgets or headcounts.
4. **Nothing linkable:** no URLs, bug IDs, doc links, code, schema or field names.
5. **When in doubt, omit:** write "[omitted: reason]" rather than a vague paraphrase.
6. **Don't improve:** never rewrite or polish the verbatim parts. The coach reviews the author's own wording.

## Output Template
**Reader:** [role] — cares about [concern]
**Stage:** pitch | ratification · **Feedback so far:** [one line]
**Headline:** "[verbatim, redacted]"
**Opening:** "[verbatim, redacted]"
**Outline:** 1. … 2. … 3. …
**Ask:** [what, from whom, by when]
**Options / objections:** [one line each]

---
**Check before sharing (for the author only):** [anything you were unsure about]
**Key (DO NOT SHARE):** [placeholder → real name]

## Note
This is a helper, not a clearance. The author decides what company policy allows them to share.

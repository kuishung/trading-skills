"""Journal layer -- structured logs of every interesting moment.

Two flows:

  1. Shortlist + conviction journaling: scanner outputs + the analysis
     text explaining why a candidate is or is not promising.
  2. Setup-execution journaling: full plan, entry submission, fills,
     exits, R-multiple, post-mortem in highly informative form.

Output:
  state/journal_<date>.jsonl    one JSON record per line
  state/events_<date>.jsonl     legacy event stream for the dashboard

Goal: someone reading the journal weeks later should be able to
reconstruct exactly what was seen, what was decided, why, and what
happened next.
"""

Expected-diff declarations for refactor-backlog items whose gate is
`expected-diff`.  One JSON file per item id, written BEFORE implementing it:

    {
      "reason": "why this output is meant to change",
      "paths": ["docs/**/*.html"],
      "line_patterns": ["creatures/pictures\\.html"]
    }

`paths` is a glob allowlist of files permitted to differ; `line_patterns`
(optional) are regexes every added/removed line must match.  Used as
`bin/wiki-golden check --expect gates/<item-id>.json`.

Never widen a declaration after seeing the diff — see CLAUDE-refactor-autopilot.md.

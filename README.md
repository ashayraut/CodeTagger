# Java Code Tagger

A utility that analyzes Java code and generates risk-based metadata tags per method. Tags drive PR routing, test-suite selection, and reviewer assignment — enabling AI-native development pipelines to make smarter, faster decisions about what needs human attention and what can ship autonomously.

## What it tags

Each non-trivial Java method gets four tags:

| Dimension | Values | Drives |
|---|---|---|
| `riskLevel` | LOW / MEDIUM / HIGH / CRITICAL | PR approval gate, deployment confidence |
| `attentionTypes` | SECURITY, PERFORMANCE, STABILITY, CORRECTNESS, RELIABILITY | Reviewer assignment, static analysis focus |
| `reviewRequirement` | AUTO_MERGE / HUMAN_REVIEW / SECURITY_REVIEW / ARCH_REVIEW | PR routing |
| `testingDepth` | UNIT_ONLY / INTEGRATION / E2E / LOAD | Test suite selection |

The file-level summary rolls up to the strictest values found across all methods in a file, and the top-level summary rolls up across all analyzed files.

## Setup

The script runs through the `claude` CLI — no API key needed. Authentication is handled natively by Claude Code.

```bash
# Install Claude Code if you haven't already, then authenticate once:
# https://claude.ai/code
claude --version   # verify it's available
```

## Usage

```bash
# Analyze a single file
python3 code-tagger.py --file src/main/java/com/example/AuthService.java --pretty

# Analyze all Java files in a directory
python3 code-tagger.py --dir src/main/java/com/example --pretty

# Analyze only files changed in the current git diff vs HEAD
python3 code-tagger.py --diff --pretty

# Analyze files changed since branching from main
python3 code-tagger.py --diff main --pretty
```

Output is JSON to stdout. Use `--pretty` for human reading or omit it for CI piping.

## Output format

```json
{
  "summary": {
    "overallRiskLevel": "HIGH",
    "primaryAttentionTypes": ["SECURITY", "CORRECTNESS"],
    "recommendedReviewRequirement": "SECURITY_REVIEW",
    "recommendedTestingDepth": "INTEGRATION",
    "filesAnalyzed": 3,
    "filesWithErrors": 0
  },
  "files": [
    {
      "file": "src/main/java/com/example/AuthService.java",
      "fileSummary": { ... },
      "methods": [
        {
          "name": "validateToken",
          "className": "AuthService",
          "signature": "boolean validateToken(String token)",
          "riskLevel": "CRITICAL",
          "attentionTypes": ["SECURITY"],
          "reviewRequirement": "SECURITY_REVIEW",
          "testingDepth": "INTEGRATION",
          "rationale": "Handles JWT verification; a bug here bypasses all authentication."
        }
      ]
    }
  ]
}
```

## Claude Code interactive use

If you have Claude Code, you can run `/tag-code` as a slash command. The `tag-code` skill is in `.claude/skills/tag-code.md`.

```
/tag-code                        # tags current diff vs HEAD
/tag-code --file path/to/Foo.java
/tag-code --dir src/main/java
/tag-code --diff origin/main
```

Claude will run the script and present a formatted summary with method-level tables and highlighted CRITICAL/HIGH items.

## CI integration (recommended pattern)

The intended use is **generated-on-demand at PR time**, not as checked-in metadata. Run the tagger on the PR diff, consume the JSON in your pipeline, and discard the report — no stale state to maintain.

### GitHub Actions example

```yaml
- name: Tag changed Java code
  run: |
    python3 code-tagger.py --diff origin/main --pretty > tag-report.json

- name: Enforce review gate
  run: |
    REVIEW=$(jq -r '.summary.recommendedReviewRequirement' tag-report.json)
    RISK=$(jq -r '.summary.overallRiskLevel' tag-report.json)
    echo "Risk: $RISK | Review: $REVIEW"
    if [ "$REVIEW" = "SECURITY_REVIEW" ]; then
      echo "::warning::This PR requires security team review"
      # Add your label/assignment automation here
    fi
    if [ "$RISK" = "CRITICAL" ]; then
      echo "::error::CRITICAL risk detected — blocking auto-merge"
      exit 1
    fi
```

### What to wire up

- **Block auto-merge** when `summary.recommendedReviewRequirement` is `SECURITY_REVIEW` or `ARCH_REVIEW`
- **Select test suites** based on `summary.recommendedTestingDepth`: run the full E2E suite only when needed
- **Assign reviewers** based on `summary.primaryAttentionTypes`: route SECURITY changes to your security champions list
- **PR labels** based on `summary.overallRiskLevel`: visually flag HIGH/CRITICAL PRs in your code review tool

### Amazon/CRUX integration pattern

Run the tagger as part of your pre-CR pipeline script. Parse `summary.recommendedReviewRequirement` to decide whether to add a required-reviewer group to the CR, and parse `summary.recommendedTestingDepth` to decide which integration test suites to trigger.

## Design decisions

Tags are generated on-demand rather than stored in the repo. This avoids the maintenance burden of keeping metadata fresh as code evolves. Re-run on every PR diff; the analysis is fast and the output is disposable. The README (this file) is the only persistent artifact.

Granularity is per-method with a file-level and repo-level rollup. This gives both a quick headline decision (block or approve the PR?) and precise drill-down (which specific methods need a security expert?).

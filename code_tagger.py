#!/usr/bin/env python3
"""
code-tagger.py — Java code risk and metadata tagging utility.

Runs through the `claude` CLI (Claude Code) — no API key required.
Auth is handled natively by Claude Code.

Requires: Claude Code CLI installed and authenticated (`claude --version`).

Usage:
  python3 code-tagger.py --file path/to/MyClass.java
  python3 code-tagger.py --dir src/main/java/com/example
  python3 code-tagger.py --diff               # analyzes files changed vs HEAD
  python3 code-tagger.py --diff main           # analyzes files changed vs main

Output: JSON to stdout (pipe to a file or jq for CI consumption).
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap

ANALYSIS_PROMPT = textwrap.dedent("""\
    You are a senior software engineer performing risk-based code tagging for an AI-native
    development pipeline. Your tags drive PR routing, test-suite selection, and reviewer
    assignment. Be precise and conservative: when in doubt, escalate the risk level.

    Analyze the Java code below and tag every non-trivial method
    (skip getters/setters/equals/hashCode/toString).

    Tag each method across four dimensions:

    1. riskLevel — impact if this code breaks:
       LOW      Utility, logging, simple transforms, pure computation
       MEDIUM   Business logic, data processing, non-critical state changes
       HIGH     Core business flows, persistence, external service calls
       CRITICAL Security, auth, payments, PII handling, data deletion

    2. attentionTypes — specialist review needed (one or more):
       SECURITY     Auth, input validation, crypto, PII, ACLs, injection risk
       PERFORMANCE  Hot paths, complex algorithms, DB queries, caching, allocations
       STABILITY    Error handling, retries, timeouts, circuit breakers, idempotency
       CORRECTNESS  Business-logic edge cases, numeric precision, state transitions
       RELIABILITY  Fallbacks, distributed-system concerns, partial-failure modes

    3. reviewRequirement — PR approval gate:
       AUTO_MERGE      Trivial cosmetic/refactor, no logic change
       HUMAN_REVIEW    Standard peer review
       SECURITY_REVIEW Security-team sign-off required
       ARCH_REVIEW     Cross-team or architectural impact

    4. testingDepth — minimum test investment required:
       UNIT_ONLY    Pure functions, simple isolated logic
       INTEGRATION  Service interactions, DB calls, external APIs
       E2E          Critical user-facing flows, multi-service orchestration
       LOAD         Performance-sensitive or high-traffic code paths

    For each method also write a single-sentence `rationale` explaining the dominant concern.

    File: {filename}

    ```java
    {code}
    ```

    Respond with JSON only — no prose outside the JSON object:
    {{
      "file": "{filename}",
      "fileSummary": {{
        "overallRiskLevel": "<highest riskLevel across all methods>",
        "primaryAttentionTypes": ["<union of all attentionTypes>"],
        "recommendedReviewRequirement": "<strictest reviewRequirement>",
        "recommendedTestingDepth": "<deepest testingDepth>"
      }},
      "methods": [
        {{
          "name": "<methodName>",
          "className": "<ClassName>",
          "signature": "<return type + name + param types>",
          "riskLevel": "LOW|MEDIUM|HIGH|CRITICAL",
          "attentionTypes": ["<one or more>"],
          "reviewRequirement": "AUTO_MERGE|HUMAN_REVIEW|SECURITY_REVIEW|ARCH_REVIEW",
          "testingDepth": "UNIT_ONLY|INTEGRATION|E2E|LOAD",
          "rationale": "<one sentence>"
        }}
      ]
    }}
""")

RISK_ORDER   = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
REVIEW_ORDER = {"AUTO_MERGE": 0, "HUMAN_REVIEW": 1, "SECURITY_REVIEW": 2, "ARCH_REVIEW": 3}
DEPTH_ORDER  = {"UNIT_ONLY": 0, "INTEGRATION": 1, "E2E": 2, "LOAD": 3}


def check_claude_cli():
    try:
        subprocess.check_output(["claude", "--version"], stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        sys.exit(
            "Claude Code CLI not found. Install it from https://claude.ai/code "
            "and run `claude` once to authenticate."
        )


def call_claude(prompt: str) -> str:
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "claude CLI returned non-zero exit code")
    return result.stdout.strip()


def analyze_file(filepath: str, code: str) -> dict:
    filename = os.path.basename(filepath)
    prompt = ANALYSIS_PROMPT.format(code=code, filename=filename)

    raw = call_claude(prompt)

    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rstrip("`").rstrip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"file": filepath, "error": f"JSON parse failed: {exc}", "raw": raw}

    result["file"] = filepath
    return result


def collect_diff_files(ref: str | None) -> list[str]:
    cmd = ["git", "diff", "--name-only", "--diff-filter=ACM"]
    cmd.append(ref if ref else "HEAD")
    try:
        files = subprocess.check_output(cmd, text=True).strip().split("\n")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"git diff failed: {exc}")
    return [f for f in files if f.endswith(".java") and os.path.exists(f)]


def roll_up(file_results: list[dict]) -> dict:
    valid = [r for r in file_results if "error" not in r and r.get("fileSummary")]
    if not valid:
        return {}

    all_risks   = [r["fileSummary"]["overallRiskLevel"] for r in valid]
    all_reviews = [r["fileSummary"]["recommendedReviewRequirement"] for r in valid]
    all_depths  = [r["fileSummary"]["recommendedTestingDepth"] for r in valid]
    all_attention: set[str] = set()
    for r in valid:
        all_attention.update(r["fileSummary"].get("primaryAttentionTypes", []))

    return {
        "overallRiskLevel":            max(all_risks,   key=lambda x: RISK_ORDER.get(x, 0)),
        "primaryAttentionTypes":       sorted(all_attention),
        "recommendedReviewRequirement": max(all_reviews, key=lambda x: REVIEW_ORDER.get(x, 0)),
        "recommendedTestingDepth":     max(all_depths,  key=lambda x: DEPTH_ORDER.get(x, 0)),
        "filesAnalyzed":               len(valid),
        "filesWithErrors":             len(file_results) - len(valid),
    }


def main():
    parser = argparse.ArgumentParser(description="Tag Java code with risk and testing metadata.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", metavar="PATH", help="Single Java file to analyze")
    group.add_argument("--dir",  metavar="PATH", help="Directory to analyze recursively")
    group.add_argument("--diff", nargs="?", const="HEAD", metavar="REF",
                       help="Analyze Java files changed vs REF (default: HEAD)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    check_claude_cli()

    file_results = []

    if args.file:
        if not os.path.exists(args.file):
            sys.exit(f"File not found: {args.file}")
        print(f"  Tagging {args.file} …", file=sys.stderr)
        file_results.append(analyze_file(args.file, open(args.file).read()))

    elif args.dir:
        for root, _, files in os.walk(args.dir):
            for fname in files:
                if fname.endswith(".java"):
                    filepath = os.path.join(root, fname)
                    print(f"  Tagging {filepath} …", file=sys.stderr)
                    file_results.append(analyze_file(filepath, open(filepath).read()))

    else:
        java_files = collect_diff_files(args.diff if args.diff != "HEAD" else None)
        if not java_files:
            sys.exit("No Java files changed in diff.")
        for filepath in java_files:
            print(f"  Tagging {filepath} …", file=sys.stderr)
            file_results.append(analyze_file(filepath, open(filepath).read()))

    output = {"summary": roll_up(file_results), "files": file_results}
    print(json.dumps(output, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()

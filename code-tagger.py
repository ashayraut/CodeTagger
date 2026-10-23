#!/usr/bin/env python3
"""
code-tagger.py — Java code risk and metadata tagging utility.

Usage:
  python3 code-tagger.py --file path/to/MyClass.java
  python3 code-tagger.py --dir src/main/java/com/example
  python3 code-tagger.py --diff               # analyzes staged git diff
  python3 code-tagger.py --diff HEAD~1         # analyzes diff vs a ref

Output: JSON to stdout (pipe to a file or jq for CI consumption).
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap

try:
    import anthropic
except ImportError:
    sys.exit("anthropic package not found. Run: pip install anthropic")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = textwrap.dedent("""\
    You are a senior software engineer performing risk-based code tagging for an AI-native
    development pipeline. Your tags drive PR routing, test-suite selection, and reviewer
    assignment. Be precise and conservative: when in doubt, escalate the risk level.
""")

ANALYSIS_PROMPT = textwrap.dedent("""\
    Analyze the Java code below and tag every non-trivial method (skip getters/setters/equals/hashCode/toString).

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

    Code to analyze:
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

# ---------------------------------------------------------------------------
# Risk ordering helpers (for rolling up file summary)
# ---------------------------------------------------------------------------

RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
REVIEW_ORDER = {"AUTO_MERGE": 0, "HUMAN_REVIEW": 1, "SECURITY_REVIEW": 2, "ARCH_REVIEW": 3}
DEPTH_ORDER = {"UNIT_ONLY": 0, "INTEGRATION": 1, "E2E": 2, "LOAD": 3}

# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze_file(client: "anthropic.Anthropic", filepath: str, code: str) -> dict:
    filename = os.path.basename(filepath)
    prompt = ANALYSIS_PROMPT.format(code=code, filename=filename)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if model wraps the JSON
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rstrip("`").rstrip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"file": filepath, "error": f"JSON parse failed: {exc}", "raw": raw}

    result["file"] = filepath
    return result


def analyze_diff(client: "anthropic.Anthropic", ref: str | None) -> list[dict]:
    cmd = ["git", "diff", "--name-only", "--diff-filter=ACM"]
    if ref:
        cmd.append(ref)
    else:
        cmd.append("HEAD")

    try:
        files = subprocess.check_output(cmd, text=True).strip().split("\n")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"git diff failed: {exc}")

    java_files = [f for f in files if f.endswith(".java") and os.path.exists(f)]
    if not java_files:
        print("No Java files changed in diff.", file=sys.stderr)
        return []

    results = []
    for filepath in java_files:
        print(f"  Tagging {filepath} …", file=sys.stderr)
        code = open(filepath).read()
        results.append(analyze_file(client, filepath, code))
    return results


def analyze_dir(client: "anthropic.Anthropic", dirpath: str) -> list[dict]:
    results = []
    for root, _, files in os.walk(dirpath):
        for fname in files:
            if fname.endswith(".java"):
                filepath = os.path.join(root, fname)
                print(f"  Tagging {filepath} …", file=sys.stderr)
                code = open(filepath).read()
                results.append(analyze_file(client, filepath, code))
    return results


# ---------------------------------------------------------------------------
# Roll-up: aggregate a list of file results into a repo summary
# ---------------------------------------------------------------------------

def roll_up(file_results: list[dict]) -> dict:
    valid = [r for r in file_results if "error" not in r]
    if not valid:
        return {}

    all_risks = [r["fileSummary"]["overallRiskLevel"] for r in valid if r.get("fileSummary")]
    all_reviews = [r["fileSummary"]["recommendedReviewRequirement"] for r in valid if r.get("fileSummary")]
    all_depths = [r["fileSummary"]["recommendedTestingDepth"] for r in valid if r.get("fileSummary")]
    all_attention: set[str] = set()
    for r in valid:
        if r.get("fileSummary"):
            all_attention.update(r["fileSummary"].get("primaryAttentionTypes", []))

    highest_risk = max(all_risks, key=lambda x: RISK_ORDER.get(x, 0), default="LOW")
    strictest_review = max(all_reviews, key=lambda x: REVIEW_ORDER.get(x, 0), default="HUMAN_REVIEW")
    deepest_testing = max(all_depths, key=lambda x: DEPTH_ORDER.get(x, 0), default="UNIT_ONLY")

    return {
        "overallRiskLevel": highest_risk,
        "primaryAttentionTypes": sorted(all_attention),
        "recommendedReviewRequirement": strictest_review,
        "recommendedTestingDepth": deepest_testing,
        "filesAnalyzed": len(valid),
        "filesWithErrors": len(file_results) - len(valid),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Tag Java code with risk and testing metadata.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", metavar="PATH", help="Single Java file to analyze")
    group.add_argument("--dir", metavar="PATH", help="Directory to analyze recursively")
    group.add_argument("--diff", nargs="?", const="HEAD", metavar="REF",
                       help="Analyze Java files changed vs REF (default: HEAD)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY environment variable not set.")

    client = anthropic.Anthropic(api_key=api_key)

    if args.file:
        if not os.path.exists(args.file):
            sys.exit(f"File not found: {args.file}")
        print(f"  Tagging {args.file} …", file=sys.stderr)
        code = open(args.file).read()
        file_results = [analyze_file(client, args.file, code)]
    elif args.dir:
        file_results = analyze_dir(client, args.dir)
    else:
        file_results = analyze_diff(client, args.diff if args.diff != "HEAD" else None)

    output = {
        "summary": roll_up(file_results),
        "files": file_results,
    }

    indent = 2 if args.pretty else None
    print(json.dumps(output, indent=indent))


if __name__ == "__main__":
    main()

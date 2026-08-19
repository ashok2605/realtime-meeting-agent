#!/usr/bin/env bash
# Generate spoken test fixtures (16 kHz mono s16le WAV) with macOS `say`.
# These drive the integration tests and `gladiator bench --wav`.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p tests/fixtures

fixture() { # name text
  say -v Samantha -o "tests/fixtures/$1.wav" --data-format=LEI16@16000 "$2"
  echo "  tests/fixtures/$1.wav"
}

echo "recording fixtures:"
fixture hi_glad        "Hi Glad, can you hear me?"
fixture hi_gladiator   "Gladiator, are you there?"
fixture siebel_answer  "So today we run everything on Siebel C R M, and we want to move the whole sales team onto Salesforce."
fixture interrupt      "Hold on, stop for a second, let me ask something else."
fixture search_request "Glad, could you look up what Salesforce Automotive Cloud is?"
echo "done."

# MHS pilot: agentic AI on the production line

A pilot in which an AI agent directly operates and optimizes a small physical production process through Anthropic's
[Model Hardware Standard (MHS)](https://www.anthropic.com/news/model-hardware-standard-research-preview).

The test bed is a tabletop **precision-dispensing cell** (LEGO Technic motors, a Raspberry Pi and a load cell) that portions
dry grain by weight and returns every portion to the hopper, so it can run unattended. The agent tunes three settings
(feeder speed, feeder run time and conveyor speed) to find the fastest operation that still meets the weight specification
(Cpk ≥ 1.33), verifies the result statistically and re-calibrates when conditions change.

**Status:** proposal stage. The proposal and its planning material (digital twin plan, design image, concept video) are
kept out of version control (`proposal/` is git-ignored).

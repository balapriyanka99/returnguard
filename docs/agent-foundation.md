# ReturnGuard agent foundation

ReturnGuard agents sit above the existing MCP protocol boundary:

```text
FastAPI (later) -> Orchestrator -> specialists -> MCP Client -> MCP Server
                  -> deterministic Intelligence -> frozen BigQuery data
```

The internal Orchestrator coordinates case-processing specialists. Ask
ReturnGuard is a separate merchant-facing Investigation Copilot whose Gemini
model dynamically selects the smallest relevant subset of the seven approved
MCP tools.

Implemented bounded specialists are Customer Behavior, Product Intelligence,
and Inspection. They receive facts only through MCP. Network and Economics
remain deterministic capabilities. Evidence metadata retrieval exists, but the
Vision Agent is explicitly unavailable until the Cloud Storage and Gemini
multimodal pipeline is implemented. Decision & Policy is explicitly unavailable
until deterministic Risk and policy services exist.

Agents must not use evaluation-only fraud labels, invent missing facts, assign a
numeric risk score, treat network linkage as fraud, or manufacture policy. All
agent outputs use typed Pydantic contracts with compact tool provenance.

## Configuration

- `RETURNGUARD_GEMINI_MODEL` (default `gemini-2.5-flash`)
- `GOOGLE_GENAI_USE_VERTEXAI=true` to use Vertex AI
- `GOOGLE_CLOUD_PROJECT` (required for Vertex AI)
- `GOOGLE_CLOUD_LOCATION` (required for Vertex AI)
- Application Default Credentials or the standard Google Gen AI authentication
  environment must be configured outside the repository.

Python 3.11 or newer is required. Install the direct dependencies with:

```bash
python -m pip install -r requirements.txt
```

Run all local tests without BigQuery or Gemini:

```bash
python -m unittest discover -s tests
```

A credentialed MCP-to-frozen-BigQuery smoke test remains available as:

```bash
python scripts/smoke_test_mcp_protocol_live.py
```

That smoke script is read-only. Run the real Gemini agent smoke test with an
authoritative return ID and assessment timestamp:

```bash
python scripts/smoke_test_agents_live.py \
  --return-id RTN-S01-001 \
  --assessment-at 2026-09-02T23:59:59+00:00 \
  --question "Is this customer's behavior unusual?"
```

The agent smoke test is also read-only. Unit tests deliberately use no Gemini
or BigQuery credentials.

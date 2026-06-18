"""FraudCase AI — AI corporate-finance audit agent.

Package layout (each top-level module/dir is an independently-owned build slice):
  models     - shared Pydantic contracts (THE interface every slice codes against)
  config     - settings + MOCK/REAL switch
  tools/     - agent detectors: policy, ofac, dedup, risk triage
  agent/     - audit planning helpers, approval-gate state machine, report rendering
  uipath/    - UiPath Data Service + Context Grounding clients
  server/    - FastAPI bridge + SSE event stream
"""

__version__ = "0.1.0"

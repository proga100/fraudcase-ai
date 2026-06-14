"""FraudCase AI — AI corporate-finance audit agent.

Package layout (each top-level module/dir is an independently-owned build slice):
  models     - shared Pydantic contracts (THE interface every slice codes against)
  config     - settings + MOCK/REAL switch
  tools/     - agent tools: mongo reads, policy, ofac, dedup, flagging
  agent/     - audit planning helpers, approval-gate state machine, report rendering
  server/    - FastAPI bridge + SSE event stream
"""

__version__ = "0.1.0"
